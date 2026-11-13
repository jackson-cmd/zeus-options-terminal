"""Benzinga digest 本地存储的格式层(唯一入口)。

每天的原始数据存为:
  .YYYY-MM-DD.json       普通 JSON(daemon/server 写今天用)
  .YYYY-MM-DD.json.zst   zstd 无损压缩(compactor 把旧日子压成它)

读取端(optionstrat_server)同时认两种格式,**优先普通 .json**(= daemon 刚写的最新);
compactor 只把 daemon 不再触碰的旧日子压成 .zst。

无损保证:compress_day() 删原 .json 前,逐字节校验 decompress(.zst) == 原始 bytes
(内存里 + 落盘后各校验一次),任一不符就不删,只留日志。本模块顶层不 import 任何
第三方库(zstandard 惰性 import),所以 import 本模块永不失败。

calendar 计数走 .counts.json 索引(compactor 维护),避免每次解析全部历史(8GB)。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
from pathlib import Path

# 本模块在 benzinga_digest/src/benzinga_mcp/ → digests 在 benzinga_digest/digests
DIGEST_DIR = Path(__file__).resolve().parent.parent.parent / "digests"
COUNTS_INDEX = DIGEST_DIR / ".counts.json"
ZSTD_LEVEL = 19

_DAY_RE = re.compile(r"^\.(\d{4}-\d{2}-\d{2})\.json(\.zst)?$")
_index_lock = threading.Lock()


# ── 路径 ──────────────────────────────────────────────────────────────────
def _day(date) -> str:
    """YYYY-MM-DD rebuilt from the parsed date, so a date from a request can never carry a path."""
    return datetime.date.fromisoformat(str(date)).isoformat()


def _is_day(date) -> bool:
    try:
        _day(date)
        return True
    except ValueError:
        return False


def _month_dir(date: str) -> Path:
    return DIGEST_DIR / _day(date)[:7]


def plain_path(date: str) -> Path:
    return _month_dir(date) / f".{_day(date)}.json"


def zst_path(date: str) -> Path:
    return _month_dir(date) / f".{_day(date)}.json.zst"


def md_path(date: str) -> Path:
    return _month_dir(date) / f"{_day(date)}.md"


def day_path(date: str):
    """该天实际存在的数据文件:优先普通 .json,否则 .json.zst,都没有则 None。"""
    if not _is_day(date):
        return None
    p = plain_path(date)
    if p.exists():
        return p
    z = zst_path(date)
    if z.exists():
        return z
    return None


# ── 读 ────────────────────────────────────────────────────────────────────
def _zstd():
    import zstandard as zstd  # 惰性:import 本模块不需要 zstandard
    return zstd


def load_day(date: str):
    """返回该天的 item 列表(无则 None)。优先读普通 .json(最新),否则解压 .json.zst。"""
    if not _is_day(date):
        return None
    p = plain_path(date)
    if p.exists():
        return json.loads(p.read_bytes())
    z = zst_path(date)
    if z.exists():
        raw = _zstd().ZstdDecompressor().decompress(z.read_bytes())
        return json.loads(raw)
    return None


def list_days() -> list:
    """所有可用日期(YYYY-MM-DD),新→旧。只 glob 文件名,不解析内容。"""
    out = set()
    try:
        for fp in DIGEST_DIR.glob("*/.20*.json*"):
            m = _DAY_RE.match(fp.name)
            if m:
                out.add(m.group(1))
    except Exception:
        pass
    return sorted(out, reverse=True)


def day_count(date: str) -> int:
    d = load_day(date)
    return len(d) if isinstance(d, list) else 0


# ── 计数索引(给日历用,避免解析全部历史) ──────────────────────────────────
def load_counts_index() -> dict:
    try:
        return json.loads(COUNTS_INDEX.read_bytes())
    except Exception:
        return {}


def save_counts_index(idx: dict) -> None:
    with _index_lock:
        _atomic_write_bytes(COUNTS_INDEX, json.dumps(idx, ensure_ascii=False).encode("utf-8"))


# ── 压缩原语(compact.py 用) ───────────────────────────────────────────────
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def compress_day(date: str, *, keep_original: bool = False,
                 delete_md: bool = True, level: int = ZSTD_LEVEL) -> dict:
    """无损把某天的普通 .json 压成 .json.zst:
    读 → 解析(取条数) → 压缩 → 内存校验 decompress==原始 → 原子落盘 →
    落盘后再校验 == 原始 → 只有都通过才删普通 .json(除非 keep_original)与 .md。
    任何一步异常/不符:不删任何东西,返回非 ok 状态。
    """
    p = plain_path(date)
    z = zst_path(date)
    if not p.exists():
        if z.exists():
            return {"date": date, "status": "already", "count": day_count(date)}
        return {"date": date, "status": "missing"}
    raw = p.read_bytes()
    try:
        obj = json.loads(raw)
    except Exception as e:
        return {"date": date, "status": "parse_error", "error": str(e)}
    if not isinstance(obj, list):
        return {"date": date, "status": "not_a_list"}
    count = len(obj)
    try:
        zstd = _zstd()
        comp = zstd.ZstdCompressor(level=level).compress(raw)
        if zstd.ZstdDecompressor().decompress(comp) != raw:
            return {"date": date, "status": "verify_fail_mem"}
        _atomic_write_bytes(z, comp)
        if zstd.ZstdDecompressor().decompress(z.read_bytes()) != raw:
            return {"date": date, "status": "verify_fail_disk"}
    except Exception as e:
        return {"date": date, "status": "error", "error": str(e)}
    # 校验全过 → 删原文件
    deleted_md = False
    if not keep_original:
        os.remove(p)
    if delete_md:
        mp = md_path(date)
        if mp.exists():
            os.remove(mp)
            deleted_md = True
    return {"date": date, "status": "ok", "count": count,
            "in": len(raw), "out": len(comp),
            "kept_original": keep_original, "deleted_md": deleted_md}

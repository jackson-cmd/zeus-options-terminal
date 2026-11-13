"""Finviz AI「为何异动」新闻的本地每日库(独立于 Benzinga digest)。

为什么独立:Benzinga digest 的 digests/ 目录由 digest daemon 独占写(并发写会撞车,
且约定"别碰 daemon")。所以 finviz AI news 存到**同级的 finviz_ai/ 目录**,格式/布局
完全照搬 digest_store(每月一目录、隐藏文件名、优先 .json 否则 .json.zst),这样既"每天
存到一个地方、可筛选",又不和 daemon 抢同一批文件。

每天:
  finviz_ai/YYYY-MM/.YYYY-MM-DD.json       普通 JSON(crawler 当天多次 upsert)
  finviz_ai/YYYY-MM/.YYYY-MM-DD.json.zst   zstd(可选, 旧日子压缩; reader 也认)

每个 item 是一只票的 finviz AI 条目(schema 由 crawler 定),按 ticker 去重 upsert。
本模块顶层不 import 第三方库(zstandard 惰性),import 本模块永不失败。
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

# 本模块在 benzinga_digest/src/benzinga_mcp/ → 库在 benzinga_digest/finviz_ai
FINVIZ_AI_DIR = Path(__file__).resolve().parent.parent.parent / "finviz_ai"
ZSTD_LEVEL = 19

_DAY_RE = re.compile(r"^\.(\d{4}-\d{2}-\d{2})\.json(\.zst)?$")


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
    return FINVIZ_AI_DIR / _day(date)[:7]


def plain_path(date: str) -> Path:
    return _month_dir(date) / f".{_day(date)}.json"


def zst_path(date: str) -> Path:
    return _month_dir(date) / f".{_day(date)}.json.zst"


def day_path(date: str):
    if not _is_day(date):
        return None
    p = plain_path(date)
    if p.exists():
        return p
    z = zst_path(date)
    return z if z.exists() else None


# ── 读 ────────────────────────────────────────────────────────────────────
def _zstd():
    import zstandard as zstd          # 惰性
    return zstd


def load_day(date: str):
    """该天的 item 列表(无则 None)。优先普通 .json,否则解压 .json.zst。"""
    if not _is_day(date):
        return None
    p = plain_path(date)
    if p.exists():
        try:
            return json.loads(p.read_bytes())
        except Exception:
            return None
    z = zst_path(date)
    if z.exists():
        try:
            raw = _zstd().ZstdDecompressor().decompress(z.read_bytes())
            return json.loads(raw)
        except Exception:
            return None
    return None


def list_days() -> list:
    """所有可用日期(YYYY-MM-DD),新→旧。只 glob 文件名。"""
    out = set()
    try:
        for fp in FINVIZ_AI_DIR.glob("*/.20*.json*"):
            m = _DAY_RE.match(fp.name)
            if m:
                out.add(m.group(1))
    except Exception:
        pass
    return sorted(out, reverse=True)


# ── 写 ────────────────────────────────────────────────────────────────────
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_day(date: str, items: list) -> int:
    """整天覆盖写普通 .json(原子)。返回条数。"""
    data = json.dumps(items, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(plain_path(date), data)
    return len(items)


def upsert_day(date: str, new_items: list, key: str = "ticker") -> int:
    """按 key 合并 upsert(新条目覆盖同 key 旧条目),其余保留。返回合并后总条数。
    crawler 每次只交付本轮抓到的票;没抓到的票保留上一轮(慢速分多轮也能凑齐)。"""
    existing = load_day(date) or []
    by_key = {}
    for it in existing:
        k = it.get(key)
        if k is not None:
            by_key[k] = it
    for it in (new_items or []):
        k = it.get(key)
        if k is not None:
            by_key[k] = it
    merged = list(by_key.values())
    save_day(date, merged)
    return len(merged)

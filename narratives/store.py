"""多空叙事每日库(独立 zstd 库, 仿 finviz_ai_store / digest_store)。

布局: benzinga_digest/narratives/YYYY-MM/.YYYY-MM-DD.json(.zst)
每个 item 是一只票的多空叙事, 按 ticker upsert(本轮没重算的票保留上轮)。
顶层不 import 第三方库(zstandard 惰性), import 永不失败。
"""
from __future__ import annotations
import json, os, re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NARR_DIR = REPO / "benzinga_digest" / "narratives"
ZSTD_LEVEL = 19
_DAY_RE = re.compile(r"^\.(\d{4}-\d{2}-\d{2})\.json(\.zst)?$")


def _month_dir(date: str) -> Path:
    return NARR_DIR / date[:7]


def plain_path(date: str) -> Path:
    return _month_dir(date) / f".{date}.json"


def zst_path(date: str) -> Path:
    return _month_dir(date) / f".{date}.json.zst"


def _zstd():
    import zstandard as zstd
    return zstd


def load_day(date: str):
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
    out = set()
    try:
        for fp in NARR_DIR.glob("*/.20*.json*"):
            m = _DAY_RE.match(fp.name)
            if m:
                out.add(m.group(1))
    except Exception:
        pass
    return sorted(out, reverse=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save_day(date: str, items: list) -> int:
    data = json.dumps(items, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(plain_path(date), data)
    return len(items)


def upsert_day(date: str, new_items: list, key: str = "ticker") -> int:
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

"""Digest ❤️ 喜欢(收藏)存储 — 纯函数,不碰 FastAPI。

服务端把喜欢的新闻快照存进 options_analyzer/data/digest_liked.json,跨"已读"、跨天、
甚至原 digest 被压缩/滚走后都还在。仿 credit_spread_seen 套路(纯函数 + 显式传时间戳,
便于单测)。见 spec docs/superpowers/specs/2026-07-20-digest-liked-design.md
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path

# 快照保留够"独立渲染一行 + 点开原文"的字段(别把整篇正文塞进来 → best-effort 补)。
_SNAP_FIELDS = ("url", "title", "title_cn", "source", "ts", "hhmm", "_date",
                "sentiment", "tickers", "channels", "_bilingual", "teaser")


def item_key(item: dict):
    """稳定 key:优先 url,退回 '_date#id',皆无 → None(不可收藏)。"""
    url = (item.get("url") or "").strip()
    if url:
        return url
    _id = item.get("id")
    if _id is not None and item.get("_date"):
        return f"{item['_date']}#{_id}"
    return None


def load(path: str) -> dict:
    """读 store;文件缺失/坏 JSON/结构不对 → {'items': {}}。"""
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception:
        pass
    return {"items": {}}


def add(store: dict, item: dict, liked_at: float):
    """算 key、写快照(含 key + liked_at)。无 key → 原样返回 + None。幂等覆盖同 key。"""
    key = item_key(item)
    if key is None:
        return store, None
    snap = {f: item.get(f) for f in _SNAP_FIELDS if item.get(f) is not None}
    snap["key"] = key
    snap["liked_at"] = liked_at
    store.setdefault("items", {})[key] = snap
    return store, key


def remove(store: dict, key: str) -> dict:
    """删 key;不存在则 no-op。"""
    store.setdefault("items", {}).pop(key, None)
    return store


def clear(store: dict) -> dict:
    store["items"] = {}
    return store


def list_items(store: dict) -> list:
    """按 liked_at 倒序(最近喜欢在前)。"""
    items = list(store.get("items", {}).values())
    items.sort(key=lambda it: it.get("liked_at", 0), reverse=True)
    return items


def save(path: str, store: dict) -> None:
    """原子写(tmp + rename),避免并发/崩溃写坏。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise

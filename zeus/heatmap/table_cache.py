"""Disk + memory cache for the slow heatmap tables: stock_perf (1 day), etf_trends
(6 hours) and risk_stats (1 week).

Each table comes from one large yfinance download and is kept for hours to a week,
so a bad download must not replace a good table:

* A refresh that brings back no data is never written to disk. The last good table
  (from memory, else from disk) is served instead, even when it is older than its
  TTL, and the refresh is tried again after RETRY_SECONDS. With no good table yet,
  the empty result is served for RETRY_SECONDS only. Other readers of the disk file
  (market_brief reads etf_trends.json) therefore keep the last good table as well.
* The tables refresh one at a time. yfinance.download keeps the state of a call in
  module globals (yfinance.shared._DFS), so downloads that overlap in different
  threads lose each other's tickers or fail with "dictionary changed size during
  iteration". The server starts all three refreshes at the same moment on startup.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

RETRY_SECONDS = 5 * 60     # a refresh that brought back no data is tried again after this
LOCK_WAIT_SECONDS = 120    # wait this long for a refresh in progress, then refresh anyway

_refresh_lock = threading.Lock()


def has_data(payload) -> bool:
    """True when the table holds at least one ticker with prices."""
    if not isinstance(payload, dict):
        return False
    return any(isinstance(it, dict) and it.get("has_data", True)
               for it in payload.get("items") or [])


def _read(path: Path) -> Optional[dict]:
    """The table on disk, or None when it is missing, unreadable or has no data."""
    try:
        disk = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return disk if has_data(disk) else None


def _write(path: Path, payload: dict) -> None:
    """Replace the file in one step, so a reader never sees half a table."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _fresh(state: dict, path: Path, ttl: float) -> Optional[dict]:
    """The cached table while it is still valid, else None."""
    now = time.time()
    entry = state.get("entry")
    if entry is not None and now < entry[1]:
        return entry[0]
    disk = _read(path)
    if disk is not None and now - disk.get("_ts", 0) < ttl:
        state["entry"] = (disk, disk.get("_ts", 0) + ttl)
        return disk
    return None


def _last_good(state: dict, path: Path) -> Optional[dict]:
    entry = state.get("entry")
    good = [p for p in ((entry[0] if entry else None), _read(path)) if has_data(p)]
    return max(good, key=lambda p: p.get("_ts", 0)) if good else None


def _refresh(state: dict, path: Path, ttl: float, compute: Callable[[], dict], tag: str) -> dict:
    now = time.time()
    error = None
    try:
        payload = compute()
    except Exception as e:
        payload, error = None, e
        print(f"[{tag}] refresh failed: {e}")
    if has_data(payload):
        payload["_ts"] = now
        _write(path, payload)
        state["entry"] = (payload, now + ttl)
        return payload
    last = _last_good(state, path)
    if last is not None:
        print(f"[{tag}] refresh brought back no data; keeping the table from "
              f"{last.get('generated_at', '?')}, next try in {RETRY_SECONDS // 60} min")
        state["entry"] = (last, now + RETRY_SECONDS)
        return last
    if error is not None:
        raise error
    payload["_ts"] = now
    state["entry"] = (payload, now + RETRY_SECONDS)
    return payload


def get(state: dict, path: Path, ttl: float, compute: Callable[[], dict],
        force: bool = False, tag: str = "heatmap") -> dict:
    """The table cached in `state` (memory) and at `path` (disk); `compute()` builds
    it again when forced, missing or older than `ttl` seconds."""
    if not force:
        hit = _fresh(state, path, ttl)
        if hit is not None:
            return hit
    locked = _refresh_lock.acquire(timeout=LOCK_WAIT_SECONDS)
    if not locked:
        print(f"[{tag}] another table is still refreshing after {LOCK_WAIT_SECONDS}s; refreshing anyway")
    try:
        if not force:
            hit = _fresh(state, path, ttl)   # refreshed by another thread while we waited
            if hit is not None:
                return hit
        return _refresh(state, path, ttl, compute, tag)
    finally:
        if locked:
            _refresh_lock.release()

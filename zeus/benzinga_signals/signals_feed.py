"""Persistent Benzinga signals WS → ring buffer + 7-day local parquet history.

One background task keeps the WS open (reconnecting with backoff) and appends
every pushed signal to a bounded deque AND a rolling 7-day parquet
(data/signals/signals_history.parquet). On startup the last 7 days are
preloaded from that parquet into the buffer, so the tab shows "past week".

NOTE: Benzinga exposes no historical-signals API — only the live WS. So the
past week can only be ACCUMULATED going forward; we can't retroactively fetch
signals from before this feed first ran.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections import deque
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benzinga_api import signals as _sig  # noqa: E402

_HISTORY_FILE = _REPO / "data" / "signals" / "signals_history.parquet"
_WEEK_MS = 7 * 86_400 * 1000


class SignalsFeed:
    """Live WS connection + ring buffer + 7-day persisted history."""

    def __init__(self, maxlen: int = 8000):
        self.buf: deque = deque(maxlen=maxlen)
        self._keys: set = set()
        self.connected = False
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._seq = 0
        self._dirty = False

    # ---- dedup (survives restart: content hash + minute bucket) ----
    def _key(self, ev: dict) -> str | None:
        try:
            base = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
            h = hashlib.md5(json.dumps(base, sort_keys=True, default=str).encode()).hexdigest()[:16]
            return f"{h}:{int(ev.get('_recv_ms', 0)) // 60000}"
        except Exception:
            return None

    def _add(self, ev: dict) -> bool:
        if not isinstance(ev, dict):
            ev = {"value": ev}
        k = self._key(ev)
        if k is not None:
            if k in self._keys:
                return False
            self._keys.add(k)
        self.buf.append(ev)
        self._dirty = True
        return True

    # ---- history persistence ----
    def _load_history(self):
        if not _HISTORY_FILE.exists():
            return
        try:
            recs = json.loads(pd.read_parquet(_HISTORY_FILE).to_json(orient="records"))
        except Exception as e:
            self.last_error = f"history load: {e}"
            return
        cutoff = int(time.time() * 1000) - _WEEK_MS
        recs.sort(key=lambda e: e.get("_recv_ms") or 0)
        for ev in recs:
            if (ev.get("_recv_ms") or 0) >= cutoff:
                self._add(ev)

    def _flush(self):
        if not self._dirty:
            return
        cutoff = int(time.time() * 1000) - _WEEK_MS
        recent = [e for e in self.buf if (e.get("_recv_ms") or 0) >= cutoff]
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(recent).to_parquet(_HISTORY_FILE, index=False)
            self._dirty = False
        except Exception as e:
            self.last_error = f"history flush: {e}"

    # ---- reads ----
    def recent(self, limit: int = 300) -> list[dict]:
        items = sorted(self.buf, key=lambda e: e.get("_recv_ms") or 0, reverse=True)
        return items[:limit]

    def status(self) -> dict:
        return {"connected": self.connected, "buffered": len(self.buf),
                "last_error": self.last_error, "seq": self._seq}

    # ---- live WS loop ----
    async def _run(self):
        self._load_history()
        backoff = 3
        while True:
            try:
                self.connected = True
                self.last_error = None
                backoff = 3
                async for ev in _sig.stream():
                    self._seq += 1
                    if isinstance(ev, dict):
                        ev = dict(ev)
                        ev["_seq"] = self._seq
                        ev["_recv_ms"] = int(time.time() * 1000)
                    self._add(ev)
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(60)
            self._flush()

    def start(self):
        loop = asyncio.get_event_loop()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_loop())
        return self._task


# Module-level singleton shared by the API + server startup.
FEED = SignalsFeed()

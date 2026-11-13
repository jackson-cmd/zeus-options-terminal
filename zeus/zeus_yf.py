"""One yfinance download at a time, for the whole server process.

yfinance.download() keeps the state of a call in module globals
(yfinance.shared._DFS, _ERRORS, _TRACEBACKS) and clears them when a call starts
(yfinance 0.2.65). Downloads that overlap in different threads therefore lose
their own tickers, come back with another call's tickers, or fail with
"dictionary changed size during iteration", and a table built from such a
download stays cached for hours. So every yf.download in ZEUS goes through
download() below, which takes the same arguments:

    import zeus_yf
    df = zeus_yf.download(tickers, period="5d", progress=False)

* Downloads run one at a time. The others wait their turn in arrival order, so a
  thread that downloads a long list chunk by chunk cannot starve a single quote.
* A download that waited WAIT_SECONDS without getting its turn raises
  DownloadBusy (a TimeoutError); callers already treat a failed download as
  missing data. A wait of SLOW_WAIT_SECONDS or more is printed with its caller.
* acquire() / release() hold the turn for a block that checks a cache and then
  downloads, so a thread that waited finds the fresh result instead of
  downloading again (heatmap/table_cache.py). Downloads inside the block, in the
  same thread, go straight through.
* Call it from a worker thread. A thread that runs an asyncio event loop (the
  server's) never waits: while another download runs it gets DownloadBusy at
  once, and the call site is printed once, so the server cannot freeze.
* The lock is unique in the process even when this file is imported under two
  names (zeus_yf and zeus.zeus_yf): it is kept in sys.modules under one key.

tests/test_yf_download_lock.py fails when code calls yf.download directly.
"""
from __future__ import annotations

import asyncio
import collections
import sys
import threading
import time
import traceback
import types
from pathlib import Path

WAIT_SECONDS = 300.0        # longest wait for the downloads ahead, then DownloadBusy
SLOW_WAIT_SECONDS = 30.0    # print a line for a wait at least this long
_SHARED = "_zeus_yf_shared"  # sys.modules key of the state every import of this file shares


class _FairLock:
    """Re-entrant lock that goes to the waiting threads in arrival order."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner: int | None = None
        self._depth = 0
        self._queue: collections.deque = collections.deque()

    def acquire(self, wait: float) -> bool:
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return True
            if self._owner is None and not self._queue:
                self._owner, self._depth = me, 1
                return True
            if wait <= 0:
                return False
            ticket = object()
            self._queue.append(ticket)
            got = False
            try:
                got = self._cond.wait_for(
                    lambda: self._owner is None and self._queue[0] is ticket, wait)
            finally:
                if got:
                    self._queue.popleft()
                    self._owner, self._depth = me, 1
                else:
                    self._queue.remove(ticket)
                    self._cond.notify_all()      # the next in line may be first now
            return got

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("zeus_yf.release() by a thread that does not hold the turn")
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._cond.notify_all()


class DownloadBusy(TimeoutError):
    """The downloads ahead did not finish within the wait."""


def _shared() -> types.ModuleType:
    shared = sys.modules.get(_SHARED)
    if shared is None:
        fresh = types.ModuleType(_SHARED, "State of zeus_yf, shared by every import of it.")
        fresh.lock = _FairLock()
        fresh.DownloadBusy = DownloadBusy
        fresh.warned = set()
        shared = sys.modules.setdefault(_SHARED, fresh)
    return shared


_STATE = _shared()
DownloadBusy = _STATE.DownloadBusy          # noqa: F811  one class for every import of this file


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _caller() -> str:
    """file:line of the code that asked for the turn."""
    for frame in reversed(traceback.extract_stack()):
        name = Path(frame.filename).name
        if name != "zeus_yf.py":
            return f"{name}:{frame.lineno}"
    return "?"


def acquire(lock_wait: float | None = None) -> bool:
    """Wait for this thread's turn to download, up to `lock_wait` seconds
    (WAIT_SECONDS by default). False when the wait ran out; on a thread that runs
    an event loop it does not wait at all. Re-entrant; pair with release()."""
    wait = WAIT_SECONDS if lock_wait is None else max(0.0, float(lock_wait))
    if _on_event_loop():
        site = _caller()
        if site not in _STATE.warned:
            _STATE.warned.add(site)
            print(f"[yfinance] download on the event loop at {site}: it does not wait there; "
                  "run it in a worker thread")
        wait = 0.0
    start = time.monotonic()
    got = _STATE.lock.acquire(wait)
    waited = time.monotonic() - start
    if waited >= SLOW_WAIT_SECONDS:
        print(f"[yfinance] {_caller()} waited {waited:.0f}s for the downloads ahead of it"
              + ("" if got else "; giving up"))
    return got


def release() -> None:
    """End the turn taken with acquire()."""
    _STATE.lock.release()


def download(*args, lock_wait: float | None = None, **kwargs):
    """yfinance.download(*args, **kwargs), one at a time in the process.
    Raises DownloadBusy when its turn did not come within `lock_wait` seconds."""
    import yfinance as yf
    if not acquire(lock_wait):
        raise DownloadBusy("another yfinance download is still running")
    try:
        return yf.download(*args, **kwargs)
    finally:
        release()

"""Ctrl+C and SIGTERM stop the server within seconds, even while long jobs run.

uvicorn stops serving at once, but Python then waits for every job still running in a
worker thread: asyncio.run() waits for its default executor (up to 300 s on Python 3.12
and later, without a limit before), and the interpreter waits for every
ThreadPoolExecutor thread before it exits. A cold start runs jobs that take minutes,
such as the heatmap earnings refresh (134 tickers) and the Finviz crawl (399 tickers),
so Ctrl+C used to leave the server running for minutes.

The server starts uvicorn through serve() below, which takes the same arguments as
uvicorn.run(). From the first Ctrl+C or SIGTERM:

* stopping() is True. Long jobs check it between tickers and return early; they keep
  what they finished and write nothing half done.
* Open requests get GRACEFUL_SECONDS to finish, as uvicorn's timeout_graceful_shutdown.
* EXIT_SECONDS after the signal the process ends with os._exit() if it is still
  running, whatever threads are still busy. The files these jobs write are written to
  a temporary file and renamed into place, so a job cut off at that moment leaves the
  previous file, never half of one.

The state is unique in the process even when this file is imported under two names
(zeus_shutdown and zeus.zeus_shutdown): it is kept in sys.modules under one key.
tests/test_shutdown.py starts a server with a job that never returns and sends it
Ctrl+C and SIGTERM.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
import types

GRACEFUL_SECONDS = 3.0      # open requests get this long to finish
EXIT_SECONDS = 5.0          # the process is gone this long after Ctrl+C / SIGTERM at the latest
_SHARED = "_zeus_shutdown_shared"   # sys.modules key of the state every import of this file shares


def _shared() -> types.ModuleType:
    shared = sys.modules.get(_SHARED)
    if shared is None:
        fresh = types.ModuleType(_SHARED, "State of zeus_shutdown, shared by every import of it.")
        fresh.since = None          # time.monotonic() of the first stop request
        fresh.signal = None         # the signal that asked for it, if any
        shared = sys.modules.setdefault(_SHARED, fresh)
    return shared


_STATE = _shared()


def stopping() -> bool:
    """True once the server has been asked to stop. Long loops check it between items
    and return what they have."""
    return _STATE.since is not None


def request_stop(sig: int | None = None) -> None:
    """Ask long jobs to stop. Takes no lock, so a signal handler may call it."""
    if _STATE.since is None:
        _STATE.signal = sig
        _STATE.since = time.monotonic()


def _exit_code() -> int:
    sig = _STATE.signal
    return 0 if sig in (None, signal.SIGINT) else 128 + int(sig)


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _cut_off(limit: float) -> None:
    """Runs in a daemon thread: ends the process `limit` seconds after the stop request
    unless it has ended by itself."""
    while _STATE.since is None:
        time.sleep(0.1)
    left = limit - (time.monotonic() - _STATE.since)
    if left > 0:
        time.sleep(left)
    try:
        print(f"[ZEUS] stopped: jobs still running {limit:.0f} s after the stop request were cut off",
              file=sys.stderr)
    except Exception:
        pass
    flusher = threading.Thread(target=_flush, daemon=True)  # a stuck pipe must not stop the exit
    flusher.start()
    flusher.join(1.0)
    os._exit(_exit_code())


def serve(app, host: str, port: int, *, log_level: str = "warning",
          graceful: float = GRACEFUL_SECONDS, exit_after: float = EXIT_SECONDS) -> None:
    """uvicorn.run(app, host=host, port=port, log_level=log_level) that ends the process
    within `exit_after` seconds of Ctrl+C or SIGTERM. Call it from the main thread."""
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            request_stop(sig)
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, reload=False,
                            timeout_graceful_shutdown=graceful)
    server = _Server(config)
    threading.Thread(target=_cut_off, args=(exit_after,), name="zeus-exit", daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:       # uvicorn re-raises the Ctrl+C after shutting down
        pass
    if not server.started:
        sys.exit(3)                 # uvicorn's code for a failed start, as uvicorn.run() exits

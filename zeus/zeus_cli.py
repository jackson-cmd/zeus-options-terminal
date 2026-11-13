"""The `zeus` command of a pip / pipx install:  pipx install zeus-options-terminal  →  zeus

It starts the same server as start.sh, from the zeus/ folder inside the installed package, and
opens it in the browser once it answers. Settings and data stay in ~/.zeus-terminal (or
$ZEUS_HOME), so upgrading with `pipx upgrade zeus-options-terminal` keeps them.

    zeus                  start on the port set in Settings (8765 by default) and open the browser
    zeus --port 8790      use another port
    zeus --no-browser     start without opening a browser window
    zeus --version        print the version
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent                     # the zeus/ folder
SERVER = HERE / "options_analyzer" / "optionstrat_server.py"


def _settings():
    sys.path.insert(0, str(HERE))
    import zeus_config
    return zeus_config


def _open_when_ready(port: int, timeout: float = 120.0) -> None:
    """Open the browser once the server answers, as start.sh does a few seconds after starting."""
    url = f"http://127.0.0.1:{port}/api/version"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            webbrowser.open(f"http://localhost:{port}/")
            return
        except Exception:
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):                 # Windows writes pipes in cp1252, which
        try:                                                # has no 宙斯 and no →
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg = _settings()
    parser = argparse.ArgumentParser(
        prog="zeus", description="ZEUS Terminal · 宙斯期权终端: the self-hosted options trading terminal.")
    parser.add_argument("--port", type=int, help="port to listen on (default: the one in Settings, else 8765)")
    parser.add_argument("--no-browser", action="store_true", help="start without opening a browser window")
    parser.add_argument("--version", action="version", version=f"ZEUS Terminal {cfg.APP_VERSION}")
    args = parser.parse_args(argv)

    port = args.port or int(os.environ.get("PORT") or cfg.get("port") or 8765)
    env = dict(os.environ, PORT=str(port), ZEUS_PYTHON=sys.executable)
    print(f"ZEUS Terminal {cfg.APP_VERSION} → http://localhost:{port}/   (Ctrl+C stops it)", flush=True)
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()
    proc = subprocess.Popen([sys.executable, str(SERVER)], cwd=str(HERE.parent), env=env)
    try:                                                    # `kill` of zeus stops the server too
        signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    except (ValueError, OSError):
        pass
    try:
        return proc.wait()
    except KeyboardInterrupt:                               # Ctrl+C reaches the server too
        try:
            return proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            return proc.wait()


if __name__ == "__main__":
    sys.exit(main())

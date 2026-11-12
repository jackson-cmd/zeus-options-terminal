#!/usr/bin/env bash
# ZEUS Terminal (宙斯期权终端) — one-command start (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "▶ creating virtual environment (.venv)…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  echo "▶ installing requirements (first run takes a minute)…"
  .venv/bin/python -m pip install -r requirements.txt
fi
export ZEUS_PYTHON="$(pwd)/.venv/bin/python"
if [ -z "${PORT:-}" ]; then   # the port set in ⚙ Settings, else 8765
  PORT="$(.venv/bin/python -c 'import zeus_config as c; print(c.get("port") or 8765)' 2>/dev/null || echo 8765)"
fi
( sleep 4; (command -v open >/dev/null && open "http://localhost:${PORT}/") || (command -v xdg-open >/dev/null && xdg-open "http://localhost:${PORT}/") || true ) &
exec .venv/bin/python options_analyzer/optionstrat_server.py

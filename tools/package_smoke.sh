#!/usr/bin/env bash
# Install a built wheel in a clean virtual environment and start ZEUS from it, the way
#   pipx install zeus-options-terminal  →  zeus
# does for a user. CI runs it on every pull request, and the Release workflow runs it again
# before the upload to PyPI, so a wheel that does not start never reaches PyPI.
# Usage: tools/package_smoke.sh dist/zeus_options_terminal-<version>-py3-none-any.whl
set -euo pipefail
wheel="$1"
work="$(mktemp -d)"
port="${ZEUS_SMOKE_PORT:-8799}"
export ZEUS_HOME="$work/home"                       # never the real ~/.zeus-terminal
python -m venv "$work/venv"
"$work/venv/bin/python" -m pip install -q "$wheel"
echo "installed: $("$work/venv/bin/zeus" --version)"

"$work/venv/bin/zeus" --no-browser --port "$port" > "$work/zeus.log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
for _ in $(seq 90); do
  curl -fs "http://127.0.0.1:$port/api/version" > /dev/null && break
  kill -0 "$pid" 2>/dev/null || { cat "$work/zeus.log"; echo "zeus stopped before it answered"; exit 1; }
  sleep 2
done
for page in / /static/settings.html /ta/ /api/settings; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$page")"
  echo "$page → $code"
  [ "$code" = 200 ] || { cat "$work/zeus.log"; exit 1; }
done
if grep -q Traceback "$work/zeus.log"; then cat "$work/zeus.log"; exit 1; fi
site="$("$work/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
links="$(find "$site/zeus" -maxdepth 3 -type l | wc -l | tr -d ' ')"
echo "runtime links under the installed zeus/: $links"
[ "$links" -gt 0 ]
echo "✓ the wheel installs and ZEUS starts from it"

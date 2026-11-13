#!/usr/bin/env bash
# Start a ZEUS image the way the manual says and check that it works:
#
#   tools/docker_smoke.sh <image>          e.g. tools/docker_smoke.sh zeus-smoke:ci
#
# .github/workflows/docker.yml runs it before an image is pushed. It checks that the page, the
# settings page and the status API answer on 127.0.0.1; that the localhost checks (DNS-rebinding
# guard, CORS) still refuse other sites; that the sample portfolio loads and survives a restart;
# that the app runs as a non-root user with read-only code and its data in /data; and that MCP
# works through docker exec, is refused over HTTP from the host, and answers over HTTP only in a
# container started with -e ZEUS_MCP_FROM_HOST=1.
set -euo pipefail

IMAGE="${1:?usage: tools/docker_smoke.sh <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # the repository, for server.json
PORT="${SMOKE_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
NAME=zeus                               # the --name the manual uses; Settings shows commands with it
VOLUME="zeus-smoke-$$"
TMP="$(mktemp -d)"

cleanup() {
  docker rm -f "$NAME" zeus-smoke-http >/dev/null 2>&1 || true
  docker volume rm -f "$VOLUME" "$VOLUME-http" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
ok() { echo "✓ $*"; }
fail() {
  echo "✗ $*"
  echo "---- container log (last 80 lines)"
  docker logs "$NAME" 2>&1 | tail -80 || true
  exit 1
}
json() {  # json <file> <python expression on d>: prints the value
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1], encoding="utf-8")); print(eval(sys.argv[2]))' "$1" "$2"
}
wait_up() {  # wait_up <base url>: up to 3 minutes
  for _ in $(seq 1 90); do
    curl -fsS -o /dev/null "$1/api/version" 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

echo "▶ docker run -d --name $NAME -p 127.0.0.1:$PORT:8765 -v $VOLUME:/data $IMAGE"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8765" -v "$VOLUME:/data" "$IMAGE" >/dev/null
wait_up "$URL" || fail "the server did not answer within 3 minutes"
ok "server answers on $URL"

# ---- pages and the status API
code=$(curl -s -o "$TMP/index.html" -w '%{http_code}' "$URL/")
[ "$code" = 200 ] && grep -q "ZEUS" "$TMP/index.html" || fail "GET / answered $code"
ok "GET / → 200 ($(wc -c < "$TMP/index.html") bytes)"
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/settings")
[ "$code" = 200 ] || fail "GET /settings answered $code"
ok "GET /settings → 200"
code=$(curl -s -o "$TMP/status.json" -w '%{http_code}' "$URL/api/zeus/status")
[ "$code" = 200 ] || fail "GET /api/zeus/status answered $code"
[ "$(json "$TMP/status.json" 'd["container"]')" = True ] || fail "status does not report the container"
[ "$(json "$TMP/status.json" 'd["config_file"]')" = /data/config.json ] || fail "settings are not in /data"
ok "GET /api/zeus/status → 200, broker $(json "$TMP/status.json" 'd["broker_mode"]'), quotes $(json "$TMP/status.json" 'd["market_source"]'), settings in /data"

# ---- the localhost checks still hold inside the container
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: evil.example:$PORT" "$URL/api/zeus/status")
[ "$code" = 403 ] || fail "a foreign Host header got $code, not 403 (DNS-rebinding guard)"
ok "foreign Host header → 403"
curl -s -D "$TMP/h_evil" -o /dev/null -H "Origin: https://evil.example" "$URL/api/zeus/status"
! grep -qi '^access-control-allow-origin' "$TMP/h_evil" || fail "CORS allowed another site"
curl -s -D "$TMP/h_local" -o /dev/null -H "Origin: http://localhost:$PORT" "$URL/api/zeus/status"
grep -qi '^access-control-allow-origin' "$TMP/h_local" || fail "CORS refused the page's own origin"
ok "CORS: other sites refused, localhost allowed"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$URL/api/trades/sample")
[ "$code" = 403 ] || fail "a write without X-Zeus-Client got $code, not 403"
ok "writes without X-Zeus-Client → 403"

# ---- the sample portfolio (priced from live free quotes: retried when Cboe and Yahoo are busy)
loaded=""
for attempt in 1 2 3; do
  code=$(curl -s -m 120 -o "$TMP/sample.json" -w '%{http_code}' -X POST -H 'X-Zeus-Client: 1' "$URL/api/trades/sample") || code=000
  if [ "$code" = 200 ] && [ "$(json "$TMP/sample.json" 'd.get("ok")')" = True ]; then loaded=1; break; fi
  echo "  sample attempt $attempt: HTTP $code $(head -c 300 "$TMP/sample.json" 2>/dev/null)"
  sleep 20
done
[ -n "$loaded" ] || fail "the sample portfolio did not load"
curl -fsS -o "$TMP/trades.json" "$URL/api/trades"
n=$(json "$TMP/trades.json" 'd["summary"]["sample"]')
open=$(json "$TMP/trades.json" 'd["summary"]["open_positions"]')
# the sample needs quotes for at least 3 of its 6 underlyings, so it has at least 3 trades
[ "$n" -ge 3 ] && [ "$open" -ge 2 ] || fail "sample has $n trades and $open open positions"
strategies=0
for _ in $(seq 1 20); do   # the board is recomputed right after loading; give a slow quote source a minute
  curl -fsS -o "$TMP/data.json" "$URL/api/data"
  strategies=$(json "$TMP/data.json" 'len(d["strategies"])')
  [ "$strategies" -ge 1 ] && break
  sleep 3
done
[ "$strategies" -ge 1 ] || fail "the positions board has no strategies after loading the sample"
ok "sample portfolio: $n trades, $open open positions, $strategies strategies on the board"

# ---- user, read-only code, data in the volume, clock
[ "$(docker exec "$NAME" id -u)" = 1000 ] || fail "the app does not run as uid 1000"
! docker exec "$NAME" sh -c 'touch /app/zeus/zeus_config.py' 2>/dev/null || fail "the code folder is writable"
docker exec "$NAME" test -f /data/trades.json || fail "trades are not in /data"
docker exec "$NAME" test -s /data/runtime/data/heatmap_universe.txt || fail "the default lists were not seeded into /data/runtime"
[ "$(docker exec "$NAME" readlink /app/zeus/data)" = /data/runtime/data ] || fail "zeus/data is not a link into /data/runtime"
tz=$(docker exec "$NAME" date +%Z)
case "$tz" in EST|EDT) ;; *) fail "TZ=America/New_York gives $tz" ;; esac
ok "uid 1000, read-only code, data and runtime links in /data, clock $tz"
label=$(docker inspect --format '{{ index .Config.Labels "io.modelcontextprotocol.server.name" }}' "$IMAGE")
[ "$label" = "$(json "$ROOT/server.json" 'd["name"]')" ] || fail "the image's MCP Registry label is '$label', not the name in server.json"
ok "MCP Registry label: $label"

# ---- MCP: docker exec works, HTTP from the host is refused
docker exec -i "$NAME" python /app/zeus/zeus_mcp_stdio.py --check > "$TMP/check.txt" || { cat "$TMP/check.txt"; fail "MCP through docker exec failed"; }
grep -q "tools:" "$TMP/check.txt" || fail "the MCP check listed no tools"
ok "MCP through docker exec: $(sed -n 3p "$TMP/check.txt" | cut -c1-60)…"
code=$(curl -s -o "$TMP/mcp.json" -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$URL/mcp")
[ "$code" = 403 ] && grep -q "docker exec" "$TMP/mcp.json" || fail "MCP over HTTP from the host got $code: $(cat "$TMP/mcp.json")"
ok "MCP over HTTP from the host → 403 with the docker exec hint"
curl -fsS -o "$TMP/mcpinfo.json" "$URL/api/zeus/mcp"
[ "$(json "$TMP/mcpinfo.json" 'd["container"]["http"]')" = False ] || fail "Settings would offer the HTTP address"
ok "Settings → AI agents shows the docker exec commands"

# ---- the sample survives a restart (volume + read-only code)
docker restart "$NAME" >/dev/null
wait_up "$URL" || fail "the server did not come back after a restart"
curl -fsS -o "$TMP/trades2.json" "$URL/api/trades"
[ "$(json "$TMP/trades2.json" 'd["summary"]["sample"]')" = "$n" ] || fail "trades were lost in the restart"
ok "restart keeps the trades"
health=""
for _ in $(seq 1 30); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$NAME")
  [ "$health" = healthy ] && break
  sleep 3
done
[ "$health" = healthy ] || fail "health check says $health"
ok "health check: healthy"

# ---- opt-in: MCP over HTTP from the host with -e ZEUS_MCP_FROM_HOST=1
HPORT=$((PORT + 1))
docker run -d --name zeus-smoke-http -e ZEUS_MCP_FROM_HOST=1 -p "127.0.0.1:$HPORT:8765" -v "$VOLUME-http:/data" "$IMAGE" >/dev/null
wait_up "http://127.0.0.1:$HPORT" || { NAME=zeus-smoke-http; fail "the second container did not start"; }
code=$(curl -s -o "$TMP/mcp2.json" -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
  "http://127.0.0.1:$HPORT/mcp")
[ "$code" = 200 ] && [ "$(json "$TMP/mcp2.json" 'd["result"]["serverInfo"]["name"]')" = zeus-terminal ] \
  || { NAME=zeus-smoke-http; fail "MCP over HTTP with ZEUS_MCP_FROM_HOST=1 got $code: $(cat "$TMP/mcp2.json")"; }
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H "Host: evil.example:$HPORT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "http://127.0.0.1:$HPORT/mcp")
[ "$code" = 403 ] || { NAME=zeus-smoke-http; fail "a foreign Host header reached MCP with ZEUS_MCP_FROM_HOST=1 ($code)"; }
ok "with -e ZEUS_MCP_FROM_HOST=1: MCP over HTTP from the host → 200, foreign Host still 403"

echo "All checks passed for $IMAGE"

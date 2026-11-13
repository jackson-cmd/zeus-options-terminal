"""Benzinga Signals — real-time push over a single persistent WebSocket.

CRACKED (2026-05): the handshake needs `?apikey=<32-char data-api key>` in the
URL — the bare URL fails (401 headless / 1006 even in-browser). The AuthRequest
`sessionId` is just a CLIENT-generated correlation id (any 32-char string).
Cookies are sent in the handshake too. Verified: apikey + random sessionId →
AuthResponse(success) → live signal frames. No polling → no 429.

    import asyncio
    from benzinga_api import signals
    async def main():
        async for ev in signals.stream(filters=["PRICE_SPIKE_UP","PRICE_SPIKE_DOWN","GAP"]):
            print(ev.get("type"), ev.get("payload"))
    asyncio.run(main())

Auth: set BENZINGA_DATA_APIKEY (the apikey embedded in pro.benzinga.com's
frontend — same key the scanner uses; client-side key, not committed).
Fallbacks: benzinga/data_apikey.txt, then BENZINGA_SCANNER_APIKEY.
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from websockets.asyncio.client import connect as ws_connect

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_NR = _ROOT / "news_radar"
if str(_NR) not in sys.path:
    sys.path.insert(0, str(_NR))

from benzinga_api._http import data_apikey      # noqa: E402
from benzinga_client.cookies import COOKIES_PATH  # noqa: E402

SIGNALS_WS = "wss://data-api-pro.benzinga.com/signals/ws"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# PRICE_SPIKE_*/GAP confirmed from captured FeedRequest frames; the rest map to
# the Signals widget's UI toggles (Option Activity / Block Trade / Halt / Highs…).
ALL_FILTERS = [
    "PRICE_SPIKE_UP", "PRICE_SPIKE_DOWN", "GAP", "OPTION_ACTIVITY",
    "BLOCK_TRADE", "HALT", "RESUME", "NEW_HIGH", "NEW_LOW", "ERX_GAP",
]


def _cookie_header() -> str:
    raw = json.loads(Path(COOKIES_PATH).read_text())
    return "; ".join(f"{c['name']}={c['value']}" for c in raw
                     if "benzinga" in (c.get("domain") or ""))


async def stream(*, filters: Optional[Iterable[str]] = None, screener_query: str = "",
                 session_id: Optional[str] = None,
                 apikey: Optional[str] = None) -> AsyncIterator[dict]:
    """Yield signal events forever (heartbeats swallowed). Reconnect/backoff on
    drop is the caller's job — wrap in a retry loop for production."""
    key = apikey or data_apikey()
    if not key:
        raise RuntimeError(
            "signals needs the data-api apikey — set BENZINGA_DATA_APIKEY (or put it "
            "in benzinga/data_apikey.txt). It's the apikey embedded in "
            "pro.benzinga.com's frontend (same one the scanner uses).")
    sid = session_id or secrets.token_hex(16)        # any 32-char id works
    flt = list(filters) if filters else list(ALL_FILTERS)
    headers = {"Cookie": _cookie_header(), "Origin": "https://pro.benzinga.com",
               "User-Agent": _UA}
    async with ws_connect(f"{SIGNALS_WS}?apikey={key}", additional_headers=headers,
                          max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "AuthRequest", "id": 1, "sessionId": sid}))
        authed = False
        async for msg in ws:
            try:
                data = json.loads(msg)
            except Exception:
                continue
            t = data.get("type")
            if t == "AuthResponse":
                if not (data.get("payload") or {}).get("success"):
                    raise RuntimeError("signals AuthRequest rejected — apikey invalid/expired?")
                authed = True
                await ws.send(json.dumps({"type": "FeedRequest",
                                          "screenerQuery": screener_query,
                                          "signalFilters": flt}))
                continue
            if t == "Heartbeat":
                continue
            if authed:
                yield data

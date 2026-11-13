"""TastyTrade DXLink (dxfeed) real-time quote stream for the heatmap.

Replaces 15s REST polling with a pushed websocket feed. Flow:

    GET /api-quote-tokens (REST, ~24h token)  →  wss dxlink-url
    SETUP → AUTH(token) → CHANNEL_REQUEST(FEED) → FEED_SETUP → FEED_SUBSCRIPTION
    ← FEED_DATA  (Trade=last, Quote=bid/ask, Summary=prevDayClose)

Per symbol we accumulate {last, bid, ask, prev} and emit (last, prev_close,
change_pct) whenever both a price and the prior close are known. The caller
wires `on_quote` into the heatmap snapshot. Robust to disconnects: reconnects
with backoff and re-fetches a fresh token each time.

DXLink streams only symbols the account is entitled to; everything else is
left to the heatmap's yfinance fallback (same limitation the REST poll had).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

# Class shares: universe/yfinance use "BRK-B" but dxfeed uses "BRK/B".
_CLASS_SHARE = re.compile(r"^[A-Z]+-[A-Z]$")


def _to_dxfeed(sym: str) -> str:
    return sym.replace("-", "/") if _CLASS_SHARE.match(sym) else sym


def _from_dxfeed(sym: str) -> str:
    return sym.replace("/", "-")  # map dxfeed symbol back to the universe key

# trading_assistant importable (mirror heatmap_api.py)
_TA_DIR = Path(__file__).resolve().parent.parent / "trading_assistant"
if str(_TA_DIR) not in sys.path:
    sys.path.insert(0, str(_TA_DIR))

_KEEPALIVE_SECS = 30
_SUB_CHUNK = 40          # symbols per FEED_SUBSCRIPTION message (×3 event types)
_BACKOFF = [5, 10, 20, 30, 30]


def _f(v) -> Optional[float]:
    """dxfeed COMPACT value → float, treating None/'NaN'/'' as missing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _fetch_token() -> tuple[str, str]:
    """Blocking REST call — run in an executor. Returns (token, dxlink_url)."""
    from api_client import TastyClient  # type: ignore
    d = TastyClient().get("/api-quote-tokens").get("data", {})
    return d["token"], d["dxlink-url"]


# acceptEventFields — order matters for COMPACT parsing below.
# Trade = regular-session last (= today's close after RTH); TradeETH = extended
# (pre/post-market) last; Quote bid/ask = current quote (extended-aware) — used
# as the extended price when no ETH trade has printed.
_FIELDS = {
    "Quote":    ["eventType", "eventSymbol", "bidPrice", "askPrice"],
    "Trade":    ["eventType", "eventSymbol", "price"],
    "TradeETH": ["eventType", "eventSymbol", "price"],
    "Summary":  ["eventType", "eventSymbol", "prevDayClosePrice"],
}


async def stream_forever(
    get_symbols: Callable[[], Iterable[str]],
    on_quote: Callable[[str, float, float, float], None],
    on_status: Callable[[bool, int], None] | None = None,
) -> None:
    """Run the DXLink stream forever, reconnecting on failure.

    get_symbols() is called on each (re)connect so universe edits are picked up.
    on_quote(symbol, last, prev_close, change_pct) fires on every fresh tick.
    on_status(connected, n_symbols) fires on connect/disconnect (optional).
    """
    attempt = 0
    while True:
        try:
            await _run_once(get_symbols, on_quote, on_status)
            attempt = 0  # clean exit (shouldn't happen) → reset backoff
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if on_status:
                try:
                    on_status(False, 0)
                except Exception:
                    pass
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            print(f"[heatmap.dxlink] stream error: {e!r} — reconnect in {wait}s")
            await asyncio.sleep(wait)


async def _run_once(get_symbols, on_quote, on_status) -> None:
    import websockets

    loop = asyncio.get_running_loop()
    token, url = await loop.run_in_executor(None, _fetch_token)
    symbols = [s for s in dict.fromkeys(get_symbols())]  # de-dupe, keep order

    # per-symbol accumulation
    state: dict[str, dict] = {}

    def emit(sym: str) -> None:
        s = state.get(sym)
        if not s:
            return
        b, a = s.get("bid"), s.get("ask")
        mid = ((b + a) / 2.0) if (b and a and b > 0 and a > 0) else None
        # Regular/today price = RTH last trade (frozen at the close after RTH;
        # dxfeed Trade is RTH-only so it does NOT drift in extended hours). mid
        # is only a cold-start fallback before the Trade snapshot arrives.
        reg = s.get("last")
        if not reg or reg <= 0:
            reg = mid
        # Extended (pre/post) price = ETH last trade if printed, else current mid.
        ext = s.get("eth")
        if not ext or ext <= 0:
            ext = mid
        if reg and reg > 0:
            # Pass raw prices + dxfeed's prevDayClose (a COLD-START fallback only:
            # it rolls to TODAY's close after the session, so the API prefers a
            # stable date-aware prior close instead). on_quote computes the %s.
            ext_px = ext if (ext and ext > 0) else reg
            on_quote(sym, round(reg, 4), round(ext_px, 4), round(s.get("prev") or 0, 4))

    async with websockets.connect(url, max_size=2 ** 24, ping_interval=None) as ws:
        async def send(m: dict) -> None:
            await ws.send(json.dumps(m))

        await send({"type": "SETUP", "channel": 0, "version": "1.0-heatmap",
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})

        authorized = False
        subbed = False
        last_keepalive = time.time()

        while True:
            # keepalive (DXLink closes the socket after 60s of silence)
            if time.time() - last_keepalive > _KEEPALIVE_SECS:
                await send({"type": "KEEPALIVE", "channel": 0})
                last_keepalive = time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            typ = m.get("type")

            if typ == "AUTH_STATE":
                st = m.get("state")
                if st == "UNAUTHORIZED":
                    await send({"type": "AUTH", "channel": 0, "token": token})
                elif st == "AUTHORIZED" and not authorized:
                    authorized = True
                    await send({"type": "CHANNEL_REQUEST", "channel": 1,
                                "service": "FEED", "parameters": {"contract": "AUTO"}})
            elif typ == "CHANNEL_OPENED" and m.get("channel") == 1:
                await send({"type": "FEED_SETUP", "channel": 1,
                            "acceptAggregationPeriod": 0.5,
                            "acceptDataFormat": "COMPACT",
                            "acceptEventFields": _FIELDS})
            elif typ == "FEED_CONFIG" and not subbed:
                subbed = True
                for i in range(0, len(symbols), _SUB_CHUNK):
                    add = []
                    for s in symbols[i:i + _SUB_CHUNK]:
                        ds = _to_dxfeed(s)
                        add += [{"type": "Trade", "symbol": ds},
                                {"type": "TradeETH", "symbol": ds},
                                {"type": "Quote", "symbol": ds},
                                {"type": "Summary", "symbol": ds}]
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": add})
                if on_status:
                    on_status(True, len(symbols))
                print(f"[heatmap.dxlink] connected, subscribed {len(symbols)} symbols")
            elif typ == "FEED_DATA":
                _ingest(m.get("data", []), state, emit)
            elif typ == "KEEPALIVE":
                pass
            elif typ == "ERROR":
                print(f"[heatmap.dxlink] server ERROR: {m}")


def _ingest(data, state, emit) -> None:
    """Parse one FEED_DATA payload (may carry several event-type blocks)."""
    # data = [eventTypeName, [flat values...], eventTypeName, [flat...], ...]
    i = 0
    while i + 1 < len(data):
        et, flat = data[i], data[i + 1]
        i += 2
        fields = _FIELDS.get(et)
        if not fields or not isinstance(flat, list):
            continue
        n = len(fields)
        for j in range(0, len(flat), n):
            rec = flat[j:j + n]
            if len(rec) != n:
                break
            sym = _from_dxfeed(rec[1]) if rec[1] else rec[1]
            if not sym:
                continue
            s = state.setdefault(sym, {})
            if et == "Trade":
                v = _f(rec[2])
                if v is not None:
                    s["last"] = v
            elif et == "TradeETH":
                v = _f(rec[2])
                if v is not None:
                    s["eth"] = v
            elif et == "Quote":
                b, a = _f(rec[2]), _f(rec[3])
                if b is not None:
                    s["bid"] = b
                if a is not None:
                    s["ask"] = a
            elif et == "Summary":
                p = _f(rec[2])
                if p is not None:
                    s["prev"] = p
            emit(sym)

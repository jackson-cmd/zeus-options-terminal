"""DXLink (dxfeed) daily Candle fetcher — authoritative daily OHLCV.

One-shot snapshot: connect → AUTH → FEED → subscribe `SYM{=1d}` (fromTime) in
chunks → collect Candle events until the feed goes quiet → return
{ticker: DataFrame[Open,High,Low,Close,Volume]} with a tz-naive ET DatetimeIndex.

Same token/socket path as `dxlink_stream` (the live quote feed). dxfeed Candle is
the same daily basis TradingView/TastyTrade use and — unlike yfinance — always
carries the most-recent completed session (yfinance backfills it late, per-symbol,
which silently lagged the heatmap's prior close). Prices are RAW (unadjusted),
which is exactly what a day-over-day change% and a TradingView-style chart want.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Iterable, Optional

import pandas as pd

from .dxlink_stream import _f, _fetch_token, _from_dxfeed, _to_dxfeed

# COMPACT field order for the Candle event — must match acceptEventFields below.
_CANDLE_FIELDS = ["eventType", "eventSymbol", "time",
                  "open", "high", "low", "close", "volume"]
_SUB_CHUNK = 40          # symbols per FEED_SUBSCRIPTION message
_QUIET_SECS = 5.0        # stop once no Candle data arrives for this long
_MAX_SECS = 300.0        # hard ceiling for one fetch


def _candle_symbol(sym: str) -> str:
    """universe key → dxfeed daily-candle symbol, e.g. BRK-B → BRK/B{=1d}."""
    return _to_dxfeed(sym) + "{=1d}"


def _base_symbol(dxsym: str) -> str:
    """'NVDA{=1d}' → 'NVDA'; maps class shares back ('BRK/B' → 'BRK-B')."""
    return _from_dxfeed(dxsym.split("{", 1)[0])


def _bar_date(ms) -> Optional[pd.Timestamp]:
    """Candle epoch-ms → tz-naive trading-day date (normalized to midnight).

    dxfeed stamps a daily candle at ~midnight UTC of the trading day, so a plain
    UTC-naive normalize yields the right calendar date. (tz-converting to ET would
    push midnight-UTC back into the prior evening → wrong day, e.g. Mon→Sun.)"""
    try:
        return pd.Timestamp(int(float(ms)), unit="ms").normalize()
    except Exception:
        return None


def _ingest(data, rows: dict) -> None:
    """Parse one FEED_DATA payload → rows[ticker][date] = (o,h,l,c,v)."""
    i = 0
    n = len(_CANDLE_FIELDS)
    while i + 1 < len(data):
        et, flat = data[i], data[i + 1]
        i += 2
        if et != "Candle" or not isinstance(flat, list):
            continue
        for j in range(0, len(flat), n):
            rec = flat[j:j + n]
            if len(rec) != n:
                break
            sym = _base_symbol(rec[1]) if rec[1] else None
            if not sym:
                continue
            d = _bar_date(rec[2])
            c = _f(rec[6])
            if d is None or c is None:
                continue
            rows.setdefault(sym, {})[d] = (
                _f(rec[3]), _f(rec[4]), _f(rec[5]), c, _f(rec[7]),
            )


async def _run(symbols, from_ts_ms: int,
               on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    import websockets

    loop = asyncio.get_running_loop()
    token, url = await loop.run_in_executor(None, _fetch_token)
    syms = [s for s in dict.fromkeys(symbols)]
    rows: dict[str, dict] = {}

    async with websockets.connect(url, max_size=2 ** 24, ping_interval=None) as ws:
        async def send(m: dict) -> None:
            await ws.send(json.dumps(m))

        await send({"type": "SETUP", "channel": 0, "version": "1.0-candles",
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        authorized = False
        subbed = False
        last_data = time.time()
        t0 = time.time()

        while True:
            now = time.time()
            if subbed and (now - last_data) > _QUIET_SECS:
                break
            if (now - t0) > _MAX_SECS:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    await send({"type": "KEEPALIVE", "channel": 0})
                except websockets.exceptions.ConnectionClosed:
                    break
                continue
            except websockets.exceptions.ConnectionClosed:
                # dxfeed delivers the historical snapshot then closes the idle
                # connection (1000 OK) — that's "snapshot done", not an error.
                break
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
                            "acceptAggregationPeriod": 0.1,
                            "acceptDataFormat": "COMPACT",
                            "acceptEventFields": {"Candle": _CANDLE_FIELDS}})
            elif typ == "FEED_CONFIG" and not subbed:
                subbed = True
                last_data = time.time()
                for i in range(0, len(syms), _SUB_CHUNK):
                    add = [{"type": "Candle", "symbol": _candle_symbol(s),
                            "fromTime": from_ts_ms} for s in syms[i:i + _SUB_CHUNK]]
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": add})
            elif typ == "FEED_DATA":
                last_data = time.time()
                _ingest(m.get("data", []), rows)
                if on_progress:
                    on_progress(len(rows), len(syms))

    out: dict = {}
    for t, daymap in rows.items():
        if not daymap:
            continue
        idx = sorted(daymap)
        df = pd.DataFrame(
            [daymap[d] for d in idx],
            index=pd.DatetimeIndex(idx),
            columns=["Open", "High", "Low", "Close", "Volume"],
        )
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["Close"])
        if len(df):
            out[t] = df
    return out


# dxfeed closes the socket once a chunk's historical snapshot is drained, and
# streams candles at a steady pace (~1s/symbol of history), so we keep chunks
# small and run several connections concurrently to cut wall-clock.
_CONN_CHUNK = 40
_CONCURRENCY = 4
_RETRIES = 2


def _yf_daily(symbols, lookback_days, on_progress=None, on_chunk=None) -> dict:
    """Manual (no-tastytrade) mode: daily OHLCV from Yahoo Finance, batched."""
    import zeus_yf
    out: dict = {}
    start = (pd.Timestamp.today() - pd.Timedelta(days=max(int(lookback_days), 5))).strftime("%Y-%m-%d")
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        ymap = {s: s.replace(".", "-").replace("/", "-") for s in chunk}
        try:
            df = zeus_yf.download(list(ymap.values()), start=start, interval="1d", group_by="ticker",
                                  auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            print(f"[daily_candles] yfinance chunk err: {e!r}")
            continue
        got = {}
        for s, ys in ymap.items():
            try:
                sub = df[ys] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
                if sub.empty:
                    continue
                sub.index = pd.to_datetime(sub.index).tz_localize(None)
                got[s] = sub
            except Exception:
                continue
        out.update(got)
        if on_chunk and got:
            try:
                on_chunk(got)
            except Exception as e:
                print(f"[daily_candles] on_chunk err: {e!r}")
        if on_progress:
            on_progress(len(out), len(symbols))
    return out


async def fetch_daily_async(symbols: Iterable[str], lookback_days: int = 1100,
                            on_progress=None, on_chunk=None,
                            concurrency: int = _CONCURRENCY) -> dict:
    try:
        import zeus_config as _hc
        if _hc.account_source() != "tastytrade":
            return await asyncio.get_event_loop().run_in_executor(
                None, _yf_daily, list(dict.fromkeys(symbols)), lookback_days, on_progress, on_chunk)
    except ImportError:
        pass
    """Authoritative daily OHLCV for `symbols` over ~lookback_days, via dxfeed.
    Returns {ticker: DataFrame}. Runs up to `concurrency` short-lived DXLink
    connections at once (each independent of the live quote stream), calls
    `on_chunk(got)` as each connection's snapshot lands (for incremental persist),
    and retries symbols that came back empty. Safe to await on the server loop."""
    from_ts = int((time.time() - lookback_days * 86400) * 1000)
    want = [s for s in dict.fromkeys(symbols)]
    out: dict = {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(chunk):
        async with sem:
            try:
                got = await _run(chunk, from_ts)
            except Exception as e:
                print(f"[daily_candles] chunk err: {e!r}")
                got = {}
        out.update(got)
        if on_chunk and got:
            try:
                on_chunk(got)
            except Exception as e:
                print(f"[daily_candles] on_chunk err: {e!r}")
        if on_progress:
            on_progress(len(out), len(want))

    pending = want
    for rnd in range(_RETRIES + 1):
        if not pending:
            break
        chunks = [pending[i:i + _CONN_CHUNK] for i in range(0, len(pending), _CONN_CHUNK)]
        await asyncio.gather(*(one(c) for c in chunks))
        pending = [s for s in want if s not in out]
        if pending and rnd < _RETRIES:
            await asyncio.sleep(2)
    return out


def fetch_daily(symbols: Iterable[str], lookback_days: int = 1100,
                on_progress=None, on_chunk=None) -> dict:
    """Blocking wrapper for scripts / executor threads (no running event loop)."""
    return asyncio.run(fetch_daily_async(symbols, lookback_days, on_progress, on_chunk))

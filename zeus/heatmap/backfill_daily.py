"""Backfill / refresh the local daily-OHLCV store from DXLink Candle.

  python -m heatmap.backfill_daily              # full universe, ~3y
  python -m heatmap.backfill_daily --days 400   # shorter window
  python -m heatmap.backfill_daily AAPL NVDA    # specific tickers

Also exposes `refresh_recent()` — append the last few sessions for the whole
universe — which the server's poll loop calls once per trading day so the store
always carries the most-recent completed session (no yfinance lag).
"""
from __future__ import annotations

import sys
import time

from . import daily_candles as DC
from . import daily_store as DS
from . import metadata


def backfill(symbols=None, days: int = 1100) -> int:
    syms = list(symbols) if symbols else metadata.tickers()
    t0 = time.time()

    def prog(done, total):
        print(f"[backfill] {done}/{total} tickers, {int(time.time()-t0)}s", flush=True)

    # Persist each connection's snapshot as it lands — incremental + resilient
    # (a mid-run failure keeps everything fetched so far).
    DC.fetch_daily(syms, lookback_days=days, on_progress=prog,
                   on_chunk=lambda got: DS.upsert(got))
    print(f"[backfill] done: store coverage={DS.coverage()}, "
          f"last_date={DS.last_date()}, {int(time.time()-t0)}s", flush=True)
    return DS.coverage()


async def refresh_recent_async(days: int = 7) -> int:
    """Append the last `days` sessions for the whole universe (server loop)."""
    got = await DC.fetch_daily_async(metadata.tickers(), lookback_days=days)
    return DS.upsert(got)


def refresh_recent(days: int = 7) -> int:
    got = DC.fetch_daily(metadata.tickers(), lookback_days=days)
    return DS.upsert(got)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    days = 1100
    if "--days" in args:
        i = args.index("--days")
        days = int(args[i + 1])
        del args[i:i + 2]
    tickers = [a.upper() for a in args] or None
    backfill(tickers, days=days)

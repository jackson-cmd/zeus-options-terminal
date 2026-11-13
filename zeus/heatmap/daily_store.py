"""Local authoritative daily-OHLCV store (parquet), sourced from DXLink Candle.

One parquet for the whole universe: columns Open/High/Low/Close/Volume + a
`ticker` column, tz-naive DatetimeIndex. RAW (unadjusted) daily bars — the same
basis as the live DXLink quote feed and TradingView's default chart.

This is the single source of truth for:
  • heatmap prior close (change% basis) — `get_prev_close`
  • the 📈 Graph K-line wall + 📊 TA detail charts — `get_ohlc`

yfinance is no longer trusted for these: it backfills the most-recent daily bar
late and per-symbol, which silently dropped the prior-close basis back a session
(NVDA read +2% vs the real -1.4% on 2026-06-16). dxfeed always has the latest
completed session.

Writers (backfill / daily append) live in `daily_candles.py`; this module only
reads, merges, and serves. Atomic writes; fail-soft on a corrupt/missing file.
"""
from __future__ import annotations

import datetime
import threading
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_FILE = REPO_ROOT / "data" / "daily_ohlc.parquet"   # gitignored (large)
MAX_BARS_KEEP = 1300          # ~5y of daily bars per ticker
_COLS = ["Open", "High", "Low", "Close", "Volume"]

_lock = threading.Lock()
# mtime-invalidated in-memory cache: {ticker: DataFrame}
_memo: dict = {"mtime": -1.0, "data": {}}


def _mtime() -> float:
    try:
        return STORE_FILE.stat().st_mtime
    except OSError:
        return 0.0


def load() -> dict:
    """{ticker: DataFrame} from disk, memoized by file mtime. Fail-soft → {}."""
    mt = _mtime()
    if mt == _memo["mtime"]:
        return _memo["data"]
    data: dict = {}
    if STORE_FILE.exists():
        try:
            df = pd.read_parquet(STORE_FILE)
            for t, sub in df.groupby("ticker"):
                d = sub.drop(columns=["ticker"]).sort_index()
                d.index = pd.DatetimeIndex(d.index)
                data[str(t)] = d
        except Exception as e:
            print(f"[daily_store] load err: {e}")
            data = {}
    _memo["mtime"] = mt
    _memo["data"] = data
    return data


def _save(hist: dict) -> None:
    """Atomic write (tmp + replace); cap each ticker to MAX_BARS_KEEP."""
    parts = []
    for t, df in hist.items():
        if df is None or not len(df):
            continue
        sub = df.tail(MAX_BARS_KEEP).copy()
        # keep only the canonical columns we know about (Volume optional)
        keep = [c for c in _COLS if c in sub.columns]
        sub = sub[keep]
        sub["ticker"] = t
        parts.append(sub)
    if not parts:
        return
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    pd.concat(parts).to_parquet(tmp)
    tmp.replace(STORE_FILE)
    _memo["mtime"] = -1.0   # force reload on next read


def upsert(new: dict, drop_today: bool = True) -> int:
    """Merge {ticker: DataFrame} into the store (new bars win on same date).
    Returns the number of tickers touched. Thread-safe + atomic.

    drop_today: exclude the ET-today bar (it's an in-progress partial during the
    session). The store thus holds only COMPLETED sessions — `get_prev_close`
    stays exact and the Graph never shows a frozen half-formed candle. Today's
    final bar is captured tomorrow, when it's no longer "today"."""
    if not new:
        return 0
    cutoff = pd.Timestamp(_et_today()) if drop_today else None
    with _lock:
        cur = dict(load())   # shallow copy of the memoized map
        touched = 0
        for t, df in new.items():
            if df is None or not len(df):
                continue
            df = df[[c for c in _COLS if c in df.columns]].copy()
            df.index = pd.DatetimeIndex(df.index)
            if cutoff is not None:
                df = df[df.index < cutoff]
            if not len(df):
                continue
            old = cur.get(t)
            if old is not None and len(old):
                merged = pd.concat([old[~old.index.isin(df.index)], df])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            else:
                merged = df.sort_index()
            cur[t] = merged.tail(MAX_BARS_KEEP)
            touched += 1
        _save(cur)
        return touched


def get_prev_close(ticker: str, today: Optional[datetime.date] = None) -> Optional[float]:
    """Close of the most recent stored session STRICTLY BEFORE `today` (ET).
    This is the TradingView-basis prior close. None if not covered."""
    df = load().get(ticker)
    if df is None or not len(df):
        return None
    if today is None:
        today = _et_today()
    cutoff = pd.Timestamp(today)
    s = df.loc[df.index < cutoff, "Close"]
    s = s[s > 0]
    return float(s.iloc[-1]) if len(s) else None


def get_ohlc(ticker: str, days: int = 0) -> Optional[pd.DataFrame]:
    """Stored OHLCV for `ticker` (last `days` bars, or all if days<=0)."""
    df = load().get(ticker)
    if df is None or not len(df):
        return None
    return df.tail(days) if days and days > 0 else df


def has(ticker: str) -> bool:
    return ticker in load()


def last_date() -> Optional[datetime.date]:
    """Latest bar date across the whole store (max of per-ticker last dates)."""
    data = load()
    mx = None
    for df in data.values():
        if len(df):
            d = df.index[-1]
            if mx is None or d > mx:
                mx = d
    return mx.date() if mx is not None else None


def coverage() -> int:
    """Number of tickers with at least one stored bar."""
    return len(load())


def _et_today() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date()

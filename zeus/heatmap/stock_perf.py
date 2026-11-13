"""Multi-period % returns for the heatmap stock universe (not real-time).

For every (non-ETF) ticker, compute the return over 3天 / 1周 / 1月 / 3月 / 1年
from yfinance daily closes, plus market cap / sector from metadata. Powers the
zoomable beeswarm "涨跌榜" view. Cached to disk, refreshed at most daily.
Mirrors etf_trends.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import metadata, table_cache

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _REPO_ROOT / "data" / "heatmap" / "stock_perf.json"
_CACHE_TTL = 24 * 60 * 60  # 1 day — "一天一更新"

# (label, key, trading-day lookback)
_PERIODS = [("3天", "r3d", 3), ("1周", "r1w", 5), ("1月", "r1mo", 21),
            ("3月", "r3mo", 63), ("1年", "r1yr", 252)]

_state: dict = {}   # in-memory cache, see table_cache


def _compute() -> dict:
    """Download daily closes and compute per-ticker multi-period returns."""
    etfs = metadata.etf_set()
    stocks = [t for t in metadata.tickers() if t not in etfs]
    meta = metadata.get_all()

    closes: dict[str, list] = {}
    if stocks:
        try:
            import zeus_yf
            df = zeus_yf.download(stocks, period="400d", interval="1d", progress=False,
                                  auto_adjust=True, threads=True)
            close = df["Close"]
            single = (getattr(close, "ndim", 1) == 1)
            for t in stocks:
                try:
                    s = close.dropna() if single else close[t].dropna()
                    closes[t] = [float(x) for x in s.tolist()]
                except Exception:
                    closes[t] = []
        except Exception as e:
            print(f"[heatmap.stock_perf] yfinance failed: {e}")

    items = []
    for t in stocks:
        c = closes.get(t, [])
        m = meta.get(t, {})
        item = {
            "ticker": t,
            "name": m.get("name", t),
            "sector": m.get("sector", "Unknown"),
            "industry": m.get("industry", "Unknown"),
            "market_cap": m.get("market_cap", 0.0),
            "last": round(c[-1], 4) if c else 0.0,
            "has_data": bool(c),
        }
        for _, key, n in _PERIODS:
            if len(c) > n and c[-1 - n] > 0:
                item[key] = round((c[-1] / c[-1 - n] - 1.0) * 100.0, 2)
            else:
                item[key] = None
        if item["has_data"]:
            items.append(item)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "periods": [{"label": lbl, "key": key} for lbl, key, _ in _PERIODS],
        "count": len(items),
        "items": items,
    }


def get(force: bool = False) -> dict:
    """Cached payload; recompute if forced, missing, or stale (>1 day). A refresh
    that brings back no data keeps the last good table (see table_cache)."""
    return table_cache.get(_state, _CACHE, _CACHE_TTL, _compute, force=force,
                           tag="heatmap.stock_perf")

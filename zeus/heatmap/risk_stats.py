"""Beta / correlation / volatility for the heatmap stock universe (not real-time).

For every (non-ETF) ticker we compute, over 3M / 6M / 1Y windows of yfinance
daily returns, against both SPY and QQQ:

    beta  — slope of the stock's daily returns regressed on the benchmark's
            (>1 = amplifies the market). beta = cov(stock,bench)/var(bench)
    corr  — Pearson correlation with the benchmark (−1…+1): how tightly it tracks
    r2    — corr² (how much of the move the benchmark explains)

and, benchmark-independent:

    vol   — annualized stdev of daily returns (%). The "how wild is it" axis.

These statistics barely move day-to-day, so the result is cached to disk and
refreshed at most weekly (7-day TTL) + a manual Refresh. Mirrors etf_trends.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import metadata, table_cache

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _REPO_ROOT / "data" / "heatmap" / "risk_stats.json"
_CACHE_TTL = 7 * 24 * 60 * 60  # 1 week — "一周更新一次"

# (label, key, trading-day lookback)
_WINDOWS = [("3M", "3m", 63), ("6M", "6m", 126), ("1Y", "1y", 252)]
_BENCHMARKS = ["SPY", "QQQ"]
_ANN = 252 ** 0.5  # daily → annualized vol factor


def _min_obs(n: int) -> int:
    """Minimum overlapping observations to trust a stat for a window."""
    return max(20, int(n * 0.6))


def _vol(a, n: int):
    import numpy as np
    w = a[-n:]
    w = w[np.isfinite(w)]
    if len(w) < _min_obs(n):
        return None
    return round(float(np.std(w, ddof=1) * _ANN * 100.0), 2)


def _beta_corr(a, b, n: int):
    import numpy as np
    aa, bb = a[-n:], b[-n:]
    m = np.isfinite(aa) & np.isfinite(bb)
    if int(m.sum()) < _min_obs(n):
        return None
    aa, bb = aa[m], bb[m]
    vb = float(np.var(bb, ddof=1))
    if vb <= 0:
        return None
    cov = float(np.cov(aa, bb, ddof=1)[0, 1])
    sa, sb = float(np.std(aa, ddof=1)), float(np.std(bb, ddof=1))
    corr = (cov / (sa * sb)) if (sa > 0 and sb > 0) else None
    return {
        "beta": round(cov / vb, 3),
        "corr": round(corr, 3) if corr is not None else None,
        "r2": round(corr * corr, 3) if corr is not None else None,
    }


def _targets(universe: str) -> list[tuple]:
    """(ticker, name, sector, industry, mcap) for the chosen universe.

    'stocks' → the non-ETF universe with sector/industry/mcap from metadata.
    'etf'    → the curated ETF list (etf_trends.txt); ETFs have no sector/mcap,
               so we group them by the file's category (板块/主题 vs 国家/全球)
               and leave mcap 0 (the frontend renders them at a uniform size).
    """
    if universe == "etf":
        from . import etf_trends
        cat_label = {"SECTOR": "板块/主题 ETF", "GLOBAL": "国家/全球 ETF"}
        out = []
        for cat, t, name in etf_trends._parse_list():
            out.append((t, name, cat_label.get(cat, "ETF"), "", 0.0))
        return out
    etfs = metadata.etf_set()
    meta = metadata.get_all()
    out = []
    for t in metadata.tickers():
        if t in etfs:
            continue
        m = meta.get(t, {})
        out.append((t, m.get("name", t), m.get("sector", "Unknown"),
                    m.get("industry", "Unknown"), m.get("market_cap", 0.0)))
    return out


def _compute(universe: str = "stocks") -> dict:
    """Download daily closes and compute per-ticker risk stats."""
    import numpy as np

    targets = _targets(universe)
    tickers = [t[0] for t in targets]
    syms = list(dict.fromkeys(tickers + _BENCHMARKS))

    items: list[dict] = []
    bench_vol = {b: {wk: None for _, wk, _ in _WINDOWS} for b in _BENCHMARKS}

    if syms:
        try:
            import zeus_yf
            df = zeus_yf.download(syms, period="400d", interval="1d", progress=False,
                                  auto_adjust=True, threads=True)
            close = df["Close"]
            close_arr = close.values
            rets_arr = close.pct_change().values
            cols = list(close.columns)
            idx = {c: i for i, c in enumerate(cols)}

            def ret_col(t):
                i = idx.get(t)
                return rets_arr[:, i] if i is not None else None

            def last_px(t):
                i = idx.get(t)
                if i is None:
                    return 0.0
                cc = close_arr[:, i]
                cc = cc[np.isfinite(cc)]
                return round(float(cc[-1]), 4) if len(cc) else 0.0

            bench_ret = {b: ret_col(b) for b in _BENCHMARKS}
            for b in _BENCHMARKS:
                ba = bench_ret[b]
                if ba is not None:
                    for _, wk, n in _WINDOWS:
                        bench_vol[b][wk] = _vol(ba, n)

            for t, name, sector, industry, mcap in targets:
                a = ret_col(t)
                if a is None:
                    continue
                item = {
                    "ticker": t,
                    "name": name,
                    "sector": sector,
                    "industry": industry,
                    "market_cap": mcap,
                    "mcap": mcap,
                    "last": last_px(t),
                    "vol": {wk: _vol(a, n) for _, wk, n in _WINDOWS},
                }
                has_any = any(v is not None for v in item["vol"].values())
                for b in _BENCHMARKS:
                    ba = bench_ret[b]
                    bd = {}
                    for _, wk, n in _WINDOWS:
                        st = _beta_corr(a, ba, n) if ba is not None else None
                        bd[wk] = st
                        if st:
                            has_any = True
                    item[b] = bd
                if has_any:
                    items.append(item)
        except Exception as e:
            print(f"[heatmap.risk_stats] compute failed: {e}")

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "universe": universe,
        "windows": [{"label": lbl, "key": wk} for lbl, wk, _ in _WINDOWS],
        "benchmarks": list(_BENCHMARKS),
        "bench_vol": bench_vol,
        "count": len(items),
        "items": items,
    }


# Separate in-memory + on-disk cache per universe (stocks / etf).
_state_by: dict = {"stocks": {}, "etf": {}}


def _cache_path(universe: str) -> Path:
    return _CACHE if universe == "stocks" else _CACHE.with_name("risk_stats_etf.json")


def get(force: bool = False, universe: str = "stocks") -> dict:
    """Cached payload for the given universe; recompute if forced/missing/stale. A
    refresh that brings back no data keeps the last good table (see table_cache)."""
    universe = "etf" if universe == "etf" else "stocks"
    return table_cache.get(_state_by[universe], _cache_path(universe), _CACHE_TTL,
                           lambda: _compute(universe), force=force,
                           tag=f"heatmap.risk_stats.{universe}")

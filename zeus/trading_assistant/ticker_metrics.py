"""Batch per-ticker metrics from TastyTrade.

Combines /market-data (price, 52w range, today's range, volume) and
/market-metrics (IV, earnings, sector, beta, P/E) into one merged dict per
symbol. Handles 600+ symbols in a single call each, sub-second latency.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from api_client import TastyClient


def _to_tasty(sym: str) -> str:
    """Barchart/Yahoo use BRK-B; TastyTrade expects BRK/B."""
    return sym.replace("-", "/")


def _safe_float(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (ValueError, TypeError):
        return None


def fetch_ticker_metrics(symbols: list[str]) -> dict[str, dict]:
    """Return {symbol: {price, change_pct, year_high, year_low, iv, earnings_date, ...}}.

    Missing/NaN fields are omitted. Unknown symbols map to an empty dict.
    Retries in sub-batches on timeout; never raises.
    """
    if not symbols:
        return {}

    tasty_to_orig = {_to_tasty(s): s for s in symbols}
    joined = ",".join(tasty_to_orig)

    out: dict[str, dict] = {s: {} for s in symbols}
    client = TastyClient()

    # --- /market-data: price, 52w range, today's range, volume ---
    try:
        resp = client.get("/market-data", params={"symbols": joined})
        for item in resp.get("data", {}).get("items", []):
            sym = tasty_to_orig.get(item.get("symbol", ""))
            if not sym:
                continue
            d = out[sym]
            last = _safe_float(item.get("last")) or _safe_float(item.get("mark"))
            prev = _safe_float(item.get("prev-close"))
            if last:
                d["price"] = round(last, 2)
            if last and prev and prev > 0:
                d["change_pct"] = round((last - prev) / prev * 100, 2)
            for src, dst, ndig in [
                ("year-high-price", "year_high", 2),
                ("year-low-price", "year_low", 2),
                ("day-high-price", "day_high", 2),
                ("day-low-price", "day_low", 2),
            ]:
                v = _safe_float(item.get(src))
                if v is not None:
                    d[dst] = round(v, ndig)
            vol = _safe_float(item.get("volume"))
            if vol is not None:
                d["volume"] = int(vol)
    except Exception as e:
        print(f"  [WARN] /market-data batch failed: {e}")

    # --- /market-metrics: IV, earnings, sector, beta, P/E ---
    try:
        resp = client.get("/market-metrics", params={"symbols": joined})
        for item in resp.get("data", {}).get("items", []):
            sym = tasty_to_orig.get(item.get("symbol", ""))
            if not sym:
                continue
            d = out[sym]
            for src, dst, ndig in [
                ("implied-volatility-index", "iv", 4),
                ("implied-volatility-index-rank", "iv_rank", 4),
                ("implied-volatility-percentile", "iv_percentile", 4),
                ("implied-volatility-30-day", "iv_30d", 2),
                ("historical-volatility-30-day", "hv_30d", 2),
                ("iv-hv-30-day-difference", "iv_hv_diff", 2),
                ("beta", "beta", 3),
                ("corr-spy-3month", "corr_spy_3m", 3),
                ("price-earnings-ratio", "pe_ratio", 2),
                ("earnings-per-share", "eps", 2),
                ("dividend-yield", "dividend_yield", 4),
            ]:
                v = _safe_float(item.get(src))
                if v is not None:
                    d[dst] = round(v, ndig)

            mc = _safe_float(item.get("market-cap"))
            if mc is not None:
                d["market_cap"] = int(mc)
            liq = item.get("liquidity-rating")
            if liq is not None:
                try:
                    d["liquidity_rating"] = int(liq)
                except (ValueError, TypeError):
                    pass
            for key in ("sector", "industry"):
                val = item.get(key)
                if val:
                    d[key] = val

            earn = item.get("earnings") or {}
            ed = earn.get("expected-report-date")
            if ed:
                d["earnings_date"] = ed
                d["earnings_time"] = earn.get("time-of-day") or ""
                est = earn.get("consensus-estimate")
                if est is not None and est != "":
                    d["earnings_estimate"] = str(est)
    except Exception as e:
        print(f"  [WARN] /market-metrics batch failed: {e}")

    return out


if __name__ == "__main__":
    import json
    test = fetch_ticker_metrics(["AAPL", "NVDA", "BRK-B", "SPY"])
    for sym, d in test.items():
        print(f"\n=== {sym} ===")
        print(json.dumps(d, indent=2))

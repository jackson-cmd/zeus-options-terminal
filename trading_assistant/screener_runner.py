"""
Screener Runner — produces structured JSON for the interactive screener UI.

Pipeline (parallelized with ThreadPoolExecutor):
  1. scan_ticker() × N in parallel → signals
  2. Load/compute win rates per ticker in parallel
  3. Filter signals < 60% win rate
  4. Fetch Finviz news per signal ticker in parallel
  5. Return JSON-serializable dict
"""
from __future__ import annotations

import datetime
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Ensure trading_assistant is importable
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

_PROJECT_ROOT = _THIS_DIR.parent
_WINRATE_DATA_DIR = _PROJECT_ROOT / "winrate_scanner" / "data"

# Also need winrate_scanner on path
_WINRATE_DIR = _PROJECT_ROOT / "winrate_scanner"
if str(_WINRATE_DIR) not in sys.path:
    sys.path.insert(0, str(_WINRATE_DIR))

_SCRAPER_DIR = _PROJECT_ROOT / "barchart"  # 迁移：旧 options_analyzer/screener_scraper 已并入 barchart 模块


# ---------------------------------------------------------------------------
# Signal name → winrate key mapping
# ---------------------------------------------------------------------------

def _signal_to_winrate_key(signal_name: str, category: str) -> str:
    """Map scanner.py signal names → winrate_engine storage keys (SSP1).

    scanner.py produces "STACK (EMA...)", "EMA20 crossover", "V1 (...)", etc.
    signals.py stores under keys like LONG_STACK, LONG_EMA20, BEAR_V1, BEAR_V2.
    """
    s = signal_name.strip()
    if s.startswith("STACK"):
        return "LONG_STACK"
    if s.startswith("EMA20"):
        return "LONG_EMA20"
    if s.startswith("EMA50"):
        return "LONG_EMA50"
    if s.startswith("EMA100"):
        return "LONG_EMA100"
    if s.startswith("EMA200"):
        return "LONG_EMA200"
    if s.startswith("V1"):
        return "BEAR_V1"
    if s.startswith("V2"):
        return "BEAR_V2"
    # Fallback
    prefix = s.split(" ")[0].split("(")[0].strip()
    return f"{category}_{prefix}"


def _best_winrate(winrates: dict, key: str) -> dict | None:
    info = winrates.get(key)
    if not info:
        return None
    best = None
    for wk_key, stats in info.get("weeks", {}).items():
        total = stats.get("total", 0)
        if total == 0:
            continue
        close_rate = round(stats["close_win"] / total * 100, 1)
        highlow_rate = round(stats["highlow_win"] / total * 100, 1)
        best_rate = max(close_rate, highlow_rate)
        if best is None or best_rate > best["best_rate"]:
            best = {
                "best_week": wk_key,
                "close_rate": close_rate,
                "highlow_rate": highlow_rate,
                "best_rate": best_rate,
                "samples": total,
            }
    return best


# ---------------------------------------------------------------------------
# External screener execution + loading (Barchart + Finviz)
# ---------------------------------------------------------------------------

# Barchart screener direction map — user-curated whitelist.
# Noisy / redundant screeners removed: 1mUP30%, barchart_signals,
# 3m(15)_6m(30)_stock, 5day10%, Institutional Favorites.
_BC_DIRECTIONS: dict[str, str] = {
    # BULL — continuation-of-strength signals
    "Gainer recently":        "BULL",
    "weighted_alpha_ema":     "BULL",
    "short_medium_long_buy":  "BULL",
    "barchart_popular":       "BULL",
    # BEAR
    "Loser recently":         "BEAR",
}
_BC_WHITELIST_NAMES = set(_BC_DIRECTIONS.keys())


def _bc_safe_filename(name: str) -> str:
    """Match scan.py's safe_filename: replace non-word chars with _"""
    import re
    return re.sub(r'[^\w\-]', '_', name)


# Precomputed safe filenames for whitelist lookup
_BC_WHITELIST_SAFE = {_bc_safe_filename(n): n for n in _BC_WHITELIST_NAMES}


def bc_direction(screener_name: str) -> str:
    """Return 'BULL' or 'BEAR' for a Barchart screener. Unknown → 'BULL'."""
    return _BC_DIRECTIONS.get(screener_name, "BULL")


def _run_external_screeners(report_fn=None, tick_fn=None):
    """Run Barchart Playwright screener directly (no HTML/CSV orchestration).

    Returns: {ticker: [{"type": "BC", "name": "screener_name"}, ...]}
    """
    import subprocess
    from collections import defaultdict

    if report_fn:
        report_fn("Running Barchart screener...")

    screener_py = _SCRAPER_DIR / "scrape_screener.py"
    if screener_py.exists():
        # Filter to whitelisted screeners only — screener.py's --name uses
        # nargs="*" so all names go after a single --name flag.
        try:
            proc_bc = subprocess.Popen(
                [sys.executable, str(screener_py), "--name", *_BC_WHITELIST_NAMES],
                cwd=str(_SCRAPER_DIR),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            proc_bc = None

        if proc_bc:
            try:
                proc_bc.wait(timeout=300)
                if tick_fn:
                    tick_fn("Barchart screener done")
                elif report_fn:
                    report_fn("Barchart screener done")
            except Exception:
                proc_bc.kill()

    # ── Load results from generated files ──
    sources = defaultdict(list)

    # Barchart: read *_tickers.txt — only whitelisted screeners
    bc_dir = _SCRAPER_DIR / "downloads" / "screeners"
    if bc_dir.is_dir():
        for txt_path in bc_dir.glob("*_tickers.txt"):
            safe_name = txt_path.stem.replace("_tickers", "")
            if safe_name not in _BC_WHITELIST_SAFE:
                continue  # skip screeners not in whitelist
            display_name = _BC_WHITELIST_SAFE[safe_name]
            direction = bc_direction(display_name)
            tickers = [t.strip() for t in txt_path.read_text().split() if t.strip()]
            for t in tickers:
                sources[t].append({"type": "BC", "name": display_name, "direction": direction})

    if report_fn:
        bc_count = sum(1 for srcs in sources.values() for s in srcs if s["type"] == "BC")
        report_fn(f"Barchart: {bc_count} hits")

    return dict(sources)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

SCAN_WORKERS = 10
WR_WORKERS = 8
NEWS_WORKERS = 10
MIN_WINRATE = 65.0   # SSP1: require ≥65% HIGH/LOW win rate


def _load_screener_tickers() -> list[str]:
    """Load curated ticker list from screener_idea.csv (Barchart export).

    Primary: winrate_scanner/screener_idea.csv — standard CSV with 'Symbol'
    column. Filters out Barchart footer rows like "DOWNLOADED FROM...".

    Fallback: winrate_scanner/screener.txt — legacy comma-separated format
    with 'EXCHANGE:SYMBOL' entries (used for earlier 710-ticker universe).
    """
    csv_path = _PROJECT_ROOT / "winrate_scanner" / "screener_idea.csv"
    if csv_path.exists():
        import csv as _csv
        symbols = []
        seen = set()
        with csv_path.open(encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                sym = (row.get("Symbol") or "").strip().upper()
                # Skip footer/garbage rows (non-ticker)
                if not sym or not sym.replace("-", "").replace(".", "").isalnum():
                    continue
                if len(sym) > 7:  # valid tickers are ≤5 chars typically; filter noise
                    continue
                sym = sym.replace(".", "-")
                if sym not in seen:
                    seen.add(sym)
                    symbols.append(sym)
        if symbols:
            return symbols

    # Fallback: legacy txt format
    path = _PROJECT_ROOT / "winrate_scanner" / "screener.txt"
    if not path.exists():
        return []
    raw = path.read_text()
    symbols = []
    seen = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok or tok.startswith("###"):
            continue
        sym = tok.split(":", 1)[-1].strip().upper()
        if not sym or sym.startswith("#"):
            continue
        sym = sym.replace(".", "-")
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def run_screener_scan(tickers: list[str] | None = None,
                      progress_cb=None) -> dict:
    """Run the full screener pipeline and return structured JSON dict."""
    from scanner import scan_ticker
    from news_collector import fetch_finviz_news

    # ── Thread-safe progress tracking ──
    _lock = threading.Lock()
    _done = [0]
    _total = [1]

    def _tick(msg):
        with _lock:
            _done[0] += 1
            if progress_cb:
                progress_cb(msg, done=_done[0], total=_total[0])

    def _report(msg):
        print(f"  [screener] {msg}")
        with _lock:
            if progress_cb:
                progress_cb(msg, done=_done[0], total=_total[0])

    today_str = datetime.date.today().isoformat()

    # ── 1. Load tickers from winrate_scanner/screener.txt (user-curated list) ──
    if tickers is None:
        tickers = _load_screener_tickers()
        if not tickers:
            # Fallback to legacy CSV
            csv_path = _PROJECT_ROOT / "data" / "tickers.csv"
            if csv_path.exists():
                import csv
                with open(csv_path) as f:
                    reader = csv.DictReader(f)
                    tickers = [row.get("ticker", row.get("Ticker", "")).strip()
                               for row in reader]
                    tickers = [t for t in tickers if t]
            else:
                tickers = []

    # Estimate: scan + winrates + news
    _total[0] = int(len(tickers) * 1.7)
    _report(f"Scanning {len(tickers)} tickers...")

    # ── 2. Parallel signal scan ──
    signal_results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(scan_ticker, t): t for t in tickers}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                sig = fut.result()
                if sig.has_any:
                    signal_results.append(sig)
            except Exception:
                pass
            _tick(f"Scanning {t} ({i}/{len(tickers)})")

    _done[0] = len(tickers)
    print(f"  [screener] Found {len(signal_results)} tickers with signals")

    # ── 3. Parallel winrate computation + filtering ──
    from winrate_engine import load_winrates, compute_winrates, save_winrates
    from signals import detect_all_signals
    from yahoo_api import fetch_daily_data

    wr_count = len(signal_results)
    _total[0] = len(tickers) + wr_count + wr_count  # scan + wr + news
    _report(f"Computing win rates for {wr_count} tickers...")

    def _compute_wr_for_ticker(sig):
        # Always recompute — win rates can change as new data comes in.
        # User explicitly requested no caching here.
        wr_data = {}
        try:
            df = fetch_daily_data(sig.ticker, lookback_days=1800)  # ~5y for stable stats
            if not df.empty and len(df) >= 210:
                sig_df = detect_all_signals(df)
                wr_data = compute_winrates(df, sig_df)
                save_winrates(wr_data, sig.ticker, _WINRATE_DATA_DIR, today_str)
        except Exception:
            wr_data = {}

        filtered = []
        for category, sig_list in [
            ("LONG", sig.long_signals), ("BEAR", sig.bear_signals),
            ("MR", sig.mr_signals), ("PAT", sig.pattern_signals),
            ("GAP", sig.gap_signals),
        ]:
            for signal_name in sig_list:
                wr_key = _signal_to_winrate_key(signal_name, category)
                wr_stats = _best_winrate(wr_data, wr_key)
                if wr_stats and wr_stats["best_rate"] >= MIN_WINRATE:
                    filtered.append({
                        "name": signal_name,
                        "category": category,
                        "winrate_key": wr_key,
                        "close_rate": wr_stats["close_rate"],
                        "highlow_rate": wr_stats["highlow_rate"],
                        "best_rate": wr_stats["best_rate"],
                        "best_week": wr_stats["best_week"],
                        "samples": wr_stats["samples"],
                    })
        return sig, filtered

    output_signals = []
    with ThreadPoolExecutor(max_workers=WR_WORKERS) as pool:
        futures = {pool.submit(_compute_wr_for_ticker, sig): sig for sig in signal_results}
        for i, fut in enumerate(as_completed(futures), 1):
            sig_obj = futures[fut]
            try:
                sig, filtered = fut.result()
                if filtered:
                    output_signals.append({
                        "ticker": sig.ticker,
                        "price": round(sig.price, 2),
                        "change_pct": sig.change_pct,
                        "direction": sig.direction,
                        "signal_strength": len(filtered),
                        "signals": filtered,
                    })
            except Exception:
                pass
            _tick(f"Win rates: {sig_obj.ticker} ({i}/{wr_count})")

    output_signals.sort(key=lambda x: x["signal_strength"], reverse=True)
    print(f"  [screener] {len(output_signals)} tickers after 60% winrate filter")

    # ── 4. Skip Finviz news (429 rate limit issues) ──
    for item in output_signals:
        item["news"] = []

    # ── 5. Run + merge external sources (Barchart — Finviz removed) ──
    _total[0] = len(tickers) + wr_count + 2  # +2 for BC + FV steps
    ext_sources = _run_external_screeners(report_fn=_report, tick_fn=_tick)

    # Constrain BC output to the curated ticker universe. Any BC hit outside
    # the 264-ticker list (from screener_idea.csv) is discarded — we only
    # trade tickers the user has pre-approved.
    universe = set(tickers)
    ext_sources = {t: srcs for t, srcs in ext_sources.items() if t in universe}

    # Helper: fold WR direction + BC source directions into one aggregate.
    # Rule: if any direction conflicts, mark MIXED. Else use the agreed direction.
    def _aggregate_direction(wr_direction: str, bc_srcs: list) -> str:
        dirs = set()
        if wr_direction in ("BULL", "BEAR"):
            dirs.add(wr_direction)
        for src in bc_srcs:
            d = src.get("direction")
            if d in ("BULL", "BEAR"):
                dirs.add(d)
        if len(dirs) == 0:
            return "BULL"          # nothing to go on; safe default
        if len(dirs) == 1:
            return dirs.pop()
        return "MIXED"              # conflict → backtest will skip

    # Add sources to existing tickers + re-aggregate direction
    existing_tickers = {item["ticker"] for item in output_signals}
    for item in output_signals:
        item["sources"] = ext_sources.get(item["ticker"], [])
        item["signal_strength"] = len(item["signals"]) + len(item["sources"])
        item["direction"] = _aggregate_direction(item.get("direction", ""), item["sources"])

    # Add tickers that only appear in external sources (no winrate signals).
    external_only = [t for t in ext_sources if t not in existing_tickers]
    for ticker in external_only:
        srcs = ext_sources[ticker]
        bc_dirs = {s.get("direction") for s in srcs if s.get("direction") in ("BULL", "BEAR")}
        direction = bc_dirs.pop() if len(bc_dirs) == 1 else ("MIXED" if len(bc_dirs) > 1 else "BULL")
        output_signals.append({
            "ticker": ticker,
            "price": 0.0,
            "change_pct": 0.0,
            "direction": direction,
            "signal_strength": len(srcs),
            "signals": [],
            "sources": srcs,
            "news": [],
        })

    # ── 5b. Batch-fetch TastyTrade metrics for ALL tickers in output ──
    # Two REST calls (/market-data + /market-metrics), ~0.5s total for 600+
    # tickers. Covers: price, change_pct, 52w range, IV, IVR, earnings, beta,
    # P/E, sector, liquidity, market-cap. Overwrites scan-path daily close with
    # real-time TastyTrade last price.
    from ticker_metrics import fetch_ticker_metrics
    all_tickers = [item["ticker"] for item in output_signals]
    _report(f"Fetching TastyTrade metrics for {len(all_tickers)} tickers...")
    metrics = fetch_ticker_metrics(all_tickers)
    for item in output_signals:
        m = metrics.get(item["ticker"], {})
        if not m:
            continue
        if "price" in m:
            item["price"] = m["price"]
        if "change_pct" in m:
            item["change_pct"] = m["change_pct"]
        for k in ("year_high", "year_low", "day_high", "day_low", "volume",
                  "iv", "iv_rank", "iv_percentile", "iv_30d", "hv_30d",
                  "iv_hv_diff", "beta", "corr_spy_3m", "pe_ratio", "eps",
                  "dividend_yield", "market_cap", "liquidity_rating",
                  "sector", "industry", "earnings_date", "earnings_time",
                  "earnings_estimate"):
            if k in m:
                item[k] = m[k]

    output_signals.sort(key=lambda x: x["signal_strength"], reverse=True)
    _report(f"{len(output_signals)} tickers total (with external sources)")

    # ── 6. Build summary ──
    bull_count = sum(1 for s in output_signals if s["direction"] == "BULL")
    bear_count = sum(1 for s in output_signals if s["direction"] == "BEAR")

    # Collect all unique signal/source names for filter dropdown
    all_signal_names = sorted({
        s["name"].split(" (")[0].split(" ")[0]  # short prefix like "3D", "RSI", "EMA20"
        for item in output_signals for s in item["signals"]
    })
    all_source_names = sorted({
        f"{s['type']}:{s['name']}"
        for item in output_signals for s in item.get("sources", [])
    })

    result = {
        "scan_date": today_str,
        "scan_time": datetime.datetime.now().isoformat(),
        "tickers_scanned": len(tickers),
        "signals_found": len(output_signals),
        "signals": output_signals,
        "all_signal_names": all_signal_names,
        "all_source_names": all_source_names,
        "summary": {
            "bull": bull_count,
            "bear": bear_count,
            "mixed": len(output_signals) - bull_count - bear_count,
            "total_signals": sum(s["signal_strength"] for s in output_signals),
        },
    }

    out_path = _PROJECT_ROOT / "trading_assistant" / "reports" / f"screener_{today_str}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    _report(f"Saved to {out_path.name}")

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run screener scan")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated ticker list (default: all)")
    args = parser.parse_args()

    t_list = args.tickers.split(",") if args.tickers else None
    result = run_screener_scan(t_list)
    print(f"\nDone. {result['signals_found']} tickers with qualified signals.")
    print(json.dumps(result["summary"], indent=2))

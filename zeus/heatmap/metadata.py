"""Heatmap metadata cache: sector / industry / market cap per ticker.

yfinance is hit once per day per ticker and the result is persisted to
`data/heatmap/metadata.json`. Lookups are pure JSON reads after that — no
network on the hot path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TICKERS_CSV = _REPO_ROOT / "data" / "tickers.csv"
# Heatmap-specific universe. Categorized list the user maintains by hand:
#   "###Section name,EXCHANGE:TICKER,EXCHANGE:TICKER,…,###Next section,…"
# A section whose name contains "ETF" tags its tickers as ETFs (shown in the
# Table "compact"/movers view, hidden from the "full" detailed table). When
# this file is absent we fall back to the ML master list (data/tickers.csv).
_UNIVERSE_TXT = _REPO_ROOT / "data" / "heatmap_universe.txt"
_CACHE_PATH = _REPO_ROOT / "data" / "heatmap" / "metadata.json"
_ERROR_RETRY_SECONDS = 2 * 3600  # error 条目 2h 后重试

# In-process cache so we don't re-read the JSON on every request
_mem_cache: dict | None = None
_mem_loaded_at: float = 0.0

# Parsed universe cache, invalidated by the file's mtime
_uni_cache: dict[str, bool] | None = None
_uni_mtime: float = -1.0


def _parse_universe_txt() -> dict[str, bool]:
    """Parse the categorized universe file → {TICKER: is_etf}.

    Strips EXCHANGE: prefixes, upper-cases, and normalizes class shares for
    yfinance (BRK.B → BRK-B). Cached until the file changes on disk.
    """
    global _uni_cache, _uni_mtime
    if not _UNIVERSE_TXT.exists():
        return {}
    try:
        mtime = _UNIVERSE_TXT.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _uni_cache is not None and mtime == _uni_mtime:
        return _uni_cache

    raw = _UNIVERSE_TXT.read_text()
    out: dict[str, bool] = {}
    is_etf_section = False
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("###"):
            is_etf_section = "ETF" in tok[3:].strip().upper()
            continue
        sym = tok.split(":")[-1].strip().upper().replace(".", "-")
        if not sym:
            continue
        # If a symbol shows up in both an ETF and a stock section, treat it as a
        # stock (only stays ETF when every occurrence is in an ETF section).
        out[sym] = (out[sym] and is_etf_section) if sym in out else is_etf_section

    _uni_cache, _uni_mtime = out, mtime
    return out


def etf_set() -> set[str]:
    """Tickers classified as ETFs in the universe file (empty if no file)."""
    return {t for t, is_etf in _parse_universe_txt().items() if is_etf}


def _load_tickers() -> list[str]:
    """Heatmap ticker universe — the hand-maintained list if present, else the
    ML master CSV."""
    uni = _parse_universe_txt()
    if uni:
        return sorted(uni.keys())
    if not _TICKERS_CSV.exists():
        return []
    lines = _TICKERS_CSV.read_text().splitlines()
    out = []
    for ln in lines[1:]:  # skip header
        t = ln.strip().upper()
        if t and t.isascii() and 1 <= len(t) <= 5 and t.isalpha():
            out.append(t)
    return sorted(set(out))


def _read_cache() -> dict:
    """Load on-disk JSON cache; return {} if absent or corrupt."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def _yf_fetch_one(ticker: str) -> Optional[dict]:
    """Fetch one ticker's sector/industry/market_cap from yfinance.

    Returns None on any failure — caller decides whether to retry.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    sector = (info.get("sector") or "").strip() or "Unknown"
    industry = (info.get("industry") or "").strip() or "Unknown"
    mcap = info.get("marketCap") or 0
    try:
        mcap = float(mcap)
    except Exception:
        mcap = 0.0
    name = (info.get("shortName") or info.get("longName") or ticker).strip()
    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "industry": industry,
        "market_cap": mcap,
    }


def _finviz_fetch_one(ticker: str) -> Optional[dict]:
    """Fallback: fetch metadata from finviz when yfinance fails."""
    try:
        from finviz import get_stock
    except ImportError:
        return None
    try:
        d = get_stock(ticker)
    except Exception:
        return None
    sector = (d.get("Sector") or "").strip() or "Unknown"
    industry = (d.get("Industry") or "").strip() or "Unknown"
    mcap_str = d.get("Market Cap") or "0"
    try:
        mcap = _parse_mcap(mcap_str)
    except Exception:
        mcap = 0.0
    name = (d.get("Company") or ticker).strip()
    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "industry": industry,
        "market_cap": mcap,
    }


def _parse_mcap(s: str) -> float:
    """Parse finviz market cap strings like '932.05B' or '1.53T'."""
    s = s.strip().replace(",", "")
    if not s or s == "-":
        return 0.0
    mult = 1.0
    if s.endswith("T"):
        mult, s = 1e12, s[:-1]
    elif s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("K"):
        mult, s = 1e3, s[:-1]
    return float(s) * mult


def refresh(force: bool = False, max_tickers: Optional[int] = None,
            log: bool = True) -> dict:
    """Refresh the cache for any ticker that is missing or stale.

    Returns the full ticker → metadata dict after refresh.
    Safe to call from a background thread; pure I/O.
    """
    tickers = _load_tickers()
    if max_tickers is not None:
        tickers = tickers[:max_tickers]

    cache = _read_cache()
    now = time.time()

    to_fetch = []
    for t in tickers:
        entry = cache.get(t)
        if not entry:
            to_fetch.append(t)
            continue
        if force:
            to_fetch.append(t)
            continue
        if entry.get("error") and now - entry.get("fetched_at", 0) > _ERROR_RETRY_SECONDS:
            to_fetch.append(t)

    if log and to_fetch:
        print(f"[heatmap.metadata] refreshing {len(to_fetch)} ticker(s)...")
    changed = bool(to_fetch)      # Finviz may fill everything, leaving nothing for Yahoo

    global _mem_cache, _mem_loaded_at
    # Finviz first: one request covers 20 tickers (≈9 requests for 170) instead of
    # one Yahoo `.info` call per ticker — the call Yahoo rate-limits hardest.
    if to_fetch:
        try:
            import zeus_finviz as _fv
            fv = _fv.rows(to_fetch, max_age=3600)
        except Exception as e:
            fv = {}
            print(f"[heatmap.metadata] finviz batch failed: {e}")
        got = 0
        groups = _universe_groups()
        etfs = etf_set()
        done_fv = set()
        for t in list(to_fetch):
            r = fv.get(t)
            if not r:
                continue
            sector = r.get("sector") or ""
            if not sector and (t in etfs or "ETF" in (r.get("name") or "").upper()):
                sector = groups.get(t) or "ETF"      # Finviz has no sector for funds
            if sector or r.get("market_cap"):
                cache[t] = {"ticker": t, "name": r.get("name") or t, "sector": sector or "Unknown",
                            "industry": r.get("industry") or ("ETF" if t in etfs else "Unknown"),
                            "market_cap": float(r.get("market_cap") or 0.0), "fetched_at": now}
                done_fv.add(t)
                got += 1
        to_fetch = [t for t in to_fetch if t not in done_fv]
        if log and got:
            print(f"[heatmap.metadata] finviz filled {got} ticker(s); {len(to_fetch)} left for Yahoo")
    yf_fails = 0
    for i, t in enumerate(to_fetch, start=1):
        meta = _yf_fetch_one(t)
        if meta is None or meta.get("sector") == "Unknown":
            # Yahoo failed outright, or answered a rate-limited partial record
            yf_fails += 1
            meta = _finviz_fetch_one(t) or meta
        if meta is None:
            cache[t] = {
                "ticker": t,
                "name": t,
                "sector": "Unknown",
                "industry": "Unknown",
                "market_cap": 0.0,
                "fetched_at": now,
                "error": True,
            }
        else:
            meta["fetched_at"] = now
            cache[t] = meta
        if i % 25 == 0:
            if log:
                print(f"[heatmap.metadata]  {i}/{len(to_fetch)}"
                      + (f" (yf_fail={yf_fails})" if yf_fails else ""))
            _write_cache(cache)
            _mem_cache = cache
            _mem_loaded_at = time.time()
        if len(to_fetch) > 10:
            time.sleep(0.3)

    if changed:
        _write_cache(cache)

    _mem_cache = cache
    _mem_loaded_at = now
    return cache


def get_all() -> dict:
    """Get current ticker → metadata map (in-memory if fresh, else from disk).

    Does NOT trigger a network refresh. Use refresh() for that.
    """
    global _mem_cache, _mem_loaded_at
    if _mem_cache is not None and (time.time() - _mem_loaded_at) < 60:
        return _mem_cache
    _mem_cache = _read_cache()
    # When Yahoo could not supply a sector (rate limits happen), fall back to the
    # group name the ticker sits under in the universe file ("Semiconductors 半导体"
    # → "Semiconductors").
    groups = _universe_groups()
    for t, m in _mem_cache.items():
        if isinstance(m, dict) and (m.get("sector") or "Unknown") == "Unknown" and t in groups:
            m["sector"] = groups[t]
    _mem_loaded_at = time.time()
    return _mem_cache


def _universe_groups() -> dict[str, str]:
    """TICKER → English part of its ###section name in the universe file."""
    if not _UNIVERSE_TXT.exists():
        return {}
    out: dict[str, str] = {}
    section = ""
    for tok in _UNIVERSE_TXT.read_text().replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("###"):
            name = tok[3:].strip()
            cut = next((i for i, ch in enumerate(name) if ord(ch) >= 0x2E80), len(name))
            section = name[:cut].strip() or name
            continue
        sym = tok.split(":")[-1].strip().upper().replace(".", "-")
        if sym and section:
            out.setdefault(sym, section)
    return out


def tickers() -> list[str]:
    """Currently-tracked tickers (from the master CSV)."""
    return _load_tickers()

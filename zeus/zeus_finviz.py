"""Finviz batch lookups — the fallback when Yahoo Finance rate-limits.

The free Finviz screener accepts a list of tickers (`t=A,B,C`) and returns one
row per ticker with company, sector, industry, country, market cap, P/E, price,
change % and volume. Twenty tickers per request, about one request per second,
results cached — so the whole default universe (170 tickers) costs nine polite
requests instead of 170 per-ticker Yahoo calls.

Used by:
  heatmap/metadata.py   sector / industry / market cap / name   (Finviz first)
  heatmap/heatmap_api.py universe quotes when Yahoo fails or is partial
  local_broker.py        manual-mode stock quotes when Yahoo fails
Prices on the free page are delayed (typically ~15 minutes).
"""
from __future__ import annotations

import html
import re
import threading
import time

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SCREENER = "https://finviz.com/screener.ashx?v=111&t={tickers}"
CHUNK = 20
_GAP = 1.1          # seconds between requests (be polite, avoid blocks)

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_last_request = [0.0]
_CELL = re.compile(r'data-column-id="([A-Za-z]+)"[^>]*>(?:<[^>]+>)*([^<]*)<')


def _norm(sym: str) -> str:
    return (sym or "").strip().upper().lstrip("$").replace(".", "-").replace("/", "-")


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "").replace("%", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_cap(s: str) -> float:
    s = (s or "").strip().replace(",", "")
    if not s or s == "-":
        return 0.0
    mult = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}.get(s[-1], 1.0)
    try:
        return float(s[:-1] if mult != 1.0 else s) * mult
    except ValueError:
        return 0.0


def _parse(page: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in page.split('data-ticker="')[1:]:
        t = part[:part.find('"')].strip().upper()
        if not t:
            continue
        cells = {k: html.unescape(v).strip() for k, v in _CELL.findall(part[:9000])}
        price = _num(cells.get("price", ""))
        chg = _num(cells.get("change", ""))
        prev = None
        if price is not None and chg is not None and chg > -100:
            prev = round(price / (1 + chg / 100.0), 4)
        out[t] = {
            "ticker": t,
            "name": cells.get("company") or t,
            "sector": cells.get("sector") or "",
            "industry": cells.get("industry") or "",
            "country": cells.get("country") or "",
            "market_cap": parse_cap(cells.get("marketCap", "")),
            "pe": _num(cells.get("PE", "")),
            "last": price or 0.0,
            "change_pct": chg if chg is not None else 0.0,
            "prev_close": prev or 0.0,
            "volume": _num(cells.get("volume", "")) or 0.0,
        }
    return out


def _fetch(chunk: list[str]) -> dict[str, dict]:
    import requests
    with _lock:
        wait = _GAP - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
    r = requests.get(SCREENER.format(tickers=",".join(chunk)),
                     headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=20)
    r.raise_for_status()
    return _parse(r.text)


def rows(tickers, max_age: float = 60.0) -> dict[str, dict]:
    """ticker → row for every ticker Finviz knows; cached `max_age` seconds.
    Never raises: on any network problem the tickers are simply missing."""
    want = [_norm(t) for t in dict.fromkeys(tickers) if t]
    now = time.time()
    out: dict[str, dict] = {}
    missing = []
    with _lock:
        for t in want:
            hit = _cache.get(t)
            if hit and now - hit[0] < max_age:
                out[t] = hit[1]
            else:
                missing.append(t)
    # Pass 1: chunks of 20. Pass 2: whatever came back missing, asked on its own
    # (Finviz drops index ETFs such as SPY/QQQ from mixed stock + ETF lists).
    for attempt in range(2):
        if not missing:
            break
        for i in range(0, len(missing), CHUNK):
            chunk = missing[i:i + CHUNK]
            try:
                got = _fetch(chunk)
            except Exception as e:
                print(f"[finviz] batch lookup failed: {e}")
                continue
            stamp = time.time()
            with _lock:
                for t, row in got.items():
                    _cache[t] = (stamp, row)
            out.update(got)
        missing = [t for t in missing if t not in out]
    return out


def quotes(tickers, max_age: float = 60.0) -> dict[str, dict]:
    """ticker → {last, prev_close, change_pct} (only tickers with a price)."""
    out = {}
    for t, r in rows(tickers, max_age=max_age).items():
        if r.get("last"):
            out[t] = {"last": r["last"], "prev_close": r.get("prev_close") or 0.0,
                      "change_pct": r.get("change_pct") or 0.0}
    return out

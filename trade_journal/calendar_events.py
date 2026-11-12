"""Earnings + Econ calendar feed for the trade journal.

Two data sources, both free, no API keys:

  • Earnings — Nasdaq daily earnings calendar JSON
    (api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD). One request
    per day in the window (today → end of next month, ~60 requests),
    threaded. Filtered to the user's watchlist (data/watchlist.txt) with
    ETFs excluded. Yahoo's earnings_dates was tried first but only
    publishes past quarters; Nasdaq's feed has both date AND BMO/AMC
    session, which the user explicitly asked for.

  • Economic events — ForexFactory monthly HTML page
    (https://www.forexfactory.com/calendar?month=this | next). FF
    embeds the full month's events as a JSON array inside a JS init
    block (`window.calendarComponentStates[1] = {days:[...]}`); we
    extract individual flat event objects via regex, parse JSON, and
    filter to USD currency + High impact. Two requests = both months
    in one refresh. (The XML weekly feed only covers 7 days — not
    enough for the user's "this month + next month" ask.)

Cache: data/calendar_cache.json. Refresh is user-triggered (button) or
weekly via the user's manual workflow — no cron.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

# Resolve sibling files relative to this module so the symlinked dev
# environment + the prod repo both work.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
WATCHLIST_PATH = _REPO_ROOT / "data" / "watchlist.txt"
CACHE_PATH = _THIS_DIR / "data" / "calendar_cache.json"

# Confirmed with the user. Anything in this set is excluded from earnings
# calls (ETFs don't have an earnings date and yfinance returns None for
# them — listing them is a slow no-op).
_ETF_TICKERS = frozenset({
    "SPY", "QQQ", "DIA", "IWM", "TQQQ", "SQQQ", "NVDL",
    "XLK", "XLY", "XLE", "XLI", "XLF", "XLV", "XLU", "XLP", "XLB",
    "GLD", "SLV", "IAU", "GDX", "GDXJ", "SIL",
    "TLT", "IEF", "HYG", "LQD",
    "SOXL", "SOXX", "IGV", "ARKK", "FXI", "EEM", "EFA",
    "EWY", "EWZ", "INDA",
    "KRE", "RSP", "COPX", "URA", "XOP", "XBI",
    "IBIT", "SATS",
})

# Earnings window: from today through the end of next month inclusive.
def _earnings_window() -> tuple[datetime, datetime]:
    now = datetime.now()
    start = datetime.combine(now.date(), time.min)
    # End of month+2 (3-month window): first day of month+3 minus 1 second
    m = now.month + 3
    y = now.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    next_month_first = datetime(y, m, 1)
    end = next_month_first - timedelta(seconds=1)
    return start, end


# ─── Watchlist ──────────────────────────────────────────────────────────
def load_watchlist() -> list[str]:
    """Strip 'EXCH:' prefix, dedupe, drop ETFs. Tickers may be separated by commas
    (the original format) or by line breaks (what ZEUS seeds into data/watchlist.txt);
    reading only commas turned a one-per-line file into a single bogus ticker and left
    the earnings calendar empty."""
    if not WATCHLIST_PATH.exists():
        return []
    raw = WATCHLIST_PATH.read_text(encoding="utf-8").strip()
    out, seen = [], set()
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip()
        if not tok:
            continue
        sym = tok.split(":", 1)[-1].strip().upper()
        if sym in _ETF_TICKERS or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


# ─── Earnings (Nasdaq daily JSON) ───────────────────────────────────────
_NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
_NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Nasdaq uses these literal strings — map to the user-facing labels.
_SESSION_MAP = {
    "time-pre-market":  "BMO",  # before open
    "time-after-hours": "AMC",  # after close
    "time-not-supplied": "--",
}


def _fetch_nasdaq_day(day: datetime) -> list[dict]:
    """Fetch one day's earnings calendar from Nasdaq. Returns raw rows."""
    url = _NASDAQ_URL.format(date=day.strftime("%Y-%m-%d"))
    req = urllib.request.Request(url, headers=_NASDAQ_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return []
    return (data.get("data") or {}).get("rows") or []


def fetch_earnings(tickers: Optional[list[str]] = None) -> list[dict]:
    if tickers is None:
        tickers = load_watchlist()
    universe = set(tickers)
    start, end = _earnings_window()
    days = []
    d = start
    while d <= end:
        # Skip weekends — Nasdaq only publishes weekday calendars
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_nasdaq_day, day): day for day in days}
        for fut in cf.as_completed(futures):
            day = futures[fut]
            try:
                rows = fut.result()
            except Exception:
                continue
            for row in rows:
                sym = (row.get("symbol") or "").strip().upper()
                if sym not in universe:
                    continue
                session = _SESSION_MAP.get(row.get("time"), "--")
                results.append({
                    "ticker": sym,
                    "date": day.strftime("%Y-%m-%d"),
                    "session": session,
                    "eps_forecast": row.get("epsForecast"),
                    "name": row.get("name"),
                    "fiscal_quarter": row.get("fiscalQuarterEnding"),
                })
    if not results:
        results = _earnings_from_store(universe, start, end)
    # Order: date asc, BMO before AMC before --, ticker alpha
    _SORT = {"BMO": 0, "AMC": 1, "DMH": 2, "--": 3}
    results.sort(key=lambda e: (e["date"], _SORT.get(e["session"], 9), e["ticker"]))
    return results


def _earnings_from_store(universe: set, start: datetime, end: datetime) -> list[dict]:
    """Fallback when Nasdaq refuses the request (it answers 403 to many networks):
    the per-ticker earnings dates the Picker keeps up to date (heatmap/earnings.py,
    from tastytrade, yfinance or Finviz). Only confirmed dates, no estimates."""
    try:
        import sys as _sys
        if str(_REPO_ROOT) not in _sys.path:
            _sys.path.insert(0, str(_REPO_ROOT))
        from heatmap import earnings as _store
        records = _store.load()
    except Exception:
        return []
    out = []
    for sym in sorted(universe):
        rec = records.get(sym) or {}
        day = str(rec.get("date") or "")[:10]
        try:
            when = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        if start <= when <= end:
            out.append({"ticker": sym, "date": day,
                        "session": {"am": "BMO", "pm": "AMC"}.get(rec.get("time"), "--"),
                        "eps_forecast": None, "name": None, "fiscal_quarter": None})
    return out


# ─── Econ events (multi-source: TradingView primary + Finviz backup) ────
# Two independent feeds, aggregated + deduped. If one source 403s or changes
# shape, the other still fills the calendar — only an all-sources failure
# surfaces an error. US high-impact only, per the project rule. (ForexFactory
# was dropped: Cloudflare 403s a plain urllib request.)
_ET = ZoneInfo("America/New_York")

_TV_EVENTS_URL = ("https://economic-calendar.tradingview.com/events"
                  "?from={frm}&to={to}&countries=US")
_TV_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}

_FINVIZ_CAL_URL = "https://finviz.com/calendar.ashx"
_FINVIZ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
}


def _fetch_tradingview_econ() -> list[dict]:
    """US high-impact events from TradingView (this month + next month).

    importance==1 is TradingView's high tier (NFP, CPI, ISM, FOMC, GDP …).
    Event times arrive in UTC (…Z) and are converted to US/Eastern.
    """
    now = datetime.now()
    frm = now.replace(day=1).strftime("%Y-%m-%dT00:00:00.000Z")
    nxt = (now.replace(day=1) + timedelta(days=93)).replace(day=1)
    to = nxt.strftime("%Y-%m-%dT00:00:00.000Z")
    url = _TV_EVENTS_URL.format(frm=frm, to=to)
    req = urllib.request.Request(url, headers=_TV_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
    out = []
    for ev in data.get("result", []):
        if ev.get("importance") != 1:
            continue
        ds = ev.get("date") or ""
        title = (ev.get("title") or "").strip()
        if not ds or not title:
            continue
        try:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00")).astimezone(_ET)
        except Exception:
            continue
        out.append({
            "title": title, "country": "USD",
            "date": dt.date().isoformat(), "time_local": dt.strftime("%H:%M"),
            "forecast": ev.get("forecast") or None,
            "previous": ev.get("previous") or None,
            "actual":   ev.get("actual") or None,
            "source": "TradingView",
        })
    return out


def _fetch_finviz_econ() -> list[dict]:
    """US high-impact events from Finviz (≈ this week). importance==3 = high.

    Finviz embeds each event as a flat JSON object ({"calendarId":...}); regex
    pulls them out and json.loads each. Times are already US/Eastern.
    """
    req = urllib.request.Request(_FINVIZ_CAL_URL, headers=_FINVIZ_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="ignore")
    out = []
    for raw in re.findall(r'\{"calendarId":\d+[^{}]*\}', html):
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if ev.get("importance") != 3:
            continue
        ds = ev.get("date") or ""
        if "T" not in ds:
            continue
        d, t = ds.split("T", 1)
        title = (ev.get("event") or "").strip()
        if not title:
            continue
        out.append({
            "title": title, "country": "USD",
            "date": d, "time_local": t[:5],
            "forecast": (ev.get("forecast") or "").strip() or None,
            "previous": (ev.get("previous") or "").strip() or None,
            "actual":   (ev.get("actual") or "").strip() or None,
            "source": "Finviz",
        })
    return out


_ECON_SOURCES = (_fetch_tradingview_econ, _fetch_finviz_econ)

_HARDCODED_TIME = {
    "FOMC": "14:00", "CPI": "08:30", "PCE": "08:30", "NFP": "08:30",
    "GDP": "08:30", "TripleWit": "16:00", "OPEC": "10:00",
}


def _hardcoded_econ_rows() -> list[dict]:
    """Pull HARDCODED_MACRO_EVENTS from the PWA server as a fallback.

    TV/Finviz only populate ~4 weeks ahead; FOMC/CPI/PCE beyond that
    window won't show unless we fill them from the hardcoded list.
    """
    try:
        import sys as _sys, importlib as _il
        _sys.path.insert(0, str(_REPO_ROOT / "options_analyzer"))
        mod = _il.import_module("optionstrat_server")
        events = getattr(mod, "HARDCODED_MACRO_EVENTS", [])
    except Exception:
        return []
    now = datetime.now()
    frm = now.date().isoformat()
    nxt = (now.replace(day=1) + timedelta(days=93)).replace(day=1)
    to = nxt.date().isoformat()
    out = []
    for date_iso, typ, name, _tier in events:
        if date_iso < frm or date_iso > to:
            continue
        out.append({
            "title": name, "country": "USD",
            "date": date_iso,
            "time_local": _HARDCODED_TIME.get(typ, "08:30"),
            "forecast": None, "previous": None, "actual": None,
            "source": "hardcoded",
        })
    return out


def fetch_econ_events() -> list[dict]:
    """Aggregate every econ source, dedup by (date, time, title).

    Resilient: a source that 403s or changes shape is skipped; as long as ONE
    source returns rows we still produce a calendar. Only when ALL sources
    fail do we raise, so refresh() can surface econ_error.
    """
    rows, errors = [], []
    for fn in _ECON_SOURCES:
        try:
            rows.extend(fn())
        except Exception as e:
            tag = fn.__name__.replace("_fetch_", "").replace("_econ", "")
            errors.append(f"{tag}: {e}")
    # hardcoded fallback fills gaps TV/Finviz haven't published yet
    rows.extend(_hardcoded_econ_rows())
    if not rows and errors:
        raise RuntimeError("all econ sources failed — " + "; ".join(errors))
    seen, out = set(), []
    for ev in rows:
        key = (ev["date"], ev["time_local"], ev["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    out.sort(key=lambda e: (e["date"], e["time_local"]))
    return out


# ─── Cache + refresh ────────────────────────────────────────────────────
def load_cached() -> dict:
    if not CACHE_PATH.exists():
        return {"earnings": [], "econ": [], "fetched_at": None,
                "earnings_universe_size": 0}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"earnings": [], "econ": [], "fetched_at": None,
                "earnings_universe_size": 0}


def refresh() -> dict:
    """Fetch both feeds, write cache, return the new payload."""
    tickers = load_watchlist()
    earnings = fetch_earnings(tickers)
    try:
        econ = fetch_econ_events()
        econ_error = None
    except Exception as e:
        econ = []
        econ_error = str(e)
    payload = {
        "earnings": earnings,
        "econ": econ,
        "econ_error": econ_error,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "earnings_universe_size": len(tickers),
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return payload

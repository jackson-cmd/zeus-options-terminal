"""Benzinga "Market Events" — the calendar family + a couple of siblings.

Every type below was reverse-engineered from pro.benzinga.com and confirmed
working headlessly with cookies.json (see benzinga/BENZINGA_API.md §2). They
share one query shape, so `fetch(kind, ...)` does the work and the named
helpers are thin wrappers.

    from benzinga_api import calendar as cal
    cal.earnings(tickers=["AAPL", "NVDA"], date_from="2026-05-01")
    cal.squawk(tickers="AAPL", date_from="2026-05-29")
    cal.option_activity(tickers=["TSLA"])
    cal.government_trades(tickers=["NVDA"])     # special endpoint, see below
"""
from __future__ import annotations

import datetime
from typing import Iterable, Optional, Union

from ._http import API_HOST, get_json

Tickers = Optional[Union[str, Iterable[str]]]
DateLike = Optional[Union[str, datetime.date, datetime.datetime]]

# kind -> (api_version, path, top-level response key)
_REGISTRY: dict[str, tuple[str, str, str]] = {
    "squawk":           ("v1",   "calendar/squawk",           "squawk"),
    "earnings":         ("v2",   "calendar/earnings",         "earnings"),
    "economics":        ("v2",   "calendar/economics",        "economics"),
    "ratings":          ("v2",   "calendar/ratings",          "ratings"),
    "dividends":        ("v2",   "calendar/dividends",        "dividends"),
    "guidance":         ("v2",   "calendar/guidance",         "guidance"),
    "conference_calls": ("v2",   "calendar/conference-calls", "conference"),
    "splits":           ("v2",   "calendar/splits",           "splits"),
    "ipos":             ("v2",   "calendar/ipos",             "ipos"),
    "ma":               ("v2",   "calendar/ma",               "ma"),
    "offerings":        ("v2",   "calendar/offerings",        "offerings"),
    "fda":              ("v2.1", "calendar/fda",              "fda"),
    "option_activity":  ("v1",   "signal/option_activity",    "option_activity"),
}

KINDS = sorted(_REGISTRY)


def _fmt_date(d: DateLike) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime.datetime):
        return d.date().isoformat()
    if isinstance(d, datetime.date):
        return d.isoformat()
    return str(d)


def _csv(tickers: Tickers) -> Optional[str]:
    if tickers is None:
        return None
    if isinstance(tickers, str):
        return tickers.upper()
    return ",".join(str(t).upper() for t in tickers)


def fetch(kind: str, *, tickers: Tickers = None, date_from: DateLike = None,
          date_to: DateLike = None, pagesize: int = 1000,
          date_search_field: str = "target", extra: Optional[dict] = None) -> list[dict]:
    """Fetch one Market-Events type → list of event dicts (the top-key array).

    `kind` must be one of KINDS. `tickers` accepts a CSV string or any iterable.
    Dates accept ISO strings or date/datetime objects; omit for the API default.
    """
    if kind not in _REGISTRY:
        raise ValueError(f"unknown kind '{kind}'. valid: {KINDS}")
    ver, path, key = _REGISTRY[kind]
    params: dict = {"pagesize": pagesize, "parameters[dateSearchField]": date_search_field}
    df, dt, tk = _fmt_date(date_from), _fmt_date(date_to), _csv(tickers)
    if df:
        params["parameters[date_from]"] = df
    if dt:
        params["parameters[date_to]"] = dt
    if tk:
        params["parameters[tickers]"] = tk
    if extra:
        params.update(extra)
    data = get_json(f"{API_HOST}/api/{ver}/{path}", params)
    # Most types return {"<key>": [...]}, but several (fda/ipos/ma/offerings/
    # splits) return a bare [] when the ticker/date filter matches nothing.
    if isinstance(data, list):
        return data
    rows = data.get(key)
    return rows if isinstance(rows, list) else []


# ── named helpers (so callers get autocomplete + don't memorize kind strings) ──
def squawk(**kw) -> list[dict]:            return fetch("squawk", **kw)
def earnings(**kw) -> list[dict]:          return fetch("earnings", **kw)
def economics(**kw) -> list[dict]:         return fetch("economics", **kw)
def ratings(**kw) -> list[dict]:           return fetch("ratings", **kw)
def dividends(**kw) -> list[dict]:         return fetch("dividends", **kw)
def guidance(**kw) -> list[dict]:          return fetch("guidance", **kw)
def conference_calls(**kw) -> list[dict]:  return fetch("conference_calls", **kw)
def splits(**kw) -> list[dict]:            return fetch("splits", **kw)
def ipos(**kw) -> list[dict]:              return fetch("ipos", **kw)
def mergers_acquisitions(**kw) -> list[dict]: return fetch("ma", **kw)
def offerings(**kw) -> list[dict]:         return fetch("offerings", **kw)
def fda(**kw) -> list[dict]:               return fetch("fda", **kw)
def option_activity(**kw) -> list[dict]:   return fetch("option_activity", **kw)


def government_trades(*, tickers: Tickers = None, date_from: DateLike = None,
                      date_to: DateLike = None, pagesize: int = 1000,
                      date_search_field: str = "target") -> list[dict]:
    """US congressional trades. NOTE: different endpoint + param style than the
    calendar family — uses `search_keys=` (not `parameters[tickers]`), bare
    `date_to`, and the response array lives under key `data`.
    """
    params: dict = {"pagesize": pagesize, "dateSearchField": date_search_field}
    if _fmt_date(date_from):
        params["date_from"] = _fmt_date(date_from)
    if _fmt_date(date_to):
        params["date_to"] = _fmt_date(date_to)
    if _csv(tickers):
        params["search_keys"] = _csv(tickers)
    data = get_json(f"{API_HOST}/api/v1/gov/usa/congress/trades", params)
    if isinstance(data, list):
        return data
    rows = data.get("data")
    return rows if isinstance(rows, list) else []

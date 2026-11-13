"""Market-session state for the heatmap — holiday + early-close aware (XNYS).

The heatmap distinguishes four phases so it knows when a value is *live* and
when it should be *frozen* (and therefore never re-requested):

    PRE     04:00 ET → regular open (usually 09:30)   — pre-market
    RTH     open → close (09:30–16:00, 13:00 on early-close days)
    POST    close → 20:00 ET                           — after-hours
    CLOSED  everything else: overnight, weekends, holidays

`session_now()` is the hot path (called per stream tick) so it caches its
answer for a couple of seconds; the underlying XNYS schedule is built at most
once per ET calendar day. Falls back to a weekday+time heuristic (no holiday
awareness) if pandas_market_calendars is unavailable.
"""
from __future__ import annotations

import datetime
import time
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

PRE = "PRE"
RTH = "RTH"
POST = "POST"
CLOSED = "CLOSED"

_PRE_START = datetime.time(4, 0)
_POST_END = datetime.time(20, 0)

# XNYS schedule window, keyed by ET date → (open_et, close_et) python datetimes.
# Rebuilt when the ET calendar day rolls over.
_sched: dict[datetime.date, tuple[datetime.datetime, datetime.datetime]] | None = None
_sched_day: datetime.date | None = None

# Short-lived cache of the last computed phase so per-tick callers are cheap.
_cache: dict = {"at": 0.0, "phase": CLOSED, "date": None}
_CACHE_TTL = 2.0


def _now_et() -> datetime.datetime:
    return datetime.datetime.now(_ET)


def _ensure_sched(today: datetime.date) -> None:
    """Build/refresh the XNYS schedule window for `today` (cached for the day)."""
    global _sched, _sched_day
    if _sched is not None and _sched_day == today:
        return
    import pandas_market_calendars as mcal
    cal = mcal.get_calendar("XNYS")
    sched = cal.schedule(
        start_date=today - datetime.timedelta(days=10),
        end_date=today + datetime.timedelta(days=5),
    )
    out: dict[datetime.date, tuple[datetime.datetime, datetime.datetime]] = {}
    for idx, row in sched.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        out[d] = (
            row["market_open"].tz_convert(_ET).to_pydatetime(),
            row["market_close"].tz_convert(_ET).to_pydatetime(),
        )
    _sched, _sched_day = out, today


def _fallback(et: datetime.datetime) -> tuple[str, datetime.date | None]:
    """Calendar unavailable → weekday + fixed 09:30–16:00 ET (no holidays)."""
    d = et.date()
    if et.weekday() >= 5:
        return CLOSED, _last_weekday(d)
    t = et.time()
    if t < _PRE_START:
        return CLOSED, _last_weekday(d - datetime.timedelta(days=1))
    if t < datetime.time(9, 30):
        return PRE, d
    if t <= datetime.time(16, 0):
        return RTH, d
    if t < _POST_END:
        return POST, d
    return CLOSED, d


def _last_weekday(d: datetime.date) -> datetime.date:
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def last_rth_close_date() -> datetime.date | None:
    """Date of the most recent RTH session whose close has already passed.

    This is the trading day the *frozen* reg series belongs to. Returns None
    only if the calendar is unavailable and no schedule could be built.
    """
    et = _now_et()
    today = et.date()
    try:
        _ensure_sched(today)
    except Exception:
        return _last_weekday(today if et.time() >= datetime.time(16, 0)
                             else today - datetime.timedelta(days=1))
    assert _sched is not None
    for d in sorted(_sched.keys(), reverse=True):
        if d > today:
            continue
        _open, close = _sched[d]
        if et >= close:
            return d
    return None


def session_now() -> tuple[str, datetime.date | None]:
    """Return (phase, session_date).

    session_date is the active trading day for PRE/RTH/POST, and the most
    recently completed RTH date for CLOSED (so frozen reg keys to it). Cached
    for ~2s; safe to call on every stream tick.
    """
    now = time.time()
    if _cache["date"] is not None and (now - _cache["at"]) < _CACHE_TTL:
        return _cache["phase"], _cache["date"]

    et = _now_et()
    today = et.date()
    try:
        _ensure_sched(today)
    except Exception:
        phase, date = _fallback(et)
        _cache.update(at=now, phase=phase, date=date)
        return phase, date

    assert _sched is not None
    oc = _sched.get(today)
    if oc is None:
        # Not a trading day (weekend/holiday).
        phase, date = CLOSED, last_rth_close_date()
    else:
        mo, mc = oc
        if et < mo:
            if et.time() >= _PRE_START:
                phase, date = PRE, today
            else:  # 00:00–04:00 on a trading day → prior session still the last close
                phase, date = CLOSED, last_rth_close_date()
        elif et < mc:
            phase, date = RTH, today
        elif et.time() < _POST_END:
            phase, date = POST, today
        else:  # 20:00–24:00 → today's RTH is the last completed close
            phase, date = CLOSED, today

    _cache.update(at=now, phase=phase, date=date)
    return phase, date


def is_rth() -> bool:
    return session_now()[0] == RTH

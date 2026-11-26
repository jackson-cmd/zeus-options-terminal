"""Optional Equibles source: earnings-call transcripts with speakers, and a daily IV rank.

Only used when `equibles_api_key` is set in Settings → Other data. With no key every
function here returns None at once and ZEUS keeps its own sources (YouTube captions for
the 📞 button, data/options_vol/history.parquet for the checkup's IV rank).

Quiet by design: any failure returns None so the caller falls back. A 401, 403 or 429
pauses that endpoint until the next UTC midnight, when the API's daily counter resets,
so a free key asking for IV rank (a paid-plan route, 403) costs one request a day, not
one per checkup. Transcripts are cached on disk per ticker and fiscal quarter, and the
"which call is the latest" lookup for a few hours, so reloads don't spend requests.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

import zeus_config as hc

API = "https://api.equibles.com/v1"
_TIMEOUT = 20
_PAGE = 200                      # most turns the transcript route returns per request
_MAX_PAGES = 10                  # a call is ~30-150 turns; stop runaway paging
_LATEST_FRESH_SECS = 6 * 3600    # re-ask which call is the latest after 6 h (picks up new quarters)
_PAUSE_CODES = (401, 403, 429)

_lock = threading.Lock()


def api_key() -> str:
    return str(hc.get("equibles_api_key") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def _dir() -> Path:
    return hc.data_dir("equibles")


def _safe(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9._-]", "", ticker.upper()) or "_"


# ─── pause after 401 / 403 / 429 ─────────────────────────────────────────────

def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _pause_file() -> Path:
    return _dir() / "paused.json"


def _paused() -> dict:
    try:
        data = json.loads(_pause_file().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_paused(endpoint: str) -> bool:
    return _paused().get(endpoint) == _utc_today()


def _pause(endpoint: str) -> None:
    with _lock:
        data = {k: v for k, v in _paused().items() if v == _utc_today()}
        data[endpoint] = _utc_today()
        try:
            _pause_file().write_text(json.dumps(data), "utf-8")
        except Exception:
            pass


# ─── HTTP ────────────────────────────────────────────────────────────────────

def _get(path: str, params: Optional[dict] = None, key: Optional[str] = None):
    """(status, json or None). Never raises; a network error is status 0."""
    import requests
    try:
        r = requests.get(API + path, params=params or {}, timeout=_TIMEOUT,
                         headers={"Authorization": "Bearer " + (key or api_key()),
                                  "Accept": "application/json", "User-Agent": "zeus-options-terminal"})
    except Exception:
        return 0, None
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def _call(endpoint: str, path: str, params: Optional[dict] = None):
    """JSON body on 200, else None; pauses `endpoint` for the day on 401/403/429."""
    if not enabled() or is_paused(endpoint):
        return None
    status, body = _get(path, params)
    if status in _PAUSE_CODES:
        _pause(endpoint)
        return None
    return body if status == 200 and isinstance(body, dict) else None


# ─── transcripts ─────────────────────────────────────────────────────────────

def _latest_call(ticker: str, refresh: bool = False) -> Optional[dict]:
    """The newest earnings call with a transcript: {id, title, callDate, fiscalYear, fiscalQuarter}."""
    cache = _dir() / f"{_safe(ticker)}.latest.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < _LATEST_FRESH_SECS:
        try:
            return json.loads(cache.read_text("utf-8")) or None
        except Exception:
            pass
    body = _call("transcripts", f"/stocks/{ticker}/investor-events",
                 {"eventType": "EarningsCall", "limit": 8})
    if body is None:
        return None
    call = next((e for e in body.get("data") or [] if e.get("hasTranscript") and e.get("id")), None)
    if call:
        call = {k: call.get(k) for k in ("id", "title", "callDate", "fiscalYear", "fiscalQuarter")}
    try:
        cache.write_text(json.dumps(call), "utf-8")
    except Exception:
        pass
    return call


def latest_transcript(ticker: str, refresh: bool = False) -> Optional[dict]:
    """The latest earnings call's transcript, or None (no key, paused, not covered, error).

    {"ticker", "title", "callDate", "fiscalYear", "fiscalQuarter", "isFiledRecord",
     "hasSpeakerLabels", "turns": [{"speakerIndex", "speakerName", "speakerRole", "text"}]}
    """
    if not enabled():
        return None
    ticker = ticker.upper()
    call = _latest_call(ticker, refresh=refresh)
    if not call:
        return None
    fy, fq = call.get("fiscalYear"), call.get("fiscalQuarter")
    period = f"{fy}Q{fq}" if fy and fq else str(call["id"])[:8]
    cache = _dir() / f"{_safe(ticker)}_{period}.json"
    if cache.exists():                      # a past call's transcript doesn't change
        try:
            return json.loads(cache.read_text("utf-8"))
        except Exception:
            pass
    turns, first, offset = [], None, 0
    for _ in range(_MAX_PAGES):
        page = _call("transcripts", f"/investor-events/{call['id']}/speakers",
                     {"limit": _PAGE, "offset": offset})
        if page is None:
            return None
        first = first or page
        rows = page.get("data") or []
        turns += [{k: t.get(k) for k in ("speakerIndex", "speakerName", "speakerRole", "text")} for t in rows]
        offset += len(rows)
        if not page.get("hasMore") or not rows:
            break
    if not turns:
        return None
    out = {"ticker": ticker, "title": first.get("eventTitle") or call.get("title"),
           "callDate": first.get("callDate") or call.get("callDate"), "fiscalYear": fy, "fiscalQuarter": fq,
           "isFiledRecord": bool(first.get("isFiledRecord")),
           "hasSpeakerLabels": first.get("hasSpeakerLabels") is not False, "turns": turns}
    try:
        cache.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    return out


# ─── IV rank ─────────────────────────────────────────────────────────────────

def iv_rank(ticker: str) -> Optional[dict]:
    """{ivr, iv, asof} from the latest session, in the same shape and scale as
    pre_trade_checkup.data_sources.get_ivr: ivr is 0-100 (1-year rank), iv a fraction
    (Equibles' 30-day implied volatility). None when there is no usable rank.
    Cached per ticker for the UTC day: the series updates nightly."""
    if not enabled():
        return None
    ticker = ticker.upper()
    cache_file = _dir() / "ivr_cache.json"
    try:
        cache = json.loads(cache_file.read_text("utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(ticker)
    if isinstance(hit, dict) and hit.get("day") == _utc_today():
        return hit.get("value")
    body = _call("iv_rank", f"/stocks/{ticker}/options/volatility", {"limit": 1})
    if body is None:
        return None
    s = (body.get("sessions") or [None])[0] or {}
    value = None
    if s.get("ivRank") is not None:
        iv = s.get("derivedIv30") if s.get("derivedIv30") is not None else s.get("quotedIv30")
        value = {"ivr": float(s["ivRank"]), "iv": float(iv) if iv is not None else None,
                 "asof": s.get("session")}
    with _lock:
        cache = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("day") == _utc_today()}
        cache[ticker] = {"day": _utc_today(), "value": value}
        try:
            cache_file.write_text(json.dumps(cache), "utf-8")
        except Exception:
            pass
    return value


# ─── Settings → test button ──────────────────────────────────────────────────

def self_test(ticker: str = "AAPL") -> dict:
    """Ask both routes once, ignoring today's pause, and say what this key can use."""
    key = api_key()
    if not key:
        return {"ok": False, "error": "save an Equibles API key first"}
    status, body = _get(f"/stocks/{ticker}/investor-events", {"eventType": "EarningsCall", "limit": 1}, key=key)
    if status == 401:
        return {"ok": False, "error": "key rejected (401): check it in Settings"}
    if status != 200:
        msg = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else None
        return {"ok": False, "error": f"HTTP {status}" + (f": {msg}" if msg else "")}
    ev = (body.get("data") or [{}])[0]
    transcripts = f"{ticker} {ev.get('fiscalYear')} Q{ev.get('fiscalQuarter')}" if ev else "✓"
    v_status, _ = _get(f"/stocks/{ticker}/options/volatility", {"limit": 1}, key=key)
    iv = "✓" if v_status == 200 else ("not in this key's plan: ZEUS keeps its own IV data" if v_status == 403
                                      else f"HTTP {v_status}")
    return {"ok": True, "transcripts": transcripts, "iv_rank": iv}

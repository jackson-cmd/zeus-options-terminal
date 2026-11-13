"""Heatmap tab — session-aware quote state + JSON endpoints.

Two price series, tracked independently and persisted to disk:

  reg  (盘中/今日收盘)  — % vs the prior trading day's close.
      Live from DXLink/TastyTrade during RTH. FROZEN the moment the session
      closes and served from disk afterwards — never re-requested until the
      next RTH open. Survives restarts and weekends.

  ext  (盘前盘后)        — % vs today's close. Live during pre/post-market.
      Cleared at RTH open; held overnight otherwise.

Endpoints:
  GET  /api/heatmap/data       — current snapshot (metadata + reg + ext + session)
  GET  /api/heatmap/stream     — SSE push (2s live, 15s when frozen)
  POST /api/heatmap/refresh    — force a poll (RTH) / rebuild the frozen close

A background asyncio task reconciles the session each cycle (freeze on close,
clear ext on open) and, only during RTH, tops up quotes the DXLink stream
doesn't cover. When the market is closed it makes ZERO upstream requests.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import cards as _cards
from . import earnings as _earn
from . import metadata, session

# Make trading_assistant importable (same trick as optionstrat_server.py:28)
_TA_DIR = Path(__file__).resolve().parent.parent / "trading_assistant"
if str(_TA_DIR) not in sys.path:
    sys.path.insert(0, str(_TA_DIR))

# Lazy-imported on first poll so module import never fails if TT creds missing
_TastyClient = None


def _get_client():
    global _TastyClient
    if _TastyClient is None:
        from api_client import TastyClient as _TC  # type: ignore
        _TastyClient = _TC
    return _TastyClient()


# ─── Snapshot state ────────────────────────────────────────────────────────

_BATCH = 50
_POLL_INTERVAL_MARKET = 15  # seconds, during RTH (stream-down fallback)
_POLL_INTERVAL_OFF = 30     # seconds, off-hours — just to catch session transitions

# Two persisted price series. `frozen` reg is read-only until the next open.
_reg: dict = {"trading_date": None, "frozen": False, "tickers": {}}
_ext: dict = {"trading_date": None, "tickers": {}}

_generated_at: Optional[str] = None
_last_poll_ok: bool = False
_last_error: Optional[str] = None

_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "heatmap" / "snapshot.json"


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_persisted() -> None:
    """Load the on-disk reg/ext snapshot at startup (best-effort)."""
    global _generated_at
    try:
        data = json.loads(_SNAPSHOT_PATH.read_text())
    except Exception:
        return
    if isinstance(data.get("reg"), dict) and isinstance(data["reg"].get("tickers"), dict):
        _reg.update({k: data["reg"][k] for k in ("trading_date", "frozen", "tickers")
                     if k in data["reg"]})
    if isinstance(data.get("ext"), dict) and isinstance(data["ext"].get("tickers"), dict):
        _ext.update({k: data["ext"][k] for k in ("trading_date", "tickers")
                     if k in data["ext"]})
    _generated_at = data.get("generated_at") or _generated_at
    print(f"[heatmap] loaded snapshot: reg={len(_reg['tickers'])} "
          f"(date={_reg.get('trading_date')}, frozen={_reg.get('frozen')}), "
          f"ext={len(_ext['tickers'])}")


def _save_persisted() -> None:
    try:
        _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_PATH.write_text(json.dumps(
            {"reg": _reg, "ext": _ext, "generated_at": _generated_at},
            separators=(",", ":")))
    except Exception as e:
        print(f"[heatmap] snapshot save failed: {e}")


# ─── DXLink streaming state ─────────────────────────────────────────────────
_STREAM_FILL_SECS = 30

_stream: dict = {"connected": False, "last_msg": 0.0, "syms": set(), "n": 0}

# Stable prior-session close per symbol (date-aware, hourly from yfinance).
_prev_close: dict = {}
_prev_close_at: float = 0.0
_PREV_TTL = 3600


def _on_stream_quote(sym: str, reg_last: float, ext_last: float, dx_prev: float = 0.0) -> None:
    """Stream tick. reg_last = RTH last (frozen at close); ext_last = pre/post.

    reg is written only during RTH and only while not frozen; ext only during
    pre/post-market. This is what stops off-hours pushes from disturbing the
    frozen close, and stops the regular session from polluting ext."""
    phase, _ = session.session_now()
    # DXLink's Summary.prevDayClose IS the prior-session close TradingView/TT use,
    # pushed live. TRUST IT over the yfinance daily-bar map (_prev_close): Yahoo
    # backfills the prior session late and per-symbol, so that map can silently
    # drop the basis back to a 2-day-old close (on 2026-06-16 NVDA read +2% vs
    # the real -1.4% from exactly this). During RTH dxfeed is authoritative, so
    # cache it into _prev_close for the freeze / reconnect / cold-start paths too.
    # (It rolls to today's close after the session, but reg is frozen at close,
    # so post-session ticks never recompute change_pct.)
    if phase == session.RTH and dx_prev and dx_prev > 0:
        _prev_close[sym] = round(dx_prev, 4)
    prev = dx_prev or _prev_close.get(sym) or 0.0

    if phase == session.RTH and not _reg["frozen"]:
        if reg_last and reg_last > 0 and prev > 0:
            _reg["tickers"][sym] = {
                "last": round(reg_last, 4),
                "prev_close": round(prev, 4),
                "change_pct": round((reg_last / prev - 1.0) * 100.0, 3),
            }

    if phase in (session.PRE, session.POST):
        # ext vs today's close. dxfeed Trade (RTH-only) holds today's close in
        # POST and the prior close in PRE, so reg_last is the right basis for both.
        base = reg_last if (reg_last and reg_last > 0) else prev
        if ext_last and ext_last > 0 and base > 0:
            _ext["tickers"][sym] = {
                "ext_price": round(ext_last, 4),
                "ext_change_pct": round((ext_last / base - 1.0) * 100.0, 3),
            }

    _stream["syms"].add(sym)
    _stream["last_msg"] = time.time()


def _refresh_prev_close() -> None:
    """Fill the prior-session close map from the LOCAL daily store (DXLink Candle).

    The live basis is DXLink's Summary.prevDayClose, cached into _prev_close on
    every RTH tick (see _on_stream_quote); this is the durable fallback for
    cold-start / non-streamed symbols and for the frozen-board reconstruction.
    Source = `daily_store` (authoritative raw daily bars, always carries the last
    completed session). yfinance is no longer used here — it backfills the prior
    bar late + per-symbol, which silently lagged the basis a full session. Hourly TTL."""
    global _prev_close_at
    if _prev_close and (time.time() - _prev_close_at) < _PREV_TTL:
        return
    try:
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        today = (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date()
    syms = metadata.tickers()
    if not syms:
        return
    try:
        from . import daily_store
        n = 0
        for t in syms:
            pc = daily_store.get_prev_close(t, today)
            if pc and pc > 0:
                _prev_close[t] = round(pc, 4); n += 1
        if n:
            _prev_close_at = time.time()
        print(f"[heatmap] prev_close from local store: {n}/{len(syms)} symbols "
              f"(prior to {today})")
    except Exception as e:
        print(f"[heatmap] prev_close refresh failed: {e}")


# Refresh the local daily store at most once per ET day: cold-start backfill if
# empty, else append the last few completed sessions (yesterday's final close).
# Runs in an executor thread (asyncio.run inside DXLink fetch) and fire-and-forget
# from the poll loop, so a multi-minute cold-start backfill never blocks it.
_store_refreshed_date: Optional[str] = None
_store_refreshing = False
_poller_started_at = 0.0
# Don't open candle connections during the live quote stream's post-restart
# warm-up (it re-subscribes 696 symbols over ~30-60s; competing DXLink
# connections lengthen the window where unfilled cells render dark). The daily
# append isn't urgent — the store already holds yesterday — so defer it.
_STORE_WARMUP_GRACE = 150


def _maybe_refresh_daily_store() -> None:
    global _store_refreshed_date, _store_refreshing, _prev_close_at
    try:
        from zoneinfo import ZoneInfo
        today = str(datetime.datetime.now(ZoneInfo("America/New_York")).date())
    except Exception:
        today = str((datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date())
    if _store_refreshed_date == today or _store_refreshing:
        return
    try:
        from . import daily_store, backfill_daily
    except Exception as e:
        print(f"[heatmap] daily store refresh import err: {e}")
        return
    empty = (daily_store.coverage() == 0)
    # Cold start (empty store) runs immediately — there's no live board to protect
    # yet. A non-empty store's daily append waits out the warm-up grace.
    if not empty and (time.time() - _poller_started_at) < _STORE_WARMUP_GRACE:
        return
    _store_refreshing = True
    try:
        if empty:
            print("[heatmap] daily store empty — backfilling from DXLink Candle…")
            backfill_daily.backfill()
        else:
            n = backfill_daily.refresh_recent(days=10)
            print(f"[heatmap] daily store appended ({n} symbols, last={daily_store.last_date()})")
        if daily_store.coverage() > 0:
            _store_refreshed_date = today
            _prev_close_at = 0.0  # let the next _refresh_prev_close re-read the fresh store
    except Exception as e:
        print(f"[heatmap] daily store refresh err: {e}")
    finally:
        _store_refreshing = False


def _on_stream_status(connected: bool, n: int) -> None:
    _stream["connected"] = connected
    _stream["n"] = n
    if not connected:
        _stream["syms"] = set()
    print(f"[heatmap] DXLink {'connected' if connected else 'disconnected'} ({n} syms)")


def _stream_healthy() -> bool:
    return _stream["connected"] and (time.time() - _stream["last_msg"] < 60)


def live_prices() -> dict:
    """公开只读快照 {ticker: {"last", "prev_close", "change_pct"}}(universe 键)。

    给 sr_alert 的盘中触及告警用(见 optionstrat_server.sr_zone_alert_loop)。
    · 键已由 dxlink_stream:216 的 _from_dxfeed 归一,无需调用方再转。
    · prev_close = dxfeed Summary.prevDayClose(与券商/TradingView 同源的权威前收)。
    · ⚠️ RTH 且未冻结时才由流写入;盘后留着冻结的收盘价,**开盘瞬间可能仍是昨天的**
      (_reconcile_session 换日只清 _ext 不清 _reg)→ 调用方须自行做 RTH 判断 + 开盘预热。
    """
    return dict(_reg["tickers"])


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


# yfinance fallback for reg. TastyTrade /market-data/by-type returns data only
# for entitled symbols (and is sparse on weekends); yfinance gives reliable
# delayed last + prev close for the whole universe. Cached 5 min.
_yf_cache: dict = {"ts": 0.0, "data": {}}
_YF_TTL = 300


def _yf_quotes(tickers: list) -> dict:
    now = time.time()
    if _yf_cache["data"] and (now - _yf_cache["ts"]) < _YF_TTL:
        return _yf_cache["data"]
    out: dict = {}
    try:
        import zeus_yf
        df = zeus_yf.download(tickers, period="5d", progress=False,
                              auto_adjust=False, threads=True)
        close = df["Close"]
        single = (getattr(close, "ndim", 1) == 1)
        for t in tickers:
            try:
                s = close.dropna() if single else close[t].dropna()
                if len(s) >= 2:
                    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
                    if last > 0 and prev > 0:
                        out[t] = {"last": round(last, 4), "prev_close": round(prev, 4),
                                  "change_pct": round((last / prev - 1) * 100.0, 3)}
            except Exception:
                continue
    except Exception as e:
        print(f"[heatmap] yfinance fallback failed: {e}")
    missing = [t for t in tickers if t not in out]
    if missing:
        try:
            import zeus_finviz as _fv     # Yahoo rate-limited or partial → Finviz
            out.update(_fv.quotes(missing, max_age=120))
        except Exception as e:
            print(f"[heatmap] finviz fallback failed: {e}")
    if out:
        _yf_cache["ts"] = now
        _yf_cache["data"] = out
    return out


def _poll_once_sync() -> dict:
    """Synchronous one-shot batch poll for the reg series. Returns
    ticker → {last, prev_close, change_pct}. Run via run_in_executor."""
    client = _get_client()
    universe = metadata.tickers()
    out: dict = {}
    for i in range(0, len(universe), _BATCH):
        chunk = universe[i:i + _BATCH]
        try:
            resp = client.get(
                "/market-data/by-type",
                params={"equity": ",".join(chunk)},
            )
        except Exception as e:
            print(f"[heatmap] batch {i}-{i+len(chunk)} failed: {e}")
            continue
        for it in resp.get("data", {}).get("items", []):
            sym = it.get("symbol", "")
            if not sym:
                continue
            last = _f(it.get("mark")) or _f(it.get("last")) or _f(it.get("close"))
            prev = _f(it.get("prev-close")) or _f(it.get("close"))
            if last <= 0:
                continue
            change_pct = ((last - prev) / prev * 100.0) if prev > 0 else 0.0
            out[sym] = {
                "last": round(last, 4),
                "prev_close": round(prev, 4),
                "change_pct": round(change_pct, 3),
            }
    missing = [t for t in universe if t not in out]
    if missing:
        for t, q in _yf_quotes(universe).items():
            if t not in out:
                out[t] = q
    return out


def _reconstruct_reg_from_yf(close_date: datetime.date, only: Optional[list] = None) -> dict:
    """Rebuild reg for `close_date` from yfinance daily bars: last = close on
    that date, prev = the session before it. Used to freeze a closed session
    the server didn't witness live (cold start on a weekend)."""
    syms = list(only) if only is not None else metadata.tickers()
    if not syms:
        return {}
    out: dict = {}
    try:
        import zeus_yf
        df = zeus_yf.download(syms, period="1mo", interval="1d", progress=False,
                              auto_adjust=False, threads=True)
        close = df["Close"]
        single = (getattr(close, "ndim", 1) == 1)
        for t in syms:
            try:
                s = close.dropna() if single else close[t].dropna()
                upto = []
                for dt, val in s.items():
                    d = dt.date() if hasattr(dt, "date") else dt
                    if d <= close_date and val and val > 0:
                        upto.append(float(val))
                if len(upto) >= 2:
                    last, prev = upto[-1], upto[-2]
                    out[t] = {"last": round(last, 4), "prev_close": round(prev, 4),
                              "change_pct": round((last / prev - 1) * 100.0, 3)}
            except Exception:
                continue
    except Exception as e:
        print(f"[heatmap] reg reconstruct failed: {e}")
    return out


def _reconstruct_reg(close_date: datetime.date, only: Optional[list] = None) -> dict:
    """Rebuild reg for `close_date`, preferring the local daily store (DXLink
    Candle — authoritative, never lags). Falls back to yfinance only for what the
    store can't cover, e.g. a same-day freeze where close_date == today (the store
    drops today's in-progress bar, so it can't supply today's close yet)."""
    syms = list(only) if only is not None else metadata.tickers()
    if not syms:
        return {}
    out: dict = {}
    try:
        import pandas as pd
        from . import daily_store
        cutoff = pd.Timestamp(close_date).normalize()
        for t in syms:
            df = daily_store.get_ohlc(t)
            if df is None or not len(df):
                continue
            s = df.loc[df.index <= cutoff, "Close"]
            s = s[s > 0]
            # only trust the store when it actually holds close_date's bar as the
            # latest one — otherwise "last" would be an earlier session.
            if len(s) >= 2 and s.index[-1].normalize() == cutoff:
                last, prev = float(s.iloc[-1]), float(s.iloc[-2])
                out[t] = {"last": round(last, 4), "prev_close": round(prev, 4),
                          "change_pct": round((last / prev - 1) * 100.0, 3)}
    except Exception as e:
        print(f"[heatmap] store reconstruct err: {e}")
    rest = [t for t in syms if t not in out]
    if rest:
        out.update(_reconstruct_reg_from_yf(close_date, only=rest))
    rest = [t for t in syms if t not in out]
    if rest:
        try:
            phase, _sd = session.session_now()
            # Finviz shows the latest session only, so it can stand in for the
            # most recent close (not for an older date, not during the session).
            if phase != session.RTH and close_date == session.last_rth_close_date():
                import zeus_finviz as _fv
                out.update(_fv.quotes(rest, max_age=300))
        except Exception as e:
            print(f"[heatmap] finviz reconstruct err: {e}")
    return out


def _ensure_frozen_reg(close_date: datetime.date, force: bool = False) -> None:
    """Make reg a complete, frozen snapshot for `close_date`.

    Keeps any live values already collected for that date and backfills the
    rest from yfinance daily bars, then marks the series frozen + persists.
    No-op if already frozen for that date (unless force)."""
    global _generated_at
    cd = str(close_date)
    if not force and _reg.get("frozen") and _reg.get("trading_date") == cd:
        return
    have = dict(_reg["tickers"]) if _reg.get("trading_date") == cd else {}
    universe = metadata.tickers()
    missing = [t for t in universe if t not in have]
    if missing:
        recon = _reconstruct_reg(close_date, only=missing)
        have = {**recon, **have}  # prefer live values we already had
    if not have:
        # Couldn't build anything (yfinance down). Leave unfrozen so the next
        # cycle retries rather than freezing an empty board.
        print(f"[heatmap] freeze for {cd} produced no data — will retry")
        return
    _reg["tickers"] = have
    _reg["trading_date"] = cd
    _reg["frozen"] = True
    _generated_at = _now_iso()
    _save_persisted()
    print(f"[heatmap] reg frozen for {cd} ({len(have)} symbols)")


def _reconcile_session() -> str:
    """Apply session transitions to the persisted series. Returns the phase.

    RTH       → ensure a live reg series for today + clear ext (at open).
    otherwise → ensure reg is frozen at the last completed RTH close.
    Called every poll cycle and at startup. Cheap + idempotent when steady."""
    phase, sdate = session.session_now()

    if phase == session.RTH:
        if _reg.get("trading_date") != str(sdate) or _reg.get("frozen"):
            _reg["trading_date"] = str(sdate)
            _reg["frozen"] = False
            # Clear ext at RTH open (user preference): pre-market values stop
            # being meaningful once the regular session is underway.
            _ext["tickers"] = {}
            _ext["trading_date"] = str(sdate)
            _save_persisted()
            print(f"[heatmap] RTH open {sdate} — reg live, ext cleared")
    else:
        last_close = session.last_rth_close_date()
        if last_close is not None and (
            _reg.get("trading_date") != str(last_close) or not _reg.get("frozen")
        ):
            _ensure_frozen_reg(last_close)

    return phase


def _merge_reg(quotes: dict) -> None:
    """Merge REST/yfinance reg quotes into _reg without blanking existing
    tickers and never touching a frozen series."""
    if _reg["frozen"]:
        return
    for sym, q in quotes.items():
        if q:
            _reg["tickers"][sym] = q


def _fill_uncovered_yf() -> None:
    """During RTH, fill reg for symbols the DXLink stream doesn't cover
    (non-entitled / odd symbols) with yfinance. Never overwrites stream-covered
    symbols and never touches a frozen series."""
    if _reg["frozen"]:
        return
    universe = metadata.tickers()
    covered = _stream["syms"]
    missing = [t for t in universe if t not in covered]
    if not missing:
        return
    yq = _yf_quotes(universe)  # cached full-universe download (5 min TTL)
    for t in missing:
        q = yq.get(t)
        if q:
            _reg["tickers"][t] = q


async def _poll_loop() -> None:
    """Background task — reconcile session + keep reg fresh during RTH only.

    Off-hours it makes zero upstream calls: it just re-checks the session so it
    can freeze on close / clear ext on open."""
    global _last_error, _last_poll_ok, _generated_at
    loop = asyncio.get_running_loop()
    while True:
        phase = session.CLOSED
        try:
            phase = await loop.run_in_executor(None, _reconcile_session)
            # Keep the local daily store current (once/ET-day; cold-start backfill
            # if empty). Fire-and-forget so a multi-minute backfill never blocks
            # the loop; the busy-guard prevents pile-up.
            loop.run_in_executor(None, _maybe_refresh_daily_store)
            if phase == session.RTH:
                await loop.run_in_executor(None, _refresh_prev_close)
                if _stream_healthy():
                    await loop.run_in_executor(None, _fill_uncovered_yf)
                else:
                    quotes = await loop.run_in_executor(None, _poll_once_sync)
                    _merge_reg(quotes)
                _generated_at = _now_iso()
            elif phase in (session.PRE, session.POST):
                # ext arrives via the stream; just keep prev_close current so
                # the basis is right. No full universe poll (yfinance has no
                # pre/post data anyway).
                await loop.run_in_executor(None, _refresh_prev_close)
                _generated_at = _now_iso()
            # CLOSED: nothing — reg is frozen and served from memory/disk.
            _last_poll_ok = True
            _last_error = None
        except Exception as e:
            _last_poll_ok = False
            _last_error = str(e)
            print(f"[heatmap] poll loop error: {e}")

        if phase == session.RTH:
            await asyncio.sleep(_STREAM_FILL_SECS if _stream_healthy()
                                else _POLL_INTERVAL_MARKET)
        else:
            await asyncio.sleep(_POLL_INTERVAL_OFF)


def start_background_poller() -> None:
    """Called from server startup. Loads the persisted snapshot, reconciles the
    current session once (so a weekend cold start shows the frozen close
    immediately), then spawns the polling coroutine."""
    global _poller_started_at
    _poller_started_at = time.time()   # warm-up grace baseline for the store append
    _load_persisted()
    try:
        loop = asyncio.get_event_loop()
        # Pre-warm the XNYS calendar off the event loop so the poll loop's first
        # session_now() is instant. The poll loop is the SOLE owner of reconcile
        # (and thus of any cold-start rebuild) — running it here too would race
        # and trigger a duplicate full yfinance download.
        loop.run_in_executor(None, session.session_now)
        loop.create_task(_poll_loop())
        loop.create_task(_earnings_loop())
        print("[heatmap] background poller started")
    except RuntimeError as e:
        print(f"[heatmap] couldn't start poller: {e}")


def warm_etf_trends() -> None:
    """Warm the ETF-trends + risk-stats caches off the event loop so the first
    tab open is instant. Both respect their on-disk TTL, so a warm cache is just
    a cheap disk read (ETF 6h, risk-stats 1 week)."""
    try:
        from . import etf_trends, risk_stats, stock_perf
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, etf_trends.get)
        loop.run_in_executor(None, risk_stats.get)
        loop.run_in_executor(None, stock_perf.get)
        print("[heatmap] ETF-trends + risk-stats + stock-perf warm kicked")
    except Exception as e:
        print(f"[heatmap] couldn't warm ETF trends / risk / perf: {e}")


def start_dxlink_stream() -> None:
    """Spawn the DXLink real-time quote stream. Pushes into _reg/_ext; the
    poll loop is the off-stream fallback during RTH."""
    try:
        from . import dxlink_stream
        loop = asyncio.get_event_loop()
        loop.create_task(dxlink_stream.stream_forever(
            get_symbols=lambda: metadata.tickers(),
            on_quote=_on_stream_quote,
            on_status=_on_stream_status,
        ))
        print("[heatmap] DXLink stream task started")
    except Exception as e:
        print(f"[heatmap] couldn't start DXLink stream: {e}")


# ─── Snapshot assembler ────────────────────────────────────────────────────

_CARDS_CACHE: dict = {"data": {}, "ts": 0.0, "day": ""}
_CARDS_TTL = 300.0        # 5 分钟 —— SR 是 5s 轮询落库, 但卡片一览不必那么实时


def _card_lines(tickers) -> dict:
    """{ticker: {sr?, nb?, ai?}} —— Graph 卡片下方的三行(SR/多空/AI)。

    ⚠️ SR 只取**当天**且落库时已按天清理(见 optionstrat_server 的 SR 清库),
    多空取最近一天的 narratives, AI 取当天 ai_picks。三个源都是本地文件,
    任何一个读不到就少画那一行, 绝不让整块炸掉。
    """
    today = datetime.datetime.now().date().isoformat()
    now = time.time()
    if (_CARDS_CACHE["day"] == today and _CARDS_CACHE["data"]
            and (now - _CARDS_CACHE["ts"]) < _CARDS_TTL):
        return _CARDS_CACHE["data"]

    root = Path(__file__).resolve().parent.parent

    sr_entries = []
    try:
        sr_entries = (json.loads(
            (root / "options_analyzer" / "data" / "sr_alert_log.json").read_text(encoding="utf-8")
        ) or {}).get("entries") or []
    except Exception:
        pass

    narr = []
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from narratives import store as _ns
        days = _ns.list_days()
        if days:
            narr = _ns.load_day(max(days)) or []
    except Exception:
        pass

    picks = {}
    try:
        picks = (json.loads(
            (root / "data" / "ai_picks" / f"{today}.json").read_text(encoding="utf-8")
        ) or {}).get("picks") or {}
    except Exception:
        pass

    out = _cards.build_all(tickers, today, sr_entries, narr, picks)
    _CARDS_CACHE.update(data=out, ts=now, day=today)
    return out


def _build_response() -> dict:
    """Join metadata + reg + ext; drop tickers not in the current universe."""
    meta = metadata.get_all()
    etfs = metadata.etf_set()
    universe = set(metadata.tickers())
    phase, _ = session.session_now()
    regt = _reg["tickers"]
    extt = _ext["tickers"]
    # 财报日:本地库只读(刷新由 _earnings_loop 后台按需做)。带进每个 item →
    # treemap / 图墙 / 表格三个视图零额外请求都能用。
    earn_map = _earn.get_map(universe)
    card_map = _card_lines(universe)      # Graph 卡片三行(SR/多空/AI), 5 分钟缓存
    items = []
    for t, m in meta.items():
        if t not in universe:
            continue
        r = regt.get(t)
        e = extt.get(t)
        last = (r or {}).get("last", 0.0)
        items.append({
            "ticker": t,
            "name": m.get("name", t),
            "sector": m.get("sector", "Unknown"),
            "industry": m.get("industry", "Unknown"),
            "market_cap": m.get("market_cap", 0.0),
            "last": last,
            "prev_close": (r or {}).get("prev_close", 0.0),
            "change_pct": (r or {}).get("change_pct", 0.0),
            # pre/post-market; falls back to the regular price when absent
            "ext_price": (e or {}).get("ext_price", last),
            "ext_change_pct": (e or {}).get("ext_change_pct", 0.0),
            "has_quote": (r is not None) or (e is not None),
            "is_etf": t in etfs,
            # {date, time('am'盘前/'pm'盘后), days(负=已过)} 或 None
            "earnings": earn_map.get(t),
            # True = 这标的本身没有财报(ETF/基金) → 前端不画财报行, 区别于「没拿到」
            "no_earnings": (t in etfs) or (t in _earn.NO_EARNINGS),
            # Graph 卡片下方三行: {sr?:{hhmm,dir,zone,why}, nb?:str, ai?:{verdict,confidence,news,why}}
            "card": card_map.get(t),
        })
    return {
        "generated_at": _generated_at,
        "last_poll_ok": _last_poll_ok,
        "last_error": _last_error,
        "count": len(items),
        "items": items,
        "session": phase,
        "frozen": bool(_reg["frozen"]),
        "trading_date": _reg.get("trading_date"),
        "stream": {
            "live": _stream_healthy(),
            "connected": _stream["connected"],
            "symbols": len(_stream["syms"]),
        },
    }


# ─── FastAPI router ────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/heatmap", tags=["heatmap"])


# ─── 定时弹窗(每日 4 档异动新闻 Mover News 必看) ─────────────────────────────
# 服务端调度器(optionstrat_server.heatmap_popup_loop)在本地 9/11/14/20 点触发:
#   ① 原生通知(terminal-notifier 可点 → 打开 PWA 深链 ?mvnews=slot / osascript 兜底)
#   ② 这里记一笔「待弹」状态;打开着的 PWA 轮询 /popup_state,新鲜未看过就自动跳到
#      Mover News 视图(±2% 异动票 + digest/finviz 双语新闻)。FRESH_SECS 内有效。
#   kind 标明该开哪个视图(当前只有 movernews);向后兼容默认 movernews。
_POPUP_STATE: dict = {"slot": None, "label": None, "kind": None,
                      "fired_at": 0.0, "date": None}
_POPUP_FRESH_SECS = 90 * 60   # 触发后 90 分钟内打开 PWA 仍自动弹;超时算错过


def set_popup_due(slot: str, label: str, kind: str = "movernews") -> None:
    """由调度器调用,标记某个时段该弹了。kind=要打开的视图(默认 movernews)。"""
    _POPUP_STATE.update(slot=slot, label=label, kind=kind, fired_at=time.time(),
                        date=datetime.date.today().isoformat())


@router.get("/popup_state")
def popup_state():
    """PWA 轮询:是否有新鲜的「该弹」时段。id = 日期:时段,前端按它去重(localStorage),
    保证同一时段一天只自动弹一次。kind=该打开的视图。"""
    st = _POPUP_STATE
    fresh = bool(st["fired_at"]) and (time.time() - st["fired_at"]) < _POPUP_FRESH_SECS
    if fresh:
        return {"due": True, "slot": st["slot"], "label": st["label"],
                "kind": st.get("kind") or "movernews",
                "fired_at": st["fired_at"], "id": f"{st['date']}:{st['slot']}"}
    return {"due": False}


@router.get("/data")
def data_endpoint():
    return _build_response()


@router.get("/stream")
async def stream_endpoint(request: Request):
    """Server-Sent Events: push the snapshot every 2s while live so DXLink
    real-time data shows without REST polling. When the reg series is frozen
    (off-hours), slow to 15s — the data isn't changing, so there's nothing to
    re-request; the connection stays open to resume instantly at the next open."""
    async def gen():
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = json.dumps(_build_response(), separators=(",", ":"))
            except Exception as e:
                payload = json.dumps({"error": str(e)})
            yield f"data: {payload}\n\n"
            await asyncio.sleep(15 if _reg["frozen"] else 2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.get("/etf_trends")
def etf_trends_endpoint():
    """Multi-period ETF returns (3d/7d/1mo/3mo/1yr) — leaders vs laggards.
    Sync def → runs in a threadpool, so the cold yfinance compute (~5s) doesn't
    block the event loop. Not real-time (cached ~6h)."""
    from . import etf_trends
    return etf_trends.get()


@router.post("/etf_trends/refresh")
async def etf_trends_refresh_endpoint():
    from . import etf_trends
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: etf_trends.get(force=True))


@router.get("/risk_stats")
def risk_stats_endpoint(universe: str = "stocks"):
    """Beta / correlation / volatility vs SPY & QQQ over 3M/6M/1Y, for the stock
    universe or the curated ETF list (?universe=etf). Cached ~1 week. Sync def →
    runs in a threadpool so the cold yfinance compute doesn't block the loop."""
    from . import risk_stats
    return risk_stats.get(universe=universe)


@router.post("/risk_stats/refresh")
async def risk_stats_refresh_endpoint(universe: str = "stocks"):
    from . import risk_stats
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: risk_stats.get(force=True, universe=universe))


@router.get("/stock_perf")
def stock_perf_endpoint():
    """Multi-period % returns (3天/1周/1月/3月/1年) per ticker for the 涨跌榜
    beeswarm. Not real-time (cached ~1 day). Sync def → threadpool."""
    from . import stock_perf
    return stock_perf.get()


@router.post("/stock_perf/refresh")
async def stock_perf_refresh_endpoint():
    from . import stock_perf
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: stock_perf.get(force=True))


# ── Momentum signals (rebound / pullback) — 30 分钟缓存, 收盘后才变 ──
_MOMENTUM_CACHE: dict = {"data": None, "ts": 0.0}
_MOMENTUM_TTL = 1800.0  # 30 分钟


def _momentum_get(force: bool = False) -> dict:
    """Read-through cache for heatmap.momentum.compute_signals()."""
    now = time.time()
    if not force and _MOMENTUM_CACHE["data"] and (now - _MOMENTUM_CACHE["ts"]) < _MOMENTUM_TTL:
        return _MOMENTUM_CACHE["data"]
    from . import momentum
    out = momentum.compute_signals()
    _MOMENTUM_CACHE["data"] = out
    _MOMENTUM_CACHE["ts"] = now
    return out


@router.get("/momentum_signals")
async def momentum_signals_endpoint():
    """📈 触底反弹 + 📉 趋势回撤 候选 tickers, 从 daily_ohlc.parquet 算。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _momentum_get)


@router.post("/momentum_signals/refresh")
async def momentum_signals_refresh_endpoint():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _momentum_get(force=True))


# ── 财报日历 ────────────────────────────────────────────────────────
# 本地长存, 只在「已过期」或「≤21 天且记录 >3 天没刷」时才请求(见 earnings.py),
# 所以这个 loop 每 6 小时跑一趟, 实际每次只碰几十只临近财报的票。

def _earnings_refresh_sync(force: bool = False) -> dict:
    recs = _earn.refresh(sorted(metadata.tickers()), etfs=metadata.etf_set(), force=force)
    return {"tickers": len(recs),
            "with_date": sum(1 for v in recs.values() if v.get("date"))}


async def _earnings_loop() -> None:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(90)          # 让 metadata / 行情先起来
    while True:
        try:
            await loop.run_in_executor(None, _earnings_refresh_sync)
        except Exception as e:
            print(f"[heatmap-earnings] loop err: {e}")
        await asyncio.sleep(6 * 3600)


@router.get("/card_full_all")
async def card_full_all_endpoint():
    """一键全展开用: {ticker: {ai?, nb?, sr?}} 全 universe。不含公司简介(全展开只要
    AI + 多空; 简介进「深度」详情页)。一次拉完, 免得 160 张卡各发一个请求。"""
    def _work():
        today = datetime.datetime.now().date().isoformat()
        root = Path(__file__).resolve().parent.parent
        sr, narr, picks = [], [], {}
        try:
            sr = (json.loads((root / "options_analyzer" / "data" / "sr_alert_log.json")
                             .read_text(encoding="utf-8")) or {}).get("entries") or []
        except Exception:
            pass
        try:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from narratives import store as _ns
            days = _ns.list_days()
            if days:
                narr = _ns.load_day(max(days)) or []
        except Exception:
            pass
        try:
            picks = (json.loads((root / "data" / "ai_picks" / f"{today}.json")
                                .read_text(encoding="utf-8")) or {}).get("picks") or {}
        except Exception:
            pass
        out = {}
        for t in metadata.tickers():
            f = _cards.build_full(t, today, sr, narr, picks, None)   # profiles=None → 无简介
            if len(f) > 1:      # 除 ticker 外还有内容
                out[t] = f
        return out
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _work)


@router.get("/card_full/{ticker}")
async def card_full_endpoint(ticker: str):
    """点卡片就地展开的全文: AI 全文 + 多空全 bullet + 公司简介 + 当天 SR。
    读本地库, 轻量(几十 ms), 不像 /ta/ iframe 那样慢。"""
    def _work():
        today = datetime.datetime.now().date().isoformat()
        root = Path(__file__).resolve().parent.parent
        sr, narr, picks, profiles = [], [], {}, {}
        try:
            sr = (json.loads((root / "options_analyzer" / "data" / "sr_alert_log.json")
                             .read_text(encoding="utf-8")) or {}).get("entries") or []
        except Exception:
            pass
        try:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from narratives import store as _ns
            days = _ns.list_days()
            if days:
                narr = _ns.load_day(max(days)) or []
        except Exception:
            pass
        try:
            picks = (json.loads((root / "data" / "ai_picks" / f"{today}.json")
                                .read_text(encoding="utf-8")) or {}).get("picks") or {}
        except Exception:
            pass
        try:
            profiles = json.loads((root / "data" / "company_profiles.json")
                                  .read_text(encoding="utf-8")) or {}
        except Exception:
            pass
        return _cards.build_full(ticker, today, sr, narr, picks, profiles)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _work)


@router.get("/earnings")
async def earnings_endpoint():
    """{ticker: {date, time, days}} —— 只读本地库。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _earn.get_map(metadata.tickers()))


@router.post("/earnings/refresh")
async def earnings_refresh_endpoint(force: bool = False):
    """按需刷新(force=1 强制全量重拉, 平时只刷该刷的)。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _earnings_refresh_sync(force=force))


# ── 期权波动实验 API 暂停使用(结论已收敛, 代码保留) ──────────────────
# 恢复时: 解注释下方 3 个路由 + heatmap.py 的按钮/panel/JS/state
#
# @router.get("/options_lab")
# def options_lab_report(date: str | None = None): ...
#
# @router.get("/options_smile")
# def options_smile_report(date: str | None = None, ticker: str | None = None): ...
#
# @router.get("/options_experiments")
# def options_experiments_report(date: str | None = None): ...
# ─────────────────────────────────────────────────────────────────────


@router.post("/refresh")
async def refresh_endpoint():
    """Force a refresh. During RTH → poll TastyTrade and merge. Off-hours →
    rebuild the frozen close from yfinance (so a manual refresh can repair a
    bad snapshot without waiting for the next session)."""
    global _generated_at, _last_poll_ok
    loop = asyncio.get_running_loop()
    phase, _ = session.session_now()
    try:
        if phase == session.RTH:
            quotes = await loop.run_in_executor(None, _poll_once_sync)
            _merge_reg(quotes)
            _generated_at = _now_iso()
        else:
            last_close = session.last_rth_close_date()
            if last_close is not None:
                await loop.run_in_executor(
                    None, lambda: _ensure_frozen_reg(last_close, force=True))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    _last_poll_ok = True
    return _build_response()


@router.post("/metadata/refresh")
async def metadata_refresh_endpoint():
    """Force-refresh the sector/industry/market-cap cache. Slow (network)."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: metadata.refresh(force=True))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "count": len(metadata.get_all())}

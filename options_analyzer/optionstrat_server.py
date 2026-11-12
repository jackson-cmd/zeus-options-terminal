#!/usr/bin/env python3
"""
Live OptionStrat server — FastAPI + DXLink WebSocket streaming.
Serves the P/L analyzer with real-time price updates from TastyTrade.

Usage:
    python3 options_analyzer/optionstrat_server.py
"""

import sys
import os
import json
import shlex
import time
import asyncio
import datetime
import subprocess
import glob
import re
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd

# Add trading_assistant to path
sys.path.insert(0, str(Path(__file__).parent.parent / "trading_assistant"))
# Also put the project root on sys.path so `from heatmap.* import …` works
# without needing each importer to fiddle with paths.
sys.path.insert(0, str(Path(__file__).parent.parent))
# ── ZEUS bootstrap: settings (~/.zeus-terminal/config.json) + bring-your-own AI ──
import zeus_config as _hc  # noqa: E402
import zeus_ai as _hai  # noqa: E402
import zeus_news as _zeus_news  # noqa: E402
_kept = _hc.relocate_runtime_dirs()  # runtime data lives in ~/.zeus-terminal/runtime, never in the code folder
if _kept:
    print(f"[ZEUS] could not link {_kept} outside the code folder; they stay in place (git-ignored)")
_hai.install_cli_shim()            # every AI feature now talks to the provider chosen in Settings
_hc.apply_notification_shims()     # desktop alerts follow the Settings switch (macOS only)
_hc.barchart_credentials_file()    # Barchart login (optional) from Settings → ~/.zeus-terminal
_hc.ensure_default_data()          # first run: seed the default ticker universe
os.environ["BENZINGA_COOKIES_PATH"] = str(_hc.benzinga_cookies_path())
try:
    import mustread as _mr          # digest「必读」判定纯函数(规则即时算,零外部依赖)
except Exception:
    _mr = None
try:
    from checkup.risk_radar import TOPIC_KW as _RR_KW, LIFT as _RR_LIFT, LABEL as _RR_LABEL
except Exception:
    _RR_KW, _RR_LIFT, _RR_LABEL = {}, {}, {}
from api_client import TastyClient

# Import data-fetching functions from existing analyzer
from optionstrat_analyzer import (
    group_into_strategies,
    build_opening_order_map,
    enrich_with_mid_prices,
    _safe_float,
    _parse_expiration,
    _parse_strike,
    _parse_option_type,
    _name_strategy,
    HTML_TEMPLATE,
    RISK_FREE_RATE,
)
# strategy_builder tab removed 2026-07-30 (lightweight refactor)
# strategy_builder_api._bsm_vega is still imported lazily inside functions.

# --- history news ---
from history_news_api import router as history_news_router
from history_news import HISTORY_NEWS_HTML, HISTORY_NEWS_JS
# --- /history news ---

# --- paper trade (simulated option + stock trades with live P/L) ---
from paper_trade import PAPER_TRADE_JS
import paper_trade_routes as _pt_routes
# --- /paper trade ---

# --- heatmap ---
# heatmap/ is a top-level package; project root is already on sys.path via
# the .parent.parent insertion above (line ~28). Import the router + the two
# template r-strings the same way history_news does.
from heatmap.heatmap_api import router as heatmap_router, start_background_poller as _heatmap_start_poller, start_dxlink_stream as _heatmap_start_stream, warm_etf_trends as _heatmap_warm_etf
from heatmap.heatmap import HEATMAP_HTML, HEATMAP_JS
from heatmap import metadata as _heatmap_metadata
# --- /heatmap ---

# --- sec filings ---
from sec_filings.sec_api import router as sec_router
from sec_filings.sec_filings import SEC_PANE_HTML, SEC_JS
from benzinga_market_events.market_events_api import router as me_router
from benzinga_market_events.market_events import ME_PANE_HTML, ME_JS
from benzinga_signals.signals_api import router as signals_router, start_feed as _signals_start_feed
from benzinga_signals.signals_view import SIGNALS_PANE_HTML, SIGNALS_JS
# --- /sec filings ---

# --- pre-trade checkup (standalone page; independent of the checkup/ engine below) ---
from pretrade_checkup_page import PRETRADE_CHECKUP_HTML
# --- /pre-trade checkup ---

# --- journal review (standalone page; 5-min pre-market review, layered on top of
# the existing Trade Journal tabs — doesn't touch them) ---
from journal_review_page import JOURNAL_REVIEW_HTML
# --- /journal review ---

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# ── App state ──
app = FastAPI()

# CORS — only pages served from this machine may call the API. A wildcard here
# would let any website you visit read your positions from localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _zeus_host_guard(request, call_next):
    # DNS-rebinding guard: unless you deliberately listen on all interfaces,
    # only answer requests addressed to localhost.
    if str(_hc.get("host") or "127.0.0.1") in ("127.0.0.1", "localhost", "::1"):
        h = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if h and h not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return JSONResponse({"error": "host not allowed"}, status_code=403)
    return await call_next(request)

# --- 电源模式:每个 HTTP 请求戳一次 last_request_ts(lite 档 idle 自动关实时用) ---
import power_mode as _power_mode  # noqa: E402


@app.middleware("http")
async def _stamp_request_ts(request, call_next):
    state.last_request_ts = time.time()
    return await call_next(request)


@app.middleware("http")
async def _zeus_static_revalidate(request, call_next):
    # The page's own scripts and styles change with every update. Without a Cache-Control
    # header browsers keep them "fresh" for a while on their own, and a new page laid out
    # with an old zeus.css / zeus.js breaks. Revalidating is a cheap 304 on localhost.
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") and path.endswith((".js", ".css", ".html", ".json")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _live_on() -> bool:
    """DXLink 实时流本拍是否该连。读 /tmp/pwa_power_mode + state 的 override/idle。
    翻档(full↔lite↔off)时复位 override;idle 超时由 power_mode.live_on 懒复位。"""
    mode = _power_mode.read_mode()
    if mode != state._power_mode_seen:
        state.live_override = False      # 翻档 → 复位手动开关
        state._power_mode_seen = mode
    live, override_after = _power_mode.live_on(
        mode, state.live_override, state.last_request_ts, time.time())
    state.live_override = override_after
    return live


# 🚀 AI 选股(Graph 集成): 端点全在独立包 ai_picks/, 这里只挂载(降与其他改动的冲突面)。
try:
    from ai_picks.routes import register as _register_ai_picks
    _register_ai_picks(app)
except Exception as _e:
    print(f"[ai_picks] route register skipped: {_e}")

class AppState:
    def __init__(self):
        self.client: Optional[TastyClient] = None
        self.strategies: list = []
        self.equities: list = []
        self.quotes: dict = {}           # underlying -> price
        self.metrics_data: dict = {}     # underlying -> {iv, earnings_date, ...}
        self.dxlink_url: str = ""
        self.dxlink_token: str = ""
        self.live_quotes: dict = {}      # streamer_symbol -> {bid, ask}
        self.symbols_map: dict = {}      # streamer_symbol -> [(strat_idx, leg_idx)]
        self.ws_clients: list = []       # connected browser WebSocket clients
        self.last_refresh: float = 0
        self.ready = False
        self.equity_prev_close: dict = {}  # underlying -> prev close price
        self.equity_symbols: list = []     # equity streamer symbols for DXLink
        self.balance: dict = {}            # TastyTrade account balance snapshot
        self.backtest_running: bool = False
        self.backtest_progress: str = ""

        self.journal_refreshing: bool = False
        # 老 Screener 已删 (2026-07-30). 保留 screener_data 空字典让 backtest
        # 和 batch-earnings 两处 reader 自然走 ssp1_latest.json / TastyTrade 兜底.
        self.screener_data: dict = {}
        # --- strategy builder ---
        self.builder_symbols: dict[str, set] = {}    # ticker -> set of streamer symbols
        self.builder_resub_pending: bool = False     # signal DXLink loop to resub now
        # --- /strategy builder ---
        # --- 电源模式(DXLink 实时流门控;见 power_mode.py + pwa_power_guard.sh)---
        self.last_request_ts: float = 0.0   # 每个 HTTP 请求戳一次 → lite 档 idle 判定
        self.live_override: bool = False     # lite 档下作者在 PWA 手动「开实时」
        self._power_mode_seen: str = "full"  # 上次见到的档位;翻档时复位 override

state = AppState()

# --- history news ---
app.include_router(history_news_router)
# --- /history news ---

# --- heatmap ---
app.include_router(heatmap_router)
# --- /heatmap ---

# --- sec filings ---
app.include_router(sec_router)
app.include_router(me_router)
app.include_router(signals_router)
import zeus_routes as _hroutes  # noqa: E402
app.include_router(_hroutes.router)
# --- /sec filings ---

# --- MCP: AI agents on this computer read ZEUS at /mcp (zeus_mcp.py; bridge zeus_mcp_stdio.py) ---
import zeus_mcp as _zeus_mcp  # noqa: E402
_zeus_mcp.configure(app)
app.include_router(_zeus_mcp.router)
# --- /MCP ---

# --- paper trade: same quotes as the Positions tab (fetch_* are defined further down) ---
_pt_routes.configure(
    client=lambda: state.client,
    option_marks=lambda c, syms: fetch_option_marks(c, syms),
    equity_quotes=lambda c, tickers: fetch_equity_quotes(c, tickers),
    quotes=lambda: state.quotes,
    metrics=lambda: state.metrics_data,
)
app.include_router(_pt_routes.router)
# --- /paper trade ---

# --- TA Screener(技术分析 screener:MTF S/R 共振 + 概率 edge,ta_screener/)---
# 以包形式导入(ta_screener.api),内部相对导入 —— 避免裸 `import data/api`
# 与仓库根 data/ 目录或其它模块撞名(本仓曾因双 store.py 被 import 缓存坑过)。
try:
    _repo_root_ta = str(Path(__file__).resolve().parent.parent)
    if _repo_root_ta not in sys.path:
        sys.path.insert(0, _repo_root_ta)
    from ta_screener.api import router as _ta_router
    app.include_router(_ta_router)
except Exception as _ta_e:
    print(f"[TA Screener] router 加载失败(tab 将 404): {_ta_e}")
# --- /TA Screener ---

# short_sellers tab removed 2026-07-30 (lightweight refactor)

# --- Options 量稳定性 tab(options_vol/:合并清单→爬 barchart options-history→按 ≥90% 日达标分档)---
# options_vol 在 options_analyzer/ 下,普通顶层 import(同 strategy_builder_api/paper_trade_api)。
try:
    from options_vol.api import router as _optvol_router
    app.include_router(_optvol_router)
except Exception as _ov_e:
    print(f"[options_vol] router 加载失败(tab 将 404): {_ov_e}")
# --- /Options ---

PROJECT_ROOT = Path(__file__).parent.parent
TRADE_JOURNAL_DIR = PROJECT_ROOT / "trade_journal"
REPORTS_DIR = PROJECT_ROOT / "trading_assistant" / "reports"


# ── Data fetching (reuses existing logic) ──

def fetch_option_marks(client, option_symbols):
    """Fetch mark (theoretical), last-trade, and prev-close from TastyTrade REST.

    Returns dict: OCC symbol -> {"mark", "last", "prev_close"}.
    `mark` drives current unrealized P/L; `prev_close` drives today's P/L
    (current - prev_close = today's change).
    """
    if not option_symbols:
        return {}
    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        resp = client.get("/market-data/by-type", params={"equity-option": ",".join(option_symbols)})
        items = resp.get("data", {}).get("items", [])
        return {
            it.get("symbol", ""): {
                "mark": _f(it.get("mark")),
                "last": _f(it.get("last")),
                "prev_close": _f(it.get("prev-close")),
                # Per-leg IV from TastyTrade (the field is `volatility`, NOT
                # `implied-volatility`). Used by Positions tab to compute a
                # vega-weighted spread IV — replaces the underlying-level IV
                # index that misses skew.
                "iv": _f(it.get("volatility") or it.get("implied-volatility")),
            }
            for it in items
        }
    except Exception as e:
        print(f"  [WARN] Could not fetch option marks: {e}")
        return {}


def fetch_stock_marks(client, tickers):
    """Fetch current mark prices for a batch of equity tickers via TastyTrade
    REST. Returns {ticker: spot_price}."""
    if not tickers:
        return {}
    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    out = {}
    BATCH = 50
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i+BATCH]
        try:
            resp = client.get("/market-data/by-type",
                               params={"equity": ",".join(chunk)})
            for it in resp.get("data", {}).get("items", []):
                sym = it.get("symbol", "")
                # Prefer mark, fall back to last, then prev-close
                price = _f(it.get("mark")) or _f(it.get("last")) or _f(it.get("close")) or _f(it.get("prev-close"))
                if sym and price > 0:
                    out[sym] = price
        except Exception as e:
            print(f"  [WARN] fetch_stock_marks: {e}")
    return out


def fetch_equity_quotes(client, tickers):
    """Authoritative {sym: {"price", "prev_close"}} from TastyTrade REST
    (/market-data/by-type, equity). `prev-close` is the official prior-session
    settle (the SAME value the broker app + TradingView use), so the underlying's
    daily change% = (price - prev_close)/prev_close matches them exactly.

    This REPLACES the old self-computed prev_close (DXLink Trade.change at startup,
    frozen by a `not in` guard, + yfinance fast_info), which drifted multiple days
    stale → wrong (sometimes wrong-sign) day-change% while the spot price was fine.
    """
    if not tickers:
        return {}
    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    out = {}
    BATCH = 50
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i+BATCH]
        try:
            resp = client.get("/market-data/by-type",
                               params={"equity": ",".join(chunk)})
            for it in resp.get("data", {}).get("items", []):
                sym = it.get("symbol", "")
                if not sym:
                    continue
                price = _f(it.get("mark")) or _f(it.get("last")) or _f(it.get("close"))
                pc = _f(it.get("prev-close"))
                rec = {}
                if price > 0:
                    rec["price"] = round(price, 2)
                if pc > 0:
                    rec["prev_close"] = round(pc, 2)
                if rec:
                    out[sym] = rec
        except Exception as e:
            print(f"  [WARN] fetch_equity_quotes: {e}")
    return out


def _current_from_mark(mark: float, last: float) -> Optional[float]:
    """Use TastyTrade's `mark` (theoretical fair value). `last` can be stale when
    an option hasn't traded recently, so prefer mark. Fall back to last only if
    mark is missing. This matches TastyTrade app's displayed unrealized P/L
    within ~$10 across tested positions."""
    if mark > 0:
        return round(mark, 2)
    if last > 0:
        return round(last, 2)
    return None


def _compute_live_nlv():
    """实时 NLV = 现金 + Σ(期权腿 signed_qty × current × mult) + Σ(股票 signed_qty × current)。

    leg['current'] 由 30s REST mark + DXLink mid 维护(matches TastyTrade app 的未实现盈亏),
    取代 /balances 的 net-liquidating-value 字段——后者后台「整块慢更新」,急涨/急跌时可滞后上千刀
    与持仓卡 P/L 同源。数据不全 / 异常偏离时回退已入账的 booked 值。返回 float 或 None。"""
    bal = getattr(state, "balance", None) or {}
    booked = bal.get("net_liquidating_value")
    try:
        booked_f = float(booked) if booked is not None else None
    except (TypeError, ValueError):
        booked_f = None
    cash = bal.get("cash_balance")
    strategies = getattr(state, "strategies", None) or []
    if cash is None or not strategies:
        return booked_f
    try:
        opt = 0.0
        for s in strategies:
            for l in s.get("legs", []):
                px = l.get("current")
                if px is None:
                    px = l.get("mark")
                if px is None:
                    px = l.get("mid")
                if px is None:
                    return booked_f          # 任一腿缺价 → 回退 booked,别给半截数
                opt += float(l.get("qty") or 0) * float(px) * float(l.get("mult") or 100)
        eq = 0.0
        for e in (getattr(state, "equities", None) or []):
            q = e.get("quantity") or 0
            if e.get("direction") == "Short":
                q = -q
            eq += q * float(e.get("current") or 0)
        live = float(cash) + opt + eq
        if live <= 0:
            return booked_f
        if booked_f and booked_f > 0 and abs(live - booked_f) / booked_f > 0.5:
            return booked_f                  # 异常偏离(漏腿/符号/乘数 bug)→ 防灾回退
        return live
    except Exception:
        return booked_f


def _txn_window_start(positions):
    """Earliest date we must pull transactions from to reconstruct each current
    leg's opening order. TT's sticky created-at is the FIRST-ever open (<= the
    current lot's real open), so starting a few days before min(created-at)
    guarantees the window covers the actual opens/closes. Floored at 365d so a
    very old leg can't force an unbounded fetch (build_opening_order_map's
    lots[-1] is robust to a truncated front)."""
    from datetime import date, timedelta
    days = [(p.get("created-at") or "")[:10] for p in positions]
    days = [d for d in days if d]
    if not days:
        return None
    try:
        start = date.fromisoformat(min(days)) - timedelta(days=3)
    except ValueError:
        return None
    floor = date.today() - timedelta(days=365)
    return max(start, floor).isoformat()


def fetch_recent_transactions(client, acct_num, start_date):
    """Paginate /accounts/{acct}/transactions from start_date (inclusive).

    Windowed (not the journal's full-history pull) — we only need enough to FIFO
    the currently-open lots back to their opening order. per-page/page-offset
    semantics match trade_journal.fetch_trades (page-offset is a page number)."""
    results = []
    page = 0
    while True:
        params = {"per-page": 250, "page-offset": page, "sort": "Asc"}
        if start_date:
            params["start-date"] = start_date
        resp = client.get(f"/accounts/{acct_num}/transactions", params=params)
        items = resp.get("data", {}).get("items", [])
        total = int(resp.get("pagination", {}).get("total-items", 0))
        results.extend(items)
        if not items or len(results) >= total:
            break
        page += 1
        if page > 200:  # safety backstop against a runaway paginate
            break
    return results


def fetch_positions_and_strategies():
    """Fetch all data from TastyTrade and build strategies."""
    print("Connecting to TastyTrade...")
    client = TastyClient()
    state.client = client

    try:
        _, acct_num = client.pick_active_account()
    except RuntimeError:
        print("No TastyTrade accounts found.")
        return
    print(f"Account: {acct_num[:4]}****")

    # Fetch balances (NLV drives Kelly sizing recommendations in Risk view)
    try:
        bal_resp = client.get(f"/accounts/{acct_num}/balances")
        bal = bal_resp.get("data", {})
        def _f(k):
            try: return float(bal.get(k, 0) or 0)
            except (TypeError, ValueError): return 0.0
        state.balance = {
            "net_liquidating_value": _f("net-liquidating-value"),
            "cash_balance": _f("cash-balance"),
            "derivative_buying_power": _f("derivative-buying-power"),
            "equity_buying_power": _f("equity-buying-power"),
            "maintenance_requirement": _f("maintenance-requirement"),
        }
        print(f"  NLV: ${state.balance['net_liquidating_value']:,.0f}")
    except Exception as e:
        print(f"  [WARN] Could not fetch balance: {e}")

    raw_positions = client.get_positions(acct_num)
    option_positions = [p for p in raw_positions if p.get("instrument-type") == "Equity Option"]
    equity_positions = [p for p in raw_positions if p.get("instrument-type") == "Equity"]

    if not option_positions and not equity_positions:
        print("No positions found." + ("  (manual mode: add trades in Settings)" if _hc.broker_mode() == "manual" else ""))
        state.strategies = []
        state.equities = []
        state.symbols_map = {}
        state.last_refresh = time.time()
        state.ready = True
        return

    underlyings = sorted(set(p.get("underlying-symbol", "") for p in option_positions if p.get("underlying-symbol")))
    print(f"Found {len(option_positions)} option legs across {len(underlyings)} underlyings")

    # Initial equity prices — reuse cached prices from prior fetch so the 60s
    # periodic_refresh doesn't blow real prices back to 100 (the yfinance loop
    # only ticks every 5 min, and DXLink may be silent pre-market or for thin
    # tickers, so without this safety net Positions BE/COP stay empty).
    prev_quotes = getattr(state, "quotes", None) or {}
    quotes = {}
    for und in underlyings:
        cached = prev_quotes.get(und, 0) or 0
        quotes[und] = cached if cached > 0 else 100
    if getattr(client, "local", False):
        # manual mode has no streaming: price the underlyings right away (Yahoo Finance)
        try:
            quotes.update({k: v for k, v in fetch_stock_marks(client, underlyings).items() if v > 0})
        except Exception as e:
            print(f"  [WARN] manual-mode quotes: {e}")
    fresh_count = sum(1 for v in quotes.values() if v != 100)
    print(f"  {len(underlyings)} underlyings queued ({fresh_count} reused cached prices)")

    # Market metrics
    metrics_data = {}
    try:
        metrics_resp = client.get("/market-metrics", params={"symbols": ",".join(underlyings)})
        items = metrics_resp.get("data", {}).get("items", [])
        for item in items:
            symbol = item.get("symbol", "")
            iv = _safe_float(item, "implied-volatility-index", "implied-volatility-index-5-day-change")
            if iv > 5:
                iv = iv / 100.0
            earnings = item.get("earnings", {}) or {}
            metrics_data[symbol] = {
                "iv": iv,
                "earnings_date": earnings.get("expected-report-date", "") if earnings else "",
                "earnings_time": earnings.get("time-of-day", "") if earnings else "",
            }
    except Exception as e:
        print(f"  [WARN] Could not get market metrics: {e}")

    # IV from yfinance if missing
    if HAS_YF:
        import math
        import numpy as np
        for und in underlyings:
            if und in metrics_data and metrics_data[und].get("iv", 0) > 0:
                continue
            try:
                hist = yf.download(und, period="60d", progress=False)
                if not hist.empty:
                    returns = hist["Close"].pct_change().dropna()
                    iv_est = float(returns.std() * math.sqrt(252))
                    metrics_data[und] = metrics_data.get(und, {})
                    metrics_data[und]["iv"] = iv_est
                    metrics_data[und].setdefault("earnings_date", "")
                    metrics_data[und].setdefault("earnings_time", "")
            except Exception:
                pass

    # Reconstruct each leg's opening order from trade history. TT's position
    # created-at is sticky (first-ever open, not the current lot), so grouping
    # by created-at splits spreads that were closed+reopened or legged in on
    # different days. Opening order-id is the reliable strategy key. Any failure
    # here degrades gracefully to date-based grouping (opening_orders stays {}).
    opening_orders = {}
    try:
        start_date = _txn_window_start(option_positions)
        txns = fetch_recent_transactions(client, acct_num, start_date) if option_positions else []
        opening_orders = build_opening_order_map(txns, option_positions)
        print(f"  Opening-order map: {len(opening_orders)}/{len(option_positions)} legs "
              f"resolved (txn window from {start_date})")
    except Exception as e:
        print(f"  [WARN] opening-order grouping unavailable, using open-date fallback: {e}")

    # Group into strategies
    strategies = group_into_strategies(option_positions, quotes, metrics_data,
                                       opening_orders=opening_orders)
    print(f"Identified {len(strategies)} option strategies")

    # Fetch authoritative marks from TastyTrade REST (matches app's unrealized P/L).
    # `leg.current` drives the P/L calc — we set it from min(mark, last) here so the
    # display doesn't depend on DXLink bid/ask (which overvalues legs when bid=0).
    option_symbols_list = sorted({l["symbol"] for s in strategies for l in s["legs"]})
    marks = fetch_option_marks(client, option_symbols_list)
    applied_marks = 0
    for strat in strategies:
        for leg in strat["legs"]:
            m = marks.get(leg["symbol"])
            if not m:
                continue
            leg["mark"] = m["mark"]
            leg["last"] = m["last"]
            # NOTE: `prev_close` is set from position's `close-price` (yesterday's
            # settle) in group_into_strategies. The `prev-close` from
            # /market-data/by-type is unreliable for illiquid options so we don't
            # overwrite here.
            cv = _current_from_mark(m["mark"], m["last"])
            if cv is not None:
                leg["current"] = cv
                applied_marks += 1
            # Per-leg IV from TastyTrade (skew-aware). Falls back to the
            # underlying-level IV from market-metrics when TT didn't supply it.
            iv_leg = m.get("iv", 0) or 0
            if iv_leg > 0:
                leg["iv"] = round(iv_leg, 4)
    print(f"  Applied marks to {applied_marks}/{sum(len(s['legs']) for s in strategies)} legs")

    # Recompute strat["iv"] as a vega-weighted blend of per-leg IVs — same
    # convention as Strategy Builder. This replaces the underlying ATM IV
    # (or yfinance HV fallback) which had no skew. A spread on AMD with
    # strikes far from spot now reflects the actual leg IVs (e.g. ~62%
    # instead of the 30% ATM proxy).
    try:
        from strategy_builder_api import _bsm_vega
        import datetime as _dt_iv
        now_dt = _dt_iv.datetime.now()
        for strat in strategies:
            S = float(strat.get("current_price") or 0)
            r = float(strat.get("risk_free_rate") or 0.045)
            try:
                exp_dt = _dt_iv.datetime.strptime(strat["expiration"], "%Y-%m-%d").replace(hour=16)
                T = max(1e-6, (exp_dt - now_dt).total_seconds() / (365.0 * 86400.0))
            except Exception:
                continue
            num, den = 0.0, 0.0
            for leg in strat["legs"]:
                iv_leg = float(leg.get("iv") or 0)
                if iv_leg <= 0 or S <= 0:
                    continue
                K = float(leg.get("strike") or 0)
                if K <= 0:
                    continue
                v = _bsm_vega(S, K, T, r, iv_leg)
                w = abs(v) * abs(int(leg.get("qty") or 0))
                num += w * iv_leg
                den += w
            if den > 0:
                strat["iv"] = round(num / den, 4)
    except Exception as e:
        print(f"  [WARN] spread-IV blend failed: {e}")

    # NOTE: enrich_with_mid_prices uses asyncio.run() which conflicts with uvicorn's loop.
    # Initial mid prices will come from the persistent DXLink stream within a few seconds.

    # Equity data
    equity_data = []
    for ep in equity_positions:
        equity_data.append({
            "symbol": ep.get("symbol", ""),
            "quantity": int(ep.get("quantity", 0)),
            "direction": ep.get("quantity-direction", "Long"),
            "entry": float(ep.get("average-open-price", 0) or 0),
            "current": float(ep.get("close-price", 0) or 0),
        })

    # Build symbols map for DXLink streaming using TastyTrade's streamer-symbol
    symbols_map = {}
    for si, strat in enumerate(strategies):
        for li, leg in enumerate(strat["legs"]):
            streamer = leg.get("streamer_symbol", "")
            if not streamer:
                # Fallback: construct from OCC symbol
                sym = leg["symbol"].strip()
                parts = sym.split()
                if len(parts) >= 2:
                    rest = parts[-1]
                    for i, ch in enumerate(rest):
                        if ch in ("C", "P"):
                            strike_val = float(rest[i + 1:]) / 1000
                            streamer = f".{parts[0].strip()}{rest[:i]}{ch}{strike_val:g}"
                            break
            if streamer:
                if streamer not in symbols_map:
                    symbols_map[streamer] = []
                symbols_map[streamer].append((si, li))

    # Get DXLink token for persistent streaming
    try:
        token_resp = client.get("/api-quote-tokens")
        token_data = token_resp.get("data", {})
        state.dxlink_url = token_data.get("dxlink-url", "")
        state.dxlink_token = token_data.get("token", "")
    except Exception as e:
        print(f"  [WARN] Could not get DXLink token: {e}")

    state.strategies = strategies
    state.equities = equity_data
    state.quotes = quotes
    state.metrics_data = metrics_data
    state.symbols_map = symbols_map
    # DXLink equity streaming — include market pulse symbols + pure-stock holdings
    # (so their live price gets pulled and flows into _compute_live_nlv).
    _mp_syms = {"SPY", "QQQ", "IWM"}
    _eq_syms = {e.get("symbol", "") for e in equity_data if e.get("symbol")}
    state.equity_symbols = list(set(underlyings) | _mp_syms | _eq_syms)  # DXLink equity streaming
    # Seed AUTHORITATIVE prev-close from TastyTrade so the FIRST page load shows
    # correct underlying day-change% (equity_price_loop then refreshes every 5 min).
    try:
        _eq = fetch_equity_quotes(client, sorted(set(underlyings) | _mp_syms))
        for _sym, _rec in _eq.items():
            if _rec.get("prev_close"):
                state.equity_prev_close[_sym] = _rec["prev_close"]
        print(f"  Seeded TastyTrade prev-close for {len(state.equity_prev_close)} equities")
    except Exception as e:
        print(f"  [WARN] prev-close seed failed: {e}")
    state.last_refresh = time.time()
    state.ready = True
    print(f"Server ready with {len(strategies)} strategies, {len(symbols_map)} option symbols, {len(underlyings)} equities")


# ── DXLink persistent WebSocket ──

async def dxlink_stream():
    """Maintain a persistent DXLink WebSocket and push updates to browser clients."""
    import websockets as ws_lib

    while True:
        if not state.dxlink_url or not state.dxlink_token:
            await asyncio.sleep(5)
            continue

        # 电源门控:lite/off 档且未手动开实时 → 不连 websocket,空转省电。
        # 靠 power guard 写的 /tmp/pwa_power_mode + 手动 override(见 _live_on)。
        if not _live_on():
            await asyncio.sleep(3)
            continue

        print("[DXLink] Connecting...")
        try:
            async with ws_lib.connect(state.dxlink_url, close_timeout=5) as ws:
                # SETUP
                await ws.send(json.dumps({
                    "type": "SETUP",
                    "channel": 0,
                    "version": "0.1-js/1.0.0",
                    "keepaliveTimeout": 60,
                    "acceptKeepaliveTimeout": 60,
                }))
                await ws.recv()

                # AUTH — DXLink returns AUTH_STATE in TWO frames: first
                # UNAUTHORIZED (transient/pending) then AUTHORIZED. A single
                # ws.recv() consumed the wrong one and CHANNEL_REQUEST below
                # was sent before auth completed, so DXLink silently refused
                # to open channel 1 → every subsequent FEED_SETUP /
                # FEED_SUBSCRIPTION came back with
                #   ERROR 'Channel with id 1 not exists'
                # and the data feed went totally dark.
                await ws.send(json.dumps({
                    "type": "AUTH",
                    "channel": 0,
                    "token": state.dxlink_token,
                }))
                while True:
                    _m = json.loads(await ws.recv())
                    if (_m.get("type") == "AUTH_STATE"
                            and _m.get("state") == "AUTHORIZED"):
                        break
                    if (_m.get("type") == "AUTH_STATE"
                            and _m.get("state") == "UNAUTHORIZED"):
                        continue  # transient — keep waiting
                    if _m.get("type") == "ERROR":
                        raise RuntimeError(f"DXLink AUTH error: {_m.get('message')}")

                # CHANNEL_REQUEST — also wait for the CHANNEL_OPENED ack
                # before sending FEED_SETUP, otherwise the same race trips.
                await ws.send(json.dumps({
                    "type": "CHANNEL_REQUEST",
                    "channel": 1,
                    "service": "FEED",
                    "parameters": {"contract": "AUTO"},
                }))
                while True:
                    _m = json.loads(await ws.recv())
                    if (_m.get("type") == "CHANNEL_OPENED"
                            and _m.get("channel") == 1):
                        break
                    if _m.get("type") == "ERROR":
                        raise RuntimeError(
                            f"DXLink CHANNEL_REQUEST error: {_m.get('message')}")

                # FEED_SETUP — Quote (bid/ask), Trade (last/volume), Greeks
                # (IV + delta/gamma/theta/vega/rho streamed in real-time, no
                # REST polling needed for IV).
                await ws.send(json.dumps({
                    "type": "FEED_SETUP",
                    "channel": 1,
                    "acceptAggregationPeriod": 0,
                    "acceptDataFormat": "COMPACT",
                    "acceptEventFields": {
                        "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
                        "Trade": ["eventSymbol", "price", "dayVolume", "change"],
                        "Greeks": ["eventSymbol", "volatility", "delta", "gamma", "theta", "vega", "rho"],
                    },
                }))

                # Subscribe — Quote+Trade for everything, Greeks only for options
                # (equity symbols have no Greeks).
                option_syms = list(state.symbols_map.keys())
                symbols = option_syms + state.equity_symbols
                subs = [{"type": "Quote", "symbol": s} for s in symbols]
                subs += [{"type": "Trade", "symbol": s} for s in symbols]
                subs += [{"type": "Greeks", "symbol": s} for s in option_syms]
                await ws.send(json.dumps({
                    "type": "FEED_SUBSCRIPTION",
                    "channel": 1,
                    "add": subs,
                }))

                subscribed_set = set(symbols)
                print(f"[DXLink] Connected, subscribed to {len(symbols)} symbols")

                # Keep-alive + data loop with BATCHED updates
                last_keepalive = time.time()
                last_push = time.time()
                last_resub = time.time()
                pending_updates = {}  # sym -> {bid, ask, mid, is_equity}
                PUSH_INTERVAL = 1.0   # batch and push every 1 second

                last_gate = time.time()
                while True:
                    # 电源门控:≤1/s 复查,翻到关就断开 ws 省电(节流避免高频读文件)
                    if time.time() - last_gate >= 1.0:
                        last_gate = time.time()
                        if not _live_on():
                            print("[DXLink] Power gate closed — disconnecting")
                            break
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1)
                        data = json.loads(msg)

                        if data.get("type") == "FEED_DATA" and data.get("channel") == 1:
                            feed = data.get("data", [])

                            # Trade events
                            if (isinstance(feed, list) and len(feed) >= 2
                                    and feed[0] == "Trade" and isinstance(feed[1], list)):
                                arr = feed[1]
                                for i in range(0, len(arr) - 3, 4):
                                    sym = arr[i]
                                    if isinstance(sym, str):
                                        try:
                                            tp = float(arr[i + 1]) if arr[i + 1] not in ("NaN", None) else 0
                                            if tp > 0:
                                                state.live_quotes.setdefault(sym, {})["last_trade"] = tp
                                                # prev_close is NO LONGER derived from Trade.change
                                                # here (it drifted stale → wrong day-change%); it now
                                                # comes from TastyTrade's authoritative prev-close via
                                                # equity_price_loop / fetch_equity_quotes.
                                                # For equities, push trade prints so the displayed
                                                # price ticks with every trade — Quote events alone
                                                # miss prints when bid/ask haven't moved.
                                                if sym in state.equity_symbols:
                                                    old = state.live_quotes[sym]
                                                    b = old.get("bid", 0) or 0
                                                    a = old.get("ask", 0) or 0
                                                    px = round(tp, 2)
                                                    state.quotes[sym] = px
                                                    for strat in state.strategies:
                                                        if strat["underlying"] == sym:
                                                            strat["current_price"] = px
                                                    pending_updates[sym] = {
                                                        "symbol": sym, "bid": b, "ask": a,
                                                        "mid": px, "is_equity": True,
                                                    }
                                        except (ValueError, TypeError):
                                            pass

                            # Quote events — accumulate into pending_updates
                            if (isinstance(feed, list) and len(feed) >= 2
                                    and feed[0] == "Quote" and isinstance(feed[1], list)):
                                arr = feed[1]
                                for i in range(0, len(arr) - 4, 5):
                                    sym = arr[i]
                                    bid_raw = arr[i + 1]
                                    ask_raw = arr[i + 2]
                                    if not isinstance(sym, str):
                                        continue
                                    try:
                                        b = float(bid_raw) if bid_raw not in ("NaN", None) else 0
                                        a = float(ask_raw) if ask_raw not in ("NaN", None) else 0
                                    except (ValueError, TypeError):
                                        continue
                                    if b <= 0 and a <= 0:
                                        continue

                                    old = state.live_quotes.get(sym, {})
                                    if old.get("bid") == b and old.get("ask") == a:
                                        continue

                                    # Display mid: use (b+a)/2 when both sides quoted.
                                    # If bid=0, don't fall back to ask alone (overvalues long legs) —
                                    # use last_trade if it's within the quote, else (0+ask)/2.
                                    lt = state.live_quotes.get(sym, {}).get("last_trade", 0)
                                    if b > 0 and a > 0:
                                        mid = round((b + a) / 2, 2)
                                    elif a > 0 and lt > 0 and lt <= a:
                                        mid = round(lt, 2)
                                    else:
                                        mid = round((b + a) / 2, 2)
                                    state.live_quotes.setdefault(sym, {}).update({"bid": b, "ask": a})

                                    # Update bid/ask/mid for display. `current` is NOT touched —
                                    # it's driven by REST mark from fetch_option_marks (TastyTrade
                                    # app-style min(mark, last)), refreshed by periodic_mark_refresh.
                                    if sym in state.symbols_map:
                                        for si, li in state.symbols_map[sym]:
                                            state.strategies[si]["legs"][li]["bid"] = b
                                            state.strategies[si]["legs"][li]["ask"] = a
                                            state.strategies[si]["legs"][li]["mid"] = mid
                                    if sym in state.equity_symbols:
                                        state.quotes[sym] = mid
                                        for strat in state.strategies:
                                            if strat["underlying"] == sym:
                                                strat["current_price"] = mid

                                    pending_updates[sym] = {
                                        "symbol": sym, "bid": b, "ask": a, "mid": mid,
                                        "is_equity": sym in state.equity_symbols,
                                    }

                            # Greeks events — IV + delta/gamma/theta/vega/rho
                            # streamed per option symbol. Updates leg.iv (and
                            # cached greeks) on the live strategy state. The
                            # vega-weighted spread IV is recomputed in
                            # periodic_refresh's tick.
                            if (isinstance(feed, list) and len(feed) >= 2
                                    and feed[0] == "Greeks" and isinstance(feed[1], list)):
                                arr = feed[1]
                                # Field order: eventSymbol, volatility, delta, gamma, theta, vega, rho (7 fields)
                                for i in range(0, len(arr) - 6, 7):
                                    sym = arr[i]
                                    if not isinstance(sym, str):
                                        continue
                                    try:
                                        def _g(idx):
                                            v = arr[i + idx]
                                            return float(v) if v not in ("NaN", None) else 0.0
                                        vol = _g(1); d = _g(2); gm = _g(3)
                                        th = _g(4); ve = _g(5); rh = _g(6)
                                    except (ValueError, TypeError):
                                        continue
                                    cached = state.live_quotes.setdefault(sym, {})
                                    cached.update({
                                        "iv": vol, "delta": d, "gamma": gm,
                                        "theta": th, "vega": ve, "rho": rh,
                                    })
                                    if sym in state.symbols_map:
                                        for si, li in state.symbols_map[sym]:
                                            leg = state.strategies[si]["legs"][li]
                                            if vol > 0:
                                                leg["iv"] = round(vol, 4)
                                            leg["delta"] = d
                                            leg["gamma"] = gm
                                            leg["theta"] = th
                                            leg["vega"]  = ve
                                            leg["rho"]   = rh

                    except asyncio.TimeoutError:
                        pass

                    # Flush batched updates to browser every PUSH_INTERVAL seconds
                    now = time.time()
                    if pending_updates and state.ws_clients and now - last_push >= PUSH_INTERVAL:
                        msg_out = json.dumps({
                            "type": "quote_update",
                            "time": datetime.datetime.now().isoformat(),
                            "updates": list(pending_updates.values()),
                        })
                        dead = []
                        for client_ws in state.ws_clients:
                            try:
                                await client_ws.send_text(msg_out)
                            except Exception:
                                dead.append(client_ws)
                        for d in dead:
                            state.ws_clients.remove(d)
                        pending_updates.clear()
                        last_push = now

                    # Re-subscribe to new symbols every 10s, OR immediately if
                    # the builder signaled a strike change (sub-second responsiveness).
                    # --- strategy builder ---
                    builder_wake = state.builder_resub_pending
                    if builder_wake:
                        state.builder_resub_pending = False
                    # --- /strategy builder ---
                    if builder_wake or time.time() - last_resub > 10:
                        # --- strategy builder ---
                        builder_syms = set().union(*state.builder_symbols.values()) if state.builder_symbols else set()
                        # --- /strategy builder ---
                        current_syms = set(state.symbols_map.keys()) | set(state.equity_symbols) | builder_syms
                        new_syms = current_syms - subscribed_set
                        if new_syms:
                            new_subs = [{"type": "Quote", "symbol": s} for s in new_syms]
                            new_subs += [{"type": "Trade", "symbol": s} for s in new_syms]
                            # Greeks only for options (equities have none).
                            equity_set = set(state.equity_symbols)
                            new_subs += [{"type": "Greeks", "symbol": s}
                                         for s in new_syms if s not in equity_set]
                            try:
                                await ws.send(json.dumps({
                                    "type": "FEED_SUBSCRIPTION",
                                    "channel": 1,
                                    "add": new_subs,
                                }))
                                subscribed_set.update(new_syms)
                                print(f"[DXLink] Subscribed to {len(new_syms)} new symbols")
                            except Exception:
                                pass
                        last_resub = time.time()

                    # Keepalive every 30s
                    if time.time() - last_keepalive > 30:
                        try:
                            await ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                            last_keepalive = time.time()
                        except Exception:
                            break

        except Exception as e:
            print(f"[DXLink] Disconnected: {e}")

        print("[DXLink] Reconnecting in 5s...")
        await asyncio.sleep(5)

        # Refresh token on reconnect
        try:
            token_resp = state.client.get("/api-quote-tokens")
            token_data = token_resp.get("data", {})
            state.dxlink_url = token_data.get("dxlink-url", "")
            state.dxlink_token = token_data.get("token", "")
        except Exception:
            pass


def reapply_cached_quotes():
    """After rebuilding strategies, re-apply cached DXLink quotes (bid/ask/mid only).
    `current` is left alone — it's managed by REST mark fetch, not DXLink.
    """
    applied = 0
    for sym, quote in state.live_quotes.items():
        b = quote.get("bid", 0)
        a = quote.get("ask", 0)
        if b <= 0 and a <= 0:
            continue
        lt = quote.get("last_trade", 0)
        if b > 0 and a > 0:
            mid = round((b + a) / 2, 2)
        elif a > 0 and lt > 0 and lt <= a:
            mid = round(lt, 2)
        else:
            mid = round((b + a) / 2, 2)
        if sym in state.symbols_map:
            for si, li in state.symbols_map[sym]:
                if si < len(state.strategies) and li < len(state.strategies[si]["legs"]):
                    state.strategies[si]["legs"][li]["bid"] = b
                    state.strategies[si]["legs"][li]["ask"] = a
                    state.strategies[si]["legs"][li]["mid"] = mid
                    applied += 1
        if sym in state.equity_symbols:
            state.quotes[sym] = mid
            for strat in state.strategies:
                if strat["underlying"] == sym:
                    strat["current_price"] = mid
            for e in (getattr(state, "equities", None) or []):
                if e.get("symbol") == sym:
                    e["current"] = mid   # keep pure-stock NLV live (not stale close)
    print(f"[Refresh] Re-applied {applied} cached DXLink quotes")


def is_market_hours(now_et=None) -> bool:
    """True during the US regular session (9:30–16:00 ET), Mon–Fri.

    Computed in the MARKET timezone (America/New_York) so it's correct no matter
    what the machine's local timezone is. The old version used naive local time
    with an 8:30–15:00 window that only matched the market when the box was on
    Chicago time; on an Eastern-time machine it reported "closed" during the final
    trading hour (3–4 PM ET), which froze option marks / P/L for that hour.
    `now_et` is for tests."""
    from zoneinfo import ZoneInfo
    if now_et is None:
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return datetime.time(9, 30) <= t <= datetime.time(16, 0)


async def periodic_refresh():
    """Two-tier refresh:
    - Every 1s: re-blend vega-weighted strat IV from streamed leg IVs +
      ping browser to refetch /api/data. No REST calls — IV/Greeks come
      from DXLink Greeks events streamed in real-time.
    - Every 60s: full rebuild (positions, market-metrics) — catches new
      positions, closures, and earnings/IV-rank refreshes.
    Both gated to market hours.
    """
    full_counter = 0
    while True:
        await asyncio.sleep(1)
        if not is_market_hours():
            continue
        full_counter += 1
        try:
            if full_counter >= 60:
                full_counter = 0
                old_quotes = dict(state.live_quotes)
                fetch_positions_and_strategies()
                state.live_quotes = old_quotes
                reapply_cached_quotes()
            # Re-blend strat IV from per-leg IVs streamed via DXLink.
            try:
                from strategy_builder_api import _bsm_vega
                import datetime as _dt_iv
                now_dt = _dt_iv.datetime.now()
                for strat in state.strategies:
                    S = float(strat.get("current_price") or 0)
                    r = float(strat.get("risk_free_rate") or 0.045)
                    try:
                        exp_dt = _dt_iv.datetime.strptime(strat["expiration"], "%Y-%m-%d").replace(hour=16)
                        T = max(1e-6, (exp_dt - now_dt).total_seconds() / (365.0 * 86400.0))
                    except Exception:
                        continue
                    num, den = 0.0, 0.0
                    for leg in strat["legs"]:
                        iv_leg = float(leg.get("iv") or 0)
                        if iv_leg <= 0 or S <= 0:
                            continue
                        K = float(leg.get("strike") or 0)
                        if K <= 0:
                            continue
                        v = _bsm_vega(S, K, T, r, iv_leg)
                        w = abs(v) * abs(int(leg.get("qty") or 0))
                        num += w * iv_leg
                        den += w
                    if den > 0:
                        strat["iv"] = round(num / den, 4)
            except Exception:
                pass
            # Notify browsers — they'll refetch /api/data which has freshly
            # blended IV and per-leg Greeks.
            if state.ws_clients:
                msg = json.dumps({"type": "full_refresh"})
                dead = []
                for ws in state.ws_clients:
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.append(ws)
                for d in dead:
                    state.ws_clients.remove(d)
        except Exception as e:
            print(f"[Refresh] Error: {e}")


async def balance_refresh_loop():
    """轻量 NLV/余额刷新:每 12s(交易时段)只打 /balances,与 60s 全量重建解耦
    —— 全量重建偶发抛异常会连带吞掉浏览器刷新 ping,导致 NLV 冻住(只有 DXLink
    报价在动)。这条独立、阻塞调用丢线程池不堵事件循环,NLV 变了就 ping 浏览器重取。
    符合 「余额必须每次实时获取、禁止缓存」。"""
    import asyncio as _a
    await asyncio.sleep(6)
    last_nlv = None
    while True:
        await asyncio.sleep(12)
        if not is_market_hours():
            continue
        try:
            client = state.client
            if client is None:
                continue

            def _pull():
                _, acct = client.pick_active_account()
                bal = (client.get(f"/accounts/{acct}/balances") or {}).get("data", {})

                def _f(k):
                    try:
                        return float(bal.get(k, 0) or 0)
                    except (TypeError, ValueError):
                        return 0.0
                return {
                    "net_liquidating_value": _f("net-liquidating-value"),
                    "cash_balance": _f("cash-balance"),
                    "derivative_buying_power": _f("derivative-buying-power"),
                    "equity_buying_power": _f("equity-buying-power"),
                    "maintenance_requirement": _f("maintenance-requirement"),
                }
            newbal = await _a.get_running_loop().run_in_executor(None, _pull)
            nlv = newbal.get("net_liquidating_value", 0)
            if not nlv or nlv <= 0:
                continue
            state.balance = newbal
            if last_nlv is None or abs(nlv - last_nlv) >= 0.5:
                last_nlv = nlv
                if state.ws_clients:
                    msg = json.dumps({"type": "full_refresh"})
                    for ws in list(state.ws_clients):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            try:
                                state.ws_clients.remove(ws)
                            except Exception:
                                pass
        except Exception as e:
            print(f"[BalanceRefresh] {e}")


async def hourly_journal_refresh():
    """Auto-refresh trades_raw.json every hour so today's trades appear in the
    Trade Journal without the user clicking Refresh."""
    await asyncio.sleep(30)  # let server settle
    while True:
        try:
            python = sys.executable
            fetch_script = str(TRADE_JOURNAL_DIR / "fetch_trades.py")
            proc = await asyncio.create_subprocess_exec(
                python, fetch_script,
                cwd=str(TRADE_JOURNAL_DIR),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=120)
                print(f"[Journal] Hourly refresh complete at {datetime.datetime.now().strftime('%H:%M:%S')}")
                # Sync decision-log entries (open + close) for any new trades
                try:
                    je = _journal_entries_module()
                    r = je.sync_entries_from_trades_raw()
                    if r.get("open_added") or r.get("close_added"):
                        print(f"[Journal] Decision-log entries: +{r.get('open_added',0)} open, "
                              f"+{r.get('close_added',0)} close")
                except Exception as je_e:
                    print(f"[Journal] entry sync warn: {je_e}")
            except asyncio.TimeoutError:
                proc.kill()
                print("[Journal] Hourly refresh timed out")
        except Exception as e:
            print(f"[Journal] Hourly refresh error: {e}")
        await asyncio.sleep(3600)  # next run in 1 hour


def _benzinga_session_status() -> dict:
    """benzinga 登录 cookie 的到期状况。**只读到期时间, 不碰 cookie 值。**
    复用 benzinga/fetcher.py 的单一实现(playwright 是 lazy import, 导入很轻)。"""
    import sys as _sys
    from pathlib import Path as _Path
    bz = str(_Path(__file__).resolve().parent.parent / "benzinga")
    if bz not in _sys.path:
        _sys.path.insert(0, bz)
    try:
        import fetcher as _bzf
        return _bzf.cookie_expiry_status()
    except Exception as e:
        return {"expires_at": None, "days_left": None, "expired": False,
                "found": False, "error": str(e)}


def _benzinga_relogin_cmd() -> str:
    """在 Terminal 里跑 puppeteer stealth 登录的 shell 命令。"""
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent.parent
    cookie_dst = _hc.benzinga_cookies_path()
    home = _hc.home()
    return (f'cd {shlex.quote(str(root))} && ZEUS_HOME={shlex.quote(str(home))} '
            f'node premarket/login.mjs {shlex.quote(str(cookie_dst))}')


_BZ_RELOGIN_NOTIFIED = {"day": ""}
# 连续几次 fetcher exit 2 的计数 —— 用来兜住"服务端 revoke 但 cookie 文件里
# expires 还没到"这类盲点(cookie_expiry_status 只读文件, 服务端主动踢会漏)。
# 达阈值就当作登录态失效弹通知, 同一天防抖已在 _notify_benzinga_relogin 里。
_BZ_EXIT2_STREAK = {"n": 0}
_BZ_EXIT2_NOTIFY_AT = 2  # ~2 * 10min ≈ 20 分钟连挂再叫, 排除偶发抖动
# fetcher 暂停时间戳 —— 服务端 revoke 时置一段冷却期, 别继续每 10min 撞
# benzinga 登录页把 Cloudflare 反爬计数拉高触发全站封 IP。作者点重登成功后清零。
# 2026-08-10 加: log 里累积 161 次 exit 2 撞 login, 触发 "Sorry, you have been blocked".
_BZ_PAUSED_UNTIL: float = 0.0
_BZ_PAUSE_MINUTES = 60  # revoke 命中 → 停 60min 不 kick 让 IP 冷却


def _bz_relogin_reason(returncode, session, streak):
    """给定一次 fetcher 结果, 判定该不该弹重登通知。

    返回 (should_notify: bool, title: str, msg: str) —— 不弹时后两个是空串。

    判据(第二条是 2026-08-10 修的盲点):
      A) exit 2 且 cookie 文件里 expires 已到 → 立刻弹。
      B) exit 2 且 streak >= 阈值 → 服务端提前 revoke(文件里 expires 可能还剩很多天)。
         历史上 08-07 停更就是这条盲点漏掉:文件说还剩 72 天, 服务端已把 session 踢掉。
    """
    if returncode != 2:
        return False, "", ""
    if session and session.get("expired"):
        return True, "🔑 Benzinga 登录已过期", \
            "pre-market 抓不到了。点这条通知开浏览器重新登录。"
    if streak >= _BZ_EXIT2_NOTIFY_AT:
        return True, "🔑 Benzinga 登录态失效", \
            f"连续 {streak} 次抓不到 pre-market(cookie 未到期但服务端已 revoke)。点这条重新登录。"
    return False, "", ""


async def _notify_benzinga_relogin(title: str, msg: str) -> None:
    """弹一条**可点**的通知:点一下就在 Terminal 里打开重新登录流程。
    同一天只弹一次, 免得 10 分钟一轮的 loop 刷屏。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if _BZ_RELOGIN_NOTIFIED["day"] == today:
        return
    _BZ_RELOGIN_NOTIFIED["day"] = today
    if _notifications_muted():
        return
    import shutil as _shutil
    tn = _shutil.which("terminal-notifier") or next(
        (c for c in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
         if os.path.exists(c)), None)
    inner = _benzinga_relogin_cmd().replace("\\", "\\\\").replace('"', '\\"')
    osa = (f'tell application "Terminal" to do script "{inner}"\n'
           f'tell application "Terminal" to activate')
    try:
        if tn:
            await asyncio.create_subprocess_exec(
                tn, "-title", title, "-message", msg,
                "-execute", f"osascript -e {shlex.quote(osa)}",
                "-sound", "Basso")
        else:   # 没装 terminal-notifier 就退回不可点的系统通知
            await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'display notification {json.dumps(msg)} with title {json.dumps(title)}')
        print(f"[Benzinga] 已弹重新登录提醒: {msg}")
    except Exception as e:
        print(f"[Benzinga] 通知失败: {e}")


def _seconds_until_next_830():
    """Return seconds until the next weekday 08:30 local time."""
    now = datetime.datetime.now()
    target = now.replace(hour=8, minute=30, second=0, microsecond=0)
    if now >= target or now.weekday() >= 5:
        target += datetime.timedelta(days=1)
    while target.weekday() >= 5:
        target += datetime.timedelta(days=1)
    return max(0, (target - now).total_seconds())


async def benzinga_daily_refresh():
    """Run the Benzinga fetcher once at 08:30 CT on weekday mornings.

    Sleeps until the next weekday 08:30 instead of polling every 10 min.
    After one attempt (success or fail), sleeps until the next trading day.
    """
    import os as _os
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    fetcher_script = repo_root / "benzinga" / "fetcher.py"
    data_dir = repo_root / "data" / "benzinga"

    await asyncio.sleep(60)  # let server settle / DXLink connect first

    while True:
        try:
            wait = _seconds_until_next_830()
            if wait > 60:
                print(f"[Benzinga] sleeping {wait/3600:.1f}h until next weekday 08:30")
            await asyncio.sleep(wait + 5)  # +5s buffer past 08:30

            now = datetime.datetime.now()
            today_iso = now.strftime("%Y-%m-%d")
            snapshot_exists = (data_dir / f"{today_iso}.json").exists()

            if snapshot_exists:
                print(f"[Benzinga] {today_iso} snapshot already exists, skipping")
                continue

            # 临期预警
            try:
                _sess = _benzinga_session_status()
                _dl = _sess.get("days_left")
                if _sess.get("found") and _dl is not None and 0 < _dl <= 3:
                    await _notify_benzinga_relogin(
                        "🔑 Benzinga 登录即将过期",
                        f"还剩 {_dl:.1f} 天。点这条通知现在就续, 免得哪天早上 pre-market 停更。")
            except Exception:
                pass

            print(f"[Benzinga] kicking fetcher at {now.strftime('%H:%M')}")
            log_path = data_dir / "_autofetch.log"
            try:
                if log_path.exists() and log_path.stat().st_size > 2_000_000:
                    log_path.write_text("", encoding="utf-8")
            except Exception:
                pass
            logf = open(log_path, "a", encoding="utf-8")
            try:
                logf.write(f"\n===== {now.strftime('%Y-%m-%d %H:%M:%S')} kicking fetcher =====\n")
                logf.flush()
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(fetcher_script),
                    cwd=str(repo_root),
                    stdout=logf, stderr=logf,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=600)
                    logf.write(f"===== fetcher exited with {proc.returncode} =====\n")
                    print(f"[Benzinga] fetcher exited with {proc.returncode} (log: {log_path})")
                    if proc.returncode == 0:
                        _BZ_EXIT2_STREAK["n"] = 0
                    elif proc.returncode == 2:
                        _BZ_EXIT2_STREAK["n"] += 1
                        _should, _title, _msg = _bz_relogin_reason(
                            proc.returncode, _benzinga_session_status(),
                            _BZ_EXIT2_STREAK["n"])
                        if _should:
                            await _notify_benzinga_relogin(_title, _msg)
                except asyncio.TimeoutError:
                    proc.kill()
                    logf.write("===== fetcher TIMED OUT after 10 min =====\n")
                    print("[Benzinga] fetcher timed out after 10 min")
            finally:
                try:
                    logf.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[Benzinga] daily refresh error: {e}")
            await asyncio.sleep(3600)


def _price_tracker_module():
    """Lazy import of trade_journal.price_tracker (single source of truth
    for the snapshot Parquet store)."""
    import importlib
    import sys
    if str(TRADE_JOURNAL_DIR) not in sys.path:
        sys.path.insert(0, str(TRADE_JOURNAL_DIR))
    return importlib.import_module("price_tracker")


async def option_price_snapshot_loop():
    """Once per hour during US-market trading hours (09:30-16:00 ET, Mon-Fri),
    snapshot the mark of every leg from every still-trackable Decision Log
    entry (open trades + closed trades whose options haven't expired yet)
    to a Parquet file under trade_journal/data/option_prices/.

    The drawer's price-history chart reads this store back to plot how the
    spread's value evolved.

    Network gaps just leave holes — already-saved points stay valid.
    Laptop-sleep gaps look the same. We don't try to backfill.
    """
    await asyncio.sleep(45)   # let other loaders settle
    last_snapshot_hour = None
    while True:
        try:
            pt = _price_tracker_module()
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            in_window = pt.is_market_open(now_utc)
            # Bucket by ET clock-hour (DST-aware) so we snapshot at most once
            # per market-hour bucket (09:30, 10:00-, 11:00-, ..., 15:00-).
            try:
                from zoneinfo import ZoneInfo
                et = now_utc.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                et = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=-4)))
            this_hour_key = et.strftime("%Y%m%d-%H")
            if in_window and last_snapshot_hour != this_hour_key:
                je = _journal_entries_module()
                opens = je.load_open_entries()
                closes = je.load_close_entries()
                symbols = pt.tracked_symbols(opens, closes)
                if symbols and state.client:
                    # Option marks (existing batched REST)
                    BATCH = 50
                    marks = {}
                    for i in range(0, len(symbols), BATCH):
                        chunk = symbols[i:i+BATCH]
                        marks.update(fetch_option_marks(state.client, chunk))
                    # Underlying spot prices — needed to solve IV + Greeks
                    underlyings = sorted({
                        s.split()[0] for s in symbols
                        if s.split()
                    })
                    spots = fetch_stock_marks(state.client, underlyings)
                    if marks:
                        path = pt.snapshot_to_parquet(marks, spots=spots)
                        if path:
                            now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
                            print(f"[PriceTracker] {now_et.strftime('%H:%M ET')} "
                                   f"snapshot: {len(marks)} legs ({len(spots)} spots) "
                                   f"→ {path.name}")
                            last_snapshot_hour = this_hour_key
        except Exception as e:
            print(f"[PriceTracker] loop warn: {e}")
        await asyncio.sleep(300)   # check every 5 minutes


async def periodic_mark_refresh():
    """Refresh option marks via TastyTrade REST every 30s.
    This is what drives leg.current → strategy unrealized P/L, matching the app."""
    await asyncio.sleep(5)  # wait for initial load
    while True:
        try:
            if state.client and state.strategies and is_market_hours():
                opt_syms = sorted({l["symbol"] for s in state.strategies for l in s["legs"]})
                marks = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_option_marks, state.client, opt_syms
                )
                updated = 0
                for strat in state.strategies:
                    for leg in strat["legs"]:
                        m = marks.get(leg["symbol"])
                        if not m:
                            continue
                        leg["mark"] = m["mark"]
                        leg["last"] = m["last"]
                        # prev_close stays as set in group_into_strategies (from position close-price)
                        cv = _current_from_mark(m["mark"], m["last"])
                        if cv is not None:
                            leg["current"] = cv
                            updated += 1
                if updated and state.ws_clients:
                    msg = json.dumps({"type": "marks_refresh"})
                    dead = []
                    for ws in state.ws_clients:
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.append(ws)
                    for d in dead:
                        state.ws_clients.remove(d)
                # Event-driven take-profit push: evaluate against the marks we JUST
                # refreshed and fire immediately on a crossing — no waiting on a
                # separate 60s timer. Primary alert path (works whether the PWA is
                # foreground / background / closed). _EXIT_FIRED dedups vs the
                # exit_alert_loop backstop.
                await _run_exit_alert_check()
        except Exception as e:
            print(f"[MarkRefresh] Error: {e}")
        await asyncio.sleep(30)


# ── FastAPI endpoints ──

async def initial_midprice_fetch():
    """Async one-shot DXLink fetch for initial mid prices."""
    import websockets as ws_lib

    if not state.dxlink_url or not state.dxlink_token or not state.symbols_map:
        return

    symbols = list(state.symbols_map.keys())
    print(f"[Init] Fetching initial mid prices for {len(symbols)} options...")

    try:
        async with ws_lib.connect(state.dxlink_url, close_timeout=5) as ws:
            await ws.send(json.dumps({"type": "SETUP", "channel": 0, "version": "0.1-js/1.0.0", "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}))
            await ws.recv()
            await ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": state.dxlink_token}))
            await ws.recv()
            await ws.send(json.dumps({"type": "CHANNEL_REQUEST", "channel": 1, "service": "FEED", "parameters": {"contract": "AUTO"}}))
            await ws.recv()
            await ws.send(json.dumps({"type": "FEED_SETUP", "channel": 1, "acceptAggregationPeriod": 0, "acceptDataFormat": "COMPACT",
                "acceptEventFields": {"Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"], "Trade": ["eventSymbol", "price", "dayVolume", "change"]}}))
            all_syms = symbols + state.equity_symbols
            subs = [{"type": "Quote", "symbol": s} for s in all_syms] + [{"type": "Trade", "symbol": s} for s in all_syms]
            await ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs}))

            deadline = asyncio.get_event_loop().time() + 15
            updated = 0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    if data.get("type") == "FEED_DATA" and data.get("channel") == 1:
                        feed = data.get("data", [])
                        # Trade events: store last trade price + compute prev close
                        if isinstance(feed, list) and len(feed) >= 2 and feed[0] == "Trade" and isinstance(feed[1], list):
                            arr = feed[1]
                            for i in range(0, len(arr) - 3, 4):
                                sym = arr[i]
                                if isinstance(sym, str):
                                    try:
                                        tp = float(arr[i+1]) if arr[i+1] not in ("NaN", None) else 0
                                        if tp > 0:
                                            state.live_quotes.setdefault(sym, {})["last_trade"] = tp
                                            # prev_close comes from TastyTrade's authoritative
                                            # prev-close (fetch_equity_quotes), NOT Trade.change.
                                            # Update equity prices from Trade
                                            if sym in state.equity_symbols:
                                                state.quotes[sym] = round(tp, 2)
                                                for strat in state.strategies:
                                                    if strat["underlying"] == sym:
                                                        strat["current_price"] = round(tp, 2)
                                    except (ValueError, TypeError):
                                        pass
                        # Quote events
                        if isinstance(feed, list) and len(feed) >= 2 and feed[0] == "Quote" and isinstance(feed[1], list):
                            arr = feed[1]
                            for i in range(0, len(arr) - 4, 5):
                                sym = arr[i]
                                if not isinstance(sym, str):
                                    continue
                                try:
                                    b = float(arr[i+1]) if arr[i+1] not in ("NaN", None) else 0
                                    a = float(arr[i+2]) if arr[i+2] not in ("NaN", None) else 0
                                except (ValueError, TypeError):
                                    continue
                                if b <= 0 and a <= 0:
                                    continue
                                # Display mid only — current is managed by REST mark fetch.
                                last_trade = state.live_quotes.get(sym, {}).get("last_trade", 0)
                                if b > 0 and a > 0:
                                    simple_mid = round((b + a) / 2, 2)
                                    spread_pct = (a - b) / simple_mid if simple_mid > 0 else 0
                                    if spread_pct > 0.15 and last_trade > 0 and b <= last_trade <= a:
                                        mid = round(last_trade, 2)
                                    else:
                                        mid = simple_mid
                                elif a > 0 and last_trade > 0 and last_trade <= a:
                                    mid = round(last_trade, 2)
                                else:
                                    mid = round((b + a) / 2, 2)
                                state.live_quotes.setdefault(sym, {}).update({"bid": b, "ask": a})
                                # Update equity prices
                                if sym in state.equity_symbols:
                                    eq_mid = round((b + a) / 2, 2)
                                    state.quotes[sym] = eq_mid
                                    for strat in state.strategies:
                                        if strat["underlying"] == sym:
                                            strat["current_price"] = eq_mid
                                # Update option legs (bid/ask/mid for display — NOT current)
                                if sym in state.symbols_map:
                                    for si, li in state.symbols_map[sym]:
                                        state.strategies[si]["legs"][li]["bid"] = b
                                        state.strategies[si]["legs"][li]["ask"] = a
                                        state.strategies[si]["legs"][li]["mid"] = mid
                                        updated += 1
                    if len(state.live_quotes) >= len(symbols):
                        break
                except asyncio.TimeoutError:
                    break

        print(f"[Init] Updated {updated} legs with mid prices")
    except Exception as e:
        print(f"[Init] DXLink fetch failed: {e}")


async def equity_price_loop():
    """Refresh equity spot + AUTHORITATIVE prev-close every 5 min.

    PRIMARY source = TastyTrade REST (/market-data/by-type). Its `prev-close` is
    the official prior-session settle, so the underlying day-change% on the
    Positions screen matches the broker app + TradingView exactly. yfinance is a
    fallback only for symbols TastyTrade didn't return.

    NOT gated on market hours: prev_close is a daily constant, so this also rolls
    the baseline forward to the new session each morning. (The old version derived
    prev_close from DXLink Trade.change + yfinance fast_info, which froze multiple
    days stale → wrong / even wrong-sign day-change% while the spot was correct.)
    """
    INTERVAL = 300   # 5 minutes
    await asyncio.sleep(8)   # seed prev_close shortly after startup, not +5min
    while True:
        try:
            syms = list(state.equity_symbols)
            loop = asyncio.get_event_loop()
            got_pc = set()
            # 1) TastyTrade authoritative — price + official prev-close
            if state.client and syms:
                q = await loop.run_in_executor(
                    None, fetch_equity_quotes, state.client, syms)
                for sym, rec in q.items():
                    pc = rec.get("prev_close")
                    if pc:
                        state.equity_prev_close[sym] = pc
                        got_pc.add(sym)
                    px = rec.get("price")
                    if px:
                        state.quotes[sym] = px
                        for strat in state.strategies:
                            if strat["underlying"] == sym:
                                strat["current_price"] = px
                        for e in (getattr(state, "equities", None) or []):
                            if e.get("symbol") == sym:
                                e["current"] = px   # live pure-stock price for NLV
                if got_pc:
                    print(f"  [equity-prices] TT prev-close {len(got_pc)}/{len(syms)}")
            # 2) yfinance fallback ONLY for symbols TastyTrade didn't cover
            missing = [s for s in syms if s not in got_pc]
            if HAS_YF and missing:
                def _yf(tickers):
                    results = {}
                    for t in tickers:
                        try:
                            info = yf.Ticker(t).fast_info
                            price = round(info.last_price, 2) if info.last_price else 0
                            prev = round(info.previous_close, 2) if info.previous_close else 0
                            if prev > 0 or price > 0:
                                results[t] = {"price": price, "prev": prev}
                        except Exception:
                            pass
                    return results
                yf_res = await loop.run_in_executor(None, _yf, missing)
                for sym, data in yf_res.items():
                    if data.get("prev", 0) > 0:
                        state.equity_prev_close[sym] = data["prev"]
                    if data.get("price", 0) > 0:
                        state.quotes[sym] = data["price"]
                        for strat in state.strategies:
                            if strat["underlying"] == sym:
                                strat["current_price"] = data["price"]
                        for e in (getattr(state, "equities", None) or []):
                            if e.get("symbol") == sym:
                                e["current"] = data["price"]   # live pure-stock price
                if yf_res:
                    print(f"  [equity-prices] yfinance fallback {len(yf_res)}")
        except Exception as e:
            print(f"[equity-prices] error: {e}")
        await asyncio.sleep(INTERVAL)


async def option_activity_archive_loop():
    """每 15 分钟(盘中)把今天的 watchlist 异动期权流分批抓取、按 id 去重累积写进
    data/market_events/option_activity_YYYY-MM.parquet(zstd, 只必要列)。复用 PWA 进程
    (已有 ~/Desktop 权限), 免 launchd/Terminal。作者设计 2026-06-04。"""
    import sys as _sys
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    await asyncio.sleep(30)        # 让启动期其它任务先跑
    while True:
        try:
            if is_market_hours():
                from benzinga_market_events import archive_oa
                _loop = asyncio.get_event_loop()
                rows = await _loop.run_in_executor(None, archive_oa.fetch_today)
                n = await _loop.run_in_executor(None, archive_oa.upsert, rows)
                print(f"[oa-archive] {len(rows)} watchlist trades → archive {n} rows")
        except Exception as e:
            print("[oa-archive] error:", e)
        await asyncio.sleep(900)


async def calendar_events_refresh_loop():
    """每天自动刷新财报+经济日历缓存(calendar_cache.json), 省得手动点 Refresh 爬。
    启动时若缓存缺失/超 12h 旧才刷一次(频繁重启不会重爬), 之后每 24h 刷。refresh() 做 ~60 个
    阻塞 HTTP(NASDAQ 逐日 + 经济源)→ 丢线程池, 不堵事件循环。"""
    import datetime as _dt
    await asyncio.sleep(45)        # 让启动期其它任务先跑
    while True:
        try:
            mod = _calendar_events_module()
            stale = True
            try:
                fa = (mod.load_cached() or {}).get("fetched_at")
                if fa:
                    age = (_dt.datetime.now() - _dt.datetime.fromisoformat(fa)).total_seconds()
                    stale = age > 12 * 3600
            except Exception:
                stale = True
            if stale:
                _loop = asyncio.get_event_loop()
                await _loop.run_in_executor(None, mod.refresh)
                print("[calendar] 自动刷新财报/经济日历缓存完成")
        except Exception as e:
            print("[calendar] 自动刷新出错:", e)
        await asyncio.sleep(24 * 3600)


@app.on_event("startup")
async def startup():
    # Sync data fetch (positions, quotes, metrics)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fetch_positions_and_strategies)
    # Async initial mid price fetch
    await initial_midprice_fetch()
    # Start persistent stream + periodic refresh + equity prices + mark refresh
    _tt = _hc.has_tastytrade()            # live broker connected?
    _bz = _hc.has_benzinga()              # Benzinga Pro cookies pasted?
    _ai = _hc.ai_schedules_enabled()      # an AI provider configured + schedules on?
    print(f"[ZEUS] broker={_hc.broker_mode()}  benzinga={_bz}  ai={_hai.provider()}  "
          f"free-news={','.join(_zeus_news.enabled_sources()) or 'off'}")
    asyncio.create_task(dxlink_stream())              # idles until a tastytrade DXLink token exists
    asyncio.create_task(periodic_refresh())
    asyncio.create_task(balance_refresh_loop())
    asyncio.create_task(equity_price_loop())
    asyncio.create_task(periodic_mark_refresh())
    asyncio.create_task(hourly_journal_refresh())
    if _tt:
        # backtest_scheduler (the signal backtest) is not started: it opens trades
        # from SSP1 screener signals, which this repository does not ship, and it
        # has no page, so it would only download 264 option chains every weekday.
        # The code and /api/backtest/* stay for whoever adds a signal source and a page.
        asyncio.create_task(missing_gtc_loop())       # reads working orders on the broker
    if _ai:
        asyncio.create_task(positions_ai_scheduler())
        asyncio.create_task(digest_ai_morning_loop())   # daily 8:30 AI news summary
        asyncio.create_task(market_brief_daily_loop())  # daily 9:00 market brief
    asyncio.create_task(option_price_snapshot_loop())
    if _bz:
        asyncio.create_task(benzinga_daily_refresh())
        asyncio.create_task(digest_today_live_loop())
    asyncio.create_task(exit_alert_loop())
    asyncio.create_task(credit_spread_alert_loop())
    asyncio.create_task(risk_radar_alert_loop())
    asyncio.create_task(decision_log_reminder_loop())
    if _hc.get_bool("finviz_ai_crawl"):
        asyncio.create_task(finviz_ai_crawl_loop())
    asyncio.create_task(heatmap_popup_loop())
    asyncio.create_task(heatmap_ohlc_warm_loop())
    asyncio.create_task(sr_zone_alert_loop())
    asyncio.create_task(bilingual_daily_notify_loop())
    asyncio.create_task(free_news_loop())             # Finviz + WSJ + MarketWatch, bilingual
    # TA screener 交易日定时自动扫(ET 9:40/10:30/12:00/15:00;router 加载失败则跳过)
    try:
        from ta_screener.scheduler import run_scheduler as _ta_sched
        asyncio.create_task(_ta_sched())
    except Exception as _ta_se:
        print(f"[TA Screener] scheduler 未启动: {_ta_se}")
    if _bz:
        asyncio.create_task(option_activity_archive_loop())
    asyncio.create_task(calendar_events_refresh_loop())

    # --- heatmap ---
    # Bootstrap the metadata cache off the event loop if empty, then kick
    # the polling task. Mirrors heatmap/serve.py's startup behavior.
    try:
        # Always kick an incremental metadata refresh in the background.
        # refresh() only fetches tickers that are missing or >24h stale, so it's
        # cheap on a warm cache and auto-fills new names when the universe file
        # (data/heatmap_universe.txt) changes. Off the loop — yfinance blocks.
        if not _heatmap_metadata.get_all():
            print("[heatmap] metadata cache empty — bootstrapping (yfinance)…")
        asyncio.get_event_loop().run_in_executor(
            None, lambda: _heatmap_metadata.refresh(log=True)
        )
        _heatmap_start_poller()
        if _tt:
            _heatmap_start_stream()   # DXLink real-time push; poller becomes fallback
        _heatmap_warm_etf()       # warm ETF-trends cache (not real-time)
    except Exception as e:
        print(f"[heatmap] startup error: {e}")
    # --- /heatmap ---
    # --- signals: persistent WS feed (real-time signal tab) ---
    try:
        if _hc.has_benzinga_apikey():
            _signals_start_feed()
    except Exception as e:
        print(f"[signals] startup error: {e}")
    # --- /signals ---


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the HTML page with live mode enabled."""
    # Inject data + live WebSocket JS into the template
    data = {
        "generated_at": datetime.datetime.now().isoformat(),
        "strategies": state.strategies,
        "equities": state.equities,
        "equity_prev_close": state.equity_prev_close,
        "balance": state.balance,
    }
    data_json = json.dumps(data, indent=2)

    # Build the HTML with live WebSocket overlay
    html = HTML_TEMPLATE.replace("/*__DATA_PLACEHOLDER__*/", data_json)

    # Inject live WebSocket client code before </script>
    live_js = LIVE_JS_SNIPPET
    html = html.replace("window.addEventListener('DOMContentLoaded', init);",
                         live_js + "\nwindow.addEventListener('DOMContentLoaded', init);")
    # --- history news ---
    html += "<script>" + HISTORY_NEWS_JS + "</script>"
    # --- /history news ---

    # --- heatmap ---
    html += "<script>" + HEATMAP_JS + "</script>"
    # --- /heatmap ---

    # --- sec filings ---
    html += "<script>" + SEC_JS + "</script>"
    html += "<script>" + ME_JS + "</script>"
    html += "<script>" + SIGNALS_JS + "</script>"
    # --- /sec filings ---

    # --- paper trade ---
    html += "<script>" + PAPER_TRADE_JS + "</script>"
    # --- /paper trade ---


    # Add LIVE indicator + top-nav CSS
    live_css = """
.live-badge {
  display: inline-flex; align-items: center; gap: 5px;
  background: rgba(63,185,80,0.15); color: var(--green);
  padding: 3px 10px; border-radius: 4px; font-size: 11px;
  font-weight: 700; letter-spacing: 0.5px;
}
.live-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--green); animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { opacity: 1; } 50% { opacity: 0.3; }
}
@keyframes slideIn {
  from { transform: translateX(100px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
.live-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 16px; background: var(--bg2);
  border-bottom: 1px solid var(--border); font-size: 12px;
}
.live-bar .stock-price { font-size: 14px; font-weight: 700; }
.live-bar .last-update { color: var(--text2); font-size: 11px; margin-left: auto; }

/* Top navigation tabs */
.top-nav {
  display: flex; align-items: center; gap: 0;
  background: #0a0e14; border-bottom: 2px solid #1f6feb;
  padding: 0 16px; height: 38px; flex-shrink: 0;
  position: sticky; top: 0; z-index: 1000;
}
.top-nav-brand { font-size: 14px; font-weight: 800; color: #58a6ff; margin-right: 14px; flex-shrink: 0; white-space: nowrap; }
/* Every item stays on one line in every language. Tabs that do not fit scroll sideways (wheel or
   trackpad, zeus.js) instead of wrapping: wrapped labels grew taller than the bar and were cut off. */
.top-nav-tabs {
  display: flex; align-self: stretch; flex: 1 1 auto; min-width: 0; margin-bottom: -2px;
  overflow-x: auto; overflow-y: hidden; scrollbar-width: none; overscroll-behavior-x: contain;
}
.top-nav-tabs::-webkit-scrollbar { display: none; }
.top-nav-tabs.zs-more-r { -webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 32px), transparent); mask-image: linear-gradient(90deg, #000 calc(100% - 32px), transparent); }
.top-nav-tabs.zs-more-l { -webkit-mask-image: linear-gradient(90deg, transparent, #000 32px); mask-image: linear-gradient(90deg, transparent, #000 32px); }
.top-nav-tabs.zs-more-l.zs-more-r { -webkit-mask-image: linear-gradient(90deg, transparent, #000 32px, #000 calc(100% - 32px), transparent); mask-image: linear-gradient(90deg, transparent, #000 32px, #000 calc(100% - 32px), transparent); }
.top-nav-tab {
  display: flex; align-items: center; gap: 4px; flex-shrink: 0; white-space: nowrap;
  padding: 0 13px; font-size: 12px; font-weight: 600;
  color: #8b949e; cursor: pointer; border-bottom: 2px solid transparent;
}
.top-nav-tab:hover { color: #e6edf3; }
.top-nav-tab.active { color: #58a6ff; border-bottom-color: #1f6feb; }
.top-nav-right { margin-left: 10px; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.top-nav-right > * { flex-shrink: 0; white-space: nowrap; }
/* narrower windows: tighter tabs first, then the status text and the brand's subtitle go */
@media (max-width: 1240px) {
  .top-nav-tab { padding: 0 9px; }
  .top-nav-right { margin-left: 6px; gap: 6px; }
}
@media (max-width: 1100px) {
  .top-nav-brand > span, #trStatusCur { display: none; }
  .top-nav-tab { padding: 0 8px; }
}
.top-tab-panel { display: none; flex: 1; min-height: 0; }
.top-tab-panel.active { display: flex; flex-direction: column; }
/* Benzinga Digest rendered markdown */
.digest-body { font-size: 13px; line-height: 1.7; color: #c9d1d9; max-width: 920px; }
.digest-body h1 { font-size: 20px; color: #e6edf3; margin: 0 0 6px; }
.digest-body h2 { font-size: 14.5px; color: #e6edf3; margin: 16px 0 4px; scroll-margin-top: 64px; font-weight: 650; }
.digest-body blockquote { color: #8b949e; border-left: 3px solid #30363d; margin: 6px 0 14px; padding: 2px 12px; font-size: 12px; }
.digest-body a { color: #58a6ff; text-decoration: none; }
.digest-body a:hover { text-decoration: underline; }
.digest-body ul { padding-left: 16px; margin: 6px 0; }
.digest-body li { margin: 2px 0; }
.digest-body code { background: #161b22; padding: 1px 5px; border-radius: 4px; font-size: 12px; color: #79c0ff; }
.digest-body sub { color: #6e7681; font-size: 11px; }
.digest-body strong { color: #e6edf3; }
.digest-body hr { border: none; border-top: 1px solid #21262d; margin: 12px 0; }
.digest-body .dg-toc { list-style: none; padding-left: 0; margin: 6px 0; }
.digest-body .dg-toc li { margin: 2px 0; }
.digest-body .dg-tk { color: #6e7681; font-size: 11px; }
.digest-body .dg-art { margin: 14px 0; padding-bottom: 10px; border-bottom: 1px solid #21262d; }
.digest-body .dg-art h3 { font-size: 14.5px; color: #e6edf3; margin: 0 0 4px; scroll-margin-top: 64px; font-weight: 650; }
.digest-body .dg-meta { color: #8b949e; font-size: 12px; margin: 2px 0; }
.digest-body .dg-cn { margin: 6px 0; }
.digest-body .dg-en { display: none; color: #8b949e; font-size: 12px; margin: 2px 0 8px; padding-left: 8px; border-left: 2px solid #30363d; }
.digest-body.bilingual .dg-en { display: block; }
.digest-body .dg-title-en { font-style: italic; }
.digest-body .dg-back { font-size: 11px; color: #6e7681; }
.digest-body .dg-img { max-width: 100%; height: auto; border-radius: 6px; margin: 8px 0; border: 1px solid #21262d; }
.digest-body .dg-chan { display: inline-block; background: #1f2937; color: #9cc3ff; border: 1px solid #30417a; border-radius: 10px; padding: 0 7px; margin-right: 4px; font-size: 11px; }
.digest-body .dg-teaser { color: #adbac7; font-size: 12.5px; font-style: italic; margin: 4px 0 6px; padding-left: 8px; border-left: 2px solid #30363d; }
.digest-body .dg-sent { font-size: 11px; font-weight: 600; padding: 1px 6px; border: 1px solid; border-radius: 10px; margin-left: 6px; vertical-align: middle; }
/* back-btn 默认 display:none — 在 Digest 面板里强制显示(年/月跳转、双语、刷新按钮) */
#tab-digest .back-btn { display: inline-flex !important; }
#digestBiBtn { color: #58a6ff; }
#digestFilter { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 12px; font-size: 12px; }
/* 统一 digest 里所有筛选按钮/输入: 同一高度(28px)、圆角、内距, 一致美观。
   (原来 .df-chip/.df-pill 的样式被限定在 #digestChanChips/#digestFilter, 本日筛选条
   #digestInDayBar 里的没匹配到 → 渲染成默认丑灰按钮、大小随文字变。改 #tab-digest 作用域。) */
#tab-digest .df-in { height: 28px; box-sizing: border-box; background: #0d1117; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px; padding: 0 9px; font-size: 12px; }
#tab-digest .df-in:focus { outline: none; border-color: #58a6ff; }
#tab-digest .df-pill { display: inline-flex; align-items: center; height: 28px; box-sizing: border-box; background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 0 12px; font-size: 12px; font-weight: 600; line-height: 1; cursor: pointer; white-space: nowrap; }
#tab-digest .df-pill:hover { background: #30363d; border-color: #3d444d; }
#tab-digest .df-go { display: inline-flex; align-items: center; height: 28px; box-sizing: border-box; background: #238636; color: #fff; border: none; border-radius: 6px; padding: 0 16px; font-size: 12px; font-weight: 600; cursor: pointer; }
#tab-digest .df-go:hover { background: #2ea043; }
#tab-digest .df-chip { display: inline-flex; align-items: center; height: 28px; box-sizing: border-box; background: #161b22; color: #9cc3ff; border: 1px solid #30417a; border-radius: 6px; padding: 0 12px; font-size: 12px; font-weight: 600; line-height: 1; cursor: pointer; white-space: nowrap; }
#tab-digest .df-chip:hover { background: #1f2937; border-color: #3d4f8a; }
#tab-digest .df-chip.active, #tab-digest .df-pill.active { background: #1f6feb !important; color: #fff !important; border-color: #1f6feb !important; }
#tab-digest .df-chip.dg-mustread { color: #e3b341; border-color: #9e7a1e; }
#tab-digest .df-chip.dg-mustread.active { background: #bb8009 !important; color: #fff !important; border-color: #bb8009 !important; }
#tab-digest .dg-mrbadge { display: inline-block; margin-left: 6px; padding: 0 6px; height: 16px; line-height: 16px; border-radius: 8px; font-size: 10px; font-weight: 700; vertical-align: middle; }
#tab-digest .dg-mrbadge.a { background: #3a2d05; color: #e3b341; border: 1px solid #9e7a1e; }
#tab-digest .dg-mrbadge.b { background: #0d2b3e; color: #58a6ff; border: 1px solid #1f6feb; }
/* 两模式按钮 — 同样 28px 高度对齐 */
#tab-digest .dg-mode { display: inline-flex; align-items: center; height: 28px; box-sizing: border-box; background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 6px; padding: 0 14px; font-size: 12px; font-weight: 600; cursor: pointer; }
#tab-digest .dg-mode.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
/* 目录列表 + 点开展开 */
.dg-list { max-width: 1100px; }
.dg-list .dg-row { border-bottom: 1px solid #1b2129; }
.dg-list .dg-rowhead { display: flex; align-items: baseline; gap: 8px; padding: 7px 4px; cursor: pointer; }
.dg-list .dg-rowhead:hover { background: #11161d; }
.dg-list .dg-rowhead .t { color: #6e7681; font-size: 11px; font-family: ui-monospace, monospace; flex: 0 0 auto; min-width: 86px; }
.dg-list .dg-rowhead .ttl { color: #cdd9e5; font-size: 13px; flex: 1 1 auto; }
.dg-list .dg-row.open .dg-rowhead .ttl { color: #58a6ff; font-weight: 600; }
.dg-list .dg-rowhead .tags { font-size: 10px; color: #6e7681; flex: 0 0 auto; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-list .dg-rowbody { padding: 4px 4px 14px 18px; border-left: 2px solid #21262d; margin: 0 0 6px 8px; }
.dg-list .dg-rowbody p { margin: 6px 0; color: #adbac7; font-size: 13px; }
.dg-list .dg-rowbody .dg-meta { color: #8b949e; font-size: 12px; margin: 3px 0; }
.dg-list .dg-rowbody .dg-chan { display: inline-block; background: #1f2937; color: #9cc3ff; border: 1px solid #30417a; border-radius: 10px; padding: 0 7px; margin-right: 4px; font-size: 11px; }
.dg-list .dg-rowbody .dg-teaser { color: #adbac7; font-size: 12.5px; font-style: italic; margin: 4px 0 6px; }
.dg-list .dg-rowbody img.dg-img { max-width: 100%; height: auto; border-radius: 6px; margin: 8px 0; border: 1px solid #21262d; }
.dg-list .dg-rowbody a { color: #58a6ff; }
.dg-list .dg-sent { font-size: 11px; font-weight: 600; padding: 1px 6px; border: 1px solid; border-radius: 10px; }
/* 双语标题：英文主、中文次(灰、稍小、另起一行) */
.dg-list .dg-rowhead .ttl { display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; }
.dg-list .dg-rowhead .ttl-en { color: #cdd9e5; font-size: 13px; }
.dg-list .dg-row.open .dg-rowhead .ttl-en { color: #58a6ff; font-weight: 600; }
.dg-list .dg-rowhead .ttl-cn { color: #8b949e; font-size: 12px; }
.dg-list .dg-reload { flex-shrink: 0; cursor: pointer; padding: 0 5px; margin-left: 6px; font-size: 12px; color: #6e7681; border: 1px solid transparent; border-radius: 3px; user-select: none; opacity: .55; transition: opacity .15s, color .15s, border-color .15s; }
.dg-list .dg-reload:hover { opacity: 1; color: #1f6feb; border-color: #1f6feb; }
.dg-list .dg-reload.busy { color: #1f6feb; opacity: 1; }
/* Tickers 排行榜: 时间 chip + ticker 卡片 */
#digestPaneTickers .dg-tk-day { background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 600; cursor: pointer; }
#digestPaneTickers .dg-tk-day.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
#digestPaneTickers .dg-tk-day:hover:not(.active) { background: #2d333b; color: #cdd9e5; }
#digestPaneTickers .dg-tk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; }
#digestPaneTickers .dg-tk-card { display: flex; align-items: center; justify-content: space-between; background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px; cursor: pointer; transition: background .1s, border-color .1s; }
#digestPaneTickers .dg-tk-card:hover { background: #1f2733; border-color: #58a6ff; }
#digestPaneTickers .dg-tk-sym { color: #58a6ff; font-weight: 700; font-size: 13px; }
#digestPaneTickers .dg-tk-cnt { color: #cdd9e5; font-size: 11px; background: #21262d; border-radius: 10px; padding: 1px 8px; font-variant-numeric: tabular-nums; }
#digestPaneTickers .dg-tk-latest { color: #6e7681; font-size: 10px; font-family: ui-monospace, monospace; margin-top: 2px; }
#digestPaneTickers .dg-tk-px { color: #cdd9e5; font-size: 11px; font-family: ui-monospace, monospace; font-variant-numeric: tabular-nums; margin-top: 2px; letter-spacing: 0.2px; }
/* 💼 持仓 news: 时间 chip 复用 dg-tk-day 样式; ticker count chip */
#digestPanePositions .dg-pos-day { background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 600; cursor: pointer; }
#digestPanePositions .dg-pos-day.active { background: #238636; color: #fff; border-color: #2ea043; }
#digestPanePositions .dg-pos-day:hover:not(.active) { background: #2d333b; color: #cdd9e5; }
#digestPanePositions .dg-pos-tk { display: inline-flex; align-items: center; gap: 4px; background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 2px 8px; font-size: 11px; color: #58a6ff; cursor: pointer; user-select: none; transition: background .1s, border-color .1s; }
#digestPanePositions .dg-pos-tk:hover { background: #1f2733; border-color: #58a6ff; }
#digestPanePositions .dg-pos-tk.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
#digestPanePositions .dg-pos-tk.active .n { background: rgba(255,255,255,0.2); color: #fff; }
#digestPanePositions .dg-pos-tk .n { color: #cdd9e5; background: #21262d; border-radius: 8px; padding: 0 5px; font-variant-numeric: tabular-nums; font-size: 10px; }
#digestPanePositions .dg-pos-all { color: #8b949e; }
.dg-list .dg-trbar { margin: 2px 0 6px; }
.dg-list .dg-trbtn { background: #1f6feb; color: #fff; border: none; border-radius: 6px; padding: 2px 10px; font-size: 11px; cursor: pointer; }
.dg-list .dg-trbtn:hover { background: #388bfd; }
/* 对照：英文白、紧跟中文灰，成对 */
.dg-list .dg-paras .dg-p-en { color: #adbac7; margin: 8px 0 1px; }
.dg-list .dg-paras .dg-p-cn { color: #8b949e; margin: 0 0 4px; font-size: 12.5px; }
/* 紧凑段:多个短标题 term(P/E, 公司, ROE) 合并 · 分隔, 减小 margin */
.dg-list .dg-paras .dg-p-en.dg-p-tight { margin: 4px 0 0; font-size: 12.5px; color: #9ba9b8; }
.dg-list .dg-paras .dg-p-cn.dg-p-tight { margin: 0 0 4px; font-size: 12px; }
.screener-toolbar {
  display: flex; align-items: center; gap: 16px; padding: 8px 16px;
  background: var(--bg2); border-bottom: 1px solid var(--border);
}
.screener-toolbar button {
  background: var(--accent); color: #fff; border: none; border-radius: 4px;
  padding: 5px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
}
.screener-toolbar button:disabled { opacity: 0.5; cursor: not-allowed; }
.screener-toolbar .status { color: var(--text2); font-size: 11px; }
.journal-frame { width: 100%; flex: 1; border: none; min-height: 0; }

/* Screener table styles */
.scr-summary {
  display: flex; gap: 12px; padding: 10px 16px; background: var(--bg2);
  border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text2);
}
.scr-summary .val { font-weight: 700; color: var(--text); margin-left: 4px; }
.scr-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.scr-table th {
  position: sticky; top: 0; background: #0d1117; padding: 8px 10px;
  text-align: left; font-weight: 700; color: #8b949e; text-transform: uppercase;
  font-size: 10px; letter-spacing: 0.5px; border-bottom: 1px solid #21262d;
}
.scr-table td { padding: 6px 10px; border-bottom: 1px solid #161b22; }
.scr-table tr { cursor: pointer; transition: background 0.1s; }
.scr-table tr:hover { background: #161b22; }
.scr-dir { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 3px; }
.scr-dir.bull { background: rgba(63,185,80,0.15); color: #3fb950; }
.scr-dir.bear { background: rgba(248,81,73,0.15); color: #f85149; }
.scr-dir.mixed { background: rgba(210,153,34,0.15); color: #d29922; }
.scr-chg.pos { color: #3fb950; }
.scr-chg.neg { color: #f85149; }
.scr-sig-tag {
  display: inline-block; font-size: 10px; padding: 1px 5px; margin: 1px 2px;
  border-radius: 3px; background: #21262d; color: #c9d1d9;
}
.scr-sig-tag .wr { color: #8b949e; margin-left: 3px; }
.scr-sig-tag.src-bc { background: #1a2d1e; color: #4caf50; border: 1px solid #2e4a32; }
.scr-sig-tag.src-fv { background: #2d1a2e; color: #ab47bc; border: 1px solid #4a2e5a; }
.scr-strength { font-weight: 700; color: #58a6ff; }

/* Screener detail view */
.scr-detail { padding: 16px; }
.scr-detail-header {
  display: flex; align-items: center; gap: 16px; margin-bottom: 16px;
  padding-bottom: 12px; border-bottom: 1px solid #21262d;
}
.scr-detail-header h2 { font-size: 20px; font-weight: 800; margin: 0; }
.scr-back-btn {
  background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 4px; padding: 4px 12px; font-size: 12px; cursor: pointer;
}
.scr-back-btn:hover { background: #30363d; }
.scr-section { margin-bottom: 20px; }
.scr-section h3 {
  font-size: 12px; font-weight: 700; color: #8b949e; text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 8px;
}
.scr-detail-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.scr-detail-table th {
  padding: 6px 10px; text-align: left; font-weight: 700; color: #8b949e;
  font-size: 10px; text-transform: uppercase; border-bottom: 1px solid #21262d;
  background: #0d1117;
}
.scr-detail-table td { padding: 5px 10px; border-bottom: 1px solid #161b22; }
.scr-news-item {
  padding: 8px 0; border-bottom: 1px solid #161b22;
}
.scr-news-item a { color: #58a6ff; text-decoration: none; font-size: 12px; }
.scr-news-item a:hover { text-decoration: underline; }
.scr-news-meta { font-size: 10px; color: #8b949e; margin-top: 2px; }
.scr-sentiment { font-size: 10px; padding: 1px 5px; border-radius: 3px; }
.scr-sentiment.positive { background: rgba(63,185,80,0.15); color: #3fb950; }
.scr-sentiment.negative { background: rgba(248,81,73,0.15); color: #f85149; }
.scr-sentiment.neutral { background: #21262d; color: #8b949e; }

/* Privacy mode */
.privacy-btn {
  background: transparent; border: 1px solid #30363d; border-radius: 4px;
  color: #8b949e; font-size: 14px; padding: 2px 8px; cursor: pointer;
  line-height: 1; transition: all 0.2s;
}
.privacy-btn:hover { background: #21262d; color: #e6edf3; }
.privacy-btn.active { background: #f8514926; border-color: #f85149; color: #f85149; }
/* Positions — dashboard table: first row visible, rest blurred */
.privacy-on #dashBody tr { filter: blur(6px); user-select: none; }
.privacy-on #dashBody tr:first-child { filter: none; user-select: auto; }
/* Positions — detail view: sidebar blurred, main content (table+graph) visible */
.privacy-on #sidebar .strat-item { filter: blur(6px); user-select: none; }
.privacy-on #sidebar .strat-item.active { filter: none; user-select: auto; }
/* Screener tab — blur list/detail data only */
.privacy-on #scr-list-view,
.privacy-on #scr-detail-view { filter: blur(6px); user-select: none; }
/* Journal tab — privacy handled inside iframe via postMessage */
/* Watchlist & Heatmap */
.privacy-on #watchlist-container,
.privacy-on #heatmap-container { filter: blur(6px); user-select: none; }
/* Options Flow chart data */
.privacy-on .of-chart-wrap,
.privacy-on .of-pcr-table,
.privacy-on .of-stats { filter: blur(6px); user-select: none; }

/* Market pulse bar */
.market-pulse {
  display: flex; align-items: center; gap: 14px; padding: 8px 16px;
  background: var(--bg2); border-bottom: 1px solid var(--border);
  font-size: 12px; flex-wrap: wrap;
}
.mp-item {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  background: #161b22; border-radius: 6px; border: 1px solid #21262d;
}
.mp-label { color: #8b949e; font-weight: 600; font-size: 11px; }
.mp-price { color: #e6edf3; font-weight: 700; font-size: 13px; }
.mp-chg { font-weight: 600; font-size: 11px; }
.mp-chg.pos { color: #3fb950; }
.mp-chg.neg { color: #f85149; }
.mp-divider { width: 1px; height: 20px; background: #30363d; }
.mp-fear {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  background: #161b22; border-radius: 6px; border: 1px solid #21262d;
}
.mp-fear-val { font-weight: 700; font-size: 13px; }
.mp-fear-label { color: #8b949e; font-size: 10px; }
.mp-loading { color: #8b949e; font-style: italic; }

/* Screener progress bar */
.scr-progress-wrap {
  padding: 40px 16px; text-align: center;
}
.scr-progress-bar {
  width: 100%; max-width: 500px; height: 6px; background: #21262d;
  border-radius: 3px; margin: 12px auto 8px; overflow: hidden;
}
.scr-progress-fill {
  height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff);
  border-radius: 3px; transition: width 0.5s ease;
}
.scr-progress-text { color: #8b949e; font-size: 12px; }
.scr-progress-pct { color: #58a6ff; font-size: 14px; font-weight: 700; }

/* Filter bar */
.scr-filter-bar {
  display: flex; gap: 16px; padding: 8px 16px; background: var(--bg2);
  border-bottom: 1px solid var(--border); align-items: center; flex-wrap: wrap;
}
.scr-filter-bar label { font-size: 10px; color: #8b949e; text-transform: uppercase; margin-right: 4px; }
.scr-filter-bar select, .scr-multi-btn {
  background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 4px; padding: 4px 8px; font-size: 11px; cursor: pointer;
}
.scr-filter-grp { display: flex; align-items: center; gap: 4px; }
.scr-multi-wrap { position: relative; display: inline-block; }
.scr-multi-btn { min-width: 130px; text-align: left; }
.scr-multi-drop {
  position: absolute; top: 100%; left: 0; background: #161b22;
  border: 1px solid #30363d; border-radius: 4px; padding: 4px 0;
  max-height: 240px; overflow-y: auto; z-index: 100; min-width: 200px;
  display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}
.scr-multi-drop.open { display: block; }
.scr-multi-drop label {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  font-size: 11px; color: #c9d1d9; cursor: pointer; text-transform: none;
}
.scr-multi-drop label:hover { background: #21262d; }
.scr-multi-drop input[type=checkbox] { accent-color: #58a6ff; }
.scr-filter-count {
  font-size: 10px; color: #8b949e; margin-left: auto;
}

/* Sort indicators */
.scr-table th { cursor: pointer; user-select: none; white-space: nowrap; }
.scr-table th:hover { color: #58a6ff; }
.sort-ind { margin-left: 3px; font-size: 9px; opacity: 0.5; }
.sort-ind.active { opacity: 1; color: #58a6ff; }

/* Finviz fundamentals grid */
.fv-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
}
@media (max-width: 900px) { .fv-grid { grid-template-columns: repeat(2, 1fr); } }
.fv-card {
  background: #161b22; border: 1px solid #21262d; border-radius: 6px;
  padding: 10px 12px; font-size: 12px;
}
.fv-card-title {
  font-weight: 700; color: #8b949e; text-transform: uppercase;
  font-size: 10px; letter-spacing: 0.5px; margin-bottom: 8px;
  padding-bottom: 4px; border-bottom: 1px solid #21262d;
}
.fv-row {
  display: flex; justify-content: space-between; padding: 2px 0;
  color: #c9d1d9;
}
.fv-row span:first-child { color: #8b949e; }

/* Options Flow */
.of-section { margin: 16px 0; }
.of-tabs { display: flex; gap: 8px; margin-bottom: 12px; }
.of-tab { padding: 4px 12px; border-radius: 4px; font-size: 11px; cursor: pointer;
  background: #21262d; color: #8b949e; border: 1px solid #30363d; }
.of-tab.active { background: var(--accent, #1f6feb); color: #fff; border-color: var(--accent, #1f6feb); }
.of-charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .of-charts { grid-template-columns: 1fr; } }
.of-chart-box { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 12px; }
.of-chart-title { font-size: 12px; font-weight: 700; color: #c9d1d9; margin-bottom: 8px; text-align: center; }
.of-bar-row { display: flex; align-items: center; gap: 4px; margin: 2px 0; font-size: 10px; }
.of-bar-label { width: 50px; text-align: right; color: #8b949e; flex-shrink: 0; }
.of-bar-wrap { flex: 1; display: flex; height: 16px; position: relative; }
.of-bar-call { background: rgba(63,185,80,0.6); height: 100%; border-radius: 2px 0 0 2px; position: relative; cursor: pointer; }
.of-bar-put { background: rgba(248,81,73,0.6); height: 100%; border-radius: 0 2px 2px 0; position: relative; cursor: pointer; }
.of-bar-call:hover { background: rgba(63,185,80,0.9); }
.of-bar-put:hover { background: rgba(248,81,73,0.9); }
.of-bar-tip { display: none; position: absolute; top: -28px; left: 50%; transform: translateX(-50%);
  background: #161b22; border: 1px solid #30363d; border-radius: 4px; padding: 2px 6px;
  font-size: 10px; color: #c9d1d9; white-space: nowrap; z-index: 5; pointer-events: none; }
.of-bar-call:hover .of-bar-tip, .of-bar-put:hover .of-bar-tip { display: block; }
.of-stats { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; }
.of-stat { background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 8px 14px;
  font-size: 12px; }
.of-stat-label { font-size: 10px; color: #8b949e; text-transform: uppercase; margin-bottom: 2px; }
.of-stat-val { font-weight: 700; font-size: 14px; }
.of-stat-val.bullish { color: #3fb950; }
.of-stat-val.bearish { color: #f85149; }
.of-stat-val.neutral { color: #c9d1d9; }
.of-progress { width: 100%; height: 3px; background: #21262d; border-radius: 2px; overflow: hidden; margin: 8px 0; }
.of-progress-bar { height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); width: 0%;
  transition: width 0.3s; animation: of-load 1.5s ease-in-out infinite; }
@keyframes of-load { 0% { opacity: 0.6; } 50% { opacity: 1; } 100% { opacity: 0.6; } }
.of-pcr-table { width: 100%; border-collapse: collapse; font-size: 11px; margin-top: 8px; }
.of-pcr-table th { text-align: left; padding: 4px 8px; color: #8b949e; font-size: 10px;
  text-transform: uppercase; border-bottom: 1px solid #21262d; }
.of-pcr-table td { padding: 4px 8px; color: #c9d1d9; border-bottom: 1px solid rgba(33,38,45,0.5); }

/* Ticker Analysis overlays */
.ta-overlay {
  position:absolute; z-index:10; background:rgba(13,17,23,0.92);
  border:1px solid #30363d; border-radius:6px; padding:8px 10px;
  font-size:11px; backdrop-filter:blur(8px); max-height:340px; overflow-y:auto;
}
.ta-overlay-left { left:8px; top:8px; min-width:210px; }
.ta-overlay-right { right:8px; top:8px; min-width:240px; }
.ta-overlay h4 { margin:0 0 6px; font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; }
.ta-row { display:flex; justify-content:space-between; padding:1px 0; }
.ta-row .lbl { color:#8b949e; }
.ta-row .val { font-weight:600; }
.ta-divider { border-top:1px solid #21262d; margin:4px 0; }
.ta-corr-tbl { width:100%; border-collapse:collapse; }
.ta-corr-tbl th { font-size:9px; color:#8b949e; text-transform:uppercase; padding:2px 4px; text-align:right; }
.ta-corr-tbl th:first-child { text-align:left; }
.ta-corr-tbl td { padding:1px 4px; text-align:right; font-size:11px; font-variant-numeric:tabular-nums; }
.ta-corr-tbl td:first-child { text-align:left; color:#8b949e; }
.ta-pivot-wrap { max-width:900px; margin:0; padding:0 0 16px; }
.ta-pivot-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.ta-pcard { background:#161b22; border:1px solid #21262d; border-radius:6px; padding:10px 14px; }
.ta-pcard h4 { margin:0 0 6px; font-size:12px; }
.ta-prow { display:flex; justify-content:space-between; font-size:11px; padding:2px 0; }
.ta-prow .lbl { color:#8b949e; }
/* 横向两指滑动不再触发浏览器前进/后退手势(本 PWA 用 tab 导航,从不需要 swipe-back)。
   → Risk/Heatmap/Digest 等宽表格横滑只滚动表格,不会"右滑后退"跳回主屏。 */
html, body { overscroll-behavior-x: none; }
[style*="overflow-x"], [style*="overflow:auto"], [style*="overflow-x:auto"], .dashboard-scroll, .dg-list { overscroll-behavior-x: contain; }
"""
    html = html.replace("</style>", live_css + "\n</style>")

    # PWA: inject manifest link and theme-color into <head>
    pwa_head = """
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#0d1117">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="apple-touch-icon" href="/static/icon-192.png">
    <!-- Browser-tab favicon: inline SVG with money emoji so it renders sharp
         at any size, no extra request, and works without a static image. -->
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20100%20100%22%3E%3Ctext%20y%3D%22.9em%22%20font-size%3D%2290%22%3E%F0%9F%92%B0%3C%2Ftext%3E%3C%2Fsvg%3E">
    <!-- Interface language (zt / ztt) for the page scripts below; zeus.js translates the rest -->
    <script src="/static/zeus_lang.js"></script>
    <!-- Timestamp formatter: must load before inline JS that calls _dlFmt.
         Single source of truth for "always-local-time" formatting. -->
    <script src="/static/dl_time_fmt.js"></script>
    <!-- 📈 大盘图(digest 子 tab)蜡烛引擎: 定义 window.MarketChart(IIFE, 不自动跑) -->
    <script src="/static/market_chart.js?v=5"></script>
    <!-- 这里原来引 /static/ta_filters.js 来驱动 heatmap 的 window.taFilterApi。
         9f8c6d3 按用户「反馈没用」删掉了 heatmap 工具栏的 #taFilterDD/#taPresetBar 两个
         容器, 但引入标签留下了 —— ta_filters.js 的 init() 找不到容器就
         setTimeout(init, 300) 无限重试, 实测在主页面上每秒空转一次, 永不停止。
         该脚本仍由独立的 /ta/ 页面(ta_screener/page.py)加载, 那边容器还在, 不受影响。
         (注: 此注释内不要出现字面的脚本标签 —— 会让"抽 script 块逐块 node --check"
          这套校验流程把注释当成代码块, 误报语法错。) -->
"""
    html = html.replace("</head>", pwa_head + "</head>")

    # PWA: inject service worker registration + auto-update check before </body>
    sw_script = """
<script>
// Clear all old caches on every page load to prevent stale HTML
if ('caches' in window) {
  caches.keys().then(keys => keys.forEach(k => caches.delete(k)));
}
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/static/sw.js').then(reg => {
    setInterval(() => reg.update(), 30000);
    reg.addEventListener('updatefound', () => {
      const nw = reg.installing;
      if (nw) nw.addEventListener('statechange', () => {
        if (nw.state === 'activated' && typeof softRefresh === 'function') softRefresh();
      });
    });
  }).catch(() => {});
}
</script>
"""
    html = html.replace("</body>", sw_script + '<link rel="stylesheet" href="/static/zeus.css"><script src="/static/zeus.js"></script>' + "</body>")

    # Add live bar HTML after <div class="main">
    live_bar_html = """
    <!-- Live status bar -->
    <div class="live-bar" id="liveBar">
      <span class="live-badge"><span class="live-dot"></span>LIVE</span>
      <span class="stock-price" id="livePrice"></span>
      <span class="last-update" id="lastUpdate">Connecting...</span>
    </div>
"""
    html = html.replace('<!-- Legs detail -->', live_bar_html + '    <!-- Legs detail -->')

    # Inject top-nav tab bar and wrap content in tab panels
    top_nav_html = """
<div class="top-nav" id="topNav">
  <div class="top-nav-brand" data-no-i18n title="ZEUS Terminal · 宙斯期权终端 — self-hosted options trading terminal · 本地部署的全方位期权交易终端">ZEUS <span style="opacity:.6;font-weight:600">Terminal</span></div>
  <!-- tabs scroll sideways when the window is too narrow, instead of wrapping (.top-nav-tabs above, zeus.js) -->
  <div class="top-nav-tabs" id="topNavTabs">
  <div class="top-nav-tab active" data-tab="positions" onclick="switchTopTab('positions')">Positions</div>
  <!-- --- heatmap --- -->
  <div class="top-nav-tab" data-tab="heatmap" onclick="switchTopTab('heatmap')">🎯 选股</div>
  <!-- --- /heatmap --- -->
  <!-- TA Screener 已并入 Heatmap(🔎 工具栏 TA 筛选 + 点格子开单票详情 overlay); 独立 TA tab 已删 -->
  <div class="top-nav-tab" data-tab="digest" onclick="switchTopTab('digest')">📋 Digest</div>
  <!-- 2026-07-30: 市场判断 tab 内容 re-parent 到 🎯 选股, 顶栏入口隐藏(tab-mbrief panel 保留作为源头) -->
  <div class="top-nav-tab" data-tab="mbrief" onclick="switchTopTab('mbrief')" style="display:none">🧭 市场判断</div>
  <div class="top-nav-tab" data-tab="events" onclick="switchTopTab('events')">📅 Events</div>
  <!-- Benzinga tab 已移除: Today/History→Digest(Pre-market), Market Events→Events -->
  <div class="top-nav-tab" data-tab="journal" onclick="switchTopTab('journal')">Trade Journal <span id="dl-pending-badge" style="display:none;background:#f85149;color:#fff;border-radius:10px;padding:1px 6px;font-size:10px;font-weight:700;margin-left:4px"></span></div>
  <div class="top-nav-tab" data-tab="paper" onclick="switchTopTab('paper')">🧪 模拟盘</div>
  <!-- Strategy Builder / Screener / Options 量稳定性 tab 已在 2026-07-30 轻量化重构里删除 -->
  </div>
  <div class="top-nav-right">
    <button id="powerPill" class="privacy-btn" onclick="togglePowerLive()" title="电源模式" style="font-size:11px;font-weight:700;display:none"></button>
    <button id="aiPauseBtn" class="privacy-btn" onclick="event.stopPropagation();toggleAiPause();return false;" title="暂停定时 AI(选股/叙事)" style="font-size:11px;font-weight:700">AI</button>
    <button id="muteBtn" class="privacy-btn" onclick="toggleMute()" title="静音所有通知(止盈/异动/风险/AI持仓)" style="font-size:15px">&#128276;</button>
    <button id="trStatusBtn" class="privacy-btn" onclick="trStatusToggle()" title="翻译源:当前用哪家 / 为什么别家不行 / 手动测试" style="font-size:13px">&#127760;<span id="trStatusCur" style="font-size:10px;margin-left:3px;color:#8b949e"></span></button>
    <button id="privacyBtn" class="privacy-btn" onclick="togglePrivacy()" title="Toggle Privacy Mode">&#128065;</button>
    <span class="live-badge"><span class="live-dot"></span>LIVE</span>
    <div id="trStatusPanel" style="display:none;position:fixed;top:48px;right:12px;z-index:2000;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px 12px;width:344px;max-height:80vh;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);font-size:12px"></div>
  </div>
</div>
<!-- ⚠ 全局仓位告警条:未挂 90% 止盈 GTC + 今日到期(由 loadMissingGtc 60s 填,默认隐藏)
     藏在 topNav 之下、tab panel 之上 → 无论切到哪个 tab 都能看见 -->
<div id="posAlertBar" onclick="openCheckup()" title="点开体检看详情" style="display:none;background:linear-gradient(90deg,#3d1215,#4a1a1e);border-bottom:2px solid #f85149;padding:8px 14px;color:#ffb4b4;font-size:12.5px;font-weight:600;cursor:pointer;line-height:1.55;box-shadow:0 2px 8px rgba(248,81,73,.2)"></div>
<div id="tab-positions" class="top-tab-panel active">
"""
    html = html.replace("<!-- __TAB_INJECT__ -->", top_nav_html)

    tab_end_html = """
</div>

<!-- --- history news --- -->
__HISTORY_NEWS_HTML__
<!-- --- /history news --- -->

<!-- --- heatmap --- -->
__HEATMAP_HTML__
<!-- --- /heatmap --- -->
<!-- 🧭 市场判断: 消息坏度 vs 价格已反映 + 大跌概率(带历史基准率) + 三层支撑 + 逐板块 -->
<div id="tab-mbrief" class="top-tab-panel" style="overflow-y:auto;padding:14px 20px">
  <div id="mbHeader" style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <strong style="color:#e6edf3;font-size:15px">🧭 市场判断</strong>
    <span style="color:#6e7681;font-size:11px">消息坏度 vs 价格已反映 · 不给方向标签, 给概率与证伪条件</span>
    <span style="flex:1"></span>
    <span id="mbMeta" style="color:#6e7681;font-size:11px"></span>
    <button id="mbRunBtn" onclick="mbRun(true)"
      style="background:#238636;color:#fff;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🔄 重新分析</button>
  </div>
  <div id="mbSubNav" style="display:flex;gap:2px;margin-bottom:12px">
    <button data-mb="brief" onclick="mbSub('brief')" style="background:#1f6feb;color:#fff;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🧭 市场判断</button>
    <button data-mb="rot" onclick="mbSub('rot')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🔄 行业轮动</button>
    <button data-mb="opt" onclick="mbSub('opt')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🎲 期权情绪</button>
  </div>
  <div id="mbBody"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
  <div id="mbRotPane" style="display:none">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <span style="color:#8b949e;font-size:11px">排序周期</span>
      <select id="igHorizon" onchange="igRender()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 8px;font-size:11px">
        <option value="m1" selected>短期(1个月)</option>
        <option value="m3">中期(3个月)</option>
        <option value="m6">长期(6个月)</option>
        <option value="y1">超长(1年)</option>
      </select>
      <label style="color:#8b949e;font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer">
        <input type="checkbox" id="igHideEtf" onchange="igRender()"> 隐藏 ETF 组</label>
      <label style="color:#8b949e;font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer"
        title="图里只画涨跌 ≥3% 的组 —— 贴近 0 的组画出来是看不见的细线, 表格仍显示全部">
        <input type="checkbox" id="igOnlyBig" onchange="igRender()"> 图只看显著(≥3%)</label>
      <span style="flex:1"></span>
      <span id="igMeta" style="color:#6e7681;font-size:11px"></span>
      <button onclick="igRefresh()" id="igRefreshBtn" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer">🔄 重拉数据</button>
    </div>
    <div id="igChart"></div>
    <div id="igBody"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
  </div>
  <div id="mbOptPane" style="display:none">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
      <span style="color:#8b949e;font-size:11px">排序</span>
      <select id="opSortKey" onchange="opRender()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 8px;font-size:12px">
        <option value="atm_iv_pctl" selected>IV 百分位(恐慌已定价多少)</option>
        <option value="skew25_pctl">25Δ 偏斜百分位(避险需求)</option>
        <option value="pc_vol_pctl">P/C 成交量百分位</option>
        <option value="pc_oi_pctl">P/C 持仓百分位</option>
      </select>
      <label style="color:#8b949e;font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer">
        <input type="checkbox" id="opHideLow" checked onchange="opRender()"> 隐藏低覆盖组
      </label>
      <span id="opMeta" style="color:#6e7681;font-size:11px"></span>
      <button onclick="opRefresh()" id="opRefreshBtn" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer">🔄 汇总新交易日</button>
    </div>
    <div style="color:#6e7681;font-size:11px;line-height:1.6;margin-bottom:10px;padding:8px 10px;background:#0d1117;border:1px solid #21262d;border-radius:6px">
      读 EOD 全链(20-60 DTE, 与你自己的持仓周期对齐)。<b style="color:#8b949e">只看百分位, 不看绝对值</b> ——
      有的票常年 IV 30%、有的常年 90%, 绝对值之间没法比; 「在自己历史里排第几」才可读。<br>
      <b style="color:#8b949e">IV 百分位 = 坏消息已经被定价了多少</b>:
      高位 = 已经贵了(这时追空常是接盘); 低位 = 真出事的话没有保护。
      <span style="color:#565f6a">偏斜 = 25Δ put IV 减 25Δ call IV, 正值越大说明下跌保护被抢得越凶。
      本页只陈述"贵不贵/偏不偏", 不给方向标签 —— 高 put 量可能是看空, 也可能是持股买保险或卖 put 收钱, 成交量本身分不出来。</span>
    </div>
    <div id="opBody"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
  </div>
</div>

<div id="tab-events" class="top-tab-panel" style="overflow-y:auto;padding:14px">
  <div id="evSubNav" style="display:flex;gap:2px;margin-bottom:12px">
    <button data-ev="cal" onclick="evNav('cal')" style="background:#1f6feb;color:#fff;border:1px solid #1f6feb;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">📅 财报日历</button>
    <!-- 异动期权 tab 用户不用,先注释掉(后端 15min 抓取未动,仅隐藏入口;要恢复取消注释即可)
    <button data-ev="me_option_activity" onclick="evNav('me_option_activity')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">⚡ 异动期权</button>
    -->
    <button data-ev="me_corp" onclick="evNav('me_corp')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">📋 企业事件</button>
    <button data-ev="sec" onclick="evNav('sec')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">📄 SEC 文件</button>
    <!-- 📡 风险雷达: 从 Positions 移来的独立 modal(openRiskRadar 已 re-parent 到 body, 任何 tab 可弹) -->
    <button onclick="openRiskRadar()" title="风险雷达:今日新闻风险 + 未来30天事件密度 + landmine 日历" style="background:transparent;color:#58a6ff;border:1px solid #30363d;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600;margin-left:auto">📡 风险雷达</button>
  </div>
  <div id="evCalPane">
    <div style="text-align:center;color:#8b949e;padding:60px">Loading events…</div>
  </div>
  __SEC_PANE_HTML__
  <!-- Market Events：运行时把 Benzinga 的 #bzMarketEvents re-parent 进来,5 类由 evNav 驱动 -->
  <div id="evMarketEvents" style="display:none;height:calc(100vh - 175px)"></div>
</div>
<div id="ev-tooltip"></div>
<div id="tab-digest" class="top-tab-panel" style="overflow-y:auto;padding:14px 20px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap;position:sticky;top:0;background:#0d1117;padding:6px 0;z-index:5">
    <span style="font-size:18px;font-weight:700;color:#e6edf3">📋 Digest</span><span style="font-size:11px;color:#6e7681;margin-left:-6px">Benzinga · Finviz · WSJ · MarketWatch</span>
    <button class="dg-mode active" data-mode="today" onclick="digestMode('today')">📡 今天</button>
    <button class="dg-mode" data-mode="cal" onclick="digestMode('cal')">📅 日历/历史</button>
    <button class="dg-mode" data-mode="premarket" onclick="digestMode('premarket')">🌅 Pre-market</button>
    <button class="dg-mode" data-mode="like" onclick="digestMode('like')">❤️ 喜欢</button>
    <button class="dg-mode" data-mode="chart" onclick="digestMode('chart')">📈 大盘图</button>
    <span id="digestMeta" style="font-size:12px;color:#8b949e;margin-left:8px"></span>
  </div>


  <!-- 本日筛选 · compact 单行: 源 chip(Benzinga / Finviz / WSJ / MarketWatch) + 必读/AI + 分类下拉 + 情绪 + ticker -->
  <div id="digestInDayBar" style="display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-bottom:10px;font-size:11px">
    <span id="digestSourceChips" style="display:flex;gap:3px;flex-wrap:wrap"></span>
    <span style="width:1px;height:16px;background:#30363d;margin:0 4px"></span>
    <button class="df-chip dg-mustread" onclick="digestMustRead()" title="只看必读(票池强 catalyst / 大宏观)">⭐必读</button>
    <button class="df-chip dg-fvai" onclick="digestFvai()" title="只看 finviz AI「为何异动」(读每日库; 配日历看历史)" style="color:#a371f7;border-color:#a371f766">✦AI</button>
    <select id="dgChanSel" onchange="digestChan(this.value)" title="按 benzinga 分类筛选" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px;cursor:pointer">
      <option value="">🗂 分类 ▾</option>
      <option value="Analyst Ratings">评级</option>
      <option value="Price Target">目标价</option>
      <option value="Earnings">财报</option>
      <option value="WIIM">WIIM</option>
      <option value="Movers">异动</option>
      <option value="Trading Ideas">交易点子</option>
      <option value="M&amp;A">并购</option>
      <option value="IPOs">IPO</option>
      <option value="Cryptocurrency">加密</option>
      <option value="Economics">宏观</option>
    </select>
    <span style="width:1px;height:16px;background:#30363d;margin:0 4px"></span>
    <button class="df-chip dg-sentf" data-sv="pos" onclick="digestSent('pos')" style="color:#3fb950;border-color:#2c5">▲多</button>
    <button class="df-chip dg-sentf" data-sv="neg" onclick="digestSent('neg')" style="color:#f85149;border-color:#a33">▼空</button>
    <button class="df-chip dg-sentf" data-sv="neu" onclick="digestSent('neu')">●中</button>
    <button class="df-chip dg-sentf" data-sv="any" onclick="digestSent('any')">有情绪</button>
    <span style="width:1px;height:16px;background:#30363d;margin:0 4px"></span>
    <input type="text" id="digestInTk" class="df-in" placeholder="🔍 ticker" style="width:80px">
    <button class="df-pill" onclick="digestInTicker()" title="按 ticker 筛选(Enter 也行)">筛</button>
    <button class="df-pill" onclick="digestInClear()" title="清空分类/情绪/ticker 筛选" style="color:#f85149;border-color:#f8514966">清除</button>
    <span id="digestInDayInfo" style="color:#9cc3ff;margin-left:auto;font-style:italic"></span>
  </div>

  <!-- 今天：常驻 WS → SSE 实时推送，目录列表+点开展开 -->
  <div id="digestPaneToday">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;font-size:12px">
      <span id="digestLiveDot" style="width:8px;height:8px;border-radius:50%;background:#3fb950;box-shadow:0 0 6px #3fb950"></span>
      <span id="digestTodayStatus" style="color:#8b949e">连接中…</span>
      <span style="margin-left:auto;display:flex;gap:5px;align-items:center;flex-wrap:wrap">
        <!-- 视图状态组 -->
        <button class="df-pill dg-wl-btn" onclick="digestToggleWatchlist()" title="只看清单内 ticker 的新闻(= heatmap 手维护 ~160 票 universe, 默认开; 今天/日历/历史 同一 toggle)">🔖 只看清单</button>
        <button class="df-pill dg-dn-btn" onclick="digestToggleDenoise()" title="默认过滤掉标题噪音 (3 Stocks to Buy / What's Going On / Options Swing / 花哨叙事), 与「只看清单」独立可组合">✨ 去噪</button>
        <button id="dgReadHideBtn" class="df-pill" onclick="digestMarkAllRead()" title="把当前可见的全部标为已读并隐藏(仅今天有效, 过夜自动重置; 新 push 进来的算未读, 仍显示)" style="background:#1a7f37;color:#fff;border-color:#2ea043">✓ 标已读</button>
        <button id="dgReadShowBtn" class="df-pill" onclick="digestShowAllRead()" title="显示全部(把已读的也显示出来)" style="display:none">👁 显示全部</button>
        <span style="width:1px;height:18px;background:#30363d;margin:0 3px"></span>
        <!-- 展开控制组 -->
        <button class="df-pill" onclick="digestExpandAll('digestTodayList',true)" title="全部展开卡片">⇊ 展开</button>
        <button class="df-pill" onclick="digestExpandAll('digestTodayList',false)" title="全部收起卡片">⇈ 收起</button>
        <span style="width:1px;height:18px;background:#30363d;margin:0 3px"></span>
        <!-- 主动作组:AI/Tickers/刷新 (「🚀开前30」2026-08-06 删, 双语源已移除) -->
        <button class="df-pill" onclick="digestAiSummary()" title="用最便宜的 Haiku 把当前显示的标题合并相似 + 中文速读" style="background:#6e40c9;color:#fff;border-color:#8957e5">🤖 AI总结</button>
        <button id="dgTkToggleBtn" class="df-pill" onclick="digestTkViewToggle()" title="按 ticker 新闻数排行,点 ticker 看该窗口全部新闻(可切 1/3/7/30 天)" style="background:#1f6feb;color:#fff;border-color:#1f6feb">📊 Tickers</button>
        <button id="dgPosToggleBtn" class="df-pill" onclick="digestPosViewToggle()" title="只看当前持仓 tickers 的新闻(持仓从 TastyTrade 实时拉, 持仓变了 news 也跟着变)" style="background:#238636;color:#fff;border-color:#2ea043">💼 持仓 news</button>
        <button class="df-pill" onclick="digestTodayLoad(true)" title="手动刷新">🔄</button>
      </span>
    </div>
    <div id="digestTodayList" class="dg-list"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>

    <!-- 💼 持仓 news 子视图 (嵌在"今天"里, 通过 dgPosToggleBtn 切换) -->
    <div id="digestPanePositions" style="display:none">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <span style="font-size:13px;color:#e6edf3;font-weight:600">💼 持仓 news</span>
        <span id="digestPosMeta" style="color:#6e7681;font-size:11px"></span>
        <span style="margin-left:auto;display:flex;gap:4px;align-items:center">
          <span style="color:#7c8590;font-size:11px;margin-right:4px">时间</span>
          <button class="dg-pos-day active" data-days="1" onclick="digestPosDays(1)">今天</button>
          <button class="dg-pos-day" data-days="3" onclick="digestPosDays(3)">3天</button>
          <button class="dg-pos-day" data-days="7" onclick="digestPosDays(7)">7天</button>
          <button class="dg-pos-day" data-days="30" onclick="digestPosDays(30)">30天</button>
        </span>
      </div>
      <div id="digestPosTickers" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px"></div>
      <div id="digestPosList" class="dg-list"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
    </div>

    <!-- 📊 Tickers 子视图 (嵌在"今天"里, 通过 dgTkToggleBtn 切换) -->
    <div id="digestPaneTickers" style="display:none">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <button id="digestTkBackBtn" onclick="digestTkBack()" class="df-pill" style="display:none;color:#58a6ff;border-color:#58a6ff">← 返回排行榜</button>
        <span id="digestTkTitle" style="font-size:13px;color:#e6edf3;font-weight:600">按 ticker 新闻数排行</span>
        <span style="margin-left:auto;display:flex;gap:4px;align-items:center">
          <span style="color:#7c8590;font-size:11px;margin-right:4px">时间</span>
          <button class="dg-tk-day active" data-days="1" onclick="digestTkDays(1)">今天</button>
          <button class="dg-tk-day" data-days="3" onclick="digestTkDays(3)">3天</button>
          <button class="dg-tk-day" data-days="7" onclick="digestTkDays(7)">7天</button>
          <button class="dg-tk-day" data-days="30" onclick="digestTkDays(30)">30天</button>
        </span>
      </div>
      <div id="digestTkStatus" style="color:#8b949e;font-size:12px;margin-bottom:8px"></div>
      <div id="digestTkContent"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
      <div id="digestTkList" class="dg-list" style="display:none"></div>
    </div>
  </div>

  <!-- 日历/历史：日历 + 筛选 + 单日/搜索结果（点开展开） -->
  <div id="digestPaneCal" style="display:none">
    <div style="display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:wrap">
      <button onclick="digestStep(-1)" class="back-btn" title="前一天">&#8249;</button>
      <input type="date" id="digestDate" onchange="digestDatePick(this.value)" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 8px;font-size:12px">
      <button onclick="digestStep(1)" class="back-btn" title="后一天">&#8250;</button>
      <button class="df-pill dg-wl-btn" onclick="digestToggleWatchlist()" title="只看清单内 ticker 的新闻(= heatmap 手维护 ~160 票 universe, 默认开; 今天/日历/历史 同一 toggle)">🔖 只看清单</button>
        <button class="df-pill dg-dn-btn" onclick="digestToggleDenoise()" title="默认过滤掉标题噪音 (3 Stocks to Buy / What's Going On / Options Swing / 花哨叙事), 与「只看清单」独立可组合">✨ 去噪</button>
      <button class="df-pill" onclick="digestExpandAll('digestContent',true)">全部展开</button>
      <button class="df-pill" onclick="digestExpandAll('digestContent',false)">全部收起</button>
      <button class="df-pill" onclick="digestAiSummary()" title="用最便宜的 Haiku 总结这一天/当前筛选(ticker)的新闻;后台跑,可关窗口" style="background:#6e40c9;color:#fff;border-color:#8957e5">🤖 AI总结</button>
      <button onclick="digestLoad((document.getElementById('digestDate')||{}).value, true)" class="back-btn" title="刷新">&#128260;</button>
    </div>
    <div id="digestFilter">
      <span style="color:#8b949e">🔎 筛选</span>
      <input type="date" id="dfStart" class="df-in" title="起始日期">
      <span style="color:#6e7681">→</span>
      <input type="date" id="dfEnd" class="df-in" title="结束日期">
      <button class="df-pill" onclick="digestPreset(7)">7天</button>
      <button class="df-pill" onclick="digestPreset(30)">1月</button>
      <button class="df-pill" onclick="digestPreset(90)">3月</button>
      <input type="text" id="dfTicker" class="df-in" placeholder="ticker" style="width:74px">
      <input type="text" id="dfTitle" class="df-in" placeholder="标题含…" style="width:110px">
      <input type="text" id="dfBody" class="df-in" placeholder="正文含…" style="width:110px">
      <input type="text" id="dfChannel" class="df-in" placeholder="分类含…" style="width:100px" title="官方分类: Analyst Ratings / Earnings / WIIM / Movers …">
      <button class="df-go" onclick="digestSearch()">搜索</button>
      <button class="df-pill" onclick="digestClearFilter()" title="清空筛选，回到按天浏览">清除</button>
    </div>
    <div id="digestCal" style="margin-bottom:14px"></div>
    <div id="digestContent" class="dg-list"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
  </div>
  <!-- Pre-market：运行时把 Benzinga 的 #bzPremarketGroup(Today/History) re-parent 进来 -->
  <div id="digestPanePremarket" style="display:none"></div>

  <!-- ❤️ 喜欢：收藏的新闻(服务端持久,跨已读/跨天),digestLikeLoad 渲染 -->
  <div id="digestPaneLike" style="display:none">
    <div style="display:flex;align-items:center;gap:10px;padding:8px 4px">
      <span id="digestLikeCount" style="font-size:12px;color:#8b949e"></span>
      <button onclick="digestClearLikes()" style="margin-left:auto;background:#21262d;color:#f85149;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer">🗑 清空</button>
    </div>
    <div id="digestLikeList" class="dg-list"><div style="color:#8b949e;padding:40px;text-align:center">加载中…</div></div>
  </div>

  <!-- 📈 大盘图：SPY/QQQ 全历史日线蜡烛(自绘 canvas),点蜡烛→透明弹窗看那天 AI 新闻总结 -->
  <div id="digestPaneChart" style="display:none">
    <div id="mcSymBar" style="display:flex;align-items:center;gap:6px;margin-bottom:8px;flex-wrap:wrap">
      <button class="df-chip mc-sym active" data-sym="SPY" onclick="MarketChart.setSymbol('SPY')" style="font-weight:700">SPY</button>
      <button class="df-chip mc-sym" data-sym="QQQ" onclick="MarketChart.setSymbol('QQQ')" style="font-weight:700">QQQ</button>
      <span style="width:1px;height:16px;background:#30363d;margin:0 4px"></span>
      <button id="mcSMABtn" class="df-chip active" onclick="MarketChart.toggleSMA()" title="均线 MA50/MA200">MA</button>
      <button id="mcVolBtn" class="df-chip active" onclick="MarketChart.toggleVol()" title="成交量">量</button>
      <button id="mcPivBtn" class="df-chip active" onclick="MarketChart.togglePivots()" title="关节点(大转折) — 点▲▼看是什么消息导致的" style="color:#a371f7">📍 关节点</button>
      <span style="width:1px;height:16px;background:#30363d;margin:0 4px"></span>
      <button class="df-pill" onclick="MarketChart.resetZoom()" title="回到最近 ~120 天">↺ 复位</button>
      <button class="df-pill" onclick="MarketChart.fitAll()" title="显示全部历史">⇔ 全览</button>
      <button class="df-pill" onclick="MarketChart.refresh()" title="重新拉取日线(到最新交易日)">🔄</button>
      <span style="color:#6e7681;font-size:11px;margin-left:auto">拖动平移 · 滚轮缩放 · 点蜡烛看当天新闻</span>
    </div>
    <div id="mcChartWrap" style="position:relative;width:100%;height:calc(100vh - 210px);min-height:420px;background:#0d1117;border:1px solid #21262d;border-radius:8px;overflow:hidden">
      <canvas id="mcChartCanvas" style="display:block"></canvas>
    </div>
  </div>


</div>

<div id="tab-benzinga" class="top-tab-panel" style="overflow-y:auto;padding:14px">
  <!-- Pre-market group(Benzinga Today/History): 运行时 re-parent 进 Digest #digestPanePremarket -->
  <div id="bzPremarketGroup">
  <div id="bzSubNav" style="display:flex;gap:2px;margin-bottom:10px;align-items:center;flex-wrap:wrap">
    <button class="bz-sub-pill active" data-bz="today" onclick="bzSubTab('today')" style="background:#1f6feb;color:#fff;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">📅 Today</button>
    <button class="bz-sub-pill" data-bz="history" onclick="bzSubTab('history')" style="background:#21262d;color:#8b949e;border:1px solid #30363d;padding:5px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🗂 History</button>
    <span style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <button onclick="bzRefresh()" id="bzRefreshBtn" style="background:#238636;color:#fff;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">🔄 Refresh</button>
      <span id="bzStatus" style="color:#8b949e;font-size:11px"></span>
    </span>
  </div>

  <div id="bzHistoryControls" style="display:none;flex-direction:column;gap:10px;margin-bottom:10px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <button onclick="bzCalNav(-1)" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px">‹</button>
      <span id="bzCalMonthLabel" style="color:#e6edf3;font-size:13px;font-weight:600;min-width:140px;text-align:center"></span>
      <button onclick="bzCalNav(1)" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px">›</button>
      <button onclick="bzCalNav(0)" style="background:#161b22;color:#58a6ff;border:1px solid #30363d;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:11px">Today</button>
      <span id="bzHistInfo" style="color:#8b949e;font-size:11px;margin-left:auto"></span>
    </div>
    <div id="bzCalendar" style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px"></div>
  </div>

  <div id="bzTranslateBar" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px;margin-bottom:12px">
    <div style="color:#8b949e;font-size:11px;margin-bottom:4px">Google Translate — 切换语言看中文版本（widget 加载后）:</div>
    <div id="google_translate_element"></div>
  </div>

  <div id="bzContent" style="color:#8b949e">Loading Benzinga snapshot…</div>
  </div><!-- /bzPremarketGroup -->
  <!-- Market Events (IPO/M&A/增发/拆股/异动期权): 运行时 re-parent 进 Events #evMarketEvents -->
  <div id="bzMarketEvents" style="display:none;height:calc(100vh - 175px)">
    __ME_PANE_HTML__
  </div>

</div>

<div id="tab-paper" class="top-tab-panel" style="overflow-y:auto">
  <div id="papersim-container"></div>
</div>

<div id="tab-journal" class="top-tab-panel">
  <!-- Outer sub-nav: 4 iframe-driven tabs + 1 Decision-Log tab -->
  <div id="tj-sub-nav" style="display:flex;gap:2px;padding:6px 16px;background:#161b22;border-bottom:1px solid #30363d;align-items:center;flex-wrap:wrap">
    <button class="tj-sub-pill active" data-tj="overview" onclick="tjSubTab('overview')">&#128202; Overview</button>
    <button class="tj-sub-pill" data-tj="calendar" onclick="tjSubTab('calendar')">&#128197; Calendar</button>
    <button class="tj-sub-pill" data-tj="alltrades" onclick="tjSubTab('alltrades')">&#128203; All Trades</button>
    <button class="tj-sub-pill" data-tj="analysis" onclick="tjSubTab('analysis')">&#128269; Analysis</button>
    <button class="tj-sub-pill" data-tj="ai" onclick="tjSubTab('ai')" style="color:#a371f7;font-weight:700">&#10022; AI 复盘</button>
    <span style="color:#30363d;margin:0 4px">|</span>
    <button class="tj-sub-pill" data-tj="decisions" onclick="tjSubTab('decisions')">&#128203; Decisions</button>
    <span style="margin-left:auto;display:flex;align-items:center;gap:6px">
      <button id="refreshJournalBtn" onclick="refreshJournal()" style="background:#1f6feb;color:#fff;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">Refresh Trades</button>
      <span class="status" id="journalStatus" style="color:#8b949e;font-size:11px"></span>
    </span>
  </div>

  <!-- View 1: original Trade Journal iframe (drives Overview / Trades Table / Calendar / Analysis) -->
  <iframe id="journalFrame" class="journal-frame" src="about:blank"
          style="display:block"></iframe>

  <!-- View 2: Decision Log toolbar (only shown for decisions / stats sub-tabs) -->
  <div id="dl-toolbar" style="display:none;padding:10px 20px;align-items:center;gap:12px;background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap">
    <button onclick="dlSync()" style="background:#1f6feb;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:700">&#128260; Sync from TastyTrade</button>
    <button onclick="dlTestNotification()" style="background:#21262d;color:#d29922;border:1px solid #d2992266;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">&#128276; Test Notification</button>
    <span id="dl-status" style="color:#8b949e;font-size:12px"></span>
    <div id="dl-decisions-only-filters" style="display:flex;align-items:center;gap:4px;margin-left:auto">
      <span style="color:#8b949e;font-size:11px">Status:</span>
      <button id="dl-st-pending" class="dl-pill" onclick="dlSetFilter('status','pending')">Pending</button>
      <button id="dl-st-completed" class="dl-pill" onclick="dlSetFilter('status','completed')">Completed</button>
      <button id="dl-st-all" class="dl-pill" onclick="dlSetFilter('status','all')">All</button>
      <span style="color:#8b949e;font-size:11px;margin-left:10px">Kind:</span>
      <button id="dl-k-all" class="dl-pill" onclick="dlSetFilter('kind','all')">All</button>
      <button id="dl-k-active" class="dl-pill" onclick="dlSetFilter('kind','active')">🟢 Active</button>
      <button id="dl-k-closed" class="dl-pill" onclick="dlSetFilter('kind','closed')">🔒 Closed</button>
      <input id="dl-search" placeholder="Search ticker..." oninput="dlRender()" style="margin-left:10px;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 8px;border-radius:4px;font-size:12px;text-transform:uppercase">
    </div>
  </div>

  <!-- View 2a: Decisions sub-tab (DL trade list) -->
  <div id="dl-list" style="display:none;flex:1;overflow-y:auto;padding:14px"></div>

  <!-- View 3: ✦ AI 复盘 (claude -p Haiku 分析开/平单原因 + 常见错误 + 改进建议) -->
  <div id="tj-ai-view" style="display:none;flex:1;overflow-y:auto;padding:18px 22px;background:#010409">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px">
      <button id="aiReviewBtn" onclick="aiReview()" style="background:#a371f7;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:700">&#10022; 让 AI 复盘我的交易</button>
      <span style="color:#8b949e;font-size:11px">用最便宜的 Haiku 分析你的开单/平单原因、常见错误、给改进建议(约 15 秒, 走订阅)</span>
      <span id="aiReviewStatus" style="color:#6e7681;font-size:11px"></span>
    </div>
    <div id="aiReviewOut" style="color:#c9d1d9;font-size:13px;line-height:1.7;max-width:920px"></div>
  </div>

  <!-- View 2b: Stats sub-tab (analytics dashboard) -->
  <div id="dl-stats-view" style="display:none;flex:1;overflow-y:auto;padding:14px">
    <div style="text-align:center;color:#8b949e;padding:60px">Loading stats…</div>
  </div>


  <!-- Full-screen overlay drawer (kept globally available — used from Decisions sub-tab) -->
  <div id="dl-overlay" onclick="if(event.target===this)dlCloseDrawer()"
       style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.75)">
    <div id="dl-drawer"
         style="position:absolute;inset:24px;background:#0d1117;border:1px solid #30363d;border-radius:8px;overflow:hidden;display:grid;grid-template-rows:auto 1fr">
    </div>
  </div>
</div>
<style>
.dl-metric-pill { background:transparent; color:#8b949e; border:none; border-bottom:2px solid transparent; padding:6px 12px; cursor:pointer; font-size:11px; font-weight:600; }
.dl-metric-pill:hover { color:#e6edf3; }
.dl-metric-pill.active { color:#58a6ff; border-bottom-color:#1f6feb; }
.tj-sub-pill { background:transparent; color:#8b949e; border:1px solid transparent; padding:6px 14px; border-radius:5px; cursor:pointer; font-size:12px; font-weight:600; }
.tj-sub-pill:hover { color:#e6edf3; background:#21262d; }

/* Events calendar grid: three months side by side on wide screens, two or one when the
   window is narrower, so a day is never too narrow to read its events */
.ev-cal-months { display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 560px), 1fr)); gap:14px; }
.ev-cal-month { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; }
.ev-cal-month-title { font-size:14px; font-weight:700; color:#e6edf3; margin-bottom:10px; text-align:center; letter-spacing:.3px; }
.ev-cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:3px; }
.ev-cal-dow { font-size:10px; font-weight:700; color:#8b949e; text-transform:uppercase; text-align:center; padding:4px 0; letter-spacing:.4px; }
.ev-cal-blank { background:transparent; min-height:80px; }
.ev-cal-day { background:#161b22; border:1px solid #21262d; border-radius:4px; padding:5px 6px; min-height:80px; display:flex; flex-direction:column; gap:2px; overflow:hidden; }
.ev-cal-day.ev-cal-weekend { background:#0e1218; }
.ev-cal-day.ev-cal-past { opacity:0.45; }
.ev-cal-day.ev-cal-today { border-color:#1f6feb; box-shadow:0 0 0 1px #1f6feb44 inset; }
.ev-cal-daynum { font-size:11px; font-weight:700; color:#8b949e; margin-bottom:2px; }
.ev-cal-today .ev-cal-daynum { color:#58a6ff; }
.ev-econ { font-size:9px; color:#3fb950; background:#0d3a1f44; border-left:2px solid #3fb950; padding:1px 4px; border-radius:2px; cursor:default; line-height:1.25; overflow:hidden; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
.ev-earn-row { display:flex; flex-wrap:wrap; gap:2px; margin-top:2px; }
.ev-earn { font-size:9px; font-weight:700; padding:1px 4px; border-radius:2px; cursor:default; line-height:1.2; }
.ev-earn.ev-bmo { background:#1f3d5a; color:#79c0ff; border:1px solid #1f6feb55; }
/* Floating tooltip — semi-transparent, follows the cursor */
#ev-tooltip { position:fixed; z-index:99999; pointer-events:none; background:rgba(13,17,23,0.92); border:1px solid #58a6ff; border-radius:5px; padding:8px 10px; font-size:11px; color:#e6edf3; max-width:280px; line-height:1.5; box-shadow:0 4px 12px rgba(0,0,0,0.5); display:none; }
#ev-tooltip .ev-tt-title { font-weight:700; color:#79c0ff; margin-bottom:3px; font-size:12px; }
#ev-tooltip .ev-tt-row { color:#8b949e; }
#ev-tooltip .ev-tt-row b { color:#e6edf3; font-weight:600; }
#ev-tooltip .ev-tt-up { color:#3fb950; font-weight:700; }
#ev-tooltip .ev-tt-down { color:#f85149; font-weight:700; }
.ev-earn.ev-amc { background:#3a2d0d; color:#d29922; border:1px solid #5a481555; }
.ev-earn.ev-other { background:#21262d; color:#8b949e; border:1px solid #30363d; }
.tj-sub-pill.active { background:#1f6feb22; color:#58a6ff; border-color:#1f6feb; }
.dl-pill { background:#161b22; color:#8b949e; border:1px solid #30363d; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:11px; font-weight:500; }
.dl-pill.active { background:#1f6feb; color:#fff; border-color:#1f6feb; font-weight:700; }
.dl-row { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px 14px; margin-bottom:8px; cursor:pointer; display:flex; align-items:center; gap:12px; }
.dl-row:hover { background:#1c2230; }
.dl-row.pending { border-left:3px solid #d29922; }
.dl-row.completed { border-left:3px solid #3fb950; opacity:0.75; }
.dl-chip { display:inline-flex; align-items:center; gap:3px; background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:14px; padding:3px 10px; font-size:11px; cursor:pointer; user-select:none; margin:2px; }
.dl-chip.active { background:#1f6feb; color:#fff; border-color:#1f6feb; }
.dl-chip .x { color:#f85149; margin-left:3px; cursor:pointer; opacity:0.7; }
.dl-chip .x:hover { opacity:1; }
.dl-tpl-bar { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:8px; }
.dl-tpl-btn { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding:4px 10px; font-size:11px; cursor:pointer; white-space:nowrap; transition:.15s; }
.dl-tpl-btn:hover { background:#30363d; border-color:#58a6ff; color:#58a6ff; }
.dl-section { background:#161b22; border:1px solid #30363d; border-radius:5px; padding:8px 10px; margin-bottom:6px; }
.dl-section-title { font-size:11px; font-weight:700; color:#e6edf3; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.3px; }
.dl-add-input { background:transparent; border:none; color:#58a6ff; font-size:11px; outline:none; padding:3px 6px; border-bottom:1px dashed #30363d; width:120px; }
.dl-slider-row { display:flex; align-items:center; gap:8px; margin:3px 0; font-size:11px; }
.dl-slider-row label { width:130px; color:#8b949e; }
.dl-slider-row input[type=range] { flex:1; accent-color:#1f6feb; }
.dl-slider-row .val { width:28px; text-align:right; font-weight:700; }
.dl-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.dl-readonly { background:#0d1117; border:1px solid #30363d; border-radius:5px; padding:8px 10px; font-size:11px; color:#8b949e; }
.dl-readonly b { color:#e6edf3; }
.dl-side .dl-section { padding:6px 8px; margin-bottom:4px; }
.dl-side .dl-section-title { font-size:10px; margin-bottom:3px; }
.dl-sub-pill { background:#161b22; color:#8b949e; border:1px solid #30363d; padding:5px 14px; border-radius:5px; cursor:pointer; font-size:12px; font-weight:600; }
.dl-sub-pill.active { background:#1f6feb22; color:#58a6ff; border-color:#1f6feb; }
.stats-grid { display:grid; gap:12px; }
.stats-card { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:14px; }
.stats-card-title { font-size:11px; font-weight:700; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px; }
.stats-kpi-tile { background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:10px 12px; }
.stats-kpi-label { font-size:10px; color:#8b949e; text-transform:uppercase; letter-spacing:0.4px; }
.stats-kpi-value { font-size:18px; font-weight:700; color:#e6edf3; margin-top:4px; }
.stats-kpi-sub { font-size:10px; color:#8b949e; margin-top:2px; }
.stats-bar-track { background:#0d1117; height:18px; border-radius:3px; overflow:hidden; position:relative; }
.stats-bar-fill { height:100%; transition:width .3s; }
.stats-table { width:100%; border-collapse:collapse; font-size:11px; }
.stats-table th { text-align:left; padding:6px 8px; color:#8b949e; font-weight:600; border-bottom:1px solid #30363d; font-size:10px; text-transform:uppercase; }
.stats-table td { padding:5px 8px; border-bottom:1px solid #21262d; }
.stats-table tr:hover { background:#1c2230; }
</style>

<!-- Earnings history modal (body level, visible from any tab) -->
<div class="modal-overlay" id="earningsModal" onclick="if(event.target===this)closeEarningsModal()">
  <div class="modal" style="max-width:680px;max-height:85vh;overflow-y:auto">
    <button class="modal-close" onclick="closeEarningsModal()">&times;</button>
    <div id="earningsModalContent"></div>
  </div>
</div>

<script>
let currentTopTab = 'positions';
let screenerLoaded = false;
let journalLoaded = false;
let scrData = null;  // cached screener JSON
let scrSortCol = 'signal_strength';
let scrSortAsc = false;
let scrFilterDir = 'ALL';
let scrFilterSigs = new Set();  // empty = show all
let scrSigDropOpen = false;
let scrEarnings = {};         // ticker -> {date, time, days}
let scrEarningsLoaded = false;
let scrEarnMaxDays = 28;      // default filter: 28 days
let scrEarnMode = 'gte';      // 'off', 'lte', 'gte' — default: ≥28 days (avoid earnings risk)
let scrSrcFilter = 'ALL';     // 'ALL', 'BC', 'FV', 'WR'
let scrMinStr = 0;            // minimum signal strength

// ── Soft Refresh (keep current tab, re-fetch data) ──
function softRefresh() {
  console.log('[SoftRefresh] Re-fetching data for current tab:', currentTopTab);
  // Positions: re-fetch strategies
  fetch('/api/data').then(r=>r.json()).then(d => {
    RAW_DATA.strategies = d.strategies;
    RAW_DATA.equities = d.equities;
    RAW_DATA.equity_prev_close = d.equity_prev_close;
    if (d.balance) RAW_DATA.balance = d.balance;
    const dash = document.getElementById('dashboardView');
    const risk = document.getElementById('riskView');
    if (risk && risk.style.display !== 'none' && typeof renderRiskView === 'function') {
      renderRiskView();
    } else if (dash && dash.style.display !== 'none') {
      renderDashView();
    } else {
      renderSidebar(RAW_DATA.strategies);
      renderStrategy();
    }
  }).catch(() => {});
  // Screener: re-fetch if loaded
  if (screenerLoaded) {
    fetch('/api/screener/data').then(r=>r.json()).then(d => {
      if (d.signals) { scrData = d; renderSignalsTable(); }
    }).catch(() => {});
  }
  // Market pulse
  fetchMarketPulse();
  // Journal: reload iframe
  const jf = document.getElementById('journalFrame');
  if (journalLoaded && jf) {
    jf.contentWindow.location.reload();
    jf.onload = () => {
      if (document.body.classList.contains('privacy-on') && jf.contentWindow) {
        jf.contentWindow.postMessage({type:'privacy', on: true}, '*');
      }
    };
  }
}

// ── Privacy Mode ──
function togglePrivacy() {
  const body = document.body;
  const btn = document.getElementById('privacyBtn');
  body.classList.toggle('privacy-on');
  btn.classList.toggle('active');
  const on = body.classList.contains('privacy-on');
  localStorage.setItem('privacyMode', on ? '1' : '0');
  // Notify journal iframe
  const jf = document.getElementById('journalFrame');
  if (jf && jf.contentWindow) jf.contentWindow.postMessage({type:'privacy', on}, '*');
}

// ─── 翻译源状态图标(顶栏 🌐):当前用哪家 / 别家为啥不行 / 手动测试 ───────────────
function _trEsc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
function trStatusToggle(){
  var p=document.getElementById('trStatusPanel'); if(!p) return;
  if(p.style.display==='block'){ p.style.display='none'; return; }
  p.style.display='block'; trStatusLoad();
}
function trStatusLoad(){
  var p=document.getElementById('trStatusPanel'); if(!p) return;
  p.innerHTML='<div style="color:#8b949e">加载中…</div>';
  fetch('/api/translate/status').then(function(r){return r.json();}).then(trStatusRender)
    .catch(function(){ p.innerHTML='<div style="color:#f85149">状态加载失败</div>'; });
}
function trStatusRender(d){
  var p=document.getElementById('trStatusPanel'); if(!p) return;
  var cur=document.getElementById('trStatusCur'); if(cur) cur.textContent=(d&&d.current)||'';
  var rows=((d&&d.providers)||[]).map(function(x){
    var dot,note;
    if(x.cooled){ dot='🟡'; note='冷却中 '+x.cooldown_left+'s'+(x.last_err?' · '+x.last_err:''); }
    else if(x.last_err){ dot='🔴'; note=x.last_err; }
    else if(x.last_ok){ dot='🟢'; note='正常'; }
    else { dot='⚪'; note=x.remote?'待用':'本地兜底·待命'; }
    return '<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid #161b22">'
      +'<span>'+dot+'</span>'
      +'<div style="flex:1;min-width:0"><div><b style="color:#e6edf3">'+_trEsc(x.label)+'</b> '
      +'<span style="color:#6e7681;font-size:10px">'+_trEsc(x.role)+'</span></div>'
      +'<div class="tr-note" style="color:#8b949e;font-size:11px;word-break:break-word">'+_trEsc(note)+'</div></div>'
      +'<button class="tr-test" data-prov="'+_trEsc(x.id)+'" style="background:transparent;color:#58a6ff;border:1px solid #30363d;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px;flex:none">测试</button></div>';
  }).join('');
  p.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
    +'<b style="color:#e6edf3">翻译源 · 自动轮转</b>'
    +'<button onclick="trStatusLoad()" style="background:transparent;border:none;color:#58a6ff;cursor:pointer;font-size:11px">↻ 刷新</button></div>'
    +rows
    +'<div style="color:#6e7681;font-size:10px;margin-top:6px;line-height:1.5">Google(translate-pa)/Bing/本地 argos 三家**每次调用轮换**当首选,分摊负载防限流;远程撞 429 自动冷却跳过,本地 argos 零封禁永远兜底,极端才暂留英文。UA 每请求轮换。已删 DeepL(keyless 持续 429)。点「测试」实测某家当下为啥不行。</div>';
}
function trStatusTest(prov,btn){
  btn.textContent='…'; btn.disabled=true;
  fetch('/api/translate/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:prov})})
    .then(function(r){return r.json();}).then(function(d){
      var note=btn.parentNode.querySelector('.tr-note');
      if(note){ note.innerHTML = d.ok
        ? '<span style="color:#3fb950">✅ '+_trEsc(d.text)+'</span> <span style="color:#6e7681">('+d.ms+'ms)</span>'
        : '<span style="color:#f85149">❌ '+_trEsc(d.error||'失败')+'</span> <span style="color:#6e7681">('+(d.ms||0)+'ms)</span>'; }
      btn.textContent='测试'; btn.disabled=false;
    }).catch(function(){ btn.textContent='测试'; btn.disabled=false; });
}
// 委托「测试」按钮点击(避开内联 onclick 带引号的转义坑)
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('.tr-test'):null;
  if(b) trStatusTest(b.getAttribute('data-prov'),b);
});
// 首屏拉一次,顶栏图标显示当前家
setTimeout(function(){ fetch('/api/translate/status').then(function(r){return r.json();}).then(function(d){
  var c=document.getElementById('trStatusCur'); if(c) c.textContent=(d&&d.current)||''; }).catch(function(){}); }, 1800);

// Restore on load
if (localStorage.getItem('privacyMode') === '1') {
  document.body.classList.add('privacy-on');
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('privacyBtn');
    if (btn) btn.classList.add('active');
  });
}

// ── Market Pulse ──
let mpInterval = null;
async function fetchMarketPulse() {
  try {
    const resp = await fetch('/api/market-pulse');
    const d = await resp.json();
    const el = document.getElementById('marketPulse');
    if (!el) return;
    let h = '';
    // Indices: SPY, QQQ, IWM, VIX
    for (const item of (d.indices || [])) {
      const chgPct = item.change_pct != null ? item.change_pct.toFixed(2) : '—';
      const cls = item.change_pct >= 0 ? 'pos' : 'neg';
      const sign = item.change_pct >= 0 ? '+' : '';
      h += '<div class="mp-item">';
      h += '<span class="mp-label">' + item.symbol + '</span>';
      h += '<span class="mp-price">' + (item.price != null ? item.price.toFixed(2) : '—') + '</span>';
      h += '<span class="mp-chg ' + cls + '">' + sign + chgPct + '%</span>';
      h += '</div>';
    }
    // CNN Fear & Greed
    if (d.fear_greed != null) {
      h += '<div class="mp-divider"></div>';
      const fg = d.fear_greed;
      let fgColor = '#d29922';
      if (fg.score <= 25) fgColor = '#f85149';
      else if (fg.score <= 40) fgColor = '#f0883e';
      else if (fg.score >= 75) fgColor = '#3fb950';
      else if (fg.score >= 60) fgColor = '#56d364';
      h += '<div class="mp-fear">';
      h += '<span class="mp-label">CNN F&G</span>';
      h += '<span class="mp-fear-val" style="color:' + fgColor + '">' + fg.score + '</span>';
      h += '<span class="mp-fear-label">' + (fg.label || '') + '</span>';
      h += '</div>';
    }
    el.innerHTML = h;
  } catch(e) {
    console.warn('Market pulse error:', e);
  }
}
// Start market pulse when screener tab opens
function startMarketPulse() {
  fetchMarketPulse();
  if (!mpInterval) mpInterval = setInterval(fetchMarketPulse, 60000);
}
function stopMarketPulse() {   // 离开 screener → 停 60s 轮询(否则切走后仍打 /api/screener/status)
  if (mpInterval) { clearInterval(mpInterval); mpInterval = null; }
}

// 打开某票单票详情(heatmap 格子/卡片 / 任意 [data-ta-tk] 深链 → 这里)。
// TA 已并入 Heatmap: 切到 heatmap 并在其内 overlay 打开 /ta/?ticker= 详情页。
function openTaTicker(sym) {
  if (!sym) return;
  sym = String(sym).toUpperCase().replace(/^\\$/, '').trim();   // 容错 $AAPL / 小写
  if (!sym) return;
  switchTopTab('heatmap');
  if (typeof window.hmOpenTickerDetail === 'function') window.hmOpenTickerDetail(sym);
}
window.openTaTicker = openTaTicker;

// 跳转到 Events → SEC 财报,并按 ticker 自动拉取(TA 单票页「📄 财报」按钮 / ?sec= 深链 → 这里)。
function openSecFiling(sym) {
  if (!sym) return;
  sym = String(sym).toUpperCase().replace(/^\\$/, '').trim();
  if (!sym) return;
  switchTopTab('events');
  if (typeof evNav === 'function') evNav('sec');
  const inp = document.getElementById('secTicker');
  if (inp) inp.value = sym;
  if (typeof secLoad === 'function') secLoad();
}
window.openSecFiling = openSecFiling;

// 全局 ticker 深链: 任何带 [data-ta-tk] 的元素(heatmap 各表/列表、Digest 新闻里的
// 标的)被点 → 打开 TA 单票详情页。capture 阶段拦截并 stopPropagation, 这样能盖过
// 所在行自己的 onclick(如 Digest 行展开 / 偏斜表设焦点), 点 ticker 永远进详情页,
// 点行内其它地方仍走原行为。一处注册, 覆盖全页(含 innerHTML 动态插入的内容)。
document.addEventListener('click', function (ev) {
  const el = ev.target && ev.target.closest ? ev.target.closest('[data-ta-tk]') : null;
  if (!el) return;
  const sym = el.getAttribute('data-ta-tk');
  if (!sym) return;
  ev.preventDefault();
  ev.stopPropagation();
  openTaTicker(sym);
}, true);

function switchTopTab(tab) {
  // Decision Log was merged into Trade Journal as a sub-tab; redirect any
  // stale `decisions` deeplink straight there.
  if (tab === 'decisions') {
    switchTopTab('journal');
    setTimeout(() => tjSubTab('decisions'), 50);
    return;
  }
  if (tab === currentTopTab) return;
  // --- strategy builder ---
  if (currentTopTab === 'builder' && window.SB && window.SB._onLeave) window.SB._onLeave();
  // --- /strategy builder ---
  // Leaving heatmap → close its live SSE stream + poll.
  if (currentTopTab === 'heatmap' && typeof hmTabBecameInactive === 'function') hmTabBecameInactive();
  // Leaving digest → close its today-stream SSE(否则切走后仍在轮询 /api/digest/today/stream)。
  if (currentTopTab === 'digest' && typeof digestSSEClose === 'function') digestSSEClose();
  // Leaving screener → stop the 60s market-pulse poll.
  if (currentTopTab === 'screener' && typeof stopMarketPulse === 'function') stopMarketPulse();
  // Leaving the dashboard panel — clear viz freeze so re-entry is fresh.
  if (typeof _setDashVizFrozen === 'function' && typeof dashVizFrozen !== 'undefined' && dashVizFrozen) {
    _setDashVizFrozen(false, true);
  }
  // History News has no standalone top-nav tab — it lives under "News". Keep that entry lit.
  const navTab = (tab === 'history-news') ? 'news' : tab;
  document.querySelectorAll('.top-nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === navTab));
  document.querySelectorAll('.top-tab-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('tab-' + tab);
  if (panel) panel.classList.add('active');
  currentTopTab = tab;
  // Returning to Positions: live updates were skipped while it was hidden, so
  // repaint once from the (still-patched) RAW_DATA to show fresh numbers now.
  if (tab === 'positions' && typeof renderPositionsLive === 'function') renderPositionsLive(null);
  if (tab === 'screener') { startMarketPulse(); if (!screenerLoaded) loadScreener(); }
  if (tab === 'journal' && !journalLoaded) loadJournal();
  if (tab === 'paper' && typeof PT !== 'undefined') PT.init();   // 🧪 模拟盘: reload trades + live marks
  // 回到 Events 恢复上次的子 tab。原来写死 evNav('cal') 无条件执行 —— 在 SEC 里
  // 拉了财报、开了双语翻译, 去 Positions 瞄一眼再回来就被打回财报日历, 白干。
  // (Journal 的 _tjSub 本来就是记住的, Events 这里不一致。)
  if (tab === 'events') { if (typeof evNav === 'function') evNav(_evSub || 'cal'); }
  if (tab === 'mbrief' && typeof mbInit === 'function') mbInit();            // 🧭 市场判断(读缓存, 不自动跑模型)
  // --- history news ---
  if (tab === 'history-news') hnTabBecameActive();
  // --- /history news ---
  // --- heatmap ---
  if (tab === 'heatmap' && typeof hmTabBecameActive === 'function') hmTabBecameActive();
  // --- /heatmap ---
  if (tab === 'digest') {
    digestInit();
    // 后台预拉 live_prices → _dgAiPriceOf 里 AI 总结才有非仓位 ticker 的价格
    // (2026-07-30 轻量化: 老 /api/screener/data 已删, 换成 heatmap live_prices 数据源)
    if (typeof scrData === 'undefined' || !scrData || !scrData.signals) {
      fetch('/api/live_prices').then(r => r.ok ? r.json() : null).then(d => {
        if (d && d.signals && typeof scrData !== 'undefined') scrData = d;
      }).catch(() => {});
    }
    // 双语 60s 自动拉(跟扩展的 alarm 同步):Digest tab 打开就每 60s reload 一次今日列表,
    // _digestFetchBilingual 内部有 60s cache,正好每次都能拉到新的 push 完的 /api/bilingual/data。
    if (_digestBlAutoTimer) clearInterval(_digestBlAutoTimer);
    _digestBlAutoTimer = setInterval(function () {
      if (currentTopTab !== 'digest') return;               // tab 切走了就等下轮
      if (_digestMode !== 'today') return;                  // 日历/搜索模式不动
      _digestBlPromise = null;                              // 清双语缓存, 强制拉最新
      if (typeof digestTodayLoad === 'function') digestTodayLoad(false);
    }, 60000);
  } else if (typeof _digestBlAutoTimer !== 'undefined' && _digestBlAutoTimer) {
    // 离开 Digest tab → 停 timer, 别浪费网络
    clearInterval(_digestBlAutoTimer);
    _digestBlAutoTimer = null;
  }
  // Shorts 已并入 Events 子 tab
}
var _digestBlAutoTimer = null;

// ── Benzinga Digest tab (英文原文 · 目录列表+点开展开 · 今天/日历两模式) ──
let _digestDates = [];
let _digestLoaded = false;
let _digestCounts = {};
let _digestCalMonth = null;  // 'YYYY-MM' shown in the calendar
let _digestMode = 'today';
let _digestCalInited = false;
let _digestTodayCount = -1;
let _digestTodayItems = [];
let _digestDayItems = [];
let _digestDayDate = '';
let _digestSearchMode = false;
// 当前显示的列表(供本日筛选/翻译用)
let _digestActive = { items: [], host: 'digestTodayList', date: '', showDate: false };
let _digestCalActive = null;   // 日历/历史侧最近一次的 active 快照(切回日历时恢复)

// ── 双语并入「今天/日历」的助手 ──
// Digest = Benzinga (Pro login) + free bilingual wire (Finviz / WSJ / MarketWatch);归档口径=文章 published date。
// 4 家来源 chip 存 localStorage,默认全开(null 表示全开,更省事)。
const _DIGEST_SOURCES = ['Benzinga', 'Finviz', 'WSJ', 'MarketWatch'];
let _digestSourceFilter = null;
function _digestLoadSourceFilter() {
  try {
    const raw = localStorage.getItem('digestSourceFilter');
    if (!raw) return null;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr) || !arr.length || arr.length >= _DIGEST_SOURCES.length) return null;
    return new Set(arr);
  } catch (e) { return null; }
}
function _digestSaveSourceFilter() {
  try {
    if (!_digestSourceFilter || _digestSourceFilter.size >= _DIGEST_SOURCES.length) localStorage.removeItem('digestSourceFilter');
    else localStorage.setItem('digestSourceFilter', JSON.stringify([..._digestSourceFilter]));
  } catch (e) {}
}
function _digestSourceCanon(s) {
  const k = String(s || '').toLowerCase().replace(/[^a-z]/g, '');
  if (k === 'wsj') return 'WSJ';
  if (k === 'finviz') return 'Finviz';
  if (k === 'barron' || k === 'barrons') return "Barron's";
  if (k === 'marketwatch') return 'MarketWatch';
  return s || 'Bilingual';
}
function _digestSha1Short(s) {
  // djb2 32-bit hash → 10-hex id;URL 不敏感碰撞率,做 id 足够
  let h = 5381; s = String(s);
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h.toString(16).padStart(8, '0').slice(0, 10);
}
// 统一 ts 到 epoch ms(前端排序用):
//   benzinga → ISO 字符串 "2026-07-06T13:55:39.605Z"
//   双语     → 秒 (10 位数)
//   若已是毫秒 (≥ 1e11) 原样返回
function _digestTsMs(v) {
  if (v == null) return 0;
  if (typeof v === 'string') { const n = Date.parse(v); return isNaN(n) ? 0 : n; }
  const n = Number(v); if (!n || isNaN(n)) return 0;
  return n < 1e11 ? n * 1000 : n;
}
function _digestNormalizeBl(a) {
  const src = _digestSourceCanon(a.source || a.section);
  const ts = _digestTsMs(a.ts);
  let hhmm = '';
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(new Date(ts));
    const hh = parts.find(p => p.type === 'hour')?.value || '';
    const mm = parts.find(p => p.type === 'minute')?.value || '';
    hhmm = hh + ':' + mm;
  } catch (e) {}
  return {
    id: 'bl-' + _digestSha1Short(a.url),
    ts: ts,
    hhmm: hhmm,
    title: a.title || '',
    title_cn: a.titleZh || '',
    body: a.teaser || '', body_cn: a.teaserZh || '',
    section: a.section || '',
    source: src,
    url: a.url,
    tickers: [],
    channels: [src],
    sentiment: null,
    _bilingual: true,
    _date: a.date || '',
  };
}
function _digestApplySourceFilter(items) {
  if (!_digestSourceFilter) return items;
  return items.filter(function(it) {
    const src = it && it._bilingual ? (it.channels && it.channels[0]) : 'Benzinga';
    return _digestSourceFilter.has(src);
  });
}
// 双语日历 counts:{date: {WSJ:n, "Barron's":n, MarketWatch:n}}
let _digestBlCountsByDate = null;
function _digestBuildBlCounts() {
  return _digestFetchBilingual().then(function(box) {
    const out = {};
    for (const a of box.articles) {
      if (!a || box.deleted[a.url]) continue;
      const localDate = _digestLocalDateOf(a.ts) || a.date;
      if (!localDate) continue;
      const src = _digestSourceCanon(a.source || a.section);
      if (!out[localDate]) out[localDate] = {};
      out[localDate][src] = (out[localDate][src] || 0) + 1;
    }
    _digestBlCountsByDate = out;
    return out;
  });
}
// 综合 benzinga + 双语 + 当前 source filter, 返回某天条数
function _digestDayCount(date) {
  const flt = _digestSourceFilter;
  const bnz = (_digestCounts && _digestCounts[date]) || 0;
  const bl = (_digestBlCountsByDate && _digestBlCountsByDate[date]) || {};
  if (!flt) {
    let s = bnz;
    for (const k in bl) s += bl[k];
    return s;
  }
  let s = 0;
  if (flt.has('Benzinga')) s += bnz;
  for (const k in bl) if (flt.has(k)) s += bl[k];
  return s;
}
// 拉双语全量 + 筛出某天;60s 缓存避免每次渲染都请求
let _digestBlPromise = null, _digestBlPromiseAt = 0;
function _digestFetchBilingual(force) {
  const now = Date.now();
  if (!force && _digestBlPromise && (now - _digestBlPromiseAt) < 60000) return _digestBlPromise;
  _digestBlPromiseAt = now;
  _digestBlPromise = fetch('/api/bilingual/data').then(function(r) { return r.json(); }).then(function(d) {
    return { articles: d.articles || [], deleted: d.deleted || {}, seen: d.seen || {} };
  }).catch(function() { return { articles: [], deleted: {}, seen: {} }; });
  return _digestBlPromise;
}
// 用本地时区从 ts 算 YYYY-MM-DD (扩展存的 a.date 是 UTC,晚间 CDT 20:00 后的稿子会被算到"明天",这里改回本地日历日)
function _digestLocalDateOf(ts) {
  const t = ts && ts > 1e11 ? ts : (ts || 0) * 1000;
  if (!t) return '';
  const d = new Date(t);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}
function _digestBlForDate(date, force) {
  return _digestFetchBilingual(force).then(function(box) {
    const out = [];
    for (const a of box.articles) {
      if (!a || !a.url || !a.title) continue;
      if (box.deleted[a.url]) continue;
      const localDate = _digestLocalDateOf(a.ts) || a.date;
      if (localDate !== date) continue;
      out.push(_digestNormalizeBl(a));
    }
    return out;
  });
}
// 「今天」视图专用:并入全部"未删"双语条目(= 双语浏览器里的未读收件箱),不按日期切。
// 因为扩展抓来的稿子发布时间跨天(晚间稿会被算到相邻日),严格按"今天"会漏掉作者
// 明明在浏览器里看到的未读稿。作者读完在浏览器/扩展里删 → 这里自动清空。
// 历史/日历仍按日期(用 _digestBlForDate)。
function _digestBlAllUndeleted(force) {
  return _digestFetchBilingual(force).then(function(box) {
    const out = [];
    const seen = box.seen || {};
    const cutoff = Date.now() - 3 * 86400000;   // 「今天」只并入近 3 天未读的免费源条目
    for (const a of box.articles) {
      if (!a || !a.url || !a.title) continue;
      if (box.deleted[a.url]) continue;
      if (seen[a.url]) continue;
      if (_digestTsMs(a.ts) < cutoff) continue;
      out.push(_digestNormalizeBl(a));
    }
    return out;
  });
}
// 双语三家(WSJ/Barron/MW)标已读 → 持久化进服务器 bilingual seen map,
// 扩展下次 /api/bilingual/sync 会把 merged.seen 拉回本地 → 「我的 html」也同步已读。
// Benzinga 不走这里(它不在扩展里),只用本地 _digestReadIds。
function _digestBlMarkSeen(urls) {
  const list = Array.isArray(urls) ? urls.filter(Boolean) : (urls ? [urls] : []);
  if (!list.length) return;
  fetch('/api/bilingual/state', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'markSeen', urls: list }),
  }).catch(function() {});
  _digestBlPromise = null;   // 清双语缓存,下次今天重载按新 seen 过滤(已读双语条目消失)
}
const _DIGEST_TRIO = ['Finviz', 'WSJ', 'MarketWatch'];
function _digestSetSourceChip(src) {
  if (!_digestSourceFilter) _digestSourceFilter = new Set(_DIGEST_SOURCES);
  if (_digestSourceFilter.has(src)) _digestSourceFilter.delete(src);
  else _digestSourceFilter.add(src);
  if (!_digestSourceFilter.size) _digestSourceFilter.add(src);  // 至少留一个,否则全空
  _digestSaveSourceFilter();
  _digestRenderSourceChips();
  digestRenderActive(false);
  // 日历若已加载,数字随 filter 实时变
  if (_digestCounts && Object.keys(_digestCounts).length) renderDigestCalendar();
}
// 「📰 三家」一键切:WSJ+Barron's+MarketWatch 三个双语源同时 on/off (Benzinga 状态不动)
function _digestToggleTrio() {
  if (!_digestSourceFilter) _digestSourceFilter = new Set(_DIGEST_SOURCES);
  const allOn = _DIGEST_TRIO.every(function(s) { return _digestSourceFilter.has(s); });
  if (allOn) _DIGEST_TRIO.forEach(function(s) { _digestSourceFilter.delete(s); });
  else _DIGEST_TRIO.forEach(function(s) { _digestSourceFilter.add(s); });
  if (!_digestSourceFilter.size) _DIGEST_TRIO.forEach(function(s) { _digestSourceFilter.add(s); });
  _digestBlPromise = null;   // 清双语 fetch 缓存,顺手拉一次最新扩展 push
  _digestSaveSourceFilter();
  _digestRenderSourceChips();
  if (_digestMode === 'today') digestTodayLoad(true);
  else digestRenderActive(false);
  if (_digestCounts && Object.keys(_digestCounts).length) renderDigestCalendar();
}
function _digestRenderSourceChips() {
  const box = document.getElementById('digestSourceChips'); if (!box) return;
  const active = _digestSourceFilter;
  const parts = [];
  _DIGEST_SOURCES.forEach(function(s) {
    const on = (!active || active.has(s));
    const style = on ? 'background:#1f6feb;color:#fff;border-color:#1f6feb' : 'background:transparent;color:#8b949e;border-color:#30363d';
    parts.push('<button class="df-chip dg-srcf" data-src="' + s.replace(/"/g, '&quot;') + '" style="' + style + '">' + s + '</button>');
  });
  box.innerHTML = parts.join('');
  box.querySelectorAll('.dg-srcf').forEach(function(b) {
    b.addEventListener('click', function() { _digestSetSourceChip(b.getAttribute('data-src')); });
  });
}
// 双语条目多选(内存状态,刷新丢失);benzinga 条目无 checkbox
let _digestSelectedUrls = new Set();
function _digestUpdateOpenBtn() {
  const btns = document.querySelectorAll('.dg-open-tabs-btn');
  const n = _digestSelectedUrls.size;
  btns.forEach(function(b) {
    if (n > 0) {
      b.textContent = '🚀 打开选中(' + n + ')';
      b.style.background = '#238636'; b.style.color = '#fff'; b.style.borderColor = '#2ea043';
    } else {
      b.textContent = '🚀 开前30';
      b.style.background = ''; b.style.color = ''; b.style.borderColor = '';
    }
  });
}
function digestToggleSelect(url, cb) {
  if (!url) return;
  if (cb.checked) _digestSelectedUrls.add(url);
  else _digestSelectedUrls.delete(url);
  _digestUpdateOpenBtn();
}
function digestOpenTabs() {
  const sel = _digestSelectedUrls;
  let urls, label;
  if (sel && sel.size) {
    urls = Array.from(sel);
    label = urls.length + ' 条选中';
  } else {
    urls = (window._digestLastRendered || []).slice(0, 30)
              .map(function(x) { return x && x.url; }).filter(Boolean);
    label = '前 ' + urls.length + ' 条';
  }
  if (!urls.length) { alert('当前列表没有可打开的原文'); return; }
  if (!confirm('将打开 ' + label + ' 原文 tab\\n(浏览器可能拦截弹窗,首次请在地址栏「允许」)')) return;
  urls.forEach(function(u) { try { window.open(u, '_blank', 'noopener'); } catch (e) {} });
  if (sel && sel.size) { sel.clear(); _digestUpdateOpenBtn();
    document.querySelectorAll('.dg-cb').forEach(function(cb) { cb.checked = false; });
  }
}
// 保留旧名字兼容(万一按钮 handler 有 legacy 引用)
function digestOpenTop30() { return digestOpenTabs(); }
let _digestWatchlist = new Set();  
let _digestWatchlistOn = true;      // 默认只看清单, 可切「显示全部」;今天/日历/历史 共用同一 toggle
function _digestUpdateWlBtn() {
  const btns = document.querySelectorAll('.dg-wl-btn'); if (!btns.length) return;
  const label = _digestWatchlistOn ? ('🔖 只看清单(' + _digestWatchlist.size + ')') : '🌐 显示全部';
  const bg = _digestWatchlistOn ? '#1f6feb' : '';
  const fg = _digestWatchlistOn ? '#fff' : '';
  btns.forEach(function(b) { b.textContent = label; b.style.background = bg; b.style.color = fg; });
}
function digestToggleWatchlist() {
  _digestWatchlistOn = !_digestWatchlistOn; _digestUpdateWlBtn(); digestRenderActive();
  // 作者主动切换视图 → 回到顶部看第一条(否则 renderDigestList 的中段锚点会停在中段, 第一条被 sticky 头挡住)
  const sc = document.getElementById('tab-digest'); if (sc) sc.scrollTop = 0;
}
// ✨ 去噪 chip: 默认开, localStorage 记忆; 独立于「只看清单」, 可组合
let _digestDenoiseOn = true;
try { const _sv = localStorage.getItem('dg_denoise'); if (_sv === '0') _digestDenoiseOn = false; } catch(e) {}
function _digestUpdateDnBtn() {
  const btns = document.querySelectorAll('.dg-dn-btn'); if (!btns.length) return;
  const label = _digestDenoiseOn ? '✨ 去噪' : '🗂 含噪音';
  const bg = _digestDenoiseOn ? '#8957e5' : '';
  const fg = _digestDenoiseOn ? '#fff' : '';
  btns.forEach(function(b) { b.textContent = label; b.style.background = bg; b.style.color = fg; b.style.borderColor = _digestDenoiseOn ? '#8957e5' : ''; });
}
function digestToggleDenoise() {
  _digestDenoiseOn = !_digestDenoiseOn;
  try { localStorage.setItem('dg_denoise', _digestDenoiseOn ? '1' : '0'); } catch(e) {}
  _digestUpdateDnBtn();
  digestRenderActive();
  const sc = document.getElementById('tab-digest'); if (sc) sc.scrollTop = 0;
}
function digestLoadWatchlist() {
  fetch('/api/digest/watchlist').then(function(r){ return r.json(); }).then(function(d){
    _digestWatchlist = new Set((d.tickers || []).map(function(t){ return String(t).toUpperCase(); }));
    _digestUpdateWlBtn();
    digestRenderActive();   // 今天/日历/历史 都可能是当前 host, 无需区分
  }).catch(function(){});
}
// === 今天 digest 的「标已读 / 显示全部」(仅今天有效, 过夜自动清空) ===
// 存 localStorage 的 digestRead_YYYY-MM-DD;启动+每次 mark 时清掉其他日期 key。
// 新 push 进来的 id 不在 set 里 → 仍然显示, 作者再点「标已读」累加。
let _digestReadIds = new Set();
let _digestReadDate = '';
let _digestHideRead = false;
// 跨天已读 carry (保留 7 天): 让"今天"列表能过滤"昨天你手动标已读过的",
// 从而昨天没看的第二天还能自动出现。localStorage key = 'digestReadCarry',
// 结构 [[id, 'YYYY-MM-DD'], ...]; 每次启动清 >7 天的。
let _digestReadCarry = new Map();
function _digestReadCutoffStr(daysBack) {
  const d = new Date(); d.setDate(d.getDate() - daysBack);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function _digestLoadReadCarry() {
  try {
    const raw = localStorage.getItem('digestReadCarry');
    if (!raw) return;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return;
    const cutoff = _digestReadCutoffStr(7);
    _digestReadCarry = new Map();
    arr.forEach(function(pair) {
      if (Array.isArray(pair) && pair.length === 2 && pair[1] >= cutoff) {
        _digestReadCarry.set(String(pair[0]), pair[1]);
      }
    });
    _digestSaveReadCarry();      // 顺便剪掉过期条目
  } catch (e) {}
}
function _digestSaveReadCarry() {
  try {
    localStorage.setItem('digestReadCarry', JSON.stringify(Array.from(_digestReadCarry.entries())));
  } catch (e) {}
}
function _digestYesterdayStr() { return _digestReadCutoffStr(1); }
function _digestTodayKey() {
  // 优先用服务器 today(active.date), 否则本地日期作兜底
  if (_digestActive && _digestActive.host === 'digestTodayList' && _digestActive.date) return _digestActive.date;
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function _digestLoadReadState() {
  try {
    const t = _digestTodayKey();
    // 清掉非今天的旧 key
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && k.indexOf('digestRead_') === 0 && k !== 'digestRead_' + t) localStorage.removeItem(k);
    }
    const raw = localStorage.getItem('digestRead_' + t);
    if (raw) {
      const obj = JSON.parse(raw);
      if (obj && obj.date === t && Array.isArray(obj.ids)) {
        _digestReadIds = new Set(obj.ids.map(String));
        _digestReadDate = t;
        _digestHideRead = !!obj.hideRead;
      }
    }
  } catch (e) {}
}
function _digestSaveReadState() {
  try {
    const t = _digestTodayKey();
    if (_digestReadDate !== t) { _digestReadIds = new Set(); _digestReadDate = t; }
    localStorage.setItem('digestRead_' + t, JSON.stringify({
      date: t, ids: Array.from(_digestReadIds), hideRead: _digestHideRead
    }));
  } catch (e) {}
}
function digestMarkAllRead() {
  const t = _digestTodayKey();
  if (_digestReadDate !== t) { _digestReadIds = new Set(); _digestReadDate = t; }
  // 「目前的」= 当前过滤后已渲染的列表(标题/分类/ticker 等本日筛选都尊重)
  const visible = window._digestLastRendered || [];
  const blUrls = [];
  visible.forEach(function(it) {
    if (it && it.id != null) {
      _digestReadIds.add(String(it.id));
      // 也塞进跨天 carry: 用条目本身的 _date (若有) 作为 mark 日期,
      // 让"今天+昨未读"合并时能识别昨天已读的条目
      _digestReadCarry.set(String(it.id), it._date || t);
    }
    if (it && it._bilingual && it.url) blUrls.push(it.url);   // 双语四家:额外持久化到服务器
  });
  _digestHideRead = true;
  _digestSaveReadState();
  _digestSaveReadCarry();
  _digestBlMarkSeen(blUrls);   // WSJ/Barron/MW 标已读同步进 bilingual state → 回传扩展
  digestRenderActive(true);   // 跳顶,否则全部隐藏后停在原位很迷
}
function digestShowAllRead() {
  _digestHideRead = false;
  _digestSaveReadState();
  digestRenderActive(true);   // 重新显示全部 → 回到第一条
}
function _digestUpdateReadBtns() {
  const t = _digestTodayKey();
  if (_digestReadDate && _digestReadDate !== t) { _digestReadIds = new Set(); _digestReadDate = t; _digestHideRead = false; }
  const cnt = _digestReadIds.size;
  const bH = document.getElementById('dgReadHideBtn');
  const bS = document.getElementById('dgReadShowBtn');
  // ✓ 标已读:始终可见(可累加);带读过的条数
  if (bH) bH.textContent = cnt ? ('✓ 标已读 ' + cnt) : '✓ 标已读';
  // 👁 显示全部:有读过的 + 当前在隐藏态时才显示
  if (bS) bS.style.display = (cnt && _digestHideRead) ? 'inline-flex' : 'none';
}
_digestLoadReadState();
let _digestInFilter = { channel: '', ticker: '', sentiment: '', mustread: false, fvai: false };

// 情绪徽章 {color,lbl} 或 null
function digestSentBadge(s) {
  const v = parseInt(s, 10);
  if (isNaN(v)) return null;
  if (v > 0) return { color: '#3fb950', lbl: '▲+' + v };
  if (v < 0) return { color: '#f85149', lbl: '▼' + v };
  return { color: '#8b949e', lbl: '●' };
}
// 把一个 digest 标的渲染成可点击元素 → 打开 TA 单票详情页(走全局 [data-ta-tk] 委托)。
// 保留原文(可能带 $)显示, data-ta-tk 存清洗后的纯代码。
function _dgTkEl(sym) {
  const raw = String(sym == null ? '' : sym).trim();
  const clean = raw.toUpperCase().replace(/^\\$/, '');
  const s = document.createElement('span');
  s.className = 'dg-tk-link';
  s.setAttribute('data-ta-tk', clean);
  s.style.cursor = 'pointer';
  s.style.color = '#58a6ff';
  s.title = '点击看 ' + clean + ' 单票详情';
  s.textContent = raw;
  return s;
}
// 用 DOM API 构建正文(避免 innerHTML 转义坑；点开才构建→不卡)
// 默认「对照」(一段英一段中)。openMode='en' 时只显英文(供"全部展开"避免批量翻译)。
function digestBuildBody(it, openMode) {
  // 免费源(Finviz / WSJ / MarketWatch):只存标题 + 摘要(中英对照),全文到出版方网站看
  if (it && it._bilingual) {
    const b = document.createElement('div'); b.className = 'dg-rowbody';
    const meta = document.createElement('div'); meta.className = 'dg-meta';
    meta.textContent = '来源:' + (it.source || 'Bilingual') + (it.section ? ' · ' + it.section : '');
    b.appendChild(meta);
    if (it.body) {
      const pe = document.createElement('div'); pe.className = 'dg-teaser'; pe.style.color = '#c9d1d9';
      pe.textContent = it.body; b.appendChild(pe);
    }
    if (it.body_cn) {
      const pz = document.createElement('div'); pz.className = 'dg-teaser dg-zh'; pz.style.color = '#9cc3ff';
      pz.textContent = it.body_cn; b.appendChild(pz);
    }
    const p = document.createElement('div'); p.className = 'dg-teaser'; p.style.color = '#6e7681';
    p.textContent = '全文在出版方网站打开(用你自己的订阅)。';
    b.appendChild(p);
    const btn = document.createElement('a');
    btn.href = it.url || '#'; btn.target = '_blank'; btn.rel = 'noopener';
    btn.textContent = '📖 阅读原文';
    btn.style.cssText = 'display:inline-block;padding:6px 12px;background:#1f6feb;color:#fff;border-radius:5px;text-decoration:none;margin-top:6px;font-size:12px';
    b.appendChild(btn);
    return b;
  }
  const b = document.createElement('div'); b.className = 'dg-rowbody';
  b._mode = openMode || 'both';
  function meta(prefix, txt) { const d = document.createElement('div'); d.className = 'dg-meta'; d.textContent = prefix + txt; b.appendChild(d); return d; }
  const bar = document.createElement('div'); bar.className = 'dg-trbar';
  const btn = document.createElement('button'); btn.className = 'dg-trbtn';
  btn.addEventListener('click', function(ev) { ev.stopPropagation(); digestBodyToggle(b, it, btn); });
  bar.appendChild(btn); b.appendChild(bar);
  if (it.channels && it.channels.length) {
    const d = document.createElement('div'); d.className = 'dg-meta'; d.appendChild(document.createTextNode('🗂 '));
    it.channels.slice(0, 12).forEach(function(c) { const s = document.createElement('span'); s.className = 'dg-chan'; s.textContent = c; d.appendChild(s); });
    b.appendChild(d);
  }
  if (it.tickers && it.tickers.length) {
    const d = document.createElement('div'); d.className = 'dg-meta'; d.appendChild(document.createTextNode('🏷 '));
    it.tickers.forEach(function(t, i) { if (i) d.appendChild(document.createTextNode(' · ')); d.appendChild(_dgTkEl(t)); });
    b.appendChild(d);
  }
  if (it.teaser) { const d = document.createElement('div'); d.className = 'dg-teaser'; d.textContent = it.teaser; b.appendChild(d); }
  if (it.quotes && it.quotes.length) meta('💹 ', it.quotes.join(' · '));
  if (it.links && it.links.length) {
    const d = document.createElement('div'); d.className = 'dg-meta'; d.appendChild(document.createTextNode('🔗 '));
    it.links.slice(0, 12).forEach(function(u, i) { const a = document.createElement('a'); a.href = u; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'link ' + (i + 1); d.appendChild(a); d.appendChild(document.createTextNode(' ')); });
    b.appendChild(d);
  }
  const paras = document.createElement('div'); paras.className = 'dg-paras'; b.appendChild(paras);
  b._paras = paras; b._btn = btn;
  (it.images || []).slice(0, 8).forEach(function(u) { const im = document.createElement('img'); im.className = 'dg-img'; im.loading = 'lazy'; im.src = u; b.appendChild(im); });
  digestRenderParas(b, it);   // 按 _mode 渲染(对照→自动取译文)
  return b;
}
// 按 b._mode 渲染段落：en=只英 / cn=只中 / both=一段英一段中。需中文且未译→拉取后再渲染。
// 表格 header / 短 term(P/E, 公司, ROE 之类) 原文里就是独立段落, 若连续多段每段 < 25 字,
// 把它们合并成一个「紧凑段」用 · 分隔, 避免翻译后每 term 独占 3 行(英+中+空)拉长列表。
const _DG_TIGHT_LEN = 25;
// Benzinga 原文偶尔嵌 CSS 声明 (table { width: 100%; ... }) 未剥净, 翻译后
// 「表格 { 宽度: 100%; 边框折叠: 折叠; }」这类对读者无意义 → 前端过滤。
function _dgIsCssLike(s) {
  const t = (s || '').trim();
  if (!t) return false;
  return /\\{[^}]*[a-zA-Z一-鿿-]+\\s*[:：]\\s*[^;}]+[;}]/.test(t);
}
function digestRenderParas(b, it) {
  const mode = b._mode, host = b._paras, en = it.body || [];
  if (b._btn) b._btn.textContent = (mode === 'en') ? '英文 ▾' : (mode === 'cn') ? '中文 ▾' : '对照 ▾';
  function _isShort(s) { return (s || '').length < _DG_TIGHT_LEN; }
  function _flushTight(buf) {
    if (!buf.length) return;
    if (buf.length === 1) {
      const p = buf[0];
      if (p.en) host.appendChild(_dgP(p.en, 'dg-p-en'));
      if (p.cn) host.appendChild(_dgP(p.cn, 'dg-p-cn'));
      return;
    }
    const enTxt = buf.map(function(p){ return p.en; }).filter(Boolean).join(' · ');
    const cnTxt = buf.map(function(p){ return p.cn; }).filter(Boolean).join(' · ');
    if (enTxt) host.appendChild(_dgP(enTxt, 'dg-p-en dg-p-tight'));
    if (cnTxt) host.appendChild(_dgP(cnTxt, 'dg-p-cn dg-p-tight'));
  }
  function draw() {
    host.innerHTML = '';
    const cn = it.body_cn || [];
    if (mode === 'en') {
      let buf = [];
      en.forEach(function(p) {
        if (_dgIsCssLike(p)) return;   // 过滤嵌入的 CSS 声明
        if (_isShort(p)) { buf.push({en: p, cn: null}); }
        else { _flushTight(buf); buf = []; host.appendChild(_dgP(p, 'dg-p-en')); }
      });
      _flushTight(buf);
      return;
    }
    if (mode === 'cn') {
      const src = cn.length ? cn : en;
      let buf = [];
      src.forEach(function(p) {
        if (_dgIsCssLike(p)) return;
        if (_isShort(p)) { buf.push({en: null, cn: p}); }
        else { _flushTight(buf); buf = []; host.appendChild(_dgP(p, 'dg-p-cn')); }
      });
      _flushTight(buf);
      return;
    }
    // both: 交错对照 + 短段合并 + CSS 过滤(任一是 CSS 视为该 pair 都跳过)
    const n = Math.max(en.length, cn.length);
    let buf = [];
    for (let i = 0; i < n; i++) {
      const e = en[i] || '', c = cn[i] || '';
      if (_dgIsCssLike(e) || _dgIsCssLike(c)) continue;
      if (_isShort(e) && _isShort(c)) {
        buf.push({en: e, cn: c});
      } else {
        _flushTight(buf); buf = [];
        if (e) host.appendChild(_dgP(e, 'dg-p-en'));
        if (c) host.appendChild(_dgP(c, 'dg-p-cn'));
      }
    }
    _flushTight(buf);
  }
  if ((mode === 'cn' || mode === 'both') && !(it.body_cn && it.body_cn.length) && en.length) {
    host.innerHTML = '<p style="color:#8b949e">翻译中…</p>';
    fetch('/api/digest/translate_article', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: it._date, id: it.id })
    }).then(function(r) { return r.json(); }).then(function(d) {
      it.body_cn = d.body_cn || [];
      if (d.title_cn) it.title_cn = d.title_cn;
      draw();
    }).catch(function() { host.innerHTML = '<p style="color:#f85149">翻译失败</p>'; });
  } else { draw(); }
}
function _dgP(text, cls) { const x = document.createElement('p'); x.className = cls; x.textContent = text; return x; }
// 切换：对照(both, 一段英一段中) ↔ 英文(en)。固定格式, 不要纯中文。
function digestBodyToggle(b, it, btn) {
  const order = ['both', 'en'];
  let i = order.indexOf(b._mode);
  if (i < 0) i = 0;
  b._mode = order[(i + 1) % 2];
  digestRenderParas(b, it);
}
// 构建单行表头(正文点开才建)
function _digestMakeRow(it, showDate) {
  const row = document.createElement('div'); row.className = 'dg-row'; row._it = it;
  const head = document.createElement('div'); head.className = 'dg-rowhead';
  // 双语条目加多选 checkbox(仅 UI, 不入库);点 checkbox 不展开卡片
  if (it && it._bilingual) {
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.className = 'dg-cb';
    cb.checked = _digestSelectedUrls.has(it.url);
    cb.style.cssText = 'margin-right:8px;cursor:pointer;flex-shrink:0;transform:scale(1.1)';
    cb.addEventListener('click', function(ev) { ev.stopPropagation(); digestToggleSelect(it.url, cb); });
    head.appendChild(cb);
  }
  const t = document.createElement('span'); t.className = 't';
  t.textContent = (showDate && it._date ? it._date + ' ' : '') + (it.hhmm || '');
  head.appendChild(t);
  const ttl = document.createElement('span'); ttl.className = 'ttl';
  const en = document.createElement('span'); en.className = 'ttl-en';
  // 双语条目:标题前加醒目彩色 source badge (跟 benzinga 视觉区分)
  if (it && it._bilingual) {
    const _blPal = { 'Finviz': ['#2f6fdf','Finviz'], 'WSJ': ['#8b4513','WSJ'], "Barron's": ['#6f42c1','Barron'], 'MarketWatch': ['#e36209','MW'] };
    const p = _blPal[it.source] || ['#1f6feb', (it.source || 'BL')];
    const sb = document.createElement('span');
    sb.style.cssText = 'background:' + p[0] + ';color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;margin-right:6px;letter-spacing:0.3px';
    sb.textContent = '📰 ' + p[1];
    en.appendChild(sb);
  }
  en.appendChild(document.createTextNode(it.title || ''));
  const bd = digestSentBadge(it.sentiment);
  if (bd) { const sb = document.createElement('span'); sb.className = 'dg-sent'; sb.style.color = bd.color; sb.style.borderColor = bd.color; sb.style.marginLeft = '6px'; sb.textContent = bd.lbl; en.appendChild(sb); }
  if (it.mr === 1 && it.mr_tag) { const mb = document.createElement('span'); mb.className = 'dg-mrbadge ' + (it.mr_b === 'B' ? 'b' : 'a'); mb.textContent = it.mr_tag; en.appendChild(mb); }
  ttl.appendChild(en);
  const cn = document.createElement('span'); cn.className = 'ttl-cn'; cn.textContent = it.title_cn || '';
  ttl.appendChild(cn); row._ttlCn = cn;
  head.appendChild(ttl);
  const tags = document.createElement('span'); tags.className = 'tags';
  (it.tickers || []).forEach(function(t, i) { if (i) tags.appendChild(document.createTextNode(' ')); tags.appendChild(_dgTkEl(t)); });
  if (it.channels && it.channels.length) tags.appendChild(document.createTextNode((it.tickers && it.tickers.length ? '  ·  ' : '') + it.channels.slice(0, 3).join(',')));
  head.appendChild(tags);
  // 双语条目:点标题(row 头)直接开原文 tab,不展开卡片(checkbox 已 stopPropagation 不受影响)
  if (it && it._bilingual) {
    head.addEventListener('click', function() { if (it.url) { window.open(it.url, '_blank', 'noopener'); _digestBlMarkSeen(it.url); } });
  } else {
    head.addEventListener('click', function() { digestToggleRow(row); });
  }
  // ↻ 重翻按钮:每行都有,常驻显示(SEC 双语一致)。点了 force 走 translate_article
  //   强制重翻标题(+ 已展开时的正文),覆盖缓存 —— 修 "Google 返英文原样、缓存卡死"
  //   那类假中文行(cache 里 't' 是英文,前端一直显英文)。双语条目无 id, 跳过。
  if (it && !it._bilingual && it.id != null) {
    const rb = document.createElement('span');
    rb.className = 'dg-reload';
    rb.setAttribute('title', '重翻这条 (跳过缓存)');
    rb.textContent = '↻';
    rb.addEventListener('click', function(ev) { ev.stopPropagation(); digestReloadRow(row, rb); });
    head.appendChild(rb);
  }
  // ❤️ 喜欢:有稳定 key 才显;点击 stopPropagation 不触发展开/开原文
  const _lk = _digestItemKey(it);
  if (_lk) {
    const hb = document.createElement('span');
    hb.className = 'dg-like';
    hb.setAttribute('data-key', _lk);
    hb.style.cssText = 'cursor:pointer;margin-left:8px;flex-shrink:0;font-size:14px;user-select:none';
    _digestSyncHeart(hb, _lk);
    hb.addEventListener('click', function(ev) { ev.stopPropagation(); digestToggleLike(it, hb); });
    head.appendChild(hb);
  }
  row.appendChild(head);
  return row;
}

// 手动重翻单条:调 translate_article {force:true} 覆盖缓存的标题+正文;
// 更新 row._it 的 title_cn/body_cn 并刷新界面(标题原地改,body 若展开则重渲)。
function digestReloadRow(row, btn) {
  const it = row && row._it; if (!it || it.id == null || !it._date) return;
  if (btn) { btn.classList.add('busy'); btn.textContent = '⟳'; }
  fetch('/api/digest/translate_article', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date: it._date, id: it.id, force: true })
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.title_cn) { it.title_cn = d.title_cn; if (row._ttlCn) row._ttlCn.textContent = d.title_cn; }
    else if (row._ttlCn) row._ttlCn.textContent = '';       // 翻不出来: 清空,别再显假中文
    if (d.body_cn) it.body_cn = d.body_cn;
    if (row.classList.contains('open')) {
      const bd = row.querySelector('.dg-rowbody');
      if (bd) digestRenderParas(bd, it);
    }
    if (btn) { btn.classList.remove('busy'); btn.textContent = '↻'; }
  }).catch(function() {
    if (btn) { btn.classList.remove('busy'); btn.textContent = '!'; setTimeout(function(){ btn.textContent = '↻'; }, 1500); }
  });
}
// 渲染目录列表：按 it.id 增量协调——复用已有行(保留已展开阅读的正文)，只增/删/重排，
// 绝不整页重渲染。这样后台 SSE 来新文章只插入，不会把你正在看的文章弄没。date 用于翻译缓存定位。
function renderDigestList(items, host, showDate, date) {
  if (!host) return;
  if (!items || !items.length) { host.innerHTML = '<div style="color:#8b949e;padding:30px;text-align:center">无内容</div>'; return; }
  const _sa = _digestScrollAnchor();                                 // 重渲前记滚动锚点
  const existing = {};
  host.querySelectorAll(':scope > .dg-row').forEach(function(r) { if (r._it && r._it.id != null) existing[r._it.id] = r; });
  if (!host.querySelector(':scope > .dg-row')) host.innerHTML = '';   // 清掉占位(加载中/无内容)
  const keep = {};
  let prev = null;
  items.forEach(function(it) {
    if (!it._date && date) it._date = date;
    let row = (it.id != null) ? existing[it.id] : null;
    if (row) {
      if (row._ttlCn && it.title_cn && row._ttlCn.textContent !== it.title_cn) row._ttlCn.textContent = it.title_cn;
      row._it = it;                                                  // 刷新数据引用，但保留已展开 body
    } else {
      row = _digestMakeRow(it, showDate);
    }
    if (it.id != null) keep[it.id] = true;
    const ref = prev ? prev.nextSibling : host.firstChild;
    if (row !== ref) host.insertBefore(row, ref);                   // 按新顺序放置(移动节点不丢状态)
    prev = row;
  });
  // 删除已不在列表里的旧行；正在展开阅读的行保留(避免读着读着消失)
  host.querySelectorAll(':scope > .dg-row').forEach(function(r) {
    if (r._it && r._it.id != null && !keep[r._it.id] && !r.classList.contains('open')) r.remove();
  });
  _digestScrollRestore(_sa);                                         // 重渲后视觉位置补回，不跳
}
function digestToggleRow(row, openMode) {
  if (row.classList.contains('open')) {
    row.classList.remove('open');
    const bd = row.querySelector('.dg-rowbody'); if (bd) bd.remove();
  } else {
    row.classList.add('open');
    row.appendChild(digestBuildBody(row._it, openMode));
  }
}
// 自动把某天所有标题谷歌翻译成中文(429→qwen)，回填到行标题
let _digestTitlesPending = '';
// 保持滚动锚点：DOM 变动(标题回填/增量重渲)会改变上方行高→视口乱跳。
// 锚定"正在展开阅读的行"(或视口顶部首个可见行)，变动后把滚动量补回，使其视觉不动。
function _digestScrollAnchor() {
  const sc = document.getElementById('tab-digest'); if (!sc) return null;
  let anchor = document.querySelector('#tab-digest .dg-row.open');
  if (!anchor) {
    const rows = document.querySelectorAll('#tab-digest .dg-row');
    const top = sc.getBoundingClientRect().top;
    for (let i = 0; i < rows.length; i++) { const r = rows[i].getBoundingClientRect(); if (r.bottom > top + 1) { anchor = rows[i]; break; } }
  }
  if (!anchor) return null;
  return { sc: sc, anchor: anchor, top: anchor.getBoundingClientRect().top };
}
function _digestScrollRestore(a) {
  if (!a || !a.anchor || !a.sc || !a.anchor.isConnected) return;
  const delta = a.anchor.getBoundingClientRect().top - a.top;
  if (delta) a.sc.scrollTop += delta;
}
function digestTranslateTitles(date, hostId) {
  if (!date || _digestTitlesPending === date) return;   // 同一天避免并发翻译
  _digestTitlesPending = date;
  const info = document.getElementById('digestInDayInfo');
  if (info && !info.textContent) info.textContent = '标题翻译中…';
  fetch('/api/digest/translate_titles', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date: date })
  }).then(function(r) { return r.json(); }).then(function(d) {
    _digestTitlesPending = '';
    const titles = d.titles || {};
    const _a = _digestScrollAnchor();                 // 翻译回填会改行高，先记锚点
    // 回填到当前所有列表(今天/日历)的行
    document.querySelectorAll('#tab-digest .dg-row').forEach(function(row) {
      const it = row._it; const cn = it && titles[it.id];
      if (cn && row._ttlCn) { row._ttlCn.textContent = cn; it.title_cn = cn; }
    });
    _digestScrollRestore(_a);                          // 视觉位置补回，不跳
    if (info && info.textContent === '标题翻译中…') info.textContent = '';
  }).catch(function() { _digestTitlesPending = ''; if (info && info.textContent === '标题翻译中…') info.textContent = ''; });
}
function digestExpandAll(hostId, open) {
  const host = document.getElementById(hostId); if (!host) return;
  host.querySelectorAll('.dg-row').forEach(function(row) {
    const isOpen = row.classList.contains('open');
    if (open !== isOpen) digestToggleRow(row, 'en');  // 批量展开只显英文,避免一次性触发大量翻译
  });
}

// ══════════ 📊 Tickers 视图: 排行榜 + 时间窗口 + 点进详情 ══════════
// 嵌在"今天"pane 里,通过 dgTkToggleBtn 切换 news 列表 ↔ Tickers 排行榜。
// state: days = 1/3/7/30, ticker = null(排行榜) | 'SPY'(详情), open = 是否显示中
var _digestTkView = { days: 1, ticker: null, loading: false, open: false };
// 从"今天"pane 点按钮切换 Tickers 视图 (news ↔ tickers 排行榜)
function digestTkViewToggle() {
  var pane = document.getElementById('digestPaneTickers');
  var todayList = document.getElementById('digestTodayList');
  var btn = document.getElementById('dgTkToggleBtn');
  if (!pane || !todayList) return;
  var toOpen = !_digestTkView.open;
  // 打开时收起持仓 news (两个子视图互斥)
  if (toOpen && typeof _digestPosResetIfOpen === 'function') _digestPosResetIfOpen();
  _digestTkView.open = toOpen;
  pane.style.display = toOpen ? '' : 'none';
  todayList.style.display = toOpen ? 'none' : '';
  if (btn) {
    btn.textContent = toOpen ? '📰 回新闻列表' : '📊 Tickers';
    btn.title = toOpen ? '返回今天新闻列表' : '按 ticker 新闻数排行,点 ticker 看该窗口全部新闻(可切 1/3/7/30 天)';
  }
  if (toOpen) {
    _digestTkView.ticker = null;
    var back = document.getElementById('digestTkBackBtn'); if (back) back.style.display = 'none';
    _digestTkSyncDayChips();
    digestTkLoad();
  }
}
// digestMode 切走"今天"时收回 tickers 视图
function _digestTkResetIfOpen() {
  if (!_digestTkView.open) return;
  _digestTkView.open = false;
  var pane = document.getElementById('digestPaneTickers');
  var todayList = document.getElementById('digestTodayList');
  var btn = document.getElementById('dgTkToggleBtn');
  if (pane) pane.style.display = 'none';
  if (todayList) todayList.style.display = '';
  if (btn) { btn.textContent = '📊 Tickers'; btn.title = '按 ticker 新闻数排行,点 ticker 看该窗口全部新闻(可切 1/3/7/30 天)'; }
}
function _digestTkSyncDayChips() {
  document.querySelectorAll('#digestPaneTickers .dg-tk-day').forEach(function(b){
    b.classList.toggle('active', parseInt(b.getAttribute('data-days'),10) === _digestTkView.days);
  });
}
function _digestTkSetTitle(text) {
  var el = document.getElementById('digestTkTitle'); if (el) el.textContent = text;
}
function _digestTkSetStatus(text) {
  var el = document.getElementById('digestTkStatus'); if (el) el.textContent = text || '';
}
function _digestTkShowGrid(show) {
  var grid = document.getElementById('digestTkContent');
  var list = document.getElementById('digestTkList');
  if (grid) grid.style.display = show ? '' : 'none';
  if (list) list.style.display = show ? 'none' : '';
}
// 时间切换: 排行榜 → 重新拉排行榜; 详情页 → 保持在当前 ticker, 拉新时间窗
function digestTkDays(n) {
  if (_digestTkView.days === n) return;
  _digestTkView.days = n;
  _digestTkSyncDayChips();
  if (_digestTkView.ticker) digestTkOpen(_digestTkView.ticker);
  else digestTkLoad();
}
// 排行榜
function digestTkLoad() {
  if (_digestTkView.loading) return;
  _digestTkView.loading = true;
  _digestTkSetTitle('按 ticker 新闻数排行 · 最近 ' + _digestTkView.days + ' 天');
  _digestTkSetStatus('加载中…');
  var host = document.getElementById('digestTkContent');
  if (host) host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>';
  _digestTkShowGrid(true);
  // 并发拉 ticker 排行 + heatmap 实时价格 (DXLink 快照, 700 票 universe,
  // 剩下 4000+ tickers 无价格显 "-")
  Promise.all([
    fetch('/api/digest/tickers?days=' + _digestTkView.days).then(function(r){ return r.json(); }),
    fetch('/api/live_prices').then(function(r){ return r.ok ? r.json() : { signals: [] }; }).catch(function(){ return { signals: [] }; })
  ]).then(function(pair) {
    _digestTkView.loading = false;
    var d = pair[0], px = pair[1] || { signals: [] };
    var items = (d && d.items) || [];
    // 价格表: ticker(upper, 去 $) → {price, change_pct}
    var pxMap = {};
    (px.signals || []).forEach(function(s) {
      var k = String(s.ticker || '').toUpperCase().replace(/^\\$/, '');
      if (k) pxMap[k] = { p: s.price, c: s.change_pct };
    });
    var haveN = 0; items.forEach(function(it) { if (pxMap[String(it.ticker).toUpperCase().replace(/^\\$/, '')]) haveN++; });
    _digestTkSetStatus(items.length + ' 个 ticker (' + haveN + ' 带价格) · 时间窗 ' + (d.days || _digestTkView.days) + ' 天');
    if (!items.length) {
      if (host) host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">该时间窗内无新闻</div>';
      return;
    }
    // 用 data-tk 属性 + 事件委托 (避免 JS-in-Python 里 \\' 被吞掉的坑,
    var frag = '<div class="dg-tk-grid">';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var sym = (it.ticker || '').replace(/["<>&']/g,'');
      var latest = (it.latest || '').replace(/["<>&']/g,'');
      var px_i = pxMap[sym.toUpperCase().replace(/^\\$/, '')];
      var pxLine = '';
      if (px_i && px_i.p > 0) {
        var pStr = px_i.p >= 100 ? px_i.p.toFixed(2) : px_i.p.toFixed(2);
        var cPct = (px_i.c == null) ? null : Number(px_i.c);
        var cColor = (cPct == null) ? '#6e7681' : (cPct > 0 ? '#3fb950' : (cPct < 0 ? '#f85149' : '#8b949e'));
        var cStr = (cPct == null) ? '' : ((cPct > 0 ? '+' : '') + cPct.toFixed(2) + '%');
        pxLine = '<div class="dg-tk-px">$' + pStr
              + (cStr ? (' <span style="color:' + cColor + '">' + cStr + '</span>') : '')
              + '</div>';
      }
      frag += '<div class="dg-tk-card" data-tk="' + sym + '" title="点击看 ' + sym + ' 的全部新闻">'
        +   '<div style="min-width:0;flex:1"><div class="dg-tk-sym">' + sym + '</div>'
        +   pxLine
        +   (latest ? '<div class="dg-tk-latest">最近 ' + latest + '</div>' : '')
        +   '</div>'
        +   '<span class="dg-tk-cnt">' + it.count + '</span>'
        + '</div>';
    }
    frag += '</div>';
    if (host) {
      host.innerHTML = frag;
      // 委托: 一次绑定, cover 所有 card
      host.onclick = function(ev) {
        var c = ev.target.closest ? ev.target.closest('.dg-tk-card') : null;
        if (c) { var tk = c.getAttribute('data-tk'); if (tk) digestTkOpen(tk); }
      };
    }
  }).catch(function(e){
    _digestTkView.loading = false;
    _digestTkSetStatus('');
    if (host) host.innerHTML = '<div style="color:#f85149;padding:40px;text-align:center">加载失败: ' + (e && e.message || e) + '</div>';
  });
}
// 打开某 ticker 详情, 复用 renderDigestList (自动带 ↻/❤️/展开双语)
function digestTkOpen(ticker) {
  var tk = (ticker || '').toUpperCase();
  if (!tk) return;
  _digestTkView.ticker = tk;
  var back = document.getElementById('digestTkBackBtn'); if (back) back.style.display = '';
  _digestTkSetTitle('📌 ' + tk + ' · 最近 ' + _digestTkView.days + ' 天新闻');
  _digestTkSetStatus('加载中…');
  _digestTkShowGrid(false);
  var list = document.getElementById('digestTkList');
  if (list) list.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>';
  fetch('/api/digest/by_ticker?ticker=' + encodeURIComponent(tk) + '&days=' + _digestTkView.days)
    .then(function(r){ return r.json(); }).then(function(d){
      var items = (d && d.items) || [];
      _digestTkSetStatus(items.length + ' 条 · ' + tk + ' · 时间窗 ' + (d.days || _digestTkView.days) + ' 天');
      if (!items.length) {
        if (list) list.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">该时间窗内 ' + tk + ' 无新闻</div>';
        return;
      }
      // renderDigestList 需要 items 有 id 且惟一; benzinga id 全局惟一, 跨天不冲突。
      // showDate=true 让每行头部显示日期(跨天视图必要)。
      renderDigestList(items, list, true, null);
    }).catch(function(e){
      _digestTkSetStatus('');
      if (list) list.innerHTML = '<div style="color:#f85149;padding:40px;text-align:center">加载失败: ' + (e && e.message || e) + '</div>';
    });
}
// 返回排行榜
function digestTkBack() {
  _digestTkView.ticker = null;
  var back = document.getElementById('digestTkBackBtn'); if (back) back.style.display = 'none';
  digestTkLoad();
}

// ══════════ 💼 持仓 news 视图 ══════════
// 从 TastyTrade 实时拉持仓 tickers → 过滤最近 N 天所有 news 合并显示。
// 持仓变了 news 也跟着变 (每次点开/切时间都实时重拉持仓)。
// filter=null → 全部持仓票; filter='SPCX' → 只看 SPCX (chip 点击切换)
var _digestPosView = { days: 1, loading: false, open: false,
                       items: [], tickers: [], counts: {}, filter: null };
function _digestPosSyncDayChips() {
  document.querySelectorAll('#digestPanePositions .dg-pos-day').forEach(function(b){
    b.classList.toggle('active', parseInt(b.getAttribute('data-days'),10) === _digestPosView.days);
  });
}
function digestPosViewToggle() {
  var pane = document.getElementById('digestPanePositions');
  var todayList = document.getElementById('digestTodayList');
  var tkPane = document.getElementById('digestPaneTickers');
  var btn = document.getElementById('dgPosToggleBtn');
  if (!pane || !todayList) return;
  var toOpen = !_digestPosView.open;
  // 打开时把 Tickers 视图收起 (两个子视图互斥)
  if (toOpen && _digestTkView && _digestTkView.open) _digestTkResetIfOpen();
  _digestPosView.open = toOpen;
  pane.style.display = toOpen ? '' : 'none';
  todayList.style.display = toOpen ? 'none' : '';
  if (btn) {
    btn.textContent = toOpen ? '📰 回新闻列表' : '💼 持仓 news';
    btn.title = toOpen ? '返回今天新闻列表' : '只看当前持仓 tickers 的新闻 (持仓从 TastyTrade 实时拉, 变了立即反映)';
    btn.style.background = toOpen ? '#238636' : '#238636';
  }
  if (toOpen) {
    _digestPosSyncDayChips();
    digestPosLoad();
  }
}
function _digestPosResetIfOpen() {
  if (!_digestPosView.open) return;
  _digestPosView.open = false;
  var pane = document.getElementById('digestPanePositions');
  var todayList = document.getElementById('digestTodayList');
  var btn = document.getElementById('dgPosToggleBtn');
  if (pane) pane.style.display = 'none';
  if (todayList) todayList.style.display = '';
  if (btn) { btn.textContent = '💼 持仓 news'; btn.title = '只看当前持仓 tickers 的新闻 (持仓从 TastyTrade 实时拉, 变了立即反映)'; }
}
function digestPosDays(n) {
  if (_digestPosView.days === n) return;
  _digestPosView.days = n;
  _digestPosSyncDayChips();
  digestPosLoad();
}
function digestPosLoad() {
  if (_digestPosView.loading) return;
  _digestPosView.loading = true;
  _digestPosView.filter = null;      // 换时间窗/重拉时清 filter
  var meta = document.getElementById('digestPosMeta');
  var chips = document.getElementById('digestPosTickers');
  var list = document.getElementById('digestPosList');
  if (meta) meta.textContent = '加载中…';
  if (chips) chips.innerHTML = '';
  if (list) list.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>';
  fetch('/api/digest/positions_news?days=' + _digestPosView.days + '&_t=' + Date.now())
    .then(function(r){ return r.json(); }).then(function(d){
      _digestPosView.loading = false;
      if (d.error) {
        if (meta) meta.textContent = '拉持仓失败: ' + d.error;
        if (list) list.innerHTML = '<div style="color:#f85149;padding:40px;text-align:center">' + (d.error || '拉持仓失败') + '</div>';
        return;
      }
      _digestPosView.tickers = d.tickers || [];
      _digestPosView.counts = d.counts || {};
      _digestPosView.items = d.items || [];
      _digestPosRender();
    }).catch(function(e){
      _digestPosView.loading = false;
      if (meta) meta.textContent = '';
      if (list) list.innerHTML = '<div style="color:#f85149;padding:40px;text-align:center">加载失败: ' + (e && e.message || e) + '</div>';
    });
}
// filter chip 切换: 复用现有 items 数据 (不重拉), 只改前端过滤 + 重渲染
function digestPosFilter(tk) {
  var v = tk || '';
  _digestPosView.filter = (_digestPosView.filter === v && v) ? null : (v || null);
  _digestPosRender();
}
function _digestPosRender() {
  var meta = document.getElementById('digestPosMeta');
  var chips = document.getElementById('digestPosTickers');
  var list = document.getElementById('digestPosList');
  var flt = _digestPosView.filter;
  var all = _digestPosView.items;
  var tickers = _digestPosView.tickers;
  var counts = _digestPosView.counts;
  // filter items
  var items = flt
    ? all.filter(function(it) {
        return (it.tickers || []).some(function(t) {
          return String(t).toUpperCase().replace(/^\\$/, '') === flt;
        });
      })
    : all;
  if (meta) meta.textContent = tickers.length + ' 持仓票 · '
    + items.length + (flt ? ('/' + all.length) : '') + ' 条'
    + (flt ? (' · 只看 ' + flt) : '')
    + ' · 时间窗 ' + _digestPosView.days + ' 天';
  // chips: 「全部」+ 每 ticker (点击 filter, 委托绑定)
  if (chips) {
    var frag = '<span class="dg-pos-tk dg-pos-all' + (flt ? '' : ' active') + '" data-tk="">全部 <span class="n">' + all.length + '</span></span>';
    for (var i = 0; i < tickers.length; i++) {
      var tk = tickers[i];
      var n = counts[tk] || 0;
      var active = flt === tk;
      frag += '<span class="dg-pos-tk' + (active ? ' active' : '') + '" data-tk="' + tk + '">' + tk + ' <span class="n">' + n + '</span></span>';
    }
    chips.innerHTML = frag;
    // 事件委托 (chip 内含 .n span, 用 closest 定位)
    chips.onclick = function(ev) {
      var c = ev.target.closest ? ev.target.closest('.dg-pos-tk') : null;
      if (c) digestPosFilter(c.getAttribute('data-tk') || '');
    };
  }
  if (!items.length) {
    if (list) list.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
      + (tickers.length ? (flt ? ('该时间窗内 ' + flt + ' 无新闻') : '该时间窗内你持仓的 ticker 无新闻')
                        : '暂无持仓 (等 TastyTrade client 连上)')
      + '</div>';
    return;
  }
  renderDigestList(items, list, true, null);
}
// ✦finviz AI:「今天/日历」筛选条里的一个开关(不是独立 tab)。开 → 当前列表只显示该 active
// 日期的 finviz AI「为何异动」(读 finviz_ai 每日库, 已双语)。复用现有 ticker 输入做同日搜索、
// 日历选历史日。digestRenderActive 里按 active 日期惰性载入对应库日。
let _digestFvaiItems = null, _digestFvaiDate = null, _digestFvaiLoading = false;
function _digestSetFvaiChip() {
  const b = document.querySelector('#digestInDayBar .dg-fvai');
  if (b) b.classList.toggle('active', !!_digestInFilter.fvai);
}
function digestFvai() {
  _digestInFilter.fvai = !_digestInFilter.fvai;
  // 分类/必读 会把 finviz AI(channels=[✦AI]、无 mr)过滤空 → 开 finviz AI 时清掉这俩
  if (_digestInFilter.fvai) { _digestInFilter.channel = ''; _digestInFilter.mustread = false; }
  digestRenderActive(true);
}
function digestMode(m) {
  reorgTabs();
  _digestMode = m;
  document.querySelectorAll('#tab-digest .dg-mode').forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-mode') === m); });
  const pt = document.getElementById('digestPaneToday'), pc = document.getElementById('digestPaneCal');
  const pm = document.getElementById('digestPanePremarket');
  const pl = document.getElementById('digestPaneLike');
  const pch = document.getElementById('digestPaneChart');
  if (pt) pt.style.display = (m === 'today') ? '' : 'none';
  if (pc) pc.style.display = (m === 'cal') ? '' : 'none';
  if (pm) pm.style.display = (m === 'premarket') ? '' : 'none';
  if (pl) pl.style.display = (m === 'like') ? '' : 'none';
  if (pch) pch.style.display = (m === 'chart') ? '' : 'none';
  // 本日筛选条只对 今天/日历 的 Benzinga 新闻日报有意义
  const bar = document.getElementById('digestInDayBar');
  if (bar) bar.style.display = (m === 'today' || m === 'cal') ? '' : 'none';
  // 切换今天↔日历 → 清掉上一视图的本日筛选(否则筛选+计数带到新视图, 而 _digestActive
  // 还指向旧列表 → "显示2个但无内容"那类串台 bug)。清输入/高亮/信息。
  if (m === 'today' || m === 'cal') {
    _digestInFilter = { channel: '', ticker: '', sentiment: '', mustread: false, fvai: false };
    const itk = document.getElementById('digestInTk'); if (itk) itk.value = '';
    const inf = document.getElementById('digestInDayInfo'); if (inf) inf.textContent = '';
    document.querySelectorAll('#digestInDayBar .dg-chanf, #digestInDayBar .dg-sentf').forEach(function(b) { b.classList.remove('active'); });
  }
  if (m === 'today') { digestTodayLoad(); digestSSEConnect(); }   // digestTodayLoad 总是恢复 active=今天并重渲
  else { digestSSEClose(); }
  if (m === 'cal') {
    if (!_digestCalInited) { _digestCalInited = true; digestLoadCalendar(); digestLoad(''); }
    else {
      // 重新进日历 → 恢复 active 到日历内容并重渲(否则 active 还指向今天列表)
      _digestActive = _digestCalActive || { items: _digestDayItems || [], host: 'digestContent', date: _digestDayDate || '', showDate: false };
      digestRenderActive();
    }
  }
  if (m === 'premarket') pmInit();
  if (m === 'like') digestLikeLoad();
  if (m === 'chart') { if (window.MarketChart) { MarketChart.open(); requestAnimationFrame(function(){ MarketChart.relayout(); }); } }
  // 切换到别的 mode 时把 tickers/持仓 子视图收回, 避免"今天"下次显示时留着这些界面
  if (m !== 'today') { _digestTkResetIfOpen(); _digestPosResetIfOpen(); }
  // 今天/日历/盘前共用 #tab-digest 这一个滚动容器。切视图后必须回顶,
  // 否则上一视图的滚动量(如在"今天"看多筛选后停在某条)会带进新视图,
  // 看起来像"切到日历就跳到那条看多帖子"。作者切视图应从顶部开始。
  const _dsc = document.getElementById('tab-digest'); if (_dsc) _dsc.scrollTop = 0;
}

// ══════════════════ ❤️ 喜欢(收藏)══════════════════
// 服务端持久(/api/digest/liked),跨"已读"/跨天。稳定 key = url 或 _date#id;
// 快照条目直接带 .key。心形在 _digestMakeRow 里逐行渲染。
let _digestLiked = new Set();
function _digestItemKey(it) {
  if (!it) return null;
  if (it.key) return it.key;                       // 快照条目直接带 key
  const u = (it.url || '').trim();
  if (u) return u;
  if (it.id != null && it._date) return it._date + '#' + it.id;
  return null;
}
function _digestSyncHeart(el, key) {
  const liked = _digestLiked.has(key);
  el.textContent = liked ? '❤️' : '🤍';
  el.title = liked ? '取消喜欢' : '喜欢(收藏)';
}
function _digestSyncAllHearts() {
  document.querySelectorAll('#tab-digest .dg-like').forEach(function(el) {
    _digestSyncHeart(el, el.getAttribute('data-key'));
  });
}
function _digestLikeSnapshot(it) {
  const fields = ['url','title','title_cn','source','ts','hhmm','_date','sentiment','tickers','channels','_bilingual','teaser'];
  const o = {}; fields.forEach(function(k) { if (it[k] != null) o[k] = it[k]; });
  if (it.id != null) o.id = it.id;                 // 无 url 时服务端算 _date#id 用
  return o;
}
function digestToggleLike(it, el) {
  const key = _digestItemKey(it); if (!key) return;
  const wasLiked = _digestLiked.has(key);
  if (wasLiked) _digestLiked.delete(key); else _digestLiked.add(key);   // 乐观更新
  if (el) _digestSyncHeart(el, key);
  const payload = wasLiked ? { action: 'remove', key: key }
                           : { action: 'add', item: _digestLikeSnapshot(it) };
  fetch('/api/digest/liked', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (!d || !d.ok) { if (wasLiked) _digestLiked.add(key); else _digestLiked.delete(key); if (el) _digestSyncHeart(el, key); return; }
      if (_digestMode === 'like') digestLikeLoad();  // 在喜欢 tab 里增/删后重渲
    })
    .catch(function() { if (wasLiked) _digestLiked.add(key); else _digestLiked.delete(key); if (el) _digestSyncHeart(el, key); });
}
function _digestLoadLiked() {
  return fetch('/api/digest/liked').then(function(r) { return r.json(); }).then(function(d) {
    _digestLiked = new Set(d.keys || []);
    _digestSyncAllHearts();
  }).catch(function() {});
}
function digestLikeLoad() {
  const host = document.getElementById('digestLikeList');
  const cnt = document.getElementById('digestLikeCount');
  fetch('/api/digest/liked').then(function(r) { return r.json(); }).then(function(d) {
    const items = d.items || [];
    _digestLiked = new Set(d.keys || []);
    items.forEach(function(it) { if (it.id == null) it.id = it.key; });  // renderDigestList 按 id 去重
    _digestActive = { items: items, host: 'digestLikeList', date: '', showDate: true };
    if (cnt) cnt.textContent = items.length ? (items.length + ' 条收藏') : '还没有收藏 — 在任意新闻右侧点 🤍 即可';
    renderDigestList(items, host, true, null);
  }).catch(function() { if (host) host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载失败</div>'; });
}
function digestClearLikes() {
  if (!confirm('清空所有喜欢?此操作不可撤销。')) return;
  fetch('/api/digest/liked', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'clear' }) })
    .then(function(r) { return r.json(); })
    .then(function() { _digestLiked = new Set(); digestLikeLoad(); _digestSyncAllHearts(); })
    .catch(function() {});
}

// ══════════════════ 🧭 市场判断(决策树) ══════════════════
// 渲染 /api/market_brief 的结构化结果。刻意的设计取舍:
//  · 「大跌概率」放最上面且带历史基准率 —— 这是对抗「一跌就想到 00/08 年」的锚
//  · 不显示任何「看多/看空」字样 —— 标签会变成身份认同(见 brief.py 的 system prompt)
//  · 每个板块/情景都强制显示证伪条件 —— 说不出证伪条件的判断不值得看
var _mbPollTimer = null;
function mbEsc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function _mbBar(label, p, color, note) {
  var pct = Math.round((p || 0) * 100);
  return '<div style="margin-bottom:7px">'
    + '<div style="display:flex;align-items:baseline;gap:8px;font-size:12px">'
    +   '<span style="color:#e6edf3;min-width:150px">' + mbEsc(label) + '</span>'
    +   '<span style="color:' + color + ';font-weight:700;font-size:15px;min-width:46px">' + pct + '%</span>'
    +   '<span style="color:#6e7681;font-size:11px">' + mbEsc(note || "") + '</span>'
    + '</div>'
    + '<div style="height:7px;background:#161b22;border-radius:4px;overflow:hidden;margin-top:3px">'
    +   '<div style="height:100%;width:' + pct + '%;background:' + color + '"></div></div></div>';
}
function _mbSection(title, inner) {
  return '<div style="margin:16px 0 6px;color:#8b949e;font-size:12px;font-weight:700;'
    + 'border-bottom:1px solid #21262d;padding-bottom:4px">' + title + '</div>' + inner;
}
var _MB_GRADE = { "偏强": "#3fb950", "中性": "#8b949e", "偏弱": "#f85149" };
var _MB_PRICED = { "过度": "#3fb950", "合理": "#8b949e", "不足": "#f85149" };
var _MB_SEV = { "低": "#3fb950", "中": "#d29922", "高": "#f85149" };

function mbRender(d) {
  var body = document.getElementById("mbBody"); if (!body) return;
  if (!d || d.status === "none") {
    body.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
      + '还没有今天的分析<br><span style="font-size:11px">点右上角「🔄 重新分析」生成(约 3 分钟)</span></div>';
    return;
  }
  if (d.status === "error" || d.error) {
    body.innerHTML = '<div style="color:#f85149;padding:24px">分析失败: ' + mbEsc(d.error || "") + '</div>';
    return;
  }
  var h = "";
  var st = d.state || {};
  if (st.desc) {
    var conf = (st.confidence != null) ? Math.round(st.confidence * 100) + "%" : "—";
    h += '<div style="background:#0d1117;border:1px solid #21262d;border-left:3px solid #58a6ff;'
      + 'border-radius:6px;padding:12px 14px">'
      + '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:5px">'
      +   '<strong style="color:#58a6ff;font-size:13px">现状刻画</strong>'
      +   '<span style="color:#6e7681;font-size:11px">对现状的置信度 ' + conf
      +   ' · 这是「是什么」不是「会怎样」</span></div>'
      + '<div style="color:#e6edf3;font-size:13px;line-height:1.65">' + mbEsc(st.desc) + '</div>'
      + (st.confidence_why ? '<div style="color:#6e7681;font-size:11px;margin-top:6px;line-height:1.5">'
          + mbEsc(st.confidence_why) + '</div>' : '')
      + '</div>';
  }
  var o = d.drawdown_odds || {};
  if (o.normal != null) {
    h += _mbSection("📉 这轮下跌会走多远(概率)",
      '<div style="background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:12px 14px">'
      + _mbBar("只是 3-5% 正常波动", o.normal, "#3fb950", "历史上每年约 3 次")
      + _mbBar("走成 10% 级别回调", o.correction, "#d29922", "历史上每年约 1 次")
      + _mbBar("走成 20%+ 熊市", o.bear, "#f85149", "历史上每 4-5 年 1 次")
      + (o.why ? '<div style="color:#8b949e;font-size:11.5px;line-height:1.6;margin-top:8px;'
          + 'border-top:1px solid #21262d;padding-top:8px">' + mbEsc(o.why) + '</div>' : '')
      + '</div>');
  }
  var sp = d.support || {};
  var cards = [["news", "📰 消息面"], ["tech", "📊 技术面"], ["fund", "📈 基本面"]]
    .filter(function (p) { return sp[p[0]] && (sp[p[0]].grade || sp[p[0]].why); })
    .map(function (p) {
      var v = sp[p[0]], col = _MB_GRADE[v.grade] || "#8b949e";
      return '<div style="flex:1;min-width:230px;background:#0d1117;border:1px solid #21262d;'
        + 'border-top:2px solid ' + col + ';border-radius:6px;padding:10px 12px">'
        + '<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">'
        +   '<strong style="color:#e6edf3;font-size:12px">' + p[1] + '</strong>'
        +   '<span style="color:' + col + ';font-size:12px;font-weight:700">' + mbEsc(v.grade) + '</span></div>'
        + '<div style="color:#8b949e;font-size:11px;line-height:1.6">' + mbEsc(v.why) + '</div></div>';
    }).join("");
  if (cards) {
    h += _mbSection("三层支撑(互相矛盾时, 矛盾本身就是信息)",
      '<div style="display:flex;gap:10px;flex-wrap:wrap">' + cards + '</div>'
      + (sp.conflict ? '<div style="margin-top:8px;background:#1c1408;border:1px solid #d2992255;'
          + 'border-radius:6px;padding:9px 12px;color:#e3b341;font-size:11.5px;line-height:1.6">'
          + '⚠️ 矛盾: ' + mbEsc(sp.conflict) + '</div>' : ''));
  }
  var secs = d.sectors || [];
  if (secs.length) {
    var rows = secs.map(function (s) {
      var sev = _MB_SEV[s.severity] || "#8b949e", pr = _MB_PRICED[s.priced_in] || "#8b949e";
      return '<tr style="border-bottom:1px solid #161b22">'
        + '<td style="padding:7px 8px;color:#e6edf3;font-size:12px;font-weight:600;white-space:nowrap">'
        +   mbEsc(s.name) + (s.tickers && s.tickers.length
              ? '<div style="color:#6e7681;font-size:10px;font-weight:400">' + mbEsc(s.tickers.join(" ")) + '</div>' : '')
        + '</td>'
        + '<td style="padding:7px 8px;color:#8b949e;font-size:11px;white-space:nowrap">' + mbEsc(s.trend)
        +   (s.price_move ? '<div style="color:#6e7681;font-size:10px">' + mbEsc(s.price_move) + '</div>' : '') + '</td>'
        + '<td style="padding:7px 8px;white-space:nowrap"><span style="color:' + sev + ';font-size:12px;font-weight:700">'
        +   mbEsc(s.severity) + '</span>'
        +   (s.structural ? '<div style="color:#f85149;font-size:10px">结构性</div>'
                          : '<div style="color:#6e7681;font-size:10px">一次性</div>') + '</td>'
        + '<td style="padding:7px 8px;color:' + pr + ';font-size:12px;font-weight:700;white-space:nowrap">'
        +   mbEsc(s.priced_in) + '</td>'
        + '<td style="padding:7px 8px;color:#c9d1d9;font-size:11px;line-height:1.55">' + mbEsc(s.why)
        +   (s.falsify ? '<div style="color:#8b949e;font-size:10.5px;margin-top:3px">'
              + '↩︎ 证伪: ' + mbEsc(s.falsify) + '</div>' : '') + '</td>'
        + '</tr>';
    }).join("");
    h += _mbSection("各板块: 坏消息有多坏 vs 价格反映了多少",
      '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;min-width:760px">'
      + '<tr style="color:#6e7681;font-size:10.5px;text-align:left">'
      +   '<th style="padding:4px 8px">板块</th><th style="padding:4px 8px">趋势</th>'
      +   '<th style="padding:4px 8px">坏度</th><th style="padding:4px 8px">定价</th>'
      +   '<th style="padding:4px 8px">机制与证伪条件</th></tr>' + rows + '</table></div>');
  }
  var scen = d.scenarios || [];
  if (scen.length) {
    var sc = scen.map(function (s) {
      var pct = Math.round((s.prob || 0) * 100);
      return '<div style="flex:1;min-width:250px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px">'
        + '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">'
        +   '<strong style="color:#e6edf3;font-size:12px">' + mbEsc(s.name) + '</strong>'
        +   '<span style="color:#58a6ff;font-size:15px;font-weight:700">' + pct + '%</span></div>'
        + '<div style="color:#c9d1d9;font-size:11.5px;line-height:1.6">' + mbEsc(s.desc) + '</div>'
        + (s.mechanism ? '<div style="color:#8b949e;font-size:11px;line-height:1.55;margin-top:5px">机制: '
            + mbEsc(s.mechanism) + '</div>' : '')
        + (s.watch ? '<div style="color:#6e7681;font-size:10.5px;margin-top:5px">👁 ' + mbEsc(s.watch) + '</div>' : '')
        + '</div>';
    }).join("");
    h += _mbSection("情景分布", '<div style="display:flex;gap:10px;flex-wrap:wrap">' + sc + '</div>');
  }
  if (d.wrong_where) {
    h += _mbSection("我最可能错在哪",
      '<div style="background:#0d1117;border:1px solid #21262d;border-left:3px solid #d29922;'
      + 'border-radius:6px;padding:11px 13px;color:#c9d1d9;font-size:12px;line-height:1.65">'
      + mbEsc(d.wrong_where) + '</div>');
  }
  var un = d.unknowns || [], wa = d.watch || [];
  if (un.length || wa.length) {
    var col = function (title, arr, icon) {
      if (!arr.length) return "";
      return '<div style="flex:1;min-width:280px">'
        + '<div style="color:#8b949e;font-size:11px;font-weight:700;margin-bottom:5px">' + title + '</div>'
        + arr.map(function (x) {
            return '<div style="color:#c9d1d9;font-size:11.5px;line-height:1.6;margin-bottom:4px">'
              + icon + ' ' + mbEsc(x) + '</div>'; }).join("")
        + '</div>';
    };
    h += _mbSection("不知道的事 / 接下来盯什么",
      '<div style="display:flex;gap:16px;flex-wrap:wrap">'
      + col("我不知道但会影响判断的", un, "?") + col("观察清单", wa, "👁") + '</div>');
  }
  var ev = d.evidence || [];
  if (ev.length) {
    h += _mbSection("底层证据(可与上面的判断对账)",
      '<div style="display:flex;gap:8px;flex-wrap:wrap">'
      + ev.map(function (e) {
          return '<div style="background:#0d1117;border:1px solid #21262d;border-radius:5px;padding:7px 10px;min-width:150px">'
            + '<div style="color:#6e7681;font-size:10px">' + mbEsc(e.name) + '</div>'
            + '<div style="color:#e6edf3;font-size:13px;font-weight:600">' + mbEsc(e.value) + '</div>'
            + (e.note ? '<div style="color:#6e7681;font-size:10px">' + mbEsc(e.note) + '</div>' : '')
            + '</div>'; }).join("") + '</div>');
  }
  var au = d.audit || {};
  if (au.checks && au.checks.length) {
    var sc = au.score || 0;
    var scol = sc >= 90 ? "#3fb950" : (sc >= 70 ? "#d29922" : "#f85149");
    h += _mbSection("输出质量审计(自动跑, 不达标会重新生成一次)",
      '<div style="background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px">'
      + '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">'
      +   '<span style="color:' + scol + ';font-size:18px;font-weight:700">' + sc + '</span>'
      +   '<span style="color:#6e7681;font-size:11px">/100'
      +   (d.n_calls > 1 ? ' · 已自动重生成 ' + (d.n_calls - 1) + ' 次' : '') + '</span></div>'
      + '<div style="display:flex;gap:6px;flex-wrap:wrap">'
      + au.checks.map(function (c) {
          return '<span title="' + mbEsc(c.detail) + '" style="font-size:10.5px;padding:2px 7px;border-radius:3px;'
            + 'background:' + (c.ok ? "#3fb95022" : "#f8514922") + ';color:' + (c.ok ? "#3fb950" : "#f85149") + '">'
            + (c.ok ? "✓" : "✗") + " " + mbEsc(c.name) + '</span>'; }).join("")
      + '</div></div>');
  }
  var src = d.sources || [];
  if (src.length) {
    h += _mbSection("用到的分析文章(" + src.length + " 篇, 已按「是否含机制分析」筛选)",
      '<div style="color:#8b949e;font-size:11px;line-height:1.7">'
      + src.map(function (s) {
          return '<div>· ' + mbEsc(s.title)
            + (s.channels && s.channels.length
                ? ' <span style="color:#6e7681">(' + mbEsc(s.channels.join("/")) + ')</span>' : '')
            + '</div>'; }).join("") + '</div>');
  }
  body.innerHTML = h || '<div style="color:#8b949e;padding:40px;text-align:center">没有内容</div>';
  var meta = document.getElementById("mbMeta");
  var u = d.usage || {};
  var tot = ["in", "out", "cache_write", "cache_read"].reduce(function (a, k) {
    return a + (typeof u[k] === "number" ? u[k] : 0); }, 0);
  if (meta) meta.textContent = (d.date || "") + (d.model ? " · " + d.model : "")
    + (d.ts ? " · " + String(d.ts).replace("T", " ").slice(5, 16) : "")
    + (tot ? " · " + Math.round(tot / 1000) + "k tok" : "")
    + (d.summary_day && d.summary_day !== d.date ? " · 新闻取自 " + d.summary_day : "");
}

function mbRun(force) {
  var body = document.getElementById("mbBody");
  var btn = document.getElementById("mbRunBtn");
  if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
  if (body) body.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
    + '正在分析(Sonnet 读全板块数据 + 当日分析文章, 约 3 分钟)…<br>'
    + '<span style="font-size:11px">可以切走做别的, 跑完回来看</span></div>';
  fetch("/api/market_brief/run" + (force ? "?force=true" : ""), { method: "POST" })
    .then(function (r) { return r.json(); })
    .then(function () { _mbPoll(); })
    .catch(function (e) {
      if (body) body.innerHTML = '<div style="color:#f85149;padding:24px">启动失败: ' + mbEsc("" + e) + '</div>';
      if (btn) { btn.disabled = false; btn.textContent = "🔄 重新分析"; }
    });
}
function _mbPoll() {
  if (_mbPollTimer) clearTimeout(_mbPollTimer);
  fetch("/api/market_brief/status").then(function (r) { return r.json(); }).then(function (d) {
    if (d.status === "running") {
      var body = document.getElementById("mbBody");
      if (body) body.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
        + '正在分析… ' + Math.round(d.elapsed || 0) + 's<br>'
        + '<span style="font-size:11px">Sonnet 读全板块 + 分析文章, 通常 2-4 分钟</span></div>';
      _mbPollTimer = setTimeout(_mbPoll, 4000);
      return;
    }
    var btn = document.getElementById("mbRunBtn");
    if (btn) { btn.disabled = false; btn.textContent = "🔄 重新分析"; }
    mbRender(d);
  }).catch(function () {
    var btn = document.getElementById("mbRunBtn");
    if (btn) { btn.disabled = false; btn.textContent = "🔄 重新分析"; }
  });
}
// ── 🔄 行业轮动(81 组细分行业, 短/中/长) ──
var _igData = null;
var _IG_STATE_COL = { "趋势向上": "#3fb950", "高位回调": "#d29922",
                      "超跌反弹": "#58a6ff", "趋势向下": "#f85149" };
// 柱状图: 涨的在横轴上方、跌的在下方, 按当前周期排序。
// ⚠️ 颜色按**状态**而非涨跌 —— 「超跌反弹」和「趋势向上」都是正的但含义完全不同
//    (前者中期仍弱、未确认转势), 这个区别比涨跌本身更该被一眼看到。
var _igTipData = {};      // {组名: 组数据} —— 悬浮时即时渲染, 不用原生 title
var _IG_BAR = {
  "趋势向上": { fill: "#3fb950", label: "趋势向上" },
  "超跌反弹": { fill: "#58a6ff", label: "超跌反弹(未确认)" },
  "高位回调": { fill: "#d29922", label: "高位回调" },
  "趋势向下": { fill: "#f85149", label: "趋势向下" }
};
function igDrawChart(rows, k) {
  var host = document.getElementById("igChart"); if (!host) return;
  var data = rows.filter(function (g) { return typeof g[k] === "number"; });
  if (!data.length) { host.innerHTML = ""; return; }
  // ⚠️ tab 刚切过来时容器还没布局(clientWidth=0) → 画出来的 SVG 实际宽度是 0,
  //    看起来就是「图不见了」。等下一帧再画。
  //    但**必须有重试上限** —— 窗口最小化/面板折叠时宽度会一直是 0, 无限 rAF
  //    等于永远不画(比画错更糟)。超过上限就用兜底宽度画出来, ResizeObserver
  //    会在真正有宽度时重画。
  if (!host.clientWidth) {
    var tries = (igDrawChart._t = (igDrawChart._t || 0) + 1);
    if (tries <= 8) {
      // ⚠️ rAF 是可见时的快路径, 但**页面隐藏时它一次都不触发** —— 而 clientWidth=0
      //    恰恰最常发生在页面不可见的时候(窗口在后台/别的 Space/刚切过来还没布局)。
      //    只用 rAF 的话重试链会永远停在第 1 次, 上面那个上限根本走不到, 图就一直不画
      //    —— 实测 document.hidden=true 时 rAF 触发 0 次, setTimeout 正常。
      var again = function () { igDrawChart(rows, k); };
      if (document.hidden) setTimeout(again, 120); else requestAnimationFrame(again);
      return;
    }
  }
  igDrawChart._t = 0;
  var W = Math.max(host.clientWidth || 1200, 700);
  // English group names run longer than the Chinese ones: give them a little more room
  var enLabels = !!(window.zeusLang && window.zeusLang() === "en");
  // the left margin holds the ±% scale and the start of the first rotated labels
  var padL = 50, padR = 8, labelH = enLabels ? 100 : 88, chartH = 400;
  var innerW = W - padL - padR;
  var bw = innerW / data.length;
  var maxPos = Math.max(0, Math.max.apply(null, data.map(function (g) { return g[k]; })));
  var maxNeg = Math.max(0, -Math.min.apply(null, data.map(function (g) { return g[k]; })));
  var span = (maxPos + maxNeg) || 1;
  var zeroY = (maxPos / span) * chartH;          // 零线位置按正负幅度比例分配
  var H = chartH + labelH;

  var bars = "", labels = "", grid = "", gridText = "";
  // Rotated labels need about 13px between neighbours or they overprint each other:
  // on a narrow window label every 2nd (or 3rd) bar, keeping about 12 labels at each end.
  var stride = Math.max(1, Math.ceil(13 / bw)), lastLab = -1e9;
  // 参考线: ±10% / ±20%
  [10, 20, 30, 50].forEach(function (v) {
    [v, -v].forEach(function (sv) {
      var y = zeroY - (sv / span) * chartH;
      if (y < 2 || y > chartH - 2) return;
      grid += '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (W - padR)
        + '" y2="' + y.toFixed(1) + '" stroke="#21262d" stroke-width="1" stroke-dasharray="2,3"/>';
      // the value sits in the left margin, clear of the bars
      gridText += '<text x="' + (padL - 5) + '" y="' + (y + 3).toFixed(1)
        + '" text-anchor="end" fill="#6e7681" font-size="9">' + (sv > 0 ? "+" : "") + sv + '%</text>';
    });
  });

  data.forEach(function (g, i) {
    var v = g[k];
    var x = padL + i * bw;
    var h = Math.abs(v / span) * chartH;
    var y = v >= 0 ? (zeroY - h) : zeroY;
    var meta = _IG_BAR[g.state] || { fill: "#8b949e", label: g.state };
    _igTipData[g.name] = g;   // 悬浮内容由 _igTip 即时渲染成 HTML(原生 title 要等 ~1s)
    bars += '<rect x="' + (x + bw * 0.12).toFixed(1) + '" y="' + y.toFixed(1) + '" '
      + 'width="' + (bw * 0.76).toFixed(1) + '" height="' + Math.max(h, 1).toFixed(1) + '" '
      + 'fill="' + meta.fill + '" opacity="0.88" rx="1" class="ig-bar" '
      + 'data-ig="' + mbEsc(g.name) + '"></rect>';
    // 标签: 79 个中文标签全画会糊成一团(实测) —— 只标**两端显著的**那些。
    // 中间接近 0 的组本来就不是轮动关注点, 悬停仍能看到全名。
    var lx = x + bw / 2, ly = chartH + 6;
    var nEnd = 12 * stride;
    if ((data.length <= 40 || i < nEnd || i >= data.length - nEnd) && Math.abs(v) >= 1.5
        && i - lastLab >= stride) {
      lastLab = i;
      // the full name goes in (so the interface language can translate it) and is fitted below
      labels += '<text class="ig-lab" x="' + lx.toFixed(1) + '" y="' + ly + '" fill="#8b949e" font-size="9.5" '
        + 'text-anchor="end" transform="rotate(-55,' + lx.toFixed(1) + ',' + ly + ')">'
        + mbEsc(g.name) + '</text>';
    }
  });

  var legend = Object.keys(_IG_BAR).map(function (kk) {
    return '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">'
      + '<span style="width:9px;height:9px;background:' + _IG_BAR[kk].fill + ';border-radius:2px"></span>'
      + '<span style="color:#8b949e;font-size:10.5px">' + _IG_BAR[kk].label + '</span></span>';
  }).join("");

  var _html = '<div style="background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 8px 4px;margin-bottom:12px">'
    + '<div style="display:flex;align-items:baseline;gap:10px;padding:0 6px 6px;flex-wrap:wrap">'
    +   '<span style="color:#e6edf3;font-size:12px;font-weight:600">行业强弱</span>'
    +   '<span style="color:#6e7681;font-size:10.5px">柱高=涨跌幅 · 颜色=趋势状态 · 仅标注两端各12组, 悬停看全名</span>'
    +   '<span style="flex:1"></span>' + legend + '</div>'
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" style="display:block">'
    +   grid
    +   '<line x1="' + padL + '" y1="' + zeroY.toFixed(1) + '" x2="' + (W - padR)
    +     '" y2="' + zeroY.toFixed(1) + '" stroke="#484f58" stroke-width="1"/>'
    +   bars + labels + gridText
    + '</svg></div>';
  host.innerHTML = _html;
  // 事件委托: 鼠标进柱子即时显示成分股(原生 title 要等 ~1s, 容易以为没有)
  var svgEl = host.querySelector("svg");
  if (svgEl) {
    svgEl.addEventListener("mousemove", function (ev) {
      var t = ev.target;
      if (t && t.classList && t.classList.contains("ig-bar")) _igTipShow(t.getAttribute("data-ig"), ev);
      else _igTipHide();
    });
    svgEl.addEventListener("mouseleave", _igTipHide);
    // Shorten each label to the room under the axis (a -55° label of length L drops 0.82·L),
    // and again once the interface language has swapped in the English name. In English the
    // Chinese name waits for its translation: cutting it first would break the lookup.
    // A label also runs left by 0.57·L plus about 0.82 of a glyph's height, so the first
    // few are capped by the room to their left.
    var maxLab = (labelH - 12) / Math.sin(55 * Math.PI / 180), cos55 = Math.cos(55 * Math.PI / 180);
    var fitLabels = function (force) {
      svgEl.querySelectorAll("text.ig-lab").forEach(function (t) {
        if (!force && enLabels && /[一-鿿]/.test(t.textContent)) return;
        var lim = Math.min(maxLab, (parseFloat(t.getAttribute("x")) - 10) / cos55);
        var len = t.getComputedTextLength();
        if (!len || len <= lim) return;
        var s = t.textContent.replace(/…$/, "");
        while (s.length > 2) {
          s = s.slice(0, -1);
          t.textContent = s.replace(/[\\s\\/:·,(（-]+$/, "") + "…";
          if (t.getComputedTextLength() <= lim) break;
        }
      });
    };
    fitLabels(false);
    if (window.MutationObserver)
      new MutationObserver(function () { fitLabels(false); })
        .observe(svgEl, { subtree: true, childList: true, characterData: true });
    setTimeout(function () { fitLabels(true); }, 1500);
  }
}

// 即时悬浮框(跟随鼠标): 组信息 + **全部成分股及各自 3 个月收益**
function _igTipEl() {
  var el = document.getElementById("igTipBox");
  if (!el) {
    el = document.createElement("div");
    el.id = "igTipBox";
    el.style.cssText = "position:fixed;z-index:9999;display:none;pointer-events:none;"
      + "background:#161b22;border:1px solid #30363d;border-radius:6px;padding:9px 11px;"
      + "box-shadow:0 6px 20px rgba(0,0,0,.6);max-width:430px;font-size:11px;line-height:1.6";
    document.body.appendChild(el);
  }
  return el;
}
function _igTipShow(name, ev) {
  var g = _igTipData[name]; if (!g) return;
  var el = _igTipEl();
  var meta = _IG_BAR[g.state] || { fill: "#8b949e", label: g.state };
  function num(v) {
    if (v == null) return '<span style="color:#484f58">—</span>';
    var c = v > 0 ? "#3fb950" : (v < 0 ? "#f85149" : "#8b949e");
    return '<span style="color:' + c + '">' + (v > 0 ? "+" : "") + v.toFixed(1) + '</span>';
  }
  var h = '<div style="color:#e6edf3;font-weight:700;font-size:12.5px;margin-bottom:3px">' + mbEsc(g.name)
    + ' <span style="color:' + meta.fill + ';font-weight:600;font-size:11px">' + mbEsc(meta.label) + '</span></div>'
    + '<div style="color:#8b949e">5日 ' + num(g.d5) + ' · 1月 ' + num(g.m1) + ' · 3月 ' + num(g.m3)
    + ' · 6月 ' + num(g.m6) + ' · 1年 ' + num(g.y1) + '</div>'
    + '<div style="color:#6e7681;margin-bottom:5px">' + g.n + ' 只 · 组内 '
    + (g.pct_above_ma50 == null ? "?" : g.pct_above_ma50) + '% 在 MA50 上方'
    + (g.skewed ? ' · <span style="color:#d29922">分化(涨的是少数)</span>' : '') + '</div>';
  var mem = g.members || [];
  if (mem.length) {
    h += '<div style="color:#6e7681;border-top:1px solid #21262d;padding-top:5px;margin-bottom:2px">成分股(3月收益):</div>'
      + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:2px 8px">'
      + mem.map(function (m) {
          var c = (m.r == null) ? "#484f58" : (m.r > 0 ? "#3fb950" : "#f85149");
          return '<span style="color:#c9d1d9;white-space:nowrap">' + mbEsc(m.t)
            + ' <span style="color:' + c + '">'
            + (m.r == null ? "—" : ((m.r > 0 ? "+" : "") + m.r.toFixed(0))) + '</span></span>';
        }).join("") + '</div>';
  }
  if (g.missing && g.missing.length) {
    h += '<div style="color:#484f58;margin-top:4px">无数据: ' + mbEsc(g.missing.join(" ")) + '</div>';
  }
  el.innerHTML = h;
  el.style.display = "block";
  var r = el.getBoundingClientRect();          // 贴鼠标但不越出视口
  var x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > window.innerWidth - 8) x = Math.max(8, ev.clientX - r.width - 14);
  if (y + r.height > window.innerHeight - 8) y = Math.max(8, ev.clientY - r.height - 14);
  el.style.left = x + "px";
  el.style.top = y + "px";
}
function _igTipHide() {
  var el = document.getElementById("igTipBox");
  if (el) el.style.display = "none";
}

function igRender() {
  var box = document.getElementById("igBody"); if (!box) return;
  var d = _igData;
  if (!d || d.error) {
    box.innerHTML = '<div style="color:#f85149;padding:24px">' + mbEsc((d && d.error) || "无数据") + '</div>';
    return;
  }
  var k = (document.getElementById("igHorizon") || {}).value || "m1";   // 默认 1 个月 ≈ 用户 2-5 周持仓期
  var hideEtf = !!(document.getElementById("igHideEtf") || {}).checked;
  var onlyBig = !!(document.getElementById("igOnlyBig") || {}).checked;
  var rows = (d.rows || []).slice();
  if (hideEtf) rows = rows.filter(function (g) { return g.name.indexOf("ETF") < 0; });
  rows.sort(function (a, b) {
    var x = a[k], y = b[k];
    if (x == null) return 1; if (y == null) return -1; return y - x;
  });
  // ⚠️ 必须在 sort **之后**再过滤 —— filter 返回新数组, 先过滤后排序的话
  //    图拿到的是未排序的副本(柱子会乱序, 实测踩过)。
  var chartRows = onlyBig
    ? rows.filter(function (g) { return typeof g[k] === "number" && Math.abs(g[k]) >= 3; })
    : rows.slice();
  function cell(v) {
    if (v == null) return '<td style="padding:5px 7px;color:#484f58;text-align:right">—</td>';
    var c = v > 0 ? "#3fb950" : (v < 0 ? "#f85149" : "#8b949e");
    return '<td style="padding:5px 7px;color:' + c + ';text-align:right;font-variant-numeric:tabular-nums">'
      + (v > 0 ? "+" : "") + v.toFixed(1) + '</td>';
  }
  function bar(pct) {
    if (pct == null) return "—";
    var c = pct >= 60 ? "#3fb950" : (pct >= 30 ? "#d29922" : "#f85149");
    return '<span style="display:inline-block;width:34px;height:6px;background:#161b22;border-radius:3px;overflow:hidden;vertical-align:middle">'
      + '<span style="display:block;height:100%;width:' + pct + '%;background:' + c + '"></span></span>'
      + ' <span style="color:#6e7681;font-size:10px">' + pct + '%</span>';
  }
  var h = '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;min-width:820px">'
    + '<tr style="color:#6e7681;font-size:10.5px;text-align:left">'
    + '<th style="padding:4px 7px">行业组</th><th style="padding:4px 7px;text-align:right">5日</th>'
    + '<th style="padding:4px 7px;text-align:right">1月</th><th style="padding:4px 7px;text-align:right">3月</th>'
    + '<th style="padding:4px 7px;text-align:right">6月</th><th style="padding:4px 7px;text-align:right">1年</th>'
    + '<th style="padding:4px 7px">组内>MA50</th><th style="padding:4px 7px">状态</th>'
    + '<th style="padding:4px 7px">最强 / 最弱(3月)</th></tr>';
  rows.forEach(function (g) {
    var col = _IG_STATE_COL[g.state] || "#8b949e";
    var top = (g.top || []).map(function (x) { return x.t; }).join(" ");
    var bot = (g.bottom || []).map(function (x) { return x.t; }).join(" ");
    h += '<tr style="border-bottom:1px solid #161b22">'
      + '<td style="padding:5px 7px;color:#e6edf3;font-size:12px;font-weight:600;white-space:nowrap">'
      +   mbEsc(g.name) + ' <span style="color:#6e7681;font-size:10px;font-weight:400">' + g.n + '只</span>'
      +   (g.skewed ? ' <span title="中位数与均值差异大 = 组内分化, 涨的是少数" style="color:#d29922;font-size:10px">⚠分化</span>' : '')
      + '</td>'
      + cell(g.d5) + cell(g.m1) + cell(g.m3) + cell(g.m6) + cell(g.y1)
      + '<td style="padding:5px 7px;white-space:nowrap">' + bar(g.pct_above_ma50) + '</td>'
      + '<td style="padding:5px 7px;white-space:nowrap"><span style="color:' + col + ';font-size:11px;font-weight:700">'
      +   mbEsc(g.state) + '</span><span style="color:#6e7681;font-size:10px">·' + mbEsc(g.grade) + '</span></td>'
      + '<td style="padding:5px 7px;color:#6e7681;font-size:10px;white-space:nowrap">'
      +   '<span style="color:#3fb950">' + mbEsc(top) + '</span> / <span style="color:#f85149">' + mbEsc(bot) + '</span></td>'
      + '</tr>';
  });
  h += '</table></div>';
  var r = d.rotation || {};
  if (r.warming || r.cooling) {
    h = '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">'
      + '<div style="flex:1;min-width:250px;background:#0d1117;border:1px solid #21262d;border-left:3px solid #3fb950;border-radius:6px;padding:9px 12px">'
      +   '<div style="color:#3fb950;font-size:11px;font-weight:700;margin-bottom:3px">📈 升温(短期强于中期趋势)</div>'
      +   '<div style="color:#c9d1d9;font-size:11.5px;line-height:1.6">' + mbEsc((r.warming || []).join(" · ")) + '</div></div>'
      + '<div style="flex:1;min-width:250px;background:#0d1117;border:1px solid #21262d;border-left:3px solid #f85149;border-radius:6px;padding:9px 12px">'
      +   '<div style="color:#f85149;font-size:11px;font-weight:700;margin-bottom:3px">📉 降温(短期弱于中期趋势)</div>'
      +   '<div style="color:#c9d1d9;font-size:11.5px;line-height:1.6">' + mbEsc((r.cooling || []).join(" · ")) + '</div></div>'
      + '</div>' + h;
  }
  box.innerHTML = h;
  igDrawChart(chartRows, k);
  var meta = document.getElementById("igMeta");
  if (meta) meta.textContent = zt("截至 " + (d.as_of || "?") + " · " + (d.n_groups || 0) + " 组 / "
    + (d.n_tickers || 0) + " 只" + ((d.stale && d.stale.length) ? " · 剔除陈旧 " + d.stale.length : ""),
    "As of " + (d.as_of || "?") + " · " + (d.n_groups || 0) + " groups / "
    + (d.n_tickers || 0) + " tickers" + ((d.stale && d.stale.length) ? " · " + d.stale.length + " stale left out" : ""));
}
// 容器尺寸变了就重画(切 tab、拖窗口、开合侧栏都会触发)
var _igRO = null;
function igWatchResize() {
  if (_igRO || typeof ResizeObserver === "undefined") return;
  var host = document.getElementById("igChart"); if (!host) return;
  var last = 0;
  _igRO = new ResizeObserver(function () {
    var w = host.clientWidth;
    if (w && Math.abs(w - last) > 30) { last = w; if (_igData) igRender(); }
  });
  _igRO.observe(host);
}

function igLoad(force) {
  fetch("/api/industry_groups" + (force ? "?force=true" : ""))
    .then(function (r) { return r.json(); })
    .then(function (d) { _igData = d; igRender(); })
    .catch(function (e) {
      var b = document.getElementById("igBody");
      if (b) b.innerHTML = '<div style="color:#f85149;padding:24px">加载失败: ' + mbEsc("" + e) + '</div>';
    });
}
function igRefresh() {
  var btn = document.getElementById("igRefreshBtn");
  if (btn) { btn.disabled = true; btn.textContent = "重拉中…"; }
  var b = document.getElementById("igBody");
  if (b) b.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
    + '正在重拉 572 只票的日线(约 1-2 分钟)…<br><span style="font-size:11px">'
    + '必须全量重拉 —— 半量会让新旧数据混算, 横向排名失效</span></div>';
  fetch("/api/industry_groups/refresh", { method: "POST" })
    .then(function (r) { return r.json(); })
    .then(function (d) { _igData = d; igRender(); })
    .catch(function (e) { if (b) b.innerHTML = '<div style="color:#f85149;padding:24px">失败: ' + mbEsc("" + e) + '</div>'; })
    .finally(function () { if (btn) { btn.disabled = false; btn.textContent = "🔄 重拉数据"; } });
}

// ── 🎲 板块期权情绪(IV / 25Δ 偏斜 / P-C 的自身历史百分位) ──────────────
// 只渲染百分位驱动的结论。绝对 IV 一并显示但只作参考 —— 板块之间比绝对 IV
// 没有意义(小盘生技常年 90%, 大行常年 25%), 唯一可比的是"各自离自己的常态多远"。
var _opData = null;

function opLoad() {
  var b = document.getElementById("opBody");
  if (b) b.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>';
  fetch("/api/options_pulse")
    .then(function (r) { return r.json(); })
    .then(function (d) { _opData = d; opRender(); })
    .catch(function (e) {
      if (b) b.innerHTML = '<div style="color:#f85149;padding:24px">失败: ' + mbEsc("" + e) + '</div>';
    });
}

function opRefresh() {
  var btn = document.getElementById("opRefreshBtn");
  if (btn) { btn.disabled = true; btn.textContent = "汇总中…"; }
  var b = document.getElementById("opBody");
  if (b) b.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">'
    + '正在从 EOD 全链汇总新交易日…<br><span style="font-size:11px">'
    + '只算日汇总里还没有的日期, 已有的不重算</span></div>';
  fetch("/api/options_pulse/refresh", { method: "POST" })
    .then(function (r) { return r.json(); })
    .then(function (d) { _opData = d; opRender(); })
    .catch(function (e) { if (b) b.innerHTML = '<div style="color:#f85149;padding:24px">失败: ' + mbEsc("" + e) + '</div>'; })
    .finally(function () { if (btn) { btn.disabled = false; btn.textContent = "🔄 汇总新交易日"; } });
}

// 百分位 → 颜色。高 = 已定价(暖色), 低 = 未定价(冷色)。刻意不用红绿:
// 红绿在交易界面里天然被读成"跌/涨", 而这里根本不是方向。
function _opPctColor(p) {
  if (p == null) return "#6e7681";
  if (p >= 80) return "#d29922";
  if (p >= 60) return "#a78948";
  if (p <= 20) return "#58a6ff";
  if (p <= 40) return "#4d7ea8";
  return "#8b949e";
}

function _opBar(p) {
  if (p == null) return '<span style="color:#6e7681;font-size:11px">样本不足</span>';
  var c = _opPctColor(p);
  return '<span style="display:inline-flex;align-items:center;gap:5px">'
    + '<span style="display:inline-block;width:52px;height:6px;background:#21262d;border-radius:3px;overflow:hidden;vertical-align:middle">'
    + '<span style="display:block;height:6px;width:' + p + '%;background:' + c + '"></span></span>'
    + '<span style="color:' + c + ';font-size:11px;font-variant-numeric:tabular-nums;min-width:26px">' + p + '</span></span>';
}

function opRender() {
  var host = document.getElementById("opBody");
  if (!host) return;
  var d = _opData || {};
  if (d.error) {
    host.innerHTML = '<div style="color:#f85149;padding:24px">' + mbEsc(d.error)
      + '<div style="color:#8b949e;font-size:12px;margin-top:10px">'
      + '首次使用请点右上角「🔄 汇总新交易日」从 EOD 链建日汇总。</div></div>';
    return;
  }
  var rows = (d.rows || []).slice();
  if (!rows.length) { host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">暂无数据</div>'; return; }

  var hideLow = !!(document.getElementById("opHideLow") || {}).checked;
  var key = ((document.getElementById("opSortKey") || {}).value) || "atm_iv_pctl";
  if (hideLow) rows = rows.filter(function (g) { return !g.low_coverage; });
  rows.sort(function (a, b) {
    var x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return y - x;
  });

  var m = document.getElementById("opMeta");
  if (m) m.textContent = "截至 " + (d.asof || "?") + " · " + (d.n_groups || 0) + " 组 / "
    + (d.n_tickers || 0) + " 只 · 历史中位 " + (d.hist_days || 0) + " 天"
    + (d.n_low_coverage ? (" · 低覆盖 " + d.n_low_coverage + " 组") : "");

  var h = '<table style="width:100%;border-collapse:collapse;font-size:12px">'
    + '<thead><tr style="color:#8b949e;font-size:11px;text-align:left;border-bottom:1px solid #21262d">'
    + '<th style="padding:6px 8px">板块</th>'
    + '<th style="padding:6px 8px">覆盖</th>'
    + '<th style="padding:6px 8px">IV 百分位</th>'
    + '<th style="padding:6px 8px">偏斜百分位</th>'
    + '<th style="padding:6px 8px">P/C 量</th>'
    + '<th style="padding:6px 8px">IV</th>'
    + '<th style="padding:6px 8px">读数</th></tr></thead><tbody>';

  rows.forEach(function (g, i) {
    var lowTag = g.low_coverage
      ? '<span title="该组在分类表里的票大多不在期权池里, 用少数几只代表整个板块会失真; 因此不参与排行榜" '
        + 'style="color:#d29922;font-size:10px;border:1px solid #6b5326;border-radius:3px;padding:0 4px;margin-left:5px">低覆盖</span>'
      : '';
    var iv = (g.atm_iv != null) ? (g.atm_iv * 100).toFixed(1) + "%" : "—";
    var pcv = (g.pc_vol != null) ? g.pc_vol.toFixed(2) : "—";
    h += '<tr style="border-bottom:1px solid #161b22;' + (i % 2 ? 'background:#0d111744' : '') + '">'
      + '<td style="padding:6px 8px;color:#e6edf3">' + mbEsc(g.name) + lowTag + '</td>'
      + '<td style="padding:6px 8px;color:#6e7681;font-size:11px;font-variant-numeric:tabular-nums">'
        + g.n + "/" + g.n_listed + '</td>'
      + '<td style="padding:6px 8px">' + _opBar(g.atm_iv_pctl) + '</td>'
      + '<td style="padding:6px 8px">' + _opBar(g.skew25_pctl) + '</td>'
      + '<td style="padding:6px 8px">' + _opBar(g.pc_vol_pctl) + '</td>'
      + '<td style="padding:6px 8px;color:#8b949e;font-variant-numeric:tabular-nums">' + iv
        + '<span style="color:#565f6a;font-size:10px"> ·pc ' + pcv + '</span></td>'
      + '<td style="padding:6px 8px;color:#8b949e;font-size:11px;line-height:1.5">'
        + mbEsc(g.priced_in || "") + '</td>'
      + '</tr>';
  });
  h += '</tbody></table>';
  host.innerHTML = h;
}

function mbSub(which) {
  document.querySelectorAll("#mbSubNav button").forEach(function (b) {
    var on = b.dataset.mb === which;
    b.style.background = on ? "#1f6feb" : "transparent";
    b.style.color = on ? "#fff" : "#8b949e";
    b.style.border = on ? "none" : "1px solid #30363d";
  });
  var isRot = (which === "rot"), isOpt = (which === "opt");
  var bd = document.getElementById("mbBody"), rp = document.getElementById("mbRotPane");
  var op = document.getElementById("mbOptPane");
  if (bd) bd.style.display = (isRot || isOpt) ? "none" : "";
  if (rp) rp.style.display = isRot ? "" : "none";
  if (op) op.style.display = isOpt ? "" : "none";
  var rb = document.getElementById("mbRunBtn");
  if (rb) rb.style.display = (isRot || isOpt) ? "none" : "";
  if (isRot) { igWatchResize(); if (!_igData) igLoad(); else igRender(); }
  if (isOpt) { if (!_opData) opLoad(); else opRender(); }
}

var _mbInited = false;
function mbInit() {
  if (_mbInited) return;
  _mbInited = true;
  fetch("/api/market_brief").then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.status === "done") { mbRender(d); return; }
    // 没缓存 → 看看是不是正在跑(别的窗口点过)
    fetch("/api/market_brief/status").then(function (r) { return r.json(); }).then(function (s) {
      if (s && s.status === "running") { _mbPoll(); } else { mbRender(d); }
    }).catch(function () { mbRender(d); });
  }).catch(function (e) {
    var body = document.getElementById("mbBody");
    if (body) body.innerHTML = '<div style="color:#f85149;padding:24px">加载失败: ' + mbEsc("" + e) + '</div>';
  });
}

// ══════════════════ 📰 双语 sub-tab(WSJ/Barron's/MarketWatch,由 Chrome 扩展 push) ══════════════════
// Data pushed by Chrome extension via POST /api/bilingual/sync.
// All local edits go through POST /api/bilingual/state and then re-render.

// ══════════════════ Today 实时推送 (SSE) ══════════════════
let _digestSSE = null;
function _digestSetLive(state) {
  const dot = document.getElementById('digestLiveDot');
  if (!dot) return;
  // green=实时连通, amber=重连中
  dot.style.background = state ? '#3fb950' : '#d29922';
  dot.style.boxShadow = state ? '0 0 6px #3fb950' : 'none';
}
function digestSSEConnect() {
  if (_digestSSE || typeof EventSource === 'undefined') return;
  try {
    _digestSSE = new EventSource('/api/digest/today/stream');
    _digestSSE.addEventListener('hello', function() { _digestSetLive(true); });
    _digestSSE.addEventListener('refresh', function() { _digestSetLive(true); digestTodayLoad(false); });
    _digestSSE.onerror = function() { _digestSetLive(false); };  // EventSource 会自动重连
  } catch (e) { _digestSSE = null; }
}
function digestSSEClose() {
  if (_digestSSE) { try { _digestSSE.close(); } catch (e) {} _digestSSE = null; }
}
function digestInit() {
  if (_digestLoaded) return;
  _digestLoaded = true;
  _digestSourceFilter = _digestLoadSourceFilter();   // 恢复来源 chip 状态
  _digestRenderSourceChips();
  digestLoadWatchlist();   // 载入默认筛选清单
  _digestUpdateDnBtn();    // ✨ 去噪 chip 初始态 (localStorage 记忆)
  _digestLoadReadCarry();  // 跨天已读 carry (让昨天没看的今天还能看到)
  _digestLoadLiked();      // 拉喜欢 key 集合 → 各列表心形回填
  digestMode('today');  // 默认今天
  // Enter 键在筛选框触发搜索（JS 绑定，避免内联引号转义坑）
  ['dfStart', 'dfEnd', 'dfTicker', 'dfTitle', 'dfBody', 'dfChannel'].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('keydown', function(ev) { if (ev.key === 'Enter') digestSearch(); });
  });
  const itk = document.getElementById('digestInTk');
  if (itk) itk.addEventListener('keydown', function(ev) { if (ev.key === 'Enter') digestInTicker(); });
  // 今天自动刷新：每 90s 拉一次本地今天文件(服务器读本地，0 次 429)；条数没变不重渲染
  // tab 未 active 也照拉:三家扩展 push 进来 90s 内自动可见,切回 digest 就是最新
  setInterval(function() {
    if (_digestMode === 'today') digestTodayLoad(false);
  }, 90000);
}
function digestTodayLoad(force) {
  const host = document.getElementById('digestTodayList'); if (!host) return;
  if (force) { host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>'; _digestTodayCount = -1; }
  const yStr = _digestYesterdayStr();
  const ts = Date.now();
  // 并发拉今天 + 昨天(作者: "昨天没看的第二天还需要看"): 昨天条目
  // 过滤掉跨天 carry 里已 mark 已读的, 剩下的合并进"今天"列表。
  Promise.all([
    fetch('/api/digest/day?_t=' + ts).then(function(r) { return r.json(); }),
    fetch('/api/digest/day?date=' + yStr + '&_t=' + ts)
      .then(function(r) { return r.json(); })
      .catch(function() { return { items: [] }; })
  ]).then(function(pair) {
    const d = pair[0], yd = pair[1] || { items: [] };
    const st = document.getElementById('digestTodayStatus');
    const bzMissing = !!d.error;            // Benzinga 未连接 → 只显示免费源(Finviz / WSJ / MarketWatch)
    if (bzMissing) { d.items = []; yd.items = []; }
    const todayDate = d.date || new Date().toISOString().slice(0, 10);
    const todayItems = (d.items || []).map(function(it) { it._date = todayDate; return it; });
    // 昨天: 过滤掉 read carry 里已标已读的, 剩下的都是"你没看到的重要新闻"
    const yestItems = (yd.items || [])
      .filter(function(it) { return it && it.id != null && !_digestReadCarry.has(String(it.id)); })
      .map(function(it) { it._date = yStr; return it; });
    const bnzItems = todayItems.concat(yestItems);
    _digestBlAllUndeleted(!!force).then(function(blItems) {
      const merged = bnzItems.concat(blItems).sort(function(a, b) { return _digestTsMs(b.ts) - _digestTsMs(a.ts); });
      const total = merged.length;
      const changed = (total !== _digestTodayCount);
      if (changed) { _digestTodayCount = total; _digestTodayItems = merged; }
      // showDate=true: 昨日条目会带日期前缀; 今天条目 _date 存在但视觉一致
      // 有跨天条目才 showDate, 否则和原来一样只显 HH:MM
      _digestActive = { items: _digestTodayItems, host: 'digestTodayList',
                        date: todayDate, showDate: yestItems.length > 0 };
      digestRenderActive();
      if (changed) {
        digestTranslateTitles(todayDate, 'digestTodayList');
        if (yestItems.length) digestTranslateTitles(yStr, 'digestTodayList');
      }
      // 存原始"总数"+ 更新时间, 供 digestRenderActive 每次筛选后重算 status 用
      _digestActive.totalToday = todayItems.length;
      _digestActive.totalYest = yestItems.length;
      _digestActive.yestDate = yStr;
      _digestActive.generatedAt = d.generated_at || '';
      if (st) st.textContent = todayDate + ' · ' + zt(
          todayItems.length + ' 今' + (yestItems.length ? (' + ' + yestItems.length + ' 昨未读') : '')
          + (blItems.length ? (' + ' + blItems.length + ' 免费源双语') : '')
          + (bzMissing ? ' · Benzinga 未连接(设置 ⚙ 里粘贴 cookie)' : (' · 更新于 ' + (d.generated_at || '?'))),
          todayItems.length + ' today' + (yestItems.length ? (' + ' + yestItems.length + ' unread from yesterday') : '')
          + (blItems.length ? (' + ' + blItems.length + ' bilingual from free sources') : '')
          + (bzMissing ? ' · Benzinga not connected (paste a cookie in Settings ⚙)' : (' · updated ' + (d.generated_at || '?'))));
      if (!merged.length && host) host.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">📭 暂无新闻 · 免费源每 5 分钟更新一次</div>';
    });
  }).catch(function(e) { host.innerHTML = '<div style="color:#f85149;padding:40px">加载失败：' + e + '</div>'; });
}
// ── 🤖 AI 新闻总结: 后台生成(关窗口/断连都继续)+进度条轮询; 按 date+筛选scope 缓存; 主摘要可增量 ──
var _dgAiPollTimer = null;
var _dgAiCtx = { date: '', scope: '' };
// Cheap ticker price lookup: 优先 heatmap scrData.signals(160 tickers 都有),
// 兜底 positions 里持仓的 underlying。找不到就返回 null,不塞给 AI。
function _dgAiPriceOf(ticker) {
  if (!ticker) return null;
  var tk = String(ticker).toUpperCase().replace(/^\\$/, '');
  try {
    if (typeof scrData !== 'undefined' && scrData && scrData.signals) {
      var s = scrData.signals.find(function(x) { return x.ticker === tk; });
      if (s && s.price > 0) return { price: s.price, pct: (typeof s.change_pct === 'number') ? s.change_pct : null };
    }
  } catch (e) {}
  try {
    if (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) {
      var pos = RAW_DATA.strategies.find(function(p) { return p.underlying === tk; });
      if (pos && pos.current_price > 0) {
        var pc = (RAW_DATA.equity_prev_close || {})[tk];
        var pct = (pc > 0) ? ((pos.current_price - pc) / pc) * 100 : null;
        return { price: pos.current_price, pct: pct };
      }
    }
  } catch (e) {}
  return null;
}
function _dgAiCollect() {
  var items = window._digestLastRendered || [];
  var out = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i]; if (!it || !it.title) continue;
    var t = it.title;
    // 双语(WSJ/Barron/MW)条目没 ticker,给标题加个源前缀,让 Haiku 知道这是主流大媒体的标题(值得当"宏观/大事"归组)。
    if (it._bilingual) {
      var src = (it.channels && it.channels[0]) || it.source || '';
      if (src) t = '[' + src + '] ' + t;
    }
    // 顺便快照 tickers 的价格 + 今日涨跌 → server 端拼进标题前缀 (老 Haiku prompt 不变)
    var quotes = null;
    var ks = it.tickers || [];
    for (var j = 0; j < ks.length && j < 4; j++) {
      var q = _dgAiPriceOf(ks[j]);
      if (q) { if (!quotes) quotes = {}; quotes[String(ks[j]).toUpperCase().replace(/^\\$/, '')] = { p: q.price, c: q.pct }; }
    }
    var row = { id: it.id, t: t, k: (it.tickers || []).slice(0, 4), s: it.sentiment };
    if (quotes) row.q = quotes;
    out.push(row);
  }
  return out;
}
function _dgHash(s) { var h = 0; for (var i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; } return (h >>> 0).toString(36); }
function _dgAiScope() {
  var f = {}; try { f = _digestInFilter || {}; } catch (e) {}
  var parts = [];
  if (f.ticker) parts.push('tk=' + String(f.ticker).toUpperCase());
  if (f.channel) parts.push('ch=' + f.channel);
  if (f.sentiment) parts.push('sv=' + f.sentiment);
  if (f.mustread) parts.push('mr');
  if (f.fvai) parts.push('ai');
  // 「只看清单」关掉时条目数天差地别(241 → 918), 必须进 key,
  // 否则两个视图共用一份缓存 → 看全量时拿到清单版的旧总结。
  // 只在「关」时加标记:默认(开)保持 scope='',与后台每日生成的口径一致、旧缓存不失效。
  try { if (!_digestWatchlistOn) parts.push('all'); } catch (e) {}
  return parts.length ? _dgHash(parts.join(';')) : '';
}
function _dgAiCtxNow() {
  var date = ''; try { date = (_digestActive && _digestActive.date) || ''; } catch (e) {}
  var scope = _dgAiScope();
  var payload = _dgAiCollect();
  if (!date) { date = 'adhoc'; if (!scope) scope = _dgHash(payload.map(function (x) { return x.id; }).join(',')); }
  return { date: date, scope: scope, items: payload };
}
function digestAiSummary() {
  var ov = _dgAiModal();
  ov.style.display = 'flex';
  // 需要非仓位 ticker 的价格 → 点开总结时确保 scrData 已到位(阻塞 <500ms)
  if (typeof scrData === 'undefined' || !scrData || !scrData.signals) {
    fetch('/api/live_prices').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (d && d.signals) scrData = d;
    }).catch(function () {}).finally(function () { _dgAiKick('auto'); });
  } else {
    _dgAiKick('auto');
  }
}
function _dgAiKick(mode) {
  var ctx = _dgAiCtxNow();
  _dgAiCtx = { date: ctx.date, scope: ctx.scope };
  var sub = document.getElementById('dgAiSub');
  var bd = document.getElementById('dgAiBody');
  if (sub) sub.textContent = (ctx.scope ? '筛选子集 · ' : '') + (ctx.date === 'adhoc' ? '搜索结果' : ctx.date) + ' · ' + ctx.items.length + ' 条';
  // auto 模式打开 modal 瞬间的占位: 别空白让作者以为在重跑, 明说"从缓存加载"
  if (mode === 'auto' && bd) {
    bd.innerHTML = '<div style="color:#8b949e;padding:30px;text-align:center;font-size:12px">📦 检查今日缓存中… <br><span style="font-size:10px;color:#6e7681">(当天已总结过则秒开; 若无缓存才后台生成)</span></div>';
  } else if (mode !== 'auto') {
    _dgAiRenderRunning(0, mode, ctx.items.length);
  }
  fetch('/api/digest/ai_summary', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: ctx.items, date: ctx.date, scope: ctx.scope, mode: mode }) })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.status === 'empty') { if (sub) sub.textContent = ''; bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">当前列表没有可总结的新闻<br><span style="font-size:11px">(试试关掉「只看清单」，或换个日期/筛选)</span></div>'; return; }
      _dgAiPoll();
    })
    .catch(function (e) { bd.innerHTML = '<div style="color:#f85149;padding:20px">请求失败：' + _dgAiEsc('' + e) + '</div>'; });
}
function _dgAiRun(mode) { _dgAiKick(mode); }
function _dgAiPoll() {
  var ov = document.getElementById('dgAiOverlay');
  if (!ov || ov.style.display === 'none') return;   // 窗口关了就停轮询(后台任务照跑)
  var q = '?date=' + encodeURIComponent(_dgAiCtx.date) + '&scope=' + encodeURIComponent(_dgAiCtx.scope);
  fetch('/api/digest/ai_summary/status' + q)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.status === 'running') {
        _dgAiRenderRunning(d.elapsed || 0, d.mode, d.n_items || 0);
        if (_dgAiPollTimer) clearTimeout(_dgAiPollTimer);
        _dgAiPollTimer = setTimeout(_dgAiPoll, 1500);
      } else if (d.status === 'done') {
        _dgAiRenderDone(d);
      } else if (d.status === 'error') {
        var bd = document.getElementById('dgAiBody');
        var em = String(d.error || '');
        // Claude 订阅登录失效 → 显眼提示 + 教怎么修(narratives/claude_login.py 也弹 macOS 通知)
        var isLogin = /订阅登录|not logged in|please run \\/login|failed to authenticate|could not be refreshed|invalid_grant|not authenticated|oauth/i.test(em);
        var extra = isLogin
          ? '<div style="margin-top:10px;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:12px;line-height:1.6">🔑 修法: 打开 Terminal 敲 <code style="background:#161b22;padding:2px 6px;border-radius:4px;color:#a5d6ff">claude</code>, 里面再敲 <code style="background:#161b22;padding:2px 6px;border-radius:4px;color:#a5d6ff">/login</code> 走一遍订阅登录。<br><span style="color:#8b949e;font-size:11px">重登后, 这里点「♻️ 重新总结」就能跑。</span></div>'
          : '';
        bd.innerHTML = '<div style="color:#f85149;padding:20px">总结失败：' + _dgAiEsc(em) + extra + '</div>' + _dgAiFoot(0);
        _dgAiWireFoot();
      } else {
        var bd2 = document.getElementById('dgAiBody');
        bd2.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">还没有总结</div>' + _dgAiFoot(0);
        _dgAiWireFoot();
      }
    })
    .catch(function () { if (_dgAiPollTimer) clearTimeout(_dgAiPollTimer); _dgAiPollTimer = setTimeout(_dgAiPoll, 2500); });
}
function _dgAiRenderRunning(elapsed, mode, nItems) {
  var sub = document.getElementById('dgAiSub');
  var bd = document.getElementById('dgAiBody');
  var pct = Math.min(96, Math.round(100 * (1 - Math.exp(-(elapsed || 0) / 20))));
  if (pct < 6) pct = 6;
  var label = (mode === 'incremental') ? '增量并入新新闻' : '正在用 Haiku 总结';
  var detail = nItems ? (' ' + nItems + ' 条') : '';
  if (sub) sub.textContent = label + detail + ' · ' + Math.round(elapsed || 0) + 's';
  bd.innerHTML =
    '<div style="padding:26px 8px;text-align:center">'
    + '<div style="color:#8b949e;font-size:13px;margin-bottom:12px">⏳ ' + label + detail + '…</div>'
    + '<div style="height:8px;border-radius:5px;background:#21262d;overflow:hidden">'
    + '<div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#6e40c9,#a371f7);border-radius:5px;transition:width .6s ease"></div>'
    + '</div>'
    + '<div style="color:#6e7681;font-size:11px;margin-top:10px">一次性 API 调用(非逐票)，通常 30-60s · 可关窗口，后台继续</div>'
    + '</div>';
}
function _dgAiFoot(newN) {
  var foot = '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid #21262d">';
  if (newN > 0 && !_dgAiCtx.scope) foot += '<button id="dgAiIncr" style="background:#238636;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">🔄 更新 (+' + newN + ' 条新)</button>';
  foot += '<button id="dgAiFull" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">♻️ 重新总结</button>';
  foot += '<span style="color:#6e7681;font-size:11px">' + (newN > 0 ? ('有 ' + newN + ' 条新新闻未并入') : '') + '</span></div>';
  return foot;
}
function _dgAiWireFoot() {
  var bi = document.getElementById('dgAiIncr'); if (bi) bi.onclick = function () { _dgAiRun('incremental'); };
  var bf = document.getElementById('dgAiFull'); if (bf) bf.onclick = function () { _dgAiRun('full'); };
}
function _dgAiRenderDone(d) {
  var sub = document.getElementById('dgAiSub');
  var bd = document.getElementById('dgAiBody');
  if (_dgAiPollTimer) { clearTimeout(_dgAiPollTimer); _dgAiPollTimer = null; }
  var cachedIds = {}; (d.ids || []).forEach(function (x) { cachedIds[x] = 1; });
  var cur = _dgAiCollect();
  var newN = 0; for (var i = 0; i < cur.length; i++) { if (cur[i].id && !cachedIds[cur[i].id]) newN++; }
  var when = (d.ts || '').replace('T', ' ').slice(5, 16);
  if (sub) sub.textContent = (d.n || 0) + ' 条 · ' + _dgAiEsc(d.model || '') + (when ? (' · ' + when) : '');
  bd.innerHTML = _dgAiMd(d.summary || '(空)') + _dgAiFoot(newN);
  _dgAiWireFoot();
}
function _dgAiModal() {
  var ov = document.getElementById('dgAiOverlay');
  if (ov) return ov;
  ov = document.createElement('div');
  ov.id = 'dgAiOverlay';
  ov.style.cssText = 'display:none;position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,0.75);align-items:flex-start;justify-content:center';
  ov.onclick = function (e) { if (e.target === ov) _dgAiClose(); };
  ov.innerHTML = '<div style="background:#161b22;border:1px solid #30363d;border-radius:12px;max-width:760px;width:92%;max-height:84vh;margin-top:6vh;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.6)">'
    + '<div style="display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid #21262d">'
    + '<span style="font-size:16px;font-weight:700;color:#e6edf3">🤖 AI 新闻总结</span>'
    + '<span id="dgAiSub" style="font-size:11px;color:#8b949e"></span>'
    + '<button onclick="_dgAiClose()" style="margin-left:auto;background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer;line-height:1;padding:0 4px">&times;</button>'
    + '</div>'
    + '<div id="dgAiBody" style="padding:16px 20px;overflow-y:auto;font-size:13px"></div>';
  document.body.appendChild(ov);
  return ov;
}
function _dgAiClose() { var ov = document.getElementById('dgAiOverlay'); if (ov) ov.style.display = 'none'; if (_dgAiPollTimer) { clearTimeout(_dgAiPollTimer); _dgAiPollTimer = null; } }
function _dgAiEsc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function _dgAiMd(md) {
  var lines = String(md || '').replace(/\\r/g, '').split(String.fromCharCode(10));
  var html = '', inList = false;
  function closeList() { if (inList) { html += '</ul>'; inList = false; } }
  function inline(t) {
    t = _dgAiEsc(t);
    t = t.replace(/\\*\\*(.+?)\\*\\*/g, '<b style="color:#e6edf3">$1</b>');
    t = t.replace(/(\\$[A-Za-z]{1,6})\\b/g, '<span style="color:#58a6ff;font-weight:600">$1</span>');
    t = t.replace(/利好/g, '<span style="color:#3fb950">利好</span>');
    t = t.replace(/利空/g, '<span style="color:#f85149">利空</span>');
    return t;
  }
  for (var i = 0; i < lines.length; i++) {
    var raw = lines[i].trim();
    if (!raw) { closeList(); continue; }
    var m;
    if (m = raw.match(/^>\\s?(.*)$/)) { closeList(); html += '<div style="border-left:3px solid #58a6ff;padding:7px 11px;margin:0 0 12px;background:#0d1117;border-radius:5px;color:#9cc3ff;font-size:13px;line-height:1.5">' + inline(m[1]) + '</div>'; continue; }
    if (m = raw.match(/^#{1,6}\\s+(.*)$/)) { closeList(); html += '<div style="font-weight:700;color:#e6edf3;margin:14px 0 5px;font-size:14px">' + inline(m[1]) + '</div>'; continue; }
    if (m = raw.match(/^[-*]\\s+(.*)$/)) { if (!inList) { html += '<ul style="margin:2px 0 9px;padding-left:18px">'; inList = true; } html += '<li style="margin:3px 0;color:#c9d1d9;line-height:1.55">' + inline(m[1]) + '</li>'; continue; }
    if (/^\\*\\*.+\\*\\*$/.test(raw)) { closeList(); html += '<div style="font-weight:700;color:#e6edf3;margin:14px 0 5px;font-size:14px">' + inline(raw.replace(/^\\*\\*|\\*\\*$/g, '')) + '</div>'; continue; }
    closeList(); html += '<div style="margin:4px 0;color:#c9d1d9;line-height:1.6">' + inline(raw) + '</div>';
  }
  closeList();
  return html;
}

// 本日筛选：对当前显示的列表(今天/某天/搜索结果)客户端过滤，可清除
function digestRenderActive(resetScroll) {
  const host = document.getElementById(_digestActive.host); if (!host) return;
  // ✦finviz AI 开 → 用库里 active 日期的 AI 条目当数据源(惰性载入对应日期, _digestFvaiLoading 防重入)
  if (_digestInFilter.fvai) {
    const wantDate = _digestActive.date || '';
    if (_digestFvaiDate !== wantDate) {
      if (!_digestFvaiLoading) {
        _digestFvaiLoading = true;
        fetch('/api/digest/finviz_ai?_t=' + Date.now() + (wantDate ? ('&date=' + encodeURIComponent(wantDate)) : ''))
          .then(function(r) { return r.json(); }).then(function(d) {
            _digestFvaiItems = d.items || []; _digestFvaiDate = wantDate; _digestFvaiLoading = false;
            digestRenderActive(resetScroll);
          }).catch(function() { _digestFvaiItems = []; _digestFvaiDate = wantDate; _digestFvaiLoading = false; digestRenderActive(resetScroll); });
      }
      _digestSetFvaiChip();
      host.innerHTML = '<div style="color:#8b949e;padding:30px;text-align:center">加载 finviz AI…</div>';
      return;
    }
  }
  const all = _digestInFilter.fvai ? (_digestFvaiItems || []) : (_digestActive.items || []);
  const ch = (_digestInFilter.channel || '').toLowerCase();
  const tk = (_digestInFilter.ticker || '').toUpperCase().replace(/^\\$/, '');
  const sv = _digestInFilter.sentiment || '';
  const mrOn = !!_digestInFilter.mustread;
  let items = _digestApplySourceFilter(all);   // 4 家来源 chip:Benzinga/WSJ/Barron's/MarketWatch
  // 必读筛选:双语条目豁免(双语本身就是策展精选, 无 mr 属性)
  if (mrOn) items = items.filter(function(it) { return it._bilingual || it.mr === 1; });
  // 默认只看清单内 ticker 的新闻:双语条目豁免(双语无 ticker 语义, 卡不进清单)
  if (_digestWatchlistOn && !_digestInFilter.fvai && _digestWatchlist && _digestWatchlist.size) {
    items = items.filter(function(it) { return it._bilingual || (it.tickers || []).some(function(t) { return _digestWatchlist.has(String(t).toUpperCase().replace(/^\\$/, '')); }); });
  }
  // ✨ 去噪: 默认过滤掉 mustread.is_noise 判命中的模板噪音 (双语豁免, 因双语已策展)
  if (_digestDenoiseOn) items = items.filter(function(it) { return it._bilingual || !it.noise; });
  // 「✓ 标已读」隐藏态:把已 mark 的条目过滤掉(仅今天 tab; 跨天会在 _digestUpdateReadBtns 里自动重置)
  if (_digestHideRead && _digestActive.host === 'digestTodayList' && _digestReadIds && _digestReadIds.size) {
    items = items.filter(function(it) { return !(it && it.id != null && _digestReadIds.has(String(it.id))); });
  }
  if (ch) items = items.filter(function(it) { return (it.channels || []).some(function(c) { return String(c).toLowerCase().indexOf(ch) >= 0; }); });
  if (tk) items = items.filter(function(it) { return (it.tickers || []).some(function(t) { return String(t).toUpperCase().replace(/^\\$/, '') === tk; }); });
  if (sv) items = items.filter(function(it) {
    const s = parseInt(it.sentiment, 10);
    if (isNaN(s)) return false;
    return sv === 'pos' ? s > 0 : sv === 'neg' ? s < 0 : sv === 'neu' ? s === 0 : s !== 0;  // any=非中性
  });
  // 高亮激活的情绪 + 分类 + 来源按钮
  document.querySelectorAll('#digestInDayBar .dg-sentf').forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-sv') === sv); });
  var _mrBtn = document.querySelector('#digestInDayBar .dg-mustread'); if (_mrBtn) _mrBtn.classList.toggle('active', mrOn);
  // 分类下拉同步选中态 (chip 版已删)
  var _dgCS = document.getElementById('dgChanSel'); if (_dgCS) _dgCS.value = _digestInFilter.channel || '';
  _digestSetFvaiChip();
  _digestUpdateReadBtns();
  window._digestLastRendered = items;   // 给「🤖 AI总结」+「✓ 标已读」用当前过滤后的可见列表
  renderDigestList(items, host, _digestActive.showDate, _digestActive.date);
  // 今天视图的 status 栏跟着筛选动态更新: 按 _date 分组重算今/昨条数, 格式
  // "date · X/T今 + Y/S昨未读 · 更新于 ..." (总数放斜杠后, 一眼看到过滤强度)
  if (_digestActive.host === 'digestTodayList') {
    var _st = document.getElementById('digestTodayStatus');
    if (_st && _digestActive.totalToday != null) {
      var _todayDate = _digestActive.date || '';
      var _yStr = _digestActive.yestDate || '';
      var _nT = 0, _nY = 0;
      for (var _i = 0; _i < items.length; _i++) {
        var _dt = items[_i]._date;
        if (_dt === _todayDate) _nT++;
        else if (_dt === _yStr) _nY++;
      }
      var _tT = _digestActive.totalToday, _tY = _digestActive.totalYest;
      _st.textContent = _todayDate + ' · '
        + _nT + (_nT !== _tT ? ('/' + _tT) : '') + ' 今'
        + (_tY ? (' + ' + _nY + (_nY !== _tY ? ('/' + _tY) : '') + ' 昨未读') : '')
        + ' · 更新于 ' + (_digestActive.generatedAt || '?');
    }
  }
  _digestUpdateOpenBtn();   // 按钮 label 反映当前 selection
  const info = document.getElementById('digestInDayInfo');
  const svlbl = {pos: '看多', neg: '看空', neu: '中性', any: '有情绪'}[sv] || '';
  if (info) info.textContent = (_digestInFilter.fvai || ch || tk || sv || mrOn)
    ? ((_digestInFilter.fvai ? '✦finviz AI ' : '') + (mrOn ? '⭐必读 ' : '') + (_digestInFilter.channel ? ('分类「' + _digestInFilter.channel + '」') : '') + (tk ? (' ' + tk) : '') + (svlbl ? (' ' + svlbl) : '') + ' — ' + items.length + '/' + all.length)
    : '';
  // User-initiated filter → jump to the TOP of the results. (Without this the
  // shorter filtered list lets the browser clamp scrollTop, leaving you parked
  // at the bottom — looked like "jumping to some news".) Background/SSE
  // refresh calls this without resetScroll so it keeps your reading position.
  if (resetScroll) { const sc = document.getElementById('tab-digest'); if (sc) sc.scrollTop = 0; }
}
// ⚠️ 与 digestFvai 的互斥必须是**双向**的。finviz AI 的条目 channels 恒为
//    ['✦AI'] 且根本没有 mr 字段(实测 69/69 都没有), 所以它和「分类 chip」「⭐必读」
//    任一同时打开, 列表都会被过滤成 0 条、显示「无内容」。digestFvai 原本只在
//    **开 finviz AI 时**清掉这俩 —— 反过来先开 finviz AI 再点必读/分类就漏掉了,
//    实测就是 0/70 的空列表。
function digestChan(name) {
  // select 版本: 直接 set (空值=清空); 之前 chip 版本是 toggle, 保留传相同 name
  // 二次点击也能清 (等同 select 选空 option)。
  const cur = _digestInFilter.channel || '';
  _digestInFilter.channel = (arguments.length && name === cur) ? '' : (name || '');
  if (_digestInFilter.channel) _digestInFilter.fvai = false;
  digestRenderActive(true);
}
function digestInTicker() { const el = document.getElementById('digestInTk'); _digestInFilter.ticker = el ? el.value.trim() : ''; digestRenderActive(true); }
function digestSent(v) { _digestInFilter.sentiment = (_digestInFilter.sentiment === v) ? '' : v; digestRenderActive(true); }
function digestMustRead() {
  _digestInFilter.mustread = !_digestInFilter.mustread;
  if (_digestInFilter.mustread) _digestInFilter.fvai = false;   // 见 digestChan 上方注释
  digestRenderActive(true);
}
function digestInClear() {
  _digestInFilter = { channel: '', ticker: '', sentiment: '', mustread: false, fvai: false };
  const el = document.getElementById('digestInTk'); if (el) el.value = '';
  digestRenderActive(true);
}
function _dfVal(id) { const el = document.getElementById(id); return el ? el.value.trim() : ''; }
function digestPreset(days) {
  const end = (_digestDates && _digestDates.length) ? _digestDates[0]
            : new Date().toISOString().slice(0, 10);
  const e = new Date(end + 'T00:00:00');
  const s = new Date(e); s.setDate(s.getDate() - (days - 1));
  const fmt = function(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
                                 + '-' + String(d.getDate()).padStart(2, '0'); };
  const se = document.getElementById('dfStart'); if (se) se.value = fmt(s);
  const ee = document.getElementById('dfEnd'); if (ee) ee.value = end;
  digestSearch();
}
function digestSearch() {
  const content = document.getElementById('digestContent');
  if (!content) return;
  const start = _dfVal('dfStart'), end = _dfVal('dfEnd'), ticker = _dfVal('dfTicker'),
        title = _dfVal('dfTitle'), body = _dfVal('dfBody'), channel = _dfVal('dfChannel');
  if (!start && !end && !ticker && !title && !body && !channel) { digestClearFilter(); return; }
  content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">搜索中…</div>';
  const q = [];
  if (start) q.push('start=' + encodeURIComponent(start));
  if (end) q.push('end=' + encodeURIComponent(end));
  if (ticker) q.push('ticker=' + encodeURIComponent(ticker));
  if (title) q.push('title=' + encodeURIComponent(title));
  if (body) q.push('body=' + encodeURIComponent(body));
  if (channel) q.push('channel=' + encodeURIComponent(channel));
  fetch('/api/digest/search?' + q.join('&')).then(function(r) { return r.json(); }).then(function(d) {
    if (d.error) { content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">📭 ' + d.error + '</div>'; return; }
    _digestActive = { items: d.items || [], host: 'digestContent', date: '', showDate: true };
    _digestCalActive = _digestActive;                                    // 存搜索结果快照(切回日历恢复)
    _digestInFilter = { channel: '', ticker: '', sentiment: '', mustread: false, fvai: false };
    const itk1 = document.getElementById('digestInTk'); if (itk1) itk1.value = '';
    digestRenderActive();
    const meta = document.getElementById('digestMeta');
    if (meta) meta.textContent = (d.count != null ? (d.count + ' 条匹配') : '')
      + (d.truncated ? '（已截断，缩小区间）' : '') + '  ·  扫描 ' + (d.scanned_days || 0) + ' 天';
  }).catch(function(e) { content.innerHTML = '<div style="color:#f85149;padding:40px">搜索失败：' + e + '</div>'; });
}
function digestClearFilter() {
  ['dfStart', 'dfEnd', 'dfTicker', 'dfTitle', 'dfBody', 'dfChannel'].forEach(function(id) {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  digestLoad('');  // 回到最新单日
}
let _digestDateTimer = null;
// 手打日期专用: 防抖加载。原生 date input 在 Chrome 里每改一段(complete value 变化)就 fire 一次 change,
// 直接 digestLoad 会边打边重载 + 回写 value 把输入打断。这里等作者停手 350ms 再真正加载;
// 前/后一天、刷新、日历点选仍直接调 digestLoad,保持即时响应。
function digestDatePick(v) {
  if (_digestDateTimer) clearTimeout(_digestDateTimer);
  _digestDateTimer = setTimeout(function() { _digestDateTimer = null; digestLoad(v); }, 350);
}
function digestLoad(date, force) {
  if (_digestDateTimer) { clearTimeout(_digestDateTimer); _digestDateTimer = null; }
  const content = document.getElementById('digestContent');
  const meta = document.getElementById('digestMeta');
  if (!content) return;
  content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">加载中…</div>';
  let url = '/api/digest/day';
  const q = [];
  if (date) q.push('date=' + encodeURIComponent(date));
  if (force) q.push('_t=' + Date.now());
  if (q.length) url += '?' + q.join('&');
  fetch(url).then(function(r) { return r.json(); }).then(function(d) {
    // benzinga 该日无数据也不 early return —— 双语可能有,继续拉。
    // 只有真错误 (非 "no digest" 类) 才提示。目前 d.error 里主要就是「无 digest」,一律降级到「只显双语」。
    const noBnz = !!d.error;
    _digestDates = d.dates || [];
    const dp = document.getElementById('digestDate');
    // 作者正在框里手打日期时, 绝不碰这个 input —— value / min / max 都不要动。
    // 回写 value 会清掉正在输入的段(月/日只剩"1"+跳段);
    // 重设 min/max 会打断原生 date input 的「年份4位累积」(敲 2025 中途被冲成 0002)。
    // 只在焦点不在框上(按钮翻页 / 日历点选 / 初次加载)时才同步 input 状态。
    if (dp && _digestDates.length && document.activeElement !== dp) {
      dp.min = _digestDates[_digestDates.length - 1];
      dp.max = _digestDates[0];
      dp.value = d.date;
    }
    const bnzItems = d.items || [];
    // benzinga 无数据时 d.date 可能空,fallback 到作者请求日期
    const dateKey = d.date || date || new Date().toISOString().slice(0, 10);
    // 并入双语条目(按 published date 匹配)
    _digestBlForDate(dateKey, !!force).then(function(blItems) {
      const merged = bnzItems.concat(blItems).sort(function(a, b) { return _digestTsMs(b.ts) - _digestTsMs(a.ts); });
      if (!merged.length) {
        content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">📭 该日无 benzinga 也无双语</div>';
        if (meta) meta.textContent = dateKey; return;
      }
      if (meta) meta.textContent = zt(merged.length + ' 条 · ' + dateKey
        + (blItems.length ? (' (含 ' + blItems.length + ' 双语' + (noBnz ? ',benzinga 无' : '') + ')') : '')
        + (_digestDates.length > 1 ? ('  ·  共 ' + _digestDates.length + ' 天') : ''),
        merged.length + ' stories · ' + dateKey
        + (blItems.length ? (' (' + blItems.length + ' bilingual' + (noBnz ? ', none from Benzinga' : '') + ')') : '')
        + (_digestDates.length > 1 ? ('  ·  ' + _digestDates.length + ' days in all') : ''));
      _digestDayItems = merged;
      _digestDayDate = dateKey;
      _digestActive = { items: _digestDayItems, host: 'digestContent', date: dateKey, showDate: false };
      _digestCalActive = _digestActive;                                    // 存日历快照(切回日历恢复)
      _digestInFilter = { channel: '', ticker: '', sentiment: '', mustread: false, fvai: false };        // 新的一天，清掉本日筛选
      const itk0 = document.getElementById('digestInTk'); if (itk0) itk0.value = '';
      digestRenderActive();
      if (!noBnz) digestTranslateTitles(dateKey, 'digestContent');   // 标题自动谷歌翻译(仅 benzinga 走这条路)
      if (dateKey) _digestCalMonth = dateKey.slice(0, 7);
      if (Object.keys(_digestCounts).length) renderDigestCalendar();
    });
  }).catch(function(e) { content.innerHTML = '<div style="color:#f85149;padding:40px">加载失败：' + e + '</div>'; });
}
function digestStep(delta) {
  // _digestDates is newest-first. delta=+1 → newer, -1 → older.
  if (!_digestDates.length) return;
  const cur = (document.getElementById('digestDate') || {}).value || _digestDates[0];
  let i = _digestDates.indexOf(cur);
  if (i < 0) i = 0;
  const ni = i - delta;
  if (ni < 0 || ni >= _digestDates.length) return;
  digestLoad(_digestDates[ni]);
}
function digestLoadCalendar() {
  fetch('/api/digest/calendar').then(r => r.json()).then(d => {
    _digestCounts = d.counts || {};
    if (!_digestCalMonth) {
      const days = Object.keys(_digestCounts).sort();
      _digestCalMonth = days.length ? days[days.length - 1].slice(0, 7)
                                    : new Date().toISOString().slice(0, 7);
    }
    // 只有 benzinga counts(四家双语源已删); 直接渲染。
    // ⚠️ 曾漏删一行 _digestBuildBlCounts() 调用 → 函数已删, 一进日历 tab 就
    //    ReferenceError 崩掉整个日历。已移除。
    renderDigestCalendar();
  }).catch(() => {});
}
function digestCalNav(delta) {
  if (!_digestCalMonth) return;
  const y = +_digestCalMonth.slice(0, 4), m = +_digestCalMonth.slice(5, 7);
  const d = new Date(y, m - 1 + delta, 1);
  _digestCalMonth = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  renderDigestCalendar();
}
function renderDigestCalendar() {
  const host = document.getElementById('digestCal');
  if (!host || !_digestCalMonth) return;
  const y = +_digestCalMonth.slice(0, 4), m = +_digestCalMonth.slice(5, 7);
  const startDow = new Date(y, m - 1, 1).getDay();
  const dim = new Date(y, m, 0).getDate();
  const cur = (document.getElementById('digestDate') || {}).value || '';
  // 综合 counts 用来定 badge 强度(每次 render 按当前 filter 重算)
  const _allDates = new Set([...Object.keys(_digestCounts || {}), ...Object.keys(_digestBlCountsByDate || {})]);
  const _mergedCounts = {}; for (const dt of _allDates) _mergedCounts[dt] = _digestDayCount(dt);
  const maxC = Math.max(1, ...Object.values(_mergedCounts));
  let h = '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">'
    + '<button class="back-btn" onclick="digestCalNav(-12)" title="上一年">&#171;</button>'
    + '<button class="back-btn" onclick="digestCalNav(-1)" title="上月">&#8249;</button>'
    + '<span style="font-weight:600;color:#e6edf3;min-width:72px;text-align:center">' + y + '-' + String(m).padStart(2, '0') + '</span>'
    + '<button class="back-btn" onclick="digestCalNav(1)" title="下月">&#8250;</button>'
    + '<button class="back-btn" onclick="digestCalNav(12)" title="下一年">&#187;</button>'
    + '<span style="font-size:11px;color:#8b949e;margin-left:4px">点某天看 · «»跳年 ‹›翻月</span></div>';
  h += '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;max-width:380px">';
  ['日', '一', '二', '三', '四', '五', '六'].forEach(w =>
    h += '<div style="text-align:center;font-size:10px;color:#6e7681;padding-bottom:2px">' + w + '</div>');
  for (let i = 0; i < startDow; i++) h += '<div></div>';
  for (let day = 1; day <= dim; day++) {
    const ds = y + '-' + String(m).padStart(2, '0') + '-' + String(day).padStart(2, '0');
    const c = _mergedCounts[ds] || 0;
    const has = c > 0, sel = ds === cur;
    const inten = has ? (0.16 + 0.5 * Math.sqrt(c / maxC)) : 0;
    const bg = has ? ('rgba(88,166,255,' + inten.toFixed(2) + ')') : 'transparent';
    const bd = sel ? '2px solid #58a6ff' : '1px solid #21262d';
    h += '<div ' + (has ? ('data-d="' + ds + '" class="digcal-day" ') : '')
      + 'title="' + (has ? (c + ' 条') : '无') + '" '
      + 'style="aspect-ratio:1;border:' + bd + ';border-radius:6px;background:' + bg
      + ';display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:12px;'
      + (has ? 'cursor:pointer;color:#e6edf3' : 'color:#484f58') + '">'
      + '<div>' + day + '</div>' + (has ? ('<div style="font-size:9px;color:#c9d1d9">' + c + '</div>') : '') + '</div>';
  }
  h += '</div>';
  host.innerHTML = h;
  host.querySelectorAll('.digcal-day').forEach(function(el){ el.onclick = function(){ digestLoad(el.getAttribute('data-d')); }; });
}

/* ==================== Benzinga tab ==================== */
let _bzSub = 'today';
let _evSub = 'cal';   // Events 当前子 tab —— switchTopTab 回来时恢复它, 别无条件跳回日历
let _bzData = null;

// ===== Tab 重构(2026-06-04): Benzinga 合并进 Digest / Events =====
// 用 re-parent 把现有面板 DOM 搬进新容器(一次性, 幂等)。
function reorgTabs() {
  if (window.__reorgDone) return;
  window.__reorgDone = true;
  // Pre-market group(Benzinga Today/History)→ Digest "Pre-market"
  const pm = document.getElementById('bzPremarketGroup'), pmHost = document.getElementById('digestPanePremarket');
  if (pm && pmHost && pm.parentNode !== pmHost) pmHost.appendChild(pm);
  // Market Events → Events
  const me = document.getElementById('bzMarketEvents'), meHost = document.getElementById('evMarketEvents');
  if (me && meHost && me.parentNode !== meHost) { meHost.appendChild(me); me.style.display = 'block'; }
  // 2026-07-30: mbrief 3 sub → 🎯 选股 (顶栏 mbrief button 已隐藏)
  // The header (🔄 Re-analyze, date) and the brief / rotation / options sub-tabs move too:
  // left behind in the hidden tab, the button the empty state points to could not be clicked.
  const mbHost = document.getElementById('hmSubMbriefHost');
  if (mbHost) {
    ['mbHeader', 'mbSubNav', 'mbBody', 'mbRotPane', 'mbOptPane'].forEach(id => {
      const e = document.getElementById(id);
      if (e && e.parentNode !== mbHost) mbHost.appendChild(e);
    });
  }
}
let __pmInited = false;

function pmInit() {
  reorgTabs();
  if (!__pmInited) { __pmInited = true; if (typeof bzSubTab === 'function') bzSubTab('today'); }
}
// Events 子导航统一入口: cal/sec 委托 SEC 模块的 evSubTab; me_* 显示 Market Events 并切到对应类
function evNav(name) {
  reorgTabs();
  const nav = document.getElementById('evSubNav');
  if (nav) nav.querySelectorAll('button[data-ev]').forEach(function(b) {
    const a = (b.dataset.ev === name);
    b.style.background = a ? '#1f6feb' : 'transparent';
    b.style.color = a ? '#fff' : '#8b949e';
  });
  const me = document.getElementById('evMarketEvents');
  if (name === 'me_corp') {
    const cal = document.getElementById('evCalPane'); if (cal) cal.style.display = 'none';
    const sec = document.getElementById('evSecPane'); if (sec) sec.style.display = 'none';
    if (me) me.style.display = 'block';
    if (!window.__meStarted && typeof meCorpStart === 'function') { window.__meStarted = true; meCorpStart(); }
    else if (typeof meCorpStart === 'function') meCorpStart();
    document.querySelectorAll('#evMarketEvents .me-pill').forEach(function(b) { b.style.display = 'none'; });
  } else {
    if (me) me.style.display = 'none';
    if (name === 'cal' && typeof eventsLoad === 'function') eventsLoad();
    if (typeof evSubTab === 'function') evSubTab(name);
  }
  _evSub = name;   // 记住子 tab, 回到 Events 时不再无条件跳回财报日历
}

function bzSubTab(name) {
  _bzSub = name;
  document.querySelectorAll('.bz-sub-pill').forEach(b => {
    const active = b.dataset.bz === name;
    b.classList.toggle('active', active);
    b.style.background = active ? '#1f6feb' : '#21262d';
    b.style.color = active ? '#fff' : '#8b949e';
    b.style.border = active ? 'none' : '1px solid #30363d';
  });
  // Pre-market 只剩 Today / History 两个视图(Market Events/Signals/News 已迁出)
  document.getElementById('bzHistoryControls').style.display = (name === 'history') ? 'flex' : 'none';
  if (name === 'today') { bzLoad(); } else { bzLoadHistoryIndex(); }
}

function bzLoad() {
  // "Today" tab: latest row per channel. Chris commentary posts daily, OFR
  // posts weekly on Mondays — so a single-date lookup loses one channel on
  // most days. We ask the server for each channel's freshest row instead,
  // and render them side-by-side with their actual dates labeled.
  const content = document.getElementById('bzContent');
  const status = document.getElementById('bzStatus');
  const today = new Date();
  const todayIso = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
  status.textContent = 'Loading latest…';
  content.innerHTML = '<div style="color:#8b949e">Loading…</div>';
  fetch('/api/benzinga/latest-per-channel').then(r => r.json()).then(d => {
    const channels = d.channels || {};
    if (d.error || !Object.keys(channels).length) {
      content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">📭 No Benzinga data yet.<br><br>Click 🔄 Refresh to trigger the fetcher (or wait for the next cron run: Mon–Fri 8:30 / 9:00 AM CT).</div>';
      status.textContent = '';
      return;
    }
    // Render freshest channel first, summarize each channel's date in the status bar
    const entries = Object.entries(channels).sort((a, b) => (b[1].date || '').localeCompare(a[1].date || ''));
    const ordered = {};
    entries.forEach(([k, v]) => { ordered[k] = v; });
    status.textContent = entries.map(([k, v]) => {
      const label = v.label || k;
      const tag = v.date === todayIso ? ' (today)' : '';
      return `${label}: ${v.date || '?'}${tag}`;
    }).join(' · ');
    _bzData = d;
    bzRender(ordered);
  }).catch(e => {
    content.innerHTML = `<div style="color:#f85149">Error: ${e}</div>`;
  });
}

// Calendar state
let _bzAvailableDates = new Set();
let _bzCalView = null;   // {year, month} (month is 0-indexed)
let _bzSelectedDate = null;

async function bzLoadHistoryIndex() {
  const content = document.getElementById('bzContent');
  const info = document.getElementById('bzHistInfo');
  content.innerHTML = '<div style="color:#8b949e">Loading history index…</div>';
  try {
    const r = await fetch('/api/benzinga/dates');
    const d = await r.json();
    const dates = (d.dates || []);
    if (!dates.length) {
      content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">No history files yet. Run a fetch first.</div>';
      info.textContent = '';
      document.getElementById('bzCalendar').innerHTML = '';
      return;
    }
    _bzAvailableDates = new Set(dates);
    info.textContent = `${dates.length} day(s) on file`;
    // open calendar on the month of the newest date
    const newest = dates[dates.length - 1];
    const parts = newest.split('-').map(Number);
    _bzCalView = { year: parts[0], month: parts[1] - 1 };
    _bzSelectedDate = newest;
    bzRenderCalendar();
    bzLoadByDate(newest);
  } catch (e) {
    content.innerHTML = `<div style="color:#f85149">Error loading history: ${e}</div>`;
  }
}

function bzCalNav(delta) {
  if (!_bzCalView) return;
  if (delta === 0) {
    // "Today" — jump to current month
    const now = new Date();
    _bzCalView = { year: now.getFullYear(), month: now.getMonth() };
  } else {
    let m = _bzCalView.month + delta;
    let y = _bzCalView.year;
    if (m < 0) { m = 11; y--; }
    if (m > 11) { m = 0; y++; }
    _bzCalView = { year: y, month: m };
  }
  bzRenderCalendar();
}

function bzRenderCalendar() {
  if (!_bzCalView) return;
  const { year, month } = _bzCalView;
  const monthName = new Date(year, month, 1).toLocaleString('default', { month: 'long', year: 'numeric' });
  document.getElementById('bzCalMonthLabel').textContent = monthName;

  const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const cal = document.getElementById('bzCalendar');
  const cells = [];
  // header row
  dow.forEach(d => cells.push(`<div style="text-align:center;color:#8b949e;font-size:10px;font-weight:600;padding:4px 0">${d}</div>`));

  const first = new Date(year, month, 1);
  const lead = first.getDay();
  // blanks before day 1
  for (let i = 0; i < lead; i++) cells.push('<div></div>');

  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const todayISO = (() => { const t = new Date(); return `${t.getFullYear()}-${String(t.getMonth()+1).padStart(2,'0')}-${String(t.getDate()).padStart(2,'0')}`; })();

  for (let d = 1; d <= daysInMonth; d++) {
    const iso = `${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const hasContent = _bzAvailableDates.has(iso);
    const isToday = iso === todayISO;
    const isSelected = iso === _bzSelectedDate;
    const wDay = new Date(year, month, d).getDay();
    const isWeekend = wDay === 0 || wDay === 6;

    let bg = '#0d1117';
    let color = '#484f58';
    let border = '1px solid transparent';
    let cursor = 'default';
    let title = '';
    if (hasContent) {
      bg = isSelected ? '#1f6feb' : '#21262d';
      color = isSelected ? '#fff' : '#e6edf3';
      cursor = 'pointer';
      title = `Open ${iso}`;
    } else if (!isWeekend) {
      color = '#6e7681';
    }
    if (isToday) border = '1px solid #58a6ff';

    const onclick = hasContent ? `onclick="bzPickDate('${iso}')"` : '';
    cells.push(`
      <div ${onclick} title="${title}" style="
        text-align:center;padding:8px 2px;border-radius:4px;
        background:${bg};color:${color};border:${border};
        cursor:${cursor};font-size:12px;font-weight:${isToday?'700':'500'};
        transition:background 0.15s;
      "${hasContent ? ' onmouseover="this.style.background=\\''+(isSelected?'#388bfd':'#30363d')+'\\'" onmouseout="this.style.background=\\''+(isSelected?'#1f6feb':'#21262d')+'\\'"' : ''}>${d}</div>
    `);
  }

  cal.innerHTML = cells.join('');
}

function bzPickDate(iso) {
  _bzSelectedDate = iso;
  bzRenderCalendar();
  bzLoadByDate(iso);
}

async function bzLoadByDate(date) {
  if (!date) return;
  const content = document.getElementById('bzContent');
  const status = document.getElementById('bzStatus');
  content.innerHTML = '<div style="color:#8b949e">Loading ' + date + '…</div>';
  status.textContent = '';
  try {
    const r = await fetch('/api/benzinga/by-date/' + encodeURIComponent(date));
    const d = await r.json();
    if (d.error) {
      content.innerHTML = `<div style="color:#f85149">Error: ${d.error}</div>`;
      return;
    }
    const parts = date.split('-').map(Number);
    const dow = new Date(parts[0], parts[1]-1, parts[2]).toLocaleString('default', {weekday: 'long'});
    status.textContent = `History: ${date} (${dow})`;
    bzRender(d.channels);
  } catch (e) {
    content.innerHTML = `<div style="color:#f85149">Error: ${e}</div>`;
  }
}

/**
 * Parse a Benzinga chat dump into discrete messages.
 *
 * The DOM scrape produces blocks shaped like:
 *
 *     Chris Capre
 *     6:31AM
 *
 *     Apr 14 Market Commentary
 *     ...body...
 *
 *     Chris Capre
 *     8:12AM
 *
 *     ...next body...
 *
 * Returns [{author, time, body}, ...]. Anything before the first author/time
 * pair is treated as a header (e.g. "X Users Online") and dropped.
 */
function bzParseMessages(text) {
  if (!text) return [];
  // (regex/string built via constructors so backslash escapes survive the
  // outer Python template parsing intact)
  const lines = text.replace(new RegExp('\\r', 'g'), '').split(String.fromCharCode(10));
  const TIME_RE = /^[0-9]{1,2}:[0-9]{2} ?(AM|PM)$/i;
  const messages = [];
  let cur = null;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    const next = lines[i + 1] || '';
    // Detect "<author>" then "<time>" on the next line. Author must be a
    // short non-time string (<=40 chars, no leading digits, not blank).
    if (TIME_RE.test(next.trim()) && ln.trim().length > 0 && ln.trim().length <= 40
        && !/^[0-9]/.test(ln.trim()) && !TIME_RE.test(ln.trim())) {
      if (cur) messages.push(cur);
      cur = { author: ln.trim(), time: next.trim(), body: [] };
      i++; // consume the time line too
      continue;
    }
    if (cur) {
      cur.body.push(ln);
    }
    // else: header text before first message — discarded
  }
  if (cur) messages.push(cur);
  // collapse trailing blank lines in each body
  messages.forEach(m => {
    while (m.body.length && !m.body[m.body.length - 1].trim()) m.body.pop();
    while (m.body.length && !m.body[0].trim()) m.body.shift();
    m.body = m.body.join(String.fromCharCode(10));
  });
  return messages;
}

/** Render one message body to HTML with light formatting:
 *   - escape HTML
 *   - linkify URLs
 *   - $TICKER → muted-color chip
 *   - lines starting with key labels (SPY IM:, RES =, SUPP =) → highlight color
 */
function bzFormatBody(body) {
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Build regexes via RegExp() so embedded JS escapes survive Python's outer
  // template parsing (otherwise control chars leak into the JS source).
  const RE_MANY_NL    = new RegExp('\\n{3,}', 'g');
  const RE_URL        = new RegExp('(https?://[^ \\t<>"]+)', 'g');
  const RE_TICKER     = new RegExp('(^|[^A-Za-z])[$]?([A-Z]{2,5})(?=[ \\t])', 'g');
  const RE_KEY_LINE   = new RegExp('^(SPY IM|VIX|RES|SUPP|SPY|QQQ|IWM):[ \\t]*(.+)$', 'gm');
  const RE_RES_SUPP   = new RegExp('^(RES|SUPP)[ \\t]*=[ \\t]*(.+)$', 'gm');
  const RE_PARA_SPLIT = new RegExp('\\n\\n+');
  const RE_SINGLE_NL  = new RegExp('\\n', 'g');

  let out = esc(body).replace(RE_MANY_NL, '\\n\\n');
  out = out.replace(RE_URL,
    '<a href="$1" target="_blank" rel="noopener" style="color:#58a6ff;text-decoration:none">$1</a>');
  out = out.replace(RE_TICKER, (_, pre, t) =>
    `${pre}<span style="color:#79c0ff;font-weight:600">${t}</span>`);
  out = out.replace(RE_KEY_LINE,
    '<span style="color:#d2a8ff;font-weight:600">$1:</span> <span style="color:#ffa657">$2</span>');
  out = out.replace(RE_RES_SUPP,
    '<span style="color:#d2a8ff;font-weight:600">$1 =</span> <span style="color:#ffa657">$2</span>');
  return out.split(RE_PARA_SPLIT)
    .map(p => `<p style="margin:0 0 8px">${p.replace(RE_SINGLE_NL, '<br>')}</p>`)
    .join('');
}

function bzRender(channels) {
  const content = document.getElementById('bzContent');
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const sections = [];
  for (const [key, ch] of Object.entries(channels || {})) {
    if (ch.error) {
      sections.push(`<div style="margin-bottom:24px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px"><h3>${esc(ch.label || key)}</h3><div style="color:#f85149">Error: ${esc(ch.error)}</div></div>`);
      continue;
    }

    // Parse the raw text into message cards
    const messages = bzParseMessages(ch.text || '');
    let bodyHtml;
    if (messages.length === 0) {
      bodyHtml = `<div style="color:#8b949e;padding:14px;text-align:center">No messages parsed.</div>`;
    } else {
      bodyHtml = messages.map(m => `
        <div class="bz-msg" style="margin-bottom:14px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 12px;border-left:3px solid #1f6feb">
          <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
            <span style="font-weight:700;color:#e6edf3;font-size:13px">${esc(m.author)}</span>
            <span style="color:#8b949e;font-size:11px">${esc(m.time)}</span>
          </div>
          <div class="bz-translate" style="color:#c9d1d9;font-size:13px;line-height:1.6">${bzFormatBody(m.body)}</div>
        </div>
      `).join('');
    }

    // PDF section (if any) — keep collapsible, render as HTML paragraphs
    let pdfBlock = '';
    if (ch.latest_pdf && ch.latest_pdf.text) {
      pdfBlock = `
        <details style="margin-top:10px;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px" open>
          <summary style="cursor:pointer;color:#58a6ff;font-weight:600">📎 ${esc(ch.latest_pdf.name || 'PDF')} <span style="color:#8b949e;font-weight:400;font-size:11px">(${ch.latest_pdf.text_chars.toLocaleString()} chars)</span></summary>
          <div class="bz-translate" style="margin-top:10px;color:#e6edf3;font-size:12.5px;line-height:1.7;max-height:600px;overflow-y:auto;padding:8px 4px">
            ${bzFormatBody(ch.latest_pdf.text)}
          </div>
        </details>`;
    }

    sections.push(`
      <div style="margin-bottom:24px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px">
        <h3 style="margin:0 0 12px;color:#e6edf3;font-size:14px;display:flex;align-items:center;gap:6px">
          <span>#</span>
          <span>${esc(ch.label || key)}</span>
          ${ch.date ? `<span style="color:#58a6ff;font-weight:600;font-size:11px;background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:1px 8px">${esc(ch.date)}</span>` : ''}
          <span style="color:#8b949e;font-weight:400;font-size:11px">${messages.length} message${messages.length === 1 ? '' : 's'}</span>
        </h3>
        ${bodyHtml}
        ${pdfBlock}
      </div>
    `);
  }
  if (!sections.length) {
    content.innerHTML = '<div style="color:#8b949e;padding:40px;text-align:center">No content for this day.</div>';
    return;
  }
  content.innerHTML = sections.join('');

  // 自动翻译 pre-market message body (中文段插到英文 <p> 后面)
  bzAutoTranslate().catch(function(e) { console.warn('[bz autotr] fail:', e); });

  // mount Google Translate widget once
  if (!window._bzTransLoaded) {
    window.googleTranslateElementInit = function() {
      try {
        new google.translate.TranslateElement({
          pageLanguage: 'en',
          includedLanguages: 'zh-CN,zh-TW,en',
          layout: google.translate.TranslateElement.InlineLayout.SIMPLE,
          autoDisplay: false,
        }, 'google_translate_element');
      } catch(e) { console.warn('Translate init:', e); }
    };
    const s = document.createElement('script');
    s.src = 'https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit';
    s.async = true;
    document.body.appendChild(s);
    window._bzTransLoaded = true;
  }
}

// 自动翻译 pre-market message body — 收集每个 .bz-translate 里的 <p> 段落,
// 批量走 /api/benzinga/translate,回来把中文段插到英文 p 后面(灰色小段,类似 digest 对照模式)。
// sessionStorage 按段 hash 缓存,切 tab 复用。
function _bzHash(s) {
  let h = 5381; s = String(s);
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h.toString(16);
}
function _bzInsertCn(pEl, cn) {
  if (!cn) return;
  // 已插过就跳过(重复渲染保护)
  const nx = pEl.nextElementSibling;
  if (nx && nx.classList && nx.classList.contains('bz-cn')) return;
  const div = document.createElement('div');
  div.className = 'bz-cn';
  div.textContent = cn;
  div.style.cssText = 'color:#8b949e;font-size:12.5px;margin:2px 0 10px;padding:3px 0 3px 10px;border-left:2px solid #21262d;line-height:1.55';
  pEl.insertAdjacentElement('afterend', div);
}
async function bzAutoTranslate() {
  const cells = document.querySelectorAll('#bzContent .bz-translate:not([data-tr-done])');
  if (!cells.length) return;
  const jobs = [];
  cells.forEach(function(cell) {
    cell.setAttribute('data-tr-done', 'pending');
    cell.querySelectorAll(':scope > p').forEach(function(p) {
      const en = (p.textContent || '').trim();
      if (!en || en.length < 4) return;
      // 全 ASCII 且看着像纯数字/符号的跳过
      if (/^[\\s\\d\\W]+$/.test(en)) return;
      const key = 'pmtr2_' + _bzHash(en);
      try {
        const cached = sessionStorage.getItem(key);
        if (cached) { _bzInsertCn(p, cached); return; }
      } catch(e) {}
      jobs.push({ p: p, en: en, key: key });
    });
  });
  if (!jobs.length) { cells.forEach(function(c){ c.setAttribute('data-tr-done','yes'); }); return; }
  // 后端一次翻译,若返回等长则一一对应
  try {
    const r = await fetch('/api/benzinga/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: jobs.map(function(j){ return j.en; }) })
    });
    const d = await r.json();
    const cn = d.cn || [];
    jobs.forEach(function(j, i) {
      const zh = (cn[i] || '').trim();
      if (!zh || zh === j.en) return;
      try { sessionStorage.setItem(j.key, zh); } catch(e) {}
      _bzInsertCn(j.p, zh);
    });
  } catch (e) {
    console.warn('[bz-tr] failed:', e);
  } finally {
    cells.forEach(function(c){ c.setAttribute('data-tr-done','yes'); });
  }
}

async function bzRefresh() {
  const btn = document.getElementById('bzRefreshBtn');
  const status = document.getElementById('bzStatus');
  btn.disabled = true;
  status.textContent = 'Refreshing — opens a headless browser, takes ~60s…';
  try {
    await fetch('/api/benzinga/refresh', {method:'POST'});
    // poll latest every 5s for up to 2 minutes
    let tries = 0;
    const prevAt = _bzData?.fetched_at;
    const poll = setInterval(async () => {
      tries++;
      try {
        const r = await fetch('/api/benzinga/latest');
        const d = await r.json();
        if (d && !d.empty && d.fetched_at !== prevAt) {
          clearInterval(poll);
          btn.disabled = false;
          _bzData = d;
          if (_bzSub === 'today') bzLoad(); else bzLoadHistoryIndex();
        }
      } catch(e) {}
      if (tries > 24) { clearInterval(poll); btn.disabled = false; status.textContent = 'Refresh timed out — check server logs.'; }
    }, 5000);
  } catch (e) {
    btn.disabled = false;
    status.textContent = 'Refresh failed: ' + e;
  }
}

// Sub-tab nav inside the Trade Journal. First 4 buttons drive the iframe's
// internal switchTab(); last 2 hide the iframe and reveal the Decision Log
// content (which used to live in a separate top-tab).
let _tjSub = 'overview';
const _TJ_IFRAME_TABS = ['overview', 'trades', 'calendar', 'alltrades', 'analysis'];
const _TJ_DL_TABS = ['decisions', 'ai'];

function tjSubTab(name) {
  // Legacy alias: 'stats' was its own pill but is now folded under 'analysis'.
  if (name === 'stats') name = 'analysis';
  if (!_TJ_IFRAME_TABS.includes(name) && !_TJ_DL_TABS.includes(name)) return;
  _tjSub = name;
  document.querySelectorAll('.tj-sub-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.tj === name));
  const jf = document.getElementById('journalFrame');
  const tb = document.getElementById('dl-toolbar');
  const list = document.getElementById('dl-list');
  const stats = document.getElementById('dl-stats-view');
  const filters = document.getElementById('dl-decisions-only-filters');
  const aiv = document.getElementById('tj-ai-view');
  list.style.display = 'none';
  stats.style.display = 'none';
  if (aiv) aiv.style.display = 'none';
  if (_TJ_IFRAME_TABS.includes(name)) {
    jf.style.display = 'block';
    tb.style.display = 'none';
    const parent = jf.parentElement;
    if (name === 'analysis') {
      // Single clean scroll: the iframe fills the pane and owns the only
      // scroll (like every other iframe tab). The old layout stacked a
      // server-rendered stats block (dl-stats-view) BELOW a fixed-height
      // iframe, which created a nested scroll that pinned the Monthly Goal
      // Tracker in place. That block is now hidden here; the Monthly Goal
      // Tracker + decision insights are rendered INSIDE the iframe's Analysis
      // tab (top panel) where they scroll normally.
      jf.style.flex = '1';
      jf.style.height = '';
      stats.style.display = 'none';
      parent.style.overflowY = '';
    } else {
      jf.style.flex = '1';
      jf.style.height = '';
      stats.style.flex = '1';
      parent.style.overflowY = '';
    }
    if (!journalLoaded) loadJournal();
    const callSwitch = () => {
      try {
        if (jf.contentWindow && typeof jf.contentWindow.switchTab === 'function') {
          jf.contentWindow.switchTab(name);
        }
      } catch (e) {}
    };
    if (jf.contentDocument && jf.contentDocument.readyState === 'complete') callSwitch();
    else jf.addEventListener('load', callSwitch, {once: true});
  } else if (name === 'ai') {
    // ✦ AI 复盘(非 iframe 面板)
    jf.style.display = 'none';
    tb.style.display = 'none';
    if (aiv) aiv.style.display = 'block';
    aiReviewLoad();   // 先读本地缓存(不调模型), 有就秒显
  } else {
    // decisions
    jf.style.display = 'none';
    tb.style.display = 'flex';
    list.style.display = 'block';
    filters.style.display = 'flex';
    dlLoad();
  }
}

// ✦ AI 交易复盘: 打开 tab 先读本地缓存(GET, 不调模型); 点按钮才重新生成(POST, ~15s)。
function aiReviewLoad() {
  const out = document.getElementById('aiReviewOut');
  const st = document.getElementById('aiReviewStatus');
  const btn = document.getElementById('aiReviewBtn');
  if (!out || out.getAttribute('data-loaded') === '1') return;   // 已显示过就别重复拉
  fetch('/api/journal/ai_review').then(r => r.json()).then(d => {
    if (d && d.cached && d.text) {
      out.setAttribute('data-loaded', '1');
      out.innerHTML = _aiFmt(d.text);
      st.textContent = '基于 ' + (d.n_closed || 0) + ' 笔 · ' + ((d.asof || '').replace('T', ' ').slice(0, 16)) + (d.stale ? ' · ⚠️有新平仓，点重新复盘' : ' · 已存本地');
      if (btn) btn.textContent = '🔄 重新复盘';
    } else if (d && !d.error) {
      st.textContent = '还没生成过 — 点按钮让 AI 复盘';
    }
  }).catch(() => {});
}
function aiReview() {
  const btn = document.getElementById('aiReviewBtn');
  const out = document.getElementById('aiReviewOut');
  const st = document.getElementById('aiReviewStatus');
  if (!btn) return;
  btn.disabled = true; btn.textContent = '分析中…';
  st.textContent = 'Haiku 正在复盘你的交易(约 15 秒)…';
  out.innerHTML = '';
  fetch('/api/journal/ai_review', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (d.error) { st.textContent = '失败: ' + d.error; }
      else {
        st.textContent = '基于 ' + (d.n_closed || 0) + ' 笔已平仓 · ' + ((d.asof || '').replace('T', ' ').slice(0, 16)) + ' · 已存本地';
        out.setAttribute('data-loaded', '1');
        out.innerHTML = _aiFmt(d.text || '');
      }
    })
    .catch(() => { st.textContent = '失败(网络)'; })
    .finally(() => { btn.disabled = false; btn.textContent = '🔄 重新复盘'; });
}
function _aiEsc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function _aiFmt(t) {
  return _aiEsc(t)
    .replace(/^#### (.*)$/gm, '<div style="color:#e6edf3;font-weight:700;margin:10px 0 4px">$1</div>')
    .replace(/^### (.*)$/gm, '<h4 style="color:#e6edf3;margin:12px 0 6px">$1</h4>')
    .replace(/^## (.*)$/gm, '<h3 style="color:#a371f7;margin:18px 0 8px;border-bottom:1px solid #21262d;padding-bottom:4px">$1</h3>')
    .replace(/\\*\\*(.+?)\\*\\*/g, '<b style="color:#e6edf3">$1</b>')
    .replace(/^\\s*[-•] (.*)$/gm, '<div style="margin:4px 0 4px 16px;text-indent:-12px">• $1</div>')
    .replace(/\\n{2,}/g, '<br><br>').replace(/\\n/g, '<br>');
}

// ─── 🤖 AI 持仓新闻分析 ─────────────────────────────────────────────────
let _posAiData = null;
function openPositionsAi() {
  let overlay = document.getElementById('posAiOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'posAiOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:5000;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center';
    overlay.innerHTML = `
      <div style="background:#0d1117;border:1px solid #30363d;border-radius:12px;width:min(720px,94vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 16px 48px rgba(0,0,0,.6)">
        <div style="display:flex;align-items:center;padding:14px 18px;border-bottom:1px solid #21262d;flex-wrap:wrap;gap:6px">
          <span style="font-size:15px;font-weight:700;color:#a371f7">&#129302; AI 持仓新闻分析</span>
          <span id="posAiStatus" style="font-size:12px;color:#8b949e"></span>
          <span style="margin-left:auto;display:flex;gap:8px">
            <button id="posAiRunBtn" onclick="posAiRun()" style="background:#6e40c9;color:#fff;border:none;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700">&#9654; 开始分析</button>
            <button onclick="document.getElementById('posAiOverlay').style.display='none'" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px">关闭</button>
          </span>
        </div>
        <div id="posAiFilters" style="display:none;padding:8px 18px;border-bottom:1px solid #21262d;gap:6px;flex-wrap:wrap"></div>
        <div id="posAiBody" style="flex:1;overflow-y:auto;padding:12px 18px;font-size:13px;color:#e6edf3"></div>
      </div>`;
    document.body.appendChild(overlay);
  }
  overlay.style.display = 'flex';
  posAiLoad();
}
function posAiLoad() {
  const body = document.getElementById('posAiBody');
  const st = document.getElementById('posAiStatus');
  if (!body) return;
  body.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">加载中…</div>';
  document.getElementById('posAiFilters').style.display = 'none';
  fetch('/api/positions/ai_analysis').then(r => r.json()).then(d => {
    if (d.status === 'running') {
      st.textContent = '分析中…';
      body.innerHTML = '<div style="color:#d29922;padding:20px;text-align:center">&#9203; AI 正在并行分析每个持仓的新闻面…<br><span style="font-size:11px;color:#8b949e">通常 30-60 秒, 每个 ticker 一个 agent</span></div>';
      setTimeout(posAiPoll, 3000);
      return;
    }
    if (d.status === 'done' || d.status === 'cached') {
      _posAiRender(d, d.status === 'cached');
    } else {
      st.textContent = '';
      body.innerHTML = '<div style="color:#8b949e;padding:30px;text-align:center">点「开始分析」, AI 会读取每个持仓的多空新闻,<br>判断是否有重大催化需要行动。<br><br><span style="font-size:11px">Haiku 模型, 走订阅, 不花 API 额度。</span></div>';
    }
  }).catch(() => { body.innerHTML = '<div style="color:#f85149">加载失败</div>'; });
}
function posAiRun() {
  const btn = document.getElementById('posAiRunBtn');
  const body = document.getElementById('posAiBody');
  const st = document.getElementById('posAiStatus');
  if (btn) { btn.disabled = true; btn.textContent = '分析中…'; }
  if (st) st.textContent = '启动并行 agents…';
  if (body) body.innerHTML = '<div style="color:#d29922;padding:20px;text-align:center">&#9203; 启动中…</div>';
  document.getElementById('posAiFilters').style.display = 'none';
  fetch('/api/positions/ai_analysis', { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.status === 'running') {
      if (st) st.textContent = '分析中…(并行 Haiku agents)';
      if (body) body.innerHTML = '<div style="color:#d29922;padding:20px;text-align:center">&#9203; AI 正在并行分析每个持仓…<br><span style="font-size:11px;color:#8b949e">通常 30-60 秒</span></div>';
      setTimeout(posAiPoll, 3000);
    } else if (d.error) {
      if (st) st.textContent = '失败';
      if (body) body.innerHTML = '<div style="color:#f85149;padding:20px">' + _aiEsc(d.error) + '</div>';
      if (btn) { btn.disabled = false; btn.innerHTML = '&#9654; 重新分析'; }
    }
  }).catch(() => {
    if (st) st.textContent = '网络错误';
    if (btn) { btn.disabled = false; btn.innerHTML = '&#9654; 重新分析'; }
  });
}
function posAiPoll() {
  fetch('/api/positions/ai_analysis').then(r => r.json()).then(d => {
    if (d.status === 'running') { setTimeout(posAiPoll, 3000); return; }
    if (d.status === 'done') { _posAiRender(d, false); }
    else {
      const st = document.getElementById('posAiStatus');
      const body = document.getElementById('posAiBody');
      if (st) st.textContent = '完成(有错误)';
      if (body) body.innerHTML = '<div style="color:#f85149;padding:20px">' + _aiEsc(d.error || '未知错误') + '</div>';
    }
    const btn = document.getElementById('posAiRunBtn');
    if (btn) { btn.disabled = false; btn.innerHTML = '&#9654; 重新分析'; }
  }).catch(() => setTimeout(posAiPoll, 5000));
}
// 用 positions 里 underlying 现价 + prev_close 算 {price, pct today}。
// 兜底找 heatmap scrData(如果加载了),两个源都命中不到就返回 null。
function _posAiPriceOf(ticker) {
  if (!ticker) return null;
  var tk = String(ticker).toUpperCase().replace(/^\\$/, '');
  try {
    if (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) {
      var pos = RAW_DATA.strategies.find(function (p) { return p.underlying === tk; });
      if (pos && pos.current_price > 0) {
        var pc = (RAW_DATA.equity_prev_close || {})[tk] || 0;
        var pct = pc > 0 ? ((pos.current_price - pc) / pc) * 100 : null;
        return { price: pos.current_price, pct: pct };
      }
    }
  } catch (e) {}
  try {
    if (typeof scrData !== 'undefined' && scrData && scrData.signals) {
      var s = scrData.signals.find(function (x) { return x.ticker === tk; });
      if (s && s.price > 0) return { price: s.price, pct: (typeof s.change_pct === 'number') ? s.change_pct : null };
    }
  } catch (e) {}
  return null;
}
function _posAiRender(d, cached) {
  _posAiData = d;
  const st = document.getElementById('posAiStatus');
  const btn = document.getElementById('posAiRunBtn');
  const items = (d.items || []).filter(x => !x.error);
  const errs = (d.items || []).filter(x => x.error);
  const asof = (d.asof || '').replace('T', ' ').slice(0, 16);
  if (st) st.textContent = items.length + ' 个持仓' + (errs.length ? ' · ' + errs.length + ' 失败' : '') + ' · ' + asof + (cached ? ' (缓存)' : '');
  if (btn) { btn.disabled = false; btn.innerHTML = '&#9654; 重新分析'; }
  // build filter bar
  const counts = { all: items.length, take_profit: 0, watch_risk: 0, hold: 0 };
  for (const it of items) counts[it.action] = (counts[it.action] || 0) + 1;
  const fb = document.getElementById('posAiFilters');
  const chips = [
    { key: 'all', label: '全部', color: '#8b949e', n: counts.all },
    { key: 'take_profit', label: '&#128309; 止盈', color: '#58a6ff', n: counts.take_profit },
    { key: 'watch_risk', label: '&#128993; 风险', color: '#d29922', n: counts.watch_risk },
    { key: 'hold', label: '&#128994; 持有', color: '#3fb950', n: counts.hold },
  ];
  fb.innerHTML = chips.map(c => {
    const bg = c.key === 'all' ? c.color + '33' : '#161b22';
    return '<button data-posai-f="' + c.key + '" style="background:' + bg + ';color:' + c.color + ';border:1px solid ' + c.color + '44;padding:3px 12px;border-radius:14px;cursor:pointer;font-size:12px;font-weight:600">' + c.label + ' <span style="opacity:.7">' + c.n + '</span></button>';
  }).join('');
  fb.onclick = function(e) { const b = e.target.closest('[data-posai-f]'); if (b) _posAiFilter(b.getAttribute('data-posai-f')); };
  fb.style.display = 'flex';
  _posAiFilter('all');
}
function _posAiFilter(action) {
  if (!_posAiData) return;
  // update chip active state
  document.querySelectorAll('[data-posai-f]').forEach(el => {
    const k = el.getAttribute('data-posai-f');
    const isActive = (k === action);
    el.style.background = isActive ? (k === 'all' ? '#8b949e33' : k === 'take_profit' ? '#58a6ff22' : k === 'watch_risk' ? '#d2992222' : '#3fb95022') : '#161b22';
    el.style.fontWeight = isActive ? '700' : '600';
  });
  const items = (_posAiData.items || []).filter(it => {
    if (it.error) return action === 'all';
    return action === 'all' || it.action === action;
  });
  const body = document.getElementById('posAiBody');
  if (!items.length) { body.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">该分类下无结果</div>'; return; }
  const colors = { bullish: '#3fb950', bearish: '#f85149', neutral: '#8b949e' };
  const impColors = { high: '#f85149', medium: '#d29922', low: '#8b949e' };
  const actLabels = { hold: '&#128994; 持有', take_profit: '&#128309; 考虑止盈', watch_risk: '&#128993; 注意风险' };
  let html = '<div style="display:flex;flex-direction:column;gap:10px">';
  for (const it of items) {
    if (it.error) {
      html += '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px">'
        + '<span style="font-weight:700;color:#e6edf3">' + _aiEsc(it.ticker) + '</span>'
        + ' <span style="color:#f85149;font-size:12px">错误: ' + _aiEsc(it.error) + '</span></div>';
      continue;
    }
    const sentC = colors[it.news_sentiment] || '#8b949e';
    const impC = impColors[it.impact] || '#8b949e';
    const act = actLabels[it.action] || it.action || '';
    // Underlying spot + 今日 %(点开 modal 时的快照,重跑分析 = 新快照)
    var _q = _posAiPriceOf(it.ticker);
    var _priceChip = '';
    if (_q) {
      var _sign = _q.pct == null ? '' : (_q.pct >= 0 ? '+' : '');
      var _pctTxt = _q.pct == null ? '' : (' ' + _sign + _q.pct.toFixed(2) + '%');
      var _pctColor = _q.pct == null ? '#8b949e' : (_q.pct >= 0 ? '#3fb950' : '#f85149');
      _priceChip = '<span style="font-size:12px;color:#c9d1d9">$' + _q.price.toFixed(2) + '</span>'
                 + '<span style="font-size:12px;font-weight:700;color:' + _pctColor + '">' + _pctTxt + '</span>';
    }
    html += '<div style="background:#161b22;border:1px solid ' + (it.impact === 'high' ? '#f8514944' : '#30363d') + ';border-radius:8px;padding:10px 14px' + (it.impact === 'high' ? ';border-left:3px solid #f85149' : '') + '">'
      + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">'
      + '<span style="font-weight:700;font-size:14px;color:#e6edf3">' + _aiEsc(it.ticker) + '</span>'
      + _priceChip
      + '<span style="background:' + sentC + '22;color:' + sentC + ';padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">' + _aiEsc(it.news_sentiment || '') + '</span>'
      + '<span style="background:' + impC + '22;color:' + impC + ';padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">影响: ' + _aiEsc(it.impact || '') + '</span>'
      + '<span style="margin-left:auto;font-size:13px;font-weight:600">' + act + '</span>'
      + '</div>';
    if (it.catalyst) html += '<div style="font-size:12px;color:#d29922;margin-bottom:4px">&#9889; ' + _aiEsc(it.catalyst) + '</div>';
    if (it.reason) html += '<div style="font-size:12px;color:#c9d1d9;line-height:1.5">' + _aiEsc(it.reason) + '</div>';
    html += '</div>';
  }
  html += '</div>';
  body.innerHTML = html;
}

// ─── Daily Decision Log reminders ─────────────────────────────────────────
// (Moved server-side.) The old browser loop fired 3 fixed slots (morning /
// pre-close / end-of-day) every day regardless of whether anything was
// actually un-reviewed, and only worked while a PWA window was open. The
// reminder now lives in `decision_log_reminder_loop()` in this file: ONE push
// after the US close, only when a decision entry is still pending, fired via
// native osascript by the launchd-resident server — so it survives the Mac
// being asleep/off at the close and lands when it next wakes. Nothing here.

// Listen for ticker-click messages from the embedded dashboard.html iframe.
// Inside the iframe (Trades Table / Calendar / Analysis / Overview) the
// user clicks a row → iframe sends {type:'open-decision', ticker, open_date}
// → we find the matching Decision Log entry and open its drawer here.
window.addEventListener('message', async (e) => {
  const m = e && e.data;
  if (!m || m.type !== 'open-decision') return;
  // The drawer is a full-screen position:fixed overlay so it works
  // regardless of which sub-tab is active. Stay on the user's current
  // sub-tab — when they close the drawer they're back where they were
  // (Overview / Calendar / Analysis / etc.). Just ensure entries are
  // loaded so dlOpenTrade can find the matching entry.
  if (currentTopTab !== 'journal') {
    currentTopTab = '';
    switchTopTab('journal');
  }
  if (!_dlEntries || !_dlEntries.length) {
    try { await dlLoad(); } catch (err) { /* surface below */ }
  }
  // Best-match: same ticker + same open_date.
  let entry = _dlEntries.find(x =>
    x._kind === 'open' &&
    x.ticker === m.ticker &&
    (m.open_date && x.open_date === m.open_date));
  // Fallback: same ticker, any leg symbol overlap (catches diff TZ shift)
  if (!entry && m.symbol) {
    entry = _dlEntries.find(x =>
      x._kind === 'open' && x.ticker === m.ticker &&
      ((x.symbol || '').includes((m.symbol || '').trim())));
  }
  // Last fallback: ticker match (newest open)
  if (!entry) {
    const matches = _dlEntries.filter(x =>
      x._kind === 'open' && x.ticker === m.ticker);
    matches.sort((a, b) => (b.open_at || '').localeCompare(a.open_at || ''));
    entry = matches[0];
  }
  if (entry) {
    dlOpenTrade(entry.id);
  } else {
    alert(`No matching Decision Log entry for ${m.ticker} (${m.open_date}).`);
  }
});

// On page load, honor ?tab=<name> and optional &sub=<name> so notification-
// click landings open the right tab + sub-tab automatically.
window.addEventListener('DOMContentLoaded', () => {
  try {
    const sp = new URLSearchParams(location.search);
    const t = sp.get('tab');
    const sub = sp.get('sub');
    if (t && ['positions','screener','journal','decisions'].includes(t)) {
      currentTopTab = ''; // force switch
      switchTopTab(t);
    }
    if (sub) {
      // Wait a tick for tab panels to be active
      setTimeout(() => tjSubTab(sub), 80);
    }
    const secTk = sp.get('sec');   // ?sec=AAPL → 直接打开该票 SEC 财报(TA 单票页独立打开时的兜底深链)
    if (secTk && typeof openSecFiling === 'function') {
      setTimeout(() => openSecFiling(secTk), 120);
    }
    if (sp.get('posai') && typeof openPositionsAi === 'function') {
      setTimeout(() => { switchTopTab('positions'); openPositionsAi(); }, 150);
    }
  } catch (e) {}
});

// ─── Decision Log ─────────────────────────────────────────────────────────
let _dlEntries = [];
let _dlOptions = {};
let _dlMerges = new Set();   // "TICKER|open_date" 已合并的组

async function dlMergeToggle(ticker, openDate, merge) {
  try {
    const r = await fetch('/api/journal_merge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ticker, open_date: openDate, merge: merge}),
    }).then(x => x.json());
    if (!r || !r.ok) { alert('合并失败: ' + ((r && r.msg) || '未知')); return; }
    await dlLoad();   // 重同步后重新拉取 entries + merges 并重渲染
  } catch (e) { alert('合并失败: ' + e); }
}
let _dlFilter = { status: 'pending', kind: 'all' };

async function dlLoad() {
  const lv = document.getElementById('dl-list');
  lv.innerHTML = '<div style="text-align:center;padding:40px;color:#8b949e">Loading...</div>';
  try {
    const [optsR, listR, mergeR] = await Promise.all([
      fetch('/api/journal_entries/options').then(r => r.json()),
      fetch('/api/journal_entries/list?status=all').then(r => r.json()),
      fetch('/api/journal_merges').then(r => r.json()).catch(() => ({merges: []})),
    ]);
    _dlOptions = optsR;
    _dlEntries = listR.entries || [];
    _dlMerges = new Set((mergeR.merges || []).map(m => (m[0] + '|' + m[1])));
    _dlUpdateBadge();
    _dlUpdateFilterPills();
    dlRender();
  } catch (e) {
    lv.innerHTML = '<div style="color:#f85149">Error: ' + e.message + '</div>';
  }
}

function _dlUpdateBadge() {
  // Count UNIQUE TRADES that need any journal action — not raw entries.
  // Each trade has up to 2 entries (open + close), and the user thinks in
  // trades, not entries, so 124 unique pending trades is the right number,
  // not 234 entries.
  const pendingTids = new Set();
  for (const e of _dlEntries) {
    if (e.status === 'pending' && e.trade_id) pendingTids.add(e.trade_id);
  }
  const pending = pendingTids.size;
  const b = document.getElementById('dl-pending-badge');
  if (b) {
    if (pending > 0) { b.style.display = ''; b.textContent = pending; }
    else b.style.display = 'none';
  }
}

function _dlUpdateFilterPills() {
  ['pending','completed','all'].forEach(s => {
    const el = document.getElementById('dl-st-' + s);
    if (el) el.classList.toggle('active', _dlFilter.status === s);
  });
  ['all','active','closed'].forEach(k => {
    const el = document.getElementById('dl-k-' + k);
    if (el) el.classList.toggle('active', _dlFilter.kind === k);
  });
}

function dlSetFilter(key, val) {
  _dlFilter[key] = val;
  _dlUpdateFilterPills();
  dlRender();
}

function dlRender() {
  const lv = document.getElementById('dl-list');
  const search = (document.getElementById('dl-search')?.value || '').trim().toUpperCase();

  // Collapse OPEN+CLOSE entries into one row per trade_id
  const tradeMap = {};   // trade_id → {open, close}
  for (const e of _dlEntries) {
    const slot = tradeMap[e.trade_id] || (tradeMap[e.trade_id] = {});
    slot[e._kind] = e;
  }
  let trades = Object.values(tradeMap);
  // Sort by open_at desc
  trades.sort((a, b) => {
    const ax = (a.open || a.close)?.open_at || '';
    const bx = (b.open || b.close)?.open_at || '';
    return bx.localeCompare(ax);
  });

  // Filters
  if (_dlFilter.kind === 'active') trades = trades.filter(t => !t.close);
  if (_dlFilter.kind === 'closed') trades = trades.filter(t => !!t.close);
  if (_dlFilter.status === 'pending')   trades = trades.filter(t => _dlIsPending(t));
  if (_dlFilter.status === 'completed') trades = trades.filter(t => !_dlIsPending(t));
  if (search) trades = trades.filter(t => ((t.open || t.close)?.ticker || '').toUpperCase().includes(search));

  if (!trades.length) {
    lv.innerHTML = '<div style="text-align:center;padding:40px;color:#8b949e">No trades match. Click Sync to backfill from TastyTrade.</div>';
    return;
  }
  let h = '<div style="color:#8b949e;font-size:11px;margin-bottom:8px">Showing <b style="color:#e6edf3">' + trades.length + '</b> of ' + Object.keys(tradeMap).length + ' trades</div>';

  // 同票同天可合并:统计每个 (ticker,open_date) 未合并的单数(≥2 才给「合并」)
  const mergeable = {};
  trades.forEach(t => { const e = t.open || t.close; const mk = (e.ticker || '') + '|' + (e.open_date || ''); if (!_dlMerges.has(mk)) mergeable[mk] = (mergeable[mk] || 0) + 1; });

  for (const t of trades) {
    const e = t.open || t.close;
    const closed = !!t.close;
    const openFilled = t.open?.status === 'completed';
    const closeFilled = !t.close || t.close.status === 'completed';

    const lifecycleBadge = closed
      ? '<span style="background:#21262d;color:#8b949e;border:1px solid #30363d;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px">🔒 CLOSED</span>'
      : '<span style="background:#d2992222;color:#d29922;border:1px solid #d2992266;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px">🟢 ACTIVE</span>';

    const dirColor = e.direction === 'BULL' ? '#3fb950'
                   : e.direction === 'BEAR' ? '#f85149'
                   : e.direction === 'NEUTRAL' ? '#d29922' : '#8b949e';
    const dirBadge = e.direction
      ? `<span style="background:${dirColor}22;color:${dirColor};border:1px solid ${dirColor}44;font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px">${e.direction}</span>` : '';
    const stratBadge = e.strategy
      ? `<span style="color:#8b949e;font-size:10px;background:#21262d;border:1px solid #30363d;padding:1px 6px;border-radius:3px">${e.strategy}</span>` : '';
    const qty = e.quantity ? `×${e.quantity}` : '';
    const label = e.spread_label || (e.symbol || '').split(',')[0];
    const dateStr = closed ? `${e.open_date} → ${t.close.close_date}` : e.open_date;
    const pnl = closed && typeof t.close.net_pnl === 'number'
      ? ` <span style="color:${t.close.net_pnl>=0?'#3fb950':'#f85149'};font-weight:700">${t.close.net_pnl>=0?'+':''}$${t.close.net_pnl.toFixed(0)}</span>`
      : '';

    // Two-step fill indicator: open ✓/○ + (if closed) close ✓/○
    const oMark = openFilled ? '✓' : '○';
    const cMark = !t.close ? '' : (closeFilled ? '✓' : '○');
    const fillStyle = (mark) => mark === '✓' ? 'color:#3fb950' : 'color:#d29922';
    const fillHtml = closed
      ? `<span style="font-size:11px"><span style="${fillStyle(oMark)}">${oMark} open</span> · <span style="${fillStyle(cMark)}">${cMark} close</span></span>`
      : `<span style="font-size:11px"><span style="${fillStyle(oMark)}">${oMark} open</span></span>`;

    // The clickable id: prefer the open entry id (stable since open exists for every trade)
    const targetId = t.open?.id || t.close?.id;
    const rowClass = _dlIsPending(t) ? 'pending' : 'completed';
    // 同票同天合并/拆开按钮
    const mk = (e.ticker || '') + '|' + (e.open_date || '');
    let mergeBtn = '';
    if (_dlMerges.has(mk)) {
      mergeBtn = `<button onclick="event.stopPropagation();dlMergeToggle('${e.ticker}','${e.open_date}',false)" title="拆开:恢复成当天分开的多单" style="background:#21262d;border:1px solid #30363d;color:#d29922;font-size:10px;padding:2px 7px;border-radius:3px;cursor:pointer">⛓ 已合并·拆开</button>`;
    } else if ((mergeable[mk] || 0) >= 2) {
      mergeBtn = `<button onclick="event.stopPropagation();dlMergeToggle('${e.ticker}','${e.open_date}',true)" title="把 ${e.ticker} ${e.open_date} 当天分开开的 ${mergeable[mk]} 单合并成一单(可拆开)" style="background:#1f6feb22;border:1px solid #1f6feb66;color:#58a6ff;font-size:10px;padding:2px 7px;border-radius:3px;cursor:pointer">🔗 合并${mergeable[mk]}单</button>`;
    }
    h += `<div class="dl-row ${rowClass}" onclick="dlOpenTrade('${targetId}')">
      ${lifecycleBadge}
      ${dirBadge}
      <div style="font-weight:700;color:#58a6ff;min-width:55px">${e.ticker}</div>
      <div style="color:#e6edf3;font-size:12px;font-weight:600">${label} ${qty}</div>
      ${stratBadge}
      <div style="color:#8b949e;font-size:11px;flex:1;text-align:right">${dateStr}${pnl}</div>
      ${mergeBtn}
      ${fillHtml}
    </div>`;
  }
  lv.innerHTML = h;
}

function _dlIsPending(t) {
  if (t.open && t.open.status === 'pending') return true;
  if (t.close && t.close.status === 'pending') return true;
  return false;
}

// ─── Stats sub-tab ─────────────────────────────────────────────────────
let _dlStatsPayload = null;
let _dlStatsLoaded = false;
let _dlCurrentFilters = {};         // sticky across re-renders
let _dlStatsAllOptions = null;      // captured from first (unfiltered) load

function dlSubTab(name) {
  /* Legacy entry — outer tjSubTab is now master. Map old names to new. */
  if (name === 'trades') return tjSubTab('decisions');
  if (name === 'stats') return tjSubTab('stats');
}

// ─── Events top-level tab (Earnings + Econ calendar) ────────────────────
let _eventsLoaded = false;

async function eventsLoad() {
  if (_eventsLoaded) return;
  const view = document.getElementById('evCalPane');
  view.innerHTML = '<div style="text-align:center;color:#8b949e;padding:60px">Loading events…</div>';
  try {
    const r = await fetch('/api/calendar_events').then(r => r.json());
    _eventsLoaded = true;
    _eventsRender(r);
  } catch (e) {
    view.innerHTML = `<div style="color:#f85149;padding:20px">Failed to load: ${e.message}</div>`;
  }
}

async function eventsRefresh() {
  const btn = document.getElementById('events-refresh-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Refreshing… (~5-10s)'; }
  try {
    const r = await fetch('/api/calendar_events/refresh', {method: 'POST'}).then(r => r.json());
    _eventsRender(r);
  } catch (e) {
    const view = document.getElementById('evCalPane');
    view.insertAdjacentHTML('afterbegin',
      `<div style="background:#3a1f1f;color:#f85149;padding:8px 12px;border-radius:4px;margin-bottom:10px">Refresh failed: ${e.message}</div>`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Refresh'; }
  }
}

function _eventsRender(p) {
  const view = document.getElementById('evCalPane');
  if (!p || (!p.fetched_at && !(p.earnings || []).length && !(p.econ || []).length)) {
    view.innerHTML = `<div style="padding:60px;text-align:center;color:#8b949e">
      <div style="font-size:36px">📭</div>
      <div style="margin-top:14px">No cached data yet. Click <b>Refresh</b> to fetch this + next 2 months.</div>
      <button id="events-refresh-btn" onclick="eventsRefresh()"
        style="margin-top:18px;background:#1f6feb;color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-weight:700">🔄 Refresh</button>
    </div>`;
    return;
  }

  // Group both feeds by date.
  const byDate = {};   // 'YYYY-MM-DD' → {earnings: [...], econ: [...]}
  const ensure = (d) => byDate[d] || (byDate[d] = {earnings: [], econ: []});
  for (const e of (p.earnings || [])) ensure(e.date).earnings.push(e);
  for (const e of (p.econ || []))     ensure(e.date).econ.push(e);

  const fetchedHuman = p.fetched_at ? new Date(p.fetched_at).toLocaleString() : '—';
  const econErr = p.econ_error
    ? `<div style="color:#d29922;font-size:11px;margin-bottom:8px">⚠ econ feed error: ${p.econ_error}</div>` : '';

  // Three-month grid: this month + next 2 months, side-by-side.
  const today = new Date();
  const m1 = new Date(today.getFullYear(), today.getMonth(), 1);
  const m2 = new Date(today.getFullYear(), today.getMonth() + 1, 1);
  const m3 = new Date(today.getFullYear(), today.getMonth() + 2, 1);

  view.innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px;padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:6px">
      <button id="events-refresh-btn" onclick="eventsRefresh()"
        style="background:#1f6feb;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer;font-weight:700;font-size:12px">🔄 Refresh</button>
      <span style="color:#8b949e;font-size:11px">Last refresh: <b style="color:#e6edf3">${fetchedHuman}</b></span>
      <span style="color:#8b949e;font-size:11px">Earnings universe: <b style="color:#e6edf3">${p.earnings_universe_size || '—'}</b> tickers</span>
      <span style="color:#8b949e;font-size:11px">📊 ${(p.earnings||[]).length} earnings · 🌐 ${(p.econ||[]).length} high-impact econ</span>
    </div>
    ${econErr}
    <div class="ev-cal-months">
      ${_renderMonthGrid(m1, byDate, today)}
      ${_renderMonthGrid(m2, byDate, today)}
      ${_renderMonthGrid(m3, byDate, today)}
    </div>
  `;
}

function _renderMonthGrid(monthDate, byDate, today) {
  const year = monthDate.getFullYear();
  const month = monthDate.getMonth();
  const monthName = monthDate.toLocaleDateString(undefined, {month:'long', year:'numeric'});
  const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const firstDay = new Date(year, month, 1).getDay();          // 0=Sun
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const todayKey = today.toISOString().slice(0, 10);

  let cells = '';
  // Leading blanks
  for (let i = 0; i < firstDay; i++) cells += `<div class="ev-cal-blank"></div>`;
  // Real cells
  for (let d = 1; d <= daysInMonth; d++) {
    const key = `${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const data = byDate[key] || {earnings: [], econ: []};
    const isToday = key === todayKey;
    const isPast = key < todayKey;
    const isWeekend = ((firstDay + d - 1) % 7) === 0 || ((firstDay + d - 1) % 7) === 6;
    cells += _renderDayCell(d, data, isToday, isPast, isWeekend);
  }

  return `<div class="ev-cal-month">
    <div class="ev-cal-month-title">${monthName}</div>
    <div class="ev-cal-grid">
      ${dow.map(d => `<div class="ev-cal-dow">${d}</div>`).join('')}
      ${cells}
    </div>
  </div>`;
}

function _renderDayCell(day, data, isToday, isPast, isWeekend) {
  const sessClass = (s) => s === 'BMO' ? 'ev-bmo' : s === 'AMC' ? 'ev-amc' : 'ev-other';
  // Earnings: store full payload as data-* on the chip; sup shows BMO/AMC
  // or "TBD" so the user knows when Nasdaq just hasn't published the
  // session — not a render bug, that's the company's filing.
  const earnHtml = data.earnings.map(e => {
    const payload = encodeURIComponent(JSON.stringify(e));
    const supText = (e.session === '--') ? 'TBD' : e.session;
    return `<div class="ev-earn ${sessClass(e.session)}" data-ev-earn="${payload}"
      onmouseenter="evShowEarnTip(this, event)" onmousemove="evMoveTip(event)" onmouseleave="evHideTip()"
      >${e.ticker}<sup style="font-size:7px;margin-left:1px;opacity:.7">${supText}</sup></div>`;
  }).join('');
  const econHtml = data.econ.map(e => {
    const payload = encodeURIComponent(JSON.stringify(e));
    return `<div class="ev-econ" data-ev-econ="${payload}"
      onmouseenter="evShowEconTip(this, event)" onmousemove="evMoveTip(event)" onmouseleave="evHideTip()"
      >⚡ ${_truncEvent(e.title)} <span style="opacity:.6">${e.time_local}</span></div>`;
  }).join('');
  const cellClasses = ['ev-cal-day'];
  if (isToday) cellClasses.push('ev-cal-today');
  if (isPast) cellClasses.push('ev-cal-past');
  if (isWeekend) cellClasses.push('ev-cal-weekend');
  return `<div class="${cellClasses.join(' ')}">
    <div class="ev-cal-daynum">${day}</div>
    ${econHtml}
    ${earnHtml ? `<div class="ev-earn-row">${earnHtml}</div>` : ''}
  </div>`;
}

function _truncEvent(s) {
  return s.length > 24 ? s.slice(0, 22) + '…' : s;
}

// ─── Tooltip ──────────────────────────────────────────────────────────
// Floating div that follows the mouse. Earnings tooltip kicks off an
// async quote fetch (cached per session) and patches the price line in
// when it arrives.
const _evQuoteCache = new Map();   // ticker → {price, change, changePct} or 'loading' or 'error'

function _evTip() { return document.getElementById('ev-tooltip'); }

function evShowEconTip(el, event) {
  let e;
  try { e = JSON.parse(decodeURIComponent(el.dataset.evEcon)); } catch (_) { return; }
  const tip = _evTip();
  tip.innerHTML = `
    <div class="ev-tt-title">⚡ ${e.title}</div>
    <div class="ev-tt-row">${e.date} · <b>${e.time_local}</b> local</div>
    <div class="ev-tt-row">forecast <b>${e.forecast || '—'}</b> · prev <b>${e.previous || '—'}</b>${e.actual ? ` · actual <b>${e.actual}</b>` : ''}</div>
    <div class="ev-tt-row" style="font-size:10px;margin-top:4px;color:#666">High-impact ${e.country || 'USD'} · source: ${e.source || 'TradingView'}</div>
  `;
  tip.style.display = 'block';
  evMoveTip(event);
}

function evShowEarnTip(el, event) {
  let e;
  try { e = JSON.parse(decodeURIComponent(el.dataset.evEarn)); } catch (_) { return; }
  const tip = _evTip();
  const sessLabel = e.session === '--'
    ? '<span style="color:#8b949e">time TBD (not published)</span>'
    : `<b>${e.session}</b> ${e.session === 'BMO' ? '(before open)' : e.session === 'AMC' ? '(after close)' : ''}`;
  // Build with placeholder for quote — patch in when fetched.
  tip.innerHTML = `
    <div class="ev-tt-title">${e.ticker}${e.name ? ` <span style="color:#8b949e;font-weight:400">${e.name}</span>` : ''}</div>
    <div class="ev-tt-row">${e.date} · ${sessLabel}</div>
    <div class="ev-tt-row">EPS est <b>${e.eps_forecast || '—'}</b>${e.fiscal_quarter ? ` · ${e.fiscal_quarter}` : ''}</div>
    <div class="ev-tt-row" id="ev-tt-quote" style="margin-top:4px">price <b>loading…</b></div>
  `;
  tip.style.display = 'block';
  evMoveTip(event);
  _evFetchQuote(e.ticker);
}

function evMoveTip(event) {
  const tip = _evTip();
  if (tip.style.display === 'none') return;
  const x = event.clientX + 14;
  const y = event.clientY + 14;
  // Flip if going off the right/bottom edges
  const rect = tip.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 10;
  const maxY = window.innerHeight - rect.height - 10;
  tip.style.left = Math.min(x, maxX) + 'px';
  tip.style.top  = Math.min(y, maxY) + 'px';
}

function evHideTip() {
  const tip = _evTip();
  tip.style.display = 'none';
}

async function _evFetchQuote(ticker) {
  const cached = _evQuoteCache.get(ticker);
  if (cached && cached !== 'error') {
    if (cached !== 'loading') _evPaintQuote(ticker, cached);
    return;
  }
  _evQuoteCache.set(ticker, 'loading');
  try {
    const r = await fetch('/api/quote_simple?symbol=' + encodeURIComponent(ticker)).then(r => r.json());
    if (r.error) throw new Error(r.error);
    _evQuoteCache.set(ticker, r);
    _evPaintQuote(ticker, r);
  } catch (e) {
    _evQuoteCache.set(ticker, 'error');
    _evPaintQuote(ticker, null);
  }
}

function _evPaintQuote(ticker, q) {
  const tip = _evTip();
  if (tip.style.display === 'none') return;
  // Only paint if the tooltip is currently for this ticker
  const titleEl = tip.querySelector('.ev-tt-title');
  if (!titleEl || !titleEl.textContent.startsWith(ticker)) return;
  const row = document.getElementById('ev-tt-quote');
  if (!row) return;
  if (!q) {
    row.innerHTML = '<span style="color:#8b949e">price unavailable</span>';
    return;
  }
  const sign = q.changePct >= 0 ? '+' : '';
  const cls = q.changePct >= 0 ? 'ev-tt-up' : 'ev-tt-down';
  row.innerHTML = `price <b>$${q.price.toFixed(2)}</b> <span class="${cls}">${sign}${q.change.toFixed(2)} (${sign}${q.changePct.toFixed(2)}%)</span>`;
}

async function dlStatsLoad(filters) {
  /* Fetch full stats payload. Optional filters: {from, to, strategy, direction, ticker}. */
  const view = document.getElementById('dl-stats-view');
  view.innerHTML = '<div style="text-align:center;color:#8b949e;padding:60px">Loading stats…</div>';
  // Persist current filter selections so the filter bar can pre-fill after re-render
  _dlCurrentFilters = filters || {};
  const qs = new URLSearchParams();
  for (const k of ['from','to','strategy','direction','ticker']) {
    if (_dlCurrentFilters[k]) qs.set(k, _dlCurrentFilters[k]);
  }
  const url = '/api/journal_stats' + (qs.toString() ? '?' + qs.toString() : '');
  try {
    const r = await fetch(url).then(r => r.json());
    if (r.error) throw new Error(r.error);
    _dlStatsPayload = r;
    _dlStatsLoaded = true;
    // Capture the full list of distinct values on the FIRST (unfiltered) load
    // so dropdown options don't shrink as filters narrow the result set.
    if (!_dlStatsAllOptions || (!filters || Object.keys(filters).filter(k=>filters[k]).length === 0)) {
      _dlStatsAllOptions = {
        direction: [...new Set((r.by_direction || []).map(x => x.direction).filter(Boolean))].sort(),
        strategy:  [...new Set((r.by_strategy  || []).map(x => x.strategy).filter(Boolean))].sort(),
        ticker:    [...new Set((r.by_ticker    || []).map(x => x.ticker).filter(Boolean))].sort(),
      };
    }
    _dlStatsRender(r);
  } catch (e) {
    view.innerHTML = `<div style="color:#f85149;padding:20px">Failed to load stats: ${e.message}</div>`;
  }
}

function _dlStatsRender(p) {
  const view = document.getElementById('dl-stats-view');
  if (p.n_trades === 0) {
    view.innerHTML = `<div style="text-align:center;color:#8b949e;padding:60px">
      <div style="font-size:36px">📭</div>
      <div style="margin-top:14px">No closed trades yet — close a position to see stats here.</div>
    </div>`;
    return;
  }
  // Trimmed per user request: this view sits beneath the dashboard.html
  // Analysis charts inside the merged "Analysis" pill, so cards that the
  // dashboard already shows (KPI hero / Hold Time / DTE / Direction /
  // Strategy / DoW / Equity Curve / ToD / R-Multiple / Top Tickers /
  // Calendar Heatmap) are dropped to avoid duplication. Filter bar,
  // Auto-Recommendations, Weekly, Guardrails, and both Edge Finder cards
  // were also dropped (low signal / noisy).
  view.innerHTML = `
    ${_dlMonthlyProgressHtml(p.monthly_progress)}
    <div class="stats-grid" style="grid-template-columns:1fr 1fr;margin-top:12px">
      ${_dlStatsCard('🎯 Quadrant — Process × Result', _dlQuadrantHtml(p.quadrant))}
      ${_dlStatsCard('🪧 Followed Plan vs Broke Plan', _dlFollowedPlanHtml(p.followed_plan))}
    </div>
    <div class="stats-grid" style="grid-template-columns:1fr 1fr;margin-top:12px">
      ${_dlStatsCard('📊 Confidence Calibration', _dlConfCalibrationHtml(p.confidence_calibration))}
      ${_dlStatsCard('💔 Top Mistakes (by $-cost)', _dlMistakesHtml(p.mistakes))}
    </div>
    <div style="margin-top:12px">
      ${_dlStatsCard('🧠 Emotion × P/L Correlation', _dlEmotionHtml(p.emotion_correlation))}
    </div>
    ${_dlLessonsHtml(p.lessons)}
    <div class="stats-grid" style="grid-template-columns:1fr 2fr;margin-top:12px">
      ${_dlStatsCard('📐 Max P/L Realization Rates', _dlMaxLossFreqHtml(p.max_loss_frequency))}
      ${_dlStatsCard('📰 Earnings Bets vs Non-Earnings', _dlEarningsHtml(p.earnings))}
    </div>
  `;
}

// ─ Screenshot strip + capture flow ───────────────────────────────────────
function _dlScreenshotsStrip(open, close) {
  /* Render thumbnails for any saved screenshots on either side. The
     dataset model: each entry has a `screenshots: [{filename, caption,
     added_at}]` list — files served at
     /api/journal_entries/<entry_id>/screenshot/<filename>. */
  const items = [];
  for (const e of [open, close]) {
    if (!e || !Array.isArray(e.screenshots)) continue;
    for (const s of e.screenshots) {
      items.push({...s, entry_id: e.id});
    }
  }
  if (!items.length) {
    return `<div class="dl-readonly" style="margin-bottom:10px;font-size:11px;color:#8b949e">
      📸 No screenshots yet — click <b>Capture chart</b> at the top to save the
      TradingView view with your annotations.
    </div>`;
  }
  // Sort newest-first
  items.sort((a, b) => (b.added_at || '').localeCompare(a.added_at || ''));
  return `<div class="stats-card" style="margin-bottom:10px">
    <div class="stats-card-title">📸 Screenshots <span style="font-weight:400;text-transform:none;color:#8b949e">(${items.length})</span></div>
    <div style="display:flex;gap:6px;overflow-x:auto;padding:2px 0">
      ${items.map(s => `<div style="position:relative;flex-shrink:0">
        <a href="/api/journal_entries/${s.entry_id}/screenshot/${s.filename}" target="_blank" rel="noopener">
          <img src="/api/journal_entries/${s.entry_id}/screenshot/${s.filename}"
               title="${(s.caption || '').replace(/"/g,'&quot;')} · ${s.added_at || ''}"
               style="height:80px;border:1px solid #30363d;border-radius:4px;display:block">
        </a>
        <button onclick="_dlDeleteScreenshot('${s.entry_id}','${s.filename}')"
                title="Delete screenshot"
                style="position:absolute;top:2px;right:2px;background:rgba(0,0,0,0.7);color:#f85149;border:none;border-radius:50%;width:18px;height:18px;cursor:pointer;font-size:10px">×</button>
      </div>`).join('')}
    </div>
  </div>`;
}

async function dlCaptureScreenshot(entryId) {
  /* Capture the user's current tab using getDisplayMedia, crop to the
     TradingView area, upload as PNG to the entry, refresh the drawer. */
  const status = document.getElementById('dl-status');
  const setS = (msg, color) => { if (status) { status.textContent = msg; status.style.color = color || '#8b949e'; } };
  if (!entryId) { alert('No entry selected.'); return; }
  if (!navigator.mediaDevices?.getDisplayMedia) {
    alert('Screen capture not supported in this browser.');
    return;
  }
  let stream;
  try {
    setS('Selecting screen / tab to capture…');
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: 'browser' },
      preferCurrentTab: true,
    });
  } catch (e) {
    setS('❌ Capture cancelled or denied.', '#f85149');
    return;
  }
  try {
    const video = document.createElement('video');
    video.srcObject = stream; video.muted = true;
    await video.play();
    await new Promise(r => setTimeout(r, 250));   // let it render

    // Crop to the TV container's bounding rect (we want the chart only,
    // not the surrounding browser chrome).
    const tv = document.getElementById('dl-tv');
    const rect = tv && tv.getBoundingClientRect();
    const useCrop = rect && rect.width > 0 && rect.height > 0;
    const sx = video.videoWidth / window.innerWidth;
    const sy = video.videoHeight / window.innerHeight;
    const canvas = document.createElement('canvas');
    if (useCrop) {
      canvas.width  = Math.round(rect.width  * sx);
      canvas.height = Math.round(rect.height * sy);
      canvas.getContext('2d').drawImage(video,
        Math.round(rect.left * sx), Math.round(rect.top * sy),
        canvas.width, canvas.height,
        0, 0, canvas.width, canvas.height);
    } else {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
    }
    stream.getTracks().forEach(t => t.stop());
    video.srcObject = null; video.remove();

    // Convert to blob + upload
    const blob = await new Promise(r => canvas.toBlob(r, 'image/png', 0.92));
    if (!blob) { setS('❌ Failed to encode PNG.', '#f85149'); return; }
    setS(`Uploading screenshot (${(blob.size/1024).toFixed(0)} KB)…`);
    const r = await fetch(`/api/journal_entries/${entryId}/screenshot`, {
      method: 'POST',
      headers: {'Content-Type': 'image/png'},
      body: blob,
    });
    const j = await r.json();
    if (!r.ok || j.error) {
      setS(`❌ Upload failed: ${j.error || r.status}`, '#f85149');
      return;
    }
    setS('✓ Screenshot saved.', '#3fb950');
    // Refresh drawer in place to show the new thumbnail
    const dr = document.getElementById('dl-drawer');
    const id = dr.dataset.openEntryId || dr.dataset.closeEntryId;
    if (id) dlOpenTrade(id);
    setTimeout(() => setS(''), 4000);
  } catch (e) {
    try { stream && stream.getTracks().forEach(t => t.stop()); } catch (_) {}
    setS(`❌ Capture failed: ${e.message}`, '#f85149');
  }
}

async function _dlDeleteScreenshot(entryId, filename) {
  if (!confirm('Delete this screenshot?')) return;
  await fetch(`/api/journal_entries/${entryId}/screenshot/${encodeURIComponent(filename)}`,
              {method: 'DELETE'});
  const dr = document.getElementById('dl-drawer');
  const id = dr.dataset.openEntryId || dr.dataset.closeEntryId;
  if (id) dlOpenTrade(id);
}

// ─ Daily-loss / consecutive-loss STOP banner ────────────────────────────
function _dlGuardrailsBanner(g) {
  if (!g) return '';
  const blocks = [];
  if (g.daily_limit_breached) {
    blocks.push(`<div style="background:#3a1f1f;color:#f85149;border:1px solid #f8514966;padding:10px 14px;border-radius:6px;display:flex;align-items:center;gap:10px;font-weight:700">
      🛑 DAILY LOSS LIMIT BREACHED — Today: $${g.today_pnl.toFixed(0)} (limit $${g.daily_loss_limit}). Step away from the screen.
    </div>`);
  }
  if (g.warn_3_consec_losses) {
    blocks.push(`<div style="background:#3a2d0d;color:#d29922;border:1px solid #d2992266;padding:10px 14px;border-radius:6px;display:flex;align-items:center;gap:10px;font-weight:700">
      ⚠ ${g.current_loss_streak} consecutive losses — size down 50% or pause until next session.
    </div>`);
  }
  if (!blocks.length) return '';
  return `<div style="margin-top:12px;display:flex;flex-direction:column;gap:8px">${blocks.join('')}</div>`;
}

// ─ 2×2 Quadrant grid ────────────────────────────────────────────────────
function _dlQuadrantHtml(q) {
  if (!q || !q.total_filled) {
    return `<div style="color:#8b949e;font-size:11px;line-height:1.6">
      No quadrant ratings yet.<br>
      Fill the <b>Quadrant</b> radio in any closed-trade review to see this populate.
    </div>`;
  }
  const cell = (key, label, color) => {
    const c = q.cells[key];
    return `<div style="background:#0d1117;border:1px solid ${color}66;border-radius:5px;padding:10px;text-align:center">
      <div style="font-size:10px;color:${color};font-weight:700;text-transform:uppercase;letter-spacing:0.5px">${label}</div>
      <div style="font-size:20px;color:#e6edf3;font-weight:700;margin:4px 0">${c.n}</div>
      <div style="font-size:10px;color:#8b949e">${(c.share*100).toFixed(0)}% · ${c.avg_pnl>=0?'+':''}$${c.avg_pnl.toFixed(0)}/trade</div>
    </div>`;
  };
  const alert = q.outcome_bias_alert
    ? `<div style="margin-top:8px;font-size:10px;color:#d29922">⚠ Outcome-bias alert: ≥10% of trades are "Bad Decision / Good Result" — luck is masking process problems.</div>`
    : '';
  return `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
    ${cell('good_dec_good_res', '✓ Good Dec · Good Result', '#3fb950')}
    ${cell('good_dec_bad_res',  '~ Good Dec · Bad Result',  '#d29922')}
    ${cell('bad_dec_good_res',  '⚠ Bad Dec · Good Result',  '#d29922')}
    ${cell('bad_dec_bad_res',   '✗ Bad Dec · Bad Result',   '#f85149')}
  </div>${alert}`;
}

// ─ Confidence calibration plot (bar per bin) ────────────────────────────
function _dlConfCalibrationHtml(rows) {
  if (!rows || !rows.length) {
    return `<div style="color:#8b949e;font-size:11px">
      Fill <b>Confidence</b> on open entries to see whether your stated confidence predicts win rate.
    </div>`;
  }
  return rows.map(r => {
    const wr = r.win_rate * 100;
    const wrColor = wr >= 60 ? '#3fb950' : wr >= 40 ? '#d29922' : '#f85149';
    const pnlColor = r.net_pnl >= 0 ? '#3fb950' : '#f85149';
    return `<div style="margin-bottom:6px">
      <div style="display:flex;font-size:11px;color:#e6edf3;margin-bottom:2px">
        <b style="min-width:90px">${r.confidence}</b>
        <span style="color:#8b949e">n=${r.n}</span>
        <span style="margin-left:auto;color:${wrColor};font-weight:700">${wr.toFixed(0)}% W</span>
        <span style="margin-left:8px;color:${pnlColor};font-weight:700">${r.net_pnl>=0?'+':''}$${r.net_pnl.toFixed(0)}</span>
      </div>
      <div class="stats-bar-track"><div class="stats-bar-fill" style="width:${wr}%;background:${wrColor}66"></div></div>
    </div>`;
  }).join('') + '<div style="font-size:10px;color:#8b949e;margin-top:6px">Calibrated traders show monotonic rise from low → high confidence.</div>';
}

// ─ Followed Plan vs Broken Plan ──────────────────────────────────────────
function _dlFollowedPlanHtml(fp) {
  if (!fp) return '<div style="color:#8b949e;font-size:11px">No data.</div>';
  const total = fp.followed.n + fp.broken.n;
  if (total === 0) {
    return `<div style="color:#8b949e;font-size:11px">
      Mark <b>Followed plan SL/TP?</b> on close reviews to see this.
    </div>`;
  }
  const cohort = (label, c, color) => `<div style="background:#0d1117;border:1px solid ${color}66;border-radius:5px;padding:10px">
    <div style="font-size:10px;color:${color};font-weight:700;text-transform:uppercase;letter-spacing:0.5px">${label}</div>
    <div style="font-size:18px;color:${c.net_pnl>=0?'#3fb950':'#f85149'};font-weight:700;margin:4px 0">${c.net_pnl>=0?'+':''}$${c.net_pnl.toFixed(0)}</div>
    <div style="font-size:11px;color:#8b949e">${c.n} trades · ${(c.win_rate*100).toFixed(0)}% W · ${c.avg_r >= 0?'+':''}${c.avg_r.toFixed(2)}R avg</div>
  </div>`;
  return `<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
    ${cohort('✓ Followed Plan', fp.followed, '#3fb950')}
    ${cohort('✗ Broke Plan', fp.broken, '#f85149')}
  </div>
  <div style="font-size:11px;color:#8b949e;margin-top:8px">
    Rule adherence: <b style="color:${fp.rate_adherence>=0.9?'#3fb950':fp.rate_adherence>=0.7?'#d29922':'#f85149'}">${(fp.rate_adherence*100).toFixed(0)}%</b>
    (target ≥90%)
  </div>`;
}

// ─ Mistake list (top bleeders) ───────────────────────────────────────────
function _dlMistakesHtml(rows) {
  if (!rows || !rows.length) {
    return `<div style="color:#8b949e;font-size:11px">
      Tag <b>Mistakes Made</b> on close reviews — recurring patterns surface here.
    </div>`;
  }
  return rows.slice(0, 8).map(r => {
    const pnlColor = r.net_pnl >= 0 ? '#3fb950' : '#f85149';
    return `<div style="display:flex;align-items:center;font-size:11px;padding:5px 0;border-bottom:1px solid #21262d">
      <b style="color:#e6edf3;flex:1">${r.mistake}</b>
      <span style="color:#8b949e;margin:0 8px">${r.n}×</span>
      <span style="color:${pnlColor};font-weight:700;min-width:60px;text-align:right">${r.net_pnl>=0?'+':''}$${r.net_pnl.toFixed(0)}</span>
    </div>`;
  }).join('');
}

// ─ Emotion × P/L correlation panel ──────────────────────────────────────
function _dlEmotionHtml(rows) {
  if (!rows || !rows.length) {
    return `<div style="color:#8b949e;font-size:11px">
      Set the emotion sliders on open entries to see correlation between
      your state and realized R-multiples.
    </div>`;
  }
  const fmt = v => v == null ? '—' : (v >= 0 ? '+' : '') + '$' + v.toFixed(0);
  const corrColor = c => c == null ? '#8b949e' : c < -0.2 ? '#f85149' : c > 0.2 ? '#3fb950' : '#d29922';
  return `<table class="stats-table">
    <thead><tr><th>Emotion</th><th style="text-align:right">N</th><th style="text-align:right">Pearson r</th><th style="text-align:right">Avg R</th><th style="text-align:right">Low Bucket</th><th style="text-align:right">High Bucket</th></tr></thead>
    <tbody>
    ${rows.map(r => `<tr>
      <td><b>${r.emotion.replace(/_/g, ' ')}</b></td>
      <td style="text-align:right">${r.n}</td>
      <td style="text-align:right;color:${corrColor(r.correlation)};font-weight:700">${r.correlation == null ? '—' : r.correlation.toFixed(2)}</td>
      <td style="text-align:right">${r.avg_r >= 0 ? '+' : ''}${r.avg_r.toFixed(2)}R</td>
      <td style="text-align:right;color:#8b949e">${fmt(r.low_bucket_avg_pnl)} <span style="color:#666">(n=${r.low_bucket_n})</span></td>
      <td style="text-align:right;color:#8b949e">${fmt(r.high_bucket_avg_pnl)} <span style="color:#666">(n=${r.high_bucket_n})</span></td>
    </tr>`).join('')}
    </tbody>
  </table>
  <div style="font-size:10px;color:#8b949e;margin-top:6px">
    Green r &gt; 0 = emotion helps · Red r &lt; 0 = emotion hurts. "Low Bucket" = trades where this emotion ≤3 (or =0 for slept_7h/stable_today); "High" = ≥7 (or =1).
  </div>`;
}

function _dlStatsFilterBar(p) {
  // Pre-fill <select> + <input> values from the persisted _dlCurrentFilters
  // so the user's selections survive the re-render after Apply.
  const cur = _dlCurrentFilters || {};
  // Options come from the cached unfiltered domain (so all values remain
  // pickable even after a filter narrowed the result), with current pick
  // pre-selected.
  const allOpts = _dlStatsAllOptions || {direction:[], strategy:[], ticker:[]};
  const opts = (values, current) => ['<option value="">all</option>']
    .concat(values.map(v =>
      `<option value="${v}"${v === current ? ' selected' : ''}>${v}</option>`)).join('');
  const dirOpts = opts(allOpts.direction, cur.direction);
  const stratOpts = opts(allOpts.strategy, cur.strategy);
  const tickOpts = opts(allOpts.ticker, cur.ticker);
  const activeChip = (label, value) => value
    ? `<span style="background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb;font-size:10px;padding:2px 8px;border-radius:3px;font-weight:700">${label}=${value}</span>`
    : '';
  const activeStrip = [
    activeChip('from', cur.from), activeChip('to', cur.to),
    activeChip('dir', cur.direction), activeChip('strat', cur.strategy),
    activeChip('ticker', cur.ticker),
  ].filter(Boolean).join(' ');
  return `<div class="stats-card" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px">
    <span style="color:#8b949e;font-size:11px;font-weight:700">FILTERS</span>
    <label style="font-size:11px;color:#8b949e">From <input type="date" id="dl-stats-from" value="${cur.from || ''}" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:3px;font-size:11px"></label>
    <label style="font-size:11px;color:#8b949e">To <input type="date" id="dl-stats-to" value="${cur.to || ''}" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:3px;font-size:11px"></label>
    <label style="font-size:11px;color:#8b949e">Direction <select id="dl-stats-dir" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:3px;font-size:11px">${dirOpts}</select></label>
    <label style="font-size:11px;color:#8b949e">Strategy <select id="dl-stats-strat" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:3px;font-size:11px">${stratOpts}</select></label>
    <label style="font-size:11px;color:#8b949e">Ticker <select id="dl-stats-tick" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:3px;font-size:11px">${tickOpts}</select></label>
    <button onclick="_dlStatsApplyFilters()" style="background:#1f6feb;color:#fff;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:700">Apply</button>
    <button onclick="_dlStatsResetFilters()" style="background:#21262d;color:#8b949e;border:1px solid #30363d;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:11px">Reset</button>
    <span style="color:#8b949e;font-size:11px;margin-left:auto">Showing <b style="color:#e6edf3">${p.n_trades}</b> closed trades ${activeStrip}</span>
  </div>`;
}

function _dlStatsApplyFilters() {
  dlStatsLoad({
    from: document.getElementById('dl-stats-from').value,
    to: document.getElementById('dl-stats-to').value,
    direction: document.getElementById('dl-stats-dir').value,
    strategy: document.getElementById('dl-stats-strat').value,
    ticker: document.getElementById('dl-stats-tick').value,
  });
}

function _dlStatsResetFilters() {
  _dlStatsLoaded = false;
  _dlCurrentFilters = {};
  dlStatsLoad();
}

function _dlStatsCard(title, body) {
  return `<div class="stats-card">
    <div class="stats-card-title">${title}</div>
    ${body}
  </div>`;
}

// ─ KPI hero bar (9 tiles) ────────────────────────────────────────────────
function _dlKpiHero(kpi) {
  const winColor = kpi.win_rate >= 0.5 ? '#3fb950' : kpi.win_rate >= 0.4 ? '#d29922' : '#f85149';
  const pnlColor = (kpi.total_pnl || 0) >= 0 ? '#3fb950' : '#f85149';
  const pf = kpi.profit_factor;
  const pfColor = pf == null ? '#3fb950' : pf >= 1.5 ? '#3fb950' : pf >= 1 ? '#d29922' : '#f85149';
  const expColor = (kpi.expectancy_dollar || 0) >= 0 ? '#3fb950' : '#f85149';
  const streakLabel = kpi.current_streak.type === 'none' ? '—'
                    : (kpi.current_streak.type === 'win' ? `🔥 ${kpi.current_streak.len}W` : `⛓ ${kpi.current_streak.len}L`);
  const streakColor = kpi.current_streak.type === 'win' ? '#3fb950'
                    : kpi.current_streak.type === 'loss' ? '#f85149' : '#8b949e';
  const dollar = (v) => `${v >= 0 ? '+' : ''}$${v.toFixed(0)}`;
  const tiles = [
    {label: 'TRADES',         value: kpi.total_trades,                                  sub: ''},
    {label: 'WIN RATE',       value: `${(kpi.win_rate*100).toFixed(1)}%`, color: winColor, sub: `${Math.round(kpi.total_trades*kpi.win_rate)} W / ${kpi.total_trades - Math.round(kpi.total_trades*kpi.win_rate)} L`},
    {label: 'PROFIT FACTOR',  value: pf == null ? '∞' : pf.toFixed(2), color: pfColor, sub: pf == null ? 'no losses' : pf >= 1.5 ? 'healthy' : pf >= 1 ? 'borderline' : 'sub-1 = bleed'},
    {label: 'EXPECTANCY',     value: dollar(kpi.expectancy_dollar), color: expColor, sub: `${kpi.expectancy_r >= 0 ? '+' : ''}${kpi.expectancy_r.toFixed(2)}R / trade`},
    {label: 'AVG WIN',        value: dollar(kpi.avg_winner), color: '#3fb950', sub: ''},
    {label: 'AVG LOSS',       value: dollar(kpi.avg_loser), color: '#f85149', sub: ''},
    {label: 'TOTAL P/L',      value: dollar(kpi.total_pnl), color: pnlColor, sub: ''},
    {label: 'MAX DRAWDOWN',   value: `-$${kpi.max_dd_dollar.toFixed(0)}`, color: '#f85149', sub: ''},
    {label: 'CURRENT STREAK', value: streakLabel, color: streakColor, sub: `max W=${kpi.max_win_streak} · max L=${kpi.max_loss_streak}`},
  ];
  const html = tiles.map(t => `
    <div class="stats-kpi-tile">
      <div class="stats-kpi-label">${t.label}</div>
      <div class="stats-kpi-value" style="${t.color ? 'color:' + t.color : ''}">${t.value}</div>
      <div class="stats-kpi-sub">${t.sub || '&nbsp;'}</div>
    </div>`).join('');
  return `<div class="stats-grid" style="grid-template-columns:repeat(9, 1fr)">${html}</div>`;
}

// ─ Bucket-bar list (used for hold-time, DTE, direction, strategy, etc.) ─
function _dlBucketBars(rows, keyName) {
  if (!rows || !rows.length) return '<div style="color:#8b949e;font-size:11px">No data.</div>';
  const maxN = Math.max(...rows.map(r => r.n));
  const rowsHtml = rows.map(r => {
    const pct = maxN ? (r.n / maxN * 100) : 0;
    const wr = r.win_rate * 100;
    const wrColor = wr >= 50 ? '#3fb950' : wr >= 35 ? '#d29922' : '#f85149';
    const pnlColor = r.net_pnl >= 0 ? '#3fb950' : '#f85149';
    return `<div style="margin-bottom:6px">
      <div style="display:flex;font-size:10px;color:#8b949e;margin-bottom:2px">
        <b style="color:#e6edf3;min-width:70px">${r[keyName]}</b>
        <span>n=${r.n}</span>
        <span style="margin-left:auto;color:${wrColor};font-weight:700">${wr.toFixed(0)}%</span>
        <span style="margin-left:8px;color:${pnlColor};font-weight:700">${r.net_pnl >= 0 ? '+' : ''}$${r.net_pnl.toFixed(0)}</span>
      </div>
      <div class="stats-bar-track">
        <div class="stats-bar-fill" style="width:${pct}%;background:${wrColor}66;"></div>
      </div>
    </div>`;
  }).join('');
  return rowsHtml;
}

// ─ Ticker table (top 15 by net P/L) ─────────────────────────────────────
function _dlTickerTableHtml(rows) {
  if (!rows || !rows.length) return '<div style="color:#8b949e;font-size:11px">No data.</div>';
  const top = rows.slice(0, 15);
  const winners = top.filter(r => r.net_pnl > 0);
  const losers = rows.filter(r => r.net_pnl < 0).sort((a,b) => a.net_pnl - b.net_pnl).slice(0, 5);
  const totalPnl = rows.reduce((s,r) => s + r.net_pnl, 0);
  // Bug fix: rows is sorted by net_pnl desc (winners first), so rows.slice(0,3)
  // grabbed the top 3 winners, not the top 3 |P/L| contributors. Re-sort by
  // absolute P/L magnitude so concentration reflects real exposure (incl. losers).
  const byAbs = rows.slice().sort((a,b) => Math.abs(b.net_pnl) - Math.abs(a.net_pnl));
  const top3 = byAbs.slice(0, 3).reduce((s,r) => s + Math.abs(r.net_pnl), 0);
  const totalAbs = rows.reduce((s,r) => s + Math.abs(r.net_pnl), 0);
  const concentration = totalAbs > 0 ? (top3 / totalAbs * 100).toFixed(0) : 0;
  const concTickers = byAbs.slice(0, 3).map(r => r.ticker).join(', ');
  const concentrationFlag = concentration > 50 ? `<div style="color:#d29922;font-size:10px;margin-top:4px">⚠ Top-3 (${concTickers}) = ${concentration}% of P/L magnitude (concentration risk)</div>` : '';
  const renderRow = r => `<tr>
    <td style="font-weight:700;color:#58a6ff">${r.ticker}</td>
    <td style="text-align:right">${r.n}</td>
    <td style="text-align:right;color:${r.win_rate>=0.5?'#3fb950':'#f85149'}">${(r.win_rate*100).toFixed(0)}%</td>
    <td style="text-align:right;color:${r.net_pnl>=0?'#3fb950':'#f85149'};font-weight:700">${r.net_pnl>=0?'+':''}$${r.net_pnl.toFixed(0)}</td>
  </tr>`;
  return `<table class="stats-table">
    <thead><tr><th>Ticker</th><th style="text-align:right">N</th><th style="text-align:right">W%</th><th style="text-align:right">Net</th></tr></thead>
    <tbody>
    <tr><td colspan="4" style="font-size:9px;color:#3fb950;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;padding-top:6px">▼ Top performers</td></tr>
    ${top.slice(0, 8).map(renderRow).join('')}
    ${losers.length ? `<tr><td colspan="4" style="font-size:9px;color:#f85149;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;padding-top:8px">▼ Bleeders</td></tr>${losers.map(renderRow).join('')}` : ''}
    </tbody>
  </table>${concentrationFlag}`;
}

// ─ Equity curve canvas + drawdown shading ───────────────────────────────
function _dlEquityCurveHtml(curve) {
  if (!curve || !curve.length) return '<div style="color:#8b949e">No equity data.</div>';
  return `<canvas id="dl-equity-canvas" width="800" height="240" style="width:100%;max-width:100%;height:240px;background:#0d1117;border-radius:4px"></canvas>
    <div style="display:flex;gap:14px;font-size:10px;color:#8b949e;margin-top:6px">
      <span><span style="display:inline-block;width:10px;height:2px;background:#58a6ff;vertical-align:middle"></span> cumulative P/L</span>
      <span><span style="display:inline-block;width:10px;height:2px;background:rgba(248,81,73,0.5)"></span> underwater drawdown</span>
    </div>`;
}

function _dlDrawEquityCurve(curve) {
  const c = document.getElementById('dl-equity-canvas');
  if (!c || !curve || !curve.length) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 40;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);
  const cumValues = curve.map(p => p.cumulative);
  const ddValues = curve.map(p => p.drawdown); // already negative
  const vmax = Math.max(0, ...cumValues);
  const vmin = Math.min(0, ...ddValues);
  const span = Math.max(1, vmax - vmin);
  const sx = i => pad + (i / Math.max(1, curve.length - 1)) * (W - 2*pad);
  const sy = v => H - pad - (v - vmin) / span * (H - 2*pad);
  // Zero line
  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, sy(0)); ctx.lineTo(W-pad, sy(0)); ctx.stroke();
  // Drawdown shading (under zero line)
  ctx.fillStyle = 'rgba(248,81,73,0.18)';
  ctx.beginPath();
  ctx.moveTo(sx(0), sy(0));
  ddValues.forEach((v, i) => ctx.lineTo(sx(i), sy(v)));
  ctx.lineTo(sx(curve.length-1), sy(0));
  ctx.closePath(); ctx.fill();
  // Drawdown line (red)
  ctx.strokeStyle = 'rgba(248,81,73,0.6)'; ctx.lineWidth = 1;
  ctx.beginPath();
  ddValues.forEach((v, i) => { const x=sx(i), y=sy(v); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.stroke();
  // Cumulative P/L line (blue)
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2;
  ctx.beginPath();
  cumValues.forEach((v, i) => { const x=sx(i), y=sy(v); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.stroke();
  // Y-axis labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui';
  ctx.fillText('$' + vmax.toFixed(0), 4, 14);
  ctx.fillText('$0', 4, sy(0) + 3);
  if (vmin < 0) ctx.fillText('$' + vmin.toFixed(0), 4, H - 6);
  // Last value annotation
  const last = cumValues[cumValues.length - 1];
  ctx.fillStyle = last >= 0 ? '#3fb950' : '#f85149';
  ctx.font = 'bold 11px system-ui';
  ctx.fillText(`${last >= 0 ? '+' : ''}$${last.toFixed(0)}`, W - 70, sy(last) - 6);
}

// ─ R-multiple distribution histogram ────────────────────────────────────
function _dlRDistHtml(dist) {
  if (!dist) return '<div>No data.</div>';
  const maxN = Math.max(...dist.map(d => d.count), 1);
  return dist.map(d => {
    const pct = (d.count / maxN) * 100;
    const isWin = d.lo != null && d.lo >= 0;
    const color = isWin ? '#3fb950' : '#f85149';
    return `<div style="display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:3px">
      <span style="min-width:90px;color:#e6edf3">${d.bucket}</span>
      <div class="stats-bar-track" style="flex:1;height:14px">
        <div class="stats-bar-fill" style="width:${pct}%;background:${color}66"></div>
      </div>
      <span style="min-width:30px;text-align:right;color:#8b949e">${d.count}</span>
    </div>`;
  }).join('');
}

// ─ Calendar heatmap (last 90 days) ──────────────────────────────────────
function _dlCalendarHtml(cal) {
  if (!cal || !Object.keys(cal).length) return '<div style="color:#8b949e">No data.</div>';
  // Render last 12 weeks worth of cells, anchored to today
  const today = new Date();
  const days = 84; // 12 weeks
  const start = new Date(today.getTime() - days * 86400000);
  // Align to Monday
  const dow = start.getDay() || 7;
  start.setDate(start.getDate() - (dow - 1));
  const cells = [];
  const allPnl = Object.values(cal).map(v => v.pnl);
  const maxAbs = Math.max(1, ...allPnl.map(p => Math.abs(p)));
  const fmt = d => d.toISOString().slice(0,10);
  for (let i = 0; i < 7 * 13; i++) {
    const d = new Date(start.getTime() + i * 86400000);
    const k = fmt(d);
    const v = cal[k];
    let bg = '#161b22';
    if (v) {
      const intensity = Math.min(1, Math.abs(v.pnl) / maxAbs);
      const hex = v.pnl >= 0 ? '63,185,80' : '248,81,73';
      bg = `rgba(${hex},${0.2 + intensity*0.7})`;
    }
    const tip = v ? `${k}: ${v.pnl>=0?'+':''}$${v.pnl.toFixed(0)} (${v.count} trade${v.count!==1?'s':''})` : k;
    cells.push(`<div title="${tip}" style="width:14px;height:14px;background:${bg};border-radius:2px"></div>`);
  }
  return `<div style="display:grid;grid-template-columns:repeat(13, 14px);grid-auto-flow:column;grid-template-rows:repeat(7, 14px);gap:3px">
    ${cells.join('')}
  </div>
  <div style="margin-top:10px;display:flex;gap:6px;font-size:10px;color:#8b949e;align-items:center">
    Less
    <div style="width:10px;height:10px;background:rgba(248,81,73,0.9);border-radius:2px"></div>
    <div style="width:10px;height:10px;background:rgba(248,81,73,0.4);border-radius:2px"></div>
    <div style="width:10px;height:10px;background:#161b22;border-radius:2px"></div>
    <div style="width:10px;height:10px;background:rgba(63,185,80,0.4);border-radius:2px"></div>
    <div style="width:10px;height:10px;background:rgba(63,185,80,0.9);border-radius:2px"></div>
    More · 12-week view (loss → no-trade → win)
  </div>`;
}

// ─ Recommendations panel (auto-generated, priority-ordered) ──────────────
function _dlRecommendationsHtml(recs) {
  if (!recs || !recs.length) return '';
  const colorByPri = {critical:'#f85149', high:'#d29922', medium:'#58a6ff', low:'#8b949e'};
  const items = recs.map(r => {
    const c = colorByPri[r.priority] || '#8b949e';
    return `<div style="background:#0d1117;border:1px solid ${c}66;border-left:4px solid ${c};border-radius:5px;padding:9px 12px;margin-bottom:6px">
      <div style="font-size:12px;color:#e6edf3;font-weight:600">${r.message}</div>
      <div style="font-size:10px;color:#8b949e;margin-top:3px">↳ ${r.evidence}</div>
    </div>`;
  }).join('');
  return `<div class="stats-card" style="margin-bottom:12px">
    <div class="stats-card-title">🎯 Auto-Recommendations <span style="font-weight:400;text-transform:none;color:#8b949e">(${recs.length} actionable items based on your data)</span></div>
    ${items}
  </div>`;
}

// ─ Monthly Goal Tracker ─────────────────────────────────────────────────
function _dlMonthlyProgressHtml(m) {
  if (!m) return '';
  // User-configurable goals (localStorage). Backend returns raw stats only.
  const g = _dlGetGoals();
  const pnlPct = Math.max(0, Math.min(150, m.current_pnl / g.target_pnl * 100 || 0));
  const tcPct  = Math.max(0, Math.min(120, m.n_trades / g.trade_cap * 100 || 0));
  const wrPct  = (m.current_win_rate || 0) * 100;
  const wrFloor = g.win_rate_floor * 100;
  const pnlColor = m.current_pnl >= g.target_pnl ? '#3fb950'
                  : m.current_pnl >= 0 ? '#d29922' : '#f85149';
  const tcColor  = m.n_trades > g.trade_cap ? '#f85149'
                  : m.n_trades > g.trade_cap * 0.85 ? '#d29922' : '#3fb950';
  const wrColor  = wrPct >= wrFloor ? '#3fb950'
                  : wrPct >= wrFloor - 10 ? '#d29922' : '#f85149';
  const dollar = v => `${v >= 0 ? '+' : ''}$${Math.round(v)}`;
  // Pace "if I keep going at this rate" projection
  const paceColor = m.pace_pnl_eom >= g.target_pnl ? '#3fb950'
                   : m.pace_pnl_eom >= 0 ? '#d29922' : '#f85149';
  const cell = (label, value, sub, color) => `<div style="background:#0d1117;border:1px solid #30363d;border-radius:5px;padding:8px 10px">
    <div style="font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:0.4px">${label}</div>
    <div style="font-size:16px;font-weight:700;color:${color || '#e6edf3'};margin-top:2px">${value}</div>
    <div style="font-size:10px;color:#8b949e">${sub || '&nbsp;'}</div>
  </div>`;
  return `<div class="stats-card" style="margin-bottom:12px">
    <div class="stats-card-title" style="display:flex;align-items:center">
      🎯 Monthly Goal Tracker — ${m.month}
      <span style="margin-left:auto;font-weight:400;text-transform:none;color:#8b949e">
        Day ${m.days_elapsed} / ${m.days_in_month} ·
        <button onclick="_dlEditGoals()" style="background:none;border:none;color:#58a6ff;cursor:pointer;font-size:11px;font-weight:600">⚙ targets</button>
      </span>
    </div>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
      ${cell('P/L', dollar(m.current_pnl), `target ${dollar(g.target_pnl)}`, pnlColor)}
      ${cell('Pace EOM', dollar(m.pace_pnl_eom), 'projected end-of-month', paceColor)}
      ${cell('Trades', `${m.n_trades} / ${g.trade_cap}`, m.n_trades > g.trade_cap ? '⚠ over cap' : 'within cap', tcColor)}
      ${cell('Win Rate', `${wrPct.toFixed(0)}%`, `floor ${wrFloor.toFixed(0)}%`, wrColor)}
    </div>
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:8px">
      <div>
        <div style="font-size:10px;color:#8b949e">P/L PROGRESS</div>
        <div class="stats-bar-track" style="height:10px;margin-top:2px"><div class="stats-bar-fill" style="width:${pnlPct}%;background:${pnlColor}99"></div></div>
      </div>
      <div>
        <div style="font-size:10px;color:#8b949e">TRADE COUNT</div>
        <div class="stats-bar-track" style="height:10px;margin-top:2px"><div class="stats-bar-fill" style="width:${tcPct}%;background:${tcColor}99"></div></div>
      </div>
    </div>
  </div>`;
}

function _dlEditGoals() {
  const cur = _dlGetGoals();
  const t = prompt('Monthly P/L target (USD):', cur.target_pnl);
  if (t === null) return;
  const c = prompt('Max trades per month:', cur.trade_cap);
  if (c === null) return;
  const w = prompt('Min win rate %:', Math.round(cur.win_rate_floor * 100));
  if (w === null) return;
  const goals = {
    target_pnl: Number(t) || 1000,
    trade_cap: Math.max(1, Math.round(Number(c) || 30)),
    win_rate_floor: Math.max(0, Math.min(1, Number(w) / 100)) || 0.5,
  };
  localStorage.setItem('dl_goals', JSON.stringify(goals));
  // Goals are evaluated client-side, so just re-render with cached data
  if (_dlStatsPayload) _dlStatsRender(_dlStatsPayload);
}

function _dlGetGoals() {
  try {
    const g = JSON.parse(localStorage.getItem('dl_goals') || '{}');
    return {
      target_pnl: g.target_pnl ?? 1000,
      trade_cap: g.trade_cap ?? 30,
      win_rate_floor: g.win_rate_floor ?? 0.50,
    };
  } catch (e) {
    return {target_pnl: 1000, trade_cap: 30, win_rate_floor: 0.50};
  }
}

// ─ Weekly comparison (last 7d vs prior 7d) ───────────────────────────────
function _dlWeeklyHtml(w) {
  if (!w) return '';
  if (w.last_7d.n === 0 && w.prior_7d.n === 0) return '';
  const delta = w.delta_pnl || 0;
  const arrow = delta >= 0 ? '🟢' : '🔴';
  const dColor = delta >= 0 ? '#3fb950' : '#f85149';
  const cohort = (label, c, color) => `<div style="flex:1">
    <div style="font-size:10px;color:${color};font-weight:700;text-transform:uppercase">${label}</div>
    <div style="font-size:18px;font-weight:700;color:${c.net_pnl>=0?'#3fb950':'#f85149'};margin:2px 0">${c.net_pnl>=0?'+':''}$${c.net_pnl.toFixed(0)}</div>
    <div style="font-size:11px;color:#8b949e">${c.n} trades · ${(c.win_rate*100).toFixed(0)}% W</div>
  </div>`;
  return `<div class="stats-card" style="margin-bottom:12px">
    <div class="stats-card-title">📅 7-Day Rolling — Last Week vs Prior Week</div>
    <div style="display:flex;gap:14px;align-items:center">
      ${cohort('Last 7 days', w.last_7d, '#58a6ff')}
      <div style="font-size:24px">${arrow}</div>
      ${cohort('Prior 7 days', w.prior_7d, '#8b949e')}
      <div style="text-align:right">
        <div style="font-size:10px;color:#8b949e;text-transform:uppercase">DELTA</div>
        <div style="font-size:18px;font-weight:700;color:${dColor}">${delta>=0?'+':''}$${delta.toFixed(0)}</div>
      </div>
    </div>
  </div>`;
}

// ─ Lessons aggregation (token frequency from journal text) ───────────────
function _dlLessonsHtml(lessons) {
  if (!lessons || !lessons.tokens || !lessons.tokens.length) {
    return `<div class="stats-card" style="margin-top:12px">
      <div class="stats-card-title">📝 Lessons Aggregation</div>
      <div style="color:#8b949e;font-size:11px">
        Fill <b>Lesson Learned</b> on close reviews — recurring words/phrases
        from your reflections will surface here so you can spot patterns.
      </div>
    </div>`;
  }
  const maxN = Math.max(...lessons.tokens.map(t => t.count), 1);
  const tags = lessons.tokens.map(t => {
    const intensity = t.count / maxN;
    const fontSize = 11 + Math.round(intensity * 8); // 11..19px
    const opacity = 0.5 + intensity * 0.5;
    return `<span style="display:inline-block;font-size:${fontSize}px;padding:3px 8px;margin:3px;background:#21262d;border:1px solid #30363d;border-radius:14px;color:#e6edf3;opacity:${opacity}">
      ${t.token} <span style="color:#8b949e;font-size:0.85em">×${t.count}</span>
    </span>`;
  }).join('');
  return `<div class="stats-card" style="margin-top:12px">
    <div class="stats-card-title">📝 Lessons Aggregation
      <span style="font-weight:400;text-transform:none;color:#8b949e">(${lessons.n_sources} entries with reflection text)</span>
    </div>
    <div style="line-height:2">${tags}</div>
  </div>`;
}

// ─ Edge Finder list (top or bottom) ──────────────────────────────────────
function _dlEdgeHtml(rows, sign) {
  if (!rows || !rows.length) {
    return `<div style="color:#8b949e;font-size:11px">
      Need at least 5 trades per combination to surface a statistically
      meaningful edge. Keep trading and tagging — patterns appear here.
    </div>`;
  }
  const head = sign === '+'
    ? '<div style="color:#3fb950;font-size:10px;margin-bottom:6px">Combinations with the highest expectancy. Lean into these.</div>'
    : '<div style="color:#f85149;font-size:10px;margin-bottom:6px">Combinations bleeding the most. Stop doing these or shrink size.</div>';
  return head + rows.map(r => {
    const expColor = r.expectancy >= 0 ? '#3fb950' : '#f85149';
    const wrColor = r.win_rate >= 0.5 ? '#3fb950' : r.win_rate >= 0.35 ? '#d29922' : '#f85149';
    return `<div style="background:#0d1117;border:1px solid #30363d;border-radius:5px;padding:8px 10px;margin-bottom:6px">
      <div style="font-size:11px;color:#e6edf3;font-weight:600">${r.label}</div>
      <div style="display:flex;font-size:10px;color:#8b949e;margin-top:4px;gap:12px">
        <span>n=<b style="color:#e6edf3">${r.n}</b></span>
        <span>W%=<b style="color:${wrColor}">${(r.win_rate*100).toFixed(0)}%</b></span>
        <span style="margin-left:auto">expect <b style="color:${expColor}">${r.expectancy>=0?'+':''}$${r.expectancy.toFixed(0)}</b>/trade</span>
        <span>net <b style="color:${expColor}">${r.net_pnl>=0?'+':''}$${r.net_pnl.toFixed(0)}</b></span>
      </div>
    </div>`;
  }).join('');
}

// ─ Max-loss / max-profit realization frequencies ─────────────────────────
function _dlMaxLossFreqHtml(m) {
  if (!m || !m.n_measured) {
    return '<div style="color:#8b949e;font-size:11px">No measurable trades yet (only verticals with computable max-loss).</div>';
  }
  return `<div style="font-size:11px;color:#8b949e;line-height:1.7">
    <div>Trades measured: <b style="color:#e6edf3">${m.n_measured}</b></div>
    <div style="margin-top:6px">
      <div>🛑 Hit ≥95% of max loss: <b style="color:#f85149">${m.full_max_loss}</b>
        <span style="color:#666">(${(m.full_max_loss_rate*100).toFixed(1)}%)</span></div>
      <div>🏆 Captured ≥80% of max profit: <b style="color:#3fb950">${m.near_max_profit}</b>
        <span style="color:#666">(${(m.near_max_profit_rate*100).toFixed(1)}%)</span></div>
    </div>
    <div style="margin-top:8px;font-size:10px;color:#8b949e">
      Per tasty research, the typical optimal: close at 50-60% of max profit, exit before max loss with rules.
    </div>
  </div>`;
}

// ─ Earnings vs non-earnings cohort ───────────────────────────────────────
function _dlEarningsHtml(e) {
  if (!e || !e.n_tagged) {
    return `<div style="color:#8b949e;font-size:11px">
      Mark <b>Earnings Bet?</b> on open entries to surface how earnings IV-crush plays
      perform vs your other trades.
    </div>`;
  }
  const card = (label, c, color) => `<div style="background:#0d1117;border:1px solid ${color}66;border-radius:5px;padding:10px;flex:1">
    <div style="font-size:10px;color:${color};font-weight:700;text-transform:uppercase;letter-spacing:0.5px">${label}</div>
    <div style="font-size:18px;color:${c.net_pnl>=0?'#3fb950':'#f85149'};font-weight:700;margin:4px 0">${c.net_pnl>=0?'+':''}$${c.net_pnl.toFixed(0)}</div>
    <div style="font-size:11px;color:#8b949e">${c.n} trades · ${(c.win_rate*100).toFixed(0)}% W · ${c.avg_r >= 0?'+':''}${c.avg_r.toFixed(2)}R avg</div>
  </div>`;
  return `<div style="display:flex;gap:8px">
    ${card('📰 Earnings Plays', e.earnings, '#d29922')}
    ${card('Non-Earnings', e.non_earnings, '#1f6feb')}
  </div>`;
}

async function dlTestNotification() {
  /* Trigger a sample notification through the local Notification API.
     Visible status messages at every step so failures aren't silent. */
  const status = document.getElementById('dl-status');
  const setS = (msg, color) => { status.textContent = msg; status.style.color = color || '#8b949e'; };

  if (!('Notification' in window)) {
    setS('❌ Browser does not support Notification API.', '#f85149');
    alert('This browser does not support notifications.');
    return;
  }
  setS(`Permission state: ${Notification.permission} — requesting if needed…`);
  let perm = Notification.permission;
  if (perm === 'default') {
    try {
      perm = await Notification.requestPermission();
    } catch (e) {
      setS(`❌ requestPermission threw: ${e.message}`, '#f85149');
      alert('Permission request failed: ' + e.message);
      return;
    }
  }
  if (perm !== 'granted') {
    setS(`❌ Permission = "${perm}". macOS / browser settings → site permissions → enable.`, '#f85149');
    alert(`Notifications NOT granted (status: "${perm}").\n\nFix in System Settings → Notifications → Chrome (or your browser) → enable.\nAlso check: chrome://settings/content/notifications`);
    return;
  }

  const pending = _dlEntries.filter(e => e.status === 'pending').length;
  const body = pending > 0
    ? `${pending} trade entries waiting for your decision review`
    : 'You are all caught up — no pending entries';
  const opts = {
    body,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: 'dl-reminder-' + Date.now(),
    requireInteraction: false,
    data: { url: '/?tab=journal&sub=decisions' },
  };

  // Try direct Notification first — works even if SW is wonky / not ready.
  // (showNotification via SW is preferred for persistence but plain
  // Notification fires synchronously on macOS so it's a better smoke test.)
  let directOk = false;
  try {
    const n = new Notification('🟡 Decision Log Reminder', opts);
    n.onclick = () => { window.focus(); n.close(); };
    directOk = true;
  } catch (e) {
    setS(`⚠ Direct Notification threw: ${e.message} — trying service worker…`, '#d29922');
  }

  // Also fire via service-worker (so click handler routes through SW)
  let swOk = false;
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      const reg = await Promise.race([
        navigator.serviceWorker.ready,
        new Promise((_, rej) => setTimeout(() => rej(new Error('SW ready timeout 3s')), 3000)),
      ]);
      if (reg && typeof reg.showNotification === 'function') {
        await reg.showNotification('🟡 Decision Log (SW) Reminder', {...opts, tag: opts.tag + '-sw'});
        swOk = true;
      }
    }
  } catch (e) {
    setS(`⚠ SW path failed: ${e.message}`, '#d29922');
  }

  if (directOk || swOk) {
    setS(`✓ Notification fired (direct=${directOk}, sw=${swOk}). Check macOS Notification Center.`, '#3fb950');
  } else {
    setS('❌ All notification paths failed. See alert.', '#f85149');
    alert(['No notification could be displayed. Possible causes:',
           '1) macOS System Settings → Notifications → your browser → Allow Notifications is OFF.',
           '2) Browser is in Focus mode / Do Not Disturb.',
           '3) Notifications muted for localhost site.',
           '',
           'Try: chrome://settings/content/notifications and remove any block for localhost.'].join('\\n'));
  }
  setTimeout(() => { setS(''); }, 8000);
}

async function dlSync() {
  document.getElementById('dl-status').textContent = 'Syncing from TastyTrade...';
  const r = await fetch('/api/journal_entries/sync', {method:'POST'}).then(r => r.json());
  document.getElementById('dl-status').textContent =
    `Synced: +${r.open_added||0} open, +${r.close_added||0} close · ${r.total_trades||0} trades total`;
  setTimeout(() => { document.getElementById('dl-status').textContent = ''; }, 6000);
  dlLoad();
}

async function dlOpenDrawer(entryId) {
  // Legacy entry-by-id opener — delegate to dlOpenTrade so we always show pair if present
  return dlOpenTrade(entryId);
}

async function dlOpenTrade(entryId) {
  /* Open the full-screen overlay for a trade. Left column = decision +
     review forms (compact). Right column = TradingView chart of the
     underlying with the open/close window. */
  const overlay = document.getElementById('dl-overlay');
  const dr = document.getElementById('dl-drawer');
  overlay.style.display = 'block';
  dr.innerHTML = '<div style="padding:40px;color:#8b949e">Loading...</div>';
  _dlDrawerDirty = false;   // fresh open → no edits yet
  try {
    const r = await fetch('/api/journal_entries/' + entryId).then(r => r.json());
    if (!r.entry) { dr.innerHTML = '<div style="padding:20px;color:#f85149">Not found</div>'; return; }
    if (typeof r.account_nlv === 'number') _dlAccountNlv = r.account_nlv;
    const tradeId = r.entry.trade_id;
    const open = _dlEntries.find(x => x.trade_id === tradeId && x._kind === 'open');
    const close = _dlEntries.find(x => x.trade_id === tradeId && x._kind === 'close');
    dr.innerHTML = _dlRenderTrade(open, close);
    _dlBindTrade(open, close);
    // Attach a single 'change' / 'click' listener to track dirty state.
    // Any chip toggle, radio click, slider input, or text edit marks dirty.
    setTimeout(() => {
      const drNow = document.getElementById('dl-drawer');
      if (drNow && !drNow.dataset.dirtyHook) {
        drNow.addEventListener('input', _dlMarkDirty);
        drNow.addEventListener('change', _dlMarkDirty);
        drNow.addEventListener('click', e => {
          if (e.target.closest('.dl-chip') ||
              e.target.closest('input[type=range]')) _dlMarkDirty();
        });
        drNow.dataset.dirtyHook = '1';
      }
    }, 50);
    // Mount the TV chart after DOM is in place
    const e = open || close;
    if (e && e.ticker) {
      _dlMountTV('dl-tv', e.ticker, e.open_at, close?.close_at);
    }
    // Pull spread MTM history (hourly snapshots) and render below TV
    const histEntryId = entryId || (open && open.id) || (close && close.id);
    if (histEntryId) _dlLoadMtmHistory(histEntryId, e);
  } catch (e) { dr.innerHTML = '<div style="color:#f85149;padding:20px">' + e.message + '</div>'; }
}

// Module-scope cache so the metric tabs can switch without re-fetching
let _dlPriceHistory = null;       // {snapshots, legs}
let _dlPriceMeta = null;           // entry meta for cost-line annotation
let _dlActiveMetric = 'mtm';
let _dlAccountNlv = 0;             // current TastyTrade NLV — set on dlOpenTrade

async function _dlLoadMtmHistory(entryId, entryMeta) {
  const info = document.getElementById('dl-mtm-info');
  const canvas = document.getElementById('dl-mtm-canvas');
  if (!info || !canvas) return;
  _dlPriceHistory = null;
  _dlPriceMeta = entryMeta;
  // Reset active metric to MTM each open
  _dlActiveMetric = 'mtm';
  document.querySelectorAll('.dl-metric-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.metric === 'mtm'));
  try {
    const r = await fetch(`/api/journal_entries/${entryId}/price_history`).then(r => r.json());
    if (r.error) { info.textContent = `(${r.error})`; return; }
    _dlPriceHistory = {snapshots: r.snapshots || [], legs: r.legs || []};
    const snaps = _dlPriceHistory.snapshots;
    if (snaps.length) {
      // _dlFmt.fmtVerbose → user's local timezone (no fixed TZ baked in)
      info.innerHTML = `${snaps.length} snapshots · `
        + `first ${_dlFmt.fmtVerbose(snaps[0].ts)} · `
        + `last ${_dlFmt.fmtVerbose(snaps[snaps.length-1].ts)} `
        + `<span style="color:#666">(${_dlFmt.localTimezone()})</span>`;
    } else {
      info.textContent = 'No snapshots yet — they accumulate hourly during market hours.';
    }
    // Honour saved collapsed/expanded state. Default = collapsed so the
    // TradingView chart gets the full height by default.
    const collapsed = localStorage.getItem('dl_metrics_collapsed');
    if (collapsed === '0') _dlExpandMetricsPanel();
    else _dlCollapseMetricsPanel();
  } catch (err) {
    info.textContent = `Failed to load history: ${err.message}`;
  }
}

function _dlSetMetric(metric) {
  _dlActiveMetric = metric;
  document.querySelectorAll('.dl-metric-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.metric === metric));
  // Clicking a metric tab auto-expands the panel
  _dlExpandMetricsPanel();
  _dlRenderMetricChart();
}

function _dlToggleMetricsPanel() {
  const body = document.getElementById('dl-metric-body');
  if (!body) return;
  if (body.style.display === 'none') _dlExpandMetricsPanel();
  else _dlCollapseMetricsPanel();
}

function _dlExpandMetricsPanel() {
  const body = document.getElementById('dl-metric-body');
  const tog = document.getElementById('dl-metric-toggle');
  if (body) body.style.display = '';
  if (tog) tog.textContent = '▼';
  localStorage.setItem('dl_metrics_collapsed', '0');
  _dlRenderMetricChart();
}

function _dlCollapseMetricsPanel() {
  const body = document.getElementById('dl-metric-body');
  const tog = document.getElementById('dl-metric-toggle');
  if (body) body.style.display = 'none';
  if (tog) tog.textContent = '▶';
  localStorage.setItem('dl_metrics_collapsed', '1');
}

function _dlRenderMetricChart() {
  if (!_dlPriceHistory) return;
  const canvas = document.getElementById('dl-mtm-canvas');
  const tableEl = document.getElementById('dl-mtm-table');
  if (!canvas || !tableEl) return;
  const m = _dlActiveMetric;
  if (m === 'table') {
    canvas.style.display = 'none';
    tableEl.style.display = 'block';
    tableEl.innerHTML = _dlSnapshotTableHtml();
    return;
  }
  // All non-table tabs use the canvas
  tableEl.style.display = 'none';
  canvas.style.display = 'block';
  if (m === 'mtm') {
    const points = _dlPriceHistory.snapshots.map(s => ({ts: s.ts, value: s.mtm}));
    _dlDrawSingleSeries(canvas, points, _dlPriceMeta, {
      label: 'Spread $', costAnchor: true, format: '$',
    });
  } else if (m === 'iv') {
    const series = _dlPriceHistory.legs.map(l => ({
      label: l.label, points: (l.ivs || []).map(p => ({ts: p.ts, value: p.iv * 100})),
    }));
    _dlDrawMultiSeries(canvas, series, {label: 'IV %', format: '%'});
  } else {
    const points = _dlPriceHistory.snapshots.map(s => ({ts: s.ts, value: s[m]}));
    _dlDrawSingleSeries(canvas, points, _dlPriceMeta, {
      label: m, costAnchor: false, format: m === 'gamma' ? 'g' : '$',
    });
  }
}

function _dlSnapshotTableHtml() {
  /* Render a table: one row per snapshot, columns include each leg's IV.
     Newest first. Cost-basis comparison colors the Spread cell. */
  if (!_dlPriceHistory || !_dlPriceHistory.snapshots.length) {
    return '<div style="padding:30px;text-align:center;color:#8b949e">No snapshots yet — they accumulate hourly during market hours.</div>';
  }
  const snaps = [..._dlPriceHistory.snapshots].sort((a, b) => b.ts.localeCompare(a.ts));
  const legs = _dlPriceHistory.legs || [];
  // Build {ts → {leg.symbol: iv}} lookup
  const ivByTs = {};
  for (const l of legs) {
    for (const p of (l.ivs || [])) {
      ivByTs[p.ts] = ivByTs[p.ts] || {};
      ivByTs[p.ts][l.symbol] = p.iv;
    }
  }
  const cost = _dlPriceMeta
    ? -((_dlPriceMeta.open_price || 0) * (_dlPriceMeta.quantity || 1))
    : null;
  const fmtTs = (ts) => _dlFmt.fmtTableTs(ts);   // "MM-DD HH:MM" in user's local TZ
  const fmtPnl = (v) => `<span style="color:${v >= 0 ? '#3fb950' : '#f85149'};font-weight:700">${v >= 0 ? '+' : ''}$${v.toFixed(0)}</span>`;
  const ivCols = legs.map(l => `<th style="text-align:right;padding:4px 8px">IV ${l.label}</th>`).join('');
  const rows = snaps.map(s => {
    const ivs = ivByTs[s.ts] || {};
    const ivCells = legs.map(l => {
      const v = ivs[l.symbol];
      return `<td style="text-align:right;padding:4px 8px;color:${v ? '#e6edf3' : '#666'}">${v ? (v*100).toFixed(1)+'%' : '—'}</td>`;
    }).join('');
    const mtmColored = cost != null
      ? `<span style="color:${s.mtm >= cost ? '#3fb950' : '#f85149'};font-weight:700">$${s.mtm.toFixed(0)}</span>`
      : `$${s.mtm.toFixed(0)}`;
    const pnl = cost != null ? s.mtm - cost : null;
    return `<tr style="border-top:1px solid #21262d">
      <td style="padding:4px 8px;font-family:monospace;color:#58a6ff">${fmtTs(s.ts)}</td>
      <td style="text-align:right;padding:4px 8px">${mtmColored}</td>
      <td style="text-align:right;padding:4px 8px">${pnl != null ? fmtPnl(pnl) : '—'}</td>
      <td style="text-align:right;padding:4px 8px">${s.delta.toFixed(2)}</td>
      <td style="text-align:right;padding:4px 8px">${s.gamma.toFixed(3)}</td>
      <td style="text-align:right;padding:4px 8px;color:${s.theta >= 0 ? '#3fb950' : '#f85149'}">$${s.theta.toFixed(2)}</td>
      <td style="text-align:right;padding:4px 8px">$${s.vega.toFixed(2)}</td>
      ${ivCells}
    </tr>`;
  }).join('');
  const tzLabel = _dlFmt.localTimezone() || 'local';
  return `<table style="width:100%;border-collapse:collapse;font-size:11px">
    <thead style="position:sticky;top:0;background:#161b22;z-index:1">
      <tr style="color:#8b949e;text-transform:uppercase;font-size:10px;letter-spacing:0.4px">
        <th style="text-align:left;padding:6px 8px">Time (${tzLabel})</th>
        <th style="text-align:right;padding:6px 8px">Spread $</th>
        <th style="text-align:right;padding:6px 8px">vs Cost</th>
        <th style="text-align:right;padding:6px 8px">Δ</th>
        <th style="text-align:right;padding:6px 8px">Γ</th>
        <th style="text-align:right;padding:6px 8px">Θ /day</th>
        <th style="text-align:right;padding:6px 8px">Vega</th>
        ${ivCols}
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function _dlClearCanvas(c) {
  if (!c) return;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, c.width, c.height);
}

function _dlDrawSingleSeries(canvas, points, meta, opts) {
  const rect = canvas.getBoundingClientRect();
  canvas.width  = Math.max(200, Math.floor(rect.width));
  canvas.height = Math.max(140, Math.floor(rect.height));
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const padL = 50, padR = 50, padT = 12, padB = 22;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);
  if (!points.length) return;
  const vals = points.map(p => p.value);
  let vmax = Math.max(...vals);
  let vmin = Math.min(...vals);
  // Anchor cost basis if applicable
  const cost = (opts.costAnchor && meta) ? -((meta.open_price || 0) * (meta.quantity || 1)) : null;
  if (cost != null) {
    if (cost > vmax) vmax = cost;
    if (cost < vmin) vmin = cost;
  }
  if (vmax === vmin) { vmax = vmax + 1; vmin = vmin - 1; }
  const span = Math.max(0.0001, vmax - vmin);
  const ts = points.map(p => Date.parse(p.ts));
  const tmin = ts[0], tmax = ts[ts.length - 1];
  const tspan = Math.max(1, tmax - tmin);
  const sx = t => padL + (t - tmin) / tspan * (W - padL - padR);
  const sy = v => H - padB - (v - vmin) / span * (H - padT - padB);

  const fmt = (v) => {
    if (opts.format === '%') return v.toFixed(1) + '%';
    if (opts.format === 'g') return v.toFixed(3);
    return '$' + v.toFixed(0);
  };

  // Cost-basis horizontal line
  if (cost != null) {
    ctx.strokeStyle = '#d29922'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(padL, sy(cost)); ctx.lineTo(W - padR, sy(cost)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#d29922'; ctx.font = '9px system-ui';
    ctx.fillText('cost ' + fmt(cost), padL + 4, sy(cost) - 3);
  }
  // Zero line for non-cost charts
  if (cost == null && vmin < 0 && vmax > 0) {
    ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, sy(0)); ctx.lineTo(W - padR, sy(0)); ctx.stroke();
  }

  // Series line
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = sx(Date.parse(p.ts)), y = sy(p.value);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Dots — coloured vs cost (if applicable) else neutral
  points.forEach(p => {
    const ok = cost != null ? p.value >= cost : p.value >= 0;
    ctx.fillStyle = ok ? '#3fb950' : '#f85149';
    ctx.beginPath(); ctx.arc(sx(Date.parse(p.ts)), sy(p.value), 2.5, 0, Math.PI * 2); ctx.fill();
  });

  // Y-axis labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
  ctx.fillText(fmt(vmax), padL - 4, padT + 9);
  ctx.fillText(fmt(vmin), padL - 4, H - padB + 3);
  // Last value annotation
  const last = points[points.length - 1];
  const lastOk = cost != null ? last.value >= cost : last.value >= 0;
  ctx.fillStyle = lastOk ? '#3fb950' : '#f85149';
  ctx.font = 'bold 11px system-ui'; ctx.textAlign = 'left';
  ctx.fillText(fmt(last.value), Math.min(W - padR, sx(Date.parse(last.ts)) + 6), sy(last.value) - 5);

  // X-axis ticks
  ctx.fillStyle = '#8b949e'; ctx.font = '9px system-ui'; ctx.textAlign = 'center';
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const t = tmin + (tspan * i / ticks);
    const lbl = _dlFmt.fmtShort(new Date(t));
    ctx.fillText(lbl, sx(t), H - 6);
  }
}

function _dlDrawMultiSeries(canvas, series, opts) {
  /* Draw multiple lines (one per leg) on the same axes. */
  const rect = canvas.getBoundingClientRect();
  canvas.width  = Math.max(200, Math.floor(rect.width));
  canvas.height = Math.max(140, Math.floor(rect.height));
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const padL = 50, padR = 60, padT = 12, padB = 22;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);
  const validSeries = series.filter(s => s.points && s.points.length > 0);
  if (!validSeries.length) {
    ctx.fillStyle = '#8b949e'; ctx.font = '11px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('No data for this metric yet', W / 2, H / 2);
    return;
  }
  const allVals = validSeries.flatMap(s => s.points.map(p => p.value));
  const allTs = validSeries.flatMap(s => s.points.map(p => Date.parse(p.ts)));
  let vmax = Math.max(...allVals);
  let vmin = Math.min(...allVals);
  if (vmax === vmin) { vmax = vmax + 1; vmin = vmin - 1; }
  const span = Math.max(0.0001, vmax - vmin);
  const tmin = Math.min(...allTs), tmax = Math.max(...allTs);
  const tspan = Math.max(1, tmax - tmin);
  const sx = t => padL + (t - tmin) / tspan * (W - padL - padR);
  const sy = v => H - padB - (v - vmin) / span * (H - padT - padB);
  const fmt = v => opts.format === '%' ? v.toFixed(1) + '%'
                  : opts.format === 'g' ? v.toFixed(3)
                  : '$' + v.toFixed(0);

  const palette = ['#58a6ff', '#d29922', '#3fb950', '#f85149', '#8b949e', '#6d3eff'];
  validSeries.forEach((s, i) => {
    const color = palette[i % palette.length];
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.beginPath();
    s.points.forEach((p, j) => {
      const x = sx(Date.parse(p.ts)), y = sy(p.value);
      if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    s.points.forEach(p => {
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(sx(Date.parse(p.ts)), sy(p.value), 2, 0, Math.PI * 2); ctx.fill();
    });
    // Legend right edge
    ctx.fillStyle = color; ctx.font = '10px system-ui'; ctx.textAlign = 'left';
    ctx.fillText(s.label, W - padR + 4, padT + 12 + i * 14);
  });

  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
  ctx.fillText(fmt(vmax), padL - 4, padT + 9);
  ctx.fillText(fmt(vmin), padL - 4, H - padB + 3);
  ctx.font = '9px system-ui'; ctx.textAlign = 'center';
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const t = tmin + (tspan * i / ticks);
    const lbl = _dlFmt.fmtShort(new Date(t));
    ctx.fillText(lbl, sx(t), H - 6);
  }
}

// Legacy entry kept so the old code path doesn't blow up if anything still calls it.
function _dlDrawMtmChart(canvas, points, meta) {
  // Resize canvas to displayed pixel size for crisp rendering
  const rect = canvas.getBoundingClientRect();
  canvas.width  = Math.max(200, Math.floor(rect.width));
  canvas.height = Math.max(140, Math.floor(rect.height));
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const padL = 50, padR = 12, padT = 12, padB = 22;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);
  if (!points.length) return;
  const vals = points.map(p => p.mtm);
  let vmax = Math.max(...vals);
  let vmin = Math.min(...vals);
  if (vmax === vmin) { vmax = vmax + 50; vmin = vmin - 50; }
  // Anchor 0 line if cost basis sits in range
  const cost = -((meta?.open_price || 0) * (meta?.quantity || 1));
  if (cost && cost > vmax) vmax = cost;
  if (cost && cost < vmin) vmin = cost;
  const span = Math.max(1, vmax - vmin);
  const ts = points.map(p => Date.parse(p.ts));
  const tmin = ts[0], tmax = ts[ts.length - 1];
  const tspan = Math.max(1, tmax - tmin);
  const sx = t => padL + (t - tmin) / tspan * (W - padL - padR);
  const sy = v => H - padB - (v - vmin) / span * (H - padT - padB);

  // Cost-basis horizontal line
  if (cost) {
    ctx.strokeStyle = '#d29922'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(padL, sy(cost)); ctx.lineTo(W - padR, sy(cost)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#d29922'; ctx.font = '9px system-ui';
    ctx.fillText('cost $' + cost.toFixed(0), padL + 4, sy(cost) - 3);
  }

  // MTM line
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = sx(Date.parse(p.ts)), y = sy(p.mtm);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Dots
  points.forEach(p => {
    ctx.fillStyle = (cost && p.mtm >= cost) ? '#3fb950' : '#f85149';
    ctx.beginPath(); ctx.arc(sx(Date.parse(p.ts)), sy(p.mtm), 2.5, 0, Math.PI * 2); ctx.fill();
  });

  // Y-axis labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui'; ctx.textAlign = 'right';
  ctx.fillText('$' + vmax.toFixed(0), padL - 4, padT + 9);
  ctx.fillText('$' + vmin.toFixed(0), padL - 4, H - padB + 3);
  ctx.textAlign = 'left';
  // Last value annotation
  const last = points[points.length - 1];
  ctx.fillStyle = (cost && last.mtm >= cost) ? '#3fb950' : '#f85149';
  ctx.font = 'bold 11px system-ui';
  ctx.fillText('$' + last.mtm.toFixed(0), W - padR - 50, sy(last.mtm) - 5);

  // X-axis: a few date ticks
  ctx.fillStyle = '#8b949e'; ctx.font = '9px system-ui'; ctx.textAlign = 'center';
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const t = tmin + (tspan * i / ticks);
    const lbl = _dlFmt.fmtShort(new Date(t));
    ctx.fillText(lbl, sx(t), H - 6);
  }
}

let _dlDrawerDirty = false;
function _dlMarkDirty() { _dlDrawerDirty = true; }

async function dlCloseDrawer() {
  if (_dlDrawerDirty) {
    const yes = confirm(['You have unsaved changes.', '',
      'OK  → save changes then close',
      'Cancel → discard changes and close'].join('\\n'));
    if (yes) {
      const dr = document.getElementById('dl-drawer');
      const ids = [dr.dataset.openEntryId, dr.dataset.closeEntryId].filter(Boolean);
      _dlDrawerDirty = false;
      // dlSaveTrade calls dlCloseDrawer at the end → reentry will skip the
      // dirty-prompt because the flag is already cleared.
      return dlSaveTrade(...ids);
    }
    // User chose Cancel → fall through, discard changes
  }
  _dlDrawerDirty = false;
  const ov = document.getElementById('dl-overlay');
  if (ov) ov.style.display = 'none';
  // Clean up TV iframe (otherwise the next mount overlays it)
  const tv = document.getElementById('dl-tv');
  if (tv) tv.innerHTML = '';
  dlLoad();   // refresh list
}

// Inject a TradingView Advanced Chart widget into the given container.
// Range chosen automatically from trade open/close span.
function _dlMountTV(containerId, ticker, openAtIso, closeAtIso) {
  const container = document.getElementById(containerId);
  if (!container) return;
  while (container.firstChild) container.removeChild(container.firstChild);
  // Compute the duration (in days) so the chart auto-zooms to a useful window
  let rangeDays = 30;
  if (openAtIso) {
    const o = new Date(openAtIso);
    const c = closeAtIso ? new Date(closeAtIso) : new Date();
    const days = Math.max(1, Math.round((c - o) / 86400000));
    rangeDays = Math.max(15, Math.min(180, days * 3));
  }
  const range = rangeDays <= 30 ? '1M' : rangeDays <= 90 ? '3M' : '6M';
  const cfg = {
    symbol: ticker,
    interval: 'D',
    timezone: 'America/New_York',
    theme: 'dark',
    style: '1',
    locale: 'en',
    backgroundColor: '#0d1117',
    gridColor: 'rgba(48,54,61,0.6)',
    range,
    allow_symbol_change: false,
    hide_top_toolbar: false,
    hide_side_toolbar: false,   // ← keep drawing tools sidebar visible
    save_image: false,
    autosize: true,
  };
  const outer = document.createElement('div');
  outer.className = 'tradingview-widget-container';
  outer.style.cssText = 'height:100%;width:100%';
  const inner = document.createElement('div');
  inner.className = 'tradingview-widget-container__widget';
  inner.style.cssText = 'height:100%;width:100%';
  outer.appendChild(inner);
  container.appendChild(outer);
  const script = document.createElement('script');
  script.type = 'text/javascript';
  script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
  script.async = true;
  script.textContent = JSON.stringify(cfg);
  outer.appendChild(script);
}

function _dlChipsHtml(section, selected) {
  const opts = _dlOptions[section] || {defaults:[], custom:[]};
  let h = '<div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center">';
  for (const v of opts.defaults) {
    const on = (selected || []).includes(v);
    h += `<span class="dl-chip ${on?'active':''}" onclick="_dlToggleChip('${section}','${v.replace(/'/g,"\\'")}', this)">${v}</span>`;
  }
  for (const v of opts.custom) {
    const on = (selected || []).includes(v);
    h += `<span class="dl-chip ${on?'active':''}" onclick="_dlToggleChip('${section}','${v.replace(/'/g,"\\'")}', this)">${v} <span class="x" onclick="event.stopPropagation();_dlRemoveCustom('${section}','${v.replace(/'/g,"\\'")}')">×</span></span>`;
  }
  h += `<input class="dl-add-input" placeholder="+ add..." onkeydown="if(event.key==='Enter'){_dlAddCustom('${section}',this.value);this.value='';}">`;
  h += '</div>';
  return h;
}

function _dlToggleChip(section, value, el) {
  el.classList.toggle('active');
  // Track in scope dataset (per-panel). Save reads from same scope.
  const scope = _dlScope(el);
  scope.dataset[section] = JSON.stringify(
    Array.from(scope.querySelectorAll(`[data-section="${section}"] .dl-chip.active`))
      .map(c => c.firstChild.textContent.trim().replace(/ ×$/, ''))
      .filter(Boolean)
  );
}

async function _dlAddCustom(section, value) {
  value = (value || '').trim();
  if (!value) return;
  const r = await fetch('/api/journal_entries/options/' + section + '/add', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({value})
  });
  if (!r.ok) return;
  // Update local options cache
  _dlOptions = await fetch('/api/journal_entries/options').then(r => r.json());
  // Insert chip in-place into all matching section blocks (open + close panels)
  // WITHOUT re-rendering the drawer — this preserves all unsaved chip selections,
  // sliders, textarea/input values that the user has typed.
  const drawer = document.getElementById('dl-drawer');
  const ve = value.replace(/'/g, "\\'");
  const escAttr = value.replace(/"/g, '&quot;');
  const chipExists = (container) =>
    Array.from(container.querySelectorAll('.dl-chip')).some(
      c => c.firstChild && c.firstChild.textContent.trim().replace(/ ×$/, '') === value
    );
  // Multi-select sections (data-section) — chips wrapped in inner flex div
  drawer.querySelectorAll(`[data-section="${section}"] > div`).forEach(container => {
    if (chipExists(container)) return;
    const chip = document.createElement('span');
    chip.className = 'dl-chip';
    chip.setAttribute('onclick', `_dlToggleChip('${section}','${ve}', this)`);
    chip.innerHTML = `${escAttr} <span class="x" onclick="event.stopPropagation();_dlRemoveCustom('${section}','${ve}')">×</span>`;
    const input = container.querySelector('.dl-add-input');
    if (input) { container.insertBefore(chip, input); input.value = ''; }
    else container.appendChild(chip);
  });
  // Single-select sections (data-radio) — chips are direct children
  drawer.querySelectorAll(`[data-radio="${section}"]`).forEach(container => {
    if (chipExists(container)) return;
    const chip = document.createElement('span');
    chip.className = 'dl-chip';
    chip.setAttribute('onclick', `_dlSetRadio('${section}','${ve}',this)`);
    chip.innerHTML = `${escAttr} <span class="x" onclick="event.stopPropagation();_dlRemoveCustom('${section}','${ve}')">×</span>`;
    const input = container.querySelector('.dl-add-input');
    if (input) { container.insertBefore(chip, input); input.value = ''; }
    else container.appendChild(chip);
  });
}

async function _dlRemoveCustom(section, value) {
  if (!confirm('Delete chip: ' + value + '?')) return;
  const r = await fetch('/api/journal_entries/options/' + section + '/' + encodeURIComponent(value), {method:'DELETE'});
  if (!r.ok) return;
  _dlOptions = await fetch('/api/journal_entries/options').then(r => r.json());
  // Remove chips in-place across all panels — preserve the rest of the form state.
  const drawer = document.getElementById('dl-drawer');
  const containers = drawer.querySelectorAll(
    `[data-section="${section}"] > div, [data-radio="${section}"]`
  );
  containers.forEach(container => {
    Array.from(container.querySelectorAll('.dl-chip')).forEach(c => {
      const label = c.firstChild && c.firstChild.textContent.trim().replace(/ ×$/, '');
      if (label === value) {
        // If this chip was active and it's a multi-select, also drop it from
        // the panel dataset so save doesn't see a phantom.
        if (c.classList.contains('active')) {
          const scope = _dlScope(c);
          const sec = container.closest('[data-section]')?.dataset.section
                   || container.dataset.radio;
          if (sec && container.closest('[data-section]')) {
            try {
              const arr = JSON.parse(scope.dataset[sec] || '[]').filter(x => x !== value);
              scope.dataset[sec] = JSON.stringify(arr);
            } catch (e) {}
          } else if (sec && scope.dataset[sec] === value) {
            delete scope.dataset[sec];
          }
        }
        c.remove();
      }
    });
  });
}

function _dlComputeRiskReward(e) {
  /* For 2-leg vertical spreads only. Returns null otherwise.
     Per-spread net (e.open_price): negative = debit, positive = credit.
     Strike-width × $100 = max gross at expiration (if going to full value). */
  const legs = e.legs || [];
  if (legs.length !== 2) return null;
  if (legs[0].type !== legs[1].type || legs[0].exp !== legs[1].exp) return null;
  const sortedLegs = [...legs].sort((a, b) => a.strike - b.strike);
  const widthDollars = (sortedLegs[1].strike - sortedLegs[0].strike) * 100;
  const perSpread = e.open_price || 0;
  const qty = e.quantity || 1;
  if (perSpread < 0) {
    // Debit spread
    const maxLossPer = Math.abs(perSpread);
    const maxProfitPer = widthDollars - maxLossPer;
    return {
      type: 'debit',
      maxLossPer: -maxLossPer, maxProfitPer,
      maxLossTotal: -maxLossPer * qty, maxProfitTotal: maxProfitPer * qty,
      rr: maxProfitPer / maxLossPer,
    };
  }
  if (perSpread > 0) {
    // Credit spread
    const maxProfitPer = perSpread;
    const maxLossPer = widthDollars - perSpread;
    return {
      type: 'credit',
      maxLossPer: -maxLossPer, maxProfitPer,
      maxLossTotal: -maxLossPer * qty, maxProfitTotal: maxProfitPer * qty,
      rr: maxProfitPer / maxLossPer,
    };
  }
  return null;
}

function _dlComputeDuration(e, closeAt) {
  if (!e || !e.open_at) return null;
  const o = new Date(e.open_at);
  const c = closeAt ? new Date(closeAt) : new Date();
  const ms = c - o;
  if (isNaN(ms)) return null;
  const days = ms / 86400000;
  if (days < 1) {
    const hours = Math.round(days * 24);
    return hours + 'h';
  }
  return Math.round(days) + 'd';
}

function _dlMetaHtml(e, closeEntry) {
  const dirColor = e.direction === 'BULL' ? '#3fb950'
                : e.direction === 'BEAR' ? '#f85149'
                : e.direction === 'NEUTRAL' ? '#d29922' : '#8b949e';
  const dirHtml = e.direction
    ? `<span style="background:${dirColor}22;color:${dirColor};border:1px solid ${dirColor}66;font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;margin-right:8px">${e.direction}</span>` : '';
  const perSpread = (e.open_price || 0);
  const perSpreadStr = perSpread === 0 ? '—'
    : (perSpread < 0 ? `<span style="color:#f85149">$${Math.abs(perSpread).toFixed(2)} debit</span>`
                      : `<span style="color:#3fb950">$${perSpread.toFixed(2)} credit</span>`);

  // Risk/Reward + max profit/loss
  const rr = _dlComputeRiskReward(e);
  let rrHtml = '';
  if (rr) {
    const lossColor = '#f85149', profitColor = '#3fb950';
    // % of capital based on current TastyTrade NLV
    const pctOfNlv = (_dlAccountNlv > 0)
      ? (Math.abs(rr.maxLossTotal) / _dlAccountNlv * 100)
      : null;
    const pctOfNlvHtml = pctOfNlv != null
      ? `<div style="color:${pctOfNlv >= 5 ? '#f85149' : pctOfNlv >= 2 ? '#d29922' : '#8b949e'};font-size:10px;font-weight:700">${pctOfNlv.toFixed(2)}% of NLV</div>`
      : '';
    rrHtml = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px;font-size:11px">
        <div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px">
          <div style="color:#8b949e;font-size:10px">MAX LOSS</div>
          <div style="color:${lossColor};font-weight:700">$${rr.maxLossTotal.toFixed(0)}</div>
          <div style="color:#8b949e;font-size:9px">$${rr.maxLossPer.toFixed(0)}/spread</div>
          ${pctOfNlvHtml}
        </div>
        <div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px">
          <div style="color:#8b949e;font-size:10px">MAX PROFIT</div>
          <div style="color:${profitColor};font-weight:700">$${rr.maxProfitTotal.toFixed(0)}</div>
          <div style="color:#8b949e;font-size:9px">$${rr.maxProfitPer.toFixed(0)}/spread</div>
        </div>
        <div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px">
          <div style="color:#8b949e;font-size:10px">R:R RATIO</div>
          <div style="color:${rr.rr >= 1.5 ? profitColor : '#d29922'};font-weight:700">${rr.rr.toFixed(2)}</div>
          <div style="color:#8b949e;font-size:9px">${rr.type}</div>
        </div>
      </div>`;
  }

  // Outcome strip (P/L + Duration + Realized %)
  let outcomeHtml = '';
  if (closeEntry) {
    const pnl = closeEntry.net_pnl;
    const pnlColor = pnl >= 0 ? '#3fb950' : '#f85149';
    const dur = _dlComputeDuration(e, closeEntry.close_at || closeEntry.close_date);
    let realizedPct = '';
    if (rr && pnl != null) {
      const denom = pnl >= 0 ? rr.maxProfitTotal : Math.abs(rr.maxLossTotal);
      if (denom > 0) {
        const pct = (pnl / denom) * 100;
        realizedPct = `<div style="color:#8b949e;font-size:9px">${pct.toFixed(0)}% of ${pnl>=0?'max profit':'max loss'}</div>`;
      }
    }
    outcomeHtml = `
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:6px;font-size:11px">
        <div style="background:#0d1117;border:1px solid ${pnlColor}66;border-radius:4px;padding:6px 8px">
          <div style="color:#8b949e;font-size:10px">REALIZED P/L</div>
          <div style="color:${pnlColor};font-weight:700;font-size:14px">${pnl>=0?'+':''}$${pnl.toFixed(0)}</div>
          ${realizedPct}
        </div>
        <div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:6px 8px">
          <div style="color:#8b949e;font-size:10px">HELD</div>
          <div style="color:#e6edf3;font-weight:700;font-size:14px">${dur || '—'}</div>
          <div style="color:#8b949e;font-size:9px">${e.open_date} → ${closeEntry.close_date}</div>
        </div>
      </div>`;
  } else {
    // Active position — just duration so far
    const dur = _dlComputeDuration(e, null);
    if (dur) {
      outcomeHtml = `
        <div style="margin-top:6px;font-size:11px;color:#8b949e">⏱ Held: <b style="color:#e6edf3">${dur}</b> so far</div>`;
    }
  }

  const legs = e.legs || e.close_legs || [];
  const legsTable = legs.length ? `
    <table style="width:100%;font-size:10px;border-collapse:collapse;margin-top:8px;background:#0d1117;border:1px solid #30363d;border-radius:4px">
      <thead><tr style="background:#161b22">
        <th style="padding:3px 6px;text-align:left;color:#8b949e;font-weight:600">#</th>
        <th style="padding:3px 6px;text-align:left;color:#8b949e;font-weight:600">Action</th>
        <th style="padding:3px 6px;text-align:right;color:#8b949e;font-weight:600">Strike</th>
        <th style="padding:3px 6px;text-align:right;color:#8b949e;font-weight:600">Qty</th>
        <th style="padding:3px 6px;text-align:right;color:#8b949e;font-weight:600">Price</th>
      </tr></thead>
      <tbody>
      ${legs.map((l, i) => `<tr style="border-top:1px solid #21262d">
          <td style="padding:3px 6px;font-family:monospace;color:#58a6ff">${i+1}</td>
          <td style="padding:3px 6px">${l.action || ''}</td>
          <td style="padding:3px 6px;text-align:right;font-weight:600">${l.strike}${l.type || ''}</td>
          <td style="padding:3px 6px;text-align:right">${l.quantity}</td>
          <td style="padding:3px 6px;text-align:right;color:#8b949e">$${(l.price || 0).toFixed(2)}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '';
  return `
    <div class="dl-readonly">
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">
        ${dirHtml}
        <b style="font-size:15px">${e.ticker}</b>
        <span style="color:#e6edf3;font-weight:600">${e.spread_label || ''}</span>
        <span style="color:#8b949e">×${e.quantity || '?'}</span>
        <span style="color:#8b949e;background:#161b22;border:1px solid #30363d;padding:1px 6px;border-radius:3px;font-size:10px">${e.strategy || ''}</span>
      </div>
      <div style="margin-top:6px;color:#8b949e;font-size:11px">
        Open ${e.open_date || ''} · per-spread ${perSpreadStr}
      </div>
      ${rrHtml}
      ${outcomeHtml}
      ${legsTable}
    </div>
  `;
}

function _dlSidePanel(side, entry) {
  /* Wrap a form in a scoped panel. Each panel has its own dataset for radio
     state, its own thesis/notes/lesson inputs, and its own entry-id so the
     save logic can patch each side independently. */
  const isOpen = side === 'open';
  const accent = isOpen ? '#1f6feb' : '#6d3eff';
  const title = isOpen ? '📝 Open Decision' : '📕 Close Review';
  // status === 'pending' here means the journal entry hasn't been filled out by the
  // user yet — NOT that the broker order is unfilled. Old label "pending fill" was
  // misleading (looked like an open order).
  const fillStatus = entry.status === 'pending'
    ? '<span style="font-size:10px;color:#d29922">📝 needs review</span>'
    : '<span style="font-size:10px;color:#3fb950">✓ reviewed</span>';
  const formHtml = isOpen ? _dlRenderOpenForm(entry) : _dlRenderCloseForm(entry);
  return `<div class="dl-side dl-side-${side}" data-side="${side}" data-entry-id="${entry.id}" style="margin-top:14px;padding-top:10px;border-top:2px solid ${accent}33">
    <div class="dl-section-title" style="color:${accent};display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:6px">
      <span>${title}</span>
      <span style="margin-left:auto">${fillStatus}</span>
    </div>
    ${formHtml}
  </div>`;
}

function _dlRenderTrade(open, close) {
  /* Full-screen overlay: compact left column (metadata + open form +
     close form stacked) + TradingView chart on the right. */
  const e = open || close;
  const closed = !!close;
  const headerColor = closed ? '#6d3eff' : '#1f6feb';
  const headerLabel = closed ? '🔒 Trade Closed — Decision + Review' : '🟢 Active Position — Decision';

  // Save button — saves both sides if both present
  const ids = [];
  if (open) ids.push("'" + open.id + "'");
  if (close) ids.push("'" + close.id + "'");
  const saveCall = `dlSaveTrade(${ids.join(',')})`;

  // Left column: metadata + Open Decision + (if closed) Close Review, stacked
  let leftBody = '';
  leftBody += _dlMetaHtml(e, close);
  if (open) leftBody += _dlSidePanel('open', open);
  if (close) leftBody += _dlSidePanel('close', close);

  // Chart subtitle: open / close timeline
  const fmtDt = (s) => _dlFmt.fmtFriendly(s);   // local TZ, 12-hour
  const tvSubtitle = closed
    ? `📅 Open: <b style="color:#e6edf3">${fmtDt(e.open_at)}</b> → Close: <b style="color:#e6edf3">${fmtDt(close.close_at || close.close_date)}</b> | ${e.ticker} — ${e.strategy || 'spread'}`
    : `📅 Open: <b style="color:#e6edf3">${fmtDt(e.open_at)}</b> | Still Open | ${e.ticker} — ${e.strategy || 'spread'}`;

  // Pick the entry id we'll attach screenshots to (prefer open entry)
  const targetForShots = (open && open.id) || (close && close.id);
  return `
    <div style="display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid #30363d;background:#161b22">
      <button onclick="dlCloseDrawer()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 12px;border-radius:6px;cursor:pointer">← Close</button>
      <h3 style="margin:0;color:${headerColor};font-size:15px">${headerLabel}</h3>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button onclick="dlCaptureScreenshot('${targetForShots}')" title="Capture the TradingView chart with your annotations" style="background:#21262d;color:#d29922;border:1px solid #d2992266;padding:7px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">📸 Capture chart</button>
        <button onclick="${saveCall}" style="background:#3fb950;color:#000;border:none;padding:7px 16px;border-radius:6px;font-weight:700;cursor:pointer;font-size:12px">Save All</button>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:480px 1fr;height:100%;min-height:0;overflow:hidden">
      <!-- Left column: forms + screenshot strip at the top -->
      <div style="overflow-y:auto;padding:14px;border-right:1px solid #30363d;background:#0d1117">
        ${_dlScreenshotsStrip(open, close)}
        ${leftBody}
      </div>
      <!-- Right column: TradingView chart on top, collapsible spread metrics panel below.
           Default: TV gets the full height (metrics panel collapsed) so the chart
           is large enough to read. Click the ▼ to expand the metrics. -->
      <div style="display:flex;flex-direction:column;overflow:hidden;background:#0d1117">
        <div style="padding:8px 14px;border-bottom:1px solid #30363d;font-size:11px;color:#8b949e;background:#161b22;flex-shrink:0">
          ${tvSubtitle}
        </div>
        <div id="dl-tv" style="flex:1;min-height:0"></div>
        <div style="border-top:1px solid #30363d;background:#161b22;flex-shrink:0;display:flex;align-items:center;gap:0;padding:0 6px">
          <button id="dl-metric-toggle" onclick="_dlToggleMetricsPanel()"
                  title="Show / hide spread metrics"
                  style="background:transparent;border:none;color:#58a6ff;font-size:13px;cursor:pointer;padding:6px 8px;font-weight:700">▶</button>
          <button class="dl-metric-pill active" data-metric="mtm"   onclick="_dlSetMetric('mtm')">Spread $</button>
          <button class="dl-metric-pill" data-metric="iv"    onclick="_dlSetMetric('iv')">IV</button>
          <button class="dl-metric-pill" data-metric="delta" onclick="_dlSetMetric('delta')">Δ</button>
          <button class="dl-metric-pill" data-metric="gamma" onclick="_dlSetMetric('gamma')">Γ</button>
          <button class="dl-metric-pill" data-metric="theta" onclick="_dlSetMetric('theta')">Θ</button>
          <button class="dl-metric-pill" data-metric="vega"  onclick="_dlSetMetric('vega')">Vega</button>
          <button class="dl-metric-pill" data-metric="table" onclick="_dlSetMetric('table')">📋 Table</button>
          <span id="dl-mtm-info" style="margin-left:auto;font-size:10px;color:#8b949e;padding-right:10px">— loading —</span>
        </div>
        <div id="dl-metric-body" style="display:none;flex:0 0 auto;height:240px;border-bottom:1px solid #30363d;overflow:hidden;position:relative">
          <canvas id="dl-mtm-canvas" style="display:block;width:100%;height:100%;background:#0d1117"></canvas>
          <div id="dl-mtm-table" style="display:none;width:100%;height:100%;overflow:auto;background:#0d1117;color:#e6edf3;font-size:11px"></div>
        </div>
      </div>
    </div>
  ` + (() => {
    setTimeout(() => {
      const dr = document.getElementById('dl-drawer');
      if (open) dr.dataset.openEntryId = open.id;
      if (close) dr.dataset.closeEntryId = close.id;
    }, 0);
    return '';
  })();
}

function _section(title, body) {
  return `<div class="dl-section"><div class="dl-section-title">${title}</div>${body}</div>`;
}

// Same as _section but with a right-aligned action button (e.g. ✏️ pencil).
function _sectionWithAction(title, actionBtn, body) {
  return `<div class="dl-section">
    <div class="dl-section-title" style="display:flex;align-items:center">
      <span>${title}</span>
      ${actionBtn}
    </div>${body}</div>`;
}

function _chipsBlock(section, selected) {
  return `<div data-section="${section}">${_dlChipsHtml(section, selected)}</div>`;
}

function _radioBlock(section, current) {
  // Single-select pill row
  const opts = _dlOptions[section] || {defaults:[], custom:[]};
  let h = '<div style="display:flex;flex-wrap:wrap;gap:4px" data-radio="' + section + '">';
  const all = [...opts.defaults, ...opts.custom];
  for (const v of all) {
    const on = current === v;
    const ve = v.replace(/'/g, "\\'");
    const xBtn = opts.custom.includes(v)
      ? ` <span class="x" onclick="event.stopPropagation();_dlRemoveCustom('${section}','${ve}')">×</span>`
      : '';
    h += `<span class="dl-chip ${on?'active':''}" onclick="_dlSetRadio('${section}','${ve}',this)">${v}${xBtn}</span>`;
  }
  h += `<input class="dl-add-input" placeholder="+ add..." onkeydown="if(event.key==='Enter'){_dlAddCustom('${section}',this.value);this.value='';}">`;
  h += '</div>';
  return h;
}

// Find the form-panel scope (`.dl-side`) for a clicked element. Returns the
// drawer itself as a fallback for legacy single-form path.
function _dlScope(el) {
  return (el && el.closest && el.closest('.dl-side')) || document.getElementById('dl-drawer');
}

function _dlSetRadio(section, value, el) {
  const parent = el.parentElement;
  parent.querySelectorAll('.dl-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  _dlScope(el).dataset[section] = value;
}

function _slidersBlock(emotionKeys, current) {
  let h = '<div data-section="emotion">';
  for (const k of emotionKeys) {
    const v = (current || {})[k];
    const isBinary = (k === 'slept_7h' || k === 'stable_today');
    if (isBinary) {
      const on = v === 1 || v === true;
      h += `<div class="dl-slider-row"><label>${k.replace(/_/g,' ')}</label>
        <span class="dl-chip ${on?'active':''}" onclick="_dlBinaryEmotion('${k}',true,this)">Yes</span>
        <span class="dl-chip ${v===0||v===false?'active':''}" onclick="_dlBinaryEmotion('${k}',false,this)">No</span></div>`;
    } else {
      const val = v == null ? 5 : v;
      h += `<div class="dl-slider-row"><label>${k.replace(/_/g,' ')}</label>
        <input type="range" min="0" max="10" value="${val}" oninput="_dlSliderEmotion('${k}', this.value, this)">
        <span class="val">${val}</span></div>`;
    }
  }
  h += '</div>';
  return h;
}

function _dlSliderEmotion(k, v, el) {
  // Stash emotion in the panel's dataset (one panel per side).
  const scope = _dlScope(el);
  let emo = {};
  try { emo = JSON.parse(scope.dataset.emotion || '{}'); } catch (e) {}
  emo[k] = parseInt(v);
  scope.dataset.emotion = JSON.stringify(emo);
  el.parentElement.querySelector('.val').textContent = v;
}
function _dlBinaryEmotion(k, val, el) {
  const scope = _dlScope(el);
  let emo = {};
  try { emo = JSON.parse(scope.dataset.emotion || '{}'); } catch (e) {}
  emo[k] = val ? 1 : 0;
  scope.dataset.emotion = JSON.stringify(emo);
  el.parentElement.querySelectorAll('.dl-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
}

// Greeks/IV/COP display — read-only by default with a pencil ✏️ to switch
// into edit mode. Auto-fill values come from journal_greeks compute, but the
// user can override anything (override is honored — collect reads inputs if
// they're rendered, otherwise leaves values untouched on PATCH).
function _greeksTableHtml(e) {
  // Read-only summary line (the default view)
  const fmtNum = (v, suffix='') => v == null ? '<span style="color:#666">—</span>' : `<b style="color:#e6edf3">${v}${suffix}</b>`;
  const display = `
    <div class="dl-greeks-display" style="font-family:monospace;font-size:11px;color:#8b949e;line-height:1.7">
      ${e.spot_at_open != null ? `spot ${fmtNum(e.spot_at_open, '')} · ` : ''}
      price ${fmtNum(e.open_price, '')} ·
      POP ${fmtNum(e.pop_at_open, '%')} ·
      IV ${fmtNum(e.iv_at_open, '%')} ·
      Δ ${fmtNum(e.delta_at_open)} ·
      Θ ${fmtNum(e.theta_at_open)} ·
      Γ ${fmtNum(e.gamma_at_open)} ·
      V ${fmtNum(e.vega_at_open)}
    </div>`;
  // Edit form (hidden by default)
  const inp = (cls, val, ph) =>
    `<input type="number" step="any" class="${cls}" value="${val == null ? '' : val}" placeholder="${ph}"
      style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 6px;border-radius:3px;font-size:12px;text-align:right">`;
  const cell = (label, sub, input) => `<td style="padding:3px 4px;vertical-align:top">
      <div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">${label}<span style="color:#666;font-weight:400;text-transform:none;margin-left:3px">${sub}</span></div>
      ${input}
    </td>`;
  const editForm = `
    <div class="dl-greeks-edit" style="display:none">
      <table style="width:100%;border-collapse:collapse;table-layout:fixed">
        <tr>
          ${cell('Spot', '$', inp('dl-spot-input',  e.spot_at_open,  ''))}
          ${cell('Price', '$', inp('dl-openprice-input', e.open_price, ''))}
          ${cell('POP', '%', inp('dl-pop-input',   e.pop_at_open, ''))}
          ${cell('IV',  '%', inp('dl-iv-input',    e.iv_at_open,  ''))}
        </tr>
        <tr>
          ${cell('Δ Delta', '', inp('dl-delta-input', e.delta_at_open, ''))}
          ${cell('Θ Theta', '$/d', inp('dl-theta-input', e.theta_at_open, ''))}
          ${cell('Γ Gamma', '', inp('dl-gamma-input', e.gamma_at_open, ''))}
          ${cell('V Vega',  '$/iv', inp('dl-vega-input', e.vega_at_open, ''))}
        </tr>
      </table>
    </div>`;
  return display + editForm;
}

// Same idea for close-side: display by default + ✏️ for edit.
function _greeksCloseTableHtml(e) {
  const fmtNum = (v, suffix='') => v == null ? '<span style="color:#666">—</span>' : `<b style="color:#e6edf3">${v}${suffix}</b>`;
  const display = `
    <div class="dl-greeks-close-display" style="font-family:monospace;font-size:11px;color:#8b949e;line-height:1.7">
      ${e.spot_at_close != null ? `spot ${fmtNum(e.spot_at_close)} · ` : ''}
      price ${fmtNum(e.close_price)} ·
      POP ${fmtNum(e.pop_at_close, '%')} ·
      IV ${fmtNum(e.iv_at_close, '%')} ·
      Δ ${fmtNum(e.delta_at_close)} ·
      Θ ${fmtNum(e.theta_at_close)} ·
      Γ ${fmtNum(e.gamma_at_close)} ·
      V ${fmtNum(e.vega_at_close)}
    </div>`;
  const inp = (cls, val) =>
    `<input type="number" step="any" class="${cls}" value="${val == null ? '' : val}"
      style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:4px 6px;border-radius:3px;font-size:12px;text-align:right">`;
  const cell = (label, sub, input) => `<td style="padding:3px 4px;vertical-align:top">
      <div style="font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">${label}<span style="color:#666;font-weight:400;text-transform:none;margin-left:3px">${sub}</span></div>
      ${input}
    </td>`;
  const editForm = `
    <div class="dl-greeks-close-edit" style="display:none">
      <table style="width:100%;border-collapse:collapse;table-layout:fixed">
        <tr>
          ${cell('Spot', '$', inp('dl-spot-close-input', e.spot_at_close))}
          ${cell('Price', '$', inp('dl-closeprice-input', e.close_price))}
          ${cell('POP', '%', inp('dl-pop-close-input', e.pop_at_close))}
          ${cell('IV',  '%', inp('dl-iv-close-input',  e.iv_at_close))}
        </tr>
        <tr>
          ${cell('Δ Delta', '', inp('dl-delta-close-input', e.delta_at_close))}
          ${cell('Θ Theta', '$/d', inp('dl-theta-close-input', e.theta_at_close))}
          ${cell('Γ Gamma', '', inp('dl-gamma-close-input', e.gamma_at_close))}
          ${cell('V Vega',  '$/iv', inp('dl-vega-close-input', e.vega_at_close))}
        </tr>
      </table>
    </div>`;
  return display + editForm;
}

// Toggle the display ↔ edit view. The pencil button next to the section
// title fires this; selector lets one button serve both open + close panels.
function _dlToggleGreeksEdit(btn, displaySel, editSel) {
  const sect = btn.closest('.dl-section');
  if (!sect) return;
  const disp = sect.querySelector(displaySel);
  const edit = sect.querySelector(editSel);
  if (!disp || !edit) return;
  const editing = edit.style.display === 'none';
  disp.style.display = editing ? 'none' : '';
  edit.style.display = editing ? '' : 'none';
  btn.textContent = editing ? '✓' : '✏️';
  btn.title = editing ? 'Done editing' : 'Edit Greeks/price';
}

const _DL_OPEN_TEMPLATES = [
  { label: '🎯 超跌反弹', chips: { logic_type: ['Dip Buy','Mean Reversion'], signal_sources: ['OptionStrat'], confidence: 'Low', emotion_keys: ['stable_today'] } },
  { label: '📈 趋势追涨', chips: { logic_type: ['Momentum','Mean Reversion'], signal_sources: ['OptionStrat','Technical Analysis'], confidence: 'High', emotion_keys: ['stable_today'] } },
  { label: '🛡️ 对冲单', chips: { logic_type: ['Hedge'], signal_sources: ['OptionStrat'], confidence: 'Medium', emotion_keys: ['stable_today'] } },
  { label: '🎲 赌一把', chips: { logic_type: ['赌博'], signal_sources: ['OptionStrat'], confidence: 'Gambling', emotion_keys: ['fomo'] } },
];

const _DL_CLOSE_TEMPLATES = [
  { label: '✅ 止盈平仓', chips: { close_logic: ['Reduced risk (close part)','Take profit hit'], quadrant: 'good_dec_good_res', mistakes_made: [], rule_changes_midway: [] } },
  { label: '😞 没拿住', chips: { close_logic: ['Reduced risk (close part)'], quadrant: 'good_dec_bad_res', mistakes_made: ['没拿住','没有提前止盈'], rule_changes_midway: [] } },
  { label: '😤 乱抄底跟单', chips: { close_logic: ['Reduced risk (close part)','Stop loss hit'], quadrant: 'bad_dec_bad_res', mistakes_made: ['乱抄底','乱跟单','开单时间太短','没拿住'], rule_changes_midway: [] } },
  { label: '🎲 胡乱开单', chips: { close_logic: ['Stop loss hit'], quadrant: 'bad_dec_bad_res', mistakes_made: ['无清晰开单逻辑','胡乱开单'], rule_changes_midway: [] } },
  { label: '💰 赚了早跑', chips: { close_logic: ['Reduced risk (close part)','Take profit hit'], quadrant: 'good_dec_good_res', mistakes_made: ['其实可以持有更久的'], rule_changes_midway: [] } },
];

function _dlApplyTemplate(side, idx) {
  const templates = side === 'open' ? _DL_OPEN_TEMPLATES : _DL_CLOSE_TEMPLATES;
  const tpl = templates[idx];
  if (!tpl) return;
  const drawer = document.getElementById('dl-drawer');
  if (!drawer) return;
  const scope = side === 'close'
    ? (drawer.querySelector('.dl-side[data-side="close"]') || drawer)
    : (drawer.querySelector('.dl-side[data-side="open"]') || drawer);
  const chips = tpl.chips;
  Object.entries(chips).forEach(([section, val]) => {
    if (section === 'quadrant') {
      const container = scope.querySelector('[data-radio="quadrant"]');
      if (!container) return;
      container.querySelectorAll('.dl-chip').forEach(c => {
        c.classList.remove('active');
        const s = c.dataset.bgStyle;
        if (s) c.setAttribute('style', s);
      });
      container.querySelectorAll('.dl-chip').forEach(c => {
        const txt = c.textContent.trim().toLowerCase();
        const map = {
          'good_dec_good_res': '✓ good dec + good result',
          'good_dec_bad_res': '~ good dec + bad result',
          'bad_dec_good_res': '⚠ bad dec + good result',
          'bad_dec_bad_res': '✗ bad dec + bad result',
        };
        if (txt === (map[val]||'')) {
          c.classList.add('active');
          c.removeAttribute('style');
        }
      });
      const sc = scope.closest('.dl-side') || scope;
      sc.dataset.quadrant = val;
    } else if (section === 'confidence') {
      const container = scope.querySelector('[data-radio="confidence"]');
      if (!container) return;
      container.querySelectorAll('.dl-chip').forEach(c => {
        c.classList.remove('active');
        if (c.textContent.trim() === val) c.classList.add('active');
      });
      const sc = scope.closest('.dl-side') || scope;
      sc.dataset.confidence = val;
    } else if (Array.isArray(val)) {
      const block = scope.querySelector('[data-section="' + section + '"]');
      if (!block) return;
      block.querySelectorAll('.dl-chip').forEach(c => {
        const label = c.childNodes[0]?.textContent?.trim() || c.textContent.trim();
        if (val.includes(label)) c.classList.add('active');
        else c.classList.remove('active');
      });
    }
  });
  _dlMarkDirty();
}

function _dlTemplateButtons(side) {
  const templates = side === 'open' ? _DL_OPEN_TEMPLATES : _DL_CLOSE_TEMPLATES;
  let h = '<div class="dl-tpl-bar">⚡快捷: ';
  templates.forEach((t, i) => {
    h += '<button class="dl-tpl-btn" onclick="_dlApplyTemplate(\\'' + side + '\\',' + i + ')">' + t.label + '</button>';
  });
  h += '</div>';
  return h;
}

function _dlRenderOpenForm(e) {
  let h = _dlTemplateButtons('open');
  // Greeks/IV/COP at Open — read-only display by default; pencil ✏️
  // button switches into edit mode where every cell (incl. spot + price)
  // is overridable. Auto-filled values come from journal_greeks.
  const openEditBtn = `<button onclick="_dlToggleGreeksEdit(this, '.dl-greeks-display', '.dl-greeks-edit')"
    title="Edit Greeks/price"
    style="background:transparent;border:1px solid #30363d;color:#8b949e;cursor:pointer;padding:1px 6px;border-radius:3px;font-size:11px;margin-left:auto">✏️</button>`;
  h += _sectionWithAction('Greeks / IV / COP at Open', openEditBtn, _greeksTableHtml(e));
  h += _section('Logic Type', _chipsBlock('logic_type', e.logic_type));
  h += _section('Earnings Bet?', `<div data-radio="is_earnings_bet" style="display:flex;gap:6px">
    <span class="dl-chip ${e.is_earnings_bet===true?'active':''}" onclick="this.closest('.dl-side,#dl-drawer').dataset.is_earnings_bet='true';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')">Yes — earnings play</span>
    <span class="dl-chip ${e.is_earnings_bet===false?'active':''}" onclick="this.closest('.dl-side,#dl-drawer').dataset.is_earnings_bet='false';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')">No</span>
    </div>`);
  h += _section('Signal Sources', _chipsBlock('signal_sources', e.signal_sources));
  h += _section('Confidence', _radioBlock('confidence', e.confidence));
  // Emotional State — multi-select chips. Backward compat: legacy entries may
  // store emotion as {focused:7, anxious:3} (slider values); pull keys with
  // non-zero values out as the selected list.
  const emoSelected = Array.isArray(e.emotion)
    ? e.emotion
    : Object.keys(e.emotion || {}).filter(k => e.emotion[k]);
  h += _section('Emotional State (multi-select)', _chipsBlock('emotion_keys', emoSelected));
  h += _section('One-line Thesis (required)',
    `<input class="dl-thesis-input" type="text" value="${(e.one_line_thesis||'').replace(/"/g,'&quot;')}" style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px;border-radius:4px;font-size:13px" placeholder="Why open this trade in one sentence?">`);
  h += _section('Notes (optional)',
    `<textarea class="dl-notes-input" rows="3" style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px;border-radius:4px;font-size:12px;resize:vertical">${(e.notes||'')}</textarea>`);
  return h;
}

function _dlRenderCloseForm(e) {
  const SCOPE = "this.closest('.dl-side,#dl-drawer')";
  let h = _dlTemplateButtons('close');
  // Greeks/IV/COP at Close — same display+pencil pattern as open.
  const closeEditBtn = `<button onclick="_dlToggleGreeksEdit(this, '.dl-greeks-close-display', '.dl-greeks-close-edit')"
    title="Edit Greeks/price"
    style="background:transparent;border:1px solid #30363d;color:#8b949e;cursor:pointer;padding:1px 6px;border-radius:3px;font-size:11px;margin-left:auto">✏️</button>`;
  h += _sectionWithAction('Greeks / IV / COP at Close', closeEditBtn, _greeksCloseTableHtml(e));
  h += _section('Close Logic (multi)', _chipsBlock('close_logic', e.close_logic));
  h += _section('Rule Changes Midway', _chipsBlock('rule_changes_midway', e.rule_changes_midway));
  // Quadrant: colored bg only when NOT active. When active, fall back to .dl-chip.active styling
  // so the user gets visible feedback. (Inline styles override CSS rules; previously the chips
  // looked unclickable because the colored bg masked the active state.)
  const _qStyle = (active, css) => active ? '' : `style="${css}"`;
  h += _section('Quadrant (decision quality × outcome)', `<div data-radio="quadrant" style="display:grid;grid-template-columns:1fr 1fr;gap:4px">
    <span class="dl-chip ${e.quadrant==='good_dec_good_res'?'active':''}" onclick="${SCOPE}.dataset.quadrant='good_dec_good_res';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>{c.classList.remove('active');const s=c.dataset.bgStyle;if(s)c.setAttribute('style',s);});this.classList.add('active');this.removeAttribute('style')" data-bg-style="background:#0d3a1f;color:#3fb950;border-color:#1a5a35" ${_qStyle(e.quadrant==='good_dec_good_res','background:#0d3a1f;color:#3fb950;border-color:#1a5a35')}>✓ Good Dec + Good Result</span>
    <span class="dl-chip ${e.quadrant==='good_dec_bad_res'?'active':''}" onclick="${SCOPE}.dataset.quadrant='good_dec_bad_res';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>{c.classList.remove('active');const s=c.dataset.bgStyle;if(s)c.setAttribute('style',s);});this.classList.add('active');this.removeAttribute('style')" data-bg-style="background:#3a2d0d;color:#d29922;border-color:#5a4815" ${_qStyle(e.quadrant==='good_dec_bad_res','background:#3a2d0d;color:#d29922;border-color:#5a4815')}>~ Good Dec + Bad Result</span>
    <span class="dl-chip ${e.quadrant==='bad_dec_good_res'?'active':''}" onclick="${SCOPE}.dataset.quadrant='bad_dec_good_res';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>{c.classList.remove('active');const s=c.dataset.bgStyle;if(s)c.setAttribute('style',s);});this.classList.add('active');this.removeAttribute('style')" data-bg-style="background:#3a1f1f;color:#f85149;border-color:#5a3535" ${_qStyle(e.quadrant==='bad_dec_good_res','background:#3a1f1f;color:#f85149;border-color:#5a3535')}>⚠ Bad Dec + Good Result</span>
    <span class="dl-chip ${e.quadrant==='bad_dec_bad_res'?'active':''}" onclick="${SCOPE}.dataset.quadrant='bad_dec_bad_res';this.parentElement.querySelectorAll('.dl-chip').forEach(c=>{c.classList.remove('active');const s=c.dataset.bgStyle;if(s)c.setAttribute('style',s);});this.classList.add('active');this.removeAttribute('style')" data-bg-style="background:#3a1f1f;color:#f85149;border-color:#5a3535" ${_qStyle(e.quadrant==='bad_dec_bad_res','background:#3a1f1f;color:#f85149;border-color:#5a3535')}>✗ Bad Dec + Bad Result</span></div>`);
  h += _section('Mistakes Made (multi)', _chipsBlock('mistakes_made', e.mistakes_made));
  h += _section('Lesson Learned (actionable)',
    `<textarea class="dl-lesson-input" rows="3" style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:8px;border-radius:4px;font-size:12px;resize:vertical">${(e.lesson_learned||'')}</textarea>`);
  return h;
}

function _dlRenderOpenSnapshot(snap) {
  if (!snap) return '<div class="dl-readonly">No matching open entry.</div>';
  const fmt = v => Array.isArray(v) ? (v.length ? v.join(', ') : '—') : (v == null ? '—' : v);
  const dirColor = snap.direction === 'BULL' ? '#3fb950'
                : snap.direction === 'BEAR' ? '#f85149'
                : snap.direction === 'NEUTRAL' ? '#d29922' : '#8b949e';
  const dirHtml = snap.direction
    ? `<span style="background:${dirColor}22;color:${dirColor};border:1px solid ${dirColor}66;font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;margin-right:6px">${snap.direction}</span>` : '';
  const legs = snap.legs || [];
  const legsLine = legs.length
    ? `<div style="font-family:monospace;color:#8b949e;font-size:10px;margin-top:2px">${legs.map(l=>(l.action.startsWith('Sell')?'-':'+')+l.quantity+' '+l.strike+l.type+' @ $'+(l.price||0).toFixed(2)).join('  ')}</div>`
    : '';
  return `<div class="dl-readonly" style="font-size:11px;line-height:1.7">
    <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px">
      ${dirHtml}<b>${snap.ticker || ''}</b>
      <span style="color:#e6edf3;font-weight:600">${snap.spread_label || ''}</span>
      <span style="color:#8b949e">×${snap.quantity || '?'}</span>
      <span style="color:#8b949e;background:#161b22;border:1px solid #30363d;padding:1px 6px;border-radius:3px;font-size:10px">${snap.strategy || ''}</span>
    </div>
    ${legsLine}
    <div style="margin-top:6px;border-top:1px solid #30363d;padding-top:6px"></div>
    ${(snap.pop_at_open != null || snap.iv_at_open != null || snap.delta_at_open != null) ? `
    <div style="font-family:monospace;color:#8b949e;font-size:10px">
      ${snap.pop_at_open != null ? `POP <b style="color:#e6edf3">${snap.pop_at_open}%</b>` : ''}
      ${snap.iv_at_open != null ? ` · IV <b style="color:#e6edf3">${snap.iv_at_open}%</b>` : ''}
      ${snap.delta_at_open != null ? ` · Δ <b style="color:#e6edf3">${snap.delta_at_open}</b>` : ''}
      ${snap.theta_at_open != null ? ` · Θ <b style="color:#e6edf3">${snap.theta_at_open}</b>` : ''}
      ${snap.gamma_at_open != null ? ` · Γ <b style="color:#e6edf3">${snap.gamma_at_open}</b>` : ''}
      ${snap.vega_at_open != null ? ` · V <b style="color:#e6edf3">${snap.vega_at_open}</b>` : ''}
    </div>` : ''}
    <div><b>Logic:</b> ${fmt(snap.logic_type)}</div>
    <div><b>Earnings bet:</b> ${snap.is_earnings_bet === true ? 'Yes' : snap.is_earnings_bet === false ? 'No' : '—'}</div>
    <div><b>Sources:</b> ${fmt(snap.signal_sources)}</div>
    <div><b>Confidence:</b> ${fmt(snap.confidence)}</div>
    <div><b>Emotion:</b> ${(Array.isArray(snap.emotion) ? snap.emotion.join(', ') : Object.entries(snap.emotion || {}).map(([k,v])=>k+'='+v).join(' · ')) || '—'}</div>
    <div style="margin-top:8px;border-top:1px solid #30363d;padding-top:6px"><b>Thesis:</b> ${fmt(snap.one_line_thesis)}</div>
    <div><b>Notes:</b> ${fmt(snap.notes)}</div>
  </div>`;
}

function _dlBindTrade(open, close) {
  // Initialize each panel's own dataset with current values for radios + emotion
  const dr = document.getElementById('dl-drawer');
  if (open) {
    const p = dr.querySelector('.dl-side-open');
    if (p) {
      if (open.is_earnings_bet != null) p.dataset.is_earnings_bet = String(open.is_earnings_bet);
      if (open.confidence) p.dataset.confidence = open.confidence;
      if (open.emotion) p.dataset.emotion = JSON.stringify(open.emotion);
    }
  }
  if (close) {
    const p = dr.querySelector('.dl-side-close');
    if (p) {
      if (close.quadrant) p.dataset.quadrant = close.quadrant;
    }
  }
}

function _dlCollectPanel(panel, kind) {
  const updates = {};
  const collectChips = (sec) => {
    const block = panel.querySelector(`[data-section="${sec}"]`);
    if (!block) return [];
    return Array.from(block.querySelectorAll('.dl-chip.active'))
      .map(c => c.firstChild.textContent.trim().replace(/ ×$/, '').trim())
      .filter(Boolean);
  };
  if (kind === 'open') {
    updates.logic_type     = collectChips('logic_type');
    updates.signal_sources = collectChips('signal_sources');
    updates.confidence     = panel.dataset.confidence || null;
    updates.is_earnings_bet = panel.dataset.is_earnings_bet === 'true' ? true
                            : panel.dataset.is_earnings_bet === 'false' ? false : null;
    // Emotion now multi-select chips (array of strings); collected from
    // [data-section="emotion_keys"]. Stored as a list to match the new UI.
    updates.emotion        = collectChips('emotion_keys');
    updates.one_line_thesis = panel.querySelector('.dl-thesis-input')?.value || '';
    updates.notes           = panel.querySelector('.dl-notes-input')?.value || '';
    // Greeks/IV/POP — empty input → null (not 0); otherwise parse number.
    const numOrNull = (sel) => {
      const v = panel.querySelector(sel)?.value;
      if (v == null || v === '') return null;
      const n = parseFloat(v);
      return Number.isFinite(n) ? n : null;
    };
    updates.spot_at_open  = numOrNull('.dl-spot-input');
    updates.open_price    = numOrNull('.dl-openprice-input');
    updates.pop_at_open   = numOrNull('.dl-pop-input');
    updates.iv_at_open    = numOrNull('.dl-iv-input');
    updates.delta_at_open = numOrNull('.dl-delta-input');
    updates.theta_at_open = numOrNull('.dl-theta-input');
    updates.gamma_at_open = numOrNull('.dl-gamma-input');
    updates.vega_at_open  = numOrNull('.dl-vega-input');
  } else {
    updates.close_logic       = collectChips('close_logic');
    updates.rule_changes_midway = collectChips('rule_changes_midway');
    updates.mistakes_made     = collectChips('mistakes_made');
    updates.quadrant          = panel.dataset.quadrant || null;
    updates.lesson_learned    = panel.querySelector('.dl-lesson-input')?.value || '';
    // Greeks/price at close — editable via pencil button
    const numOrNull = (sel) => {
      const v = panel.querySelector(sel)?.value;
      if (v == null || v === '') return null;
      const n = parseFloat(v);
      return Number.isFinite(n) ? n : null;
    };
    updates.spot_at_close  = numOrNull('.dl-spot-close-input');
    updates.close_price    = numOrNull('.dl-closeprice-input');
    updates.pop_at_close   = numOrNull('.dl-pop-close-input');
    updates.iv_at_close    = numOrNull('.dl-iv-close-input');
    updates.delta_at_close = numOrNull('.dl-delta-close-input');
    updates.theta_at_close = numOrNull('.dl-theta-close-input');
    updates.gamma_at_close = numOrNull('.dl-gamma-close-input');
    updates.vega_at_close  = numOrNull('.dl-vega-close-input');
  }
  // Mark complete if user filled anything meaningful
  const anyFilled = Object.values(updates).some(v =>
    v && (Array.isArray(v) ? v.length : (typeof v === 'object' ? Object.keys(v).length : true))
  );
  if (anyFilled) updates.status = 'completed';
  return updates;
}

async function dlSaveTrade() {
  // Variadic args: PATCH each entry id whose panel exists in the drawer
  const dr = document.getElementById('dl-drawer');
  const entryIds = Array.from(arguments).filter(Boolean);
  for (const id of entryIds) {
    const panel = dr.querySelector(`.dl-side[data-entry-id="${id}"]`);
    if (!panel) continue;
    const kind = panel.dataset.side;
    const updates = _dlCollectPanel(panel, kind);
    await fetch('/api/journal_entries/' + id, {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(updates)
    });
  }
  // Save just succeeded — clear the dirty flag so dlCloseDrawer doesn't
  // re-prompt "you have unsaved changes" when the user clicked Save All directly.
  _dlDrawerDirty = false;
  dlCloseDrawer();
}

// Legacy single-entry save — used by chip CRUD's re-render loop
async function dlSaveEntry(entryId) { return dlSaveTrade(entryId); }

async function loadScreener() {
  const lv = document.getElementById('scr-list-view');
  lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">Loading...</div>';
  try {
    // First check if a scan is currently running — if so, show progress UI
    // and poll status. Don't render any stale results while scanning.
    const statusR = await fetch('/api/screener/status');
    const status = await statusR.json();
    if (status.running) {
      // Takeover into scanning UI (same rendering path as runScan)
      _renderScanProgressAndPoll();
      return;
    }
    const r = await fetch('/api/screener/data');
    const d = await r.json();
    if (d.signals && d.signals.length > 0) {
      scrData = d;
      screenerLoaded = true;
      document.getElementById('screenerDate').textContent = 'Scan: ' + d.scan_date;
      initSigFilter();
      renderSignalsTable();
      fetchScreenerEarnings(d.signals.map(s => s.ticker));
    } else if (d.scan_date) {
      scrData = d;
      screenerLoaded = true;
      document.getElementById('screenerDate').textContent = 'Scan: ' + d.scan_date;
      lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">No qualified signals (win rate ≥65% required). Try Run Scan.</div>';
    } else {
      lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">No scan data. Click "Run Scan" to start.</div>';
    }
  } catch(e) {
    lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">No scan data. Click "Run Scan" to start.</div>';
  }
}

// Render scanning UI + poll until done. Used by loadScreener when scan already
// running at page load, and by runScan when user clicks button.
function _renderScanProgressAndPoll() {
  const btn = document.getElementById('runScanBtn');
  const lv = document.getElementById('scr-list-view');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning...'; }
  document.getElementById('scr-filter-bar').style.display = 'none';
  lv.innerHTML = '<div class="scr-progress-wrap">' +
    '<div class="scr-progress-pct" id="scrPct">0%</div>' +
    '<div class="scr-progress-bar"><div class="scr-progress-fill" id="scrFill" style="width:0%"></div></div>' +
    '<div class="scr-progress-text" id="scrMsg">Scanning...</div>' +
    '<div class="scr-progress-text" id="scrTime" style="margin-top:6px"></div>' +
    '</div>';
  const poll = setInterval(async () => {
    try {
      const r = await fetch('/api/screener/status');
      const d = await r.json();
      const fill = document.getElementById('scrFill');
      const pctEl = document.getElementById('scrPct');
      const msg = document.getElementById('scrMsg');
      if (fill && d.pct !== undefined) { fill.style.width = d.pct + '%'; if (pctEl) pctEl.textContent = d.pct + '%'; }
      if (msg && d.progress) msg.textContent = d.progress;
      const timeEl = document.getElementById('scrTime');
      if (timeEl && d.elapsed !== undefined) {
        const fmtT = (s) => { const m = Math.floor(s/60); const sec = s%60; return m > 0 ? m+'m '+sec+'s' : sec+'s'; };
        let t = fmtT(d.elapsed);
        if (d.eta > 0 && d.pct > 0 && d.pct < 100) t += ' \u00B7 ETA ' + fmtT(d.eta);
        timeEl.textContent = t;
      }
      if (!d.running) {
        clearInterval(poll);
        if (btn) { btn.disabled = false; btn.textContent = 'Run Scan'; }
        const dr = await fetch('/api/screener/data');
        scrData = await dr.json();
        screenerLoaded = true;
        if (scrData.scan_date) document.getElementById('screenerDate').textContent = 'Scan: ' + scrData.scan_date;
        if (scrData.signals && scrData.signals.length > 0) {
          initSigFilter();
          scrEarnings = {}; scrEarningsLoaded = false;
          renderSignalsTable();
          fetchScreenerEarnings(scrData.signals.map(s => s.ticker));
        } else {
          lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">No qualified signals found.</div>';
        }
      }
    } catch(e) {}
  }, 1500);
}

function initSigFilter() {
  if (!scrData) return;
  // Build unique signal short names + source names from data
  const wrNames = new Set();
  const srcNames = new Set();
  for (const t of scrData.signals) {
    for (const sg of t.signals) {
      wrNames.add(sg.name.split('(')[0].trim().split(' ')[0]);
    }
    for (const src of (t.sources || [])) {
      srcNames.add(src.type + ':' + src.name);
    }
  }
  const sortedWr = [...wrNames].sort();
  const sortedSrc = [...srcNames].sort();
  const drop = document.getElementById('scrSigDrop');
  let h = '<label style="border-bottom:1px solid #30363d;padding-bottom:4px;margin-bottom:2px"><input type="checkbox" checked onchange="toggleAllSigs(this)"> <b>Select All</b></label>';
  if (sortedSrc.length) {
    h += '<label style="color:#8b949e;font-size:10px;padding-top:4px;border-top:1px solid #21262d">External Sources</label>';
    for (const n of sortedSrc) {
      const clr = n.startsWith('BC:') ? '#4caf50' : '#ab47bc';
      h += '<label style="color:' + clr + '"><input type="checkbox" value="' + n + '" checked onchange="updateSigFilter()"> ' + n + '</label>';
    }
    h += '<label style="color:#8b949e;font-size:10px;padding-top:4px;border-top:1px solid #21262d">Winrate Signals</label>';
  }
  for (const n of sortedWr) {
    h += '<label><input type="checkbox" value="' + n + '" checked onchange="updateSigFilter()"> ' + n + '</label>';
  }
  drop.innerHTML = h;
  document.getElementById('scr-filter-bar').style.display = 'flex';
}

function toggleSigDrop() {
  scrSigDropOpen = !scrSigDropOpen;
  document.getElementById('scrSigDrop').classList.toggle('open', scrSigDropOpen);
}
// Close dropdown on outside click
document.addEventListener('click', function(e) {
  if (!e.target.closest('.scr-multi-wrap') && scrSigDropOpen) {
    scrSigDropOpen = false;
    document.getElementById('scrSigDrop').classList.remove('open');
  }
});

function toggleAllSigs(el) {
  const boxes = document.querySelectorAll('#scrSigDrop input[type=checkbox][value]');
  boxes.forEach(b => b.checked = el.checked);
  updateSigFilter();
}

function updateSigFilter() {
  scrFilterSigs.clear();
  const boxes = document.querySelectorAll('#scrSigDrop input[type=checkbox][value]');
  let allChecked = true;
  boxes.forEach(b => { if (b.checked) scrFilterSigs.add(b.value); else allChecked = false; });
  const btn = document.getElementById('scrSigBtn');
  if (allChecked || scrFilterSigs.size === 0) {
    btn.textContent = 'All Signals \u25BE';
    scrFilterSigs.clear();  // empty = all
  } else {
    btn.textContent = scrFilterSigs.size + ' selected \u25BE';
  }
  // Update select-all checkbox
  const allBox = document.querySelector('#scrSigDrop input[type=checkbox]:not([value])');
  if (allBox) allBox.checked = allChecked;
  applyFilters();
}

function setSrcFilter(src) {
  // Toggle behavior: click BC → BC only; click BC again → ALL (filter off).
  // 'ALL' button always resets. BC and WR toggle on/off when re-clicked.
  if (src !== 'ALL' && scrSrcFilter === src) {
    scrSrcFilter = 'ALL';
  } else {
    scrSrcFilter = src;
  }
  document.querySelectorAll('.scr-src-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.src === scrSrcFilter);
    if (b.dataset.src === scrSrcFilter) { b.style.opacity = '1'; b.style.fontWeight = '700'; }
    else { b.style.opacity = '0.6'; b.style.fontWeight = '400'; }
  });
  applyFilters();
}

function applyFilters() {
  scrFilterDir = document.getElementById('scrFilterDir').value;
  const earnInput = document.getElementById('scrEarnDays');
  scrEarnMaxDays = earnInput ? parseInt(earnInput.value) || 0 : 0;
  scrEarnMode = document.getElementById('scrEarnMode').value;
  scrMinStr = parseInt(document.getElementById('scrMinStr').value) || 0;
  renderSignalsTable();
}

async function fetchScreenerEarnings(tickers) {
  try {
    const r = await fetch('/api/batch-earnings-dates', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({tickers})
    });
    const d = await r.json();
    scrEarnings = d.earnings || {};
    scrEarningsLoaded = true;
    renderSignalsTable();
  } catch(e) { console.warn('fetchScreenerEarnings:', e); }
}

function sortBy(col) {
  if (scrSortCol === col) { scrSortAsc = !scrSortAsc; }
  else { scrSortCol = col; scrSortAsc = col === 'ticker'; }
  renderSignalsTable();
}

function getFiltered() {
  if (!scrData || !scrData.signals) return [];
  let items = scrData.signals;
  // Direction filter
  if (scrFilterDir !== 'ALL') {
    items = items.filter(t => t.direction === scrFilterDir);
  }
  // Signal name + source filter
  if (scrFilterSigs.size > 0) {
    items = items.filter(t => {
      const wrMatch = t.signals.some(sg => {
        const short = sg.name.split('(')[0].trim().split(' ')[0];
        return scrFilterSigs.has(short);
      });
      const srcMatch = (t.sources || []).some(src => scrFilterSigs.has(src.type + ':' + src.name));
      return wrMatch || srcMatch;
    });
  }
  // Source type filter (BC / FV / WR)
  if (scrSrcFilter !== 'ALL') {
    items = items.filter(t => {
      if (scrSrcFilter === 'WR') return t.signals && t.signals.length > 0;
      if (scrSrcFilter === 'BC') return (t.sources || []).some(s => s.type === 'BC');
      if (scrSrcFilter === 'FV') return (t.sources || []).some(s => s.type === 'FV');
      return true;
    });
  }
  // Minimum signal strength filter
  if (scrMinStr > 0) {
    items = items.filter(t => t.signal_strength >= scrMinStr);
  }
  // Earnings date filter.
  // Tickers with NO earnings (ETFs like QQQ/SPY, indexes, commodity funds)
  // pass all earnings filters — there's no earnings risk to filter against.
  // Only tickers WITH a known earnings date get checked against the mode.
  if (scrEarnMode !== 'off' && scrEarnMaxDays > 0 && scrEarningsLoaded) {
    items = items.filter(t => {
      const e = scrEarnings[t.ticker];
      // No earnings info known → keep (ETFs/indexes) — no earnings risk.
      if (!e || e.days == null || e.days < 0) return true;
      if (scrEarnMode === 'lte') return e.days <= scrEarnMaxDays;
      if (scrEarnMode === 'gte') return e.days >= scrEarnMaxDays;
      return true;
    });
  }
  // Sort
  const col = scrSortCol;
  const dir = scrSortAsc ? 1 : -1;
  items = [...items].sort((a, b) => {
    if (col === 'earnings_days') {
      const ea = scrEarnings[a.ticker], eb = scrEarnings[b.ticker];
      const da = ea && ea.days != null ? ea.days : 9999;
      const db = eb && eb.days != null ? eb.days : 9999;
      return (da - db) * dir;
    }
    let va = a[col], vb = b[col];
    if (typeof va === 'string') return va.localeCompare(vb) * dir;
    return ((va || 0) - (vb || 0)) * dir;
  });
  return items;
}

function renderSignalsTable() {
  if (!scrData || !scrData.signals) return;
  const filtered = getFiltered();
  const lv = document.getElementById('scr-list-view');
  const s = scrData.summary || {};

  // Summary bar
  let html = '<div class="scr-summary">';
  html += '<span>Scanned:<span class="val">' + scrData.tickers_scanned + '</span></span>';
  html += '<span>Signals:<span class="val">' + scrData.signals_found + '</span></span>';
  html += '<span>Bull:<span class="val" style="color:#3fb950">' + (s.bull||0) + '</span></span>';
  html += '<span>Bear:<span class="val" style="color:#f85149">' + (s.bear||0) + '</span></span>';
  html += '<span>Showing:<span class="val">' + filtered.length + '</span></span>';
  html += '</div>';

  // Sort indicator helper
  const si = (col) => {
    if (scrSortCol !== col) return '<span class="sort-ind">\u25B2\u25BC</span>';
    return '<span class="sort-ind active">' + (scrSortAsc ? '\u25B2' : '\u25BC') + '</span>';
  };

  // Table
  html += '<table class="scr-table"><thead><tr>';
  html += '<th onclick="sortBy(' + "'ticker'" + ')">Ticker' + si('ticker') + '</th>';
  html += '<th onclick="sortBy(' + "'price'" + ')">Price' + si('price') + '</th>';
  html += '<th onclick="sortBy(' + "'change_pct'" + ')">Chg%' + si('change_pct') + '</th>';
  html += '<th onclick="sortBy(' + "'direction'" + ')">Dir' + si('direction') + '</th>';
  html += '<th>Signals</th>';
  html += '<th onclick="sortBy(' + "'earnings_days'" + ')" style="min-width:80px">Earnings' + si('earnings_days') + '</th>';
  html += '<th onclick="sortBy(' + "'signal_strength'" + ')">Str' + si('signal_strength') + '</th>';
  html += '</tr></thead><tbody id="scrBody">';

  for (const t of filtered) {
    const chgCls = t.change_pct >= 0 ? 'pos' : 'neg';
    const chgStr = (t.change_pct >= 0 ? '+' : '') + t.change_pct.toFixed(2) + '%';
    const dirCls = t.direction.toLowerCase();
    let sigHtml = '';
    // Source tags (BC / FV) first
    if (t.sources) {
      for (const src of t.sources) {
        const cls = src.type === 'BC' ? 'src-bc' : 'src-fv';
        sigHtml += '<span class="scr-sig-tag ' + cls + '">' + src.type + ':' + src.name + '</span>';
      }
    }
    // Winrate signal tags
    for (const sg of t.signals) {
      const wr = Math.round(sg.best_rate);
      sigHtml += '<span class="scr-sig-tag">' + sg.name.split('(')[0].trim() + '<span class="wr">' + wr + '%</span></span>';
    }
    // Earnings cell — only show future (next) earnings
    const ei = scrEarnings[t.ticker];
    let earnHtml = '';
    if (!scrEarningsLoaded) {
      earnHtml = '<span style="color:#484f58;font-size:11px">...</span>';
    } else if (ei && ei.date && ei.days != null && ei.days >= 0) {
      const dd = ei.days + 'd';
      const tm = ei.time === 'BMO' ? ' BM' : ei.time === 'AMC' ? ' AM' : '';
      const earnClr = ei.days <= 7 ? '#d29922' : ei.days <= 30 ? '#8b949e' : '#484f58';
      earnHtml = '<span style="color:' + earnClr + ';font-size:11px;white-space:nowrap">' + ei.date.slice(5) + tm + ' <span style="opacity:0.6">(' + dd + ')</span></span>';
    } else {
      earnHtml = '<span style="color:#484f58;font-size:11px">-</span>';
    }
    // Cells tagged so handleQuoteUpdate can patch them in place on live DXLink ticks.
    html += '<tr data-ticker="' + t.ticker + '" onclick="showTickerDetail(' + "'" + t.ticker + "'" + ')">';
    html += '<td style="font-weight:700;color:#58a6ff">' + t.ticker + '</td>';
    html += '<td class="scr-price-cell">$' + t.price.toFixed(2) + '</td>';
    html += '<td class="scr-chg ' + chgCls + ' scr-chg-cell">' + chgStr + '</td>';
    html += '<td><span class="scr-dir ' + dirCls + '">' + t.direction + '</span></td>';
    html += '<td>' + sigHtml + '</td>';
    html += '<td>' + earnHtml + '</td>';
    html += '<td class="scr-strength">' + t.signal_strength + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';
  lv.innerHTML = html;
  document.getElementById('scrFilterCount').textContent = filtered.length + ' / ' + scrData.signals.length + ' tickers';
}

function scrSearchGo() {
  const input = document.getElementById('scrSearchInput');
  const ticker = (input.value || '').trim().toUpperCase();
  if (!ticker) return;
  input.value = ticker;
  showTickerDetail(ticker);
}

function showTickerDetail(ticker) {
  const item = scrData ? scrData.signals.find(s => s.ticker === ticker) : null;

  document.getElementById('scr-list-view').style.display = 'none';
  document.getElementById('scr-filter-bar').style.display = 'none';
  const dv = document.getElementById('scr-detail-view');
  dv.style.display = 'block';

  let html = '<div class="scr-detail">';
  html += '<div class="scr-detail-header">';
  html += '<button class="scr-back-btn" onclick="backToSignals()">\u2190 Back</button>';
  html += '<h2>' + ticker + '</h2>';
  if (item) {
    const chgCls = item.change_pct >= 0 ? 'pos' : 'neg';
    const chgStr = (item.change_pct >= 0 ? '+' : '') + item.change_pct.toFixed(2) + '%';
    const dirCls = item.direction.toLowerCase();
    html += '<span style="font-size:16px;font-weight:700">$' + item.price.toFixed(2) + '</span>';
    html += '<span class="scr-chg ' + chgCls + '" style="font-size:14px">' + chgStr + '</span>';
    html += '<span class="scr-dir ' + dirCls + '">' + item.direction + '</span>';
  }
  html += '<span id="ta-earnings-icon" style="margin-left:8px"></span>';
  html += '</div>';

  // Sources section (Barchart + Finviz)
  if (item && item.sources && item.sources.length > 0) {
    html += '<div class="scr-section"><h3>External Sources</h3><div style="display:flex;flex-wrap:wrap;gap:4px">';
    for (const src of item.sources) {
      const cls = src.type === 'BC' ? 'src-bc' : 'src-fv';
      html += '<span class="scr-sig-tag ' + cls + '" style="font-size:12px;padding:3px 8px">' + src.type + ':' + src.name + '</span>';
    }
    html += '</div></div>';
  }

  // Signals section (only if local data exists)
  if (item && item.signals && item.signals.length > 0) {
    html += '<div class="scr-section"><h3>Winrate Signals</h3>';
    html += '<table class="scr-detail-table"><thead><tr>';
    html += '<th>Signal</th><th>Category</th><th>Close WR</th><th>H/L WR</th><th>Best Week</th><th>Samples</th>';
    html += '</tr></thead><tbody>';
    for (const sg of item.signals) {
      html += '<tr>';
      html += '<td style="font-weight:600">' + sg.name + '</td>';
      html += '<td><span class="scr-dir ' + (sg.category === 'BEAR' ? 'bear' : 'bull') + '">' + sg.category + '</span></td>';
      html += '<td>' + sg.close_rate.toFixed(1) + '%</td>';
      html += '<td>' + sg.highlow_rate.toFixed(1) + '%</td>';
      html += '<td>' + sg.best_week + '</td>';
      html += '<td>' + sg.samples + '</td>';
      html += '</tr>';
    }
    html += '</tbody></table></div>';
  }

  // Options Flow section (loaded async)
  html += '<div class="scr-section of-section" id="of-container">';
  html += '<h3>Options Flow</h3>';
  html += '<div class="of-progress"><div class="of-progress-bar" id="of-progress" style="width:100%"></div></div>';
  html += '<div style="color:#8b949e;font-size:12px" id="of-loading-text">Loading options data...</div>';
  html += '<div id="of-content" style="display:none"></div>';
  html += '</div>';

  // Analysis panels ABOVE chart (side by side)
  html += '<div id="ta-panels-row" style="display:none;max-width:900px;display:flex;gap:12px;margin-bottom:12px">';
  html += '<div id="ta-squeeze-panel" class="ta-pcard" style="flex:1;display:none"></div>';
  html += '<div id="ta-corr-panel" class="ta-pcard" style="flex:1;display:none;max-height:340px;overflow-y:auto"></div>';
  html += '</div>';

  // TradingView chart
  html += '<div class="scr-section"><h3>Chart</h3>';
  html += '<div id="scr-tv-chart" style="height:600px;max-width:900px;border:1px solid #21262d;border-radius:6px;overflow:hidden"></div>';
  html += '</div>';

  // Pivot stats below chart
  html += '<div id="ta-pivot-panel" style="display:none;max-width:900px"></div>';

  // Finviz data placeholder (fundamentals + analysts + description + news)
  html += '<div id="scr-finviz"><div style="color:#8b949e;font-size:12px;padding:16px">Loading fundamentals...</div></div>';

  html += '</div>';
  dv.innerHTML = html;

  // Inject TradingView chart
  injectTVWidget('scr-tv-chart',
    'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js',
    {
      symbol: ticker,
      theme: 'dark',
      style: '1',
      interval: 'D',
      timezone: 'America/New_York',
      backgroundColor: '#0F0F0F',
      gridColor: 'rgba(242,242,242,0.06)',
      hide_side_toolbar: false,
      hide_top_toolbar: false,
      hide_legend: false,
      hide_volume: false,
      allow_symbol_change: true,
      save_image: true,
      calendar: false,
      withdateranges: true,
      studies: ['STD;RSI','STD;EMA','STD;Bollinger_Bands','Volume@tv-basicstudies'],
      autosize: true,
      locale: 'en',
    }
  );

  // Fetch Finviz fundamentals + Options flow
  loadFinvizData(ticker);
  loadOptionsFlow(ticker);
  loadTickerAnalysis(ticker);
}

let ofMode = 'volume'; // or 'oi'
let ofRange = 'near'; // 'near' = ±20%, 'all' = all strikes

async function loadOptionsFlow(ticker) {
  const container = document.getElementById('of-content');
  const progress = document.getElementById('of-progress');
  const loadText = document.getElementById('of-loading-text');
  if (!container) return;
  try {
    const resp = await fetch('/api/options-flow/' + ticker);
    const d = await resp.json();
    if (d.error) throw new Error(d.error);

    progress.parentElement.style.display = 'none';
    loadText.style.display = 'none';
    container.style.display = 'block';

    // Attach current price from scrData
    const item = scrData ? scrData.signals.find(s => s.ticker === ticker) : null;
    d._price = item ? item.price : 0;
    window._ofData = d;
    ofRange = 'near';
    renderOptionsFlow(d, 'volume');
  } catch(e) {
    loadText.textContent = 'Could not load options data';
    progress.parentElement.style.display = 'none';
  }
}

function renderOptionsFlow(d, mode) {
  ofMode = mode;
  const container = document.getElementById('of-content');
  if (!container) return;
  let h = '';

  // Toggle tabs
  h += '<div class="of-tabs">';
  var volCls = mode === 'volume' ? 'of-tab active' : 'of-tab';
  var oiCls = mode === 'oi' ? 'of-tab active' : 'of-tab';
  h += '<div class="' + volCls + '" onclick="renderOptionsFlow(window._ofData,&quot;volume&quot;)">Volume</div>';
  h += '<div class="' + oiCls + '" onclick="renderOptionsFlow(window._ofData,&quot;oi&quot;)">Open Interest</div>';
  h += '<div style="flex:1"></div>';
  var nearCls = ofRange === 'near' ? 'of-tab active' : 'of-tab';
  var allCls = ofRange === 'all' ? 'of-tab active' : 'of-tab';
  h += '<div class="' + nearCls + '" onclick="ofRange=&quot;near&quot;;renderOptionsFlow(window._ofData,ofMode)">\u00B120%</div>';
  h += '<div class="' + allCls + '" onclick="ofRange=&quot;all&quot;;renderOptionsFlow(window._ofData,ofMode)">All Strikes</div>';
  h += '</div>';

  // Sentiment stats
  const s = d.sentiment || {};
  if (s.net_sentiment || s.delta_imbalance || (d.put_call_ratios && d.put_call_ratios.length > 0)) {
    h += '<div class="of-stats">';
    if (s.net_sentiment) {
      const cls = s.net_sentiment.includes('-') ? 'bearish' : 'bullish';
      h += '<div class="of-stat"><div class="of-stat-label">Net Trade Sentiment</div><div class="of-stat-val ' + cls + '">' + s.net_sentiment + '</div></div>';
    }
    if (s.delta_imbalance) {
      const cls = s.delta_imbalance.includes('-') ? 'bearish' : 'bullish';
      h += '<div class="of-stat"><div class="of-stat-label">Delta Imbalance</div><div class="of-stat-val ' + cls + '">' + s.delta_imbalance + '</div></div>';
    }
    // PCR stats
    for (const pcr of (d.put_call_ratios || [])) {
      const ratio = mode === 'oi' ? pcr.oi_ratio : pcr.vol_ratio;
      const cls = ratio > 1 ? 'bearish' : ratio < 0.7 ? 'bullish' : 'neutral';
      h += '<div class="of-stat"><div class="of-stat-label">P/C Ratio ' + pcr.exp + ' (' + pcr.dte + 'd)</div><div class="of-stat-val ' + cls + '">' + ratio.toFixed(2) + '</div></div>';
    }
    h += '</div>';
  }

  // Bar charts
  if (d.expirations && d.expirations.length > 0) {
    h += '<div class="of-charts">';
    for (const exp of d.expirations) {
      h += '<div class="of-chart-box">';
      h += '<div class="of-chart-title">' + exp.exp + ' (' + exp.dte + ' DTE) — ' + (mode === 'oi' ? 'Open Interest' : 'Volume') + '</div>';
      // Filter by range, then sort by total activity, keep top 30
      var price = d._price || 0;
      var lo = price * 0.8, hi = price * 1.2;
      var strikes = exp.strikes.filter(sk => ofRange === 'all' || price === 0 || (sk.strike >= lo && sk.strike <= hi))
        .map(sk => {
          const cv = mode === 'oi' ? sk.call_oi : sk.call_vol;
          const pv = mode === 'oi' ? sk.put_oi : sk.put_vol;
          return {...sk, _cv: cv, _pv: pv, _total: cv + pv};
        }).filter(sk => sk._total > 0).sort((a,b) => b._total - a._total).slice(0, 30);
      // Re-sort by strike price for display
      strikes.sort((a,b) => a.strike - b.strike);
      if (strikes.length === 0) { h += '<div style="color:#8b949e;font-size:11px;text-align:center">No data</div></div>'; continue; }

      // Find max value for scaling
      let maxVal = 0;
      for (const sk of strikes) {
        if (sk._total > maxVal) maxVal = sk._total;
      }
      if (maxVal === 0) maxVal = 1;

      var priceLine = false;
      for (const sk of strikes) {
        // Insert current price line
        if (!priceLine && price > 0 && sk.strike >= price) {
          h += '<div style="display:flex;align-items:center;margin:4px 0;gap:4px">';
          h += '<div style="width:50px;text-align:right;font-size:10px;font-weight:700;color:#58a6ff;flex-shrink:0">$' + price.toFixed(0) + '</div>';
          h += '<div style="flex:1;height:0;border-top:2px dashed #58a6ff"></div>';
          h += '<div style="width:55px;text-align:right;font-size:9px;color:#58a6ff">Current</div>';
          h += '</div>';
          priceLine = true;
        }
        const cv = sk._cv, pv = sk._pv, total = sk._total;
        const callPct = (cv / maxVal * 100).toFixed(1);
        const putPct = (pv / maxVal * 100).toFixed(1);
        h += '<div class="of-bar-row">';
        h += '<div class="of-bar-label">$' + sk.strike + '</div>';
        h += '<div class="of-bar-wrap">';
        if (cv > 0) h += '<div class="of-bar-call" style="width:' + callPct + '%"><span class="of-bar-tip">Call: ' + cv.toLocaleString() + '</span></div>';
        if (pv > 0) h += '<div class="of-bar-put" style="width:' + putPct + '%"><span class="of-bar-tip">Put: ' + pv.toLocaleString() + '</span></div>';
        h += '</div>';
        h += '<div style="width:55px;text-align:right;color:#8b949e;font-size:9px">' + total.toLocaleString() + '</div>';
        h += '</div>';
      }
      h += '</div>';
    }
    h += '</div>';
  }

  // Put/Call ratio table
  if (d.put_call_ratios && d.put_call_ratios.length > 0) {
    h += '<table class="of-pcr-table" style="margin-top:12px">';
    h += '<thead><tr><th>Expiration</th><th>DTE</th><th>Call Vol</th><th>Put Vol</th><th>Vol P/C</th><th>Call OI</th><th>Put OI</th><th>OI P/C</th></tr></thead><tbody>';
    for (const pcr of d.put_call_ratios) {
      const vc = pcr.vol_ratio > 1 ? 'bearish' : pcr.vol_ratio < 0.7 ? 'bullish' : 'neutral';
      const oc = pcr.oi_ratio > 1 ? 'bearish' : pcr.oi_ratio < 0.7 ? 'bullish' : 'neutral';
      h += '<tr>';
      h += '<td>' + pcr.exp + '</td><td>' + pcr.dte + 'd</td>';
      h += '<td>' + pcr.call_vol.toLocaleString() + '</td><td>' + pcr.put_vol.toLocaleString() + '</td>';
      h += '<td class="of-stat-val ' + vc + '">' + pcr.vol_ratio.toFixed(2) + '</td>';
      h += '<td>' + pcr.call_oi.toLocaleString() + '</td><td>' + pcr.put_oi.toLocaleString() + '</td>';
      h += '<td class="of-stat-val ' + oc + '">' + pcr.oi_ratio.toFixed(2) + '</td>';
      h += '</tr>';
    }
    h += '</tbody></table>';
  }

  container.innerHTML = h;
}

async function loadFinvizData(ticker) {
  const el = document.getElementById('scr-finviz');
  if (!el) return;
  try {
    const r = await fetch('/api/finviz/' + ticker);
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    const f = d.fundamentals || {};

    const v = (k) => f[k] || '-';
    const pctCls = (val) => {
      if (!val || val === '-') return '';
      return parseFloat(val) >= 0 ? 'style="color:#3fb950"' : 'style="color:#f85149"';
    };

    let h = '';

    // ── Fundamentals ──
    h += '<div class="scr-section"><h3>Fundamentals</h3><div class="fv-grid">';

    h += '<div class="fv-card"><div class="fv-card-title">Overview</div>';
    h += '<div class="fv-row"><span>Sector</span><span>' + v('Sector') + '</span></div>';
    h += '<div class="fv-row"><span>Industry</span><span>' + v('Industry') + '</span></div>';
    h += '<div class="fv-row"><span>Market Cap</span><span>' + v('Market Cap') + '</span></div>';
    h += '<div class="fv-row"><span>Price</span><span>' + v('Price') + '</span></div>';
    h += '<div class="fv-row"><span>Volume</span><span>' + v('Volume') + '</span></div>';
    h += '<div class="fv-row"><span>Avg Volume</span><span>' + v('Avg Volume') + '</span></div>';
    h += '</div>';

    h += '<div class="fv-card"><div class="fv-card-title">Valuation</div>';
    h += '<div class="fv-row"><span>P/E</span><span>' + v('P/E') + '</span></div>';
    h += '<div class="fv-row"><span>Forward P/E</span><span>' + v('Forward P/E') + '</span></div>';
    h += '<div class="fv-row"><span>PEG</span><span>' + v('PEG') + '</span></div>';
    h += '<div class="fv-row"><span>P/S</span><span>' + v('P/S') + '</span></div>';
    h += '<div class="fv-row"><span>P/B</span><span>' + v('P/B') + '</span></div>';
    h += '<div class="fv-row"><span>EPS</span><span>' + v('EPS (ttm)') + '</span></div>';
    h += '</div>';

    h += '<div class="fv-card"><div class="fv-card-title">Financial</div>';
    h += '<div class="fv-row"><span>Profit Margin</span><span>' + v('Profit Margin') + '</span></div>';
    h += '<div class="fv-row"><span>Oper. Margin</span><span>' + v('Oper. Margin') + '</span></div>';
    h += '<div class="fv-row"><span>Gross Margin</span><span>' + v('Gross Margin') + '</span></div>';
    h += '<div class="fv-row"><span>ROE</span><span>' + v('ROE') + '</span></div>';
    h += '<div class="fv-row"><span>ROA</span><span>' + v('ROA') + '</span></div>';
    h += '<div class="fv-row"><span>Debt/Eq</span><span>' + v('Debt/Eq') + '</span></div>';
    h += '</div>';

    h += '<div class="fv-card"><div class="fv-card-title">Ownership</div>';
    h += '<div class="fv-row"><span>Insider Own</span><span>' + v('Insider Own') + '</span></div>';
    h += '<div class="fv-row"><span>Inst Own</span><span>' + v('Inst Own') + '</span></div>';
    h += '<div class="fv-row"><span>Short Float</span><span>' + v('Short Float') + '</span></div>';
    h += '<div class="fv-row"><span>Short Ratio</span><span>' + v('Short Ratio') + '</span></div>';
    h += '<div class="fv-row"><span>Insider Trans</span><span>' + v('Insider Trans') + '</span></div>';
    h += '<div class="fv-row"><span>Inst Trans</span><span>' + v('Inst Trans') + '</span></div>';
    h += '</div>';

    h += '<div class="fv-card"><div class="fv-card-title">Performance</div>';
    h += '<div class="fv-row"><span>Perf Week</span><span ' + pctCls(f['Perf Week']) + '>' + v('Perf Week') + '</span></div>';
    h += '<div class="fv-row"><span>Perf Month</span><span ' + pctCls(f['Perf Month']) + '>' + v('Perf Month') + '</span></div>';
    h += '<div class="fv-row"><span>Perf Quarter</span><span ' + pctCls(f['Perf Quarter']) + '>' + v('Perf Quarter') + '</span></div>';
    h += '<div class="fv-row"><span>Perf Half Y</span><span ' + pctCls(f['Perf Half Y']) + '>' + v('Perf Half Y') + '</span></div>';
    h += '<div class="fv-row"><span>Perf Year</span><span ' + pctCls(f['Perf Year']) + '>' + v('Perf Year') + '</span></div>';
    h += '<div class="fv-row"><span>Perf YTD</span><span ' + pctCls(f['Perf YTD']) + '>' + v('Perf YTD') + '</span></div>';
    h += '</div>';

    h += '<div class="fv-card"><div class="fv-card-title">Technical</div>';
    h += '<div class="fv-row"><span>RSI (14)</span><span>' + v('RSI (14)') + '</span></div>';
    h += '<div class="fv-row"><span>SMA20</span><span ' + pctCls(f['SMA20']) + '>' + v('SMA20') + '</span></div>';
    h += '<div class="fv-row"><span>SMA50</span><span ' + pctCls(f['SMA50']) + '>' + v('SMA50') + '</span></div>';
    h += '<div class="fv-row"><span>SMA200</span><span ' + pctCls(f['SMA200']) + '>' + v('SMA200') + '</span></div>';
    h += '<div class="fv-row"><span>Volatility</span><span>' + v('Volatility') + '</span></div>';
    h += '<div class="fv-row"><span>Beta</span><span>' + v('Beta') + '</span></div>';
    h += '</div>';

    h += '</div></div>';  // close fv-grid + section

    // ── Analyst Ratings ──
    if (d.analysts && d.analysts.length > 0) {
      h += '<div class="scr-section"><h3>Analyst Ratings</h3>';
      h += '<table class="scr-detail-table"><thead><tr>';
      h += '<th>Date</th><th>Action</th><th>Analyst</th><th>Rating</th><th>Price Target</th>';
      h += '</tr></thead><tbody>';
      for (const a of d.analysts) {
        const tf = a.target_from ? '$' + a.target_from : '';
        const tt = a.target_to ? '$' + a.target_to : '';
        const pt = tf && tt && tf !== tt ? tf + ' \u2192 ' + tt : (tt || tf || '-');
        const rCls = (a.rating || '').match(/buy|overweight|outperform/i) ? 'style="color:#3fb950"' :
                     (a.rating || '').match(/sell|underweight/i) ? 'style="color:#f85149"' : '';
        h += '<tr>';
        h += '<td>' + (a.date || '-') + '</td>';
        h += '<td>' + (a.category || '-') + '</td>';
        h += '<td>' + (a.analyst || '-') + '</td>';
        h += '<td ' + rCls + '>' + (a.rating || '-') + '</td>';
        h += '<td>' + pt + '</td>';
        h += '</tr>';
      }
      h += '</tbody></table></div>';
    }

    // ── Company Description ──
    if (d.description) {
      h += '<div class="scr-section"><h3>About ' + ticker + '</h3>';
      h += '<p style="font-size:12px;color:#c9d1d9;line-height:1.6">' + d.description + '</p>';
      h += '</div>';
    }

    // ── News (bull/bear only, last 7 days) ──
    const weekAgo = new Date(); weekAgo.setDate(weekAgo.getDate() - 7);
    const _bullWords = /surge|rally|gain|rise|beat|exceed|upgrade|bullish|record|growth|profit|strong|soar|jump|outperform|buy|positive|breakout|momentum/i;
    const _bearWords = /drop|fall|decline|crash|miss|downgrade|bearish|loss|weak|plunge|sell|warning|risk|cut|layoff|recession|lawsuit|investigation|fraud|selloff|slump|slide|tumble/i;
    function _sentiment(title) {
      const b = (title.match(_bullWords) || []).length;
      const r = (title.match(_bearWords) || []).length;
      if (b > r) return 'positive';
      if (r > b) return 'negative';
      return 'neutral';
    }
    const _sentLabel = s => s === 'positive' ? 'bull' : s === 'negative' ? 'bear' : '';
    const _sentClass = s => s === 'positive' ? 'positive' : s === 'negative' ? 'negative' : '';
    // Merge: scrData news (has sentiment) + finviz API news
    let allNews = [];
    const item = scrData ? scrData.signals.find(s => s.ticker === ticker) : null;
    if (item && item.news) {
      for (const n of item.news) {
        if (n.sentiment && n.sentiment !== 'neutral') allNews.push(n);
      }
    }
    // Add finviz API news (compute sentiment from title, filter to last week)
    if (d.news && d.news.length > 0) {
      const seen = new Set(allNews.map(n => n.title));
      for (const n of d.news) {
        if (seen.has(n.title)) continue;
        try {
          const nd = new Date(n.date);
          if (nd >= weekAgo) {
            const sent = _sentiment(n.title);
            if (sent !== 'neutral') allNews.push({title: n.title, source: n.source, url: n.url, date: n.date, sentiment: sent});
          }
        } catch(e) {}
      }
    }

    if (allNews.length > 0) {
      h += '<div class="scr-section"><h3>News</h3>';
      for (const n of allNews.slice(0, 15)) {
        h += '<div class="scr-news-item">';
        h += '<a href="' + n.url + '" target="_blank">' + n.title + '</a>';
        h += '<div class="scr-news-meta">' + (n.source || '') + ' \u00B7 ' + (n.date || '');
        const lbl = _sentLabel(n.sentiment); const cls = _sentClass(n.sentiment);
        if (lbl) h += ' <span class="scr-sentiment ' + cls + '">' + lbl + '</span>';
        h += '</div></div>';
      }
      h += '</div>';
    }

    el.innerHTML = h;
  } catch(e) {
    el.innerHTML = '<div class="scr-section"><h3>Fundamentals</h3><p style="color:#8b949e;font-size:12px">Could not load data</p></div>';
  }
}

// ── Ticker Analysis: TTM/ATR/ADX + Correlations + Pivots ──
async function loadTickerAnalysis(ticker) {
  // Show loading placeholders
  const loadHtml = '<div style="padding:16px;color:#8b949e;font-size:12px;text-align:center">Loading analysis...</div>';
  const sq = document.getElementById('ta-squeeze-panel');
  const cr = document.getElementById('ta-corr-panel');
  const pv = document.getElementById('ta-pivot-panel');
  if (sq) { sq.innerHTML = loadHtml; sq.style.display = 'block'; }
  if (cr) { cr.innerHTML = loadHtml; cr.style.display = 'block'; }
  if (pv) { pv.innerHTML = loadHtml; pv.style.display = 'block'; }
  const pr = document.getElementById('ta-panels-row');
  if (pr) pr.style.display = 'flex';

  try {
    const r = await fetch('/api/ticker-analysis/' + ticker);
    const d = await r.json();
    if (d.error) { console.warn('ticker-analysis:', d.error); if(sq)sq.innerHTML='<div style="padding:8px;color:#f85149;font-size:11px">'+d.error+'</div>'; return; }
    renderSqueezePanel(d);
    renderCorrPanel(d);
    renderPivotPanel(d);
    renderTAEarningsIcon(ticker, d.next_earnings, d.next_earnings_time);
  } catch(e) { console.warn('loadTickerAnalysis failed:', e); }
}

function renderSqueezePanel(d) {
  const el = document.getElementById('ta-squeeze-panel');
  if (!el) return;
  const sq = d.squeeze || {};
  const atr = d.atr || {};
  const adx = d.adx || {};

  const sqClr = sq.state === 'ON' ? '#f85149' : sq.state === 'OFF' ? '#3fb950' : '#d29922';
  const momClr = sq.mom_bull ? '#3fb950' : '#f85149';
  const momArr = sq.mom_dir === 'up' ? '▲' : '▼';
  const volClr = atr.zone === 'High' ? '#f85149' : atr.zone === 'Low' ? '#3fb950' : '#d29922';
  const adxClr = adx.value >= 25 ? '#3fb950' : adx.value >= 20 ? '#d29922' : '#8b949e';
  const dirClr = adx.direction === 'Bull' ? '#3fb950' : '#f85149';

  let h = '<h4>TTM Squeeze</h4>';
  h += '<div class="ta-row"><span class="lbl">State</span><span class="val" style="color:'+sqClr+'">'+sq.state+' ('+sq.bars_in_state+'b)</span></div>';
  h += '<div class="ta-row"><span class="lbl">Momentum</span><span class="val" style="color:'+momClr+'">'+momArr+' '+(sq.mom_bull?'Bull':'Bear')+' '+sq.momentum+'</span></div>';
  h += '<div class="ta-divider"></div>';
  h += '<h4>ATR (14)</h4>';
  h += '<div class="ta-row"><span class="lbl">ATR</span><span class="val">'+atr.value+' ('+atr.pct+'%)</span></div>';
  h += '<div class="ta-row"><span class="lbl">Volatility</span><span class="val" style="color:'+volClr+'">'+atr.zone+'</span></div>';
  h += '<div class="ta-row"><span class="lbl">Trend</span><span class="val">'+atr.trend+(atr.breakout?' · Breakout':'')+'</span></div>';
  h += '<div class="ta-divider"></div>';
  h += '<h4>ADX (14)</h4>';
  h += '<div class="ta-row"><span class="lbl">ADX</span><span class="val" style="color:'+adxClr+'">'+adx.value+'</span></div>';
  h += '<div class="ta-row"><span class="lbl">DI+/DI−</span><span class="val"><span style="color:#3fb950">'+adx.di_plus+'</span> / <span style="color:#f85149">'+adx.di_minus+'</span></span></div>';
  h += '<div class="ta-row"><span class="lbl">Trend</span><span class="val" style="color:'+dirClr+'">'+adx.trend+' · '+adx.direction+'</span></div>';

  el.innerHTML = h;
  el.style.display = 'block';
  document.getElementById('ta-panels-row').style.display = 'flex';
}

function renderCorrPanel(d) {
  const el = document.getElementById('ta-corr-panel');
  if (!el || !d.correlations) return;

  function corrClr(v) {
    if (v == null) return '#8b949e';
    if (v >= 0.8) return '#3fb950';
    if (v >= 0.6) return '#7ee787';
    if (v >= 0.3) return '#d29922';
    if (v >= 0) return '#f0883e';
    return '#f85149';
  }
  function fmt(v) { return v != null ? v.toFixed(3) : 'n/a'; }

  let h = '<h4>Correlations vs ETFs</h4>';
  h += '<table class="ta-corr-tbl"><thead><tr><th>ETF</th><th>1M</th><th>3M</th></tr></thead><tbody>';
  for (const c of d.correlations) {
    h += '<tr>';
    h += '<td>'+c.etf+'</td>';
    h += '<td style="color:'+corrClr(c.c1m)+'">'+fmt(c.c1m)+'</td>';
    h += '<td style="color:'+corrClr(c.c3m)+'">'+fmt(c.c3m)+'</td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  el.innerHTML = h;
  el.style.display = 'block';
  document.getElementById('ta-panels-row').style.display = 'flex';
}

function renderPivotPanel(d) {
  const el = document.getElementById('ta-pivot-panel');
  if (!el || !d.pivots) return;
  const p = d.pivots;
  const r = p.rise || {};
  const pb = p.pull || {};

  function prow(lbl, val, clr) {
    const c = clr ? ' style="color:'+clr+'"' : '';
    return '<div class="ta-prow"><span class="lbl">'+lbl+'</span><span class="val"'+c+'>'+(val != null ? val : '-')+'</span></div>';
  }

  function distTable(title, buckets, unit) {
    if (!buckets || !buckets.length) return '';
    let t = '<div class="ta-pcard" style="margin-top:12px"><h4>'+title+'</h4>';
    t += '<table style="width:100%;border-collapse:collapse;font-size:11px">';
    t += '<thead><tr style="border-bottom:1px solid #21262d"><th style="text-align:left;padding:3px 6px;color:#8b949e;font-size:10px">Bucket</th><th style="text-align:right;padding:3px 6px;color:#3fb950;font-size:10px">Rise %</th><th style="text-align:right;padding:3px 6px;color:#f0883e;font-size:10px">Pull %</th></tr></thead><tbody>';
    for (const b of buckets) {
      const lbl = unit === 'd' ? Math.round(b.lo)+'–'+Math.round(b.hi)+'d' : b.lo.toFixed(1)+'–'+b.hi.toFixed(1)+'%';
      t += '<tr style="border-bottom:1px solid rgba(33,38,45,0.3)">';
      t += '<td style="padding:2px 6px;color:#c9d1d9">'+lbl+'</td>';
      t += '<td style="padding:2px 6px;text-align:right;color:#3fb950;font-weight:600">'+b.rise_pct+'%</td>';
      t += '<td style="padding:2px 6px;text-align:right;color:#f0883e;font-weight:600">'+b.pull_pct+'%</td>';
      t += '</tr>';
    }
    t += '</tbody></table></div>';
    return t;
  }

  let h = '<div class="scr-section"><h3>Pivot Rise / Pullback Stats</h3>';
  h += '<div class="ta-pivot-grid">';

  // Rise card
  h += '<div class="ta-pcard"><h4 style="color:#3fb950">&#9650; Rise</h4>';
  h += prow('Count', r.count);
  h += prow('Avg Bars', r.avg_bars, '#58a6ff');
  h += prow('Min / Max Bars', r.min_bars != null ? r.min_bars+' / '+r.max_bars : null);
  h += prow('Avg %', r.avg_pct != null ? '+'+r.avg_pct+'%' : null, '#3fb950');
  h += prow('Min / Max %', r.min_pct != null ? '+'+r.min_pct+'% / +'+r.max_pct+'%' : null);
  h += '</div>';

  // Pullback card
  h += '<div class="ta-pcard"><h4 style="color:#f0883e">&#9660; Pullback</h4>';
  h += prow('Count', pb.count);
  h += prow('Avg Bars', pb.avg_bars, '#58a6ff');
  h += prow('Min / Max Bars', pb.min_bars != null ? pb.min_bars+' / '+pb.max_bars : null);
  h += prow('Avg %', pb.avg_pct != null ? '-'+pb.avg_pct+'%' : null, '#f0883e');
  h += prow('Min / Max %', pb.min_pct != null ? '-'+pb.min_pct+'% / -'+pb.max_pct+'%' : null);
  h += '</div>';

  h += '</div>';  // close grid

  // Current position row
  h += '<div style="display:flex;gap:24px;margin-top:8px;font-size:12px">';
  const phClr = p.close_vs_ph_pct != null && p.close_vs_ph_pct < 0 ? '#f85149' : '#3fb950';
  const plClr = p.close_vs_pl_pct != null && p.close_vs_pl_pct > 0 ? '#3fb950' : '#f85149';
  h += '<span style="color:#8b949e">Last PH: <strong style="color:'+phClr+'">'+(p.close_vs_ph_pct != null ? (p.close_vs_ph_pct>=0?'+':'')+p.close_vs_ph_pct+'%' : '-')+'</strong> ('+(p.bars_since_ph||'-')+'d ago)</span>';
  h += '<span style="color:#8b949e">Last PL: <strong style="color:'+plClr+'">'+(p.close_vs_pl_pct != null ? (p.close_vs_pl_pct>=0?'+':'')+p.close_vs_pl_pct+'%' : '-')+'</strong> ('+(p.bars_since_pl||'-')+'d ago)</span>';
  h += '</div>';

  // Distribution tables (side by side)
  h += '<div class="ta-pivot-grid">';
  h += distTable('Bars Distribution', p.bars_dist, 'd');
  h += distTable('% Move Distribution', p.pct_dist, '%');
  h += '</div>';

  h += '</div>';  // close section
  el.innerHTML = h;
  el.style.display = 'block';
}

function renderTAEarningsIcon(ticker, nextEarnings, nextEarningsTime) {
  const el = document.getElementById('ta-earnings-icon');
  if (!el) return;
  if (nextEarnings) {
    const timeLabel = nextEarningsTime === 'BMO' ? ' BM' : nextEarningsTime === 'AMC' ? ' AM' : '';
    const btn = document.createElement('button');
    btn.title = 'Click for 5Y earnings history';
    btn.style.cssText = 'background:#d2992222;color:#d29922;border:1px solid #d2992244;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;font-weight:600;display:inline-flex;align-items:center;gap:4px';
    btn.innerHTML = '&#128227; Earnings ' + nextEarnings + (timeLabel ? ' <span style="background:#d2992244;padding:1px 5px;border-radius:3px;font-size:10px">' + timeLabel.trim() + '</span>' : '');
    btn.onclick = function(e) { e.stopPropagation(); showEarningsHistory(ticker, nextEarnings); };
    el.innerHTML = '';
    el.appendChild(btn);
  }
}

function backToSignals() {
  document.getElementById('scrSearchInput').value = '';
  document.getElementById('scr-detail-view').style.display = 'none';
  document.getElementById('scr-list-view').style.display = 'block';
  document.getElementById('scr-filter-bar').style.display = 'flex';
}

function downloadScreenerJsonl() {
  if (!scrData || !scrData.signals || !scrData.signals.length) {
    alert('No screener data. Run a scan first.');
    return;
  }
  const filtered = getFiltered();
  const lines = filtered.map(t => {
    return JSON.stringify({
      ticker: t.ticker,
      price: t.price,
      change_pct: t.change_pct,
      direction: t.direction,
      signal_strength: t.signal_strength,
      signals: t.signals.map(sg => ({
        name: sg.name,
        category: sg.category,
        close_wr: sg.close_rate,
        hl_wr: sg.highlow_rate,
        best_week: sg.best_week
      })),
      sources: (t.sources || []).map(s => s.type + ':' + s.name)
    });
  });
  const blob = new Blob([lines.join('\\n') + '\\n'], {type: 'application/x-jsonlines'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const d = scrData.scan_date || new Date().toISOString().slice(0,10);
  a.href = url;
  a.download = 'screener_' + d + '.jsonl';
  a.click();
  URL.revokeObjectURL(url);
}

function downloadScreenerCsv() {
  if (!scrData || !scrData.signals || !scrData.signals.length) {
    alert('No screener data. Run a scan first.');
    return;
  }
  const filtered = getFiltered();
  const tickers = filtered.map(t => t.ticker);
  const csv = 'Ticker\\n' + tickers.join('\\n') + '\\n';
  const blob = new Blob([csv], {type: 'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const d = scrData.scan_date || new Date().toISOString().slice(0,10);
  a.href = url;
  a.download = 'screener_tickers_' + d + '.csv';
  a.click();
  URL.revokeObjectURL(url);
}

// ── Backtest view ────────────────────────────────────────────
async function openBacktestView() {
  document.getElementById('scr-list-view').style.display = 'none';
  document.getElementById('scr-detail-view').style.display = 'none';
  document.getElementById('bt-detail-view').style.display = 'none';
  document.getElementById('scr-filter-bar').style.display = 'none';
  document.getElementById('bt-view').style.display = '';
  await renderBacktestView();
}

async function exitBacktestView() {
  document.getElementById('bt-view').style.display = 'none';
  document.getElementById('bt-detail-view').style.display = 'none';
  document.getElementById('scr-list-view').style.display = 'block';
  if (scrData && scrData.signals && scrData.signals.length) {
    document.getElementById('scr-filter-bar').style.display = 'flex';
  }
}

// Backtest filter state — defaults: show only open positions
const _btFilter = {
  status: 'open',      // 'open' | 'closed' | 'all'
  direction: 'all',    // 'all' | 'BULL' | 'BEAR'
  openDate: 'all',     // 'all' or YYYY-MM-DD
  signal: 'all',       // 'all' or signal name
};
let _btMode = 'a';      // 'a' SL+TP / 'b' TP only / 'c' Hold / 'custom'
let _btSlPct = 50;      // for custom mode (positive = -X% threshold)
let _btTpPct = 120;     // for custom mode

async function btSetMode(m) {
  if (_btMode === m) return;
  _btMode = m;
  await renderBacktestView();
}
let _btCustomDebounce = null;
function btSetCustomVal(key, val) {
  const v = parseFloat(val);
  if (isNaN(v)) return;
  if (key === 'sl') _btSlPct = v;
  if (key === 'tp') _btTpPct = v;
  // Debounce so typing doesn't fire a fetch on every keystroke
  if (_btCustomDebounce) clearTimeout(_btCustomDebounce);
  _btCustomDebounce = setTimeout(() => {
    if (_btMode !== 'custom') _btMode = 'custom';
    renderBacktestView();
  }, 300);
}
async function btRunSnapshot() {
  const s = document.getElementById('bt-status');
  if (s) s.textContent = 'Snapshotting 264 tickers — this takes a few minutes...';
  await fetch('/api/backtest/snapshot', {method:'POST'});
  _btPollStatus();
}
// Column sort state. Default: P/L descending.
let _btSort = { col: 'pnl', asc: false };
let _btAllPositions = [];
let _btSummary = null;

function btSetSort(col) {
  if (_btSort.col === col) _btSort.asc = !_btSort.asc;
  else { _btSort.col = col; _btSort.asc = false; }
  const filtered = _btApplySort(_btApplyFilters());
  _btSummary = _btComputeSummary(filtered);
  document.getElementById('bt-view').innerHTML = _renderBacktestHtml(_btSummary, filtered);
}

function _btApplySort(positions) {
  const {col, asc} = _btSort;
  const mul = asc ? 1 : -1;
  const val = (p) => {
    switch (col) {
      case 'ticker':   return p.ticker || '';
      case 'direction':return p.direction || '';
      case 'open':     return p.open_date || '';
      case 'exp':      return p.expiration || '';
      case 'debit':    return p.net_debit || 0;
      case 'max':      return p.max_profit || 0;
      case 'stockOpen':return p.stock_price_at_open || 0;
      case 'stockNow': return p.current_stock_price || 0;
      case 'pnl':      return p.status === 'open' ? (p.current_pnl || 0) : (p.realized_pnl || 0);
      case 'pctMax':   return (p.max_profit > 0) ? ((p.status==='open'?(p.current_pnl||0):(p.realized_pnl||0)) / p.max_profit * 100) : 0;
      case 'status':   return p.status || '';
      default: return 0;
    }
  };
  return [...positions].sort((a, b) => {
    const va = val(a), vb = val(b);
    if (typeof va === 'string') return mul * va.localeCompare(vb);
    return mul * (va - vb);
  });
}

function _btSortInd(col) {
  if (_btSort.col !== col) return '<span style="color:#555;font-size:9px">▲▼</span>';
  return '<span style="color:#58a6ff;font-size:10px">' + (_btSort.asc ? '▲' : '▼') + '</span>';
}

async function renderBacktestView() {
  const v = document.getElementById('bt-view');
  v.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">Loading backtest...</div>';
  try {
    let url = '/api/backtest/positions?mode=' + _btMode;
    if (_btMode === 'custom') {
      url += '&sl_pct=' + _btSlPct + '&tp_pct=' + _btTpPct;
    }
    const posResp = await fetch(url).then(r=>r.json());
    _btAllPositions = posResp.positions || [];
    const filtered = _btApplySort(_btApplyFilters());
    _btSummary = _btComputeSummary(filtered);
    v.innerHTML = _renderBacktestHtml(_btSummary, filtered);
  } catch(e) {
    v.innerHTML = '<div style="color:var(--red)">Error: ' + e.message + '</div>';
  }
}

// Apply filters to _btAllPositions. If exclude=<key>, that filter key is ignored
// (used to count pill candidates independent of their own filter).
function _btApplyFilters(exclude) {
  const f = _btFilter;
  return _btAllPositions.filter(p => {
    if (exclude !== 'status') {
      if (f.status === 'open' && p.status !== 'open') return false;
      if (f.status === 'closed' && p.status === 'open') return false;
    }
    if (exclude !== 'direction' && f.direction !== 'all' && p.direction !== f.direction) return false;
    if (exclude !== 'openDate' && f.openDate !== 'all' && p.open_date !== f.openDate) return false;
    if (exclude !== 'signal' && f.signal !== 'all') {
      const names = (p.signals || []).map(s => (s.name || '').split(' ')[0].split('(')[0].trim());
      if (!names.includes(f.signal)) return false;
    }
    return true;
  });
}

// Recompute summary from a filtered position list (ignores global sum from server).
function _btComputeSummary(positions) {
  const open = positions.filter(p => p.status === 'open');
  const closed = positions.filter(p => p.status !== 'open');
  const totUnreal = open.reduce((s, p) => s + (p.current_pnl || 0), 0);
  const totReal = closed.reduce((s, p) => s + (p.realized_pnl || 0), 0);
  const wins = closed.filter(p => (p.realized_pnl || 0) > 0).length;
  const maxP = open.reduce((s, p) => s + (p.max_profit || 0), 0);
  const maxL = open.reduce((s, p) => s + (p.max_loss || 0), 0);
  const reasons = {take_profit_120pct:0, stop_loss_50pct:0, expired_win:0, expired_loss:0, closed:0};
  for (const p of closed) if (p.status in reasons) reasons[p.status]++;
  return {
    open_count: open.length,
    closed_count: closed.length,
    total_count: positions.length,
    total_unrealized_pnl: +totUnreal.toFixed(2),
    total_realized_pnl: +totReal.toFixed(2),
    win_rate_closed: closed.length ? +(wins/closed.length*100).toFixed(1) : 0,
    max_profit_if_all_hit: +maxP.toFixed(2),
    max_loss_if_all_hit: +maxL.toFixed(2),
    close_reasons: reasons,
  };
}

function btSetFilter(key, val) {
  _btFilter[key] = val;
  const filtered = _btApplySort(_btApplyFilters());
  _btSummary = _btComputeSummary(filtered);
  document.getElementById('bt-view').innerHTML = _renderBacktestHtml(_btSummary, filtered);
}

// Status display: raw string → {label, color, icon}
function _btStatusDisplay(status) {
  switch (status) {
    case 'open':                return {label:'Open',        color:'#d29922', icon:'⏱'};
    case 'stop_loss_50pct':     return {label:'Stop Loss',   color:'#f85149', icon:'🛑'};
    case 'take_profit_120pct':  return {label:'Take Profit', color:'#3fb950', icon:'✓'};
    case 'expired_win':         return {label:'Held→Win',    color:'#3fb950', icon:'⏰'};
    case 'expired_loss':        return {label:'Held→Loss',   color:'#f85149', icon:'⏰'};
    case 'closed':              return {label:'Closed',      color:'#8b949e', icon:'✕'};
    default:                    return {label: status,       color:'#8b949e', icon:'•'};
  }
}

function _renderBacktestHtml(sum, positions) {
  const clr = v => v > 0.01 ? '#3fb950' : v < -0.01 ? '#f85149' : '#8b949e';
  const fmt$ = v => (v>=0?'+':'-') + '$' + Math.abs(v).toFixed(0);

  // Build filter options from ALL positions (not filtered) so user can re-pick
  const allDates = [...new Set(_btAllPositions.map(p => p.open_date))].sort().reverse();
  const allSigs = new Set();
  for (const p of _btAllPositions) {
    for (const s of (p.signals || [])) {
      const short = (s.name || '').split(' ')[0].split('(')[0].trim();
      if (short) allSigs.add(short);
    }
  }
  const sigList = [...allSigs].sort();
  const f = _btFilter;
  const pillStyle = (active) => `background:${active?'#1f6feb':'#161b22'};color:${active?'#fff':'#8b949e'};border:1px solid ${active?'#1f6feb':'#30363d'};padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:${active?'700':'500'}`;

  // Pill counts respect the OTHER active filters (apply all filters except the
  // one tied to that pill). E.g. Status pill counts reflect the current
  // direction+date+signal choice, so "Open (N)" updates as other filters change.
  const statusPool = _btApplyFilters('status');
  const openCt = statusPool.filter(p => p.status === 'open').length;
  const closedCt = statusPool.length - openCt;
  const dirPool = _btApplyFilters('direction');
  const bullCt = dirPool.filter(p => p.direction === 'BULL').length;
  const bearCt = dirPool.filter(p => p.direction === 'BEAR').length;

  let h = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
      <button onclick="exitBacktestView()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">&#8592; Back</button>
      <h2 style="margin:0;font-size:18px">&#128202; Paper Trade Backtest</h2>
      <div style="display:flex;align-items:center;gap:4px;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:2px 8px;margin-left:20px">
        <span style="color:#8b949e;font-size:13px">&#128269;</span>
        <input id="btSearchInput" type="text" placeholder="Ticker history..." onkeydown="if(event.key==='Enter')btSearchTicker()" oninput="btFilterRows(this.value)" style="background:transparent;border:none;color:#e6edf3;font-size:13px;width:120px;outline:none;font-weight:600;text-transform:uppercase">
        <button onclick="btSearchTicker()" style="background:#1f6feb;color:#fff;border:none;padding:2px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">Go</button>
      </div>
      <div style="margin-left:auto;display:flex;gap:6px">
        <button onclick="btRunSnapshot()" style="background:#3fb950;color:#000;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700" title="Snapshot all 264 universe tickers' chains + update P/L (~5-10 min)">Snapshot 264 + P/L</button>
        <button onclick="btRunOpen()" style="background:#6d3eff;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700">Run Today's Scan</button>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
      <span style="color:#8b949e;font-size:11px">P/L Mode:</span>
      <button onclick="btSetMode('a')" style="${pillStyle(_btMode==='a')}" title="Stop-loss at -50% of debit AND take-profit at +120% of debit">A · 50% SL + 120% TP</button>
      <button onclick="btSetMode('b')" style="${pillStyle(_btMode==='b')}" title="Take-profit at +120% only — no stop-loss, let losers run to expiration">B · 120% TP only</button>
      <button onclick="btSetMode('c')" style="${pillStyle(_btMode==='c')}" title="No rules — every position held to expiration">C · Hold to Expiry</button>
      <button onclick="btSetMode('custom')" style="${pillStyle(_btMode==='custom')}" title="Type your own thresholds">Custom</button>
      <div style="display:flex;align-items:center;gap:4px;${_btMode==='custom'?'':'opacity:0.5'}">
        <span style="color:#8b949e;font-size:11px">SL=-</span>
        <input type="number" min="0" max="100" step="5" value="${_btSlPct}" onchange="btSetCustomVal('sl',this.value)" oninput="btSetCustomVal('sl',this.value)" style="width:50px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e6edf3;padding:2px 4px;font-size:11px;text-align:center" /><span style="color:#8b949e;font-size:11px">%</span>
        <span style="color:#8b949e;font-size:11px;margin-left:6px">TP=+</span>
        <input type="number" min="0" max="500" step="10" value="${_btTpPct}" onchange="btSetCustomVal('tp',this.value)" oninput="btSetCustomVal('tp',this.value)" style="width:55px;background:#0d1117;border:1px solid #30363d;border-radius:4px;color:#e6edf3;padding:2px 4px;font-size:11px;text-align:center" /><span style="color:#8b949e;font-size:11px">%</span>
      </div>
      <span style="color:#8b949e;font-size:10px;margin-left:auto">Re-projects every position from daily P/L history (no re-fetch).</span>
    </div>
    <div id="bt-status" style="font-size:12px;color:#8b949e;margin-bottom:10px"></div>
    <!-- Filter bar -->
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 12px;margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:4px">
        <span style="color:#8b949e;font-size:11px;margin-right:4px">Status:</span>
        <button onclick="btSetFilter('status','open')" style="${pillStyle(f.status==='open')}">Open (${openCt})</button>
        <button onclick="btSetFilter('status','closed')" style="${pillStyle(f.status==='closed')}">Closed (${closedCt})</button>
        <button onclick="btSetFilter('status','all')" style="${pillStyle(f.status==='all')}">All</button>
      </div>
      <div style="display:flex;align-items:center;gap:4px">
        <span style="color:#8b949e;font-size:11px;margin-right:4px">Direction:</span>
        <button onclick="btSetFilter('direction','all')" style="${pillStyle(f.direction==='all')}">All (${dirPool.length})</button>
        <button onclick="btSetFilter('direction','BULL')" style="${pillStyle(f.direction==='BULL')};color:${f.direction==='BULL'?'#fff':'#3fb950'}">Bull (${bullCt})</button>
        <button onclick="btSetFilter('direction','BEAR')" style="${pillStyle(f.direction==='BEAR')};color:${f.direction==='BEAR'?'#fff':'#f85149'}">Bear (${bearCt})</button>
      </div>
      <div style="display:flex;align-items:center;gap:4px">
        <span style="color:#8b949e;font-size:11px;margin-right:4px">Open Date:</span>
        <select onchange="btSetFilter('openDate',this.value)" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:4px;font-size:11px">
          <option value="all" ${f.openDate==='all'?'selected':''}>All dates</option>
          ${allDates.map(d => `<option value="${d}" ${f.openDate===d?'selected':''}>${d}</option>`).join('')}
        </select>
      </div>
      <div style="display:flex;align-items:center;gap:4px">
        <span style="color:#8b949e;font-size:11px;margin-right:4px">Signal:</span>
        <select onchange="btSetFilter('signal',this.value)" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:4px;font-size:11px">
          <option value="all" ${f.signal==='all'?'selected':''}>All signals</option>
          ${sigList.map(s => `<option value="${s}" ${f.signal===s?'selected':''}>${s}</option>`).join('')}
        </select>
      </div>
      <span style="margin-left:auto;color:#8b949e;font-size:11px">Showing <b style="color:#e6edf3">${positions.length}</b> of ${_btAllPositions.length}</span>
    </div>
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:18px">
      <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
        <div style="font-size:11px;color:#8b949e">Open Positions</div>
        <div style="font-size:22px;font-weight:800">${sum.open_count}</div>
      </div>
      <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
        <div style="font-size:11px;color:#8b949e">Unrealized P/L</div>
        <div style="font-size:22px;font-weight:800;color:${clr(sum.total_unrealized_pnl)}">${fmt$(sum.total_unrealized_pnl)}</div>
      </div>
      <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
        <div style="font-size:11px;color:#8b949e">Closed (${sum.closed_count})</div>
        <div style="font-size:22px;font-weight:800;color:${clr(sum.total_realized_pnl)}">${fmt$(sum.total_realized_pnl)}</div>
        ${sum.close_reasons ? `<div style="font-size:10px;color:#8b949e;margin-top:4px;line-height:1.5">
          <span style="color:#3fb950">✓ TP ${sum.close_reasons.take_profit_120pct||0}</span>
          · <span style="color:#f85149">🛑 SL ${sum.close_reasons.stop_loss_50pct||0}</span>
          · <span style="color:#3fb950">⏰ ${sum.close_reasons.expired_win||0}W</span>
          · <span style="color:#f85149">${sum.close_reasons.expired_loss||0}L</span>
        </div>`:''}
      </div>
      <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
        <div style="font-size:11px;color:#8b949e">Win Rate (closed)</div>
        <div style="font-size:22px;font-weight:800">${sum.win_rate_closed}%</div>
      </div>
      <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
        <div style="font-size:11px;color:#8b949e">Max Profit / Loss</div>
        <div style="font-size:14px;font-weight:700"><span style="color:#3fb950">+$${sum.max_profit_if_all_hit.toFixed(0)}</span> / <span style="color:#f85149">$${sum.max_loss_if_all_hit.toFixed(0)}</span></div>
      </div>
    </div>
    <table class="dashboard-table" style="width:100%;font-size:12px">
      <thead><tr>
        <th onclick="btSetSort('ticker')" style="cursor:pointer">Ticker ${_btSortInd('ticker')}</th>
        <th onclick="btSetSort('direction')" style="cursor:pointer">Dir ${_btSortInd('direction')}</th>
        <th onclick="btSetSort('open')" style="cursor:pointer">Opened ${_btSortInd('open')}</th>
        <th onclick="btSetSort('exp')" style="cursor:pointer">Exp/DTE ${_btSortInd('exp')}</th>
        <th>Spread</th>
        <th onclick="btSetSort('debit')" style="cursor:pointer">Debit ${_btSortInd('debit')}</th>
        <th onclick="btSetSort('max')" style="cursor:pointer">Max+/- ${_btSortInd('max')}</th>
        <th onclick="btSetSort('stockOpen')" style="cursor:pointer">Stock @Open ${_btSortInd('stockOpen')}</th>
        <th onclick="btSetSort('stockNow')" style="cursor:pointer">Stock Now ${_btSortInd('stockNow')}</th>
        <th onclick="btSetSort('pnl')" style="cursor:pointer">P/L ${_btSortInd('pnl')}</th>
        <th onclick="btSetSort('pctMax')" style="cursor:pointer">% of Max ${_btSortInd('pctMax')}</th>
        <th onclick="btSetSort('status')" style="cursor:pointer">Status ${_btSortInd('status')}</th>
      </tr></thead><tbody>`;
  for (const p of positions) {
    const dte = Math.max(0, Math.ceil((new Date(p.expiration) - new Date()) / 86400000));
    const pnl = p.status === 'open' ? (p.current_pnl || 0) : (p.realized_pnl || 0);
    const pctMax = p.max_profit > 0 ? (pnl / p.max_profit * 100) : 0;
    const st = _btStatusDisplay(p.status);
    h += `<tr data-ticker="${p.ticker}" onclick="btOpenDetail('${p.id}')" style="cursor:pointer">
      <td style="font-weight:700;color:#58a6ff">${p.ticker}</td>
      <td><span style="color:${p.direction==='BULL'?'#3fb950':'#f85149'};font-weight:700">${p.direction}</span></td>
      <td>${p.open_date}</td>
      <td>${p.expiration.slice(5)} (${dte}d)</td>
      <td>${p.long_strike}/${p.short_strike} ${p.option_type} <span style="color:#8b949e;font-size:10px">×${p.contracts||1}</span></td>
      <td>$${p.net_debit.toFixed(2)}</td>
      <td style="font-size:11px">+$${p.max_profit.toFixed(0)}<br>$${p.max_loss.toFixed(0)}</td>
      <td>$${p.stock_price_at_open.toFixed(2)}</td>
      <td>$${(p.current_stock_price || 0).toFixed(2)}</td>
      <td style="color:${clr(pnl)};font-weight:700">${fmt$(pnl)}</td>
      <td style="color:${clr(pctMax)}">${pctMax.toFixed(0)}%</td>
      <td style="color:${st.color};font-size:11px;font-weight:700" title="${p.status}${p.close_date?' on '+p.close_date:''}">${st.icon} ${st.label}${p.close_date?'<br><span style=\"font-size:10px;color:#8b949e;font-weight:400\">'+p.close_date+'</span>':''}</td>
    </tr>`;
  }
  h += '</tbody></table>';
  if (!positions.length) h += '<div style="color:#8b949e;padding:40px;text-align:center">No positions match current filters. Try switching Status to "All" or clearing a filter.</div>';
  return h;
}

function btFilterRows(q) {
  q = (q || '').trim().toUpperCase();
  const rows = document.querySelectorAll('#bt-view tbody tr[data-ticker]');
  rows.forEach(r => {
    const t = r.getAttribute('data-ticker') || '';
    r.style.display = (!q || t.includes(q)) ? '' : 'none';
  });
}

async function btSearchTicker() {
  const input = document.getElementById('btSearchInput');
  const ticker = (input.value || '').trim().toUpperCase();
  if (!ticker) return;
  const dv = document.getElementById('bt-detail-view');
  const mv = document.getElementById('bt-view');
  mv.style.display = 'none';
  dv.style.display = '';
  dv.innerHTML = '<div style="color:#8b949e;padding:20px">Loading ' + ticker + '...</div>';
  try {
    const r = await fetch('/api/backtest/ticker/' + encodeURIComponent(ticker)).then(x=>x.json());
    const s = r.summary || {};
    const positions = r.positions || [];
    const clr = v => v > 0.01 ? '#3fb950' : v < -0.01 ? '#f85149' : '#8b949e';
    const fmt$ = v => (v>=0?'+':'-') + '$' + Math.abs(v).toFixed(0);
    let html = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <button onclick="exitBtDetail()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">&#8592; Back</button>
        <h2 style="margin:0;font-size:20px;color:#58a6ff">${ticker}</h2>
        <span style="color:#8b949e;font-size:12px">${s.total} total positions</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px">
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="font-size:11px;color:#8b949e">Open</div>
          <div style="font-size:20px;font-weight:800">${s.open}</div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="font-size:11px;color:#8b949e">Closed</div>
          <div style="font-size:20px;font-weight:800">${s.closed} (${s.wins}W/${s.losses}L)</div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="font-size:11px;color:#8b949e">Win Rate</div>
          <div style="font-size:20px;font-weight:800">${s.win_rate}%</div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="font-size:11px;color:#8b949e">Unrealized</div>
          <div style="font-size:20px;font-weight:800;color:${clr(s.total_unrealized)}">${fmt$(s.total_unrealized)}</div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="font-size:11px;color:#8b949e">Realized</div>
          <div style="font-size:20px;font-weight:800;color:${clr(s.total_realized)}">${fmt$(s.total_realized)}</div>
        </div>
      </div>
      <table class="dashboard-table" style="width:100%;font-size:12px">
        <thead><tr>
          <th>Dir</th><th>Opened</th><th>Closed</th><th>Exp</th>
          <th>Spread</th><th>Debit</th><th>Stock @Open</th><th>Stock Now</th>
          <th>Realized / Unrealized</th><th>Status</th>
        </tr></thead><tbody>`;
    if (!positions.length) html += '<tr><td colspan="10" style="text-align:center;padding:30px;color:#8b949e">No positions found for ' + ticker + '</td></tr>';
    for (const p of positions) {
      const rv = p.status === 'open' ? (p.current_pnl || 0) : (p.realized_pnl || 0);
      const st = _btStatusDisplay(p.status);
      html += `<tr onclick="btOpenDetail('${p.id}')" style="cursor:pointer">
        <td style="color:${p.direction==='BULL'?'#3fb950':'#f85149'};font-weight:700">${p.direction}</td>
        <td>${p.open_date}</td>
        <td>${p.close_date || '-'}</td>
        <td>${p.expiration.slice(5)}</td>
        <td>${p.long_strike}/${p.short_strike}${p.option_type}</td>
        <td>$${p.net_debit.toFixed(2)}</td>
        <td>$${p.stock_price_at_open.toFixed(2)}</td>
        <td>$${(p.current_stock_price || 0).toFixed(2)}</td>
        <td style="color:${clr(rv)};font-weight:700">${fmt$(rv)}</td>
        <td style="color:${st.color};font-size:11px;font-weight:700" title="${p.status}">${st.icon} ${st.label}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    dv.innerHTML = html;
  } catch (e) { dv.innerHTML = '<div style="color:var(--red)">Error: '+e.message+'</div>'; }
}

async function btRunOpen() {
  const s = document.getElementById('bt-status');
  s.textContent = 'Running open scan... (pulling option chains from TastyTrade)';
  await fetch('/api/backtest/run', {method:'POST'});
  _btPollStatus();
}

async function btRunPnl() {
  const s = document.getElementById('bt-status');
  s.textContent = 'Updating P/L for all open positions...';
  await fetch('/api/backtest/pnl', {method:'POST'});
  _btPollStatus();
}

function _btPollStatus() {
  const poll = setInterval(async () => {
    const r = await fetch('/api/backtest/status').then(x=>x.json());
    const s = document.getElementById('bt-status');
    if (s) s.textContent = r.progress || '';
    if (!r.running) {
      clearInterval(poll);
      await renderBacktestView();
    }
  }, 2000);
}

async function btOpenDetail(posId) {
  document.getElementById('bt-view').style.display = 'none';
  const dv = document.getElementById('bt-detail-view');
  dv.style.display = '';
  dv.innerHTML = '<div style="color:#8b949e;padding:20px">Loading...</div>';
  try {
    const r = await fetch('/api/backtest/position/' + posId).then(x=>x.json());
    const p = r.position, h = r.history || [];
    const clr = v => v > 0.01 ? '#3fb950' : v < -0.01 ? '#f85149' : '#8b949e';
    const fmt$ = v => (v>=0?'+':'-') + '$' + Math.abs(v).toFixed(2);
    const st = _btStatusDisplay(p.status);
    const closeBanner = p.status === 'open' ? '' : `
      <div style="background:rgba(${p.status==='take_profit_120pct'||p.status==='expired_win'?'63,185,80':'248,81,73'},0.12);border:1px solid ${st.color};border-radius:6px;padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;gap:10px">
        <span style="font-size:22px">${st.icon}</span>
        <div>
          <div style="font-weight:700;font-size:14px;color:${st.color}">${st.label}</div>
          <div style="font-size:11px;color:#8b949e">
            ${p.status==='stop_loss_50pct'?'Auto-closed: unrealized P/L hit −50% of debit paid ':
              p.status==='take_profit_120pct'?'Auto-closed: unrealized P/L hit +120% of debit paid ':
              p.status==='expired_win'?'Held to expiration, settled in-the-money ':
              p.status==='expired_loss'?'Held to expiration, settled out-of-the-money ':
              'Closed '}
            · closed ${p.close_date}${p.realized_pnl!==undefined?' · realized $'+(p.realized_pnl>=0?'+':'')+p.realized_pnl.toFixed(2):''}
          </div>
        </div>
      </div>`;
    let html = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <button onclick="exitBtDetail()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">&#8592; Back to Backtest</button>
        <h2 style="margin:0;font-size:18px">${p.ticker} ${p.direction} ${p.long_strike}/${p.short_strike}${p.option_type}</h2>
      </div>
      ${closeBanner}
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px">
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="color:#8b949e;font-size:11px">Opened</div>
          <div style="font-weight:700">${p.open_date}</div>
          <div style="color:#8b949e;font-size:11px;margin-top:4px">Expiration</div>
          <div style="font-weight:700">${p.expiration}</div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="color:#8b949e;font-size:11px">Net Debit</div>
          <div style="font-weight:700">$${p.net_debit.toFixed(2)} (×100 = $${(p.net_debit*100).toFixed(0)})</div>
          <div style="color:#8b949e;font-size:11px;margin-top:4px">Max Profit / Loss</div>
          <div><span style="color:#3fb950;font-weight:700">+$${p.max_profit.toFixed(0)}</span> / <span style="color:#f85149;font-weight:700">$${p.max_loss.toFixed(0)}</span></div>
        </div>
        <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px">
          <div style="color:#8b949e;font-size:11px">Signals</div>
          <div style="font-size:12px;line-height:1.6">${(p.signals||[]).map(s=>s.name+(s.rate?' '+s.rate+'%':'')).join(', ') || '-'}</div>
          ${p.earnings_date?`<div style="color:#d29922;font-size:11px;margin-top:4px">⚠ Earnings: ${p.earnings_date}</div>`:''}
        </div>
      </div>`;
    // Charts: P/L history + underlying stock price history (side by side)
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">';
    html += '<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px"><div style="font-weight:700;font-size:13px;margin-bottom:8px">Options P/L History</div>';
    if (h.length === 0) html += '<div style="color:#8b949e;padding:20px;text-align:center">No P/L entries yet. Click "Update P/L" on the main Backtest page.</div>';
    else html += '<canvas id="btChart" width="600" height="240"></canvas>';
    html += '</div>';
    html += '<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px"><div style="font-weight:700;font-size:13px;margin-bottom:8px">Underlying Stock Price <span style="color:#8b949e;font-size:11px;font-weight:400">($'+p.stock_price_at_open.toFixed(2)+' @ open · long '+p.long_strike+' · short '+p.short_strike+')</span></div>';
    if (h.length === 0) html += '<div style="color:#8b949e;padding:20px;text-align:center">No price history yet.</div>';
    else html += '<canvas id="btStockChart" width="600" height="240"></canvas>';
    html += '</div>';
    html += '</div>';
    // History table
    html += '<table class="dashboard-table" style="width:100%;font-size:12px"><thead><tr><th>Date</th><th>Days Held</th><th>Stock</th><th>Long Mid</th><th>Short Mid</th><th>Spread Value</th><th>Unrealized P/L</th></tr></thead><tbody>';
    for (const e of h) {
      html += `<tr>
        <td>${e.date}</td><td>${e.days_held}</td>
        <td>$${e.stock_price.toFixed(2)}</td>
        <td>$${e.long_mid.toFixed(2)}</td><td>$${e.short_mid.toFixed(2)}</td>
        <td>$${e.spread_value.toFixed(2)}</td>
        <td style="color:${clr(e.unrealized_pnl)};font-weight:700">${fmt$(e.unrealized_pnl)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    dv.innerHTML = html;
    if (h.length) {
      _drawBtChart(h, p);
      _drawBtStockChart(h, p);
    }
  } catch(e) { dv.innerHTML = '<div style="color:var(--red)">Error: '+e.message+'</div>'; }
}

function _drawBtStockChart(history, position) {
  const c = document.getElementById('btStockChart');
  if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 40;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0,0,W,H);
  // Build series (include open price as Day 0)
  const dates = ['open', ...history.map(h => h.date.slice(5))];
  const prices = [position.stock_price_at_open, ...history.map(h => h.stock_price)];
  const longK = position.long_strike, shortK = position.short_strike;
  // Y-axis range: include price extremes + both strikes for context
  const allY = [...prices, longK, shortK];
  const vmax = Math.max(...allY) * 1.005;
  const vmin = Math.min(...allY) * 0.995;
  const sx = i => pad + (i / Math.max(1, prices.length-1)) * (W - 2*pad);
  const sy = v => H - pad - (v - vmin) / (vmax - vmin) * (H - 2*pad);
  // Strike reference lines
  ctx.strokeStyle = 'rgba(63,185,80,0.4)'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad, sy(longK)); ctx.lineTo(W-pad, sy(longK)); ctx.stroke();
  ctx.fillStyle = 'rgba(63,185,80,0.6)'; ctx.font = '10px system-ui';
  ctx.fillText('long ' + longK, W-pad-50, sy(longK) - 3);
  ctx.strokeStyle = 'rgba(248,81,73,0.4)';
  ctx.beginPath(); ctx.moveTo(pad, sy(shortK)); ctx.lineTo(W-pad, sy(shortK)); ctx.stroke();
  ctx.fillStyle = 'rgba(248,81,73,0.6)';
  ctx.fillText('short ' + shortK, W-pad-50, sy(shortK) - 3);
  ctx.setLineDash([]);
  // Open price reference line
  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1; ctx.setLineDash([2,3]);
  ctx.beginPath(); ctx.moveTo(pad, sy(position.stock_price_at_open)); ctx.lineTo(W-pad, sy(position.stock_price_at_open)); ctx.stroke();
  ctx.setLineDash([]);
  // Price curve
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2; ctx.beginPath();
  prices.forEach((v, i) => { const x=sx(i), y=sy(v); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.stroke();
  // Dots — color by direction relative to entry
  prices.forEach((v, i) => {
    const dirGood = (position.direction === 'BULL' && v >= position.stock_price_at_open) ||
                     (position.direction === 'BEAR' && v <= position.stock_price_at_open);
    ctx.fillStyle = i === 0 ? '#8b949e' : (dirGood ? '#3fb950' : '#f85149');
    ctx.beginPath(); ctx.arc(sx(i), sy(v), 3.5, 0, Math.PI*2); ctx.fill();
  });
  // Y-axis labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui';
  ctx.fillText('$' + vmax.toFixed(2), 2, 12);
  ctx.fillText('$' + vmin.toFixed(2), 2, H-4);
  // X-axis date labels (sparse)
  dates.forEach((d, i) => { if (i % Math.max(1, Math.floor(dates.length/8)) === 0) ctx.fillText(d, sx(i)-12, H-20); });
}

function exitBtDetail() {
  document.getElementById('bt-detail-view').style.display = 'none';
  document.getElementById('bt-view').style.display = '';
  const si = document.getElementById('btSearchInput');
  if (si) si.value = '';
  btFilterRows('');
}

function _drawBtChart(history, position) {
  const c = document.getElementById('btChart');
  if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 40;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0,0,W,H);
  const vals = history.map(h => h.unrealized_pnl);
  const dates = history.map(h => h.date.slice(5));
  const vmax = Math.max(position.max_profit, ...vals, 50);
  const vmin = Math.min(position.max_loss, ...vals, -50);
  const sx = i => pad + (i / Math.max(1, vals.length-1)) * (W - 2*pad);
  const sy = v => H - pad - (v - vmin) / (vmax - vmin) * (H - 2*pad);
  // Zero line
  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, sy(0)); ctx.lineTo(W-pad, sy(0)); ctx.stroke();
  // Max profit + loss lines
  ctx.strokeStyle = 'rgba(63,185,80,0.3)'; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad, sy(position.max_profit)); ctx.lineTo(W-pad, sy(position.max_profit)); ctx.stroke();
  ctx.strokeStyle = 'rgba(248,81,73,0.3)';
  ctx.beginPath(); ctx.moveTo(pad, sy(position.max_loss)); ctx.lineTo(W-pad, sy(position.max_loss)); ctx.stroke();
  ctx.setLineDash([]);
  // P/L curve
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2; ctx.beginPath();
  vals.forEach((v, i) => { const x=sx(i), y=sy(v); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.stroke();
  // Dots
  vals.forEach((v, i) => {
    ctx.fillStyle = v >= 0 ? '#3fb950' : '#f85149';
    ctx.beginPath(); ctx.arc(sx(i), sy(v), 3.5, 0, Math.PI*2); ctx.fill();
  });
  // Labels
  ctx.fillStyle = '#8b949e'; ctx.font = '10px system-ui';
  ctx.fillText('$' + vmax.toFixed(0), 2, 12);
  ctx.fillText('$0', 2, sy(0)+3);
  ctx.fillText('$' + vmin.toFixed(0), 2, H-4);
  dates.forEach((d, i) => { if (i % Math.max(1, Math.floor(dates.length/8)) === 0) ctx.fillText(d, sx(i)-12, H-20); });
}

async function runScan() {
  document.getElementById('scrSearchInput').value = '';
  // Wipe any prior data so nothing accidentally re-renders mid-scan
  scrData = null;
  screenerLoaded = false;
  scrEarnings = {};
  scrEarningsLoaded = false;
  const btn = document.getElementById('runScanBtn');
  const st = document.getElementById('screenerStatus');
  const lv = document.getElementById('scr-list-view');
  btn.disabled = true; btn.textContent = 'Scanning...';
  st.textContent = '';
  document.getElementById('scr-filter-bar').style.display = 'none';
  document.getElementById('screenerDate').textContent = '';
  lv.innerHTML = '<div class="scr-progress-wrap">' +
    '<div class="scr-progress-pct" id="scrPct">0%</div>' +
    '<div class="scr-progress-bar"><div class="scr-progress-fill" id="scrFill" style="width:0%"></div></div>' +
    '<div class="scr-progress-text" id="scrMsg">Starting scanner...</div>' +
    '<div class="scr-progress-text" id="scrTime" style="margin-top:6px"></div>' +
    '</div>';
  document.getElementById('scr-detail-view').style.display = 'none';
  lv.style.display = 'block';
  try {
    await fetch('/api/screener/run', {method:'POST'});
    const poll = setInterval(async () => {
      try {
        const r = await fetch('/api/screener/status');
        const d = await r.json();
        const fill = document.getElementById('scrFill');
        const pctEl = document.getElementById('scrPct');
        const msg = document.getElementById('scrMsg');
        if (fill && d.pct !== undefined) {
          fill.style.width = d.pct + '%';
          if (pctEl) pctEl.textContent = d.pct + '%';
        }
        if (msg && d.progress) msg.textContent = d.progress;
        const timeEl = document.getElementById('scrTime');
        if (timeEl && d.elapsed !== undefined) {
          const fmtT = (s) => { const m = Math.floor(s/60); const sec = s%60; return m > 0 ? m+'m '+sec+'s' : sec+'s'; };
          let t = fmtT(d.elapsed);
          if (d.eta > 0 && d.pct > 0 && d.pct < 100) t += ' \u00B7 ETA ' + fmtT(d.eta);
          timeEl.textContent = t;
        }
        if (!d.running) {
          clearInterval(poll);
          btn.disabled = false; btn.textContent = 'Run Scan';
          const dr = await fetch('/api/screener/data');
          scrData = await dr.json();
          screenerLoaded = true;
          if (scrData.scan_date) document.getElementById('screenerDate').textContent = 'Scan: ' + scrData.scan_date;
          if (scrData.signals && scrData.signals.length > 0) {
            initSigFilter();
            scrEarnings = {}; scrEarningsLoaded = false;
            renderSignalsTable();
            fetchScreenerEarnings(scrData.signals.map(s => s.ticker));
          } else {
            lv.innerHTML = '<div style="padding:40px;text-align:center;color:#8b949e">No qualified signals found.</div>';
          }
        }
      } catch(e) {}
    }, 1500);
  } catch(e) { btn.disabled = false; btn.textContent = 'Run Scan'; st.textContent = 'Error'; }
}

function injectTVWidget(containerId, src, config) {
  const el = document.getElementById(containerId);
  el.innerHTML = '<div class="tradingview-widget-container" style="height:100%;width:100%">' +
    '<div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div></div>';
  const s = document.createElement('script');
  s.type = 'text/javascript'; s.src = src; s.async = true;
  s.textContent = JSON.stringify(config);
  el.querySelector('.tradingview-widget-container').appendChild(s);
}

function getPositionTickers() {
  const tickers = new Set();
  if (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) {
    for (const strat of RAW_DATA.strategies) {
      if (strat.underlying) tickers.add(strat.underlying);
    }
  }
  if (typeof RAW_DATA !== 'undefined' && RAW_DATA.equities) {
    for (const eq of RAW_DATA.equities) {
      if (eq.symbol) tickers.add(eq.symbol);
    }
  }
  return [...tickers].sort();
}

function loadJournal() {
  const jf = document.getElementById('journalFrame');
  // ?embedded=1 tells dashboard.html to hide its internal navbar so only
  // the outer #tj-sub-nav drives navigation.
  jf.src = '/journal?embedded=1';
  jf.onload = () => {
    const on = document.body.classList.contains('privacy-on');
    if (on && jf.contentWindow) jf.contentWindow.postMessage({type:'privacy', on: true}, '*');
  };
  journalLoaded = true;
}

async function refreshJournal() {
  const btn = document.getElementById('refreshJournalBtn');
  const st = document.getElementById('journalStatus');
  btn.disabled = true; btn.textContent = 'Fetching...';
  st.textContent = 'Downloading trades from TastyTrade...';
  try {
    const r = await fetch('/api/journal/refresh', {method:'POST'});
    const d = await r.json();
    if (d.status === 'ok') {
      st.textContent = 'Done!';
      // Cache-bust the iframe so today's newly-added trades actually show
      document.getElementById('journalFrame').src = '/journal?embedded=1&t=' + Date.now();
    } else {
      st.textContent = 'Error: ' + (d.detail || '');
    }
  } catch(e) { st.textContent = 'Error: ' + e.message; }
  btn.disabled = false; btn.textContent = 'Refresh Trades';
  setTimeout(() => { st.textContent = ''; }, 3000);
}
</script>
"""
    html = html.replace("<!-- __TAB_END__ -->", tab_end_html)

    # --- history news ---
    html = html.replace("__HISTORY_NEWS_HTML__", HISTORY_NEWS_HTML)
    # --- /history news ---

    # --- heatmap ---
    html = html.replace("__HEATMAP_HTML__", HEATMAP_HTML)
    # --- /heatmap ---

    # --- sec filings ---
    html = html.replace("__SEC_PANE_HTML__", SEC_PANE_HTML)
    html = html.replace("__ME_PANE_HTML__", ME_PANE_HTML)
    html = html.replace("__SIGNALS_PANE_HTML__", SIGNALS_PANE_HTML)
    # --- /sec filings ---

    # Fix body to be flex column for tab layout
    html = html.replace("<body>", "<body style='display:flex;flex-direction:column;'>")

    return HTMLResponse(content=html)


@app.get("/api/benzinga/latest")
async def api_benzinga_latest():
    """Return latest Benzinga chat snapshot from data/benzinga/.

    File naming: YYYY-MM-DD.json (written by benzinga/fetcher.py via cron).
    """
    import glob, json as _json, os as _os
    base = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "benzinga")
    files = sorted(glob.glob(_os.path.join(base, "*.json")))
    if not files:
        return {"empty": True, "message": "No Benzinga data yet — fetcher hasn't run."}
    latest = files[-1]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            data = _json.load(f)
        data["_file"] = _os.path.basename(latest)
        data["_history"] = [_os.path.basename(f) for f in files[-10:]]
        return data
    except Exception as e:
        return {"empty": True, "error": str(e), "file": latest}


def _load_store(module_dir: str, alias: str):
    """Load `<module_dir>/store.py` under a unique sys.modules alias.

    Two different modules in this repo are named `store.py`
    (benzinga/store.py, news_radar/store.py). Using bare `import store`
    after putting different dirs on sys.path returns whichever was
    imported FIRST due to Python's module cache — so the second caller
    silently gets the wrong module. We bypass the cache by loading via
    importlib.util with explicit, distinct aliases.
    """
    import importlib.util as _ilu, os as _os, sys as _sys
    if alias in _sys.modules:
        return _sys.modules[alias]
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    path = _os.path.join(repo_root, module_dir, "store.py")
    spec = _ilu.spec_from_file_location(alias, path)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    _sys.modules[alias] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _benzinga_store():
    return _load_store("benzinga", "benzinga_store")


@app.get("/api/benzinga/dates")
async def api_benzinga_dates():
    """List all dates present in history.parquet."""
    try:
        return {"dates": _benzinga_store().read_dates()}
    except Exception as e:
        return {"dates": [], "error": str(e)}


@app.get("/api/benzinga/latest-per-channel")
async def api_benzinga_latest_per_channel():
    """Latest row per channel. Chris (daily) and OFR (weekly) live on
    different cadences, so a single-date fallback can drop one channel. The
    Benzinga 'Today' tab uses this to always show the freshest content for
    each channel without duplicating dates.
    """
    try:
        channels = _benzinga_store().read_latest_per_channel()
        if not channels:
            return {"error": "no data", "channels": {}}
        return {"channels": channels}
    except Exception as e:
        return {"error": str(e), "channels": {}}


@app.get("/api/benzinga/by-date/{date}")
async def api_benzinga_by_date(date: str):
    """Return one date's content from history.parquet. `date` is YYYY-MM-DD."""
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"error": "invalid date format, want YYYY-MM-DD"}
    try:
        channels = _benzinga_store().read_by_date(date)
        if not channels:
            return {"error": "not found", "date": date}
        return {"date": date, "channels": channels}
    except Exception as e:
        return {"error": str(e), "date": date}


@app.get("/api/benzinga/session")
async def api_benzinga_session():
    """登录 cookie 到期状况(给 pre-market 顶部横幅用)。只返回时间, 不含任何 cookie 值。

    needs_relogin 四条判据(C/D 2026-08-10 补, 同源于早上的通知盲点):
      A) 文件里 expires 已到
      B) 文件里 expires 剩 ≤3 天(临期预警)
      C) fetcher 连续 exit 2 达阈值(服务端提前 revoke, streak-based, PWA 重启会清零)
      D) 盘中(ET 工作日 ≥9:00)今日 snapshot 文件缺失 —— 最强证据, 不依赖 streak 累积,
         PWA 刚重启也能立刻判定。cookie 好的话文件一定在。
    """
    st = _benzinga_session_status()
    dl = st.get("days_left")
    server_revoked = _BZ_EXIT2_STREAK.get("n", 0) >= _BZ_EXIT2_NOTIFY_AT
    # 判据 D: 盘中今日 snapshot 缺失
    from zoneinfo import ZoneInfo as _Z
    from pathlib import Path as _P
    snapshot_missing = False
    try:
        now_ny = datetime.datetime.now(_Z("America/New_York"))
        if now_ny.weekday() < 5 and now_ny.time() >= datetime.time(9, 0):
            snap = _P(__file__).resolve().parent.parent / "data" / "benzinga" / (now_ny.strftime("%Y-%m-%d") + ".json")
            snapshot_missing = not snap.exists()
    except Exception:
        pass
    return {
        "found": st.get("found", False),
        "expired": st.get("expired", False),
        "days_left": (round(dl, 2) if dl is not None else None),
        "expires_at": st.get("expires_at"),
        "server_revoked": server_revoked,
        "snapshot_missing": snapshot_missing,
        "needs_relogin": bool(st.get("expired") or (dl is not None and dl <= 3)
                              or server_revoked or snapshot_missing),
    }


@app.post("/api/benzinga/relogin")
async def api_benzinga_relogin():
    """开一个带可见浏览器的续期流程 —— 作者自己在窗口里登录, 脚本存 cookie。
    ⚠️ 服务端全程不接触账号密码。"""
    # 作者主动重登 = 清 Cloudflare 冷却期 + streak, 让手动 refresh 端点能立刻跑 fetcher
    global _BZ_PAUSED_UNTIL
    _BZ_PAUSED_UNTIL = 0.0
    _BZ_EXIT2_STREAK["n"] = 0
    osa = (f'tell application "Terminal" to do script "'
           f'{_benzinga_relogin_cmd().replace(chr(92), chr(92)*2).replace(chr(34), chr(92) + chr(34))}"\n'
           f'tell application "Terminal" to activate')
    try:
        await asyncio.create_subprocess_exec("osascript", "-e", osa)
        return {"started": True}
    except Exception as e:
        return {"started": False, "error": str(e)}


@app.post("/api/benzinga/refresh")
async def api_benzinga_refresh():
    """Trigger fetcher manually. Runs in background — caller polls /api/benzinga/latest."""
    import asyncio as _asyncio, os as _os, sys as _sys
    repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    script = _os.path.join(repo_root, "benzinga", "fetcher.py")

    async def _run():
        try:
            proc = await _asyncio.create_subprocess_exec(
                _sys.executable, script,
                stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
                cwd=repo_root,
            )
            await proc.communicate()
        except Exception as e:
            print("[benzinga refresh] error:", e)

    _asyncio.create_task(_run())
    return {"started": True}


@app.post("/api/benzinga/translate")
async def api_benzinga_translate(req: Request):
    """Pre-market message body / PDF 段落自动翻译。payload {texts:[str,...]} → {cn:[str,...]},
    与输入等长。走通用 translate_lines (google/bing/argos 轮换 + gibberish 过滤)。

    ⚠️长段处理:google/bing/argos 都拒收超过 ~1000-2000 字的 chunk (payload 上限 /
    seq2seq attention 窗口), 直接送整段会全家 fail → 保底返原文, 前端把它当没翻。
    解决:单条 > CHUNK_LIMIT 就按句子边界(英文 .!? / 中文 。！? / 换行)拆多个 chunk
    批量翻译, 再拼回同一条中文。硬上限 8000 字防超长 PDF 段拖崩。"""
    body = await req.json()
    texts = (body or {}).get("texts") or []
    if not isinstance(texts, list) or not texts:
        return {"cn": []}
    from benzinga_mcp.digest import translate_lines
    import re
    CHUNK_LIMIT = 800
    HARD_LIMIT = 8000
    all_chunks: list[str] = []
    layout: list[tuple[int, int]] = []   # 每条 (start, count) in all_chunks
    for t in texts:
        s = str(t or "")[:HARD_LIMIT]
        if len(s) <= CHUNK_LIMIT:
            layout.append((len(all_chunks), 1))
            all_chunks.append(s)
            continue
        parts = re.split(r'(?<=[.!?。！？])\s+|\n+', s)
        chunks: list[str] = []
        buf = ""
        for p in parts:
            if not p:
                continue
            if buf and len(buf) + len(p) + 1 > CHUNK_LIMIT:
                chunks.append(buf); buf = p
            else:
                buf = (buf + " " + p) if buf else p
        if buf:
            chunks.append(buf)
        if not chunks:
            chunks = [s[:CHUNK_LIMIT]]
        layout.append((len(all_chunks), len(chunks)))
        all_chunks.extend(chunks)
    loop = asyncio.get_event_loop()
    cn_all = await loop.run_in_executor(None, translate_lines, all_chunks)
    out: list[str] = []
    for start, cnt in layout:
        out.append(" ".join(cn_all[start:start + cnt]).strip())
    return {"cn": out}


# ─── News Radar (Benzinga-MCP-fed parquet store) ───────────────────────────

def _news_store():
    return _load_store("news_radar", "news_radar_store")


@app.get("/api/news/summary")
async def api_news_summary(days: int = 7, sentiment: str = "signaled",
                            watchlist: bool = True):
    """Heatmap data: per-ticker mention counts + sentiment breakdown.

    days: lookback window (1..30+)
    sentiment: 'signaled' | 'bullish' | 'bearish' | 'any'
    watchlist: if True (default), restrict to tickers in
               data/news_radar/watchlist.txt; if no file, treats as off.
    """
    try:
        s = _news_store()
        tickers = s.summary(days=days, sentiment=sentiment, watchlist_only=watchlist)
        # Also report distinct news count in window
        df = s.read_window(days)
        if not df.empty:
            if sentiment == "signaled":
                df = df[df["sentiment"] != 0]
            elif sentiment == "bullish":
                df = df[df["sentiment"] == 1]
            elif sentiment == "bearish":
                df = df[df["sentiment"] == -1]
            if watchlist:
                wl = s.load_watchlist()
                if wl:
                    df = df[df["ticker"].isin(wl)]
            total = int(df["news_id"].nunique()) if not df.empty else 0
        else:
            total = 0
        wl_size = len(s.load_watchlist())
        # Surface the freshness of the underlying parquet so the UI can show
        # "Latest: 5m ago". Uses the filtered df so the timestamp matches
        # what's actually visible on the heatmap.
        latest_news_at = None
        if not df.empty and "created_at" in df.columns:
            try:
                latest_news_at = str(df["created_at"].max())
            except Exception:
                latest_news_at = None
        return {"tickers": tickers, "total_news": total, "days": days,
                "sentiment": sentiment, "watchlist": watchlist, "watchlist_size": wl_size,
                "latest_news_at": latest_news_at}
    except Exception as e:
        return {"error": str(e), "tickers": [], "total_news": 0}


@app.get("/api/news/by-ticker/{ticker}")
async def api_news_by_ticker(ticker: str, days: int = 30, sentiment: str = "any"):
    """All headlines mentioning `ticker` in the window, newest first."""
    try:
        items = _news_store().by_ticker(ticker, days=days, sentiment=sentiment)
        return {"ticker": ticker.upper(), "days": days, "sentiment": sentiment, "items": items}
    except Exception as e:
        return {"error": str(e), "items": []}


@app.get("/api/news/recent-by-ticker")
async def api_news_recent_by_ticker(days: int = 2, max_per_ticker: int = 3,
                                    watchlist: bool = False):
    """Bulk per-ticker recent headlines — feeds the heatmap "News" column.

    Returns {tickers: {TICKER: [{title, sentiment, created_at, ...}, ...]}}
    so the heatmap can paint headlines next to every row in one round-trip.
    Default window = 2 days (yesterday + today). Set watchlist=true to
    restrict to the user's watchlist.
    """
    try:
        bucket = _news_store().recent_by_ticker(
            days=days, max_per_ticker=max_per_ticker, watchlist_only=watchlist,
        )
        return {"days": days, "max_per_ticker": max_per_ticker,
                "watchlist": watchlist, "tickers": bucket}
    except Exception as e:
        return {"error": str(e), "tickers": {}}


# ── Finviz news bulk fetch (heatmap "Source: Finviz" toggle) ───────────────
# Rate-limit protection: per-process per-ticker cache with 1h TTL so repeat
# calls don't hammer Finviz and trigger 404 / IP block. Callers are expected
# to pre-filter to high-movement tickers (>3% change) on the frontend.

_FINVIZ_NEWS_CACHE: dict = {}     # ticker -> {"ts": float, "items": [...]}
_FINVIZ_CACHE_TTL = 60 * 60       # 1h
_FINVIZ_MAX_PER_CALL = 30         # max tickers per /api/news/finviz-bulk call


def _finviz_news_for(ticker: str, hours: int, max_per_ticker: int) -> list[dict]:
    """Synchronous helper — uses the `finviz` package's get_news() with a
    process-level cache. Designed to be called inside run_in_executor."""
    import time as _t
    t = ticker.upper().strip()
    now = _t.time()
    cached = _FINVIZ_NEWS_CACHE.get(t)
    if cached and now - cached["ts"] < _FINVIZ_CACHE_TTL:
        items = cached["items"]
    else:
        try:
            import finviz
            raw = finviz.get_news(t) or []
        except Exception:
            raw = []
        items = []
        for row in raw:
            # finviz returns (date_str, title, url, source) tuples
            try:
                date_s, title, url, source = row[0], row[1], row[2], row[3] if len(row) > 3 else ""
            except Exception:
                continue
            items.append({
                "title": title or "",
                "url": url or "",
                "created_at": date_s or "",
                "source": source or "Finviz",
                "sentiment": 0,   # finviz doesn't tag sentiment; UI shows grey dot
            })
        _FINVIZ_NEWS_CACHE[t] = {"ts": now, "items": items}
    # Filter to the time window (best-effort — finviz date format is messy)
    now_dt = datetime.datetime.now()
    cutoff = now_dt - datetime.timedelta(hours=hours)
    filtered = []
    for it in items:
        parsed = None
        ds = it.get("created_at", "")
        for fmt in ("%b-%d-%y %I:%M%p", "%b-%d-%y %H:%M", "%I:%M%p", "%H:%M"):
            try:
                parsed = datetime.datetime.strptime(ds, fmt)
                if fmt in ("%I:%M%p", "%H:%M"):
                    # Time-only entries are "today" by Finviz convention — stamp with
                    # TODAY's date (not cutoff's, which is `now - hours` = yesterday when
                    # hours>=24, wrongly aging today's HH:MM<now items past the cutoff).
                    parsed = parsed.replace(year=now_dt.year, month=now_dt.month,
                                            day=now_dt.day)
                break
            except Exception:
                continue
        if parsed is None or parsed >= cutoff:
            # Keep when we can't parse — better to over-show than drop news
            filtered.append(it)
        if len(filtered) >= max_per_ticker:
            break
    return filtered


# ── Finviz NEW stock page (AI「why is the stock moving」+ classic news) ──────
# 新版 finviz.com/stock?t=… 把两样东西都服务端渲染进同一页:
#   1) AI「为何异动」摘要 —— <script id="why-stock-moving-init-data-0"> 干净 JSON,
#      自带原生 sentiment(good/bad/neutral) + summary 要点 + catalyst 标记。
#      页面上的「More +」只是把 summary 展开,内容早在 JSON 里,无需点/无需浏览器。
#   2) 经典新闻表 —— fullview-news-outer,带链接/来源,比 finviz 包的 quote.ashx 还全。
# 一次 GET 拿全部 → 取代 mover news 原先单独的 quote.ashx 抓取,请求数不增加。
# ⚠️rate-limit:finviz 实测约 75-100 次/快速突发就被挡;后台以 ~2 req/s 慢速预抓全部异动票,
# 作者开页只读缓存(allow_fetch=False, 绝不打网络),抓取全交给后台循环。浏览器 UA 必带。
_FINVIZ_PAGE_CACHE: dict = {}      # ticker -> {"ts": float, "ai": {...}|None, "news": [...]}
_FINVIZ_PAGE_TTL = 120 * 60        # 2h(后台慢速刷新;TTL 略大于刷新间隔→作者读恒暖)

# UA / cookie / header 轮换防 rate-limit/封禁:一个 requests.Session 池,每个 session 固定一套
# 真实浏览器 UA + 匹配 sec-ch-ua + 自己的 cookie jar(首次请求拿到 finviz cookie, 后续带上 →
# 像"回访作者")。逐请求轮换 session → 多套指纹+多个 cookie 会话,叠加 ~1 req/s 慢速,极难被封。
_FINVIZ_UA_POOL = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36",
     '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', '"macOS"'),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/123.0.0.0 Safari/537.36",
     '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', '"Windows"'),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.4 Safari/605.1.15", None, None),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0", None, None),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
     '"Microsoft Edge";v="123", "Not:A-Brand";v="8", "Chromium";v="123"', '"macOS"'),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36",
     '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', '"Linux"'),
]
_FINVIZ_SESSIONS = None
_FINVIZ_RR = [0]                   # round-robin 计数


def _finviz_headers(ua: str, sec_ch_ua, platform) -> dict:
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finviz.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }
    if sec_ch_ua:
        h["sec-ch-ua"] = sec_ch_ua
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = platform or '"macOS"'
    return h


def _finviz_session_pool():
    """惰性建 requests.Session 池(每 session 一套 UA+headers+独立 cookie jar)。
    requests 不可用 → 返回 [] → _finviz_fetch_html 回退 urllib。"""
    global _FINVIZ_SESSIONS
    if _FINVIZ_SESSIONS is not None:
        return _FINVIZ_SESSIONS
    try:
        import requests
        pool = []
        for ua, sch, plat in _FINVIZ_UA_POOL:
            s = requests.Session()
            s.headers.update(_finviz_headers(ua, sch, plat))
            pool.append(s)
        _FINVIZ_SESSIONS = pool
    except Exception:
        _FINVIZ_SESSIONS = []
    return _FINVIZ_SESSIONS


def _finviz_fetch_html(url: str, timeout: int = 15) -> str:
    """轮换 session(UA/cookie 各异)抓 HTML。requests 自动处理 gzip+cookie;回退 urllib。"""
    pool = _finviz_session_pool()
    if pool:
        i = _FINVIZ_RR[0] % len(pool)
        _FINVIZ_RR[0] = (_FINVIZ_RR[0] + 1) % 1_000_000
        r = pool[i].get(url, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}")
        return r.text
    # 回退:urllib + 轮换 UA + gzip 解压
    import urllib.request as _u
    import gzip as _gz
    ua, sch, plat = _FINVIZ_UA_POOL[_FINVIZ_RR[0] % len(_FINVIZ_UA_POOL)]
    _FINVIZ_RR[0] = (_FINVIZ_RR[0] + 1) % 1_000_000
    hdrs = _finviz_headers(ua, sch, plat)
    hdrs["Accept-Encoding"] = "gzip, deflate"
    resp = _u.urlopen(_u.Request(url, headers=hdrs), timeout=timeout)
    raw = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = _gz.decompress(raw)
    return raw.decode("utf-8", "ignore")


def _finviz_parse_whymoving(html: str) -> dict | None:
    """解析服务端渲染的 AI「why is the stock moving」JSON hydration 块。"""
    import re as _re
    import json as _json
    m = _re.search(r'<script id="why-stock-moving-init-data-0"[^>]*>(.*?)</script>',
                   html, _re.S)
    if not m:
        return None
    try:
        wm = (_json.loads(m.group(1)) or {}).get("whyMoving")
    except Exception:
        return None
    if not wm or not (wm.get("headline") or "").strip():
        return None
    return wm


def _finviz_parse_news_table(html: str) -> list[dict]:
    """解析新版页面内嵌的经典新闻表(date / title / url / source),newest-first。"""
    import re as _re
    import html as _htmllib
    i = html.find("fullview-news-outer")
    if i < 0:
        return []
    seg = html[i:i + 80000]
    rows = _re.findall(
        r'<tr[^>]*>\s*<td[^>]*>\s*(.*?)\s*</td>\s*<td[^>]*>.*?'
        r'<a class="tab-link-news" href="([^"]+)"[^>]*>\s*(.*?)\s*</a>.*?'
        r'<span>\((.*?)\)</span>', seg, _re.S)
    out = []
    for date_s, url, title, source in rows:
        title = _htmllib.unescape(_re.sub(r"<[^>]+>", "", title)).strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": url.strip(),
            "created_at": _re.sub(r"\s+", " ", date_s).strip(),
            "source": _htmllib.unescape(source).strip() or "Finviz",
            "sentiment": 0,
        })
    return out


def _finviz_stockpage(ticker: str, force: bool = False,
                      allow_fetch: bool = True) -> dict:
    """一次 GET 新版 stock 页面 → {'ai': {...}|None, 'news': [...]}。进程级缓存。
    · force=True:无视 TTL 强制重抓(后台预构建用)。
    · allow_fetch=False:绝不打网络——有缓存(哪怕过期)就返回, 没有就空。作者开页走这条,
      finviz 抓取全交给后台慢速循环, 避免开页触发突发被 rate-limit。"""
    import time as _t
    t = ticker.upper().strip()
    now = _t.time()
    cached = _FINVIZ_PAGE_CACHE.get(t)
    if cached and not force and now - cached["ts"] < _FINVIZ_PAGE_TTL:
        return {"ai": cached["ai"], "news": cached["news"]}
    if not allow_fetch:                     # 不许打网络:回退旧缓存(过期也用), 否则空
        return {"ai": cached["ai"], "news": cached["news"]} if cached \
            else {"ai": None, "news": []}
    ai, news = None, []
    try:
        html = _finviz_fetch_html(f"https://finviz.com/stock?t={t}&p=d")  # 轮换 UA/cookie
        ai = _finviz_parse_whymoving(html)
        news = _finviz_parse_news_table(html)
    except Exception:
        ai, news = None, []
    _FINVIZ_PAGE_CACHE[t] = {"ts": now, "ai": ai, "news": news}
    return {"ai": ai, "news": news}


# ── Yahoo Finance news bulk fetch (heatmap "Source: Yahoo" toggle) ─────────
# Coverage > Benzinga free firehose for large caps. Free, no API key,
# no aggressive rate limit at moderate volume. Cached 1h per ticker.

_YF_NEWS_CACHE: dict = {}
_YF_CACHE_TTL = 60 * 60        # 1h
_YF_MAX_PER_CALL = 60          # higher than finviz; yf is friendlier


def _yf_news_for(ticker: str, hours: int, max_per_ticker: int) -> list[dict]:
    """Synchronous per-ticker fetch — uses yfinance.Ticker(t).news with a
    1h cache. Designed to be called inside run_in_executor."""
    import time as _t
    t = ticker.upper().strip()
    now = _t.time()
    cached = _YF_NEWS_CACHE.get(t)
    if cached and now - cached["ts"] < _YF_CACHE_TTL:
        items = cached["items"]
    else:
        items = []
        try:
            import yfinance as yf
            raw = yf.Ticker(t).news or []
        except Exception:
            raw = []
        for row in raw:
            c = row.get("content") if isinstance(row, dict) else None
            if not c:
                # Legacy flat shape (older yfinance versions)
                c = row if isinstance(row, dict) else None
            if not c:
                continue
            title = c.get("title") or ""
            if not title:
                continue
            url = ""
            cu = c.get("clickThroughUrl") or c.get("canonicalUrl") or {}
            if isinstance(cu, dict):
                url = cu.get("url") or ""
            elif isinstance(cu, str):
                url = cu
            published = c.get("pubDate") or c.get("displayTime") or ""
            provider = ""
            p = c.get("provider") or {}
            if isinstance(p, dict):
                provider = p.get("displayName") or ""
            items.append({
                "title": title,
                "url": url,
                "created_at": published,
                "source": provider or "Yahoo",
                "sentiment": 0,   # yfinance doesn't tag sentiment
            })
        _YF_NEWS_CACHE[t] = {"ts": now, "items": items}

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    out = []
    for it in items:
        ds = it.get("created_at") or ""
        keep = True
        if ds:
            try:
                # pubDate is ISO with trailing Z
                dt = datetime.datetime.fromisoformat(ds.replace("Z", "+00:00"))
                keep = dt >= cutoff
            except Exception:
                keep = True
        if keep:
            out.append(it)
        if len(out) >= max_per_ticker:
            break
    return out


@app.get("/api/news/yahoo-bulk")
async def api_news_yahoo_bulk(tickers: str = "", hours: int = 24,
                              max_per_ticker: int = 3):
    """Bulk Yahoo Finance news for a comma-separated list of tickers.

    Far broader large-cap coverage than the local Benzinga parquet —
    use this for the heatmap when the Benzinga column is mostly empty.
    Each ticker is process-cached for 1h.
    """
    raw = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    seen: set = set()
    universe: list[str] = []
    for t in raw:
        if t in seen:
            continue
        seen.add(t)
        universe.append(t)
        if len(universe) >= _YF_MAX_PER_CALL:
            break
    loop = asyncio.get_event_loop()

    def _gather():
        out: dict = {}
        for t in universe:
            try:
                items = _yf_news_for(t, hours=hours, max_per_ticker=max_per_ticker)
                if items:
                    out[t] = items
            except Exception:
                pass
        return out

    try:
        bucket = await loop.run_in_executor(None, _gather)
    except Exception as e:
        return {"error": str(e), "tickers": {}, "requested": len(universe)}
    return {
        "requested": len(universe),
        "fetched": len(bucket),
        "hours": hours,
        "max_per_ticker": max_per_ticker,
        "capped_at": _YF_MAX_PER_CALL,
        "tickers": bucket,
    }


@app.get("/api/news/finviz-bulk")
async def api_news_finviz_bulk(tickers: str = "", hours: int = 24,
                               max_per_ticker: int = 3):
    """Bulk Finviz news for a comma-separated list of tickers.

    Designed for the heatmap: caller pre-filters to high-mover tickers
    (|% change| ≥ 3) so we never hit Finviz for thousands of symbols.
    Each ticker is cached process-locally for 1h.
    """
    raw_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    # Dedup while preserving order; cap at _FINVIZ_MAX_PER_CALL
    seen: set = set()
    universe: list[str] = []
    for t in raw_list:
        if t in seen:
            continue
        seen.add(t)
        universe.append(t)
        if len(universe) >= _FINVIZ_MAX_PER_CALL:
            break

    loop = asyncio.get_event_loop()
    bucket: dict = {}

    def _gather():
        out = {}
        for i, t in enumerate(universe):
            try:
                items = _finviz_news_for(t, hours=hours, max_per_ticker=max_per_ticker)
                if items:
                    out[t] = items
            except Exception:
                pass
            # Tiny inter-call delay only when we actually hit the network
            cached = _FINVIZ_NEWS_CACHE.get(t)
            if cached and time.time() - cached["ts"] < 0.2:
                time.sleep(0.3)
        return out

    try:
        bucket = await loop.run_in_executor(None, _gather)
    except Exception as e:
        return {"error": str(e), "tickers": {}, "requested": len(universe)}
    return {
        "requested": len(universe),
        "fetched": len(bucket),
        "hours": hours,
        "max_per_ticker": max_per_ticker,
        "capped_at": _FINVIZ_MAX_PER_CALL,
        "tickers": bucket,
    }


@app.get("/api/news/stats")
async def api_news_stats():
    """Diagnostic: row counts, parquet size, date range."""
    try:
        return _news_store().stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/data")
async def api_data():
    """Return current strategies JSON."""
    return {
        "generated_at": datetime.datetime.now().isoformat(),
        "strategies": state.strategies,
        "equities": state.equities,
        "equity_prev_close": state.equity_prev_close,
        "balance": state.balance,
    }


# ── 组合级希腊面板用:每个标的 vs SPY 的 beta(慢变 → 缓存 1 天)+ 实时 SPY 现价。
#    持仓本身始终实时拉取(别处,从不缓存);这里只缓存慢变的 beta 查询。
_PF_BETA_CACHE = {"ts": 0.0, "betas": {}}


def _get_portfolio_betas(syms) -> dict:
    """{TICKER: β vs 大盘}(TastyTrade market-metrics,1天缓存,缺则 1.0)。
    供 portfolio greeks 面板 + hedge 引擎(每票 β 加权)共用。"""
    import time as _time
    syms = sorted({str(s).strip().upper() for s in (syms or []) if str(s).strip()})
    if not syms:
        return {}
    now = _time.time()
    cache = _PF_BETA_CACHE["betas"]
    stale = (now - _PF_BETA_CACHE["ts"]) > 86400
    to_fetch = syms if stale else [s for s in syms if s not in cache]
    if to_fetch:
        try:
            from ticker_metrics import fetch_ticker_metrics
            m = fetch_ticker_metrics(to_fetch)
            for s in to_fetch:
                cache[s] = (m.get(s) or {}).get("beta")
            if stale:
                _PF_BETA_CACHE["ts"] = now
        except Exception as e:
            print(f"  [WARN] _get_portfolio_betas: {e}")
    out = {}
    for s in syms:
        b = cache.get(s)
        # 夹到 ±3:小盘票回归 β 噪声大(如 AAOI 6.7),裸用会把对冲量放大到失真
        out[s] = round(max(-3.0, min(3.0, float(b))), 3) if b is not None else 1.0
    return out


@app.get("/api/portfolio/betas")
def api_portfolio_betas(symbols: str = ""):
    """Beta-vs-SPY per underlying (TastyTrade market-metrics, 1-day cache) + live
    SPY spot. Powers the beta-weighted portfolio Greeks block in the risk view."""
    import time as _time
    syms = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    # 实时 SPY 现价(用于折算 $SPY 当量股数)
    spy = None
    try:
        if state.client is not None:
            spy = fetch_stock_marks(state.client, ["SPY"]).get("SPY")
    except Exception:
        pass
    # betas:整张表过期(>1天)或有未知标的时刷新
    now = _time.time()
    cache = _PF_BETA_CACHE["betas"]
    stale = (now - _PF_BETA_CACHE["ts"]) > 86400
    to_fetch = syms if stale else [s for s in syms if s not in cache]
    if to_fetch:
        try:
            from ticker_metrics import fetch_ticker_metrics
            m = fetch_ticker_metrics(to_fetch)
            for s in to_fetch:
                cache[s] = (m.get(s) or {}).get("beta")
            if stale:
                _PF_BETA_CACHE["ts"] = now
        except Exception as e:
            print(f"  [WARN] api_portfolio_betas: {e}")
    betas, missing = {}, []
    for s in syms:
        b = cache.get(s)
        if b is None:
            missing.append(s)
            betas[s] = 1.0          # 兜底:无 beta 按市场 1.0
        else:
            betas[s] = round(float(b), 3)
    return {"betas": betas, "spy": spy, "missing": missing}


@app.post("/api/refresh")
async def api_refresh():
    """Force reload positions from TastyTrade."""
    old_quotes = dict(state.live_quotes)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fetch_positions_and_strategies)
    state.live_quotes = old_quotes
    reapply_cached_quotes()
    # Notify all browser clients to reload
    if state.ws_clients:
        msg = json.dumps({"type": "full_refresh"})
        dead = []
        for ws in state.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            state.ws_clients.remove(d)
    # Near-immediate credit-spread mistake check: positions just reloaded, so
    # scan now rather than waiting for the 30s loop tick.
    try:
        await _scan_credit_spreads()
    except Exception as e:
        print(f"[CreditSpread] refresh-hook error: {e}")
    return {"status": "ok", "strategies": len(state.strategies)}


# ── Finviz endpoint ──

@app.get("/api/finviz/{ticker}")
async def get_finviz_data(ticker: str):
    """Return Finviz fundamental data, analyst ratings, description, news."""
    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            import finviz
            t = ticker.upper()
            result = {"fundamentals": {}, "analysts": [], "description": "", "news": []}

            try:
                result["fundamentals"] = finviz.get_stock(t)
            except Exception:
                pass

            try:
                targets = finviz.get_analyst_price_targets(t)
                result["analysts"] = [dict(x) for x in targets]
            except Exception:
                pass

            try:
                raw_news = finviz.get_news(t)
                result["news"] = [
                    {"date": n[0], "title": n[1], "url": n[2], "source": n[3]}
                    for n in raw_news
                ]
            except Exception:
                pass

            try:
                import requests as _req
                from bs4 import BeautifulSoup
                url = f"https://finviz.com/quote.ashx?t={t}&p=d"
                headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
                resp = _req.get(url, headers=headers, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                desc_el = soup.find(class_="fullview-profile")
                if desc_el:
                    result["description"] = desc_el.get_text(" ", strip=True)
            except Exception:
                pass

            return result

        data = await loop.run_in_executor(None, _fetch)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Options Flow endpoint ──

@app.get("/api/options-flow/{ticker}")
async def get_options_flow(ticker: str):
    """Fetch option chain volume/OI from TastyTrade + barchart sentiment."""
    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            import datetime as _dt
            import requests as _req
            from bs4 import BeautifulSoup

            t = ticker.upper()
            result = {
                "ticker": t,
                "expirations": [],       # [{exp, dte, strikes: [{strike, call_vol, put_vol, call_oi, put_oi}]}]
                "sentiment": {},         # {net_sentiment, delta_imbalance}
                "put_call_ratios": [],   # [{exp, dte, vol_ratio, oi_ratio}]
            }

            # ── 1. TastyTrade option chain ──
            try:
                if state.client is None:
                    state.client = TastyClient()
                chain_data = state.client.get_option_chain(t)
                top_items = chain_data.get("data", {}).get("items", [])
                all_exps = []
                for chain in top_items:
                    all_exps.extend(chain.get("expirations", []))
                if not all_exps:
                    all_exps = chain_data.get("data", {}).get("expirations", [])
                print(f"  [options-flow] {t}: {len(all_exps)} expirations found")

                today = _dt.date.today()
                # Find this month and next month monthly expirations
                monthly_exps = []
                seen_exps = set()
                for item in sorted(all_exps, key=lambda x: x.get("expiration-date", "")):
                    exp_str = item.get("expiration-date") or item.get("expirationDate", "")
                    exp_type = item.get("expiration-type", "")
                    if exp_str in seen_exps:
                        continue
                    try:
                        exp_date = _dt.date.fromisoformat(exp_str)
                    except ValueError:
                        continue
                    dte = (exp_date - today).days
                    if dte < 0:
                        continue
                    # Monthly = "Regular" type
                    is_monthly = exp_type == "Regular"
                    if is_monthly and dte <= 70:
                        seen_exps.add(exp_str)
                        monthly_exps.append({"exp_str": exp_str, "dte": dte, "strikes": item.get("strikes", [])})
                    if len(monthly_exps) >= 2:
                        break

                # Build rows per expiration, then batch-fetch quotes
                for mexp in monthly_exps:
                    rows = []
                    for strike in mexp["strikes"]:
                        sp = float(strike.get("strike-price", 0))
                        call_sym = strike.get("call", "")
                        put_sym = strike.get("put", "")
                        rows.append({"strike": sp, "call_sym": call_sym, "put_sym": put_sym,
                                     "call_vol": 0, "put_vol": 0, "call_oi": 0, "put_oi": 0})

                    # Batch fetch quotes
                    all_syms = []
                    for r in rows:
                        if r["call_sym"]: all_syms.append(r["call_sym"])
                        if r["put_sym"]: all_syms.append(r["put_sym"])

                    quote_map = {}
                    BATCH = 100
                    for i in range(0, len(all_syms), BATCH):
                        batch = all_syms[i:i+BATCH]
                        try:
                            resp = state.client.get_option_quotes(batch)
                            for qi in resp.get("data", {}).get("items", []):
                                quote_map[qi.get("symbol", "")] = qi
                        except Exception:
                            pass

                    def _int(v):
                        try: return int(float(v))
                        except (TypeError, ValueError): return 0

                    for r in rows:
                        cq = quote_map.get(r["call_sym"], {})
                        pq = quote_map.get(r["put_sym"], {})
                        r["call_vol"] = _int(cq.get("volume") or cq.get("day-volume") or 0)
                        r["put_vol"] = _int(pq.get("volume") or pq.get("day-volume") or 0)
                        r["call_oi"] = _int(cq.get("open-interest") or cq.get("openInterest") or 0)
                        r["put_oi"] = _int(pq.get("open-interest") or pq.get("openInterest") or 0)

                    # Filter to strikes with any activity
                    active = [r for r in rows if r["call_vol"] + r["put_vol"] + r["call_oi"] + r["put_oi"] > 0]
                    result["expirations"].append({
                        "exp": mexp["exp_str"], "dte": mexp["dte"],
                        "strikes": [{"strike": r["strike"], "call_vol": r["call_vol"], "put_vol": r["put_vol"],
                                     "call_oi": r["call_oi"], "put_oi": r["put_oi"]} for r in active],
                    })

                    # Compute put/call ratios
                    total_call_vol = sum(r["call_vol"] for r in rows)
                    total_put_vol = sum(r["put_vol"] for r in rows)
                    total_call_oi = sum(r["call_oi"] for r in rows)
                    total_put_oi = sum(r["put_oi"] for r in rows)
                    result["put_call_ratios"].append({
                        "exp": mexp["exp_str"], "dte": mexp["dte"],
                        "call_vol": total_call_vol, "put_vol": total_put_vol,
                        "vol_ratio": round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else 0,
                        "call_oi": total_call_oi, "put_oi": total_put_oi,
                        "oi_ratio": round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0,
                    })
            except Exception as e:
                print(f"  [options-flow] TastyTrade error: {e}")

            # ── 2. Barchart sentiment scrape ──
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
                url = f"https://www.barchart.com/stocks/quotes/{t}/options-flow"
                resp = _req.get(url, headers=headers, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                # Look for Net Trade Sentiment and Delta Imbalance in page text
                text = soup.get_text(" ", strip=True)
                import re
                m = re.search(r"Net Trade Sentiment\s*[:\-]?\s*([\-\+]?\d+\.?\d*%?)", text)
                if m:
                    result["sentiment"]["net_sentiment"] = m.group(1)
                m = re.search(r"Delta Imbalance\s*[:\-]?\s*([\-\+]?\d+[\.,]?\d*)", text)
                if m:
                    result["sentiment"]["delta_imbalance"] = m.group(1)
            except Exception as e:
                print(f"  [options-flow] Barchart scrape error: {e}")

            return result

        data = await loop.run_in_executor(None, _fetch)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Market Pulse endpoint ──

_market_pulse_cache = {"data": None, "ts": 0, "fg_ts": 0, "fg_data": None}

@app.get("/api/market-pulse")
async def market_pulse():
    """Return VIX/SPY/QQQ/IWM prices + CNN Fear & Greed. Indices refresh every call, F&G cached 1h."""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _fetch_market_pulse)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _fetch_market_pulse():
    import requests as _req

    result = {"indices": [], "fear_greed": None}

    # ── 1. DXLink live quotes for SPY, QQQ, IWM + TastyTrade for VIX ──
    for sym in ["SPY", "QQQ", "IWM"]:
        price = state.quotes.get(sym)
        prev = state.equity_prev_close.get(sym)
        if price and price > 0:
            chg_pct = round(((price / prev) - 1) * 100, 2) if prev and prev > 0 else 0
            result["indices"].append({"symbol": sym, "price": round(price, 2), "change_pct": chg_pct})
        else:
            result["indices"].append({"symbol": sym, "price": None, "change_pct": None})

    # VIX — try DXLink live_quotes first, then yfinance fallback
    vix_price = None
    for vix_key in (":VIX", "VIX", "$VIX.X"):
        lq = state.live_quotes.get(vix_key, {})
        if lq.get("last_trade") and lq["last_trade"] > 0:
            vix_price = lq["last_trade"]
            break
        mid = lq.get("bid", 0) + lq.get("ask", 0)
        if mid > 0:
            vix_price = round(mid / 2, 2) if lq.get("bid") and lq.get("ask") else None
            break
    if vix_price is None and HAS_YF:
        try:
            info = yf.Ticker("^VIX").fast_info
            vix_price = round(info.last_price, 2) if info.last_price else None
        except Exception:
            pass
    result["indices"].insert(0, {"symbol": "VIX", "price": round(vix_price, 2) if vix_price else None, "change_pct": None})

    # ── 2. CNN Fear & Greed Index (cached 1 hour) ──
    now = time.time()
    if _market_pulse_cache["fg_data"] and (now - _market_pulse_cache["fg_ts"]) < 3600:
        result["fear_greed"] = _market_pulse_cache["fg_data"]
    else:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Referer": "https://www.cnn.com/markets/fear-and-greed",
            }
            resp = _req.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                            headers=headers, timeout=10)
            fg_json = resp.json()
            score_data = fg_json.get("fear_and_greed", {})
            score = round(float(score_data.get("score", 0)))
            rating = score_data.get("rating", "")
            fg_result = {"score": score, "label": rating}
            _market_pulse_cache["fg_data"] = fg_result
            _market_pulse_cache["fg_ts"] = now
            result["fear_greed"] = fg_result
        except Exception as e:
            print(f"  [market-pulse] CNN F&G error: {e}")
            if _market_pulse_cache["fg_data"]:
                result["fear_greed"] = _market_pulse_cache["fg_data"]

    return result


# ════════════════════════════════════════════════════════════════════
# 交易体检(开单前预警 + 持仓出场)—— checkup 引擎(纯函数)的薄壳
# 引擎: options_analyzer/checkup/  · 设计: docs/superpowers/specs/2026-06-13-trade-checkup-design.md
# ════════════════════════════════════════════════════════════════════
def _checkup_enrich_proposed(proposed: dict) -> None:
    """用实时期权链把拟开价差的腿填上 mid/bid/ask + spot/iv/dte/earnings_in_window。"""
    import datetime as _dt
    import paper_trade_api
    ticker = (proposed.get("ticker") or "").upper()
    exp = proposed.get("expiration")
    chain = paper_trade_api.fetch_chain_for_builder(
        state.client, ticker, exp,
        quotes_cache=state.quotes, metrics_cache=state.metrics_data)
    if proposed.get("spot") is None:
        proposed["spot"] = chain.get("price") or None
    if proposed.get("iv") is None:
        proposed["iv"] = chain.get("iv") or None
    strike_map = {round(float(r["strike"]), 4): r for r in chain.get("strikes", [])}
    for leg in proposed.get("legs", []):
        r = strike_map.get(round(float(leg.get("strike", 0)), 4))
        if not r:
            continue
        side = "call" if str(leg.get("type", "")).upper().startswith("C") else "put"
        q = r.get(side, {})
        if leg.get("mid") is None:
            leg["mid"] = q.get("mid") or None
        leg.setdefault("bid", q.get("bid"))
        leg.setdefault("ask", q.get("ask"))
    if exp and proposed.get("dte") is None:
        try:
            proposed["dte"] = (_dt.date.fromisoformat(exp) - _dt.date.today()).days
        except ValueError:
            pass
    ed = chain.get("earnings_date")
    if ed and exp:
        try:
            proposed["earnings_in_window"] = _dt.date.fromisoformat(ed) <= _dt.date.fromisoformat(exp)
        except ValueError:
            pass
    # 没给打算付的净价 → 默认理论中间价(滑点=0,只看 RR/OTM)
    if proposed.get("net_price") is None:
        s = sum((l.get("mid") or 0) * (l.get("qty") or 0) for l in proposed.get("legs", []))
        proposed["net_price"] = round(s, 2)


@app.post("/api/checkup/pretrade")
async def checkup_pretrade(request: Request):
    """开单前体检:联动度/集中度 + 错误镜子 + 价格合理性。

    body: {tickers: 'MAG7'|'AAPL MSFT'|[..], proposed_spread?: {ticker, direction,
           thesis_type, expiration, net_price?, legs:[{type,strike,qty}]}}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    tickers = body.get("tickers") or ""
    proposed = body.get("proposed_spread") or None
    book = [{"underlying": s.get("underlying"), "legs": s.get("legs") or []}
            for s in (state.strategies or []) if s.get("underlying")]
    nlv = _compute_live_nlv()  # 实时口径(cash+Σ持仓现值),取代滞后的 balances net-liq

    def _work():
        if proposed and proposed.get("ticker") and proposed.get("legs"):
            try:
                _checkup_enrich_proposed(proposed)
            except Exception as e:
                proposed["_enrich_error"] = str(e)
        from checkup import engine
        return engine.pretrade(tickers, proposed, book, nlv)

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _work)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


@app.get("/api/checkup/chain/{ticker}")
async def checkup_chain(ticker: str, expiration: str = Query(None)):
    """给开单前模态建表单用:无 expiration→到期列表;给了→该到期的 strikes(call/put mid)。"""
    import paper_trade_api
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: paper_trade_api.fetch_chain_for_builder(
            state.client, ticker, expiration,
            quotes_cache=state.quotes, metrics_cache=state.metrics_data))
        return data
    except Exception as e:
        return {"error": str(e), "ticker": ticker.upper(), "expirations": [], "strikes": []}


@app.get("/api/checkup/recommend/{ticker}")
async def checkup_recommend(ticker: str):
    """自动推荐 bull call spread:按验证开法(ATM/35-45DTE/10%宽)生成 5-8 候选,
    按 成交质量(bid-ask)+ 流动性(OI/vol)+ 赔率 + 规律契合 排序。零手工输入。"""
    import paper_trade_api

    def _work():
        meta = paper_trade_api.fetch_chain_for_builder(
            state.client, ticker, None,
            quotes_cache=state.quotes, metrics_cache=state.metrics_data)
        spot = meta.get("price") or 0
        earnings = meta.get("earnings_date") or None
        exps = meta.get("expirations") or []

        def _pick(lo, hi):
            c = [e for e in exps if lo <= e.get("dte", -1) <= hi]
            return sorted(c, key=lambda e: abs(e.get("dte", 99) - 40))[:2]

        chosen = _pick(35, 45) or _pick(28, 52) or _pick(21, 60)
        chains = []
        for e in chosen:
            full = paper_trade_api.fetch_chain_for_builder(
                state.client, ticker, e["date"],
                quotes_cache=state.quotes, metrics_cache=state.metrics_data)
            chains.append({"date": e["date"], "dte": e["dte"], "strikes": full.get("strikes", [])})
        from checkup import recommend as _rec, regime as _rg
        rg = _rg.market_regime()
        strike = rg.get("strike") or {}
        recs = _rec.recommend(spot, chains, earnings_date=earnings,
                              target_long_off=strike.get("long_off", 0.0))
        return {"ticker": ticker.upper(), "spot": spot, "earnings_date": earnings,
                "dtes": [e["dte"] for e in chosen], "recommendations": recs,
                "regime": rg.get("regime"), "vix": rg.get("vix"), "strike": strike}

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "ticker": ticker.upper(), "recommendations": []}


@app.get("/api/checkup/liquidity/{ticker}")
async def checkup_liquidity(ticker: str, max_dte: int = Query(50)):
    """流动性体检:逐行权价(ATM-10%~+15%)× 到期(≤max_dte 天)的 call/put 网格 +
    ticker 级裁决 + 同一价差跨周到期对比。数据=实时期权链 bid/ask/vol/oi;
    判断口径=checkup.liquidity(复用 strategy builder legWarnings 语义)。"""
    import paper_trade_api

    def _work():
        scan = paper_trade_api.fetch_liquidity_scan(
            state.client, ticker, max_dte=max_dte,
            quotes_cache=state.quotes, metrics_cache=state.metrics_data,
            market_hours=is_market_hours())
        from checkup import liquidity as _liq
        return _liq.build_scan(scan)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "ticker": ticker.upper(), "cols": [],
                "calls": None, "puts": None, "cross_week": None,
                "ticker_verdict": {"tier": "unknown", "label": "出错", "detail": str(e)}}


@app.get("/api/checkup/regime")
async def checkup_regime():
    """当前市场 regime(SPY vs 200SMA)→ 前端 regime 自适应止盈带(牛 70-80% / 震荡 50-60%)。
    日更缓存,fail-soft 返回中性。"""
    try:
        import sys as _s
        from pathlib import Path as _P
        _ck = str(_P(__file__).resolve().parent)
        if _ck not in _s.path:
            _s.path.insert(0, _ck)
        from checkup import regime as _rg
        r = _rg.market_regime()
        r["band"] = _rg.band(r.get("regime"))
        return r
    except Exception as e:
        return {"regime": None, "error": str(e),
                "band": {"near": 0.55, "tp": 0.65, "sweet": 70, "label": "中性", "zone": "65-70%"}}


@app.get("/api/checkup/profile")
async def checkup_profile():
    """你的高频错误 + 大账(开单前模态一打开就展示,不用先体检)。"""
    try:
        from checkup import rules as _ckrules
        mr = _ckrules.load_mistake_rules()
        bm = mr.get("by_mistake") or {}
        common = sorted(([{"label": k, **v} for k, v in bm.items()]),
                        key=lambda x: x.get("net_pnl", 0))[:8]
        return {"common_mistakes": common, "headline": mr.get("headline") or {},
                "generated_from": mr.get("generated_from") or {}}
    except Exception as e:
        return {"error": str(e), "common_mistakes": []}


_rr_recent_cache = {"data": None, "ts": 0.0}

def _rr_recent_activity(rr):
    """最近大跌统计(yfinance SPY/QQQ 近 6 月,缓存 1h)。fail-soft 返回 {}。"""
    try:
        if _rr_recent_cache["data"] and time.time() - _rr_recent_cache["ts"] < 3600:
            return _rr_recent_cache["data"]
        import yfinance as yf
        series = {}
        for t in ("SPY", "QQQ"):
            df = yf.download(t, period="6mo", interval="1d", auto_adjust=True, progress=False)
            if df is None or df.empty:
                series[t.lower()] = []; continue
            cl = df["Close"]
            if hasattr(cl, "columns"):        # MultiIndex 单票兜底
                cl = cl.iloc[:, 0]
            rets = cl.pct_change() * 100
            series[t.lower()] = [{"date": str(d.date())[:10], "ret": round(float(r), 2)}
                                 for d, r in rets.items() if r == r]
        data = rr.summarize_recent(series.get("spy", []), series.get("qqq", []))
        _rr_recent_cache.update({"data": data, "ts": time.time()})
        return data
    except Exception as ex:
        return {"error": str(ex)}


@app.get("/api/checkup/risk-radar")
async def checkup_risk_radar():
    """风险雷达:① regime 饱和度(今日 digest 高 lift 话题在不在烧)→ 0-100 风险分;
    ② landmine 日历(未来2-3周 FOMC/CPI/非农 + 持仓&龙头财报,贴历史 lift)。
    数据=research/drop_events 实证;给的是"高危窗口"提示,非预测。"""
    held = sorted({(s.get("underlying") or "").upper()
                   for s in (state.strategies or []) if s.get("underlying")})

    def _work(events):
        import sys as _sys
        from pathlib import Path as _P
        _ck = str(_P(__file__).resolve().parent)
        if _ck not in _sys.path:
            _sys.path.insert(0, _ck)
        from checkup import risk_radar as rr
        try:
            items = _dgs.load_day(_digest_today_str()) or []
        except Exception:
            items = []
        if not items:                       # 盘前/今日还没写 → 退回最近一天
            try:
                _days = _dgs.list_days()
                items = (_dgs.load_day(_days[0]) or []) if _days else []
            except Exception:
                items = []
        lm = rr.annotate_landmines(events, held)
        return {"gauge": rr.regime_saturation(items),
                "landmines": lm,
                "forward": rr.forward_score(lm, 30),
                "held": held,
                "recent": _rr_recent_activity(rr),
                "reference": rr.load_reference(),
                "wiim_causes": rr.load_wiim_causes_reference()}

    try:
        ev = await api_major_events(days=30)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _work(ev.get("events", [])))
        result["today"] = ev.get("today")
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e), "gauge": None, "landmines": []}


# ════════════════════════════════════════════════════════════════════
# 开单前体检(独立模块,与上面的 checkup/ 引擎不是一回事)—— pre_trade_checkup/
# 的 HTTP 薄壳。4 层摩擦闸门,CLI-first(python -m pre_trade_checkup.cli),
# 这里只加一层 HTTP 包装 + 一个独立页面,不改模块本身一个字。
# 引擎: pre_trade_checkup/engine.py · 页面: GET /pretrade-checkup
# ════════════════════════════════════════════════════════════════════
@app.get("/api/pre_trade_checkup")
async def api_pre_trade_checkup(ticker: str = Query(...), direction: str = Query(...),
                                 dte: int = Query(40), today: Optional[str] = Query(None)):
    """跑 4 层体检。返回引擎原始 dict(layers 的 1/2/3/4 键序列化成 JSON 后会变成
    字符串 "1".."4")+ 附加的 layer 名称/建议文案 —— 复用 cli.py 已有的展示层
    (_LAYER_TITLE / _suggestions),不在前端 JS 里重写一遍规则/阈值。"""
    def _work():
        from pre_trade_checkup import engine as _ptc_engine
        from pre_trade_checkup import cli as _ptc_cli
        result = _ptc_engine.run_checkup(ticker, direction, dte, today)
        for n, layer in result["layers"].items():
            layer["name"] = _ptc_cli._LAYER_TITLE.get(n, f"Layer {n}")
        result["suggestion"] = _ptc_cli._suggestions(result["layers"], result["failed_layers"])
        return result

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/pre_trade_checkup/override")
async def api_pre_trade_checkup_override(request: Request):
    """记录"我知道有 fail 但还是要开"的 override(理由 <20 字 → log_override 拒绝写入,
    不阻断真实下单——本来就只是提示工具)。body: {ticker, direction, dte, reason}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker = (body.get("ticker") or "").strip()
    direction = (body.get("direction") or "").strip().lower()
    dte = body.get("dte")
    reason = body.get("reason") or ""

    if not ticker or direction not in ("bull", "bear") or dte is None:
        return JSONResponse({"error": "ticker/direction/dte 缺失或非法"}, status_code=400)

    def _work():
        from pre_trade_checkup import engine as _ptc_engine
        return _ptc_engine.log_override(ticker=ticker, direction=direction, dte=int(dte), reason=reason)

    try:
        loop = asyncio.get_event_loop()
        entry = await loop.run_in_executor(None, _work)
        return {"ok": True, "entry": entry}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# The interface-language switch (中文 / 双语 / English) for standalone pages.
_ZEUS_SHELL = '<link rel="stylesheet" href="/static/zeus.css"><script src="/static/zeus.js"></script>'
_ZEUS_HEAD = '<head><script src="/static/zeus_lang.js"></script>'   # zt / ztt before the page's own scripts


@app.get("/pretrade-checkup", response_class=HTMLResponse)
async def pretrade_checkup_page():
    """开单前体检独立页面 —— 不进主 dashboard tab,直接开 URL 用
    (?ticker=&direction=&dte= 可预填 + 自动跑一次)。"""
    return HTMLResponse(content=PRETRADE_CHECKUP_HTML.replace("</body>", _ZEUS_SHELL + "</body>", 1).replace("<head>", _ZEUS_HEAD, 1))


# ════════════════════════════════════════════════════════════════════
# stock_selector/ 双策略选股 —— 端点保留、独立于 UI。前端消费方是 heatmap 🚀AI选股
# 弹窗里的「⭐ 精选选股」tab(见 heatmap/heatmap.py _hmPkPanelHtml)。原「🧭 市场判断」
# tab 下的 mbSub('picker') / #mbPickerPane sub-tab 已删(2026-07-30, 与 AI选股合并,
# 避免两套选股入口并存), 引擎和端点本身不受影响。
# 与 stock_picker/(12 层单一漏斗, CLI-first, 独立页面已删)是两个并存的独立引擎,
# 见 stock_selector/__init__.py 头注释。策略A趋势跟踪 + 策略B抄底/回踩支撑,各自
# 纯函数过滤 + 打分排序 + 选strike,合并去重后一次性调 Sonnet 新闻/宏观复核。
# 引擎: stock_selector/engine.py · 端点: GET /api/stock_selector
# ════════════════════════════════════════════════════════════════════
def _stock_selector_json_safe(obj):
    """NaN / pd.Timestamp 递归清洗, 确保吐出去的是真合法 JSON。逻辑就是
    stock_selector.engine.json_safe(engine.run() 内部已经清洗过一次, 这里是端点层
    再兜底一次, 防的是以后有人往 result 里加了没走 json_safe 的字段)。"""
    from stock_selector import engine as _ss_engine
    return _ss_engine.json_safe(obj)


_ss_cache: dict = {}   # {(today_iso, no_sonnet): result_dict} — 内存 cache (进程内)
_ss_cache_lock = threading.Lock()
_ss_cache_dir = PROJECT_ROOT / "data" / "stock_selector_cache"
_ss_cache_dir.mkdir(parents=True, exist_ok=True)


def _ss_cache_path(today_key: str, no_sonnet: bool):
    suffix = "_nosonnet" if no_sonnet else ""
    return _ss_cache_dir / f"{today_key}{suffix}.json"


@app.get("/api/stock_selector")
async def api_stock_selector(today: Optional[str] = Query(None),
                              no_sonnet: bool = Query(False),
                              refresh: bool = Query(False)):
    """跑一次双策略选股引擎(stock_selector/engine.py). 当日结果缓存, 同一天
    再打开直接复用 (基本面/技术指标一天不会大变, Sonnet 复核 1-2 分钟没必要
    每次跑). refresh=true 强制重跑 (弹窗内「🔄 重跑」按钮走这条).

    两级 cache: 内存 dict → 磁盘 JSON. PWA 重启后从磁盘 restore, 不会丢当日结果.
    换日自动失效: cache_key = (today, no_sonnet), today 换了 key 就变.
    """
    import datetime as _dt
    today_key = today or _dt.date.today().isoformat()
    cache_key = (today_key, bool(no_sonnet))

    if not refresh:
        with _ss_cache_lock:
            cached = _ss_cache.get(cache_key)
        if cached is None:
            # 内存 miss → 查磁盘 (PWA 重启后 restore)
            p = _ss_cache_path(today_key, bool(no_sonnet))
            if p.exists():
                try:
                    cached = json.loads(p.read_text(encoding="utf-8"))
                    with _ss_cache_lock:
                        _ss_cache[cache_key] = cached
                except Exception as _e:
                    print(f"[stock_selector] disk cache read fail: {_e}")
        if cached is not None:
            return cached

    def _work():
        from stock_selector import engine as _ss_engine
        # 优先用 TastyTrade REST 实时链取 strike(60s cache);SSD 兜底
        return _ss_engine.run(today=today, no_sonnet=no_sonnet, tt_client=state.client)

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _work)
        safe = _stock_selector_json_safe(result)
        with _ss_cache_lock:
            _ss_cache[cache_key] = safe
        # 落磁盘 (下次 PWA 重启后 restore, 不用重跑 Sonnet)
        try:
            _ss_cache_path(today_key, bool(no_sonnet)).write_text(
                json.dumps(safe, ensure_ascii=False), encoding="utf-8")
        except Exception as _e:
            print(f"[stock_selector] disk cache write fail: {_e}")
        return safe
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════════
# Journal Review —— 「5 分钟开盘前复习」独立页面。叠在现有 Trade Journal 之上
# (6+ 个 sub-tab 太杂,复习不动),不改 Journal 一个字。CLI-first 风格延续:
# 引擎 journal_review/engine.py 本身可单独 `python -m journal_review.test_harness`
# 自测,这里只加一层 HTTP 包装 + 一个独立页面。
# 引擎: journal_review/engine.py · 页面: GET /journal-review
# ════════════════════════════════════════════════════════════════════
@app.get("/api/journal_review")
async def api_journal_review(today: Optional[str] = Query(None)):
    """跑一次开盘前复习引擎(journal_review/engine.py)。引擎本身做文件 IO
    (读 entries_open/close.jsonl、daily_ohlc.parquet、option_prices 快照等,
    外加对 pre_trade_checkup 的多次回溯调用),丢进线程池不阻塞事件循环。"""
    def _work():
        from journal_review import engine as _jr_engine
        return _jr_engine.build_review(today)

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/journal-review", response_class=HTMLResponse)
async def journal_review_page():
    """Journal Review 独立页面 —— 不进 Trade Journal 的 tab 结构,直接开 URL 用。"""
    return HTMLResponse(content=JOURNAL_REVIEW_HTML.replace("</body>", _ZEUS_SHELL + "</body>", 1).replace("<head>", _ZEUS_HEAD, 1))


# ── Live prices (老 /api/screener/data 的替代, 数据源=heatmap live_prices) ──

@app.get("/api/live_prices")
async def api_live_prices():
    """700 票 universe 的最新价 + 今日涨跌.

    数据源=heatmap.heatmap_api.live_prices() (DXLink 实时流的公共快照).
    返回结构兼容老 /api/screener/data 的 `signals` 列表, 使 Digest 里
    `_dgAiPriceOf` / Positions AI `_posAiPriceOf` 无痛切换.
    """
    try:
        from heatmap import heatmap_api as _hm
        prices = _hm.live_prices() or {}
    except Exception as e:
        return {"signals": [], "error": str(e)}
    signals = [
        {"ticker": tk, "price": q.get("last") or 0,
         "change_pct": q.get("change_pct")}
        for tk, q in prices.items()
        if (q.get("last") or 0) > 0
    ]
    return {"signals": signals, "source": "heatmap_live_prices"}


# ── Missing take-profit GTC watcher ────────────────────────────────────────

@app.get("/api/missing_gtc")
async def api_missing_gtc():
    """当前开仓中没有对应 closing GTC 的 (ticker, exp) 列表。

    数据由后台 `missing_gtc_loop` 每 60s 刷新;前端持仓 tab 和体检 tab 都读这
    个端点画红徽章/黄横幅。空数组 = 全部都挂好了 ✅。
    """
    return {"items": _MISSING_GTC_STATE.get("list") or [],
            "expiring_today": _MISSING_GTC_STATE.get("expiring_today") or [],
            "at": _MISSING_GTC_STATE.get("at")}


# ── Backtest endpoints ─────────────────────────────────────────────────────

def _backtest_module():
    import importlib, sys as _sys
    _dir = str(PROJECT_ROOT / "options_analyzer")
    if _dir not in _sys.path:
        _sys.path.insert(0, _dir)
    return importlib.import_module("backtest_runner")


@app.get("/api/backtest/positions")
async def backtest_positions(mode: str = "a",
                              sl_pct: Optional[float] = None,
                              tp_pct: Optional[float] = None):
    """Return all backtest positions + per-mode effective P/L.

    mode=a: stop-loss 50% AND take-profit 120% (default, defensive)
    mode=b: take-profit 120% only (no stop-loss, let losers run)
    mode=c: no rules — hold every position to expiration
    mode=custom: use sl_pct (e.g. 30 = -30% of debit) and tp_pct (e.g. 80 =
                  +80% of debit). Either may be None to disable that side.
    """
    br = _backtest_module()
    positions = br.load_positions()
    pnl = br.load_pnl()
    # Group P/L history by position_id
    history_by_id: dict = {}
    for e in pnl:
        history_by_id.setdefault(e.get("position_id", ""), []).append(e)
    out = []
    for p in positions:
        hist = history_by_id.get(p["id"], [])
        proj = br.project_position_mode(p, hist, mode, sl_pct, tp_pct)
        # Use mode-projected status + P/L
        p["status"] = proj["effective_status"]
        p["close_date"] = proj["effective_close_date"] or p.get("close_date", "")
        if p["status"] == "open":
            p["current_pnl"] = proj["effective_pnl"]
            p["realized_pnl"] = 0.0
        else:
            p["current_pnl"] = 0.0
            p["realized_pnl"] = proj["effective_pnl"]
        # Latest stock price (regardless of mode)
        last = hist[-1] if hist else {}
        p["current_stock_price"] = last.get("stock_price", p.get("stock_price_at_open", 0))
        p["last_pnl_date"] = last.get("date", "") if hist else ""
        out.append(p)
    out.sort(key=lambda x: x.get("open_date", ""), reverse=True)
    return {"positions": out, "mode": mode}


@app.get("/api/backtest/ticker/{ticker}")
async def backtest_ticker_history(ticker: str):
    """All positions (open + closed) for a specific ticker, with latest P/L per."""
    br = _backtest_module()
    ticker_u = ticker.upper()
    positions = [p for p in br.load_positions() if p.get("ticker", "").upper() == ticker_u]
    # Get latest pnl per position
    pnl_all = br.load_pnl()
    latest = {}
    for e in pnl_all:
        pid = e.get("position_id", "")
        if pid not in latest or e.get("date", "") > latest[pid].get("date", ""):
            latest[pid] = e
    for p in positions:
        last = latest.get(p["id"])
        p["current_pnl"] = last.get("unrealized_pnl", 0.0) if last else (p.get("realized_pnl") or 0.0)
        p["current_stock_price"] = last.get("stock_price", p.get("stock_price_at_open", 0)) if last else p.get("stock_price_at_open", 0)
        p["last_pnl_date"] = last.get("date", "") if last else ""
    # Sort by open_date desc
    positions.sort(key=lambda x: x.get("open_date", ""), reverse=True)
    # Aggregate stats
    open_ct = sum(1 for p in positions if p.get("status") == "open")
    closed = [p for p in positions if p.get("status") != "open"]
    wins = sum(1 for p in closed if p.get("realized_pnl", 0) > 0)
    total_realized = sum(p.get("realized_pnl", 0.0) for p in closed)
    total_unrealized = sum(p.get("current_pnl", 0.0) for p in positions if p.get("status") == "open")
    return {
        "ticker": ticker_u,
        "positions": positions,
        "summary": {
            "total": len(positions),
            "open": open_ct,
            "closed": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
            "total_realized": round(total_realized, 2),
            "total_unrealized": round(total_unrealized, 2),
        },
    }


@app.get("/api/backtest/position/{pos_id}")
async def backtest_position_detail(pos_id: str):
    """Full position detail + full daily P/L history."""
    br = _backtest_module()
    positions = {p["id"]: p for p in br.load_positions()}
    if pos_id not in positions:
        return JSONResponse({"error": "not found"}, status_code=404)
    pos = positions[pos_id]
    hist = [e for e in br.load_pnl() if e.get("position_id") == pos_id]
    hist.sort(key=lambda e: e.get("date", ""))
    return {"position": pos, "history": hist}


# Phantom-position guard for $ aggregation: a near-zero net_debit is a bad EOD mark
# that the contract-sizer blows up into thousands of contracts (max_profit → millions).
# Exclude those from every dollar-level aggregate. Kept consistent with backtest_runner sizing.
_BT_MIN_DEBIT = 0.05        # per-share debit; <$5/contract spread is physically impossible
_BT_MAX_CONTRACTS = 2000


@app.get("/api/backtest/summary")
async def backtest_summary(mode: str = "a",
                            sl_pct: Optional[float] = None,
                            tp_pct: Optional[float] = None):
    """Aggregate stats for the backtest portfolio under selected mode."""
    br = _backtest_module()
    raw_positions = br.load_positions()
    pnl = br.load_pnl()
    history_by_id: dict = {}
    for e in pnl:
        history_by_id.setdefault(e.get("position_id", ""), []).append(e)
    # Apply mode projection in place
    positions = []
    for p in raw_positions:
        # Skip phantom positions (bad EOD mark → absurd contract count) so they never
        # pollute realized/unrealized/max_profit/max_loss/counts below.
        if (float(p.get("net_debit") or 0) < _BT_MIN_DEBIT
                or int(p.get("contracts") or 1) > _BT_MAX_CONTRACTS):
            continue
        proj = br.project_position_mode(p, history_by_id.get(p["id"], []),
                                          mode, sl_pct, tp_pct)
        q = dict(p)
        q["status"] = proj["effective_status"]
        if proj["effective_status"] == "open":
            q["current_pnl"] = proj["effective_pnl"]
            q["realized_pnl"] = 0.0
        else:
            q["current_pnl"] = 0.0
            q["realized_pnl"] = proj["effective_pnl"]
        positions.append(q)
    open_ct = sum(1 for p in positions if p.get("status") == "open")
    closed = [p for p in positions if p.get("status") != "open"]
    total_unrealized = sum(p.get("current_pnl", 0.0) for p in positions if p.get("status") == "open")
    total_realized = sum(p.get("realized_pnl", 0.0) for p in closed)
    wins = sum(1 for p in closed if p.get("realized_pnl", 0) > 0)
    losses = sum(1 for p in closed if p.get("realized_pnl", 0) < 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0.0
    max_profit_total = sum(p.get("max_profit", 0) for p in positions if p.get("status") == "open")
    max_loss_total = sum(p.get("max_loss", 0) for p in positions if p.get("status") == "open")
    # Close reason breakdown
    reasons = {"take_profit_120pct": 0, "stop_loss_50pct": 0,
               "expired_win": 0, "expired_loss": 0, "closed": 0}
    for p in closed:
        s = p.get("status", "")
        if s in reasons:
            reasons[s] += 1
    return {
        "open_count": open_ct,
        "closed_count": len(closed),
        "total_count": len(positions),
        "total_unrealized_pnl": round(total_unrealized, 2),
        "total_realized_pnl": round(total_realized, 2),
        "win_rate_closed": win_rate,
        "max_profit_if_all_hit": round(max_profit_total, 2),
        "max_loss_if_all_hit": round(max_loss_total, 2),
        "close_reasons": reasons,
    }


@app.post("/api/backtest/run")
async def backtest_run():
    """Trigger manual backtest open-scan (using the latest screener data)."""
    if state.backtest_running:
        return {"status": "already_running"}
    state.backtest_running = True
    state.backtest_progress = "Starting..."
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_backtest_open_sync)
    return {"status": "started"}


@app.post("/api/backtest/pnl")
async def backtest_pnl_update():
    """Trigger manual daily P/L update for all open positions."""
    if state.backtest_running:
        return {"status": "already_running"}
    state.backtest_running = True
    state.backtest_progress = "P/L update..."
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_backtest_pnl_sync)
    return {"status": "started"}


@app.post("/api/backtest/revalidate")
async def backtest_revalidate():
    """Drop open positions that no longer meet signal-count thresholds
    (BULL ≥3 signals, BEAR ≥1). Useful when rules tighten."""
    br = _backtest_module()
    kept, dropped = br.revalidate_past_positions()
    return {"status": "ok", "kept": kept, "dropped": dropped}


@app.post("/api/backtest/resize")
async def backtest_resize():
    """Resize legacy 1-contract positions to TARGET_RISK_USD ($1000) risk.
    Idempotent — already-sized positions are skipped."""
    br = _backtest_module()
    n = br.resize_past_positions()
    return {"status": "ok", "resized": n, "target_risk_usd": br.TARGET_RISK_USD}


@app.post("/api/backtest/snapshot")
async def backtest_snapshot_now():
    """Manually trigger a full universe (264-ticker) chain snapshot
    and inline P/L update. Same task that runs auto at 15:45 weekdays."""
    if state.backtest_running:
        return {"status": "already_running"}
    state.backtest_running = True
    state.backtest_progress = "Snapshot starting..."
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_backtest_snapshot_sync)
    return {"status": "started"}


@app.get("/api/backtest/status")
async def backtest_status():
    return {
        "running": state.backtest_running,
        "progress": state.backtest_progress,
    }


def _run_backtest_open_sync():
    try:
        br = _backtest_module()
        # Build signals list from current screener_data (merged SSP1 + Barchart)
        signals = state.screener_data.get("signals", []) if state.screener_data else []
        if not signals:
            # Fallback: ssp1_latest.json
            ssp1_path = PROJECT_ROOT / "winrate_scanner" / "data" / "ssp1_latest.json"
            if ssp1_path.exists():
                raw = json.loads(ssp1_path.read_text())
                for t in raw.get("tickers", []):
                    dirs = {s["direction"] for s in t["signals"]}
                    direction = "BULL" if dirs == {"BULL"} else "BEAR" if dirs == {"BEAR"} else "MIXED"
                    signals.append({
                        "ticker": t["ticker"], "direction": direction,
                        "signals": [{"name": f"{s['signal']} ({s['direction']})",
                                      "best_rate": s["rate"], "samples": s["samples"]}
                                     for s in t["signals"]],
                        "sources": [], "price": t.get("last_close", 0),
                    })

        def _progress(msg):
            state.backtest_progress = msg

        opened = br.run_daily_scan(signals, report_fn=_progress)
        state.backtest_progress = f"Done: opened {len(opened)} positions"
        print(f"[Backtest] Opened {len(opened)} positions")
    except Exception as e:
        import traceback; traceback.print_exc()
        state.backtest_progress = f"Error: {e}"
    finally:
        state.backtest_running = False


def _run_backtest_pnl_sync():
    try:
        br = _backtest_module()
        def _progress(msg):
            state.backtest_progress = msg
        entries = br.run_daily_pnl(report_fn=_progress)
        state.backtest_progress = f"Done: {len(entries)} P/L updates"
        print(f"[Backtest] Updated {len(entries)} P/L entries")
    except Exception as e:
        import traceback; traceback.print_exc()
        state.backtest_progress = f"Error: {e}"
    finally:
        state.backtest_running = False


async def positions_ai_scheduler():
    """交易日 CT 9:30 + 14:30 自动跑 AI 持仓新闻分析 → 完成后弹 macOS 通知。
    作者机器在 America/Chicago, datetime.now() 即 CT。"""
    import shutil
    SLOTS = [(9, 30), (14, 30)]

    tn = shutil.which("terminal-notifier")
    if not tn:
        for cand in ("/opt/homebrew/bin/terminal-notifier",
                     "/usr/local/bin/terminal-notifier"):
            if os.path.exists(cand):
                tn = cand
                break

    await asyncio.sleep(90)  # let server settle
    print("[posAI] scheduler armed: CT 09:30 / 14:30 (交易日)")
    while True:
        try:
            now = datetime.datetime.now()
            # find next slot today or tomorrow
            target = None
            for h, m in SLOTS:
                t = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now < t:
                    target = t
                    break
            if target is None:
                # all slots passed today → first slot tomorrow
                tomorrow = now + datetime.timedelta(days=1)
                target = tomorrow.replace(hour=SLOTS[0][0], minute=SLOTS[0][1],
                                          second=0, microsecond=0)
            # skip weekends
            while target.weekday() >= 5:
                target += datetime.timedelta(days=1)

            wait_s = (target - now).total_seconds()
            print(f"[posAI] next run: {target:%Y-%m-%d %H:%M} CT ({wait_s/3600:.1f}h)")
            await asyncio.sleep(wait_s)

            # run the analysis (reuse same logic as POST endpoint)
            strats = list(state.strategies) if state.strategies else []
            if not strats:
                print("[posAI] 无持仓, 跳过")
                await asyncio.sleep(60)
                continue

            global _posai_job
            if _posai_job.get("status") == "running":
                print("[posAI] 已在运行中, 等 60s")
                await asyncio.sleep(60)
                continue

            _posai_job = {"status": "running", "ts": __import__("time").time()}
            loop = asyncio.get_event_loop()

            def _run_scheduled():
                global _posai_job
                try:
                    from positions_ai import analyze_positions
                    result = analyze_positions(strats)
                    _posai_job = {"status": "done", "result": result}
                    return result
                except Exception as e:
                    _posai_job = {"status": "error",
                                 "result": {"error": str(e)[:300]}}
                    return None

            result = await loop.run_in_executor(None, _run_scheduled)

            # send notification
            if result and result.get("items"):
                items = result["items"]
                ok = [i for i in items if not i.get("error")]
                tp = sum(1 for i in ok if i.get("action") == "take_profit")
                wr = sum(1 for i in ok if i.get("action") == "watch_risk")
                hold = sum(1 for i in ok if i.get("action") == "hold")
                slot_label = f"{target:%H:%M}"
                title = f"\U0001f916 AI 持仓分析 · {slot_label}"
                body_parts = []
                if tp:
                    body_parts.append(f"\U0001f535 止盈 {tp}")
                if wr:
                    body_parts.append(f"\U0001f7e1 风险 {wr}")
                if hold:
                    body_parts.append(f"\U0001f7e2 持有 {hold}")
                body = " · ".join(body_parts) if body_parts else f"{len(ok)} 仓位已分析"
                if not _notifications_muted():
                    PORT = int(os.environ.get("PORT", "8765"))
                    url = f"http://localhost:{PORT}/?posai=1"
                    await _fire_heatmap_notification(tn, title, body, url)
                print(f"[posAI] done {slot_label}: tp={tp} wr={wr} hold={hold}")
            else:
                print(f"[posAI] 分析完成但无结果")

            await asyncio.sleep(60)  # avoid re-triggering same slot
        except Exception as e:
            print(f"[posAI] scheduler error: {e}")
            await asyncio.sleep(300)


async def backtest_scheduler():
    """Weekdays at 15:45 local: snapshot full 264-ticker universe (chain
    Parquet) + compute P/L for any open positions inline, then open new
    positions from today's screener signals.

    Not started by default (see the startup block): it needs SSP1 signals,
    which are not shipped, and the backtest view has no entry point yet."""
    await asyncio.sleep(60)  # let server settle
    while True:
        try:
            now = datetime.datetime.now()
            target = now.replace(hour=15, minute=45, second=0, microsecond=0)
            if now >= target:
                target = target + datetime.timedelta(days=1)
            # Skip weekends
            while target.weekday() >= 5:
                target = target + datetime.timedelta(days=1)
            wait_s = (target - now).total_seconds()
            print(f"[Backtest] Next 3:45 PM run: {target.strftime('%Y-%m-%d %H:%M')} "
                  f"({wait_s/3600:.1f}h away)")
            await asyncio.sleep(wait_s)
            if state.backtest_running:
                await asyncio.sleep(60)
                continue
            loop = asyncio.get_event_loop()
            # Step 1: snapshot ALL 264 tickers + compute P/L inline
            state.backtest_running = True
            state.backtest_progress = "Daily snapshot (264 tickers)..."
            await loop.run_in_executor(None, _run_backtest_snapshot_sync)
            # Step 2: open new positions for today's screener signals
            state.backtest_running = True
            state.backtest_progress = "Opening new signals..."
            await loop.run_in_executor(None, _run_backtest_open_sync)
        except Exception as e:
            print(f"[Backtest scheduler] {e}")
            await asyncio.sleep(300)


def _run_backtest_snapshot_sync():
    """Snapshot all universe tickers' chains + update P/L. Wraps backtest_runner."""
    try:
        br = _backtest_module()
        def _progress(msg):
            state.backtest_progress = msg
        chain_n, pnl_n = br.snapshot_universe(report_fn=_progress)
        state.backtest_progress = f"Snapshot done: {chain_n} chains, {pnl_n} P/L"
        print(f"[Backtest] Snapshot done: {chain_n} chains, {pnl_n} P/L entries")
    except Exception as e:
        import traceback; traceback.print_exc()
        state.backtest_progress = f"Error: {e}"
    finally:
        state.backtest_running = False


# ── Journal endpoints ──

@app.get("/journal", response_class=HTMLResponse)
async def journal_page():
    """Serve the trade journal dashboard with patched data URL."""
    dashboard_path = TRADE_JOURNAL_DIR / "dashboard.html"
    if not dashboard_path.exists():
        return HTMLResponse("<h2>Trade journal not found</h2>", status_code=404)
    html = dashboard_path.read_text(encoding="utf-8")
    # Rewrite the data fetch URL to use our API
    html = html.replace("fetch('./trades_raw.json')", "fetch('/api/journal/data')")
    html = html.replace("fetch('trades_raw.json')", "fetch('/api/journal/data')")
    # Inject privacy mode CSS + listener into journal
    privacy_inject = """
<style>
.priv-on .hstat-value { filter: blur(6px); user-select: none; }
.priv-on .card canvas,
.priv-on .chart-wrap { filter: blur(6px); }
.priv-on #trades-tbody,
.priv-on #trades-stats-bar,
.priv-on #stat-cards,
.priv-on .ov-stat-box div:not(.card-title),
.priv-on .ov-trades-panel table,
.priv-on .ov-trades-panel .ov-table-wrap,
.priv-on .analysis-grid canvas,
.priv-on .analysis-grid table { filter: blur(6px); user-select: none; }
/* Account badge */
.priv-on .acct-badge,
.priv-on #h-acct { filter: blur(6px); user-select: none; }
/* Calendar — blur numbers, trades, details; keep nav buttons */
.priv-on #cal-month-summary,
.priv-on #cal-main,
.priv-on #cal-detail,
.priv-on #cal-week-view,
.priv-on #cal-day-view,
.priv-on #cal-year-view { filter: blur(6px); user-select: none; }
/* Review — blur table data; keep filter/sort buttons */
.priv-on #review-tbody,
.priv-on #review-summary { filter: blur(6px); user-select: none; }
</style>
<script>
window.addEventListener('message', e => {
  if (e.data && e.data.type === 'privacy') {
    document.body.classList.toggle('priv-on', e.data.on);
  }
});
</script>
"""
    html = html.replace("</head>", privacy_inject + '<script src="/static/zeus_lang.js"></script></head>')
    # Remove the redundant Reload button — refresh is handled by the top bar
    html = html.replace(
        """<button class="reload-btn" onclick="triggerFilePick()">⟳ Reload</button>""",
        ""
    )
    html = html.replace("</body>", '<link rel="stylesheet" href="/static/zeus.css">'
                        '<script src="/static/zeus.js"></script></body>')   # 中/双语/EN switch + bilingual labels inside the journal iframe
    return HTMLResponse(content=html)


@app.get("/api/journal/data")
def journal_data():
    """Serve trades_raw.json.

    Plain `def` (not async) so FastAPI runs the blocking file read + 1 MB JSON
    (de)serialization in a threadpool instead of stalling the event loop — the
    same loop drives the DXLink stream and per-second WebSocket pushes, so an
    async body here would queue every live tick behind this request."""
    json_path = TRADE_JOURNAL_DIR / "data" / "trades_raw.json"
    if not json_path.exists():
        return JSONResponse({"error": "No trade data. Click Refresh Trades."}, status_code=404)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data


def _journal_entries_module():
    import importlib, sys as _sys
    _dir = str(PROJECT_ROOT / "trade_journal")
    if _dir not in _sys.path:
        _sys.path.insert(0, _dir)
    return importlib.import_module("journal_entries")


def _weekly_reviews_module():
    """Lazy import of trade_journal.weekly_reviews so the optionstrat server
    doesn't import it at startup (matches the _journal_entries_module pattern)."""
    import importlib
    import sys as _sys
    if str(TRADE_JOURNAL_DIR) not in _sys.path:
        _sys.path.insert(0, str(TRADE_JOURNAL_DIR))
    return importlib.import_module("weekly_reviews")


async def _load_weekly_review_push_loop():
    """Lazy wrapper so the push module is loaded from trade_journal/ at
    startup time, after sys.path has been adjusted by _weekly_reviews_module."""
    _weekly_reviews_module()  # ensures trade_journal/ on sys.path
    import importlib
    wrp = importlib.import_module("weekly_review_push")
    await wrp.weekly_review_push_loop()


# ── Take-profit alert: shared evaluator (event-driven + backstop) ─────────────
# Fired from TWO places that share one dedup so they never double-push:
#   1. periodic_mark_refresh (every 30s) — PRIMARY, event-driven: evaluates the
#      instant a fresh REST mark lands, so a crossing alerts within the mark
#      cadence instead of waiting on a separate timer.
#   2. exit_alert_loop (60s) — slow backstop if (1) ever stalls.
# Native push works foreground / background / closed (server-side osascript, ~0.2s).
_EXIT_FIRED: dict = {}     # ticker -> date; ≤1 push per ticker per calendar day
_EXIT_EAP = None           # cached exit_alert_push module
_EXIT_RG = None            # cached checkup.regime module (regime-adaptive bands)


def _notifications_muted() -> bool:
    try:
        return json.loads(_ALERT_CFG_PATH.read_text()).get("muted", False)
    except Exception:
        return False


async def _run_exit_alert_check() -> None:
    """Evaluate every open spread against the regime-adaptive take-profit rule and
    fire a native macOS push for any fresh crossing. Idempotent per ticker/day via
    the shared _EXIT_FIRED. Cheap (pure-Python max-profit math) → safe to call on
    every mark refresh."""
    global _EXIT_EAP, _EXIT_RG
    if not is_market_hours() or _notifications_muted():
        return
    if _EXIT_EAP is None:
        import importlib, sys as _sys
        from pathlib import Path as _P
        _ck = str(_P(__file__).resolve().parent)
        if _ck not in _sys.path:
            _sys.path.insert(0, _ck)
        try:
            _EXIT_EAP = importlib.import_module("exit_alert_push")
        except Exception as e:
            print(f"[ExitAlert] could not load exit_alert_push: {e}")
            return
        try:
            from checkup import regime as _rg
            _EXIT_RG = _rg
        except Exception:
            _EXIT_RG = None
    eap = _EXIT_EAP
    cfg = eap.load_config(_ALERT_CFG_PATH)
    if not cfg.get("enabled", True):
        return
    regime = (_EXIT_RG.market_regime().get("regime") if _EXIT_RG else None)
    fires = eap.evaluate(state.strategies, cfg, _EXIT_FIRED,
                         datetime.datetime.now(), regime)
    for f in fires:
        rg_lbl = {"bull": "牛市", "chop": "震荡"}.get(f.get("regime"), "中性")
        forced = (f.get("tier") == "forced")
        emoji, act = ("🔴", "强制止盈") if forced else ("🔵", "止盈")
        tail = ("现在就平、腾资金开下单" if forced
                else f'建议落袋;到 {f.get("forced_pct","")}% 强制')
        await eap.send_macos_notification(
            f'{emoji} {act} {f["ticker"]}',
            f'{f["name"]} 已吃到 {f["maxpct"]:.0f}% 最大利润 · '
            f'还 {int(f["dte"])} 天 · {rg_lbl}{f.get("zone","")}区 — {tail}',
            sound=cfg.get("sound", False),
        )
        print(f"[ExitAlert] pushed {f['ticker']} {f.get('tier')} "
              f"maxprofit={f['maxpct']:.0f}% dte={int(f['dte'])} regime={f.get('regime')}")


async def exit_alert_loop():
    """Slow backstop for the take-profit push.

    The PRIMARY trigger is now event-driven inside periodic_mark_refresh:
    _run_exit_alert_check() fires the instant a fresh 30s mark crosses the
    threshold (works foreground / background / closed — server-side osascript,
    browser JS not involved, so a backgrounded/throttled tab is irrelevant).
    This loop only re-runs the same check on a slow timer in case that path
    ever stalls; the shared _EXIT_FIRED dedup means it never double-fires.
    Market hours only. Pure logic in exit_alert_push.py.
    """
    await asyncio.sleep(90)  # let the first position fetch + mark refresh settle
    while True:
        try:
            await _run_exit_alert_check()
        except Exception as e:
            print(f"[ExitAlert] backstop loop error: {e}")
        await asyncio.sleep(60)


# ── Missing take-profit GTC watcher ─────────────────────────────────────────
# Every open (ticker, expiration) group SHOULD carry a closing GTC order on
# the TastyTrade book — that's the user's discipline. When one doesn't, we
# push a native notification and expose the gap to the PWA (position card red
# badge + checkup banner) via /api/missing_gtc. Pure matching logic in
# options_analyzer/missing_gtc.py; this shell owns state + loop cadence.
_MISSING_GTC_STATE: dict = {"list": [], "expiring_today": [], "at": None}
_MISSING_GTC_FIRED: dict = {}   # (ticker|exp) -> date; ≤1 push/group/day
_MISSING_GTC_ACCT: str | None = None


async def _run_missing_gtc_check() -> None:
    """One pass of the missing-GTC watcher. Pulls live orders, matches against
    open spreads, updates `_MISSING_GTC_STATE`, and pushes a native notification
    for any newly-gapped (ticker, exp) group (deduped per calendar day).

    Cheap-ish (one HTTPS round-trip for /orders/live + pure Python match).
    Skips when strategies aren't loaded yet, market is closed, or the user has
    muted alerts globally.
    """
    global _MISSING_GTC_ACCT
    if not state.strategies or not state.client:
        return
    if _notifications_muted():
        # We still refresh the state list so the PWA badge stays accurate even
        # muted — the mute only suppresses the macOS push, not the visual.
        pass
    import importlib, sys as _sys
    from pathlib import Path as _P
    _here = str(_P(__file__).resolve().parent)
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    try:
        mg = importlib.import_module("missing_gtc")
    except Exception as e:
        print(f"[MissingGTC] cannot load missing_gtc: {e}")
        return

    if _MISSING_GTC_ACCT is None:
        try:
            _, _MISSING_GTC_ACCT = state.client.pick_active_account()
        except Exception as e:
            print(f"[MissingGTC] pick_active_account failed: {e}")
            return

    try:
        orders = await asyncio.get_event_loop().run_in_executor(
            None, state.client.get_live_orders, _MISSING_GTC_ACCT)
    except Exception as e:
        print(f"[MissingGTC] get_live_orders failed: {e}")
        return

    missing = mg.find_missing_gtc(state.strategies, orders)
    # 「今日到期」同一 loop 一并算 —— 用 ET 时区的今天(市场关门口径 nyClose),
    from zoneinfo import ZoneInfo as _Z
    today_et = datetime.datetime.now(_Z("America/New_York")).date().isoformat()
    expiring_today = mg.find_expiring_today(state.strategies, today_et)
    _MISSING_GTC_STATE["list"] = missing
    _MISSING_GTC_STATE["expiring_today"] = expiring_today
    _MISSING_GTC_STATE["at"] = datetime.datetime.now().isoformat(timespec="seconds")

    # Push only during market hours and when not muted — don't wake the user
    # at 3am for a gap they can't fix.
    if not is_market_hours() or _notifications_muted():
        return
    today = datetime.date.today()
    import importlib as _il
    try:
        eap = _il.import_module("exit_alert_push")
    except Exception:
        eap = None
    for g in missing:
        key = mg.missing_key(g)
        if _MISSING_GTC_FIRED.get(key) == today:
            continue
        _MISSING_GTC_FIRED[key] = today
        if eap is None:
            continue
        try:
            await eap.send_macos_notification(
                f"⚠️ 未挂止盈单 {g['ticker']}",
                f"{g['ticker']} exp {g['exp']} 没有 working closing GTC —— 顺手挂一个,别忘了",
                sound=False,
            )
            print(f"[MissingGTC] pushed {key}")
        except Exception as e:
            print(f"[MissingGTC] push failed for {key}: {e}")


async def missing_gtc_loop():
    """Slow poll for the missing-GTC state (60s). See _run_missing_gtc_check
    for the actual logic. Sleeps 90s at boot so the first positions fetch
    and market_hours seed happen first."""
    await asyncio.sleep(90)
    while True:
        try:
            await _run_missing_gtc_check()
        except Exception as e:
            print(f"[MissingGTC] loop error: {e}")
        await asyncio.sleep(60)


_RISK_ALERT_THRESH = 60   # gauge ≥ 此 = 高危 → 推一次

async def risk_radar_alert_loop():
    """风险雷达后台告警:今日 digest regime 饱和度 ≥ 阈值 → native push(每天最多一次)。
    交易时段才查;低频(30min)。复用 exit_alert_push.send_macos_notification。"""
    import importlib, sys as _sys
    from pathlib import Path as _P
    _ck = str(_P(__file__).resolve().parent)
    if _ck not in _sys.path:
        _sys.path.insert(0, _ck)
    try:
        eap = importlib.import_module("exit_alert_push")
        from checkup import risk_radar as rr
    except Exception as e:
        print(f"[RiskRadar] load fail: {e}")
        return
    await asyncio.sleep(90)
    fired_date = None
    while True:
        try:
            if is_market_hours() and not _notifications_muted():
                try:
                    items = _dgs.load_day(_digest_today_str()) or []
                except Exception:
                    items = []
                g = rr.regime_saturation(items)
                today = datetime.date.today().isoformat()
                if g.get("score", 0) >= _RISK_ALERT_THRESH and fired_date != today:
                    top = g.get("top") or {}
                    await eap.send_macos_notification(
                        f'⚠️ 市场风险偏高 {g.get("score")}/100',
                        f'今日新闻被「{top.get("label","风险话题")}」饱和(历史这类日子跌≥1% '
                        f'{top.get("lift","?")}× 基础率)— 别开新单 / 减部署 / 留现金',
                    )
                    fired_date = today
                    print(f"[RiskRadar] pushed score={g.get('score')} top={top.get('label')}")
        except Exception as e:
            print(f"[RiskRadar] loop error: {e}")
        await asyncio.sleep(1800)


# ── Credit-spread mistake watcher ──
# The user routinely *means* to open a debit vertical (e.g. a bull call spread)
# but mis-configures the order so it fills as a CREDIT vertical (bear call
# spread) — net credit received, wrong direction. This watcher flags exactly
# that: a 2-leg same-type same-expiration vertical that came in net-credit, and
# fires ONE native push per spread (persisted to data/credit_spread_seen.json).
# Unlike exit_alert_loop it fires even when a browser is connected — the user is
# actively placing the order, so the safety warning must not be suppressed.
_CREDIT_SEEN_PATH = Path(__file__).parent / "data" / "credit_spread_seen.json"
_credit_lock = None        # asyncio.Lock, created lazily on the running loop
_credit_seeded = False     # absorbed pre-existing spreads as the baseline yet?


async def _scan_credit_spreads():
    """Scan state.strategies for NEW credit verticals → native push, once each.

    Idempotent and safe to call from both the periodic loop and right after
    /api/refresh: a stable per-spread key in data/credit_spread_seen.json (plus
    an asyncio lock) guarantees at most one push per spread. On the very first
    run (no seen-file yet) it absorbs every currently-open credit vertical as the
    baseline WITHOUT notifying, so pre-existing / intentional credit spreads
    don't trigger an alert storm — only spreads opened afterwards fire.
    """
    global _credit_lock, _credit_seeded
    if _notifications_muted():
        return
    import importlib
    try:
        csa = importlib.import_module("credit_spread_alert")
        eap = importlib.import_module("exit_alert_push")
    except Exception as e:
        print(f"[CreditSpread] could not load modules: {e}")
        return
    if _credit_lock is None:
        _credit_lock = asyncio.Lock()
    async with _credit_lock:
        if not state.ready:
            return
        cfg = csa.load_alert_config(_ALERT_CFG_PATH)
        if not csa.is_enabled(cfg):
            return
        first_run = not _CREDIT_SEEN_PATH.exists()
        seen = csa.load_seen(_CREDIT_SEEN_PATH)
        if first_run and not _credit_seeded:
            seen |= csa.credit_vertical_keys(state.strategies)
            csa.save_seen(_CREDIT_SEEN_PATH, seen)
            _credit_seeded = True
            print(f"[CreditSpread] seeded {len(seen)} existing credit verticals "
                  f"(baseline, no alert)")
            return
        _credit_seeded = True
        fires = csa.evaluate(state.strategies, seen)
        for f in fires:
            await eap.send_macos_notification(
                f'⚠️ 开成 Credit Spread? {f["ticker"]}',
                f'{f["name"]} · 卖{f["short_strike"]}/买{f["long_strike"]} · '
                f'收 +${f["credit"]:.2f} — 方向可能开反了，去检查仓位',
            )
            seen.add(f["key"])
            print(f"[CreditSpread] pushed {f['ticker']} {f['name']} "
                  f"credit +${f['credit']:.2f}")
        if fires:
            csa.save_seen(_CREDIT_SEEN_PATH, seen)


async def credit_spread_alert_loop():
    """Poll for newly-opened credit verticals every 30s. New positions only
    appear on a full position re-fetch (periodic_refresh / /api/refresh), so this
    cadence plus the post-refresh hook keeps the warning near-immediate."""
    await asyncio.sleep(75)  # let the first position fetch settle
    while True:
        try:
            await _scan_credit_spreads()
        except Exception as e:
            print(f"[CreditSpread] loop error: {e}")
        await asyncio.sleep(30)


async def decision_log_reminder_loop():
    """After the US close, nudge ONCE/day if any decision-log entry is still
    pending (un-reviewed). Replaces the old 3-slot browser reminder.

    Why server-side: native macOS push via osascript works even when no PWA
    window is open. The server is launchd-resident, so when the Mac was
    asleep/off at the 16:00 ET close, this fires as soon as it's back up.

    Rules (matching the user's ask):
    - Only weekdays, only at/after the 16:00 ET close.
    - Fire only if there's something to review (pending > 0). Nothing pending
      → stay silent for the day; no "all clear" ping.
    - At most one push per ET day. If nothing is pending when we first cross
      the close we DON'T burn the day — we keep checking, so a close that
      syncs late in the evening (or the Mac waking hours later) still nudges.
    - Carry-over: an entry left un-reviewed will nudge again the next day
      after the close, until it's filled in.
    """
    import importlib
    from zoneinfo import ZoneInfo
    try:
        eap = importlib.import_module("exit_alert_push")
    except Exception as e:
        print(f"[DLReminder] could not load exit_alert_push: {e}")
        return
    ET = ZoneInfo("America/New_York")
    last_fired_date = None    # ET 'YYYY-MM-DD' we already pushed for
    await asyncio.sleep(90)   # let startup settle
    while True:
        try:
            now_et = datetime.datetime.now(datetime.timezone.utc).astimezone(ET)
            today = now_et.date().isoformat()
            if (now_et.weekday() < 5            # Mon–Fri (ET)
                    and now_et.hour >= 16        # at/after the 16:00 ET close
                    and last_fired_date != today
                    and not _notifications_muted()):
                je = _journal_entries_module()
                pending = je.list_entries("pending", "all")
                if pending:
                    n_open = sum(1 for e in pending if e.get("_kind") == "open")
                    n_close = len(pending) - n_open
                    parts = []
                    if n_open:
                        parts.append(f"{n_open} 笔开仓")
                    if n_close:
                        parts.append(f"{n_close} 笔平仓")
                    detail = " + ".join(parts)
                    await eap.send_macos_notification(
                        "📓 决策日志待复盘",
                        f"收盘了 — 还有 {detail} 没写决策/复盘，趁记忆新鲜补上",
                    )
                    last_fired_date = today
                    print(f"[DLReminder] pushed: {len(pending)} pending "
                          f"({n_open} open, {n_close} close)")
                # pending == 0 → nothing to review; leave last_fired_date unset
                # so a late-synced close (or a later wake) can still trigger.
        except Exception as e:
            print(f"[DLReminder] loop error: {e}")
        await asyncio.sleep(300)   # re-check every 5 min


async def _fire_heatmap_notification(tn_path, title, body, url, group="heatmap-popup"):
    """大盘热图弹窗通知:terminal-notifier(可点 → -open 打开 PWA 深链)优先;
    无则 osascript 兜底(原生但不可点,靠 PWA 轮询自动跳)。

    group: terminal-notifier 同 group 的新通知会**替换**旧的。默认沿用
    "heatmap-popup"(定时弹窗本就只该留最新一条);sr_alert 传每票独立的
    group,好让不同票的告警并存、同票刷新时替换。"""
    if tn_path:
        cmd = [tn_path, "-title", title, "-message", body, "-open", url,
               "-sound", "Glass", "-group", group]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            return
        except Exception:
            pass
    try:
        import importlib
        eap = importlib.import_module("exit_alert_push")
        await eap.send_macos_notification(title, body, sound=True)
    except Exception as e:
        print(f"[hmPopup] notify err: {e}")


_SR_LOG_PATH = Path(__file__).parent / "data" / "sr_alert_log.json"


async def _sr_enrich_task(loop, srlog, srenrich, ticker: str, kind_cn: str, day: str):
    """后台给某票当天的 SR 告警补新闻原因(digest/多空/异动 → Haiku)。
    Haiku 阻塞 → executor;结果同步 load-modify-save 回填(不 await → 与落库无竞争)。"""
    try:
        enrichment = await loop.run_in_executor(
            None, srenrich.enrich_reason, ticker, kind_cn, day)
        st = srlog.load(str(_SR_LOG_PATH))
        srlog.set_enrichment(st, ticker, day, enrichment)
        srlog.save(str(_SR_LOG_PATH), st)
        print(f"[srAlert] enriched {ticker} {day}: "
              f"digest={enrichment.get('digest_count')} "
              f"reason={(enrichment.get('reason_cn') or '')[:30]}")
    except Exception as e:
        print(f"[srAlert] enrich {ticker} error: {e}")


async def sr_zone_alert_loop():
    """盘中 S/R 黄金 zone 触及告警 —— 价格进入 conf==3 zone 即弹可点通知。

    设计:docs/superpowers/specs/2026-07-15-sr-zone-realtime-alert-design.md
    判定纯函数在 sr_alert/(harness 30 PASS,含与 engine.zone_touch 交叉验证)。

    · 触发 = 价进入 conf==3 黄金 zone(日+周+月共振)且昨收在区外(方向成立)。
      语义照搬 ta_screener/engine.py:303 zone_touch;昨收已在区内('inside')不报。
    · 去重 = 同票同 zone 同交易日一次。实测 ~14 条/天(125 票 / 445 个黄金 zone)。
    · 点通知 → /ta/?ticker=XXX(ta_screener/page.py:1313 接住自动开详情)。

    ⚠️ 数据源 = heatmap 那条 DXLink 流的 live_prices()(700 票 universe,覆盖 TA 全池)。
       **不是** state.live_quotes —— 后者只有持仓标的 + SPY/QQQ/IWM(见 :637),
       用它的话告警只对已持仓的票生效,且静默半死。

    ⚠️ 开盘预热 90s:_reconcile_session 换日只清 _ext 不清 _reg,9:30 那一刻
       _reg["tickers"] 可能还是昨天的收盘价 → 会把昨天的触及当今天报。
       预热窗口内只加载 zone、不告警。代价是 gap-open 信号晚 90s(对 35-45 DTE 无影响)。
    """
    # 导入失败必须**响亮地**报出来:asyncio task 里的异常是静默的,
    # 放 try 外的话 sr_alert 一旦导不到,这个循环就无声无息地死了,
    # 表现为「再也不弹告警」且零线索。(house style 同 ta_screener.scheduler 注册处)
    try:
        import shutil
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        from heatmap import heatmap_api as _hm
        from heatmap import session as _sess
        from sr_alert import check_entry, fire_key, format_alert, load_conf3_zones
        from sr_alert import log as _srlog
        from sr_alert import enrich as _srenrich
    except Exception as e:
        print(f"[srAlert] ✗ 未启动(导入失败): {type(e).__name__}: {e}")
        return
    _ET = _ZI("America/New_York")
    enrich_spawned: set = set()   # {(ticker, day)} —— 每票每天只后台富化一次

    WARMUP = 90.0          # 开盘预热,防 _reg 残留昨日值
    ZONE_TTL = 1800.0      # results.json EOD 重跑 → 半小时重载 zone
    TICK = 5.0             # 对齐 heatmap 实时档;zone 宽度中位 ~0.4%,5s 不会穿区漏检

    tn = shutil.which("terminal-notifier")
    if not tn:
        for cand in ("/opt/homebrew/bin/terminal-notifier",
                     "/usr/local/bin/terminal-notifier"):
            if os.path.exists(cand):
                tn = cand
                break

    zones: dict = {}
    zones_at = 0.0
    fired: set = set()     # {(ticker, zone.avg, 交易日)} —— 含日期,跨日自动失效
    rth_since = 0.0
    loop = asyncio.get_event_loop()
    await asyncio.sleep(60)                       # 等 startup + DXLink 流稳定

    while True:
        try:
            phase, sdate = _sess.session_now()
            if phase != _sess.RTH:
                rth_since = 0.0
            else:
                if not rth_since:
                    rth_since = time.time()
                    print(f"[srAlert] RTH {sdate} — 预热 {WARMUP:.0f}s 后开始告警")
                if time.time() - zones_at > ZONE_TTL:
                    zones = await loop.run_in_executor(None, load_conf3_zones)
                    zones_at = time.time()
                    print(f"[srAlert] conf3 zones: {len(zones)} 票 / "
                          f"{sum(len(v) for v in zones.values())} zone")
                warmed = (time.time() - rth_since) >= WARMUP
                # ⚠️ 检测**独立于静音**:命中就落库(供 📍 SR 记录 tab),只有弹窗看静音。
                if warmed and zones:
                    day = str(sdate)
                    muted = _notifications_muted()
                    for tk, q in _hm.live_prices().items():
                        zs = zones.get(tk)
                        if not zs:
                            continue
                        a = check_entry(tk, q.get("prev_close"), q.get("last"),
                                        zs, fired, day)
                        if not a:
                            continue
                        fired.add(fire_key(tk, a.zone, day))
                        # ── 落库(同步 load-modify-save,不 await → 与富化任务无竞争)──
                        hhmm = _dt.now(_ET).strftime("%H:%M")
                        entry = _srlog.build_entry(a, time.time(), hhmm, day)
                        st = _srlog.load(str(_SR_LOG_PATH))
                        _srlog.add(st, entry)
                        # 作者口径(2026-07-23): SR 记录只看当天, 旧的不用留 →
                        # cutoff = 今天, 隔天自动清空, 磁盘不增长。
                        from datetime import date as _date
                        _srlog.prune(st, _date.today().isoformat())
                        _srlog.save(str(_SR_LOG_PATH), st)
                        # ── 后台富化(每票每天一次,Haiku 阻塞 → executor)──
                        if (tk, day) not in enrich_spawned:
                            enrich_spawned.add((tk, day))
                            asyncio.create_task(
                                _sr_enrich_task(loop, _srlog, _srenrich, tk, entry["kind_cn"], day))
                        # ── 弹窗通知(仅未静音)──
                        if not muted:
                            title, body, url = format_alert(a)
                            await _fire_heatmap_notification(
                                tn, title, body, url, group=f"sr-alert-{tk}")
                        print(f"[srAlert] {'🔔' if not muted else '📝'} {tk} {a.kind} "
                              f"${a.price:.2f} zone {a.zone['lo']}–{a.zone['hi']} "
                              f"(prev {a.prev_close}) conf3")
        except Exception as e:
            print(f"[srAlert] loop error: {e}")
        await asyncio.sleep(TICK)


async def heatmap_ohlc_warm_loop():
    """预热 heatmap 大盘股(≥100B)OHLC 到本地缓存 + 落盘 —— Graph 渲染时全缓存命中,
    不再因并发单拉被 Yahoo 限流出现「无数据」。启动 ~20s 后跑一次,之后每 6h 刷新(日线够了)。
    下载阻塞 → 丢 executor,不挡事件循环。"""
    await asyncio.sleep(20)
    while True:
        try:
            from ta_screener.api import warm_heatmap_ohlc
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, warm_heatmap_ohlc)
            print(f"[hmOHLC] warm done: {info}")
        except Exception as e:
            print(f"[hmOHLC] warm loop err: {e}")
        await asyncio.sleep(6 * 3600)


async def heatmap_popup_loop():
    """本地 9:00 / 11:00 / 14:00 / 20:00 各弹一次「异动新闻 Mover News」——
    提醒作者看今天 ±1% 异动票 + 相关新闻(digest 匹配 + finviz 有情绪的标题,双语)。

    **外加「开机补弹」**:电脑之前关着 / 没在窗口内,开机(server 重启)后只要是工作日、
    过了本地 9点、当天还一条都没弹过 → 立刻补一条(覆盖「晚上 10点才开电脑」)。睡眠唤醒
    时 server 不重启,靠前端 visibilitychange 兜;这里再用「墙钟跳变 >10min」重新武装补弹。

    · 通知:terminal-notifier 可点(-open 深链 ?mvnews=slot → 自动跳 Mover News);
      装不到就 osascript 兜底。同时置 heatmap_api「待弹」状态 → 开着的 PWA 轮询到自动跳。
    · 已弹时段持久化到 data/heatmap_popup.json(按日期),跨重启同一天不重复弹。
      窗口:s9 [9,11) · s11 [11,14) · s14 [14,20) · s20 [20,24);补弹 catchup 任意 ≥9点。
    """
    import shutil
    from pathlib import Path as _P
    PORT = int(os.environ.get("PORT", "8765"))
    STATE_F = _P(__file__).resolve().parent / "data" / "heatmap_popup.json"
    SLOTS = [
        ("s9", 9, 11, "📰 开盘异动新闻 · 9:00",
         "点开看今天 ±1% 异动票 + 相关新闻(digest / finviz 双语)"),
        ("s11", 11, 14, "📰 午盘异动新闻 · 11:00",
         "盘中谁在动 — ±1% 异动票的最新新闻都在这"),
        ("s14", 14, 20, "📰 盘尾异动新闻 · 14:00",
         "收盘前看一眼今天的异动票 + 新闻,挑明天的单"),
        ("s20", 20, 24, "📰 盘后异动新闻 · 20:00",
         "盘后复盘:今天 ±1% 异动票最终表现 + 相关新闻"),
    ]
    CATCHUP = ("catchup", "📰 今天还没看异动新闻",
               "你今天还没看异动票新闻 — 点开补上(±1% 异动 + digest/finviz 双语)")
    tn = shutil.which("terminal-notifier")
    if not tn:
        for cand in ("/opt/homebrew/bin/terminal-notifier",
                     "/usr/local/bin/terminal-notifier"):
            if os.path.exists(cand):
                tn = cand
                break

    def _today_state() -> dict:
        """当天状态 {date, fired:[slots]};跨天自动重置。"""
        try:
            d = json.loads(STATE_F.read_text())
        except Exception:
            d = {}
        today = datetime.date.today().isoformat()
        if not isinstance(d, dict) or d.get("date") != today \
                or not isinstance(d.get("fired"), list):
            d = {"date": today, "fired": []}
        return d

    def _save_state(d: dict) -> None:
        try:
            STATE_F.parent.mkdir(parents=True, exist_ok=True)
            STATE_F.write_text(json.dumps(d))
        except Exception as e:
            print(f"[hmPopup] save err: {e}")

    async def _fire(slot: str, title: str, body: str) -> None:
        if _notifications_muted():
            print(f"[hmPopup] muted, skipping {slot}")
            return
        url = f"http://localhost:{PORT}/?mvnews={slot}"
        await _fire_heatmap_notification(tn, title, body, url)
        try:
            from heatmap import heatmap_api as _hm
            _hm.set_popup_due(slot, title)
        except Exception as e:
            print(f"[hmPopup] set_popup_due err: {e}")
        d = _today_state()
        if slot not in d["fired"]:
            d["fired"].append(slot)
        _save_state(d)
        print(f"[hmPopup] fired {slot} @ {datetime.datetime.now():%H:%M} local "
              f"(terminal-notifier={'yes' if tn else 'no'})")

    armed_catchup = True              # 开机首轮 → 查一次补弹
    last_wall = datetime.datetime.now()
    await asyncio.sleep(45)           # let startup settle
    while True:
        try:
            now = datetime.datetime.now()   # 本地时间(作者当地)
            # 睡眠唤醒检测:墙钟跳变远超循环间隔(120s)→ 期间睡过 → 重新武装补弹
            if (now - last_wall).total_seconds() > 600:
                armed_catchup = True
            last_wall = now
            if now.weekday() < 5:           # Mon–Fri
                d = _today_state()
                fired_now = False
                for slot, h0, h1, title, body in SLOTS:
                    if slot in d["fired"]:
                        continue
                    if h0 <= now.hour < h1:
                        await _fire(slot, title, body)
                        fired_now = True
                # 开机/唤醒补弹:过了9点、今天一条没弹过、也不是正在弹定时档 → 立刻补
                d = _today_state()
                if (armed_catchup and now.hour >= 9
                        and not d["fired"] and not fired_now):
                    await _fire(*CATCHUP)
                armed_catchup = False
        except Exception as e:
            print(f"[hmPopup] loop error: {e}")
        await asyncio.sleep(120)   # 每 2 分钟看一次


@app.get("/api/journal_entries/list")
def journal_entries_list(status: str = "all", kind: str = "all"):
    """List Open + Close decision entries. status: pending/completed/all.

    Plain `def` → FastAPI threadpool: the JSONL load + 540 KB serialization is
    blocking, and running it on the event loop produced multi-second TTFB spikes
    whenever a live tick landed mid-request (the function itself is ~9 ms)."""
    je = _journal_entries_module()
    return {"entries": je.list_entries(status, kind)}


@app.get("/api/journal_entries/options")
def journal_entries_options():
    """Return all checkbox sections with defaults + user-added custom values.

    Plain `def` → threadpool (blocking file read); fetched alongside list on the
    decision-log init path, so keep it off the event loop too."""
    je = _journal_entries_module()
    return je.get_all_options()


@app.post("/api/journal_entries/options/{section}/add")
async def journal_entries_options_add(section: str, request: Request):
    je = _journal_entries_module()
    body = await request.json()
    val = (body.get("value") or "").strip()
    if not val:
        return JSONResponse({"error": "empty value"}, status_code=400)
    ok = je.add_custom_option(section, val)
    return {"added": ok, "value": val}


@app.delete("/api/journal_entries/options/{section}/{value}")
async def journal_entries_options_remove(section: str, value: str):
    je = _journal_entries_module()
    ok = je.remove_custom_option(section, value)
    return {"removed": ok}


@app.get("/api/journal_entries/{entry_id}")
async def journal_entries_get(entry_id: str):
    je = _journal_entries_module()
    e = je.get_entry(entry_id)
    if not e:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Surface current NLV so the drawer can compute "max_loss as % of capital"
    nlv = _compute_live_nlv() or 0
    out = {"entry": e, "account_nlv": nlv}
    # If close entry, also return paired open snapshot
    if e.get("_kind") == "close":
        paired = je.get_paired_entries(e.get("trade_id", ""))
        out["open_snapshot"] = paired.get("open")
    return out


@app.patch("/api/journal_entries/{entry_id}")
async def journal_entries_patch(entry_id: str, request: Request):
    je = _journal_entries_module()
    body = await request.json()
    ok = je.update_entry_fields(entry_id, body)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"updated": True}


@app.post("/api/journal_entries/sync")
async def journal_entries_sync():
    """Sync entries from current trades_raw.json (idempotent)."""
    je = _journal_entries_module()
    result = je.sync_entries_from_trades_raw()
    return result


@app.get("/api/journal_merges")
def journal_entries_merges():
    """当前已合并的 (ticker, open_date) 组,供前端标记。
    (用顶层路径,避开 /api/journal_entries/{entry_id} 的通配抢匹配。)"""
    je = _journal_entries_module()
    return {"merges": [list(k) for k in sorted(je.load_merge_keys())]}


@app.post("/api/journal_merge")
async def journal_entries_merge(request: Request):
    """合并同票同天多单成一单(可逆)。body: {ticker, open_date, merge:true/false}。
    改 override 存盘后立即重同步,使 entries/stats/whatif 一致看到一单。"""
    je = _journal_entries_module()
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker, od = body.get("ticker"), body.get("open_date")
    if not ticker or not od:
        return JSONResponse({"ok": False, "msg": "缺 ticker/open_date"})
    if body.get("merge", True):
        je.add_merge(ticker, od)
    else:
        je.remove_merge(ticker, od)
    res = je.sync_entries_from_trades_raw()
    return {"ok": True, "merged": body.get("merge", True), "sync": res}


@app.post("/api/journal_entries/{entry_id}/screenshot")
async def journal_entries_add_screenshot(entry_id: str, request: Request):
    """Attach a screenshot to an entry. Body is raw PNG bytes (image/png
    Content-Type) — no multipart needed since the JS uses canvas.toBlob()."""
    je = _journal_entries_module()
    blob = await request.body()
    if not blob or len(blob) > 10 * 1024 * 1024:  # 10 MB cap
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "missing or oversize body"}, status_code=400)
    caption = request.query_params.get("caption", "")
    rec = je.add_screenshot(entry_id, blob, caption=caption)
    if not rec:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "entry not found"}, status_code=404)
    return {"ok": True, "screenshot": rec}


@app.delete("/api/journal_entries/{entry_id}/screenshot/{filename}")
async def journal_entries_delete_screenshot(entry_id: str, filename: str):
    je = _journal_entries_module()
    ok = je.remove_screenshot(entry_id, filename)
    return {"ok": ok}


@app.get("/api/journal_entries/{entry_id}/price_history")
async def journal_entries_price_history(entry_id: str):
    """Return per-snapshot spread metrics (MTM + net Δ Γ Θ Vega) and per-leg
    IV series. The drawer's tabbed chart switches between these series."""
    from fastapi.responses import JSONResponse
    je = _journal_entries_module()
    pt = _price_tracker_module()
    entry = je.get_entry(entry_id)
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    legs = entry.get("legs") or entry.get("close_legs") or []
    hist = pt.spread_history(legs)
    return {
        "n_points": len(hist.get("snapshots", [])),
        "snapshots": hist.get("snapshots", []),
        "legs": hist.get("legs", []),
        "open_at": entry.get("open_at"),
        "close_at": (entry.get("close_at")
                     if entry.get("_kind") == "close" else None),
    }


@app.post("/api/option_prices/snapshot_now")
async def option_prices_snapshot_now():
    """Force an immediate snapshot. Useful after Refresh Trades or for
    manual debug — skips the hourly cadence."""
    je = _journal_entries_module()
    pt = _price_tracker_module()
    opens = je.load_open_entries()
    closes = je.load_close_entries()
    symbols = pt.tracked_symbols(opens, closes)
    if not symbols:
        return {"snapshotted": 0, "reason": "no tracked symbols"}
    if not state.client:
        return {"snapshotted": 0, "reason": "tastytrade client not connected"}
    BATCH = 50
    marks = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i+BATCH]
        marks.update(fetch_option_marks(state.client, chunk))
    if not marks:
        return {"snapshotted": 0, "reason": "marks empty"}
    underlyings = sorted({s.split()[0] for s in symbols if s.split()})
    spots = fetch_stock_marks(state.client, underlyings)
    path = pt.snapshot_to_parquet(marks, spots=spots)
    return {"snapshotted": len(marks),
            "spots_fetched": len(spots),
            "file": path.name if path else None,
            "stats": pt.storage_stats()}


@app.get("/api/journal_entries/{entry_id}/screenshot/{filename}")
async def journal_entries_serve_screenshot(entry_id: str, filename: str):
    """Serve a saved screenshot PNG."""
    from fastapi.responses import FileResponse, JSONResponse
    je = _journal_entries_module()
    p = je.screenshot_path(entry_id, filename)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(p), media_type="image/png")


# ─── Weekly Review (weekend journal) ───────────────────────────────────────

_WEEKLY_REVIEWS_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


@app.get("/api/weekly_reviews/list")
async def api_weekly_reviews_list():
    """Calendar — list every stored review (minimal projection)."""
    try:
        wr = _weekly_reviews_module()
        return {"reviews": wr.list_summary()}
    except Exception as e:
        print("[weekly_reviews] list error:", e)
        return JSONResponse({"reviews": [], "error": str(e)}, status_code=500)


@app.get("/api/weekly_reviews/by_week/{week}")
async def api_weekly_reviews_by_week(week: str):
    """Return the full row for one ISO week (or `{review: null}`)."""
    if not _WEEKLY_REVIEWS_WEEK_RE.fullmatch(week):
        return JSONResponse({"error": "invalid week, want YYYY-Www"}, status_code=400)
    try:
        wr = _weekly_reviews_module()
        return {"review": wr.by_week(week)}
    except Exception as e:
        print("[weekly_reviews] by_week error:", e)
        return JSONResponse({"review": None, "error": str(e)}, status_code=500)


@app.post("/api/weekly_reviews/save")
async def api_weekly_reviews_save(payload: dict):
    """Upsert by `week`. Body must include `week`; everything else optional."""
    if not isinstance(payload, dict) or "week" not in payload:
        return JSONResponse({"ok": False, "error": "missing 'week'"}, status_code=400)
    if not _WEEKLY_REVIEWS_WEEK_RE.fullmatch(payload["week"] or ""):
        return JSONResponse({"ok": False, "error": "invalid week, want YYYY-Www"}, status_code=400)
    try:
        wr = _weekly_reviews_module()
        row = wr.upsert(payload)
        return {"ok": True, "review": row}
    except Exception as e:
        print("[weekly_reviews] save error:", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/api/weekly_reviews/{week}")
async def api_weekly_reviews_delete(week: str):
    if not _WEEKLY_REVIEWS_WEEK_RE.fullmatch(week):
        return JSONResponse({"ok": False, "error": "invalid week"}, status_code=400)
    try:
        wr = _weekly_reviews_module()
        removed = wr.delete(week)
        return {"ok": removed}
    except Exception as e:
        print("[weekly_reviews] delete error:", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/weekly_reviews/current_status")
async def api_weekly_reviews_current_status():
    """Push loop + frontend badge — is the current ISO week reviewed?"""
    try:
        wr = _weekly_reviews_module()
        week = wr.current_week_iso()
        return {"week": week, "completed": wr.is_completed(week)}
    except Exception as e:
        print("[weekly_reviews] current_status error:", e)
        return JSONResponse({"week": None, "completed": False, "error": str(e)}, status_code=500)


def _journal_stats_module():
    """Lazy-import journal_stats so a syntax error in that file can't break
    the rest of the server at startup."""
    import importlib
    import sys
    if str(TRADE_JOURNAL_DIR) not in sys.path:
        sys.path.insert(0, str(TRADE_JOURNAL_DIR))
    return importlib.import_module("journal_stats")


@app.get("/api/journal_stats")
async def journal_stats(
    from_: str = Query("", alias="from"),
    to: str = "",
    strategy: str = "",
    direction: str = "",
    ticker: str = "",
):
    """Single-shot trade-journal analytics dashboard payload.

    Query params (all optional):
      from / to       — YYYY-MM-DD inclusive range on open_date
      strategy        — exact match on strategy classification
      direction       — BULL / BEAR / NEUTRAL
      ticker          — exact ticker
    """
    js = _journal_stats_module()
    filters = {}
    if from_: filters["from"] = from_
    if to: filters["to"] = to
    if strategy: filters["strategy"] = strategy
    if direction: filters["direction"] = direction
    if ticker: filters["ticker"] = ticker
    try:
        return js.compute_all(filters or None)
    except Exception as e:
        # Return a structured error so the UI can surface it
        return {"error": str(e), "n_trades": 0}


def _whatif_module():
    import importlib, sys as _sys
    _dir = str(PROJECT_ROOT / "trade_journal")
    if _dir not in _sys.path:
        _sys.path.insert(0, _dir)
    return importlib.import_module("whatif_engine")

_whatif_cache = {"key": None, "data": None}

def _snapshot_store_key() -> str:
    d = PROJECT_ROOT / "trade_journal" / "data" / "option_prices"
    try:
        files = sorted(p.name for p in d.glob("*.parquet"))
        return files[-1] if files else "empty"
    except Exception:
        return "err"

@app.get("/api/journal/whatif")
async def journal_whatif():
    """每单 MFE/最优止盈 what-if。懒算+内存缓存(按快照库最新ts失效)。"""
    key = _snapshot_store_key()
    if _whatif_cache["key"] == key and _whatif_cache["data"] is not None:
        return _whatif_cache["data"]
    wf = _whatif_module()
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, wf.compute_whatif)
    _whatif_cache["key"] = key
    _whatif_cache["data"] = data
    return data


def _calendar_events_module():
    import importlib, sys
    if str(TRADE_JOURNAL_DIR) not in sys.path:
        sys.path.insert(0, str(TRADE_JOURNAL_DIR))
    return importlib.import_module("calendar_events")


@app.get("/api/calendar_events")
async def calendar_events_get():
    """Cached earnings + econ events. Returns empty payload if never refreshed."""
    return _calendar_events_module().load_cached()


@app.post("/api/calendar_events/refresh")
async def calendar_events_refresh():
    """Re-fetch both feeds (~1-3s). Returns the new payload."""
    try:
        return _calendar_events_module().refresh()
    except Exception as e:
        return {"error": str(e), "earnings": [], "econ": []}


# Tiny per-ticker quote cache so the Events tooltip doesn't hit yfinance
# every hover. 30-second TTL is plenty for the use case.
_QUOTE_CACHE: dict = {}
_QUOTE_TTL_SEC = 30


@app.get("/api/quote_simple")
async def quote_simple(symbol: str = Query(...)):
    """Latest price + day change for a single ticker. Used by the
    Events calendar tooltip — yfinance fast_info is sub-100ms per call."""
    import time as _t
    sym = symbol.strip().upper()
    now = _t.time()
    cached = _QUOTE_CACHE.get(sym)
    if cached and (now - cached["_at"]) < _QUOTE_TTL_SEC:
        return {k: v for k, v in cached.items() if not k.startswith("_")}
    try:
        import yfinance as yf
        fi = yf.Ticker(sym).fast_info
        price = float(fi.last_price)
        prev = float(fi.previous_close)
        change = price - prev
        change_pct = (change / prev * 100) if prev else 0.0
        out = {"symbol": sym, "price": price, "change": change, "changePct": change_pct}
        _QUOTE_CACHE[sym] = {**out, "_at": now}
        return out
    except Exception as e:
        return {"error": str(e), "symbol": sym}


@app.post("/api/journal/refresh")
async def journal_refresh():
    """Re-fetch trades from TastyTrade AND re-sync the journal entries.

    Two-step pipeline:
      1. fetch_trades.py writes trade_journal/trades_raw.json (broker dump).
      2. journal_entries.sync_entries_from_trades_raw() turns those raw
         transactions into paired open/close entries that the Decisions
         tab reads.

    The original endpoint only ran step 1, so today's closes/opens never
    showed up in the Decisions list until the user happened to run the
    sync script manually.
    """
    if state.journal_refreshing:
        return {"status": "already_running"}
    state.journal_refreshing = True
    try:
        python = sys.executable
        fetch_script = str(TRADE_JOURNAL_DIR / "fetch_trades.py")
        result = subprocess.run(
            [python, fetch_script],
            cwd=str(TRADE_JOURNAL_DIR),
            timeout=120,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"status": "error", "step": "fetch_trades",
                    "detail": result.stderr[-500:] if result.stderr else "unknown"}
        # Step 2: rebuild the journal entries from the freshly-written raw file
        try:
            import importlib, sys as _sys
            if str(TRADE_JOURNAL_DIR) not in _sys.path:
                _sys.path.insert(0, str(TRADE_JOURNAL_DIR))
            je = importlib.import_module("journal_entries")
            sync_result = je.sync_entries_from_trades_raw()
        except Exception as e:
            return {"status": "error", "step": "sync_entries", "detail": str(e)}
        return {"status": "ok", "sync": sync_result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    finally:
        state.journal_refreshing = False


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for live quote updates to browser."""
    await websocket.accept()
    state.ws_clients.append(websocket)
    print(f"[WS] Browser connected ({len(state.ws_clients)} clients)")
    try:
        while True:
            # Keep connection alive, handle client pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.ws_clients:
            state.ws_clients.remove(websocket)
        print(f"[WS] Browser disconnected ({len(state.ws_clients)} clients)")


# ── Live JS code injected into the HTML ──

LIVE_JS_SNIPPET = """
// ── Live WebSocket Connection ──
let liveWs = null;
let reconnectTimer = null;
let livePingTimer = null;   // 30s ping;connectLive 每次重连都会重设,先清旧的免累加
let _serverVer = null;

function connectLive() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = proto + '//' + location.host + '/ws';
  liveWs = new WebSocket(url);

  liveWs.onopen = () => {
    document.getElementById('lastUpdate').textContent = 'Connected';
    console.log('[Live] WebSocket connected');
    // Check if server restarted — soft refresh data, keep current tab
    fetch('/api/version').then(r=>r.json()).then(d => {
      if (_serverVer && d.v !== _serverVer) {
        console.log('[Live] Server version changed, refreshing data...');
        softRefresh();
      }
      _serverVer = d.v;
    }).catch(() => {});
  };

  liveWs.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'quote_update') {
      handleQuoteUpdate(msg);
    } else if (msg.type === 'marks_refresh') {
      // Server refreshed option marks (current prices) — refetch and re-render P/L
      fetch('/api/data').then(r=>r.json()).then(d => {
        // Splice only the mark/current fields onto existing legs to preserve bid/ask
        const byUnd = {};
        d.strategies.forEach(s => {
          const k = s.underlying + '|' + s.expiration;
          byUnd[k] = s;
        });
        RAW_DATA.strategies.forEach(s => {
          const k = s.underlying + '|' + s.expiration;
          const fresh = byUnd[k];
          if (!fresh) return;
          s.legs.forEach((leg, i) => {
            const fl = fresh.legs[i];
            if (fl && fl.symbol === leg.symbol) {
              leg.current = fl.current;
              leg.mark = fl.mark;
              leg.last = fl.last;
            }
          });
        });
        const dash = document.getElementById('dashboardView');
        const pbv = document.getElementById('pnlBarsView');
        if (pbv && pbv.style.display !== 'none' && typeof renderPnlBarsView === 'function') renderPnlBarsView();
        else if (dash && dash.style.display !== 'none') renderDashView();
        else { renderSidebar(RAW_DATA.strategies); renderStrategy(); }
      });
    } else if (msg.type === 'full_refresh') {
      // Server refreshed positions — fetch new data in-place, don't navigate
      fetch('/api/data').then(r=>r.json()).then(d => {
        RAW_DATA.strategies = d.strategies;
        RAW_DATA.equities = d.equities;
        RAW_DATA.equity_prev_close = d.equity_prev_close;
        if (d.balance) RAW_DATA.balance = d.balance;
        // Re-render current view
        const dash = document.getElementById('dashboardView');
        const risk = document.getElementById('riskView');
        const pbv = document.getElementById('pnlBarsView');
        if (pbv && pbv.style.display !== 'none' && typeof renderPnlBarsView === 'function') {
          renderPnlBarsView();
        } else if (risk && risk.style.display !== 'none' && typeof renderRiskView === 'function') {
          renderRiskView();
        } else if (dash && dash.style.display !== 'none') {
          renderDashView();
        } else {
          // In detail view — re-render sidebar + current strategy
          renderSidebar(RAW_DATA.strategies);
          renderStrategy();
        }
        console.log('[Live] Positions refreshed: ' + d.strategies.length + ' strategies');
      });
    }
  };

  liveWs.onclose = () => {
    document.getElementById('lastUpdate').textContent = 'Reconnecting...';
    reconnectTimer = setTimeout(connectLive, 3000);
  };

  liveWs.onerror = () => {
    liveWs.close();
  };

  // Ping every 30s。⚠️ connectLive 每次重连都会跑到这, 旧实现每次 setInterval
  // 而从不清 → 断线重连 N 次就有 N 个 ping 定时器叠着打。先清旧的再建。
  if (livePingTimer) clearInterval(livePingTimer);
  livePingTimer = setInterval(() => {
    if (liveWs && liveWs.readyState === WebSocket.OPEN) {
      liveWs.send('ping');
    }
  }, 30000);
}

function renderPositionsLive(timeStr) {
  // Repaint the Positions tab from current RAW_DATA. Called on every DXLink
  // batch while Positions is the visible top-tab, and once by switchTopTab()
  // when the user returns to Positions (so a tab that was skipped while hidden
  // shows fresh numbers immediately). RAW_DATA is kept patched regardless of
  // tab — this only touches the (otherwise hidden) Positions DOM.
  const strategies = RAW_DATA.strategies;
  const prevClose = RAW_DATA.equity_prev_close || {};
  const strat = strategies[state.selectedIdx];
  if (!strat) return;
  if (timeStr) document.getElementById('lastUpdate').textContent = 'Last: ' + timeStr;

  // Update stock price display with change
  const price = strat.current_price;
  const pc = prevClose[strat.underlying] || 0;
  const chg = pc > 0 ? price - pc : 0;
  const chgPct = pc > 0 ? (chg / pc * 100) : 0;
  const chgCls = chg >= 0 ? 'color:var(--green)' : 'color:var(--red)';
  const sign = chg >= 0 ? '+' : '';
  let priceHtml = strat.underlying + ' <strong>$' + price.toFixed(2) + '</strong>';
  if (pc > 0) priceHtml += ` <span style="${chgCls};font-weight:700">${sign}$${chg.toFixed(2)} (${sign}${chgPct.toFixed(2)}%)</span>`;
  document.getElementById('livePrice').innerHTML = priceHtml;

  // Re-render everything including table/graph (throttled)
  const iv = state.ivPct / 100;
  const analysis = analyzeStrategy(strat, iv);
  if (typeof updateCreditSpreadBanner === 'function') updateCreditSpreadBanner(strat);
  renderLegs(strat);
  renderSummary(strat, analysis);
  renderGreeks(analysis);
  updateSidebarPnl();

  // Throttled table/graph refresh (every 1 second max)
  const now = Date.now();
  if (!window._lastFullRender || now - window._lastFullRender > 1000) {
    window._lastFullRender = now;
    if (state.view === 'table') renderTable(strat, analysis, iv);
    else renderGraph(strat, analysis, iv);
  }

  // Throttled viz refresh — keep current-price markers + status moving when
  // the spread visualization is the active dashboard view.
  const dashEl = document.getElementById('dashboardView');
  if (dashEl && dashEl.style.display !== 'none' && typeof dashViewMode !== 'undefined' && dashViewMode === 'viz') {
    if (!window._lastVizRender || now - window._lastVizRender > 1500) {
      window._lastVizRender = now;
      if (typeof renderDashViz === 'function') renderDashViz(strategies);
    }
  }
}

function handleQuoteUpdate(msg) {
  const strategies = RAW_DATA.strategies;
  const prevClose = RAW_DATA.equity_prev_close || {};
  let changed = false;
  const scrUpdates = [];  // {ticker, price, change_pct} to apply to screener cells

  for (const upd of msg.updates) {
    if (upd.is_equity) {
      // Update underlying price in all matching strategies
      for (const strat of strategies) {
        if (strat.underlying === upd.symbol) {
          strat.current_price = upd.mid;
          changed = true;
        }
      }
      // Also update screener row if matches
      if (scrData && scrData.signals) {
        const row = scrData.signals.find(s => s.ticker === upd.symbol);
        if (row && upd.mid > 0) {
          row.price = upd.mid;
          const pc = prevClose[upd.symbol];
          if (pc && pc > 0) {
            row.change_pct = ((upd.mid - pc) / pc) * 100;
          }
          scrUpdates.push({ticker: upd.symbol, price: row.price, change_pct: row.change_pct});
        }
      }
    } else {
      // Find matching option legs. Update bid/ask/mid for display, and drive
      // leg.current (→ unrealized P/L) from the DXLink mid when we have a
      // reliable two-sided quote. REST mark refresh (every 30s) reconciles —
      // when bid=0 (illiquid / pre-post market) we leave leg.current alone so
      // the saved REST mark stays the source of truth.
      for (const strat of strategies) {
        for (const leg of strat.legs) {
          const streamerSym = leg.streamer_symbol || occToStreamer(leg.symbol);
          if (streamerSym === upd.symbol) {
            leg.bid = upd.bid;
            leg.ask = upd.ask;
            leg.mid = upd.mid;
            if (upd.bid > 0 && upd.ask > 0 && upd.mid > 0) {
              leg.current = upd.mid;
            }
            changed = true;
          }
        }
      }
    }
  }

  if (changed) {
    // Alerts must fire regardless of which top-tab is visible — keep this
    // outside the Positions gate below.
    if (typeof checkAlerts === 'function') checkAlerts(strategies);

    // The Positions DOM (live bar, legs, summary, greeks, sidebar, table/graph,
    // dashboard viz) sits inside the hidden #tab-positions panel whenever another
    // top-tab (Journal/Screener/…) is active. Re-pricing the full ±10% BSM matrix
    // and rewriting that off-screen DOM every second is pure waste and steals the
    // main thread from the active tab (e.g. Journal's 11 Chart.js charts) — the
    // single biggest source of UI jank. RAW_DATA was already patched above, so the
    // data stays fresh; switchTopTab('positions') repaints once on return.
    if (currentTopTab === 'positions') {
      renderPositionsLive(new Date(msg.time).toLocaleTimeString());
    }
  }

  // Apply screener cell updates (patch only the 2 cells per changed row,
  // preserve sort/scroll/selection). Works even when Positions tab is active.
  if (scrUpdates.length && document.getElementById('scrBody')) {
    updateScreenerCells(scrUpdates);
  }
}

function updateScreenerCells(updates) {
  const tbody = document.getElementById('scrBody');
  if (!tbody) return;
  for (const u of updates) {
    const row = tbody.querySelector('tr[data-ticker="' + u.ticker + '"]');
    if (!row) continue;
    const priceTd = row.querySelector('.scr-price-cell');
    const chgTd = row.querySelector('.scr-chg-cell');
    if (priceTd) priceTd.textContent = '$' + (u.price || 0).toFixed(2);
    if (chgTd && u.change_pct !== undefined) {
      const cp = u.change_pct;
      chgTd.textContent = (cp >= 0 ? '+' : '') + cp.toFixed(2) + '%';
      chgTd.className = 'scr-chg ' + (cp >= 0 ? 'pos' : 'neg') + ' scr-chg-cell';
    }
  }
}

function updateSidebarPnl() {
  // Resolve each row's strategy via dataset.idx — the sidebar is rendered in
  // dashSort order (see renderSidebar), NOT RAW_DATA order. Positional
  // items[i] <-> strategies[i] wrote P/L into the wrong rows whenever the sort
  // wasn't natural order: live (~1s) updates showed mismatched values while the
  // 30s/60s renderSidebar restored the correct ones → the P/L kept flickering.
  // Format must match renderSidebar EXACTLY so the two paths never differ.
  document.querySelectorAll('.strat-item').forEach((item) => {
    const idx = parseInt(item.dataset.idx, 10);
    const s = RAW_DATA.strategies[idx];
    if (!s) return;
    const a = analyzeStrategy(s);
    const pnl = a.unrealizedPnl, pct = a.unrealizedPct;
    const pnlEl = item.querySelector('.strat-pnl');
    if (pnlEl) {
      pnlEl.className = 'strat-pnl ' + (pnl >= 0 ? 'pos' : 'neg');
      pnlEl.textContent = (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl).toFixed(0) +
        ' (' + (pct >= 0 ? '+' : '-') + Math.abs(pct).toFixed(1) + '%)';
    }
  });
}

function occToStreamer(occ) {
  // Convert OCC "AAPL  261218C00200000" -> ".AAPL261218C200"
  const parts = occ.trim().split(/\\s+/);
  if (parts.length < 2) return '';
  const underlying = parts[0];
  const rest = parts[parts.length - 1];
  let optType = '', datePart = '', strikeRaw = '';
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === 'C' || rest[i] === 'P') {
      datePart = rest.substring(0, i);
      optType = rest[i];
      strikeRaw = rest.substring(i + 1);
      break;
    }
  }
  if (!optType) return '';
  const strikeVal = parseFloat(strikeRaw) / 1000;
  // Format: remove trailing zeros
  let strikeStr = strikeVal % 1 === 0 ? strikeVal.toFixed(0) : strikeVal.toString();
  return '.' + underlying + datePart + optType + strikeStr;
}

// Auto-connect on load
setTimeout(connectLive, 500);
"""


# Dynamic sw.js with version stamp — forces browser to detect update on server restart
_server_version = str(int(time.time()))

# ── Batch Earnings Dates (for Screener table) ──
_earn_date_cache: dict = {}  # ticker -> {date, time, ts}


def _fetch_single_earnings_date(ticker: str) -> dict:
    """Fetch next earnings date + BM/AM for a single ticker. Thread-safe."""
    # Check cache (24h)
    if ticker in _earn_date_cache and time.time() - _earn_date_cache[ticker]["ts"] < 86400:
        return {"ticker": ticker, "date": _earn_date_cache[ticker]["date"],
                "time": _earn_date_cache[ticker]["time"]}
    result = {"ticker": ticker, "date": None, "time": None}
    try:
        t = yf.Ticker(ticker)
        info = t.info
        ets = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ets:
            from datetime import timezone, timedelta
            et_tz = timezone(timedelta(hours=-4))
            earn_dt = datetime.datetime.fromtimestamp(ets, tz=et_tz)
            result["date"] = earn_dt.strftime("%Y-%m-%d")
            if earn_dt.hour < 12:
                result["time"] = "BMO"
            elif earn_dt.hour >= 16:
                result["time"] = "AMC"
        if not result["date"]:
            # Fallback: try calendar
            cal = t.calendar
            if cal and isinstance(cal, dict) and "Earnings Date" in cal:
                edl = cal["Earnings Date"]
                if edl and len(edl) > 0:
                    result["date"] = str(edl[0])
    except Exception:
        pass
    _earn_date_cache[ticker] = {"date": result["date"], "time": result["time"], "ts": time.time()}
    return result


@app.post("/api/batch-earnings-dates")
async def batch_earnings_dates(request: Request):
    """Return earnings dates for the requested tickers.

    Earnings are now baked into the screener JSON via TastyTrade /market-metrics
    (see screener_runner.py). Serve from that cache first; fall back to yfinance
    only for tickers that somehow aren't in the cache.
    """
    body = await request.json()
    tickers = body.get("tickers", [])
    if not tickers:
        return {"earnings": {}}

    earnings: dict = {}
    today = datetime.datetime.now().date()

    # 1. Serve from screener cache (TastyTrade data)
    screener_by_ticker = {
        s.get("ticker"): s for s in state.screener_data.get("signals", [])
    }
    missing: list[str] = []
    for t in tickers:
        s = screener_by_ticker.get(t)
        ed = s.get("earnings_date") if s else None
        if ed:
            try:
                dt = datetime.date.fromisoformat(ed)
                earnings[t] = {
                    "date": ed,
                    "time": s.get("earnings_time") or None,
                    "days": (dt - today).days,
                }
            except Exception:
                missing.append(t)
        else:
            missing.append(t)

    # 2. Fallback: yfinance for any gaps (usually empty)
    if missing and HAS_YF:
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_event_loop()
        BATCH = 10
        results = []
        for i in range(0, len(missing), BATCH):
            batch = missing[i:i + BATCH]
            with ThreadPoolExecutor(max_workers=BATCH) as pool:
                futures = [loop.run_in_executor(pool, _fetch_single_earnings_date, t) for t in batch]
                batch_results = await asyncio.gather(*futures, return_exceptions=True)
                results.extend(batch_results)
            if i + BATCH < len(missing):
                await asyncio.sleep(1)
        for r in results:
            if isinstance(r, dict) and r.get("date"):
                try:
                    dt = datetime.date.fromisoformat(r["date"])
                    earnings[r["ticker"]] = {
                        "date": r["date"],
                        "time": r.get("time"),
                        "days": (dt - today).days,
                    }
                except Exception:
                    pass
    return {"earnings": earnings}


# ── Earnings History ──
_earnings_cache: dict = {}  # ticker -> {data, ts}

@app.get("/api/earnings-history/{ticker}")
async def api_earnings_history(ticker: str):
    ticker = ticker.upper()

    # Cache for 24h per ticker
    if ticker in _earnings_cache and time.time() - _earnings_cache[ticker]["ts"] < 86400:
        return _earnings_cache[ticker]["data"]

    if not HAS_YF:
        return {"error": "yfinance not installed"}

    try:
        import pandas as pd
        t = yf.Ticker(ticker)

        # Get earnings dates (last 5 years ≈ 20 quarters)
        ed = t.get_earnings_dates(limit=24)
        if ed is None or ed.empty:
            return {"ticker": ticker, "rows": [], "down_prob": None}

        # Get 6y of price history to cover lookback
        hist = t.history(period="6y", interval="1d")
        if hist.empty:
            return {"ticker": ticker, "rows": [], "down_prob": None}

        rows = []
        cnt_next = 0
        cnt_down = 0

        for earn_dt in ed.index:
            earn_date = earn_dt.date() if hasattr(earn_dt, 'date') else pd.Timestamp(earn_dt).date()

            # Skip future earnings
            if earn_date > datetime.datetime.now().date():
                continue

            # Get EPS data
            eps_est = ed.loc[earn_dt, 'EPS Estimate']
            eps_act = ed.loc[earn_dt, 'Reported EPS']
            surprise = ed.loc[earn_dt, 'Surprise(%)']

            eps_est = float(eps_est) if pd.notna(eps_est) else None
            eps_act = float(eps_act) if pd.notna(eps_act) else None
            surprise = float(surprise) if pd.notna(surprise) else None

            # Find this date in price history
            mask = hist.index.date == earn_date
            if not mask.any():
                # Try next trading day (earnings might have been on weekend/holiday)
                for offset in range(1, 4):
                    shifted = earn_date + pd.Timedelta(days=offset)
                    mask = hist.index.date == shifted
                    if mask.any():
                        earn_date = shifted
                        break
                if not mask.any():
                    continue

            idx = hist.index.get_loc(hist.index[mask][0])
            if idx < 1:
                continue

            prev_close = float(hist.iloc[idx - 1]['Close'])
            day_close = float(hist.iloc[idx]['Close'])
            day_pct = (day_close - prev_close) / prev_close * 100 if prev_close > 0 else 0

            next_pct = None
            if idx + 1 < len(hist):
                next_close = float(hist.iloc[idx + 1]['Close'])
                next_pct = (next_close - day_close) / day_close * 100 if day_close > 0 else 0
                cnt_next += 1
                if next_pct < 0:
                    cnt_down += 1

            rows.append({
                "date": str(earn_date),
                "day_pct": round(day_pct, 2),
                "next_pct": round(next_pct, 2) if next_pct is not None else None,
                "eps_est": round(eps_est, 2) if eps_est is not None else None,
                "eps_act": round(eps_act, 2) if eps_act is not None else None,
                "surprise": round(surprise, 2) if surprise is not None else None,
            })

        down_prob = round(cnt_down / cnt_next * 100, 1) if cnt_next > 0 else None

        # Compute averages
        day_pcts = [r["day_pct"] for r in rows]
        next_pcts = [r["next_pct"] for r in rows if r["next_pct"] is not None]
        avg_day = round(sum(day_pcts) / len(day_pcts), 2) if day_pcts else None
        avg_next = round(sum(next_pcts) / len(next_pcts), 2) if next_pcts else None

        result = {
            "ticker": ticker,
            "rows": rows,
            "down_prob": down_prob,
            "down_count": cnt_down,
            "total_count": cnt_next,
            "avg_day_pct": avg_day,
            "avg_next_pct": avg_next,
        }
        _earnings_cache[ticker] = {"data": result, "ts": time.time()}
        return result

    except Exception as e:
        return {"error": str(e), "ticker": ticker}


# ── Ticker Analysis (TTM/ATR/ADX + Correlations + Pivots) ──
_analysis_cache: dict = {}  # ticker -> {data, ts}

_ETF_LIST = ["SPY","QQQ","XLV","XLU","XLP","XLK","XLI","XLE","XLB",
             "XHB","XBI","XRT","XOP","XLF","IWM","SMH","IBIT"]

def _compute_ticker_analysis(ticker: str) -> dict:
    import numpy as np
    import pandas as pd

    t = yf.Ticker(ticker)
    hist = t.history(period="1y", interval="1d")
    if hist.empty or len(hist) < 30:
        return {"error": "Insufficient price data", "ticker": ticker}

    c = hist["Close"].values.astype(float)
    h = hist["High"].values.astype(float)
    lo = hist["Low"].values.astype(float)
    n = len(c)

    # ── TTM Squeeze ──
    L = 20
    if n >= L:
        sma20 = pd.Series(c).rolling(L).mean().values
        std20 = pd.Series(c).rolling(L).std().values
        ema20 = pd.Series(c).ewm(span=L, adjust=False).mean().values
        tr = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
        tr = np.concatenate([[h[0] - lo[0]], tr])
        kc_range = pd.Series(tr).ewm(span=L, adjust=False).mean().values

        bb_top = sma20 + 2.0 * std20
        bb_bot = sma20 - 2.0 * std20
        kc_top = ema20 + 1.5 * kc_range
        kc_bot = ema20 - 1.5 * kc_range

        sq_on = bool(bb_top[-1] < kc_top[-1] and bb_bot[-1] > kc_bot[-1])
        sq_off = bool(bb_top[-1] > kc_top[-1] and bb_bot[-1] < kc_bot[-1])

        # Momentum oscillator
        hh = pd.Series(h).rolling(L).max().values
        ll = pd.Series(lo).rolling(L).min().values
        e1 = (hh + ll) / 2.0 + sma20
        osc_src = c - e1 / 2.0
        # Linear regression of last L values
        from numpy.polynomial.polynomial import polyfit
        x_lr = np.arange(L, dtype=float)
        osc_val = float(polyfit(x_lr, osc_src[-L:], 1)[1] * (L - 1) + polyfit(x_lr, osc_src[-L:], 1)[0])
        # Simpler: just use last value
        osc_curr = float(osc_src[-1]) if not np.isnan(osc_src[-1]) else 0
        osc_prev = float(osc_src[-2]) if len(osc_src) > 1 and not np.isnan(osc_src[-2]) else 0

        # Count bars in current state
        bars_in_sq = 0
        for i in range(n - 1, max(L - 1, 0), -1):
            this_on = bb_top[i] < kc_top[i] and bb_bot[i] > kc_bot[i]
            if this_on == sq_on:
                bars_in_sq += 1
            else:
                break

        squeeze = {
            "state": "ON" if sq_on else ("OFF" if sq_off else "NEUTRAL"),
            "momentum": round(osc_curr, 3),
            "mom_dir": "up" if osc_curr > osc_prev else "down",
            "mom_bull": osc_curr > 0,
            "bars_in_state": bars_in_sq,
        }
    else:
        squeeze = {"state": "N/A", "momentum": 0, "mom_dir": "flat", "mom_bull": False, "bars_in_state": 0}

    # ── ATR ──
    atr_len = 14
    tr_arr = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
    tr_arr = np.concatenate([[h[0] - lo[0]], tr_arr])
    atr_s = pd.Series(tr_arr).ewm(span=atr_len, adjust=False).mean()
    atr_val = float(atr_s.iloc[-1])
    median_atr = float(atr_s.tail(20).median())
    atr_pct = atr_val / c[-1] * 100 if c[-1] > 0 else 0
    if atr_val > median_atr * 1.2:
        vol_zone = "High"
    elif atr_val < median_atr * 0.8:
        vol_zone = "Low"
    else:
        vol_zone = "Normal"
    atr_trend = "Rising" if atr_val > float(atr_s.iloc[-2]) else "Falling"
    atr_info = {
        "value": round(atr_val, 2),
        "pct": round(atr_pct, 2),
        "zone": vol_zone,
        "trend": atr_trend,
        "breakout": atr_val > median_atr,
    }

    # ── ADX ──
    adx_len = 14
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = lo[i - 1] - lo[i]
        dm_plus[i] = up if (up > dn and up > 0) else 0
        dm_minus[i] = dn if (dn > up and dn > 0) else 0

    sm_tr = np.zeros(n, dtype=float)
    sm_dp = np.zeros(n, dtype=float)
    sm_dm = np.zeros(n, dtype=float)
    sm_tr[adx_len] = tr_arr[:adx_len + 1].sum()
    sm_dp[adx_len] = dm_plus[:adx_len + 1].sum()
    sm_dm[adx_len] = dm_minus[:adx_len + 1].sum()
    for i in range(adx_len + 1, n):
        sm_tr[i] = sm_tr[i - 1] - sm_tr[i - 1] / adx_len + tr_arr[i]
        sm_dp[i] = sm_dp[i - 1] - sm_dp[i - 1] / adx_len + dm_plus[i]
        sm_dm[i] = sm_dm[i - 1] - sm_dm[i - 1] / adx_len + dm_minus[i]

    with np.errstate(divide='ignore', invalid='ignore'):
        di_p = np.where(sm_tr != 0, sm_dp / sm_tr * 100, 0)
        di_m = np.where(sm_tr != 0, sm_dm / sm_tr * 100, 0)
        dx = np.where((di_p + di_m) != 0, np.abs(di_p - di_m) / (di_p + di_m) * 100, 0)

    adx_arr = pd.Series(dx).rolling(adx_len).mean().values
    adx_val = float(adx_arr[-1]) if not np.isnan(adx_arr[-1]) else 0
    di_plus_val = float(di_p[-1])
    di_minus_val = float(di_m[-1])

    if adx_val < 20:
        trend_str = "Consolidation"
    elif adx_val < 25:
        trend_str = "Weak Trend"
    else:
        trend_str = "Strong Trend"

    adx_info = {
        "value": round(adx_val, 1),
        "di_plus": round(di_plus_val, 1),
        "di_minus": round(di_minus_val, 1),
        "trend": trend_str,
        "direction": "Bull" if di_plus_val > di_minus_val else "Bear",
    }

    # ── ETF Correlations ──
    etf_tickers = [ticker] + _ETF_LIST
    try:
        raw = yf.download(etf_tickers, period="4mo", interval="1d", progress=False, threads=True)
        etf_close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw
        log_ret = np.log(etf_close / etf_close.shift(1)).dropna()
        corrs = []
        if ticker in log_ret.columns:
            base = log_ret[ticker]
            for etf in _ETF_LIST:
                if etf in log_ret.columns:
                    c1m = float(base.tail(21).corr(log_ret[etf].tail(21)))
                    c3m = float(base.tail(63).corr(log_ret[etf].tail(63)))
                    corrs.append({"etf": etf,
                                  "c1m": round(c1m, 3) if not np.isnan(c1m) else None,
                                  "c3m": round(c3m, 3) if not np.isnan(c3m) else None})
                else:
                    corrs.append({"etf": etf, "c1m": None, "c3m": None})
        else:
            corrs = [{"etf": e, "c1m": None, "c3m": None} for e in _ETF_LIST]
    except Exception:
        corrs = [{"etf": e, "c1m": None, "c3m": None} for e in _ETF_LIST]

    # ── Pivot Rise / Pullback Stats ──
    left = 5
    right = 5
    pivots_h = []  # (bar_idx, price)
    pivots_l = []
    for i in range(left, n - right):
        if h[i] == max(h[i - left:i + right + 1]):
            pivots_h.append((i, float(h[i])))
        if lo[i] == min(lo[i - left:i + right + 1]):
            pivots_l.append((i, float(lo[i])))

    # Merge and sort all pivots
    all_pivots = [(b, p, "H") for b, p in pivots_h] + [(b, p, "L") for b, p in pivots_l]
    all_pivots.sort(key=lambda x: x[0])

    rises = []  # (bars, pct)
    pulls = []
    last_low = None
    last_high = None
    for bar, price, ptype in all_pivots:
        if ptype == "H":
            if last_low is not None:
                rbars = bar - last_low[0]
                rpct = (price - last_low[1]) / last_low[1] * 100 if last_low[1] > 0 else 0
                if rbars > 0 and rpct > 0:
                    rises.append((rbars, round(rpct, 2)))
            last_high = (bar, price)
            last_low = None
        else:  # "L"
            if last_high is not None:
                pbars = bar - last_high[0]
                ppct = (last_high[1] - price) / last_high[1] * 100 if last_high[1] > 0 else 0
                if pbars > 0 and ppct > 0:
                    pulls.append((pbars, round(ppct, 2)))
            last_low = (bar, price)
            last_high = None

    def _stats(arr):
        if not arr:
            return {"count": 0, "avg_bars": None, "min_bars": None, "max_bars": None,
                    "avg_pct": None, "min_pct": None, "max_pct": None}
        bars = [a[0] for a in arr]
        pcts = [a[1] for a in arr]
        return {
            "count": len(arr),
            "avg_bars": round(sum(bars) / len(bars), 1),
            "min_bars": min(bars), "max_bars": max(bars),
            "avg_pct": round(sum(pcts) / len(pcts), 2),
            "min_pct": min(pcts), "max_pct": max(pcts),
        }

    # Bucket distribution (5 buckets for bars and pct)
    def _buckets(arr, key_idx, num_buckets=5):
        """Build histogram: list of {lo, hi, rise_pct, pull_pct}."""
        if not rises and not pulls:
            return []
        all_vals = [a[key_idx] for a in rises] + [a[key_idx] for a in pulls]
        if not all_vals:
            return []
        lo, hi = min(all_vals), max(all_vals)
        if lo == hi:
            hi = lo + 1
        width = (hi - lo) / num_buckets
        buckets = []
        for i in range(num_buckets):
            b_lo = lo + i * width
            b_hi = lo + (i + 1) * width if i < num_buckets - 1 else hi + 0.01
            r_cnt = sum(1 for a in rises if b_lo <= a[key_idx] < b_hi)
            p_cnt = sum(1 for a in pulls if b_lo <= a[key_idx] < b_hi)
            r_pct = round(r_cnt / len(rises) * 100, 1) if rises else 0
            p_pct = round(p_cnt / len(pulls) * 100, 1) if pulls else 0
            buckets.append({
                "lo": round(b_lo, 1), "hi": round(b_hi if i < num_buckets - 1 else hi, 1),
                "rise_pct": r_pct, "pull_pct": p_pct,
            })
        return buckets

    bars_dist = _buckets(rises + pulls, 0)  # key_idx=0 = bars
    pct_dist = _buckets(rises + pulls, 1)   # key_idx=1 = pct

    # Bars since last PH/PL, close vs last PH/PL
    last_ph = pivots_h[-1] if pivots_h else None
    last_pl = pivots_l[-1] if pivots_l else None
    pivot_info = {
        "rise": _stats(rises),
        "pull": _stats(pulls),
        "bars_since_ph": n - 1 - last_ph[0] if last_ph else None,
        "bars_since_pl": n - 1 - last_pl[0] if last_pl else None,
        "close_vs_ph_pct": round((c[-1] - last_ph[1]) / last_ph[1] * 100, 2) if last_ph else None,
        "close_vs_pl_pct": round((c[-1] - last_pl[1]) / last_pl[1] * 100, 2) if last_pl else None,
        "bars_dist": bars_dist,
        "pct_dist": pct_dist,
    }

    # ── Next Earnings ──
    next_earn = None
    next_earn_time = None  # BMO / AMC / TNS
    try:
        ed = t.get_earnings_dates(limit=4)
        if ed is not None and not ed.empty:
            today = datetime.datetime.now().date()
            for dt in ed.index:
                d = dt.date() if hasattr(dt, 'date') else pd.Timestamp(dt).date()
                if d >= today:
                    next_earn = str(d)
                    break
        # Get BM/AM from earningsTimestamp in Yahoo info
        try:
            info = t.info
            ets = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if ets:
                from datetime import timezone, timedelta
                et_tz = timezone(timedelta(hours=-4))  # EDT approx
                earn_dt = datetime.datetime.fromtimestamp(ets, tz=et_tz)
                if earn_dt.hour < 12:
                    next_earn_time = 'BMO'
                elif earn_dt.hour >= 16:
                    next_earn_time = 'AMC'
        except Exception:
            pass
    except Exception:
        pass

    return {
        "ticker": ticker,
        "squeeze": squeeze,
        "atr": atr_info,
        "adx": adx_info,
        "correlations": corrs,
        "pivots": pivot_info,
        "next_earnings": next_earn,
        "next_earnings_time": next_earn_time,
        "price": round(float(c[-1]), 2),
    }


def _sanitize_for_json(obj):
    """Recursively convert numpy types and NaN/Inf to JSON-safe values."""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    try:
        import numpy as _np
        if isinstance(obj, (_np.integer,)):
            return int(obj)
        if isinstance(obj, (_np.floating,)):
            v = float(obj)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        if isinstance(obj, _np.ndarray):
            return _sanitize_for_json(obj.tolist())
    except ImportError:
        pass
    return obj


# ── ATR-14 (used by spread visualization to compare against IV-implied 1σ daily move) ──
_atr_cache: dict = {}      # ticker -> {atr, ts}
_ATR_TTL_SEC = 3600        # 1h


def _compute_atr14_batch(tickers: list) -> dict:
    """Fetch ~2mo daily history once via yfinance, then compute ATR-14 in dollars
    per ticker. Returns {ticker: atr_dollars or None}."""
    out: dict = {t: None for t in tickers}
    if not HAS_YF or not tickers:
        return out
    try:
        import pandas as pd
        if len(tickers) == 1:
            hist_all = yf.download(tickers[0], period="2mo", progress=False, auto_adjust=False)
            frames = {tickers[0]: hist_all}
        else:
            hist_all = yf.download(tickers, period="2mo", group_by="ticker",
                                   progress=False, auto_adjust=False, threads=True)
            frames = {}
            for t in tickers:
                try:
                    frames[t] = hist_all[t] if t in hist_all.columns.get_level_values(0) else None
                except Exception:
                    frames[t] = None
        for t, hist in frames.items():
            try:
                if hist is None or hist.empty or len(hist) < 15:
                    continue
                high = hist["High"].astype(float)
                low = hist["Low"].astype(float)
                close = hist["Close"].astype(float)
                prev_close = close.shift(1)
                # True Range = max(H-L, |H-PC|, |L-PC|), then 14d SMA
                tr = pd.concat([
                    high - low,
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr14 = tr.rolling(14).mean().iloc[-1]
                if atr14 == atr14 and atr14 > 0:   # NaN-safe
                    out[t] = round(float(atr14), 4)
            except Exception:
                out[t] = None
    except Exception:
        pass
    return out


# ── Major market-moving events (Tier 1 macro / mega-cap; Tier 2 sector-mover) ──
#
# Macro release dates are hardcoded — Fed/BLS/BEA publish full-year schedules
# in advance and the dates rarely shift. Earnings are fetched live from
# yfinance per ticker. Update HARDCODED_MACRO_EVENTS each January with the new
# year's official calendars (sources cited per group).
#
# Tier 1 = "moves SPY in seconds" (FOMC / CPI / PCE / NFP / GDP / Mag 7 / JPM / NVDA)
# Tier 2 = "moves a sector heavily" (AMD/AVGO/MRVL/MU/SNDK / OPEC / Triple Witching)

EARNINGS_TIER_1 = ["NVDA", "META", "AAPL", "AMZN", "GOOGL", "MSFT", "TSLA", "JPM"]
EARNINGS_TIER_2 = ["AMD", "AVGO", "MRVL", "MU", "SNDK"]

# (date_iso, type, name, tier). Update annually from official sources:
#   FOMC:  federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI:   bls.gov/schedule/news_release/cpi.htm
#   PCE:   bea.gov/news/schedule
#   NFP:   bls.gov/schedule/news_release/empsit.htm
#   GDP:   bea.gov/news/schedule
#   OPEC:  opec.org/opec_web/en/press_room/5294.htm
HARDCODED_MACRO_EVENTS: list = [
    # ── 2026 Macro (verify against sources above each Jan) ──
    ("2026-01-14", "CPI", "CPI Dec 2025", 1),
    ("2026-01-27", "FOMC", "FOMC Day 1", 1),
    ("2026-01-28", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2026-01-30", "GDP", "Q4 2025 GDP advance", 1),
    ("2026-01-30", "PCE", "PCE Dec 2025", 1),
    ("2026-02-06", "NFP", "NFP Jan 2026", 1),
    ("2026-02-13", "CPI", "CPI Jan 2026", 1),
    ("2026-02-27", "PCE", "PCE Jan 2026", 1),
    ("2026-03-06", "NFP", "NFP Feb 2026", 1),
    ("2026-03-12", "CPI", "CPI Feb 2026", 1),
    ("2026-03-17", "FOMC", "FOMC Day 1", 1),
    ("2026-03-18", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2026-03-20", "TripleWit", "Triple Witching Mar", 2),
    ("2026-03-27", "PCE", "PCE Feb 2026", 1),
    ("2026-04-03", "NFP", "NFP Mar 2026", 1),
    ("2026-04-14", "CPI", "CPI Mar 2026", 1),
    ("2026-04-28", "FOMC", "FOMC Day 1", 1),
    ("2026-04-29", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2026-04-30", "GDP", "Q1 2026 GDP advance", 1),
    ("2026-04-30", "PCE", "PCE Mar 2026", 1),
    ("2026-05-01", "NFP", "NFP Apr 2026", 1),
    ("2026-05-13", "CPI", "CPI Apr 2026", 1),
    ("2026-05-29", "PCE", "PCE Apr 2026", 1),
    ("2026-06-03", "OPEC", "OPEC+ JMMC meeting (tentative)", 2),
    ("2026-06-05", "NFP", "NFP May 2026", 1),
    ("2026-06-10", "CPI", "CPI May 2026", 1),
    ("2026-06-16", "FOMC", "FOMC Day 1", 1),
    ("2026-06-17", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2026-06-19", "TripleWit", "Triple Witching Jun", 2),
    ("2026-06-26", "PCE", "PCE May 2026", 1),
    ("2026-07-02", "NFP", "NFP Jun 2026", 1),
    ("2026-07-15", "CPI", "CPI Jun 2026", 1),
    ("2026-07-28", "FOMC", "FOMC Day 1", 1),
    ("2026-07-29", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2026-07-30", "GDP", "Q2 2026 GDP advance", 1),
    ("2026-07-31", "PCE", "PCE Jun 2026", 1),
    # ── 2026 H2 Macro ──
    ("2026-08-07", "NFP", "NFP Jul 2026", 1),
    ("2026-08-12", "CPI", "CPI Jul 2026", 1),
    ("2026-08-28", "PCE", "PCE Jul 2026", 1),
    ("2026-09-04", "NFP", "NFP Aug 2026", 1),
    ("2026-09-11", "CPI", "CPI Aug 2026", 1),
    ("2026-09-15", "FOMC", "FOMC Day 1", 1),
    ("2026-09-16", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2026-09-18", "TripleWit", "Triple Witching Sep", 2),
    ("2026-09-25", "PCE", "PCE Aug 2026", 1),
    ("2026-10-02", "NFP", "NFP Sep 2026", 1),
    ("2026-10-14", "CPI", "CPI Sep 2026", 1),
    ("2026-10-29", "GDP", "Q3 2026 GDP advance", 1),
    ("2026-10-30", "PCE", "PCE Sep 2026", 1),
    ("2026-11-04", "FOMC", "FOMC Day 1", 1),
    ("2026-11-05", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2026-11-06", "NFP", "NFP Oct 2026", 1),
    ("2026-11-12", "CPI", "CPI Oct 2026", 1),
    ("2026-11-25", "PCE", "PCE Oct 2026", 1),
    ("2026-12-04", "NFP", "NFP Nov 2026", 1),
    ("2026-12-10", "CPI", "CPI Nov 2026", 1),
    ("2026-12-15", "FOMC", "FOMC Day 1", 1),
    ("2026-12-16", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2026-12-18", "TripleWit", "Triple Witching Dec", 2),
    ("2026-12-23", "PCE", "PCE Nov 2026", 1),
    # ── 2027 Macro (FOMC from federalreserve.gov; CPI/NFP/PCE/GDP estimated) ──
    ("2027-01-08", "NFP", "NFP Dec 2026", 1),
    ("2027-01-13", "CPI", "CPI Dec 2026", 1),
    ("2027-01-26", "FOMC", "FOMC Day 1", 1),
    ("2027-01-27", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2027-01-28", "GDP", "Q4 2026 GDP advance", 1),
    ("2027-01-29", "PCE", "PCE Dec 2026", 1),
    ("2027-02-05", "NFP", "NFP Jan 2027", 1),
    ("2027-02-10", "CPI", "CPI Jan 2027", 1),
    ("2027-02-26", "PCE", "PCE Jan 2027", 1),
    ("2027-03-05", "NFP", "NFP Feb 2027", 1),
    ("2027-03-10", "CPI", "CPI Feb 2027", 1),
    ("2027-03-16", "FOMC", "FOMC Day 1", 1),
    ("2027-03-17", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2027-03-19", "TripleWit", "Triple Witching Mar", 2),
    ("2027-03-26", "PCE", "PCE Feb 2027", 1),
    ("2027-04-02", "NFP", "NFP Mar 2027", 1),
    ("2027-04-14", "CPI", "CPI Mar 2027", 1),
    ("2027-04-29", "GDP", "Q1 2027 GDP advance", 1),
    ("2027-04-30", "PCE", "PCE Mar 2027", 1),
    ("2027-05-04", "FOMC", "FOMC Day 1", 1),
    ("2027-05-05", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2027-05-07", "NFP", "NFP Apr 2027", 1),
    ("2027-05-12", "CPI", "CPI Apr 2027", 1),
    ("2027-05-28", "PCE", "PCE Apr 2027", 1),
    ("2027-06-04", "NFP", "NFP May 2027", 1),
    ("2027-06-10", "CPI", "CPI May 2027", 1),
    ("2027-06-15", "FOMC", "FOMC Day 1", 1),
    ("2027-06-16", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2027-06-18", "TripleWit", "Triple Witching Jun", 2),
    ("2027-06-25", "PCE", "PCE May 2027", 1),
    ("2027-07-02", "NFP", "NFP Jun 2027", 1),
    ("2027-07-14", "CPI", "CPI Jun 2027", 1),
    ("2027-07-27", "FOMC", "FOMC Day 1", 1),
    ("2027-07-28", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2027-07-29", "GDP", "Q2 2027 GDP advance", 1),
    ("2027-07-30", "PCE", "PCE Jun 2027", 1),
    ("2027-08-06", "NFP", "NFP Jul 2027", 1),
    ("2027-08-11", "CPI", "CPI Jul 2027", 1),
    ("2027-08-27", "PCE", "PCE Jul 2027", 1),
    ("2027-09-03", "NFP", "NFP Aug 2027", 1),
    ("2027-09-15", "CPI", "CPI Aug 2027", 1),
    ("2027-09-17", "TripleWit", "Triple Witching Sep", 2),
    ("2027-09-21", "FOMC", "FOMC Day 1", 1),
    ("2027-09-22", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2027-09-30", "PCE", "PCE Aug 2027", 1),
    ("2027-10-08", "NFP", "NFP Sep 2027", 1),
    ("2027-10-13", "CPI", "CPI Sep 2027", 1),
    ("2027-10-28", "GDP", "Q3 2027 GDP advance", 1),
    ("2027-10-29", "PCE", "PCE Sep 2027", 1),
    ("2027-11-02", "FOMC", "FOMC Day 1", 1),
    ("2027-11-03", "FOMC", "FOMC Day 2 + Powell presser", 1),
    ("2027-11-05", "NFP", "NFP Oct 2027", 1),
    ("2027-11-10", "CPI", "CPI Oct 2027", 1),
    ("2027-11-24", "PCE", "PCE Oct 2027", 1),
    ("2027-12-03", "NFP", "NFP Nov 2027", 1),
    ("2027-12-10", "CPI", "CPI Nov 2027", 1),
    ("2027-12-14", "FOMC", "FOMC Day 1", 1),
    ("2027-12-15", "FOMC", "FOMC Day 2 + dot plot + Powell presser", 1),
    ("2027-12-17", "TripleWit", "Triple Witching Dec", 2),
    ("2027-12-23", "PCE", "PCE Nov 2027", 1),
]

# Fetch a single ticker's next earnings date via yfinance — return ISO date or None.
def _next_earnings_date(ticker: str):
    if not HAS_YF:
        return None
    try:
        cal = yf.Ticker(ticker).calendar
        if not isinstance(cal, dict):
            return None
        ed = cal.get("Earnings Date")
        if not ed:
            return None
        # ed can be a list of datetime/date objects; pick the first future one
        today = datetime.date.today()
        candidates = []
        for d in (ed if isinstance(ed, list) else [ed]):
            try:
                if hasattr(d, "date"):
                    d = d.date()
                if d >= today:
                    candidates.append(d)
            except Exception:
                continue
        if not candidates:
            return None
        return min(candidates).isoformat()
    except Exception:
        return None


_events_cache: dict = {"data": None, "ts": 0.0}
_EVENTS_TTL_SEC = 1800   # 30 min — earnings rarely move intra-day


@app.get("/api/major-events")
async def api_major_events(days: int = Query(30)):
    """Return major market-moving events for the next `days` days, sorted
    ascending by date. Combines hardcoded macro calendar + live yfinance
    earnings for the curated Tier 1/2 watchlists."""
    now = time.time()
    if _events_cache["data"] and now - _events_cache["ts"] < _EVENTS_TTL_SEC:
        cached = _events_cache["data"]
    else:
        loop = asyncio.get_event_loop()
        # Earnings — fetch in parallel via thread pool
        from concurrent.futures import ThreadPoolExecutor
        watchlist = [(t, 1) for t in EARNINGS_TIER_1] + [(t, 2) for t in EARNINGS_TIER_2]
        earnings_results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(loop.run_in_executor(pool, _next_earnings_date, t), t, tier)
                       for (t, tier) in watchlist]
            for fut, ticker, tier in futures:
                try:
                    d = await fut
                    if d:
                        earnings_results.append({
                            "date": d, "type": "Earnings",
                            "name": f"{ticker} earnings", "ticker": ticker, "tier": tier,
                        })
                except Exception:
                    continue
        macro = [{"date": d, "type": t, "name": n, "tier": tier, "ticker": None}
                 for (d, t, n, tier) in HARDCODED_MACRO_EVENTS]
        cached = sorted(macro + earnings_results, key=lambda e: e["date"])
        _events_cache.update({"data": cached, "ts": now})

    today = datetime.date.today()
    cutoff = today + datetime.timedelta(days=int(days))
    out = []
    for ev in cached:
        try:
            d = datetime.date.fromisoformat(ev["date"])
        except Exception:
            continue
        if today <= d <= cutoff:
            ev2 = dict(ev)
            ev2["days_away"] = (d - today).days
            out.append(ev2)
    return {"events": out, "today": today.isoformat()}


# ── Pearson correlation vs SPY (used by overview view) ──
_corr_cache: dict = {}     # (ticker, window) -> {corr, ts}
_CORR_TTL_SEC = 3600       # 1h
_CORR_WINDOWS = {30, 60, 90, 252}  # supported lookback windows


def _fetch_yf_single_close(ticker: str, period: str):
    """Single-ticker yfinance fetch — no batching, no threads. yf's threaded
    batch mode silently drops random tickers under load; single-ticker calls
    are reliable. Used as fallback when batch mode misses a symbol."""
    if not HAS_YF:
        return None
    try:
        df = yf.download(ticker, period=period, progress=False,
                         auto_adjust=False, threads=False)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        ser = df["Close"].dropna()
        if hasattr(ser, "columns"):  # multi-col edge case
            ser = ser.iloc[:, 0]
        ser.name = ticker
        return ser if len(ser) >= 20 else None
    except Exception:
        return None


def _compute_corr_vs_spy_batch(tickers: list, lookback_days: int = 60) -> dict:
    """Fetch ~3mo daily history once via yfinance (tickers + SPY in one call),
    compute Pearson correlation of daily returns vs SPY. Returns {ticker: corr}.
    Falls back to Stooq for any ticker yfinance silently drops in batch mode
    (common with threaded downloads). SPY's correlation with itself is always
    1.0 — we still include it so the UI bar chart can show a reference."""
    out: dict = {t: None for t in tickers}
    if not tickers:
        return out
    try:
        import pandas as pd
        fetch_tickers = sorted(set(tickers) | {"SPY"})
        period_days = max(lookback_days * 3, 180)
        period = f"{period_days}d"
        cols: dict = {}
        if HAS_YF:
            try:
                if len(fetch_tickers) == 1:
                    spy_only = yf.download("SPY", period=period, progress=False, auto_adjust=False)
                    if spy_only is not None and not spy_only.empty and "Close" in spy_only.columns:
                        cols["SPY"] = spy_only["Close"]
                else:
                    hist_all = yf.download(fetch_tickers, period=period, group_by="ticker",
                                           progress=False, auto_adjust=False, threads=True)
                    for t in fetch_tickers:
                        try:
                            if t in hist_all.columns.get_level_values(0):
                                ser = hist_all[t]["Close"].dropna()
                                if len(ser) >= 20:
                                    cols[t] = ser
                        except Exception:
                            continue
            except Exception:
                pass
        # Single-ticker yf fallback for whatever batch missed (including SPY).
        # yf's threaded batch silently drops symbols under load; serial single
        # calls are reliable.
        for t in fetch_tickers:
            if t in cols:
                continue
            ser = _fetch_yf_single_close(t, period=period)
            if ser is not None:
                cols[t] = ser
        if "SPY" not in cols:
            return out
        close_df = pd.DataFrame(cols)
        returns = close_df.pct_change().dropna(how="all")
        spy_ret = returns["SPY"]
        for t in tickers:
            if t not in returns.columns:
                continue
            try:
                joined = pd.concat([returns[t], spy_ret], axis=1).dropna()
                # Use exactly the last `lookback_days` trading days so 30/90/252
                # actually produce different correlations.
                joined = joined.tail(lookback_days)
                if len(joined) >= 20:
                    corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
                    if corr == corr:
                        out[t] = round(float(corr), 3)
            except Exception:
                continue
    except Exception:
        pass
    return out


@app.get("/api/correlation")
async def api_correlation(symbols: str = Query(""), window: int = Query(60)):
    """Return Pearson correlation of each requested symbol vs SPY over the
    last `window` trading days. Cached 1h per (ticker, window). Used by the
    dashboard Overview view's correlation bar chart with 30/90/252 toggle."""
    if window not in _CORR_WINDOWS:
        window = 60
    syms = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    if not syms:
        return {"corr": {}, "window": window}
    out: dict = {}
    now = time.time()
    missing: list = []
    for s in syms:
        c = _corr_cache.get((s, window))
        if c and now - c["ts"] < _CORR_TTL_SEC:
            out[s] = c["corr"]
        else:
            missing.append(s)
    if missing:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, _compute_corr_vs_spy_batch, missing, window
        )
        for s in missing:
            corr = results.get(s)
            # Only cache successful results — caching None would stick a transient
            # yfinance batch hiccup for the full TTL and surface as N/A in the UI.
            if corr is not None:
                _corr_cache[(s, window)] = {"corr": corr, "ts": now}
            out[s] = corr
    return {"corr": out, "window": window}


# ── Pairwise correlation matrix among held tickers (NOT vs SPY) ──
# Surfaces hidden concentration: NVDA+AMD+AVGO long all read as separate rows
# in the dashboard, but a 0.8+ pairwise correlation means they're effectively
# one bet. Reuses the same yfinance batch + single-ticker fallback as the SPY
# helper above.
_corr_matrix_cache: dict = {}   # (tuple(sorted tickers), window) -> {matrix, ts}


def _compute_corr_matrix_batch(tickers: list, lookback_days: int = 90) -> dict:
    """Pearson correlation of daily returns for every pair of `tickers`.
    Returns {a: {b: corr}}. Empty/partial on data problems (never raises)."""
    out: dict = {t: {} for t in tickers}
    if len(tickers) < 2:
        return out
    try:
        import pandas as pd
        fetch_tickers = sorted(set(tickers))
        period_days = max(lookback_days * 3, 180)
        period = f"{period_days}d"
        cols: dict = {}
        if HAS_YF:
            try:
                hist_all = yf.download(fetch_tickers, period=period, group_by="ticker",
                                       progress=False, auto_adjust=False, threads=True)
                for t in fetch_tickers:
                    try:
                        if len(fetch_tickers) == 1:
                            ser = hist_all["Close"].dropna()
                        elif t in hist_all.columns.get_level_values(0):
                            ser = hist_all[t]["Close"].dropna()
                        else:
                            continue
                        if hasattr(ser, "columns"):
                            ser = ser.iloc[:, 0]
                        if len(ser) >= 20:
                            cols[t] = ser
                    except Exception:
                        continue
            except Exception:
                pass
        # Serial single-ticker fallback for whatever the threaded batch dropped.
        for t in fetch_tickers:
            if t in cols:
                continue
            ser = _fetch_yf_single_close(t, period=period)
            if ser is not None:
                cols[t] = ser
        # Guard against yfinance batch flakiness occasionally returning an
        # identical series for two distinct tickers (→ spurious 1.00 and a whole
        # mirrored row). Drop any ticker whose recent-close signature collides
        # with another; they show as n/a and refetch next cache cycle.
        sigs: dict = {}
        aliased = set()
        for t, ser in cols.items():
            s = ser.dropna()
            if len(s) < 20:
                continue
            sig = (round(float(s.iloc[-1]), 4), round(float(s.iloc[-10]), 4),
                   round(float(s.iloc[-20]), 4))
            if sig in sigs:
                aliased.add(t)
                aliased.add(sigs[sig])
            else:
                sigs[sig] = t
        for t in aliased:
            cols.pop(t, None)
        if len(cols) < 2:
            return out
        close_df = pd.DataFrame(cols)
        returns = close_df.pct_change().dropna(how="all").tail(lookback_days)
        # min_periods: a pair needs >=20 overlapping returns, else n/a — a few
        # common points can fabricate a near-perfect correlation.
        corr_df = returns.corr(min_periods=20)
        for a in tickers:
            for b in tickers:
                try:
                    if a in corr_df.index and b in corr_df.columns:
                        v = corr_df.loc[a, b]
                        if v != v:  # NaN
                            continue
                        # A perfect off-diagonal correlation between distinct
                        # tickers is always an artifact — drop it.
                        if a != b and abs(float(v)) >= 0.999:
                            continue
                        out[a][b] = round(float(v), 3)
                except Exception:
                    continue
    except Exception:
        pass
    return out


@app.get("/api/correlation-matrix")
async def api_correlation_matrix(symbols: str = Query(""), window: int = Query(90)):
    """Pairwise Pearson correlation matrix among the requested tickers over the
    last `window` trading days. Cached 1h per (ticker-set, window). Powers the
    Overview tab's Holdings Correlation heatmap."""
    if window not in _CORR_WINDOWS:
        window = 90
    syms = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    if len(syms) < 2:
        return {"matrix": {}, "symbols": syms, "window": window}
    key = (tuple(syms), window)
    now = time.time()
    c = _corr_matrix_cache.get(key)
    if c and now - c["ts"] < _CORR_TTL_SEC:
        return {"matrix": c["matrix"], "symbols": syms, "window": window}
    loop = asyncio.get_event_loop()
    matrix = await loop.run_in_executor(None, _compute_corr_matrix_batch, syms, window)
    _corr_matrix_cache[key] = {"matrix": matrix, "ts": now}
    return {"matrix": matrix, "symbols": syms, "window": window}


# ── Alert config sync (PWA → disk) so the server-side exit watcher knows the
# user's thresholds even when no browser is open. The PWA POSTs on every save. ──
_ALERT_CFG_PATH = Path(__file__).parent / "data" / "alert_config.json"
_AI_PAUSE_PATH = Path(__file__).parent / "data" / "ai_paused"


@app.get("/api/ai-pause")
async def get_ai_pause():
    return {"paused": _AI_PAUSE_PATH.exists()}


@app.post("/api/ai-pause")
async def set_ai_pause(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    paused = bool(body.get("paused", False))
    if paused:
        _AI_PAUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AI_PAUSE_PATH.write_text(str(int(time.time())))
    else:
        _AI_PAUSE_PATH.unlink(missing_ok=True)
    return {"ok": True, "paused": paused}


@app.get("/api/alert-config")
async def get_alert_config():
    try:
        if _ALERT_CFG_PATH.exists():
            return json.loads(_ALERT_CFG_PATH.read_text())
    except Exception:
        pass
    return {"enabled": True,
            "exit": {"enabled": True, "bullPct": 70, "chopPct": 50, "neutralPct": 65},
            "creditSpread": {"enabled": True}}


@app.post("/api/alert-config")
async def set_alert_config(req: Request):
    try:
        body = await req.json()
        _ALERT_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_CFG_PATH.write_text(json.dumps(body, indent=2))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/power/live")
async def get_power_live():
    """当前电源档位 + 实时流状态,供头部药丸显示。"""
    mode = _power_mode.read_mode()
    return {
        "mode": mode,                       # full | lite | off
        "live_on": _live_on(),              # DXLink 实时流本拍是否开
        "override_on": state.live_override,  # lite 档下作者是否手动开着
        "idle_secs": int(_power_mode.IDLE_SECS),
    }


@app.post("/api/power/live")
async def set_power_live(req: Request):
    """lite 档下手动开/关 DXLink 实时流。full 档恒开(无操作);off 档进程已被 guard 杀。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    on = bool(body.get("on", False))
    state.live_override = on
    if on:
        state.last_request_ts = time.time()  # 开时重置 idle 计时
    return {"ok": True, "mode": _power_mode.read_mode(),
            "live_on": _live_on(), "override_on": state.live_override}


@app.post("/api/notify-native")
async def notify_native(req: Request):
    """前端告警 → 系统级(osascript)macOS 通知。比浏览器 new Notification() 可靠:
    不受浏览器 macOS 通知开关 / 标签聚焦抑制影响,与后台推送同一通道(继承 Terminal TCC)。"""
    if _notifications_muted():
        return {"ok": True, "muted": True}
    try:
        body = await req.json()
        import importlib, sys as _s
        from pathlib import Path as _P
        _ck = str(_P(__file__).resolve().parent)
        if _ck not in _s.path:
            _s.path.insert(0, _ck)
        eap = importlib.import_module("exit_alert_push")
        await eap.send_macos_notification(
            str(body.get("title", "提醒"))[:120],
            str(body.get("message", ""))[:300],
            bool(body.get("sound", False)),
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Benzinga Digest viewer (中文机翻 digest) ──
# Renders the per-day digests produced hourly by MCP/benzinga_mcp (qwen2.5:3b
# full-Chinese translation + noise filtering). Phase 1: read the per-day
# markdown and serve it as HTML for the PWA Digest tab. (Pipeline will be moved
# into the app in a later phase; this only depends on the digests/ folder.)
_DIGEST_DIR = Path(__file__).resolve().parent.parent / "benzinga_digest" / "digests"

# digest 存储格式层:双格式读取(.json 普通 / .json.zst zstd 压缩)。旧日子由
# benzinga_digest/compact.py 无损压成 .zst + 删派生 .md;读取端优先普通 .json
# (daemon 刚写的今天/昨天)。digest_store 顶层不依赖第三方库,import 安全。
import sys as _sys  # noqa: E402
_BZ_SRC = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
if _BZ_SRC not in _sys.path:
    _sys.path.insert(0, _BZ_SRC)
from benzinga_mcp import digest_store as _dgs  # noqa: E402
from benzinga_mcp import finviz_ai_store as _fvai  # noqa: E402  finviz AI 每日库(独立于 digest)
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
from narratives import store as _narr  # noqa: E402  多空叙事每日库


def _digest_available_dates() -> list:
    """所有可用日期(新→旧)。"""
    return _dgs.list_days()


@app.get("/api/digest/dates")
async def api_digest_dates():
    return {"dates": _digest_available_dates()}


@app.get("/api/digest/search")
async def api_digest_search(start: str = Query(""), end: str = Query(""),
                           ticker: str = Query(""), title: str = Query(""),
                           body: str = Query(""), channel: str = Query(""),
                           limit: int = Query(400)):
    """Cross-day filter over stored digests. Filters (all optional, AND-combined):
    start/end date range, ticker (exact symbol), title substring, body substring,
    channel (官方分类子串, 如 'Analyst'/'Earnings'/'WIIM').
    Scans newest→oldest, stops once `limit` matches collected."""
    dates = _digest_available_dates()  # newest-first
    if not dates:
        return {"error": "尚无 digest 数据", "count": 0, "html": "", "truncated": False}
    lo = start.strip() or None
    hi = end.strip() or None
    sel = [d for d in dates if (not lo or d >= lo) and (not hi or d <= hi)]
    tk = ticker.strip().lstrip("$").upper()
    tq = title.strip().lower()
    bq = body.strip().lower()
    cq = channel.strip().lower()
    limit = max(1, min(int(limit), 2000))
    results, scanned, truncated = [], 0, False
    for d in sel:
        try:
            metas = _dgs.load_day(d) or []
        except Exception:
            continue
        scanned += 1
        for m in metas:
            if not isinstance(m, dict):
                continue
            if tk:
                tks = [str(t).lstrip("$").upper() for t in (m.get("tickers") or [])]
                if tk not in tks:
                    continue
            if tq:
                hay = (str(m.get("title", "")) + " " + str(m.get("title_en", ""))).lower()
                if tq not in hay:
                    continue
            if bq:
                hay = " ".join((str(p.get("cn", "")) + " " + str(p.get("en", "")))
                               for p in (m.get("paras") or [])).lower()
                if bq not in hay:
                    continue
            if cq:
                chans = " ".join(str(c) for c in (m.get("channels") or [])).lower()
                if cq not in chans:
                    continue
            mm = dict(m)
            mm["_date"] = d
            results.append(mm)
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
    # within a day metas are time-desc already; days iterated newest-first → keep order
    items = [_digest_slim(m, m.get("_date")) for m in results]
    if _mr is not None:                     # search 跨天: 按各自日期叠加覆盖层(无天级去重)
        for it in items:
            d = it.get("_date")
            if d:
                try:
                    _mr.apply_overlay_item(it, d)
                except Exception:
                    pass
    return {"count": len(results), "items": items, "truncated": truncated, "scanned_days": scanned}


def _digest_rr_match(title: str, teaser: str = "") -> list:
    """Per-article risk radar topic matching — returns [{topic, label, lift}, ...] sorted by lift desc."""
    txt = (title + " " + teaser).lower()
    hits = []
    for topic, kws in _RR_KW.items():
        for kw in kws:
            if kw in txt:
                hits.append({"topic": topic, "label": _RR_LABEL.get(topic, topic),
                             "lift": _RR_LIFT.get(topic, 1.0)})
                break
    hits.sort(key=lambda h: h["lift"], reverse=True)
    return hits


def _digest_slim(m: dict, with_date: str = None) -> dict:
    """Lightweight per-article payload for client-side lazy rendering (TOC + 点开展开).
    Drops meta_raw / *_full / block (~half the size); body as plain string list."""
    body = [(p.get("cn") or p.get("en")) for p in (m.get("paras") or [])
            if (p.get("cn") or p.get("en"))]
    s = {"id": str(m.get("id", "")), "hhmm": m.get("hhmm", ""), "ts": m.get("ts", ""),
         "title": m.get("title", ""), "sentiment": m.get("sentiment"),
         "teaser": m.get("teaser", ""), "tickers": m.get("tickers") or [],
         "channels": m.get("channels") or [], "quotes": m.get("quotes") or [],
         "images": (m.get("images") or [])[:8], "links": (m.get("links") or [])[:12],
         "body": body}
    if _mr is not None:
        try:
            mr, mrb, mrtag = _mr.classify(m, _mr.load_universe())
        except Exception:
            mr, mrb, mrtag = 0, "", ""
        s["mr"], s["mr_b"], s["mr_tag"] = mr, mrb, mrtag
        # noise=1 → 前端「✨去噪」chip 打开时会过滤掉 (默认开)
        try:
            s["noise"] = 1 if _mr.is_noise(m) else 0
        except Exception:
            s["noise"] = 0
    if _RR_KW:
        rr_hits = _digest_rr_match(s.get("title", ""), s.get("teaser", ""))
        if rr_hits:
            s["rr"] = rr_hits
    if with_date:
        s["_date"] = with_date
    return s


@app.get("/api/digest/watchlist")
def api_digest_watchlist():
    """digest 默认 ticker 筛选清单 = heatmap  universe(data/heatmap_universe.txt, ~160 票);
    作者改这个文件即可, digest 今天/日历/历史 与 heatmap 同源。"""
    return {"tickers": sorted(_oa_watchlist_set())}


# ── Digest ❤️ 喜欢(收藏)── 服务端持久,跨"已读"/跨天;纯逻辑在 digest_liked.py
import digest_liked as _digest_liked  # noqa: E402
_LIKED_PATH = Path(__file__).parent / "data" / "digest_liked.json"


@app.get("/api/digest/liked")
def api_digest_liked_get():
    store = _digest_liked.load(str(_LIKED_PATH))
    items = _digest_liked.list_items(store)
    return {"items": items, "keys": [it.get("key") for it in items]}


@app.get("/api/sr_alerts/log")
def api_sr_alerts_log():
    """📍 SR 记录 tab:按天分组的 SR 黄金区告警(最新在前),含新闻富化字段。"""
    from sr_alert import log as _srlog
    st = _srlog.load(str(_SR_LOG_PATH))
    items = _srlog.list_recent(st)   # ts 倒序
    days: list = []
    idx: dict = {}
    for it in items:
        d = it.get("day", "")
        if d not in idx:
            idx[d] = {"day": d, "items": []}
            days.append(idx[d])
        idx[d]["items"].append(it)
    return {"days": days, "count": len(items)}


@app.post("/api/sr_alerts/log")
async def api_sr_alerts_log_post(req: Request):
    from sr_alert import log as _srlog
    try:
        body = await req.json()
    except Exception:
        body = {}
    if body.get("action") == "clear":
        _srlog.save(str(_SR_LOG_PATH), {"entries": []})
        return {"ok": True}
    return {"ok": False, "error": "unknown action"}


@app.post("/api/digest/liked")
async def api_digest_liked_post(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    action = body.get("action")
    store = _digest_liked.load(str(_LIKED_PATH))
    if action == "add":
        store, key = _digest_liked.add(store, body.get("item") or {}, time.time())
        if key is None:
            return {"ok": False, "error": "no stable key"}
        _digest_liked.save(str(_LIKED_PATH), store)
        return {"ok": True, "key": key}
    if action == "remove":
        key = body.get("key")
        if key:
            _digest_liked.save(str(_LIKED_PATH), _digest_liked.remove(store, key))
        return {"ok": True}
    if action == "clear":
        _digest_liked.save(str(_LIKED_PATH), _digest_liked.clear(store))
        return {"ok": True}
    return {"ok": False, "error": "unknown action"}


@app.get("/api/digest/day")
async def api_digest_day(date: str = Query("")):
    """Slim per-day data for lazy client render. date='' → latest available day."""
    dates = _digest_available_dates()
    if not dates:
        return {"error": "尚无 digest 数据", "dates": [], "items": []}
    if not date or date not in dates:
        date = dates[0]
    try:
        metas = _dgs.load_day(date) or []
    except Exception:
        metas = []
    metas = sorted([m for m in metas if isinstance(m, dict)],
                   key=lambda m: m.get("ts", ""), reverse=True)
    items = [_digest_slim(m) for m in metas]
    items.sort(key=lambda m: m.get("ts", ""), reverse=True)
    trc = _digest_tr_load(date)            # 合并已缓存的中文标题/正文
    if trc:
        for it in items:
            e = trc.get(it["id"])
            if e:
                if e.get("t"):
                    it["title_cn"] = e["t"]
                if e.get("b"):
                    it["body_cn"] = e["b"]
    if _mr is not None:                     # 必读天级定稿: 有覆盖层以LLM裁决为准, 否则规则去重
        try:
            _mr.finalize_day(items, date, _mr.load_universe())
        except Exception:
            pass
    gen = ""
    try:
        _dp = _dgs.day_path(date)
        if _dp:
            gen = datetime.datetime.fromtimestamp(_dp.stat().st_mtime).strftime("%m-%d %H:%M")
    except Exception:
        pass
    return {"date": date, "count": len(items), "items": items,
            "generated_at": gen, "dates": dates}


# ── AI 总结: 按 (date, scope) 缓存到本地; 生成在后台跑(关窗口/断连都继续), 前端轮询进度 ──
# scope="" = 当天主摘要(今天/历史整天, 支持增量); scope=<前端 filter hash> = 该天筛选子集(一次性)
_aisum_jobs = {}     # "date|scope" -> {running,started(monotonic),error,mode,task}


def _aisum_key(date: str, scope: str) -> str:
    return f"{date}|{scope or ''}"


def _aisum_path(date: str, scope: str = ""):
    base = Path(__file__).resolve().parent.parent / "benzinga_digest" / "ai_summary"
    name = f"{date}__{scope}.json" if scope else f"{date}.json"
    name = name.replace("/", "_").replace("..", "_")      # 兜底过滤路径
    return base / name


def _aisum_load(date: str, scope: str = ""):
    try:
        return json.loads(_aisum_path(date, scope).read_text(encoding="utf-8"))
    except Exception:
        return None


def _aisum_save(date: str, scope: str, obj: dict):
    try:
        p = _aisum_path(date, scope)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[ai_summary] save err: {e}")


def _ai_enrich_items_with_prices(items):
    """跑 AI 前, 为 items 里**所有** tickers 现拉实时价并**覆盖** it.q。

    ⚠️ 关键修复(2026-07-07): 前端 it.q 快照来自 screener 缓存, 而那份扫描可能是好几
    周前的(scan_date 陈旧) → 非持仓票(MSFT/GOOGL/AMZN 等)的价严重过时。旧版本只补
    "缺价的" ticker、从不覆盖已有值 → 前端塞的陈旧价被判"已有价"永不重拉, bug 根因。
    现改为: 一律用实时价覆盖。持仓票用内存实时(equity_price_loop 每 5min 维护), 其余
    走 TastyTrade REST(权威 mark + 官方 prev-close, BATCH=50) → yfinance 兜底。
    有 client 才补; mutate items[*].q。"""
    if not items or not getattr(state, "client", None):
        return items
    need = set()
    for it in items:
        for tk in (it.get("k") or []):
            tku = str(tk).upper().lstrip("$")
            if tku:
                need.add(tku)
    if not need:
        return items
    _pc = state.equity_prev_close or {}
    _lq = state.live_quotes or {}
    fresh = {}
    remote_need = []
    for tk in need:
        pc = _pc.get(tk)
        lt = (_lq.get(tk) or {}).get("last_trade")
        if pc and lt and pc > 0 and lt > 0:            # 持仓票: 内存实时(5min 刷新)
            fresh[tk] = {"p": round(lt, 2), "c": round((lt - pc) / pc * 100, 2)}
        else:
            remote_need.append(tk)
    # 其余批量拉 TastyTrade(BATCH=50)
    if remote_need:
        try:
            eq = fetch_equity_quotes(state.client, sorted(remote_need))
            for tk, rec in eq.items():
                tku = tk.upper()
                p = rec.get("price") or 0
                pc = rec.get("prev_close") or _pc.get(tku) or 0
                if p > 0 and pc > 0:
                    fresh[tku] = {"p": round(p, 2), "c": round((p - pc) / pc * 100, 2)}
                elif p > 0:
                    fresh[tku] = {"p": round(p, 2), "c": None}
        except Exception as e:
            print(f"[ai_enrich] fetch_equity_quotes fail: {e}")
    # yfinance 兜底(TT 没返回的少数, 如部分 ETF/外国票)
    still = [t for t in remote_need if t.upper() not in fresh]
    if still and HAS_YF:
        try:
            import yfinance as yf
            for t in still[:20]:
                try:
                    fi = yf.Ticker(t).fast_info
                    p = float(fi.get("last_price") or fi.get("lastPrice") or 0)
                    pc = float(fi.get("previous_close") or fi.get("previousClose") or 0)
                    if p > 0:
                        fresh[t.upper()] = {"p": round(p, 2),
                                            "c": (round((p - pc) / pc * 100, 2) if pc > 0 else None)}
                except Exception:
                    pass
        except Exception as e:
            print(f"[ai_enrich] yf fail: {e}")
    if not fresh:
        return items
    # 一律覆盖 it.q(不再 "not in q" 跳过 → 陈旧快照被实时价替换)
    for it in items:
        q = it.get("q") or {}
        for tk in (it.get("k") or []):
            tku = str(tk).upper().lstrip("$")
            if tku in fresh:
                q[tku] = fresh[tku]
        if q:
            it["q"] = q
    print(f"[ai_enrich] 覆盖 {len(fresh)}/{len(need)} 只实时价(digest AI 总结)")
    return items


async def _aisum_run(date, scope, kind, items, old_summary, new_items, result_ids,
                     premarket=""):
    """后台任务: 跑 Haiku 生成/增量并存缓存。与请求生命周期解耦(关窗口/断连都跑完)。
    premarket = 盘前评论文本(仅当天主摘要 full 时带), 会作为 🌅盘前要点 并入摘要。"""
    key = _aisum_key(date, scope)
    from narratives import digest_summary as _dsum
    loop = asyncio.get_event_loop()
    try:
        if kind == "incremental":
            summary, _model, _n, err = await loop.run_in_executor(
                None, _dsum.summarize_incremental, old_summary, new_items)
        else:
            summary, _model, _n, err = await loop.run_in_executor(
                None, _dsum.summarize, items, premarket)
        if err:
            _aisum_jobs.setdefault(key, {})["error"] = err
        else:
            _aisum_save(date, scope, {
                "date": date, "scope": scope, "summary": summary,
                "ids": result_ids, "model": _model,
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "n": len(result_ids), "premarket": bool(premarket)})
            _aisum_jobs.setdefault(key, {})["error"] = None
    except Exception as e:
        _aisum_jobs.setdefault(key, {})["error"] = str(e)[:300]
    finally:
        if key in _aisum_jobs:
            _aisum_jobs[key]["running"] = False
            _aisum_jobs[key]["ended"] = time.monotonic()


def _aisum_start(date, scope, kind, items, old_summary, new_items, result_ids,
                 premarket=""):
    key = _aisum_key(date, scope)
    j = _aisum_jobs.get(key)
    if j and j.get("running"):
        return False                                     # 已在跑, 别重复起
    n = len(new_items) if kind == "incremental" else len(items)
    _aisum_jobs[key] = {"running": True, "started": time.monotonic(),
                        "error": None, "mode": kind, "n_items": n}
    _aisum_jobs[key]["task"] = asyncio.create_task(
        _aisum_run(date, scope, kind, items, old_summary, new_items, result_ids,
                   premarket))
    return True


def _digest_premarket_text(date: str) -> str:
    """当天 Chris 盘前评论文本(data/benzinga/<date>.json), 用于喂进当天主摘要的 🌅盘前要点。"""
    try:
        f = Path(__file__).resolve().parent.parent / "data" / "benzinga" / f"{date}.json"
        if not f.exists():
            return ""
        d = json.loads(f.read_text(encoding="utf-8"))
        ch = (d.get("channels") or {}).get("chris-market-commentary") or {}
        return (ch.get("text") or "")[:6000]
    except Exception:
        return ""


def _aisum_premarket_for(date: str, scope: str) -> str:
    """只有「当天主摘要」(date==今天 且 scope=="")才带盘前评论。"""
    if scope or date != _digest_today_str():
        return ""
    return _digest_premarket_text(date)


@app.post("/api/digest/ai_summary")
async def api_digest_ai_summary(req: Request):
    """启动一次后台 AI 总结(claude -p Haiku, 走订阅), 立刻返回 {status}; 前端轮询 status 取进度/结果。
    body {items:[{id,t,k,s}...], date, scope, mode}:
      mode=auto         有缓存→{status:done}; 否则后台全量生成→{status:running}
      mode=incremental  只把"缓存里没有的标题"增量并入(仅 scope=="")→{status:running}
      mode=full         后台全量重新生成→{status:running}"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    items = (body or {}).get("items") or []
    if not isinstance(items, list):
        items = []
    mode = (body or {}).get("mode") or "auto"
    date = (body or {}).get("date") or _digest_today_str()
    scope = str((body or {}).get("scope") or "")
    cur_ids = [str(it.get("id")) for it in items
               if isinstance(it, dict) and it.get("id")]
    # 补价: 前端 scrData 只 170 只,这里从 TastyTrade REST 拉全 S&P
    items = _ai_enrich_items_with_prices(items)
    key = _aisum_key(date, scope)
    cache = _aisum_load(date, scope)
    j = _aisum_jobs.get(key) or {}
    running = bool(j.get("running"))
    # 上次跑错就冷却 5 分钟 —— 关键防止每次开弹窗都白起一个必错的 job(尤其
    # `claude` 订阅登录失效时, 每点一次 AI 总结都又启一个几分钟后同样报错
    # 的任务, 作者以为"每次都在重新总结")。auto 直接回 error, full 也拦
    # 一下(作者显式选了♻️重新总结 = 5 分钟外才放行)。
    recent_err = (j.get("error") and not running
                  and (time.monotonic() - float(j.get("ended", 0))) < 300)

    if mode == "auto":
        if cache:
            return {"status": "done", "date": date, "scope": scope}
        if running:
            return {"status": "running", "date": date, "scope": scope}
        if recent_err:
            return {"status": "error", "error": j.get("error"),
                    "date": date, "scope": scope}
        if not items:
            return {"status": "empty"}
        _aisum_start(date, scope, "full", items, "", [], cur_ids,
                     _aisum_premarket_for(date, scope))
        return {"status": "running", "date": date, "scope": scope}

    if running:
        return {"status": "running", "date": date, "scope": scope}
    if recent_err and mode == "full":
        return {"status": "error", "error": j.get("error"),
                "date": date, "scope": scope}

    if mode == "incremental" and cache:
        cached_ids = set(cache.get("ids") or [])
        new_items = [it for it in items if str(it.get("id")) not in cached_ids]
        if not new_items:
            return {"status": "done", "date": date, "scope": scope, "updated": False}
        union = list(dict.fromkeys((cache.get("ids") or []) + cur_ids))
        _aisum_start(date, scope, "incremental", items,
                     cache.get("summary", ""), new_items, union)
        return {"status": "running", "added": len(new_items), "date": date, "scope": scope}

    if not items:
        return {"status": "empty"}
    _aisum_start(date, scope, "full", items, "", [], cur_ids,    # full / 首次无缓存
                 _aisum_premarket_for(date, scope))
    return {"status": "running", "date": date, "scope": scope}


@app.get("/api/digest/ai_summary/status")
def api_digest_ai_summary_status(date: str = Query(""), scope: str = Query("")):
    """轮询: {status: running|done|idle|error, elapsed, summary?, ids?, model?, ts?, n?, error?}。"""
    date = date or _digest_today_str()
    j = _aisum_jobs.get(_aisum_key(date, scope)) or {}
    if j.get("running"):
        r = {"status": "running", "mode": j.get("mode"),
             "elapsed": round(time.monotonic() - j.get("started", time.monotonic()), 1),
             "date": date, "scope": scope}
        if j.get("n_items"):
            r["n_items"] = j["n_items"]
        return r
    cache = _aisum_load(date, scope)
    if cache:
        r = {"status": "done", "summary": cache.get("summary", ""),
             "ids": cache.get("ids") or [], "model": cache.get("model", ""),
             "ts": cache.get("ts", ""), "n": cache.get("n", 0), "date": date, "scope": scope}
        if cache.get("n_up") is not None:
            r["n_up"] = cache["n_up"]
        if cache.get("n_down") is not None:
            r["n_down"] = cache["n_down"]
        return r
    if j.get("error"):
        return {"status": "error", "error": j.get("error"), "date": date, "scope": scope}
    return {"status": "idle", "date": date, "scope": scope}


# ── 📈 大盘图(digest 子 tab): SPY/QQQ 全历史日线蜡烛 + 每根对应当天 AI 总结 ──
# 数据源: yfinance 全历史(2011→今)日线 RAW(不复权, 与 TradingView 默认一致)。
# 磁盘缓存 data/market_chart/<SYM>.json, 内存 memo 12h;点某根蜡烛 → 前端复用
# GET /api/digest/ai_summary/status?date=... 拉那天已缓存的 AI 总结(见 backfill)。
_MARKET_CHART_SYMS = ("SPY", "QQQ")
_market_chart_memo: dict = {}          # SYM -> (bars, mono_ts)
_MARKET_CHART_TTL = 12 * 3600


def _market_chart_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "market_chart"


def _market_chart_path(sym: str) -> Path:
    return _market_chart_dir() / f"{sym}.json"


def _market_chart_build(sym: str) -> list:
    """yfinance 拉 sym 全历史日线 → [[YYYY-MM-DD, o, h, l, c, v], ...](旧→新)。"""
    if not HAS_YF:
        return []
    import yfinance as yf
    df = yf.download(sym, start="2011-01-01", interval="1d", progress=False,
                     auto_adjust=False, threads=False)
    if df is None or not len(df):
        return []
    if isinstance(df.columns, pd.MultiIndex):      # 单票有时也回 MultiIndex
        df.columns = df.columns.get_level_values(0)
    df = df.loc[:, ~df.columns.duplicated()]
    bars = []
    for idx, row in df.iterrows():
        try:
            o = float(row["Open"]); h = float(row["High"])
            lo = float(row["Low"]); c = float(row["Close"])
            v = float(row.get("Volume", 0) or 0)
        except Exception:
            continue
        if not (o == o and h == h and lo == lo and c == c):   # 跳 NaN
            continue
        bars.append([idx.date().isoformat(), round(o, 2), round(h, 2),
                     round(lo, 2), round(c, 2), int(v)])
    return bars


def _market_chart_bars(sym: str, force: bool = False) -> list:
    """取 sym 蜡烛(内存 memo → 磁盘缓存 → yfinance 现拉)。当天首个请求刷新到最新交易日。"""
    sym = sym.upper()
    if sym not in _MARKET_CHART_SYMS:
        return []
    hit = _market_chart_memo.get(sym)
    if hit and not force and (time.monotonic() - hit[1]) < _MARKET_CHART_TTL:
        return hit[0]
    p = _market_chart_path(sym)
    today = _digest_today_str()
    # 磁盘缓存新鲜(最后一根是今天, 或文件当天写过)→ 直接用
    if p.exists() and not force:
        try:
            disk = json.loads(p.read_text(encoding="utf-8"))
            bars = disk.get("bars") or []
            fresh = disk.get("built_date") == today or (bars and bars[-1][0] >= today)
            if bars and fresh:
                _market_chart_memo[sym] = (bars, time.monotonic())
                return bars
        except Exception:
            pass
    # 现拉(yfinance 阻塞, 但本函数只在 executor / sync 端点里跑)
    bars = _market_chart_build(sym)
    if not bars:
        # 拉失败: 回退磁盘旧数据, 免得整图空白
        if p.exists():
            try:
                disk = json.loads(p.read_text(encoding="utf-8"))
                if disk.get("bars"):
                    _market_chart_memo[sym] = (disk["bars"], time.monotonic())
                    return disk["bars"]
            except Exception:
                pass
        return []
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"symbol": sym, "built_date": today, "bars": bars},
                                ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[market_chart] save {sym} err: {e}")
    _market_chart_memo[sym] = (bars, time.monotonic())
    return bars


@app.get("/api/digest/market_chart")
async def api_digest_market_chart(symbol: str = Query("SPY"), refresh: int = Query(0)):
    """SPY/QQQ 全历史日线蜡烛 + 有 AI 总结的日期集合(前端标记 + 点击弹窗)。"""
    sym = (symbol or "SPY").upper()
    if sym not in _MARKET_CHART_SYMS:
        return {"error": "unsupported symbol", "symbols": list(_MARKET_CHART_SYMS)}
    loop = asyncio.get_event_loop()
    bars = await loop.run_in_executor(None, _market_chart_bars, sym, bool(refresh))
    # 有 AI 总结缓存的日期(scope='' 整日总结; 已 backfill 全历史) → 前端标点/判可点击
    try:
        sdays = sorted(_aisum_cached_days())
    except Exception:
        sdays = []
    return {"symbol": sym, "symbols": list(_MARKET_CHART_SYMS),
            "bars": bars, "summary_days": sdays,
            "pivots": _market_pivots(bars),      # 关节点(ZigZag 转折 + 突出度)
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}


# ── 关节点(ZigZag 转折)+ 转折归因(是什么消息导致的) ────────────────────────
_MARKET_PIVOT_PCT = 6.0     # ZigZag 阈值(%): 收盘价从上个极值反向≥此幅度 → 确认转折


def _market_pivots(bars, pct: float = _MARKET_PIVOT_PCT) -> list:
    """ZigZag 转折点。bars=[[date,o,h,l,c,v]...]。返回
    [{i,date,price,kind('high'见顶|'low'见底),move_pct(进腿涨跌%),prom(突出度%)}]。
    prom = min(进腿, 出腿) 幅度 → 前端按 prom 排序做「缩放自适应只显著者」。"""
    if not bars or len(bars) < 3:
        return []
    th = pct / 100.0
    piv = []
    up_ref = down_ref = bars[0][4]
    up_i = down_i = 0
    trend = 0
    for i in range(1, len(bars)):
        c = bars[i][4]
        if c > up_ref:
            up_ref = c; up_i = i
        if c < down_ref:
            down_ref = c; down_i = i
        if trend >= 0 and c <= up_ref * (1 - th):
            piv.append({"i": up_i, "date": bars[up_i][0], "price": round(up_ref, 2), "kind": "high"})
            trend = -1; down_ref = c; down_i = i
        elif trend <= 0 and c >= down_ref * (1 + th):
            piv.append({"i": down_i, "date": bars[down_i][0], "price": round(down_ref, 2), "kind": "low"})
            trend = 1; up_ref = c; up_i = i
    for k in range(len(piv)):
        pin = None if k == 0 else abs((piv[k]["price"] - piv[k - 1]["price"]) / piv[k - 1]["price"] * 100)
        pout = None if k == len(piv) - 1 else abs((piv[k + 1]["price"] - piv[k]["price"]) / piv[k]["price"] * 100)
        piv[k]["move_pct"] = None if pin is None else round(
            (piv[k]["price"] - piv[k - 1]["price"]) / piv[k - 1]["price"] * 100, 1)
        cands = [x for x in (pin, pout) if x is not None]
        piv[k]["prom"] = round(min(cands), 1) if cands else 0.0
    return piv


_pivot_jobs: dict = {}     # "SYM|date|kind" -> {running,started,error,task}


def _pivot_key(symbol: str, date: str, kind: str) -> str:
    return f"{symbol}|{date}|{kind}"


def _pivot_path(symbol: str, date: str, kind: str) -> Path:
    base = Path(__file__).resolve().parent.parent / "benzinga_digest" / "pivot_summary"
    name = f"{symbol}__{date}__{kind}.json".replace("/", "_").replace("..", "_")
    return base / name


def _pivot_load(symbol: str, date: str, kind: str):
    try:
        return json.loads(_pivot_path(symbol, date, kind).read_text(encoding="utf-8"))
    except Exception:
        return None


def _pivot_save(symbol: str, date: str, kind: str, obj: dict):
    try:
        p = _pivot_path(symbol, date, kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[pivot] save err: {e}")


def _pivot_window_summaries(date: str, n: int = 3) -> list:
    """转折点前后 n 天(日历日)已缓存的每日市场摘要 → [(date, summary)](升序)。"""
    out = []
    try:
        base = datetime.date.fromisoformat(date)
    except Exception:
        return out
    for k in range(-n, n + 1):
        dd = (base + datetime.timedelta(days=k)).isoformat()
        c = _aisum_load(dd, "")
        if c and c.get("summary") and not c.get("empty"):
            out.append((dd, c.get("summary", "")))
    return out


def _pivot_generate(symbol: str, date: str, kind: str, move_pct):
    """同步生成转折归因并落盘 → (ok, err)。给后台任务 + 预生成脚本共用。"""
    if _pivot_load(symbol, date, kind):
        return True, None
    win = _pivot_window_summaries(date, 3)
    if not win:
        return False, "该转折点附近没有可用的每日摘要"
    from narratives import digest_summary as _dsum
    summary, model, nd, err = _dsum.summarize_pivot(symbol, date, kind, move_pct, win)
    if err:
        return False, err
    _pivot_save(symbol, date, kind, {
        "symbol": symbol, "date": date, "kind": kind, "move_pct": move_pct,
        "summary": summary, "model": model, "n_days": nd,
        "src_days": [d for d, _ in win],
        "ts": datetime.datetime.now().isoformat(timespec="seconds")})
    return True, None


async def _pivot_run(symbol, date, kind, move_pct):
    key = _pivot_key(symbol, date, kind)
    loop = asyncio.get_event_loop()
    try:
        ok, err = await loop.run_in_executor(None, _pivot_generate, symbol, date, kind, move_pct)
        _pivot_jobs.setdefault(key, {})["error"] = (None if ok else err)
    except Exception as e:
        _pivot_jobs.setdefault(key, {})["error"] = str(e)[:300]
    finally:
        if key in _pivot_jobs:
            _pivot_jobs[key]["running"] = False


def _pivot_start(symbol, date, kind, move_pct) -> bool:
    key = _pivot_key(symbol, date, kind)
    j = _pivot_jobs.get(key)
    if j and j.get("running"):
        return False
    _pivot_jobs[key] = {"running": True, "started": time.monotonic(), "error": None}
    _pivot_jobs[key]["task"] = asyncio.create_task(_pivot_run(symbol, date, kind, move_pct))
    return True


@app.post("/api/digest/pivot_summary")
async def api_pivot_summary(req: Request):
    """点关节点 → 生成/取「是什么消息导致这个转折」。有缓存秒回, 否则后台跑 + 轮询。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    symbol = str((body or {}).get("symbol") or "SPY").upper()
    date = str((body or {}).get("date") or "")
    kind = str((body or {}).get("kind") or "")
    move_pct = (body or {}).get("move_pct")
    if symbol not in _MARKET_CHART_SYMS or not date or kind not in ("high", "low"):
        return {"status": "error", "error": "bad params"}
    cache = _pivot_load(symbol, date, kind)
    if cache:
        return {"status": "done", "symbol": symbol, "date": date, "kind": kind}
    key = _pivot_key(symbol, date, kind)
    if _pivot_jobs.get(key, {}).get("running"):
        return {"status": "running", "symbol": symbol, "date": date, "kind": kind}
    _pivot_start(symbol, date, kind, move_pct)
    return {"status": "running", "symbol": symbol, "date": date, "kind": kind}


@app.get("/api/digest/pivot_summary/status")
def api_pivot_summary_status(symbol: str = Query("SPY"), date: str = Query(""),
                             kind: str = Query("")):
    symbol = (symbol or "SPY").upper()
    key = _pivot_key(symbol, date, kind)
    j = _pivot_jobs.get(key) or {}
    if j.get("running"):
        return {"status": "running",
                "elapsed": round(time.monotonic() - j.get("started", time.monotonic()), 1),
                "symbol": symbol, "date": date, "kind": kind}
    cache = _pivot_load(symbol, date, kind)
    if cache:
        return {"status": "done", "summary": cache.get("summary", ""),
                "model": cache.get("model", ""), "ts": cache.get("ts", ""),
                "n_days": cache.get("n_days", 0), "move_pct": cache.get("move_pct"),
                "symbol": symbol, "date": date, "kind": kind}
    if j.get("error"):
        return {"status": "error", "error": j.get("error"), "symbol": symbol, "date": date, "kind": kind}
    return {"status": "idle", "symbol": symbol, "date": date, "kind": kind}


# ── 🧭 市场状态判断(决策树 tab) ────────────────────────────────────
# 判断口径见 market_brief/brief.py 的 system prompt: 不给看多/看空标签, 给
# 「消息坏度 vs 价格已反映」+ 大跌概率(带历史基准率) + 三层支撑 + 逐板块 + 证伪条件。
# 跑一次约 3 分钟(Sonnet 读 12 篇长文 + 全板块数据), 所以按天缓存 + 后台任务轮询。
_mbrief_job = {"running": False, "started": 0.0, "error": None, "date": ""}


def _mbrief_mod():
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from market_brief import runner as _r
    return _r


async def _mbrief_run(date: str):
    loop = asyncio.get_event_loop()
    try:
        r = _mbrief_mod()
        res = await loop.run_in_executor(None, lambda: r.run(date))
        if res.get("error"):
            _mbrief_job["error"] = res["error"]
        else:
            await loop.run_in_executor(None, lambda: r.save(res))
            _mbrief_job["error"] = None
    except Exception as e:
        _mbrief_job["error"] = str(e)[:300]
    finally:
        _mbrief_job["running"] = False


def _igroups_mod():
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from market_brief import groups_store as _gs
    return _gs


def _optpulse_mod():
    import sys
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from market_brief import options_pulse_store as _ops
    return _ops


@app.get("/api/options_pulse")
async def api_options_pulse(force: bool = Query(False)):
    """板块级期权情绪: IV / 25Δ 偏斜 / P-C 的**自身历史百分位**(读本地日汇总)。

    绝对值不可比(有的票常年 IV 30%, 有的常年 90%), 所以结论只由百分位驱动。
    IV 百分位 = 「坏消息已经被定价了多少」, 这是它对市场判断最有用的一条。
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: _optpulse_mod().get(force=force))
    except Exception as e:
        return {"error": str(e)[:300], "rows": []}


@app.post("/api/options_pulse/refresh")
async def api_options_pulse_refresh():
    """从 EOD 全链增量汇总新日期(只算日汇总里缺的天, 已有的不重算)。"""
    loop = asyncio.get_running_loop()

    def _work():
        ops = _optpulse_mod()
        r = ops.build_daily()
        if r.get("error"):
            return r
        res = ops.get(force=True)
        res["built"] = r
        return res

    try:
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        return {"error": str(e)[:300], "rows": []}


@app.get("/api/industry_groups")
async def api_industry_groups(force: bool = Query(False)):
    """81 组细分行业的短/中/长期趋势 + 轮动摘要(读本地 OHLC 库, 不调 LLM)。"""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: _igroups_mod().get(force=force))
    except Exception as e:
        return {"error": str(e)[:300], "rows": []}


@app.post("/api/industry_groups/refresh")
async def api_industry_groups_refresh():
    """全量重拉 572 只票的日线(约 1-2 分钟) —— 必须全量, 半量刷新会导致
    新旧数据混算, 横向排名失效(见 groups_store 模块头注释)。"""
    loop = asyncio.get_running_loop()

    def _work():
        gs = _igroups_mod()
        r = gs.refresh()
        if r.get("error"):
            return r
        return {**r, **gs.get(force=True)}
    try:
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        return {"error": str(e)[:300]}


@app.get("/api/market_brief")
def api_market_brief(date: str = Query("")):
    """读缓存。没有就 {status:'none'}(前端提示点「生成」)。"""
    try:
        r = _mbrief_mod()
        day = date or datetime.date.today().isoformat()
        c = r.load(day)
        if c:
            return {"status": "done", **c}
        return {"status": "none", "date": day}
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}


@app.post("/api/market_brief/run")
async def api_market_brief_run(date: str = Query(""), force: bool = Query(False)):
    """起后台分析(约 3 分钟)。已有缓存且非 force → 直接 done。"""
    day = date or datetime.date.today().isoformat()
    if _mbrief_job.get("running"):
        return {"status": "running", "date": _mbrief_job.get("date") or day}
    try:
        if not force and _mbrief_mod().load(day):
            return {"status": "done", "date": day}
    except Exception:
        pass
    _mbrief_job.update(running=True, started=time.monotonic(), error=None, date=day)
    asyncio.create_task(_mbrief_run(day))
    return {"status": "running", "date": day}


@app.get("/api/market_brief/status")
def api_market_brief_status(date: str = Query("")):
    day = date or datetime.date.today().isoformat()
    if _mbrief_job.get("running"):
        return {"status": "running", "date": _mbrief_job.get("date") or day,
                "elapsed": round(time.monotonic() - _mbrief_job.get("started", time.monotonic()), 1)}
    try:
        c = _mbrief_mod().load(day)
        if c:
            return {"status": "done", **c}
    except Exception:
        pass
    if _mbrief_job.get("error"):
        return {"status": "error", "error": _mbrief_job["error"], "date": day}
    return {"status": "none", "date": day}


# ── 🧭 市场判断: 每天本地 9:00 自动生成 ──
# 为什么是 9:00: digest 的 AI 总结 8:30 生成, 本分析要用它 —— 早于 8:30 跑会拿不到
# 当天新闻(虽有回退到前一天的兜底, 但那是降级)。跑一次约 3 分钟 / 5-9 万 token。
_MBRIEF_HHMM = (9, 0)
_mbrief_auto_last = ""


def _mbrief_auto_state_path():
    return Path(__file__).resolve().parent.parent / "data" / "market_brief" / ".auto.json"


def _mbrief_auto_load():
    try:
        return (json.loads(_mbrief_auto_state_path().read_text()) or {}).get("date", "")
    except Exception:
        return ""


def _mbrief_auto_save(day: str):
    try:
        p = _mbrief_auto_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"date": day}))
    except Exception:
        pass


async def market_brief_daily_loop():
    """每天 9:00 本地自动生成(开机补跑)。只有**真的产出**才记完成 ——
    同 digest AI 总结那个坑: 无条件记完成会让失败的那天永久没有结果。"""
    global _mbrief_auto_last
    _mbrief_auto_last = _mbrief_auto_load()
    await asyncio.sleep(120)          # 让 server 起稳
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            h, mi = _MBRIEF_HHMM
            past = (now.hour > h) or (now.hour == h and now.minute >= mi)
            if _mbrief_auto_last != today and past and now.weekday() < 5:
                r = _mbrief_mod()
                if r.load(today):                     # 已有(手动跑过) → 记完成
                    _mbrief_auto_last = today
                    _mbrief_auto_save(today)
                elif not _mbrief_job.get("running"):
                    print(f"[market_brief] 自动生成 {today}")
                    _mbrief_job.update(running=True, started=time.monotonic(),
                                       error=None, date=today)
                    await _mbrief_run(today)
                    if r.load(today):                 # 只有真的落盘了才记完成
                        _mbrief_auto_last = today
                        _mbrief_auto_save(today)
                    else:
                        print(f"[market_brief] {today} 自动生成未产出, 稍后重试")
        except Exception as e:
            print(f"[market_brief] auto loop err: {e}")
        await asyncio.sleep(600)


# ── 每天本地 8:30 自动生成「当天 AI 总结」(今天清单新闻 + 盘前评论) → 存 scope="" 缓存 ──
# 作者打开就秒显, 之后 🔄 增量加新闻。开机补跑(过 8:30 且今天没跑过); 状态独立持久化(手动重生成不会触发重跑)。
_AISUM_MORNING_HHMM = (8, 30)        # 本地时间(作者在芝加哥)
_aisum_morning_last = ""


def _aisum_morning_state_path():
    return (Path(__file__).resolve().parent.parent / "benzinga_digest"
            / "ai_summary" / ".morning.json")


def _aisum_load_morning_state() -> str:
    try:
        return json.loads(_aisum_morning_state_path().read_text()).get("date", "")
    except Exception:
        return ""


def _aisum_save_morning_state(date: str):
    try:
        p = _aisum_morning_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"date": date}))
    except Exception as e:
        print(f"[ai-morning] state save err: {e}")


def _oa_watchlist_set() -> set:
    """digest UI「只看清单」+ AI 早报共用的默认票池 = data/heatmap_universe.txt( ~160 票,
    与 heatmap 同源)。格式:一行逗号分隔(兼容多行), token 形如 `EXCHANGE:TICKER` 或裸 `TICKER`,
    `###` / `#` 开头的分组注释跳过。函数名保留 `_oa_` 以兼容旧引用。"""
    p = Path(__file__).resolve().parent.parent / "data" / "heatmap_universe.txt"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return set()
    out: set = set()
    for tok in text.replace("\n", ",").split(","):
        t = tok.strip()
        if not t or t.startswith("#"):
            continue
        if ":" in t:                 # `EXCHANGE:TICKER` → `TICKER`
            t = t.split(":", 1)[1].strip()
        if t and not t.startswith("#"):
            out.add(t.upper().lstrip("$"))
    return out


def _aisum_today_items(date: str) -> list:
    """当天 digest + 外部新闻(只看清单, 同前端今天默认视图)→ [{id,t,k,s}]。"""
    try:
        metas = _dgs.load_day(date) or []
    except Exception:
        metas = []
    wl = _oa_watchlist_set()
    out = []
    for m in metas:
        if not isinstance(m, dict) or not m.get("title"):
            continue
        tks = [str(t).upper().lstrip("$") for t in (m.get("tickers") or [])]
        if wl and not any(t in wl for t in tks):
            continue
        out.append({"id": str(m.get("id", "")), "t": m.get("title", ""),
                    "k": m.get("tickers") or [], "s": m.get("sentiment")})
    return out


async def _run_morning_summary(today: str) -> bool:
    """生成并落盘某天的 AI 总结。**只有真的存下来才返回 True。**

    ⚠️ 返回值是关键: 调用方必须凭它决定要不要把这天记为「已完成」。以前无论成败
    都记完成 → 当天数据还没到 / Haiku 出错时这天被永久跳过, 再也不会有总结
    (7 月 12 个交易日就是这么丢的, 作者每次翻历史都得现场重新生成)。
    """
    items = _aisum_today_items(today)
    pm = _digest_premarket_text(today)
    if not items and not pm:
        print(f"[ai-morning] {today}: nothing to summarize (no items, no premarket) — 稍后重试")
        return False
    from narratives import digest_summary as _dsum
    loop = asyncio.get_event_loop()
    summary, model, _n, err = await loop.run_in_executor(
        None, _dsum.summarize, items, pm)
    if err:
        print(f"[ai-morning] {today}: summarize err: {err} — 稍后重试")
        return False
    ids = [it["id"] for it in items if it.get("id")]
    _aisum_save(today, "", {
        "date": today, "scope": "", "summary": summary, "ids": ids, "model": model,
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "n": len(ids), "premarket": bool(pm), "morning": True})
    print(f"[ai-morning] generated {today}: {len(ids)} items, premarket={bool(pm)}")
    return True


def _aisum_cached_days() -> set:
    """已经有总结缓存的日期(只算 scope='' 的整日总结)。"""
    d = Path(__file__).resolve().parent.parent / "benzinga_digest" / "ai_summary"
    out = set()
    try:
        for p in d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"):
            out.add(p.stem)
    except Exception:
        pass
    return out


def _aisum_available_days() -> list:
    """有 digest 数据、因而**可以**被总结的日期。"""
    try:
        return list(_dgs.list_days() or [])
    except Exception:
        return []


async def digest_ai_morning_loop():
    """每天本地 8:30 生成当天 AI 总结, 并顺带把历史缺口补上。

    「已完成」只在**真的存下总结**时才记(见 _run_morning_summary 返回值);
    失败则当天内有限次重试(aisum_schedule.MAX_ATTEMPTS_PER_DAY 兜底, 不会死循环烧钱)。
    每轮再补 1 天历史缺口 → 作者翻历史时总结已经在那儿, 不必现场等生成。
    """
    global _aisum_morning_last
    import aisum_schedule as _sch
    _aisum_morning_last = _aisum_load_morning_state()
    attempts = {"day": "", "n": 0}
    await asyncio.sleep(50)                 # let startup settle
    while True:
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            if attempts["day"] != today:
                attempts.update(day=today, n=0)     # 跨天重置重试计数

            if _sch.should_attempt_today(now, _aisum_morning_last, attempts["n"],
                                         hhmm=_AISUM_MORNING_HHMM):
                if await _run_morning_summary(today):
                    _aisum_morning_last = today
                    _aisum_save_morning_state(today)
                    attempts["n"] = 0
                else:
                    attempts["n"] += 1
                    if attempts["n"] >= _sch.MAX_ATTEMPTS_PER_DAY:
                        print(f"[ai-morning] {today}: 连续 {attempts['n']} 次没生成出来, "
                              f"今天不再试(明天自动重来; 也可在 digest 里手动点🤖)")

            # 历史补齐: 有 digest 数据却没总结的日子, 每轮补一天(由新到旧)
            for d in _sch.days_needing_backfill(_aisum_available_days(),
                                                _aisum_cached_days(), today):
                print(f"[ai-morning] 补历史总结 {d} …")
                await _run_morning_summary(d)
        except Exception as e:
            print(f"[ai-morning] loop error: {e}")
        await asyncio.sleep(120)


# ── 双语新文章每日通知(早 8:30 ET + 晚 17:00 ET,受全局 mute 管)──
_BL_NOTIFY_PATH = Path(__file__).resolve().parent / "data" / "bilingual_notify.json"


def _bl_notify_load() -> dict:
    try:
        return json.loads(_BL_NOTIFY_PATH.read_text())
    except Exception:
        return {}


def _bl_notify_save(d: dict) -> None:
    try:
        _BL_NOTIFY_PATH.write_text(json.dumps(d))
    except Exception:
        pass


def _bl_today_unread_count() -> int:
    """今天(ET)发布、未删、未读的双语文章数(WSJ/Barron/MW)。"""
    from zoneinfo import ZoneInfo
    today = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    s = _bilingual_state or {}
    seen = s.get("seen") or {}
    deleted = s.get("deleted") or {}
    n = 0
    for a in (s.get("articles") or []):
        u = a.get("url")
        if not u or u in deleted or u in seen:
            continue
        if (a.get("date") or "") == today:
            n += 1
    return n


async def bilingual_daily_notify_loop():
    """每天 8:30 ET + 17:00 ET 各推一次「今日 N 篇新财经稿」。
    只在有未读今日稿时推;受全局静音开关管;开机补跑(过点且今天该档没推过)。"""
    from zoneinfo import ZoneInfo
    await asyncio.sleep(60)
    slots = [("morning", 8, 30), ("evening", 17, 0)]
    while True:
        try:
            if not _notifications_muted():
                now = datetime.datetime.now(ZoneInfo("America/New_York"))
                today = now.date().isoformat()
                st = _bl_notify_load()
                changed = False
                for key, hh, mm in slots:
                    past = (now.hour > hh) or (now.hour == hh and now.minute >= mm)
                    if past and st.get(key) != today:
                        n = _bl_today_unread_count()
                        if n > 0:
                            import importlib
                            try:
                                eap = importlib.import_module("exit_alert_push")
                                await eap.send_macos_notification(
                                    f"📰 今日 {n} 篇新财经稿待读",
                                    "WSJ / Barron's / MW — 打开 PWA「Digest → 📡 今天」，看完自动清空",
                                )
                                print(f"[bl-notify] {key} pushed: {n} unread")
                            except Exception as e:
                                print(f"[bl-notify] push err: {e}")
                        st[key] = today
                        changed = True
                if changed:
                    _bl_notify_save(st)
        except Exception as e:
            print(f"[bl-notify] loop error: {e}")
        await asyncio.sleep(120)


# ── Today 实时推送：服务器(经 .run.lock 串行，绝不与 daemon 并发 WS) 每 60s 抓今天 → SSE 推浏览器 ──
_digest_sse_clients = set()      # 连着的 SSE 客户端队列；为空时不抓(省 WS/防 429)
_digest_today_last_count = -1
_digest_wake = asyncio.Event()   # 点开 tab(SSE 连上)→ set → 后台 loop 立刻刷一次,不等 60s 周期
_digest_last_refresh = 0.0       # 单调时钟;给"立刻刷新"做 ≥12s 去抖(防 SSE 频繁重连猛打 Benzinga)


def _digest_today_str() -> str:
    # 本机本地时区(作者在芝加哥),与 daemon digest.py 落盘口径一致;
    # 用 ET 会在 23:00-24:00 CT 把"今天"提前跳成明天(此时 NY 已过午夜)。
    return datetime.datetime.now().astimezone().date().isoformat()


def _refresh_today_blocking():
    """WS 抓今天并写本地文件(backfill_day 内部 _acquire_lock → 与 daemon 串行, 单 WS)。
    返回 (count, status, updated_count)。locked 时直接跳过(下轮再来)。
    updated_count = Benzinga 改稿被覆盖回今天文件的条数(不改 count, 但要触发 SSE refresh)。"""
    import sys as _sys
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in _sys.path:
        _sys.path.insert(0, src)
    from benzinga_mcp.digest import backfill_day
    ds = _digest_today_str()
    r = backfill_day(ds, open_file=False, sentiment_only=False, translate=False)
    try:
        n = len(_dgs.load_day(ds) or [])
    except Exception:
        n = -1
    return n, (r or {}).get("status"), int((r or {}).get("updated") or 0)


async def digest_today_live_loop():
    """有人在看「今天」(SSE 连着)才刷今天:点开立刻刷一次,之后每 60s;
    条数变了 / 有 UPDATED 覆盖 都推 SSE 给所有客户端。
    没人看(无 SSE)完全不刷(0 次 Benzinga,保持原样)。
    "立刻刷新"有 ≥12s 去抖,防 SSE 频繁重连猛打。"""
    global _digest_today_last_count, _digest_last_refresh
    while True:
        try:
            if _digest_sse_clients and (time.monotonic() - _digest_last_refresh >= 12):
                _digest_last_refresh = time.monotonic()
                loop = asyncio.get_event_loop()
                n, status, updated = await loop.run_in_executor(None, _refresh_today_blocking)
                count_changed = isinstance(n, int) and n >= 0 and n != _digest_today_last_count
                if count_changed or updated > 0:
                    if count_changed:
                        _digest_today_last_count = n
                    msg = json.dumps({"count": n, "ts": _digest_today_str(), "updated": updated})
                    for q in list(_digest_sse_clients):
                        try:
                            q.put_nowait(msg)
                        except Exception:
                            pass
        except Exception as e:
            print("[digest-live] error:", e)
        # 等到被唤醒(点开 tab)或最多 60s;唤醒就提前刷新
        try:
            await asyncio.wait_for(_digest_wake.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        _digest_wake.clear()


@app.get("/api/digest/today/stream")
async def digest_today_stream():
    """SSE 流：今天有新内容就推 refresh 事件，浏览器收到后重拉 /api/digest/day。
    点开 tab(本端点被连)→ 唤醒后台 loop 立刻抓一次最新(带 12s 去抖)。"""
    from fastapi.responses import StreamingResponse
    q = asyncio.Queue()
    _digest_sse_clients.add(q)
    _digest_wake.set()   # 点开就立刻刷一次,不用等 60s 周期

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield "event: refresh\ndata: " + msg + "\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _digest_sse_clients.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ── 翻译缓存(标题/正文中文)：独立 .<date>.tr.json，不碰主 digest 文件 ──
def _digest_tr_path(date: str):
    return _DIGEST_DIR / date[:7] / f".{date}.tr.json"


def _digest_tr_load(date: str) -> dict:
    try:
        return json.loads(_digest_tr_path(date).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _digest_tr_save(date: str, cache: dict) -> None:
    import os as _os
    p = _digest_tr_path(date)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, p)
    except Exception as e:
        print("[digest-tr] save failed:", e)


def _digest_import_tr():
    """惰性导入 benzinga_digest 的批量翻译(Google→qwen 兜底)。"""
    import sys as _sys
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in _sys.path:
        _sys.path.insert(0, src)
    from benzinga_mcp.digest import translate_lines
    return translate_lines


def _digest_zh_looks_english(en_text: str, zh_text: str) -> bool:
    """检测缓存/新译"中文"其实是英文原样返回(Google 有时对长技术句/专名如此)。
    同一份逻辑复用自 sec_filings/sec_api.py。判 True → 视为未翻,让上层重试。"""
    if not zh_text or len(zh_text.strip()) < 10:
        return False
    if " ".join(zh_text.split()).lower() == " ".join((en_text or "").split()).lower():
        return True
    cjk = sum(1 for c in zh_text if ("一" <= c <= "鿿" or "㐀" <= c <= "䶿"))
    if cjk == 0:
        return True
    if len(zh_text) > 20 and cjk / len(zh_text) < 0.1:
        return True
    return False


def _digest_tr_update(date: str, mutate):
    """经 benzinga_digest 的 flock tr_update 安全写 .tr.json(与预翻 worker 共用锁)。"""
    import sys as _sys
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in _sys.path:
        _sys.path.insert(0, src)
    from benzinga_mcp.digest import tr_update
    return tr_update(date, mutate)


def _digest_day_metas(date: str) -> list:
    try:
        return _dgs.load_day(date) or []
    except Exception:
        return []


@app.post("/api/digest/translate_titles")
async def api_digest_translate_titles(req: Request):
    """把某天所有标题用 Google(429→qwen) 翻成中文，缓存到 .tr.json，返回 {id: 中文标题}。

    `force=True` → 无视缓存重翻并覆盖 (reload 按钮路径)。
    假中文过滤: 缓存里的 t 如果被检测为英文原样, 视为未翻, 一并重翻。
    过滤了新译回英文的情况: 不写入缓存 (下次自动再试) 也不回给前端。
    """
    body = await req.json()
    date = (body or {}).get("date", "")
    force = bool((body or {}).get("force", False))
    if not date:
        return {"titles": {}}
    metas = _digest_day_metas(date)
    cache = _digest_tr_load(date)

    def _is_stuck_english(mid: str, en: str) -> bool:
        cur = cache.get(str(mid), {}).get("t", "") or ""
        return bool(cur) and _digest_zh_looks_english(en, cur)

    need = [(str(m.get("id")), m.get("title", "")) for m in metas
            if m.get("title") and (force
                                   or not cache.get(str(m.get("id")), {}).get("t")
                                   or _is_stuck_english(m.get("id"), m.get("title", "")))]
    if need:
        loop = asyncio.get_event_loop()
        tr = _digest_import_tr()
        cn = await loop.run_in_executor(None, tr, [t for _, t in need])
        # 假中文防漏: 新译回英文的清成空 → 下次刷新自然重试, 不污染缓存
        pairs = []
        for (mid, en_ttl), zh in zip(need, cn):
            if zh and _digest_zh_looks_english(en_ttl, zh):
                zh = ""
            pairs.append((mid, zh))

        def _mut(c):
            for mid, v in pairs:
                if v:
                    c.setdefault(mid, {})["t"] = v
                else:
                    # 强制重翻时先清掉旧的假中文, 让后续自然重试; 非 force 也清, 保持一致
                    c.setdefault(mid, {}).pop("t", None)
        cache = await loop.run_in_executor(None, _digest_tr_update, date, _mut)
    # 返回前也过滤: 缓存里可能还有历史遗留的假中文 (旧路径写进去的), 一次性洗白
    out = {}
    for m in metas:
        mid = str(m.get("id"))
        t = cache.get(mid, {}).get("t", "") or ""
        if t and _digest_zh_looks_english(m.get("title", ""), t):
            t = ""
        out[mid] = t
    return {"titles": out}


@app.post("/api/digest/translate_article")
async def api_digest_translate_article(req: Request):
    """翻译单篇(标题+正文)→中文，缓存。返回 {title_cn, body_cn:[...]}。

    `force=True` → 无视缓存, 标题+正文全部重翻并覆盖 (reload 按钮路径)。
    假中文过滤: 缓存/新译的 t 或 b 段被检测为英文 → 视为未翻, 让下次自动重试
    (老逻辑 "b" in ent 即认为已翻 → 若首次翻回英文的空/英文 body, 永远卡死;
    现在写缓存时会过滤掉这类, 下次请求自动重翻)。
    """
    body = await req.json()
    date = (body or {}).get("date", "")
    aid = str((body or {}).get("id", ""))
    force = bool((body or {}).get("force", False))
    if not date or not aid:
        return {"title_cn": "", "body_cn": []}
    cache = _digest_tr_load(date)
    ent = dict(cache.get(aid, {}))
    m = next((x for x in _digest_day_metas(date) if str(x.get("id")) == aid), None)
    en_ttl = (m or {}).get("title", "") if m else ""
    paras_en = ([(p.get("cn") or p.get("en")) for p in ((m or {}).get("paras") or [])
                 if (p.get("cn") or p.get("en"))] if m else [])

    # 判老缓存是否卡了假中文 → 视同缺失, 触发重翻
    t_cached = ent.get("t", "") or ""
    b_cached = ent.get("b")
    t_stuck = bool(t_cached) and _digest_zh_looks_english(en_ttl, t_cached)
    # b 是列表, 逐段检查; 只要有非空段判为英文, 视为卡死
    b_stuck = False
    if isinstance(b_cached, list) and b_cached and paras_en:
        for i, zh_p in enumerate(b_cached):
            if not zh_p:
                continue
            en_p = paras_en[i] if i < len(paras_en) else ""
            if _digest_zh_looks_english(en_p, zh_p):
                b_stuck = True
                break

    need_title = force or not t_cached or t_stuck
    need_body = force or ("b" not in ent) or b_stuck
    if m and (need_title or need_body):
        loop = asyncio.get_event_loop()
        tr = _digest_import_tr()
        if need_body:
            new_b = await loop.run_in_executor(None, tr, paras_en) if paras_en else []
            # 段级假中文过滤: 单段翻回英文就置空 (前端 renderParas 会用 en 兜底)
            ent["b"] = [(zh if not (zh and _digest_zh_looks_english(paras_en[i] if i < len(paras_en) else "", zh)) else "")
                        for i, zh in enumerate(new_b)]
        if need_title and en_ttl:
            new_t = (await loop.run_in_executor(None, tr, [en_ttl]))[0] or ""
            if new_t and _digest_zh_looks_english(en_ttl, new_t):
                new_t = ""       # 假中文直接丢, 让下次自然重试
            ent["t"] = new_t

        def _mut(c):
            e = c.setdefault(aid, {})
            if need_body:
                e["b"] = ent["b"]
            if need_title:
                if ent.get("t"):
                    e["t"] = ent["t"]
                else:
                    e.pop("t", None)     # 清掉老的假中文, 别再回污染
        await loop.run_in_executor(None, _digest_tr_update, date, _mut)
    return {"title_cn": ent.get("t", ""), "body_cn": ent.get("b", [])}


# ── 📊 Tickers 视图 ─────────────────────────────────────────────────────
# 按最近 N 天(1/3/7/30)聚合 benzinga digest 的 ticker → 条数排行榜; 点某个
# ticker 拉该 ticker 在该窗口内的全部新闻。前端复用 renderDigestList 显示
# (❤️/↻/展开双语翻译都能用)。窗口按本地时区,与 _digest_today_str 一致。

_DIGEST_TK_ALLOWED_DAYS = {1, 3, 7, 30}


def _digest_last_n_date_strs(n: int) -> list:
    """本机时区的最近 N 天 YYYY-MM-DD, 从今天倒推, 新到旧。"""
    today = datetime.datetime.now().astimezone().date()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(max(1, n))]


def _digest_meta_stamp(m: dict, date: str) -> str:
    """给 meta 拼一个可排序的时间串 (YYYY-MM-DD HH:MM), 用于 latest 排序。
    hhmm 缺失时退化到 date 头部。"""
    return f"{date} {(m.get('hhmm') or '')}".strip()


@app.get("/api/digest/tickers")
def api_digest_tickers(days: int = 1):
    """聚合最近 days 天的 benzinga digest,按 ticker 出现次数排行。
    返回 [{ticker, count, latest}] (count desc, latest desc tie-break)。
    days 只接受 1/3/7/30 (超出裁到 30)。"""
    if days not in _DIGEST_TK_ALLOWED_DAYS:
        days = min(_DIGEST_TK_ALLOWED_DAYS, key=lambda x: abs(x - max(1, days)))
    counts: dict = {}
    latest: dict = {}
    for d in _digest_last_n_date_strs(days):
        for m in _digest_day_metas(d):
            tks = m.get("tickers") or []
            if not tks:
                continue
            stamp = _digest_meta_stamp(m, d)
            for raw in tks:
                tk = str(raw or "").strip().upper()
                if not tk:
                    continue
                counts[tk] = counts.get(tk, 0) + 1
                if stamp > latest.get(tk, ""):
                    latest[tk] = stamp
    items = [{"ticker": tk, "count": n, "latest": latest.get(tk, "")}
             for tk, n in counts.items()]
    items.sort(key=lambda x: (-x["count"], x["latest"] and (-1)), reverse=False)
    # 上面 sort 只做了 count 主键, tie-break 用 latest 倒序:
    items.sort(key=lambda x: (-x["count"], -(_ord_stamp(x["latest"]))))
    return {"days": days, "total_tickers": len(items), "items": items}


def _ord_stamp(s: str) -> int:
    """把 'YYYY-MM-DD HH:MM' 变成一个可比较的整数, 缺失当 0。"""
    if not s:
        return 0
    try:
        d, hm = (s + " 00:00").split()[:2]
        y, mo, dd = d.split("-")
        h, mi = (hm.split(":") + ["0"])[:2]
        return int(y) * 100000000 + int(mo) * 1000000 + int(dd) * 10000 + int(h) * 100 + int(mi)
    except Exception:
        return 0


_pos_tickers_cache: dict = {"ts": 0.0, "tickers": set()}
_pos_tickers_lock = threading.Lock()


def _fetch_position_tickers() -> tuple:
    """拉当前持仓 tickers, 30s 内存缓存 (避免作者切时间 chip 时 spam TT +
    并发 uvicorn 竞争 TT client 挂起前端 fetch)。持仓 30s 后自动重拉,
    符合"变了就反映"要求但不 hammer API。返 (tickers set, err_str or '')。"""
    import time as _tt
    now = _tt.time()
    with _pos_tickers_lock:
        if _pos_tickers_cache["ts"] and (now - _pos_tickers_cache["ts"]) < 30:
            return set(_pos_tickers_cache["tickers"]), ""
        client = state.client
        if client is None:
            return set(), "TT client 未连"
        try:
            _, acct = client.pick_active_account()
            raw = client.get_positions(acct) or []
        except Exception as e:
            return set(), str(e)[:200]
        tickers: set = set()
        for p in raw:
            sym = str(p.get("underlying-symbol", "") or "").strip().upper().lstrip("$")
            if sym:
                tickers.add(sym)
        _pos_tickers_cache["ts"] = now
        _pos_tickers_cache["tickers"] = tickers
        return set(tickers), ""


@app.get("/api/digest/positions_news")
def api_digest_positions_news(days: int = 1):
    """当前持仓 tickers 在最近 days 天里的合并 news。
    持仓 tickers 30s 内存 cache (见 _fetch_position_tickers), 30s 后自动
    重拉 → 持仓改了下次点开自动反映; 快速切时间 chip 时不 spam TT API。
    days 只接受 {1,3,7,30},越界裁到最近档。"""
    if days not in _DIGEST_TK_ALLOWED_DAYS:
        days = min(_DIGEST_TK_ALLOWED_DAYS, key=lambda x: abs(x - max(1, days)))
    tickers, err = _fetch_position_tickers()
    if err:
        return {"tickers": [], "days": days, "items": [], "counts": {},
                "count": 0, "error": err}
    if not tickers:
        return {"tickers": [], "days": days, "items": [], "counts": {}, "count": 0}
    # 扫日期, 过滤 tickers ∩ holdings; 用 _digest_slim 转成前端一致格式
    # (含 body list, 才能点开正确展开双语正文; raw meta 只有 paras 不行)
    out: list = []
    counts: dict = {tk: 0 for tk in tickers}
    for d in _digest_last_n_date_strs(days):
        day_metas = _digest_day_metas(d)
        matched: list = []
        for m in day_metas:
            tks = [str(x or "").strip().upper().lstrip("$")
                   for x in (m.get("tickers") or [])]
            hits = [t for t in tks if t in tickers]
            if hits:
                matched.append((m, hits))
        if not matched:
            continue
        cache = _digest_tr_load(d)
        for m, hits in matched:
            slim = _digest_slim(m, with_date=d)
            aid = str(slim.get("id", ""))
            e = cache.get(aid) or {}
            t_zh = e.get("t") or ""
            if t_zh and not _digest_zh_looks_english(slim.get("title", ""), t_zh):
                slim["title_cn"] = t_zh
            if e.get("b"):
                slim["body_cn"] = e["b"]
            out.append(slim)
            for tk in hits:
                counts[tk] = counts.get(tk, 0) + 1
    out.sort(key=lambda x: -_ord_stamp(_digest_meta_stamp(x, x.get("_date", ""))))
    return {"tickers": sorted(tickers), "days": days, "items": out,
            "counts": counts, "count": len(out)}


@app.get("/api/digest/by_ticker")
def api_digest_by_ticker(ticker: str = "", days: int = 1):
    """拉某 ticker 在最近 days 天里的全部 benzinga 新闻, 按 (date, hhmm) 降序。
    走 _digest_slim (含 body list, 前端才能正确展开双语正文;不是 raw meta
    只有 paras 导致点开只显一句)。塞 _date + title_cn + body_cn (缓存命中且
    非假中文)。
    """
    tk = (ticker or "").strip().upper()
    if not tk:
        return {"ticker": "", "days": days, "items": []}
    if days not in _DIGEST_TK_ALLOWED_DAYS:
        days = min(_DIGEST_TK_ALLOWED_DAYS, key=lambda x: abs(x - max(1, days)))
    out: list = []
    for d in _digest_last_n_date_strs(days):
        day_metas = _digest_day_metas(d)
        matched = []
        for m in day_metas:
            tks = [str(x or "").strip().upper() for x in (m.get("tickers") or [])]
            if tk in tks:
                matched.append(m)
        if not matched:
            continue
        cache = _digest_tr_load(d)
        for m in matched:
            slim = _digest_slim(m, with_date=d)
            aid = str(slim.get("id", ""))
            e = cache.get(aid) or {}
            t_zh = e.get("t") or ""
            if t_zh and not _digest_zh_looks_english(slim.get("title", ""), t_zh):
                slim["title_cn"] = t_zh
            if e.get("b"):
                slim["body_cn"] = e["b"]
            out.append(slim)
    out.sort(key=lambda x: -_ord_stamp(_digest_meta_stamp(x, x.get("_date", ""))))
    return {"ticker": tk, "days": days, "items": out, "count": len(out)}


# ════════════════════════════════════════════════════════════════════════
# 📰 Mover News —— 当天 ±1% 异动票 + 双语新闻(digest 匹配 + finviz 有情绪标题)
#   · movers :heatmap 实时快照(_build_response)的 change_pct,|Δ|≥threshold
#   · digest :本地 load_day(当天) 建 ticker→articles 索引(零网络,全票都查),
#             双语正文走现成 /api/digest/translate_article
#   · finviz :get_news 标题(无原生 sentiment→关键词规则打利好/利空;无正文→跳外链),
#             网络贵 → 只对 top 异动票并发拉;中性也返回(前端默认隐藏,可开关)
#   · 标题双语:批量 translate_lines,与 digest 共用 .tr.json 缓存(按 id,finviz 用标题 hash)
#   每天按当天实时现算,天然每日清空。
# ════════════════════════════════════════════════════════════════════════
_MV_FV_CAP_TICKERS = 500       # 安全上限(universe≈399 → 实抓全部异动票);后台预构建+缓存+限速兜住 rate-limit
_MV_FV_PER_TICKER = 4          # 每票最多 4 条 finviz
_MV_DIGEST_PER_TICKER = 6      # 每票最多 6 条 digest

# 关键词规则:金融标题利好/利空(挑单义性强的词,宁缺毋滥)。命中净值定 ±1/0。
_MV_BULL_WORDS = {
    "beat", "beats", "beating", "tops", "topped", "surge", "surges", "surged",
    "soar", "soars", "soared", "jump", "jumps", "jumped", "rally", "rallies",
    "rallied", "upgrade", "upgrades", "upgraded", "raises", "raised", "boost",
    "boosts", "boosted", "record", "approval", "approved", "approves", "wins",
    "win", "won", "awarded", "awards", "launch", "launches", "launched",
    "expands", "expansion", "partnership", "buyback", "buybacks", "dividend",
    "outperform", "outperforms", "strong", "growth", "profit", "profits",
    "breakthrough", "positive", "milestone", "secures", "secured", "soaring",
    "rallying", "gains", "gain", "gained", "higher", "bullish", "upbeat",
    "exceeds", "exceeded", "accelerate", "accelerates", "rebound", "rebounds",
    "jumps", "skyrocket", "skyrockets", "doubles", "tripled",
}
_MV_BEAR_WORDS = {
    "miss", "misses", "missed", "cut", "cuts", "slashes", "slashed", "plunge",
    "plunges", "plunged", "plummet", "plummets", "drop", "drops", "dropped",
    "fall", "falls", "fell", "tumble", "tumbles", "tumbled", "sink", "sinks",
    "sank", "downgrade", "downgrades", "downgraded", "lowers", "lowered",
    "warning", "warns", "warned", "probe", "probes", "investigation",
    "lawsuit", "lawsuits", "sue", "sued", "sues", "recall", "recalls",
    "halt", "halts", "halted", "delay", "delays", "delayed", "layoff",
    "layoffs", "bankruptcy", "fraud", "decline", "declines", "declined",
    "weak", "weaker", "loss", "losses", "disappoint", "disappoints",
    "disappointing", "concerns", "breach", "hack", "hacked", "slump",
    "slumps", "bearish", "downbeat", "shortfall", "subpoena", "crash",
    "crashes", "crashed", "selloff", "slowdown", "sinks", "sliding", "slides",
}


def _mv_headline_sentiment(title: str) -> int:
    """关键词规则:利好 +1 / 利空 -1 / 中性 0(净值)。finviz 标题无原生 sentiment。"""
    import re as _re
    words = set(_re.findall(r"[a-z]+", (title or "").lower()))
    b = len(words & _MV_BULL_WORDS)
    s = len(words & _MV_BEAR_WORDS)
    return 1 if b > s else (-1 if s > b else 0)


def _mv_summary_paras(summary: str) -> list:
    """AI summary(markdown 要点)→ 干净段落列表(去 bullet 标记 + 去 **粗体**)。"""
    import re as _re
    out = []
    for ln in (summary or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        ln = _re.sub(r"^[-*•]\s*", "", ln)          # bullet 标记
        ln = _re.sub(r"\*\*(.+?)\*\*", r"\1", ln)   # **粗体**
        ln = _re.sub(r"\*(.+?)\*", r"\1", ln)       # *斜体*
        ln = ln.strip()
        if ln:
            out.append(ln)
    return out[:8]


def _mv_fmt_dt(iso: str) -> str:
    """finviz ISO(ET)→ 'HH:MM'(今天)/ 'MM-DD HH:MM'(更早)。"""
    try:
        dt = datetime.datetime.fromisoformat((iso or "").split(".")[0])
    except Exception:
        return ""
    return dt.strftime("%H:%M") if dt.date() == datetime.date.today() \
        else dt.strftime("%m-%d %H:%M")


def _mv_ai_item(ai: dict, ticker: str) -> dict | None:
    """whyMoving JSON → mover-news 条目(原生情绪 + 双语正文位 + ✦AI src)。"""
    import hashlib as _h
    if not ai:
        return None
    head = (ai.get("headline") or "").strip()
    if not head:
        return None
    sv = (ai.get("sentiment") or "").lower()
    sent = 1 if sv == "good" else (-1 if sv == "bad" else 0)
    paras = _mv_summary_paras(ai.get("summary") or "")
    aid = "fvai:" + str(ai.get("id") or
                        _h.md5(head.encode("utf-8", "ignore")).hexdigest()[:12])
    return {
        "id": aid, "src": "finviz_ai",
        "hhmm": _mv_fmt_dt(ai.get("dateTime") or ""),
        "dt": (ai.get("dateTime") or ""),     # 原始 ISO(给 digest「✦finviz AI」视图排序)
        "title_en": head, "title_cn": "",
        "sentiment": sent, "catalyst": bool(ai.get("catalyst")),
        "link": f"https://finviz.com/stock?t={ticker.upper()}&p=d",
        "paras_en": paras, "paras_cn": [], "has_body": bool(paras),
    }


def _mv_finviz_news(ticker: str, hours: int = 36,
                    force: bool = False, allow_fetch: bool = True) -> list:
    """一次新版 stock 页面抓取 → AI「为何异动」(置顶, 原生情绪, 双语 summary)
    + 经典新闻标题(关键词情绪)。页面抓不到新闻时回退 finviz 包的 quote.ashx。
    force/allow_fetch 透传 _finviz_stockpage(作者开页 allow_fetch=False 只读缓存)。"""
    import hashlib as _h
    page = _finviz_stockpage(ticker, force=force, allow_fetch=allow_fetch)
    out = []
    ai = _mv_ai_item(page.get("ai"), ticker)
    if ai:
        out.append(ai)                       # ✦AI 置顶
    raw = page.get("news") or []
    # 不再回退 quote.ashx:被 rate-limit 时回退会再发一次请求(finviz 包默认 UA 更易被挡)
    # → 加剧雪崩。新版页面 news-table 已够全(~80 条);抓不到就这只票无 finviz(digest 仍在)。
    cnt = 0
    for it in raw:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        fid = "fv:" + _h.md5(title.encode("utf-8", "ignore")).hexdigest()[:12]
        out.append({
            "id": fid, "src": "finviz",
            "hhmm": it.get("created_at") or "",
            "title_en": title, "title_cn": "",
            "sentiment": _mv_headline_sentiment(title),
            "link": it.get("url") or "",
            "paras_en": [], "paras_cn": [], "has_body": False,
        })
        cnt += 1
        if cnt >= _MV_FV_PER_TICKER:
            break
    return out


def _mv_gather_finviz(tickers: list, hours: int = 36,
                      max_workers: int = 1, batch_pause: float = 0.9,
                      force: bool = False, allow_fetch: bool = True) -> dict:
    """分批拉 finviz(带 _finviz_stockpage 进程缓存),返回 {ticker: [news]}。
    ⚠️rate-limit 关键:finviz 是速率/突发限制——一旦被throttle会快速返空, 若此时不继续限速
    就会雪崩(之前自适应"只在慢批暂停"反被坑:被挡→秒返→不暂停→提速→全挂, 56/300)。
    这里**固定节流**:只要该批真打了网络(按缓存状态判断, 命中缓存的批秒回不睡)就 sleep。
    默认串行 1 并发 + 0.9s ≈ 1 req/s(实测 45/45 零拦截);后台 force=True 慢速预抓全部
    异动票(~5min/300 只)。作者开页 allow_fetch=False → 全程不打网络、不暂停。"""
    from concurrent.futures import ThreadPoolExecutor
    import time as _t
    out: dict = {}
    if not tickers:
        return out

    def _is_cached(t):
        c = _FINVIZ_PAGE_CACHE.get(t.upper().strip())
        return bool(c) and not force and (_t.time() - c["ts"]) < _FINVIZ_PAGE_TTL

    def _one(t):
        try:
            return t, _mv_finviz_news(t, hours=hours, force=force,
                                      allow_fetch=allow_fetch)
        except Exception:
            return t, []
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for i in range(0, len(tickers), max_workers):
                batch = tickers[i:i + max_workers]
                # 该批会真打网络吗(决定要不要节流):不许抓 / 全命中缓存 → 不暂停
                will_fetch = allow_fetch and any(not _is_cached(t) for t in batch)
                for t, news in ex.map(_one, batch):
                    if news:
                        out[t] = news
                if will_fetch and (i + max_workers < len(tickers)):
                    _t.sleep(batch_pause)
    except Exception:
        pass
    return out


def _finviz_ai_store_today() -> tuple:
    """finviz_ai 库最新一天 → (date, {TICKER: entry})。优先今天本地日期。"""
    try:
        date = datetime.date.today().isoformat()
        day = _fvai.load_day(date)
        if not day:
            days = _fvai.list_days() or []
            if days:
                date = days[0]
                day = _fvai.load_day(date)
    except Exception:
        return ("", {})
    idx = {}
    for e in (day or []):
        tk = e.get("ticker")
        if tk:
            idx[str(tk).upper()] = e
    return (date, idx)


def _fvai_shape_item(e: dict, with_date: str = "") -> dict:
    """finviz_ai 库条目 → digest-item 形状(给「✦finviz AI」视图)。无 AI 返 None。
    with_date(跨库日搜索时)→ 写 _date 并把 id 加 @date 后缀(防跨天同 id 撞 renderDigestList)。"""
    ai = e.get("ai")
    if not ai or not (ai.get("title_en") or "").strip():
        return None
    chans = ["✦AI"] + (["催化"] if ai.get("catalyst") else [])
    iid = ai.get("id") or ("fvai:" + str(e.get("ticker")))
    it = {
        "id": (str(iid) + "@" + with_date) if with_date else str(iid),
        "hhmm": ai.get("hhmm", ""), "ts": ai.get("dt", ""),
        "title": ai.get("title_en", ""), "title_cn": ai.get("title_cn", ""),
        "sentiment": ai.get("sentiment"),
        "tickers": [e.get("ticker")] if e.get("ticker") else [],
        "channels": chans,
        "body": ai.get("paras_en") or [], "body_cn": ai.get("paras_cn") or [],
        "links": [ai["link"]] if ai.get("link") else [],
        "source": "finviz_ai",
    }
    if with_date:
        it["_date"] = with_date
    return it


@app.get("/api/digest/finviz_ai")
def api_digest_finviz_ai(date: str = Query(""), q: str = Query("")):
    """finviz AI「为何异动」库视图(数字 digest「✦finviz AI」mode)。
    · q 非空 → 跨所有库日搜 ticker/标题(结果带 _date, 按 日期+时间 倒序)。
    · 否则 → 返回 date 那天(默认最新)。dates 给前端做「今天/历史」选择。"""
    days = _fvai.list_days() or []
    if not days:
        return {"date": "", "items": [], "count": 0, "dates": []}
    qq = (q or "").strip().upper()
    if qq:
        out = []
        for d in days:
            for e in (_fvai.load_day(d) or []):
                tk = str(e.get("ticker") or "").upper()
                ai = e.get("ai") or {}
                hay = tk + " " + (ai.get("title_en") or "").upper() + " " + (ai.get("title_cn") or "")
                if qq in hay:
                    it = _fvai_shape_item(e, d)
                    if it:
                        out.append(it)
        out.sort(key=lambda x: (x.get("_date", ""), x.get("ts") or x.get("hhmm") or ""),
                 reverse=True)
        return {"date": "", "q": qq, "items": out, "count": len(out), "dates": days}
    if not date or date not in days:
        date = days[0]
    items = [it for it in (_fvai_shape_item(e) for e in (_fvai.load_day(date) or [])) if it]
    items.sort(key=lambda x: x.get("ts") or x.get("hhmm") or "", reverse=True)
    return {"date": date, "items": items, "count": len(items), "dates": days}


@app.get("/api/narrative")
def api_narrative(ticker: str = Query("")):
    """多空叙事(Mover News「⚖️多空」按钮)。读 narrative 库最新一天该票, 零网络。
    每天 8点/18点 由 narratives/build.py 经 Haiku 生成。无数据 → empty。"""
    tk = (ticker or "").strip().lstrip("$").upper()
    if not tk:
        return {"ticker": "", "empty": True}
    days = _narr.list_days() or []
    if not days:
        return {"ticker": tk, "empty": True}
    today = datetime.date.today().isoformat()
    use = today if today in days else days[0]
    for e in (_narr.load_day(use) or []):
        if str(e.get("ticker", "")).upper() == tk:
            return {"ticker": tk, "asof": e.get("asof"), "date": use,
                    "bull": e.get("bull") or [], "bear": e.get("bear") or [],
                    "net": e.get("net") or "", "evidence": e.get("evidence") or []}
    return {"ticker": tk, "empty": True, "date": use}


_NARR_TK_RE = re.compile(r"^[A-Z][A-Z.\-]{0,6}$")


def _narr_live_finviz(tk: str):
    """搜票池外的票时实时抓 1 次 finviz → finviz_ai 库条目形状({ticker, ai, news})。

    ⚠️ 只在内存返回, **不写 finviz_ai 库**(那是 crawler 的地盘, 写进去会让搜过的票
       混进 Mover News / digest 的「✦finviz AI」模式)。
    ⚠️ best-effort: 失败/无数据返 None → 调用方退化成纯 benzinga(票池外的票本来就没 finviz)。
    force=False → 命中 _FINVIZ_PAGE_CACHE 就不打网络(作者狂点搜索也不会撞限流)。
    """
    try:
        fv_map = _mv_gather_finviz([tk], 36, force=False, allow_fetch=True)
    except Exception as e:
        print(f"[narr_search] finviz 实时抓失败 {tk}: {e}")
        return None
    items = (fv_map or {}).get(tk) or []
    if not items:
        return None
    ai, news = None, []
    for n in items:
        if n.get("src") == "finviz_ai":
            ai = n
        elif n.get("sentiment") not in (0, None):
            news.append(n)
    return {"ticker": tk, "ai": ai, "news": news[:_MV_FV_PER_TICKER]}


@app.post("/api/narrative/search")
def api_narrative_search(ticker: str = Query("")):
    """任意 ticker 的按需深度多空(票池外也能搜)。

    benzinga 近60天**全文** + primary 分层选池(search_pool) + 票池外实时补 finviz
    → Haiku 1 次(~10-15s) → 写 narrative 库(= 免费缓存, 再搜走 GET /api/narrative 秒显)。
    池空则**不调 Haiku**(不烧额度)。FastAPI 把 def 端点丢线程池, 不阻塞事件循环。
    """
    tk = (ticker or "").strip().lstrip("$").upper()
    if not tk or not _NARR_TK_RE.match(tk):
        return {"ticker": tk, "error": "无效 ticker", "searched": True}
    try:
        from narratives import universe as _nu
        in_uni = tk in set(_nu.individual_stocks())
    except Exception:
        in_uni = False
    fv_entry = None if in_uni else _narr_live_finviz(tk)
    try:
        from narratives import build as _nb
        item = _nb.build_one(tk, deep=True, finviz_entry=fv_entry)
    except Exception as e:
        return {"ticker": tk, "error": str(e)[:300], "searched": True,
                "in_universe": in_uni}
    if not item:
        return {"ticker": tk, "empty": True, "searched": True, "in_universe": in_uni,
                "reason": f"近60天没有 {tk} 的新闻"}
    return {"ticker": tk, "asof": item.get("asof"),
            "date": datetime.date.today().isoformat(),
            "bull": item.get("bull") or [], "bear": item.get("bear") or [],
            "net": item.get("net") or "", "evidence": item.get("evidence") or [],
            "searched": True, "in_universe": in_uni}


@app.post("/api/narrative/refresh")
def api_narrative_refresh(ticker: str = Query("")):
    """按需重建单票多空叙事(PWA「🔄更新」按钮)。

    与 /api/narrative/search 是同一条「按需单票深度」路径(🔄 本质就是手动搜一只票)——
    否则会出现「票池外的 RIVN 读全文、票池内的 AAPL 反而只读 600 字」的怪事。
    每日 147 票的批量管线(build.py main)不走这里, 仍用 600 字浅池省 token。
    """
    return api_narrative_search(ticker)


@app.get("/api/narrative/list")
def api_narrative_list():
    """多空 tab 的 ticker 网格: 全部个股(排ETF) + 今日是否已生成多空 + 多/空条数。"""
    days = _narr.list_days() or []
    today = datetime.date.today().isoformat()
    use = today if today in days else (days[0] if days else "")
    built = {}
    for e in ((_narr.load_day(use) or []) if use else []):
        tk = str(e.get("ticker", "")).upper()
        if tk:
            built[tk] = {"asof": e.get("asof"),
                         "nb": len(e.get("bull") or []), "ns": len(e.get("bear") or [])}
    try:
        from narratives import universe as _nu
        uni = _nu.individual_stocks()
    except Exception:
        uni = sorted(built.keys())
    rows = []
    for tk in uni:
        b = built.get(tk)
        rows.append({"ticker": tk, "built": bool(b), "asof": (b or {}).get("asof"),
                     "nb": (b or {}).get("nb", 0), "ns": (b or {}).get("ns", 0)})
    return {"date": use, "count": len(rows), "built": len(built), "tickers": rows}


@app.get("/api/company_profile")
def api_company_profile(ticker: str = Query(..., min_length=1)):
    """读取公司简介缓存(10-Q MD&A + Risk Factors 的 Haiku 中文摘要)。"""
    try:
        from narratives.company_profile import get_profile
        p = get_profile(ticker)
        if p:
            return p
        return {"ticker": ticker.upper(), "empty": True}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


@app.post("/api/company_profile/refresh")
def api_company_profile_refresh(ticker: str = Query(..., min_length=1)):
    """手动刷新: 重新拉最新 10-Q + Haiku 摘要(~30s)。"""
    try:
        from narratives.company_profile import build_and_save
        p = build_and_save(ticker.upper())
        if p:
            return p
        return {"ticker": ticker.upper(), "error": "无法生成(可能该票无 10-Q)"}
    except Exception as e:
        return {"ticker": ticker.upper(), "error": str(e)}


def _ai_review_mod():
    import sys as _s
    tjd = str(TRADE_JOURNAL_DIR)
    if tjd not in _s.path:
        _s.path.insert(0, tjd)
    import ai_review as _air
    return _air


@app.get("/api/journal/ai_review")
def api_journal_ai_review_get():
    """读本地缓存(不调模型): {cached, text, n_closed, asof, stale, n_now} 或 {cached:False}。
    打开 AI 复盘 tab 时调它 → 有缓存秒显, 不每次烧 Haiku。"""
    try:
        c = _ai_review_mod().load_cached()
        if not c:
            return {"cached": False}
        return {"cached": True, "text": c.get("text", ""),
                "n_closed": c.get("n_closed", 0), "asof": c.get("asof", ""),
                "stale": bool(c.get("stale")), "n_now": c.get("n_now")}
    except Exception as e:
        return {"error": str(e)[:300]}


@app.post("/api/journal/ai_review")
def api_journal_ai_review():
    """✦ AI 交易复盘(claude -p Haiku, 走订阅): 重新生成 + 写本地缓存(~15s)。
    FastAPI 把 def 端点丢线程池, 不阻塞。"""
    try:
        return _ai_review_mod().build_review()
    except Exception as e:
        return {"error": str(e)[:300]}


# ── 🤖 AI 持仓新闻分析 ──────────────────────────────────────────────
_posai_job: dict = {}  # {"status","result","ts"}

@app.get("/api/positions/ai_analysis")
def api_positions_ai_get():
    """读缓存 / 查后台任务状态(不调模型)。"""
    if _posai_job.get("status") == "running":
        return {"status": "running"}
    if _posai_job.get("status") == "done":
        return {"status": "done", **_posai_job.get("result", {})}
    try:
        from positions_ai import load_cached
        c = load_cached()
        if c:
            return {"status": "cached", **c}
    except Exception:
        pass
    return {"status": "empty"}

@app.post("/api/positions/ai_analysis")
async def api_positions_ai_post():
    """后台启动 AI 分析(并行 Haiku agents)。立即返回 {status:'running'}。"""
    global _posai_job
    if _posai_job.get("status") == "running":
        return {"status": "running", "msg": "已在运行中"}
    strats = list(state.strategies) if state.strategies else []
    if not strats:
        return {"status": "error", "error": "无持仓数据, 请先刷新"}
    # 给 AI 塞一份当前 underlying 现价 + 昨收(算今日 %),点分析时的快照
    prev = state.equity_prev_close or {}
    for s in strats:
        u = s.get("underlying")
        if u and u in prev:
            s["_prev_close"] = prev[u]
    _posai_job = {"status": "running", "ts": __import__("time").time()}
    loop = asyncio.get_event_loop()
    def _run():
        global _posai_job
        try:
            from positions_ai import analyze_positions
            result = analyze_positions(strats)
            _posai_job = {"status": "done", "result": result}
        except Exception as e:
            _posai_job = {"status": "error", "result": {"error": str(e)[:300]}}
    loop.run_in_executor(None, _run)
    return {"status": "running"}


async def _build_movers_news(thr: float) -> dict:
    """当天 ±thr% 异动票 + 双语新闻。finviz AI/新闻读 finviz_ai 每日库(后台定时爬+预译,
    零网络、已双语);digest 实时匹配、标题按需翻译。movers 取实时快照 → 每次现算。"""
    loop = asyncio.get_event_loop()
    # 1) movers ← heatmap 实时快照
    try:
        from heatmap import heatmap_api as _hm
        snap = await loop.run_in_executor(None, _hm._build_response)
    except Exception as e:
        return {"error": f"snapshot: {e}", "movers": [], "date": "",
                "generated_at": time.time()}
    items = snap.get("items", []) or []
    movers = [it for it in items if abs(it.get("change_pct") or 0.0) >= thr]
    movers.sort(key=lambda x: abs(x.get("change_pct") or 0.0), reverse=True)
    # 2) digest day(最新归档日 = 当天,避时区错位)+ ticker→articles 索引(一次遍历)
    try:
        days = _dgs.list_days() or []
    except Exception:
        days = []
    dg_date = days[0] if days else datetime.date.today().isoformat()
    day_arts = await loop.run_in_executor(None, _digest_day_metas, dg_date)
    dg_index: dict = {}
    for a in (day_arts or []):
        for tk in (a.get("tickers") or []):
            dg_index.setdefault(str(tk).upper(), []).append(a)

    def _digest_news_for(tk: str) -> list:
        out = []
        for a in dg_index.get(tk.upper(), []):
            out.append({
                "id": str(a.get("id")), "src": "digest",
                "hhmm": a.get("hhmm") or "",
                "title_en": a.get("title_en") or a.get("title") or "",
                "title_cn": "", "sentiment": a.get("sentiment"),
                "link": (a.get("links") or [None])[0],
                # 与 /api/digest/translate_article 同样的「非空段」过滤,保证
                # paras_en 与返回的 body_cn 段落一一对齐(未翻文章原文在 cn 字段、en 空)
                "paras_en": [(p.get("en") or p.get("cn") or "")
                             for p in (a.get("paras") or [])
                             if (p.get("en") or p.get("cn"))][:12],
                "has_body": True,
            })
            if len(out) >= _MV_DIGEST_PER_TICKER:
                break
        return out
    # 3) finviz AI/新闻 ← finviz_ai 每日库(后台定时爬好, 已双语, 零网络)
    fvai_date, fvai_idx = await loop.run_in_executor(None, _finviz_ai_store_today)
    # 4) 组装(✦AI 置顶 → digest → finviz 经典);digest 标题待翻, finviz 库条目已双语
    out_movers = []
    seen_titles: dict = {}
    for m in movers:
        tk = m["ticker"]
        e = fvai_idx.get(tk.upper()) or {}
        ai_items = [e["ai"]] if e.get("ai") else []
        fv_classic = (e.get("news") or [])[:_MV_FV_PER_TICKER]
        news = ai_items + _digest_news_for(tk) + fv_classic   # ✦AI → digest → finviz
        for n in news:
            # 仅 digest(title_cn 空)需翻译;finviz 库条目已带 cn
            if n.get("title_en") and not n.get("title_cn") and n["id"] not in seen_titles:
                seen_titles[n["id"]] = n["title_en"]
        out_movers.append({
            "ticker": tk,
            "change_pct": round(m.get("change_pct") or 0.0, 2),
            "last": m.get("last") or 0.0,
            "market_cap": m.get("market_cap") or 0.0,
            "sector": m.get("sector") or "",
            "news": news,
        })
    # 5) digest 标题翻译(.tr.json 按 id 缓存);finviz 库条目已双语, 不重复翻
    cache = _digest_tr_load(dg_date)
    need = [(i, t) for i, t in seen_titles.items() if not cache.get(i, {}).get("t")]
    if need:
        try:
            tr = _digest_import_tr()
            cn = await loop.run_in_executor(None, tr, [t for _, t in need])
            pairs = list(zip([i for i, _ in need], cn))

            def _mut(c):
                for i, v in pairs:
                    if v:
                        c.setdefault(i, {})["t"] = v
            cache = await loop.run_in_executor(None, _digest_tr_update, dg_date, _mut)
        except Exception as e:
            print(f"[movers] title translate err: {e}")
    # 6) 回填 digest 标题中文(finviz 库条目 title_cn/paras_cn 已就位, 不覆盖)
    for mv in out_movers:
        for n in mv["news"]:
            if not n.get("title_cn"):
                n["title_cn"] = cache.get(n["id"], {}).get("t", "") or ""
    with_news = sum(1 for mv in out_movers if mv["news"])
    ai_movers = sum(1 for mv in out_movers
                    if any(n.get("src") == "finviz_ai" for n in mv["news"]))
    return {
        "date": dg_date,
        "generated_at": time.time(),
        "session": snap.get("session"),
        "threshold": thr,
        "fvai_date": fvai_date,
        "counts": {"movers": len(out_movers), "with_news": with_news,
                   "finviz_store": len(fvai_idx), "ai_movers": ai_movers},
        "movers": out_movers,
    }


@app.get("/api/movers/news")
async def api_movers_news(threshold: float = 1.0):
    """当天 ±threshold% 异动票 + 双语新闻。finviz AI/新闻读 finviz_ai 每日库(零网络、已双语);
    movers 取实时快照故每次现算(前端有 2min 缓存)。finviz 抓取全在后台定时爬虫里。"""
    try:
        thr = abs(float(threshold))
    except Exception:
        thr = 1.0
    return await _build_movers_news(thr)


# ── 异动新闻「🤖 AI总结」: 把今天异动票按多空(多消息→多tickers)用 Haiku 总结 ──
# 复用 _aisum_* 缓存/任务系统(scope='movers')+ GET /api/digest/ai_summary/status?scope=movers 轮询。
async def _aisum_run_movers(date, scope, movers=None):
    """后台: 今天异动票 → Haiku 多空总结 → 存缓存。movers 优先用前端传来的(已加载/已译),
    没有才回退服务端现拉 _build_movers_news(慢: 实时快照+翻译)。与请求生命周期解耦。"""
    key = _aisum_key(date, scope)
    from narratives import digest_summary as _dsum
    loop = asyncio.get_event_loop()
    try:
        movers = movers or []
        if not movers:                                    # 回退: 服务端现拉
            payload = await _build_movers_news(1.0)
            movers = payload.get("movers", []) or []
        if not movers:
            _aisum_jobs.setdefault(key, {})["error"] = "今天暂无异动票"
            return
        n_total = len(movers)
        n_cap = min(n_total, _dsum.MOVERS_CAP)
        _aisum_jobs.setdefault(key, {})["n_items"] = n_cap
        summary, model, _n, err = await loop.run_in_executor(
            None, _dsum.summarize_movers, movers)
        if err:
            _aisum_jobs.setdefault(key, {})["error"] = err
        else:
            ids = [m.get("ticker") for m in movers if m.get("ticker")]
            _aisum_save(date, scope, {
                "date": date, "scope": scope, "summary": summary, "ids": ids,
                "model": model, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "n": len(ids), "movers": True})
            _aisum_jobs.setdefault(key, {})["error"] = None
    except Exception as e:
        _aisum_jobs.setdefault(key, {})["error"] = str(e)[:300]
    finally:
        if key in _aisum_jobs:
            _aisum_jobs[key]["running"] = False


def _aisum_start_movers(date, scope, movers=None):
    key = _aisum_key(date, scope)
    j = _aisum_jobs.get(key)
    if j and j.get("running"):
        return False
    _aisum_jobs[key] = {"running": True, "started": time.monotonic(),
                        "error": None, "mode": "movers"}
    _aisum_jobs[key]["task"] = asyncio.create_task(
        _aisum_run_movers(date, scope, movers))
    return True


@app.post("/api/movers/ai_summary")
async def api_movers_ai_summary(req: Request):
    """启动一次后台「今日异动多空总结」(claude -p Haiku), 立刻返回 {status}; 前端复用
    GET /api/digest/ai_summary/status?scope=movers 轮询。
    body {mode:auto|full, movers:[{ticker,change_pct,sector,news}...]}: movers 传前端当前
    异动列表(快, 已译); 不传则服务端现拉(慢)。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    mode = (body or {}).get("mode") or "auto"
    movers = (body or {}).get("movers") or []
    date = _digest_today_str()
    scope = "movers"
    key = _aisum_key(date, scope)
    cache = _aisum_load(date, scope)
    running = bool(_aisum_jobs.get(key, {}).get("running"))
    if mode == "auto" and cache:
        return {"status": "done", "date": date, "scope": scope}
    if running:
        return {"status": "running", "date": date, "scope": scope}
    _aisum_start_movers(date, scope, movers)
    return {"status": "running", "date": date, "scope": scope}


# ── 异动新闻「🎯 AI抄底推荐」: 下跌票 → Haiku 判哪些是暂时利空值得抄底 ──────────
async def _aisum_run_dip(date, scope, movers=None):
    key = _aisum_key(date, scope)
    from narratives import digest_summary as _dsum
    loop = asyncio.get_event_loop()
    try:
        movers = movers or []
        if not movers:
            payload = await _build_movers_news(1.0)
            movers = payload.get("movers", []) or []
        downs = [m for m in movers if (m.get("change_pct") or 0) < -1.0]
        if not downs:
            _aisum_jobs.setdefault(key, {})["error"] = "今天没有跌超1%的票"
            return
        _aisum_jobs.setdefault(key, {})["n_items"] = len(downs)
        summary, model, _n, err = await loop.run_in_executor(
            None, _dsum.summarize_dip_picks, movers)
        if err:
            _aisum_jobs.setdefault(key, {})["error"] = err
        else:
            _aisum_save(date, scope, {
                "date": date, "scope": scope, "summary": summary,
                "ids": [m.get("ticker") for m in downs],
                "model": model, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "n": len(downs), "movers": True})
            _aisum_jobs.setdefault(key, {})["error"] = None
    except Exception as e:
        _aisum_jobs.setdefault(key, {})["error"] = str(e)[:300]
    finally:
        if key in _aisum_jobs:
            _aisum_jobs[key]["running"] = False


@app.post("/api/movers/dip_picks")
async def api_movers_dip_picks(req: Request):
    """启动「AI抄底推荐」(只看下跌票, Haiku 判暂时/结构性利空)。
    复用 _aisum 缓存(scope='dip'), 前端轮询 /api/digest/ai_summary/status?scope=dip。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    mode = (body or {}).get("mode") or "auto"
    movers = (body or {}).get("movers") or []
    date = _digest_today_str()
    scope = "dip"
    key = _aisum_key(date, scope)
    cache = _aisum_load(date, scope)
    running = bool(_aisum_jobs.get(key, {}).get("running"))
    if mode == "auto" and cache:
        return {"status": "done", "date": date, "scope": scope}
    if running:
        return {"status": "running", "date": date, "scope": scope}
    _aisum_jobs[key] = {"running": True, "started": time.monotonic(),
                        "error": None, "mode": "dip"}
    _aisum_jobs[key]["task"] = asyncio.create_task(
        _aisum_run_dip(date, scope, movers))
    return {"status": "running", "date": date, "scope": scope}


# ── 异动新闻「🚀 AI选股」: 两个 Haiku 并行(涨票momentum + 跌票抄底/避开) ──────────
async def _aisum_run_picks(date, scope, movers=None):
    key = _aisum_key(date, scope)
    from narratives import digest_summary as _dsum
    loop = asyncio.get_event_loop()
    try:
        movers = movers or []
        if not movers:
            payload = await _build_movers_news(1.0)
            movers = payload.get("movers", []) or []
        ups = [m for m in movers if (m.get("change_pct") or 0) > 1.0]
        downs = [m for m in movers if (m.get("change_pct") or 0) < -1.0]
        n_total = len(ups) + len(downs)
        if not n_total:
            _aisum_jobs.setdefault(key, {})["error"] = "今天没有涨跌超1%的票"
            return
        _aisum_jobs.setdefault(key, {})["n_items"] = n_total
        up_task = loop.run_in_executor(None, _dsum.summarize_momentum_up, movers)
        down_task = loop.run_in_executor(None, _dsum.summarize_dip_picks, movers)
        (up_sum, up_model, n_up, up_err), (down_sum, down_model, n_down, down_err) = \
            await asyncio.gather(up_task, down_task)
        parts = []
        if up_sum:
            parts.append(up_sum)
        elif up_err and ups:
            parts.append(f"> ⚠️ 上涨分析失败: {up_err}")
        if down_sum:
            parts.append(down_sum)
        elif down_err and downs:
            parts.append(f"> ⚠️ 下跌分析失败: {down_err}")
        if not parts:
            _aisum_jobs.setdefault(key, {})["error"] = up_err or down_err or "无数据"
            return
        combined = "\n\n---\n\n".join(parts)
        _aisum_save(date, scope, {
            "date": date, "scope": scope, "summary": combined,
            "ids": [m.get("ticker") for m in ups + downs],
            "model": up_model, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "n": n_total, "n_up": n_up, "n_down": n_down, "movers": True})
        _aisum_jobs.setdefault(key, {})["error"] = None
    except Exception as e:
        _aisum_jobs.setdefault(key, {})["error"] = str(e)[:300]
    finally:
        if key in _aisum_jobs:
            _aisum_jobs[key]["running"] = False


@app.post("/api/movers/picks")
async def api_movers_picks(req: Request):
    """启动「AI选股」: 两个 Haiku 并行(涨票momentum + 跌票抄底/避开)。
    scope='picks', 前端轮询 /api/digest/ai_summary/status?scope=picks。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    mode = (body or {}).get("mode") or "auto"
    movers = (body or {}).get("movers") or []
    date = _digest_today_str()
    scope = "picks"
    key = _aisum_key(date, scope)
    cache = _aisum_load(date, scope)
    running = bool(_aisum_jobs.get(key, {}).get("running"))
    if mode == "auto" and cache:
        return {"status": "done", "date": date, "scope": scope}
    if running:
        return {"status": "running", "date": date, "scope": scope}
    _aisum_jobs[key] = {"running": True, "started": time.monotonic(),
                        "error": None, "mode": "picks"}
    _aisum_jobs[key]["task"] = asyncio.create_task(
        _aisum_run_picks(date, scope, movers))
    return {"status": "running", "date": date, "scope": scope}


@app.get("/api/movers/picks/dates")
def api_movers_picks_dates():
    """返回所有有 picks 缓存的日期列表(倒序), 供前端日历渲染。"""
    d = Path(__file__).resolve().parent.parent / "benzinga_digest" / "ai_summary"
    dates = []
    try:
        for f in d.glob("*__picks.json"):
            dt = f.stem.split("__")[0]
            if len(dt) == 10:
                dates.append(dt)
    except Exception:
        pass
    dates.sort(reverse=True)
    return {"dates": dates}


@app.get("/api/movers/picks/history")
def api_movers_picks_history(date: str = Query("")):
    """读取指定日期的 picks 缓存。"""
    if not date:
        return {"error": "need date"}
    cache = _aisum_load(date, "picks")
    if not cache:
        return {"status": "empty", "date": date}
    r = {"status": "done", "summary": cache.get("summary", ""),
         "model": cache.get("model", ""), "ts": cache.get("ts", ""),
         "n": cache.get("n", 0), "date": date}
    if cache.get("n_up") is not None:
        r["n_up"] = cache["n_up"]
    if cache.get("n_down") is not None:
        r["n_down"] = cache["n_down"]
    return r


# ── finviz AI 定时爬虫:交易日 6 次 / 非交易日 2 次,慢速存进 finviz_ai 每日库 ──────
def _finviz_ai_universe() -> list:
    """读 data/options_vol/core_universe.txt(核心 399 票, 一行一个)。"""
    p = Path(__file__).resolve().parent.parent / "data" / "options_vol" / "core_universe.txt"
    out = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                out.append(s.split()[0].upper())
    except Exception:
        pass
    return out


async def _finviz_ai_crawl_once() -> int:
    """爬全 universe 的 finviz AI(慢速 ~1 req/s + UA/cookie 轮换)→ 只译变化内容(复用
    .tr.json 按 id 缓存, 同一新闻 id 稳定→跨轮不重复翻)→ 双语条目 upsert 进 finviz_ai
    每日库。返回写入的票数。"""
    loop = asyncio.get_event_loop()
    tickers = _finviz_ai_universe()
    if not tickers:
        return 0
    # 1) 慢速抓全(force=True 刷新 + gather 内固定 ~1 req/s + UA 轮换)
    fv_map = await loop.run_in_executor(
        None, lambda: _mv_gather_finviz(tickers, 36, force=True, allow_fetch=True))
    if not fv_map:
        return 0
    date = datetime.date.today().isoformat()
    # 2) 收集待翻(标题 + AI 正文),复用 .tr.json 按 id 缓存
    cache = _digest_tr_load(date)
    title_need, body_need = {}, {}
    for items in fv_map.values():
        for n in items:
            nid = n.get("id")
            if n.get("title_en") and not cache.get(nid, {}).get("t"):
                title_need[nid] = n["title_en"]
            if n.get("src") == "finviz_ai" and n.get("paras_en") and not cache.get(nid, {}).get("b"):
                body_need[nid] = n["paras_en"]
    if title_need:
        try:
            tr = _digest_import_tr()
            cn = await loop.run_in_executor(None, tr, list(title_need.values()))
            pairs = list(zip(title_need.keys(), cn))

            def _mt(c):
                for i, v in pairs:
                    if v:
                        c.setdefault(i, {})["t"] = v
            cache = await loop.run_in_executor(None, _digest_tr_update, date, _mt)
        except Exception as e:
            print("[finviz_ai] 标题翻译出错:", e)
    if body_need:
        try:
            tr = _digest_import_tr()
            flat, spans = [], []
            for nid, paras in body_need.items():
                spans.append((nid, len(paras)))
                flat.extend(paras)
            cn_flat = await loop.run_in_executor(None, tr, flat)
            by_id, pos = {}, 0
            for nid, ln in spans:
                by_id[nid] = cn_flat[pos:pos + ln]
                pos += ln

            def _mb(c):
                for nid, arr in by_id.items():
                    c.setdefault(nid, {})["b"] = arr
            cache = await loop.run_in_executor(None, _digest_tr_update, date, _mb)
        except Exception as e:
            print("[finviz_ai] 正文翻译出错:", e)
    # 3) 组装双语条目(每票:AI 条目 + 经典新闻 top-N, 丢中性)
    entries = []
    for tk, items in fv_map.items():
        ai, news = None, []
        for n in items:
            nid = n.get("id")
            n2 = dict(n)
            n2["title_cn"] = cache.get(nid, {}).get("t", "") or n2.get("title_cn", "")
            if n.get("src") == "finviz_ai":
                n2["paras_cn"] = cache.get(nid, {}).get("b", []) or n2.get("paras_cn", [])
                ai = n2
            elif n.get("sentiment") not in (0, None):
                news.append(n2)
        entries.append({"ticker": tk, "crawled_at": time.time(),
                        "ai": ai, "news": news[:_MV_FV_PER_TICKER]})
    # 4) upsert(本轮没抓到的票保留上一轮 → 慢速分轮也能凑齐)
    await loop.run_in_executor(None, _fvai.upsert_day, date, entries, "ticker")
    return len(entries)


async def finviz_ai_crawl_loop():
    """定时爬 finviz AI 存库:交易日(周一-五)08:00/10:30/12:30/14:00/16:00/21:00,
    非交易日(周末)12:00/20:00(本地时钟)。慢速 ~1 req/s + UA/cookie 轮换 → 不会被封。
    开机:今天库空则立即补爬一次(Mover News / digest 立刻有数据)。
    注:节假日按工作日处理(多爬几次, 无害)。"""
    TRADING = [(8, 0), (10, 30), (12, 30), (14, 0), (16, 0), (21, 0)]
    NONTRADING = [(12, 0), (20, 0)]
    await asyncio.sleep(30)
    try:
        if not _fvai.load_day(datetime.date.today().isoformat()):
            n = await _finviz_ai_crawl_once()
            print(f"[finviz_ai] 开机补爬: {n} 票")
    except Exception as e:
        print("[finviz_ai] 开机补爬出错:", e)
    while True:
        now = datetime.datetime.now()
        slots = TRADING if now.weekday() < 5 else NONTRADING
        todays = [now.replace(hour=h, minute=mi, second=0, microsecond=0)
                  for h, mi in slots]
        future = [t for t in todays if t > now]
        if future:
            nxt = min(future)
        else:
            tmr = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            sl2 = TRADING if tmr.weekday() < 5 else NONTRADING
            h, mi = sl2[0]
            nxt = tmr.replace(hour=h, minute=mi)
        await asyncio.sleep(max(30, (nxt - now).total_seconds()))
        try:
            n = await _finviz_ai_crawl_once()
            print(f"[finviz_ai] 定时爬取 {nxt:%m-%d %H:%M}: {n} 票")
        except Exception as e:
            print("[finviz_ai] 定时爬取出错:", e)



@app.get("/api/translate/status")
def api_translate_status():
    """各翻译源当前状态(顶栏「翻译源」图标用)。"""
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from benzinga_mcp.digest import translate_status
    return translate_status()


@app.post("/api/translate/test")
async def api_translate_test(req: Request):
    """实测某一家翻译源 → {ok, text/error, ms}(给「点哪家看为啥不行」)。"""
    body = await req.json()
    prov = str((body or {}).get("provider", "")).strip()
    if not prov:
        return {"ok": False, "error": "缺 provider"}
    src = str(Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from benzinga_mcp.digest import translate_test
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, translate_test, prov)


def _digest_sentiment_badge(s) -> str:
    """numeric sentiment(-3..3) → small colored badge HTML (or '')."""
    try:
        v = int(s)
    except (TypeError, ValueError):
        return ""
    if v > 0:
        c, lbl = "#3fb950", "▲ Bullish +" + str(v)
    elif v < 0:
        c, lbl = "#f85149", "▼ Bearish " + str(v)
    else:
        c, lbl = "#8b949e", "● Neutral"
    return ('<span class="dg-sent" style="color:' + c + ';border-color:' + c
            + '">' + lbl + "</span>")


def _digest_ticker_links(tickers, sep: str = " · ") -> str:
    """Render digest tickers as clickable deep-links → TA single-ticker page (via
    the global [data-ta-tk] click delegate in the main PWA). Keeps the raw text
    (may carry a leading $); the data attr holds the cleaned uppercase symbol."""
    import html as _h
    parts = []
    for t in tickers or []:
        raw = str(t).strip()
        if not raw:
            continue
        clean = raw.upper().lstrip("$")
        parts.append('<span class="dg-tk-link" data-ta-tk="' + _h.escape(clean)
                     + '" style="cursor:pointer;color:#58a6ff" title="点击看 '
                     + _h.escape(clean) + ' 单票详情">' + _h.escape(raw) + "</span>")
    return sep.join(parts)


def _digest_toc_item(m: dict, label: str) -> str:
    """One <li> for the digest TOC. label = hhmm (single-day) or 'date hhmm' (search)."""
    import html as _h
    tks = m.get("tickers") or []
    return ('<li><code>' + _h.escape(label) + '</code> '
            '<a href="#a' + _h.escape(str(m.get("id", ""))) + '">'
            + _h.escape(m.get("title", "")) + '</a>'
            + (' <span class="dg-tk">' + _digest_ticker_links(tks, " ") + '</span>' if tks else "") + "</li>")


def _digest_article_html(m: dict, label: str) -> str:
    """Render one article block (English source: sentiment badge, tickers/quotes/
    links meta, body paragraphs, inline images). label shown in the <h3> header."""
    import html as _h
    out = ['<div class="dg-art"><a id="a' + _h.escape(str(m.get("id", ""))) + '"></a>']
    badge = _digest_sentiment_badge(m.get("sentiment"))
    out.append("<h3>[" + _h.escape(label) + "] "
               + _h.escape(m.get("title", "")) + (" " + badge if badge else "") + "</h3>")
    if m.get("channels"):
        out.append('<div class="dg-meta">🗂 ' + " ".join(
            '<span class="dg-chan">' + _h.escape(str(c)) + "</span>" for c in m["channels"][:12]) + "</div>")
    if m.get("tickers"):
        out.append('<div class="dg-meta">🏷 ' + _digest_ticker_links(m["tickers"]) + "</div>")
    if m.get("teaser"):
        out.append('<div class="dg-teaser">' + _h.escape(m["teaser"]) + "</div>")
    if m.get("quotes"):
        out.append('<div class="dg-meta">💹 ' + _h.escape(" · ".join(m["quotes"])) + "</div>")
    links = m.get("links") or []
    if links:
        ls = " · ".join('<a href="' + _h.escape(u) + '" target="_blank" rel="noopener">link ' + str(i + 1) + "</a>"
                        for i, u in enumerate(links[:12]))
        out.append('<div class="dg-meta">🔗 ' + ls + "</div>")
    for p in (m.get("paras") or []):
        txt = p.get("cn") or p.get("en")  # raw crawl stores English in cn slot
        if txt:
            out.append('<p class="dg-cn">' + _h.escape(txt) + "</p>")
    for u in (m.get("images") or [])[:8]:
        out.append('<img class="dg-img" loading="lazy" src="' + _h.escape(u) + '" alt="">')
    out.append('<a href="#toc" class="dg-back">↑ 回目录</a></div>')
    return "\n".join(out)


def _digest_html_from_metas(date: str, metas: list) -> str:
    """Build English HTML for a single day from the structured .json metas."""
    metas = sorted(metas, key=lambda m: m.get("ts", ""), reverse=True)
    out = ["<h1>Benzinga Digest " + date + "（" + str(len(metas)) + " 条）</h1>",
           "<blockquote>English source · images + links + meta stored locally · newest first · 点目录跳转</blockquote>",
           '<a id="toc"></a><h2>📑 目录</h2><ul class="dg-toc">']
    out += [_digest_toc_item(m, m.get("hhmm", "")) for m in metas]
    out.append("</ul>")
    out += [_digest_article_html(m, m.get("hhmm", "")) for m in metas]
    return "\n".join(out)


def _digest_search_html(metas: list, filt: dict, scanned_days: int, truncated: bool) -> str:
    """Build HTML for cross-day search results. Each item labeled with its date."""
    import html as _h
    parts = []
    if filt.get("ticker"):
        parts.append("ticker=" + filt["ticker"])
    if filt.get("title"):
        parts.append("标题含「" + filt["title"] + "」")
    if filt.get("body"):
        parts.append("正文含「" + filt["body"] + "」")
    if filt.get("channel"):
        parts.append("分类含「" + filt["channel"] + "」")
    rng = (filt.get("start") or "最早") + " ~ " + (filt.get("end") or "最新")
    summary = "区间 " + rng + ("  ·  " + " · ".join(parts) if parts else "")
    out = ["<h1>🔎 Digest 搜索结果（" + str(len(metas)) + " 条"
           + ("，已截断" if truncated else "") + "）</h1>",
           "<blockquote>" + _h.escape(summary) + "  ·  扫描 " + str(scanned_days) + " 天</blockquote>"]
    if not metas:
        out.append('<div style="color:#8b949e;padding:30px;text-align:center">无匹配结果</div>')
        return "\n".join(out)
    out.append('<a id="toc"></a><h2>📑 目录</h2><ul class="dg-toc">')
    out += [_digest_toc_item(m, (m.get("_date", "") + " " + m.get("hhmm", "")).strip()) for m in metas]
    out.append("</ul>")
    out += [_digest_article_html(m, (m.get("_date", "") + " " + m.get("hhmm", "")).strip()) for m in metas]
    return "\n".join(out)


@app.get("/api/digest")
async def api_digest(date: str = Query("")):
    dates = _digest_available_dates()
    if not dates:
        return {"error": "尚无 digest（回填/翻译进行中）", "dates": []}
    if not date or date not in dates:
        date = dates[0]  # latest
    try:
        metas = _dgs.load_day(date)
    except Exception:
        metas = None
    count = None
    bilingual = False
    if isinstance(metas, list) and metas and all(isinstance(m, dict) and "paras" in m for m in metas):
        html = _digest_html_from_metas(date, metas)
        count = len(metas)
        bilingual = True
    elif isinstance(metas, list) and metas:
        # 理论到不了(全部新格式);.md 已被 compact 删除,仍尽力从 metas 渲染。
        html = _digest_html_from_metas(date, metas)
        count = len(metas)
        bilingual = True
    else:
        return {"error": "该日无可渲染数据", "date": date, "dates": dates}
    try:
        _dp = _dgs.day_path(date)
        gen = (datetime.datetime.fromtimestamp(_dp.stat().st_mtime).strftime("%m-%d %H:%M")
               if _dp else "")
    except Exception:
        gen = ""
    return {"date": date, "count": count, "generated_at": gen, "html": html,
            "bilingual": bilingual, "dates": dates}


@app.get("/api/digest/calendar")
async def api_digest_calendar():
    """date -> 条数, 给 Digest 日历用。历史日走 .counts.json 索引(compact 维护,
    免解析 8GB);索引缺的日子(今天/昨天/未压缩)实时数 load_day。"""
    counts = dict(_dgs.load_counts_index())
    try:
        for d in _dgs.list_days():
            if d not in counts:
                counts[d] = _dgs.day_count(d)
    except Exception:
        pass
    return {"counts": counts}


# ── 📰 双语 (Chrome extension ⇄ PWA sync) ──
# The bilingual Chrome extension scrapes WSJ / Barron's / MarketWatch and pushes
# article lists + read/saved/deleted state here. PWA sub-tab reads/edits the
# same file. Merge policy: extension is authoritative on article data; state
# maps merge by max ts, with a 60s grace window so stale extension pushes
# can't wipe recent PWA edits.
_BILINGUAL_PATH = Path(__file__).resolve().parent / "data" / "bilingual_news.json"


def _bilingual_default() -> dict:
    return {
        "articles": [],
        "sections": [],
        "seen": {},
        "saved": {},
        "deleted": {},
        "updatedAt": 0,
    }


def _bilingual_load() -> dict:
    try:
        if _BILINGUAL_PATH.exists():
            d = json.loads(_BILINGUAL_PATH.read_text(encoding="utf-8"))
            base = _bilingual_default()
            base.update(d or {})
            # normalise state maps to dicts (older files might be lists)
            for k in ("seen", "saved", "deleted"):
                if not isinstance(base.get(k), dict):
                    base[k] = {}
            if not isinstance(base.get("articles"), list):
                base["articles"] = []
            if not isinstance(base.get("sections"), list):
                base["sections"] = []
            return base
    except Exception as e:
        print(f"[bilingual] load error: {e}")
    return _bilingual_default()


def _bilingual_save(state: dict) -> None:
    try:
        _BILINGUAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BILINGUAL_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_BILINGUAL_PATH)
    except Exception as e:
        print(f"[bilingual] save error: {e}")


_bilingual_state: dict = _bilingual_load()


async def _zeus_refresh_freenews():
    """Pull Finviz / WSJ / MarketWatch into the bilingual store the Digest reads."""
    global _bilingual_state
    if not _zeus_news.enabled_sources():
        return {"disabled": True}
    snapshot = dict(_bilingual_state)
    new_state, report = await asyncio.get_event_loop().run_in_executor(None, _zeus_news.refresh, snapshot)
    # keep read / saved / deleted marks made while we were fetching
    urls = {a.get("url") for a in new_state.get("articles", [])}
    for k in ("seen", "saved", "deleted"):
        merged = dict(new_state.get(k) or {})
        for u, v in (_bilingual_state.get(k) or {}).items():
            if u in urls or k == "saved":
                merged[u] = v
        new_state[k] = merged
    _bilingual_state = new_state
    _bilingual_save(_bilingual_state)
    if report.get("new"):
        print(f"[freenews] +{report['new']} new ({report.get('total')} kept) {report}")
    return report


async def free_news_loop():
    await asyncio.sleep(6)
    while True:
        try:
            await _zeus_refresh_freenews()
        except Exception as e:
            print(f"[freenews] refresh error: {e}")
        await asyncio.sleep(300)


# 纯合并逻辑抽到 bilingual_merge.py(可单测 test_bilingual_merge.py)。
# ⚠️ seen/deleted 用 drop_missing=False(单调并集)→ 修「已读文章被 sync 漏掉就复活」bug。
from bilingual_merge import merge_ts_map as _merge_ts_map  # noqa: E402


@app.post("/api/bilingual/sync")
async def api_bilingual_sync(payload: dict = Body(...)):
    """Extension pushes its full state. Article list is overwritten; state maps
    are merged by max ts with a 60s grace window for deletions. Returns the
    merged state maps so the extension can overwrite its local state."""
    global _bilingual_state
    payload = payload or {}
    updated_at = int(payload.get("updatedAt") or 0)
    articles = payload.get("articles") or []
    sections = payload.get("sections") or []
    if not isinstance(articles, list):
        articles = []
    if not isinstance(sections, list):
        sections = []

    # 补 date 字段:extension 该给的都给了,但历史/异常 payload 可能缺;归档口径用 published date,
    # 缺 date 就按 ts 换成 ET 日期兜底,再没有就用今天。
    try:
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        _today = datetime.datetime.now(_ET).strftime("%Y-%m-%d")
        for a in articles:
            if not isinstance(a, dict) or a.get("date"):
                continue
            ts_ms = a.get("ts")
            if ts_ms:
                try:
                    a["date"] = datetime.datetime.fromtimestamp(int(ts_ms) / 1000, tz=_ET).strftime("%Y-%m-%d")
                    continue
                except Exception:
                    pass
            a["date"] = _today
    except Exception:
        pass

    # seen/deleted 单调:server 有、扩展这次没推的,不删 → 已读/已删不会被一次 sync 复活。
    merged_seen = _merge_ts_map(_bilingual_state.get("seen", {}),
                                 payload.get("seen") or {}, updated_at, drop_missing=False)
    merged_deleted = _merge_ts_map(_bilingual_state.get("deleted", {}),
                                    payload.get("deleted") or {}, updated_at, drop_missing=False)
    # saved 保留删传播:un-save 是作者真实操作,扩展漏掉即视为取消收藏。
    merged_saved = _merge_ts_map(_bilingual_state.get("saved", {}),
                                  payload.get("saved") or {}, updated_at, drop_missing=True)

    _bilingual_state = {
        "articles": articles,
        "sections": sections,
        "seen": merged_seen,
        "saved": merged_saved,
        "deleted": merged_deleted,
        "updatedAt": max(updated_at, int(_bilingual_state.get("updatedAt") or 0)),
    }
    _bilingual_save(_bilingual_state)
    return {
        "ok": True,
        "count": len(articles),
        "merged": {
            "seen": merged_seen,
            "saved": merged_saved,
            "deleted": merged_deleted,
        },
    }


@app.get("/api/bilingual/data")
async def api_bilingual_data():
    """Return the full cached bilingual state."""
    s = _bilingual_state
    return {
        "articles": s.get("articles", []),
        "sections": s.get("sections", []),
        "seen": s.get("seen", {}),
        "saved": s.get("saved", {}),
        "deleted": s.get("deleted", {}),
        "updatedAt": s.get("updatedAt", 0),
    }


@app.post("/api/bilingual/state")
async def api_bilingual_state(payload: dict = Body(...)):
    """Frontend triggers single/bulk edits. Updates in-memory + disk, returns
    the new state maps so the client can rebuild its local view."""
    global _bilingual_state
    payload = payload or {}
    action = str(payload.get("action") or "")
    url = payload.get("url")
    urls = payload.get("urls") or []
    if url and not urls:
        urls = [url]

    now_ms = int(time.time() * 1000)
    seen = dict(_bilingual_state.get("seen") or {})
    saved = dict(_bilingual_state.get("saved") or {})
    deleted = dict(_bilingual_state.get("deleted") or {})

    if action == "markSeen":
        for u in urls:
            if u:
                seen[u] = now_ms
    elif action == "unmarkSeen":
        for u in urls:
            seen.pop(u, None)
    elif action == "addSave":
        for u in urls:
            if u:
                saved[u] = now_ms
                # opening/saving implicitly marks read
                seen.setdefault(u, now_ms)
    elif action == "removeSave" or action == "bulkRemoveSave":
        for u in urls:
            saved.pop(u, None)
    elif action == "deleteArticle" or action == "bulkDelete":
        for u in urls:
            if u:
                deleted[u] = now_ms
                seen.setdefault(u, now_ms)
    elif action == "restoreArticle":
        for u in urls:
            deleted.pop(u, None)
    elif action == "clearDeleted":
        deleted = {}
    else:
        return JSONResponse({"ok": False, "error": f"unknown action: {action}"}, status_code=400)

    _bilingual_state["seen"] = seen
    _bilingual_state["saved"] = saved
    _bilingual_state["deleted"] = deleted
    _bilingual_state["updatedAt"] = now_ms
    _bilingual_save(_bilingual_state)
    return {"ok": True, "seen": seen, "saved": saved, "deleted": deleted}


# ── nano 翻译进度 + 前端控制(仅 nano；opus 只读进度) ──
def _nano_paths():
    bdir = _DIGEST_DIR.parent
    return {
        "prog": _DIGEST_DIR / "_bodies_progress.json",
        "opus_prog": _DIGEST_DIR / "_bodies_opus_progress.json",
        "control": _DIGEST_DIR / "_bodies_control.json",
        "pid": _DIGEST_DIR / "_bodies_nano.pid",
        "stdout": _DIGEST_DIR / "_bodies_stdout.log",
        "py": bdir / ".venv_opus" / "bin" / "python",
        "bdir": bdir,
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _nano_read_pid():
    try:
        return int(_nano_paths()["pid"].read_text().strip())
    except Exception:
        return None


def _digest_total_days() -> int:
    try:
        return len(_dgs.list_days())
    except Exception:
        return 0


@app.get("/api/digest/nano/status")
async def api_digest_nano_status():
    """nano 翻译进度 + 状态(供 PWA 进度条/控制)；opus 只读进度一并返回。"""
    p = _nano_paths()

    def _load(fp):
        try:
            return json.loads(fp.read_text())
        except Exception:
            return {}

    npr, opr = _load(p["prog"]), _load(p["opus_prog"])
    nano_days = [k for k, v in npr.items() if v is True]
    opus_days = [k for k, v in opr.items() if v is True]
    done = len(set(nano_days) | set(opus_days))
    total = _digest_total_days()
    cost = float(npr.get("_openai_cost", 0.0) or 0.0)
    pid = _nano_read_pid()
    alive = bool(pid and _pid_alive(pid))
    try:
        ctrl = json.loads(p["control"].read_text()).get("nano", "run")
    except Exception:
        ctrl = "run"
    state = "stopped" if not alive else ("paused" if ctrl == "pause" else "running")
    now = time.time()
    prog_m = pid_m = None
    try:
        prog_m = p["prog"].stat().st_mtime
    except Exception:
        pass
    try:
        pid_m = p["pid"].stat().st_mtime          # nano 本次启动时刻
    except Exception:
        pass
    advanced = bool(prog_m and pid_m and prog_m > pid_m)   # 启动后是否完成过新天
    run_secs = int(now - pid_m) if pid_m else None
    age = int(now - prog_m) if prog_m else None
    # 卡住 = 运行中 且 启动>120s 仍没完成任何新天(给冷启动/缓存跳过宽限)
    stuck = bool(state == "running" and run_secs is not None and run_secs > 120 and not advanced)
    return {
        "state": state, "stuck": stuck,
        "done": done, "total": total, "remaining": max(0, total - done),
        "pct": round(done / total * 100, 1) if total else 0,
        "nano_days": len(nano_days), "opus_days": len(opus_days),
        "nano_oldest": min(nano_days) if nano_days else None,
        "opus_frontier": max(opus_days) if opus_days else None,
        "cost": round(cost, 2), "budget": 150,
        "idle_sec": (run_secs if (stuck and run_secs is not None) else age),
    }


@app.post("/api/digest/nano/control")
async def api_digest_nano_control(req: Request):
    """前端控制 nano：start / resume / pause / stop。"""
    body = await req.json()
    action = (body or {}).get("action", "")
    p = _nano_paths()

    def _set(v):
        try:
            p["control"].write_text(json.dumps({"nano": v}))
        except Exception:
            pass

    pid = _nano_read_pid()
    alive = bool(pid and _pid_alive(pid))
    if action in ("start", "resume"):
        _set("run")
        if not alive:
            env = dict(os.environ)
            env["BENZINGA_ENGINE"] = "openai"
            # 3 而非 6: 并发 HTTPS 到 api.openai.com 会间歇性 SSL read 挂起(实测 6 并发约 1/6
            # 挂起, ≤4 基本 0), 挂起线程被 _urlopen_hard 遗弃后越积越多 → 拖慢/泄漏。低并发更稳,
            # 配合 backfill_bodies 的 opus 兜底足够快。"稳定比不跑东西强"。
            env.setdefault("BENZINGA_OPENAI_WORKERS", "3")
            # 只标题模式(作者要求 2026-06-04): 全量天只翻标题, 正文点开文章时按需 Google 翻
            # (/api/digest/translate_article)。标题轻、几乎不触发 SSL 挂起 → 快很多。
            env["BENZINGA_TITLES_ONLY"] = "1"
            env["BENZINGA_BODIES_YEARS"] = ""   # 空=全部天的标题
            env["BENZINGA_OPENAI_BUDGET"] = "150"
            py = str(p["py"]) if p["py"].exists() else sys.executable
            try:
                lf = open(p["stdout"], "a")
                subprocess.Popen([py, "backfill_bodies.py"], cwd=str(p["bdir"]),
                                 env=env, stdout=lf, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True, "state": "running"}
    if action == "pause":
        _set("pause")
        return {"ok": True, "state": "paused", "note": "当天处理完后生效"}
    if action == "stop":
        _set("stop")
        if alive:
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        try:
            p["pid"].unlink()
        except Exception:
            pass
        return {"ok": True, "state": "stopped"}
    return {"ok": False, "error": "unknown action: " + str(action)}


@app.get("/api/atr")
async def api_atr(symbols: str = Query("")):
    """Return ATR-14 in dollars for each requested symbol. Cached 1h.

    Used by the dashboard spread visualization to give a price-history-based
    daily-move estimate alongside the IV-implied 1σ move. ATR captures actual
    realized volatility; IV captures market-implied forward expectation —
    showing both lets the user see when they disagree.
    """
    syms = sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    if not syms:
        return {"atr": {}}
    out: dict = {}
    now = time.time()
    missing: list = []
    for s in syms:
        c = _atr_cache.get(s)
        if c and now - c["ts"] < _ATR_TTL_SEC:
            out[s] = c["atr"]
        else:
            missing.append(s)
    if missing:
        loop = asyncio.get_event_loop()
        # One yfinance batch — much faster than per-ticker calls.
        results = await loop.run_in_executor(None, _compute_atr14_batch, missing)
        for s in missing:
            atr = results.get(s)
            _atr_cache[s] = {"atr": atr, "ts": now}
            out[s] = atr
    return {"atr": out}


@app.get("/api/ticker-analysis/{ticker}")
async def api_ticker_analysis(ticker: str):
    ticker = ticker.upper()
    if ticker in _analysis_cache and time.time() - _analysis_cache[ticker]["ts"] < 3600:
        return _analysis_cache[ticker]["data"]
    if not HAS_YF:
        return {"error": "yfinance not installed"}
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: _compute_ticker_analysis(ticker))
        data = _sanitize_for_json(data)
        _analysis_cache[ticker] = {"data": data, "ts": time.time()}
        return data
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


@app.get("/api/version")
async def get_version():
    return {"v": _server_version}

@app.get("/static/sw.js")
async def serve_sw():
    from fastapi.responses import Response
    sw_content = f"""const V = '{_server_version}';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => {{
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k=>k!=='trading-hub-v'+V).map(k => caches.delete(k)))));
  self.clients.claim();
}});
self.addEventListener('fetch', (e) => {{
  // Always go to network for HTML pages and API calls — never serve stale cache
  const url = new URL(e.request.url);
  if (e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.startsWith('/api/')) {{
    e.respondWith(fetch(e.request));
    return;
  }}
  // Static assets: network first, cache fallback
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
}});

// When the user clicks a notification: focus an existing PWA window if one
// is open, otherwise open a fresh one — both routed to ?tab=decisions so the
// Decision Log tab is active.
self.addEventListener('notificationclick', (event) => {{
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/?tab=journal&sub=decisions';
  event.waitUntil((async () => {{
    const allClients = await clients.matchAll({{type: 'window', includeUncontrolled: true}});
    for (const c of allClients) {{
      // If we already have a tab on our origin, focus it.
      if (c.url && c.url.includes(self.location.host)) {{
        await c.focus();
        if ('navigate' in c) await c.navigate(target).catch(() => {{}});
        return;
      }}
    }}
    if (clients.openWindow) await clients.openWindow(target);
  }})());
}});
"""
    return Response(content=sw_content, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, no-store"})


# Mount static files for PWA assets (icons, manifest)
# ── Barchart tab: 多选榜单看交集 + 一键爬取 ──────────────────────────
_BC_DIR = PROJECT_ROOT / "barchart"
# (key, 标签, 跳转url, 分层tier, 是否可勾选交集)  key 须与 barchart/pages.py 对应
_BC_LISTS = [
    ("my_screener",           "🎯 我的动量筛选器",     "https://www.barchart.com/stocks/stocks-screener?screener=492801&viewName=190773", "screener", True),
    ("opt_iv_range",          "IV vs 已实现波动",      "https://www.barchart.com/options/implied-volatility-range",        "iv", True),
    ("opt_iv_rank",           "IV Rank & Percentile",  "https://www.barchart.com/options/iv-rank-percentile",              "iv", True),
    ("opt_upcoming_earnings", "临财报期权",            "https://www.barchart.com/options/upcoming-earnings",               "iv", True),
    ("stk_top100",            "Top100 (Weighted Alpha)","https://www.barchart.com/stocks/top-100-stocks",                  "mom", True),
    ("inv_standout",          "持续跑赢 SPX",          "https://www.barchart.com/investing-ideas/standout-stocks",         "mom", True),
    ("inv_short_interest",    "高空头持仓",            "https://www.barchart.com/investing-ideas/short-interest-stocks",   "struct", True),
    ("stk_price_surprises",   "价格异动",              "https://www.barchart.com/stocks/price-surprises",                  "idea", True),
    ("inv_rsi_bull_div",      "RSI 底背离",            "https://www.barchart.com/investing-ideas/rsi-bullish-divergence",  "idea", True),
    ("inv_rsi_bear_div",      "RSI 顶背离",            "https://www.barchart.com/investing-ideas/rsi-bearish-divergence",  "idea", True),
    ("stk_industry_rankings", "行业组排名",            "https://www.barchart.com/stocks/sectors/industry-rankings",        "idea", False),
]
_bc_scrape_state = {"running": False, "msg": "", "ts": None}


@app.get("/api/barchart/lists")
async def barchart_lists():
    """读各榜单已爬到本地的 tickers（barchart/downloads/pages/<key>_tickers.txt）。"""
    pages_dir = _BC_DIR / "downloads" / "pages"
    out = []
    for key, label, url, tier, sel in _BC_LISTS:
        f = pages_dir / f"{key}_tickers.txt"
        tickers, asof = [], None
        if f.exists():
            try:
                tickers = sorted({t.strip().upper() for t in f.read_text().split() if t.strip()})
                asof = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
            except Exception:
                pass
        out.append({"key": key, "label": label, "url": url, "tier": tier,
                    "sel": sel, "tickers": tickers, "count": len(tickers), "asof": asof})
    return {"lists": out}


@app.post("/api/barchart/scrape")
async def barchart_scrape():
    """后台跑 barchart/scrape_pages.py --only <tab 榜单>（单次登录, 防并发）。"""
    if _bc_scrape_state["running"]:
        return {"status": "already_running"}
    keys = [k for (k, *_rest) in _BC_LISTS]
    _bc_scrape_state.update(running=True, msg="登录 Barchart + 抓取中…")

    async def _run():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "scrape_pages.py", "--only", *keys,
                cwd=str(_BC_DIR),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            _bc_scrape_state.update(running=False, msg="✓ 完成")
        except Exception as e:
            _bc_scrape_state.update(running=False, msg=f"✗ {str(e)[:60]}")

    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/api/barchart/scrape/status")
async def barchart_scrape_status():
    return _bc_scrape_state


# ── Saved Screeners (作者自己在 Barchart 定义的筛选器) ──────────────────
_bc_screener_scrape_state = {"running": False, "msg": "", "ts": None}


@app.get("/api/barchart/screeners")
async def barchart_screeners():
    """自动发现 barchart/downloads/screeners/ 下所有已爬的 saved screener。"""
    scr_dir = _BC_DIR / "downloads" / "screeners"
    out = []
    if scr_dir.exists():
        for tf in sorted(scr_dir.glob("*_tickers.txt")):
            key = tf.stem.replace("_tickers", "")
            name = key.replace("_", " ")
            tickers = sorted({t.strip().upper() for t in tf.read_text().split() if t.strip()})
            asof = datetime.datetime.fromtimestamp(tf.stat().st_mtime).strftime("%m-%d %H:%M")
            out.append({"key": key, "name": name, "tickers": tickers,
                        "count": len(tickers), "asof": asof})
    return {"screeners": out}


@app.post("/api/barchart/screeners/scrape")
async def barchart_screeners_scrape():
    """后台跑 barchart/scrape_screener.py 爬取所有 saved screener。"""
    if _bc_screener_scrape_state["running"]:
        return {"status": "already_running"}
    _bc_screener_scrape_state.update(running=True, msg="登录 Barchart + 爬取 Screener…")

    async def _run():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "scrape_screener.py",
                cwd=str(_BC_DIR),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            _bc_screener_scrape_state.update(running=False,
                msg="✓ 完成", ts=datetime.datetime.now().isoformat())
        except Exception as e:
            _bc_screener_scrape_state.update(running=False, msg=f"✗ {str(e)[:60]}")

    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/api/barchart/screeners/scrape/status")
async def barchart_screeners_scrape_status():
    return _bc_screener_scrape_state


@app.get("/api/barchart/screeners/tagged")
async def barchart_screeners_tagged():
    """分类标注版 saved screeners(喂 heatmap Graph 图墙的「📊 Barchart」多选筛选)。
    读 data/barchart_screeners.json(barchart/consolidate.py 产出, 缺文件时 load 会自动 build)。"""
    try:
        from barchart import consolidate
        data = consolidate.load(rebuild_if_missing=True)
        if not isinstance(data, dict):
            raise ValueError("consolidate.load 返回非 dict")
        return data
    except Exception as e:
        return JSONResponse(
            {"screeners": [], "by_ticker": {}, "categories": {},
             "ai_universe_add": [], "error": str(e)[:160]},
            status_code=200,
        )


_bc_ohlc_memo = {}  # ticker -> (df, epoch_ts)  1h TTL, 避免重复抓 yfinance


def _bc_chart_dict(D, E, df, n):
    """df(Open/High/Low/Close/Volume + 日期索引) → 图视图字典(同 /api/ta/ohlc 格式)。"""
    out = D.ohlc_chart(df, n)
    try:
        dly = D.to_arrays(df)
        wk = D.resample_ohlc(df, "W-FRI")
        mo = D.resample_ohlc(df, "ME")
        p = E.PARAMS
        piv = {"D": E.pivots(dly["high"], dly["low"], p["piv_len_d"], p["max_pivot"]),
               "W": E.pivots(wk["high"], wk["low"], p["piv_len_w"], p["max_pivot"]),
               "M": E.pivots(mo["high"], mo["low"], p["piv_len_m"], p["max_pivot"])}
        zs = E.cluster_zones(piv, p["bin_pct"], p["min_conf_show"], p["max_zones"])
        out["zones"] = [{"lo": round(float(z["lo"]), 2), "hi": round(float(z["hi"]), 2),
                         "avg": round(float(z["avg"]), 2), "conf": int(z["conf"]),
                         "tfs": z.get("tfs", "")} for z in zs]
    except Exception:
        out["zones"] = []
    return out


@app.get("/api/barchart/ohlc/{ticker}")
def barchart_ohlc(ticker: str, days: int = 126):
    """TA 池外票的图: yfinance 现抓 OHLC + SMA50/200 + S/R zones(格式同 /api/ta/ohlc)。
    前端先试 /api/ta/ohlc(缓存内的票), 无数据再 fallback 到这里。sync def → 线程池跑(yfinance 阻塞)。"""
    import time as _t
    try:
        from ta_screener import data as D, engine as E
    except Exception as e:
        return {"error": f"import: {e}", "ticker": ticker}
    t = ticker.strip().upper().replace(".", "-")
    n = max(30, min(520, int(days)))
    try:
        hit = _bc_ohlc_memo.get(t)
        if hit and _t.time() - hit[1] < 3600:
            df = hit[0]
        else:
            import pandas as pd
            import yfinance as yf
            df = yf.download(t, period="2y", interval="1d", progress=False,
                             auto_adjust=False, threads=False)
            # yfinance 单票常返回 MultiIndex 列(('Close','CBOE')...) → 压成单层 + 去重,
            # 否则 df["Volume"] 是 DataFrame, ohlc_chart 里 v==v 在数组上报 ambiguous。
            if df is not None and getattr(df, "columns", None) is not None:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.loc[:, ~df.columns.duplicated()]
            if df is not None and len(df) >= 30:
                _bc_ohlc_memo[t] = (df, _t.time())   # 只缓存成功结果, 失败下次重试(不缓存空)
        if df is None or len(df) < 30:
            return {"error": "no data", "ticker": t}
        return _bc_chart_dict(D, E, df, n)
    except Exception as e:
        return {"error": str(e), "ticker": t}


async def _zeus_refresh_journal():
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(TRADE_JOURNAL_DIR / "fetch_trades.py"), cwd=str(TRADE_JOURNAL_DIR),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    except Exception as e:
        print(f"[journal] refresh error: {e}")


_hroutes.HOOKS["refresh_positions"] = api_refresh
_hroutes.HOOKS["refresh_journal"] = _zeus_refresh_journal
_hroutes.HOOKS["refresh_freenews"] = _zeus_refresh_freenews

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

if __name__ == "__main__":
    import os
    _port = int(os.environ.get("PORT") or _hc.get("port") or 8765)
    _host = str(os.environ.get("HOST") or _hc.get("host") or "127.0.0.1")
    print(f"ZEUS Terminal · 宙斯期权终端  →  http://{'localhost' if _host in ('127.0.0.1', '0.0.0.0') else _host}:{_port}")
    print(f"Settings / 设置  →  http://localhost:{_port}/settings   (config: {_hc.config_path()})")
    # reload=False in production: this runs under launchd with a persistent
    # DXLink WebSocket + ~10 background asyncio tasks and no shutdown handler,
    # so uvicorn's hot-reload (watch files → kill/respawn worker) can leave a
    # worker stuck during shutdown holding the port (orphan → service hangs).
    # Deploy by an explicit restart instead:
    #   (restart the process to deploy)
    uvicorn.run("optionstrat_server:app", host=_host, port=_port, log_level="warning", reload=False)

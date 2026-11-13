"""MCP server: lets AI agents on this computer use ZEUS.

    POST http://127.0.0.1:<port>/mcp      Streamable HTTP, JSON responses
    python zeus_mcp_stdio.py               stdio bridge for clients that launch a command

Claude Code, Claude Desktop, Codex CLI, Gemini CLI or an agent you build yourself
connect here and get ZEUS's own numbers: positions with P/L, Greeks, max profit and
loss, breakevens and exit advice; strategy what-if analysis; the four-gate pre-trade
checkup; spread suggestions; liquidity; quotes and option chains; market regime and
risk radar; news; the earnings and macro calendar; sector rotation; trade ideas;
ticker reports; the trade journal; Paper Trade.

Both eras of the Model Context Protocol are served on the same endpoint:
  modern  2026-07-28: stateless; every request carries its version and capabilities
          in _meta; server/discover.
  legacy  2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05: initialize handshake.

Tools call the terminal's JSON API in-process (httpx ASGI transport), so an agent
sees exactly what the pages show. Every tool is read-only except paper_trade_open
and paper_trade_close, which need Settings → AI agents → "Agents may place paper
trades" = on. No tool can place a real order or read a stored secret or a file path.
Only programs on this computer get an answer; a foreign Origin or Host header is
refused (DNS rebinding) and only JSON bodies are read.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import zeus_config as hc
import zeus_payoff as pf

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED = (MODERN,) + LEGACY
NO_HEADER_VERSION = "2025-03-26"  # what a legacy request without MCP-Protocol-Version means
K_VERSION = "io.modelcontextprotocol/protocolVersion"
K_CAPS = "io.modelcontextprotocol/clientCapabilities"
K_SERVER = "io.modelcontextprotocol/serverInfo"
LIST_TTL_MS = 300_000
MAX_BODY = 2_000_000
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TICKER = re.compile(r"^[A-Za-z][A-Za-z0-9./-]{0,9}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ROOT = Path(__file__).resolve().parent

router = APIRouter()
_APP = None

INSTRUCTIONS = (
    "ZEUS Terminal is the user's own options trading terminal, running on this computer. "
    "The tools return its live data: positions with P/L, Greeks, max profit and loss, breakevens, chance of "
    "profit and exit advice; what-if analysis of any strategy; the four-gate pre-trade checkup; spread "
    "suggestions; liquidity; quotes and option chains; market regime and risk radar; news; the earnings and "
    "macro calendar; sector rotation; trade ideas; ticker reports; the trade journal; Paper Trade. "
    "Every tool is read-only except paper_trade_open and paper_trade_close: those make simulated trades that "
    "never reach a broker, and work only when the user has allowed them in ZEUS Settings. Nothing here can "
    "place a real order. Money is in US dollars; 'unlimited' means the payoff has no bound. "
    "Start with get_status. Quote numbers as the tools return them instead of estimating what a tool computes."
)


def configure(app) -> None:
    """Give the module the running FastAPI app; tools call its JSON API in-process."""
    global _APP
    _APP = app


def server_info() -> dict:
    return {"name": "zeus-terminal", "title": "ZEUS Terminal · 宙斯期权终端", "version": hc.APP_VERSION}


def enabled() -> bool:
    return hc.get_bool("mcp_server")


def paper_allowed() -> bool:
    return hc.get_bool("mcp_paper_trades")


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, status: int = 200):
        super().__init__(message)
        self.code, self.message, self.data, self.status = code, message, data, status


class ToolError(Exception):
    """A problem the agent can act on; returned as a tool result with isError true."""


# ====================================================================== labels
_EN: dict | None = None


def _lang() -> str:
    lang = str(hc.get("language") or "auto").lower()
    return lang if lang in ("zh", "en", "bi") else "auto"


def _label(s):
    """A Chinese label from the app, in the interface language (both languages for bi / auto)."""
    global _EN
    if not isinstance(s, str) or not s or _lang() == "zh":
        return s
    if _EN is None:
        try:
            _EN = json.loads((_ROOT / "options_analyzer" / "static" / "i18n_en.json").read_text(encoding="utf-8"))
        except Exception:
            _EN = {}
    en = _EN.get(s)
    if not en or en == s:
        return s
    return en if _lang() == "en" else f"{s} ({en})"


# ====================================================================== helpers
def _r(x, nd: int = 2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    f = round(f, nd)
    return int(f) if nd == 0 else f


def _money(x):
    """Dollar amount, or 'unlimited' for the page's ∞ convention."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isinf(f) or abs(f) >= pf.UNLIMITED:
        return "unlimited"
    return _r(f, 2)


def _slim(obj, depth: int = 0, max_list: int = 25, max_str: int = 400):
    """Trim an unknown payload for an agent: short strings, short lists, no deep nesting."""
    if isinstance(obj, float):
        return _r(obj, 4)
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + "…"
    if isinstance(obj, list):
        out = [_slim(v, depth + 1, max_list, max_str) for v in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"… {len(obj) - max_list} more")
        return out
    if isinstance(obj, dict):
        if depth >= 4:
            return "…"
        return {k: _slim(v, depth + 1, max_list, max_str) for k, v in obj.items()
                if k not in ("spark", "zones", "images", "links", "body", "paras", "meta_raw", "html")}
    return obj


def _drop_none(obj):
    """Leave empty fields out of what the agent reads: fewer tokens, same meaning."""
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def _today() -> _dt.date:
    return _dt.datetime.now(pf._NY).date()


def _ny_time(ms) -> str | None:
    try:
        return _dt.datetime.fromtimestamp(float(ms) / 1000, pf._NY).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _ticker(v) -> str:
    t = str(v or "").strip().upper()
    if not _TICKER.match(t):
        raise ToolError(f"'{v}' is not a ticker symbol (letters, digits, '.', '/' or '-', up to 10 characters)")
    return t


def _import_checkup():
    oa = str(_ROOT / "options_analyzer")
    if oa not in sys.path:
        sys.path.insert(0, oa)
    from checkup import exit_advisor  # noqa: WPS433
    return exit_advisor


async def _api(path: str, params: dict | None = None, *, method: str = "GET", body: Any = None,
               timeout: float = 60.0, write: bool = False):
    """Call the terminal's own JSON API in-process and return the parsed JSON."""
    if _APP is None:
        raise ToolError("ZEUS is still starting. Try again in a few seconds.")
    headers = {"X-Zeus-Client": "1"} if write else {}
    transport = httpx.ASGITransport(app=_APP, client=("127.0.0.1", 0))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            r = await asyncio.wait_for(c.request(method, path, params=params, json=body, headers=headers), timeout)
    except asyncio.TimeoutError:
        raise ToolError(f"ZEUS took longer than {timeout:.0f} seconds for {path}. Try again in a minute.")
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"{path} failed: {type(e).__name__}: {e}")
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        detail = (data.get("detail") or data.get("error")) if isinstance(data, dict) else None
        raise ToolError(str(detail or f"{path} answered HTTP {r.status_code}"))
    if data is None:
        raise ToolError(f"{path} did not return JSON")
    return data


async def _soft(coro, default=None):
    """Await, but turn a failure into `default` so one missing source never sinks a report."""
    try:
        return await coro
    except Exception:
        return default


# ====================================================================== tools
TOOLS: dict[str, dict] = {}


def tool(name: str, title: str, description: str, properties: dict | None = None,
         required: tuple = (), write: bool = False, destructive: bool = False):
    def deco(fn: Callable):
        schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
        if required:
            schema["required"] = list(required)
        ann = {"title": title, "readOnlyHint": not write, "openWorldHint": False}
        if write:
            ann.update(destructiveHint=destructive, idempotentHint=False)
        else:
            ann["idempotentHint"] = True
        TOOLS[name] = {"def": {"name": name, "title": title, "description": description,
                               "inputSchema": schema, "annotations": ann}, "fn": fn}
        return fn
    return deco


def _check(schema: dict, value, where: str = "arguments"):
    """Validate `value` against the small JSON Schema subset the tools use; fill in defaults."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ToolError(f"{where} must be an object")
        props = schema.get("properties", {})
        extra = sorted(set(value) - set(props))
        if extra and schema.get("additionalProperties") is False:
            raise ToolError(f"{where}: unknown field(s) {', '.join(extra)}; allowed: {', '.join(props) or 'none'}")
        for k in schema.get("required", []):
            if value.get(k) in (None, ""):
                raise ToolError(f"{where}: '{k}' is required")
        out = {}
        for k, sub in props.items():
            if k in value and value[k] is not None:
                out[k] = _check(sub, value[k], f"{where}.{k}" if where != "arguments" else k)
            elif "default" in sub:
                out[k] = sub["default"]
        return out
    if t == "array":
        if not isinstance(value, list):
            raise ToolError(f"{where} must be a list")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10 ** 6):
            raise ToolError(f"{where} needs {schema.get('minItems', 0)} to {schema.get('maxItems')} items")
        return [_check(schema.get("items", {}), v, f"{where}[{i}]") for i, v in enumerate(value)]
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
            raise ToolError(f"{where} must be a whole number")
        value = int(value)
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ToolError(f"{where} must be a number")
        value = float(value)
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"{where} must be true or false")
    elif t == "string":
        if not isinstance(value, str):
            raise ToolError(f"{where} must be text")
        if schema.get("pattern") and not re.match(schema["pattern"], value):
            raise ToolError(f"{where}: '{value}' does not look right ({schema.get('description', '')})")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{where} must be one of {', '.join(map(str, schema['enum']))}")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{where} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{where} must be at most {schema['maximum']}")
    return value


TICKER = {"type": "string", "pattern": _TICKER.pattern, "description": "Ticker symbol, e.g. AAPL or SPY"}
DATE = {"type": "string", "pattern": _DATE.pattern, "description": "Date as YYYY-MM-DD"}
LEGS = {
    "type": "array", "minItems": 1, "maxItems": 8,
    "description": "The legs. Options need strike and expiration; stock legs count shares.",
    "items": {
        "type": "object", "additionalProperties": False, "required": ["type", "action", "quantity"],
        "properties": {
            "type": {"type": "string", "enum": ["call", "put", "stock"]},
            "action": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 100000,
                         "description": "Contracts for options (100 shares each), shares for stock"},
            "strike": {"type": "number", "minimum": 0},
            "expiration": DATE,
            "price": {"type": "number", "minimum": 0,
                      "description": "Optional fill price per share; default is the current mid"},
        },
    },
}


# ---------------------------------------------------------------- status
@tool("get_status", "Status and account",
      "What ZEUS is connected to, how fresh the data is, the account (net liquidating value, cash, buying "
      "power), how many positions and paper trades are open, the market regime, and what agents may do. "
      "Call this first.")
async def t_status(a):
    st, data, regime, paper = await asyncio.gather(
        _api("/api/zeus/status"), _soft(_api("/api/data"), {}), _soft(_api("/api/checkup/regime"), {}),
        _soft(_api("/api/paper-trade/trades", {"status": "active"}), {}))
    bal = data.get("balance") or {}
    out = {
        "app": f"ZEUS Terminal {hc.APP_VERSION}",
        "now_new_york": _dt.datetime.now(pf._NY).strftime("%Y-%m-%d %H:%M %a"),
        "broker": {"tastytrade": "tastytrade (live)", "public": "Public (live account)",
                   "alpaca": "Alpaca (" + ("paper" if st.get("alpaca_paper") else "live") + " account)"}.get(
                       st.get("broker_mode"), "manual (uploaded trades)"),
        "quotes": {"tastytrade": "tastytrade (streaming)", "alpaca": "Alpaca", "public": "Public"}.get(
            st.get("market_source"), "free (Cboe options ~15 min delayed, Yahoo stocks)"),
        "positions_as_of": data.get("generated_at"),
        "account": {"net_liquidating_value": _r(bal.get("net_liquidating_value")),
                    "cash": _r(bal.get("cash_balance")),
                    "option_buying_power": _r(bal.get("derivative_buying_power")),
                    "maintenance_requirement": _r(bal.get("maintenance_requirement"))},
        "open_option_strategies": len(data.get("strategies") or []),
        "open_stock_positions": len(data.get("equities") or []),
        "open_paper_trades": len(paper.get("trades") or []),
        "market_regime": {"regime": regime.get("regime"), "spy": regime.get("spy"), "spy_sma200": regime.get("sma200"),
                          "vix": regime.get("vix"),
                          "take_profit_zone": (regime.get("band") or {}).get("zone")} if regime else None,
        "news_sources": (st.get("news") or {}).get("sources"),
        "benzinga": bool(st.get("benzinga")),
        "ai_provider_in_zeus": (st.get("ai") or {}).get("provider"),
        "agent_permissions": {"read": True, "paper_trades": paper_allowed(), "real_orders": False},
    }
    if st.get("manual"):
        m = st["manual"]
        out["manual_trades"] = {k: m.get(k) for k in ("trades", "open_positions", "cash", "starting_cash", "realized")}
    return out


# ---------------------------------------------------------------- positions
_ADVICE_EN = {"force_tp": "take profit now", "take_profit": "take profit", "near_tp": "near the take-profit zone",
              "cut": "deep loss (defined risk: the rules say hold)", "hold": "hold"}


def _stress(strat: dict, an: dict) -> dict:
    """The Risk view's one-day stress test, VaR 95 % and CVaR 95 % (P/L against entry)."""
    legs = pf._legs(strat)
    S = float(strat.get("current_price") or 0)
    iv = an["iv"] or float(strat.get("iv") or 0)
    r = float(strat.get("risk_free_rate") or 0)
    t1 = max(0.0, an["T"] - 1 / 365)
    daily = iv * math.sqrt(1 / 365)

    def at(price):
        total = 0.0
        for l in legs:
            if t1 > 0:
                v = pf.bsm_price(price, l["strike"], t1, r, iv, l["type"])
            else:
                v = max(0.0, price - l["strike"]) if l["type"] == "C" else max(0.0, l["strike"] - price)
            total += (v - l["entry"]) * l["qty"] * l["mult"]
        return total

    def worst(z):
        m = S * daily * z
        return min(at(S + m), at(max(0.01, S - m)))

    zs = [1.645 + (3.5 - 1.645) * i / 12 for i in range(1, 13)]
    return {"one_sd_move": _r(S * daily), "worst_1sd": _r(worst(1)), "worst_2sd": _r(worst(2)),
            "var95": _r(worst(1.645)), "cvar95": _r(sum(worst(z) for z in zs) / len(zs))}


def _leg_out(l: dict) -> dict:
    if l.get("type") == "S":
        return {"type": "stock", "action": "buy" if l.get("qty", 0) > 0 else "sell", "quantity": abs(int(l.get("qty") or 0)),
                "entry": _r(l.get("entry")), "price": _r(l.get("current"))}
    return {"type": "call" if l.get("type") == "C" else "put", "action": "buy" if (l.get("qty") or 0) > 0 else "sell",
            "quantity": abs(int(l.get("qty") or 0)), "strike": _r(l.get("strike")),
            "expiration": l.get("exp") or l.get("expiration"), "entry": _r(l.get("entry")),
            "mark": _r(l.get("current")), "iv": _r(l.get("iv"), 4)}


async def _regime() -> str | None:
    rg = await _soft(_api("/api/checkup/regime"), {})
    return (rg or {}).get("regime")


@tool("get_positions", "Open positions",
      "Every open option strategy and stock position, computed the way the Positions page does: cost basis "
      "(+ debit paid, - credit received), P/L open and today, max profit and max loss ('unlimited' when "
      "unbounded), breakevens, chance of profit, Greeks (delta in shares, theta per day, vega per IV point), "
      "IV, days to expiration, earnings before expiration, one-day stress (worst P/L after a 1 and 2 standard "
      "deviation move, VaR 95 %, CVaR 95 %), and the exit advice of ZEUS's regime-based take-profit rule. "
      "Ends with portfolio totals and risk per underlying as a share of net liquidating value.",
      {"underlying": {**TICKER, "description": "Only this underlying (optional)"},
       "include_legs": {"type": "boolean", "default": True, "description": "List each leg"}})
async def t_positions(a):
    data, regime = await asyncio.gather(_api("/api/data"), _regime())
    ea = _import_checkup()
    want = a.get("underlying", "").upper()
    nlv = float((data.get("balance") or {}).get("net_liquidating_value") or 0)
    rows, risk_by = [], {}
    tot = {"pnl_open": 0.0, "pnl_today": 0.0, "delta": 0.0, "theta_per_day": 0.0, "vega": 0.0}
    for s in data.get("strategies") or []:
        und = str(s.get("underlying") or "").upper()
        if want and und != want:
            continue
        an = pf.analyze(s)
        g = an["greeks"]
        mp, ml = an["max_profit"], an["max_loss"]
        exp, earn = s.get("expiration") or "", s.get("earnings_date") or ""
        advice = ea.evaluate(an["unrealized_pnl"], mp, s.get("dte"), an["unrealized_pct"], regime=regime)
        row = {
            "strategy": s.get("name"), "underlying": und, "spot": _r(s.get("current_price")),
            "opened": (s.get("created_at") or "")[:10] or None, "expiration": exp or None, "dte": s.get("dte"),
            "cost_basis": _r(an["net_debit"]), "pnl_open": _r(an["unrealized_pnl"]),
            "pnl_open_pct": _r(an["unrealized_pct"], 1), "pnl_today": _r(an["today_pnl"]),
            "max_profit": _money(mp), "max_loss": _money(ml),
            "progress_to_max_profit_pct": _r(an["unrealized_pnl"] / mp * 100, 1) if 0 < mp < pf.UNLIMITED else None,
            "breakevens": [_r(b) for b in an["breakevens"]],
            "chance_of_profit_pct": _r(an["chance_of_profit"], 1),
            "greeks": {"delta": _r(g["delta"]), "gamma": _r(g["gamma"], 4), "theta_per_day": _r(g["theta"]),
                       "vega": _r(g["vega"])},
            "iv": _r(an["iv"], 4),
            "earnings": earn or None,
            "earnings_before_expiration": bool(earn and exp and earn <= exp),
            "risk_1d": _stress(s, an),
            "exit_advice": {"code": advice.get("code"), "action": _ADVICE_EN.get(advice.get("code"), advice.get("label")),
                            "headline": advice.get("headline"), "detail": advice.get("detail")},
        }
        if a.get("include_legs", True):
            row["legs"] = [_leg_out(l) for l in s.get("legs") or []]
        rows.append(row)
        tot["pnl_open"] += an["unrealized_pnl"]
        tot["pnl_today"] += an["today_pnl"]
        tot["delta"] += g["delta"]
        tot["theta_per_day"] += g["theta"]
        tot["vega"] += g["vega"]
        risk = risk_by.setdefault(und, {"legs": [], "spot": 0.0, "iv": 0.0})
        risk["legs"] += [dict(l, exp=l.get("exp") or exp) for l in s.get("legs") or []]
        risk["spot"] = float(s.get("current_price") or 0) or risk["spot"]
        risk["iv"] = risk["iv"] or float(s.get("iv") or 0)

    prev = data.get("equity_prev_close") or {}
    stocks = []
    for e in data.get("equities") or []:
        sym = str(e.get("symbol") or "").upper()
        if want and sym != want:
            continue
        sh = int(e.get("quantity") or 0) * (-1 if str(e.get("direction")) == "Short" else 1)
        px, entry, pc = float(e.get("current") or 0), float(e.get("entry") or 0), float(prev.get(sym) or 0)
        pnl = (px - entry) * sh
        stocks.append({"symbol": sym, "shares": sh, "entry": _r(entry), "price": _r(px), "pnl_open": _r(pnl),
                       "pnl_open_pct": _r((px / entry - 1) * 100 * (1 if sh > 0 else -1), 1) if entry else None,
                       "pnl_today": _r((px - pc) * sh) if pc else None,
                       "value": _r(px * sh)})
        tot["pnl_open"] += pnl
        tot["pnl_today"] += (px - pc) * sh if pc else 0.0
        tot["delta"] += sh
        risk = risk_by.setdefault(sym, {"legs": [], "spot": px, "iv": 0.0})
        risk["legs"].append({"type": "S", "qty": sh, "entry": entry, "current": px, "mid": px, "mult": 1})
        risk["spot"] = risk["spot"] or px

    # Risk per underlying: options and shares together, so a short call covered by stock is not "unlimited".
    concentration = []
    for und, g in risk_by.items():
        exps = sorted({l.get("exp") for l in g["legs"] if l.get("exp")})
        combined = {"current_price": g["spot"], "iv": g["iv"] or 0.3, "risk_free_rate": 0.045,
                    "expiration": exps[0] if exps else (_today() + _dt.timedelta(days=365)).isoformat(),
                    "legs": g["legs"]}
        ml = pf.analyze(combined)["max_loss"] if g["spot"] else 0.0
        unlimited = ml <= -pf.UNLIMITED
        concentration.append({"underlying": und, "max_loss": "unlimited" if unlimited else _r(min(0.0, ml)),
                              "pct_of_net_liq": None if unlimited or not nlv else _r(-min(0.0, ml) / nlv * 100, 1)})
    concentration.sort(key=lambda x: -(x["pct_of_net_liq"] if isinstance(x["pct_of_net_liq"], (int, float)) else 1e9))
    return {
        "as_of": data.get("generated_at"), "market_regime": regime,
        "net_liquidating_value": _r(nlv),
        "option_strategies": rows, "stocks": stocks,
        "totals": {k: _r(v) for k, v in tot.items()},
        "risk_by_underlying": concentration,
        "notes": "cost_basis > 0 is a debit paid, < 0 a credit received. P/L is mark to market at the mid. "
                 "risk_1d values are P/L against entry after a one-day move. Stock delta counts shares. "
                 "risk_by_underlying is the worst P/L at the front expiration of all options and shares on "
                 "that underlying together.",
    }


# ---------------------------------------------------------------- strategy analysis
async def _chain(ticker: str, expiration: str | None = None) -> dict:
    params = {"expiration": expiration} if expiration else None
    return await _api(f"/api/paper-trade/chain/{ticker}", params, timeout=90)


def _row_for(chain: dict, strike: float):
    rows = chain.get("strikes") or []
    for row in rows:
        if abs(float(row.get("strike") or 0) - strike) < 1e-6:
            return row
    near = sorted((float(x.get("strike") or 0) for x in rows), key=lambda k: abs(k - strike))[:6]
    listed = ", ".join(f"{k:g}" for k in sorted(near)) or "none"
    raise ToolError(f"No {strike:g} strike on {chain.get('ticker')} for this expiration. Nearest strikes: {listed}")


async def _priced_legs(ticker: str, legs: list) -> tuple[dict, list, list]:
    """Turn the agent's legs into analyzer legs priced from the live chain.
    Returns (underlying quote, analyzer legs, warnings)."""
    base = await _chain(ticker)
    spot = float(base.get("price") or 0)
    if spot <= 0:
        raise ToolError(f"No quote for {ticker} right now.")
    exps = {e.get("date") for e in base.get("expirations") or []}
    chains: dict = {}
    out, warnings = [], []
    for i, leg in enumerate(legs, 1):
        sign = 1 if leg["action"] == "buy" else -1
        if leg["type"] == "stock":
            px = leg.get("price", spot)
            out.append({"type": "S", "strike": 0, "qty": sign * leg["quantity"], "entry": px, "current": spot,
                        "mid": spot, "mult": 1, "symbol": ticker})
            continue
        exp, strike = leg.get("expiration"), leg.get("strike")
        if not exp or strike is None:
            raise ToolError(f"Leg {i}: an option leg needs strike and expiration")
        try:
            want = _dt.date.fromisoformat(exp)
        except ValueError:
            raise ToolError(f"Leg {i}: '{exp}' is not a date (YYYY-MM-DD)")
        if exp not in exps:
            near = sorted((d for d in exps if d), key=lambda d: abs((_dt.date.fromisoformat(d) - want).days))[:5]
            raise ToolError(f"Leg {i}: {ticker} has no {exp} expiration. Nearest: {', '.join(sorted(near)) or 'none'}")
        if exp not in chains:
            chains[exp] = await _chain(ticker, exp)
        row = _row_for(chains[exp], float(strike))
        side = row.get("call" if leg["type"] == "call" else "put") or {}
        mid = float(side.get("mid") or 0)
        bid, ask = float(side.get("bid") or 0), float(side.get("ask") or 0)
        px = leg.get("price", mid)
        if mid <= 0 and "price" not in leg:
            raise ToolError(f"Leg {i}: the {exp} {strike:g} {leg['type']} has no bid/ask now; pass price")
        if mid > 0 and ask > bid > 0 and (ask - bid) / mid > 0.10:
            warnings.append(f"{exp} {strike:g} {leg['type']}: wide market {bid:g} / {ask:g} "
                            f"({(ask - bid) / mid * 100:.0f} % of mid)")
        if int(side.get("oi") or 0) < 100:
            warnings.append(f"{exp} {strike:g} {leg['type']}: thin open interest ({int(side.get('oi') or 0)})")
        out.append({"type": "C" if leg["type"] == "call" else "P", "strike": float(strike), "qty": sign * leg["quantity"],
                    "entry": px, "current": mid or px, "mid": mid or px, "mult": 100, "exp": exp,
                    "iv": float(side.get("iv") or 0), "symbol": side.get("symbol"), "streamer_symbol": side.get("streamer"),
                    "bid": bid, "ask": ask, "oi": side.get("oi"), "volume": side.get("vol")})
    return base, out, warnings


def _strategy_name(legs: list) -> str:
    """Paper Trade's naming (nameSt)."""
    c = [l for l in legs if l["type"] == "C"]
    p = [l for l in legs if l["type"] == "P"]
    sk = [l for l in legs if l["type"] == "S"]
    if sk:
        long_stock = any(l["qty"] > 0 for l in sk)
        short_calls = [l for l in c if l["qty"] < 0]
        long_puts = [l for l in p if l["qty"] > 0]
        if not c and not p:
            return "Long Stock" if long_stock else "Short Stock"
        if long_stock and len(c) == 1 and len(short_calls) == 1 and not p:
            return "Covered Call"
        if long_stock and len(p) == 1 and len(long_puts) == 1 and not c:
            return "Protective Put"
        if long_stock and len(c) == 1 and len(short_calls) == 1 and len(p) == 1 and len(long_puts) == 1:
            return "Collar"
        return "Custom"
    lc, sc = [l for l in c if l["qty"] > 0], [l for l in c if l["qty"] < 0]
    lp, sp = [l for l in p if l["qty"] > 0], [l for l in p if l["qty"] < 0]
    if len(c) == 2 and not p and len(lc) == 1 and len(sc) == 1:
        return "Bull Call Spread" if lc[0]["strike"] < sc[0]["strike"] else "Bear Call Spread"
    if len(p) == 2 and not c and len(lp) == 1 and len(sp) == 1:
        return "Bear Put Spread" if lp[0]["strike"] > sp[0]["strike"] else "Bull Put Spread"
    if len(c) == 2 and len(p) == 2:
        return "Iron Condor"
    if len(c) == 1 and not p:
        return "Long Call" if lc else "Short Call"
    if len(p) == 1 and not c:
        return "Long Put" if lp else "Short Put"
    return "Custom"


def _analysis_out(strat: dict, legs: list, base: dict) -> dict:
    an = pf.analyze(strat)
    if pf.single_expiration(strat):
        mp, ml = pf.expiry_extremes(strat)
    else:
        mp, ml = an["max_profit"], an["max_loss"]
    spot = float(strat["current_price"])
    ks = sorted({l["strike"] for l in legs if l["type"] != "S"})
    marks = sorted({round(spot * f, 2) for f in (0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2)} | set(ks)
                   | {round(b, 2) for b in an["breakevens"]})
    table = [{"price": _r(p), "pnl": _r(v)} for p, v in pf.pnl_at_front(strat, marks, an["iv"])]
    g = an["greeks"]
    earn = base.get("earnings_date") or ""
    exp = strat.get("expiration") or ""
    reward_risk = None
    if isinstance(_money(mp), float) and isinstance(_money(ml), float) and ml < 0:
        reward_risk = _r(mp / -ml, 2)
    return {
        "net": "debit" if an["net_debit"] > 0 else ("credit" if an["net_debit"] < 0 else "even"),
        "cost_basis": _r(an["net_debit"]),
        "max_profit": _money(mp), "max_loss": _money(ml), "reward_to_risk": reward_risk,
        "breakevens": [_r(b) for b in an["breakevens"]],
        "chance_of_profit_pct": _r(an["chance_of_profit"], 1),
        "greeks": {"delta": _r(g["delta"]), "gamma": _r(g["gamma"], 4), "theta_per_day": _r(g["theta"]),
                   "vega": _r(g["vega"])},
        "iv": _r(an["iv"], 4), "dte": _r(an["dte"], 1),
        "earnings": earn or None, "earnings_before_expiration": bool(earn and exp and earn <= exp),
        "pnl_at_front_expiration": table,
    }


@tool("analyze_strategy", "Analyze a strategy (what-if)",
      "Price any option or stock strategy on a ticker from the live chain and analyze it like ZEUS's strategy "
      "builder: net debit or credit, max profit and max loss (exact at expiration when the legs share one "
      "expiration), reward to risk, breakevens, chance of profit, Greeks, earnings before expiration, a P/L "
      "table at the front expiration, and warnings for wide markets or thin open interest. Nothing is saved. "
      "Missing prices default to the current mid. Use get_option_chain first to see expirations and strikes.",
      {"ticker": TICKER, "legs": LEGS,
       "iv": {"type": "number", "minimum": 0.01, "maximum": 5, "description": "Optional IV override, e.g. 0.35"}},
      required=("ticker", "legs"))
async def t_analyze(a):
    ticker = _ticker(a["ticker"])
    base, legs, warnings = await _priced_legs(ticker, a["legs"])
    exps = sorted({l["exp"] for l in legs if l.get("exp")})
    strat = {"current_price": float(base["price"]), "iv": a.get("iv") or float(base.get("iv") or 0.3),
             "risk_free_rate": 0.045, "expiration": exps[0] if exps else (_today() + _dt.timedelta(days=365)).isoformat(),
             "legs": legs}
    if a.get("iv"):
        for l in legs:
            l["iv"] = 0.0  # the override applies to every leg
    out = {"ticker": ticker, "spot": _r(base.get("price")), "strategy": _strategy_name(legs),
           "legs": [{**_leg_out(l), "bid": _r(l.get("bid")), "ask": _r(l.get("ask")), "open_interest": l.get("oi")}
                    if l["type"] != "S" else _leg_out(l) for l in legs]}
    out.update(_analysis_out(strat, legs, base))
    if warnings:
        out["warnings"] = warnings
    return out


# ---------------------------------------------------------------- pre-trade
@tool("pretrade_checkup", "Pre-trade checkup (four gates)",
      "ZEUS's four-gate pre-trade checkup for one ticker and direction: 1 long-term trend, 2 touch of a "
      "support or resistance EMA, 3 implied volatility and earnings, 4 macro calendar and market stress. "
      "Returns each gate's result with reasons, the verdict and a suggestion. A failed gate means: do not "
      "open now.",
      {"ticker": TICKER, "direction": {"type": "string", "enum": ["bull", "bear"], "default": "bull"},
       "dte": {"type": "integer", "minimum": 1, "maximum": 400, "default": 40,
               "description": "Days to expiration you plan to use"}},
      required=("ticker",))
async def t_pretrade(a):
    ticker = _ticker(a["ticker"])
    d = await _api("/api/pre_trade_checkup", {"ticker": ticker, "direction": a["direction"], "dte": a["dte"]},
                   timeout=120)
    gates = []
    for k in sorted((d.get("layers") or {}), key=lambda x: int(x) if str(x).isdigit() else 9):
        layer = d["layers"][k] or {}
        gates.append({"gate": int(k) if str(k).isdigit() else k, "name": _label(layer.get("name")),
                      "pass": layer.get("pass"),
                      "lines": layer.get("lines") or (layer.get("reasons_pass") or []) + (layer.get("reasons_fail") or []),
                      "warnings": layer.get("warnings") or None})
    return {"ticker": ticker, "direction": a["direction"], "dte": a["dte"], "data_as_of": d.get("as_of"),
            "pass": d.get("pass"), "verdict": _label(d.get("verdict")), "failed_gates": d.get("failed_layers"),
            "suggestion": d.get("suggestion"), "gates": gates}


@tool("suggest_spreads", "Spread suggestions",
      "Bull call spreads for a ticker ranked by fill quality, liquidity, reward to risk and fit with ZEUS's "
      "tested entry rules (about at the money, 30-45 days, 10 % wide), adapted to the market regime. Each has "
      "expiration, strikes, debit, max profit and loss, breakeven, open interest and the reason for its rank.",
      {"ticker": TICKER, "top": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
      required=("ticker",))
async def t_suggest(a):
    ticker = _ticker(a["ticker"])
    d = await _api(f"/api/checkup/recommend/{ticker}", timeout=120)
    recs = []
    for r in (d.get("recommendations") or [])[: a["top"]]:
        recs.append({k: r.get(k) for k in ("exp", "dte", "long", "short", "debit", "max_profit", "max_loss", "rr",
                                           "breakeven", "be_dist_pct", "width_pct", "spread_pct", "oi_min",
                                           "vol_min", "score", "earnings_in_window", "reason")})
    return {"ticker": ticker, "spot": _r(d.get("spot")), "earnings": d.get("earnings_date"),
            "market_regime": d.get("regime"), "vix": d.get("vix"), "strike_rule": _slim(d.get("strike")),
            "spreads": recs,
            "fields": "long/short = strikes bought/sold; debit, max_profit and max_loss are per spread in $ "
                      "(debit per share); rr = reward to risk; be_dist_pct = breakeven distance from spot; "
                      "spread_pct = bid/ask width as % of mid; oi_min / vol_min = the thinner leg."}


@tool("check_liquidity", "Option liquidity",
      "How tradable a ticker's options are: ZEUS's verdict, bid/ask width, volume and open interest at the "
      "money for every expiration, the best expiration per strike near the money, and the same call spread "
      "priced across weeks.",
      {"ticker": TICKER, "max_dte": {"type": "integer", "minimum": 7, "maximum": 120, "default": 50}},
      required=("ticker",))
async def t_liquidity(a):
    ticker = _ticker(a["ticker"])
    d = await _api(f"/api/checkup/liquidity/{ticker}", {"max_dte": a["max_dte"]}, timeout=120)
    cols = d.get("cols") or []
    spot = float(d.get("spot") or 0)

    def cell(c):
        if not c:
            return None
        return {"bid": c.get("bid"), "ask": c.get("ask"), "spread_pct": c.get("spread_pct"), "volume": c.get("vol"),
                "open_interest": c.get("oi"), "iv_pct": c.get("iv"), "quality": c.get("tier"),
                "notes": [_label(x) for x in c.get("reasons") or []] or None}

    def side(name):
        block = d.get(name) or {}
        strikes, cells, best = block.get("strikes") or [], block.get("cells") or [], block.get("best_col") or []
        out = {"at_the_money": [], "best_expiration_by_strike": []}
        if not strikes:
            return out
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - (d.get("atm_strike") or spot)))
        for j, col in enumerate(cols):
            if atm < len(cells) and j < len(cells[atm]):
                out["at_the_money"].append({"expiration": col.get("date"), "dte": col.get("dte"),
                                            "strike": strikes[atm], **(cell(cells[atm][j]) or {})})
        for i, k in enumerate(strikes):
            if spot and abs(k / spot - 1) <= 0.05 and i < len(best) and best[i] is not None and i < len(cells):
                j = best[i]
                if j < len(cols) and j < len(cells[i]):
                    out["best_expiration_by_strike"].append({"strike": k, "expiration": cols[j].get("date"),
                                                             "dte": cols[j].get("dte"), **(cell(cells[i][j]) or {})})
        return out

    tv = d.get("ticker_verdict") or {}
    cw = d.get("cross_week") or {}
    return {"ticker": ticker, "spot": _r(spot), "as_of": d.get("asof"), "market_hours": d.get("market_hours"),
            "earnings": d.get("earnings_date"),
            "verdict": {"quality": tv.get("tier"), "label": _label(tv.get("label")), "detail": tv.get("detail"),
                        "atm_spread_pct": tv.get("spread_pct"), "expiration": tv.get("exp"), "open_interest": tv.get("oi")},
            "calls": side("calls"), "puts": side("puts"),
            "same_spread_across_weeks": {"side": cw.get("side"), "long": cw.get("long"), "width_pct": cw.get("width_pct"),
                                         "rows": [{k: r.get(k) for k in ("date", "dte", "long", "short", "debit",
                                                                         "net_width", "best")}
                                                  for r in cw.get("rows") or []]}}


# ---------------------------------------------------------------- quotes and chains
@tool("get_quotes", "Stock quotes",
      "Latest price and change today for up to 20 tickers (stocks, ETFs or indexes such as ^VIX).",
      {"tickers": {"type": "array", "minItems": 1, "maxItems": 20,
                   "items": {"type": "string", "pattern": r"^\^?[A-Za-z][A-Za-z0-9./-]{0,9}$"}}},
      required=("tickers",))
async def t_quotes(a):
    syms = list(dict.fromkeys(s.strip().upper() for s in a["tickers"]))

    async def one(s):
        q = await _soft(_api("/api/quote_simple", {"symbol": s}, timeout=30), None)
        if not q or not q.get("price"):
            return {"ticker": s, "error": "no quote"}
        return {"ticker": s, "price": _r(q.get("price")), "change": _r(q.get("change")),
                "change_pct": _r(q.get("changePct"))}

    return {"quotes": list(await asyncio.gather(*(one(s) for s in syms)))}


@tool("get_option_chain", "Option chain",
      "Expirations and the option chain of a ticker: bid, ask, mid, IV, delta, volume and open interest for "
      "the strikes nearest the stock price. Without an expiration it lists the expirations and shows the "
      "first one at least 30 days out.",
      {"ticker": TICKER, "expiration": DATE,
       "strikes": {"type": "integer", "minimum": 2, "maximum": 60, "default": 12,
                   "description": "How many strikes around the stock price"},
       "side": {"type": "string", "enum": ["both", "call", "put"], "default": "both"}},
      required=("ticker",))
async def t_chain(a):
    ticker = _ticker(a["ticker"])
    base = await _chain(ticker)
    exps = base.get("expirations") or []
    if not exps:
        raise ToolError(f"No option chain for {ticker}.")
    exp = a.get("expiration")
    if exp and exp not in {e.get("date") for e in exps}:
        raise ToolError(f"{ticker} has no {exp} expiration. Expirations: "
                        + ", ".join(e.get("date") for e in exps[:24]))
    if not exp:
        exp = next((e.get("date") for e in exps if (e.get("dte") or 0) >= 30), exps[-1].get("date"))
    ch = await _chain(ticker, exp)
    spot = float(ch.get("price") or base.get("price") or 0)
    rows = sorted(ch.get("strikes") or [], key=lambda x: abs(float(x.get("strike") or 0) - spot))[: a["strikes"]]
    rows.sort(key=lambda x: float(x.get("strike") or 0))
    T = max(0.0, (pf.ny_close_ms(exp) - _dt.datetime.now().timestamp() * 1000) / pf._YEAR_MS)

    def leg(q, typ, k):
        if not q:
            return None
        iv = float(q.get("iv") or 0)
        delta = pf.bsm_greeks(spot, k, T, 0.045, iv, typ)["delta"] if (iv > 0 and spot > 0) else None
        return {"bid": q.get("bid"), "ask": q.get("ask"), "mid": q.get("mid"), "iv": _r(iv, 4) if iv else None,
                "delta": _r(delta, 3), "volume": q.get("vol"), "open_interest": q.get("oi")}

    chain = []
    for row in rows:
        k = float(row.get("strike") or 0)
        item = {"strike": k}
        if a["side"] in ("both", "call"):
            item["call"] = leg(row.get("call"), "C", k)
        if a["side"] in ("both", "put"):
            item["put"] = leg(row.get("put"), "P", k)
        chain.append(item)
    return {"ticker": ticker, "spot": _r(spot), "prev_close": _r(ch.get("prev_close")),
            "underlying_iv": _r(ch.get("iv"), 4), "earnings": ch.get("earnings_date") or None,
            "expiration": exp, "dte": next((e.get("dte") for e in exps if e.get("date") == exp), None),
            "expirations": [{"date": e.get("date"), "dte": e.get("dte"), "type": e.get("type")} for e in exps],
            "chain": chain}


# ---------------------------------------------------------------- market
@tool("get_market_overview", "Market overview",
      "The market in one call: ZEUS's regime (SPY against its 200-day average, VIX, the take-profit zone and "
      "strike rule that follow), the risk radar (today's news risk score, forward event-risk score, landmine "
      "calendar), the AI market judgement if it has been generated today, sector rotation leaders and "
      "laggards, sector option sentiment, and SPY / QQQ / IWM / VIX quotes.")
async def t_market(a):
    regime, radar, brief, groups, pulse, quotes = await asyncio.gather(
        _soft(_api("/api/checkup/regime"), {}), _soft(_api("/api/checkup/risk-radar", timeout=90), {}),
        _soft(_api("/api/market_brief"), {}), _soft(_api("/api/industry_groups", timeout=120), {}),
        _soft(_api("/api/options_pulse", timeout=60), {}), t_quotes({"tickers": ["SPY", "QQQ", "IWM", "^VIX"]}))
    out = {"quotes": quotes["quotes"]}
    if regime:
        out["regime"] = {"regime": regime.get("regime"), "reason": _label(regime.get("reason")),
                         "spy": regime.get("spy"), "spy_sma200": regime.get("sma200"),
                         "spy_pct_above_sma200": regime.get("pct_above"), "vix": regime.get("vix"),
                         "high_volatility": regime.get("high_vol"),
                         "take_profit_zone": (regime.get("band") or {}).get("zone"),
                         "strike_rule": {"label": (regime.get("strike") or {}).get("label"),
                                         "detail": (regime.get("strike") or {}).get("detail")},
                         "as_of": regime.get("asof")}
    if radar:
        out["risk_radar"] = {
            "news_risk_today": _slim({k: (radar.get("gauge") or {}).get(k) for k in ("score", "level", "hot_topics", "top")}),
            "event_risk_next_30_days": _slim({k: (radar.get("forward") or {}).get(k)
                                              for k in ("score", "level", "n_events", "top_events")}),
            "landmines": [_slim({k: m.get(k) for k in ("date", "days_away", "name", "ticker", "severity", "note")})
                          for m in (radar.get("landmines") or [])[:12]]}
    if brief and brief.get("status") == "done":
        out["ai_market_judgement"] = _slim({k: v for k, v in brief.items() if k not in ("status",)}, max_list=12)
    else:
        out["ai_market_judgement"] = "not generated today (Picker → 🧭 Market judgement, needs an AI provider)"
    rot = (groups or {}).get("rotation") or {}
    if rot:
        out["sector_rotation"] = {k: [_label(x) for x in rot.get(k) or []] for k in ("leaders", "laggards", "warming", "cooling")}
    if pulse and pulse.get("rows"):
        out["sector_option_sentiment"] = _slim(pulse, max_list=15)
    return out


@tool("get_events", "Events calendar",
      "Upcoming market-moving events: macro releases (CPI, FOMC, jobs, PCE…) with forecast, previous and actual, "
      "and earnings, with a flag for every position that holds through an earnings date.",
      {"days": {"type": "integer", "minimum": 1, "maximum": 60, "default": 14},
       "tickers": {"type": "array", "maxItems": 10, "items": TICKER,
                   "description": "Also look up these tickers' next earnings"}})
async def t_events(a):
    days = a["days"]
    today = _today()
    end = today + _dt.timedelta(days=days)
    major, cal, data = await asyncio.gather(_soft(_api("/api/major-events", {"days": days}), {}),
                                            _soft(_api("/api/calendar_events"), {}), _soft(_api("/api/data"), {}))

    def in_range(d):
        try:
            return today <= _dt.date.fromisoformat(str(d)[:10]) <= end
        except ValueError:
            return False

    econ = [{k: e.get(k) for k in ("date", "time_local", "title", "country", "forecast", "previous", "actual")}
            for e in (cal.get("econ") or []) if in_range(e.get("date"))]
    econ.sort(key=lambda e: (e["date"] or "", e.get("time_local") or ""))
    key_events = [{k: e.get(k) for k in ("date", "days_away", "type", "name", "ticker", "tier")}
                  for e in (major.get("events") or []) if in_range(e.get("date"))]
    held = {}
    for s in data.get("strategies") or []:
        und = str(s.get("underlying") or "").upper()
        h = held.setdefault(und, {"ticker": und, "earnings": s.get("earnings_date") or None,
                                  "time": s.get("earnings_time") or None, "positions_through_earnings": []})
        exp, earn = s.get("expiration") or "", s.get("earnings_date") or ""
        if earn and exp and earn <= exp:
            h["positions_through_earnings"].append(f"{s.get('name')} exp {exp}")
    held_earn = [h for h in held.values() if h["earnings"] and in_range(h["earnings"])]
    extra = []
    for t in dict.fromkeys(x.upper() for x in a.get("tickers") or []):
        ch = await _soft(_chain(t), {})
        extra.append({"ticker": t, "next_earnings": (ch or {}).get("earnings_date") or None,
                      "time": (ch or {}).get("earnings_time") or None})
    out = {"from": today.isoformat(), "to": end.isoformat(), "key_events": key_events, "macro_calendar": econ,
           "earnings_of_holdings": held_earn}
    earn_list = [e for e in (cal.get("earnings") or []) if in_range(e.get("date"))]
    if earn_list:
        out["earnings_calendar"] = _slim(earn_list, max_list=40)
    if extra:
        out["requested_tickers"] = extra
    return out


# ---------------------------------------------------------------- news
def _to_ms(v) -> float:
    """Epoch milliseconds from epoch seconds, epoch milliseconds or an ISO time (0 when unknown)."""
    if v in (None, ""):
        return 0.0
    try:
        f = float(v)
        return f * 1000 if f < 1e12 else f
    except (TypeError, ValueError):
        pass
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=pf._NY)
        return d.timestamp() * 1000
    except ValueError:
        return 0.0


_ALIASES = {"GOOGL": ["Google"], "GOOG": ["Google"], "META": ["Facebook"], "BRK-B": ["Berkshire"],
            "BRK.B": ["Berkshire"], "SPY": ["S&P 500"], "QQQ": ["Nasdaq"], "IWM": ["Russell 2000"],
            "DIA": ["Dow Jones"], "GLD": ["gold"], "USO": ["oil"], "TLT": ["Treasury", "Treasuries"]}
_GENERIC = {"the", "advanced", "american", "united", "general", "first", "global", "international", "national",
            "new", "bank", "spdr", "ishares", "invesco", "vanguard", "proshares", "direxion", "select", "royal",
            "western", "southern", "northern", "eastern", "public", "applied", "digital", "energy", "health"}
_SUFFIX = re.compile(r"[,.]|\b(inc|corp|corporation|co|company|ltd|plc|holdings?|group|class [a-c]|sa|nv|ag|se|"
                     r"trust|series \d+|etf|fund|index)\b", re.I)


def _name_keys(ticker: str) -> list[str]:
    """Words that stand for the ticker in a headline: the company name, its distinctive first word, aliases."""
    keys = list(_ALIASES.get(ticker, []))
    try:
        oa = str(_ROOT)
        if oa not in sys.path:
            sys.path.insert(0, oa)
        from heatmap import metadata as _meta  # cached names, no network
        name = ((_meta.get_all() or {}).get(ticker) or {}).get("name") or ""
    except Exception:
        name = ""
    name = re.sub(r"\.com\b", "", name, flags=re.I)
    base = re.sub(r"\s+", " ", _SUFFIX.sub(" ", name)).strip()
    base = re.sub(r"[^\w]+$", "", base).strip()
    if base and base.upper() != ticker:
        keys.append(base)
        first = base.split(" ")[0]
        if len(first) >= 4 and first.lower() not in _GENERIC and first != base:
            keys.append(first)
    return keys


def _match(text: str, ticker: str | None, query: str | None, names: list | None = None) -> bool:
    if ticker:
        pats = [rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])"]
        pats += [rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z])" for n in names or []]
        if not any(re.search(p, text, 0 if i == 0 else re.I) for i, p in enumerate(pats)):
            return False
    if query and query.lower() not in text.lower():
        return False
    return True


@tool("get_news", "News",
      "Recent headlines from the terminal's news wire (Finviz, WSJ, MarketWatch, and Benzinga when connected), "
      "newest first, with Chinese translations where ZEUS made them. Filter by ticker or by words in the "
      "headline.",
      {"ticker": {**TICKER, "description": "Only stories about this ticker"},
       "query": {"type": "string", "description": "Only headlines containing these words"},
       "hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 24},
       "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}})
async def t_news(a):
    ticker = _ticker(a["ticker"]) if a.get("ticker") else None
    query = (a.get("query") or "").strip() or None
    names = _name_keys(ticker) if ticker else []
    since_ms = (_dt.datetime.now().timestamp() - a["hours"] * 3600) * 1000
    items = []
    free = await _soft(_api("/api/bilingual/data"), {})
    for art in (free or {}).get("articles") or []:
        ts = float(art.get("ts") or 0)
        if ts < since_ms:
            continue
        text = f"{art.get('title') or ''} {art.get('teaser') or ''}"
        if not _match(text, ticker, query, names):
            continue
        items.append({"ts": ts, "time": _ny_time(ts), "source": art.get("source"), "section": art.get("section"),
                      "title": art.get("title"), "title_zh": art.get("titleZh"),
                      "teaser": (art.get("teaser") or "")[:240] or None, "url": art.get("url")})
    days = max(1, math.ceil(a["hours"] / 24))
    if ticker:
        bz = await _soft(_api("/api/digest/by_ticker", {"ticker": ticker, "days": min(days, 30)}), {})
        bz_items = (bz or {}).get("items") or []
    else:
        bz = await _soft(_api("/api/digest/day"), {})
        bz_items = (bz or {}).get("items") or []
    for m in bz_items:
        ts = _to_ms(m.get("ts"))
        if ts and ts < since_ms:
            continue
        text = f"{m.get('title') or ''} {m.get('teaser') or ''} {' '.join(m.get('tickers') or [])}"
        if query and not _match(text, None, query):
            continue
        items.append({"ts": ts, "time": _ny_time(ts) if ts else f"{m.get('_date', '')} {m.get('hhmm', '')}".strip(),
                      "source": "Benzinga", "section": ", ".join(m.get("channels") or [])[:80] or None,
                      "title": m.get("title"), "title_zh": m.get("title_cn") or None,
                      "teaser": (m.get("teaser") or "")[:240] or None, "tickers": (m.get("tickers") or [])[:8] or None,
                      "sentiment": m.get("sentiment")})
    items.sort(key=lambda x: -x["ts"])
    for it in items:
        it.pop("ts", None)
    return {"filter": {"ticker": ticker, "also_matched": names or None, "query": query, "hours": a["hours"]},
            "count": len(items), "items": items[: a["limit"]]}


# ---------------------------------------------------------------- ideas and sectors
@tool("get_sector_rotation", "Sector rotation",
      "ZEUS's 80 industry groups (575 tickers): returns over 5 days, 1, 3 and 6 months and a year, the share "
      "of members above their 50- and 200-day averages, trend state and strength, the strongest members, "
      "and which groups lead, lag, warm up or cool down.",
      {"sort": {"type": "string", "enum": ["m1", "d5", "m3", "m6", "y1"], "default": "m1"},
       "limit": {"type": "integer", "minimum": 1, "maximum": 80, "default": 20},
       "weakest": {"type": "boolean", "default": False, "description": "Weakest first instead of strongest"}})
async def t_sectors(a):
    d = await _api("/api/industry_groups", timeout=120)
    rows = [r for r in d.get("rows") or [] if r.get(a["sort"]) is not None]
    rows.sort(key=lambda r: r.get(a["sort"]) or 0, reverse=not a["weakest"])
    rot = d.get("rotation") or {}
    return {"as_of": d.get("as_of"), "groups_total": d.get("n_groups"), "tickers_total": d.get("n_tickers"),
            "rotation": {k: [_label(x) for x in rot.get(k) or []] for k in ("leaders", "laggards", "warming", "cooling")},
            "sorted_by": a["sort"] + ("(weakest first)" if a["weakest"] else ""),
            "groups": [{"name": _label(r.get("name")), "members": r.get("n"),
                        "return_pct": {k: r.get(k) for k in ("d5", "m1", "m3", "m6", "y1")},
                        "pct_above_ma50": r.get("pct_above_ma50"), "pct_above_ma200": r.get("pct_above_ma200"),
                        "state": _label(r.get("state")), "strength": _label(r.get("grade")), "note": _label(r.get("note")),
                        "top": [f"{m.get('t')} {m.get('r'):+.1f}%" for m in (r.get("top") or [])[:3] if m.get("r") is not None]}
                       for r in rows[: a["limit"]]],
            "fields": "return_pct are the group's median returns in %; d5 = 5 days, m1/m3/m6 = months, y1 = a year."}


@tool("get_trade_ideas", "Trade ideas",
      "The latest trade ideas ZEUS has produced: AI picks (trend-following and dip-buying candidates with "
      "strikes and chance of profit) and the TA screener's signals (MACD, RSI, volume, EMA breaks and "
      "support / resistance confluence with walk-forward win rates and a suggested spread), best first.",
      {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}})
async def t_ideas(a):
    picks, ta = await asyncio.gather(_soft(_api("/api/ai_picks"), {}), _soft(_api("/api/ta/results"), {}))
    out = {}
    if picks and not picks.get("empty") and picks.get("picks"):
        out["ai_picks"] = {"date": picks.get("date"), "picks": _slim(picks.get("picks"), max_list=a["limit"])}
    else:
        out["ai_picks"] = "none yet today (Picker → 🚀 AI picks; runs on schedule when an AI provider is set)"
    rows = (ta or {}).get("rows") or []
    if rows:
        rows = sorted(rows, key=lambda r: -(r.get("score") or 0))[: a["limit"]]
        out["ta_signals"] = {"as_of": ta.get("ts") or ta.get("asof") or ta.get("generated_at"),
                             "rows": [_slim({k: v for k, v in r.items() if k not in ("score_detail",)}, max_list=8)
                                      for r in rows]}
    else:
        out["ta_signals"] = ("no scan yet (it runs at 09:40, 10:30, 12:00 and 15:00 New York time on trading days, "
                             "or from the TA screener page)")
    return out


@tool("get_ticker_report", "Ticker report",
      "One ticker in depth: quote, Finviz fundamentals and technicals, analyst actions, the company "
      "description, past earnings reactions (move on the day and the next day, EPS surprise), the TA "
      "screener's view if the ticker is in the universe, and the latest headlines.",
      {"ticker": TICKER}, required=("ticker",))
async def t_ticker(a):
    t = _ticker(a["ticker"])
    q, fv, eh, ta, news = await asyncio.gather(
        _soft(t_quotes({"tickers": [t]}), {}), _soft(_api(f"/api/finviz/{t}", timeout=60), {}),
        _soft(_api(f"/api/earnings-history/{t}", timeout=60), {}), _soft(_api(f"/api/ta/detail/{t}", timeout=60), {}),
        _soft(t_news({"ticker": t, "hours": 96, "limit": 8}), {}))
    out = {"ticker": t, "quote": (q.get("quotes") or [None])[0] if q else None}
    if fv and (fv.get("fundamentals") or fv.get("description")):
        out["fundamentals"] = fv.get("fundamentals")
        out["analysts"] = _slim(fv.get("analysts"), max_list=6)
        out["description"] = (fv.get("description") or "")[:600] or None
    if eh and eh.get("rows"):
        out["earnings_reactions"] = {"recent": _slim(eh.get("rows"), max_list=8),
                                     "down_probability": eh.get("down_prob"), "avg_day_move_pct": eh.get("avg_day_pct"),
                                     "avg_next_day_move_pct": eh.get("avg_next_pct")}
    if ta and not ta.get("error") and not ta.get("empty"):
        out["ta_screener"] = _slim(ta, max_list=10)
    out["news"] = (news or {}).get("items") or []
    return out


# ---------------------------------------------------------------- journal
@tool("get_trade_journal", "Trade journal",
      "Performance from the trade journal: win rate, profit factor, expectancy, average winner and loser, max "
      "drawdown and streaks; results by strategy, ticker, holding time, DTE at open and direction; and the "
      "latest closed trades with the notes, mistakes and lessons written in the decision log.",
      {"days": {"type": "integer", "minimum": 1, "maximum": 3650,
                "description": "Only trades closed in the last N days (default: all)"},
       "ticker": {**TICKER, "description": "Only this ticker"},
       "recent": {"type": "integer", "minimum": 0, "maximum": 50, "default": 10}})
async def t_journal(a):
    params = {}
    if a.get("days"):
        params["from"] = (_today() - _dt.timedelta(days=a["days"])).isoformat()
        params["to"] = _today().isoformat()
    if a.get("ticker"):
        params["ticker"] = _ticker(a["ticker"])
    stats, entries = await asyncio.gather(_api("/api/journal_stats", params, timeout=90),
                                          _soft(_api("/api/journal_entries/list", {"status": "all"}), {}))

    def tbl(rows, key, n=None):
        rows = [{k: r.get(k) for k in (key, "n", "win_rate", "net_pnl", "avg_pnl", "profit_factor", "best", "worst")}
                for r in rows or []]
        return rows if n is None else rows[:n]

    by_t = sorted(stats.get("by_ticker") or [], key=lambda r: r.get("net_pnl") or 0)
    curve = stats.get("equity_curve") or []
    out = {"filter": params or "all trades", "trades": stats.get("n_trades"), "kpi": stats.get("kpi"),
           "by_strategy": tbl(stats.get("by_strategy"), "strategy"),
           "by_direction": tbl(stats.get("by_direction"), "direction"),
           "by_holding_time": _slim(stats.get("by_holdtime")), "by_dte_at_open": _slim(stats.get("by_dte_at_open")),
           "worst_tickers": tbl(by_t, "ticker", 5), "best_tickers": tbl(list(reversed(by_t)), "ticker", 5),
           "equity_curve_points": len(curve)}
    rows = [e for e in (entries or {}).get("entries") or []
            if (e.get("_kind") == "close" or str(e.get("id", "")).startswith("close"))
            and (not a.get("ticker") or e.get("ticker") == params.get("ticker"))]
    rows.sort(key=lambda e: e.get("close_date") or "", reverse=True)
    out["recent_closed"] = [{k: e.get(k) for k in ("close_date", "ticker", "strategy", "spread_label", "open_date",
                                                   "net_pnl", "status", "process_score", "followed_plan",
                                                   "mistakes_made", "lesson_learned", "close_logic")}
                            for e in rows[: a["recent"]]]
    out["decision_logs_waiting"] = sum(1 for e in (entries or {}).get("entries") or [] if e.get("status") == "pending")
    return out


# ---------------------------------------------------------------- paper trade
def _paper_view(t: dict, marks: dict | None) -> dict:
    legs = t.get("legs") or []
    und = str(t.get("underlying") or "").upper()
    active = t.get("status") == "active"
    spot_q = ((marks or {}).get("spots") or {}).get(und) or {}
    spot = float(spot_q.get("price") or t.get("current_price") or 0)
    value_legs, unreal, today, have_today = [], 0.0, 0.0, False
    for l in legs:
        q = ((marks or {}).get("legs") or {}).get(l.get("symbol")) or {}
        if active:
            if l.get("type") == "S":
                px, pc = spot, float(spot_q.get("prev_close") or 0)
            else:
                px, pc = float(q.get("mark") or l.get("current") or 0), float(q.get("prev_close") or 0)
        else:
            px, pc = float(l.get("current") or 0), 0.0
        mult = 1 if l.get("type") == "S" else float(l.get("mult") or 100)
        unreal += (px - float(l.get("entry") or 0)) * float(l.get("qty") or 0) * mult
        if pc > 0:
            today += (px - pc) * float(l.get("qty") or 0) * mult
            have_today = True
        value_legs.append({**l, "current": px, "mid": px, "prev_close": pc})
    out = {"id": t.get("id"), "name": t.get("name"), "underlying": und, "group": t.get("group"),
           "status": "open" if active else "closed", "opened": (t.get("created_at") or "")[:16] or None,
           "closed": (t.get("closed_at") or "")[:16] or None, "expiration": t.get("expiration") or None,
           "legs": [_leg_out(l) for l in value_legs], "notes": t.get("notes") or None}
    cost = sum(float(l.get("entry") or 0) * float(l.get("qty") or 0) * (1 if l.get("type") == "S" else float(l.get("mult") or 100))
               for l in legs)
    out["cost_basis"] = _r(cost)
    if active:
        out["pnl_open"] = _r(unreal)
        out["pnl_open_pct"] = _r(unreal / abs(cost) * 100, 1) if cost else None
        out["pnl_today"] = _r(today) if have_today else None
        if spot > 0:
            strat = {"current_price": spot, "iv": float(t.get("iv") or 0.3), "risk_free_rate": float(t.get("risk_free_rate") or 0.045),
                     "expiration": t.get("expiration") or (_today() + _dt.timedelta(days=365)).isoformat(), "legs": value_legs}
            an = pf.analyze(strat)
            mp, ml = pf.expiry_extremes(strat)
            out.update(spot=_r(spot), dte=_r(an["dte"], 0), max_profit=_money(mp), max_loss=_money(ml),
                       breakevens=[_r(b) for b in an["breakevens"]], chance_of_profit_pct=_r(an["chance_of_profit"], 1))
    else:
        out["realized_pnl"] = _r(unreal)
        out["realized_pct"] = _r(unreal / abs(cost) * 100, 1) if cost else None
    return out


@tool("list_paper_trades", "Paper trades",
      "Simulated trades in ZEUS's Paper Trade tab with live P/L (open trades are marked to market) or the "
      "realized result (closed trades), plus max profit, max loss, breakevens and chance of profit.",
      {"status": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}})
async def t_paper_list(a):
    status = {"open": "active", "closed": "closed", "all": "all"}[a["status"]]
    d = await _api("/api/paper-trade/trades", {"status": status})
    trades = d.get("trades") or []
    marks = await _soft(_api("/api/paper-trade/marks", timeout=90), {}) if any(t.get("status") == "active" for t in trades) else {}
    views = [_paper_view(t, marks) for t in trades]
    total = {"pnl_open": _r(sum(v.get("pnl_open") or 0 for v in views)),
             "realized": _r(sum(v.get("realized_pnl") or 0 for v in views))}
    return {"count": len(views), "groups": d.get("groups"), "totals": total, "trades": views,
            "agents_may_trade": paper_allowed()}


def _need_paper_permission():
    if not paper_allowed():
        raise ToolError("Agents may not place paper trades on this terminal. The user can allow it in "
                        "ZEUS → ⚙ Settings → AI agents (MCP) → \"Agents may place paper trades\" = on.")


@tool("paper_trade_open", "Open a paper trade",
      "Open a simulated trade in ZEUS's Paper Trade tab, filled at the current mid unless a price is given. "
      "It is never sent to a broker. Option legs must share one expiration. Works only when the user allows "
      "agents to paper trade in ZEUS Settings.",
      {"ticker": TICKER, "legs": LEGS,
       "name": {"type": "string", "description": "Optional name; default is the strategy type"},
       "group": {"type": "string", "description": "Paper Trade group (default 'Agents')"},
       "notes": {"type": "string", "description": "Why the trade was opened"}},
      required=("ticker", "legs"), write=True)
async def t_paper_open(a):
    _need_paper_permission()
    ticker = _ticker(a["ticker"])
    base, legs, warnings = await _priced_legs(ticker, a["legs"])
    exps = sorted({l["exp"] for l in legs if l.get("exp")})
    if len(exps) > 1:
        raise ToolError("Paper Trade keeps one expiration per trade; open calendars and diagonals as separate trades.")
    body = {"underlying": ticker, "name": (a.get("name") or _strategy_name(legs))[:60],
            "group": (a.get("group") or "Agents")[:40], "notes": (a.get("notes") or "")[:1000],
            "expiration": exps[0] if exps else "", "current_price": float(base["price"]),
            "iv": float(base.get("iv") or 0.3), "risk_free_rate": 0.045,
            "earnings_date": base.get("earnings_date") or "", "earnings_time": base.get("earnings_time") or "",
            "legs": [{k: l.get(k) for k in ("type", "strike", "qty", "entry", "symbol", "streamer_symbol")}
                     for l in legs]}
    trade = await _api("/api/paper-trade/open", method="POST", body=body, write=True)
    view = _paper_view(trade, await _soft(_api("/api/paper-trade/marks", {"id": trade.get("id")}), {}))
    if warnings:
        view["warnings"] = warnings
    return {"opened": view, "note": "Simulated fill; nothing was sent to a broker."}


@tool("paper_trade_close", "Close a paper trade",
      "Close an open paper trade at the current market prices and return the realized P/L. Works only when the "
      "user allows agents to paper trade in ZEUS Settings.",
      {"trade_id": {"type": "string", "description": "id from list_paper_trades"}},
      required=("trade_id",), write=True, destructive=True)
async def t_paper_close(a):
    _need_paper_permission()
    tid = a["trade_id"].strip()
    if not re.match(r"^[A-Za-z0-9_-]{1,80}$", tid):
        raise ToolError("trade_id looks wrong; take it from list_paper_trades")
    trade = await _api(f"/api/paper-trade/close/{tid}", method="POST", body={}, write=True, timeout=90)
    return {"closed": _paper_view(trade, None), "note": "Simulated close at market; nothing was sent to a broker."}


# ====================================================================== prompts
def _reply_in() -> str:
    return {"zh": "Answer in Chinese (simplified).",
            "en": "Answer in English.",
            "bi": "Answer in Chinese and keep the English trading terms in brackets."}.get(
        _lang(), "Answer in the language I write in.")


PROMPTS = {
    "portfolio_review": {
        "title": "Review my positions",
        "description": "Hold, take profit, cut or roll for every open position, with the reasons and today's top actions.",
        "arguments": [],
        "text": lambda args: (
            "Review my open options book in ZEUS Terminal.\n"
            "1. Call get_status, then get_positions, get_market_overview and get_events with days=21.\n"
            "2. For each position decide: hold, take profit, cut or roll. Base it on the numbers ZEUS returns: "
            "progress to max profit against the regime's take-profit zone (exit_advice), days to expiration "
            "(gamma risk inside 21 days), distance from the stock price to the breakevens and short strikes, "
            "earnings or macro events before expiration, and the one-day stress and VaR.\n"
            "3. Check concentration: risk per underlying as a share of net liquidating value; flag anything above 10 %.\n"
            "4. Call get_news for the tickers that need action and say whether the news changes the decision.\n"
            "5. Finish with a table (position, action, reason) and the three actions that matter most today.\n"
            "Quote only numbers the tools return, and say when data is missing. " + _reply_in()),
    },
    "pretrade_check": {
        "title": "Check a trade before opening it",
        "description": "Four-gate checkup, liquidity, events, a concrete spread and a verdict for one ticker.",
        "arguments": [{"name": "ticker", "description": "Ticker symbol, e.g. NVDA", "required": True},
                      {"name": "direction", "description": "bull or bear (default bull)", "required": False}],
        "text": lambda args: (
            f"I am thinking about a {args.get('direction') or 'bull'} options trade on {args.get('ticker')}.\n"
            f"1. Call pretrade_checkup (ticker={args.get('ticker')}, direction={args.get('direction') or 'bull'}) "
            "and report each gate with its reasons and the verdict.\n"
            f"2. Call check_liquidity, and get_events with tickers=[{args.get('ticker')}] and days=60; warn if "
            "earnings fall before a likely expiration.\n"
            "3. For bull, call suggest_spreads; for bear, use get_option_chain to pick a bear put spread 30-45 days out.\n"
            "4. Run analyze_strategy on the best candidate: max profit, max loss, breakeven, chance of profit, reward to risk.\n"
            f"5. Call get_news for {args.get('ticker')} with hours=72.\n"
            "6. Verdict: open now, wait (and for what), or skip. If the checkup says not to open, say so plainly. "
            + _reply_in()),
    },
    "morning_brief": {
        "title": "Morning brief",
        "description": "Regime, risk radar, today's events and news, and the positions that need attention.",
        "arguments": [],
        "text": lambda args: (
            "Give me a pre-market brief from ZEUS.\n"
            "1. get_market_overview: regime, VIX, risk radar, sector rotation.\n"
            "2. get_events with days=7: this week's macro releases and earnings, especially for what I hold.\n"
            "3. get_news with hours=16 and limit=40: only the stories that move the market or my positions.\n"
            "4. get_positions: positions near their take-profit zone, near a short strike, or exposed to today's events.\n"
            "5. End with what to watch today and at most three actions. " + _reply_in()),
    },
    "journal_coach": {
        "title": "Journal coach",
        "description": "The habits that cost me money, from my own trade journal, and rules to fix them.",
        "arguments": [{"name": "days", "description": "Look back this many days (default 90)", "required": False}],
        "text": lambda args: (
            f"Coach me from my ZEUS trade journal. Call get_trade_journal with days={args.get('days') or 90} and recent=20.\n"
            "1. Find the patterns that cost money: by strategy, ticker, holding time, DTE at open and direction, "
            "and the mistakes and lessons in my decision logs.\n"
            "2. Compare winners with losers and name my three most expensive habits, with numbers.\n"
            "3. Suggest at most three concrete rules for next week, each tied to the evidence. " + _reply_in()),
    },
}


# ====================================================================== protocol
def _list_extras(result: dict, modern: bool) -> dict:
    if modern:
        result.update(ttlMs=LIST_TTL_MS, cacheScope="public")
    return result


async def call_tool(name: str, args) -> dict:
    """Run a tool; problems the agent can fix come back as isError results."""
    entry = TOOLS.get(name)
    if not entry:
        raise RpcError(-32602, f"Unknown tool: {name}. Available: {', '.join(TOOLS)}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RpcError(-32602, "arguments must be an object")
    try:
        clean = _check(entry["def"]["inputSchema"], args)
        data = await entry["fn"](clean)
        text = data if isinstance(data, str) else json.dumps(_drop_none(data), ensure_ascii=False,
                                                              separators=(",", ":"), default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:  # a bug or an outage: still tell the agent what happened
        return {"content": [{"type": "text", "text": f"{name} failed: {type(e).__name__}: {e}"}], "isError": True}


def get_prompt(name: str, args) -> dict:
    p = PROMPTS.get(name)
    if not p:
        raise RpcError(-32602, f"Unknown prompt: {name}. Available: {', '.join(PROMPTS)}")
    args = args if isinstance(args, dict) else {}
    for spec in p["arguments"]:
        if spec.get("required") and not str(args.get(spec["name"]) or "").strip():
            raise RpcError(-32602, f"Prompt {name} needs the argument '{spec['name']}'")
    if "ticker" in args:
        try:
            args = {**args, "ticker": _ticker(args["ticker"])}
        except ToolError as e:
            raise RpcError(-32602, str(e))
    if "direction" in args and str(args["direction"]).lower() not in ("bull", "bear", ""):
        raise RpcError(-32602, "direction must be bull or bear")
    return {"description": p["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": p["text"](args)}}]}


def capabilities() -> dict:
    return {"tools": {"listChanged": False}, "prompts": {"listChanged": False}}


async def dispatch(method: str, params: dict, modern: bool, legacy_version: str | None) -> dict:
    if method == "server/discover":
        return {"supportedVersions": list(SUPPORTED), "capabilities": capabilities(), "instructions": INSTRUCTIONS,
                "ttlMs": LIST_TTL_MS, "cacheScope": "public"}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in LEGACY else LEGACY[0]
        return {"protocolVersion": version, "capabilities": capabilities(), "serverInfo": server_info(),
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return _list_extras({"tools": [t["def"] for t in TOOLS.values()]}, modern)
    if method == "tools/call":
        return await call_tool(str(params.get("name") or ""), params.get("arguments"))
    if method == "prompts/list":
        return _list_extras({"prompts": [{"name": n, "title": p["title"], "description": p["description"],
                                          "arguments": p["arguments"]} for n, p in PROMPTS.items()]}, modern)
    if method == "prompts/get":
        return get_prompt(str(params.get("name") or ""), params.get("arguments"))
    raise RpcError(-32601, f"Method not found: {method}", status=404 if modern else 200)


_MODERN_ONLY = {"server/discover"}
_LEGACY_ONLY = {"initialize", "ping"}


def _decode_name(v: str) -> str:
    """Mcp-Name may use the Base64 sentinel form =?base64?…?= for values that are not plain ASCII."""
    m = re.fullmatch(r"=\?base64\?(.*)\?=", v.strip())
    if not m:
        return v
    try:
        return base64.b64decode(m.group(1)).decode("utf-8")
    except Exception:
        return "\x00invalid"


async def handle(msg, headers: dict | None = None) -> tuple[dict | None, int]:
    """One JSON-RPC message → (response or None, HTTP status). `headers` is None on stdio."""
    mid = msg.get("id") if isinstance(msg, dict) else None

    def err(e: RpcError):
        body = {"code": e.code, "message": e.message}
        if e.data is not None:
            body["data"] = e.data
        return {"jsonrpc": "2.0", "id": mid, "error": body}, e.status

    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return err(RpcError(-32600, "Invalid Request: expected a JSON-RPC 2.0 object", status=400))
    method = msg.get("method")
    if not isinstance(method, str):
        return err(RpcError(-32600, "Invalid Request: clients send requests and notifications only", status=400))
    if "id" not in msg:
        return None, 202  # notifications (initialized, cancelled, …) need no answer
    if mid is None or isinstance(mid, (bool, float, dict, list)):
        return err(RpcError(-32600, "Invalid Request: id must be a string or an integer", status=400))
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return err(RpcError(-32602, "params must be an object", status=400))
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(K_VERSION)
    h = {k.lower(): v for k, v in (headers or {}).items()}
    modern = version is not None and version not in LEGACY

    try:
        if version is not None:
            if headers is not None:
                hv = h.get("mcp-protocol-version")
                if hv is None or h.get("mcp-method") is None:
                    raise RpcError(-32020, "Header mismatch: MCP-Protocol-Version and Mcp-Method headers are required",
                                   status=400)
                if hv != version:
                    raise RpcError(-32020, f"Header mismatch: MCP-Protocol-Version header value '{hv}' does not "
                                           f"match body value '{version}'", status=400)
                if h.get("mcp-method") != method:
                    raise RpcError(-32020, f"Header mismatch: Mcp-Method header value '{h.get('mcp-method')}' does "
                                           f"not match body value '{method}'", status=400)
                if method in ("tools/call", "prompts/get", "resources/read"):
                    name = params.get("name") if method != "resources/read" else params.get("uri")
                    hn = h.get("mcp-name")
                    if hn is None or _decode_name(hn) != str(name):
                        raise RpcError(-32020, f"Header mismatch: Mcp-Name header value '{hn}' does not match "
                                               f"body value '{name}'", status=400)
            if version not in SUPPORTED:
                raise RpcError(-32022, "Unsupported protocol version",
                               {"supported": list(SUPPORTED), "requested": version}, status=400)
            if modern and not isinstance(meta.get(K_CAPS), dict):
                raise RpcError(-32602, f"Missing required _meta field {K_CAPS}", status=400)
            if modern and method in _LEGACY_ONLY:
                raise RpcError(-32601, f"Method not found: {method} (not part of protocol {version})", status=404)
            legacy_version = None if modern else version
        else:
            if method in _MODERN_ONLY:
                raise RpcError(-32602, f"Missing required _meta field {K_VERSION}", status=400)
            hv = h.get("mcp-protocol-version")
            if hv is not None and hv not in SUPPORTED:
                raise RpcError(-32600, f"Unsupported protocol version {hv}; supported: {', '.join(SUPPORTED)}",
                               {"supported": list(SUPPORTED), "requested": hv}, status=400)
            legacy_version = hv or NO_HEADER_VERSION
        result = await dispatch(method, params, modern, legacy_version)
    except RpcError as e:
        return err(e)
    except Exception as e:  # never let a bug take the endpoint down
        return err(RpcError(-32603, f"Internal error: {type(e).__name__}: {e}"))
    if modern:
        result = {"resultType": "complete", **result,
                  "_meta": {**(result.get("_meta") or {}), K_SERVER: server_info()}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}, 200


# ====================================================================== HTTP
def _refuse(status: int, message: str, code: int = -32600) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}, status_code=status)


def _host_only(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _local_request(request: Request) -> str | None:
    """Why this request may not use MCP, or None when it may."""
    client = request.client.host if request.client else ""
    if client not in _LOCAL_HOSTS and not client.startswith("127.") and client != "::ffff:127.0.0.1":
        return "MCP answers only programs on this computer"
    host = _host_only(request.headers.get("host", ""))
    if host and host not in _LOCAL_HOSTS:
        return "Host header must be localhost or 127.0.0.1"
    origin = request.headers.get("origin")
    if origin is not None and (urlparse(origin).hostname or "").lower() not in _LOCAL_HOSTS:
        return "cross-origin request refused"
    return None


@router.api_route("/mcp", methods=["GET", "DELETE", "PUT", "PATCH"])
async def mcp_other():
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request):
    why = _local_request(request)
    if why:
        return _refuse(403, why)
    if not enabled():
        return _refuse(403, "The MCP server is switched off in ZEUS → ⚙ Settings → AI agents (MCP).")
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        return _refuse(415, "Send the JSON-RPC message with Content-Type: application/json")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _refuse(413, "Message too large")
    try:
        msg = json.loads(raw)
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                            status_code=400)
    headers = dict(request.headers)
    if isinstance(msg, list):  # JSON-RPC batches (protocol 2025-03-26)
        if not msg:
            return _refuse(400, "Empty batch")
        replies = [r for r, _ in [await handle(m, headers) for m in msg[:50]] if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply, status = await handle(msg, headers)
    if reply is None:
        return Response(status_code=status)
    return JSONResponse(reply, status_code=status, headers={"Cache-Control": "no-store"})


async def self_test() -> dict:
    """Settings → AI agents → Test: the same requests a modern client sends, plus one real tool call."""
    if not enabled():
        return {"ok": False, "error": "the MCP server is switched off: set it to on and save"}
    meta = {K_VERSION: MODERN, K_CAPS: {}, "io.modelcontextprotocol/clientInfo": {"name": "zeus-settings", "version": "1"}}
    found, _ = await handle({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}})
    tools, _ = await handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}})
    probe, _ = await handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "get_status", "arguments": {}, "_meta": meta}})
    res = (probe or {}).get("result") or {}
    ok = "result" in (found or {}) and "result" in (tools or {}) and not res.get("isError", True)
    out = {"ok": ok, "tools": len(((tools or {}).get("result") or {}).get("tools", [])), "prompts": len(PROMPTS),
           "protocols": ", ".join(SUPPORTED), "paper_trades": "allowed" if paper_allowed() else "not allowed"}
    if not ok:
        out["error"] = (res.get("content") or [{}])[0].get("text") or "see the server log"
    return out


@router.get("/api/zeus/mcp")
async def mcp_info(request: Request):
    """For the Settings page: the address and copy-paste setup for each client."""
    port = request.url.port or int(hc.get("port") or 8765)
    return {"enabled": enabled(), "paper_trades": paper_allowed(),
            "url": f"http://127.0.0.1:{port}/mcp",
            "python": sys.executable, "bridge": str(_ROOT / "zeus_mcp_stdio.py"),
            "protocol_versions": list(SUPPORTED),
            "tools": [{"name": n, "title": t["def"]["title"], "write": not t["def"]["annotations"]["readOnlyHint"]}
                      for n, t in TOOLS.items()],
            "prompts": [{"name": n, "title": p["title"]} for n, p in PROMPTS.items()]}

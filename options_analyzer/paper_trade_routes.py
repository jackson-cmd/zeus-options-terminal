"""Paper Trade API: simulated option and stock trades with live profit/loss.

Nothing in this module sends an order anywhere. A trade is a JSON record in
options_analyzer/data/paper_trades.json (see paper_trade_api); prices come from
the market-data source the terminal already uses (tastytrade, or the free
Cboe / Yahoo / Finviz feeds in manual mode).

The server hands this module its data sources with configure(); the tests pass
fakes. Front end: paper_trade.py (PAPER_TRADE_JS).
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

import paper_trade_api

try:  # same write guard as the settings API: a custom header no other site can send
    from zeus_routes import _guard
except Exception:  # pragma: no cover
    def _guard(request: Request) -> None:
        if request.headers.get("x-zeus-client") != "1":
            raise HTTPException(403, "missing X-Zeus-Client header")

router = APIRouter(prefix="/api/paper-trade")
_write = [Depends(_guard)]

_deps: dict = {
    "client": lambda: None,          # current market-data client
    "option_marks": None,            # (client, [OCC symbol]) -> {sym: {"mark", "last", "prev_close"}}
    "equity_quotes": None,           # (client, [ticker]) -> {ticker: {"price", "prev_close"}}
    "quotes": lambda: {},            # cached underlying prices
    "metrics": lambda: {},           # cached IV / earnings per underlying
}

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9./-]{0,9}$")


def configure(**deps) -> None:
    unknown = set(deps) - set(_deps)
    if unknown:
        raise TypeError(f"unknown dependencies: {sorted(unknown)}")
    _deps.update(deps)


def _client():
    client = _deps["client"]()
    if client is None:
        raise HTTPException(503, "No market data yet. Finish setup in Settings (tastytrade login or manual mode) "
                                 "and wait for the first refresh.")
    return client


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _expired(leg: dict, trade: dict, today: _dt.date) -> bool:
    try:
        return _dt.date.fromisoformat(leg.get("exp") or trade.get("expiration") or "") < today
    except ValueError:
        return False


def _intrinsic(leg: dict, spot: float) -> float:
    k = _num(leg.get("strike"))
    return max(0.0, spot - k) if leg.get("type") == "C" else max(0.0, k - spot)


def compute_marks(trades: list) -> dict:
    """Current price of every leg of `trades`.

    Option legs: broker mark and previous close. Underlyings (and stock legs):
    last price and the official previous close. An option leg past its
    expiration with no quote is worth its intrinsic value at today's spot.
    """
    client = _client()
    opt_syms = sorted({l.get("symbol") for t in trades for l in t.get("legs", [])
                       if l.get("type") in ("C", "P") and l.get("symbol")})
    unders = sorted({str(t.get("underlying") or "").upper() for t in trades if t.get("underlying")})
    legs: dict = {}
    for i in range(0, len(opt_syms), 100):
        for sym, q in (_deps["option_marks"](client, opt_syms[i:i + 100]) or {}).items():
            mark = _num(q.get("mark")) or _num(q.get("last"))
            legs[sym] = {"mark": round(mark, 4), "prev_close": _num(q.get("prev_close"))}
    spots = (_deps["equity_quotes"](client, unders) or {}) if unders else {}
    today = _dt.date.today()
    for t in trades:
        spot = _num((spots.get(str(t.get("underlying") or "").upper()) or {}).get("price"))
        if spot <= 0:
            continue
        for leg in t.get("legs", []):
            sym = leg.get("symbol")
            if leg.get("type") in ("C", "P") and (legs.get(sym) or {}).get("mark", 0) <= 0 and _expired(leg, t, today):
                legs[sym] = {"mark": round(_intrinsic(leg, spot), 4), "prev_close": 0.0, "expired": True}
    return {"legs": legs, "spots": spots, "asof": _dt.datetime.now().isoformat(timespec="seconds")}


def _find(trade_id: str) -> dict:
    for t in paper_trade_api.list_trades()["trades"]:
        if t.get("id") == trade_id:
            return t
    raise HTTPException(404, "Trade not found")


def _leg_label(leg: dict, trade: dict) -> str:
    if leg.get("type") == "S":
        return f"{trade.get('underlying')} shares"
    k = _num(leg.get("strike"))
    return f"{trade.get('underlying')} {k:g}{leg.get('type')}"


def clean_trade(body) -> dict:
    """Validate a new trade and normalise its legs (stock: 1 share per unit; option: 100)."""
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    und = str(body.get("underlying") or "").strip().upper()
    if not _TICKER_RE.match(und):
        raise HTTPException(400, "Missing or invalid ticker")
    legs_in = body.get("legs")
    if not isinstance(legs_in, list) or not legs_in:
        raise HTTPException(400, "A trade needs at least one leg")
    legs = []
    for i, leg in enumerate(legs_in, 1):
        if not isinstance(leg, dict) or leg.get("type") not in ("C", "P", "S"):
            raise HTTPException(400, f"Leg {i}: type must be C (call), P (put) or S (stock)")
        try:
            qty, entry = float(leg.get("qty")), float(leg.get("entry"))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Leg {i}: quantity and entry price must be numbers")
        if qty == 0 or qty != int(qty):
            raise HTTPException(400, f"Leg {i}: quantity must be a whole number other than zero")
        if not entry >= 0:
            raise HTTPException(400, f"Leg {i}: entry price must be zero or more")
        out = dict(leg, qty=int(qty), entry=entry)
        out.setdefault("current", entry)
        out.setdefault("mid", entry)
        if leg["type"] == "S":
            out.update(symbol=und, strike=0, mult=1)
        else:
            if not leg.get("symbol"):
                raise HTTPException(400, f"Leg {i}: missing option symbol")
            try:
                out["strike"] = float(leg.get("strike"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Leg {i}: strike must be a number")
            out["mult"] = 100
        legs.append(out)
    exp = str(body.get("expiration") or "")
    if exp:
        try:
            _dt.date.fromisoformat(exp)
        except ValueError:
            raise HTTPException(400, "Expiration must look like YYYY-MM-DD")
    elif any(leg["type"] != "S" for leg in legs):
        raise HTTPException(400, "Option legs need an expiration")
    group = str(body.get("group") or "Default").strip()[:40] or "Default"
    return dict(body, underlying=und, legs=legs, expiration=exp, group=group)


# ------------------------------------------------------------------ routes
@router.get("/trades")
def trades(status: str = Query("all"), group: Optional[str] = Query(None)):
    return paper_trade_api.list_trades(group=group or None, status=status)


@router.get("/marks")
def marks(id: Optional[str] = Query(None)):
    active = [t for t in paper_trade_api.list_trades(status="active")["trades"] if not id or t.get("id") == id]
    if not active:
        return {"legs": {}, "spots": {}, "asof": None}
    return compute_marks(active)


@router.get("/chain/{ticker}")
def chain(ticker: str, expiration: Optional[str] = Query(None)):
    tk = ticker.strip().upper()
    if not _TICKER_RE.match(tk):
        raise HTTPException(400, "Invalid ticker")
    client = _client()
    data = dict(paper_trade_api.fetch_chain_for_builder(
        client, tk, expiration, quotes_cache=_deps["quotes"](), metrics_cache=_deps["metrics"]()))
    quote = (_deps["equity_quotes"](client, [tk]) or {}).get(tk) or {}
    if _num(quote.get("price")) > 0:
        data["price"] = _num(quote["price"])
    data["prev_close"] = _num(quote.get("prev_close"))
    if not data.get("price") and not data.get("expirations"):
        raise HTTPException(404, f"No quote or option chain found for {tk}")
    return data


@router.post("/open", dependencies=_write)
def open_trade(body: dict = Body(...)):
    return paper_trade_api.open_trade(clean_trade(body))


@router.post("/close/{trade_id}", dependencies=_write)
def close_trade(trade_id: str, body: Optional[dict] = Body(None)):
    trade = _find(trade_id)
    if trade.get("status") == "closed":
        return trade
    prices = (body or {}).get("close_prices")
    if not prices:
        m = compute_marks([trade])
        spot = _num((m["spots"].get(str(trade.get("underlying") or "").upper()) or {}).get("price"))
        prices, missing = {}, []
        for leg in trade.get("legs", []):
            if leg.get("type") == "S":
                px = spot if spot > 0 else None
            else:
                q = m["legs"].get(leg.get("symbol")) or {}
                px = q["mark"] if (q.get("mark", 0) > 0 or q.get("expired")) else None
            if px is None:
                missing.append(_leg_label(leg, trade))
            else:
                prices[leg.get("symbol") or trade["underlying"]] = px
        if missing:
            raise HTTPException(409, "No price yet for " + ", ".join(missing) + ". Try again in a minute.")
        if spot > 0:
            prices["close_stock_price"] = spot
    result = paper_trade_api.close_trade(trade_id, prices)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.post("/groups", dependencies=_write)
def groups(body: dict = Body(...)):
    action, name = body.get("action"), str(body.get("name") or "").strip()
    if action not in ("create", "rename", "delete") or not name or len(name) > 40:
        raise HTTPException(400, "Send action create, rename or delete with a group name of up to 40 characters")
    new_name = str(body.get("new_name") or "").strip()[:40] or None
    return paper_trade_api.manage_groups(action, name, new_name)


@router.put("/{trade_id}", dependencies=_write)
def update_trade(trade_id: str, body: dict = Body(...)):
    if "group" in body:
        body["group"] = str(body.get("group") or "").strip()[:40] or "Default"
    result = paper_trade_api.update_trade(trade_id, body)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.delete("/{trade_id}", dependencies=_write)
def delete_trade(trade_id: str):
    if not paper_trade_api.delete_trade(trade_id):
        raise HTTPException(404, "Trade not found")
    return {"ok": True}

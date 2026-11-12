"""Paper Trade API: validation, stock legs, live marks, closing at market, groups."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "options_analyzer"))

import paper_trade_api as pta  # noqa: E402
import paper_trade_routes as ptr  # noqa: E402

W = {"X-Zeus-Client": "1"}
TODAY = dt.date.today()
EXP = (TODAY + dt.timedelta(days=30)).isoformat()
PAST = (TODAY - dt.timedelta(days=3)).isoformat()
CALL = "AAPL  261218C00200000"
PUT = "AAPL  261218P00190000"


@pytest.fixture()
def market():
    """Fake quotes; tests change them in place."""
    return {"options": {CALL: {"mark": 4.10, "last": 4.0, "prev_close": 3.80},
                        PUT: {"mark": 2.50, "last": 2.4, "prev_close": 2.70}},
            "stocks": {"AAPL": {"price": 205.0, "prev_close": 203.0}}}


@pytest.fixture()
def client(tmp_path, monkeypatch, market):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(pta, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pta, "STORE_PATH", tmp_path / "paper_trades.json")
    ptr.configure(
        client=lambda: object(),
        option_marks=lambda c, syms: {s: market["options"][s] for s in syms if s in market["options"]},
        equity_quotes=lambda c, tks: {t: market["stocks"][t] for t in tks if t in market["stocks"]},
        quotes=lambda: {}, metrics=lambda: {})
    app = FastAPI()
    app.include_router(ptr.router)
    return TestClient(app)


def covered_call(**over):
    trade = {"underlying": "aapl", "name": "Covered Call", "current_price": 200.0, "expiration": EXP,
             "legs": [{"type": "S", "qty": 100, "entry": 200.0, "symbol": "ignored", "strike": 999},
                      {"type": "C", "qty": -1, "entry": 3.00, "symbol": CALL, "strike": 200}]}
    trade.update(over)
    return trade


def test_writes_need_the_client_header(client):
    assert client.post("/api/paper-trade/open", json=covered_call()).status_code == 403
    assert client.post("/api/paper-trade/open", json=covered_call(), headers=W).status_code == 200


@pytest.mark.parametrize("bad, msg", [
    ({"legs": []}, "at least one leg"),
    ({"legs": [{"type": "X", "qty": 1, "entry": 1}]}, "type must be"),
    ({"legs": [{"type": "C", "qty": 0, "entry": 1, "symbol": CALL, "strike": 200}]}, "whole number"),
    ({"legs": [{"type": "C", "qty": 1.5, "entry": 1, "symbol": CALL, "strike": 200}]}, "whole number"),
    ({"legs": [{"type": "C", "qty": 1, "entry": -1, "symbol": CALL, "strike": 200}]}, "zero or more"),
    ({"legs": [{"type": "C", "qty": 1, "entry": 1, "strike": 200}]}, "option symbol"),
    ({"expiration": ""}, "need an expiration"),
    ({"expiration": "12/18/2026"}, "YYYY-MM-DD"),
    ({"underlying": "not a ticker!"}, "ticker"),
])
def test_open_rejects_bad_trades(client, bad, msg):
    r = client.post("/api/paper-trade/open", json=covered_call(**bad), headers=W)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_stock_legs_are_shares_and_option_legs_are_contracts(client):
    t = client.post("/api/paper-trade/open", json=covered_call(), headers=W).json()
    stock, call = t["legs"]
    assert t["underlying"] == "AAPL" and t["status"] == "active" and t["group"] == "Default"
    assert (stock["symbol"], stock["strike"], stock["mult"], stock["qty"]) == ("AAPL", 0, 1, 100)
    assert (call["mult"], call["qty"], call["strike"]) == (100, -1, 200.0)


def test_stock_only_trade_needs_no_expiration(client):
    r = client.post("/api/paper-trade/open", headers=W, json={
        "underlying": "MSFT", "expiration": "", "legs": [{"type": "S", "qty": -50, "entry": 400}]})
    assert r.status_code == 200 and r.json()["expiration"] == ""


def test_marks_price_every_open_leg(client, market):
    client.post("/api/paper-trade/open", json=covered_call(), headers=W)
    m = client.get("/api/paper-trade/marks").json()
    assert m["legs"][CALL] == {"mark": 4.10, "prev_close": 3.80}
    assert m["spots"]["AAPL"] == {"price": 205.0, "prev_close": 203.0}
    assert m["asof"]


def test_expired_leg_without_quote_is_worth_intrinsic(client, market):
    market["options"].clear()
    client.post("/api/paper-trade/open", headers=W, json=covered_call(
        expiration=PAST, legs=[{"type": "P", "qty": 1, "entry": 1.0, "symbol": PUT, "strike": 210, "exp": PAST}]))
    leg = client.get("/api/paper-trade/marks").json()["legs"][PUT]
    assert leg == {"mark": 5.0, "prev_close": 0.0, "expired": True}       # 210 strike put, spot 205


def test_close_at_market_uses_spot_for_shares_and_marks_for_options(client):
    t = client.post("/api/paper-trade/open", json=covered_call(), headers=W).json()
    closed = client.post(f"/api/paper-trade/close/{t['id']}", json={}, headers=W).json()
    stock, call = closed["legs"]
    assert closed["status"] == "closed" and closed["close_price"] == 205.0 and closed["closed_at"]
    assert stock["current"] == 205.0 and call["current"] == 4.10
    # realized P/L: shares +5 x 100, short call 3.00 -> 4.10 x 100
    pnl = sum((l["current"] - l["entry"]) * l["qty"] * l["mult"] for l in closed["legs"])
    assert pnl == pytest.approx(500 - 110)
    again = client.post(f"/api/paper-trade/close/{t['id']}", json={}, headers=W)
    assert again.status_code == 200 and again.json()["closed_at"] == closed["closed_at"]


def test_close_refuses_without_a_price(client, market):
    t = client.post("/api/paper-trade/open", json=covered_call(), headers=W).json()
    market["options"].clear()
    r = client.post(f"/api/paper-trade/close/{t['id']}", json={}, headers=W)
    assert r.status_code == 409 and "AAPL 200C" in r.json()["detail"]
    assert client.get("/api/paper-trade/trades").json()["trades"][0]["status"] == "active"


def test_groups_update_delete(client):
    t = client.post("/api/paper-trade/open", json=covered_call(), headers=W).json()
    assert client.post("/api/paper-trade/groups", json={"action": "create", "name": "Income"}, headers=W).json() == ["Default", "Income"]
    assert client.post("/api/paper-trade/groups", json={"action": "explode", "name": "x"}, headers=W).status_code == 400
    assert client.put(f"/api/paper-trade/{t['id']}", json={"group": "Income"}, headers=W).json()["group"] == "Income"
    assert client.get("/api/paper-trade/trades", params={"group": "Income"}).json()["trades"][0]["id"] == t["id"]
    assert client.delete(f"/api/paper-trade/{t['id']}", headers=W).json() == {"ok": True}
    assert client.delete(f"/api/paper-trade/{t['id']}", headers=W).status_code == 404


def test_chain_adds_live_price_and_previous_close(client, monkeypatch):
    seen = {}

    def fake_chain(c, tk, exp, quotes_cache=None, metrics_cache=None):
        seen["args"] = (tk, exp)
        return {"ticker": tk, "price": 150.0, "iv": 0.3, "expirations": [{"date": EXP, "dte": 30, "type": "Regular"}], "strikes": []}

    monkeypatch.setattr(pta, "fetch_chain_for_builder", fake_chain)
    d = client.get("/api/paper-trade/chain/aapl", params={"expiration": EXP}).json()
    assert seen["args"] == ("AAPL", EXP)
    assert d["price"] == 205.0 and d["prev_close"] == 203.0
    assert client.get("/api/paper-trade/chain/bad$tk").status_code == 400


def test_no_market_data_source_yet(client):
    ptr.configure(client=lambda: None)
    assert client.get("/api/paper-trade/chain/AAPL").status_code == 503

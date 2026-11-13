"""Alpaca and Public: which source is used for positions and quotes, how their answers become the
tastytrade shapes the rest of ZEUS reads, the sample portfolio, and the pages that drive them.
No network: every HTTP call goes through a fake `send`."""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

import local_broker as lb
import zeus_brokers as zb
import zeus_config as hc

ROOT = Path(__file__).resolve().parent.parent / "zeus"   # the code
KEYS = ("account_source", "market_data", "alpaca_key_id", "alpaca_secret_key", "alpaca_paper", "alpaca_stock_feed",
        "alpaca_options_feed", "public_secret_key", "public_account_id", "tt_client_secret", "tt_refresh_token",
        "manual_starting_cash")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    hc.update({k: "" for k in KEYS})
    lb.save_trades([])
    lb._cache.clear()
    lb._broker_errors.clear()
    zb._instances.clear()
    yield
    hc.update({k: "" for k in KEYS})
    lb.save_trades([])
    zb._instances.clear()


EXP = (date.today() + timedelta(days=40)).isoformat()
YMD = EXP[2:4] + EXP[5:7] + EXP[8:10]


# --------------------------------------------------------------------------- which source
def test_sources_follow_the_keys():
    assert (hc.account_source(), hc.market_source()) == ("manual", "free")
    hc.update({"public_secret_key": "s"})
    assert (hc.account_source(), hc.market_source()) == ("public", "public")
    hc.update({"alpaca_key_id": "PK1", "alpaca_secret_key": "x"})
    assert (hc.account_source(), hc.market_source()) == ("alpaca", "alpaca")     # Alpaca before Public
    hc.update({"tt_client_secret": "a", "tt_refresh_token": "b"})
    assert (hc.account_source(), hc.market_source()) == ("tastytrade", "tastytrade")
    assert hc.broker_mode() == "tastytrade"


def test_picks_and_fallbacks():
    hc.update({"alpaca_key_id": "PK1", "alpaca_secret_key": "x", "account_source": "manual"})
    assert hc.account_source() == "manual" and hc.market_source() == "alpaca"   # quotes still from Alpaca
    hc.update({"market_data": "free"})
    assert hc.market_source() == "free"
    hc.update({"account_source": "public", "market_data": "public"})            # picked, but no Public key
    st = hc.status()
    assert hc.account_source() == "manual" and hc.market_source() == "free"
    assert st["broker_fallback"] == "public" and st["market_fallback"] == "public"
    assert st["brokers"] == {"tastytrade": False, "alpaca": True, "public": False}


def test_tastytrade_clients_follow_settings_without_restart():
    import importlib.util
    for rel in ("trading_assistant/api_client.py", "trade_journal/api_client.py", "eod_crawler/api_client.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "def local(self) -> bool:" in src and 'return _hc.account_source() != "tastytrade"' in src, rel
        assert "self.local = " not in src and "self._auth = None" in src, rel
    assert importlib.util.find_spec("zeus_brokers")


# --------------------------------------------------------------------------- fake HTTP
class Fake:
    """Records calls; answers by (method, path) → data or (status, data)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, params, body, timeout):
        u = urlparse(url)
        self.calls.append((method, u.netloc, u.path, dict(params or {}), body, dict(headers)))
        ans = self.routes.get((method, u.path))
        if callable(ans):
            ans = ans(params or {}, body)
        if ans is None:
            return 404, {"message": "not found"}
        return ans if isinstance(ans, tuple) else (200, ans)


OCC_C = f"AAPL{YMD}C00200000"
OCC_P = f"SPY{YMD}P00600000"


def alpaca_routes():
    return {
        ("GET", "/v2/account"): {"account_number": "PA123", "status": "ACTIVE", "equity": "105000", "cash": "40000",
                                 "buying_power": "160000", "options_buying_power": "40000", "long_market_value": "70000",
                                 "short_market_value": "-5000", "maintenance_margin": "9000", "last_equity": "104000",
                                 "options_trading_level": 3},
        ("GET", "/v2/positions"): [
            {"symbol": "MSFT", "asset_class": "us_equity", "qty": "10", "side": "long", "avg_entry_price": "400.5",
             "current_price": "410", "lastday_price": "405"},
            {"symbol": "BRK.B", "asset_class": "us_equity", "qty": "3", "side": "long", "avg_entry_price": "450",
             "current_price": "455", "lastday_price": "452"},
            {"symbol": OCC_C, "asset_class": "us_option", "qty": "2", "side": "long", "avg_entry_price": "5.25",
             "current_price": "6.1", "lastday_price": "5.8"},
            {"symbol": OCC_P, "asset_class": "us_option", "qty": "-1", "side": "short", "avg_entry_price": "3.4",
             "current_price": "2.9", "lastday_price": "3.0"},
        ],
        ("GET", "/v2/orders"): [
            {"id": "o1", "status": "new", "order_class": "mleg", "time_in_force": "gtc", "type": "limit",
             "limit_price": "1.1", "submitted_at": "2026-11-02T15:00:00Z", "qty": "1", "legs": [
                 {"symbol": OCC_P, "side": "buy", "position_intent": "buy_to_close", "qty": "1"},
                 {"symbol": f"SPY{YMD}P00590000", "side": "sell", "position_intent": "sell_to_close", "qty": "1"}]},
            {"id": "o2", "status": "canceled", "symbol": "MSFT", "side": "sell", "time_in_force": "day"},
            {"id": "o3", "status": "accepted", "symbol": "MSFT", "side": "sell", "position_intent": "sell_to_close",
             "time_in_force": "gtc", "type": "limit", "qty": "10", "limit_price": "450"},
        ],
        ("GET", "/v2/account/activities"): [
            {"id": "20261103::bbb", "activity_type": "FILL", "transaction_time": "2026-11-03T15:00:01.123456789Z",
             "symbol": OCC_P, "side": "sell_short", "qty": "1", "price": "3.4", "order_id": "x2"},
            {"id": "20261102::aaa", "activity_type": "FILL", "transaction_time": "2026-11-02T14:31:00Z",
             "symbol": "MSFT", "side": "buy", "qty": "10", "price": "400.5", "order_id": "x1"},
        ],
        ("GET", "/v2/stocks/snapshots"): lambda params, body: {
            s: {"latestTrade": {"p": {"MSFT": 410.0, "BRK.B": 455.0}.get(s, 100.0), "t": "2026-11-12T20:59:00Z"},
                "latestQuote": {"bp": 409.9, "ap": 0}, "dailyBar": {"h": 412, "l": 401, "v": 12345, "c": 410},
                "prevDailyBar": {"c": 405.0}}
            for s in params["symbols"].split(",")},
        ("GET", "/v1beta1/options/snapshots"): lambda params, body: {"snapshots": {
            s: {"latestQuote": {"bp": 6.0, "ap": 6.2}, "latestTrade": {"p": 6.1}, "prevDailyBar": {"c": 5.8},
                "dailyBar": {"v": 77}, "impliedVolatility": 0.31,
                "greeks": {"delta": 0.55, "gamma": 0.02, "theta": -0.05, "vega": 0.3, "rho": 0.1}}
            for s in params["symbols"].split(",")}},
        ("GET", "/v2/options/contracts"): {"option_contracts": [
            {"symbol": OCC_C, "expiration_date": EXP, "strike_price": "200", "type": "call", "open_interest": "1500"},
            {"symbol": f"AAPL{YMD}P00200000", "expiration_date": EXP, "strike_price": "200", "type": "put"},
            {"symbol": f"AAPL{YMD}C00210000", "expiration_date": EXP, "strike_price": "210", "type": "call"}],
            "next_page_token": None},
    }


def make_alpaca(routes=None):
    fake = Fake(routes or alpaca_routes())
    return zb.Alpaca("PKTEST", "secret", paper=True, send=fake), fake


def test_alpaca_positions_balances_orders():
    a, fake = make_alpaca()
    pos = {p["symbol"]: p for p in a.tt_positions()}
    assert set(pos) == {"MSFT", "BRK/B", zb.padded_occ(OCC_C), zb.padded_occ(OCC_P)}
    c, p = pos[zb.padded_occ(OCC_C)], pos[zb.padded_occ(OCC_P)]
    assert c["instrument-type"] == "Equity Option" and c["underlying-symbol"] == "AAPL" and c["multiplier"] == 100
    assert c["quantity"] == 2 and c["quantity-direction"] == "Long" and c["close-price"] == "5.8000"
    assert c["streamer-symbol"] == f".AAPL{YMD}C200" and c["expires-at"].startswith(EXP)
    assert p["quantity"] == 1 and p["quantity-direction"] == "Short" and p["cost-effect"] == "Credit"
    assert pos["MSFT"]["average-open-price"] == "400.5000" and pos["MSFT"]["account-number"] == "ALPACA-PAPER"
    b = a.tt_balances()
    assert b["net-liquidating-value"] == "105000.00" and b["derivative-buying-power"] == "40000.00"
    assert b["short-equity-value"] == "5000.00"
    orders = a.tt_orders()
    assert [o["id"] for o in orders] == ["o1", "o3"]            # the cancelled one is not working
    assert orders[0]["underlying-symbol"] == "SPY" and orders[0]["time-in-force"] == "GTC"
    assert [leg["action"] for leg in orders[0]["legs"]] == ["Buy to Close", "Sell to Close"]
    assert orders[1]["legs"][0]["action"] == "Sell to Close"
    # keys go only to Alpaca, in its headers
    hosts = {c[1] for c in fake.calls}
    assert hosts <= {"paper-api.alpaca.markets", "data.alpaca.markets"}
    assert all(c[5]["APCA-API-KEY-ID"] == "PKTEST" for c in fake.calls)


def test_alpaca_live_keys_use_the_live_host():
    fake = Fake(alpaca_routes())
    a = zb.Alpaca("AKLIVE", "s", paper=False, send=fake)
    a.account()
    assert fake.calls[0][1] == "api.alpaca.markets" and a.account_label == "ALPACA"


def test_alpaca_fills_become_trades_and_replay():
    a, _ = make_alpaca()
    trades = a.trades()
    assert [t["symbol"] for t in trades] == ["MSFT", "SPY"]      # oldest first
    put = trades[1]
    assert put["asset"] == "option" and put["option_type"] == "P" and put["strike"] == 600.0
    assert put["side"] == "sell" and put["expiration"] == EXP and put["multiplier"] == 100.0
    assert trades[0]["date"] == "2026-11-02T09:31:00"            # New York wall clock
    st = lb.replay_trades([lb.normalize_trade(t) for t in trades], ("t", 1), label="ALPACA-PAPER")
    actions = [(x["symbol"].strip(), x["action"]) for x in st["transactions"]]
    assert ("MSFT", "Buy to Open") in actions and (zb.padded_occ(OCC_P), "Sell to Open") in actions
    assert all(x["account-number"] == "ALPACA-PAPER" for x in st["transactions"])


def test_alpaca_quotes():
    a, fake = make_alpaca()
    q = a.stock_snapshots(["MSFT", "BRK/B", "VIX", "^GSPC"])
    assert set(q) == {"MSFT", "BRK/B", "VIX"} or set(q) == {"MSFT", "BRK/B"}
    assert q["MSFT"]["last"] == 410.0 and q["MSFT"]["prev_close"] == 405.0
    assert q["MSFT"]["bid"] == 0.0 and q["MSFT"]["ask"] == 0.0   # one-sided IEX quote is not shown
    sent = [c for c in fake.calls if c[2] == "/v2/stocks/snapshots"][0][3]
    assert "BRK.B" in sent["symbols"] and sent["feed"] == "iex"
    a.contracts("AAPL")
    o = a.option_snapshots([zb.padded_occ(OCC_C)])[zb.padded_occ(OCC_C)]
    assert (o["bid"], o["ask"], o["last"], o["prev_close"], o["iv"]) == (6.0, 6.2, 6.1, 5.8, 0.31)
    assert o["delta"] == 0.55 and o["oi"] == 1500 and o["volume"] == 77
    assert a.strikes("AAPL") == {EXP: {200.0, 210.0}}
    n = len(fake.calls)
    a.stock_snapshots(["MSFT"])                                  # cached
    assert len(fake.calls) == n


def test_alpaca_errors_say_what_to_check():
    routes = alpaca_routes()
    routes[("GET", "/v2/account")] = (401, {"message": "unauthorized."})
    a, _ = make_alpaca(routes)
    with pytest.raises(zb.BrokerAPIError, match="paper or live keys"):
        a.account()


# --------------------------------------------------------------------------- Public
def public_routes(token_calls):
    def token(params, body):
        token_calls.append(body)
        return {"accessToken": f"tok{len(token_calls)}"}
    return {
        ("POST", "/userapiauthservice/personal/access-tokens"): token,
        ("GET", "/userapigateway/trading/account"): {"accounts": [
            {"accountId": "HY1", "accountType": "HIGH_YIELD"}, {"accountId": "BR1", "accountType": "BROKERAGE"}]},
        ("GET", "/userapigateway/trading/BR1/portfolio/v2"): {
            "accountId": "BR1", "accountType": "BROKERAGE", "cash": "2500.50", "totalAccountValue": "30000",
            "buyingPower": {"buyingPower": "5000", "optionsBuyingPower": "2500", "dayTradeBuyingPower": "0"},
            "equity": [{"type": "CASH", "value": "2500.50"}],
            "positions": [
                {"instrument": {"symbol": "AAPL", "type": "EQUITY"}, "quantity": "10", "openedAt": "2026-10-01T14:00:00Z",
                 "lastPrice": {"lastPrice": "350"}, "positionDailyGain": {"gainValue": "20"},
                 "costBasis": {"totalCost": "3000", "unitCost": "300"}},
                {"instrument": {"symbol": OCC_C, "type": "OPTION"}, "quantity": "-2",
                 "lastPrice": {"lastPrice": "4.0"}, "positionDailyGain": {"gainValue": "-40"},
                 "costBasis": {"totalCost": "-1000"}},
                {"instrument": {"symbol": "BTC", "type": "CRYPTO"}, "quantity": "0.1"}],
            "orders": [
                {"orderId": "p1", "status": "NEW", "type": "LIMIT", "limitPrice": "1.5",
                 "expiration": {"timeInForce": "GTD"}, "instrument": {"symbol": OCC_C, "type": "OPTION"},
                 "side": "BUY", "openCloseIndicator": "CLOSE"},
                {"orderId": "p2", "status": "FILLED", "instrument": {"symbol": "AAPL", "type": "EQUITY"}}]},
        ("GET", "/userapigateway/trading/BR1/history"): {"transactions": [
            {"id": "t2", "timestamp": "2026-11-03T15:00:00Z", "type": "TRADE", "securityType": "OPTION",
             "symbol": OCC_C, "side": "SELL", "quantity": "2", "principalAmount": "1000", "fees": "1.30"},
            {"id": "t1", "timestamp": "2026-10-01T14:00:00Z", "type": "TRADE", "securityType": "EQUITY",
             "symbol": "AAPL", "side": "BUY", "quantity": "10", "principalAmount": "-3000"},
            {"id": "t0", "timestamp": "2026-09-30T14:00:00Z", "type": "MONEY_MOVEMENT", "subType": "DEPOSIT",
             "netAmount": "5000"}], "nextToken": None},
        ("POST", "/userapigateway/marketdata/BR1/quotes"): lambda params, body: {"quotes": [
            {"instrument": i, "outcome": "SUCCESS", "last": "350.5", "bid": "350.4", "ask": "350.6",
             "previousClose": "348", "volume": 1000, "openInterest": 0}
            if i["type"] == "EQUITY" else
            {"instrument": i, "outcome": "SUCCESS", "last": "4.1", "bid": "4.0", "ask": "4.2", "previousClose": "4.3",
             "volume": 12, "openInterest": 900, "optionDetails": {"greeks": {
                 "delta": "0.45", "gamma": "0.03", "theta": "-0.06", "vega": "0.2", "impliedVolatility": "0.28"}}}
            for i in body["instruments"]]},
        ("POST", "/userapigateway/marketdata/BR1/option-expirations"): {"baseSymbol": "AAPL", "expirations": [EXP]},
        ("POST", "/userapigateway/marketdata/BR1/option-chain"): {"baseSymbol": "AAPL", "calls": [
            {"instrument": {"symbol": OCC_C, "type": "OPTION"}, "outcome": "SUCCESS", "bid": "4", "ask": "4.2",
             "optionDetails": {"greeks": {"delta": "0.45"}}}], "puts": []},
    }


def test_public_account_quotes_and_chain():
    tokens = []
    fake = Fake(public_routes(tokens))
    p = zb.Public("SECRET", send=fake)
    pos = {x["symbol"]: x for x in p.tt_positions()}
    assert set(pos) == {"AAPL", zb.padded_occ(OCC_C)}               # crypto left out
    aapl, call = pos["AAPL"], pos[zb.padded_occ(OCC_C)]
    assert aapl["average-open-price"] == "300.0000" and aapl["close-price"] == "348.0000"
    assert call["quantity-direction"] == "Short" and call["average-open-price"] == "5.0000"
    assert call["close-price"] == "3.8000"                          # 4.0 - (-40 / (-2 x 100))
    assert p.account_id == "BR1"                                      # the brokerage account was picked
    b = p.tt_balances()
    assert b["net-liquidating-value"] == "30000.00" and b["cash-balance"] == "2500.50"
    assert b["derivative-buying-power"] == "2500.00"
    trades = p.trades()
    assert [t["symbol"] for t in trades] == ["AAPL", "AAPL"] and trades[1]["asset"] == "option"
    assert trades[1]["price"] == 5.0 and trades[1]["fees"] == 1.3 and trades[0]["price"] == 300.0
    orders = p.tt_orders()
    assert [o["id"] for o in orders] == ["p1"] and orders[0]["legs"][0]["action"] == "Buy to Close"
    assert orders[0]["time-in-force"] == "GTC"
    q = p.quotes(["AAPL", zb.padded_occ(OCC_C)])
    assert q["AAPL"]["last"] == 350.5 and q["AAPL"]["prev_close"] == 348.0
    oq = q[zb.padded_occ(OCC_C)]
    assert oq["iv"] == 0.28 and oq["delta"] == 0.45 and oq["oi"] == 900
    sent = [c for c in fake.calls if c[2].endswith("/quotes")][0][4]
    assert {"symbol": OCC_C, "type": "OPTION"} in sent["instruments"]
    assert p.strikes("AAPL") == {EXP: {200.0}}
    assert tokens == [{"validityInMinutes": 60, "secret": "SECRET"}]  # one token, reused
    assert all(c[1] == "api.public.com" for c in fake.calls)
    assert all(c[5].get("Authorization") == "Bearer tok1" for c in fake.calls if "access-tokens" not in c[2])


def test_public_gets_a_new_token_after_401():
    tokens = []
    routes = public_routes(tokens)
    seen = []

    def portfolio(params, body):
        seen.append(1)
        return (401, {"message": "expired"}) if len(seen) == 1 else routes_ok
    routes_ok = routes[("GET", "/userapigateway/trading/BR1/portfolio/v2")]
    routes[("GET", "/userapigateway/trading/BR1/portfolio/v2")] = portfolio
    p = zb.Public("SECRET", account_id="BR1", send=Fake(routes))
    assert p.portfolio()["accountId"] == "BR1" and len(tokens) == 2


def test_public_bad_secret():
    p = zb.Public("bad", send=Fake({("POST", "/userapiauthservice/personal/access-tokens"): (401, {"message": "no"})}))
    with pytest.raises(zb.BrokerAPIError, match="secret key"):
        p.tt_balances()


# --------------------------------------------------------------------------- local_broker routing
@pytest.fixture
def alpaca_on(monkeypatch):
    a, fake = make_alpaca()
    hc.update({"alpaca_key_id": "PKTEST", "alpaca_secret_key": "secret"})
    monkeypatch.setattr(zb, "alpaca", lambda: a)
    monkeypatch.setattr(lb, "_free_equity_quote", lambda s: {"last": 18.5, "prev_close": 17.0})
    monkeypatch.setattr(lb, "_cboe_book", lambda und: None)
    monkeypatch.setattr(lb, "_cboe_strikes", lambda und: {})
    return a, fake


def test_rest_answers_come_from_alpaca(alpaca_on):
    a, fake = alpaca_on
    acct = lb.handle_get("/customers/me/accounts")["data"]["items"][0]["account"]
    assert acct["account-number"] == "ALPACA-PAPER" and acct["nickname"] == "Alpaca (paper)"
    pos = lb.handle_get("/accounts/ALPACA-PAPER/positions")["data"]["items"]
    assert len(pos) == 4
    msft = next(p for p in pos if p["symbol"] == "MSFT")
    assert msft["created-at"].startswith("2026-11-02T14:31")      # opened time from the fills (EST, UTC-5)
    assert lb.handle_get("/accounts/ALPACA-PAPER/balances")["data"]["net-liquidating-value"] == "105000.00"
    assert len(lb.handle_get("/accounts/ALPACA-PAPER/orders/live")["data"]["items"]) == 2
    tx = lb.handle_get("/accounts/ALPACA-PAPER/transactions")["data"]["items"]
    assert len(tx) == 2
    items = lb.handle_get("/market-data/by-type", {"equity": "MSFT,VIX", "equity-option": zb.padded_occ(OCC_C)})
    by = {i["symbol"]: i for i in items["data"]["items"]}
    assert by["MSFT"]["last"] == 410.0                            # Alpaca
    assert by["VIX"]["last"] == 18.5                               # index → free sources
    opt = by[zb.padded_occ(OCC_C)]
    assert opt["bid"] == 6.0 and opt["implied-volatility"] == 0.31 and opt["delta"] == 0.55
    assert lb.expirations("AAPL") == [EXP]                          # Cboe empty → Alpaca contracts
    s = lb.summary()
    assert s["source"] == "alpaca" and s["open_positions"] == 4 and s["paper"] is True


def test_broker_errors_fall_back_to_free_quotes(monkeypatch):
    routes = alpaca_routes()
    routes[("GET", "/v2/stocks/snapshots")] = (429, {"message": "too many requests"})
    a, _ = make_alpaca(routes)
    hc.update({"alpaca_key_id": "PKTEST", "alpaca_secret_key": "secret", "account_source": "manual"})
    monkeypatch.setattr(zb, "alpaca", lambda: a)
    monkeypatch.setattr(lb, "_free_equity_quote", lambda s: {"last": 99.0, "prev_close": 98.0})
    assert lb.equity_quote("MSFT")["last"] == 99.0
    assert "429" in lb.broker_errors().get("alpaca", "")
    n = len(a.send.calls)
    assert lb.equity_quote("AAPL")["last"] == 99.0 and len(a.send.calls) == n      # resting for a minute


# --------------------------------------------------------------------------- sample portfolio
def fake_book(und, spot):
    rows = {}
    exps = [date.today() + timedelta(days=d) for d in (9, 30, 37, 44, 51, 72, 86)]
    for e in exps:
        ymd = e.strftime("%y%m%d")
        for i in range(-30, 31):
            k = round(spot * (1 + i * 0.01))
            for cp in "CP":
                intrinsic = max(spot - k, 0) if cp == "C" else max(k - spot, 0)
                mid = intrinsic + spot * 0.02
                rows[(und, ymd, cp, k * 1000)] = (mid - 0.05, mid + 0.05, mid, mid, 0.3, 10, 100, 0.5, 0.01, -0.02, 0.1)
    return {"spot": spot, "iv30": 0.3, "rows": rows}


def test_sample_portfolio_load_and_remove(monkeypatch):
    spots = {"SPY": 700.0, "QQQ": 600.0, "AAPL": 350.0, "NVDA": 170.0, "MSFT": 520.0, "TSLA": 400.0}
    monkeypatch.setattr(lb, "_cboe_book", lambda und: fake_book(und, spots[und]) if und in spots else None)
    monkeypatch.setattr(lb, "_free_equity_quote", lambda s: {"last": 200.0, "prev_close": 199.0})
    monkeypatch.setattr(lb, "equity_quote", lambda s: {"last": 200.0, "prev_close": 199.0})
    monkeypatch.setattr(lb, "option_quote", lambda occ: {"bid": 1.0, "ask": 1.2, "last": 1.1, "prev_close": 1.1})
    mine = lb.normalize_trade({"date": "2026-01-05 10:00", "symbol": "KO", "asset": "stock", "side": "buy", "qty": 5,
                               "price": 60})
    lb.add_trades([mine])
    res = lb.load_sample()
    assert res["ok"] and res["added"] == 13 and res["starting_cash_set"]
    trades = lb.load_trades()
    sample = [t for t in trades if t["id"].startswith("sample-")]
    assert len(sample) == 13 and lb.sample_count() == 13
    groups = {t["order_id"] for t in sample}
    assert {"sample-spy-bps", "sample-nvda-bcs", "sample-qqq-ic", "sample-msft-call", "sample-tsla-csp"} <= groups
    ic = [t for t in sample if t["order_id"] == "sample-qqq-ic"]
    assert len(ic) == 4 and len({t["expiration"] for t in ic}) == 1
    for t in sample:
        if t["asset"] == "option":
            assert 20 <= (date.fromisoformat(t["expiration"]) - date.today()).days <= 100
    assert hc.get("manual_starting_cash") == "50000"
    st = lb.replay()
    assert len(st["positions"]) == 12 and st["realized"] > 0          # 11 sample legs + KO; AMD closed at a profit
    assert lb.summary()["sample"] == 13
    lb.load_sample()                                                   # loading twice does not double it
    assert lb.sample_count() == 13
    out = lb.remove_sample()
    assert out["removed"] == 13 and [t["symbol"] for t in lb.load_trades()] == ["KO"]
    assert str(hc.get("manual_starting_cash")) in ("", "0")


def test_sample_needs_quotes(monkeypatch):
    monkeypatch.setattr(lb, "_cboe_book", lambda und: None)
    with pytest.raises(lb.LocalModeError):
        lb.sample_trades()


# --------------------------------------------------------------------------- pages
SETTINGS = (ROOT / "options_analyzer" / "static" / "settings.html").read_text(encoding="utf-8")
ZEUS_JS = (ROOT / "options_analyzer" / "static" / "zeus.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "zeus_routes.py").read_text(encoding="utf-8")
SERVER = (ROOT / "options_analyzer" / "optionstrat_server.py").read_text(encoding="utf-8")


def test_settings_page_has_every_broker_block_and_choice():
    for key in ("tt_client_id", "alpaca_key_id", "public_secret_key", "manual_starting_cash"):
        assert key in hc.SCHEMA and f"  {key}: {{ id: " in SETTINGS, key
    for key, vals in (("account_source", "['auto', 'tastytrade', 'alpaca', 'public', 'manual']"),
                      ("market_data", "['auto', 'free', 'alpaca', 'public']"), ("alpaca_paper", "['on', 'off']"),
                      ("alpaca_stock_feed", "['iex', 'delayed_sip', 'sip']"),
                      ("alpaca_options_feed", "['indicative', 'opra']")):
        assert f"{key}: {vals}" in SETTINGS, key
        assert hc.SCHEMA[key][0] == re.findall(r"'(\w+)'", vals)[0], key      # the default is listed first
    for t in ("alpaca", "public", "free", "tastytrade"):
        assert f"test: '{t}'" in SETTINGS
    assert 'if what in ("alpaca", "public"):' in ROUTES and 'if what == "free":' in ROUTES
    assert '"/api/trades/sample"' in ROUTES


def test_welcome_card_and_sample_bar():
    hint = ZEUS_JS.split("function manualHint() {", 1)[1].split("\n  function sampleCall", 1)[0]
    for ident in ("zsHintSample", "zsHintTT", "zsHintCSV", "zsSampleOff", "zsSampleBroker"):
        assert ident in hint, ident
    assert "openSettings('broker', 'tradesSec')" in hint and "sampleCall('POST'" in hint
    assert "fetch('/api/trades/sample'" in ZEUS_JS and "'X-Zeus-Client': '1'" in ZEUS_JS
    assert "#tradesSec" in SETTINGS or "id=\"tradesSec\"" in SETTINGS


def test_esc_closes_every_pop_up():
    for pid in ("posAiOverlay", "hmAiModal", "dgAiOverlay", "earningsModal", "trStatusPanel"):
        assert pid + ":" in ZEUS_JS, pid
        # the id really is a pop-up somewhere in the pages
        assert re.search(r"""id=["']%s["']|\.id = ['"]%s['"]""" % (pid, pid), SERVER + (ROOT / "heatmap" / "heatmap.py")
                         .read_text(encoding="utf-8")), pid
    assert "var PANELS = { trStatusPanel: 'trStatusBtn' };" in ZEUS_JS
    assert "!inPopup) return;" in ZEUS_JS and "if (closeTopPopup()) return;" in ZEUS_JS
    assert "#trStatusPanel { white-space: normal; }" in SERVER


def test_first_visit_is_not_pulled_away_from_the_welcome_card():
    hm = (ROOT / "heatmap" / "heatmap.py").read_text(encoding="utf-8")
    catch = hm.split("function _hmMaybeDailyCatchup() {", 1)[1].split("\n  }\n", 1)[0]
    assert "first = localStorage.getItem('hmSeenDate') === null" in catch and "_hmWelcomeShowing()" in catch
    assert "!_hmPopupAlreadyShown(r.id) && !_hmWelcomeShowing()" in hm


def test_journal_without_data_explains_itself():
    dash = (ROOT / "trade_journal" / "dashboard.html").read_text(encoding="utf-8")
    # the line the server rewrites must still be there, or the friendly empty state silently disappears
    assert "showLoadError('Auto-load failed: ' + e.message + '. Use the file picker instead.');" in dash
    assert "window.__zsJournalEmpty = function" in SERVER and "tryAutoLoad(); }, 10000)" in SERVER


def test_status_reports_the_broker_summary():
    src = ROUTES.split("async def zeus_status():", 1)[1].split("\n\n", 1)[0]
    assert 'st["manual" if st["broker_mode"] == "manual" else "broker"] = summ' in src
    assert 'st["broker_errors"] = lb.broker_errors()' in src
    js = json.dumps(hc.status())
    assert '"market_source"' in js and '"brokers"' in js

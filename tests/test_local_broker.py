"""Manual-mode broker: symbol parsing, CSV import, FIFO replay, expirations,
cash, and the tastytrade-shaped REST answers."""
import pytest

import local_broker as lb


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    lb.save_trades([])
    lb._cache.clear()
    # deterministic quotes, no network
    monkeypatch.setattr(lb, "equity_quote", lambda s: {"last": 100.0, "prev_close": 98.0})
    monkeypatch.setattr(lb, "option_quote", lambda occ: {"bid": 1.9, "ask": 2.1, "last": 2.0, "prev_close": 1.8,
                                                         "iv": 0.3, "volume": 10, "oi": 100})
    monkeypatch.setattr(lb, "_intrinsic_at_expiry", lambda und, cp, k, exp: 5.0)
    yield
    lb.save_trades([])


def T(**kw):
    base = {"date": "2026-01-05 10:00", "symbol": "AAPL", "asset": "stock", "side": "buy", "effect": "auto",
            "qty": 1, "price": 100, "fees": 0}
    base.update(kw)
    return lb.normalize_trade(base)


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,exp", [
    ("AAPL  261218C00200000", ("AAPL", "2026-12-18", "C", 200.0)),
    ("AAPL261218C00200000", ("AAPL", "2026-12-18", "C", 200.0)),
    (".AAPL261218C200", ("AAPL", "2026-12-18", "C", 200.0)),
    ("SPY 12/18/2026 650.5 P", ("SPY", "2026-12-18", "P", 650.5)),
    ("MSFT 6/18/26 Call $500.00", ("MSFT", "2026-06-18", "C", 500.0)),
])
def test_parse_option_symbol(text, exp):
    p = lb.parse_option_symbol(text)
    assert (p["underlying"], p["expiration"], p["type"], p["strike"]) == exp


def test_occ_round_trip():
    occ = lb.occ_symbol("AAPL", "2026-12-18", "C", 200)
    assert occ == "AAPL  261218C00200000"
    assert lb.streamer_symbol("AAPL", "2026-12-18", "C", 200) == ".AAPL261218C200"
    assert lb.streamer_symbol("SPY", "2026-12-18", "P", 650.5) == ".SPY261218P650.5"


@pytest.mark.parametrize("action,side,effect", [
    ("BTO", "buy", "open"), ("STO", "sell", "open"), ("BTC", "buy", "close"), ("STC", "sell", "close"),
    ("Buy to Open", "buy", "open"), ("Sell to Close", "sell", "close"), ("buy", "buy", "auto"),
])
def test_normalize_actions(action, side, effect):
    t = lb.normalize_trade({"date": "2026-01-05", "symbol": "AAPL", "side": action, "qty": 2, "price": 10})
    assert (t["side"], t["effect"]) == (side, effect)
    assert t["qty"] == 2 and t["asset"] == "stock" and t["multiplier"] == 1


def test_signed_quantity_and_option_fields():
    t = lb.normalize_trade({"date": "2026-01-05", "symbol": "NVDA", "asset": "option", "qty": -3, "price": 1.5,
                            "option_type": "put", "strike": "150", "expiration": "2026-03-20"})
    assert t["side"] == "sell" and t["qty"] == 3 and t["option_type"] == "P" and t["multiplier"] == 100
    with pytest.raises(ValueError):
        lb.normalize_trade({"date": "2026-01-05", "symbol": "NVDA", "asset": "option", "side": "buy", "qty": 1,
                            "price": 1, "strike": 150, "expiration": "2026-03-20"})   # missing C/P


def test_parse_csv_template():
    trades, errors = lb.parse_csv(lb.template_csv())
    assert not errors and len(trades) == 5
    assert {t["asset"] for t in trades} == {"stock", "option"}


def test_parse_csv_tastytrade_export():
    csv_text = (
        "Date,Type,Sub Type,Action,Symbol,Instrument Type,Description,Value,Quantity,Average Price,Commissions,Fees,"
        "Multiplier,Root Symbol,Underlying Symbol,Expiration Date,Strike Price,Call or Put,Order #,Currency\n"
        "2026-03-03T11:02:00-0500,Trade,Buy to Open,BUY_TO_OPEN,NVDA  260417C00180000,Equity Option,Bought 1 NVDA,"
        "-1250.00,1,-1250.00,-1.00,-0.10,100,NVDA,NVDA,4/17/26,180,CALL,1,USD\n"
        "2026-03-04T09:00:00-0500,Money Movement,Deposit,,,,Deposit,1000.00,0,0,0,0,,,,,,,,USD\n"
    )
    trades, errors = lb.parse_csv(csv_text)
    assert not errors and len(trades) == 1
    t = trades[0]
    assert (t["symbol"], t["asset"], t["side"], t["effect"], t["strike"], t["option_type"]) == \
        ("NVDA", "option", "buy", "open", 180.0, "C")
    assert t["price"] == pytest.approx(12.5) and t["fees"] == pytest.approx(1.1)


def test_parse_csv_reports_bad_rows():
    trades, errors = lb.parse_csv("date,symbol,side,qty,price\n2026-01-05,AAPL,buy,1,100\n2026-01-06,,buy,1,1\n")
    assert len(trades) == 1 and len(errors) == 1


# --------------------------------------------------------------------------- replay
def test_fifo_long_option_partial_close():
    opt = dict(asset="option", symbol="AAPL", option_type="C", strike=200, expiration="2099-12-18")
    lb.add_trades([T(side="buy", effect="open", qty=2, price=2.0, **opt),
                   T(date="2026-01-06 10:00", side="sell", effect="close", qty=1, price=3.0, **opt)])
    st = lb.replay()
    pos = st["positions"][lb.occ_symbol("AAPL", "2099-12-18", "C", 200)]
    assert pos["qty"] == 1 and pos["avg"] == pytest.approx(2.0)
    assert st["realized"] == pytest.approx(100.0)


def test_short_put_closed_for_profit():
    opt = dict(asset="option", symbol="MSFT", option_type="P", strike=500, expiration="2099-12-18")
    lb.add_trades([T(side="sell", effect="open", qty=1, price=9.8, **opt),
                   T(date="2026-01-07 10:00", side="buy", effect="close", qty=1, price=4.8, **opt)])
    st = lb.replay()
    assert st["positions"] == {}
    assert st["realized"] == pytest.approx(500.0)


def test_explicit_close_without_open_is_not_a_new_position():
    lb.add_trades([T(side="sell", effect="close", qty=10, price=100)])
    st = lb.replay()
    assert st["positions"] == {} and len(st["orphan_closes"]) == 1


def test_auto_effect_can_flip_through_zero():
    lb.add_trades([T(side="buy", qty=10, price=100), T(date="2026-01-06 10:00", side="sell", qty=15, price=110)])
    pos = lb.replay()["positions"]["AAPL"]
    assert pos["qty"] == -5


def test_expired_option_settles_at_intrinsic():
    opt = dict(asset="option", symbol="NVDA", option_type="C", strike=200, expiration="2025-09-19")
    lb.add_trades([T(date="2025-08-01 10:00", side="buy", effect="open", qty=1, price=8.0, **opt)])
    st = lb.replay()
    assert st["positions"] == {}
    assert st["realized"] == pytest.approx((5.0 - 8.0) * 100)
    exp = [t for t in st["transactions"] if t["transaction-sub-type"] == "Expiration"]
    assert len(exp) == 1 and exp[0]["transaction-type"] == "Receive Deliver"


def test_starting_cash_is_smallest_account_that_funds_history():
    lb.add_trades([T(side="buy", qty=10, price=100, fees=1), T(date="2026-01-06 10:00", side="sell", qty=10,
                                                                 price=120, fees=1)])
    st = lb.replay()
    assert st["starting_cash"] == pytest.approx(1001.0)
    assert st["cash"] == pytest.approx(1001.0 - 1001.0 + 1199.0)


def test_dedup_and_delete():
    t = T()
    assert lb.add_trades([t])["added"] == 1
    assert lb.add_trades([dict(t)])["duplicates"] == 1
    assert lb.delete_trade(t["id"]) and lb.load_trades() == []


# --------------------------------------------------------------------------- REST emulation
def test_rest_answers_have_tastytrade_shape():
    opt = dict(asset="option", symbol="SPY", option_type="C", strike=760, expiration="2099-12-18")
    lb.add_trades([T(side="buy", qty=20, price=90), T(side="buy", effect="open", qty=2, price=22.4, **opt)])
    acct = lb.handle_get("/customers/me/accounts")["data"]["items"][0]["account"]["account-number"]
    assert acct == lb.ACCOUNT
    items = lb.handle_get(f"/accounts/{acct}/positions")["data"]["items"]
    kinds = {i["instrument-type"] for i in items}
    assert kinds == {"Equity", "Equity Option"}
    opt_pos = next(i for i in items if i["instrument-type"] == "Equity Option")
    assert opt_pos["streamer-symbol"] == ".SPY991218C760" and opt_pos["quantity-direction"] == "Long"
    bal = lb.handle_get(f"/accounts/{acct}/balances")["data"]
    # cash + 20 shares × 100 + 2 contracts × mid 2.0 × 100
    assert float(bal["net-liquidating-value"]) == pytest.approx(float(bal["cash-balance"]) + 2000 + 400)
    tx = lb.handle_get(f"/accounts/{acct}/transactions", {"per-page": 1, "page-offset": 0, "sort": "Asc"})
    assert tx["pagination"]["total-items"] == 2 and len(tx["data"]["items"]) == 1
    assert lb.handle_get(f"/accounts/{acct}/orders/live")["data"]["items"] == []
    with pytest.raises(lb.LocalModeError):
        lb.handle_get("/api-quote-tokens")


# --------------------------------------------------------------------------- Cboe option data
from datetime import date as _date, timedelta as _td

_REAL_OPTION_QUOTE = lb.option_quote
_REAL_EQUITY_QUOTE = lb.equity_quote
_EXP = _date.today() + _td(days=30)
_YMD = _EXP.strftime("%y%m%d")


def _cboe_payload(root="AAPL"):
    def row(sym, bid, ask, last, prev, iv):
        return {"option": sym, "bid": bid, "ask": ask, "last_trade_price": last, "prev_day_close": prev, "iv": iv,
                "volume": 10, "open_interest": 100, "delta": 0.5, "gamma": 0.02, "theta": -0.1, "vega": 0.3}
    return {"data": {"current_price": 100.0, "iv30": 25.0, "options": [
        row(f"{root}{_YMD}C00100000", 5.0, 5.4, 5.2, 4.9, 0.3),
        row(f"{root}{_YMD}P00100000", 4.0, 4.3, 0.0, 4.1, 0.00001),
        row(f"{root}{_YMD}C00105000", 2.0, 2.2, 2.1, 1.9, 0.28),
        row(f"B1{root}{_YMD}C00100000", 9.0, 9.9, 9.5, 9.0, 0.3),        # adjusted root → ignored
    ]}}


@pytest.fixture()
def fake_cboe(monkeypatch):
    calls = []

    def fake(sym):
        calls.append(sym)
        if sym == "_SPX":
            p = _cboe_payload("SPXW")
            p["data"]["options"] += _cboe_payload("SPX")["data"]["options"][:1]
            return p
        if sym == "AAPL":
            return _cboe_payload("AAPL")
        raise OSError("403")
    monkeypatch.setattr(lb, "_cboe_json", fake)
    lb._cache.clear()
    return calls


def test_option_quote_prefers_cboe(fake_cboe):
    q = _REAL_OPTION_QUOTE(lb.occ_symbol("AAPL", _EXP.isoformat(), "C", 100))
    assert (q["bid"], q["ask"], q["last"], q["iv"], q["oi"]) == (5.0, 5.4, 5.2, 0.3, 100)
    put = _REAL_OPTION_QUOTE(lb.occ_symbol("AAPL", _EXP.isoformat(), "P", 100))
    assert put["iv"] == 0.0 and put["last"] == 4.1          # ~0 IV is unknown; no trade today → prior close
    assert fake_cboe == ["AAPL"]                              # one request serves the whole chain


def test_cboe_expirations_and_nested_chain(fake_cboe):
    assert lb.expirations("AAPL") == [_EXP.isoformat()]
    exps = lb._nested_chain("AAPL")["data"]["items"][0]["expirations"]
    assert [e["expiration-date"] for e in exps] == [_EXP.isoformat()]
    assert [s["strike-price"] for s in exps[0]["strikes"]] == ["100", "105"]
    assert exps[0]["strikes"][0]["call"] == lb.occ_symbol("AAPL", _EXP.isoformat(), "C", 100)


def test_cboe_index_roots(fake_cboe):
    q = _REAL_OPTION_QUOTE(lb.occ_symbol("SPX", _EXP.isoformat(), "C", 105))     # only listed as SPXW
    assert q["bid"] == 2.0
    q = _REAL_OPTION_QUOTE("SPXW" + lb.occ_symbol("SPX", _EXP.isoformat(), "C", 105)[6:])
    assert q["bid"] == 2.0 and fake_cboe == ["_SPX"]
    assert lb._cboe_roots("BRK.B") == ("BRKB",)


def test_cboe_iv30_feeds_market_metrics(fake_cboe, monkeypatch):
    class _T:
        def history(self, **kw):
            raise OSError("offline")
        calendar = None

    class _YF:
        def Ticker(self, s):
            return _T()
    monkeypatch.setattr(lb, "_yf", lambda: _YF())
    assert lb._metrics("AAPL")["implied-volatility-index"] == 0.25


def test_equity_quote_prefers_regular_market_previous_close(monkeypatch):
    class _T:     # Yahoo fast_info as seen live: previousClose was days out of date
        fast_info = {"lastPrice": 173.73, "previousClose": 158.78, "regularMarketPreviousClose": 168.89}

    class _YF:
        def Ticker(self, s):
            return _T()
    monkeypatch.setattr(lb, "_yf", lambda: _YF())
    q = _REAL_EQUITY_QUOTE("NVDA")
    assert (q["last"], q["prev_close"]) == (173.73, 168.89)


def test_yahoo_iv_cleaning():
    assert lb._clean_iv(0.00001) == 0.0 and lb._clean_iv(0.3) == 0.3 and lb._clean_iv(7.0) == 0.0


def test_cboe_failure_falls_back_to_yahoo(monkeypatch):
    lb._cache.clear()
    monkeypatch.setattr(lb, "_cboe_json", lambda sym: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(lb, "_yf", lambda: (_ for _ in ()).throw(OSError("down too")))
    q = _REAL_OPTION_QUOTE(lb.occ_symbol("AAPL", _EXP.isoformat(), "C", 100))
    assert q["bid"] == 0.0 and q["iv"] == 0.0


def test_cboe_greeks_reach_the_quote_item(fake_cboe, monkeypatch):
    monkeypatch.setattr(lb, "option_quote", _REAL_OPTION_QUOTE)
    item = lb._quote_item(lb.occ_symbol("AAPL", _EXP.isoformat(), "C", 100))
    assert (item["bid"], item["ask"], item["delta"], item["vega"]) == (5.0, 5.4, 0.5, 0.3)

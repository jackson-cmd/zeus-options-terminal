"""zeus_payoff: the Python port of the page's option math that MCP agents read.

Textbook cases always run; the cross-check runs the page's own JavaScript (analyzeStrategy
from optionstrat_analyzer.py, _extremes from paper_trade.py) under node on the same
strategies and requires the same numbers. Skipped when node is not installed."""
import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import zeus_payoff as pf
from test_analyzer_js import _extract

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
NOW = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc).timestamp() * 1000
EXP = "2026-04-02"


def leg(typ, strike, qty, entry, **kw):
    return {"type": typ, "strike": strike, "qty": qty, "entry": entry, "current": entry, "mid": entry,
            "prev_close": 0, "mult": 100, "exp": EXP, "iv": 0.3, **kw}


def strat(legs, **kw):
    return {"current_price": 100.0, "risk_free_rate": 0.045, "iv": 0.3, "expiration": EXP, "legs": legs, **kw}


# ------------------------------------------------------------------ textbook
def test_normal_and_black_scholes_basics():
    assert pf.norm_cdf(0) == pytest.approx(0.5)
    assert pf.norm_cdf(1.96) == pytest.approx(0.975, abs=1e-4)
    S, K, T, r, s = 100, 105, 0.25, 0.045, 0.37
    parity = pf.bsm_price(S, K, T, r, s, "C") - pf.bsm_price(S, K, T, r, s, "P")
    assert parity == pytest.approx(S - K * math.exp(-r * T), abs=1e-4)
    price = pf.bsm_price(S, K, T, r, s, "C")
    assert pf.implied_vol(S, K, T, r, price, "C") == pytest.approx(s, abs=1e-3)
    g = pf.bsm_greeks(S, K, T, r, s, "P")
    assert -1 < g["delta"] < 0 and g["gamma"] > 0 and g["theta"] < 0 and g["vega"] > 0


def test_stock_leg_is_a_call_at_zero():
    assert pf.bsm_price(123.0, 0, 0.5, 0.045, 0.3, "C") == pytest.approx(123.0)
    assert pf.bsm_greeks(123.0, 0, 0.5, 0.045, 0.3, "C")["delta"] == pytest.approx(1.0)


def test_new_york_close():
    summer = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc).timestamp() * 1000   # 16:00 EDT
    winter = datetime(2026, 12, 1, 21, 0, tzinfo=timezone.utc).timestamp() * 1000  # 16:00 EST
    assert pf.ny_close_ms("2026-07-01") == summer
    assert pf.ny_close_ms("2026-12-01") == winter
    assert math.isnan(pf.ny_close_ms("not a date"))


@pytest.mark.parametrize("legs, max_profit, max_loss", [
    ([leg("C", 100, 1, 6), leg("C", 110, -1, 2)], 600, -400),               # bull call spread
    ([leg("P", 95, -1, 2)], 200, -9300),                                     # short put: stock to zero
    ([leg("C", 100, 1, 3)], pf.UNLIMITED, -300),                             # long call
    ([leg("C", 100, -1, 3)], 300, -pf.UNLIMITED),                            # short call
    ([leg("P", 85, 1, 0.5), leg("P", 90, -1, 1.2), leg("C", 110, -1, 1.1), leg("C", 115, 1, 0.3)], 150, -350),
])
def test_max_profit_and_loss(legs, max_profit, max_loss):
    a = pf.analyze(strat(legs), now_ms=NOW)
    assert a["max_profit"] == pytest.approx(max_profit, abs=0.01)
    assert a["max_loss"] == pytest.approx(max_loss, abs=0.01)


def test_breakeven_cost_and_chance_of_profit():
    a = pf.analyze(strat([leg("C", 100, 1, 6), leg("C", 110, -1, 2)]), now_ms=NOW)
    assert a["net_debit"] == pytest.approx(400)
    assert a["breakevens"] == [pytest.approx(104.0, abs=0.01)]
    assert 20 < a["chance_of_profit"] < 60
    deep = pf.analyze(strat([leg("C", 60, 1, 20.0)]), now_ms=NOW)   # breakeven 80, stock at 100
    assert deep["chance_of_profit"] > 90


def test_pnl_today_and_open():
    s = strat([leg("C", 100, 2, 5.0, current=6.0, prev_close=5.5)])
    a = pf.analyze(s, now_ms=NOW)
    assert a["unrealized_pnl"] == pytest.approx(200)
    assert a["unrealized_pct"] == pytest.approx(20)
    assert a["today_pnl"] == pytest.approx(100)


def test_exact_extremes_with_stock_legs():
    covered = {"legs": [{"type": "S", "qty": 100, "entry": 100}, {"type": "C", "strike": 105, "qty": -1, "entry": 2}]}
    assert pf.expiry_extremes(covered) == (pytest.approx(700), pytest.approx(-9800))
    assert pf.expiry_extremes({"legs": [{"type": "S", "qty": -10, "entry": 50}]}) == (500, -math.inf)
    assert pf.expiry_extremes({"legs": [{"type": "S", "qty": 10, "entry": 50}]}) == (math.inf, -500)


# ------------------------------------------------------------------ same numbers as the page
JS_CASES = r"""
const D = n => { const d = new Date(); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
const e7 = D(7), e30 = D(30), e60 = D(60);
const leg = (type, strike, qty, entry, o) => Object.assign(
  {type, strike, qty, entry, current: entry, mid: entry, prev_close: 0, mult: 100, exp: e30, iv: 0.3}, o || {});
const strat = (legs, o) => Object.assign({current_price: 100, risk_free_rate: 0.045, iv: 0.3, expiration: e30, legs}, o || {});
const cases = {
  bull_call: strat([leg('C', 100, 1, 6, {current: 7.1, mid: 7.0, prev_close: 6.8}),
                    leg('C', 110, -1, 2, {current: 2.4, mid: 2.35, prev_close: 2.2})]),
  bear_put: strat([leg('P', 100, 1, 6), leg('P', 90, -1, 2)]),
  iron_condor: strat([leg('P', 85, 1, 0.5), leg('P', 90, -1, 1.2), leg('C', 110, -1, 1.1), leg('C', 115, 1, 0.3)]),
  short_put: strat([leg('P', 95, -2, 1.8, {current: 1.2, mid: 1.25})]),
  long_call: strat([leg('C', 100, 1, 3, {iv: 0.42})]),
  short_call: strat([leg('C', 100, -1, 3)]),
  call_backspread: strat([leg('C', 100, -1, 6), leg('C', 110, 2, 2)]),
  calendar: strat([leg('C', 100, -1, 3, {exp: e30, iv: 0.32}), leg('C', 100, 1, 5, {exp: e60, iv: 0.29})]),
  diagonal: strat([leg('P', 95, -1, 2, {exp: e7, iv: 0.35}), leg('P', 90, 1, 2.5, {exp: e60, iv: 0.31})],
                  {expiration: e7, current_price: 101.3}),
  covered_call: strat([{type: 'C', strike: 0, qty: 100, entry: 98, current: 100, mid: 100, prev_close: 99, mult: 1,
                        exp: e30, iv: 0}, leg('C', 105, -1, 1.5)]),
  far_otm: strat([leg('C', 8, 1, 0.5, {iv: 0.9}), leg('C', 10, -1, 0.2, {iv: 0.95})], {current_price: 5.67, iv: 0.9}),
  no_iv: strat([leg('C', 100, 1, 4, {iv: 0})], {iv: 0}),
};
const fin = v => v === Infinity ? 'inf' : (v === -Infinity ? '-inf' : v);
const out = {analysis: {}, extremes: {}};
for (const k in cases) {
  const a = analyzeStrategy(cases[k]);
  out.analysis[k] = {strat: cases[k], a: {netDebit: a.netDebit, maxProfit: a.maxProfit, maxLoss: a.maxLoss,
    breakevens: a.breakevens, cop: a.cop, unrealizedPnl: a.unrealizedPnl, unrealizedPct: a.unrealizedPct,
    todayPnl: a.todayPnl, greeks: a.greeks, iv: a.iv, dte: a.dte}};
}
const ov = analyzeStrategy(cases.bull_call, 0.45);
out.override = {maxProfit: ov.maxProfit, maxLoss: ov.maxLoss, breakevens: ov.breakevens, cop: ov.cop};
const pt = {
  covered_call: {legs: [{type: 'S', qty: 100, entry: 100, strike: 0}, {type: 'C', strike: 105, qty: -1, entry: 2, mult: 100}]},
  protective_put: {legs: [{type: 'S', qty: 100, entry: 100}, {type: 'P', strike: 95, qty: 1, entry: 1.5, mult: 100}]},
  collar: {legs: [{type: 'S', qty: 200, entry: 50}, {type: 'C', strike: 55, qty: -2, entry: 1, mult: 100},
                  {type: 'P', strike: 45, qty: 2, entry: 0.8, mult: 100}]},
  long_stock: {legs: [{type: 'S', qty: 50, entry: 20}]},
  short_stock: {legs: [{type: 'S', qty: -50, entry: 20}]},
  short_put: {legs: [{type: 'P', strike: 50, qty: -1, entry: 1.2, mult: 100}]},
  ratio: {legs: [{type: 'C', strike: 100, qty: 1, entry: 5, mult: 100}, {type: 'C', strike: 110, qty: -2, entry: 2, mult: 100}]},
};
for (const k in pt) { const e = _extremes(pt[k]); out.extremes[k] = {s: pt[k], max: fin(e.max), min: fin(e.min)}; }
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    analyzer = (ROOT / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")
    paper = (ROOT / "options_analyzer" / "paper_trade.py").read_text(encoding="utf-8")
    script = tmp_path_factory.mktemp("js") / "payoff_cases.js"
    script.write_text(_extract(analyzer, ["analyzeStrategy"]) + "\n" + _extract(paper, ["_extremes"]) + "\n"
                      + JS_CASES, encoding="utf-8")
    run = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


CASE_NAMES = ["bull_call", "bear_put", "iron_condor", "short_put", "long_call", "short_call", "call_backspread",
              "calendar", "diagonal", "covered_call", "far_otm", "no_iv"]


@pytest.mark.parametrize("name", CASE_NAMES)
def test_same_numbers_as_the_page(page, name):
    case = page["analysis"][name]
    js, py = case["a"], pf.analyze(case["strat"])
    assert py["net_debit"] == pytest.approx(js["netDebit"], abs=1e-9)
    assert py["unrealized_pnl"] == pytest.approx(js["unrealizedPnl"], abs=1e-9)
    assert py["unrealized_pct"] == pytest.approx(js["unrealizedPct"], abs=1e-9)
    assert py["today_pnl"] == pytest.approx(js["todayPnl"], abs=1e-9)
    assert py["max_profit"] == pytest.approx(js["maxProfit"], rel=1e-9, abs=1e-6)
    assert py["max_loss"] == pytest.approx(js["maxLoss"], rel=1e-9, abs=1e-6)
    assert len(py["breakevens"]) == len(js["breakevens"])
    for a, b in zip(py["breakevens"], js["breakevens"]):
        assert a == pytest.approx(b, abs=1e-6)
    assert py["chance_of_profit"] == pytest.approx(js["cop"], abs=0.05)
    assert py["dte"] == pytest.approx(js["dte"], abs=0.01)
    for k in ("delta", "gamma", "theta", "vega", "rho"):
        assert py["greeks"][k] == pytest.approx(js["greeks"][k], rel=1e-3, abs=1e-6), k


def test_iv_override_matches_the_page(page):
    js = page["override"]
    py = pf.analyze(page["analysis"]["bull_call"]["strat"], iv_override=0.45)
    assert py["max_profit"] == pytest.approx(js["maxProfit"], abs=1e-6)
    assert py["max_loss"] == pytest.approx(js["maxLoss"], abs=1e-6)
    assert py["breakevens"] == pytest.approx(js["breakevens"], abs=1e-6)
    assert py["chance_of_profit"] == pytest.approx(js["cop"], abs=0.05)


@pytest.mark.parametrize("name", ["covered_call", "protective_put", "collar", "long_stock", "short_stock",
                                  "short_put", "ratio"])
def test_paper_trade_extremes_match_the_page(page, name):
    e = page["extremes"][name]
    conv = {"inf": math.inf, "-inf": -math.inf}
    hi, lo = pf.expiry_extremes(e["s"])
    assert hi == pytest.approx(conv.get(e["max"], e["max"]))
    assert lo == pytest.approx(conv.get(e["min"], e["min"]))

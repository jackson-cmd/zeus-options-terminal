"""The numbers in the ? help cards come from the code that computes them.

tests/test_help.py checks that every card has both languages and points at an element that exists.
These tests read the risk view, risk radar, liquidity scan and TA cards and compare each number they
quote with the analyzer's JavaScript, checkup/risk_radar.py, checkup/liquidity.py and
ta_screener/engine.py, so a change to a threshold shows up here. They also check that the guide
knows the risk view, the radar and the liquidity scan as pages, and that a ? icon inside a column
header leaves its bilingual English label alone."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "options_analyzer"))   # checkup, as the server imports it
STATIC = ROOT / "options_analyzer" / "static"
HELP = STATIC / "zeus_help.js"
ZEUS_JS = (STATIC / "zeus.js").read_text(encoding="utf-8")
ANALYZER = (ROOT / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")
TA_PAGE = (ROOT / "ta_screener" / "page.py").read_text(encoding="utf-8")
LIQUIDITY = (ROOT / "options_analyzer" / "checkup" / "liquidity.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

LOADER = r"""
const fs = require('fs'), vm = require('vm');
const noop = function () {};
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const shown = new Set(input.shown || []);
const el = id => ({ id: id, getClientRects: () => (shown.has(id) ? [1] : []) });
const box = {
  document: { readyState: 'complete', body: null, addEventListener: noop,
              getElementById: id => el(id),
              querySelector: sel => (sel === '.top-nav-tab.active'
                ? { getAttribute: () => input.tab || 'positions' } : null),
              documentElement: { classList: { toggle: noop } } },
  localStorage: { getItem: () => null, setItem: noop }, navigator: { language: 'en' },
  location: { pathname: input.path || '/', search: '' }, setTimeout: setTimeout, addEventListener: noop,
  MutationObserver: function () { this.observe = noop; },
};
box.window = box;
vm.createContext(box);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), box);
process.stdout.write(JSON.stringify({ entries: box.zeusHelp.entries(), groups: box.zeusHelp.groups(),
                                      pages: box.zeusHelp.pages() }));
"""


def _load(**state):
    run = subprocess.run(["node", "-e", LOADER, str(HELP)], input=json.dumps(state), capture_output=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.fixture(scope="module")
def cards():
    if NODE is None:
        pytest.skip("node is not installed")
    return {e["id"]: e for e in _load()["entries"]}


def text(card, lang):
    return " ".join(card["zh" if lang == "zh" else "en"])


def g(x) -> str:
    return "%g" % x


# ------------------------------------------------------------------ risk view
def test_stress_card_matches_the_stress_test(cards):
    assert "const T_tmrw = Math.max(0, analysis.T - 1/365);" in ANALYZER          # tomorrow
    assert "const dailySigma = iv * Math.sqrt(1/365);" in ANALYZER
    assert "const Z_VAR = 1.645;" in ANALYZER and "const Z_MAX = 3.5;" in ANALYZER
    cvar = ANALYZER.split("function computeVarCvar(", 1)[1].split("\n}", 1)[0]
    assert "const N = 12;" in cvar
    for lang in ("zh", "en"):
        t = text(cards["risk.stress"], lang)
        assert "√(1/365)" in t and "1.645σ" in t and "3.5σ" in t and "12 " in t and "68%" in t


def test_totals_card_matches_how_the_totals_add_up(cards):
    totals = ANALYZER.split("// Compute totals (order-independent so use unsorted sum)", 1)[1].split("const nlvStr", 1)[0]
    assert "tot1s += r.worst1; tot2s += r.worst2;" in totals and "totVar += r.var; totCvar += r.cvar;" in totals
    assert "if (r.maxloss <= -1e9) nUnlimitedLoss++; else totMaxLoss += r.maxloss;" in totals
    assert "nUnlimitedLoss ? '&minus;∞' : fmtSign(totMaxLoss)" in ANALYZER
    assert "−∞" in text(cards["risk.total"], "zh") and "−∞" in text(cards["risk.total"], "en")


def test_upside_and_captured_cards_match_the_expectation(cards):
    ev = ANALYZER.split("function expectedProfitAtExpiry(", 1)[1].split("\n}", 1)[0]
    assert "const lo = S * 0.02, hi = S * 4, N = 600" in ev
    assert "const mu = Math.log(S) + (r - 0.5 * ivEff * ivEff) * T;" in ev           # risk-free drift
    assert "maxProfit - Math.max(1, maxProfit * 0.01)" in ev
    assert "const dflt = rg === 'bull' ? 70 : rg === 'chop' ? 50 : 65;" in ANALYZER
    for lang in ("zh", "en"):
        up, cap = text(cards["risk.upside"], lang), text(cards["risk.captured"], lang)
        assert "2%" in up and "600" in up and "70%" in up and "50%" in up and "65%" in up
        assert "1%" in cap and "$1" in cap and "50%" in cap


def test_greeks_card_matches_the_portfolio_greeks(cards):
    assert "const betaOf = u => (_pfBeta.betas[u] != null ? _pfBeta.betas[u] : 1.0);" in ANALYZER
    assert "bwdNotional += g.delta * s.current_price * b;" in ANALYZER
    assert "for (let m = -0.06; m <= 0.0601; m += 0.0025)" in ANALYZER
    for lang in ("zh", "en"):
        t = text(cards["risk.greeks"], lang)
        assert "1.0" in t and "−6%" in t and "+6%" in t and "0.25%" in t


def test_ladder_runway_and_monte_carlo_cards_match_the_code(cards):
    assert "const hot = dte <= 7;" in ANALYZER
    assert "for (let d = 0; d <= 14; d++)" in ANALYZER and ".slice(0, 8).forEach(({ s, a }) =>" in ANALYZER
    assert "portfolioPnlMC(8000)" in ANALYZER
    assert "rho = Math.max(-0.95, Math.min(0.95, rho));" in ANALYZER
    assert "(_ckRegime && _ckRegime.vix) ? _ckRegime.vix / 100 : 0.16" in ANALYZER
    for lang in ("zh", "en"):
        assert "7 " in text(cards["risk.ladder"], lang)
        assert "14" in text(cards["risk.runway"], lang) and "8 " in text(cards["risk.runway"], lang)
        mc = text(cards["risk.mc"], lang)
        assert "8,000" in mc and "±0.95" in mc and "16%" in mc and "P95" in mc


# ------------------------------------------------------------------ risk radar
def test_news_and_density_cards_match_the_radar(cards):
    from checkup import risk_radar as rr
    assert len(rr.LIFT) == 20
    for lang in ("zh", "en"):
        t = text(cards["radar.news"], lang)
        assert "20" in t and "250" in t
        assert g(rr._SCORE_DIVISOR) in t and g(rr._SHARE_ELEV * 100) + "%" in t and g(rr._SHARE_SAT * 100) + "%" in t
        for topic in ("yields_bonds", "pandemic_covid", "tariffs_trade"):
            assert g(rr.LIFT[topic]) in t, topic
    zh, en = text(cards["radar.news"], "zh"), text(cards["radar.news"], "en")
    assert "条数 ≥ %d" % rr._ABS_MIN in zh and "at least %d stories" % rr._ABS_MIN in en
    assert "P90（至少 %d）" % rr._P90_FLOOR in zh and "(%d at least)" % rr._P90_FLOOR in en
    assert "P95（至少 %d）" % rr._P95_FLOOR in zh and "(%d at least)" % rr._P95_FLOOR in en
    assert rr.LIFT["ai_bubble_valuation"] <= 1
    forward = Path(rr.__file__).read_text(encoding="utf-8").split("def forward_score(", 1)[1].split("\ndef ", 1)[0]
    assert "score = min(100, round(100 * raw / 6.0))" in forward
    assert "f.busiest_week_count >= 3" in ANALYZER
    for lang in ("zh", "en"):
        t = text(cards["radar.density"], lang)
        assert "÷ 6" in t and "3 " in t and "60" in t and "30" in t


def test_landmine_card_quotes_the_lifts_the_calendar_uses(cards):
    from checkup import risk_radar as rr
    ev = rr.annotate_landmines([{"type": "macro", "name": n, "date": "2026-11-20"}
                                for n in ("FOMC", "CPI", "Nonfarm payrolls", "GDP", "PMI")]
                               + [{"type": "earnings", "ticker": t, "date": "2026-11-20"}
                                  for t in ("NVDA", "AAPL", "CSCO", "XYZ")],
                               held_tickers={"XYZ"})
    lifts = {g(e["lift"]) for e in ev}
    assert lifts == {"1.78", "1.5", "1.43", "1.16", "1.1", "2.2", "1.8", "1.2"}
    for lang in ("zh", "en"):
        t = text(cards["radar.lift"], lang)
        assert all(v + "×" in t for v in lifts) and "55%" in t


# ------------------------------------------------------------------ liquidity scan
def test_liquidity_cards_match_the_verdict(cards):
    from checkup import liquidity as liq
    zh, en = text(cards["liq.verdict"], "zh"), text(cards["liq.verdict"], "en")
    assert "≥ %s%% 且 ≥ $%.2f" % (g(liq.VWIDE_PCT), liq.VWIDE_ABS) in zh
    assert "≥ %s%% 且 ≥ $%.2f" % (g(liq.WIDE_PCT), liq.WIDE_ABS) in zh
    assert "%s%% or more and at least $%.2f" % (g(liq.VWIDE_PCT), liq.VWIDE_ABS) in en
    assert "%s%% or more and at least $%.2f" % (g(liq.WIDE_PCT), liq.WIDE_ABS) in en
    for t in (zh, en):
        assert str(liq.THIN_OI) in t and "1–%d" % (liq.DEAD_VOL - 1) in t and "28–45" in t and "35" in t
    assert "oi < 10" in LIQUIDITY and "0 if 28 <= e[\"dte\"] <= 45 else 1, abs(e[\"dte\"] - 35)" in LIQUIDITY
    assert 'if c and c["tier"] != "bad" and c["spread_pct"] is not None]' in LIQUIDITY   # ★ per strike row
    assert "def build_scan(scan: dict, lo=WINDOW_LO, hi=WINDOW_HI, width_pct=0.10)" in LIQUIDITY
    assert "+10%" in text(cards["liq.crossweek"], "zh") and "+10%" in text(cards["liq.crossweek"], "en")


# ------------------------------------------------------------------ TA scanner
def test_ta_key_levels_and_earnings_cards_match_the_engine(cards):
    from ta_screener import engine
    p = engine.PARAMS
    for lang in ("zh", "en"):
        t = text(cards["ta.keylevels"], lang)
        assert g(p["sr_min_n"]) in t and g(p["sr_cooldown"]) in t and g(p["ssp_min_rate"]) + "%" in t
        assert g(p["lift_min"]) in t and g(p["ssp_bull_thr"] * 100) + "%" in t and g(p["ssp_bear_thr"] * 100) + "%" in t
        assert "52" in t
    assert "hIcon('keylevels')" in TA_PAGE
    assert "e.days <= 7 ? 'var(--red)' : (e.days <= 14 ?" in TA_PAGE
    for lang in ("zh", "en"):
        t = text(cards["ta.c.earn"], lang)
        assert "7" in t and "14" in t


# ------------------------------------------------------------------ the guide's pages
@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("shown, page", [
    ([], "positions"), (["riskView"], "positions:risk"), (["riskRadarOverlay"], "events"),
    (["liquidityOverlay"], "liquidity"), (["riskView", "liquidityOverlay"], "liquidity")])
def test_the_guide_knows_the_risk_view_radar_and_liquidity(shown, page):
    reg = _load(shown=shown)
    assert reg["pages"] == [page, "top"]
    assert any(page in e["pages"] for e in reg["entries"])


# ------------------------------------------------------------------ bilingual labels
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_help_icon_inside_a_header_leaves_its_english_alone():
    lookup = ZEUS_JS.split("  // ------------------------------------------------------------------ dictionary (zh → en)", 1)[1]
    lookup = lookup.split("  // ---- English: translate every interface string in place", 1)[0]
    bi = "function biLabel(el)" + ZEUS_JS.split("function biLabel(el)", 1)[1].split("\n  function biTitle", 1)[0]
    script = """
const window = {};
%s
%s
DICT = { '信号胜率': 'Signal win rate', '评分': 'Score', '距下撑': 'Distance to support' };
const mk = (tag, attrs) => ({ nodeType: 1, tagName: tag, attrs: attrs || {}, parentNode: null, isContentEditable: false,
  hasAttribute(n) { return n in this.attrs; }, getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
  setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute(n) { delete this.attrs[n]; },
  classList: { contains() { return false; } } });
const out = {};
for (const label of ['信号胜率(SSP/MACD/RSI/Vol)', '评分', '距下撑 x3']) {
  const plain = mk('TH'); plain.textContent = label; plain.children = []; plain.querySelector = () => null;
  biLabel(plain);
  // the help icon is an empty span with data-no-i18n (its "?" is drawn by CSS)
  const th = mk('TH'), icon = mk('SPAN', { 'data-no-i18n': '' });
  icon.parentNode = th; th.textContent = label; th.children = [icon];
  th.querySelector = sel => (sel === '[data-no-i18n]' ? icon : null);
  globalThis.NodeFilter = { SHOW_TEXT: 4 };
  globalThis.document = { createTreeWalker: () => { const l = [{ nodeType: 3, nodeValue: label, parentNode: th }]; let i = 0;
    return { nextNode: () => l[i++] || null }; } };
  biLabel(th);
  out[label] = [plain.getAttribute('data-zs-en'), th.getAttribute('data-zs-en')];
}
process.stdout.write(JSON.stringify(out));
""" % (lookup, bi)
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "label.js"
        js.write_text(script, encoding="utf-8")
        run = subprocess.run(["node", str(js)], capture_output=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    for label, (plain, with_icon) in json.loads(run.stdout).items():
        assert plain and with_icon == plain, (label, plain, with_icon)

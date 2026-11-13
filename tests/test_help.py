"""The ? help (options_analyzer/static/zeus_help.js): every card has Chinese and English text, the
English has no Chinese in it, every heading exists, the elements the cards point at exist in the
pages, the TA scanner's own ? icons open these cards, and zeus.js loads the file."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "zeus"   # the code
HELP = ROOT / "options_analyzer" / "static" / "zeus_help.js"
ZEUS_JS = (ROOT / "options_analyzer" / "static" / "zeus.js").read_text(encoding="utf-8")
ZEUS_CSS = (ROOT / "options_analyzer" / "static" / "zeus.css").read_text(encoding="utf-8")
TA_PAGE = (ROOT / "ta_screener" / "page.py").read_text(encoding="utf-8")
SERVER = (ROOT / "options_analyzer" / "optionstrat_server.py").read_text(encoding="utf-8")
# the page sources the cards point into
PAGES = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in (
    "options_analyzer/optionstrat_server.py", "options_analyzer/optionstrat_analyzer.py",
    "options_analyzer/paper_trade.py", "options_analyzer/pretrade_checkup_page.py",
    "options_analyzer/journal_review_page.py", "heatmap/heatmap.py", "ta_screener/page.py",
    "trade_journal/dashboard.html", "options_analyzer/static/zeus.js", "options_analyzer/static/zeus_help.js",
    "options_analyzer/static/ta_filters.js"))
CJK = re.compile(r"[㐀-鿿]")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

LOADER = r"""
const fs = require('fs'), vm = require('vm');
const noop = function () {};
const box = {
  document: { readyState: 'complete', body: null, addEventListener: noop, getElementById: () => null,
              querySelector: () => null, documentElement: { classList: { toggle: noop } } },
  localStorage: { getItem: () => null, setItem: noop }, navigator: { language: 'en' },
  location: { pathname: '/', search: '' }, setTimeout: setTimeout, addEventListener: noop,
  MutationObserver: function () { this.observe = noop; },
};
box.window = box;
vm.createContext(box);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), box);
process.stdout.write(JSON.stringify({ entries: box.zeusHelp.entries(), groups: box.zeusHelp.groups() }));
"""


@pytest.fixture(scope="module")
def registry():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    run = subprocess.run(["node", "-e", LOADER, str(HELP)], capture_output=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@needs_node
def test_help_file_parses():
    run = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(HELP)],
                         capture_output=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr


def test_every_card_has_both_languages(registry):
    entries = registry["entries"]
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "duplicate ids"
    assert len(entries) >= 120
    for e in entries:
        assert len(e["zh"]) == 4 and len(e["en"]) == 4, e["id"]
        assert e["zh"][0] and e["zh"][1] and e["en"][0] and e["en"][1], e["id"]
        for i in (2, 3):     # how it is calculated / how to use it: both languages or neither
            assert bool(e["zh"][i]) == bool(e["en"][i]), (e["id"], i)


def test_english_text_has_no_chinese(registry):
    for e in registry["entries"]:
        for part in e["en"]:
            # the Digest's "EN + 中文" button is named as it is labelled
            assert not CJK.search(part.replace("EN + 中文", "")), (e["id"], part[:80])


def test_html_in_cards_is_balanced(registry):
    for e in registry["entries"]:
        for part in e["zh"] + e["en"]:
            for tag in ("ul", "li", "b"):
                assert part.count("<" + tag + ">") == part.count("</" + tag + ">"), (e["id"], tag)


def test_every_card_sits_under_a_heading(registry):
    groups = registry["groups"]
    for e in registry["entries"]:
        for page in e["pages"]:
            assert page + "/" + e["group"] in groups or "*/" + e["group"] in groups, (e["id"], page, e["group"])


def test_cards_point_at_elements_that_exist(registry):
    for e in registry["entries"]:
        sel = e["at"].split("|")[0]
        for ident in re.findall(r"#([A-Za-z][\w-]*)", sel):
            assert re.search(r"""id=["']%s["']|\.id = ['"]%s['"]""" % (ident, ident), PAGES), (e["id"], ident)
        for attr, val in re.findall(r"\[(data-[\w-]+)=\"([^\"]*)\"\]", sel):
            assert '%s="%s"' % (attr, val) in PAGES, (e["id"], attr, val)
        for fn in re.findall(r"onclick=\"([\w.]+)\(", sel):
            assert fn + "(" in PAGES, (e["id"], fn)
        if e["q"]:
            assert sel, e["id"]


def test_ta_scanner_icons_open_these_cards(registry):
    ids = {e["id"] for e in registry["entries"]}
    keys = set(re.findall(r"hIcon\('(\w+)'\)", TA_PAGE))
    assert keys >= {"srzones", "dip", "opt", "rec", "finviz"}
    for k in keys:
        assert "ta." + k in ids, k
    # the score had no ? on the scanner page before; now its column header carries one
    score = next(e for e in registry["entries"] if e["id"] == "ta.score")
    assert score["q"] and "headSort('score')" in score["at"] and "#hmGraphSort" in score["at"]
    shown = TA_PAGE.split("function showHelp(k) {", 1)[1].split("\n}", 1)[0]
    assert "window.zeusHelp.open('ta.' + k)" in shown
    assert "const HELP = {" not in TA_PAGE and "helpModal" not in TA_PAGE


def test_ta_score_card_matches_the_engine(registry):
    from ta_screener import engine
    p = engine.PARAMS
    score = next(e for e in registry["entries"] if e["id"] == "ta.score")
    zh = score["zh"][2]
    assert p["sr_cap"] == 40 and p["sr_base"] == 16 and p["sr_conf3"] == 10 and p["sr_conf2"] == 5
    assert p["sr_pivot_each"] == 3 and p["sr_pivot_cap"] == 9 and p["sr_hist_bonus"] == 5
    assert p["emax_cap"] == 20 and p["emax_base"] == 12 and p["emax_qual_bonus"] == 8
    assert (p["score_trend_long"], p["score_trend_med"], p["score_trend_short"]) == (12, 8, 5)
    assert p["macd_pts"] == 15 and p["ssp_min_rate"] == 60 and p["lift_min"] == 10
    assert p["sr_min_n"] == 12 and p["ssp_bull_min_n"] == 20
    for piece in ("最多 40", "得 16", "+10", "+5", "+3", "最多 +9", "最多 20", "得 12", "+8", "最多 25",
                  "+12", "MACD（15）", "60%", "10 个百分点", "支撑 12 次", "均线信号 20 次"):
        assert piece in zh, piece
    # the bear side can't switch on: market_regime never returns 'bear'
    from ta_screener import spreads
    regimes = {spreads.market_regime([100.0 + i * d for i in range(30)])["regime"] for d in (-2, -0.05, 0, 0.05, 2)}
    assert "bear" not in regimes
    assert "allow_bear = (mkt.get(\"regime\") == \"bear\")" in (ROOT / "ta_screener" / "scanner.py").read_text(encoding="utf-8")


def test_zeus_js_loads_the_help_and_styles_exist():
    boot = ZEUS_JS.split("function boot() {", 1)[1].split("\n  }", 1)[0]
    assert "loadHelp();" in boot
    assert "s.src = '/static/zeus_help.js'" in ZEUS_JS
    for rule in (".zs-q {", ".zs-q::before", "#zsHelpOverlay {", "#zsHelpFab {", "html.zs-noq .zs-q", ".zs-help-flash"):
        assert rule in ZEUS_CSS, rule


def test_help_button_leaves_room_for_the_tabs():
    wide = SERVER.split("@media (max-width: 1240px) {", 1)[1].split("\n}", 1)[0]
    narrow = SERVER.split("@media (max-width: 1100px) {", 1)[1].split("\n}", 1)[0]
    assert "html.zs-lang-bi .top-nav-tab { padding: 0 6px; }" in wide
    assert "html.zs-lang-bi .top-nav-tab { padding: 0 6px; }" in narrow

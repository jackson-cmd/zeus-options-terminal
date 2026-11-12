"""Interface layout and the fixes from the UI pass: one-line top bar, heatmap switch in the
picker, K-line wall redraw, Barchart button, earnings calendar, pop-ups, static caching."""
import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from heatmap import earnings as earnings_store
from trade_journal import calendar_events as ce

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "options_analyzer" / "optionstrat_server.py").read_text(encoding="utf-8")
ANALYZER = (ROOT / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")
HEATMAP = (ROOT / "heatmap" / "heatmap.py").read_text(encoding="utf-8")
ZEUS_JS = (ROOT / "options_analyzer" / "static" / "zeus.js").read_text(encoding="utf-8")
EN = json.loads((ROOT / "options_analyzer" / "static" / "i18n_en.json").read_text(encoding="utf-8"))


def _css_rule(src: str, selector: str) -> str:
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", src)
    assert m, selector
    return m.group(1)


# ------------------------------------------------------------------ top bar
def test_every_top_tab_sits_in_one_scrolling_strip():
    nav = SERVER.split('<div class="top-nav" id="topNav">', 1)[1].split('<div class="top-nav-right">', 1)[0]
    strip = nav.split('<div class="top-nav-tabs" id="topNavTabs">', 1)
    assert len(strip) == 2, "the tabs need their own strip"
    assert 'class="top-nav-tab' not in strip[0]
    assert strip[1].count('class="top-nav-tab') == 7
    assert strip[1].rstrip().endswith("</div>")


def test_top_bar_items_never_wrap():
    tab = _css_rule(SERVER, ".top-nav-tab")
    assert "white-space: nowrap" in tab and "flex-shrink: 0" in tab
    tabs = _css_rule(SERVER, ".top-nav-tabs")
    assert "overflow-x: auto" in tabs and "min-width: 0" in tabs
    assert "white-space: nowrap" in _css_rule(SERVER, ".top-nav-brand")
    assert "white-space: nowrap" in _css_rule(SERVER, ".top-nav-right > *")
    assert "initTabStrip" in ZEUS_JS and "topNavTabs" in ZEUS_JS


def test_toolbar_buttons_stay_on_one_line():
    assert "white-space: nowrap" in _css_rule(ANALYZER, ".dash-actions .back-btn")


def test_body_font_falls_back_to_sans_serif():
    body = _css_rule(ANALYZER, "body")
    stack = re.search(r"font-family:([^;]*);", body).group(1)
    assert stack.strip().endswith("sans-serif") and "monospace" not in stack
    assert "PingFang SC" in stack and "Microsoft YaHei" in stack


def test_page_scripts_are_revalidated_after_an_update():
    assert "async def _zeus_static_revalidate" in SERVER
    assert 'resp.headers["Cache-Control"] = "no-cache"' in SERVER


# ------------------------------------------------------------------ picker
def test_picker_switches_between_heatmap_and_kline_wall():
    nav = HEATMAP.split('<div id="hmSubNav"', 1)[1].split("</div>", 1)[0]
    names = re.findall(r'data-name="(\w+)" onclick="hmSubView\(\'(\w+)\'\)"', nav)
    assert ("kline", "kline") in names and ("heatmap", "heatmap") in names and ("brief", "brief") in names
    sub = HEATMAP.split("window.hmSubView = function", 1)[1].split("\n  };", 1)[0]
    assert "window.hmSetView(name === 'heatmap' ? 'treemap' : 'graph')" in sub
    assert "'hmCanvas'" not in sub.split("if (isKline)", 1)[0], "the heatmap canvas must not be hidden for good"
    assert "'🎯 选股 · 热力图'" in HEATMAP and EN["🎯 选股 · 热力图"] == "🎯 Picker · Heatmap"
    assert EN["热力图"] == "Heatmap"


def test_kline_wall_redraws_once_quotes_arrive():
    assert "window.hmState.graphDrawnWithData = " in HEATMAP
    catch_up = HEATMAP.split("function _hmGraphCatchUp()", 1)[1].split("\n  }", 1)[0]
    assert "if (!window.hmState.graphDrawnWithData) hmRenderGraph();" in catch_up
    fetch = HEATMAP.split("async function fetchAndRender", 1)[1].split("\n  }", 1)[0]
    assert "_hmGraphCatchUp();" in fetch


def test_barchart_button_needs_a_login_and_its_menu_stays_on_screen():
    assert "function barchartButton()" in ZEUS_JS and "STATUS.barchart ? 'inline-flex' : 'none'" in ZEUS_JS
    toggle = HEATMAP.split("window.hmBcTogglePanel = function", 1)[1].split("\n  };", 1)[0]
    assert "window.innerWidth" in toggle and "p.style.right = '0'" in toggle
    assert "Barchart tab" not in HEATMAP
    assert "barchart" in __import__("zeus_config").status()


def test_garbled_labels_have_whole_sentence_translations():
    legend = "⭐=强 S/R · 绿线支撑/红线阻力 · 橙EMA50 蓝EMA200 · 点AI徽章看理由"
    assert legend in HEATMAP and "orange = EMA50" in EN[legend]
    review = (ROOT / "options_analyzer" / "journal_review_page.py").read_text(encoding="utf-8")
    assert "📉 昨日平的" in review and EN["📉 昨日平的"] == "📉 Closed yesterday"
    checkup = (ROOT / "options_analyzer" / "pretrade_checkup_page.py").read_text(encoding="utf-8")
    sub = re.search(r'<div class="subtitle">([^<]*)</div>', checkup).group(1)
    assert sub in EN and "Don't open" in EN[sub]


# ------------------------------------------------------------------ pop-ups and translation queue
def test_escape_and_tab_switch_close_popups():
    assert "window.appBack()" in ZEUS_JS and "closeOverlays();" in ZEUS_JS
    for overlay, closer in (("riskRadarOverlay", "closeRiskRadar"), ("checkupOverlay", "closeCheckup")):
        assert f"{overlay}: '{closer}'" in ZEUS_JS
        assert f'id="{overlay}"' in ANALYZER and re.search(rf"^function {closer}\(", ANALYZER, re.M)
    assert re.search(r"^function appBack\(\)", ANALYZER, re.M)


def test_translation_queue_has_a_timer_when_frames_stop():
    queue = ZEUS_JS.split("function queue(n)", 1)[1].split("\n  }", 1)[0]
    assert "setTimeout(flush, 50)" in queue and "requestAnimationFrame" in queue


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_zeus_js_parses():
    js = ROOT / "options_analyzer" / "static" / "zeus.js"
    run = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(js)],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


# ------------------------------------------------------------------ events calendar
def test_watchlist_one_per_line_or_comma_separated(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.txt"
    monkeypatch.setattr(ce, "WATCHLIST_PATH", path)
    path.write_text("AAPL\nMSFT\nSPY\nNASDAQ:NVDA\n", encoding="utf-8")   # what ZEUS seeds
    assert ce.load_watchlist() == ["AAPL", "MSFT", "NVDA"]                  # ETFs have no earnings
    path.write_text("NASDAQ:AAPL,NASDAQ:MSFT, NYSE:JPM,NASDAQ:AAPL", encoding="utf-8")   # the original format
    assert ce.load_watchlist() == ["AAPL", "MSFT", "JPM"]


def test_earnings_fall_back_to_the_pickers_dates(monkeypatch):
    monkeypatch.setattr(ce, "_fetch_nasdaq_day", lambda day: [])   # Nasdaq answered 403
    start, end = ce._earnings_window()
    inside = (start + dt.timedelta(days=6)).strftime("%Y-%m-%d")
    after = (end + dt.timedelta(days=6)).strftime("%Y-%m-%d")
    monkeypatch.setattr(earnings_store, "load", lambda: {
        "AAPL": {"date": inside, "time": "pm", "src": "tastytrade"},
        "MSFT": {"date": after, "time": "am"},
        "XOM": {"last_date": "2026-01-30"},        # only an old report: no confirmed date
    })
    rows = ce.fetch_earnings(["AAPL", "MSFT", "XOM"])
    assert rows == [{"ticker": "AAPL", "date": inside, "session": "AMC",
                     "eps_forecast": None, "name": None, "fiscal_quarter": None}]


def test_calendar_months_reflow_with_the_window():
    assert "grid-template-columns:repeat(auto-fit, minmax(min(100%, 560px), 1fr))" in _css_rule(SERVER, ".ev-cal-months")
    assert '<div class="ev-cal-months">' in SERVER
    assert ">⚡ ${_truncEvent(e.title)} <span" in SERVER   # the event name before its time


# ------------------------------------------------------------------ mode in Settings, market brief, rotation chart
SETTINGS = (ROOT / "options_analyzer" / "static" / "settings.html").read_text(encoding="utf-8")
ZEUS_CSS = (ROOT / "options_analyzer" / "static" / "zeus.css").read_text(encoding="utf-8")


def test_mode_moved_from_the_top_bar_to_settings():
    assert "zsModeBadge" not in ZEUS_JS and "zs-badge" not in ZEUS_CSS
    mode = ZEUS_JS.split("function renderMode()", 1)[1].split("\n  }", 1)[0]
    assert "g.title = " in mode and "STATUS.manual" in mode
    assert "renderMode();" in ZEUS_JS
    cards = SETTINGS.split("function renderCards()", 1)[1].split("\nfunction ", 1)[0]
    assert 'id="cardTrades"' in cards and "/api/zeus/status" in cards and "t('s_trades')" in cards


def test_tabs_tighten_before_they_would_scroll():
    assert re.search(r"@media \(max-width: 1240px\) \{\s*\.top-nav-tab \{ padding: 0 9px; \}", SERVER)
    narrow = SERVER.split("@media (max-width: 1100px) {", 1)[1].split("\n}", 1)[0]
    assert "#trStatusCur { display: none; }" in narrow and ".top-nav-tab { padding: 0 8px; }" in narrow


def test_market_brief_header_and_subtabs_move_into_the_picker():
    header = SERVER.split('<div id="mbHeader"', 1)[1].split('<div id="mbSubNav"', 1)[0]
    assert 'id="mbRunBtn" onclick="mbRun(true)"' in header
    reorg = SERVER.split("function reorgTabs()", 1)[1].split("\n}", 1)[0]
    moved = re.search(r"\[([^\]]*)\]\.forEach\(id =>", reorg).group(1)
    assert [m.strip(" '") for m in moved.split(",")] == ["mbHeader", "mbSubNav", "mbBody", "mbRotPane", "mbOptPane"]
    assert "点右上角「🔄 重新分析」生成" in SERVER   # the empty brief points at that button


def test_rotation_chart_labels_and_scale_stay_readable():
    chart = SERVER.split("function igDrawChart(rows, k)", 1)[1].split("\n}\n", 1)[0]
    assert "Math.ceil(13 / bw)" in chart and "i - lastLab >= stride" in chart
    assert 'class="ig-lab"' in chart and "mbEsc(g.name) + '</text>'" in chart   # full name, fitted afterwards
    assert "fitLabels(false);" in chart and "new MutationObserver" in chart
    assert "bars + labels + gridText" in chart
    assert "gridText += '<text x=\"' + (padL - 5)" in chart


def test_movers_toast_lets_clicks_through():
    toast = HEATMAP.split("function _hmPopupToast(slot)", 1)[1].split("\n  }", 1)[0]
    assert "pointer-events:none" in toast

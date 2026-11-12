"""English interface text reads as English, not word salad.

zeus.js translates Chinese interface text with static/i18n_en.json one whole sentence at a time.
A sentence a page builds with numbers in it cannot be a dictionary key, so it carries its English
at the source: zt(zh, en) / ztt(zh, en) from static/zeus_lang.js in the page scripts, and *_en
fields in the data the server sends. These tests keep the page sources, the dictionary keys and
the English templates in step, and run zeus.js's own lookup code under node."""
import html
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
EN_RAW = (STATIC / "i18n_en.json").read_text(encoding="utf-8")
EN = json.loads(EN_RAW)
ZEUS_JS = (STATIC / "zeus.js").read_text(encoding="utf-8")
SERVER = (ROOT / "options_analyzer" / "optionstrat_server.py").read_text(encoding="utf-8")
ANALYZER = (ROOT / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")
CJK = re.compile(r"[一-鿿]")
NODE = shutil.which("node")

# every page script that is served with zeus_lang.js in its <head>
PAGE_SOURCES = [
    "options_analyzer/optionstrat_analyzer.py", "options_analyzer/optionstrat_server.py",
    "heatmap/heatmap.py", "options_analyzer/journal_review_page.py", "ta_screener/page.py",
    "trade_journal/dashboard.html",
]
# where the interface text comes from: page templates, server-built text, research data
TEXT_SOURCES = [p for p in ROOT.rglob("*") if p.suffix in (".py", ".js", ".json", ".html")
                and "tests" not in p.parts and p.name != "i18n_en.json" and not p.is_symlink()
                and not any(part.startswith(".") or part in ("node_modules", "__pycache__", "data")
                            for part in p.relative_to(ROOT).parts)]


def _flat(text: str) -> str:
    """Text as a page shows it: entities decoded, line breaks and indents collapsed."""
    return re.sub(r"\s+", " ", html.unescape(text))


_ALL = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in TEXT_SOURCES)
# all page text in one string; once more with the tags taken out, so that a label split by
# <b> / <span> reads as one line
SOURCE_TEXT = _flat(_ALL) + "\n" + _flat(re.sub(r"<[^<>]*>", "", _ALL))


# ------------------------------------------------------------------ the dictionary
def test_dictionary_is_one_line_of_compact_json():
    assert json.dumps(EN, ensure_ascii=False, separators=(",", ":")) == EN_RAW


def test_english_values_have_no_chinese():
    assert not [k for k, v in EN.items() if CJK.search(v)]


# Whole-sentence keys added for the garbled pop-ups and pages, with the file that shows each one.
# Rewording the Chinese in the source without the key leaves the page translated word by word again.
WHOLE_SENTENCES = [
    ("options_analyzer/checkup/risk_radar.py", "芯片龙头财报=板块级 landmine:坏财报当天 SOXX≤-1% 命中 55%(SOXX均-1.6%)"),
    ("options_analyzer/checkup/risk_radar.py", "非农日:SPY 跌≥1% 概率 ~1.3-1.4×(最显著的计划内日)"),
    ("options_analyzer/checkup/risk_radar_reference.json", "④ 暴跌隔夜砸(≤-5% 日均开盘 gap -5.5%)— 止损无效,只有事前现金能防"),
    ("options_analyzer/optionstrat_analyzer.py", "今日无明显风险话题在烧 · 新闻面平静"),
    ("options_analyzer/optionstrat_analyzer.py", "雷达 = 高危窗口提示(lift 1.2–3.3)，非预测哪天跌 · 来自 research/drop_events 实证"),
    ("options_analyzer/optionstrat_analyzer.py", "🟢 好成交 · 🟡 偏(宽/OI薄/低量) · 🔴 差(单边/极宽,mid难成交) · ★ 该行最佳成交周 · 点格子看真实 bid/ask 区间"),
    ("options_analyzer/optionstrat_analyzer.py", "→ 提前到软档就强制。100% 死等每段都最差,借方价差不止损。仅交易日。"),
    ("options_analyzer/optionstrat_server.py", "IV 百分位 = 坏消息已经被定价了多少"),
    ("options_analyzer/optionstrat_server.py", "只看清单内 ticker 的新闻(= heatmap 手维护 ~160 票 universe, 默认开; 今天/日历/历史 同一 toggle)"),
    ("options_analyzer/journal_review_page.py", "数据: journal_review/ · 只读本地日记/缓存,不发起新的实时行情请求"),
    ("heatmap/heatmap.py", "高波动 · 跟盘紧 (高β放大器 ⚠)"),
    ("ta_screener/scanner.py", "还没扫描过 — 点「跑扫描」"),
    ("ta_screener/page.py", "▶ 跑第一次扫描(约 2-4 分钟)"),
]


@pytest.mark.parametrize("path, zh", WHOLE_SENTENCES)
def test_whole_sentences_match_the_source(path, zh):
    assert zh in _flat((ROOT / path).read_text(encoding="utf-8")), f"{zh!r} is no longer in {path}"
    assert zh in EN and not CJK.search(EN[zh])


def test_sentence_keys_still_exist_in_the_source():
    """A key of a whole sentence that no page shows any more is dead weight: reworded Chinese
    needs its key renamed too. Short keys are words, reused anywhere, so they are not checked."""
    sentences = [k for k in EN if len(k) >= 18 and re.search(r"[，,。;；:：、(（]", k)]
    stale = {k for k in sentences if _flat(k) not in SOURCE_TEXT}
    assert stale <= OLDER_KEYS_NOT_IN_THE_SOURCE, sorted(stale - OLDER_KEYS_NOT_IN_THE_SOURCE)


# older keys for AI prompts, AI-written text and labels pieced together in code
OLDER_KEYS_NOT_IN_THE_SOURCE = {
    "把下面每行编号的英文财经新闻翻成简体中文。严格保持相同编号和行数，每行格式『编号",
    "把下面的英文财经新闻逐句直译成简体中文。保留所有股票代码、公司名、人名、数字、百分比不变。只输出译文，不要总结、不要解释、不要加任何前后缀或评论。",
    "最大利润就平、不止损、再开下一单】（资金年化",
    "百分比不变。只输出译文行，不要解释：",
    "这才是你真正的漏洞：止盈时机没问题，但亏损单经常放任浮亏越过净权利金的一半。给亏损单挂硬止损（尤其借方价差），是当前最高杠杆的改进。",
    "板块SPDR-ETF(GICS行业)",
}


# ------------------------------------------------------------------ English templates at the source
def _call_args(src: str, start: int):
    """Top-level arguments of the call whose '(' is just before `start`, as source text."""
    args, begin, stack, k = [], start, [")"], start
    while k < len(src):
        c, top = src[k], stack[-1]
        if top in ("'", '"'):
            if c == "\\":
                k += 2
                continue
            if c == top:
                stack.pop()
        elif top == "`":
            if c == "\\":
                k += 2
                continue
            if c == "`":
                stack.pop()
            elif src.startswith("${", k):
                stack.append("}")
                k += 2
                continue
        elif c in "'\"`":
            stack.append(c)
        elif c in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[c])
        elif c in ")]}":
            assert c == top, f"unbalanced {c!r} at {src[start:k + 1][-120:]!r}"
            stack.pop()
            if not stack:
                args.append(src[begin:k])
                return args
        elif c == "," and len(stack) == 1:
            args.append(src[begin:k])
            begin = k + 1
        k += 1
    raise AssertionError("unterminated call")


def _literal_text(arg: str) -> str:
    """The text inside the string and template literals of an argument, without ${...} parts."""
    out, k, quote, depth = [], 0, None, 0
    while k < len(arg):
        c = arg[k]
        if quote is None:
            if c in "'\"`":
                quote = c
        elif quote == "`" and arg.startswith("${", k):
            depth, k = 1, k + 2
            while k < len(arg) and depth:
                depth += {"{": 1, "}": -1}.get(arg[k], 0)
                k += 1
            continue
        elif c == "\\":
            k += 2
            continue
        elif c == quote:
            quote = None
        else:
            out.append(c)
        k += 1
    return "".join(out)


def _template_calls():
    calls = []
    for rel in PAGE_SOURCES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"(?<![\w.$])ztt?\(", src):
            calls.append((rel, src.count("\n", 0, m.start()) + 1, _call_args(src, m.end())))
    return calls


def test_english_templates_are_english():
    calls = _template_calls()
    assert len(calls) > 80
    for rel, line, args in calls:
        assert len(args) == 2, f"{rel}:{line} zt takes (zh, en)"
        zh, en = (_literal_text(a) for a in args)
        assert not CJK.search(en), f"{rel}:{line} English template has Chinese: {en!r}"
        assert CJK.search(zh) or not zh.strip(" ·:,()") or not re.search(r"[A-Za-z]{3}", zh), \
            f"{rel}:{line} the Chinese and English look swapped: {zh!r}"


def test_reported_sentences_have_english_templates():
    # Events → 📡 Risk Radar
    assert "zt(`⚠️ 最挤一周 ${f.busiest_week_count} 个事件扎堆`, `⚠️ ${f.busiest_week_count} events crowd into one week`)" in ANALYZER
    assert "zt('近期≥2%跌', 'recent ≥2% drops')" in ANALYZER
    assert "Crashes <b style=\"color:#e6edf3\">gap down overnight</b>" in ANALYZER
    # Digest status line
    assert "' · Benzinga not connected (paste a cookie in Settings ⚙)'" in SERVER
    assert "zt('一般', 'other')" in ANALYZER
    # Exit advice and the risk view
    assert "label_en:'Hold'" in ANALYZER and "of max profit; ${rgEn}: hold to ${zone}, then exit" in ANALYZER
    assert "'Beta-weighted, your whole book is <b style=" in ANALYZER


def test_page_scripts_get_the_language_before_they_run():
    lang = (STATIC / "zeus_lang.js").read_text(encoding="utf-8")
    assert "window.zt = function (zh, en)" in lang and "window.ztt = function (zh, en)" in lang
    assert "localStorage.getItem('zeusLang')" in lang and "localStorage.getItem('zeusLang')" in ZEUS_JS
    head = SERVER.split('pwa_head = """', 1)[1].split('"""', 1)[0]
    assert head.index('<script src="/static/zeus_lang.js"></script>') < head.index("dl_time_fmt.js")
    assert '_ZEUS_HEAD = \'<head><script src="/static/zeus_lang.js"></script>\'' in SERVER
    assert SERVER.count('.replace("<head>", _ZEUS_HEAD, 1)') == 2
    assert "privacy_inject + '<script src=\"/static/zeus_lang.js\"></script></head>'" in SERVER
    ta = (ROOT / "ta_screener" / "api.py").read_text(encoding="utf-8")
    assert 'page.PAGE_HTML.replace("<head>", ZEUS_HEAD, 1)' in ta
    users = {rel for rel, _, _ in _template_calls()}
    assert users == set(PAGE_SOURCES)
    # the report the analyzer writes on its own (generate_html) has no zeus_lang.js: zt keeps Chinese
    fallback = "if (!window.zt) { window.zt = function (zh) { return zh; }; window.ztt = window.zt; }"
    assert ANALYZER.index(fallback) < ANALYZER.index("zt(", ANALYZER.index("const RAW_DATA"))
    # the trade journal page, opened as a plain file, the same
    journal = (ROOT / "trade_journal" / "dashboard.html").read_text(encoding="utf-8")
    assert journal.index(fallback) < journal.index("zt(", journal.index(fallback) + len(fallback))


def test_ta_page_has_the_language_in_its_head():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ta_screener import api

    app = FastAPI()
    app.include_router(api.router)
    html = TestClient(app).get("/ta/").text
    assert '<head><script src="/static/zeus_lang.js"></script>' in html


# ------------------------------------------------------------------ English from the server
def test_liquidity_reasons_come_in_english_too():
    from checkup import liquidity as liq

    v = liq.leg_verdict(2.00, 2.30, vol=3, oi=5)
    assert v["reasons"] == ["宽 14% ($0.30)", "OI 极薄 (5)", "当日 vol 低 (3)"]
    assert v["reasons_en"] == ["wide 14% ($0.30)", "very thin OI (5)", "low volume today (3)"]
    assert liq.leg_verdict(1.00, 1.40)["reasons_en"] == ["very wide 33% ($0.40) — hard to fill at mid"]
    one = liq.leg_verdict(0, 0.05)
    assert one["reasons_en"] == ["one-sided quote — no order can rest at mid"]
    exps = [{"date": "2026-12-18", "dte": 36}]
    rows = [{100.0: {"call": {"bid": 4.9, "ask": 5.0, "vol": 900, "oi": 3000},
                     "put": {"bid": 4.8, "ask": 4.9, "vol": 800, "oi": 2500}}}]
    tv = liq._ticker_verdict(exps, rows, 100.0, 100.0)
    assert tv["label"] == "流动性良好" and tv["label_en"] == "Good liquidity"
    assert tv["detail_en"] == "ATM 36d options spread 2% · fills at mid"


def test_journal_review_explains_itself_in_english():
    from journal_review import insight, rule_check

    deep = [{"rule": "深亏时不止损", "rule_en": "No stop-loss on a deep loss", "net_pnl": -420.0}]
    assert insight.build({}, deep, [], []).startswith("本周深亏时砍单 1 笔")
    assert insight.build_en({}, deep, [], []) == \
        'This week you cut 1 trade at a deep loss, −$420 in all (against "no stop-loss on debit spreads": let them run to expiration)'
    caps = [{"rule": "同时 ≤10 仓", "rule_en": "≤10 positions at once"}] * 2
    assert insight.build_en({}, caps, [], []) == \
        'This week the rule "≤10 positions at once" was broken 2 times — open the details below to see which trades'
    assert insight.build_en({"yesterday_net_pnl": 0}, [], [], []) == \
        "No trades closed yesterday and no rules broken this week — keep up the discipline"
    opens = [{"ticker": "SPY", "spread_label": "SPY 600/610C 12/18/26", "open_date": "2026-11-10"}] * 3
    v = rule_check.check_daily_open_cap(opens, "2026-11-10", "2026-11-10", cap=2)
    assert v and v[0]["rule_en"] == "≤2 new trades a day"


def test_trade_journal_cards_come_in_english_too(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "trade_journal"))   # its modules import each other by name
    import journal_stats
    import whatif_engine

    stats = {
        "quadrant": {"cells": {"bad_dec_bad_res": {"share": 0.2, "n": 2, "net_pnl": -300},
                               "good_dec_good_res": {"share": 0.5}}},
        "kpi": {"win_rate": 0.76, "profit_factor": 2.75, "expectancy_dollar": 104, "avg_winner": 217, "avg_loser": -244},
        "mistakes": [{"mistake": "FOMO", "n": 3, "net_pnl": -500, "avg_pnl": -166}],
        "edge_finder": {"top_edges": [{"label": "direction=BULL", "n": 9, "win_rate": 0.89, "net_pnl": 2196, "expectancy": 244}],
                        "bleeders": []},
        "confidence_calibration": [{"confidence": "High", "n": 10, "win_rate": 0.8, "net_pnl": 900},
                                   {"confidence": "Low", "n": 9, "win_rate": 0.4, "net_pnl": -300}],
    }
    cards = journal_stats.narratives([{}] * 41, stats)
    summary = {
        "median_capture": 0.65, "median_capture_vs_maxprofit": 0.35, "winners_under_50pct_capture": 0.2,
        "optimal_tp_premium": {"best_k": 0.5, "best_total": 1000, "delta_vs_actual": -200}, "coverage": {"covered": 12},
        "stop_discipline": {"breach_rate": 0.3, "n_losers": 10, "breached_50pct": 3},
        "post_close": {"covered": 8, "left_on_table_n": 7, "left_on_table_sum": 4961, "held_to_expiry_delta": 500,
                       "losers": 4, "losers_rebounded": 4},
        "exit_policy": {"n": 25, "mp_best": {"q": 0.9, "annual": 40, "total": 3000}, "actual": 1000,
                        "actual_annual": 20, "hold_to_expiry": 1500, "hold_annual": 25, "mp_n": 22},
    }
    cards += whatif_engine.whatif_narratives(summary)
    assert len(cards) == 9
    for c in cards:
        for field in ("title", "headline", "body", "evidence"):
            if c.get(field):
                assert c.get(field + "_en") and not CJK.search(c[field + "_en"]), (c["key"], field)
    health = cards[0]
    assert health["headline_en"] == "41 closed trades, win rate 76%, expectancy $104 per trade, profit factor 2.75."
    assert cards[2]["headline_en"] == "Your strongest edge is direction=BULL: it makes $244 per trade on average."


def test_regime_bands_and_risk_topics_have_english_names():
    from checkup import regime, risk_radar

    for key in ("bull", "chop", None):
        assert regime.band(key)["label_en"] and not CJK.search(regime.band(key)["label_en"])
    assert set(risk_radar.LABEL_EN) == set(risk_radar.LABEL)
    for t, zh in risk_radar.LABEL.items():
        assert EN.get(zh), f"topic name {zh!r} shows on its own in the radar and needs a key"
    items = [{"title": "Treasury yields surge as bond selloff deepens"}] * 30
    hot = risk_radar.regime_saturation(items)["hot_topics"]
    assert hot and hot[0]["label_en"] == "Treasury yields spike"


# ------------------------------------------------------------------ zeus.js lookup, run under node
def _zeus(cases: dict, dictionary: dict) -> dict:
    lookup = ZEUS_JS.split("  // ------------------------------------------------------------------ dictionary (zh → en)", 1)[1]
    lookup = lookup.split("  // ---- English: translate every interface string in place", 1)[0]
    bi = "function biLabel(el)" + ZEUS_JS.split("function biLabel(el)", 1)[1].split("\n  function biTitle", 1)[0]
    script = """
const window = {};
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
%s
%s
DICT = input.dict;
const el = t => ({ nodeType: 1, textContent: t, attrs: {}, parentNode: null, tagName: 'BUTTON', isContentEditable: false,
  hasAttribute(n) { return n in this.attrs; }, getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
  setAttribute(n, v) { this.attrs[n] = v; }, removeAttribute(n) { delete this.attrs[n]; },
  classList: { contains() { return false; } } });
const out = { tr: {}, bi: {}, nested: {} };
for (const s of input.tr) out.tr[s] = trString(s);
for (const s of input.bi) { const e = el(s); biLabel(e); out.bi[s] = e.getAttribute('data-zs-en'); }
for (const s of input.bi) { const e = el(s); e.children = [{}]; biLabel(e); out.nested[s] = e.getAttribute('data-zs-en'); }
process.stdout.write(JSON.stringify(out));
""" % (lookup, bi)
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "lookup.js"
        js.write_text(script, encoding="utf-8")
        run = subprocess.run([NODE, str(js)], input=json.dumps({"dict": dictionary, **cases}),
                             capture_output=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


pytestmark_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytestmark_node
def test_lookup_finds_whole_sentences_on_the_real_dictionary():
    cases = {
        "芯片龙头财报=板块级 landmine:坏财报当天 SOXX≤-1% 命中 55%(SOXX均-1.6%)":
            "Chip bellwether earnings are a sector-wide landmine: after a bad report SOXX fell ≥1% on 55% of days (SOXX −1.6% on average)",
        "📭 还没扫描过 — 点「跑扫描」": '📭 No scan yet. Click "Run scan"',
        "数据: journal_review/ · 只读本地日记/缓存,不发起新的实时行情请求":
            "Data: journal_review/ · reads only your local journal and cache; makes no new market-data requests",
        "偏斜 = 25Δ put IV 减 25Δ call IV, 正值越大说明下跌保护被抢得越凶。\n      本页只陈述\"贵不贵/偏不偏\", 不给方向标签 —— 高 put 量可能是看空, 也可能是持股买保险或卖 put 收钱, 成交量本身分不出来。":
            EN["偏斜 = 25Δ put IV 减 25Δ call IV, 正值越大说明下跌保护被抢得越凶。 本页只陈述\"贵不贵/偏不偏\", 不给方向标签 —— 高 put 量可能是看空, 也可能是持股买保险或卖 put 收钱, 成交量本身分不出来。"],
        "⚠️ 你的持仓 · 个股财报:只影响这一仓(对大盘/板块无溢出)":
            "⚠️ " + EN["你的持仓"] + " · Single-stock earnings: affects only this position (no spillover to the index or sector)",
    }
    out = _zeus({"tr": list(cases), "bi": []}, EN)["tr"]
    for zh, en in cases.items():
        assert out[zh] == en


@pytestmark_node
def test_lookup_rules_on_a_small_dictionary():
    d = {"还没扫描过": "No scan yet", "跑扫描": "Run scan", "均": "all", "新闻": "news", "近期": "recent",
         "跌": "down", "隐藏": "Hide", "组": "groups", "选股": "Picker", "今日重点关注": "Today's watchlist",
         "整句 一": "Whole sentence one", "天": " days", "期权量": "Option volume", "尚未爬取": "not crawled yet"}
    tr = ["MarketWatch 新闻", "SOXX均-1.6%", "📭 还没扫描过", "近期：", "近期: ", "整句\n    一",
          "还没扫描过 · 跑扫描", "SPY跌", "近期:跌", "还有生词"]
    bi = ["隐藏 ETF 组", "🎯 选股", "今日重点关注 (10)", "7天", "💹 期权量 · 尚未爬取"]
    out = _zeus({"tr": tr, "bi": bi}, d)
    assert out["tr"]["MarketWatch 新闻"] == "MarketWatch news"          # the page's own Latin words stay whole
    assert out["tr"]["SOXX均-1.6%"] == "SOXX all-1.6%"                  # not "SOX Xall"
    assert out["tr"]["📭 还没扫描过"] == "📭 No scan yet"               # emoji around a key
    assert out["tr"]["近期："] == "recent: " and out["tr"]["近期: "] == "recent: "   # a Chinese colon keeps its space
    assert out["tr"]["整句\n    一"] == "Whole sentence one"           # line breaks from the HTML source
    assert out["tr"]["还没扫描过 · 跑扫描"] == "No scan yet · Run scan"   # pieces joined by " · "
    assert out["tr"]["SPY跌"] == "SPY down"
    assert out["tr"]["近期:跌"] == "recent: down"
    assert out["tr"]["还有生词"] == "还有生词"                            # unknown text is left alone
    assert out["bi"]["隐藏 ETF 组"] == "Hide ETF groups"                 # Latin words between Chinese ones stay
    assert out["bi"]["🎯 选股"] == "Picker"
    assert out["bi"]["今日重点关注 (10)"] == "Today's watchlist"        # a count badge after a label
    assert out["bi"]["7天"] == "7 days"                                  # the number stays in the gloss
    assert out["nested"]["7天"] == "days"                                # a header built of several elements
    assert out["bi"]["💹 期权量 · 尚未爬取"] == "Option volume · not crawled yet"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_language_helper_parses():
    js = STATIC / "zeus_lang.js"
    run = subprocess.run([NODE, "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(js)],
                         capture_output=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr

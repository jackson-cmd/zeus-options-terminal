"""风险雷达——体检第 3 个 tab 的纯函数核心。

两件事(都喂 research/drop_events 的实证结论):
  1. regime_saturation(today_items): 读今日实时 digest,数各"高 lift 风险话题"今天被
     报道的强度,跟**近 250 交易日基线**比(P50/P90/P95) → 0-100 风险分 + 哪些话题在烧。
     用基线(而非 15 年绝对阈值)是因为新闻量逐年膨胀,绝对计数会越近越"热"。
     危险权重来自研究里的剂量-反应 lift(美债收益率 3.3 / 关税 2.4 / 疫情 3.2 …)。
  2. annotate_landmines(events): 给现成的 /api/major-events 日历(FOMC/CPI/非农+财报)
     逐条贴上历史 lift 徽章 + 板块级提示(芯片龙头坏财报当天 SOXX≤-1% 命中 55%)。

数据文件 risk_radar_data.json(baseline + lift)由 risk_radar_build.py 离线产出。
关键词集是 live 计数与基线计数的唯一真源——基线脚本也 import 本模块的 count_topics,
保证 live 与基线口径一致。诚实:这给的是"高危窗口"提示(lift 1.2-3.3),不是预测。
"""
from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DATA_FILE = _HERE / "risk_radar_data.json"
_REF_FILE = _HERE / "risk_radar_reference.json"
_RALLY_REF_FILE = _HERE / "rally_reference.json"

# ── 话题关键词(与 research/drop_events/build_event_flags.py 同源,改这里要同步基线)──
TOPIC_KW = {
 "yields_bonds": ['treasury yield','10-year yield','10-yr yield','bond yield','yields surged','yields jumped','bond selloff','yield curve','yields spike','rising yields','yield spike','bond market rout'],
 "tariffs_trade": ['tariff','trade war','trade deal','section 301','import tax','trade tension','china trade','trade talks','export control','export curb','trade restriction','levies','reciprocal tariff','trade barrier','trump tariff'],
 "pandemic_covid": ['covid','coronavirus','pandemic','lockdown','omicron','delta variant','quarantine','outbreak','wuhan'],
 "fed_fomc": ['fomc','federal reserve',"fed's",'fed chair','fed minutes','fed meeting','fed officials','rate decision','interest rate decision','rate hike','rate cut','rate-hike','rate-cut','hawkish','dovish','jerome powell','powell','basis point','quantitative easing','quantitative tightening','monetary policy','fed funds','tapering',' taper ','fed signals','fed holds','fed raises','fed cuts'],
 "inflation_cpi": ['cpi','consumer price index','inflation data','inflation report','inflation rose','inflation cooled','inflation eased','core inflation','pce ','pce inflation','ppi','producer price','inflation print','inflation gauge','inflation jumped','hotter inflation','cooler inflation','disinflation','inflation accelerat'],
 "jobs_nfp": ['nonfarm','non-farm','jobs report','payrolls','unemployment rate','jobless claims','labor market','jobs data','employment report','adp report','adp employment','hiring slowed','jobs added'],
 "banking_credit": ['bank failure','bank collapse','silicon valley bank',' svb ','credit suisse','contagion','bailout','liquidity crisis','bank run','credit downgrade','regional bank','bond rout','credit crunch','default risk','sovereign debt','lehman','financial crisis','deposit flight','first republic','signature bank'],
 "geopolitics_war": ['invasion','invade','missile','airstrike','ukraine','russia invad','iran','israel','gaza','north korea','geopolitical','war in','military strike','nuclear','escalat','middle east tension','oil embargo'],
 "oil_energy": ['oil price','crude oil','opec','oil surged','oil spike','energy crisis','gas prices','brent crude','wti crude','oil shock','oil jumps'],
 "govt_fiscal": ['government shutdown','shutdown','fiscal cliff','debt ceiling','budget impasse','default on debt','sovereign downgrade','us downgrade'],
 "gdp_growth": ['gdp','gross domestic product','recession','economic growth','soft landing','hard landing','stagflation','economic contraction','growth scare','slowdown fears'],
 "vix_spike": ['vix','fear gauge','volatility index','volatility spike','vix surged','vix jumped'],
 "ai_bubble_valuation": ['bubble','overvalued','frothy','ai bubble','overhyped','valuation concern','stretched valuation','ai spending','ai capex','circular financing','correction warning','market top','euphoria','speculative mania'],
 "sovereign_debt": ['sovereign debt','eurozone','euro zone','euro-zone','greece','greek','italian bond','italian yield','italy debt','spanish bond','spain debt','portugal debt','european debt','eu debt','debt crisis','redenomination','bund spread','periphery','bailout fund','euro crisis','draghi'],
 "china_shock": ['yuan devaluation','renminbi','china circuit breaker','shanghai composite','shanghai stocks','shenzhen','china stocks','chinese stocks','china market','china crash','china rout','china tumbl','china slowdown','pboc','china gdp','offshore yuan','china property','evergrande','china stimulus','china deflation'],
 "vol_unwind": ['vol-target','volmageddon',' xiv ','svxy','short volatility','short vol','vix spike','vix surge','vix soar','vix jump','vix explod','volatility spike','volatility surge','gamma squeeze','gamma unwind','deleveraging','margin call','volatility targeting','risk parity','forced selling','quad witching','fear gauge'],
 "ai_efficiency": ['deepseek','ai efficiency','efficient ai','cheaper ai','compute cost','training cost','model efficiency','ai bubble burst','ai overspend','ai capex cut','capex concern','ai monetization','inference cost','overbuild'],
 "export_controls": ['export control','export restriction','trade restriction','chip ban','chip restriction','entity list','semiconductor export','semiconductor restriction','china chip','chip export','huawei','equipment export','euv','nvidia china','china tech ban','tech export ban'],
 "drug_pricing": ['drug pricing','drug price','medicare negotiation','medicare drug','doj healthcare','ira drug','pharma investigation','price controls','most favored nation','medicaid cut','pharma pricing','340b','pbm reform'],
 "epidemic_nonc": ['ebola','monkeypox','mpox','bird flu','h5n1','avian flu','swine flu',' sars ',' mers ','disease outbreak','virus outbreak','pandemic threat','health emergency','zika'],
}

# 危险权重 = research 剂量反应 P95 lift(当日跌≥1% 相对基础率的倍数)。
# ⚠️ banking_credit / govt_fiscal 做了 domain 修正:research 的关键词代理对它们失效
# (lift≈1 是测量假象,"financial crisis" 等词太泛),但 SVB/2008/债务上限是真危险 → 上调。
LIFT = {
 "yields_bonds": 3.29, "pandemic_covid": 3.21, "tariffs_trade": 2.39, "fed_fomc": 1.78,
 "vix_spike": 1.76, "inflation_cpi": 1.50, "jobs_nfp": 1.43, "oil_energy": 1.21,
 "gdp_growth": 1.16, "geopolitics_war": 1.13, "banking_credit": 1.80, "govt_fiscal": 1.40,
 "ai_bubble_valuation": 0.85,
 # 7 类盲区(E5 历史实测回填):vol_unwind/epidemic 统计显著(p<0.001)用实测;
 # sovereign_debt 实测 0.90 是阈值 artifact(2011 欧债背景噪声把 P90 锁到 10)→domain 设;
 # ai_efficiency/drug_pricing n<10 太稀(DeepSeek 类饱和日仍真危险)→domain 保守设。
 "sovereign_debt": 1.60, "china_shock": 1.20, "vol_unwind": 1.98, "ai_efficiency": 2.00,
 "export_controls": 1.25, "drug_pricing": 1.30, "epidemic_nonc": 2.46,
}
LABEL = {
 "yields_bonds":"美债收益率飙升","tariffs_trade":"关税/贸易战","pandemic_covid":"疫情",
 "fed_fomc":"美联储/FOMC","inflation_cpi":"通胀/CPI","jobs_nfp":"就业/非农",
 "banking_credit":"银行/信用危机","geopolitics_war":"地缘/战争","oil_energy":"油价/能源",
 "govt_fiscal":"政府/债务上限","gdp_growth":"GDP/衰退","vix_spike":"VIX/恐慌",
 "ai_bubble_valuation":"AI泡沫/估值",
 "sovereign_debt":"欧债/主权信用","china_shock":"中国贬值/A股","vol_unwind":"Vol挤压/gamma",
 "ai_efficiency":"AI效率/DeepSeek","export_controls":"芯片出口管制","drug_pricing":"药价/医保政治",
 "epidemic_nonc":"传染病(非COVID)",
}
# English names, for the interface in English (the page puts them inside its own sentences)
LABEL_EN = {
 "yields_bonds": "Treasury yields spike", "tariffs_trade": "Tariffs / trade war", "pandemic_covid": "Pandemic",
 "fed_fomc": "Fed / FOMC", "inflation_cpi": "Inflation / CPI", "jobs_nfp": "Jobs / payrolls",
 "banking_credit": "Bank / credit crisis", "geopolitics_war": "Geopolitics / war", "oil_energy": "Oil / energy",
 "govt_fiscal": "Shutdown / debt ceiling", "gdp_growth": "GDP / recession", "vix_spike": "VIX / fear",
 "ai_bubble_valuation": "AI bubble / valuation",
 "sovereign_debt": "Euro debt / sovereign credit", "china_shock": "China devaluation / A-shares",
 "vol_unwind": "Vol squeeze / gamma", "ai_efficiency": "AI efficiency / DeepSeek",
 "export_controls": "Chip export controls", "drug_pricing": "Drug pricing / Medicare politics",
 "epidemic_nonc": "Epidemics (non-COVID)",
}
# kind: scheduled=计划内(日期已知,前瞻可用) shock=突发冲击 indicator=并发指标 anti=反信号
KIND = {
 "fed_fomc":"scheduled","inflation_cpi":"scheduled","jobs_nfp":"scheduled","gdp_growth":"scheduled",
 "yields_bonds":"shock","tariffs_trade":"shock","pandemic_covid":"shock","banking_credit":"shock",
 "geopolitics_war":"shock","oil_energy":"shock","govt_fiscal":"shock",
 "vix_spike":"indicator","ai_bubble_valuation":"anti",
 "sovereign_debt":"shock","china_shock":"shock","vol_unwind":"shock","ai_efficiency":"shock",
 "export_controls":"shock","drug_pricing":"shock","epidemic_nonc":"shock",
}

# 龙头分组(财报溢出提示)
CHIP_BELLWETHER = {"NVDA","AMD","AVGO","MU","INTC","QCOM","TSM","ASML","MRVL","TXN"}
MEGACAP_BELLWETHER = {"AAPL","MSFT","GOOGL","GOOG","AMZN","META","TSLA"}
# AI 基建/网络/服务器龙头:财报能拖累整个 AI/科技 trade(如 CSCO 2026-02-12 拖累 QQQ)
AI_INFRA_BELLWETHER = {"CSCO","ANET","DELL","SMCI","VRT","NTAP","CIEN","JNPR","ORCL"}

_SCORE_DIVISOR = 1.8   # raw→0-100(校准:单个饱和高lift话题=high;疫情/SVB级=100)
_P90_FLOOR = 3         # 绝对下限:稀有话题(疫情/收益率)别在 2-3 条 incidental 上就"饱和"
_P95_FLOOR = 5
_ABS_MIN = 4           # 话题至少这么多条才算数(低量日防噪)
_SHARE_ELEV = 0.015    # 关键:话题要占今日新闻 ≥1.5% 才算"在烧"(滤掉 3/632 这种顺带提一句)
_SHARE_SAT = 0.04      # 占 ≥4% 才算"饱和/主导今日新闻周期"


def _text(item) -> str:
    return ((item.get("title_en") or item.get("title") or "") + " "
            + (item.get("teaser") or "")).lower()


def count_topics(items) -> dict:
    """数一批 digest item 里每个话题命中的条数(live 与基线唯一口径)。"""
    out = {t: 0 for t in TOPIC_KW}
    for it in (items or []):
        try:
            txt = _text(it)
        except Exception:
            continue
        for t, kws in TOPIC_KW.items():
            if any(k in txt for k in kws):
                out[t] += 1
    return out


def load_data() -> dict:
    """读 baseline + lift 数据文件;缺失则用内置 lift、基线给保守默认(fail-soft)。"""
    try:
        return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        topics = {t: {"label": LABEL[t], "lift": LIFT[t], "kind": KIND[t],
                      "p50": 1, "p90": 4, "p95": 7} for t in TOPIC_KW}
        return {"topics": topics, "baseline_through": None, "baseline_window_days": 0}


def load_reference() -> dict:
    """读「📊 数据&规律」面板的静态研究结论(fail-soft)。"""
    try:
        return json.loads(_REF_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_rally_reference() -> dict:
    """读「📈 大涨规律」tab 的静态结论(research/rally_events WIIM 蒸馏, fail-soft)。"""
    try:
        return json.loads(_RALLY_REF_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_wiim_causes_reference() -> dict:
    """读「📊 异动原因」tab 的全量 WIIM 原因统计(纯文章口径, 不看价格, fail-soft)。"""
    try:
        return json.loads((_HERE / "wiim_causes_reference.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def summarize_recent(spy_series, qqq_series) -> dict:
    """最近的大跌统计(最近出现过几次这种级别的下跌)。
    series: [{"date","ret"(%)}, ...](近~半年日收益,任意序)。
    数最近 21/63 交易日 SPY/QQQ 跌≥1%、≥2% 次数 + 列近期 ≥2% 跌日。"""
    def _sorted(s):
        return sorted([x for x in (s or []) if x.get("ret") is not None], key=lambda x: x["date"])
    spy, qqq = _sorted(spy_series), _sorted(qqq_series)
    out = {}
    for name, s in (("spy", spy), ("qqq", qqq)):
        for wlabel, n in (("m1", 21), ("m3", 63)):
            last = s[-n:]
            out[f"{name}_{wlabel}"] = {
                "d1": sum(1 for x in last if x["ret"] <= -1.0),
                "d2": sum(1 for x in last if x["ret"] <= -2.0)}
    bydate = {}
    for x in spy: bydate.setdefault(x["date"], {})["spy"] = x["ret"]
    for x in qqq: bydate.setdefault(x["date"], {})["qqq"] = x["ret"]
    recent2 = sorted(d for d, v in bydate.items()
                     if (v.get("spy", 0) <= -2.0 or v.get("qqq", 0) <= -2.0))[-8:]
    out["recent2pct"] = [{"date": d, "spy": bydate[d].get("spy"), "qqq": bydate[d].get("qqq")}
                         for d in reversed(recent2)]
    out["as_of"] = max(bydate) if bydate else None
    return out


def regime_saturation(today_items, data=None) -> dict:
    """今日 digest 的风险话题饱和度 → 0-100 分 + 在烧的话题。"""
    data = data or load_data()
    topics = data.get("topics", {})
    counts = count_topics(today_items)
    hot, context, raw = [], [], 0.0
    n_items = len(today_items or [])
    for t, info in topics.items():
        c = counts.get(t, 0)
        share = c / n_items if n_items else 0
        p90 = max(_P90_FLOOR, info.get("p90", 5))   # 绝对下限,防稀有话题 hair-trigger
        p95 = max(_P95_FLOOR, info.get("p95", 9))
        # 三重门:相对(P90)+ 绝对量(防低量日)+ 占比(关键,要"在烧"非"顺带提")
        if c < max(_ABS_MIN, p90) or share < _SHARE_ELEV:
            continue
        if c >= p95 and share >= _SHARE_SAT:
            frac, plabel = 1.0, "饱和(主导今日新闻)"
        else:
            frac, plabel = 0.6, "偏高"
        lift = info.get("lift", 1.0)
        entry = {"topic": t, "label": info.get("label", t), "label_en": LABEL_EN.get(t, t), "count": c,
                 "percentile": plabel, "frac": frac, "lift": lift,
                 "share": round(share, 3), "kind": info.get("kind", "shock")}
        if lift > 1.0:
            contrib = frac * (lift - 1.0)
            raw += contrib
            entry["contrib"] = round(contrib, 2)
            hot.append(entry)
        else:
            entry["note"] = "历史上非择时信号(常在涨势里唱衰)"
            context.append(entry)
    hot.sort(key=lambda h: -h["contrib"])
    score = min(100, round(100 * raw / _SCORE_DIVISOR))
    level = "high" if score >= 60 else "elevated" if score >= 30 else "low"
    return {
        "score": score, "level": level,
        "hot_topics": hot, "context_topics": context,
        "top": hot[0] if hot else None,
        "n_items": len(today_items or []),
        "baseline_through": data.get("baseline_through"),
        "as_of_partial": True,   # 今日 digest 是盘中累计,清晨偏低
    }


def forward_score(landmines, horizon=30) -> dict:
    """未来 horizon 天的"事件密度/严重度"评分(0-100)。
    高=未来一个月高 lift 事件扎堆(FOMC/CPI/非农 + 龙头财报)→ 格外小心、别满仓撞。
    每个事件贡献 min(1, lift-1)(单个高 lift 不独大,密度=更多事件才推高);找最挤的一周。"""
    evs = [e for e in (landmines or [])
           if e.get("days_away") is not None and 0 <= e["days_away"] <= horizon and e.get("lift")]
    raw = sum(min(1.0, max(0.0, (e["lift"] - 1.0))) for e in evs)
    score = min(100, round(100 * raw / 6.0))
    level = "high" if score >= 60 else "elevated" if score >= 30 else "low"
    days = sorted(e["days_away"] for e in evs)
    busiest, bstart = 0, None
    for d in days:
        cnt = sum(1 for x in days if d <= x < d + 7)
        if cnt > busiest:
            busiest, bstart = cnt, d
    top = sorted(evs, key=lambda e: -(e.get("lift") or 0))[:6]
    return {
        "score": score, "level": level, "n_events": len(evs), "horizon": horizon,
        "busiest_week_count": busiest, "busiest_week_start_days": bstart,
        "top_events": [{"date": e.get("date"), "name": e.get("name"), "lift": e.get("lift"),
                        "days_away": e.get("days_away"), "severity": e.get("severity")} for e in top],
    }


def annotate_landmines(events, held_tickers=None) -> list:
    """给 /api/major-events 的日历逐条贴 lift 徽章 + 板块级财报提示。

    events: [{date, type, name, ticker, tier, days?}, ...](type 约定 'macro'|'earnings')
    held_tickers: 当前持仓标的(集合);命中则提级为"你的持仓"。
    """
    held = {t.upper() for t in (held_tickers or [])}
    out = []
    for ev in (events or []):
        e = dict(ev)
        name = (ev.get("name") or "").lower()
        typ = (ev.get("type") or "").lower()
        tkr = (ev.get("ticker") or "").upper()
        lift, sev, note = None, "info", ""
        if typ == "earnings" or tkr:
            if tkr in CHIP_BELLWETHER:
                lift, sev = 2.2, "warn"
                note = "芯片龙头财报=板块级 landmine:坏财报当天 SOXX≤-1% 命中 55%(SOXX均-1.6%)"
            elif tkr in MEGACAP_BELLWETHER:
                lift, sev = 1.8, "warn"
                note = "大盘股龙头财报:坏财报当天 QQQ 均 -1.2%"
            elif tkr in AI_INFRA_BELLWETHER:
                lift, sev = 1.8, "warn"
                note = "AI基建/网络龙头财报:可拖累整个 AI/科技 trade(如 CSCO 2026-02-12 砸 QQQ)"
            elif tkr in held:
                lift, sev = 1.2, "info"
                note = "个股财报:只影响这一仓(对大盘/板块无溢出)"
            else:
                continue   # 非龙头、非持仓的个股财报(KO 这种)忽略——实证:对指数/板块无用
            if tkr in held:
                sev = "red"; note = "⚠️ 你的持仓 · " + note
        else:  # 宏观
            if any(k in name for k in ("fomc", "fed", "rate decision", "interest rate")):
                lift, sev, note = 1.78, "warn", "FOMC 当天 SPY 跌≥1% 概率 ~1.3-1.8×;别前一天开新单/满仓撞"
            elif any(k in name for k in ("cpi", "inflation", "pce", "ppi")):
                lift, sev, note = 1.50, "warn", "CPI 印刷日:饱和时 SPY 跌≥1% 概率 ~1.5×"
            elif any(k in name for k in ("payroll", "jobs", "employment", "nonfarm", "nfp")):
                lift, sev, note = 1.43, "warn", "非农日:SPY 跌≥1% 概率 ~1.3-1.4×(最显著的计划内日)"
            elif "gdp" in name:
                lift, sev, note = 1.16, "info", "GDP:历史影响偏弱"
            else:
                lift, sev, note = 1.10, "info", "宏观事件"
        e["lift"] = lift; e["severity"] = sev; e["note"] = note
        out.append(e)
    # 按(严重度, 日期)排:红→黄→蓝,同级按日期近的在前
    sev_rank = {"red": 0, "warn": 1, "info": 2}
    out.sort(key=lambda e: (sev_rank.get(e["severity"], 3), e.get("date", "")))
    return out

"""AI 选股 5 分类的 system prompt + 每票数据块渲染。"""
from __future__ import annotations

from barchart import registry as R

VERDICTS = [
    "看涨·低点抄底", "看涨·趋势追涨", "不交易", "看跌·高点阻力", "看跌·颓势追跌",
]

_SYSTEM_HEAD = """你是资深美股期权价差选股助手,服务一位垂直价差(vertical debit spread)专家。你的任务:给定若干股票的技术、基本面、期权结构、新闻数据,把每只股票**归入且仅归入**下列 5 类之一,并给出技术/基本面/新闻三段简短中文理由。

【交易者的 edge(据此判断)】
- 主打 ATM 借方价差(看涨=call debit / 看跌=put debit),35-45 DTE, 宽度≈标的价 10%。
- **看涨·低点抄底**: 上升或健康结构里回踩到支撑/EMA、RSI 偏低(≲40)或超卖反转,风险回报好 → 开 call 价差抄底。
- **看涨·趋势追涨**: 站上 EMA20/50、动能向上、突破或贴近新高、RSI 强势不超买失控 → 顺势开 call 价差追涨。
- **看跌·高点阻力**: 冲到阻力/EMA 上沿被拒、RSI 偏高(≳65)或超买回落、上方套牢盘重 → 开 put 价差做高点回落。
- **看跌·颓势追跌**: 跌破 EMA20/50/200、破位、动能持续向下、无支撑 → 顺势开 put 价差追跌。
- **不交易**: 信号矛盾(技术与基本面/期权/新闻打架)、纯震荡无方向、离关键位太远无 R:R、临财报(≤3 天)不确定性高、或数据不足。宁可归「不交易」也别硬凑方向。

【判读要点】
- EMA 价差%正=现价站上该均线;负=跌破。RSI<30 超卖、>70 超买。pos52 是 52 周区间位置(0=年内低,100=年内高)。
- 期权: pcr_oi/pcr_vol <1 偏多、>1 偏空; opt_flow_net_pct 正=大单偏多; opt_bias 正=期权综合偏多; max_pain_pct 是 MaxPain 相对现价%。
- 基本面看估值(PE/PEG)、增长(EPS/Sales)、盈利(毛利/ROE)、分析师 Recom(1=强买,5=强卖)与 Target Price 相对现价的空间。
- 新闻: 近期利好/利空催化会强化或推翻纯技术判断。无新闻则以技术+期权为主。
- ETF(如 SPY/QQQ/SMH)无基本面/新闻时,纯按技术+结构判。"""

_SYSTEM_OUTPUT = """【输出】
只输出一个 JSON 数组,每项:
{"ticker":"..","verdict":"上述5类之一(一字不差)","confidence":0-100 整数,"tech":"技术面理由一句","fundamental":"基本面理由一句(无数据写\\"—\\")","news":"新闻面理由一句(无新闻写\\"—\\")","summary":"综合一句话操作建议"}
所有理由必须**简体中文陈述句**,具体引用数字(如\\"现价站上EMA20+3.2%,RSI 58 动能健康\\"),不空泛。confidence 反映信号一致性。**每只都要给,不许漏,不许多。** 不要 markdown 代码围栏,不要任何额外文字。"""


def _barchart_system_note() -> str:
    """动态从 registry 生成「Barchart 榜单先验」说明段(加类别自动同步, 不写死)。"""
    lines = [
        "【Barchart 榜单先验(用户自己 barchart 账号里的信号池)】",
        "部分票会带一段「Barchart 榜单」, 表示它命中了用户在 barchart 保存的某些 screener。"
        "出现在某类池 = 该方向的一个**先验信号**;但这只是**先验不是结论** —— 你仍须结合技术/基本面/期权/新闻**独立判断**, 先验可被数据推翻。",
        "各类别方向含义:",
    ]
    for slug in R.category_order():
        cmeta = R.CATEGORIES.get(slug) or {}
        label = cmeta.get("label", slug)
        note = cmeta.get("ai_note", "")
        lines.append(f"  · {label}: {note}")
    lines.append(
        "⚠️ 尤其注意: 「回调卖出」类里的 rsi超卖 榜单**名叫超卖实为 RSI 超买冲高回落(看跌)**, 别被名字骗成看涨;"
        "「领涨异动」(gainer)可能是超跌死猫反抽, 需甄别是真突破还是反弹, 不可无脑追多。"
    )
    return "\n".join(lines)


SYSTEM = _SYSTEM_HEAD + "\n\n" + _barchart_system_note() + "\n\n" + _SYSTEM_OUTPUT


def _fmt_tech(t: dict) -> str:
    parts = []
    if t.get("price") is not None:
        parts.append(f"现价 {t['price']}")
    if t.get("rsi14") is not None:
        parts.append(f"RSI14 {t['rsi14']}")
    for span in (20, 50, 100, 200):
        g = t.get(f"ema{span}_gap_pct")
        if g is not None:
            parts.append(f"EMA{span} {t.get(f'ema{span}')}(价差{g:+g}%)")
    if t.get("perf20d_pct") is not None:
        parts.append(f"20d {t['perf20d_pct']:+g}%")
    if t.get("perf60d_pct") is not None:
        parts.append(f"60d {t['perf60d_pct']:+g}%")
    return "; ".join(parts)


def _fmt_structure(s: dict) -> str:
    parts = []
    if s.get("sr_up"):
        u = s["sr_up"]
        parts.append(f"上方阻力 {u['px']}(距{u.get('dist_pct')}%,conf{u.get('conf')})")
    if s.get("sr_dn"):
        d = s["sr_dn"]
        parts.append(f"下方支撑 {d['px']}(距{d.get('dist_pct')}%,conf{d.get('conf')})")
    if s.get("touching"):
        parts.append(f"正踩{s['touching']}(conf{s.get('touch_conf')})")
    if s.get("score_dir"):
        parts.append(f"TA评分方向 {s['score_dir']}({s.get('score')})")
    if s.get("pos52") is not None:
        parts.append(f"52周位置 {s['pos52']}")
    o = s.get("opt") or {}
    opt_bits = []
    if o.get("pcr_oi") is not None:
        opt_bits.append(f"PCR-OI {o['pcr_oi']}")
    if o.get("pcr_vol") is not None:
        opt_bits.append(f"PCR-Vol {o['pcr_vol']}")
    if o.get("max_pain_pct") is not None:
        opt_bits.append(f"MaxPain {o['max_pain_pct']:+g}%")
    if s.get("opt_flow_net_pct") is not None:
        opt_bits.append(f"大单净{s['opt_flow_net_pct']:+g}%")
    if s.get("opt_bias") is not None:
        opt_bits.append(f"期权偏置{s['opt_bias']:+g}")
    if opt_bits:
        parts.append("期权[" + ", ".join(opt_bits) + "]")
    if s.get("earnings_days") is not None and 0 <= s["earnings_days"] <= 14:
        parts.append(f"⚠️{s['earnings_days']}天后财报")
    return "; ".join(parts)


def _fmt_fundamentals(f: dict) -> str:
    if not f:
        return "—"
    order = ["P/E", "Forward P/E", "PEG", "EPS growth next Y", "Sales Q/Q",
             "Gross Margin", "Profit Margin", "ROE", "Recom", "Target Price",
             "Perf Month", "Perf Quarter", "Perf Year"]
    bits = [f"{k} {f[k]}" for k in order if f.get(k)]
    return "; ".join(bits) if bits else "—"


def _fmt_barchart(bc: list[dict]) -> str:
    """[{name, category_label, desc}, ...] → '名字(类别label·desc精简)、...'。"""
    bits = []
    for it in bc:
        name = it.get("name") or it.get("slug") or ""
        label = (it.get("category_label") or "").strip()
        desc = (it.get("desc") or "").strip()
        if len(desc) > 34:
            desc = desc[:34] + "…"
        meta = "·".join(x for x in (label, desc) if x)
        bits.append(f"{name}({meta})" if meta else name)
    return "、".join(b for b in bits if b)


def render_block(feat: dict) -> str:
    t = feat["ticker"]
    head = f"### {t}" + (f"  [{feat.get('sector')}]" if feat.get("sector") else "")
    lines = [head]
    tech = _fmt_tech(feat.get("tech") or {})
    if tech:
        lines.append(f"技术: {tech}")
    struct = _fmt_structure(feat.get("structure") or {})
    if struct:
        lines.append(f"结构/期权: {struct}")
    bc = feat.get("barchart") or []
    bc_line = _fmt_barchart(bc)
    if bc_line:
        lines.append(f"【Barchart 榜单】{bc_line}")
    lines.append(f"基本面: {_fmt_fundamentals(feat.get('fundamentals') or {})}")
    news = feat.get("news") or []
    if news:
        lines.append("新闻:")
        lines.extend(f"  · {n}" for n in news)
    else:
        lines.append("新闻: —")
    return "\n".join(lines)


def build_user(feats: dict, tickers: list[str]) -> str:
    blocks = "\n\n".join(render_block(feats[t]) for t in tickers if t in feats)
    header = (f"请把以下 {len([t for t in tickers if t in feats])} 只股票各归入 5 类之一,"
              f"严格按系统提示输出 JSON 数组(每股一项)。tickers: "
              f"{', '.join(t for t in tickers if t in feats)}\n\n=== 数据 ===\n\n")
    return header + blocks

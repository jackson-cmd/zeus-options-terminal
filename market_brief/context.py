"""决策树 tab 的证据组装 —— 纯函数, 不碰网络/LLM。

判断对象是**大盘 + 各个板块**(不是个股): 「芯片趋势很差、XLF 金融不错、科技一般」
这种粒度。数据源是 data/heatmap/etf_trends.json(25 板块 + 22 全球/指数, 每只带
3d/7d/1mo/3mo/1yr 五个周期)。

设计口径: 不要「看多/看空」标签(那会变成身份认同),
要**状态刻画 + 概率分布 + 机制 + 证伪条件**。所以这里只摆可观察的证据 + 做
**确定性**的趋势分类(不让模型去肉眼看数字), 判断和机制留给 brief.py 的模型。

关键指标(对应「价格只是六个输入之一」):
  · RSP vs SPY —— 等权 vs 市值加权。分辨「权重股拖累指数」还是「全面走弱」
  · HYG        —— 高收益债, 信用状况代理。信用不裂 = 更像板块性事件而非系统性
  · 板块多周期 —— 短周期(3d/7d)与中周期(1mo/3mo)方向是否一致 = 轮动还是趋势
  · 事件       —— 已公布(已知信息) vs 待公布(不确定性来源), 必须分开
"""
from __future__ import annotations

import datetime as _dt

PERIODS = ("r3d", "r7d", "r1mo", "r3mo")

# 指数/风格(etf_trends 的 GLOBAL 分类里)
INDEX_KEYS = {
    "SPY": "标普500", "QQQ": "纳指100", "DIA": "道指",
    "IWM": "罗素2000 小盘", "RSP": "标普等权",
}
# 宏观/跨资产(SECTOR 分类里, 但语义是宏观)
MACRO_KEYS = {
    "HYG": "高收益债(信用)", "TLT": "20年美债(利率)",
    "GLD": "黄金", "USO": "原油",
}


def _by_ticker(etf_trends) -> dict:
    out = {}
    for e in ((etf_trends or {}).get("items") or []):
        if isinstance(e, dict) and e.get("ticker"):
            out[str(e["ticker"]).upper()] = e
    return out


def _pick(e, keys=PERIODS) -> dict:
    return {k: (round(e[k], 2) if isinstance(e.get(k), (int, float)) else None) for k in keys}


# ── 板块趋势分类(确定性, 不靠模型肉眼看数字) ──────────────────────

def classify_sector(e) -> dict | None:
    """多周期收益 → 趋势状态标签。

    短(7d) × 中(1mo) 的四象限, 这是「回调 vs 转折」最直接的判据:
      中↑短↑ 趋势向上   ·  中↑短↓ 高位回调
      中↓短↑ 超跌反弹   ·  中↓短↓ 趋势向下
    幅度再决定强弱。返回 None = 数据不全, 不摆给模型。
    """
    if not isinstance(e, dict):
        return None
    s, m = e.get("r7d"), e.get("r1mo")
    if not isinstance(s, (int, float)) or not isinstance(m, (int, float)):
        return None
    if m >= 0 and s >= 0:
        state, note = ("趋势向上", "中短周期同向走强")
    elif m >= 0 and s < 0:
        state, note = ("高位回调", "中期仍强但短期回落")
    elif m < 0 and s >= 0:
        state, note = ("超跌反弹", "中期仍弱, 短期在修复 —— 未确认转势")
    else:
        state, note = ("趋势向下", "中短周期同向走弱")
    strength = max(abs(s), abs(m))
    grade = "强" if strength >= 5 else ("中" if strength >= 2 else "弱")
    return {"ticker": e.get("ticker"), "name": e.get("name", ""),
            "state": state, "grade": grade, "note": note, **_pick(e)}


def sector_table(etf_trends, sort_by: str = "r1mo") -> list:
    """全部板块的趋势表(按中周期排序 —— 判断板块强弱看中周期更稳)。"""
    rows = []
    for e in ((etf_trends or {}).get("items") or []):
        if not isinstance(e, dict) or e.get("category") != "SECTOR":
            continue
        c = classify_sector(e)
        if c:
            rows.append(c)
    rows.sort(key=lambda r: (r.get(sort_by) is None, -(r.get(sort_by) or 0)))
    return rows


# ── 广度: RSP vs SPY(真·等权 vs 市值加权) ─────────────────────────

def breadth(etf_trends) -> dict | None:
    """等权(RSP) vs 市值加权(SPY) 的多周期差。

    >0 = 等权跑赢 → 多数股票好于指数, 指数被少数权重股拖累(偏板块性)
    <0 = 指数跑赢 → 涨幅集中在少数权重股, 广度差(偏脆弱)
    """
    by = _by_ticker(etf_trends)
    rsp, spy = by.get("RSP"), by.get("SPY")
    if not rsp or not spy:
        return None
    out = {"rsp": _pick(rsp), "spy": _pick(spy), "spread": {}}
    for k in PERIODS:
        a, b = rsp.get(k), spy.get(k)
        out["spread"][k] = (round(a - b, 2)
                            if isinstance(a, (int, float)) and isinstance(b, (int, float))
                            else None)
    return out


def describe_breadth(b) -> str:
    if not b:
        return ""
    sp = b["spread"]
    bits = []
    for k, label in (("r7d", "7日"), ("r1mo", "1个月"), ("r3mo", "3个月")):
        v = sp.get(k)
        if v is None:
            continue
        bits.append(f"{label} {v:+.2f}pp")
    m = sp.get("r1mo")
    if m is None:
        shape = ""
    elif m > 0.5:
        shape = " → 等权跑赢, 多数股票好于指数(指数被少数权重股拖累, 偏板块性)"
    elif m < -0.5:
        shape = " → 指数跑赢等权, 涨幅集中在少数权重股(广度偏弱)"
    else:
        shape = " → 等权与指数接近, 无明显集中或分化"
    return "RSP(等权) 相对 SPY(市值加权): " + ", ".join(bits) + shape


# ── 事件 ──────────────────────────────────────────────────────────

def _in_window(date_str, today: str, days: int) -> bool:
    try:
        d = _dt.date.fromisoformat(str(date_str)[:10])
        t = _dt.date.fromisoformat(str(today)[:10])
    except Exception:
        return False
    return 0 <= (d - t).days <= days


def upcoming_earnings(cal, today: str, days: int = 7, limit: int = 20) -> list:
    out = []
    for e in ((cal or {}).get("earnings") or []):
        if not isinstance(e, dict) or not _in_window(e.get("date"), today, days):
            continue
        out.append({"ticker": str(e.get("ticker", "")).upper(),
                    "date": str(e.get("date"))[:10],
                    "session": e.get("session") or ""})
    out.sort(key=lambda x: (x["date"], x["ticker"]))
    return out[:limit]


def recent_econ(cal, today: str, back: int = 5, ahead: int = 7, limit: int = 15) -> dict:
    """已公布(带 actual = **已知信息**) vs 待公布(= **未来不确定性**)。
    这个区分是核心: 现状刻画可以用已知信息, 方向预测则没有信息优势。"""
    done, coming = [], []
    try:
        t = _dt.date.fromisoformat(str(today)[:10])
    except Exception:
        return {"done": [], "ahead": []}
    for e in ((cal or {}).get("econ") or []):
        if not isinstance(e, dict):
            continue
        try:
            d = _dt.date.fromisoformat(str(e.get("date"))[:10])
        except Exception:
            continue
        delta = (d - t).days
        row = {"title": e.get("title") or "", "date": d.isoformat(),
               "forecast": e.get("forecast"), "previous": e.get("previous"),
               "actual": e.get("actual")}
        if -back <= delta < 0 and e.get("actual") is not None:
            done.append(row)
        elif 0 <= delta <= ahead:
            coming.append(row)
    done.sort(key=lambda x: x["date"], reverse=True)
    coming.sort(key=lambda x: x["date"])
    return {"done": done[:limit], "ahead": coming[:limit]}


# ── 组装 ──────────────────────────────────────────────────────────

def _fmt_periods(p) -> str:
    parts = []
    for k, label in (("r3d", "3d"), ("r7d", "7d"), ("r1mo", "1mo"), ("r3mo", "3mo")):
        v = p.get(k)
        if v is not None:
            parts.append(f"{label} {v:+.2f}%")
    return " · ".join(parts)


def build_context(today: str, digest_summary: str = "", etf_trends=None, cal=None,
                  max_digest_chars: int = 6000, summary_day: str = "") -> str:
    """拼成喂给模型的证据包。缺哪块不写哪块(宁可少证据, 别让模型编)。"""
    parts = [f"【日期】{today}",
             "【判断对象】美股**大盘与各个板块**的当前状态(不是个股)。"]

    by = _by_ticker(etf_trends)

    idx = [f"- {tk}({label}): {_fmt_periods(_pick(by[tk]))}"
           for tk, label in INDEX_KEYS.items() if tk in by]
    if idx:
        parts.append("\n【指数与风格(多周期收益)】\n" + "\n".join(idx))

    b = breadth(etf_trends)
    if b:
        parts.append("\n【市场广度】\n" + describe_breadth(b))

    mac = [f"- {tk}({label}): {_fmt_periods(_pick(by[tk]))}"
           for tk, label in MACRO_KEYS.items() if tk in by]
    if mac:
        parts.append("\n【宏观/跨资产】\n" + "\n".join(mac)
                     + "\n(HYG 是信用状况代理: 信用不裂通常意味着压力是板块性的而非系统性的)")

    secs = sector_table(etf_trends)
    if secs:
        lines = [f"- {s['ticker']} {s['name']}: 【{s['state']}·{s['grade']}】{_fmt_periods(s)}"
                 for s in secs]
        parts.append("\n【各板块趋势(已按中周期分类, 状态标签是确定性计算的)】\n"
                     + "\n".join(lines))

    econ = recent_econ(cal, today)
    if econ["done"]:
        lines = []
        for e in econ["done"]:
            bits = [e["date"], e["title"]]
            if e["actual"] is not None:
                bits.append(f"实际 {e['actual']}")
            if e["forecast"] is not None:
                bits.append(f"预期 {e['forecast']}")
            lines.append("- " + " · ".join(str(x) for x in bits))
        parts.append("\n【最近已公布的宏观数据(已知信息)】\n" + "\n".join(lines))
    if econ["ahead"]:
        lines = [f"- {e['date']} {e['title']}"
                 + (f"(预期 {e['forecast']})" if e["forecast"] is not None else "")
                 for e in econ["ahead"]]
        parts.append("\n【未来宏观事件(尚未公布 = 不确定性来源)】\n" + "\n".join(lines))

    er = upcoming_earnings(cal, today)
    if er:
        lines = [f"- {e['date']} {e['ticker']}"
                 + (" 盘前" if e["session"] == "BMO" else (" 盘后" if e["session"] == "AMC" else ""))
                 for e in er]
        parts.append("\n【未来一周财报(只有权重股财报才有指数级影响)】\n" + "\n".join(lines))

    ds = (digest_summary or "").strip()
    if ds:
        # 标明新闻是哪天的 —— 当天总结未生成时会回退到前一天, 不能让模型当成今天的
        if summary_day and summary_day != today:
            head = f"\n【新闻总结(⚠️ 这是 {summary_day} 的, 今天的尚未生成 —— 判断时注意时滞)】\n"
        else:
            head = "\n【今日新闻(已由 digest 总结, 直接采用)】\n"
        parts.append(head + ds[:max_digest_chars])

    return "\n".join(parts)


def evidence_rows(today: str, etf_trends=None, cal=None) -> list:
    """前端「证据」面板 —— 与模型输出并列显示, 便于事后对账(哪条证据支撑哪个判断)。"""
    rows = []
    by = _by_ticker(etf_trends)
    for tk in ("SPY", "QQQ", "IWM"):
        if tk in by:
            p = _pick(by[tk])
            rows.append({"name": f"{tk} {INDEX_KEYS.get(tk,'')}",
                         "value": (f"{p['r1mo']:+.2f}%" if p.get("r1mo") is not None else "—"),
                         "note": f"7d {p['r7d']:+.2f}%" if p.get("r7d") is not None else ""})
    b = breadth(etf_trends)
    if b and b["spread"].get("r1mo") is not None:
        v = b["spread"]["r1mo"]
        rows.append({"name": "广度 等权−指数(1mo)", "value": f"{v:+.2f}pp",
                     "note": "等权跑赢=广度好" if v > 0 else "指数跑赢=集中度高"})
    if "HYG" in by:
        p = _pick(by["HYG"])
        rows.append({"name": "HYG 信用",
                     "value": (f"{p['r1mo']:+.2f}%" if p.get("r1mo") is not None else "—"),
                     "note": "信用未裂" if (p.get("r1mo") or 0) > -1 else "信用走弱"})
    econ = recent_econ(cal, today)
    if econ["ahead"]:
        rows.append({"name": "未来宏观事件", "value": f"{len(econ['ahead'])} 项",
                     "note": " · ".join(e["title"] for e in econ["ahead"][:3])})
    return rows

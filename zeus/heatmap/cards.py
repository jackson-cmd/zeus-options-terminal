"""Graph 卡片下方的三行信息 —— 纯函数组装, 不碰网络。

作者口径(2026-07-23): heatmap 顶层只留 股票/ETF, 把「SR 记录 / 多空 / AI」三块
收进 Graph 卡片, **分类各一行**; 想看全文点卡片进单票详情。

    📍 SR    今天的 S/R 触及告警(只当天, 旧的自动清库)
    ⚖️ 多空  narratives 的 net(一句话净判断)
    🚀 AI    ai_picks 的 verdict + news(选股理由 + 当天新闻总结)

每行都可能缺席(那只票今天没告警/没跑到多空/不在 AI 池)——缺就不画那一行,
不占位、不显示「无数据」, 免得 160 张卡全是空行。
"""
from __future__ import annotations

MAX_LEN = 140          # 单行最长字数, 超了截断(卡片是一览, 全文在详情页)


def _clip(s, n: int = MAX_LEN) -> str:
    s = " ".join(str(s or "").split())        # 压掉换行/连续空格
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_zone(z) -> str:
    """zone 在库里是 dict {lo,hi,avg,...} —— 直接丢给前端会渲染成 [object Object]。
    统一在这里格式化成 '44.51-44.81' 这样的可读串。"""
    if isinstance(z, dict):
        lo, hi = z.get("lo"), z.get("hi")
        if lo is not None and hi is not None:
            return f"{float(lo):.2f}-{float(hi):.2f}"
        avg = z.get("avg")
        return f"{float(avg):.2f}" if avg is not None else ""
    return str(z or "")


def sr_line(entries, ticker: str, today: str):
    """今天该票的 SR 告警 → {zone, dir, hhmm, why} 或 None。

    entries = sr_alert log store 的记录列表。只取**当天**(作者要求不看历史),
    同一票当天多条取最新那条(hhmm 最大)。
    """
    hit = None
    for e in (entries or ()):
        if not isinstance(e, dict):
            continue
        if str(e.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(e.get("day", ""))[:10] != today:
            continue
        if hit is None or str(e.get("hhmm", "")) >= str(hit.get("hhmm", "")):
            hit = e
    if hit is None:
        return None
    enr = hit.get("enrichment") or {}
    return {
        "hhmm": hit.get("hhmm", ""),
        "dir": hit.get("dir") or hit.get("kind") or "",
        "zone": fmt_zone(hit.get("zone")),
        "why": _clip(enr.get("reason") or ""),
    }


def narrative_line(items, ticker: str):
    """多空一句话净判断 → str 或 None。items = narratives store 某天的条目列表。"""
    for it in (items or ()):
        if isinstance(it, dict) and str(it.get("ticker", "")).upper() == ticker.upper():
            net = _clip(it.get("net") or "")
            return net or None
    return None


def ai_line(picks, ticker: str):
    """AI 选股结论 + 当天新闻总结 → {verdict, confidence, news, why} 或 None。

    picks = data/ai_picks/YYYY-MM-DD.json 的 picks 映射 {TICKER: {...}}。
    作者明确要「选股理由 + 新闻总结两个都要」→ why 取 tech(技术面理由),
    news 单独一段, 前端排成一行。
    """
    p = (picks or {}).get(ticker.upper())
    if not isinstance(p, dict):
        return None
    out = {
        "verdict": p.get("verdict") or "",
        "confidence": p.get("confidence"),
        "news": _clip(p.get("news") or ""),
        "why": _clip(p.get("tech") or p.get("fundamental") or ""),
    }
    return out if (out["verdict"] or out["news"] or out["why"]) else None


def build(ticker: str, today: str, sr_entries=None, narr_items=None, picks=None):
    """一张卡的三行。全缺 → None(前端不画这块)。"""
    sr = sr_line(sr_entries, ticker, today)
    nb = narrative_line(narr_items, ticker)
    ai = ai_line(picks, ticker)
    if not (sr or nb or ai):
        return None
    out = {}
    if sr:
        out["sr"] = sr
    if nb:
        out["nb"] = nb
    if ai:
        out["ai"] = ai
    return out


def _as_list(v):
    """bull/bear 在库里是 list, 但历史数据可能是 "['a','b']" 字符串 → 都归一成 list。"""
    if isinstance(v, list):
        return [str(x) for x in v if x]
    s = str(v or "").strip()
    if not s or s == "[]":
        return []
    try:
        import ast
        out = ast.literal_eval(s)
        return [str(x) for x in out if x] if isinstance(out, list) else [s]
    except Exception:
        return [s]


def narrative_full(items, ticker: str):
    """多空全文 → {bull:[...], bear:[...], net} 或 None。就地展开用, 不截断。"""
    for it in (items or ()):
        if isinstance(it, dict) and str(it.get("ticker", "")).upper() == ticker.upper():
            bull, bear = _as_list(it.get("bull")), _as_list(it.get("bear"))
            net = " ".join(str(it.get("net") or "").split())
            if bull or bear or net:
                return {"bull": bull, "bear": bear, "net": net,
                        "asof": it.get("asof", "")}
    return None


def ai_full(picks, ticker: str):
    """AI 选股全文 → {verdict,confidence,summary,tech,fundamental,news} 或 None。"""
    p = (picks or {}).get(ticker.upper())
    if not isinstance(p, dict):
        return None
    keep = ("verdict", "confidence", "summary", "tech", "fundamental", "news")
    out = {k: p.get(k) for k in keep if p.get(k) not in (None, "")}
    return out or None


def profile_text(profiles, ticker: str):
    """公司简介 → {company, summary, filing_date} 或 None。"""
    p = (profiles or {}).get(ticker.upper())
    if isinstance(p, dict) and p.get("summary"):
        return {"company": p.get("company", ""),
                "summary": " ".join(str(p["summary"]).split()),
                "filing_date": p.get("filing_date", "")}
    return None


def build_full(ticker: str, today: str, sr_entries=None, narr_items=None,
               picks=None, profiles=None) -> dict:
    """点卡片就地展开的全文包(不截断)。空块省略。"""
    out = {"ticker": ticker.upper()}
    sr = sr_line(sr_entries, ticker, today)      # SR 本就是一句话, 直接复用
    nb = narrative_full(narr_items, ticker)
    ai = ai_full(picks, ticker)
    pf = profile_text(profiles, ticker)
    if sr:
        out["sr"] = sr
    if nb:
        out["nb"] = nb
    if ai:
        out["ai"] = ai
    if pf:
        out["profile"] = pf
    return out


def build_all(tickers, today: str, sr_entries=None, narr_items=None, picks=None) -> dict:
    """{ticker: {sr?, nb?, ai?}} —— 只收有内容的票。"""
    out = {}
    for t in (tickers or ()):
        c = build(t, today, sr_entries, narr_items, picks)
        if c:
            out[t] = c
    return out

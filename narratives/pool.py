"""每票组装「近30天定向新闻池」: finviz_ai 库 + benzinga digest。

finviz 规矩(只作用于本池, 不动 finviz_ai 原始库):
  1. 只留有方向的: sentiment ∈ {+1,-1}, 丢中性(0)。AI 摘要永远保留。
  2. 去掉「与新闻无关的标题」: 荐股/排行榜/「是否值得买」/纯涨跌无原因等(正则)。
  3. benzinga.com 去重(已有 benzinga 库, finviz 里该源直接丢)。
  4. 去重(同 id / 同标题), 近30天。
benzinga: 一遍扫近30天 digest 按 tickers 收 title+teaser, 限 N 条; 不套情绪规则(Haiku 归类)。
"""
from __future__ import annotations
import sys, os, re, hashlib, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benzinga_digest" / "src"))
from benzinga_mcp import digest_store as DS          # noqa: E402
from benzinga_mcp import finviz_ai_store as FV        # noqa: E402

BENZINGA_DAYS = 60          # 2026-06 拉长到 2 个月, 覆盖近 1-2 月主题窗口
BENZINGA_CAP = 25            # 加大每票 benzinga 上限, 配合正文抽取
BENZINGA_BODY_CHARS = 600   # 每篇 benzinga 保留正文前 N 字符(paras[:3] 拼接)
FINVIZ_CAP = 25
FINVIZ_SNAPSHOTS = 5   # 最近几张快照合并(每张含~2周新闻; 5 张 ≈ 2 个月)

# 「与公司新闻无关」的标题模式 → 直接去掉(高精度, 宁可放过也别误杀实质消息)
_JUNK_PATTERNS = [
    r"^\s*\d+\s+(stocks?|things?|reasons?|charts?|ways?|etfs?|picks?)\b",   # "3 Stocks to..."
    r"\bstocks?\s+to\s+(buy|watch|sell|consider|avoid)\b",
    r"\b(is|are)\s+.+\s+a\s+(buy|sell|good\s+buy|smart\s+buy)\b",
    r"\bshould\s+you\s+(buy|sell|invest)\b",
    r"\b(top|best|worst)\s+\d*\s*(stocks?|blue.?chip|dividend|growth|value)\b",
    r"\b(motley\s+fool|zacks\b.*\brank|million(aire)?s?\b)",
    r"\bbull\s+vs\.?\s+bear\b",
    r"\bcould\s+(make|turn).+(millionaire|rich|\$\d)",
    r"\bprediction\b.*\b20\d\d\b",
]
_JUNK_RE = re.compile("|".join(_JUNK_PATTERNS), re.I)


def is_junk_title(title: str) -> bool:
    return bool(title) and bool(_JUNK_RE.search(title))


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url or "")
    return (m.group(1) if m else "").lower().replace("www.", "")


def _iso_date(s: str) -> str:
    """finviz hhmm → 'YYYY-MM-DD' 尽力解析; 失败回原串。"""
    if not s:
        return ""
    m = re.search(r"([A-Za-z]{3})-(\d{1,2})-(\d{2})", s)        # Jun-15-26
    if m:
        try:
            return datetime.datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                                              "%b-%d-%y").strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.match(r"(\d{2})-(\d{2})\b", s)                       # MM-DD (AI hhmm, 假设今年)
    if m:
        yr = datetime.date.today().year
        return f"{yr}-{m.group(1)}-{m.group(2)}"
    return s


def shape_finviz_entry(entry, seen=None):
    """一条 finviz_ai 库条目({ticker, ai, news}) → 池形状 {"ai":…, "news":[…]}。

    finviz_pool(跨快照合并)和 search_pool(单条实时注入)共用这一份口径。
    seen: 标题去重集合(跨快照合并时由调用方传入并复用); None → 本次调用内去重。
    """
    if seen is None:
        seen = set()
    ai_out = None
    ai = entry.get("ai")
    if ai and not is_junk_title(ai.get("title_en", "")):
        ai_out = {
            "title_en": ai.get("title_en"), "title_cn": ai.get("title_cn"),
            "sent": ai.get("sentiment"), "catalyst": ai.get("catalyst"),
            "summary": " ".join(ai.get("paras_en") or [])[:400],
            "link": ai.get("link"),
        }
    news = []
    for n in (entry.get("news") or []):
        if n.get("sentiment") in (0, None):            # 规则1: 丢中性
            continue
        if _domain(n.get("link")) == "benzinga.com":   # 规则3: benzinga 去重
            continue
        title = n.get("title_en") or ""
        if is_junk_title(title):                        # 规则2: 非新闻标题
            continue
        key = title[:60]
        if key in seen:
            continue
        seen.add(key)
        news.append({
            "date": _iso_date(n.get("hhmm")), "raw": n.get("hhmm"),
            "title_en": title, "title_cn": n.get("title_cn"),
            "sent": n.get("sentiment"), "link": n.get("link"),
        })
    return {"ai": ai_out, "news": news}


def finviz_pool(tickers):
    out = {t: {"ai": None, "news": []} for t in tickers}
    seen = {t: set() for t in tickers}
    for d in FV.list_days()[:FINVIZ_SNAPSHOTS]:
        for it in (FV.load_day(d) or []):
            t = it.get("ticker")
            if t not in out:
                continue
            shaped = shape_finviz_entry(it, seen[t])
            if shaped["ai"] and out[t]["ai"] is None:   # ai 取「第一个非 junk 的」
                out[t]["ai"] = shaped["ai"]
            out[t]["news"].extend(shaped["news"])
    for t in out:
        out[t]["news"] = out[t]["news"][:FINVIZ_CAP]
    return out


def benzinga_pool(tickers):
    tset = set(tickers)
    out = {t: [] for t in tickers}
    for d in DS.list_days()[:BENZINGA_DAYS]:
        for a in (DS.load_day(d) or []):
            tk = [x for x in (a.get("tickers") or []) if x in tset]
            if not tk:
                continue
            title = a.get("title") or ""
            if is_junk_title(title):
                continue
            teaser = a.get("teaser") or ""
            ps = a.get("paras") or []
            if not teaser and ps:
                teaser = (ps[0].get("en") or ps[0].get("cn") or "")
            # 拼正文前 3 段 → 让 Haiku 读到实质细节(数字/原因/关键人物), 不只标题
            body_parts = []
            for p in ps[:3]:
                seg = (p.get("en") or p.get("cn") or "").strip()
                if seg:
                    body_parts.append(seg)
            body = " ".join(body_parts)[:BENZINGA_BODY_CHARS]
            row = {"date": d, "title": title, "teaser": (teaser or "")[:180],
                   "body": body}
            for t in tk:
                if len(out[t]) < BENZINGA_CAP:
                    out[t].append(row)
    return out


def build_pools(tickers):
    """返回 {ticker: {ai, finviz:[...], benzinga:[...]}}。"""
    fv = finviz_pool(tickers)
    bz = benzinga_pool(tickers)
    return {t: {"ai": fv[t]["ai"], "finviz": fv[t]["news"], "benzinga": bz[t]}
            for t in tickers}


def pool_hash(pool) -> str:
    """池内容指纹(增量用): AI + finviz + benzinga + external 标题, 排序。"""
    parts = []
    if pool.get("ai"):
        parts.append("ai:" + (pool["ai"].get("title_en") or ""))
    parts += ["f:" + (n.get("title_en") or "") for n in pool.get("finviz", [])]
    parts += ["b:" + (b.get("title") or "") for b in pool.get("benzinga", [])]
    return hashlib.md5("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def has_news(pool) -> bool:
    return bool(pool.get("ai") or pool.get("finviz") or pool.get("benzinga"))

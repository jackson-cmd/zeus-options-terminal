"""按需单票「深度新闻池」: benzinga 全文 + primary 分层选池。

与 pool.py(每日 147 票批量)的分工:
  pool.py        每日批量 → 正文截 600 字、按日期硬取前 25 篇  → 省 token
  search_pool.py 按需单票 → 全文、primary 分层选池、取 30 篇   → 吃 token 换质量
                 (实测单票 25 篇全文 ≈ 12K tok, 单票尺度上截断没省下任何东西)

为什么分层: benzinga 的 tickers_full[].primary 标注「这篇文章写的就是这只票」。
实测 AVAV 池子头部被 3 份重复的《10 Industrials Stocks Whale Activity》(提及 8 票的
榜单)占满, RIVN 近30天 70 篇里约一半是顺带提及。按 primary 分层 + 丢弃提及 >5 票的
榜单, 头部立刻变成「$1.7B 增发」「Q2 交付超预期」这种真正的公司消息。

⚠️ 只读 digest_store, 绝不写(那是 digest daemon 的地盘)。
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benzinga_digest" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benzinga_mcp import digest_store as DS   # noqa: E402
import pool as _pool                          # noqa: E402

DAYS = 60          # 与每日批一致(作者确认沿用 60 天, 非 30)
CAP = 30           # 每票取几篇(批量是 25)
BODY_CHARS = 3000  # 单篇正文上限(兜底异常长文; 实测中位数 2941 字)
MAX_MENTION = 5    # 非 primary 文章的提及票数上限, 超过 = 榜单 → 丢


def _body(article) -> str:
    """全文拼接(en 优先, 回退 cn), 按 BODY_CHARS 截断。"""
    parts = []
    for p in (article.get("paras") or []):
        seg = (p.get("en") or p.get("cn") or "").strip()
        if seg:
            parts.append(seg)
    return " ".join(parts)[:BODY_CHARS]


def collect_articles(ticker, days=DAYS):
    """扫近 days 天 digest 收该票文章 → [{date,title,teaser,body,primary,ntk}]。"""
    tk = (ticker or "").strip().upper()
    if not tk:
        return []
    rows = []
    for d in DS.list_days()[:days]:
        for a in (DS.load_day(d) or []):
            tf = a.get("tickers_full") or []
            me = [x for x in tf if str(x.get("name", "")).upper() == tk]
            if me:
                primary = bool(me[0].get("primary"))
            else:
                # tickers_full 缺失 → 回退 tickers 数组(拿不到 primary 标注)
                if tk not in [str(x).upper() for x in (a.get("tickers") or [])]:
                    continue
                primary = False
            title = a.get("title") or ""
            if not title:
                continue
            teaser = a.get("teaser") or ""
            ps = a.get("paras") or []
            if not teaser and ps:
                teaser = (ps[0].get("en") or ps[0].get("cn") or "")
            rows.append({"date": d, "title": title, "teaser": (teaser or "")[:180],
                         "body": _body(a), "primary": primary,
                         "ntk": len(a.get("tickers") or [])})
    return rows


def dedupe(rows):
    """同题(前60字符)只留正文最长的那篇(实测有重复 wire 稿)。"""
    best = {}
    for r in rows:
        k = r["title"][:60]
        if k not in best or len(r["body"]) > len(best[k]["body"]):
            best[k] = r
    return list(best.values())


def _rank_rows(rows, cap=CAP):
    """分层选池 → [{date,title,teaser,body}] 按日期新→旧。

    Tier1 = primary 文章; Tier2 = 非 primary 且提及 <=MAX_MENTION 票。
    先用 Tier1 填, 填不满才用 Tier2 补。丢弃 junk 标题和提及 >MAX_MENTION 的榜单。
    """
    rows = [r for r in dedupe(rows) if not _pool.is_junk_title(r["title"])]
    t1 = sorted([r for r in rows if r["primary"]],
                key=lambda r: r["date"], reverse=True)
    t2 = sorted([r for r in rows if not r["primary"] and r["ntk"] <= MAX_MENTION],
                key=lambda r: r["date"], reverse=True)
    picked = (t1 + t2)[:cap]
    picked.sort(key=lambda r: r["date"], reverse=True)   # 呈现按时间线
    return [{"date": r["date"], "title": r["title"], "teaser": r["teaser"],
             "body": r["body"]} for r in picked]


def rank_articles(ticker, days=DAYS, cap=CAP):
    """扫库 + 分层选池 → 与 pool.benzinga_pool 同形状的行。"""
    return _rank_rows(collect_articles(ticker, days), cap=cap)


def build_deep_pool(ticker, finviz_entry=None):
    """→ {"ai":…, "finviz":[…], "benzinga":[…]}, 与 pool.build_pools[t] 同形状
    (所以 haiku._block 和 build._evidence 零改动)。

    finviz_entry=None → 读本地 finviz_ai 库(票池内的票)
    finviz_entry 给了 → 用服务端实时抓的条目(票池外的票, 库里没有)
    """
    tk = (ticker or "").strip().upper()
    if finviz_entry is not None:
        fv = _pool.shape_finviz_entry(finviz_entry)
    else:
        fv = _pool.finviz_pool([tk])[tk]
    return {"ai": fv["ai"], "finviz": fv["news"], "benzinga": rank_articles(tk)}

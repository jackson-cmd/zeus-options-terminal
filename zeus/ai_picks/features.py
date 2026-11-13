"""每票装配 AI 选股特征 blob(全用当天最新数据)。

来源:
  · 价格 / RSI(14) / EMA20-50-100-200(+价差%) / 20-60d 表现  ← data/daily_ohlc.parquet(一次读)
  · S/R 最近上下位 + 是否正踩 + score 方向 / 期权结构(PCR/MaxPain/GEX/大单)  ← ta_screener results.json
  · 基本面 PE/增长/毛利/ROE + 表现 + 分析师 Recom/目标价  ← finviz pip(按天缓存)
  · 近期+历史新闻 bullets  ← narratives.pool.build_pools(digest + finviz_ai)
  · earnings 临近  ← results.json
  · Barchart 榜单命中(作者账号里的 saved-screener 信号池)  ← data/barchart_screeners.json

纯函数: build_features(tickers) → {ticker: feature_dict}。任何一维缺失只留空, 不抛。
"""
from __future__ import annotations
import sys, json, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OHLC_PARQUET = REPO / "data" / "daily_ohlc.parquet"
UNIVERSE_FILE = REPO / "data" / "ta_screener_universe.txt"
RESULTS_JSON = REPO / "data" / "ta_screener" / "results.json"
FINVIZ_CACHE_DIR = REPO / "data" / "ai_picks"
BARCHART_JSON = REPO / "data" / "barchart_screeners.json"


# ── universe ──────────────────────────────────────────────────────────────
def parse_universe() -> list[str]:
    """data/ta_screener_universe.txt (EXCHANGE:TICKER 逗号分隔) → 去交易所前缀的 160 只。"""
    try:
        raw = UNIVERSE_FILE.read_text(encoding="utf-8")
    except Exception:
        return []
    out, seen = [], set()
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        t = tok.split(":")[-1].strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── Barchart 榜单 (作者账号 saved-screener 信号池, consolidate.py 产出) ─────────
def load_barchart(path: str | Path | None = None) -> dict:
    """读 data/barchart_screeners.json。失败/缺失 → 空骨架, 不抛。path 可注入(测试用)。"""
    p = Path(path) if path else BARCHART_JSON
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {"screeners": [], "by_ticker": {}, "ai_universe_add": []}
    if not isinstance(d, dict):
        return {"screeners": [], "by_ticker": {}, "ai_universe_add": []}
    d.setdefault("screeners", [])
    d.setdefault("by_ticker", {})
    d.setdefault("ai_universe_add", [])
    return d


def build_barchart(tickers: list[str], bc: dict | None = None) -> dict[str, list[dict]]:
    """每票 → 命中的 barchart screener 语义列表 [{name,slug,category,category_label,desc}, ...]。

    by_ticker 的值是 screener slug, 用 screeners 列表建 slug→screener 索引拿类别信息。
    不在任何榜单的票 → 空列表。bc 可注入(测试免读盘)。
    """
    bc = bc if bc is not None else load_barchart()
    by_slug: dict[str, dict] = {}
    for sc in bc.get("screeners") or []:
        slug = sc.get("slug")
        if slug:
            by_slug[slug] = sc
    by_ticker = bc.get("by_ticker") or {}
    out: dict[str, list[dict]] = {}
    for t in tickers:
        hits: list[dict] = []
        for slug in by_ticker.get(t) or []:
            sc = by_slug.get(slug)
            if not sc:
                continue
            hits.append({
                "name": sc.get("name") or slug,
                "slug": slug,
                "category": sc.get("category") or "",
                "category_label": sc.get("category_label") or "",
                "desc": sc.get("desc") or "",
            })
        out[t] = hits
    return out


def effective_universe(base: list[str] | None = None, bc: dict | None = None) -> list[str]:
    """parse_universe() ∪ barchart ai_universe_add, 去重保序(base 在前)。

    只在内存合并, **不修改 data/ta_screener_universe.txt 文件**。bc/base 可注入。
    """
    base = base if base is not None else parse_universe()
    bc = bc if bc is not None else load_barchart()
    out, seen = [], set()
    for t in list(base) + list(bc.get("ai_universe_add") or []):
        tt = (t or "").strip().upper()
        if tt and tt not in seen:
            seen.add(tt)
            out.append(tt)
    return out


# ── results.json (S/R + 期权 + earnings) ────────────────────────────────────
def load_results_rows() -> dict[str, dict]:
    try:
        d = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = d.get("rows") or []
    return {r.get("ticker"): r for r in rows if r.get("ticker")}


def _sr_from_row(row: dict) -> dict:
    """从 results.json 行抽 S/R + score 方向 + 期权结构 + earnings(全部可选)。"""
    out = {}
    near = row.get("nearest") or {}
    up, dn = near.get("up") or {}, near.get("dn") or {}
    if up.get("px"):
        out["sr_up"] = {"px": round(up["px"], 2), "dist_pct": up.get("dist"), "conf": up.get("conf")}
    if dn.get("px"):
        out["sr_dn"] = {"px": round(dn["px"], 2), "dist_pct": dn.get("dist"), "conf": dn.get("conf")}
    touch = row.get("touch") or {}
    if touch.get("kind"):
        out["touching"] = {"sup": "支撑", "res": "阻力"}.get(touch["kind"], touch["kind"])
        out["touch_conf"] = touch.get("conf")
    sd = row.get("score_detail") or {}
    if sd:
        out["score"] = sd.get("score")
        out["score_dir"] = {"bull": "看多", "bear": "看空"}.get(sd.get("dir"), sd.get("dir"))
    om = row.get("opt_metrics") or {}
    if om:
        out["opt"] = {
            "pcr_oi": om.get("pcr_oi"), "pcr_vol": om.get("pcr_vol"),
            "max_pain_pct": om.get("max_pain_pct"), "gex_flip_pct": om.get("gex_flip_pct"),
        }
    oa = row.get("oa_flow") or {}
    if oa and oa.get("qualified"):
        out["opt_flow_net_pct"] = oa.get("net_pct")   # 大单净多空 %(正=偏多)
    ob = row.get("opt_bias") or {}
    if ob.get("total") is not None:
        out["opt_bias"] = round(ob["total"], 1)        # 综合期权偏置分(正=偏多)
    earn = row.get("earnings") or {}
    if earn.get("days") is not None:
        out["earnings_days"] = earn.get("days")
    if row.get("pos52") is not None:
        out["pos52"] = row.get("pos52")                # 52 周区间位置 0-100
    return out


# ── 技术指标 (parquet 现算) ─────────────────────────────────────────────────
def _rsi(closes, period: int = 14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(deltas)):     # Wilder 平滑
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 1)


def _ema(closes, span: int):
    if len(closes) < span:
        return None
    k = 2 / (span + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def build_tech(tickers: list[str]) -> dict[str, dict]:
    """一次读 parquet, 逐票算 price/RSI/EMA20-50-100-200(+价差%)/20-60d 表现。"""
    try:
        import pandas as pd
        df = pd.read_parquet(OHLC_PARQUET, columns=["Close", "ticker"])
    except Exception:
        return {}
    out = {}
    want = set(tickers)
    for tk, g in df[df["ticker"].isin(want)].groupby("ticker", sort=False):
        closes = g.sort_index()["Close"].astype(float).tolist()
        if len(closes) < 30:
            continue
        price = closes[-1]
        t = {"price": round(price, 2), "rsi14": _rsi(closes)}
        for span in (20, 50, 100, 200):
            e = _ema(closes, span)
            if e:
                t[f"ema{span}"] = round(e, 2)
                t[f"ema{span}_gap_pct"] = round((price / e - 1) * 100, 1)  # 现价高于均线 %(正=站上)
        if len(closes) > 20:
            t["perf20d_pct"] = round((price / closes[-21] - 1) * 100, 1)
        if len(closes) > 60:
            t["perf60d_pct"] = round((price / closes[-61] - 1) * 100, 1)
        out[tk] = t
    return out


# ── 基本面 / 分析师 (finviz pip, 按天缓存) ────────────────────────────────────
_FV_KEEP = ["P/E", "Forward P/E", "PEG", "EPS growth next Y", "EPS next 5Y",
            "Sales Q/Q", "EPS Q/Q", "Gross Margin", "Profit Margin", "ROE",
            "Debt/Eq", "Short Float", "Recom", "Target Price", "Price",
            "Perf Week", "Perf Month", "Perf Quarter", "Perf Year", "52W High", "52W Low"]


def build_fundamentals(tickers: list[str], date: str) -> dict[str, dict]:
    """finviz.get_stock 逐票拉基本面+分析师; 命中当天缓存直接读。失败留空。"""
    FINVIZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = FINVIZ_CACHE_DIR / f"finviz_cache_{date}.json"
    cache = {}
    if cache_f.exists():
        try:
            cache = json.loads(cache_f.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    try:
        import finviz
    except Exception:
        return cache
    changed = False
    for t in tickers:
        if t in cache:
            continue
        try:
            d = finviz.get_stock(t)
            cache[t] = {k: d.get(k) for k in _FV_KEEP if d.get(k) not in (None, "-", "")}
            changed = True
        except Exception:
            cache[t] = {}      # 缓存空值避免反复重试(如 ETF)
            changed = True
    if changed:
        try:
            cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return cache


# ── 新闻池 (复用 narratives.pool) ────────────────────────────────────────────
def build_news(tickers: list[str], cap: int = 8) -> dict[str, dict]:
    """{ticker: {ai, finviz:[..], benzinga:[..]}} → 压成精简 bullets(限 cap 条/侧)。"""
    try:
        sys.path.insert(0, str(REPO / "narratives"))
        import pool as _pool
        pools = _pool.build_pools(tickers)
    except Exception:
        return {}
    out = {}
    for t in tickers:
        p = pools.get(t) or {}
        items = []
        ai = p.get("ai")
        if ai and ai.get("title_en"):
            tag = {1: "利好", -1: "利空", 0: "中性"}.get(ai.get("sent"), "")
            items.append(f"[finviz AI·{tag}] {ai.get('title_cn') or ai.get('title_en')}")
        for n in (p.get("finviz") or [])[:cap]:
            tag = {1: "利好", -1: "利空"}.get(n.get("sent"), "")
            items.append(f"[{n.get('date','')}·{tag}] {n.get('title_en')}")
        for b in (p.get("benzinga") or [])[:cap]:
            items.append(f"[{b.get('date','')}] {b.get('title')} — {(b.get('teaser') or '')[:120]}")
        if items:
            out[t] = items[: cap * 2]
    return out


# ── 组装 ────────────────────────────────────────────────────────────────────
def build_features(tickers: list[str], date: str | None = None) -> dict[str, dict]:
    """→ {ticker: {ticker, sector, tech{}, structure{}, fundamentals{}, news[], barchart[]}}。
    有价格数据(parquet)就算一票, 其余维度缺失只留空 —— 保证 160 只都能分类。"""
    date = date or datetime.date.today().isoformat()
    rows = load_results_rows()
    tech = build_tech(tickers)
    fund = build_fundamentals(tickers, date)
    news = build_news(tickers)
    barchart = build_barchart(tickers)
    out = {}
    for t in tickers:
        if t not in tech:            # 没有价格历史的票直接跳过(ETF 兜底新数据也可能缺)
            continue
        row = rows.get(t) or {}
        out[t] = {
            "ticker": t,
            "sector": row.get("sector") or "",
            "industry": row.get("industry") or "",
            "tech": tech.get(t, {}),
            "structure": _sr_from_row(row),
            "fundamentals": fund.get(t, {}),
            "news": news.get(t, []),
            "barchart": barchart.get(t, []),
        }
    return out

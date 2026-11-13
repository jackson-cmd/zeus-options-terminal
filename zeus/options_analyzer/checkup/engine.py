"""编排层——把各纯函数模块拼成开单前体检 + 出场判定的成品 JSON。

server 端点只做 IO(抓链/报价/财报/取持仓)然后调这里。本层也尽量少 IO:
只调 prices(读缓存)。
"""
from __future__ import annotations

from . import concentration, correlation, exit_advisor, mirror, prices, rules, trend

_MKT = rules._MARKET_PROXY


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def _net_debit_dollars(legs) -> float:
    """每张价差的资金占用(≈max_loss for 借方),用作集中度权重。"""
    try:
        per_share = sum(float(l.get("entry", 0)) * float(l.get("qty", 0)) for l in (legs or []))
        return abs(per_share) * 100
    except Exception:
        return 0.0


def pretrade(tickers_raw, proposed_spread=None, book_strategies=None, nlv=None) -> dict:
    """开单前体检主入口。

    tickers_raw: 'MAG7' / 'AAPL MSFT' / [..];proposed_spread(选填):
      {ticker, direction, thesis_type, dte, earnings_in_window, spot, iv,
       net_price, legs:[{type,strike,qty,mid|bid|ask,delta}]}
    book_strategies: 现有持仓 [{underlying, legs:[{type,strike,qty,entry}]}]。
    """
    from . import price_check  # 延迟导入(避免循环)

    cands = prices.expand_tickers(tickers_raw)
    book_strategies = book_strategies or []
    book_weight: dict[str, float] = {}
    for s in book_strategies:
        u = (s.get("underlying") or "").upper()
        if not u:
            continue
        book_weight[u] = book_weight.get(u, 0.0) + _net_debit_dollars(s.get("legs"))
    book_tickers = sorted(book_weight.keys())

    allt = list(dict.fromkeys(cands + book_tickers + [_MKT]))
    df = prices.returns_frame(allt)
    have = set(df.columns)

    # 候选 × book 联动表 + 候选对 SPY beta
    rows, betas = [], []
    for c in cands:
        if c not in have:
            rows.append({"candidate": c, "available": False})
            continue
        vs = []
        for b in book_tickers:
            if b == c or b not in have:
                continue
            sub = df[[c, b]].dropna()
            vs.append({"ticker": b,
                       "pearson": correlation.pearson(sub[c], sub[b]),
                       "tail": correlation.lower_tail(sub[c], sub[b], 0.05),
                       "weight": round(book_weight.get(b, 0.0))})
        beta_c = None
        if _MKT in have:
            sub = df[[c, _MKT]].dropna()
            beta_c = correlation.beta(sub[c], sub[_MKT])
            if beta_c is not None:
                betas.append(beta_c)
        vs = sorted(vs, key=lambda x: -(x["pearson"] if x["pearson"] is not None else -9))
        pears = [v["pearson"] for v in vs if v["pearson"] is not None]
        tails = [v["tail"] for v in vs if v["tail"] is not None]
        rows.append({"candidate": c, "available": True, "beta": _r(beta_c),
                     "vs_book": vs,
                     "max_pearson": max(pears) if pears else None,
                     "max_tail": max(tails) if tails else None})

    # 候选集内部相关(买全套 MAG7 = 一个 beta 注)
    cc = [c for c in cands if c in have]
    internal = None
    if len(cc) >= 2:
        ps = []
        for i in range(len(cc)):
            for j in range(i + 1, len(cc)):
                sub = df[[cc[i], cc[j]]].dropna()
                p = correlation.pearson(sub[cc[i]], sub[cc[j]])
                if p is not None:
                    ps.append(p)
        internal = sum(ps) / len(ps) if ps else None

    all_pairs = [v for r in rows if r.get("vs_book") for v in r["vs_book"]]
    conc = concentration.aggregate(
        all_pairs,
        candidate_beta=(sum(betas) / len(betas) if betas else None),
        candidate_internal=internal,
    )

    # 板块重仓 + 高度联动(给错误镜子,ticker-only 也能触发——治"太松")
    try:
        imap = prices._D.industry_map()   # 行业(比 sector 细,如 Communication Equipment——治"8个科技股凑一起"的噪音)
    except Exception:
        imap = {}
    book_inds: dict[str, list] = {}
    for b in book_tickers:
        ind = imap.get(b) or ""
        if ind:
            book_inds.setdefault(ind, []).append(b)
    high_corr = []
    for r in rows:
        if not r.get("available"):
            continue
        for v in (r.get("vs_book") or []):
            pe, ta = v.get("pearson"), v.get("tail")
            if (pe is not None and pe >= 0.45) or (ta is not None and ta >= 0.40):
                high_corr.append({"candidate": r["candidate"], "ticker": v["ticker"],
                                  "pearson": pe, "tail": ta})
    high_corr.sort(key=lambda x: -(x["pearson"] if x["pearson"] is not None else 0))
    high_corr = high_corr[:6]
    industry_hits = []
    for c in cands:
        ind = imap.get(c) or ""
        held = [t for t in book_inds.get(ind, []) if t != c]
        if ind and held:
            industry_hits.append({"candidate": c, "industry": ind, "holdings": held})

    # 价格合理性(给了拟开价差才算)
    price = None
    if proposed_spread and proposed_spread.get("legs"):
        price = price_check.evaluate(
            proposed_spread["legs"], proposed_spread.get("net_price"),
            proposed_spread.get("spot"), proposed_spread.get("iv"),
            proposed_spread.get("dte"))

    # 追高检测(主候选)
    primary = (proposed_spread or {}).get("ticker") or (cands[0] if cands else None)
    if primary:
        primary = primary.upper()
    trend_ob = {}
    if primary:
        closes = prices.load_closes([primary])
        if primary in closes:
            trend_ob = trend.overbought(closes[primary])

    # 错误镜子
    mrules = rules.load_mistake_rules()
    _bm = mrules.get("by_mistake") or {}
    common_mistakes = sorted(([{"label": k, **v} for k, v in _bm.items()]),
                             key=lambda x: x.get("net_pnl", 0))[:6]
    same_ct = sum(1 for s in book_strategies if (s.get("underlying") or "").upper() == (primary or ""))
    book_ctx = {"open_count": len(book_strategies), "same_ticker_count": same_ct, "tickers": book_tickers}
    corr_ctx = {"max_pearson": max([r.get("max_pearson") for r in rows
                                    if r.get("max_pearson") is not None], default=None),
                "high_corr": high_corr, "industry_hits": industry_hits}
    proposed_for_mirror = dict(proposed_spread or {})
    proposed_for_mirror.setdefault("ticker", primary)
    flags = mirror.evaluate(proposed_for_mirror, book_ctx, corr_ctx, price, trend_ob, mrules, nlv,
                            candidates=cands)

    # 顶部硬违规(给 alert 红条)
    hard = [f for f in flags if f["severity"] == "red"]

    return {
        "tickers": cands,
        "primary": primary,
        "comovement": {"book": book_tickers, "rows": rows, "internal": _r(internal)},
        "concentration": conc,
        "price": price,
        "trend": trend_ob or None,
        "mirror": flags,
        "hard_violations": hard,
        "common_mistakes": common_mistakes,
        "industries": {"candidate": {c: (imap.get(c) or "") for c in cands}, "book": book_inds},
    }


def position_verdict(unrealized_pnl, max_profit, dte=None, pnl_pct=None,
                     regime=None) -> dict:
    """单个持仓出场判定(供 server alert + harness;前端 ckExitVerdict 同口径)。
    口径=regime 自适应止盈(牛 70-80% / 震荡 50-60%)、借方价差不止损。"""
    return exit_advisor.evaluate(unrealized_pnl, max_profit, dte, pnl_pct, regime=regime)

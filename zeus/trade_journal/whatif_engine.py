"""每单盘中 P&L what-if: MFE/MAE/兑现率。读 option_prices 快照(经 price_tracker),
裁剪到持仓窗口。无LLM。覆盖不足的单显式标 has_intraday=False。"""
from __future__ import annotations
import datetime
from typing import Optional
import price_tracker


def _to_dt(s: str) -> Optional[datetime.datetime]:
    if not s:
        return None
    t = s.replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        try:
            d = datetime.datetime.fromisoformat(t[:19] + "+00:00")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def _entry_mtm(legs: list[dict]) -> float:
    v = 0.0
    for l in legs:
        sign = 1 if (l.get("action") or "").startswith("Buy") else -1
        v += sign * float(l.get("price") or 0) * 100 * float(l.get("quantity") or 1)
    return v


def _expiry_dt(legs: list[dict]) -> Optional[datetime.datetime]:
    """价差到期(取各腿最晚 exp,格式 YYMMDD 或 YYYY-MM-DD);返回该日 23:59:59 UTC,
    用于截断"平仓后→到期"区段。"""
    best = None
    for l in legs or []:
        e = l.get("exp")
        if not e:
            continue
        es = str(e)
        d = None
        try:
            if "-" in es:
                d = datetime.date.fromisoformat(es)
            elif len(es) == 6:
                d = datetime.date(2000 + int(es[:2]), int(es[2:4]), int(es[4:6]))
        except ValueError:
            d = None
        if d and (best is None or d > best):
            best = d
    if best is None:
        return None
    return datetime.datetime(best.year, best.month, best.day, 23, 59, 59,
                             tzinfo=datetime.timezone.utc)


def _spread_mtm_from_rows(legs: list[dict], rows_by_symbol: dict) -> list[dict]:
    """Same math as price_tracker.spread_mtm_history but from a preloaded
    {symbol: {ts: mark}} index. Returns [{ts, mtm}] sorted by ts, requiring
    all legs present at a ts."""
    leg_meta = {}
    for l in legs:
        s = (l.get("symbol") or "").strip()
        if not s:
            continue
        action = l.get("action") or ""
        sign = 1 if action.startswith("Buy") else -1 if action.startswith("Sell") else 0
        leg_meta[s] = {"sign": sign, "qty": float(l.get("quantity") or 1)}
    if not leg_meta:
        return []
    # collect ts where ALL legs have a mark
    ts_sets = [set(rows_by_symbol.get(s, {}).keys()) for s in leg_meta]
    common = set.intersection(*ts_sets) if ts_sets else set()
    out = []
    for ts in sorted(common):
        mtm = 0.0
        for s, meta in leg_meta.items():
            mtm += meta["sign"] * rows_by_symbol[s][ts] * 100 * meta["qty"]
        out.append({"ts": ts, "mtm": round(mtm, 2)})
    return out


def per_trade_excursion(open_entry: Optional[dict], close_entry: dict,
                         rows_by_symbol: Optional[dict] = None) -> dict:
    base = {"trade_id": close_entry.get("trade_id"),
            "ticker": close_entry.get("ticker"),
            # 唯一区分字段: 同标的同日多笔单靠 open_at(开仓时间戳)区分,
            # spread_label/open_date 供展示与兜底匹配。dashboard 用这些做精确匹配。
            "open_at": (open_entry or {}).get("open_at"),
            "open_date": (open_entry or {}).get("open_date"),
            "spread_label": (open_entry or {}).get("spread_label"),
            "net_pnl": float(close_entry.get("net_pnl") or 0),
            "has_intraday": False, "mfe": None, "mae": None,
            "capture_ratio": None, "giveback": None,
            "days_from_peak_to_exit": None, "last_unreal": None,
            "post_close": None, "full_eod": None,
            "eod_days": None, "actual_eod_days": None,
            "series": []}
    if not open_entry:
        return base
    legs = open_entry.get("legs") or []
    if not legs:
        return base
    if rows_by_symbol is not None:
        series = _spread_mtm_from_rows(legs, rows_by_symbol)
    else:
        series = price_tracker.spread_mtm_history(legs)
    if not series:
        return base
    open_dt = _to_dt(open_entry.get("open_at") or open_entry.get("open_date") or "")
    close_dt = _to_dt(close_entry.get("cluster_close_at")
                      or close_entry.get("close_at")
                      or close_entry.get("close_date") or "")
    expiry_dt = _expiry_dt(legs)
    entry_mtm = _entry_mtm(legs)
    net = base["net_pnl"]
    pts = []        # 平仓前(open→close):盘中浮盈/亏序列,喂现有 MFE/MAE/止盈止损栅格
    pts_post = []   # 平仓后(close→到期):用于"关了之后价差怎么走"分析
    for row in series:
        ts = _to_dt(row.get("ts"))
        if ts is None or (open_dt and ts < open_dt):
            continue
        unreal = round(row["mtm"] - entry_mtm, 2)
        if close_dt and ts > close_dt:
            if expiry_dt is None or ts <= expiry_dt:
                pts_post.append({"ts": row["ts"], "unreal": unreal})
            continue
        pts.append({"ts": row["ts"], "unreal": unreal})
    # 夹逼用的理论上下限(价差价值不会超过 max_profit、不会低于 -max_loss),去坏 mark
    mp = close_entry.get("max_profit")
    ml = close_entry.get("max_loss")

    def _clamp(u):
        if mp is not None and u > float(mp):
            return float(mp)
        if ml is not None and u < -float(ml):
            return -float(ml)
        return u

    def _eod_clamped(points):
        """降采样到 EOD(每日最后一点)+ 夹逼 → [unreal,...] 按日序(去盘中 hourly 噪声)。"""
        bd = {}
        for p in points:
            bd[p["ts"][:10]] = p
        return [_clamp(bd[d]["unreal"]) for d in sorted(bd)]

    # 平仓后表现:平仓后价差冲到的峰/谷、到期值,对比你实际兑现的 net_pnl
    pu = _eod_clamped(pts_post)
    if pu:
        peak = max(pu)
        base["post_close"] = {
            "n": len(pu),
            "peak": round(peak, 2), "trough": round(min(pu), 2),
            "last": round(pu[-1], 2),
            "peak_vs_net": round(peak - net, 2),                 # >0: 平仓后还更高 → 平早了(上限,完美后视)
            "expiry_vs_net": round(pu[-1] - net, 2),             # >0: 持到到期比你平得好(现实口径)
        }
    # 全程 EOD 路径(open→到期),供最优出场策略回测 + 资金效率
    full = _eod_clamped(pts + pts_post)
    if full:
        base["full_eod"] = full
        base["eod_days"] = len(full)                                  # open→到期 交易日数
        base["actual_eod_days"] = len({p["ts"][:10] for p in pts}) or 1  # 你实际持有到平仓的交易日数
    if not pts:
        return base
    unreals = [p["unreal"] for p in pts]
    mfe = max(unreals)
    mae = min(unreals)
    peak_i = unreals.index(mfe)
    peak_dt = _to_dt(pts[peak_i]["ts"])
    days_peak = ((close_dt - peak_dt).total_seconds() / 86400
                 if (close_dt and peak_dt) else None)
    base.update({
        "has_intraday": True,
        "mfe": round(mfe, 2),
        "mae": round(mae, 2),
        "capture_ratio": round(net / mfe, 4) if (net > 0 and mfe > 0) else None,
        "giveback": round(mfe - net, 2),
        "days_from_peak_to_exit": round(days_peak, 1) if days_peak is not None else None,
        "last_unreal": pts[-1]["unreal"],
        "series": pts,
    })
    return base


# ---------------------------------------------------------------------------
# Book-level what-if: optimal take-profit / stop-loss grids + summary
# ---------------------------------------------------------------------------

TP_GRID = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.90]
SL_GRID = [0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]


def _basis_value(ex: dict, basis: str):
    return ex.get("net_premium") if basis == "premium" else ex.get("max_profit")


def _simulate_tp(ex: dict, k: float, basis: str) -> float:
    """首次 unreal >= k*base 则在该点平仓(取该点 unreal 近似成交),否则用实际 net_pnl。"""
    if not ex.get("has_intraday") or not ex.get("series"):
        return float(ex.get("net_pnl") or 0)
    base = _basis_value(ex, basis)
    if not base:
        return float(ex.get("net_pnl") or 0)
    thresh = k * float(base)
    for p in ex["series"]:
        if p["unreal"] >= thresh:
            return round(p["unreal"], 2)
    return float(ex.get("net_pnl") or 0)


def optimal_tp_grid(exes: list, basis: str = "premium") -> dict:
    actual = sum(float(e.get("net_pnl") or 0) for e in exes)
    curve = [(k, round(sum(_simulate_tp(e, k, basis) for e in exes), 2)) for k in TP_GRID]
    best_k, best_total = max(curve, key=lambda kv: kv[1])
    return {"basis": basis, "actual_total": round(actual, 2),
            "best_k": best_k, "best_total": best_total,
            "delta_vs_actual": round(best_total - actual, 2), "curve": curve}


def _simulate_sl(ex: dict, k: float) -> float:
    """首次 unreal <= -k*net_premium 则在该点止损平仓,否则用实际 net_pnl。"""
    if not ex.get("has_intraday") or not ex.get("series"):
        return float(ex.get("net_pnl") or 0)
    base = ex.get("net_premium")
    if not base:
        return float(ex.get("net_pnl") or 0)
    thresh = -k * float(base)
    for p in ex["series"]:
        if p["unreal"] <= thresh:
            return round(p["unreal"], 2)
    return float(ex.get("net_pnl") or 0)


def optimal_sl_grid(exes: list) -> dict:
    actual = sum(float(e.get("net_pnl") or 0) for e in exes)
    curve = [(k, round(sum(_simulate_sl(e, k) for e in exes), 2)) for k in SL_GRID]
    best_k, best_total = max(curve, key=lambda kv: kv[1])
    return {"actual_total": round(actual, 2), "best_k": best_k, "best_total": best_total,
            "delta_vs_actual": round(best_total - actual, 2), "curve": curve}


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def stop_discipline(exes: list) -> dict:
    """亏损单里,盘中浮亏(MAE)突破净权利金 50% 的比例(默认规则=50%止损)。"""
    losers = [e for e in exes if e.get("has_intraday") and e.get("net_pnl", 0) < 0
              and e.get("net_premium") and e.get("mae") is not None]
    if not losers:
        return {"n_losers": 0, "breached_50pct": None, "breach_rate": None}
    breached = sum(1 for e in losers if e["mae"] <= -0.5 * float(e["net_premium"]))
    return {"n_losers": len(losers), "breached_50pct": breached,
            "breach_rate": round(breached / len(losers), 4)}


EXIT_TP_GRID = [None, 0.25, 0.35, 0.50, 0.65, 0.80, 1.00, 1.25, 1.50, 2.00, 3.00]   # 占净权利金
EXIT_SL_GRID = [None, 0.30, 0.50, 0.70, 1.00]


def _sim_exit(full_eod: list, net_premium: float, tp_k, sl_j):
    """全程 EOD 路径模拟出场:首次 unreal≥tp_k·premium 止盈、≤−sl_j·premium 止损,
    都不触发则持有到到期。返回 (该策略盈亏, 持有 EOD 天数)。"""
    tp = tp_k * net_premium if tp_k else None
    sl = -sl_j * net_premium if sl_j else None
    for i, u in enumerate(full_eod):
        if tp is not None and u >= tp:
            return u, i + 1
        if sl is not None and u <= sl:
            return u, i + 1
    return full_eod[-1], len(full_eod)


MP_EXIT_GRID = [0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00]   # 占最大利润


def _sim_exit_mp(full_eod: list, max_profit: float, q) -> tuple:
    """平在【q×最大利润】:首次 unreal≥q·max_profit 平仓,否则持到到期。返回 (盈亏, 持有EOD天数)。"""
    th = q * max_profit
    for i, u in enumerate(full_eod):
        if u >= th:
            return u, i + 1
    return full_eod[-1], len(full_eod)


def _annual(pnl, cap_days):
    """资金年化回报 = 盈亏 / Σ(权利金×持有交易日) × 252。"""
    return round(pnl / cap_days * 252 * 100, 0) if cap_days else None


def optimal_exit_policy(exes: list) -> Optional[dict]:
    """对有完整 open→到期 EOD 路径的交易,网格搜索固定 TP/SL,报总盈亏 + 资金年化回报。"""
    cands = [e for e in exes if e.get("full_eod") and len(e["full_eod"]) >= 2 and e.get("net_premium")]
    if len(cands) < 20:
        return None

    def prem(e):
        return float(e["net_premium"])
    actual = sum(float(e.get("net_pnl") or 0) for e in cands)
    actual_cd = sum(prem(e) * (e.get("actual_eod_days") or e.get("eod_days") or 1) for e in cands)
    hold = sum(e["full_eod"][-1] for e in cands)
    hold_cd = sum(prem(e) * (e.get("eod_days") or len(e["full_eod"])) for e in cands)
    best = None
    grid = []
    for k in EXIT_TP_GRID:
        for j in EXIT_SL_GRID:
            tot = cd = 0.0
            for e in cands:
                pnl, days = _sim_exit(e["full_eod"], prem(e), k, j)
                tot += pnl
                cd += prem(e) * days
            row = {"tp": k, "sl": j, "total": round(tot, 2), "annual": _annual(tot, cd)}
            grid.append(row)
            if best is None or tot > best["total"]:
                best = row
    # 按【最大利润 %】扫(作者口径:吃到 X% 利润就平、开下单),按资金年化挑最优 → 给"规矩"
    mp_cands = [e for e in cands if e.get("max_profit")]
    mp_grid, mp_best = [], None
    for q in MP_EXIT_GRID:
        tot = cd = 0.0
        for e in mp_cands:
            pnl, days = _sim_exit_mp(e["full_eod"], float(e["max_profit"]), q)
            tot += pnl
            cd += prem(e) * days
        row = {"q": q, "total": round(tot, 2), "annual": _annual(tot, cd)}
        mp_grid.append(row)
        if mp_best is None or (row["annual"] or 0) > (mp_best["annual"] or 0):
            mp_best = row
    return {"n": len(cands),
            "actual": round(actual, 2), "actual_annual": _annual(actual, actual_cd),
            "hold_to_expiry": round(hold, 2), "hold_annual": _annual(hold, hold_cd),
            "best": best, "grid": grid,
            "mp_n": len(mp_cands), "mp_best": mp_best, "mp_grid": mp_grid}


def post_close_summary(exes: list) -> dict:
    """平仓后表现汇总:多少笔平仓后还能更高(平早了)、持到到期 vs 实际的总差、亏损单是否反弹。"""
    posts = [e for e in exes if e.get("post_close")]
    if not posts:
        return {"covered": 0}
    left = [e for e in posts if e["post_close"]["peak_vs_net"] > 1]       # 平仓后还更高(留桌上)
    left_sum = sum(e["post_close"]["peak_vs_net"] for e in left)
    expiry_delta = sum(e["post_close"]["expiry_vs_net"] for e in posts)   # 全持到期 − 实际
    losers = [e for e in posts if e.get("net_pnl", 0) < 0]
    loser_rebound = [e for e in losers if e["post_close"]["peak_vs_net"] > 1]
    return {
        "covered": len(posts),
        "left_on_table_n": len(left),
        "left_on_table_sum": round(left_sum, 2),
        "held_to_expiry_delta": round(expiry_delta, 2),
        "losers": len(losers),
        "losers_rebounded": len(loser_rebound),
    }


def book_summary(exes: list) -> dict:
    covered = [e for e in exes if e.get("has_intraday")]
    winners = [e for e in covered if e.get("net_pnl", 0) > 0 and e.get("capture_ratio") is not None]
    caps = [e["capture_ratio"] for e in winners]
    under50 = sum(1 for c in caps if c < 0.5)
    # capture vs theoretical max profit (different denominator than peak-capture/MFE)
    caps_mp = [round(e["net_pnl"] / e["max_profit"], 4)
               for e in winners if e.get("max_profit")]
    return {
        "coverage": {"covered": len(covered), "total": len(exes)},
        "median_capture": round(_median(caps), 4) if caps else None,
        "median_capture_vs_maxprofit": round(_median(caps_mp), 4) if caps_mp else None,
        "winners_under_50pct_capture": round(under50 / len(winners), 4) if winners else None,
        "optimal_tp_premium": optimal_tp_grid(covered, "premium"),
        "optimal_tp_maxprofit": optimal_tp_grid(covered, "maxprofit"),
        "optimal_sl": optimal_sl_grid(covered),
        "stop_discipline": stop_discipline(covered),
        "post_close": post_close_summary(exes),
        "exit_policy": optimal_exit_policy(exes),
    }


# ---------------------------------------------------------------------------
# Top-level what-if: load all journal entries, run excursions, aggregate
# ---------------------------------------------------------------------------

import journal_entries as _je
import trade_economics as _te


def _money(v) -> str:
    if v is None:
        return "N/A"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _attach_econ(open_entry, close_entry, ex):
    """补 max_profit/net_premium: 优先用 close entry 已回填的字段, 否则现算。"""
    ex["max_profit"] = close_entry.get("max_profit")
    ex["net_premium"] = close_entry.get("net_premium")
    if ex["max_profit"] is None and open_entry:
        e = _te.spread_economics(open_entry.get("legs") or [])
        ex["max_profit"] = e["max_profit"]
        ex["net_premium"] = e["net_premium"]
    return ex


def whatif_narratives(summary: dict) -> list[dict]:
    out = []
    mc = summary.get("median_capture")                 # vs MFE (peak)
    mcmp = summary.get("median_capture_vs_maxprofit")  # vs theoretical max
    u50 = summary.get("winners_under_50pct_capture")
    tp = summary.get("optimal_tp_premium") or {}
    cov = summary.get("coverage", {})
    ev = f"基于 {cov.get('covered','?')} 笔有盘中数据的交易；理论 mark，未含滑点。"
    ev_en = f"Based on {cov.get('covered','?')} trades with intraday data; theoretical marks, no slippage."
    # every card also carries its English (title_en, headline_en, body_en, evidence_en) for the
    # interface in English

    # 1) 止盈时机(峰值兑现率) — 据实,通常是好消息
    if mc is not None:
        delta = tp.get("delta_vs_actual", 0)
        beats = delta < 0   # 固定止盈线赢不了实际
        out.append({
            "key": "early_tp", "title": "止盈时机",
            "tone": "good" if (mc >= 0.6 and beats) else "warn",
            "headline": f"赢家平均兑现了峰值浮盈的 {mc*100:.0f}%"
                        + (f"（{u50*100:.0f}% 的赢家从峰值回吐过半）。" if u50 else "。"),
            "body": (f"回测统一止盈：最佳是权利金 +{int(tp.get('best_k',0)*100)}%，"
                     f"模拟总盈利 {_money(tp.get('best_total',0))}，"
                     + (f"比你实际还少 {_money(abs(delta))} —— 说明你赢家的相机止盈已经优于任何固定止盈线，别机械化。"
                        if beats else
                        f"比实际多 {_money(abs(delta))} —— 可以考虑挂这条止盈线。")
                     ) if tp.get("best_k") is not None else "",
            "evidence": ev,
            "title_en": "Take-profit timing",
            "headline_en": f"Winners kept {mc*100:.0f}% of their peak unrealized profit on average"
                           + (f" ({u50*100:.0f}% of winners gave back more than half from the peak)." if u50 else "."),
            "body_en": (f"Backtest of one take-profit level for every trade: the best is premium +{int(tp.get('best_k',0)*100)}%, "
                        f"for a simulated total of {_money(tp.get('best_total',0))}, "
                        + (f"{_money(abs(delta))} less than you actually made: your own calls on winners already beat any "
                           "fixed take-profit line, so don't make it mechanical."
                           if beats else
                           f"{_money(abs(delta))} more than you actually made: consider placing this take-profit order.")
                        ) if tp.get("best_k") is not None else "",
            "evidence_en": ev_en,
        })

    # 2) 相对理论最大利润(参考口径)
    if mcmp is not None:
        out.append({
            "key": "maxprofit_capture", "title": "离理论最大利润多远",
            "tone": "neutral",
            "headline": f"相对理论最大利润，赢家只兑现了 {mcmp*100:.0f}%。",
            "body": "这是另一把尺：垂直价差要到期且方向走满才到 max，"
                    "平时兑现 30-40% 很正常，不必苛求——真正该看的是上面的峰值兑现率。",
            "evidence": ev,
            "title_en": "How far from the theoretical max profit",
            "headline_en": f"Measured against the theoretical max profit, winners captured only {mcmp*100:.0f}%.",
            "body_en": "This is a different yardstick: a vertical spread reaches its max only at expiration with the full "
                       "move, so capturing 30–40% is normal and nothing to chase. The peak capture rate above is what counts.",
            "evidence_en": ev_en,
        })

    # 3) 止损纪律 — 真正的红旗
    sd = summary.get("stop_discipline") or {}
    br = sd.get("breach_rate")
    if br is not None and sd.get("n_losers", 0) >= 8:
        out.append({
            "key": "stop_discipline", "title": "止损纪律",
            "tone": "bad" if br >= 0.4 else "warn" if br >= 0.2 else "good",
            "headline": f"{br*100:.0f}% 的亏损单盘中突破了 50% 止损线"
                        f"（{sd.get('breached_50pct')}/{sd.get('n_losers')} 笔）。",
            "body": "这才是你真正的漏洞：止盈时机没问题，但亏损单经常放任浮亏越过净权利金的一半。"
                    "给亏损单挂硬止损（尤其借方价差），是当前最高杠杆的改进。",
            "evidence": ev,
            "title_en": "Stop discipline",
            "headline_en": f"{br*100:.0f}% of losing trades went past the 50% stop line during the day "
                           f"({sd.get('breached_50pct')}/{sd.get('n_losers')} trades).",
            "body_en": "This is your real leak: the take-profit timing is fine, but losing trades are often allowed to lose "
                       "more than half the net premium. A hard stop on losers (debit spreads above all) is the "
                       "highest-leverage improvement right now.",
            "evidence_en": ev_en,
        })

    # 4) 平仓后表现 —— 关了之后价差怎么走(你最想看的)
    pc = summary.get("post_close") or {}
    if pc.get("covered", 0) >= 5:
        cov = pc["covered"]
        ln = pc.get("left_on_table_n", 0)
        ls = pc.get("left_on_table_sum", 0)
        hd = pc.get("held_to_expiry_delta", 0)
        losers = pc.get("losers", 0)
        reb = pc.get("losers_rebounded", 0)
        body = ("若每笔都改为持有到到期，总盈亏会比你实际"
                + (f"多 {_money(abs(hd))} —— 你整体平早了，利润留在了桌上。"
                   if hd > 0 else
                   f"少 {_money(abs(hd))} —— 你早平整体是对的（持到到期更差，theta/IV 吃掉了）。"))
        body_en = ("Holding every trade to expiration would have made "
                   + (f"{_money(abs(hd))} more than you did: overall you closed too early and left profit on the table."
                      if hd > 0 else
                      f"{_money(abs(hd))} less than you did: closing early was right overall "
                      "(holding to expiration was worse; theta and IV ate it)."))
        if losers:
            body += f" 亏损单 {losers} 笔里，{reb} 笔平仓后还反弹过（可能砍早了 / 没拿住）。"
            body_en += f" Of {losers} losing trades, {reb} bounced back after you closed them (cut too early, or not held)."
        out.append({
            "key": "post_close", "title": "平仓后表现（关了之后怎么样）",
            "tone": "warn" if (hd > 0 or ln > cov * 0.5) else "good",
            "headline": f"{cov} 笔有平仓后数据：{ln} 笔平仓后价差还更高（平早了，上限合计本可多 {_money(ls)}）。",
            "body": body,
            "evidence": f"基于 {cov} 笔有平仓后快照/EOD 的交易；理论 mark，与「持有到到期」的对照口径。",
            "title_en": "After the close (what happened next)",
            "headline_en": f"{cov} trades have data after the close: {ln} were worth more after you closed them "
                           f"(closed too early; up to {_money(ls)} more in all).",
            "body_en": body_en,
            "evidence_en": f"Based on {cov} trades with snapshots or EOD data after the close; theoretical marks, "
                           "compared with holding to expiration.",
        })

    # 5) 最优出场策略 —— 给你定个规矩(提前平 vs 持到期 vs 吃到 X% 最大利润再开下单)
    ep = summary.get("exit_policy") or {}
    mb = ep.get("mp_best")
    if ep.get("n", 0) >= 20 and mb:
        act = ep["actual"]
        aa = ep.get("actual_annual") or 0
        hold = ep["hold_to_expiry"]
        ha = ep.get("hold_annual") or 0
        q = int(mb["q"] * 100)
        ann = mb["annual"] or 0
        body = (f"三选一对照（总盈亏 / 资金年化回报）：① 提前平（你现在）{_money(act)} / {aa:.0f}%；"
                f"② 一律持到到期 {_money(hold)} / {ha:.0f}%；③ 吃到 {q}% 最大利润就平、腾资金开下单 "
                f"{_money(mb['total'])} / {ann:.0f}%。提前平=白忙（资金年化最低，平得太小）；死等到期=慢"
                f"（最后一点利润磨太久）；③ 两个口径都最优。并且 **不要止损**——借方价差本就限损，砍掉的亏损单多半反弹。")
        body_en = (f"Three choices compared (total P/L / annualized return on capital): ① closing early (what you do now) "
                   f"{_money(act)} / {aa:.0f}%; ② always holding to expiration {_money(hold)} / {ha:.0f}%; ③ closing at "
                   f"{q}% of max profit and opening the next trade {_money(mb['total'])} / {ann:.0f}%. Closing early is "
                   "busywork (the lowest annualized return; the wins are too small); waiting for expiration is slow (the "
                   "last bit of profit takes too long); ③ is best on both measures. And **no stop-loss**: a debit "
                   "spread's loss is already capped, and most of the losers that were cut came back.")
        out.append({
            "key": "exit_policy", "title": "最优出场策略（给你定规矩）",
            "tone": "good",
            "headline": f"回测 {ep['n']} 笔，规矩 = 【吃到 ~{q}% 最大利润就平、不止损、再开下一单】"
                        f"（资金年化 {ann:.0f}%，你现在才 {aa:.0f}%）。",
            "body": body,
            "evidence": f"对 {ep.get('mp_n', ep['n'])} 笔有完整 open→到期 EOD 路径的交易做网格回测（按最大利润 %）；"
                        f"EOD 粒度、理论 mark；资金年化=盈亏/Σ(权利金×持有交易日)×252。",
            "title_en": "Best exit policy (your rule)",
            "headline_en": f"Backtested on {ep['n']} trades, the rule is: close at ~{q}% of max profit, no stop-loss, then "
                           f"open the next trade (annualized return on capital {ann:.0f}%, against your {aa:.0f}% now).",
            "body_en": body_en,
            "evidence_en": f"Grid backtest over {ep.get('mp_n', ep['n'])} trades with a full open → expiration EOD path "
                           "(by % of max profit); EOD granularity, theoretical marks; annualized return = "
                           "P/L / Σ(premium × trading days held) × 252.",
        })

    return out


def compute_whatif() -> dict:
    opens_list = _je.load_open_entries()
    by_id = {o.get("id"): o for o in opens_list}
    by_tid = {o.get("trade_id"): o for o in opens_list}
    closes = _je.load_close_entries()

    # Collect ALL leg symbols across all open entries so we load history ONCE.
    all_symbols: list[str] = []
    _seen_syms: set[str] = set()
    for c in closes:
        o = by_id.get(c.get("open_entry_id")) or by_tid.get(c.get("trade_id"))
        if not o:
            continue
        for leg in (o.get("legs") or []):
            s = (leg.get("symbol") or "").strip()
            if s and s not in _seen_syms:
                _seen_syms.add(s)
                all_symbols.append(s)

    # Single history load — build {symbol: {ts: mark}} index.
    all_rows = price_tracker.load_history(all_symbols) if all_symbols else []
    rows_by_symbol: dict[str, dict[str, float]] = {}
    for row in all_rows:
        sym = row.get("symbol") or ""
        ts = row.get("ts") or ""
        if not sym or not ts:
            continue
        if sym not in rows_by_symbol:
            rows_by_symbol[sym] = {}
        rows_by_symbol[sym][ts] = float(row.get("mark") or 0)

    exes = []
    for c in closes:
        o = by_id.get(c.get("open_entry_id")) or by_tid.get(c.get("trade_id"))
        ex = per_trade_excursion(o, c, rows_by_symbol=rows_by_symbol)
        _attach_econ(o, c, ex)
        exes.append(ex)
    summary = book_summary(exes)
    per_trade = [{k: v for k, v in e.items() if k not in ("series", "full_eod")} for e in exes]
    return {"coverage": summary["coverage"], "summary": summary,
            "per_trade": per_trade, "narratives": whatif_narratives(summary)}

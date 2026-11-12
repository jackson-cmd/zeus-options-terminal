"""期权流动性体检 —— 纯函数:吃已抓好的链数据,产出 ticker 裁决 / 逐行权价网格 /
跨周价差对比。不做 IO(抓链 + 批量报价在 paper_trade_api.fetch_liquidity_scan,
端点在 optionstrat_server `/api/checkup/liquidity/{ticker}`)。

判断口径复用 strategy_builder.legWarnings 的语义(宽 spread 双门槛防 penny 误报、
低 OI / 低 vol 警告),设计见 docs/superpowers/specs/2026-06-16-liquidity-checkup-design.md。
"""
from __future__ import annotations

# ── 裁决门槛(集中常量,方便调)──
WIDE_PCT = 10.0     # spread% ≥ 此 且 spread_abs ≥ WIDE_ABS → 偏宽(🟡)
WIDE_ABS = 0.20     # penny 期权双门槛:$0.05/$0.10 虽 % 高但 abs 小,不算宽
VWIDE_PCT = 25.0    # 极宽(🔴):mid 基本挂不上
VWIDE_ABS = 0.30
THIN_OI = 50        # OI < 此 → 偏(薄)
DEAD_VOL = 10       # 当日 vol < 此(且 >0)→ 偏(无活跃)

WINDOW_LO = 0.90    # ATM-10%
WINDOW_HI = 1.15    # ATM+15%
MAX_DTE = 50

_ORDER = {"good": 0, "warn": 1, "bad": 2, "unknown": 3}


def leg_verdict(bid, ask, vol=0, oi=0) -> dict:
    """单腿流动性裁决 → {tier, reasons, bid, ask, mid, spread_abs, spread_pct, vol, oi}。

    tier: 'good'🟢 / 'warn'🟡 / 'bad'🔴。
    🔴 = mid 难成交(单边报价 / 极宽);🟡 = 能开但要磨价(宽 / OI 薄 / vol 低)。
    """
    bid = float(bid or 0)
    ask = float(ask or 0)
    vol = int(vol or 0)
    oi = int(oi or 0)

    if bid <= 0 or ask <= 0:
        return {"tier": "bad", "reasons": ["单边报价 — mid 挂不上"],
                "bid": round(bid, 2), "ask": round(ask, 2), "mid": 0.0,
                "spread_abs": None, "spread_pct": None, "vol": vol, "oi": oi}

    mid = (bid + ask) / 2
    spread_abs = ask - bid
    spread_pct = spread_abs / mid * 100 if mid > 0 else None

    reasons = []
    vwide = spread_pct is not None and spread_pct >= VWIDE_PCT and spread_abs >= VWIDE_ABS
    if vwide:
        tier = "bad"
        reasons.append(f"极宽 {spread_pct:.0f}% (${spread_abs:.2f}) — mid 难成交")
    else:
        tier = "good"
        if spread_pct is not None and spread_pct >= WIDE_PCT and spread_abs >= WIDE_ABS:
            reasons.append(f"宽 {spread_pct:.0f}% (${spread_abs:.2f})")
        if oi < 10:
            reasons.append(f"OI 极薄 ({oi})")
        elif oi < THIN_OI:
            reasons.append(f"OI 薄 ({oi})")
        if 0 < vol < DEAD_VOL:
            reasons.append(f"当日 vol 低 ({vol})")
        if reasons:
            tier = "warn"

    return {"tier": tier, "reasons": reasons,
            "bid": round(bid, 2), "ask": round(ask, 2), "mid": round(mid, 2),
            "spread_abs": round(spread_abs, 2),
            "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
            "vol": vol, "oi": oi}


def _nearest(strikes, target):
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def _mid(q):
    b = float((q or {}).get("bid") or 0)
    a = float((q or {}).get("ask") or 0)
    return (a + b) / 2 if (a > 0 and b > 0) else 0.0


def _two_sided(q):
    return float((q or {}).get("bid") or 0) > 0 and float((q or {}).get("ask") or 0) > 0


def _net_width(q):
    return float((q or {}).get("ask") or 0) - float((q or {}).get("bid") or 0)


def _window(strikes, spot, lo, hi):
    if not spot:
        return list(strikes)
    return [s for s in strikes if spot * lo <= s <= spot * hi]


def _side_grid(side, strikes_sorted, exps, per_exp_rows):
    cells, best_col = [], []
    for s in strikes_sorted:
        row = []
        for ei in range(len(exps)):
            r = per_exp_rows[ei].get(s)
            if not r:
                row.append(None)
                continue
            q = r.get(side) or {}
            cell = leg_verdict(q.get("bid"), q.get("ask"), q.get("vol"), q.get("oi"))
            iv = float(q.get("iv") or 0)
            cell["iv"] = round(iv * 100, 1) if iv > 0 else None
            row.append(cell)
        cand = [(c["spread_pct"], i) for i, c in enumerate(row)
                if c and c["tier"] != "bad" and c["spread_pct"] is not None]
        best_col.append(min(cand)[1] if cand else None)
        cells.append(row)
    return {"strikes": strikes_sorted, "cells": cells, "best_col": best_col}


def _ticker_verdict(exps, per_exp_rows, spot, atm):
    """取 28–45 DTE 月期权(没有就最接近 35d)ATM 行权价,call/put 取更差的一档当 ticker 裁决。"""
    if not exps or atm is None:
        return {"tier": "unknown", "label": "无数据", "detail": "没抓到合适的期权链",
                "spread_pct": None, "exp": None, "dte": None, "oi": None}
    target = sorted(exps, key=lambda e: (0 if 28 <= e["dte"] <= 45 else 1, abs(e["dte"] - 35)))[0]
    ti = exps.index(target)
    r = per_exp_rows[ti].get(atm)
    if not r:
        ks = sorted(per_exp_rows[ti].keys())
        nk = _nearest(ks, spot)
        r = per_exp_rows[ti].get(nk) if nk is not None else None
    if not r:
        return {"tier": "unknown", "label": "无数据", "detail": f"{target['date']} ATM 无报价",
                "spread_pct": None, "exp": target["date"], "dte": target["dte"], "oi": None}

    def _v(q):
        q = q or {}
        return leg_verdict(q.get("bid"), q.get("ask"), q.get("vol"), q.get("oi"))

    cv, pv = _v(r.get("call")), _v(r.get("put"))
    worse = cv if _ORDER[cv["tier"]] >= _ORDER[pv["tier"]] else pv
    tier, sp = worse["tier"], worse["spread_pct"]
    label = {"good": "流动性良好", "warn": "部分偏差", "bad": "此票慎开"}.get(tier, "无数据")
    sp_txt = "单边报价" if sp is None else f"spread {sp:.0f}%"
    detail = {
        "good": f"ATM {target['dte']}d 期权 {sp_txt} · mid 基本能成交",
        "warn": f"ATM {target['dte']}d 期权 {sp_txt} · 能开但要磨价",
        "bad": f"ATM {target['dte']}d 期权 {sp_txt} · mid 难成交、慎开",
    }.get(tier, "")
    return {"tier": tier, "label": label, "detail": detail, "spread_pct": sp,
            "exp": target["date"], "dte": target["dte"], "oi": min(cv["oi"], pv["oi"])}


def _cross_week(exps, per_exp_rows, spot, atm, width_pct, side="call"):
    """同一(ATM / ATM+width)价差跨周到期对比:逐周吸附真实行权价,算 net debit + net 买卖差。
    最便宜(且有效)的那周打 best。"""
    if not spot or atm is None:
        return None
    long_tgt, short_tgt = atm, atm + spot * width_pct
    rows = []
    for ei, e in enumerate(exps):
        ks = sorted(per_exp_rows[ei].keys())
        base = {"date": e["date"], "dte": e["dte"], "long": None, "short": None,
                "debit": None, "net_width": None, "best": False}
        if len(ks) < 2:
            rows.append(base)
            continue
        lk = _nearest(ks, long_tgt)
        ups = [k for k in ks if k > lk]
        sk = _nearest(ups, short_tgt) if ups else None
        base["long"] = lk
        if sk is None:
            rows.append(base)
            continue
        base["short"] = sk
        lc = per_exp_rows[ei][lk].get(side) or {}
        sc = per_exp_rows[ei][sk].get(side) or {}
        lmid, smid = _mid(lc), _mid(sc)
        if lmid > 0 and smid > 0 and lmid > smid:
            base["debit"] = round(lmid - smid, 2)
        if _two_sided(lc) and _two_sided(sc):
            base["net_width"] = round(_net_width(lc) + _net_width(sc), 2)
        rows.append(base)
    valid = [r for r in rows if r["debit"] is not None]
    if valid:
        min(valid, key=lambda r: r["debit"])["best"] = True
    return {"long": long_tgt, "width_pct": round(width_pct * 100, 1), "side": side, "rows": rows}


def build_scan(scan: dict, lo=WINDOW_LO, hi=WINDOW_HI, width_pct=0.10) -> dict:
    """scan = {ticker, spot, asof, market_hours, earnings_date,
              expirations:[{date, dte, type, strikes:[{strike, call:{bid,ask,vol,oi,iv}, put:{...}}]}]}

    → ticker 裁决 + call/put 网格(strikes × expirations) + 跨周价差对比。
    """
    ticker = (scan.get("ticker") or "").upper()
    spot = float(scan.get("spot") or 0)
    exps = sorted([e for e in (scan.get("expirations") or []) if 0 <= e.get("dte", -1) <= MAX_DTE],
                  key=lambda e: e["dte"])
    cols = [{"date": e["date"], "dte": e["dte"], "type": e.get("type", "")} for e in exps]

    all_strikes = set()
    per_exp_rows = []
    for e in exps:
        rows = {round(float(r["strike"]), 4): r for r in (e.get("strikes") or [])}
        win = _window(sorted(rows.keys()), spot, lo, hi)
        per_exp_rows.append({k: rows[k] for k in win})
        all_strikes.update(win)
    strikes_sorted = sorted(all_strikes)
    atm = _nearest(strikes_sorted, spot) if strikes_sorted else None

    return {
        "ticker": ticker, "spot": round(spot, 2), "asof": scan.get("asof"),
        "market_hours": scan.get("market_hours"), "earnings_date": scan.get("earnings_date"),
        "atm_strike": atm, "cols": cols,
        "ticker_verdict": _ticker_verdict(exps, per_exp_rows, spot, atm),
        "calls": _side_grid("call", strikes_sorted, exps, per_exp_rows),
        "puts": _side_grid("put", strikes_sorted, exps, per_exp_rows),
        "cross_week": _cross_week(exps, per_exp_rows, spot, atm, width_pct, "call"),
        "window": {"lo": lo, "hi": hi, "max_dte": MAX_DTE},
    }

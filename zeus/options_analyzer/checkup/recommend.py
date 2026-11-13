"""自动推荐 bull call spread —— 按验证过的开法(ATM 长腿 / 35-45 DTE / 宽≈10% 标的价)
生成 5-8 个候选,按【成交质量 + 流动性 + 赔率 + 规律契合】排序,挑最能下手的那几单。

纯函数:吃已经抓好的链数据(spot + 多到期的 strikes,每腿带 bid/ask/mid/vol/oi),

评分(组件均衡,权重见 _WEIGHTS):
  成交质量 exec  35% —— 价差净 bid-ask 宽 / debit(散户真正吃的滑点,最关键)
  流动性   liq   30% —— 两腿 OI/vol 的瓶颈腿(log 标度,对中等流动性更公平)
  赔率     rr    20% —— max_profit / debit
  规律契合 fit   15% —— 长腿离 ATM、宽度离 10%、DTE 离 40 的偏离惩罚
财报落在到期窗口内 → composite ×0.6 + flag(你自己的铁律:不赌财报)。
"""
from __future__ import annotations

import math

_WEIGHTS = {"exec": 0.35, "liq": 0.30, "rr": 0.20, "fit": 0.15}
WIDTHS = (0.07, 0.10, 0.13)          # 宽度候选(标的价 %),10% 为验证最优、±给变体
DTE_TGT = 40
LONG_OFFSETS = (-0.03, 0.0, 0.03)    # 长腿摆位:微 ITM / ATM / 微 OTM(ATM 为主)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _nearest(strikes, target):
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def _exec_score(spread_pct):
    """净 bid-ask 宽占 debit 的 %。≤5% 满分,之后每多 1% 扣 2 分。"""
    if spread_pct is None:
        return 0.0
    return _clamp(100 - max(0.0, spread_pct - 5) * 2)


def _liq_score(oi_min, vol_min):
    """OI 占 70(log,2000 满)、当日 vol 占 30(log,300 满)。取两腿瓶颈。"""
    oi = 70 * min(1.0, math.log10(max(0, oi_min) + 1) / math.log10(2001))
    vol = 30 * min(1.0, math.log10(max(0, vol_min) + 1) / math.log10(301))
    return _clamp(oi + vol)


def _rr_score(rr):
    """RR 0.4→0、1.1→50、1.8→100(ATM 10% 宽借方价差 RR 多在 0.8-1.5)。"""
    if rr is None:
        return 0.0
    return _clamp((rr - 0.4) / (1.8 - 0.4) * 100)


def _fit_score(long_off_pct, width_pct, dte, target_off_pct=0.0):
    """偏离 regime 目标长腿摆位 / 10%宽 / 40DTE 的惩罚。"""
    p = abs(long_off_pct - target_off_pct) * 4 + abs(width_pct - 10) * 3 + abs(dte - DTE_TGT) * 2
    return _clamp(100 - p)


def _build_one(spot, dte, exp_date, long_row, short_row, target_off_pct=0.0):
    """两腿 row(含 strike + call:{bid,ask,mid,vol,oi})→ 候选 dict 或 None。"""
    lk, sk = long_row["strike"], short_row["strike"]
    lc, sc = long_row["call"], short_row["call"]
    lmid, smid = lc.get("mid") or 0, sc.get("mid") or 0
    if sk <= lk or lmid <= 0:
        debit = (lc.get("ask", 0) - sc.get("bid", 0))  # 兜底
    else:
        debit = lmid - smid
    width = sk - lk
    if debit <= 0.02 or width <= 0:
        return None
    maxp = width - debit
    if maxp <= 0:
        return None
    rr = maxp / debit
    be = lk + debit
    be_dist = (be - spot) / spot * 100 if spot else None
    # 净 bid-ask:买价差付 long.ask - short.bid,卖收 long.bid - short.ask
    net_width = (lc.get("ask", 0) - lc.get("bid", 0)) + (sc.get("ask", 0) - sc.get("bid", 0))
    spread_pct = (net_width / debit * 100) if debit > 0 else None
    oi_min = min(int(lc.get("oi", 0) or 0), int(sc.get("oi", 0) or 0))
    vol_min = min(int(lc.get("vol", 0) or 0), int(sc.get("vol", 0) or 0))
    long_off = (lk - spot) / spot * 100 if spot else 0
    width_pct = width / spot * 100 if spot else 0
    ex, lq, rs, ft = _exec_score(spread_pct), _liq_score(oi_min, vol_min), _rr_score(rr), _fit_score(long_off, width_pct, dte, target_off_pct)
    comp = ex * _WEIGHTS["exec"] + lq * _WEIGHTS["liq"] + rs * _WEIGHTS["rr"] + ft * _WEIGHTS["fit"]
    return {
        "exp": exp_date, "dte": int(dte),
        "long": round(lk, 2), "short": round(sk, 2),
        "debit": round(debit, 2), "max_profit": round(maxp * 100, 0), "max_loss": round(debit * 100, 0),
        "rr": round(rr, 2), "breakeven": round(be, 2), "be_dist_pct": round(be_dist, 1) if be_dist is not None else None,
        "width_pct": round(width_pct, 1), "long_off_pct": round(long_off, 1),
        "spread_pct": round(spread_pct, 0) if spread_pct is not None else None,
        "oi_min": oi_min, "vol_min": vol_min,
        "scores": {"exec": round(ex), "liq": round(lq), "rr": round(rs), "fit": round(ft)},
        "score": round(comp),
    }


def _reason(c):
    bits = []
    s = c["scores"]
    if s["exec"] >= 80:
        bits.append("买卖价差紧、好成交")
    elif s["exec"] <= 45:
        bits.append("⚠️ 买卖价差偏宽")
    if s["liq"] >= 70:
        bits.append(f"流动性足(OI {c['oi_min']:,})")
    elif c["oi_min"] < 200:
        bits.append(f"⚠️ 流动性薄(OI {c['oi_min']:,})")
    if c["rr"] >= 1.3:
        bits.append(f"赔率好 {c['rr']:.1f}:1")
    if abs(c["long_off_pct"]) <= 1.5 and abs(c["width_pct"] - 10) <= 2:
        bits.append("正合 ATM·10%宽")
    if c.get("earnings_in_window"):
        bits.append("🛑 财报在到期窗口内")
    return " · ".join(bits) if bits else "符合开法"


def recommend(spot, chains, earnings_date=None, top_n=8, direction="bull",
              target_long_off=0.0):
    """spot: 标的现价;chains: [{date, dte, strikes:[{strike, call:{bid,ask,mid,vol,oi,iv}}]}]。
    返回排序后的候选列表(已附 reason)。direction 目前固定 bull call。"""
    if not spot or spot <= 0 or not chains:
        return []
    offs = sorted({round(target_long_off + d, 4) for d in (-0.03, 0.0, 0.03)})  # 围绕 regime 目标摆位
    cands = []
    seen = set()
    for ch in chains:
        dte = ch.get("dte")
        exp_date = ch.get("date")
        rows = [r for r in (ch.get("strikes") or [])
                if (r.get("call") or {}).get("mid", 0) > 0 and (r.get("call") or {}).get("bid", 0) > 0]
        ks = sorted(r["strike"] for r in rows)
        if len(ks) < 2:
            continue
        by_k = {r["strike"]: r for r in rows}
        for off in offs:
            lk = _nearest(ks, spot * (1 + off))
            if lk is None:
                continue
            ups = [k for k in ks if k > lk]
            for w in WIDTHS:
                sk = _nearest(ups, lk + spot * w)
                if sk is None:
                    continue
                key = (exp_date, lk, sk)
                if key in seen:
                    continue
                seen.add(key)
                c = _build_one(spot, dte, exp_date, by_k[lk], by_k[sk],
                               target_off_pct=target_long_off * 100)
                if not c:
                    continue
                # 财报窗口惩罚
                if earnings_date and exp_date and str(earnings_date) <= str(exp_date) and str(earnings_date) >= _today():
                    c["earnings_in_window"] = True
                    c["score"] = round(c["score"] * 0.6)
                else:
                    c["earnings_in_window"] = False
                cands.append(c)
    cands.sort(key=lambda x: -x["score"])
    cands = cands[:top_n]
    for c in cands:
        c["reason"] = _reason(c)
    if cands:
        cands[0]["top"] = True
    return cands


def _today():
    import datetime
    return datetime.date.today().isoformat()

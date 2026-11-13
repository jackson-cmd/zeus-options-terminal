"""价格合理性 + 极度OTM 判定(纯函数,基于到期 payoff 网格)。

输入腿要带报价(mid/bid/ask 来自实时链,由调用方填好)。算:理论中间价、滑点、
最大盈亏、价差宽度、赔率 R:R、盈亏平衡、近似 POP、以及盈亏平衡离现价几个 σ
(极度OTM 判定)。$ 口径按 1 张价差每股(×mult 得每张美元)。
"""
from __future__ import annotations

import math

import numpy as np


def _intrinsic(leg, p: float) -> float:
    k = float(leg["strike"])
    if leg["type"].upper().startswith("C"):
        v = max(p - k, 0.0)
    else:
        v = max(k - p, 0.0)
    return v * float(leg.get("qty", 0))


def _payoff_per_share(legs, p: float, net_debit: float) -> float:
    """到期每股盈亏 = Σ腿内在价值(带方向) − 净付出(债)。net_debit 正=付款。"""
    return sum(_intrinsic(l, p) for l in legs) - net_debit


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def evaluate(legs, net_price, spot, iv=None, dte=None, mult: int = 100) -> dict:
    """legs: [{type,strike,qty(带符号),mid?,bid?,ask?,delta?}]; net_price: 打算付的净价/股(正=债)。"""
    if not legs or spot is None or net_price is None:
        return {"ok": False, "note": "missing_inputs"}
    spot = float(spot)
    net = float(net_price)

    # 理论中间价(各腿 mid 之和,带方向);缺 mid 用 (bid+ask)/2
    theo = 0.0
    have_quotes = True
    for l in legs:
        m = l.get("mid")
        if m is None and l.get("bid") is not None and l.get("ask") is not None:
            m = (float(l["bid"]) + float(l["ask"])) / 2
        if m is None:
            have_quotes = False
            break
        theo += float(m) * float(l.get("qty", 0))
    theo = theo if have_quotes else None   # Σ(mid·qty):多头+/空头−,正=净债(付款),与 net_price 同号

    slippage = (net - theo) if (theo is not None) else None      # 正 = 比中间价贵(吃亏)
    slippage_pct = (slippage / abs(theo) * 100) if (slippage is not None and theo) else None

    # payoff 网格:max 盈亏 / 盈亏平衡 / 需要方向
    # 网格必须覆盖所有行权价(否则远 OTM 价差的 max盈亏/方向会算错)
    strikes = [float(l["strike"]) for l in legs]
    lo = max(0.01, min(spot * 0.4, min(strikes) * 0.7))
    hi = max(spot * 1.6, max(strikes) * 1.3)
    grid = np.linspace(lo, hi, 3001)
    vals = np.array([_payoff_per_share(legs, p, net) for p in grid])
    max_profit = float(vals.max())
    max_loss = float(-vals.min())          # 正数(亏损幅度)
    rr = (max_profit / max_loss) if max_loss > 0 else None
    bes = [float((grid[i - 1] + grid[i]) / 2)
           for i in range(1, len(grid)) if (vals[i - 1] < 0) != (vals[i] < 0)]
    be = min(bes, key=lambda x: abs(x - spot)) if bes else None
    bullish = bool(vals[-1] > vals[0])     # 网格两端判方向(对远 OTM 也稳)

    # 离 breakeven 多远(% + σ)+ 近似 POP + 极度OTM
    dist_pct = ((be - spot) / spot * 100) if be is not None else None
    sigma = (spot * float(iv) * math.sqrt(dte / 365.0)) if (iv and dte and dte > 0) else None
    if be is not None and sigma:
        req = (be - spot) if bullish else (spot - be)   # 朝盈利方向还要走多远
        otm_sigma = req / sigma
        pop = (1 - _phi(otm_sigma)) * 100 if otm_sigma >= 0 else (1 - _phi(otm_sigma)) * 100
    else:
        otm_sigma = None
        pop = None

    is_debit = net > 0
    return {
        "ok": True,
        "net_price": round(net, 2),
        "theo_mid": round(theo, 2) if theo is not None else None,
        "slippage": round(slippage, 2) if slippage is not None else None,
        "slippage_pct": round(slippage_pct, 1) if slippage_pct is not None else None,
        "max_profit": round(max_profit * mult, 0),
        "max_loss": round(max_loss * mult, 0),
        "rr": round(rr, 2) if rr is not None else None,
        "breakeven": round(be, 2) if be is not None else None,
        "dist_be_pct": round(dist_pct, 1) if dist_pct is not None else None,
        "otm_sigma": round(otm_sigma, 2) if otm_sigma is not None else None,
        "pop_approx": round(pop, 1) if pop is not None else None,
        "bullish": bool(bullish),
        "is_debit": bool(is_debit),
    }

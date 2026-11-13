"""持仓出场判定 —— 交易日记 + 真实期权链回测的铁律。

最优止盈档**随市场 regime 移动**(backtest_factory CONCLUSIONS §9,真实期权链验证):
  · 牛市(SPY>200SMA)→ 吃到 **70-80%** 最大利润(甜区~80%)
  · 震荡/弱(SPY≤200SMA)→ 吃到 **50-60%**(甜区~60%);2022 那种环境等过 70% 年化≈0
  · 100% 死等到期:每个 regime 都最差
借方价差**不止损**(限损,历史这类亏损单多半反弹,砍了反而亏)。深亏只作可视提示、仍建议持有。
"""
from __future__ import annotations

from . import regime as _regime


def evaluate(unrealized_pnl, max_profit, dte=None, pnl_pct=None, th=None,
             regime=None) -> dict:
    """unrealized_pnl: 当前浮盈 $;max_profit: 该单理论最大利润 $(无界 ≥1e9 则不可算进度);
    pnl_pct: 收益%(对净权利金,正=盈),仅用于深亏提示。dte 仅展示。
    regime: 'bull'|'chop'|None → 选止盈带;th 覆盖止盈下沿(测试/手调用)。"""
    up = _f(unrealized_pnl)
    mp = _f(max_profit)
    pp = _f(pnl_pct)
    dt = _f(dte)
    prog = (up / mp) if (up is not None and mp is not None and 0 < mp < 1e9) else None

    b = _regime.band(regime)
    soft = th if th is not None else b["soft"]
    forced = b["forced"]
    near_lo, zone, rg = b["near"], b["zone"], b["label"]
    fa = _regime.force_at({**b, "soft": soft}, dt)          # 强制下沿(DTE<20 提前到 soft)
    short_t = (dt is not None and dt < b.get("dte_gate", 20))

    if prog is not None and prog >= fa:
        why = (f"{rg}+剩{dt:.0f}天:时间不多、利润易回吐 → 现在就平、腾资金开下单。" if short_t
               else f"{rg}:已到 {forced * 100:.0f}% 强制档 → 现在就平、别再等。")
        return _v("force_tp", "🔴", "强制止盈", "good",
                  f"已吃到 {prog * 100:.0f}% 最大利润", why)
    if prog is not None and prog >= soft:
        return _v("take_profit", "🔵", "止盈", "good",
                  f"已吃到 {prog * 100:.0f}% 最大利润",
                  f"{rg}:进入 {zone} 止盈区 → 建议落袋;到 {forced * 100:.0f}% 强制平。")
    if prog is not None and prog >= near_lo:
        return _v("near_tp", "🟡", "接近", "warn",
                  f"已 {prog * 100:.0f}% 最大利润",
                  f"{rg}:快到 {zone} 止盈区,别提前跑、也别死等磨最后一点。")
    if pp is not None and pp <= -50:
        return _v("cut", "🔴", "深亏", "bad",
                  f"亏 {abs(pp):.0f}%",
                  f"但别砍!借方价差已限损,历史这类多半反弹,持有到 {zone} 或到期。")
    if prog is not None and prog > 0:
        detail = f"已 {prog * 100:.0f}% 最大利润,{rg}持有到 {zone} 再走。"
    elif prog is not None:
        detail = f"浮亏中,借方价差限损、持有到 {zone} 或到期不止损。"
    else:
        detail = f"{rg}:持有到 {zone} 最大利润或到期,不止损。"
    return _v("hold", "🟢", "持有", "ok",
              (f"收益 {pp:.0f}%" if pp is not None else "持有中"), detail)


def _v(code, emoji, label, severity, headline, detail) -> dict:
    return {"code": code, "emoji": emoji, "label": label, "severity": severity,
            "headline": headline, "detail": detail, "badge": f"{emoji}{label}"}


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None

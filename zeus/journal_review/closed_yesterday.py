"""昨日平仓复盘。

what-if 直接复用 journal_expiry_whatif.engine.compute_hold_pnl —— 不重新实现一遍到期
结算逻辑。⚠️已用真实数据验证过一个重要特性:该函数需要"到期日已经过去"才能算出
hypothetical_pnl(要用到期日当天或附近的收盘价),所以对"昨天才平、离到期还有好几周"的
仓位几乎总是 has_data=False —— 这不是 bug,是这个 what-if 工具的设计本质(算不出未来的
收盘价)。前端要把"whatif": null 显示成"到期未到,暂无法算"而不是当成异常。

loss_bucket 的桶口径完全抄自 journal_expiry_whatif.engine.summarize() 里的
bins/labels —— 那边只在聚合 DataFrame 上算(pd.cut),这里要逐笔算,所以把同一份
常量复制过来,没有改动阈值。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from journal_expiry_whatif.engine import compute_hold_pnl, parse_spread_label

from . import kpi as _kpi

# 跟 journal_expiry_whatif.engine.summarize() 里的桶口径完全对齐,勿单独改这里的阈值。
_BUCKET_BINS = [-2.0, -0.7, -0.3, 0.0, 0.5, 2.0]
_BUCKET_LABELS = ["深亏(<-70%)", "中亏(-70~-30%)", "小亏(-30~0%)", "小赚(0~50%)", "大赚(50%+)"]


def loss_bucket(pnl_pct: Optional[float]) -> Optional[str]:
    """net_pnl / 总 debit 的比例 → 5 档桶标签。pnl_pct 为 None(算不出比例)时返回 None。"""
    if pnl_pct is None:
        return None
    for lo, hi, label in zip(_BUCKET_BINS[:-1], _BUCKET_BINS[1:], _BUCKET_LABELS):
        if lo < pnl_pct <= hi:
            return label
    return None


def _dte_at_close(trade: dict) -> Optional[int]:
    parsed = parse_spread_label(trade.get("spread_label"))
    if not parsed:
        return None
    cd = trade.get("close_date")
    if not cd:
        return None
    try:
        close_date = dt.date.fromisoformat(cd)
    except ValueError:
        return None
    return (parsed["exp"].date() - close_date).days


def build(closes: list[dict], ohlc, yesterday: str) -> list[dict]:
    """yesterday: 'YYYY-MM-DD'。ohlc: data/daily_ohlc.parquet 读出来的 DataFrame
    (DatetimeIndex + ticker/Close 列),原样传给 compute_hold_pnl,这里不重复解析。"""
    rows = [c for c in closes if c.get("close_date") == yesterday]
    out = []
    for t in rows:
        debit = _kpi.total_debit(t)
        net_pnl = float(t.get("net_pnl") or 0)
        pnl_pct = (net_pnl / debit) if debit else None

        whatif_res = compute_hold_pnl(t, ohlc)
        whatif = None
        if whatif_res.get("has_data"):
            whatif = {
                "hypothetical_pnl": round(whatif_res["hypothetical_pnl"], 2),
                "regret": round(whatif_res["regret"], 2),
                "cut_was_wrong": bool(whatif_res["cut_was_wrong"]),
            }

        out.append({
            "ticker": t.get("ticker"),
            "direction": t.get("direction"),
            "spread_label": t.get("spread_label"),
            "net_pnl": round(net_pnl, 2),
            "dte_at_close": _dte_at_close(t),
            "whatif": whatif,
            "loss_bucket": loss_bucket(pnl_pct),
        })
    out.sort(key=lambda r: r["net_pnl"])   # 亏最多的排最前面,一眼看到最痛的
    return out

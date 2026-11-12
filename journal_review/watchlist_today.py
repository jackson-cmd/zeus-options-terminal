"""今日重点关注 —— 在仓持仓里挑需要今天多看两眼的:深亏 / TP 接近 / 近到期 / 财报临近。

不发起任何新的实时行情请求:当前市值(current_pnl_pct 的分子)来自
trade_journal/price_tracker.py 已经在跑的每小时快照缓存(spread_mtm_history),读不到就
把 current_pnl_pct 留 None,不影响其它字段(到期天数/财报都是纯日期计算,不需要行情)。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from journal_expiry_whatif.engine import parse_spread_label

DEEP_LOSS_PCT = -0.70            # 跟 closed_yesterday 的"深亏"桶口径对齐
NEAR_EXPIRY_DAYS = 5
TP_NEAR_PCT = 0.85
EARNINGS_WITHIN_DAYS = 3


def _dte(spread_label: Optional[str], today: dt.date) -> Optional[int]:
    parsed = parse_spread_label(spread_label)
    if not parsed:
        return None
    return (parsed["exp"].date() - today).days


def _earnings_within(ticker: str, today: dt.date, calendar: dict,
                      days: int = EARNINGS_WITHIN_DAYS) -> Optional[int]:
    for e in (calendar.get("earnings") or []):
        if (e.get("ticker") or "").upper() != (ticker or "").upper():
            continue
        try:
            d = dt.date.fromisoformat(e.get("date") or "")
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            return delta
    return None


def build(open_positions: list[dict], today: dt.date, calendar: dict,
          mtm_lookup: Optional[dict] = None) -> list[dict]:
    """mtm_lookup: {entry_id: {"current_value": float}} —— engine.py 从
    trade_journal.price_tracker.spread_mtm_history 取的最新一条整spread市值,注入进来
    方便这里的逻辑单测(不用真读 parquet)。current_value 口径:整个仓位当前值(已按
    quantity 汇总的美元金额),跟 total_debit 同口径,直接相减就是浮动盈亏。"""
    mtm_lookup = mtm_lookup or {}
    out = []
    for o in open_positions:
        ticker = o.get("ticker") or ""
        spread_label = o.get("spread_label")
        dte = _dte(spread_label, today)
        open_price = float(o.get("open_price") or 0)
        qty = float(o.get("quantity") or 1)
        debit = abs(open_price) * qty

        parsed = parse_spread_label(spread_label)
        max_profit_total = None
        if parsed and debit:
            width = abs(parsed["k2"] - parsed["k1"])
            per_contract_max_profit = width * 100 - abs(open_price)
            max_profit_total = per_contract_max_profit * qty

        cur = mtm_lookup.get(o.get("id"))
        pnl_pct = None
        pnl_dollar = None
        if cur and cur.get("current_value") is not None and debit:
            pnl_dollar = cur["current_value"] - debit
            pnl_pct = pnl_dollar / debit

        attention: list[str] = []
        if pnl_pct is not None and pnl_pct <= DEEP_LOSS_PCT:
            attention.append("深亏")
        if (pnl_dollar is not None and max_profit_total and max_profit_total > 0
                and pnl_dollar / max_profit_total >= TP_NEAR_PCT):
            attention.append(f"TP接近(≥{int(TP_NEAR_PCT * 100)}%最大盈利)")
        if dte is not None and dte <= NEAR_EXPIRY_DAYS:
            attention.append(f"近到期(≤{NEAR_EXPIRY_DAYS}d)")
        ed = _earnings_within(ticker, today, calendar)
        if ed is not None:
            attention.append("财报今天" if ed == 0 else f"财报{ed}天内")

        out.append({
            "ticker": ticker,
            "direction": o.get("direction"),
            "spread_label": spread_label,
            "days_to_expiry": dte,
            "current_pnl_pct": round(pnl_pct * 100, 1) if pnl_pct is not None else None,
            "attention": attention,
        })

    out.sort(key=lambda r: (0 if r["attention"] else 1,
                             r["days_to_expiry"] if r["days_to_expiry"] is not None else 999))
    return out

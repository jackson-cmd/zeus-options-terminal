"""KPI 卡片(昨日 P&L / 仓位 / 现金% / 规则状态)—— 纯函数,吃 engine.py 已经读好的
entries_open.jsonl / entries_close.jsonl(list[dict]),不做任何文件 IO。

现金%口径说明(不是"实时口径" —— 这是 review 页面故意的取舍):
  (体检/Positions tab)。这个页面的定位是"5 分钟离线复习",且任务书 data sources 明确写
  "reuse existing — DO NOT re-fetch"。而且这里真正需要的不是"当前市值"而是"占用了多少
  defined-risk 资金"——借方价差的 max loss = 开仓时付的 debit,跟当前价没关系、不需要实时
  来估算,不调用任何实时接口。这是估算值,不是权威值,字段名/注释里都要体现"estimate"。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

# 这里没有任何本地缓存的权威 NLV 快照(_compute_live_nlv 是运行中 PWA 进程内存态,依赖
# DXLink 实时报价,journal_review 作为独立可 CLI 跑的包不应该反向 import optionstrat_server
# 造成循环依赖),所以退化为一个写清楚来源的常量兜底。
ASSUMED_NLV = 25000.0   # fallback account size when no live NLV is available
POSITIONS_MAX = 10          # 铁律:同时 ≤10-12 仓,卡片展示上限用较严的 10
CASH_FLOOR_PCT = 40.0       # 铁律:现金 ≥40%


def _close_by_trade_id(closes: list[dict]) -> dict:
    """closes 按 trade_id 建索引。已用真实数据核实 entries_open/close 按 trade_id
    是干净的 1:1(无部分平仓导致的一对多),跟 trade_journal/journal_stats.build_trades
    的连接口径一致。"""
    return {c["trade_id"]: c for c in closes if c.get("trade_id")}


def positions_snapshot(opens: list[dict], closes: list[dict]) -> list[dict]:
    """当前仍在场内的持仓(open entry 在 closes 里找不到匹配的 trade_id)。"""
    close_by_tid = _close_by_trade_id(closes)
    return [o for o in opens if close_by_tid.get(o.get("trade_id")) is None]


def positions_on_day(opens: list[dict], closes: list[dict], day: str) -> list[dict]:
    """重建"某一天"在场的持仓(open_date <= day <= close_date,还没平的视为一直在场)。
    用于 rule_check 里的"同时持仓上限"/"现金底线"按天回放。"""
    close_by_tid = _close_by_trade_id(closes)
    out = []
    for o in opens:
        od = o.get("open_date") or ""
        if not od or od > day:
            continue
        c = close_by_tid.get(o.get("trade_id"))
        cd = (c.get("close_date") or "") if c else ""
        if cd and cd < day:
            continue
        out.append(o)
    return out


def total_debit(entry: dict) -> float:
    """单笔总 debit(美元,已含 ×100 合约乘数)。

    open_price 是"每张"净借记(见 trade_journal/journal_entries.py 的
    _new_open_entry: "open_price": trade["open_price"], # per-spread net debit/credit),
    quantity 是张数 —— 总 debit = |open_price| × quantity,不用再乘 100(open_price 本身
    已经是美元金额,不是"每股"点数)。"""
    return abs(float(entry.get("open_price") or 0)) * float(entry.get("quantity") or 1)


def deployed_capital(positions: list[dict]) -> float:
    return sum(total_debit(p) for p in positions)


def cash_pct_estimate(positions: list[dict], assumed_nlv: float = ASSUMED_NLV) -> Optional[float]:
    """估算现金% = 1 − 在仓 debit 总和 / 假定 NLV,夹到 [0,100]。assumed_nlv<=0 → None。"""
    if not assumed_nlv or assumed_nlv <= 0:
        return None
    deployed = deployed_capital(positions)
    pct = 100.0 * (1.0 - deployed / assumed_nlv)
    return max(0.0, min(100.0, pct))


def sum_pnl_on_date(closes: list[dict], date_str: str) -> float:
    return sum(float(c.get("net_pnl") or 0) for c in closes if c.get("close_date") == date_str)


def sum_pnl_window(closes: list[dict], start_date: str, end_date: str) -> float:
    return sum(
        float(c.get("net_pnl") or 0) for c in closes
        if c.get("close_date") and start_date <= c["close_date"] <= end_date
    )


def build_kpi(today_str: str, yesterday_str: str, closes: list[dict],
              positions: list[dict], rule_violations: list[dict]) -> dict:
    """组装顶部 KPI 卡片。rule_status 由 rule_violations(rule_check.all_violations 的
    输出,近 7 天窗口)驱动:0 条 ok / 1-2 条 warn / >=3 条 violated —— 这个三档阈值是本
    模块自己定的展示口径(不是某个已有统计结论),写清楚避免以后被当成"经统计检验"的分界。
    """
    yesterday_pnl = sum_pnl_on_date(closes, yesterday_str)
    week_start = (dt.date.fromisoformat(today_str) - dt.timedelta(days=6)).isoformat()
    week_pnl = sum_pnl_window(closes, week_start, today_str)
    cash_pct = cash_pct_estimate(positions)

    n = len(rule_violations)
    if n == 0:
        rule_status = "ok"
    elif n <= 2:
        rule_status = "warn"
    else:
        rule_status = "violated"

    return {
        "yesterday_net_pnl": round(yesterday_pnl, 2),
        "week_net_pnl": round(week_pnl, 2),
        "positions_count": len(positions),
        "positions_max": POSITIONS_MAX,
        "cash_pct": round(cash_pct, 1) if cash_pct is not None else None,
        "cash_pct_min": CASH_FLOOR_PCT,
        "rule_status": rule_status,
    }

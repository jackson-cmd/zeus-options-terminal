"""近 7 天铁律违反检测 —— 每条规则一个纯函数,吃 engine.py 读好的 opens/closes,
输出统一形状的 violation dict 列表(至少有 rule/date)。

  · 单日新开 ≤3 单            → check_daily_open_cap
  · 单笔 debit ≤3-5% NLV      → check_position_size(超 5% 才记违反,3-5% 是灰区不罚)
  · 同时 ≤10-12 仓            → check_concurrent_positions
  · 现金 ≥40%                 → check_cash_floor
  · 借方价差不止损(深亏别砍)  → check_deep_loss_cuts
这个背景派生出来的行为 heuristic,函数/规则名里都标了"非字面铁律",别当成 5 条铁律之一。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from journal_expiry_whatif.engine import parse_spread_label

from . import kpi as _kpi

MAX_DAILY_OPENS = 3
MAX_DAILY_CLOSES = 3            # 派生 heuristic 阈值,跟"单日新开≤3"的数字对称,非同一条规则
POSITION_SIZE_MAX_PCT = 5.0      # 3-5% 是允许区间,超 5% 才算违反
DEEP_LOSS_MIN_DTE = 3            # 距到期 still >=3 天就平 = "提前止损"(而非死等到期/临期正常处理)
DEEP_LOSS_PCT = -0.70            # 跟 journal_expiry_whatif.engine.summarize 的"深亏"分桶阈值对齐


def _window(today_str: str, days_back: int) -> tuple[str, str]:
    end = dt.date.fromisoformat(today_str)
    start = end - dt.timedelta(days=days_back - 1)
    return start.isoformat(), end.isoformat()


def _iter_days(start_date: str, end_date: str):
    d = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def check_daily_open_cap(opens: list[dict], start_date: str, end_date: str,
                          cap: int = MAX_DAILY_OPENS) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for o in opens:
        od = o.get("open_date") or ""
        if start_date <= od <= end_date:
            by_day.setdefault(od, []).append(o)
    out = []
    for day in sorted(by_day):
        trades = by_day[day]
        if len(trades) > cap:
            out.append({
                "rule": f"单日新开 ≤{cap} 单",
                "date": day,
                "actual": len(trades),
                "trades": [f"{t.get('ticker')} {t.get('spread_label')}" for t in trades],
            })
    return out


def check_daily_close_cap(closes: list[dict], start_date: str, end_date: str,
                           cap: int = MAX_DAILY_CLOSES) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for c in closes:
        cd = c.get("close_date") or ""
        if start_date <= cd <= end_date:
            by_day.setdefault(cd, []).append(c)
    out = []
    for day in sorted(by_day):
        trades = by_day[day]
        if len(trades) > cap:
            pnl_sum = sum(float(t.get("net_pnl") or 0) for t in trades)
            out.append({
                "rule": "单日集中平仓(疑似恐慌,非字面铁律)",
                "date": day,
                "actual": len(trades),
                "net_pnl_sum": round(pnl_sum, 2),
                "trades": [f"{t.get('ticker')} {t.get('spread_label')}" for t in trades],
            })
    return out


def check_position_size(opens: list[dict], start_date: str, end_date: str,
                         nlv: Optional[float] = None,
                         max_pct: float = POSITION_SIZE_MAX_PCT) -> list[dict]:
    nlv = _kpi.ASSUMED_NLV if nlv is None else nlv
    out = []
    if not nlv:
        return out
    for o in opens:
        od = o.get("open_date") or ""
        if not (start_date <= od <= end_date):
            continue
        debit = _kpi.total_debit(o)
        pct = 100.0 * debit / nlv
        if pct > max_pct:
            out.append({
                "rule": f"单笔 debit ≤{max_pct:.0f}% NLV",
                "date": od,
                "trade": f"{o.get('ticker')} {o.get('spread_label')} ${debit:,.0f}",
                "pct_of_nlv": round(pct, 1),
            })
    return out


def check_concurrent_positions(opens: list[dict], closes: list[dict],
                                start_date: str, end_date: str,
                                cap: Optional[int] = None) -> list[dict]:
    cap = _kpi.POSITIONS_MAX if cap is None else cap
    out = []
    for day in _iter_days(start_date, end_date):
        held = _kpi.positions_on_day(opens, closes, day)
        if len(held) > cap:
            out.append({
                "rule": f"同时 ≤{cap} 仓",
                "date": day,
                "actual": len(held),
                "trades": [f"{h.get('ticker')} {h.get('spread_label')}" for h in held],
            })
    return out


def check_cash_floor(opens: list[dict], closes: list[dict], start_date: str, end_date: str,
                      nlv: Optional[float] = None,
                      floor_pct: Optional[float] = None) -> list[dict]:
    nlv = _kpi.ASSUMED_NLV if nlv is None else nlv
    floor_pct = _kpi.CASH_FLOOR_PCT if floor_pct is None else floor_pct
    out = []
    if not nlv:
        return out
    for day in _iter_days(start_date, end_date):
        held = _kpi.positions_on_day(opens, closes, day)
        deployed = _kpi.deployed_capital(held)
        cash_pct = 100.0 * (1 - deployed / nlv)
        if cash_pct < floor_pct:
            out.append({
                "rule": f"现金 ≥{floor_pct:.0f}%",
                "date": day,
                "actual_cash_pct": round(cash_pct, 1),
                "positions_count": len(held),
            })
    return out


def check_deep_loss_cuts(closes: list[dict], start_date: str, end_date: str,
                          min_dte: int = DEEP_LOSS_MIN_DTE,
                          deep_pct: float = DEEP_LOSS_PCT) -> list[dict]:
    out = []
    for c in closes:
        cd = c.get("close_date") or ""
        if not (start_date <= cd <= end_date):
            continue
        parsed = parse_spread_label(c.get("spread_label"))
        if not parsed:
            continue
        try:
            close_date = dt.date.fromisoformat(cd)
        except ValueError:
            continue
        dte_at_close = (parsed["exp"].date() - close_date).days
        if dte_at_close < min_dte:
            continue  # 到期太近才平的,算正常处理/临期收尾,不算"提前止损"
        debit = _kpi.total_debit(c)
        if debit <= 0:
            continue
        net_pnl = float(c.get("net_pnl") or 0)
        pnl_pct = net_pnl / debit
        if pnl_pct <= deep_pct:
            out.append({
                "rule": "深亏时不止损",
                "date": cd,
                "trade": f"{c.get('ticker')} {c.get('spread_label')} {net_pnl:+.2f}",
                "net_pnl": round(net_pnl, 2),
                "pnl_pct": round(pnl_pct * 100, 1),
                "dte_at_close": dte_at_close,
            })
    return out


def _merge_day_run(run: list[dict]) -> dict:
    if len(run) == 1:
        return run[0]
    merged = dict(run[-1])
    dates = [r["date"] for r in run]
    merged["date"] = dates[-1]
    merged["date_range"] = f"{dates[0]}~{dates[-1]}"
    merged["n_days"] = len(run)
    if all("actual" in r for r in run):
        merged["actual"] = max(r["actual"] for r in run)
    if all("actual_cash_pct" in r for r in run):
        merged["actual_cash_pct"] = min(r["actual_cash_pct"] for r in run)
    return merged


def _collapse_consecutive_days(day_violations: list[dict]) -> list[dict]:
    """给"按天重建状态"类的检查(同时持仓上限 / 现金底线)合并连续超限的日期段,
    合成一条 date_range,避免连续多天几乎同一份持仓清单在界面上逐日刷屏(实测:一段
    12-15 仓的超限期会不合并地产出 6 条近乎重复的 violation)。⚠️只能用在 check_
    concurrent_positions / check_cash_floor 这种"date 就是状态快照日"的检查上——
    不能用在 check_deep_loss_cuts / check_position_size 这类每条都是独立个别事件
    (不同 ticker/不同交易)的检查,那些哪怕日期相邻也绝不能合并,会丢失个体身份。"""
    if not day_violations:
        return day_violations
    items = sorted(day_violations, key=lambda v: v["date"])
    out = []
    run = [items[0]]
    for v in items[1:]:
        prev_date = dt.date.fromisoformat(run[-1]["date"])
        cur_date = dt.date.fromisoformat(v["date"])
        if (cur_date - prev_date).days == 1:
            run.append(v)
        else:
            out.append(_merge_day_run(run))
            run = [v]
    out.append(_merge_day_run(run))
    return out


def all_violations(opens: list[dict], closes: list[dict], today_str: str,
                    days_back: int = 7) -> list[dict]:
    start, end = _window(today_str, days_back)
    out: list[dict] = []
    out += check_daily_open_cap(opens, start, end)
    out += check_daily_close_cap(closes, start, end)
    out += check_position_size(opens, start, end)
    out += _collapse_consecutive_days(check_concurrent_positions(opens, closes, start, end))
    out += _collapse_consecutive_days(check_cash_floor(opens, closes, start, end))
    out += check_deep_loss_cuts(closes, start, end)
    out.sort(key=lambda v: v.get("date", ""), reverse=True)
    return out

"""一句话洞察 —— 纯规则驱动(不调用任何 LLM),按优先级挑今天开盘前最值得看一眼的一条。

优先级:现金是否已破底线(今天能不能开新仓)> 本周深亏止损违反 > 其它铁律违反 >
持仓里的紧急关注项 > 都没有就报昨日盈亏 + "继续保持"。

Every sentence is written in Chinese and English side by side (build / build_en), so the page
shows a real English sentence instead of a word-by-word translation.
"""
from __future__ import annotations

from typing import Optional

Pair = tuple[str, str]   # (中文, English)


def _cash_sentence(kpi: dict) -> Optional[Pair]:
    cash_pct = kpi.get("cash_pct")
    floor = kpi.get("cash_pct_min")
    if cash_pct is not None and floor is not None and cash_pct < floor:
        return (f"现金 {cash_pct:.0f}% 低于 {floor:.0f}% 底线 —— 今天建议 0 单开新仓",
                f"Cash {cash_pct:.0f}% is below the {floor:.0f}% floor — open no new trades today")
    return None


def _deep_loss_sentence(rule_violations: list[dict]) -> Optional[Pair]:
    deep = [v for v in rule_violations if v.get("rule") == "深亏时不止损"]
    if not deep:
        return None
    total = sum(float(v.get("net_pnl") or 0) for v in deep)
    n = len(deep)
    return (f"本周深亏时砍单 {n} 笔,合计 ${total:,.0f}"
            f"(违反『借方价差不止损』—— 记得让它死等到期结算)",
            f"This week you cut {n} trade{'' if n == 1 else 's'} at a deep loss, "
            f"{'−' if total < 0 else ''}${abs(total):,.0f} in all "
            f"(against \"no stop-loss on debit spreads\": let them run to expiration)")


def _other_violation_sentence(rule_violations: list[dict]) -> Optional[Pair]:
    others = [v for v in rule_violations if v.get("rule") != "深亏时不止损"]
    if not others:
        return None
    counts: dict[str, int] = {}
    names_en: dict[str, str] = {}
    for v in others:
        counts[v["rule"]] = counts.get(v["rule"], 0) + 1
        names_en[v["rule"]] = v.get("rule_en") or v["rule"]
    top_rule = max(counts, key=counts.get)
    n = counts[top_rule]
    return (f"本周『{top_rule}』违反 {n} 次 —— 点开下面明细看是哪几笔",
            f"This week the rule \"{names_en[top_rule]}\" was broken {n} time{'' if n == 1 else 's'}"
            f" — open the details below to see which trades")


def _watchlist_sentence(watchlist: list[dict]) -> Optional[Pair]:
    deep = [w for w in watchlist if "深亏" in (w.get("attention") or [])]
    if deep:
        tickers = [w["ticker"] for w in deep[:3]]
        return (f"当前持仓有 {len(deep)} 笔深亏({'、'.join(tickers)})—— 记住不止损,让它死等到期结算",
                f"{len(deep)} open position{'' if len(deep) == 1 else 's'} at a deep loss ({', '.join(tickers)})"
                f" — remember: no stop-loss, let them run to expiration")
    urgent = [w for w in watchlist if w.get("attention")]
    if urgent:
        tickers = [w["ticker"] for w in urgent[:3]]
        return (f"{len(urgent)} 笔持仓今天需要多看两眼({'、'.join(tickers)})",
                f"{len(urgent)} position{'' if len(urgent) == 1 else 's'} need a second look today ({', '.join(tickers)})")
    return None


def _pairs(kpi: dict, rule_violations: list[dict], closed_yesterday: list[dict],
           watchlist: list[dict]) -> list[Pair]:
    parts: list[Pair] = []

    cash_note = _cash_sentence(kpi)
    if cash_note:
        parts.append(cash_note)

    deep_note = _deep_loss_sentence(rule_violations)
    if deep_note:
        parts.append(deep_note)
    else:
        other_note = _other_violation_sentence(rule_violations)
        if other_note:
            parts.append(other_note)

    if not parts:
        wl_note = _watchlist_sentence(watchlist)
        if wl_note:
            parts.append(wl_note)

    if not parts:
        yn = kpi.get("yesterday_net_pnl") or 0
        if yn > 0:
            parts.append((f"昨天净盈利 ${yn:,.0f},本周未违反铁律 —— 继续保持纪律",
                          f"Net profit of ${yn:,.0f} yesterday and no rules broken this week — keep up the discipline"))
        elif yn < 0:
            parts.append((f"昨天净亏损 ${abs(yn):,.0f},本周未违反铁律 —— 亏损在计划内,别因此破例",
                          f"Net loss of ${abs(yn):,.0f} yesterday and no rules broken this week"
                          " — the loss is within plan, don't break your rules over it"))
        else:
            parts.append(("昨天无平仓记录,本周未违反铁律 —— 继续保持纪律",
                          "No trades closed yesterday and no rules broken this week — keep up the discipline"))
    return parts


def build(kpi: dict, rule_violations: list[dict], closed_yesterday: list[dict],
          watchlist: list[dict]) -> str:
    return " · ".join(zh for zh, _ in _pairs(kpi, rule_violations, closed_yesterday, watchlist))


def build_en(kpi: dict, rule_violations: list[dict], closed_yesterday: list[dict],
             watchlist: list[dict]) -> str:
    return " · ".join(en for _, en in _pairs(kpi, rule_violations, closed_yesterday, watchlist))

"""Deterministic option-spread economics from leg fills — generalizes
max_profit / max_loss to any single-expiry multi-leg structure via the
piecewise-linear expiry payoff. Calendars (multi-expiry) return N/A.

No I/O, pure functions. Used by both the entry-economics backfill and the
what-if engine so the math lives in exactly one place.
"""
from __future__ import annotations
import datetime
from typing import Optional


def _signed_premium_per_share(legs: list[dict]) -> float:
    """Net premium per 1-lot spread, per share. Convention: cash OUT (debit)
    is POSITIVE cost. = Σ (buy price) − Σ (sell price)."""
    total = 0.0
    for l in legs:
        price = float(l.get("price") or 0)
        action = l.get("action") or ""
        if action.startswith("Buy"):
            total += price
        elif action.startswith("Sell"):
            total -= price
    return total  # >0 net debit, <0 net credit


def _expiries(legs: list[dict]) -> set:
    return {(l.get("exp") or "").strip() for l in legs if l.get("exp")}


def _payoff_at(legs: list[dict], spot: float) -> float:
    """Intrinsic value of the whole position at expiry for underlying=spot,
    per 1-lot, per share. Long(+)/short(−) by action."""
    val = 0.0
    for l in legs:
        k = float(l.get("strike") or 0)
        typ = (l.get("type") or "").upper()
        sign = 1 if (l.get("action") or "").startswith("Buy") else -1
        if typ == "C":
            intrinsic = max(0.0, spot - k)
        elif typ == "P":
            intrinsic = max(0.0, k - spot)
        else:
            intrinsic = 0.0
        val += sign * intrinsic
    return val


def spread_economics(legs: list[dict]) -> dict:
    """Return {net_premium, is_credit, max_profit, max_loss, spread_width, note}.
    All dollar fields per 1-lot spread in DOLLARS (×100). max_profit/max_loss
    are POSITIVE magnitudes. Calendars → max_* = None, note='calendar_na'."""
    legs = [l for l in (legs or []) if l.get("strike") is not None]
    if not legs:
        return {"net_premium": None, "is_credit": None, "max_profit": None,
                "max_loss": None, "spread_width": None, "note": "no_legs"}

    prem_share = _signed_premium_per_share(legs)
    is_credit = prem_share < 0
    net_premium = round(abs(prem_share) * 100, 2)

    strikes = sorted({float(l["strike"]) for l in legs})
    width = round(max(strikes) - min(strikes), 2) if len(strikes) > 1 else 0.0

    if len(_expiries(legs)) > 1:
        return {"net_premium": net_premium, "is_credit": is_credit,
                "max_profit": None, "max_loss": None,
                "spread_width": width, "note": "calendar_na"}

    test_pts = [0.0] + strikes + [max(strikes) * 2 + 1]
    pls = [(_payoff_at(legs, s) - prem_share) * 100 for s in test_pts]
    max_profit = round(max(pls), 2)
    max_loss = round(abs(min(pls)), 2)
    return {"net_premium": net_premium, "is_credit": is_credit,
            "max_profit": max_profit, "max_loss": max_loss,
            "spread_width": width, "note": "ok"}


def _exp_to_date(yymmdd: str) -> Optional[datetime.date]:
    s = (yymmdd or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    try:
        return datetime.date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
    except ValueError:
        return None


def dte_at_open(legs: list[dict], open_date: str) -> Optional[int]:
    """Days from open_date (YYYY-MM-DD) to first leg expiry."""
    if not legs:
        return None
    exp = _exp_to_date(legs[0].get("exp", ""))
    try:
        od = datetime.date.fromisoformat((open_date or "")[:10])
    except (ValueError, TypeError):
        return None
    if not exp:
        return None
    return (exp - od).days

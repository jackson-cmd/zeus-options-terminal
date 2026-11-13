"""Missing take-profit GTC watcher — pure logic.

User's discipline (per journal/回测): every open spread should carry a
take-profit closing GTC on the book. If it doesn't, we forget to lock in gains
when the price moonshots overnight or on our lunch break. This module answers
one question: which open (ticker, expiration) groups have NO working closing
order on TastyTrade right now?

Match rule — deliberately loose per user brief:
    "只要有对应 closing GTC 就算过, 不算 90%"

So a Live/Received order whose underlying == ticker AND at least one leg is
a "to Close" action at the matching expiration → covers that group. We don't
gate on price, on quantity, on which specific strikes it closes, or on whether
it happens to be a partial. If it exists, we assume the user meant it.

Called from optionstrat_server.py's async loop (macOS push + PWA red badge +
checkup banner). Split out here so the matching logic is unit-testable with
canned dicts and never has to import FastAPI or the TT client.
"""
from __future__ import annotations

from typing import Iterable


# TastyTrade order status codes. "Live" = GTC sitting on the book; "Received"
# = just submitted, awaiting routing. "Routed" and "In Flight" are transient
# in-between states we treat as working too (belt & suspenders — cheaper to
# suppress a false alarm during a 200ms routing hop than to alarm the user
# right after they placed the order).
_WORKING_STATUSES = frozenset({"Live", "Received", "Routed", "In Flight"})


def is_working_order(order: dict) -> bool:
    """True iff this order is still on the book (not filled / cancelled /
    rejected / expired)."""
    return (order or {}).get("status") in _WORKING_STATUSES


def parse_occ_expiration(sym: str) -> str:
    """OCC symbol → 'YYYY-MM-DD' (or '' if unparseable).

    OCC format: 'AAPL  261218C00200000' → '2026-12-18'.
    """
    parts = (sym or "").strip().split()
    if len(parts) < 2:
        return ""
    rest = parts[-1]
    if len(rest) < 6:
        return ""
    yy, mm, dd = rest[:2], rest[2:4], rest[4:6]
    if not (yy.isdigit() and mm.isdigit() and dd.isdigit()):
        return ""
    return f"20{yy}-{mm}-{dd}"


def order_covers_group(order: dict, ticker: str, exp: str) -> bool:
    """Does this working order count as the closing GTC for the (ticker, exp)
    group? Any leg with a 'to Close' action at the matching expiration is
    enough — see module docstring for why the rule is loose."""
    if (order or {}).get("underlying-symbol", "").upper() != (ticker or "").upper():
        return False
    for leg in (order.get("legs") or []):
        if "to Close" not in (leg.get("action") or ""):
            continue
        if parse_occ_expiration(leg.get("symbol") or "") == exp:
            return True
    return False


def open_spread_groups(strategies: Iterable[dict]) -> list[dict]:
    """Group open option positions by (ticker, expiration).

    A calendar or diagonal contributes multiple groups (one per unique exp) —
    that's correct, because a closing GTC on the near leg does NOT cover the
    far leg. Legs with qty==0 are skipped (already closed, still on the
    strategy card until refresh).

    Returns [{ticker, exp, strategy_indices: [i,...]}] with stable order:
    ticker asc, exp asc.
    """
    groups: dict[tuple[str, str], dict] = {}
    for i, s in enumerate(strategies or []):
        und = (s.get("underlying") or "").upper()
        if not und:
            continue
        seen_exps: set[str] = set()
        for leg in (s.get("legs") or []):
            try:
                qty = int(leg.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty == 0:
                continue
            exp = (leg.get("exp") or "").strip()
            if exp:
                seen_exps.add(exp)
        for exp in seen_exps:
            key = (und, exp)
            g = groups.get(key)
            if g is None:
                g = {"ticker": und, "exp": exp, "strategy_indices": []}
                groups[key] = g
            g["strategy_indices"].append(i)
    return sorted(groups.values(), key=lambda g: (g["ticker"], g["exp"]))


def find_missing_gtc(strategies: Iterable[dict],
                     live_orders: Iterable[dict]) -> list[dict]:
    """Main entry: for every open (ticker, exp) group, check if any working
    order covers it. Return the ones that DON'T — that's the alert list.

    Returns [{ticker, exp, strategy_indices}] in the same stable order as
    open_spread_groups().
    """
    working = [o for o in (live_orders or []) if is_working_order(o)]
    missing = []
    for g in open_spread_groups(strategies):
        if not any(order_covers_group(o, g["ticker"], g["exp"]) for o in working):
            missing.append(g)
    return missing


def missing_key(g: dict) -> str:
    """Stable key for a group — used by the alert loop's `fired_today` dedupe
    so we don't spam macOS notifications every 60s for the same gap."""
    return f"{g['ticker']}|{g['exp']}"


def find_expiring_today(strategies: Iterable[dict], today_iso: str) -> list[dict]:
    """Every open (ticker, exp) group whose exp == today. Same shape as
    find_missing_gtc so the frontend can render both bars uniformly.

    `today_iso` is a 'YYYY-MM-DD' string in the market's timezone (ET) —
    passed in explicitly so tests can pin the date and so we don't couple this
    module to zoneinfo.
    """
    return [g for g in open_spread_groups(strategies) if g.get("exp") == today_iso]

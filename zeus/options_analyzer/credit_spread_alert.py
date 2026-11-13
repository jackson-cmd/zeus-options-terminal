"""Server-side watcher: detect newly-opened CREDIT vertical spreads → native push.

The user routinely *intends* a debit vertical (e.g. a bull call spread — pay
premium, directional long) but mis-configures the order so it fills as a CREDIT
vertical (e.g. a bear call spread — receive premium, opposite direction). This
module flags exactly that mistake: a 2-leg vertical that came in net-credit.

A "credit vertical" here means, for one strategy (legs already grouped by
underlying + open day in optionstrat_analyzer.group_into_strategies):

    * exactly 2 legs
    * both the same option type (both calls or both puts)
    * both the same expiration
    * opposite directions (one long qty>0, one short qty<0)
    * net credit received at entry:  net_debit = Σ(entry·qty·mult) < 0

This deliberately ignores single-leg shorts (cash-secured puts), iron condors
(4 legs) and calendars/diagonals (different expirations) — those are commonly
*intentional* credit structures, not the directional mistake we warn about.

Fires ONCE per spread: a stable key (underlying|open-day|type|strikes) is
persisted to data/credit_spread_seen.json so a restart doesn't re-alert, and so
positions opened before this watcher existed can be seeded as "already seen" on
first run (no alert storm).

Pure helpers live here so they're unit-testable without the FastAPI app;
optionstrat_server.py owns the loop because it needs live `state.strategies`.
"""
from __future__ import annotations

import json
from pathlib import Path


def net_debit(legs) -> float:
    """Net cash at entry: Σ(entry·qty·mult). `qty` is signed (+long, −short) and
    `entry` is the (positive) average open price per contract. >0 = net debit
    paid (debit spread); <0 = net credit received (credit spread)."""
    total = 0.0
    for leg in legs or []:
        try:
            total += (float(leg.get("entry", 0) or 0)
                      * float(leg.get("qty", 0) or 0)
                      * float(leg.get("mult", 100) or 100))
        except (TypeError, ValueError):
            continue
    return total


def is_credit_vertical(strategy) -> bool:
    """True iff the strategy is a 2-leg, same-type, same-expiration vertical that
    received a net credit at entry — the debit→credit direction mistake."""
    legs = (strategy or {}).get("legs", []) or []
    if len(legs) != 2:
        return False
    a, b = legs
    if (a.get("type") or "") != (b.get("type") or ""):      # same option type
        return False
    if (a.get("exp") or "") != (b.get("exp") or ""):        # same expiration
        return False
    try:
        qa = float(a.get("qty", 0) or 0)
        qb = float(b.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    if qa == 0 or qb == 0 or (qa > 0) == (qb > 0):          # one long, one short
        return False
    return net_debit(legs) < 0                              # net credit received


def strategy_key(strategy) -> str:
    """Stable identity for a spread across refreshes: underlying | open-day |
    type | sorted strikes. The same opened position yields the same key on every
    fetch, so the seen-set fires at most one alert per spread."""
    s = strategy or {}
    und = s.get("underlying", "") or ""
    open_day = (s.get("created_at", "") or "")[:10]
    legs = s.get("legs", []) or []
    typ = "".join(sorted({(l.get("type") or "") for l in legs}))
    strikes = ",".join(
        f"{float(l.get('strike', 0) or 0):g}"
        for l in sorted(legs, key=lambda l: float(l.get("strike", 0) or 0))
    )
    return f"{und}|{open_day}|{typ}|{strikes}"


def credit_vertical_keys(strategies) -> set:
    """Keys of every currently-open credit vertical — used to seed `seen` on the
    first run so pre-existing spreads don't trigger a notification storm."""
    return {strategy_key(s) for s in (strategies or []) if is_credit_vertical(s)}


def spread_summary(strategy) -> dict:
    """Notification payload for one credit vertical: ticker, name, the net credit
    received (positive $), short/long strikes and the stable key."""
    legs = strategy.get("legs", []) or []
    credit = -net_debit(legs)  # net_debit<0 for a credit → positive amount received
    short_leg = next((l for l in legs if float(l.get("qty", 0) or 0) < 0), None)
    long_leg = next((l for l in legs if float(l.get("qty", 0) or 0) > 0), None)
    typ = (legs[0].get("type") if legs else "") or ""

    def _strike(leg):
        return f"{float(leg.get('strike', 0) or 0):g}" if leg else "?"

    return {
        "ticker": strategy.get("underlying", "") or "",
        "name": strategy.get("name", "") or "",
        "type": "Call" if typ == "C" else ("Put" if typ == "P" else typ),
        "credit": round(credit, 2),
        "short_strike": _strike(short_leg),
        "long_strike": _strike(long_leg),
        "key": strategy_key(strategy),
    }


def is_enabled(cfg) -> bool:
    """Credit-spread alerts are governed by their OWN toggle (default on) and are
    INDEPENDENT of the master "Alerts Enabled" switch — the user explicitly asked
    for this direction-mistake safety warning, so it still fires even when the
    other alerts (SL/TP/exit) are muted. Turn it off only via creditSpread.enabled
    = false (the "Credit Spread 开反提醒" toggle in the Alert panel)."""
    if not isinstance(cfg, dict):
        return True
    cs = cfg.get("creditSpread")
    if isinstance(cs, dict) and cs.get("enabled") is False:
        return False
    return True


def evaluate(strategies, seen) -> list:
    """Pure: return the list of NEW credit verticals (summary dicts) whose key is
    not already in `seen`. Does NOT mutate `seen` and does NOT notify — the
    caller marks-seen/persists only after a push succeeds, so a failed push can
    be retried next cycle."""
    fires = []
    for s in strategies or []:
        try:
            if not is_credit_vertical(s):
                continue
            if strategy_key(s) in seen:
                continue
            fires.append(spread_summary(s))
        except Exception:
            continue
    return fires


# ── seen-set persistence (one push per spread, survives restarts) ──

def load_seen(path) -> set:
    try:
        p = Path(path)
        if not p.exists():
            return set()
        raw = json.loads(p.read_text())
        if isinstance(raw, list):
            return set(raw)
        if isinstance(raw, dict):
            return set(raw.get("seen", []))
    except Exception:
        pass
    return set()


def save_seen(path, seen) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(seen), indent=2))
    except Exception:
        pass


def load_alert_config(path) -> dict:
    """Read the same alert_config.json the PWA syncs. Returns {} on any problem
    so is_enabled() falls back to its (enabled) defaults."""
    try:
        p = Path(path)
        if p.exists():
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}

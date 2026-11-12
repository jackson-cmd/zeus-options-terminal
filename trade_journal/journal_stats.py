"""Trade-journal analytics layer.

Pure functions over the journal entries (loaded from journal_entries.py's
JSONL files). No I/O on hot paths; load once, run aggregations.

Every public function takes a *list of trade dicts* and returns plain
JSON-serialisable data — no objects, no dataframes — so it round-trips
through the FastAPI endpoint cleanly.

A "trade" here = one open entry joined with its matching close entry (by
trade_id). Open positions are excluded from realized stats but available
for the active-positions count.

See TRADE_JOURNAL_STATS_PLAN.md for the broader feature plan.
"""
from __future__ import annotations

import datetime
import math
from collections import Counter, defaultdict
from typing import Any, Optional

from journal_entries import load_close_entries, load_open_entries


# ── Public types (informal) ───────────────────────────────────────────────
# trade = {
#   "trade_id", "ticker", "strategy", "direction", "spread_label",
#   "open_at", "close_at", "open_date", "close_date",
#   "quantity", "open_price",  # per-spread $ (negative=debit, positive=credit)
#   "net_pnl",                  # realized $ (post-commission/fees)
#   "max_loss_total", "max_profit_total",  # computed once at trade build time
#   "r_multiple",               # net_pnl / |max_loss_total|
#   "hold_days",                # close_at − open_at, fractional days
#   "dte_at_open",              # expiration − open_at, in days
#   "open_entry": {…full open journal…},
#   "close_entry": {…full close journal…},
# }


# ── Pure helpers ──────────────────────────────────────────────────────────

def _parse_iso(s: str) -> Optional[datetime.datetime]:
    """Parse YYYY-MM-DD or full ISO. Always returns an *offset-naive* UTC
    datetime so we never mix tz-aware and tz-naive in arithmetic."""
    if not s:
        return None
    s = s.strip()
    # Strip Z and any +00:00 / +HH:MM offset (we treat all stored times as UTC
    # for stat purposes; ET conversion done by callers as needed)
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s[10:]:
        s = s[: s.rindex("+")]
    if "-" in s[10:]:
        # only strip a trailing tz offset like '-04:00', not the date dashes
        tail = s[10:]
        if ":" in tail:
            idx = tail.rfind("-")
            if idx >= 0 and len(tail) - idx <= 7:
                s = s[:10] + tail[:idx]
    # Strip sub-millisecond fractional seconds if present (3.9 fromisoformat
    # only accepts 3 or 6 digits)
    if "." in s:
        head, frac = s.rsplit(".", 1)
        # Keep only digits in frac
        frac = "".join(ch for ch in frac if ch.isdigit())
        if len(frac) > 6:
            frac = frac[:6]
        s = f"{head}.{frac}" if frac else head
    try:
        return datetime.datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        try:
            return datetime.datetime.fromisoformat(s + "T16:00:00")
        except (ValueError, AttributeError):
            return None


def _exp_to_date(yymmdd: str) -> Optional[datetime.date]:
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    yy = int(yymmdd[:2]) + 2000
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    try:
        return datetime.date(yy, mm, dd)
    except ValueError:
        return None


def _max_loss_per_spread(legs: list[dict], open_price: float) -> Optional[float]:
    """For verticals (2 legs same exp same type): return $-max-loss/spread.
    Negative = debit spread risks the debit; positive = credit spread risks
    (width − credit). Returns None for non-verticals."""
    if not legs or len(legs) != 2:
        return None
    if legs[0].get("type") != legs[1].get("type") or legs[0].get("exp") != legs[1].get("exp"):
        return None
    width_dollars = abs(legs[1]["strike"] - legs[0]["strike"]) * 100
    if open_price < 0:
        # Debit
        return -abs(open_price)
    if open_price > 0:
        # Credit (max loss = width − credit, expressed negative)
        return -(width_dollars - open_price)
    return None


def _max_profit_per_spread(legs: list[dict], open_price: float) -> Optional[float]:
    """For verticals: return $-max-profit/spread (positive)."""
    if not legs or len(legs) != 2:
        return None
    if legs[0].get("type") != legs[1].get("type") or legs[0].get("exp") != legs[1].get("exp"):
        return None
    width_dollars = abs(legs[1]["strike"] - legs[0]["strike"]) * 100
    if open_price < 0:
        # Debit profit = width − debit
        return width_dollars - abs(open_price)
    if open_price > 0:
        return abs(open_price)
    return None


def _bucket_holdtime(days: float) -> str:
    if days < 1:
        return "0-1d"
    if days < 3:
        return "1-3d"
    if days < 7:
        return "3-7d"
    if days < 30:
        return "7-30d"
    return "30d+"


def _bucket_dte(days: int) -> str:
    if days <= 7:
        return "0-7"
    if days <= 21:
        return "8-21"
    if days <= 45:
        return "22-45"
    return "45+"


def _bucket_hour(et_hour: int) -> str:
    """Categorize a US/Eastern-time entry hour."""
    if et_hour < 9:
        return "pre-market"
    if et_hour < 11:
        return "open"
    if et_hour < 14:
        return "midday"
    if et_hour < 16:
        return "close"
    return "after-hours"


# ── Trade builder ─────────────────────────────────────────────────────────

def build_trades(open_entries: Optional[list[dict]] = None,
                  close_entries: Optional[list[dict]] = None) -> list[dict]:
    """Join open + close entries on trade_id; compute derived fields.
    Open trades (no matching close) are dropped from realized stats."""
    opens = open_entries if open_entries is not None else load_open_entries()
    closes = close_entries if close_entries is not None else load_close_entries()
    close_by_tid = {e["trade_id"]: e for e in closes}

    out = []
    for o in opens:
        c = close_by_tid.get(o.get("trade_id"))
        if not c:
            continue  # skip still-open trades
        legs = o.get("legs") or []
        qty = float(o.get("quantity") or 1)
        open_price = float(o.get("open_price") or 0)
        ml_per = _max_loss_per_spread(legs, open_price)
        mp_per = _max_profit_per_spread(legs, open_price)
        max_loss_total = ml_per * qty if ml_per is not None else None
        max_profit_total = mp_per * qty if mp_per is not None else None

        net_pnl = float(c.get("net_pnl") or 0)
        r_multiple = None
        if max_loss_total and max_loss_total != 0:
            r_multiple = net_pnl / abs(max_loss_total)

        # Hold time
        open_dt = _parse_iso(o.get("open_at") or o.get("open_date") or "")
        close_dt = _parse_iso(c.get("close_at") or c.get("close_date") or "")
        hold_days = None
        if open_dt and close_dt:
            hold_days = (close_dt - open_dt).total_seconds() / 86400

        # DTE at open
        dte = None
        if legs and open_dt:
            exp_d = _exp_to_date(legs[0].get("exp", ""))
            if exp_d:
                dte = (exp_d - open_dt.date()).days

        out.append({
            "trade_id": o["trade_id"],
            "ticker": o.get("ticker"),
            "strategy": o.get("strategy"),
            "direction": o.get("direction"),
            "spread_label": o.get("spread_label"),
            "open_at": o.get("open_at"),
            "close_at": c.get("close_at"),
            "open_date": o.get("open_date"),
            "close_date": c.get("close_date"),
            "quantity": qty,
            "open_price": open_price,
            "net_pnl": net_pnl,
            "max_loss_total": max_loss_total,
            "max_profit_total": max_profit_total,
            "r_multiple": r_multiple,
            "hold_days": hold_days,
            "dte_at_open": dte,
            "open_entry": o,
            "close_entry": c,
        })
    return out


# ── KPI tiles ─────────────────────────────────────────────────────────────

def kpi_hero(trades: list[dict]) -> dict:
    """The 9 dashboard-top stats every paid journal exposes."""
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "expectancy_dollar": 0.0, "expectancy_r": 0.0,
            "avg_winner": 0.0, "avg_loser": 0.0, "max_dd_dollar": 0.0,
            "current_streak": {"type": "none", "len": 0},
            "max_win_streak": 0, "max_loss_streak": 0,
            "total_pnl": 0.0,
        }
    pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    n = len(pnls)
    win_rate = len(wins) / n if n else 0
    avg_winner = sum(wins) / len(wins) if wins else 0
    avg_loser = sum(losses) / len(losses) if losses else 0
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    profit_factor = gross_w / gross_l if gross_l > 0 else (
        float("inf") if gross_w > 0 else 0
    )
    expectancy = total / n if n else 0

    # R-multiple expectancy (only over trades that have an R)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    expectancy_r = sum(rs) / len(rs) if rs else 0

    # Max drawdown on the cumulative P/L (sorted by close time)
    sorted_t = sorted(
        trades,
        key=lambda t: (t.get("close_at") or t.get("close_date") or "")
    )
    running, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted_t:
        running += t["net_pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # Streaks
    cur_w, cur_l = 0, 0
    max_w, max_l = 0, 0
    for t in sorted_t:
        if t["net_pnl"] > 0:
            cur_w += 1; cur_l = 0
            if cur_w > max_w: max_w = cur_w
        else:
            cur_l += 1; cur_w = 0
            if cur_l > max_l: max_l = cur_l
    if cur_w > 0:
        current = {"type": "win", "len": cur_w}
    elif cur_l > 0:
        current = {"type": "loss", "len": cur_l}
    else:
        current = {"type": "none", "len": 0}

    return {
        "total_trades": n,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "expectancy_dollar": round(expectancy, 2),
        "expectancy_r": round(expectancy_r, 4),
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "max_dd_dollar": round(max_dd, 2),
        "current_streak": current,
        "max_win_streak": max_w,
        "max_loss_streak": max_l,
        "total_pnl": round(total, 2),
    }


# ── Equity curve (cumulative P/L + underwater drawdown) ──────────────────

def equity_curve(trades: list[dict]) -> list[dict]:
    """Returns one row per closed trade in chronological order:
       {date, pnl, cumulative, drawdown}."""
    sorted_t = sorted(
        trades,
        key=lambda t: (t.get("close_at") or t.get("close_date") or "")
    )
    running, peak = 0.0, 0.0
    out = []
    for t in sorted_t:
        running += t["net_pnl"]
        peak = max(peak, running)
        dd = peak - running  # underwater = positive when below peak
        out.append({
            "date": t.get("close_date") or (t.get("close_at") or "")[:10],
            "pnl": round(t["net_pnl"], 2),
            "cumulative": round(running, 2),
            "drawdown": round(-dd, 2),  # negative for plotting underwater
            "ticker": t.get("ticker"),
        })
    return out


# ── R-multiple histogram ─────────────────────────────────────────────────

R_BUCKETS = [
    ("≤−2R", float("-inf"), -2),
    ("−2R to −1R", -2, -1),
    ("−1R to −0.5R", -1, -0.5),
    ("−0.5R to 0", -0.5, 0),
    ("0 to +0.5R", 0, 0.5),
    ("+0.5R to +1R", 0.5, 1),
    ("+1R to +2R", 1, 2),
    ("≥+2R", 2, float("inf")),
]


def r_distribution(trades: list[dict]) -> list[dict]:
    """Histogram of R-multiples."""
    out = []
    for label, lo, hi in R_BUCKETS:
        items = [t for t in trades if t.get("r_multiple") is not None
                  and lo < t["r_multiple"] <= hi]
        # The 0 boundary needs special handling so 0 doesn't get double-counted
        # We've used (lo, hi] so a trade with r_multiple=0 lands in (-0.5, 0]
        out.append({
            "bucket": label,
            "lo": lo if math.isfinite(lo) else None,
            "hi": hi if math.isfinite(hi) else None,
            "count": len(items),
            "net_pnl": round(sum(t["net_pnl"] for t in items), 2),
        })
    return out


# ── Generic group-by aggregator ──────────────────────────────────────────

def _aggregate(items: list[dict]) -> dict:
    """Aggregate stats for a single group of trades."""
    if not items:
        return {"n": 0, "wins": 0, "win_rate": 0,
                "net_pnl": 0, "avg_pnl": 0, "avg_r": 0,
                "expectancy": 0, "profit_factor": 0,
                "best": 0, "worst": 0}
    pnls = [t["net_pnl"] for t in items]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [t["r_multiple"] for t in items if t.get("r_multiple") is not None]
    gross_w = sum(wins); gross_l = abs(sum(losses))
    pf = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0)
    return {
        "n": len(items),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(items), 4) if items else 0,
        "net_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else 0,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0,
        "profit_factor": round(pf, 3) if pf != float("inf") else None,
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
    }


def _group_by(trades: list[dict], key_fn, key_label: str = "key") -> list[dict]:
    """Generic group-by helper. key_fn(trade) → bucket key (or None to skip)."""
    buckets: dict[Any, list[dict]] = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        buckets[k].append(t)
    out = []
    for k, items in buckets.items():
        agg = _aggregate(items)
        out.append({key_label: k, **agg})
    return out


# ── Time / DTE / strategy / direction / ticker breakdowns ────────────────

_HOLDTIME_ORDER = ["0-1d", "1-3d", "3-7d", "7-30d", "30d+"]
_DTE_ORDER = ["0-7", "8-21", "22-45", "45+"]
_HOUR_ORDER = ["pre-market", "open", "midday", "close", "after-hours"]
_DOW_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def by_holdtime(trades: list[dict]) -> list[dict]:
    rows = _group_by(
        trades,
        lambda t: _bucket_holdtime(t["hold_days"]) if t.get("hold_days") is not None else None,
        "bucket",
    )
    rows.sort(key=lambda r: _HOLDTIME_ORDER.index(r["bucket"])
              if r["bucket"] in _HOLDTIME_ORDER else 99)
    return rows


def by_dte_at_open(trades: list[dict]) -> list[dict]:
    rows = _group_by(
        trades,
        lambda t: _bucket_dte(t["dte_at_open"]) if t.get("dte_at_open") is not None else None,
        "bucket",
    )
    rows.sort(key=lambda r: _DTE_ORDER.index(r["bucket"])
              if r["bucket"] in _DTE_ORDER else 99)
    return rows


def by_strategy(trades: list[dict]) -> list[dict]:
    return sorted(_group_by(trades, lambda t: t.get("strategy") or "?", "strategy"),
                   key=lambda r: -r["n"])


def by_direction(trades: list[dict]) -> list[dict]:
    return sorted(_group_by(trades, lambda t: t.get("direction") or "?", "direction"),
                   key=lambda r: -r["n"])


def by_ticker(trades: list[dict]) -> list[dict]:
    return sorted(_group_by(trades, lambda t: t.get("ticker") or "?", "ticker"),
                   key=lambda r: -r["net_pnl"])


def by_dow(trades: list[dict]) -> list[dict]:
    def key(t):
        dt = _parse_iso(t.get("open_at") or "")
        if not dt:
            return None
        return _DOW_ORDER[dt.weekday()]
    rows = _group_by(trades, key, "dow")
    rows.sort(key=lambda r: _DOW_ORDER.index(r["dow"]) if r["dow"] in _DOW_ORDER else 99)
    return rows


def by_hour(trades: list[dict]) -> list[dict]:
    def key(t):
        dt = _parse_iso(t.get("open_at") or "")
        if not dt:
            return None
        # Treat naive datetimes as UTC; convert to ET (~UTC-4)
        et_hour = (dt.hour - 4) % 24
        return _bucket_hour(et_hour)
    rows = _group_by(trades, key, "bucket")
    rows.sort(key=lambda r: _HOUR_ORDER.index(r["bucket"])
              if r["bucket"] in _HOUR_ORDER else 99)
    return rows


# ── Calendar heatmap ─────────────────────────────────────────────────────

def calendar_heatmap(trades: list[dict]) -> dict:
    """{ 'YYYY-MM-DD': {pnl, count} }, keyed by close_date."""
    out: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    for t in trades:
        d = t.get("close_date") or (t.get("close_at") or "")[:10]
        if not d:
            continue
        out[d]["pnl"] += t["net_pnl"]
        out[d]["count"] += 1
    # round and JSON-ify
    return {d: {"pnl": round(v["pnl"], 2), "count": v["count"]}
            for d, v in out.items()}


# ── Filtering ─────────────────────────────────────────────────────────────

def apply_filters(trades: list[dict], filters: Optional[dict]) -> list[dict]:
    """Filter the trade list. Supported filters: from, to (YYYY-MM-DD),
    strategy, direction, ticker."""
    if not filters:
        return list(trades)
    out = list(trades)
    if filters.get("from"):
        out = [t for t in out if (t.get("open_date") or "") >= filters["from"]]
    if filters.get("to"):
        out = [t for t in out if (t.get("open_date") or "") <= filters["to"]]
    if filters.get("strategy"):
        out = [t for t in out if t.get("strategy") == filters["strategy"]]
    if filters.get("direction"):
        out = [t for t in out if t.get("direction") == filters["direction"]]
    if filters.get("ticker"):
        out = [t for t in out if t.get("ticker") == filters["ticker"]]
    return out


# ── Tier 2: behavioral / decision-quality stats (use user-filled fields) ─

def quadrant_breakdown(trades: list[dict]) -> dict:
    """2x2 process-vs-result grid. Uses close_entry.quadrant (filled by user).
    Each cell: count, share %, avg P/L. Plus an outcome-bias flag."""
    cells = {
        "good_dec_good_res": [], "good_dec_bad_res": [],
        "bad_dec_good_res":  [], "bad_dec_bad_res":  [],
    }
    for t in trades:
        q = (t.get("close_entry") or {}).get("quadrant")
        if q in cells:
            cells[q].append(t)
    total = sum(len(v) for v in cells.values())
    out = {"total_filled": total, "cells": {}}
    for k, items in cells.items():
        n = len(items)
        net = sum(t["net_pnl"] for t in items) if items else 0
        out["cells"][k] = {
            "n": n,
            "share": round(n / total, 4) if total else 0,
            "net_pnl": round(net, 2),
            "avg_pnl": round(net / n, 2) if n else 0,
        }
    # Outcome-bias flag: bad-decision-good-result share > 10% means outcome
    # bias is masking a process problem
    bdgr = out["cells"]["bad_dec_good_res"]["share"] if total else 0
    out["outcome_bias_alert"] = bdgr > 0.10 and total >= 5
    return out


# Confidence options as defined in DEFAULT_OPTIONS["confidence"] —
# ordered low-to-high so the calibration curve is monotonic on the x-axis.
_CONFIDENCE_ORDER = ["Gambling", "Low", "Medium", "High", "System Signal"]


def confidence_calibration(trades: list[dict]) -> list[dict]:
    """Bin trades by stated confidence at open; show empirical win rate per bin.
    A perfectly calibrated trader has monotonically rising win rate."""
    by_bin: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        c = (t.get("open_entry") or {}).get("confidence")
        if c:
            by_bin[c].append(t)
    out = []
    # Add only bins that exist; sort by canonical order
    for bin_name in _CONFIDENCE_ORDER:
        items = by_bin.get(bin_name)
        if not items:
            continue
        n = len(items)
        wins = sum(1 for t in items if t["net_pnl"] > 0)
        out.append({
            "confidence": bin_name,
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "avg_pnl": round(sum(t["net_pnl"] for t in items) / n, 2),
            "net_pnl": round(sum(t["net_pnl"] for t in items), 2),
        })
    # Append any non-canonical bins at the end (custom user values)
    for bin_name, items in by_bin.items():
        if bin_name in _CONFIDENCE_ORDER:
            continue
        n = len(items); wins = sum(1 for t in items if t["net_pnl"] > 0)
        out.append({
            "confidence": bin_name, "n": n, "wins": wins,
            "win_rate": round(wins / n, 4) if n else 0,
            "avg_pnl": round(sum(t["net_pnl"] for t in items) / n, 2) if n else 0,
            "net_pnl": round(sum(t["net_pnl"] for t in items), 2),
        })
    return out


_EMOTION_KEYS = ["excitement", "anxiety", "fomo", "revenge",
                 "panic", "extreme_confidence", "slept_7h", "stable_today"]


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    n = len(xs)
    mx = sum(xs) / n; my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx)**2 for x in xs))
    dy = math.sqrt(sum((y - my)**2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def emotion_correlation(trades: list[dict]) -> list[dict]:
    """For each emotion key, compute Pearson correlation between the
    user-rated emotion (0-10 or 0/1) and realized R-multiple."""
    out = []
    for key in _EMOTION_KEYS:
        xs, ys, pnl_low, pnl_high = [], [], [], []
        for t in trades:
            # Emotion now stored as a list of selected chip names (new UI),
            # but legacy entries may still be {key: 0-10 rating} dicts. Handle both.
            emo = (t.get("open_entry") or {}).get("emotion")
            if isinstance(emo, list):
                v = 1 if key in emo else None  # selected = "high", absent = unknown
            elif isinstance(emo, dict):
                v = emo.get(key)
            else:
                v = None
            r = t.get("r_multiple")
            if v is None or r is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            xs.append(v); ys.append(r)
            # Group P/L into low (≤3 or 0) and high (≥7 or 1) buckets so we
            # can show "trades when FOMO≥7 average …"
            if key in ("slept_7h", "stable_today"):
                if v == 0: pnl_low.append(t["net_pnl"])
                elif v == 1: pnl_high.append(t["net_pnl"])
            else:
                if v <= 3: pnl_low.append(t["net_pnl"])
                elif v >= 7: pnl_high.append(t["net_pnl"])
        if not xs:
            continue
        pearson = _pearson(xs, ys)
        out.append({
            "emotion": key,
            "n": len(xs),
            "correlation": round(pearson, 4) if pearson is not None else None,
            "avg_r": round(sum(ys) / len(ys), 3) if ys else 0,
            "low_bucket_avg_pnl": round(sum(pnl_low) / len(pnl_low), 2)
                                   if pnl_low else None,
            "low_bucket_n": len(pnl_low),
            "high_bucket_avg_pnl": round(sum(pnl_high) / len(pnl_high), 2)
                                    if pnl_high else None,
            "high_bucket_n": len(pnl_high),
        })
    return out


# 英文/旧标签 → 规范中文标签。覆盖 data 里出现的英文变体。
MISTAKE_CN: dict[str, str] = {
    "gamble": "赌博",
    "Held losers too long": "没拿住",
    "Closed early on noise": "因噪音过早平仓",
    "Overconfidence": "过度自信",
    "Skipped research": "未做研究",
    "Too many open positions": "持仓数量过多",
    "Against trend": "逆势操作",
}


def canonical_mistake(tag: str) -> str:
    """将英文或变体错误标签规范化为统一中文标签。"""
    t = (tag or "").strip()
    return MISTAKE_CN.get(t, t)


def mistake_aggregation(trades: list[dict]) -> list[dict]:
    """Top mistakes by frequency + total $ cost. Ranked by realized loss."""
    by_mistake: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        mistakes = (t.get("close_entry") or {}).get("mistakes_made") or []
        for m in mistakes:
            by_mistake[canonical_mistake(m)].append(t)
    out = []
    for m, items in by_mistake.items():
        net = sum(t["net_pnl"] for t in items)
        wins = sum(1 for t in items if t["net_pnl"] > 0)
        out.append({
            "mistake": m,
            "n": len(items),
            "net_pnl": round(net, 2),
            "avg_pnl": round(net / len(items), 2) if items else 0,
            "win_rate": round(wins / len(items), 4) if items else 0,
        })
    return sorted(out, key=lambda r: r["net_pnl"])  # bleeders first


def followed_plan_pnl(trades: list[dict]) -> dict:
    """Compare 'followed plan' vs 'broke plan' cohorts."""
    cohorts = {"true": [], "false": [], "null": []}
    for t in trades:
        v = (t.get("close_entry") or {}).get("followed_plan")
        key = "true" if v is True else "false" if v is False else "null"
        cohorts[key].append(t)
    def agg(items):
        if not items:
            return {"n": 0, "win_rate": 0, "net_pnl": 0, "avg_pnl": 0, "avg_r": 0}
        wins = sum(1 for t in items if t["net_pnl"] > 0)
        net = sum(t["net_pnl"] for t in items)
        rs = [t.get("r_multiple") for t in items if t.get("r_multiple") is not None]
        return {
            "n": len(items),
            "win_rate": round(wins / len(items), 4),
            "net_pnl": round(net, 2),
            "avg_pnl": round(net / len(items), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0,
        }
    return {
        "followed": agg(cohorts["true"]),
        "broken":   agg(cohorts["false"]),
        "unrated":  agg(cohorts["null"]),
        "rate_adherence": round(
            len(cohorts["true"]) / max(1, len(cohorts["true"]) + len(cohorts["false"])),
            4),
    }


def consecutive_loss_state(trades: list[dict],
                            daily_loss_limit_dollar: float = 500.0) -> dict:
    """Current and max consecutive-loss streak + daily-loss-limit state.
    Used to render the STOP guardrail banner."""
    sorted_t = sorted(
        trades,
        key=lambda t: (t.get("close_at") or t.get("close_date") or "")
    )
    cur_loss = 0; max_loss = 0
    for t in sorted_t:
        if t["net_pnl"] <= 0:
            cur_loss += 1
            if cur_loss > max_loss: max_loss = cur_loss
        else:
            cur_loss = 0

    # Today's P/L (using close_date)
    today = datetime.date.today().isoformat()
    today_pnl = sum(t["net_pnl"] for t in trades
                    if (t.get("close_date") or "") == today)
    daily_breach = today_pnl <= -abs(daily_loss_limit_dollar)

    # Last-7-days rolling
    today_dt = datetime.date.today()
    week_ago = (today_dt - datetime.timedelta(days=7)).isoformat()
    week_pnl = sum(t["net_pnl"] for t in trades
                   if (t.get("close_date") or "") >= week_ago)
    return {
        "current_loss_streak": cur_loss,
        "max_loss_streak": max_loss,
        "today_pnl": round(today_pnl, 2),
        "daily_loss_limit": daily_loss_limit_dollar,
        "daily_limit_breached": bool(daily_breach),
        "week_pnl": round(week_pnl, 2),
        "warn_3_consec_losses": cur_loss >= 3,
    }


# ── Tier 3: edge finder + max-loss freq + earnings sub-dashboard ─────────

def edge_finder(trades: list[dict], min_n: int = 5) -> dict:
    """Brute-force scan over multi-dimensional tag combinations; surface
    the highest- and lowest-expectancy combos with at least `min_n` trades.

    Dimensions scanned (only pairs — keeps the cardinality manageable):
      direction × holdtime    direction × dte    direction × strategy
      strategy × holdtime     strategy × dte     holdtime × dte
      ticker × direction (for top tickers only)
    """
    pairs = [
        ("direction", "holdtime"),
        ("direction", "dte"),
        ("direction", "strategy"),
        ("strategy", "holdtime"),
        ("strategy", "dte"),
        ("holdtime", "dte"),
    ]
    def key_value(t, dim):
        if dim == "direction": return t.get("direction") or "?"
        if dim == "strategy": return t.get("strategy") or "?"
        if dim == "holdtime":
            return _bucket_holdtime(t["hold_days"]) if t.get("hold_days") is not None else None
        if dim == "dte":
            return _bucket_dte(t["dte_at_open"]) if t.get("dte_at_open") is not None else None
        if dim == "ticker": return t.get("ticker") or "?"
        return None

    combos: list[dict] = []
    for d1, d2 in pairs:
        groups: dict = defaultdict(list)
        for t in trades:
            k1 = key_value(t, d1); k2 = key_value(t, d2)
            if k1 is None or k2 is None:
                continue
            groups[(k1, k2)].append(t)
        for (k1, k2), items in groups.items():
            if len(items) < min_n:
                continue
            agg = _aggregate(items)
            combos.append({
                "dims": [d1, d2],
                "values": [k1, k2],
                "label": f"{d1}={k1} · {d2}={k2}",
                **agg,
            })
    combos.sort(key=lambda r: -r["expectancy"])
    top = combos[:5]
    bottom = sorted(combos, key=lambda r: r["expectancy"])[:5]
    return {"top_edges": top, "bleeders": bottom, "min_n": min_n,
            "total_combos_scanned": len(combos)}


def max_loss_frequency(trades: list[dict]) -> dict:
    """How often did a trade actually realize ≥95% of its max-loss vs ≥80%
    of its max-profit. For verticals only."""
    full_loss_n = 0; near_max_profit_n = 0
    measured_n = 0
    for t in trades:
        ml = t.get("max_loss_total"); mp = t.get("max_profit_total")
        pnl = t["net_pnl"]
        if ml is None or mp is None or ml == 0 or mp == 0:
            continue
        measured_n += 1
        if pnl <= 0.95 * ml:  # ml is negative
            full_loss_n += 1
        if pnl >= 0.80 * mp:
            near_max_profit_n += 1
    return {
        "n_measured": measured_n,
        "full_max_loss": full_loss_n,
        "full_max_loss_rate": round(full_loss_n / measured_n, 4) if measured_n else 0,
        "near_max_profit": near_max_profit_n,
        "near_max_profit_rate": round(near_max_profit_n / measured_n, 4) if measured_n else 0,
    }


def earnings_subdashboard(trades: list[dict]) -> dict:
    """Slice trades by is_earnings_bet — was each one an earnings IV-crush
    play? Compare cohorts."""
    earnings = [t for t in trades
                if (t.get("open_entry") or {}).get("is_earnings_bet") is True]
    non_earnings = [t for t in trades
                     if (t.get("open_entry") or {}).get("is_earnings_bet") is False]
    return {
        "earnings": _aggregate(earnings),
        "non_earnings": _aggregate(non_earnings),
        "n_tagged": len(earnings) + len(non_earnings),
    }


# ── Tier 4: recommendation engine, lessons aggregation, weekly digest ────

def recommendations(trades: list[dict], stats: dict) -> list[dict]:
    """Generate plain-English actionable recommendations from the rest of the
    stats payload. Pure heuristics, no LLM. Each rec: {priority,
    category, message, evidence}."""
    out: list[dict] = []
    if not trades:
        return out

    # 1) Daily loss limit breach
    g = stats.get("guardrails") or {}
    if g.get("daily_limit_breached"):
        out.append({
            "priority": "critical", "category": "discipline",
            "message": f"🛑 STOP TRADING TODAY — daily loss limit breached "
                       f"(${g['today_pnl']:.0f} vs ${g['daily_loss_limit']:.0f} cap).",
            "evidence": f"Close the platform; review tomorrow.",
        })

    # 2) Consecutive-loss streak
    if g.get("warn_3_consec_losses"):
        out.append({
            "priority": "high", "category": "sizing",
            "message": f"⚠ {g['current_loss_streak']} consecutive losses — "
                       f"size down 50% on next entry or pause until next session.",
            "evidence": "Variance + tilt risk compounds in active loss streaks.",
        })

    # 3) Edge Finder bleeders ≥$50/trade lost
    ef = stats.get("edge_finder") or {}
    for r in ef.get("bleeders", [])[:3]:
        if r["expectancy"] < -50 and r["n"] >= 8:
            out.append({
                "priority": "high", "category": "stop-doing",
                "message": f"❌ Stop opening {r['label']} — "
                           f"-${abs(r['expectancy']):.0f}/trade over {r['n']} trades "
                           f"(net -${abs(r['net_pnl']):.0f}).",
                "evidence": f"Win rate {r['win_rate']*100:.0f}%, "
                            f"worst single trade ${r['worst']:.0f}.",
            })

    # 4) Edge Finder top edges ≥$80/trade gained
    for r in ef.get("top_edges", [])[:3]:
        if r["expectancy"] > 80 and r["n"] >= 5:
            out.append({
                "priority": "medium", "category": "lean-in",
                "message": f"✅ Lean into {r['label']} — "
                           f"+${r['expectancy']:.0f}/trade over {r['n']} trades "
                           f"(net +${r['net_pnl']:.0f}).",
                "evidence": f"Win rate {r['win_rate']*100:.0f}%, "
                            f"best single trade ${r['best']:.0f}.",
            })

    # 5) Hold-time skew — if 0-1d has substantially worse winrate than 7-30d
    hb = stats.get("by_holdtime") or []
    short_b = next((b for b in hb if b["bucket"] == "0-1d"), None)
    long_b = next((b for b in hb if b["bucket"] in ("7-30d", "30d+")), None)
    if short_b and long_b and short_b["n"] >= 10:
        if (long_b["win_rate"] - short_b["win_rate"]) > 0.30:
            out.append({
                "priority": "high", "category": "process",
                "message": f"⏱ Hold trades longer — same-day exits win "
                           f"{short_b['win_rate']*100:.0f}% (n={short_b['n']}) "
                           f"vs {long_b['win_rate']*100:.0f}% on {long_b['bucket']} holds.",
                "evidence": f"0-1d cumulative ${short_b['net_pnl']:.0f} vs "
                            f"{long_b['bucket']} ${long_b['net_pnl']:.0f}.",
            })

    # 6) Direction bias — if BEAR is dominant bleeder, flag
    bd = stats.get("by_direction") or []
    bear = next((b for b in bd if b["direction"] == "BEAR"), None)
    bull = next((b for b in bd if b["direction"] == "BULL"), None)
    if bear and bear["n"] >= 10 and bear["win_rate"] < 0.20:
        out.append({
            "priority": "high", "category": "stop-doing",
            "message": f"🐻 BEAR trades win only {bear['win_rate']*100:.0f}% "
                       f"(n={bear['n']}, net ${bear['net_pnl']:.0f}). Pause "
                       f"directional shorts until trend supports them.",
            "evidence": f"Compare: BULL win rate "
                        f"{(bull['win_rate']*100 if bull else 0):.0f}%.",
        })

    # 7) Profit factor sub-1
    kpi = stats.get("kpi") or {}
    pf = kpi.get("profit_factor")
    if pf is not None and pf < 1 and kpi.get("total_trades", 0) >= 30:
        out.append({
            "priority": "high", "category": "sizing",
            "message": f"📉 Profit factor {pf:.2f} (below 1.0) over "
                       f"{kpi['total_trades']} trades. Edge unproven — "
                       f"shrink position size 50% or paper-trade until PF ≥1.2.",
            "evidence": f"Avg win ${kpi['avg_winner']:.0f} × W% < "
                        f"|avg loss| ${abs(kpi['avg_loser']):.0f} × L%.",
        })

    # 8) Concentration risk
    # Bug fix: by_ticker is sorted by net_pnl desc (winners first), so bt[:3]
    # was picking the 3 biggest WINNERS, not the 3 biggest |P/L| contributors.
    # Re-sort by absolute P/L impact to surface true concentration.
    bt = stats.get("by_ticker") or []
    if len(bt) >= 5:
        bt_by_abs = sorted(bt, key=lambda r: -abs(r["net_pnl"]))
        top3_abs = sum(abs(r["net_pnl"]) for r in bt_by_abs[:3])
        total_abs = sum(abs(r["net_pnl"]) for r in bt_by_abs)
        if total_abs > 0 and (top3_abs / total_abs) > 0.55:
            tickers = ", ".join(r["ticker"] for r in bt_by_abs[:3])
            out.append({
                "priority": "medium", "category": "diversify",
                "message": f"🎯 Top-3 tickers ({tickers}) drive "
                           f"{top3_abs/total_abs*100:.0f}% of P/L magnitude. "
                           f"Consider diversifying or paper-checking your edge "
                           f"on those names — single-ticker fragility.",
                "evidence": f"{len(bt)} tickers traded total.",
            })

    # 9) Time-of-day disaster
    bh = stats.get("by_hour") or []
    open_h = next((b for b in bh if b["bucket"] == "open"), None)
    if open_h and open_h["n"] >= 8 and open_h["win_rate"] < 0.20:
        out.append({
            "priority": "high", "category": "stop-doing",
            "message": f"🕐 Trades opened in the 9-11am ET window win "
                       f"{open_h['win_rate']*100:.0f}% (n={open_h['n']}, "
                       f"net ${open_h['net_pnl']:.0f}). Wait until midday.",
            "evidence": "Open volatility / chasing the gap likely to blame.",
        })

    return out


import re

# Common stopwords to filter from lesson_learned / notes text aggregation.
_STOPWORDS_EN = {
    "the","a","an","and","or","but","i","my","me","we","to","of","in","on",
    "at","is","was","were","be","are","this","that","it","with","for","from",
    "as","by","not","no","do","did","does","have","has","had","will","would",
    "should","could","can","get","got","too","so","just","more","much","very",
    "than","then","there","here","when","what","why","how","also","into","its",
    "if","up","down","out","you","your","they","them","their","one","two",
    "had","been","over","off","like","need","needs","need","make","made","make",
}


def _tokenize_text(s: str) -> list[str]:
    """Simple tokenizer: ASCII words + Chinese character 2-grams."""
    if not s:
        return []
    out = []
    # English / numbers
    for w in re.findall(r"[a-zA-Z]{3,}", s.lower()):
        if w not in _STOPWORDS_EN:
            out.append(w)
    # Chinese: pull bigrams from each contiguous CJK chunk
    for chunk in re.findall(r"[一-鿿]+", s):
        for i in range(len(chunk) - 1):
            out.append(chunk[i:i+2])
    return out


def lessons_aggregation(trades: list[dict]) -> dict:
    """Word/phrase frequency over user-written lesson_learned + notes text.
    Returns top tokens for English (3+ chars) and CJK bigrams."""
    bag: Counter = Counter()
    sources_n = 0
    for t in trades:
        co = (t.get("close_entry") or {})
        op = (t.get("open_entry") or {})
        text = " ".join([
            co.get("lesson_learned") or "",
            op.get("notes") or "",
            op.get("one_line_thesis") or "",
        ])
        if text.strip():
            sources_n += 1
        for tok in _tokenize_text(text):
            bag[tok] += 1
    top = bag.most_common(20)
    return {
        "n_sources": sources_n,
        "tokens": [{"token": tok, "count": n} for tok, n in top],
    }


def monthly_progress(trades: list[dict],
                       targets: Optional[dict] = None) -> dict:
    """Compute progress toward configurable monthly goals: P/L target,
    trade-count cap, win-rate floor.

    Defaults: $1000 P/L · ≤30 trades · ≥50% W/L.
    """
    t = targets or {}
    target_pnl = float(t.get("target_pnl", 1000))
    trade_cap = int(t.get("trade_cap", 30))
    wr_floor = float(t.get("win_rate_floor", 0.50))
    today = datetime.date.today()
    first_of_month = today.replace(day=1).isoformat()
    next_month = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    days_in_month = (next_month - today.replace(day=1)).days
    days_elapsed = (today - today.replace(day=1)).days + 1
    in_month = [t for t in trades
                 if (t.get("close_date") or "") >= first_of_month
                 and (t.get("close_date") or "") < next_month.isoformat()]
    n = len(in_month)
    pnls = [t["net_pnl"] for t in in_month]
    pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n if n else 0
    pace_pnl = pnl / max(1, days_elapsed) * days_in_month  # if pace held
    return {
        "month": first_of_month[:7],
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "n_trades": n,
        "current_pnl": round(pnl, 2),
        "current_win_rate": round(wr, 4),
        "target_pnl": target_pnl,
        "trade_cap": trade_cap,
        "win_rate_floor": wr_floor,
        "pnl_progress": round(pnl / target_pnl, 4) if target_pnl else 0,
        "trade_count_progress": round(n / max(1, trade_cap), 4),
        "wr_vs_floor": round(wr - wr_floor, 4),
        "pace_pnl_eom": round(pace_pnl, 2),
    }


def weekly_comparison(trades: list[dict]) -> dict:
    """Compare last 7 days vs prior 7 days."""
    today = datetime.date.today()
    last_7_start = today - datetime.timedelta(days=7)
    prior_14_start = today - datetime.timedelta(days=14)
    def in_range(t, start, end):
        d = t.get("close_date") or ""
        try:
            cd = datetime.date.fromisoformat(d)
        except (ValueError, TypeError):
            return False
        return start <= cd <= end
    last_week = [t for t in trades if in_range(t, last_7_start, today)]
    prior_week = [t for t in trades
                   if in_range(t, prior_14_start, last_7_start - datetime.timedelta(days=1))]
    return {
        "last_7d": _aggregate(last_week),
        "prior_7d": _aggregate(prior_week),
        "delta_pnl": round(
            sum(t["net_pnl"] for t in last_week)
            - sum(t["net_pnl"] for t in prior_week), 2),
    }


# ── Chinese narrative generator (确定性体检结论) ──────────────────────────

def _tone_for_share(bad_share: float) -> str:
    return "bad" if bad_share >= 0.4 else "warn" if bad_share >= 0.25 else "good"


def _money(v) -> str:
    """Signed dollar format: - /  (no decimals). None → 'N/A'."""
    if v is None:
        return "N/A"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def narratives(trades: list[dict], stats: dict) -> list[dict]:
    """确定性中文叙述(无LLM)。专业+直接口吻。每条:
    {key, title, headline, body, tone, evidence, items?}. min_n≥8 才下硬结论。"""
    out: list[dict] = []
    if not trades:
        return out
    n = len(trades)

    q = stats.get("quadrant") or {}
    cells = q.get("cells") or {}
    kpi = stats.get("kpi") or {}
    bad = cells.get("bad_dec_bad_res", {})
    good = cells.get("good_dec_good_res", {})
    bad_share = bad.get("share", 0)
    wr = kpi.get("win_rate", 0)
    pf = kpi.get("profit_factor")
    pf_str = f"{pf:.2f}" if pf is not None else "∞"
    out.append({
        "key": "health", "title": "体检结论",
        "tone": _tone_for_share(bad_share),
        "headline": f"{n} 笔已平仓，胜率 {wr*100:.0f}%，期望值 {_money(kpi.get('expectancy_dollar',0))}/笔，"
                    f"盈利因子 {pf_str}。",
        "body": f"{bad.get('share',0)*100:.0f}%（{bad.get('n',0)} 笔）落在「坏决策→坏结果」累计 "
                f"{_money(bad.get('net_pnl',0))}；只有 {good.get('share',0)*100:.0f}% 是「好决策好结果」。"
                f"靠均盈 {_money(kpi.get('avg_winner',0))} / 均亏 {_money(kpi.get('avg_loser',0))} 的不对称撑着。",
        "evidence": "象限来自你自评的 quadrant 字段。",
        # the same card in English, for the interface in English
        "title_en": "Checkup summary",
        "headline_en": f"{n} closed trades, win rate {wr*100:.0f}%, expectancy "
                       f"{_money(kpi.get('expectancy_dollar',0))} per trade, profit factor {pf_str}.",
        "body_en": f"{bad.get('share',0)*100:.0f}% ({bad.get('n',0)} trades) landed in \"bad decision → bad result\", "
                   f"{_money(bad.get('net_pnl',0))} in all; only {good.get('share',0)*100:.0f}% are \"good decision, good "
                   f"result\". The gap between the average win {_money(kpi.get('avg_winner',0))} and the average loss "
                   f"{_money(kpi.get('avg_loser',0))} is what carries you.",
        "evidence_en": "The quadrants come from the quadrant field you fill in yourself.",
    })

    mistakes = [m for m in (stats.get("mistakes") or []) if m["net_pnl"] < 0][:5]
    if mistakes:
        top = mistakes[0]
        out.append({
            "key": "mistake_board", "title": "最烧钱的重复错误",
            "tone": "bad",
            "headline": f"最贵的错误是「{top['mistake']}」：{top['n']} 笔亏 {_money(abs(top['net_pnl']))}"
                        f"（均亏 {_money(abs(top['avg_pnl']))}）。",
            "body": "、".join(f"{m['mistake']} {m['n']}笔 {_money(m['net_pnl'])}" for m in mistakes),
            "evidence": "按累计亏损排序，标签已中英规范化。",
            "items": mistakes,
            "title_en": "The most expensive repeated mistakes",
            "headline_en": f"The most expensive mistake is \"{top['mistake']}\": {top['n']} trades lost "
                           f"{_money(abs(top['net_pnl']))} ({_money(abs(top['avg_pnl']))} on average).",
            "body_en": ", ".join(f"{m['mistake']} {m['n']} trades {_money(m['net_pnl'])}" for m in mistakes),
            "evidence_en": "Sorted by total loss; labels are merged across Chinese and English.",
        })

    ef = stats.get("edge_finder") or {}
    bleeders = [r for r in ef.get("bleeders", []) if r["n"] >= 8 and r["expectancy"] < -30][:3]
    edges = [r for r in ef.get("top_edges", []) if r["n"] >= 8 and r["expectancy"] > 50][:3]
    if bleeders or edges:
        parts, parts_en = [], []
        if edges:
            parts.append("继续做：" + "；".join(
                f"{r['label']}（{r['n']}笔 {r['win_rate']*100:.0f}%胜 {_money(r['net_pnl'])}）" for r in edges))
            parts_en.append("Keep doing: " + "; ".join(
                f"{r['label']} ({r['n']} trades, {r['win_rate']*100:.0f}% wins, {_money(r['net_pnl'])})" for r in edges))
        if bleeders:
            parts.append("立刻停：" + "；".join(
                f"{r['label']}（{r['n']}笔 {r['win_rate']*100:.0f}%胜 {_money(r['net_pnl'])}）" for r in bleeders))
            parts_en.append("Stop now: " + "; ".join(
                f"{r['label']} ({r['n']} trades, {r['win_rate']*100:.0f}% wins, {_money(r['net_pnl'])})" for r in bleeders))
        out.append({
            "key": "edge_audit", "title": "你的 edge 与反 edge",
            "tone": "warn" if bleeders else "good",
            "headline": (f"最该停的是 {bleeders[0]['label']}：均亏 {_money(abs(bleeders[0]['expectancy']))}/笔。"
                         if bleeders else f"最强 edge 是 {edges[0]['label']}：均盈 {_money(edges[0]['expectancy'])}/笔。"),
            "body": "。".join(parts) + "。",
            "evidence": "仅统计样本 ≥8 笔的维度。",
            "title_en": "Your edges and anti-edges",
            "headline_en": (f"Stop {bleeders[0]['label']} first: it loses {_money(abs(bleeders[0]['expectancy']))} "
                            "per trade on average." if bleeders else
                            f"Your strongest edge is {edges[0]['label']}: it makes {_money(edges[0]['expectancy'])} "
                            "per trade on average."),
            "body_en": ". ".join(parts_en) + ".",
            "evidence_en": "Only dimensions with at least 8 trades count.",
        })

    cc = stats.get("confidence_calibration") or []
    hi = next((c for c in cc if c["confidence"] == "High"), None)
    lo = next((c for c in cc if c["confidence"] == "Low"), None)
    if hi and lo and hi["n"] >= 8 and lo["n"] >= 8:
        tone = "good" if hi["win_rate"] > lo["win_rate"] else "warn"
        out.append({
            "key": "confidence", "title": "信心校准",
            "tone": tone,
            "headline": f"High 信心 {hi['win_rate']*100:.0f}% 胜 / Low {lo['win_rate']*100:.0f}% 胜"
                        f"——你{'分得清好坏单' if tone=='good' else '自评与结果背离'}。",
            "body": (f"问题在 Low 单开太多：{lo['n']} 笔累计 {_money(lo['net_pnl'])}。"
                     f"少开 Low 单是最省事的提升。" if lo["net_pnl"] < 0 else ""),
            "evidence": "对比自评 confidence 与实际胜率。",
            "title_en": "Confidence calibration",
            "headline_en": f"High confidence wins {hi['win_rate']*100:.0f}% / Low {lo['win_rate']*100:.0f}%: "
                           + ("you can tell good trades from bad." if tone == "good"
                              else "your self-rating and the results disagree."),
            "body_en": (f"The problem is too many Low-confidence trades: {lo['n']} trades, {_money(lo['net_pnl'])} in all. "
                        "Opening fewer of them is the easiest improvement." if lo["net_pnl"] < 0 else ""),
            "evidence_en": "Compares your self-rated confidence with the actual win rate.",
        })

    return out


# ── Single-shot dashboard payload ─────────────────────────────────────────

def compute_all(filters: Optional[dict] = None) -> dict:
    """One-shot — full dashboard payload in a single JSON. The
    `recommendations` block is computed last because it derives from the
    rest of the stats."""
    trades = build_trades()
    trades = apply_filters(trades, filters)
    payload = {
        "n_trades": len(trades),
        "filter_applied": filters or {},
        "kpi": kpi_hero(trades),
        "equity_curve": equity_curve(trades),
        "r_distribution": r_distribution(trades),
        "by_holdtime": by_holdtime(trades),
        "by_dte_at_open": by_dte_at_open(trades),
        "by_strategy": by_strategy(trades),
        "by_direction": by_direction(trades),
        "by_ticker": by_ticker(trades),
        "by_dow": by_dow(trades),
        "by_hour": by_hour(trades),
        "calendar": calendar_heatmap(trades),
        # Tier 2 — behavioral / decision-quality (uses user-filled fields)
        "quadrant": quadrant_breakdown(trades),
        "confidence_calibration": confidence_calibration(trades),
        "emotion_correlation": emotion_correlation(trades),
        "mistakes": mistake_aggregation(trades),
        "followed_plan": followed_plan_pnl(trades),
        "guardrails": consecutive_loss_state(trades),
        # Tier 3 — edge mining + earnings sub-dashboard
        "edge_finder": edge_finder(trades),
        "max_loss_frequency": max_loss_frequency(trades),
        "earnings": earnings_subdashboard(trades),
        # Tier 4 — narrative layer
        "lessons": lessons_aggregation(trades),
        "weekly": weekly_comparison(trades),
        "monthly_progress": monthly_progress(trades, (filters or {}).get("targets")),
    }
    # Recommendations + narratives look at the rest of the payload, so compute last
    payload["recommendations"] = recommendations(trades, payload)
    payload["narratives"] = narratives(trades, payload)
    return payload

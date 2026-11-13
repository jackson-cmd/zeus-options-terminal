"""Hourly option-price tracker.

Goal: for every open or recently-closed-but-still-pre-expiry trade,
sample each leg's mark from TastyTrade once an hour during US-market
hours and persist to Parquet. The drawer's price-history chart reads
from this store to draw "your spread's value over time."

Storage layout (Parquet, append-only, one file per snapshot):

  trade_journal/data/option_prices/
    20260429T093045Z.parquet
    20260429T103045Z.parquet
    ...

Schema per row:
  ts        - ISO8601 UTC timestamp (string)
  symbol    - OCC option symbol (e.g. "AAPL  261218C00200000")
  underlying- short ticker (e.g. "AAPL")
  mark      - float (theoretical fair value)
  last      - float (last trade)
  prev_close- float (yesterday's close)

Reading: any consumer just opens the dataset directory with
pyarrow.dataset and filters by symbol / time range.

Server-side scheduler (in optionstrat_server.py) calls
`snapshot_to_parquet()` every hour 09:30-16:00 ET on weekdays. Network
gaps just leave holes — already-saved points stay valid.
"""
from __future__ import annotations

import datetime
import math
import os
from pathlib import Path
from typing import Optional

# Risk-free rate used for BSM computations. Approximation of current 3-month
# T-bill yield — refined occasionally is fine; the impact on Greeks is small.
_RISK_FREE_RATE = 0.045


# ── Black-Scholes price + IV solver + Greeks ─────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bsm_price(S: float, K: float, T: float, r: float,
               sigma: float, opt_type: str) -> float:
    """Black-Scholes-Merton fair value. T in years, sigma annualized."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if opt_type == "C" else max(0.0, K - S)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if opt_type == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(market_price: float, S: float, K: float, T: float, r: float,
                  opt_type: str, lo: float = 0.001, hi: float = 5.0,
                  tol: float = 1e-4, max_iter: int = 60) -> Optional[float]:
    """Solve for σ such that BSM(σ) = market_price via bisection. Returns
    None if no convergence (e.g. market_price below intrinsic)."""
    if market_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, S - K) if opt_type == "C" else max(0.0, K - S)
    if market_price < intrinsic - 0.01:
        return None
    f_lo = bsm_price(S, K, T, r, lo, opt_type) - market_price
    f_hi = bsm_price(S, K, T, r, hi, opt_type) - market_price
    if f_lo * f_hi > 0:
        # Both same sign — extend hi or give up
        if f_hi < 0:
            return None
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bsm_price(S, K, T, r, mid, opt_type) - market_price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid; f_hi = f_mid
        else:
            lo = mid; f_lo = f_mid
    return 0.5 * (lo + hi)


def bsm_greeks(S: float, K: float, T: float, r: float, sigma: float,
                opt_type: str) -> dict:
    """All Greeks at once. Theta is per-day, vega per +1% IV move."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    nd1 = _norm_pdf(d1)
    if opt_type == "C":
        delta = _norm_cdf(d1)
        theta = (-S * nd1 * sigma / (2 * sqrtT)
                  - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-S * nd1 * sigma / (2 * sqrtT)
                  + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365.0
    gamma = nd1 / (S * sigma * sqrtT)
    vega = S * nd1 * sqrtT / 100.0   # per 1% IV change
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "theta": round(theta, 4), "vega": round(vega, 4)}


def _years_to_expiry(yymmdd: str, now: Optional[datetime.datetime] = None) -> Optional[float]:
    exp = _exp_to_date(yymmdd)
    if not exp:
        return None
    now = now or datetime.datetime.utcnow()
    days = (exp - now.date()).days
    if days < 0:
        return None
    if days == 0:
        days = 0.5   # 12-hour minimum so BSM doesn't divide by zero
    return days / 365.0


def compute_iv_and_greeks(symbol: str, mark: float, spot: float,
                           when: Optional[datetime.datetime] = None) -> dict:
    """Given an OCC option symbol, its current mark, and the underlying spot,
    solve IV and compute all Greeks. Returns
    {iv, delta, gamma, theta, vega} or zeros if not solvable.
    """
    info = _parse_option_full(symbol)
    if not info or spot <= 0 or mark <= 0:
        return {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    T = _years_to_expiry(info["exp"], when)
    if T is None or T <= 0:
        return {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    iv = implied_vol(mark, spot, info["strike"], T, _RISK_FREE_RATE, info["type"])
    if iv is None or iv <= 0:
        return {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    g = bsm_greeks(spot, info["strike"], T, _RISK_FREE_RATE, iv, info["type"])
    return {"iv": round(iv, 4), **g}


def _parse_option_full(sym: str) -> Optional[dict]:
    """OCC symbol → {underlying, exp(YYMMDD), type(C/P), strike(float)}."""
    s = (sym or "").strip()
    if len(s) < 18:
        return None
    try:
        return {
            "underlying": s[:-15].strip(),
            "exp": s[-15:-9],
            "type": s[-9],
            "strike": int(s[-8:]) / 1000.0,
        }
    except (ValueError, IndexError):
        return None

_THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = _THIS_DIR / "data"
PRICES_DIR = DATA_DIR / "option_prices"


# NYSE full-day holidays (no equity options trading at all).
# Update yearly. Half-days (1pm close) are NOT in this list — covered by the
# 16:00 ET upper bound which already cuts off after 4pm.
_NYSE_HOLIDAYS: set = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_market_open(now_utc: Optional[datetime.datetime] = None) -> bool:
    """True iff (best-effort) the US-equity-options regular session is open.

    Rules:
      • Mon-Fri only (Sat/Sun → False)
      • Not on NYSE full-day holidays (set above)
      • Time-of-day (US/Eastern, DST-aware) ∈ [09:30, 16:00)

    Used by the snapshot loop to decide whether to fire. The manual
    `snapshot_now` endpoint bypasses this check intentionally.
    """
    try:
        from zoneinfo import ZoneInfo
        et_zone = ZoneInfo("America/New_York")
    except Exception:
        # Last-ditch fallback — assume EDT (UTC-4); only wrong for ~4 winter months
        et_zone = datetime.timezone(datetime.timedelta(hours=-4))
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    et = now.astimezone(et_zone)
    if et.weekday() >= 5:                          # Sat/Sun
        return False
    if et.date().isoformat() in _NYSE_HOLIDAYS:    # Closed for holiday
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60        # 09:30 ≤ now < 16:00


def _exp_to_date(yymmdd: str) -> Optional[datetime.date]:
    """260618 → 2026-06-18. Returns None on bad input."""
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    try:
        return datetime.date(int(yymmdd[:2]) + 2000,
                              int(yymmdd[2:4]),
                              int(yymmdd[4:6]))
    except ValueError:
        return None


def _parse_exp_from_symbol(sym: str) -> Optional[datetime.date]:
    """Extract expiration date from an OCC symbol like 'AAPL  261218C00200000'."""
    s = (sym or "").strip()
    if len(s) < 18:
        return None
    return _exp_to_date(s[-15:-9])


def tracked_symbols(open_entries: list[dict],
                     close_entries: list[dict]) -> list[str]:
    """Return the set of OCC symbols we should snapshot — every leg from
    every entry where the expiration date is today or later. A trade
    being closed in the journal doesn't stop tracking; only expiration
    does, so the user can replay how a trade *would have* worked out.
    """
    today = datetime.date.today()
    seen: set[str] = set()
    for src in (open_entries or []) + (close_entries or []):
        for leg in (src.get("legs") or src.get("close_legs") or []):
            sym = (leg.get("symbol") or "").strip()
            if not sym:
                continue
            exp = _parse_exp_from_symbol(sym)
            if exp is not None and exp < today:
                continue
            seen.add(sym)
    return sorted(seen)


def snapshot_to_parquet(marks: dict[str, dict],
                          spots: Optional[dict[str, float]] = None,
                          when: Optional[datetime.datetime] = None) -> Optional[Path]:
    """Persist a marks-batch as a single Parquet file.

    Args:
      marks: {OCC symbol: {mark, last, prev_close}} (from fetch_option_marks)
      spots: optional {underlying ticker: current spot price}; when present,
             we additionally solve IV via BSM and compute delta/gamma/theta/
             vega for each leg. If absent, those columns are 0.
      when:  snapshot timestamp (default now in UTC).

    Returns the written Path, or None if nothing to write.

    Schema:
      ts, symbol, underlying, mark, last, prev_close, spot,
      iv, delta, gamma, theta, vega
    """
    if not marks:
        return None
    when = when or datetime.datetime.utcnow()
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    fname = when.strftime("%Y%m%dT%H%M%SZ") + ".parquet"
    path = PRICES_DIR / fname

    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = []
    ts_iso = when.replace(microsecond=0).isoformat() + "Z"
    spots = spots or {}
    for sym, data in marks.items():
        s = (sym or "").strip()
        if not s:
            continue
        underlying = s.split()[0] if s.split() else ""
        mark_val = float(data.get("mark") or 0)
        spot = float(spots.get(underlying) or 0)
        # Compute Greeks if we have both mark and spot
        if mark_val > 0 and spot > 0:
            g = compute_iv_and_greeks(s, mark_val, spot, when=when)
        else:
            g = {"iv": 0.0, "delta": 0.0, "gamma": 0.0,
                 "theta": 0.0, "vega": 0.0}
        rows.append({
            "ts": ts_iso,
            "symbol": s,
            "underlying": underlying,
            "mark": mark_val,
            "last": float(data.get("last") or 0),
            "prev_close": float(data.get("prev_close") or 0),
            "spot": spot,
            "iv": g["iv"],
            "delta": g["delta"],
            "gamma": g["gamma"],
            "theta": g["theta"],
            "vega": g["vega"],
        })
    if not rows:
        return None
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, str(path), compression="snappy")
    return path


def load_history(symbols: Optional[list[str]] = None,
                  since: Optional[datetime.datetime] = None) -> list[dict]:
    """Load price history rows; optionally filter by symbol set + since.
    Returns a list of dicts sorted by ts ascending. Each dict has
    {ts, symbol, underlying, mark, last, prev_close}."""
    if not PRICES_DIR.exists():
        return []
    files = sorted(PRICES_DIR.glob("*.parquet"))
    if not files:
        return []
    import pyarrow.parquet as pq
    sym_set = set(symbols) if symbols else None
    since_iso = (since.replace(microsecond=0).isoformat() + "Z") if since else None
    out: list[dict] = []
    for fp in files:
        # File names embed timestamps so we can skip files older than `since`
        # without parsing their content. Format: YYYYMMDDTHHMMSSZ.parquet
        if since_iso:
            stem = fp.stem
            try:
                file_ts_iso = (stem[:4] + "-" + stem[4:6] + "-" + stem[6:8]
                                + "T" + stem[9:11] + ":" + stem[11:13]
                                + ":" + stem[13:15] + "Z")
                if file_ts_iso < since_iso[:len(file_ts_iso)]:
                    continue
            except (ValueError, IndexError):
                pass
        try:
            table = pq.read_table(str(fp))
        except Exception:
            continue
        for row in table.to_pylist():
            if sym_set and row.get("symbol") not in sym_set:
                continue
            if since_iso and (row.get("ts") or "") < since_iso:
                continue
            out.append(row)
    out.sort(key=lambda r: (r.get("ts") or ""))
    return out


def spread_mtm_history(legs: list[dict],
                        since: Optional[datetime.datetime] = None) -> list[dict]:
    """For a single trade (its legs), compute the per-snapshot mark-to-market
    of the *whole spread* over time.

    legs: list of {"symbol", "action", "quantity"} (dicts as stored on
    journal entries). "Buy" means we paid for the leg → mark contributes +;
    "Sell" means we collected → mark contributes − (the position's value
    falls when the option price rises).

    Returns: [{ts, mtm}] sorted by ts.

      mtm at time t = Σ_{leg} sign_leg * mark_leg(t) * 100 * leg_qty
        where sign = +1 for Buy-side (long), -1 for Sell-side (short).

    The Decision Log drawer plots this as a line — it shows how much the
    open position was worth at each hourly snapshot. Combined with the
    cost basis (already shown in the meta strip) the user reads off
    realized + unrealized P/L for any historical moment.
    """
    if not legs:
        return []
    sym_set = [l.get("symbol", "").strip() for l in legs if l.get("symbol")]
    sym_set = [s for s in sym_set if s]
    if not sym_set:
        return []
    rows = load_history(sym_set, since=since)
    if not rows:
        return []
    # Index leg metadata by symbol so we can look up sign/qty fast.
    leg_meta = {}
    for l in legs:
        s = (l.get("symbol") or "").strip()
        if not s:
            continue
        action = l.get("action") or ""
        sign = 1 if action.startswith("Buy") else -1 if action.startswith("Sell") else 0
        leg_meta[s] = {
            "sign": sign,
            "qty": float(l.get("quantity") or 1),
        }
    # Bucket rows by timestamp so we get one MTM point per snapshot
    by_ts: dict[str, dict[str, float]] = {}
    for r in rows:
        ts = r.get("ts") or ""
        sym = r.get("symbol")
        if not ts or sym not in leg_meta:
            continue
        by_ts.setdefault(ts, {})[sym] = float(r.get("mark") or 0)
    out = []
    for ts in sorted(by_ts):
        marks = by_ts[ts]
        # Require all legs present this snapshot for a clean point;
        # incomplete snapshots produce a hole in the chart.
        if not all(s in marks for s in leg_meta):
            continue
        mtm = 0.0
        for s, meta in leg_meta.items():
            mtm += meta["sign"] * marks[s] * 100 * meta["qty"]
        out.append({"ts": ts, "mtm": round(mtm, 2)})
    return out


def spread_history(legs: list[dict],
                     since: Optional[datetime.datetime] = None) -> dict:
    """Per-snapshot spread metrics + per-leg IV series.

    Returns:
      {
        "snapshots": [{ts, mtm, delta, gamma, theta, vega}],
        "legs":      [{symbol, label, action, qty, ivs:[{ts,iv}]}]
      }

    Spread Greeks are signed-summed across legs:
      net_greek = Σ sign × qty × per-share-greek × 100
    Where sign = +1 for Buy-side legs, −1 for Sell-side.

    A snapshot is dropped from `snapshots` if any leg is missing data
    (so chart holes are honest, not interpolated).
    """
    if not legs:
        return {"snapshots": [], "legs": []}
    leg_meta: dict[str, dict] = {}
    for l in legs:
        s = (l.get("symbol") or "").strip()
        if not s:
            continue
        action = l.get("action") or ""
        sign = 1 if action.startswith("Buy") else -1 if action.startswith("Sell") else 0
        leg_meta[s] = {
            "symbol": s,
            "action": action,
            "qty": float(l.get("quantity") or 1),
            "sign": sign,
            "strike": l.get("strike"),
            "type": l.get("type"),
            "label": f"{l.get('strike','?')}{l.get('type','')}",
        }
    if not leg_meta:
        return {"snapshots": [], "legs": []}
    rows = load_history(list(leg_meta.keys()), since=since)
    if not rows:
        return {"snapshots": [],
                "legs": [{"symbol": m["symbol"], "label": m["label"],
                           "action": m["action"], "qty": m["qty"], "ivs": []}
                          for m in leg_meta.values()]}
    # Group by timestamp
    by_ts: dict[str, dict[str, dict]] = {}
    for r in rows:
        ts = r.get("ts") or ""
        sym = r.get("symbol")
        if not ts or sym not in leg_meta:
            continue
        by_ts.setdefault(ts, {})[sym] = r
    snapshots = []
    leg_iv_series: dict[str, list[dict]] = {s: [] for s in leg_meta}
    for ts in sorted(by_ts):
        row_by_sym = by_ts[ts]
        if not all(s in row_by_sym for s in leg_meta):
            continue
        mtm = 0.0
        delta = 0.0; gamma = 0.0; theta = 0.0; vega = 0.0
        for s, m in leg_meta.items():
            r = row_by_sym[s]
            sign = m["sign"]; qty = m["qty"]
            mtm   += sign * float(r.get("mark") or 0)  * 100 * qty
            delta += sign * float(r.get("delta") or 0) * 100 * qty
            gamma += sign * float(r.get("gamma") or 0) * 100 * qty
            theta += sign * float(r.get("theta") or 0) * 100 * qty
            vega  += sign * float(r.get("vega") or 0)  * 100 * qty
            leg_iv_series[s].append({"ts": ts, "iv": float(r.get("iv") or 0)})
        snapshots.append({
            "ts": ts,
            "mtm":   round(mtm, 2),
            "delta": round(delta, 2),
            "gamma": round(gamma, 4),
            "theta": round(theta, 2),
            "vega":  round(vega, 2),
        })
    legs_out = [{
        "symbol": m["symbol"], "label": m["label"], "action": m["action"],
        "qty": m["qty"], "ivs": leg_iv_series[s],
    } for s, m in leg_meta.items()]
    return {"snapshots": snapshots, "legs": legs_out}


def storage_stats() -> dict:
    """Light-weight introspection: how many snapshots we have, how big."""
    if not PRICES_DIR.exists():
        return {"snapshots": 0, "bytes": 0, "first_ts": None, "last_ts": None}
    files = sorted(PRICES_DIR.glob("*.parquet"))
    if not files:
        return {"snapshots": 0, "bytes": 0, "first_ts": None, "last_ts": None}
    return {
        "snapshots": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "first_ts": files[0].stem,
        "last_ts": files[-1].stem,
    }

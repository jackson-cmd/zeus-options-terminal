"""Compute spread-level Greeks / IV / POP for a journal Open or Close entry.

Two modes, picked automatically by `compute_entry_greeks(entry, side)`:

  • "live" — used when the reference timestamp is within the last ~6h:
      spot ← yfinance current quote (fast_info.last_price)
      iv   ← yfinance option chain impliedVolatility for the strike
    This is exact for newly-opened trades.

  • "historical" — for trades opened/closed days/weeks ago:
      spot ← yfinance daily close on the reference date
      iv   ← realized vol (30-day stdev of underlying log returns,
             ending on the reference date) annualized — a coarse but
             commonly-used IV proxy for backfilling
    Marked with `_source: 'computed (historical, realized-vol IV)'`
    so the UI can show it's an approximation.

Greeks are aggregated by signing leg quantities (Buy=+1, Sell=-1) and
multiplied by 100 (per-contract → per-spread). IV is the |delta|-
weighted average across legs. POP uses the same `max_profit / width`
heuristic the analysis tab buckets by, so the two views agree.

POP_close note: at close time, "POP" doesn't really apply (the trade
is over). We instead compute "POP based on close_price" — same formula
treating close_price as if it were the open. Useful for "did I get out
when POP had rolled to X%?" reflections.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional


# ─── Black-Scholes ─────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


_RISK_FREE_RATE = 0.045  # 10y treasury approx — close enough for short-dated options


def bs_greeks(spot: float, strike: float, t_years: float,
              iv: float, kind: str, r: float = _RISK_FREE_RATE) -> Optional[dict]:
    """Single-leg Greeks per contract (multiplier 100 applied at aggregation).
    Returns price, delta, gamma, theta_per_day, vega_per_1pct_iv.
    """
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    sqrtT = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT
    nd1 = _norm_pdf(d1)
    Nd1 = _norm_cdf(d1)
    Nd2 = _norm_cdf(d2)
    is_call = kind.upper().startswith("C")
    if is_call:
        price = spot * Nd1 - strike * math.exp(-r * t_years) * Nd2
        delta = Nd1
        theta_yr = (-spot * nd1 * iv / (2 * sqrtT)
                    - r * strike * math.exp(-r * t_years) * Nd2)
    else:
        price = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = Nd1 - 1
        theta_yr = (-spot * nd1 * iv / (2 * sqrtT)
                    + r * strike * math.exp(-r * t_years) * _norm_cdf(-d2))
    gamma = nd1 / (spot * iv * sqrtT)
    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta_yr / 365.0,    # $/day per share
        "vega":  spot * nd1 * sqrtT / 100.0,  # $/1% IV per share
    }


# ─── Market data lookups (yfinance) ─────────────────────────────────────
_SPOT_CACHE: dict = {}      # ticker → (price, ts)
_HIST_CACHE: dict = {}      # ticker → (DataFrame, ts)
_IV_CACHE: dict = {}        # (ticker, expiry) → (chain, ts)


def _live_spot(ticker: str) -> Optional[float]:
    import time
    cached = _SPOT_CACHE.get(ticker)
    now = time.time()
    if cached and (now - cached[1]) < 30:
        return cached[0]
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        p = float(fi.last_price)
        _SPOT_CACHE[ticker] = (p, now)
        return p
    except Exception:
        return None


def _hist_df(ticker: str):
    """Cached 1-year daily history for the underlying. ~5KB per ticker."""
    import time
    cached = _HIST_CACHE.get(ticker)
    now = time.time()
    if cached and (now - cached[1]) < 3600:
        return cached[0]
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="1y", auto_adjust=False)
        _HIST_CACHE[ticker] = (df, now)
        return df
    except Exception:
        return None


def _spot_at(ticker: str, ref: datetime) -> Optional[float]:
    """Underlying close on the calendar day of `ref`. Falls back to the
    most-recent trading day if `ref` is a weekend/holiday."""
    df = _hist_df(ticker)
    if df is None or df.empty:
        return None
    # tz-naive comparison
    target = ref.date()
    # Index might be tz-aware — coerce to date
    try:
        prior = df[df.index.date <= target]
    except Exception:
        prior = df
    if prior.empty:
        return None
    return float(prior["Close"].iloc[-1])


def _realized_vol(ticker: str, ref: datetime, window_days: int = 30) -> Optional[float]:
    """Annualized realized vol from log returns of the underlying over the
    `window_days` trading days ending on `ref`. Used as an IV stand-in
    when option-chain IV isn't available (historical backfill)."""
    import math as _m
    df = _hist_df(ticker)
    if df is None or df.empty:
        return None
    try:
        prior = df[df.index.date <= ref.date()]
    except Exception:
        prior = df
    if len(prior) < window_days:
        return None
    closes = prior["Close"].iloc[-window_days:]
    rets = []
    prev = None
    for c in closes:
        c = float(c)
        if prev is not None and prev > 0:
            rets.append(_m.log(c / prev))
        prev = c
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    daily_vol = _m.sqrt(var)
    return daily_vol * _m.sqrt(252)


def _iv_for_strike(ticker: str, expiry: str, strike: float, kind: str) -> Optional[float]:
    """Live: yfinance option chain impliedVolatility for the strike."""
    import time
    key = (ticker, expiry)
    cached = _IV_CACHE.get(key)
    now = time.time()
    chain = None
    if cached and (now - cached[1]) < 60:
        chain = cached[0]
    else:
        try:
            import yfinance as yf
            chain = yf.Ticker(ticker).option_chain(expiry)
            _IV_CACHE[key] = (chain, now)
        except Exception:
            return None
    if chain is None:
        return None
    df = chain.calls if kind.upper().startswith("C") else chain.puts
    matches = df[abs(df["strike"] - strike) < 0.01]
    if matches.empty:
        return None
    iv = float(matches.iloc[0]["impliedVolatility"])
    return iv if iv > 0 else None


# ─── Entry-level aggregation ────────────────────────────────────────────
def _years_to_expiry(expiry_iso: str, ref: Optional[datetime] = None) -> Optional[float]:
    try:
        exp = datetime.fromisoformat(expiry_iso)
    except Exception:
        return None
    ref = ref or datetime.now()
    days = (exp - ref).total_seconds() / 86400.0
    return days / 365.0 if days > 0 else None


def _exp_from_yymmdd(yymmdd: str) -> Optional[str]:
    """Convert OCC '260605' → '2026-06-05' iso date."""
    if not yymmdd or len(yymmdd) != 6:
        return None
    return f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def _signed_qty(action: str, qty: float) -> float:
    a = (action or "").lower()
    sign = -1 if "sell" in a else 1
    return sign * abs(qty or 1)


_LIVE_FRESHNESS_HOURS = 6.0   # within this, use live spot + chain IV


def compute_entry_greeks(entry: dict, side: str = "open",
                         legs_override: Optional[list] = None) -> dict:
    """Compute spread-level Greeks/IV/POP for an entry.

    `side`:
      • "open"  → uses entry.open_at  + entry.open_price  + entry.legs
      • "close" → uses entry.close_at + entry.close_price + entry.close_legs
                  (the legs the user actually unwound, which may be
                  different from the open legs)

    Picks live vs historical mode automatically from the timestamp
    freshness. Returns a dict with side-suffixed keys
    (`pop_at_open` vs `pop_at_close`) so the result can be merged
    straight into a PATCH payload.
    """
    ticker = entry.get("ticker")
    if side == "close":
        legs = legs_override or entry.get("close_legs") or entry.get("legs") or []
        ts_str = entry.get("close_at") or entry.get("close_date")
        ref_price = entry.get("close_price")
    else:
        legs = legs_override or entry.get("legs") or []
        ts_str = entry.get("open_at") or entry.get("open_date")
        ref_price = entry.get("open_price")

    if not ticker or not legs:
        return {"error": f"missing ticker or {side} legs"}

    ref_dt = _parse_ts(ts_str) or datetime.now()
    age_hours = (datetime.now() - ref_dt).total_seconds() / 3600.0
    is_live = age_hours <= _LIVE_FRESHNESS_HOURS

    spot = _live_spot(ticker) if is_live else _spot_at(ticker, ref_dt)
    if spot is None:
        # Live miss — fall back to last historical close
        spot = _spot_at(ticker, ref_dt)
        if spot is None:
            return {"error": f"could not fetch spot for {ticker}"}

    realized_vol = _realized_vol(ticker, ref_dt) if not is_live else None
    leg_results = []
    iv_weights = []
    for leg in legs:
        kind = leg.get("type") or ""
        strike = float(leg.get("strike") or 0)
        exp_iso = _exp_from_yymmdd(leg.get("exp", ""))
        if not exp_iso or strike <= 0:
            continue
        T = _years_to_expiry(exp_iso, ref=ref_dt)
        if T is None:
            continue
        # Pick IV
        iv = _iv_for_strike(ticker, exp_iso, strike, kind) if is_live else None
        if iv is None:
            iv = realized_vol  # historical fallback
        if iv is None or iv <= 0:
            continue
        g = bs_greeks(spot, strike, T, iv, kind)
        if g is None:
            continue
        sign_qty = _signed_qty(leg.get("action", ""), leg.get("quantity", 1))
        leg_results.append({
            "leg": f"{strike}{kind} {exp_iso}",
            "iv": iv, "sign_qty": sign_qty, **g,
        })
        iv_weights.append((iv, abs(g["delta"]) + 1e-6))

    if not leg_results:
        return {"error": "no leg Greeks computed (no IV or expired)"}

    delta = sum(r["delta"] * r["sign_qty"] for r in leg_results) * 100
    gamma = sum(r["gamma"] * r["sign_qty"] for r in leg_results) * 100
    theta = sum(r["theta"] * r["sign_qty"] for r in leg_results) * 100
    vega  = sum(r["vega"]  * r["sign_qty"] for r in leg_results) * 100
    tot_w = sum(w for _, w in iv_weights)
    iv_avg = sum(iv * w for iv, w in iv_weights) / tot_w if tot_w > 0 else 0

    # COP via Black-Scholes — uses N(d2) of the breakeven strike. Falls
    # back to the implied-R:R approximation if leg layout isn't recognized
    # (e.g. butterflies, ratio spreads).
    # Use the smallest leg T (front-month for diagonals) and the avg IV.
    min_T = min((r.get("_T", 99) for r in leg_results), default=None)
    # We didn't store T per leg — recompute from first leg
    first_T = _years_to_expiry(_exp_from_yymmdd(legs[0].get("exp", "")) or "", ref=ref_dt) or 0
    pop = _cop_bsm(legs, ref_price, spot, iv_avg, first_T)
    if pop is None:
        pop = _pop_from_price(legs, ref_price)

    suffix = side
    return {
        f"pop_at_{suffix}":   round(pop * 100, 1) if pop is not None else None,
        f"iv_at_{suffix}":    round(iv_avg * 100, 2),
        f"delta_at_{suffix}": round(delta, 3),
        f"gamma_at_{suffix}": round(gamma, 4),
        f"theta_at_{suffix}": round(theta, 3),
        f"vega_at_{suffix}":  round(vega, 3),
        f"spot_at_{suffix}":  round(spot, 2),
        "_source": ("computed (live, yfinance chain IV)" if is_live
                    else "computed (historical, realized-vol IV)"),
        "_legs":   leg_results,
    }


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 (with or without TZ) or YYYY-MM-DD into naive local."""
    if not s:
        return None
    try:
        # Strip trailing Z and parse
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None


# ─── Backfill ──────────────────────────────────────────────────────────
def backfill_all(report_fn=None, force: bool = False) -> dict:
    """Compute Greeks for every Open + Close entry that doesn't already
    have them. Safe to run repeatedly — entries with non-null delta_at_*
    are skipped unless `force=True`. Returns counts.
    """
    import journal_entries as je
    log = report_fn or (lambda *a, **k: None)
    open_entries = je.load_open_entries()
    close_entries = je.load_close_entries()

    open_done = open_skipped = open_errors = 0
    for e in open_entries:
        if not force and e.get("delta_at_open") is not None:
            open_skipped += 1
            continue
        try:
            r = compute_entry_greeks(e, side="open")
        except Exception as ex:
            log(f"  ! open {e.get('ticker')} {e.get('spread_label')} → exception: {ex}")
            open_errors += 1
            continue
        if "error" in r:
            log(f"  ! open {e.get('ticker')} {e.get('spread_label')} → {r['error']}")
            open_errors += 1
            continue
        # PATCH only the greek fields
        updates = {k: v for k, v in r.items() if not k.startswith("_")}
        je.update_entry_fields(e["id"], updates)
        open_done += 1
        if open_done % 25 == 0:
            log(f"  open progress: {open_done} done")

    close_done = close_skipped = close_errors = 0
    for e in close_entries:
        if not force and e.get("delta_at_close") is not None:
            close_skipped += 1
            continue
        try:
            r = compute_entry_greeks(e, side="close")
        except Exception as ex:
            log(f"  ! close {e.get('ticker')} {e.get('spread_label')} → exception: {ex}")
            close_errors += 1
            continue
        if "error" in r:
            log(f"  ! close {e.get('ticker')} {e.get('spread_label')} → {r['error']}")
            close_errors += 1
            continue
        updates = {k: v for k, v in r.items() if not k.startswith("_")}
        je.update_entry_fields(e["id"], updates)
        close_done += 1
        if close_done % 25 == 0:
            log(f"  close progress: {close_done} done")

    return {
        "open_done": open_done, "open_skipped": open_skipped, "open_errors": open_errors,
        "close_done": close_done, "close_skipped": close_skipped, "close_errors": close_errors,
    }


def _pop_from_price(legs: list, price: Optional[float]) -> Optional[float]:
    """LEGACY: implied R:R = max_profit / width. Kept as fallback when
    BSM inputs (spot/IV/T) aren't available. Real COP comes from
    `_cop_bsm` below — it's what brokers like TastyTrade show."""
    if price is None or not legs:
        return None
    strikes = sorted({float(l.get("strike") or 0) for l in legs if l.get("strike")})
    if len(strikes) < 2:
        return None
    width_dollars = (strikes[-1] - strikes[0]) * 100
    if width_dollars <= 0:
        return None
    if price < 0:
        max_profit = width_dollars - abs(price)
    elif price > 0:
        max_profit = price
    else:
        return None
    if max_profit <= 0:
        return None
    return max_profit / width_dollars


def _cop_bsm(legs: list, price: Optional[float], spot: float,
             iv: float, t_years: float, r: float = _RISK_FREE_RATE) -> Optional[float]:
    """Real Chance of Profit via Black-Scholes — what TastyTrade displays.

    For a vertical, we compute the BREAKEVEN price and return
    P(S_T crosses breakeven into profit), which is N(d2) of the
    breakeven strike (or 1 - N(d2), depending on direction).

    Strategy detection from leg signs (Buy/Sell) + types (C/P):
      • Long call lower + Short call higher = BULL CALL DEBIT
          BE = long_strike + debit/100 ;  COP = N(d2)
      • Long put higher + Short put lower  = BEAR PUT DEBIT
          BE = long_strike - debit/100 ;  COP = N(-d2)
      • Short call lower + Long call higher = BEAR CALL CREDIT
          BE = short_strike + credit/100; COP = N(-d2)
      • Short put higher + Long put lower  = BULL PUT CREDIT
          BE = short_strike - credit/100; COP = N(d2)
      • Iron Condor (4 legs): COP = N(d2_upper_be) - N(d2_lower_be)
      • Anything else: returns None — caller falls back to _pop_from_price.
    """
    if price is None or not legs or spot <= 0 or iv <= 0 or t_years <= 0:
        return None
    # Categorize legs
    typed = []
    for l in legs:
        try:
            strike = float(l.get("strike"))
        except (TypeError, ValueError):
            return None
        action = (l.get("action") or "").lower()
        kind = (l.get("type") or "").upper()
        if not kind:
            return None
        is_short = "sell" in action
        typed.append({"strike": strike, "kind": kind, "short": is_short})

    calls = [l for l in typed if l["kind"].startswith("C")]
    puts  = [l for l in typed if l["kind"].startswith("P")]

    def cdf_d2(strike):
        # d2 of `strike` for given spot/iv/T — used for P(S_T > strike)
        sqrtT = math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrtT)
        d2 = d1 - iv * sqrtT
        return _norm_cdf(d2)

    # 4-leg iron condor: 2 calls + 2 puts, mixed long/short
    if len(typed) == 4 and len(calls) == 2 and len(puts) == 2:
        short_call = next((c for c in calls if c["short"]), None)
        short_put  = next((p for p in puts  if p["short"]), None)
        if not short_call or not short_put:
            return None
        net_credit = price if price > 0 else 0
        per_share = net_credit / 100.0
        upper_be = short_call["strike"] + per_share
        lower_be = short_put["strike"]  - per_share
        # COP = P(lower_be < S_T < upper_be) = N(d2_upper) - N(d2_lower)
        return max(0.0, min(1.0, cdf_d2(upper_be) - cdf_d2(lower_be)))

    # 2-leg vertical
    if len(typed) != 2:
        return None  # caller falls back

    if len(calls) == 2:
        call_long  = next((c for c in calls if not c["short"]), None)
        call_short = next((c for c in calls if c["short"]), None)
        if not call_long or not call_short:
            return None
        if call_long["strike"] < call_short["strike"]:
            # BULL CALL DEBIT — breakeven above long strike
            be = call_long["strike"] + abs(price) / 100.0
            return cdf_d2(be)                       # N(d2)
        else:
            # BEAR CALL CREDIT — breakeven above short strike
            be = call_short["strike"] + price / 100.0
            return 1.0 - cdf_d2(be)                 # N(-d2)
    if len(puts) == 2:
        put_long  = next((p for p in puts if not p["short"]), None)
        put_short = next((p for p in puts if p["short"]), None)
        if not put_long or not put_short:
            return None
        if put_long["strike"] > put_short["strike"]:
            # BEAR PUT DEBIT — breakeven below long strike
            be = put_long["strike"] - abs(price) / 100.0
            return 1.0 - cdf_d2(be)                 # N(-d2)
        else:
            # BULL PUT CREDIT — breakeven below short strike
            be = put_short["strike"] - price / 100.0
            return cdf_d2(be)                       # N(d2)
    return None

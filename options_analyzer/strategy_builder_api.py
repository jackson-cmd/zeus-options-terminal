# options_analyzer/strategy_builder_api.py
"""
Strategy Builder tab — backend.

Provides a FastAPI router (`router`) and pure helpers used by tests:
- snap_to_strike(x, strikes) -> nearest strike
- preset_legs(strategy_type, spot, strikes, expiration) -> list[dict]
- diff_subscribe(ticker, new_symbols, builder_symbols, position_symbols)
    -> (to_sub, to_unsub)

Endpoints:
- GET  /api/builder/expirations/{ticker}
- GET  /api/builder/chain/{ticker}?expiration=YYYY-MM-DD
- POST /api/builder/subscribe   { ticker, leg_streamer_symbols: [...] }
- POST /api/builder/clear       { ticker }
"""
from __future__ import annotations

import math
import time
import datetime as _dt
from typing import Optional


# ── BSM implied-vol solver ─────────────────────────────────────────────
# TastyTrade's option-quote endpoint omits `implied-volatility` for many
# strikes (especially OTM equity options), so we compute IV ourselves from
# the bid/ask mid via Newton-Raphson on the BSM price. Falls back to a
# bisection bracket if Newton diverges. r = 4% is a fixed approximation —
# the sensitivity to r is small relative to mid-quote noise for typical
# 30-90 DTE options.

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bsm_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bsm_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    return S * _norm_pdf(d1) * sqrtT


RFR = 0.045  # match RISK_FREE_RATE in optionstrat_analyzer.py / S.rfr in strategy_builder.py


def solve_iv(mid: float, S: float, K: float, T: float, is_call: bool, r: float = RFR) -> float:
    """Newton-Raphson IV solver. Returns 0.0 on failure / no-arb violation."""
    if mid <= 0 or S <= 0 or K <= 0 or T <= 0:
        return 0.0
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    upper_bound = S if is_call else K * math.exp(-r * T)
    if mid <= intrinsic or mid >= upper_bound:
        return 0.0
    sigma = 0.5  # initial guess
    for _ in range(50):
        price = _bsm_price(S, K, T, r, sigma, is_call)
        diff = price - mid
        if abs(diff) < 1e-4:
            return max(0.0, sigma)
        vega = _bsm_vega(S, K, T, r, sigma)
        if vega < 1e-8:
            break
        sigma = sigma - diff / vega
        if sigma <= 0.001 or sigma > 5.0:
            break
    # Newton failed → bisection on [0.001, 5.0]
    lo, hi = 0.001, 5.0
    for _ in range(60):
        mid_sigma = (lo + hi) / 2
        if _bsm_price(S, K, T, r, mid_sigma, is_call) > mid:
            hi = mid_sigma
        else:
            lo = mid_sigma
        if hi - lo < 1e-4:
            return max(0.0, mid_sigma)
    return 0.0

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/builder", tags=["strategy-builder"])

# Caches (TTL-bounded in-memory dicts, per-process)
_EXPIRATIONS_TTL = 300  # 5 minutes
_CHAIN_TTL = 60         # 60 seconds
_expirations_cache: dict[str, tuple[float, list[dict]]] = {}
_chain_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _cache_get(cache: dict, key, ttl: int):
    entry = cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.time() - ts > ttl:
        del cache[key]
        return None
    return val


def _cache_put(cache: dict, key, val):
    cache[key] = (time.time(), val)


def snap_to_strike(x: float, strikes: list[float]) -> float:
    """Return the strike from `strikes` closest to `x`.

    On exact mid-point ties, the lower strike wins (Python's `min` is stable
    and iteration order matches the input list, which callers always pass
    sorted ascending). Raises ValueError if `strikes` is empty.
    """
    if not strikes:
        raise ValueError("strikes must be non-empty")
    return min(strikes, key=lambda s: (abs(s - x), s))


def _occ_symbol(ticker: str, exp: str, opt_type: str, strike: float) -> str:
    """Build OCC 21-byte option symbol. exp is YYYY-MM-DD."""
    yy, mm, dd = exp[2:4], exp[5:7], exp[8:10]
    strike_int = int(round(strike * 1000))
    return f"{ticker:<6}{yy}{mm}{dd}{opt_type}{strike_int:08d}"


def _streamer_symbol(ticker: str, exp: str, opt_type: str, strike: float) -> str:
    """Build DXLink streamer symbol (matches paper_trade_api.compute_streamer_symbol output)."""
    yy, mm, dd = exp[2:4], exp[5:7], exp[8:10]
    strike_str = f"{strike:.0f}" if strike == int(strike) else str(strike)
    return f".{ticker}{yy}{mm}{dd}{opt_type}{strike_str}"


def _make_leg(ticker: str, exp: str, opt_type: str, strike: float, qty: int) -> dict:
    return {
        "type": opt_type,
        "strike": float(strike),
        "qty": qty,
        "exp": exp,
        "symbol": _occ_symbol(ticker, exp, opt_type, strike),
        "streamer_symbol": _streamer_symbol(ticker, exp, opt_type, strike),
        "mult": 100,
        "entry": 0.0,
        "current": 0.0,
        "bid": 0.0,
        "ask": 0.0,
        "mid": 0.0,
    }


def _pick_distinct(target: float, strikes: list[float], exclude: set[float]) -> float:
    """Snap to nearest strike not in `exclude`. If the nearest IS excluded, walk
    outward (in the direction away from spot center) until a free strike is found.
    """
    if not strikes:
        raise ValueError("strikes must be non-empty")
    nearest = snap_to_strike(target, strikes)
    if nearest not in exclude:
        return nearest
    sorted_s = sorted(strikes)
    idx = sorted_s.index(nearest)
    for offset in range(1, len(sorted_s)):
        for direction in (1, -1):
            j = idx + direction * offset
            if 0 <= j < len(sorted_s) and sorted_s[j] not in exclude:
                return sorted_s[j]
    raise ValueError("no free strike available")


def preset_legs(strategy_type: str, spot: float, strikes: list[float],
                expiration: str, ticker: str = "AAA") -> list[dict]:
    """Generate the 2- or 4-leg structure for a preset strategy.

    Strikes default at ATM ± 5% (vertical) or ±5% / ±10% (condors).
    Strike collisions on sparse chains are resolved by walking outward.
    """
    atm = snap_to_strike(spot, strikes)

    def near(pct: float, exclude: set[float]) -> float:
        return _pick_distinct(spot * (1 + pct), strikes, exclude)

    if strategy_type == "bull_call_spread":
        long_k = atm
        short_k = near(0.05, {long_k})
        return [
            _make_leg(ticker, expiration, "C", long_k, +1),
            _make_leg(ticker, expiration, "C", short_k, -1),
        ]
    if strategy_type == "bear_put_spread":
        long_k = atm
        short_k = near(-0.05, {long_k})
        return [
            _make_leg(ticker, expiration, "P", long_k, +1),
            _make_leg(ticker, expiration, "P", short_k, -1),
        ]
    if strategy_type == "iron_condor":
        sp = near(-0.05, set())
        lp = near(-0.10, {sp})
        sc = near(+0.05, {sp, lp})
        lc = near(+0.10, {sp, lp, sc})
        return [
            _make_leg(ticker, expiration, "P", sp, -1),
            _make_leg(ticker, expiration, "P", lp, +1),
            _make_leg(ticker, expiration, "C", sc, -1),
            _make_leg(ticker, expiration, "C", lc, +1),
        ]
    if strategy_type == "inverse_iron_condor":
        lp = near(-0.05, set())
        sp = near(-0.10, {lp})
        lc = near(+0.05, {lp, sp})
        sc = near(+0.10, {lp, sp, lc})
        return [
            _make_leg(ticker, expiration, "P", lp, +1),
            _make_leg(ticker, expiration, "P", sp, -1),
            _make_leg(ticker, expiration, "C", lc, +1),
            _make_leg(ticker, expiration, "C", sc, -1),
        ]

    # ── Single-leg ─────────────────────────────────────────────────
    if strategy_type == "long_call":
        return [_make_leg(ticker, expiration, "C", atm, +1)]
    if strategy_type == "long_put":
        return [_make_leg(ticker, expiration, "P", atm, +1)]
    if strategy_type == "short_call":
        return [_make_leg(ticker, expiration, "C", atm, -1)]
    if strategy_type == "short_put":
        return [_make_leg(ticker, expiration, "P", atm, -1)]

    # ── Credit verticals (the put/call mirror of bull-call/bear-put) ──
    if strategy_type == "bull_put_spread":
        # Sell ATM put, buy lower put for downside protection
        short_k = atm
        long_k = near(-0.05, {short_k})
        return [
            _make_leg(ticker, expiration, "P", short_k, -1),
            _make_leg(ticker, expiration, "P", long_k, +1),
        ]
    if strategy_type == "bear_call_spread":
        short_k = atm
        long_k = near(+0.05, {short_k})
        return [
            _make_leg(ticker, expiration, "C", short_k, -1),
            _make_leg(ticker, expiration, "C", long_k, +1),
        ]

    # ── Iron Butterfly (and inverse) — narrow-band income / breakout plays ──
    if strategy_type == "iron_butterfly":
        long_p = near(-0.05, set())
        short_p = atm
        short_c = atm
        long_c = near(+0.05, {long_p})
        return [
            _make_leg(ticker, expiration, "P", long_p, +1),
            _make_leg(ticker, expiration, "P", short_p, -1),
            _make_leg(ticker, expiration, "C", short_c, -1),
            _make_leg(ticker, expiration, "C", long_c, +1),
        ]
    if strategy_type == "inverse_iron_butterfly":
        short_p = near(-0.05, set())
        long_p = atm
        long_c = atm
        short_c = near(+0.05, {short_p})
        return [
            _make_leg(ticker, expiration, "P", short_p, -1),
            _make_leg(ticker, expiration, "P", long_p, +1),
            _make_leg(ticker, expiration, "C", long_c, +1),
            _make_leg(ticker, expiration, "C", short_c, -1),
        ]

    # ── Butterflies (single-type, 1×−2×1×) ──
    if strategy_type == "long_call_butterfly":
        center = atm
        wing_lo = near(-0.05, {center})
        wing_hi = near(+0.05, {center, wing_lo})
        return [
            _make_leg(ticker, expiration, "C", wing_lo, +1),
            _make_leg(ticker, expiration, "C", center, -2),
            _make_leg(ticker, expiration, "C", wing_hi, +1),
        ]
    if strategy_type == "long_put_butterfly":
        center = atm
        wing_lo = near(-0.05, {center})
        wing_hi = near(+0.05, {center, wing_lo})
        return [
            _make_leg(ticker, expiration, "P", wing_lo, +1),
            _make_leg(ticker, expiration, "P", center, -2),
            _make_leg(ticker, expiration, "P", wing_hi, +1),
        ]
    if strategy_type == "short_call_butterfly":
        center = atm
        wing_lo = near(-0.05, {center})
        wing_hi = near(+0.05, {center, wing_lo})
        return [
            _make_leg(ticker, expiration, "C", wing_lo, -1),
            _make_leg(ticker, expiration, "C", center, +2),
            _make_leg(ticker, expiration, "C", wing_hi, -1),
        ]
    if strategy_type == "short_put_butterfly":
        center = atm
        wing_lo = near(-0.05, {center})
        wing_hi = near(+0.05, {center, wing_lo})
        return [
            _make_leg(ticker, expiration, "P", wing_lo, -1),
            _make_leg(ticker, expiration, "P", center, +2),
            _make_leg(ticker, expiration, "P", wing_hi, -1),
        ]

    # ── Volatility / direction-agnostic ──
    if strategy_type == "long_straddle":
        return [
            _make_leg(ticker, expiration, "C", atm, +1),
            _make_leg(ticker, expiration, "P", atm, +1),
        ]
    if strategy_type == "long_strangle":
        ck = near(+0.05, {atm})
        pk = near(-0.05, {atm, ck})
        return [
            _make_leg(ticker, expiration, "P", pk, +1),
            _make_leg(ticker, expiration, "C", ck, +1),
        ]
    if strategy_type == "short_straddle":
        return [
            _make_leg(ticker, expiration, "C", atm, -1),
            _make_leg(ticker, expiration, "P", atm, -1),
        ]
    if strategy_type == "short_strangle":
        ck = near(+0.05, {atm})
        pk = near(-0.05, {atm, ck})
        return [
            _make_leg(ticker, expiration, "P", pk, -1),
            _make_leg(ticker, expiration, "C", ck, -1),
        ]
    if strategy_type == "guts":
        # ITM straddle equivalent: long ITM call + long ITM put (both deep)
        ck = near(-0.05, set())   # call below spot = ITM
        pk = near(+0.05, {ck})    # put above spot = ITM
        return [
            _make_leg(ticker, expiration, "C", ck, +1),
            _make_leg(ticker, expiration, "P", pk, +1),
        ]

    # ── Strip / Strap (asymmetric volatility plays) ──
    if strategy_type == "strip":
        # Bearish bias: 1 long call + 2 long puts at ATM
        return [
            _make_leg(ticker, expiration, "C", atm, +1),
            _make_leg(ticker, expiration, "P", atm, +2),
        ]
    if strategy_type == "strap":
        # Bullish bias: 2 long calls + 1 long put at ATM
        return [
            _make_leg(ticker, expiration, "C", atm, +2),
            _make_leg(ticker, expiration, "P", atm, +1),
        ]

    # ── Synthetic combos (arbitrage building blocks) ──
    if strategy_type == "long_combo":
        # Synthetic long stock = long ATM call + short ATM put
        return [
            _make_leg(ticker, expiration, "C", atm, +1),
            _make_leg(ticker, expiration, "P", atm, -1),
        ]
    if strategy_type == "short_combo":
        return [
            _make_leg(ticker, expiration, "C", atm, -1),
            _make_leg(ticker, expiration, "P", atm, +1),
        ]

    # ── Condors (single-type 4-leg) ──
    if strategy_type == "long_call_condor":
        a = near(-0.10, set())
        b = near(-0.05, {a})
        c = near(+0.05, {a, b})
        d = near(+0.10, {a, b, c})
        return [
            _make_leg(ticker, expiration, "C", a, +1),
            _make_leg(ticker, expiration, "C", b, -1),
            _make_leg(ticker, expiration, "C", c, -1),
            _make_leg(ticker, expiration, "C", d, +1),
        ]
    if strategy_type == "long_put_condor":
        a = near(-0.10, set())
        b = near(-0.05, {a})
        c = near(+0.05, {a, b})
        d = near(+0.10, {a, b, c})
        return [
            _make_leg(ticker, expiration, "P", a, +1),
            _make_leg(ticker, expiration, "P", b, -1),
            _make_leg(ticker, expiration, "P", c, -1),
            _make_leg(ticker, expiration, "P", d, +1),
        ]
    if strategy_type == "short_call_condor":
        a = near(-0.10, set())
        b = near(-0.05, {a})
        c = near(+0.05, {a, b})
        d = near(+0.10, {a, b, c})
        return [
            _make_leg(ticker, expiration, "C", a, -1),
            _make_leg(ticker, expiration, "C", b, +1),
            _make_leg(ticker, expiration, "C", c, +1),
            _make_leg(ticker, expiration, "C", d, -1),
        ]
    if strategy_type == "short_put_condor":
        a = near(-0.10, set())
        b = near(-0.05, {a})
        c = near(+0.05, {a, b})
        d = near(+0.10, {a, b, c})
        return [
            _make_leg(ticker, expiration, "P", a, -1),
            _make_leg(ticker, expiration, "P", b, +1),
            _make_leg(ticker, expiration, "P", c, +1),
            _make_leg(ticker, expiration, "P", d, -1),
        ]

    # ── Ratio spreads (1×2) ──
    if strategy_type == "call_ratio_spread":
        # Long 1 lower call, short 2 higher calls — debit, capped down, naked up
        long_k = atm
        short_k = near(+0.05, {long_k})
        return [
            _make_leg(ticker, expiration, "C", long_k, +1),
            _make_leg(ticker, expiration, "C", short_k, -2),
        ]
    if strategy_type == "put_ratio_spread":
        long_k = atm
        short_k = near(-0.05, {long_k})
        return [
            _make_leg(ticker, expiration, "P", long_k, +1),
            _make_leg(ticker, expiration, "P", short_k, -2),
        ]
    if strategy_type == "call_ratio_backspread":
        # Short 1 lower call, long 2 higher calls — credit/free, breakout-bullish
        short_k = atm
        long_k = near(+0.05, {short_k})
        return [
            _make_leg(ticker, expiration, "C", short_k, -1),
            _make_leg(ticker, expiration, "C", long_k, +2),
        ]
    if strategy_type == "put_ratio_backspread":
        short_k = atm
        long_k = near(-0.05, {short_k})
        return [
            _make_leg(ticker, expiration, "P", short_k, -1),
            _make_leg(ticker, expiration, "P", long_k, +2),
        ]

    # ── Jade Lizard family ──
    if strategy_type == "jade_lizard":
        # Short put + short call + long farther call. Bullish-neutral, no
        # upside risk if the call credit covers the spread width.
        sp = near(-0.05, set())
        sc = near(+0.05, {sp})
        lc = near(+0.10, {sp, sc})
        return [
            _make_leg(ticker, expiration, "P", sp, -1),
            _make_leg(ticker, expiration, "C", sc, -1),
            _make_leg(ticker, expiration, "C", lc, +1),
        ]
    if strategy_type == "reverse_jade_lizard":
        # Short call + short put + long farther put. Bearish-neutral.
        sc = near(+0.05, set())
        sp = near(-0.05, {sc})
        lp = near(-0.10, {sc, sp})
        return [
            _make_leg(ticker, expiration, "C", sc, -1),
            _make_leg(ticker, expiration, "P", sp, -1),
            _make_leg(ticker, expiration, "P", lp, +1),
        ]

    raise ValueError(f"unknown strategy_type: {strategy_type}")


_DEFAULT_EXP_WINDOW_DAYS = 365


def filter_expirations_within_90_days(raw_items: list[dict],
                                      today: Optional[_dt.date] = None,
                                      window_days: int = _DEFAULT_EXP_WINDOW_DAYS) -> list[dict]:
    """Take TT chain `expirations` items and return [{date, dte, type}] within
    [today, today + window_days], sorted ascending. Past dates and beyond
    the window are dropped. Window default raised from 90 → 365 days so the
    strip exposes weeklies + monthlies + LEAPs within a year. Function name
    kept for backward-compat with tests; pass window_days=90 to restore the
    old behaviour."""
    if today is None:
        today = _dt.date.today()
    out = []
    for it in raw_items:
        date_str = it.get("expiration-date") or it.get("expirationDate", "")
        type_str = it.get("expiration-type") or it.get("expirationType", "")
        try:
            d = _dt.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        dte = (d - today).days
        if dte < 0 or dte > window_days:
            continue
        out.append({"date": date_str, "dte": dte, "type": type_str})
    out.sort(key=lambda e: e["date"])
    return out


def fetch_expirations(client, ticker: str,
                      today: Optional[_dt.date] = None) -> list[dict]:
    """Cached fetch + filter. Returns same shape as filter_expirations_within_90_days."""
    ticker = ticker.upper()
    cached = _cache_get(_expirations_cache, ticker, _EXPIRATIONS_TTL)
    if cached is not None:
        return cached
    data = client.get_option_chain(ticker)
    items = data.get("data", {}).get("items", [])
    raw_exps: list[dict] = []
    for chain in items:
        raw_exps.extend(chain.get("expirations", []))
    result = filter_expirations_within_90_days(raw_exps, today=today)
    _cache_put(_expirations_cache, ticker, result)
    return result


@router.get("/expirations/{ticker}")
def expirations_endpoint(ticker: str):
    from optionstrat_server import state  # late import to avoid cycle
    if state.client is None:
        raise HTTPException(503, "TastyTrade client not ready")
    try:
        return {"ticker": ticker.upper(), "expirations": fetch_expirations(state.client, ticker)}
    except Exception as e:
        raise HTTPException(500, str(e))


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fetch_equity_quote(client, ticker: str) -> dict:
    """Return {spot, prev_close, change, change_pct} for `ticker`. Falls back
    to zeros on error so callers don't have to special-case missing data.
    Uses TT REST /market-data/by-type which returns mark/last/prev-close in
    one round-trip — matches what the existing positions stream uses.
    """
    ticker = ticker.upper()
    try:
        resp = client.get("/market-data/by-type", params={"equity": ticker})
        items = resp.get("data", {}).get("items", [])
        if not items:
            return {"spot": 0.0, "prev_close": 0.0, "change": 0.0, "change_pct": 0.0}
        it = items[0]
        spot = (_num(it.get("mark")) or _num(it.get("last"))
                or _num(it.get("close")) or _num(it.get("prev-close")))
        prev = _num(it.get("prev-close")) or _num(it.get("close"))
        change = (spot - prev) if (spot and prev) else 0.0
        change_pct = (change / prev * 100.0) if prev else 0.0
        return {
            "spot": round(spot, 4),
            "prev_close": round(prev, 4),
            "change": round(change, 4),
            "change_pct": round(change_pct, 2),
        }
    except Exception:
        return {"spot": 0.0, "prev_close": 0.0, "change": 0.0, "change_pct": 0.0}


def fetch_chain(client, ticker: str, expiration: str) -> dict:
    """Return {ticker, expiration, spot, prev_close, change, change_pct,
    strikes:[{strike, call:{...}, put:{...}}, ...]}.
    Cached 60s per (ticker, expiration). Strikes sorted ascending.
    """
    ticker = ticker.upper()
    key = (ticker, expiration)
    now = time.time()
    entry = _chain_cache.get(key)
    if entry is not None:
        ts, val = entry
        if now - ts <= _CHAIN_TTL:
            return val
        del _chain_cache[key]

    eq = fetch_equity_quote(client, ticker)
    data = client.get_option_chain(ticker)
    items = data.get("data", {}).get("items", [])
    target = None
    for chain in items:
        for exp in chain.get("expirations", []):
            if (exp.get("expiration-date") or exp.get("expirationDate")) == expiration:
                target = exp
                break
        if target:
            break
    if not target:
        return {"ticker": ticker, "expiration": expiration, "strikes": [], **eq}

    rows = []
    all_syms: list = []
    for s in target.get("strikes", []):
        sp = _num(s.get("strike-price") or s.get("strikePrice"))
        call_sym = (s.get("call") or "").strip()
        put_sym = (s.get("put") or "").strip()
        rows.append({
            "strike": sp,
            "_call_sym": call_sym,
            "_put_sym": put_sym,
            "call": {"bid": 0.0, "ask": 0.0, "mid": 0.0, "iv": 0.0, "vol": 0, "oi": 0,
                     "streamer": ""},
            "put":  {"bid": 0.0, "ask": 0.0, "mid": 0.0, "iv": 0.0, "vol": 0, "oi": 0,
                     "streamer": ""},
        })
        if call_sym:
            all_syms.append(call_sym)
        if put_sym:
            all_syms.append(put_sym)

    quote_map: dict = {}
    BATCH = 100
    for i in range(0, len(all_syms), BATCH):
        batch = all_syms[i:i + BATCH]
        try:
            resp = client.get_option_quotes(batch)
            for q in resp.get("data", {}).get("items", []):
                quote_map[q.get("symbol", "")] = q
        except Exception:
            pass

    from paper_trade_api import compute_streamer_symbol  # reuse existing helper

    # T (years) for IV solver — same for all strikes at this expiration
    spot_for_iv = _num(eq.get("spot") or eq.get("price") or 0)
    try:
        exp_dt = _dt.datetime.strptime(expiration, "%Y-%m-%d").replace(hour=16)
        T_years = max(1e-6, (exp_dt - _dt.datetime.now()).total_seconds() / (365.0 * 86400.0))
    except Exception:
        T_years = 0.0

    for r in rows:
        for side, sym_key in (("call", "_call_sym"), ("put", "_put_sym")):
            sym = r[sym_key]
            q = quote_map.get(sym, {})
            bid = _num(q.get("bid") or q.get("bid-price"))
            ask = _num(q.get("ask") or q.get("ask-price"))
            # Prefer TastyTrade's own `mark` (platform mid, smarter for wide
            # spreads) over a naive (bid+ask)/2.
            mark = _num(q.get("mark"))
            mid_tt = _num(q.get("mid"))
            if mark > 0:
                mid = round(mark, 2)
            elif mid_tt > 0:
                mid = round(mid_tt, 2)
            elif bid > 0 and ask > 0:
                mid = round((bid + ask) / 2, 2)
            else:
                mid = 0.0
            # Read IV directly from TastyTrade — the field is named
            # `volatility` (not `implied-volatility`). Fall back to a
            # local BSM Newton-Raphson solver only when missing.
            iv_raw = _num(q.get("volatility") or q.get("implied-volatility") or q.get("impliedVolatility"))
            if iv_raw <= 0 and mid > 0 and spot_for_iv > 0 and T_years > 0:
                iv_raw = solve_iv(mid, spot_for_iv, r["strike"], T_years, side == "call")
            r[side] = {
                "symbol": sym,
                "streamer": compute_streamer_symbol(sym),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "iv": iv_raw,
                "vol": _int(q.get("volume") or q.get("day-volume")),
                "oi": _int(q.get("open-interest") or q.get("openInterest")),
            }
        del r["_call_sym"]
        del r["_put_sym"]

    rows.sort(key=lambda r: r["strike"])
    result = {"ticker": ticker, "expiration": expiration, "strikes": rows, **eq}
    _cache_put(_chain_cache, key, result)
    return result


@router.get("/chain/{ticker}")
def chain_endpoint(ticker: str, expiration: str):
    from optionstrat_server import state
    if state.client is None:
        raise HTTPException(503, "TastyTrade client not ready")
    try:
        return fetch_chain(state.client, ticker, expiration)
    except Exception as e:
        raise HTTPException(500, str(e))


def diff_subscribe(ticker: str, new_symbols: list[str],
                   builder_symbols: dict[str, set],
                   position_symbols: set) -> tuple[set, set]:
    """Compute (to_sub, to_unsub) when the builder for `ticker` switches its
    leg-symbol set to `new_symbols`.

    Mutates `builder_symbols[ticker]` in place to the new set.

    Critical: `to_unsub` excludes any symbol still in `position_symbols` —
    the user owns it as a real position and the DXLink stream must keep it.
    Also excludes symbols still being modeled by other tickers' builder sets.
    """
    ticker = ticker.upper()
    new_set = set(new_symbols)
    old_set = builder_symbols.get(ticker, set())
    other_builder = set()
    for tk, syms in builder_symbols.items():
        if tk != ticker:
            other_builder |= syms

    to_sub = new_set - old_set
    raw_unsub = old_set - new_set
    to_unsub = {s for s in raw_unsub if s not in position_symbols and s not in other_builder}
    builder_symbols[ticker] = new_set
    return to_sub, to_unsub


class SubscribeBody(BaseModel):
    ticker: str
    leg_streamer_symbols: list[str]


class ClearBody(BaseModel):
    ticker: str


@router.post("/subscribe")
def subscribe_endpoint(body: SubscribeBody):
    from optionstrat_server import state
    position_syms = set(state.symbols_map.keys())
    to_sub, to_unsub = diff_subscribe(
        body.ticker, body.leg_streamer_symbols,
        state.builder_symbols, position_syms,
    )
    if to_sub or to_unsub:
        state.builder_resub_pending = True
    return {
        "ticker": body.ticker.upper(),
        "subscribed": sorted(state.builder_symbols.get(body.ticker.upper(), set())),
        "added": sorted(to_sub),
        "removed": sorted(to_unsub),
    }


@router.post("/clear")
def clear_endpoint(body: ClearBody):
    from optionstrat_server import state
    position_syms = set(state.symbols_map.keys())
    to_sub, to_unsub = diff_subscribe(
        body.ticker, [], state.builder_symbols, position_syms,
    )
    if to_unsub:
        state.builder_resub_pending = True
    return {"ticker": body.ticker.upper(), "removed": sorted(to_unsub)}


# ── Saved trades (Builder-only persistence) ──────────────────────────────
# Mirrors how Paper Trade persists hand-built spreads. Lives in its own
# JSON file so it never collides with Paper Trade data, and stays out of
# the Positions stream entirely (no DXLink subscription on save — quotes
# are pulled on demand via fetch_chain when listing).

import json as _json
import uuid as _uuid
from pathlib import Path as _Path

_SAVED_FILE = _Path(__file__).resolve().parent.parent / "data" / "builder_saved.json"


def _load_saved() -> list:
    try:
        with open(_SAVED_FILE, "r") as f:
            data = _json.load(f)
        return data.get("trades", []) if isinstance(data, dict) else []
    except (FileNotFoundError, _json.JSONDecodeError):
        return []


def _write_saved(trades: list) -> None:
    _SAVED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_SAVED_FILE, "w") as f:
        _json.dump({"trades": trades}, f, indent=2)


def _price_legs_by_exp(client, ticker: str, legs: list, default_exp: str = "") -> dict:
    """Price every leg off the chain for *its own* expiration — the fix for
    calendar/diagonal spreads (same strike, two different exps). A single
    ticker+expiration chain would collide both legs onto one front-month mid
    and net to ~0. We fetch one chain per distinct expiration (each 60s-cached)
    and quote each leg from the chain matching `l['exp']`.

    Returns {leg_quotes: [{bid, ask, mid}, ...same order as legs...],
             priced: int, chains: {exp: chain}}.
    A leg counts as `priced` only when its strike row exists in its own exp's
    chain AND mid > 0. Vertical spreads (all one exp) fetch a single chain and
    behave exactly as before.
    """
    ticker = ticker.upper()
    chains: dict = {}
    by_strike: dict = {}
    leg_quotes: list = []
    priced = 0
    for l in legs:
        exp = (l.get("exp") or default_exp or "").strip()
        if exp not in chains:
            chains[exp] = fetch_chain(client, ticker, exp)
            by_strike[exp] = {round(_num(r["strike"]), 4): r
                              for r in chains[exp].get("strikes", [])}
        row = by_strike[exp].get(round(_num(l.get("strike")), 4))
        side = (row.get("call" if l.get("type") == "C" else "put", {}) if row else {})
        bid = _num(side.get("bid"))
        ask = _num(side.get("ask"))
        mid = _num(side.get("mid")) or (round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0.0)
        leg_quotes.append({"bid": bid, "ask": ask, "mid": mid})
        if mid > 0:
            priced += 1
    return {"leg_quotes": leg_quotes, "priced": priced, "chains": chains}


def _compute_pnl_summary(client, trade: dict) -> dict:
    """Pull a fresh chain (60s-cached) per distinct leg expiration and compute
    current mark, current P/L $, current P/L %. Calendar/diagonal safe: each
    leg is priced off the chain matching its own `exp`, not one shared front
    month. Falls back gracefully (stale=True) when quotes aren't available."""
    ticker = trade.get("ticker", "").upper()
    expiration = trade.get("expiration", "")
    legs = trade.get("legs", [])
    entry_net = 0.0
    for l in legs:
        entry_net += _num(l.get("entry")) * _num(l.get("qty")) * (_num(l.get("mult")) or 100)
    out = {"entry_net": round(entry_net, 2),
           "current_net": None, "pnl": None, "pnl_pct": None,
           "spot": None, "stale": True}
    if client is None or not ticker or not legs:
        return out
    try:
        res = _price_legs_by_exp(client, ticker, legs, default_exp=expiration)
    except Exception:
        return out
    chains = res["chains"]
    primary = chains.get(expiration) or next(iter(chains.values()), None)
    if primary:
        out["spot"] = primary.get("spot")
    # Guard: only a hard P/L when EVERY leg priced (mid>0). Any missing/zero
    # leg → leave stale=True so the UI shows "—" rather than a fake -100%.
    if res["priced"] == len(legs):
        cur_net = 0.0
        for l, q in zip(legs, res["leg_quotes"]):
            cur_net += q["mid"] * _num(l.get("qty")) * (_num(l.get("mult")) or 100)
        pnl = cur_net - entry_net
        out["current_net"] = round(cur_net, 2)
        out["pnl"] = round(pnl, 2)
        denom = abs(entry_net) if entry_net else 0
        out["pnl_pct"] = round(pnl / denom * 100, 2) if denom else None
        out["stale"] = False
    return out


class SavedLeg(BaseModel):
    type: str
    strike: float
    qty: int
    exp: str
    symbol: Optional[str] = None
    streamer_symbol: Optional[str] = None
    mult: int = 100
    entry: float = 0.0
    bid: Optional[float] = 0.0
    ask: Optional[float] = 0.0
    mid: Optional[float] = 0.0


class SaveBody(BaseModel):
    name: Optional[str] = None
    ticker: str
    expiration: str
    strategy_type: str = "custom"
    spot: Optional[float] = None
    iv: Optional[float] = None
    legs: list[SavedLeg]
    notes: Optional[str] = ""


@router.post("/saved")
def save_trade(body: SaveBody):
    """Persist the current builder strategy. Returns the saved record."""
    trade = {
        "id": _uuid.uuid4().hex[:12],
        "name": (body.name or _default_name(body.strategy_type, body.ticker, body.legs)),
        "ticker": body.ticker.upper(),
        "expiration": body.expiration,
        "strategy_type": body.strategy_type,
        "saved_at": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "saved_spot": body.spot,
        "saved_iv": body.iv,
        "legs": [l.model_dump() for l in body.legs],
        "notes": body.notes or "",
        "status": "open",          # open | closed
        "closed_at": None,
        "close_spot": None,
        "close_legs": None,
        "final_pnl": None,
        "final_pnl_pct": None,
    }
    trades = _load_saved()
    trades.insert(0, trade)
    _write_saved(trades)
    return trade


@router.post("/saved/{trade_id}/close")
def close_saved(trade_id: str):
    """Close a saved open trade — pulls fresh chain, captures the close-out
    mid price for each leg, computes the final P/L, and stamps the record
    closed. Once closed the P/L is locked (refresh ignores closed trades).
    """
    trades = _load_saved()
    target = None
    for t in trades:
        if t.get("id") == trade_id:
            target = t
            break
    if target is None:
        raise HTTPException(404, "saved trade not found")
    if target.get("status") == "closed":
        raise HTTPException(400, "trade already closed")

    try:
        from optionstrat_server import state as _state
        client = _state.client
    except Exception:
        client = None
    if client is None:
        raise HTTPException(503, "TastyTrade client not ready — try again in a moment")

    legs = target["legs"]
    res = _price_legs_by_exp(client, target["ticker"], legs,
                             default_exp=target["expiration"])
    # Guard: refuse to lock a close-out unless EVERY leg quoted (mid>0).
    # Without this a missing leg silently adds $0 and freezes a wrong final_pnl
    # (e.g. calendar legs colliding to a fake $0 net). 503 = retriable.
    if not legs or res["priced"] != len(legs):
        raise HTTPException(
            503, "quotes incomplete for one or more legs — try again in a moment")

    close_legs = []
    cur_net = 0.0
    entry_net = 0.0
    for l, q in zip(legs, res["leg_quotes"]):
        bid, ask, mid = q["bid"], q["ask"], q["mid"]
        close_legs.append({
            **{k: l.get(k) for k in ("type", "strike", "qty", "exp", "symbol",
                                      "streamer_symbol", "mult", "entry")},
            "close_bid": bid, "close_ask": ask, "close_mid": mid,
        })
        qty = _num(l.get("qty"))
        mult = _num(l.get("mult")) or 100
        cur_net += mid * qty * mult
        entry_net += _num(l.get("entry")) * qty * mult

    final_pnl = round(cur_net - entry_net, 2)
    denom = abs(entry_net) if entry_net else 0
    final_pnl_pct = round(final_pnl / denom * 100, 2) if denom else None

    chains = res["chains"]
    primary = chains.get(target["expiration"]) or next(iter(chains.values()), None)

    target["status"] = "closed"
    target["closed_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    target["close_spot"] = primary.get("spot") if primary else None
    target["close_legs"] = close_legs
    target["final_pnl"] = final_pnl
    target["final_pnl_pct"] = final_pnl_pct
    _write_saved(trades)
    return target


def _default_name(strategy_type: str, ticker: str, legs) -> str:
    pretty = strategy_type.replace("_", " ").title()
    if not legs:
        return f"{ticker} {pretty}"
    strikes = sorted(set(round(_num(l.strike if hasattr(l, 'strike') else l['strike']), 2) for l in legs))
    if len(strikes) == 2:
        return f"{ticker} {int(strikes[0])}/{int(strikes[1])} {pretty}"
    if len(strikes) >= 4:
        return f"{ticker} {int(strikes[0])}/{int(strikes[1])}/{int(strikes[-2])}/{int(strikes[-1])} {pretty}"
    return f"{ticker} {pretty}"


@router.get("/saved")
def list_saved(include_pnl: bool = True, status: str = "all"):
    """List saved trades. Filter by status: 'open' | 'closed' | 'all'.
    With include_pnl, every OPEN trade gets a fresh `pnl` block computed
    from the current chain mid prices. CLOSED trades use their locked-in
    `final_pnl` / `final_pnl_pct` (the chain refresh doesn't touch them).
    """
    trades = _load_saved()
    if status in ("open", "closed"):
        trades = [t for t in trades if (t.get("status") or "open") == status]
    if not include_pnl:
        return {"trades": trades}
    try:
        from optionstrat_server import state as _state
        client = _state.client
    except Exception:
        client = None
    out = []
    for t in trades:
        copy = dict(t)
        if (t.get("status") or "open") == "closed":
            # Lock final P/L into the same shape so the frontend renders one row format.
            copy["pnl"] = {
                "entry_net": _entry_net(t.get("legs", [])),
                "current_net": _close_net(t.get("close_legs") or []),
                "pnl": t.get("final_pnl"),
                "pnl_pct": t.get("final_pnl_pct"),
                "spot": t.get("close_spot"),
                "stale": False,
                "locked": True,
            }
        else:
            copy["pnl"] = _compute_pnl_summary(client, t)
        out.append(copy)
    return {"trades": out}


def _entry_net(legs: list) -> float:
    n = 0.0
    for l in legs:
        n += _num(l.get("entry")) * _num(l.get("qty")) * (_num(l.get("mult")) or 100)
    return round(n, 2)


def _close_net(close_legs: list) -> float:
    n = 0.0
    for l in close_legs:
        n += _num(l.get("close_mid")) * _num(l.get("qty")) * (_num(l.get("mult")) or 100)
    return round(n, 2)


@router.get("/saved/export")
def export_saved(status: str = "all"):
    """Export saved trades as CSV. Filter by `status` (open|closed|all).
    Returns text/csv with a Content-Disposition attachment header so browsers
    download the file directly. Each row contains entry net, current/close
    net, P/L $, P/L %, plus a compact `legs` column like
    "+1xC145@1.10;-1xC150@0.50".
    """
    import io as _io
    import csv as _csv
    from fastapi.responses import Response as _Response

    trades = _load_saved()
    if status in ("open", "closed"):
        trades = [t for t in trades if (t.get("status") or "open") == status]
    try:
        from optionstrat_server import state as _state
        client = _state.client
    except Exception:
        client = None

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "id", "name", "ticker", "expiration", "strategy_type", "status",
        "saved_at", "closed_at", "saved_spot", "close_spot",
        "entry_net", "current_or_close_net", "pnl", "pnl_pct",
        "legs", "notes",
    ])
    for t in trades:
        is_closed = (t.get("status") or "open") == "closed"
        if is_closed:
            entry_net = _entry_net(t.get("legs", []))
            current = _close_net(t.get("close_legs") or [])
            pnl = t.get("final_pnl")
            pnl_pct = t.get("final_pnl_pct")
        else:
            summary = _compute_pnl_summary(client, t)
            entry_net = summary.get("entry_net")
            current = summary.get("current_net")
            pnl = summary.get("pnl")
            pnl_pct = summary.get("pnl_pct")
        legs_str = ";".join(
            f"{'+' if _num(l.get('qty')) > 0 else ''}{int(_num(l.get('qty')))}x"
            f"{l.get('type','')}{_num(l.get('strike')):g}@{_num(l.get('entry')):g}"
            for l in t.get("legs", [])
        )
        w.writerow([
            t.get("id"), t.get("name"), t.get("ticker"), t.get("expiration"),
            t.get("strategy_type"), (t.get("status") or "open"),
            t.get("saved_at"), t.get("closed_at"),
            t.get("saved_spot"), t.get("close_spot"),
            entry_net, current, pnl, pnl_pct, legs_str, (t.get("notes") or ""),
        ])
    return _Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="builder_saved_{status}.csv"'},
    )


@router.get("/saved/{trade_id}")
def get_saved(trade_id: str):
    for t in _load_saved():
        if t.get("id") == trade_id:
            return t
    raise HTTPException(404, "saved trade not found")


@router.delete("/saved/{trade_id}")
def delete_saved(trade_id: str):
    trades = _load_saved()
    new_trades = [t for t in trades if t.get("id") != trade_id]
    if len(new_trades) == len(trades):
        raise HTTPException(404, "saved trade not found")
    _write_saved(new_trades)
    return {"deleted": trade_id, "remaining": len(new_trades)}

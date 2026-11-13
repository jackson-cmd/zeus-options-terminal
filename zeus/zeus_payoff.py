"""Option math for agents: the page's strategy analyzer, in Python.

The Positions page, the strategy detail and Paper Trade compute max profit and
loss, breakevens, chance of profit and the Greeks in JavaScript
(options_analyzer/optionstrat_analyzer.py: bsmPrice, bsmGreeks, impliedVol,
analyzeStrategy, findBreakevens, calcChanceOfProfit, _pnlAtEarliestExp;
options_analyzer/paper_trade.py: _extremes). This module is a line-for-line
port, so an AI agent that reads ZEUS through MCP sees the same numbers as the
page. tests/test_payoff.py runs both versions on the same strategies.

A strategy is the dict /api/data returns:

    current_price, iv, risk_free_rate, expiration (front leg, YYYY-MM-DD),
    legs: [{type 'C' | 'P' | 'S', strike, qty (signed: + bought, - sold),
            entry, current, prev_close, mid, mult, exp, iv}]

A stock leg ('S') counts shares (multiplier 1) and is priced as a call struck
at 0, as Paper Trade does: worth the spot price at any date, delta 1.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

UNLIMITED = 1e9  # the page's convention for an unlimited max profit or loss (shown as ∞)
_NY = ZoneInfo("America/New_York")
_DAY_MS = 86_400_000.0
_YEAR_MS = 365 * _DAY_MS


# ------------------------------------------------------------------ pricing
def norm_cdf(x: float) -> float:
    """Abramowitz and Stegun 7.1.26, the same approximation as the page."""
    a1, a2, a3, a4, a5, p = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911
    sign = -1.0 if x < 0 else 1.0
    x = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    # JavaScript gives log(S/0) = +Infinity and log(0/K) = -Infinity; keep that.
    if K <= 0:
        return math.inf
    if S <= 0:
        return -math.inf
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def bsm_price(S: float, K: float, T: float, r: float, sigma: float, typ: str) -> float:
    if T <= 0:
        return max(0.0, S - K) if typ == "C" else max(0.0, K - S)
    if sigma <= 0.001:
        sigma = 0.001
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc = K * math.exp(-r * T)
    if typ == "C":
        return S * norm_cdf(d1) - disc * norm_cdf(d2)
    return disc * norm_cdf(-d2) - S * norm_cdf(-d1)


def bsm_greeks(S: float, K: float, T: float, r: float, sigma: float, typ: str) -> dict:
    """Per share. Theta per day, vega and rho per 1 point (1 %)."""
    if T <= 0:
        itm = S > K if typ == "C" else S < K
        return {"delta": (1.0 if typ == "C" else -1.0) if itm else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    if sigma <= 0.001:
        sigma = 0.001
    if S <= 0:
        return {"delta": 0.0 if typ == "C" else -1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    sqrt_t = math.sqrt(T)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * sqrt_t
    nd1 = norm_pdf(d1)
    disc = math.exp(-r * T)
    if typ == "C":
        delta = norm_cdf(d1)
        theta = (-S * nd1 * sigma / (2 * sqrt_t) - r * K * disc * norm_cdf(d2)) / 365
        rho = K * T * disc * norm_cdf(d2) / 100
    else:
        delta = norm_cdf(d1) - 1
        theta = (-S * nd1 * sigma / (2 * sqrt_t) + r * K * disc * norm_cdf(-d2)) / 365
        rho = -K * T * disc * norm_cdf(-d2) / 100
    return {"delta": delta, "gamma": nd1 / (S * sigma * sqrt_t), "theta": theta,
            "vega": S * nd1 * sqrt_t / 100, "rho": rho}


def implied_vol(S: float, K: float, T: float, r: float, market_price: float, typ: str,
                guess: float | None = None) -> float:
    """Newton-Raphson on the BSM price, 50 steps at most, kept within 0.1 %..500 %."""
    sigma = guess or 0.3
    for _ in range(50):
        price = bsm_price(S, K, T, r, sigma, typ)
        vega = S * norm_pdf(_d1(S, K, T, r, sigma)) * math.sqrt(T)
        if vega < 1e-10:
            break
        diff = price - market_price
        if abs(diff) < 0.001:
            break
        sigma -= diff / vega
        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5:
            sigma = 5
    return sigma


def ny_close_ms(date_str) -> float:
    """Epoch milliseconds of 16:00 New York time on a YYYY-MM-DD date (NaN when unreadable)."""
    try:
        y, m, d = (int(x) for x in str(date_str).split("-")[:3])
        return datetime(y, m, d, 16, 0, tzinfo=_NY).timestamp() * 1000.0
    except (TypeError, ValueError):
        return math.nan


def generate_price_range(S: float, pct: float, steps: int = 200) -> list:
    steps = steps or 200
    low, high = S * (1 - pct), S * (1 + pct)
    step = (high - low) / steps
    if not step > 0:
        return [low]
    out, p = [], low
    while p <= high + 0.01:
        out.append(p)
        p += step
    return out


# ------------------------------------------------------------------ strategy
def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _legs(strat: dict) -> list:
    """Legs with numbers as floats; stock legs become a call struck at 0 with multiplier 1."""
    out = []
    for leg in strat.get("legs") or []:
        stock = leg.get("type") == "S"
        out.append({
            "type": "C" if stock else leg.get("type"),
            "strike": 0.0 if stock else _num(leg.get("strike")),
            "qty": _num(leg.get("qty")),
            "entry": _num(leg.get("entry")),
            "current": _num(leg.get("current")),
            "prev_close": _num(leg.get("prev_close")),
            "mid": _num(leg.get("mid")),
            "mult": 1.0 if stock else (_num(leg.get("mult")) or 100.0),
            "exp": leg.get("exp") or strat.get("expiration"),
            "iv": _num(leg.get("iv")),
        })
    return out


def _payoff_at_front(strat: dict, legs: list, fallback_iv: float):
    """P/L at the earliest leg expiration as a function of price (the page's _pnlAtEarliestExp):
    legs expiring then are worth intrinsic value, later legs their BSM value."""
    r = _num(strat.get("risk_free_rate"))
    strat_iv = _num(strat.get("iv"))
    exps = [ny_close_ms(l["exp"]) for l in legs]
    known = [e for e in exps if not math.isnan(e)]
    earliest = min(known) if known else 0.0
    exps = [earliest if math.isnan(e) else e for e in exps]
    scale = fallback_iv / strat_iv if (strat_iv > 0 and fallback_iv > 0) else 1.0
    t_at = [max(0.0, (e - earliest) / _YEAR_MS) for e in exps]
    ivs = [l["iv"] * scale if l["iv"] > 0 else fallback_iv for l in legs]

    def pnl(price: float) -> float:
        total = 0.0
        for leg, t, iv in zip(legs, t_at, ivs):
            if t <= 0:
                val = max(0.0, price - leg["strike"]) if leg["type"] == "C" else max(0.0, leg["strike"] - price)
            else:
                val = bsm_price(price, leg["strike"], t, r, iv, leg["type"])
            total += (val - leg["entry"]) * leg["qty"] * leg["mult"]
        return total

    return pnl, earliest


def find_breakevens(strat: dict, fallback_iv: float | None = None, legs: list | None = None) -> list:
    legs = legs if legs is not None else _legs(strat)
    S = _num(strat.get("current_price"))
    iv_fb = fallback_iv if fallback_iv is not None else (_num(strat.get("iv")) or 0.30)
    pnl, _ = _payoff_at_front(strat, legs, iv_fb)
    prices = generate_price_range(S, 0.5, 1000)
    out, prev = [], None
    for price in prices:
        v = pnl(price)
        if prev is not None and ((prev < 0 <= v) or (prev >= 0 > v)):
            prev_price = price - (prices[1] - prices[0])
            ratio = abs(prev) / (abs(prev) + abs(v))
            out.append(prev_price + ratio * (price - prev_price))
        prev = v
    return out


def chance_of_profit(strat: dict, iv: float, legs: list | None = None, now_ms: float | None = None) -> float:
    """Probability (%) that the P/L at the front expiration is positive, lognormal at the given IV."""
    legs = legs if legs is not None else _legs(strat)
    S = _num(strat.get("current_price"))
    r = _num(strat.get("risk_free_rate"))
    iv_eff = iv if iv > 0.001 else 0.30
    pnl, earliest = _payoff_at_front(strat, legs, iv_eff)
    now = time.time() * 1000.0 if now_ms is None else now_ms
    t_eval = max(1e-6, (earliest - now) / _YEAR_MS)
    prices = generate_price_range(S, 0.5, 500)
    mu = math.log(S) + (r - 0.5 * iv_eff * iv_eff) * t_eval if S > 0 else 0.0
    sig = iv_eff * math.sqrt(t_eval)
    prob = 0.0
    for a, b in zip(prices, prices[1:]):
        mid = (a + b) / 2
        if mid <= 0 or pnl(mid) <= 0:
            continue
        density = math.exp(-0.5 * ((math.log(mid) - mu) / sig) ** 2) / (mid * sig * math.sqrt(2 * math.pi))
        prob += density * (b - a)
    if not math.isfinite(prob):
        return 0.0
    return min(100.0, max(0.0, prob * 100))


def analyze(strat: dict, iv_override: float | None = None, now_ms: float | None = None) -> dict:
    """The page's analyzeStrategy: net debit, P/L, Greeks, max profit and loss at the front
    expiration (UNLIMITED / -UNLIMITED when the payoff never stops rising or falling),
    breakevens and chance of profit."""
    legs = _legs(strat)
    S = _num(strat.get("current_price"))
    r = _num(strat.get("risk_free_rate"))
    strat_iv = _num(strat.get("iv"))
    iv = _num(iv_override) if iv_override is not None else strat_iv
    now = time.time() * 1000.0 if now_ms is None else now_ms
    exp_ms = ny_close_ms(strat.get("expiration"))
    dte = 0.0 if math.isnan(exp_ms) else max(0.0, (exp_ms - now) / _DAY_MS)
    T = dte / 365

    net_debit = sum(l["entry"] * l["qty"] * l["mult"] for l in legs)
    current_value = sum(l["current"] * l["qty"] * l["mult"] for l in legs)
    unrealized = current_value - net_debit
    unrealized_pct = unrealized / abs(net_debit) * 100 if net_debit != 0 else 0.0
    have_prev = [l for l in legs if l["prev_close"] > 0]
    today = (sum((l["current"] - l["prev_close"]) * l["qty"] * l["mult"] for l in have_prev)
             if have_prev else unrealized)

    greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    for l in legs:
        mid = l["mid"] or l["current"]
        leg_iv = implied_vol(S, l["strike"], T, r, mid, l["type"], iv) if (mid > 0 and T > 0) else iv
        g = bsm_greeks(S, l["strike"], T, r, leg_iv, l["type"])
        for k in greeks:
            greeks[k] += g[k] * l["qty"] * l["mult"]

    pnl, _ = _payoff_at_front(strat, legs, iv)
    ks = [l["strike"] for l in legs if l["strike"] > 0]
    lo = max(0.01, min([S] + ks) * 0.5)
    hi = max([S] + ks) * 1.5
    n = 400
    step = (hi - lo) / n
    grid = [lo + i * step for i in range(n + 1)] + [0.0] + ks
    vals = [pnl(p) for p in grid]
    max_profit, max_loss = max(vals), min(vals)
    net_calls = sum(l["qty"] * l["mult"] for l in legs if l["type"] == "C")
    if net_calls > 0:
        max_profit = UNLIMITED
    if net_calls < 0:
        max_loss = -UNLIMITED

    breakevens = find_breakevens(strat, iv, legs)
    cop = chance_of_profit(strat, iv, legs, now) if (breakevens and T > 0 and iv > 0) else 0.0
    return {"net_debit": net_debit, "max_profit": max_profit, "max_loss": max_loss,
            "breakevens": breakevens, "chance_of_profit": cop,
            "unrealized_pnl": unrealized, "unrealized_pct": unrealized_pct, "today_pnl": today,
            "greeks": greeks, "iv": iv, "dte": dte, "T": T}


def expiry_extremes(strat: dict) -> tuple[float, float]:
    """Exact max profit and max loss at expiration for a strategy whose legs share one
    expiration (Paper Trade's _extremes): the payoff is piecewise linear, so its extremes
    sit at a price of 0, at a strike, or at infinity."""
    legs = strat.get("legs") or []

    def mult(l):
        return 1.0 if l.get("type") == "S" else (_num(l.get("mult")) or 100.0)

    def pay(p):
        total = 0.0
        for l in legs:
            k = _num(l.get("strike"))
            if l.get("type") == "S":
                v = p
            elif l.get("type") == "C":
                v = max(0.0, p - k)
            else:
                v = max(0.0, k - p)
            total += (v - _num(l.get("entry"))) * _num(l.get("qty")) * mult(l)
        return total

    ks = sorted({0.0} | {_num(l.get("strike")) for l in legs if l.get("type") != "S"})
    vals = [pay(k) for k in ks]
    up = sum(_num(l.get("qty")) * mult(l) for l in legs if l.get("type") != "P")
    return (math.inf if up > 0 else max(vals)), (-math.inf if up < 0 else min(vals))


def pnl_at_front(strat: dict, prices: list, iv: float | None = None) -> list:
    """[(price, P/L at the front expiration)] for a few prices, for a quick payoff table."""
    legs = _legs(strat)
    pnl, _ = _payoff_at_front(strat, legs, iv if iv is not None else (_num(strat.get("iv")) or 0.30))
    return [(p, pnl(p)) for p in prices]


def single_expiration(strat: dict) -> bool:
    exps = {l.get("exp") or strat.get("expiration") for l in strat.get("legs") or [] if l.get("type") != "S"}
    return len(exps) <= 1


def is_unlimited(v: float) -> bool:
    return v is not None and abs(v) >= UNLIMITED

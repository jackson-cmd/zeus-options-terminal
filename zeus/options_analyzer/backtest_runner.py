"""Backtest runner for SSP1 screener signals.

Strategy:
    BULL signal → Bull Call Debit Spread
        - Expiration: ~4 weeks out (≥25d, ≤35d; nearest)
        - Long: ATM call
        - Short: +5% OTM call
    BEAR signal → Bear Put Debit Spread
        - Expiration: 2-12 days (nearest 1 week)
        - Long: ATM put
        - Short: −5% OTM put

Rules:
    - Skip if option chain has no price (bid or ask missing)
    - Skip if earnings event falls between open_date and expiration
    - Skip if ticker already has an open position (one per ticker max)
    - Commissions/fees ignored (per user spec)
    - Entry price = spread mid (long.mid − short.mid for debit spreads)

Storage (JSONL, append-only):
    data/backtest_positions.jsonl — one record per opened spread
    data/backtest_pnl.jsonl       — daily P/L snapshots per position

Auto-schedule: 2:50 PM local for opens; 3:00 PM for daily P/L update.
"""
from __future__ import annotations

import datetime
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_save_lock = threading.Lock()

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent

# Make sibling modules importable
if str(_PROJECT_ROOT / "trading_assistant") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "trading_assistant"))

DATA_DIR = _THIS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
POSITIONS_PATH = DATA_DIR / "backtest_positions.jsonl"
PNL_PATH = DATA_DIR / "backtest_pnl.jsonl"
# Daily option chain snapshots (Parquet, ~10× smaller than JSONL/CSV).
# One file per trading day so we can re-project P/L for ANY hypothetical
# strike/threshold combination later.
CHAIN_DIR = DATA_DIR / "chains"
CHAIN_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ─────────────────────────────────────────────────────────────────

BULL_DTE_MIN = 25      # ~4 weeks
BULL_DTE_MAX = 35
BEAR_DTE_MIN = 2
BEAR_DTE_MAX = 12
SPREAD_WIDTH_PCT = 0.05  # 5% ATM→OTM

# Auto-close rules (checked at 2:50 PM daily)
STOP_LOSS_PCT = 0.50     # close when unrealized_pnl ≤ -50% × debit_risk
TAKE_PROFIT_PCT = 1.20   # close when unrealized_pnl ≥ +120% × debit_risk

# Signal-count gates for opening (user-defined, stricter than direction alone)
# BULL: many bull signals are easy to get → require 3 to reduce noise.
# BEAR: only 3 possible BEAR signals (V1, V2, Loser recently) → 1 is enough.
MIN_BULL_SIGNALS = 3
MIN_BEAR_SIGNALS = 1

# Strike-distance filter for chain snapshots — only keep strikes within
# ±STRIKE_PCT of stock price. ATM ±5% is the trade window; 10% buffer.
STRIKE_PCT_FILTER = 0.10

# Position sizing: each ticker risks approximately TARGET_RISK_USD.
# contracts = max(1, round(target / |max_loss_per_contract|))
# So a $100/contract spread → 10 contracts ($1000 risk),
#    a $300/contract spread →  3 contracts ($900 risk),
#    a $1500/contract spread → 1 contract  ($1500 risk, can't go lower).
TARGET_RISK_USD = 1000.0

# Defensive floors against phantom positions from bad quotes.
# A near-zero net_debit (e.g. $0.13/contract on a 10%-wide ATM spread) is
# physically impossible — it's a stale/broken quote, not a real bargain. Left
# unguarded, compute_contracts blows it up into thousands of phantom contracts.
_MIN_DEBIT = 0.05        # per-contract debit < $5 → reject as a bad quote
# Hard cap on contract count (matches optionstrat_server summary filter).
_MAX_CONTRACTS = 2000


def compute_contracts(max_loss_per_contract: float) -> int:
    """Pick contract count to normalize ticker risk near TARGET_RISK_USD."""
    abs_loss = abs(max_loss_per_contract)
    if abs_loss <= 0:
        return 1
    return min(_MAX_CONTRACTS, max(1, round(TARGET_RISK_USD / abs_loss)))


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    open_date: str            # YYYY-MM-DD
    ticker: str
    direction: str            # "BULL" or "BEAR"
    signals: list             # [{signal, rate, samples}] or [{type:"BC", name}]
    stock_price_at_open: float
    option_type: str          # "C" or "P"
    expiration: str           # YYYY-MM-DD
    long_strike: float
    short_strike: float
    long_entry_mid: float
    short_entry_mid: float
    net_debit: float          # per-share debit (long_mid - short_mid)
    max_profit: float         # TOTAL across all contracts: (width - debit)×100×contracts
    max_loss: float           # TOTAL across all contracts: -debit×100×contracts
    contracts: int = 1        # # of spreads — sized so |max_loss| ≈ TARGET_RISK_USD
    status: str = "open"      # "open" | "expired_win" | "expired_loss" | "closed"
    close_date: str = ""
    close_value: float = 0.0
    realized_pnl: float = 0.0
    earnings_date: str = ""


@dataclass
class DailyPnL:
    date: str
    position_id: str
    ticker: str
    stock_price: float
    long_mid: float
    short_mid: float
    spread_value: float       # (long_mid - short_mid) × 100
    unrealized_pnl: float     # spread_value - (net_debit × 100)
    days_held: int


# ── Storage helpers ────────────────────────────────────────────────────────

def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _save_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_positions() -> list[dict]:
    return _load_jsonl(POSITIONS_PATH)


def load_pnl() -> list[dict]:
    return _load_jsonl(PNL_PATH)


def save_position(p: Position) -> None:
    _append_jsonl(POSITIONS_PATH, asdict(p))


def save_pnl(entry: DailyPnL) -> None:
    _append_jsonl(PNL_PATH, asdict(entry))


def update_position_status(pos_id: str, **updates) -> None:
    """Rewrite positions.jsonl with updated fields for the matching id."""
    if not POSITIONS_PATH.exists():
        return
    rows = load_positions()
    changed = False
    for r in rows:
        if r.get("id") == pos_id:
            r.update(updates)
            changed = True
    if not changed:
        return
    with POSITIONS_PATH.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


# ── Option chain + strike selection ────────────────────────────────────────

def pick_expiration(expirations: list[str], today: datetime.date,
                    min_dte: int, max_dte: int) -> Optional[str]:
    """Pick the expiration in range [min_dte, max_dte] closest to midpoint.
    expirations: list of ISO date strings (YYYY-MM-DD)."""
    target_mid = (min_dte + max_dte) / 2
    best = None
    best_score = 1e9
    for exp in expirations:
        try:
            d = datetime.date.fromisoformat(exp[:10])
        except (ValueError, TypeError):
            continue
        dte = (d - today).days
        if dte < min_dte or dte > max_dte:
            continue
        score = abs(dte - target_mid)
        if score < best_score:
            best_score = score
            best = exp[:10]
    return best


def pick_strikes(chain: list[dict], stock_price: float, option_type: str,
                 direction: str, width_pct: float = SPREAD_WIDTH_PCT
                 ) -> Optional[tuple[float, float]]:
    """Pick (long_strike, short_strike) for a debit spread.

    BULL call spread: long ATM, short ATM*(1+width). Both calls.
    BEAR put spread : long ATM, short ATM*(1-width). Both puts.

    Both long and short must exist in the chain. Returns None if cannot find.
    """
    # Filter to matching type, sorted by strike
    strikes = sorted(set(float(o["strike"]) for o in chain
                         if o.get("type") == option_type))
    if not strikes:
        return None
    # Long leg = closest to ATM
    long_strike = min(strikes, key=lambda s: abs(s - stock_price))
    # Short leg = closest to target_short
    if direction == "BULL":
        target_short = stock_price * (1 + width_pct)
        cand = [s for s in strikes if s > long_strike]
    else:  # BEAR
        target_short = stock_price * (1 - width_pct)
        cand = [s for s in strikes if s < long_strike]
    if not cand:
        return None
    short_strike = min(cand, key=lambda s: abs(s - target_short))
    if short_strike == long_strike:
        return None
    return long_strike, short_strike


def _mid_of(leg: dict) -> float:
    """Return mid = (bid+ask)/2, preferring TastyTrade 'mark' if present."""
    mark = leg.get("mark", 0) or 0
    if mark > 0:
        return float(mark)
    bid = float(leg.get("bid", 0) or 0)
    ask = float(leg.get("ask", 0) or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return 0.0


def get_leg_mid(chain: list[dict], strike: float, option_type: str) -> float:
    """Find the specific leg in chain by (strike, type) and return its mid."""
    for o in chain:
        if float(o.get("strike", -1)) == strike and o.get("type") == option_type:
            return _mid_of(o)
    return 0.0


# ── Earnings check ─────────────────────────────────────────────────────────

def earnings_in_window(earnings_date: str, open_date: datetime.date,
                       expiration: datetime.date) -> bool:
    """Return True if earnings falls between open and expiration (inclusive)."""
    if not earnings_date:
        return False
    try:
        e = datetime.date.fromisoformat(earnings_date[:10])
    except (ValueError, TypeError):
        return False
    return open_date <= e <= expiration


# ── Entry logic ────────────────────────────────────────────────────────────

def open_position_record(today: datetime.date, ticker: str, direction: str,
                          signals: list, stock_price: float, chain: list[dict],
                          expirations: list[str], earnings_date: str = "",
                          ) -> Optional[Position]:
    """Return a Position record if the spread can be priced AND earnings isn't
    in the window AND both legs have mids > 0. Else None with reason printed."""
    if direction == "BULL":
        opt_type = "C"
        min_dte, max_dte = BULL_DTE_MIN, BULL_DTE_MAX
    else:
        opt_type = "P"
        min_dte, max_dte = BEAR_DTE_MIN, BEAR_DTE_MAX

    exp = pick_expiration(expirations, today, min_dte, max_dte)
    if not exp:
        return None
    exp_date = datetime.date.fromisoformat(exp)

    if earnings_in_window(earnings_date, today, exp_date):
        return None

    # Filter chain to this expiration
    chain_exp = [o for o in chain if o.get("expiration", "")[:10] == exp]
    if not chain_exp:
        return None

    picks = pick_strikes(chain_exp, stock_price, opt_type, direction)
    if not picks:
        return None
    long_k, short_k = picks

    long_mid = get_leg_mid(chain_exp, long_k, opt_type)
    short_mid = get_leg_mid(chain_exp, short_k, opt_type)
    if long_mid <= 0 or short_mid <= 0:
        return None

    debit = round(long_mid - short_mid, 4)
    width = abs(short_k - long_k)
    # Reject broken quotes: a debit below the absolute floor (or below 2% of the
    # spread width) is a stale/bad price, not a real bargain. Passing it through
    # would let compute_contracts mint thousands of phantom contracts.
    if debit < _MIN_DEBIT or debit < width * 0.02:
        return None
    max_profit_pc = (width - debit) * 100   # per contract
    max_loss_pc = -debit * 100              # per contract (negative)
    contracts = compute_contracts(max_loss_pc)

    return Position(
        id=uuid.uuid4().hex[:12],
        open_date=today.isoformat(),
        ticker=ticker,
        direction=direction,
        signals=signals,
        stock_price_at_open=round(stock_price, 2),
        option_type=opt_type,
        expiration=exp,
        long_strike=long_k,
        short_strike=short_k,
        long_entry_mid=round(long_mid, 4),
        short_entry_mid=round(short_mid, 4),
        net_debit=debit,
        contracts=contracts,
        max_profit=round(max_profit_pc * contracts, 2),
        max_loss=round(max_loss_pc * contracts, 2),
        earnings_date=earnings_date or "",
    )


# ── Daily P/L computation ──────────────────────────────────────────────────

def compute_daily_pnl_for(position: dict, chain: list[dict], stock_price: float,
                           today: datetime.date) -> Optional[DailyPnL]:
    """Compute today's mid-to-mid P/L for a still-open spread.

    Both spread_value and unrealized_pnl are TOTAL across all contracts
    (× 100 × contracts) so they aggregate by simple summation across positions.
    """
    exp = position["expiration"][:10]
    exp_date = datetime.date.fromisoformat(exp)
    opt = position["option_type"]
    long_k = position["long_strike"]
    short_k = position["short_strike"]
    contracts = int(position.get("contracts", 1) or 1)
    mult = 100 * contracts

    if today > exp_date:
        # Settled at expiration — use intrinsic value
        if opt == "C":
            long_val = max(0.0, stock_price - long_k)
            short_val = max(0.0, stock_price - short_k)
        else:  # P
            long_val = max(0.0, long_k - stock_price)
            short_val = max(0.0, short_k - stock_price)
        spread_val = round((long_val - short_val) * mult, 2)
        unrealized = round(spread_val - position["net_debit"] * mult, 2)
        return DailyPnL(
            date=today.isoformat(),
            position_id=position["id"],
            ticker=position["ticker"],
            stock_price=round(stock_price, 2),
            long_mid=round(long_val, 4),
            short_mid=round(short_val, 4),
            spread_value=spread_val,
            unrealized_pnl=unrealized,
            days_held=(today - datetime.date.fromisoformat(position["open_date"])).days,
        )

    # Live mid-pricing from chain
    chain_exp = [o for o in chain if o.get("expiration", "")[:10] == exp]
    long_mid = get_leg_mid(chain_exp, long_k, opt)
    short_mid = get_leg_mid(chain_exp, short_k, opt)
    if long_mid <= 0 or short_mid <= 0:
        return None
    spread_val = round((long_mid - short_mid) * mult, 2)
    unrealized = round(spread_val - position["net_debit"] * mult, 2)
    return DailyPnL(
        date=today.isoformat(),
        position_id=position["id"],
        ticker=position["ticker"],
        stock_price=round(stock_price, 2),
        long_mid=round(long_mid, 4),
        short_mid=round(short_mid, 4),
        spread_value=spread_val,
        unrealized_pnl=unrealized,
        days_held=(today - datetime.date.fromisoformat(position["open_date"])).days,
    )


# ── TastyTrade integration ─────────────────────────────────────────────────

def fetch_chain_and_stock(ticker: str):
    """Return (expirations, chain, stock_price, earnings_date) using TastyTrade.

    chain: list of {strike, type, expiration, bid, ask, mark, mid}
    Returns (None, None, 0.0, "") on error.
    """
    try:
        from api_client import TastyClient
        c = TastyClient()
        # Chain
        resp = c.get(f"/option-chains/{ticker}/nested")
        exp_items = resp.get("data", {}).get("items", [])
        if not exp_items or not isinstance(exp_items, list):
            return None, None, 0.0, ""
        root = exp_items[0]
        expirations = sorted({e.get("expiration-date", "")[:10]
                               for e in root.get("expirations", [])
                               if e.get("expiration-date")})
        # Build flat chain. Items: strike entries under each expiration.
        all_symbols = []
        for exp in root.get("expirations", []):
            edate = exp.get("expiration-date", "")[:10]
            for strike in exp.get("strikes", []):
                try:
                    k = float(strike.get("strike-price", 0))
                except (ValueError, TypeError):
                    continue
                call_sym = strike.get("call")
                put_sym = strike.get("put")
                if call_sym:
                    all_symbols.append((call_sym, k, "C", edate))
                if put_sym:
                    all_symbols.append((put_sym, k, "P", edate))

        # Batch market-data fetch (mid/mark/last)
        # Note: TastyTrade truncates batches > ~50 symbols silently (returns 0s)
        chain = []
        BATCH = 50
        for i in range(0, len(all_symbols), BATCH):
            batch = all_symbols[i:i+BATCH]
            syms = ",".join(s[0] for s in batch)
            try:
                r = c.get("/market-data/by-type", params={"equity-option": syms})
                items = r.get("data", {}).get("items", [])
                md = {it.get("symbol", ""): it for it in items}
            except Exception:
                md = {}
            for sym, k, typ, edate in batch:
                m = md.get(sym, {})
                def _f(v):
                    try: return float(v or 0)
                    except (TypeError, ValueError): return 0.0
                chain.append({
                    "symbol": sym, "strike": k, "type": typ, "expiration": edate,
                    "bid": _f(m.get("bid")), "ask": _f(m.get("ask")),
                    "mid": _f(m.get("mid")), "mark": _f(m.get("mark")),
                    "last": _f(m.get("last")),
                })

        # Stock price + earnings
        stock_price = 0.0
        earnings_date = ""
        try:
            mq = c.get("/market-data/by-type", params={"equity": ticker})
            it = (mq.get("data", {}).get("items", []) or [{}])[0]
            stock_price = float(it.get("last") or it.get("mark") or it.get("bid") or 0)
        except Exception:
            pass
        try:
            mm = c.get("/market-metrics", params={"symbols": ticker})
            it = (mm.get("data", {}).get("items", []) or [{}])[0]
            earn = it.get("earnings", {}) or {}
            earnings_date = earn.get("expected-report-date", "") or ""
        except Exception:
            pass

        return expirations, chain, stock_price, earnings_date
    except Exception as e:
        print(f"  [backtest] fetch_chain_and_stock({ticker}) error: {e}")
        return None, None, 0.0, ""


# ── Scan orchestration ─────────────────────────────────────────────────────

def run_daily_scan(signals: list[dict], today: Optional[datetime.date] = None,
                    report_fn=None) -> list[Position]:
    """Given today's screener signals, open positions for new tickers.

    Rules:
    - One position per ticker per day (a ticker opened yesterday can open again
      today if it has a fresh signal — open-date uniqueness, not all-time)
    - Direction must be strictly "BULL" or "BEAR" — MIXED is skipped
    - Earnings in the expiration window → skip (handled in open_position_record)
    - Option chain must have tradeable mid prices → skip otherwise

    signals: list of {ticker, direction, signals, price, sources}.
    Returns list of newly opened Position records.
    """
    today = today or datetime.date.today()
    today_iso = today.isoformat()
    opened = []
    # Block: tickers already opened TODAY (allow tomorrow's fresh signals)
    opened_today = {p["ticker"] for p in load_positions() if p.get("open_date") == today_iso}

    # Pre-filter: dedupe, drop MIXED, drop already-opened-today, enforce
    # signal-count gates (BULL ≥3 signals, BEAR ≥1 — user-tunable above).
    def _count_directional_signals(row: dict, direction: str) -> int:
        n = 0
        for s in (row.get("signals") or []):
            d = (s.get("direction") or "").upper()
            if not d:
                # Fall back to inferring from name like "STACK (BULL)"
                name = s.get("name", "")
                if "(BULL)" in name: d = "BULL"
                elif "(BEAR)" in name: d = "BEAR"
            if d == direction:
                n += 1
        for src in (row.get("sources") or []):
            if (src.get("direction") or "").upper() == direction:
                n += 1
        return n

    eligible = []
    skipped_low_signals = 0
    for row in signals:
        ticker = row.get("ticker", "").upper()
        direction = (row.get("direction") or "").upper()
        if not ticker or ticker in opened_today:
            continue
        if direction not in ("BULL", "BEAR"):
            continue
        # Signal-count gate
        sig_count = _count_directional_signals(row, direction)
        threshold = MIN_BULL_SIGNALS if direction == "BULL" else MIN_BEAR_SIGNALS
        if sig_count < threshold:
            skipped_low_signals += 1
            continue
        eligible.append((ticker, direction, row))
    # Dedupe preserving first-seen
    seen_t = set()
    eligible = [(t, d, r) for t, d, r in eligible if not (t in seen_t or seen_t.add(t))]
    _report(report_fn, f"Eligible after filter: {len(eligible)} (MIXED + dupes + "
                        f"<{MIN_BULL_SIGNALS}-bull/<{MIN_BEAR_SIGNALS}-bear skipped: "
                        f"{skipped_low_signals} below signal threshold)")

    # Parallel chain fetching (TastyTrade tolerates ~8 concurrent requests)
    def _process(item):
        ticker, direction, row = item
        expirations, chain, stock_price, earnings = fetch_chain_and_stock(ticker)
        if not chain or stock_price <= 0:
            return (ticker, None, "no chain/price")
        price = float(row.get("price") or stock_price)
        sig_compact = [
            {"type": "WR", "name": s.get("name", "").split(" ")[0],
             "rate": s.get("best_rate"), "samples": s.get("samples")}
            for s in (row.get("signals") or []) if s.get("name")
        ]
        for src in (row.get("sources") or []):
            sig_compact.append({"type": src.get("type", ""), "name": src.get("name", "")})
        pos = open_position_record(today, ticker, direction, sig_compact,
                                    price, chain, expirations, earnings)
        if not pos:
            reason = "earnings-in-window" if earnings_in_window(earnings, today,
                datetime.date.fromisoformat(pick_expiration(expirations, today,
                    BULL_DTE_MIN if direction=="BULL" else BEAR_DTE_MIN,
                    BULL_DTE_MAX if direction=="BULL" else BEAR_DTE_MAX) or today.isoformat())
            ) else "price/strike/no-exp"
            return (ticker, None, reason)
        return (ticker, pos, None)

    done = 0
    total = len(eligible)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_process, e): e for e in eligible}
        for f in as_completed(futs):
            done += 1
            try:
                ticker, pos, reason = f.result()
            except Exception as e:
                ticker, pos, reason = futs[f][0], None, f"error: {e}"
            if pos:
                save_position(pos)
                opened.append(pos)
                _report(report_fn, f"  [{done}/{total}] ✓ {ticker} {pos.direction} "
                                    f"{pos.long_strike}/{pos.short_strike}{pos.option_type} "
                                    f"exp {pos.expiration} ${pos.net_debit:.2f}")
            else:
                _report(report_fn, f"  [{done}/{total}] skip {ticker}: {reason}")

    return opened


def chain_snapshot_path(date: datetime.date) -> Path:
    """Per-day Parquet path for chain snapshots."""
    return CHAIN_DIR / f"chain_{date.isoformat()}.parquet"


# Maximum DTE worth snapshotting — covers BULL 25-35d + BEAR 2-12d with
# a comfortable buffer for replay. Anything past 60d (LEAPs, quarterlies)
# is dropped to keep parquet size sane (~50% reduction in tests).
SNAPSHOT_MAX_DTE = 60


def save_chain_snapshot(date: datetime.date, ticker: str, chain: list[dict],
                         stock_price: float = 0.0) -> int:
    """Append a ticker's chain to today's parquet snapshot.

    Schema: ticker, expiration, strike, type, bid, ask, mid, mark, last
    Concurrent-safe via _save_lock. Returns rows written.
    Existing rows for the same ticker on this date are overwritten.

    Filters applied at write time (storage savings ~80% combined):
    - DTE > SNAPSHOT_MAX_DTE (60d) — LEAPs/quarterlies, never used
    - DTE < 0 — already expired
    - Strike outside stock_price ± STRIKE_PCT_FILTER (10%) — ATM only ±5%
      is traded; 10% buffer covers all replay needs
      (Skipped if stock_price <= 0 — keep all strikes if price unknown)
    """
    if not chain:
        return 0
    import pandas as pd
    rows = []
    strike_lo = stock_price * (1 - STRIKE_PCT_FILTER) if stock_price > 0 else None
    strike_hi = stock_price * (1 + STRIKE_PCT_FILTER) if stock_price > 0 else None
    for o in chain:
        exp_str = (o.get("expiration", "") or "")[:10]
        # Skip far-dated (LEAPs / 6-12mo) — never used by 4w/1w strategies.
        if exp_str:
            try:
                exp_d = datetime.date.fromisoformat(exp_str)
                if (exp_d - date).days > SNAPSHOT_MAX_DTE:
                    continue
                if (exp_d - date).days < 0:
                    continue  # already expired — useless
            except ValueError:
                pass
        strike = float(o.get("strike", 0) or 0)
        # Skip strikes too far from ATM
        if strike_lo is not None and (strike < strike_lo or strike > strike_hi):
            continue
        rows.append({
            "ticker": ticker,
            "expiration": exp_str,
            "strike": strike,
            "type": o.get("type", ""),
            "bid": float(o.get("bid", 0) or 0),
            "ask": float(o.get("ask", 0) or 0),
            "mid": float(o.get("mid", 0) or 0),
            "mark": float(o.get("mark", 0) or 0),
            "last": float(o.get("last", 0) or 0),
        })
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    path = chain_snapshot_path(date)
    with _save_lock:
        if path.exists():
            try:
                existing = pd.read_parquet(path)
                # Replace this ticker's rows
                existing = existing[existing["ticker"] != ticker]
                df = pd.concat([existing, df], ignore_index=True)
            except Exception:
                pass
        df.to_parquet(path, compression="snappy", index=False)
    return len(rows)


def load_chain_snapshot(date: datetime.date, ticker: str | None = None):
    """Load a day's chain snapshot. Optional ticker filter. Returns DataFrame
    (may be empty)."""
    import pandas as pd
    path = chain_snapshot_path(date)
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "expiration", "strike", "type",
                                       "bid", "ask", "mid", "mark", "last"])
    df = pd.read_parquet(path)
    if ticker:
        df = df[df["ticker"] == ticker.upper()]
    return df


def project_position_mode(position: dict, history: list[dict], mode: str,
                           sl_pct: float | None = None,
                           tp_pct: float | None = None) -> dict:
    """Apply a P/L mode to a position's full daily P/L history. Returns
    {effective_status, effective_close_date, effective_pnl} based on which
    auto-close rule (if any) would have triggered first under this mode.

    Modes:
      a       — stop-loss at -50% AND take-profit at +120% (defensive)
      b       — take-profit at +120% only (let losers run to expiration)
      c       — no rules (hold to expiration always)
      custom  — user-supplied sl_pct (positive number, e.g. 50 = stop at -50%
                 of debit) and tp_pct (e.g. 80 = exit at +80% of debit).
                 Pass None for either to disable that side.

    All modes still respect the actual broker-side expiration_win/loss
    (intrinsic settlement) recorded once today > expiration.
    """
    contracts = int(position.get("contracts", 1) or 1)
    debit_risk = float(position.get("net_debit", 0)) * 100 * contracts
    if mode == "custom":
        sl_threshold = -(sl_pct / 100.0) * debit_risk if sl_pct is not None else None
        tp_threshold = (tp_pct / 100.0) * debit_risk if tp_pct is not None else None
    else:
        sl_threshold = -0.5 * debit_risk if mode == "a" else None
        tp_threshold = 1.2 * debit_risk if mode in ("a", "b") else None
    sorted_hist = sorted(history or [], key=lambda e: e.get("date", ""))

    for e in sorted_hist:
        pnl = e.get("unrealized_pnl", 0)
        if sl_threshold is not None and pnl <= sl_threshold:
            return {
                "effective_status": "stop_loss_50pct",
                "effective_close_date": e.get("date", ""),
                "effective_pnl": pnl,
            }
        if tp_threshold is not None and pnl >= tp_threshold:
            return {
                "effective_status": "take_profit_120pct",
                "effective_close_date": e.get("date", ""),
                "effective_pnl": pnl,
            }

    # Did not trigger either rule. Use real expiration outcome if past expiry.
    actual_status = position.get("status", "open")
    if actual_status in ("expired_win", "expired_loss"):
        return {
            "effective_status": actual_status,
            "effective_close_date": position.get("close_date", ""),
            "effective_pnl": position.get("realized_pnl", 0),
        }
    # Still open under this mode — show latest recorded P/L
    if sorted_hist:
        last = sorted_hist[-1]
        return {
            "effective_status": "open",
            "effective_close_date": "",
            "effective_pnl": last.get("unrealized_pnl", 0),
        }
    return {"effective_status": "open", "effective_close_date": "", "effective_pnl": 0.0}


def check_auto_close(position: dict, pnl: DailyPnL) -> tuple[bool, str]:
    """Return (should_close, reason) based on 50% stop-loss / 120% take-profit.

    debit_risk = net_debit × 100  (positive $ at risk)
    Stop-loss:   unrealized_pnl ≤ -0.5 × debit_risk → close with reason "stop_loss"
    Take-profit: unrealized_pnl ≥ +1.2 × debit_risk → close with reason "take_profit"
    """
    contracts = int(position.get("contracts", 1) or 1)
    debit_risk = float(position.get("net_debit", 0)) * 100 * contracts
    if debit_risk <= 0:
        return False, ""
    pnl_val = pnl.unrealized_pnl
    if pnl_val <= -STOP_LOSS_PCT * debit_risk:
        return True, "stop_loss_50pct"
    if pnl_val >= TAKE_PROFIT_PCT * debit_risk:
        return True, "take_profit_120pct"
    return False, ""


def resize_past_positions(report_fn=None) -> int:
    """One-time migration: every legacy contracts=1 position (BOTH open and
    closed) is scaled to TARGET_RISK_USD. Scales: max_profit, max_loss,
    realized_pnl, close_value AND every matching P/L history entry's
    spread_value + unrealized_pnl. Without this, closed losers and winners
    have wildly different magnitudes from the new positions.

    Returns number of positions resized.
    """
    rows = load_positions()
    pnl_rows = load_pnl()
    pnl_by_pos: dict = {}
    for e in pnl_rows:
        pnl_by_pos.setdefault(e.get("position_id"), []).append(e)

    resized = 0
    for p in rows:
        existing = int(p.get("contracts", 1) or 1)
        if existing > 1:
            continue  # already sized
        max_loss_pc = float(p.get("max_loss", 0))
        max_profit_pc = float(p.get("max_profit", 0))
        contracts = compute_contracts(max_loss_pc)
        if contracts == 1:
            # Scaling factor of 1 — just write the field for clarity
            if "contracts" not in p:
                p["contracts"] = 1
                resized += 1
            continue
        p["contracts"] = contracts
        p["max_loss"] = round(max_loss_pc * contracts, 2)
        p["max_profit"] = round(max_profit_pc * contracts, 2)
        # Scale already-realized fields for closed positions
        if "realized_pnl" in p and p.get("realized_pnl"):
            p["realized_pnl"] = round(float(p["realized_pnl"]) * contracts, 2)
        if "close_value" in p and p.get("close_value"):
            p["close_value"] = round(float(p["close_value"]) * contracts, 2)
        # Scale matching P/L history rows
        for e in pnl_by_pos.get(p["id"], []):
            e["spread_value"] = round(float(e.get("spread_value", 0)) * contracts, 2)
            e["unrealized_pnl"] = round(float(e.get("unrealized_pnl", 0)) * contracts, 2)
        resized += 1
    if resized:
        with _save_lock:
            with POSITIONS_PATH.open("w", encoding="utf-8") as f:
                for p in rows:
                    f.write(json.dumps(p, default=str) + "\n")
            with PNL_PATH.open("w", encoding="utf-8") as f:
                for e in pnl_rows:
                    f.write(json.dumps(e, default=str) + "\n")
    _report(report_fn, f"[resize] resized {resized} positions (open+closed) to ~${TARGET_RISK_USD:.0f} risk; P/L history scaled too")
    return resized


def revalidate_past_positions(report_fn=None) -> tuple[int, int]:
    """Re-check every open position against current MIN_BULL/MIN_BEAR_SIGNALS
    rules. Drops positions whose stored signals no longer meet the threshold.

    Also drops the matching P/L history entries to keep storage clean.
    Returns (kept, removed).
    """
    positions = load_positions()
    keep, drop = [], []
    for p in positions:
        # Only re-validate OPEN positions; closed/expired stay in history.
        if p.get("status") != "open":
            keep.append(p)
            continue
        direction = (p.get("direction") or "").upper()
        sigs = p.get("signals") or []
        # Stored signals: [{type, name, rate?, samples?, direction?}]
        n_dir = 0
        for s in sigs:
            d = (s.get("direction") or "").upper()
            if not d:
                name = s.get("name", "")
                if "(BULL)" in name or s.get("type") == "WR" and direction == "BULL":
                    d = "BULL"
                elif "(BEAR)" in name or s.get("type") == "WR" and direction == "BEAR":
                    d = "BEAR"
            if d == direction:
                n_dir += 1
        threshold = MIN_BULL_SIGNALS if direction == "BULL" else MIN_BEAR_SIGNALS
        if n_dir >= threshold:
            keep.append(p)
        else:
            drop.append(p)

    if not drop:
        _report(report_fn, f"[revalidate] all {len(keep)} positions still qualify")
        return (len(keep), 0)

    drop_ids = {p["id"] for p in drop}
    # Rewrite positions file with only the kept ones
    with _save_lock:
        with POSITIONS_PATH.open("w", encoding="utf-8") as f:
            for p in keep:
                f.write(json.dumps(p, default=str) + "\n")
        # Drop P/L entries belonging to removed positions
        if PNL_PATH.exists():
            pnl = load_pnl()
            kept_pnl = [e for e in pnl if e.get("position_id") not in drop_ids]
            with PNL_PATH.open("w", encoding="utf-8") as f:
                for e in kept_pnl:
                    f.write(json.dumps(e, default=str) + "\n")
    _report(report_fn, f"[revalidate] kept {len(keep)}, dropped {len(drop)}")
    return (len(keep), len(drop))


def _load_universe_tickers() -> list[str]:
    """Load the curated 264-ticker universe via screener_runner.

    The universe is the source of truth — we snapshot every one daily
    regardless of whether we have an open position on it. This lets us
    run hypothetical backtests on any historical day for any ticker.
    """
    try:
        from screener_runner import _load_screener_tickers
        return _load_screener_tickers()
    except Exception as e:
        print(f"[backtest] universe loader fallback: {e}")
        return []


def snapshot_universe(today: Optional[datetime.date] = None,
                      tickers: Optional[list[str]] = None,
                      report_fn=None) -> tuple[int, int]:
    """Snapshot ALL universe tickers' option chains to today's parquet.

    Also computes P/L for any open positions whose underlying is in the
    snapshot — this is the single daily 15:45 task that replaces both the
    old "P/L update" and "chain backup" steps.

    Returns (chain_count, pnl_count).
    """
    today = today or datetime.date.today()
    if tickers is None:
        tickers = _load_universe_tickers()
    if not tickers:
        _report(report_fn, "[snapshot] empty universe — nothing to do")
        return (0, 0)

    # Open-position lookup (compute P/L inline to avoid double-fetch)
    open_positions: dict = {}
    for p in load_positions():
        if p.get("status") == "open":
            open_positions.setdefault(p["ticker"], []).append(p)

    chain_count = 0
    pnl_count = 0
    done = 0
    total = len(tickers)
    chain_lock = threading.Lock()

    def _process(t: str):
        nonlocal chain_count, pnl_count
        try:
            _, chain, stock_price, _ = fetch_chain_and_stock(t)
        except Exception as e:
            return ("fetch_error", str(e))
        if not chain or stock_price <= 0:
            return ("no_data", "")
        # Snapshot (pass stock_price so we filter to ±10% strikes only)
        try:
            n = save_chain_snapshot(today, t, chain, stock_price)
        except Exception as e:
            return ("snapshot_error", str(e))
        with chain_lock:
            chain_count += 1

        # Compute P/L for any open positions on this ticker
        local_pnl = 0
        for p in open_positions.get(t, []):
            try:
                e = compute_daily_pnl_for(p, chain, stock_price, today)
            except Exception as ex_pnl:
                continue
            if not e:
                continue
            save_pnl(e)
            local_pnl += 1
            if today > datetime.date.fromisoformat(p["expiration"][:10]):
                status = "expired_win" if e.unrealized_pnl > 0 else "expired_loss"
                update_position_status(p["id"], status=status,
                                        close_date=today.isoformat(),
                                        close_value=e.spread_value,
                                        realized_pnl=e.unrealized_pnl)
        with chain_lock:
            pnl_count += local_pnl
        return ("ok", f"chain={n}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_process, t): t for t in tickers}
        for f in as_completed(futs):
            done += 1
            t = futs[f]
            status, detail = f.result() if not f.exception() else ("error", str(f.exception()))
            if done % 20 == 0 or done == total:
                _report(report_fn, f"  [{done}/{total}] {t}: {status}")

    _report(report_fn, f"[snapshot] done: {chain_count} chains, {pnl_count} P/L entries")
    return (chain_count, pnl_count)


def run_daily_pnl(today: Optional[datetime.date] = None, report_fn=None
                   ) -> list[DailyPnL]:
    """For all still-open positions, fetch current chain and record P/L.
    Positions past expiration are marked settled. Parallelized."""
    today = today or datetime.date.today()
    positions = [p for p in load_positions() if p.get("status") == "open"]
    entries = []
    done = 0
    total = len(positions)

    def _process(p):
        _, chain, stock_price, _ = fetch_chain_and_stock(p["ticker"])
        if stock_price <= 0:
            return (p, None)
        # Snapshot the full chain to today's parquet for arbitrary
        # threshold replay (custom SL/TP on any past day).
        if chain:
            try:
                save_chain_snapshot(today, p["ticker"], chain)
            except Exception as snap_err:
                print(f"  [snapshot] {p['ticker']}: {snap_err}")
        e = compute_daily_pnl_for(p, chain or [], stock_price, today)
        return (p, e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_process, p): p for p in positions}
        for f in as_completed(futs):
            done += 1
            try:
                p, e = f.result()
            except Exception as ex_err:
                _report(report_fn, f"  [{done}/{total}] error: {ex_err}")
                continue
            if not e:
                _report(report_fn, f"  [{done}/{total}] skip {p['ticker']} no price")
                continue
            save_pnl(e)
            entries.append(e)
            _report(report_fn, f"  [{done}/{total}] {p['ticker']} P/L ${e.unrealized_pnl:+.2f}")
            # Mark settled at expiration (real broker close). Auto-close rules
            # are now applied in display layer per mode (see project_position_mode)
            # so the same P/L history serves all 3 user-selectable modes.
            if today > datetime.date.fromisoformat(p["expiration"][:10]):
                status = "expired_win" if e.unrealized_pnl > 0 else "expired_loss"
                update_position_status(p["id"], status=status,
                                        close_date=today.isoformat(),
                                        close_value=e.spread_value,
                                        realized_pnl=e.unrealized_pnl)
    return entries


def _report(fn, msg):
    if fn:
        try: fn(msg)
        except Exception: pass


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["open", "pnl", "seed"], default="open",
                    help="open = run daily scan; pnl = update P/L; seed = load today's screener signals and open")
    args = ap.parse_args()

    today = datetime.date.today()
    print(f"[backtest] Mode={args.mode} date={today}")

    if args.mode in ("open", "seed"):
        # Load signals from the current screener state (ssp1 + barchart merged)
        ssp1_path = _PROJECT_ROOT / "winrate_scanner" / "data" / "ssp1_latest.json"
        if not ssp1_path.exists():
            print("  [backtest] No ssp1_latest.json. Run screener first.")
            sys.exit(1)
        ssp1 = json.loads(ssp1_path.read_text())
        # Build signal rows mirroring server's _ssp1_to_ui_format (without BC for CLI)
        signals = []
        for t in ssp1.get("tickers", []):
            dirs = {s["direction"] for s in t["signals"]}
            direction = "BULL" if dirs == {"BULL"} else "BEAR" if dirs == {"BEAR"} else "MIXED"
            signals.append({
                "ticker": t["ticker"],
                "direction": direction,
                "signals": [{"name": f"{s['signal']} ({s['direction']})",
                              "best_rate": s["rate"], "samples": s["samples"]}
                             for s in t["signals"]],
                "sources": [],
                "price": t.get("last_close", 0),
            })
        opened = run_daily_scan(signals, today, report_fn=print)
        print(f"\n{len(opened)} positions opened today.")

    elif args.mode == "pnl":
        entries = run_daily_pnl(today, report_fn=print)
        print(f"\n{len(entries)} P/L entries updated.")

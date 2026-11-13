"""
Signal Scanner - Python translation of the PineScript 'Merged Master Alert'.

Reads daily OHLCV data via yfinance, computes all signal categories:
  LONG  - 3D/4D/5D streak, RSI, BB, EMA crossovers (with EMA200 slope up)
  BEAR  - V1/V2/V3 bearish stack, BB upper, RSI overbought, L3/L4/L5 down streaks
  MR    - Mean Reversion bull/bear (BB bounce)
  PAT   - Patterns: Bullish/Bearish Engulfing, Doji, Dragonfly Doji
  GAP   - Gap Up / Gap Down unfilled

Returns a list of signal dicts for each ticker with today's active signals.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from config import SCANNER_DEFAULTS as CFG
from yahoo_api import fetch_daily_data


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------

def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def stdev(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).std()


def rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def crossover(series: pd.Series, level: pd.Series) -> pd.Series:
    """True where series crosses above level."""
    return (series > level) & (series.shift(1) <= level.shift(1))


def crossunder(series: pd.Series, level: pd.Series) -> pd.Series:
    return (series < level) & (series.shift(1) >= level.shift(1))


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class TickerSignals:
    ticker: str
    price: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    date: str = ""
    long_signals: list[str] = field(default_factory=list)
    bear_signals: list[str] = field(default_factory=list)
    mr_signals: list[str] = field(default_factory=list)
    pattern_signals: list[str] = field(default_factory=list)
    gap_signals: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.long_signals or self.bear_signals or self.mr_signals
                     or self.pattern_signals or self.gap_signals)

    @property
    def direction(self) -> str:
        """Dominant direction: BULL, BEAR, or MIXED."""
        bull = len(self.long_signals) + len(self.mr_signals) * (1 if "Bull" in str(self.mr_signals) else 0) + \
               sum(1 for p in self.pattern_signals if "Bull" in p or "Up" in p) + \
               sum(1 for g in self.gap_signals if "Up" in g)
        bear = len(self.bear_signals) + len(self.mr_signals) * (1 if "Bear" in str(self.mr_signals) else 0) + \
               sum(1 for p in self.pattern_signals if "Bear" in p or "Down" in p) + \
               sum(1 for g in self.gap_signals if "Down" in g)
        if bull > bear:
            return "BULL"
        elif bear > bull:
            return "BEAR"
        elif bull > 0 or bear > 0:
            return "MIXED"
        return "NONE"

    @property
    def all_signals(self) -> list[str]:
        return self.long_signals + self.bear_signals + self.mr_signals + \
               self.pattern_signals + self.gap_signals

    @property
    def signal_strength(self) -> int:
        """Number of concurrent signals (higher = stronger conviction)."""
        return len(self.all_signals)


# ---------------------------------------------------------------------------
# LONG signals
# ---------------------------------------------------------------------------

def scan_long(df: pd.DataFrame) -> list[str]:
    """SSP1 BULL signals fired on the last bar."""
    if len(df) < 201:
        return []

    signals = []
    c = df["close"]

    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema100 = ema(c, 100)
    ema200 = ema(c, 200)
    rsi14 = rsi(c, 14)

    bull_stack = (ema20.iloc[-1] > ema50.iloc[-1]
                  and ema50.iloc[-1] > ema100.iloc[-1]
                  and ema100.iloc[-1] > ema200.iloc[-1])

    if bull_stack and rsi14.iloc[-1] < 70:
        signals.append("STACK (EMA20>50>100>200 + RSI<70)")
    if crossover(c, ema20).iloc[-1]:
        signals.append("EMA20 crossover")
    if crossover(c, ema50).iloc[-1]:
        signals.append("EMA50 crossover")
    if crossover(c, ema100).iloc[-1]:
        signals.append("EMA100 crossover")
    if crossover(c, ema200).iloc[-1]:
        signals.append("EMA200 crossover")

    return signals


# ---------------------------------------------------------------------------
# BEAR signals
# ---------------------------------------------------------------------------

def scan_bear(df: pd.DataFrame) -> list[str]:
    """SSP1 BEAR signals (V1, V2 only) fired on the last bar."""
    if len(df) < 101:
        return []

    signals = []
    c = df["close"]
    o = df["open"]
    h = df["high"]

    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema100 = ema(c, 100)

    bear_stack = (ema20 < ema50) & (ema50 < ema100)
    cross_under_20 = crossunder(c, ema20)

    # V1: crossunder EMA20 + bear stack
    if cross_under_20.iloc[-1] and bear_stack.iloc[-1]:
        signals.append("V1 (crossunder EMA20 + bear stack)")

    # V2: bear stack + touched EMA20 but closed below + red candle
    if (bear_stack.iloc[-1] and h.iloc[-1] >= ema20.iloc[-1]
            and c.iloc[-1] < ema20.iloc[-1] and c.iloc[-1] < o.iloc[-1]):
        signals.append("V2 (EMA20 rejection)")

    return signals


def _ssp1_bear_dead_code_guard(df):
    """Unused — preserved structure below got pruned to match SSP1 (V1/V2 only)."""
    signals: list[str] = []
    last_streak = 0

    if last_streak == 3:
        signals.append("L3 (3-day down streak)")
    if last_streak == 4:
        signals.append("L4 (4-day down streak)")
    if last_streak >= 5:
        signals.append("L5 (5-day down streak)")

    return signals


# ---------------------------------------------------------------------------
# MEAN REVERSION signals
# ---------------------------------------------------------------------------

def scan_mean_reversion(df: pd.DataFrame) -> list[str]:
    if len(df) < CFG["mr_bb_len"] + 5:
        return []

    signals = []
    c = df["close"]
    o = df["open"]

    bb_basis = sma(c, CFG["mr_bb_len"])
    bb_dev = stdev(c, CFG["mr_bb_len"]) * CFG["mr_bb_mult"]
    bb_upper = bb_basis + bb_dev
    bb_lower = bb_basis - bb_dev

    # Bull MR: prev close below lower BB, today close above lower BB + green candle
    if (c.iloc[-2] < bb_lower.iloc[-2]
            and c.iloc[-1] > bb_lower.iloc[-1]
            and c.iloc[-1] > o.iloc[-1]):
        signals.append("MR Bull (BB lower bounce)")

    # Bear MR: prev close above upper BB, today close below upper BB + red candle
    if (c.iloc[-2] > bb_upper.iloc[-2]
            and c.iloc[-1] < bb_upper.iloc[-1]
            and c.iloc[-1] < o.iloc[-1]):
        signals.append("MR Bear (BB upper rejection)")

    return signals


# ---------------------------------------------------------------------------
# PATTERN signals
# ---------------------------------------------------------------------------

def scan_patterns(df: pd.DataFrame) -> list[str]:
    if len(df) < 52:
        return []

    signals = []
    c = df["close"]
    o = df["open"]
    h = df["high"]
    lo = df["low"]

    sma50 = sma(c, 50)
    sma200 = sma(c, 200) if len(df) >= 200 else pd.Series(np.nan, index=df.index)

    up_trend = (c.iloc[-1] > sma50.iloc[-1]) and (not np.isnan(sma200.iloc[-1]) and sma50.iloc[-1] > sma200.iloc[-1])
    down_trend = (c.iloc[-1] < sma50.iloc[-1]) and (not np.isnan(sma200.iloc[-1]) and sma50.iloc[-1] < sma200.iloc[-1])

    # Helpers for candle body
    body = abs(c - o)
    body_hi = pd.concat([c, o], axis=1).max(axis=1)
    body_lo = pd.concat([c, o], axis=1).min(axis=1)
    candle_range = h - lo
    up_shadow = h - body_hi
    dn_shadow = body_lo - lo

    white = c > o
    black = c < o

    # Bullish Engulfing
    if (down_trend and black.iloc[-2] and white.iloc[-1]
            and o.iloc[-1] <= c.iloc[-2] and c.iloc[-1] >= o.iloc[-2]):
        signals.append("Bullish Engulfing")

    # Bearish Engulfing
    if (up_trend and white.iloc[-2] and black.iloc[-1]
            and o.iloc[-1] >= c.iloc[-2] and c.iloc[-1] <= o.iloc[-2]):
        signals.append("Bearish Engulfing")

    # Doji
    r = candle_range.iloc[-1]
    b = body.iloc[-1]
    if r > 0 and b <= r * 0.05:
        us = up_shadow.iloc[-1]
        ds = dn_shadow.iloc[-1]
        # Dragonfly: upper shadow tiny
        if us <= b:
            signals.append("Dragonfly Doji (bullish)")
        # Gravestone: lower shadow tiny
        elif ds <= b:
            signals.append("Gravestone Doji (bearish)")
        else:
            # Regular doji - direction from trend
            if down_trend:
                signals.append("Doji (potential reversal up)")
            elif up_trend:
                signals.append("Doji (potential reversal down)")

    return signals


# ---------------------------------------------------------------------------
# GAP signals
# ---------------------------------------------------------------------------

def scan_gaps(df: pd.DataFrame, no_fill_bars: int = 5) -> list[str]:
    if len(df) < no_fill_bars + 5:
        return []

    signals = []
    h = df["high"]
    lo = df["low"]

    min_gap_pct = CFG["gap_min_pct"]

    # Check if there was a gap 'no_fill_bars' ago that hasn't been filled
    gap_idx = -(no_fill_bars + 1)  # the gap bar
    pre_gap_idx = gap_idx - 1       # bar before the gap

    if abs(gap_idx) >= len(df) or abs(pre_gap_idx) >= len(df):
        return []

    pre_high = h.iloc[pre_gap_idx]
    pre_low = lo.iloc[pre_gap_idx]
    gap_low = lo.iloc[gap_idx]
    gap_high = h.iloc[gap_idx]

    # Gap Up
    if gap_low > pre_high and pre_high > 0:
        gap_pct = (gap_low / pre_high - 1.0) * 100
        if gap_pct >= min_gap_pct:
            # Check no fill in subsequent bars
            filled = False
            for k in range(1, no_fill_bars + 1):
                check_idx = gap_idx + k
                if check_idx < 0 and lo.iloc[check_idx] <= pre_high:
                    filled = True
                    break
            if not filled:
                signals.append(f"Gap Up ({gap_pct:.1f}% unfilled)")

    # Gap Down
    if gap_high < pre_low and pre_low > 0:
        gap_pct = (1.0 - gap_high / pre_low) * 100
        if gap_pct >= min_gap_pct:
            filled = False
            for k in range(1, no_fill_bars + 1):
                check_idx = gap_idx + k
                if check_idx < 0 and h.iloc[check_idx] >= pre_low:
                    filled = True
                    break
            if not filled:
                signals.append(f"Gap Down ({gap_pct:.1f}% unfilled)")

    return signals


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_ticker(ticker: str) -> TickerSignals:
    """Run all signal scans on a single ticker."""
    result = TickerSignals(ticker=ticker)
    try:
        df = fetch_daily_data(ticker)
        if df.empty or len(df) < 30:
            return result

        result.price = float(df["close"].iloc[-1])
        if len(df) >= 2:
            result.prev_close = float(df["close"].iloc[-2])
            result.change_pct = round((result.price - result.prev_close) / result.prev_close * 100, 2)
        result.date = str(df.index[-1].date())
        # SSP1 scope: BULL (STACK, X_EMA20/50/100/200) + BEAR (V1, V2).
        # MR / patterns / gaps disabled per user spec.
        result.long_signals = scan_long(df)
        result.bear_signals = scan_bear(df)
        result.mr_signals = []
        result.pattern_signals = []
        result.gap_signals = []

    except Exception as e:
        print(f"  [WARN] {ticker}: {e}")

    return result


def scan_all_tickers(tickers: list[str], progress_cb=None) -> list[TickerSignals]:
    """Scan all tickers and return those with signals."""
    results = []
    total = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        print(f"  Scanning {ticker} ({i}/{total})...", end="")
        if progress_cb:
            progress_cb(f"Scanning {ticker} ({i}/{total})")
        sig = scan_ticker(ticker)
        if sig.has_any:
            print(f" => {sig.signal_strength} signals ({sig.direction})")
            results.append(sig)
        else:
            print(" => no signals")
    return results

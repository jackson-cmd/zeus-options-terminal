"""趋势/动量:RSI(14)、SMA20/50、离均线扩张(σ)、近20日涨幅,以及趋势是否站在持仓方向。

给两处用:① 出场顾问(趋势是否帮这单)② 开单前「追高/涨疯了进去了」检测。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).rolling(period).mean()
    loss = (-d.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def indicators(close) -> dict:
    close = pd.Series(close, dtype=float).dropna()
    if len(close) < 50:
        return {}
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    last = float(close.iloc[-1])
    r = float(rsi(close).iloc[-1])
    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else None
    dvol = float(close.pct_change().tail(20).std())
    ext_sigma = ((last / sma20 - 1) / dvol) if (dvol and sma20) else None
    return {
        "last": round(last, 2),
        "rsi": round(r, 1) if np.isfinite(r) else None,
        "sma20": round(sma20, 2),
        "sma50": round(sma50, 2),
        "ret20_pct": round(ret20 * 100, 1) if ret20 is not None else None,
        "ext_sigma": round(ext_sigma, 2) if ext_sigma is not None else None,
    }


def trend_supports(close, direction) -> bool | None:
    """趋势是否站在这单需要的方向。bull:价>SMA20≥SMA50 且 RSI≥50;bear 反之。"""
    ind = indicators(close)
    if not ind:
        return None
    last, s20, s50, r = ind["last"], ind["sma20"], ind["sma50"], ind.get("rsi")
    up = last > s20 and s20 >= s50 and (r is None or r >= 50)
    down = last < s20 and s20 <= s50 and (r is None or r <= 50)
    d = (direction or "").upper()
    if d in ("BULL", "BULLISH", "C", "CALL"):
        return bool(up)
    if d in ("BEAR", "BEARISH", "P", "PUT"):
        return bool(down)
    return None


def overbought(close) -> dict:
    """追高检测:RSI≥70 或 离 SMA20 扩张 ≥1.5σ → flag。"""
    ind = indicators(close)
    if not ind:
        return {"flag": False}
    r = ind.get("rsi")
    ext = ind.get("ext_sigma")
    flag = (r is not None and r >= 70) or (ext is not None and ext >= 1.5)
    return {"flag": bool(flag), **ind}

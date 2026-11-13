"""heatmap/momentum.py — 两种反转候选信号(纯函数, 可独立测试)。

📈 触底反弹 (rebound): 30d 跌 ≥10% AND 近 3d 止跌 (>−1%)
📉 趋势回撤 (pullback): 30d 涨 ≥10% AND 近 3d 回撤 (≤−1%)

切分点 [-4] 把最近 3 个 bar 隔离作"近期", 前面 27 个算"远期", 不重叠。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARQUET = REPO_ROOT / "data" / "daily_ohlc.parquet"
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "options_vol" / "core_universe.txt"

LONG_WINDOW = 30
SHORT_WINDOW = 3
DROP_THRESHOLD = -0.10  # 30d 跌幅 ≤ 这个数 → 算"之前一直跌"
RISE_THRESHOLD = 0.10   # 30d 涨幅 ≥ 这个数 → 算"之前一直涨"
RECENT_FLAT_THRESHOLD = -0.01   # 近 3d 跌幅 > 这个数 (即 -1% 以内或涨) → 算"止跌"
RECENT_DROP_THRESHOLD = -0.01   # 近 3d 跌幅 ≤ 这个数 → 算"回撤"

MIN_BARS = LONG_WINDOW  # 30 根即可访问 iloc[-30]..iloc[-1] (close[-30] 到 close[-1])


def _classify(close: pd.Series) -> Optional[str]:
    """对单 ticker 的 close 序列判定信号. 返回 'rebound'/'pullback'/None."""
    if len(close) < MIN_BARS:
        return None
    long_ago = close.iloc[-LONG_WINDOW]
    short_ago = close.iloc[-(SHORT_WINDOW + 1)]
    today = close.iloc[-1]
    if long_ago <= 0 or short_ago <= 0:
        return None
    drop30 = short_ago / long_ago - 1.0
    recent3 = today / short_ago - 1.0
    if drop30 <= DROP_THRESHOLD and recent3 > RECENT_FLAT_THRESHOLD:
        return "rebound"
    if drop30 >= RISE_THRESHOLD and recent3 <= RECENT_DROP_THRESHOLD:
        return "pullback"
    return None


def _load_universe(universe_path: Path) -> set[str]:
    """读 universe 文本(每行一票, # 注释行, ### 段落标记忽略)."""
    out: set[str] = set()
    if not universe_path.exists():
        return out
    for line in universe_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s.upper())
    return out


def compute_signals(
    parquet_path: Path | str = DEFAULT_PARQUET,
    universe_path: Path | str = DEFAULT_UNIVERSE,
) -> dict:
    """从 daily ohlc parquet + universe 算 rebound/pullback 命中。"""
    parquet_path = Path(parquet_path)
    universe_path = Path(universe_path)

    universe = _load_universe(universe_path)
    if not parquet_path.exists():
        return {
            "rebound": [], "pullback": [],
            "asof": None,
            "thresholds": _thresholds(),
            "stats": {"total": len(universe), "rebound_count": 0, "pullback_count": 0, "missing": len(universe)},
            "error": f"parquet missing: {parquet_path}",
        }

    df = pd.read_parquet(parquet_path)
    if "ticker" not in df.columns or "Close" not in df.columns:
        return {
            "rebound": [], "pullback": [],
            "asof": None,
            "thresholds": _thresholds(),
            "stats": {"total": len(universe), "rebound_count": 0, "pullback_count": 0, "missing": len(universe)},
            "error": "parquet missing required cols",
        }

    df["ticker"] = df["ticker"].str.upper()
    df = df.sort_index()
    asof = str(df.index.max().date()) if len(df) else None

    rebound: list[str] = []
    pullback: list[str] = []
    missing = 0

    targets = universe if universe else set(df["ticker"].unique())
    for tk in sorted(targets):
        rows = df[df["ticker"] == tk]
        if len(rows) < MIN_BARS:
            missing += 1
            continue
        verdict = _classify(rows["Close"])
        if verdict == "rebound":
            rebound.append(tk)
        elif verdict == "pullback":
            pullback.append(tk)

    return {
        "rebound": rebound,
        "pullback": pullback,
        "asof": asof,
        "thresholds": _thresholds(),
        "stats": {
            "total": len(targets),
            "rebound_count": len(rebound),
            "pullback_count": len(pullback),
            "missing": missing,
        },
    }


def _thresholds() -> dict:
    return {
        "long_window": LONG_WINDOW,
        "short_window": SHORT_WINDOW,
        "drop_threshold": DROP_THRESHOLD,
        "rise_threshold": RISE_THRESHOLD,
        "recent_flat_threshold": RECENT_FLAT_THRESHOLD,
        "recent_drop_threshold": RECENT_DROP_THRESHOLD,
    }

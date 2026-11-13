"""两个并列策略的纯函数过滤器 —— strategy_a_trend_follow(趋势跟踪) /
strategy_b_bottom_fish(抄底 / 回踩支撑)。

⚠️ 财报窗口 + 期权流动性 两条两个策略都要, 但**不在这里重复实现** —— 由调用方
(engine.py)在拆分策略之前, 对同一份 df 先各跑一次 stock_picker.filters 的
layer_t4_earnings_blocker / layer_t6_options_liquidity("Reuse, do not duplicate"),
这两层跟策略无关。本文件只放两个策略各自独有的技术面条件, 纯函数、不做 IO。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config


def _log(verbose: bool, label: str, before: int, after: int) -> None:
    if verbose:
        print(f"[{label}] {before} -> {after} 行")


# ══════════════════════════ 策略 A —— 趋势跟踪 ══════════════════════════
def strategy_a_trend_follow(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """全部为真才保留:
      · 1M %Chg > 0 且 3M %Chg > 0(近期趋势良好)
      · %Change(今日) > -3% 且 1M %Chg > -8%(没有崩盘式下跌)
      · Latest > 20D EMA > 50D EMA > 200D EMA(完整多头排列, 严格 —— 按作者 spec
        原文只列了这三条 EMA, 100D EMA 不参与这条链)
      · 14D RSI <= 70(没有过热)
      · Wtd Alpha > 0(强度为正)
    贴 strategy="trend_follow" 列。Options Vol / 财报窗口由 engine.py 前置过滤。
    """
    before = len(df)
    if df.empty:
        _log(verbose, "策略A 趋势跟踪", before, 0)
        out = df.copy()
        out["strategy"] = pd.Series(dtype="object")
        return out

    latest, ema20, ema50, ema200 = df["Latest"], df["20D EMA"], df["50D EMA"], df["200D EMA"]
    chg1m, chg3m = df["1M %Chg"], df["3M %Chg"]
    chg_today, rsi, alpha = df["%Change"], df["14D RSI"], df["Wtd Alpha"]

    cond = (
        (chg1m > 0).fillna(False)
        & (chg3m > 0).fillna(False)
        & (chg_today > config.A_TODAY_CRASH_PCT).fillna(False)
        & (chg1m > config.A_1M_CRASH_PCT).fillna(False)
        & (latest > ema20).fillna(False)
        & (ema20 > ema50).fillna(False)
        & (ema50 > ema200).fillna(False)
        & (rsi <= config.A_RSI_MAX).fillna(False)
        & (alpha > 0).fillna(False)
    )
    out = df[cond].copy()
    out["strategy"] = "trend_follow"
    _log(verbose, "策略A 趋势跟踪", before, len(out))
    return out


# ══════════════════════════ 策略 B —— 抄底 / 回踩支撑 ══════════════════════════
_EMA_PERIODS = (20, 50, 100, 200)


def _compute_touch_ema(df: pd.DataFrame) -> pd.Series:
    """逐行判定"贴哪条均线" —— abs((Latest-EMA)/Latest) <= B_TOUCH_THRESHOLD 才算贴上;
    多线同时满足时按 config.B_EMA_TOUCH_PRIORITY(200>100>50>20, 慢线优先)取更强的那条。
    一条都不满足 → None(由调用方过滤掉这行)。"""
    latest = df["Latest"]
    touch_masks = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for period in _EMA_PERIODS:
            ema = df[f"{period}D EMA"]
            pct = (latest - ema).abs() / latest
            touch_masks[period] = (pct <= config.B_TOUCH_THRESHOLD).fillna(False)

    def _pick(idx):
        for period in config.B_EMA_TOUCH_PRIORITY:
            if touch_masks[period].loc[idx]:
                return period
        return None

    return pd.Series([_pick(i) for i in df.index], index=df.index, dtype="object")


def strategy_b_bottom_fish(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """全部为真才保留:
      · Latest > 200D EMA(长期上升趋势仍然成立, 必要条件)
      · 贴 20/50/100/200 中至少一条 EMA(见 _compute_touch_ema), 贴上时价格自己算
        touch_ema(慢线优先, 200 最强)
      · 14D RSI ∈ [25, 70](不超买也不恐慌超卖)
      · 1M %Chg > -12%(允许回踩, 不允许崩盘式暴跌)
    贴 strategy="bottom_fish" + touch_ema(int)列。Options Vol / 财报窗口由 engine.py
    前置过滤。"""
    before = len(df)
    if df.empty:
        _log(verbose, "策略B 抄底", before, 0)
        out = df.copy()
        out["touch_ema"] = pd.Series(dtype="object")
        out["strategy"] = pd.Series(dtype="object")
        return out

    latest, ema200 = df["Latest"], df["200D EMA"]
    rsi, chg1m = df["14D RSI"], df["1M %Chg"]

    base_cond = (
        (latest > ema200).fillna(False)
        & (rsi <= config.B_RSI_MAX).fillna(False)
        & (rsi >= config.B_RSI_MIN).fillna(False)
        & (chg1m > config.B_1M_CRASH_PCT).fillna(False)
    )
    candidate = df[base_cond].copy()
    if candidate.empty:
        _log(verbose, "策略B 抄底", before, 0)
        candidate["touch_ema"] = pd.Series(dtype="object")
        candidate["strategy"] = pd.Series(dtype="object")
        return candidate

    candidate["touch_ema"] = _compute_touch_ema(candidate)
    out = candidate[candidate["touch_ema"].notna()].copy()
    out["strategy"] = "bottom_fish"
    _log(verbose, "策略B 抄底", before, len(out))
    return out

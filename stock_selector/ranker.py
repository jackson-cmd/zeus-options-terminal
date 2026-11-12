"""打分 + 排序层 —— 两策略各自独立打分, 取 config.TOP_N_PER_STRATEGY。

目的只是"矮子里拔将军"选出最值得喂给 Sonnet 复核 + 展示的候选, 不是精细定价:
  策略 A(趋势跟踪): Wtd Alpha 当强度代理 + Barchart 三线全 Buy 加分(作者 spec 里的
    "可选加分") - RSI 越接近 70 小幅扣分(越靠近过热线越该往后排)
  策略 B(抄底): touch_ema 优先级基础分(200>100>50>20) + 贴线贴得越紧加分越多
"""
from __future__ import annotations

import pandas as pd

from . import config


def _all_trend_buy(row: pd.Series) -> bool:
    for col in ("Short Term", "Medium Term", "Long Term"):
        v = row.get(col)
        if not isinstance(v, str) or "Buy" not in v:
            return False
    return True


def rank_trend_follow(df: pd.DataFrame, top_n: int | None = None) -> pd.DataFrame:
    top_n = config.TOP_N_PER_STRATEGY if top_n is None else top_n
    out = df.copy()
    if out.empty:
        out["score"] = pd.Series(dtype="float64")
        return out

    alpha = out["Wtd Alpha"].fillna(0.0)
    all_buy_bonus = out.apply(_all_trend_buy, axis=1).astype(float) * config.BONUS_ALL_BUY_TREND
    # RSI 每超过 60 一点扣 0.5 分(越靠近 70 过热线越靠后排, 但不像 T3 那样直接丢票)
    rsi_penalty = (out["14D RSI"].fillna(50.0) - 60.0).clip(lower=0.0) * -0.5

    out["score"] = alpha + all_buy_bonus + rsi_penalty
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    return out.head(top_n).copy()


def rank_bottom_fish(df: pd.DataFrame, top_n: int | None = None) -> pd.DataFrame:
    top_n = config.TOP_N_PER_STRATEGY if top_n is None else top_n
    out = df.copy()
    if out.empty:
        out["score"] = pd.Series(dtype="float64")
        return out

    base = out["touch_ema"].map(config.TOUCH_EMA_SCORE).fillna(0.0)

    closeness_bonus = []
    for _, row in out.iterrows():
        touch = row.get("touch_ema")
        latest = row.get("Latest")
        if pd.isna(touch) or pd.isna(latest) or latest == 0:
            closeness_bonus.append(0.0)
            continue
        ema_col = f"{int(touch)}D EMA"
        ema = row.get(ema_col)
        if ema is None or pd.isna(ema):
            closeness_bonus.append(0.0)
            continue
        pct = abs((latest - ema) / latest)
        ratio = max(0.0, (config.B_TOUCH_THRESHOLD - pct) / config.B_TOUCH_THRESHOLD)
        closeness_bonus.append(ratio * 20.0)   # 贴线贴得最紧(pct=0)时 +20 分封顶

    out["score"] = base + pd.Series(closeness_bonus, index=out.index)
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    return out.head(top_n).copy()

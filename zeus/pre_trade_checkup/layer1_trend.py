"""Layer 1 —— 长期趋势:票 + 大盘(SPY)都要站在同一方向的 SMA200 上,否则不谈技术面。

纯函数:吃 heatmap/daily_store 拿到的 OHLC DataFrame,不做 IO。

bull 需要 Ticker Close > SMA200 且 SPY Close > SMA200(大盘对齐,防逆势单);bear 相反。
EMA stack(20>50>100>200,bull 有利)只 REPORT,不参与 pass/fail —— 这是"顺不顺"的加分项,
不是硬门槛(票刚好在 SMA200 上方止跌,EMA 还没排好序也可能是合理的回踩买点)。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from . import config


def _as_of(df: Optional[pd.DataFrame], today: Optional[str]) -> Optional[pd.DataFrame]:
    """截止 today(含)的视图;today=None → 原样返回(用最新一根当"今天")。"""
    if df is None or not len(df):
        return None
    if today is None:
        return df
    return df.loc[df.index <= pd.Timestamp(today)]


def evaluate(ticker: str, direction: str, ticker_df: Optional[pd.DataFrame],
             spy_df: Optional[pd.DataFrame], today: Optional[str] = None) -> dict:
    """direction: 'bull' | 'bear'。返回 {pass, reasons_pass, reasons_fail, detail}。"""
    bull = direction == "bull"
    side = ">" if bull else "<"
    reasons_pass: list[str] = []
    reasons_fail: list[str] = []
    # 展示用:固定自然顺序(ticker→SPY→EMA stack),每行自带状态图标,不按 pass/fail 分桶
    # 打乱阅读顺序(CLI 优先用这个;reasons_pass/reasons_fail 仍保留给汇总逻辑用)。
    lines: list[str] = []
    detail: dict = {}

    d = _as_of(ticker_df, today)
    if d is None or len(d) < config.SMA_TREND:
        msg = f"{ticker} 日线数据不足(<{config.SMA_TREND}根),无法判断长期趋势 —— 保守起见按不通过处理"
        reasons_fail.append(msg)
        return {"pass": False, "reasons_pass": reasons_pass,
                "reasons_fail": reasons_fail, "lines": [msg], "detail": detail}

    close = d["Close"]
    sma200 = float(close.rolling(config.SMA_TREND, min_periods=config.SMA_TREND).mean().iloc[-1])
    last = float(close.iloc[-1])
    pct = (last / sma200 - 1) * 100 if sma200 else None
    detail["ticker_close"] = round(last, 2)
    detail["ticker_sma200"] = round(sma200, 2)
    detail["ticker_pct_above_sma200"] = round(pct, 2) if pct is not None else None

    ok_ticker = (last > sma200) if bull else (last < sma200)
    if ok_ticker:
        reasons_pass.append(f"{ticker} Close {last:.2f} {side} SMA200 {sma200:.2f} ({pct:+.1f}%)")
        lines.append(f"{ticker} Close {last:.2f} ✅ {side} SMA200 {sma200:.2f} ({pct:+.1f}%)")
    else:
        msg = f"{ticker} Close {last:.2f} 未站在 SMA200 {sma200:.2f} 该方向一侧(要 {side})"
        reasons_fail.append(msg)
        lines.append(f"{ticker} Close {last:.2f} ❌ 未站在 SMA200 {sma200:.2f} 该方向一侧(要 {side})")

    ds = _as_of(spy_df, today)
    if ds is None or len(ds) < config.SMA_TREND:
        msg = "SPY 日线数据不足,无法判断大盘对齐 —— 保守起见按不通过处理"
        reasons_fail.append(msg)
        lines.append(msg)
        ok_spy = False
    else:
        spy_close = ds["Close"]
        spy_sma200 = float(spy_close.rolling(config.SMA_TREND, min_periods=config.SMA_TREND).mean().iloc[-1])
        spy_last = float(spy_close.iloc[-1])
        spy_pct = (spy_last / spy_sma200 - 1) * 100 if spy_sma200 else None
        detail["spy_close"] = round(spy_last, 2)
        detail["spy_sma200"] = round(spy_sma200, 2)
        detail["spy_pct_above_sma200"] = round(spy_pct, 2) if spy_pct is not None else None
        ok_spy = (spy_last > spy_sma200) if bull else (spy_last < spy_sma200)
        if ok_spy:
            reasons_pass.append(f"大盘对齐:SPY {spy_last:.2f} {side} SMA200 {spy_sma200:.2f} ({spy_pct:+.1f}%)")
            lines.append(f"大盘对齐 ✅:SPY {spy_last:.2f} {side} SMA200 {spy_sma200:.2f} ({spy_pct:+.1f}%)")
        else:
            msg = f"大盘不对齐:SPY {spy_last:.2f} 未站在 SMA200 {spy_sma200:.2f} 该方向一侧(要 {side})"
            reasons_fail.append(msg)
            lines.append(f"大盘不对齐 ❌:SPY {spy_last:.2f} 未站在 SMA200 {spy_sma200:.2f} 该方向一侧(要 {side})")

    # EMA stack —— 只 REPORT,不参与 pass/fail(顺不顺是加分项,不是硬门槛)
    emas = {p: float(close.ewm(span=p, adjust=False).mean().iloc[-1]) for p in config.EMA_PERIODS}
    stack_bull = emas[20] > emas[50] > emas[100] > emas[200]
    stack_bear = emas[20] < emas[50] < emas[100] < emas[200]
    favorable = bool(stack_bull if bull else stack_bear)
    detail["ema_stack"] = {str(p): round(v, 2) for p, v in emas.items()}
    detail["ema_stack_favorable"] = favorable
    stack_arrow = ">" if bull else "<"
    stack_order = stack_arrow.join(str(p) for p in config.EMA_PERIODS)
    stack_line = (f"EMA stack: {stack_order} "
                  + ("✅" if favorable else "❌(未完全顺排,仅供参考,不影响本层判定)"))
    reasons_pass.append(stack_line)
    lines.append(stack_line)

    return {
        "pass": bool(ok_ticker and ok_spy),
        "reasons_pass": reasons_pass,
        "reasons_fail": reasons_fail,
        "lines": lines,
        "detail": detail,
    }

"""Layer 2 —— EMA 触及(严格):今天必须是真正回踩/回抽 EMA 的那一天,不是随便哪天。

bull:Low 触到 EMA 的 [-1.5%, 0%] 带内(下探到/略破 EMA,但没破太多)、
      且 Close 收回 EMA 之上、且昨天 Close 也在 EMA 之上(不是刚跌破转头向上,是
      站稳后回踩)。bear 对称(High 试探阻力、Close 被压回 EMA 之下)。

同时算 EMA20/50/100/200 四条,谁触及了都记下来;如果不止一条触及,按"慢线优先"
(200>100>50>20)当作本次触及的 EMA —— 这与约 2.8 万笔价差回测里"哪条 EMA 触及后 pnl
最好"的经验排名(200>50>100>20,记在 config.EMA_PNL_RANK)是两件不同的事:前者是
"多线打架时算谁的"的规则,后者只是展示用的"这条线历史上强不强"的旁注。

一条都没触及 → fail(今天不是好的进场时机)。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from . import config


def evaluate(ticker: str, direction: str, ticker_df: Optional[pd.DataFrame],
             today: Optional[str] = None) -> dict:
    """返回 {pass, which_ema_touched, reasons_pass, reasons_fail, detail}。"""
    bull = direction == "bull"
    band = config.TOUCH_BAND_PCT
    reasons_pass: list[str] = []
    reasons_fail: list[str] = []

    d = ticker_df
    if d is not None and len(d) and today is not None:
        d = d.loc[d.index <= pd.Timestamp(today)]
    if d is None or len(d) < max(config.EMA_PERIODS) + 2:
        reasons_fail.append(f"{ticker} 日线数据不足,无法判断 EMA 触及 —— 保守起见按不通过处理")
        return {"pass": False, "which_ema_touched": None,
                "reasons_pass": reasons_pass, "reasons_fail": reasons_fail, "detail": {}}

    close = d["Close"]
    today_bar, prev_bar = d.iloc[-1], d.iloc[-2]

    candidates = []
    all_emas = {}
    for p in config.EMA_PERIODS:
        ema_series = close.ewm(span=p, adjust=False).mean()
        ema_today = float(ema_series.iloc[-1])
        ema_prev = float(ema_series.iloc[-2])
        all_emas[p] = round(ema_today, 2)
        if ema_today <= 0:
            continue
        if bull:
            touch_pct = (float(today_bar["Low"]) - ema_today) / ema_today * 100
            in_band = -band <= touch_pct <= 0
            reclaimed = float(today_bar["Close"]) >= ema_today
            held_prev = float(prev_bar["Close"]) >= ema_prev
        else:
            touch_pct = (float(today_bar["High"]) - ema_today) / ema_today * 100
            in_band = 0 <= touch_pct <= band
            reclaimed = float(today_bar["Close"]) <= ema_today
            held_prev = float(prev_bar["Close"]) <= ema_prev
        if in_band and reclaimed and held_prev:
            candidates.append({
                "ema": p, "ema_value": round(ema_today, 2),
                "touch_pct": round(touch_pct, 2),
            })

    detail = {"ema_values": all_emas, "candidates": candidates}

    if not candidates:
        reasons_fail.append(
            f"未触及任何 EMA(20/50/100/200)的严格触及带([-{band:.1f}%,0%]/[0%,+{band:.1f}%]),"
            "今天不是好的进场时机")
        return {"pass": False, "which_ema_touched": None,
                "reasons_pass": reasons_pass, "reasons_fail": reasons_fail, "detail": detail}

    priority = {p: i for i, p in enumerate(config.EMA_TOUCH_PRIORITY)}
    candidates.sort(key=lambda c: priority.get(c["ema"], 99))
    winner = candidates[0]
    which = winner["ema"]
    rank = config.EMA_PNL_RANK.get(which)
    rank_order = ">".join(str(p) for p in sorted(config.EMA_PNL_RANK, key=config.EMA_PNL_RANK.get))

    reasons_pass.append(
        f"触及 EMA{which}(距 {winner['touch_pct']:+.2f}%,在带内 "
        f"[{'-' if bull else ''}{band:.1f}%~{'0' if bull else f'+{band:.1f}%'}])")
    reasons_pass.append("收盘拿回 EMA 之上 ✅ 前日也在同侧 ✅" if bull
                         else "收盘被压回 EMA 之下 ✅ 前日也在同侧 ✅")
    if rank is not None:
        reasons_pass.append(f"强度:ema{which}(28k笔回测 pnl 排名 第{rank}/4,由高到低 {rank_order})")
    if len(candidates) > 1:
        others = ", ".join(f"ema{c['ema']}" for c in candidates[1:])
        reasons_pass.append(f"同时触及:{others}(按慢线优先规则记为 ema{which})")

    return {
        "pass": True,
        "which_ema_touched": which,
        "reasons_pass": reasons_pass,
        "reasons_fail": reasons_fail,
        "detail": detail,
    }

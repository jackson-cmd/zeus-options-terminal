"""Layer T2 / T3 / T4 / T6 / T8 / T9 —— DataFrame 上的纯函数过滤器。

不做 IO(取数据在 engine.py / macro_context.py),每层输入输出都是 DataFrame,
verbose=True 时打印本层剩余行数,方便 CLI --verbose。T8 的宏观数据(vix/spy_df/
saturation 等)由调用方(engine.py,经 macro_context.py)取好后传入。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

import pandas as pd

from . import config
from pre_trade_checkup import layer4_macro


def _log(verbose: bool, layer: str, before: int, after: int, note: str = "") -> None:
    if verbose:
        tail = f" ({note})" if note else ""
        print(f"[{layer}] {before} -> {after} 行{tail}")


def layer_t2_bull_trend(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """多头趋势硬过滤 —— 预期在这层丢掉大多数行(GOOD,这是设计目标)。
    全部为真才保留:
      · Latest > 200D EMA(长期在均线上方)
      · 200D EMA 有效(非 0/NaN)
      · 200D EMA > 0.9×100D EMA("200D在抬头"的粗略代理)
      · 50D EMA > 200D EMA 或 100D EMA > 200D EMA(至少中期跟长期同向站队)
      · Barchart Long Term 不含"Sell"(实际值形如 "100% Sell"/"50% Sell",不是纯
        "Sell" 字面量,所以用包含判断;"Hold"/"...Buy"/N/A 都放行)
    """
    before = len(df)
    if df.empty:
        _log(verbose, "T2 多头趋势", before, 0)
        return df.copy()

    latest = df["Latest"]
    ema200 = df["200D EMA"]
    ema100 = df["100D EMA"]
    ema50 = df["50D EMA"]

    cond = (
        latest.notna() & ema200.notna() &
        (ema200 > 0) &
        (latest > ema200) &
        (ema200 > config.T2_200_VS_100_RATIO * ema100) &
        ((ema50 > ema200) | (ema100 > ema200))
    )
    if "Long Term" in df.columns:
        long_term = df["Long Term"].astype("string").fillna("")
        cond &= ~long_term.str.contains("Sell", case=False, na=False)

    out = df[cond].copy()
    _log(verbose, "T2 多头趋势", before, len(out))
    return out


def layer_t3_not_crashed(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """丢崩盘 / 抛物线式暴涨 / 深超买 / 恐慌超卖的行。缺失数据不当"命中"处理
    (fail-soft,只有确认越线才丢)。"""
    before = len(df)
    if df.empty:
        _log(verbose, "T3 非崩盘/非过热", before, 0)
        return df.copy()

    chg1m = df["1M %Chg"]
    rsi = df["14D RSI"]

    crashed = (chg1m < config.T3_1M_CHG_CRASH).fillna(False)
    parabolic = (chg1m > config.T3_1M_CHG_PARABOLIC).fillna(False)
    overbought = (rsi > config.T3_RSI_OVERBOUGHT).fillna(False)
    panic = (rsi < config.T3_RSI_PANIC).fillna(False)
    drop = crashed | parabolic | overbought | panic

    out = df[~drop].copy()
    _log(verbose, "T3 非崩盘/非过热", before, len(out))
    return out


def layer_t4_earnings_blocker(df: pd.DataFrame, today: Optional[_dt.date] = None,
                                verbose: bool = False) -> pd.DataFrame:
    """丢掉财报日期落在 [today, today+47] 的行(两端都闭 —— 财报"就是今天"必须挡:
    实测 2026-07-30 跑真实 CSV 时 AMZN 财报正是当天盘后,若按字面 spec 的开区间
    (today, today+47] 会放过它,等于让作者当天开仓走进夜里财报的二元跳空,正是
    T4 存在的意义要防的场景 —— 所以两端都闭,不严格按 spec 原文的开区间实现)。
    缺失/N/A → 保留(只有确认临近才挡,不因为查不到就悲观地拦)。"""
    before = len(df)
    if df.empty:
        _log(verbose, "T4 财报窗口", before, 0)
        return df.copy()

    today = today or _dt.date.today()
    today_ts = pd.Timestamp(today)
    cutoff_ts = today_ts + pd.Timedelta(days=config.T4_EARNINGS_BLOCK_DAYS)

    ed = df["Earnings Date"]
    in_window = ((ed >= today_ts) & (ed <= cutoff_ts)).fillna(False)
    out = df[~in_window].copy()
    _log(verbose, "T4 财报窗口", before, len(out))
    return out


def layer_t6_options_liquidity(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """丢期权量 < 500 的行(太不流动,价差买卖价差会很宽)。"""
    before = len(df)
    if df.empty:
        _log(verbose, "T6 期权流动性", before, 0)
        return df.copy()

    vol = df["Options Vol"]
    keep = (vol >= config.T6_MIN_OPTIONS_VOL).fillna(False)
    if "_local_source" in df.columns:
        keep = keep | vol.isna()       # local data has no options volume: unknown ≠ illiquid
    out = df[keep].copy()
    _log(verbose, "T6 期权流动性", before, len(out))
    return out


def evaluate_macro_gate(vix, spy_df, saturation: dict, counts: dict, baseline_topics: dict) -> dict:
    """Layer T8 —— 在 pre_trade_checkup.layer4_macro 的四项之上,再加一道"全灭"硬
    阈值(比 layer4 自己的 fail 阈值更严):VIX > 25 或 benzinga 饱和度 >= 80 时,
    只标注 macro_status = pass/warn/fail 供 Sonnet 层和 CLI 参考。"""
    l4 = layer4_macro.evaluate(vix, spy_df, saturation, counts, baseline_topics)
    sat_score = (saturation or {}).get("score", 0)

    kill_reasons = []
    if vix is not None and vix > config.MACRO_KILL_VIX:
        kill_reasons.append(
            f"VIX {vix:.1f} > {config.MACRO_KILL_VIX:.0f} —— 硬阻断,清空所有候选")
    if sat_score >= config.MACRO_KILL_SATURATION:
        kill_reasons.append(
            f"benzinga 饱和度 {sat_score}/100 >= {config.MACRO_KILL_SATURATION} —— 硬阻断,清空所有候选")

    if not l4["pass"]:
        status = "fail"
    elif l4.get("warnings"):
        status = "warn"
    else:
        status = "pass"

    return {
        "kill_all": bool(kill_reasons),
        "kill_reasons": kill_reasons,
        "macro_status": status,
        "layer4": l4,
    }


def apply_macro_gate(df: pd.DataFrame, macro_gate: dict, verbose: bool = False) -> pd.DataFrame:
    """把 T8 的结果落到 DataFrame:硬阈值触发 → 清空(保留列结构);否则给每行贴
    macro_status + macro_reasons 注解列(不丢票)。"""
    before = len(df)
    if macro_gate["kill_all"]:
        out = df.iloc[0:0].copy()
        _log(verbose, "T8 宏观硬阻断", before, len(out), "; ".join(macro_gate["kill_reasons"]))
        return out

    out = df.copy()
    out["macro_status"] = macro_gate["macro_status"]
    l4 = macro_gate["layer4"]
    reasons = list(l4.get("reasons_fail", [])) + list(l4.get("warnings", []))
    out["macro_reasons"] = [list(reasons) for _ in range(len(out))]
    _log(verbose, "T8 宏观注解(不丢票)", before, len(out), f"status={macro_gate['macro_status']}")
    return out


def layer_t9_score(df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
    """打分 + 排序 + 取 top N(config.T9_TOP_N)。公式(抄作者 spec 原文):
      base = ema_touched 优先级分(200=100,50=75,100=60,20=40)
      + Wtd Alpha 为正: +alpha×0.5(封顶 +20)
      + % Institutional > 60: +5
      − 14D RSI > 70: −10
      + 1M %Chg < −8%(回踩但没崩): +3(作者想要的逆势回踩加分)
      − Beta > 2.5: −10(杠杆/太波动)
    """
    before = len(df)
    out = df.copy()

    base = out["ema_touched"].map(config.EMA_TOUCH_SCORE).fillna(0)

    alpha = out["Wtd Alpha"]
    alpha_bonus = (alpha.clip(lower=0) * config.T9_ALPHA_WEIGHT).clip(upper=config.T9_ALPHA_CAP).fillna(0)

    inst = out["% Institutional"]
    inst_bonus = (inst > config.T9_INSTITUTIONAL_THRESHOLD).fillna(False) * config.T9_INSTITUTIONAL_BONUS

    rsi = out["14D RSI"]
    rsi_penalty = (rsi > config.T9_RSI_PENALTY_THRESHOLD).fillna(False) * config.T9_RSI_PENALTY

    chg1m = out["1M %Chg"]
    retrace_bonus = (chg1m < config.T9_RETRACE_THRESHOLD).fillna(False) * config.T9_RETRACE_BONUS

    beta = out["Beta"]
    beta_penalty = (beta > config.T9_BETA_THRESHOLD).fillna(False) * config.T9_BETA_PENALTY

    out["score"] = base + alpha_bonus + inst_bonus + rsi_penalty + retrace_bonus + beta_penalty
    out = out.sort_values("score", ascending=False).reset_index(drop=True)
    out = out.head(config.T9_TOP_N).copy()

    _log(verbose, "T9 打分排序", before, len(out), f"取 top {config.T9_TOP_N}")
    return out

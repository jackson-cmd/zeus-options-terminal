"""Layer 4 —— 宏观压制(THE KEY ONE):VIX、SPY 短期急跌、benzinga 话题饱和度。

纯函数:vix / spy_df / saturation / counts / baseline_topics 全部由
data_sources.py 取好后传入,本层只判断,不碰文件、不发网络请求。

四个硬指标,任一越线就 fail:
  · VIX > 20(VIX 15-20 只警告,不 fail)
  · SPY 5 个交易日涨跌 < -3%(大盘正在急跌)
  · risk_radar regime_saturation 分数 >= 60(多个高 lift 话题今天在烧)
  · 5 个"红旗"话题(fed_fomc/inflation_cpi/jobs_nfp/tariffs_trade/banking_credit)
    里,任一话题今天计数 >= 该话题 P95 且 lift>=2.0 → 单独 fail(即便综合饱和度还没到 60,
    单个高危话题烧穿也算数,防止"总分被稀释掩盖了一个真正的风险"这种情况)。
    计数只到 P90(没到高 lift 的 P95 门槛)则只进 warnings,不 fail。

任何一项数据缺失(VIX 拉不到 / 今天没 digest)→ 那一项不参与 fail 判断,只如实标注 ——
SPY 5日涨跌走本地 daily_ohlc.parquet,基本不会缺,这项照常判。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from . import config


def spy_5d_change(spy_df: Optional[pd.DataFrame]) -> Optional[float]:
    """最近 5 个交易日的收盘涨跌%(今天收盘 vs 5 根前收盘)。"""
    if spy_df is None:
        return None
    close = spy_df["Close"].dropna()
    if len(close) < 6:
        return None
    last, ago = float(close.iloc[-1]), float(close.iloc[-6])
    if ago == 0:
        return None
    return (last / ago - 1) * 100


def evaluate(vix: Optional[float], spy_df: Optional[pd.DataFrame],
             saturation: dict, counts: dict, baseline_topics: dict) -> dict:
    reasons_pass: list[str] = []
    reasons_fail: list[str] = []
    warnings: list[str] = []
    # 展示用:固定自然顺序(VIX→SPY 5日→饱和度),每行自带状态图标,不按 pass/fail 分桶
    # 打乱阅读顺序(CLI 优先用这个;reasons_pass/reasons_fail 仍保留给汇总/建议逻辑用)。
    lines: list[str] = []

    # ── VIX ──
    if vix is None:
        reasons_pass.append("VIX 数据不可用 —— 不阻断")
        lines.append("VIX 数据不可用 —— 不阻断")
        vix_ok = True
    elif vix > config.VIX_FAIL:
        vix_ok = False
        msg = f"VIX {vix:.1f} > {config.VIX_FAIL:.0f} —— 波动率太高,借方价差容易被震出局"
        reasons_fail.append(msg)
        lines.append(f"VIX {vix:.1f} ❌ > {config.VIX_FAIL:.0f} —— 波动率太高,借方价差容易被震出局")
    elif vix >= config.VIX_WARN:
        vix_ok = True
        reasons_pass.append(f"VIX {vix:.1f} ⚠️ 警告区间(≥{config.VIX_WARN:.0f},<{config.VIX_FAIL:.0f})")
        lines.append(f"VIX {vix:.1f} ⚠️ 警告区间(≥{config.VIX_WARN:.0f},<{config.VIX_FAIL:.0f})")
    else:
        vix_ok = True
        reasons_pass.append(f"VIX {vix:.1f} 正常(<{config.VIX_WARN:.0f})")
        lines.append(f"VIX {vix:.1f} ✅ 正常(<{config.VIX_WARN:.0f})")

    # ── SPY 5 日涨跌 ──
    spy5d = spy_5d_change(spy_df)
    if spy5d is None:
        reasons_pass.append("SPY 5日涨跌数据不可用 —— 不阻断")
        lines.append("SPY 5日涨跌数据不可用 —— 不阻断")
        spy5d_ok = True
    else:
        spy5d_ok = spy5d >= config.SPY_5D_FAIL_PCT
        if spy5d_ok:
            reasons_pass.append(f"SPY 5日 {spy5d:+.2f}% (阈值 >= {config.SPY_5D_FAIL_PCT:.0f}%)")
            lines.append(f"SPY 5日 {spy5d:+.2f}% ✅ (阈值 >= {config.SPY_5D_FAIL_PCT:.0f}%)")
        else:
            reasons_fail.append(
                f"SPY 5日 {spy5d:+.2f}% —— 大盘正在急跌(阈值 >= {config.SPY_5D_FAIL_PCT:.0f}%)")
            lines.append(f"SPY 5日 {spy5d:+.2f}% ❌ —— 大盘正在急跌(阈值 >= {config.SPY_5D_FAIL_PCT:.0f}%)")

    # ── benzinga 话题饱和度(risk_radar.regime_saturation,横跨全部~20个话题) ──
    score = saturation.get("score", 0) if saturation else 0
    sat_ok = score < config.SATURATION_FAIL
    if sat_ok:
        reasons_pass.append(f"benzinga 今日饱和度 {score}/100 (阈值 <{config.SATURATION_FAIL})")
        lines.append(f"benzinga 今日饱和度 {score}/100 ✅ (阈值 <{config.SATURATION_FAIL})")
    else:
        reasons_fail.append(
            f"benzinga 今日饱和度 {score}/100 🔴 (阈值 <{config.SATURATION_FAIL}) —— 多个高危话题在烧")
        lines.append(f"benzinga 今日饱和度 {score}/100 🔴 (阈值 <{config.SATURATION_FAIL}) —— 多个高危话题在烧")

    # ── 5 个红旗话题:>=P90 → 进热点表警示;>=P95 且 高lift → 单独 fail ──
    hot_topics = []
    redflag_fail_topics = []
    for t in config.RED_FLAG_TOPICS:
        info = baseline_topics.get(t, {}) if baseline_topics else {}
        c = counts.get(t, 0) if counts else 0
        p90, p95 = info.get("p90", 0), info.get("p95", 0)
        lift = info.get("lift", 1.0)
        label = info.get("label", t)
        if p90 and c >= p90:
            meets_p95_highlift = bool(p95 and c >= p95 and lift >= config.RED_FLAG_HIGH_LIFT)
            hot_topics.append({
                "topic": t, "label": label, "count": c, "p90": p90, "p95": p95,
                "lift": lift, "meets_p95_highlift": meets_p95_highlift,
            })
            if meets_p95_highlift:
                redflag_fail_topics.append(label)

    if redflag_fail_topics:
        reasons_fail.append(
            f"高危话题今日达 P95 且 lift>={config.RED_FLAG_HIGH_LIFT}: "
            + "、".join(redflag_fail_topics) + " —— 单独否决,不看综合分")
    elif hot_topics:
        warnings.append("话题热度偏高(达 P90)但未到高危 P95 门槛: "
                         + "、".join(h["label"] for h in hot_topics))

    overall = vix_ok and spy5d_ok and sat_ok and not redflag_fail_topics
    return {
        "pass": bool(overall),
        "reasons_pass": reasons_pass,
        "reasons_fail": reasons_fail,
        "warnings": warnings,
        "lines": lines,
        "vix": vix,
        "spy_5d": round(spy5d, 2) if spy5d is not None else None,
        "saturation_score": score,
        "hot_topics": hot_topics,
    }

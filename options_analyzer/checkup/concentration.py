"""集中度打分:候选与现有 book 的加权联动 → 一个分数 + 警告等级。

按持仓 max_loss(用 net_debit 近似)加权候选对 book 的正相关;拆出系统性 beta
与同名抱团的提示。纯函数。
"""
from __future__ import annotations

from .rules import THRESHOLDS as T


def aggregate(pairs, candidate_beta=None, candidate_internal=None) -> dict:
    """pairs: [{ticker, pearson, tail, weight}](候选×book 的所有配对)。"""
    valid = [p for p in pairs if p.get("pearson") is not None and p.get("weight")]
    tw = sum(p["weight"] for p in valid)
    wmean = (sum(max(p["pearson"], 0.0) * p["weight"] for p in valid) / tw) if tw else None

    n_high = sum(1 for p in pairs if (p.get("pearson") or 0) >= T["pearson_warn"])
    n_red = sum(1 for p in pairs if (p.get("pearson") or 0) >= T["pearson_red"])
    n_tail = sum(1 for p in pairs if (p.get("tail") or 0) >= T["tail_red"])

    score = wmean
    if score is not None and score >= T["conc_red"] or n_red > 0:
        level = "red"
    elif score is not None and score >= T["conc_warn"] or n_high > 0:
        level = "warn"
    else:
        level = "ok"

    headline = None
    if level == "red":
        headline = "你在重押同一个方向——加这些票是火上浇油"
    elif level == "warn":
        headline = "和现有持仓有明显联动,注意别过度集中"
    else:
        headline = "与现有持仓联动不强,集中度可控"

    return {
        "score": round(score, 3) if score is not None else None,
        "weighted_pearson": round(wmean, 3) if wmean is not None else None,
        "n_high": n_high, "n_red": n_red, "n_tail_red": n_tail,
        "level": level, "headline": headline,
        "candidate_beta": round(candidate_beta, 2) if candidate_beta is not None else None,
        "candidate_internal": round(candidate_internal, 3) if candidate_internal is not None else None,
    }

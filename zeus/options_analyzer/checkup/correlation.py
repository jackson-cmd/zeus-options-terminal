"""相关性引擎(集中度套装)。

对两条【日对数收益】序列算:Pearson(线性)、Spearman(秩/单调,抗异常值)、
下尾相关(一起暴跌的条件概率)、滚动相关漂移(近60d vs 250d)、对 SPY 的 beta。
纯函数,不做任何 IO。输入接受 numpy array 或 pandas Series(自动对齐去 NaN)。
"""
from __future__ import annotations

import numpy as np

try:
    from scipy import stats as _stats
except Exception:  # pragma: no cover - scipy 应已装
    _stats = None

MIN_OBS = 30          # 算相关的最少重叠交易日
MIN_TAIL_OBS = 60     # 算下尾相关的最少样本(分位估计要更多点)


def _align(a, b):
    """两条序列尾对齐到等长、去 NaN,返回 (a, b) numpy 数组。"""
    a = np.asarray(getattr(a, "values", a), dtype=float)
    b = np.asarray(getattr(b, "values", b), dtype=float)
    n = min(len(a), len(b))
    if n == 0:
        return a[:0], b[:0]
    a, b = a[-n:], b[-n:]
    m = np.isfinite(a) & np.isfinite(b)
    return a[m], b[m]


def pearson(a, b) -> float | None:
    a, b = _align(a, b)
    if len(a) < MIN_OBS or a.std() == 0 or b.std() == 0:
        return None
    r = float(np.corrcoef(a, b)[0, 1])
    return r if np.isfinite(r) else None


def spearman(a, b) -> float | None:
    a, b = _align(a, b)
    if len(a) < MIN_OBS:
        return None
    if _stats is not None:
        r = _stats.spearmanr(a, b).correlation
    else:
        r = np.corrcoef(_rankdata(a), _rankdata(b))[0, 1]
    return float(r) if r is not None and np.isfinite(r) else None


def _rankdata(x):
    """平均秩(处理平局),无 scipy 时的兜底。"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    order = x.argsort(kind="mergesort")
    sx = x[order]
    ranks_sorted = np.arange(n, dtype=float)
    i = 0
    while i < n:
        j = i + 1
        while j < n and sx[j] == sx[i]:
            j += 1
        if j - i > 1:
            ranks_sorted[i:j] = (i + j - 1) / 2.0
        i = j
    ranks = np.empty(n, dtype=float)
    ranks[order] = ranks_sorted
    return ranks


def lower_tail(a, b, q: float = 0.05) -> float | None:
    """下尾相关系数估计 λ_L ≈ 0.5·[P(A≤qa|B≤qb) + P(B≤qb|A≤qa)]。

    两只票同时跌破各自 q 分位的条件概率(对称化更稳)。0=尾部独立,1=必同跌。
    """
    a, b = _align(a, b)
    if len(a) < MIN_TAIL_OBS:
        return None
    qa = np.quantile(a, q)
    qb = np.quantile(b, q)
    a_low = a <= qa
    b_low = b <= qb
    na, nb = a_low.sum(), b_low.sum()
    if na == 0 or nb == 0:
        return None
    joint = float((a_low & b_low).sum())
    lam = 0.5 * (joint / nb + joint / na)
    return float(min(1.0, max(0.0, lam)))


def rolling_drift(a, b, short: int = 60, long: int = 250) -> dict | None:
    """近 short 日 vs 近 long 日的 Pearson,看相关性是否在变紧。"""
    a, b = _align(a, b)
    if len(a) < short + 5:
        return None
    p_s = pearson(a[-short:], b[-short:])
    p_l = pearson(a[-long:], b[-long:]) if len(a) >= min(long, MIN_OBS) else p_s
    if p_s is None or p_l is None:
        return None
    return {"short": round(p_s, 3), "long": round(p_l, 3),
            "delta": round(p_s - p_l, 3), "tightening": p_s - p_l > 0.15}


def beta(asset, market) -> float | None:
    """asset 对 market(SPY)的 beta = cov / var(market)。"""
    a, m = _align(asset, market)
    if len(a) < MIN_OBS or m.var() == 0:
        return None
    bt = float(np.cov(a, m)[0, 1] / m.var())
    return bt if np.isfinite(bt) else None


def pair_stats(a, b, market=None) -> dict:
    """一对票的全套联动指标。market 给了就算 candidate 的 beta(对全 book 一致)。"""
    a2, b2 = _align(a, b)
    return {
        "n": int(len(a2)),
        "pearson": _r(pearson(a, b)),
        "spearman": _r(spearman(a, b)),
        "tail05": _r(lower_tail(a, b, 0.05)),
        "tail10": _r(lower_tail(a, b, 0.10)),
        "drift": rolling_drift(a, b),
        "beta": _r(beta(a, market)) if market is not None else None,
    }


def _r(x, nd: int = 3):
    return None if x is None else round(float(x), nd)

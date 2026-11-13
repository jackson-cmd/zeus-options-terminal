"""TA Screener 引擎 —— 作者 PineScript「MTF SR + ProbEdge」的 Python 移植。

对每只票(日线 OHLC,2 年):
1. MTF 共振 S/R zone:日/周/月 三个周期各取 pivot(高低点),按价格聚类成 zone,
   confluence = 贡献的周期数(3 = 黄金 zone)。
2. 触及/接近判定:今日 K 线 low/high 是否打进 zone(昨收在 zone 下方→阻力测试,
   上方→支撑测试);否则算距最近上方阻力 / 下方支撑的 %。
3. 概率 edge(条件频率统计,与 Pine 完全同口径):
   composite = 动量/ATR + RSI 偏离 + 趋势偏离 → 252 根 percentile → 5 bucket;
   regime = (close vs SMA200) × (ATR percentile vs 50) → 4 格;
   统计「该 cell 出现后未来 5 根上涨」的胜率 + t-stat(|t|>2 显著)。
4. 综合排序分:触及 S/R 的排最前(conf 越高越前),其后按接近度/edge/外部信号加权。

纯函数、不碰网络;输入 numpy 数组(由 data.py 喂)。
"""
from __future__ import annotations

from typing import Optional

import math

import numpy as np

# ── 默认参数(与 PineScript inputs 对齐)────────────────────────────────────
PARAMS = {
    "piv_len_d": 5,      # 日线 pivot strength(Pine 默认 5)
    "piv_len_w": 3,      # 周线(bar 少,放宽)
    "piv_len_m": 2,      # 月线
    "max_pivot": 30,     # 每周期 pivot 缓冲(Pine maxPivot)
    "bin_pct": 0.75,     # zone 聚类宽度 % (Pine binPct)
    "max_zones": 12,     # 最多保留 zone 数
    "min_conf_show": 2,  # 入选 zone 最低 confluence(3=只看黄金)
    "fwd_bars": 5,       # 预测 horizon(Pine fwdBars)
    "mom_len": 20,
    "rsi_len": 14,
    "trend_len": 50,
    "atr_len": 14,
    "w_mom": 1.0, "w_rsi": 1.0, "w_trend": 1.0,
    "trend_reg_len": 200,
    "vol_split": 50.0,
    "n_buckets": 5,
    "rank_len": 252,
    "min_samples": 15,
    "near_pct": 3.0,     # 「接近 zone」计分的最大距离 %
    # ── SSP(作者 Spread Signal Pruned v1)EMA 胜率信号 ──
    "ssp_bull_thr": 0.05,    # Bull 胜:4 个周桶内任一周 High ≥ 入场×1.05
    "ssp_bear_thr": 0.01,    # Bear 胜:信号周桶 Low ≤ 入场×0.99
    "ssp_bull_min_n": 20, "ssp_bear_min_n": 15,
    "ssp_min_rate": 60.0,    # 达标线 %
    # ── S/R 触及历史回测(walk-forward,结果口径同 SSP)──
    "sr_min_n": 12,          # 触及事件最少样本(比逐日采样信号稀,放宽)
    "sr_cooldown": 5,        # 同向触及事件冷却(交易日),去掉连续粘连重复计数
    # ── 抄底 setup 阈值(作者打法)──
    "dip_drop_today": -2.5,   # 今日跌幅 ≤ 此 → 今天大跌
    "dip_pullback_10d": -5.0, # 近10日跌幅 ≤ 此 → 跌了一段
    # ── TA 信号(MACD/RSI/Volume)实测 + RSI 门槛 ──
    "tasig_min_n": 15,        # 达标最少样本(同 SSP bear)
    "lift_min": 10.0,         # 真 edge 检验:胜率须超"随机入场基线"≥10pts(否则是基率水分)
    "rsi_hot": 75.0,          # RSI ≥ 此 → 推荐卡标 ⛔ 提醒(v3 不再罚分,仅展示)
    "rsi_cold": 25.0,         # RSI ≤ 此 → 同上(bear 侧)
    "vol_surge": 2.0,         # 放量上攻:量 ≥ 2× 20日均量
    "vol_dry": 0.7,           # 缩量:近10日均量 < 0.7× 前10日均量
    "tasig_cooldown": 5,      # 状态型信号(缩量)冷却,防粘连重复计数
    # ── 多周期趋势(短=SMA20 / 中=SMA50 / 长=SMA200,价格位置 + 均线斜率)──
    "trend_short_ma": 20, "trend_short_slope_win": 8,
    "trend_med_ma": 50,   "trend_med_slope_win": 15,
    "trend_long_ma": 200, "trend_long_slope_win": 20,
    "trend_flat": 0.003,      # 斜率 ±0.3% 以内算"走平"(中性)
    # ── 保守胜率(置信加权,v2 引入;v3 仍用于展示 adj + 达标判定不变)──
    "wilson_z": 1.645,        # Wilson 置信下界(单边95%);样本越小、下界越低
    "shrink_k": 30.0,         # 朝基线收缩强度:adj = base + (p−base)·n/(n+k)
    # ── 评分 v4(2026-06-13 化简:只留有预测力的价格信号 + 催化,去群体噪音)──
    #    实验(归档历史10316样本):PCR/OI/GEX 对未来20日收益 spread≈±1%、ρ≈0 = 噪音 →
    #    全部降为「只展示不计分」;统计edge 也微弱(spread+1%)→ 整块删(分+显示都不要)。
    #    新满分 100 = S/R 40 + 穿越EMA 20 + 趋势 25 + MACD 15。bull 偏向见 score_parts allow_bear。
    "sr_cap": 40.0,           # ① S/R 支撑阻力块封顶(权重最高的一块)
    "sr_base": 16.0,          #   今日触及同向 S/R 基础分
    "sr_conf3": 10.0,         #   黄金 zone(日+周+月 x3)加成
    "sr_conf2": 5.0,          #   x2 zone 加成
    "sr_pivot_each": 3.0,     #   zone 内每多 1 个 pivot(多次测试)
    "sr_pivot_cap": 9.0,      #   pivot 加成封顶
    "sr_hist_bonus": 5.0,     #   该票 S/R 历史回测达标再加
    "emax_cap": 20.0,         # ③ 穿越 EMA100/200 块封顶
    "emax_base": 12.0,        #   今日穿越(上穿=bull/下穿=bear)基础分
    "emax_qual_bonus": 8.0,   #   该穿越信号历史回测达标加成
    "score_trend_long": 12.0, # ④ 趋势顺势(共25):长(SMA200)同向 +12
    "score_trend_med": 8.0,   #              中(SMA50)同向 +8
    "score_trend_short": 5.0, #              短(SMA20)同向 +5
    "macd_pts": 15.0,         # ⑤ MACD 趋势态(多头=bull/空头=bear)
    "optflow_cap": 20.0,      # 期权 OI/Vol 方向结构(只算供展示,不再计分)
    "pattern_each": 2.0,      # ⑥ 额外分:每个匹配的 Finviz 形态 / 看多 Barchart screener,加在 100 之上
}

# ⑥ 形态/筛选额外分(v3.1)分类 —— 纯数据,external.py 也复用(barchart 中性过滤)。
# Finviz 8 形态各有天然方向 → 加给该方向那一侧。
FINVIZ_PATTERN_DIR = {
    "双底": "bull", "下降楔形": "bull", "上升通道": "bull", "趋势线支撑": "bull",
    "双顶": "bear", "上升楔形": "bear", "下降通道": "bear", "趋势线阻力": "bear",
}
# Barchart saved screener:看多的计分(bull 侧 +pattern_each);中性的整批丢弃(不抓不显)。
BARCHART_BULL = {"short_medium_long_buy", "weighted_alpha_ema", "3m_15__6m_30__stock",
                 "Gainer_recently", "1mUP30_", "5day10_"}
BARCHART_DROP = {"Institutional_Favorites", "barchart_popular", "barchart_signals", "price_changed"}


def _wilson_lb(p: float, n: int, z: float = 1.645) -> float:
    """胜率 p(0–1)在 n 个样本下的 Wilson 置信下界(0–1)。样本越小下界越低。"""
    if not n or n <= 0:
        return 0.0
    p = min(1.0, max(0.0, p))
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


def _shrink_to_base(p: float, base: float, n: int, k: float = 30.0) -> float:
    """朝基线收缩:小样本拉回 base(该票随机入场基率),大样本≈p。"""
    if not n or n <= 0:
        return base
    return base + (p - base) * n / (n + k)


def conservative_rate(rate_pct: float, n: int, base_pct: float = 50.0,
                      z: float = 1.645, k: float = 30.0) -> float:
    """把"原始胜率%"按样本量打成"保守胜率%" = min(Wilson下界, 朝基线收缩)。
    rate_pct/base_pct 都是 0–100。n≤0 → 0。两道保险取更保守的一个(作者口径)。"""
    if not n or n <= 0:
        return 0.0
    p = rate_pct / 100.0
    base = (base_pct if base_pct is not None else 50.0) / 100.0
    adj = min(_wilson_lb(p, n, z), _shrink_to_base(p, base, n, k))
    return round(max(0.0, adj) * 100.0, 1)


def _trend_state(close_t: float, sma_arr, win: int, flat: float = 0.003):
    """单周期趋势:价格在均线上方且均线上行=bull;下方且下行=bear;其余=neutral。
    数据不足 → None。这是标准定义(不掺主观),UI 据此显示短/中/长。"""
    if sma_arr is None:
        return None
    m = len(sma_arr)
    if m == 0 or math.isnan(sma_arr[-1]) or m <= win or math.isnan(sma_arr[-1 - win]):
        return None
    base = sma_arr[-1 - win]
    slope = (sma_arr[-1] - base) / base if base else 0.0
    above = close_t > sma_arr[-1]
    if above and slope >= flat:
        return "bull"
    if (not above) and slope <= -flat:
        return "bear"
    return "neutral"

SSP_BULL_NAMES = ["STK", "X20", "X50", "X100", "X200"]
SSP_BEAR_NAMES = ["V1", "V2"]

REGIME_NAMES = {0: "牛市/低波", 1: "牛市/高波", 2: "熊市/低波", 3: "熊市/高波"}


# ══════════════════ 基础指标(对齐 Pine 语义)══════════════════════════════
def pivots(high: np.ndarray, low: np.ndarray, L: int, max_keep: int = 30):
    """ta.pivothigh/pivotlow(L, L) 移植:第 i 根是 pivot high 当且仅当
    high[i] 严格大于左右各 L 根的 high(低点对称)。返回按时间序
    [(idx, price, 'H'|'L'), ...],只保留最近 max_keep 个。"""
    n = len(high)
    out = []
    for i in range(L, n - L):
        seg = high[i - L:i + L + 1]
        if high[i] == seg.max() and (seg == high[i]).sum() == 1:
            out.append((i, float(high[i]), "H"))
        seg = low[i - L:i + L + 1]
        if low[i] == seg.min() and (seg == low[i]).sum() == 1:
            out.append((i, float(low[i]), "L"))
    return out[-max_keep:]


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder 平滑(Pine ta.rma):seed = 前 n 个均值。前 n-1 位为 nan。"""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    alpha = 1.0 / n
    for i in range(n, len(x)):
        out[i] = out[i - 1] + alpha * (x[i] - out[i - 1])
    return out


def rsi(close: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ru, rd = rma(up, n), rma(dn, n)
    out = np.full(len(close), np.nan)
    ok = ~np.isnan(ru)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(rd > 0, ru / np.where(rd > 0, rd, 1.0), np.inf)
    out[ok] = np.where(np.isinf(rs[ok]), 100.0, 100.0 - 100.0 / (1.0 + rs[ok]))
    return out


def atr(high, low, close, n) -> np.ndarray:
    pc = np.roll(close, 1)
    pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return rma(tr, n)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """EMA(α=2/(n+1)),前 n 个均值做种(Pine ta.ema 同口径,深历史处差异可忽略)。"""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    a = 2.0 / (n + 1.0)
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def percentrank(x: np.ndarray, length: int) -> np.ndarray:
    """Pine ta.percentrank:前 length 根(不含当前)里 ≤ 当前值的比例 ×100。"""
    out = np.full(len(x), np.nan)
    for i in range(length, len(x)):
        win = x[i - length:i]
        if np.isnan(win).any() or np.isnan(x[i]):
            continue
        out[i] = (win <= x[i]).sum() / length * 100.0
    return out


# ══════════════════ S/R zone 聚类(Pine 单遍升序算法)═══════════════════════
def cluster_zones(pivot_groups: dict, bin_pct: float, min_conf: int = 2,
                  max_zones: int = 12) -> list:
    """pivot_groups: {'D': [(idx,price,kind)...], 'W': [...], 'M': [...]}。
    与 Pine 一致:全部 pivot 按价格升序,单遍聚类 —— 与「当前簇」均价差 ≤ bin_pct%
    就并入,否则开新簇。conf = 簇内出现过的周期数。
    返回 [{lo, hi, avg, conf, tfs, n_pivots}],conf 降序、再按 n_pivots 降序,
    截 max_zones,过滤 conf < min_conf。"""
    allp = []
    for tf, plist in pivot_groups.items():
        for (_i, price, _k) in plist:
            allp.append((price, tf))
    if not allp:
        return []
    allp.sort(key=lambda t: t[0])
    clusters = []  # 每个: {sum, cnt, lo, hi, tfs:set}
    for price, tf in allp:
        if clusters:
            c = clusters[-1]
            avg = c["sum"] / c["cnt"]
            if abs(price - avg) <= avg * bin_pct / 100.0:
                c["sum"] += price
                c["cnt"] += 1
                c["lo"] = min(c["lo"], price)
                c["hi"] = max(c["hi"], price)
                c["tfs"].add(tf)
                continue
        clusters.append({"sum": price, "cnt": 1, "lo": price, "hi": price, "tfs": {tf}})
    zones = []
    for c in clusters:
        avg = c["sum"] / c["cnt"]
        lo, hi = c["lo"], c["hi"]
        if lo == hi:  # 单点簇:给半个 bin 的厚度(Pine 同款)
            lo = avg * (1 - bin_pct / 200.0)
            hi = avg * (1 + bin_pct / 200.0)
        zones.append({
            "lo": round(lo, 4), "hi": round(hi, 4), "avg": round(avg, 4),
            "conf": len(c["tfs"]), "tfs": "".join(sorted(c["tfs"])),
            "n_pivots": c["cnt"],
        })
    zones = [z for z in zones if z["conf"] >= min_conf]
    zones.sort(key=lambda z: (-z["conf"], -z["n_pivots"]))
    return zones[:max_zones]


def nearest_zones(close: float, zones: list) -> dict:
    """最近上方阻力 / 下方支撑(zone 整体在上/下;Pine 同口径)。
    dist 为 %(上方为正、下方为负)。"""
    up, dn = None, None
    for z in zones:
        if z["lo"] > close:
            if up is None or z["lo"] < up["lo"]:
                up = z
        elif z["hi"] < close:
            if dn is None or z["hi"] > dn["hi"]:
                dn = z
    out = {"up": None, "dn": None}
    if up:
        out["up"] = {"px": up["avg"], "lo": up["lo"], "hi": up["hi"],
                     "conf": up["conf"], "dist": round((up["lo"] - close) / close * 100.0, 2)}
    if dn:
        out["dn"] = {"px": dn["avg"], "lo": dn["lo"], "hi": dn["hi"],
                     "conf": dn["conf"], "dist": round((dn["hi"] - close) / close * 100.0, 2)}
    return out


def zone_touch(low_t: float, high_t: float, prev_close: float, zones: list):
    """今日 K 线触及哪个 zone(Pine alert 同口径):low≤zone.hi && high≥zone.lo。
    昨收 < zone.lo → 阻力测试 'res';昨收 > zone.hi → 支撑测试 'sup';
    在 zone 内 → 'inside'(归阻力,Pine 默认)。多个命中取 conf 最高。"""
    best = None
    for z in zones:
        if low_t <= z["hi"] and high_t >= z["lo"]:
            if prev_close < z["lo"]:
                kind = "res"
            elif prev_close > z["hi"]:
                kind = "sup"
            else:
                kind = "inside"
            cand = {"kind": kind, "conf": z["conf"], "zone": z}
            if best is None or cand["conf"] > best["conf"]:
                best = cand
    return best


# ══════════════════ 概率 edge(条件频率统计)═══════════════════════════════
def prob_edge(high, low, close, p: dict = None) -> dict:
    """walk-forward 统计(与 Pine 逐 bar 累加完全同口径)。
    返回 {cur: {regime, regime_name, bucket, n, win_rate, t, sig, dir},
          best: {...} 或 None, enough_history: bool}。"""
    p = {**PARAMS, **(p or {})}
    n = len(close)
    need = p["rank_len"] + p["trend_reg_len"] // 2  # 经验下限
    atr_v = atr(high, low, close, p["atr_len"])
    sma_sig = sma(close, p["trend_len"])
    rsi_v = rsi(close, p["rsi_len"])
    mom = np.full(n, np.nan)
    m = p["mom_len"]
    mom[m:] = close[m:] - close[:-m]
    with np.errstate(divide="ignore", invalid="ignore"):
        mom_atr = np.where(atr_v > 0, mom / atr_v, np.nan)
        trend_atr = np.where(atr_v > 0, (close - sma_sig) / atr_v, np.nan)
    rsi_dev = (rsi_v - 50.0) / 10.0
    composite = p["w_mom"] * mom_atr + p["w_rsi"] * rsi_dev + p["w_trend"] * trend_atr

    rank = percentrank(composite, p["rank_len"])
    vol_rank = percentrank(atr_v, p["rank_len"])
    trend_sma = sma(close, p["trend_reg_len"])

    nb = p["n_buckets"]
    bucket = np.full(n, -1, dtype=int)
    ok = ~np.isnan(rank)
    bucket[ok] = np.minimum(nb - 1, (rank[ok] // (100.0 / nb)).astype(int))
    vol_reg = np.where(np.isnan(vol_rank), -1, (vol_rank > p["vol_split"]).astype(int))
    trend_reg = np.where(np.isnan(trend_sma), -1, (close <= trend_sma).astype(int))
    regime = np.where((vol_reg >= 0) & (trend_reg >= 0), trend_reg * 2 + vol_reg, -1)

    # 累加:cell = regime*10 + bucket(Pine f_cellIdx)
    cnt = np.zeros(50, dtype=int)
    up_ = np.zeros(50, dtype=int)
    fwd = p["fwd_bars"]
    for i in range(fwd, n):
        b0, r0 = bucket[i - fwd], regime[i - fwd]
        if b0 < 0 or r0 < 0:
            continue
        ci = r0 * 10 + b0
        c0 = close[i - fwd]
        if c0 == 0:
            continue
        cnt[ci] += 1
        if close[i] > c0:
            up_[ci] += 1

    def cell_stats(ci):
        c = int(cnt[ci])
        wr = up_[ci] / c if c > 0 else 0.0
        se = math.sqrt(wr * (1 - wr) / c) if c > 0 else 0.0
        t = (wr - 0.5) / se if se > 0 else 0.0
        return c, wr, t

    out = {"enough_history": n >= need, "cur": None, "best": None}
    rb, bb = int(regime[-1]), int(bucket[-1])
    if rb >= 0 and bb >= 0:
        c, wr, t = cell_stats(rb * 10 + bb)
        out["cur"] = {
            "regime": rb, "regime_name": REGIME_NAMES.get(rb, "?"), "bucket": bb + 1,
            "n": c, "win_rate": round(wr * 100.0, 1), "t": round(t, 2),
            "sig": (c >= p["min_samples"] and abs(t) > 2.0),
            "dir": ("多" if t > 0 else "空") if (c >= p["min_samples"] and abs(t) > 2.0) else "—",
        }
    best_t, best_ci = 0.0, -1
    for r in range(4):
        for b in range(nb):
            ci = r * 10 + b
            c, wr, t = cell_stats(ci)
            if c >= p["min_samples"] and abs(t) > abs(best_t):
                best_t, best_ci = t, ci
    if best_ci >= 0:
        c, wr, t = cell_stats(best_ci)
        out["best"] = {
            "regime": best_ci // 10, "regime_name": REGIME_NAMES.get(best_ci // 10, "?"),
            "bucket": best_ci % 10 + 1, "n": c, "win_rate": round(wr * 100.0, 1),
            "t": round(t, 2), "dir": "多" if t > 0 else "空", "sig": abs(t) > 2.0,
        }
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, sig_n: int = 9):
    """标准 MACD:macd = EMA12 − EMA26,signal = EMA9(macd)。前段 nan。"""
    ef, es = ema(close, fast), ema(close, slow)
    line = ef - es
    sig = np.full(len(close), np.nan)
    ok = ~np.isnan(line)
    if ok.sum() >= sig_n:
        idx = np.where(ok)[0]
        sub = ema(line[idx], sig_n)
        sig[idx] = sub
    return line, sig


# ══════════════════ TA 信号(MACD/RSI/Volume)实测胜率 ═══════════════════════
def _outcome_stats(idxs: list, dirn: str, close, wseq, whigh, wlow, p) -> dict:
    """与 SSP 完全同口径的事件结算:bull=信号周起4周桶内已完结桶 High≥entry×1.05;
    bear=信号周桶完结时 Low≤entry×0.99。未走完=悬挂不计。"""
    nweeks = len(whigh)
    last_w = nweeks - 1
    n = wins = 0
    bull_thr, bear_thr = p["ssp_bull_thr"], p["ssp_bear_thr"]
    for i in idxs:
        w0, entry = int(wseq[i]), float(close[i])
        if dirn == "bull":
            thr = entry * (1.0 + bull_thr)
            win = False
            for kk in range(w0, w0 + 4):
                if kk < nweeks and kk < last_w and whigh[kk] >= thr:
                    win = True
                    break
            if win:
                n += 1; wins += 1
            elif w0 + 3 < last_w:
                n += 1
        else:
            if w0 < last_w:
                n += 1
                if wlow[w0] <= entry * (1.0 - bear_thr):
                    wins += 1
    rate = round(wins * 100.0 / n, 1) if n > 0 else 0.0
    return {"n": n, "wins": wins, "rate": rate}


def tasig_signals(open_, high, low, close, volume, week_key, p: dict = None) -> Optional[dict]:
    """6 个经典 TA 信号的逐票 walk-forward 实测(口径同 SSP),外加当前 RSI/MACD/量比。
    信号(方向=假设,数据不支持→不达标→零分):
      MACD金叉→bull / MACD死叉→bear / RSI超卖反转(上穿30)→bull /
      RSI超买回落(下穿70)→bear / 放量上攻(量≥2×20日均+阳线涨1%)→bull /
      缩量(近10日均量<0.7×前10日,作者假设)→bear(5日冷却)。"""
    p = {**PARAMS, **(p or {})}
    n = len(close)
    if n < 120 or week_key is None:
        return None
    wh, wl, _wlast, wseq = _buckets(high, low, week_key)
    rsi_v = rsi(close, p["rsi_len"])
    line, sig_l = macd(close)
    e100, e200 = ema(close, 100), ema(close, 200)
    has_vol = volume is not None and len(volume) == n and np.nansum(volume) > 0
    vol = volume if has_vol else None
    v20 = sma(vol, 20) if has_vol else None
    ev = {k: [] for k in ("macd_up", "macd_dn", "rsi_up", "rsi_dn", "vol_up", "vol_dry",
                          "ema_dn100", "ema_dn200")}
    cool_dry = -10**9
    for i in range(35, n):
        # 跌穿 EMA100/200(穿越块的空头对应物;上穿走 SSP X100/X200)
        if not (math.isnan(e100[i]) or math.isnan(e100[i - 1])):
            if close[i] < e100[i] and close[i - 1] >= e100[i - 1]:
                ev["ema_dn100"].append(i)
        if not (math.isnan(e200[i]) or math.isnan(e200[i - 1])):
            if close[i] < e200[i] and close[i - 1] >= e200[i - 1]:
                ev["ema_dn200"].append(i)
        if not (math.isnan(line[i]) or math.isnan(sig_l[i]) or math.isnan(line[i-1]) or math.isnan(sig_l[i-1])):
            if line[i-1] <= sig_l[i-1] and line[i] > sig_l[i]:
                ev["macd_up"].append(i)
            if line[i-1] >= sig_l[i-1] and line[i] < sig_l[i]:
                ev["macd_dn"].append(i)
        if not (math.isnan(rsi_v[i]) or math.isnan(rsi_v[i-1])):
            if rsi_v[i-1] < 30 <= rsi_v[i]:
                ev["rsi_up"].append(i)
            if rsi_v[i-1] > 70 >= rsi_v[i]:
                ev["rsi_dn"].append(i)
        if has_vol and i >= 20 and not math.isnan(v20[i-1]) and v20[i-1] > 0:
            if vol[i] >= p["vol_surge"] * v20[i-1] and close[i] > open_[i] and close[i] >= close[i-1] * 1.01:
                ev["vol_up"].append(i)
        if has_vol and i >= 20:
            m10 = float(np.mean(vol[i-9:i+1])); m10p = float(np.mean(vol[i-19:i-9]))
            if m10p > 0 and m10 < p["vol_dry"] * m10p and i - cool_dry > p["tasig_cooldown"]:
                ev["vol_dry"].append(i)
                cool_dry = i
    META = [("macd_up", "MACD金叉", "bull"), ("macd_dn", "MACD死叉", "bear"),
            ("rsi_up", "RSI超卖反转", "bull"), ("rsi_dn", "RSI超买回落", "bear"),
            ("vol_up", "放量上攻", "bull"), ("vol_dry", "缩量(假设bear)", "bear"),
            ("ema_dn100", "跌穿EMA100", "bear"), ("ema_dn200", "跌穿EMA200", "bear")]
    sigs = []
    for key, name, dirn in META:
        st = _outcome_stats(ev[key], dirn, close, wseq, wh, wl, p)
        today = bool(ev[key] and ev[key][-1] == n - 1)
        qualified = bool(st["n"] >= p["tasig_min_n"] and st["rate"] >= p["ssp_min_rate"])
        sigs.append({"key": key, "name": name, "dir": dirn, **st,
                     "qualified": qualified, "today": today, "alert": bool(today and qualified)})
    vr = None
    if has_vol and n >= 20:
        m10 = float(np.mean(vol[-10:])); m10p = float(np.mean(vol[-20:-10]))
        vr = round(m10 / m10p, 2) if m10p > 0 else None
    return {
        "signals": sigs,
        "rsi": round(float(rsi_v[-1]), 1) if not math.isnan(rsi_v[-1]) else None,
        "macd_bull": bool(not math.isnan(line[-1]) and not math.isnan(sig_l[-1]) and line[-1] > sig_l[-1]),
        "vol_ratio10": vr,
    }


def base_rates(high, low, close, week_key, p: dict = None) -> Optional[dict]:
    """基线胜率:**每天都入场**时,同口径结果(bull 4周桶+5% / bear 信号周−1%)
    的命中率。任何信号的"edge"必须以此为参照 —— 胜率高于基线不足 lift_min
    个百分点的,只是基率水分,不是 edge。"""
    p = {**PARAMS, **(p or {})}
    n = len(close)
    if n < 120 or week_key is None:
        return None
    wh, wl, _, wseq = _buckets(high, low, week_key)
    st_b = _outcome_stats(list(range(30, n)), "bull", close, wseq, wh, wl, p)
    st_r = _outcome_stats(list(range(30, n)), "bear", close, wseq, wh, wl, p)
    return {"bull": st_b["rate"], "bear": st_r["rate"],
            "n_bull": st_b["n"], "n_bear": st_r["n"]}


def apply_lift(stats: dict, dirn: str, base: Optional[dict], min_n: int, p: dict = None) -> dict:
    """就地给一个 {n,rate,...} 统计块补 base/lift 并按真 edge 标准重判 qualified:
    n≥min_n AND rate≥ssp_min_rate AND lift=rate−基线 ≥ lift_min。"""
    p = {**PARAMS, **(p or {})}
    b = (base or {}).get(dirn)
    stats["base"] = b
    stats["lift"] = round(stats["rate"] - b, 1) if (b is not None and stats.get("n", 0) > 0) else None
    stats["qualified"] = bool(
        stats.get("n", 0) >= min_n and stats.get("rate", 0) >= p["ssp_min_rate"]
        and (stats["lift"] is None or stats["lift"] >= p["lift_min"])
        and stats["lift"] is not None
    )
    if "today" in stats:
        stats["alert"] = bool(stats["today"] and stats["qualified"])
    return stats


# ══════════════════ S/R 触及历史回测(walk-forward,无未来函数)═══════════════
def _buckets(high, low, key_arr):
    """按 key(周/月)分桶:返回 (bhigh, blow, last_daily_idx, seq_per_day)。"""
    n = len(key_arr)
    seq = np.zeros(n, dtype=int)
    for i in range(1, n):
        seq[i] = seq[i - 1] + (1 if key_arr[i] != key_arr[i - 1] else 0)
    nb = int(seq[-1]) + 1
    bh = np.full(nb, -np.inf)
    bl = np.full(nb, np.inf)
    last = np.zeros(nb, dtype=int)
    for i in range(n):
        b = seq[i]
        if high[i] > bh[b]:
            bh[b] = high[i]
        if low[i] < bl[b]:
            bl[b] = low[i]
        last[b] = i
    return bh, bl, last, seq


def _pivots_with_confirm(bh, bl, L, last_daily_idx=None):
    """桶(或日)序列上的 pivot + **确认日**(右侧 L 根走完才知道是 pivot)。
    返回 [(price, confirm_daily_idx)](时间序)。last_daily_idx=None 表示日线。"""
    out = []
    nb = len(bh)
    for j in range(L, nb - L):
        seg = bh[j - L:j + L + 1]
        if bh[j] == seg.max() and (seg == bh[j]).sum() == 1:
            cj = j + L
            out.append((float(bh[j]), int(last_daily_idx[cj]) if last_daily_idx is not None else cj))
        seg = bl[j - L:j + L + 1]
        if bl[j] == seg.min() and (seg == bl[j]).sum() == 1:
            cj = j + L
            out.append((float(bl[j]), int(last_daily_idx[cj]) if last_daily_idx is not None else cj))
    out.sort(key=lambda t: t[1])
    return out


def sr_touch_stats(high, low, close, week_key, month_key, p: dict = None) -> Optional[dict]:
    """「触及 S/R」的逐票历史胜率(walk-forward,严格用当时已确认的 pivot 建 zone):
    - 支撑测试(昨收在 zone 上方、今日 low 打进)→ 多头结果:信号周起 4 个周桶内
      任一已完结周桶 High ≥ 入场×(1+5%)(与 SSP Bull 完全同口径);
    - 阻力测试(昨收在下方/zone 内)→ 空头结果:信号周桶 Low ≤ 入场×(1−1%)(同 SSP Bear);
    - pivot 确认滞后:日线 +5 根、周线 +3 桶、月线 +2 桶 —— zone 在确认日之后才存在;
    - 同向事件 5 个交易日冷却(连续粘连只记 1 次)。
    返回 {"sup": {n,wins,rate,qualified}, "res": {...}};数据不足 → None。"""
    p = {**PARAMS, **(p or {})}
    n = len(close)
    if n < 120 or week_key is None or month_key is None:
        return None
    wh, wl, wlast, wseq = _buckets(high, low, week_key)
    mh, ml, mlast, _ = _buckets(high, low, month_key)
    nweeks = len(wh)
    last_w = nweeks - 1
    completed = lambda w: w < last_w

    # 各 TF 的 (price, confirm_daily_idx),时间序
    piv_d = _pivots_with_confirm(high, low, p["piv_len_d"])
    piv_w = _pivots_with_confirm(wh, wl, p["piv_len_w"], wlast)
    piv_m = _pivots_with_confirm(mh, ml, p["piv_len_m"], mlast)

    ptr = {"D": 0, "W": 0, "M": 0}
    src = {"D": piv_d, "W": piv_w, "M": piv_m}
    avail = {"D": [], "W": [], "M": []}
    zones: list = []
    bull_thr, bear_thr = p["ssp_bull_thr"], p["ssp_bear_thr"]
    sup_n = sup_w = res_n = res_w = 0
    cool = {"sup": -10**9, "res": -10**9}

    for i in range(1, n):
        changed = False
        for tf in ("D", "W", "M"):
            lst, k = src[tf], ptr[tf]
            while k < len(lst) and lst[k][1] <= i:
                avail[tf].append(lst[k][0])
                k += 1
                changed = True
            ptr[tf] = k
        if changed:
            groups = {tf: [(0, px, "?") for px in avail[tf][-p["max_pivot"]:]] for tf in ("D", "W", "M")}
            zones = cluster_zones(groups, p["bin_pct"], p["min_conf_show"], p["max_zones"])
        if not zones:
            continue
        t = zone_touch(float(low[i]), float(high[i]), float(close[i - 1]), zones)
        if not t:
            continue
        kind = "sup" if t["kind"] == "sup" else "res"
        if i - cool[kind] <= p["sr_cooldown"]:
            continue
        cool[kind] = i
        w0, entry = int(wseq[i]), float(close[i])
        if kind == "sup":
            thr = entry * (1.0 + bull_thr)
            win = False
            for kk in range(w0, w0 + 4):
                if kk < nweeks and completed(kk) and wh[kk] >= thr:
                    win = True
                    break
            if win:
                sup_n += 1; sup_w += 1
            elif w0 + 3 < nweeks and completed(w0 + 3):
                sup_n += 1
        else:
            if completed(w0):
                res_n += 1
                if wl[w0] <= entry * (1.0 - bear_thr):
                    res_w += 1

    def pack(cnt, wins):
        rate = round(wins * 100.0 / cnt, 1) if cnt > 0 else 0.0
        return {"n": int(cnt), "wins": int(wins), "rate": rate,
                "qualified": bool(cnt >= p["sr_min_n"] and rate >= p["ssp_min_rate"])}
    return {"sup": pack(sup_n, sup_w), "res": pack(res_n, res_w)}


# ══════════════════ SSP(Spread Signal Pruned v1)EMA 胜率信号 ══════════════
def ssp_signals(open_, high, low, close, week_key, p: dict = None) -> Optional[dict]:
    """作者 PineScript SSP1 的移植 —— **周桶口径与 TV 脚本完全一致**:
    - 周桶 = 日历周(week_key 变化分桶);评估只在周边界、只用已完结的周桶。
    - Bull(STK/X20/X50/X100/X200):胜 = 信号周起 4 个周桶(含信号周)内
      任一已完结周桶 High ≥ 入场×(1+5%);4 桶全完结仍未胜 → 负;否则悬挂不计。
    - Bear(V1/V2):信号周桶完结即结算,胜 = 该桶 Low ≤ 入场×(1−1%)。
    - STK/V1/V2 是"条件成立的每一天都采样"(同 Pine);X 系列是 crossover 转变日。
    返回 {bull:[5×{key,n,wins,rate,qualified,today,alert}], bear:[2×…],
          bull_alert, bear_alert};数据不足 → None。"""
    p = {**PARAMS, **(p or {})}
    n = len(close)
    if n < 220 or week_key is None or len(week_key) != n:
        return None
    e20, e50, e100, e200 = ema(close, 20), ema(close, 50), ema(close, 100), ema(close, 200)
    rsi14 = rsi(close, 14)

    # 周序号 + 周桶高低
    wseq = np.zeros(n, dtype=int)
    for i in range(1, n):
        wseq[i] = wseq[i - 1] + (1 if week_key[i] != week_key[i - 1] else 0)
    nweeks = int(wseq[-1]) + 1
    whigh = np.full(nweeks, -np.inf)
    wlow = np.full(nweeks, np.inf)
    for i in range(n):
        w = wseq[i]
        if high[i] > whigh[w]:
            whigh[w] = high[i]
        if low[i] < wlow[w]:
            wlow[w] = low[i]
    last_w = nweeks - 1          # 最后一个桶视为未完结(周可能没走完)
    completed = lambda w: w < last_w

    bull_n = [0] * 5; bull_win = [0] * 5; bull_today = [False] * 5
    bear_n = [0] * 2; bear_win = [0] * 2; bear_today = [False] * 2
    bull_thr, bear_thr = p["ssp_bull_thr"], p["ssp_bear_thr"]

    def resolve_bull(sig, i):
        w0, entry = int(wseq[i]), close[i]
        thr = entry * (1.0 + bull_thr)
        for k in range(w0, w0 + 4):
            if k < nweeks and completed(k) and whigh[k] >= thr:
                bull_n[sig] += 1; bull_win[sig] += 1
                return
        if w0 + 3 < nweeks and completed(w0 + 3):
            bull_n[sig] += 1     # 4 桶全完结未达标 → 负
        # 否则悬挂(窗口没走完),不计入

    def resolve_bear(sig, i):
        w0, entry = int(wseq[i]), close[i]
        if not completed(w0):
            return               # 信号周还没走完 → 悬挂
        bear_n[sig] += 1
        if wlow[w0] <= entry * (1.0 - bear_thr):
            bear_win[sig] += 1

    start = 200                  # ema200 就绪后才有信号(Pine 同样有 warmup)
    for i in range(max(start, 1), n):
        if np.isnan(e200[i]) or np.isnan(e200[i - 1]) or np.isnan(rsi14[i]):
            continue
        stack4 = e20[i] > e50[i] > e100[i] > e200[i]
        bear_stack = e20[i] < e50[i] < e100[i]
        is_last = (i == n - 1)
        # Bull
        sigs = []
        if stack4 and rsi14[i] < 70.0:
            sigs.append(0)
        if close[i] > e20[i] and close[i - 1] <= e20[i - 1]:
            sigs.append(1)
        if close[i] > e50[i] and close[i - 1] <= e50[i - 1]:
            sigs.append(2)
        if close[i] > e100[i] and close[i - 1] <= e100[i - 1]:
            sigs.append(3)
        if close[i] > e200[i] and close[i - 1] <= e200[i - 1]:
            sigs.append(4)
        for s in sigs:
            if is_last:
                bull_today[s] = True
            resolve_bull(s, i)
        # Bear
        bsigs = []
        if close[i] < e20[i] and close[i - 1] >= e20[i - 1] and bear_stack:
            bsigs.append(0)      # V1: crossunder + 空头排列
        if bear_stack and high[i] >= e20[i] and close[i] < e20[i] and close[i] < open_[i]:
            bsigs.append(1)      # V2: 反抽 ema20 失败的阴线
        for s in bsigs:
            if is_last:
                bear_today[s] = True
            resolve_bear(s, i)

    def pack(names, ns, wins, todays, min_n):
        out = []
        for k, name in enumerate(names):
            cnt, win = int(ns[k]), int(wins[k])
            rate = round(win * 100.0 / cnt, 1) if cnt > 0 else 0.0
            q = bool(cnt >= min_n and rate >= p["ssp_min_rate"])
            out.append({"key": name, "n": cnt, "wins": win, "rate": rate,
                        "qualified": q, "today": bool(todays[k]),
                        "alert": bool(todays[k] and q)})
        return out

    bull = pack(SSP_BULL_NAMES, bull_n, bull_win, bull_today, p["ssp_bull_min_n"])
    bear = pack(SSP_BEAR_NAMES, bear_n, bear_win, bear_today, p["ssp_bear_min_n"])
    return {"bull": bull, "bear": bear,
            "bull_alert": bool(any(s["alert"] for s in bull)),
            "bear_alert": bool(any(s["alert"] for s in bear))}


# ══════════════════ 评分 v3(作者配比;只加不减,无惩罚)═══════════════════════
# 满分 100 = ① S/R 支撑阻力 25 + ② 期权 OI/Vol 方向结构 20(scanner 加)
#          + ③ 穿越 EMA100/200 20 + ④ 大中小趋势 10
#          + ⑤ 统计edge 10 + PCR 5(scanner) + GEX 5(scanner) + MACD 5。
# 方向 = 多/空两边各算一遍 core,谁高跟谁(无惩罚;两边都 0 → 无方向 0 分)。
# ① 触及同向 zone 给基础分,**多周期共振(x2/x3)和 zone 内 pivot 多(多次测试)额外加**,
#    该票 S/R 历史回测达标再加成 —— 不再要求达标才给分(作者:S/R 是最重要的)。
# ③ 今日上穿 EMA100/200(SSP X100/X200)= bull / 跌穿 = bear,穿越本身给大头,
#    该穿越信号该票历史达标(含 lift)再加成。
# ⑤ edge 显著同向=10;MACD 当趋势态(多头排列=bull/空头=bear)=5;
#    PCR(<1 bull/>1 bear)5、GEX 区位(现价高于翻转 bull/低于 bear,≤60天链)5 由 scanner 按方向补。
#    RSI / MaxPain / 异动流不再计分,仅展示。
# parts 仍带 adj(保守胜率,展示用);core 在此算(满 70),② + PCR + GEX(30)由 scanner 补。
def score_parts(row: dict, p: dict = None, allow_bear: bool = True) -> dict:
    """allow_bear=False → 只算 bull 侧(牛市/中性时不推荐 bear,对应作者「尽量不开 bear」)。"""
    p = {**PARAMS, **(p or {})}
    br = row.get("base_rates") or {}

    def _base(dirn: str, kind: str) -> float:
        if kind == "edge":
            return 50.0                       # prob_edge 5日上下对称 → 基线 50%
        b = br.get(dirn)
        return float(b) if b is not None else 50.0

    def _adj(dirn, rate, n, kind):
        return conservative_rate(rate, n, _base(dirn, kind), p["wilson_z"], p["shrink_k"])

    touch = row.get("touch")
    sr = row.get("sr_stats") or {}
    ssp = row.get("ssp") or {}
    tasigs = (row.get("tasig") or {}).get("signals") or []
    e = (row.get("edge") or {}).get("cur") or {}
    trend = (row.get("setup") or {}).get("trend") or {}

    def _side(d: str):
        """算一侧(bull/bear)的 core 各块 + parts。"""
        parts = []

        # ── ① S/R 支撑阻力(0–25):触及同向 zone 给分,共振/多 pivot/历史达标加成 ──
        sr_pts = 0.0
        t_kind = (touch or {}).get("kind")
        touch_match = bool(touch) and ((d == "bull" and t_kind == "sup")
                                       or (d == "bear" and t_kind in ("res", "inside")))
        if touch_match:
            sr_pts = p["sr_base"]
            conf = int(touch.get("conf") or 0)
            if conf >= 3:
                sr_pts += p["sr_conf3"]
            elif conf == 2:
                sr_pts += p["sr_conf2"]
            npv = int(((touch.get("zone") or {}).get("n_pivots")) or 1)
            sr_pts += min(p["sr_pivot_cap"], p["sr_pivot_each"] * max(0, npv - 1))
            st = sr.get("sup" if d == "bull" else "res") or {}
            label = "S/R支撑反弹" if d == "bull" else "S/R阻力回落"
            if st.get("qualified"):
                sr_pts += p["sr_hist_bonus"]
            sr_pts = min(p["sr_cap"], sr_pts)
            parts.append({"dir": d, "rate": st.get("rate", 0.0),
                          "adj": _adj(d, st.get("rate", 0.0), st.get("n", 0), "sr") if st.get("n") else None,
                          "n": st.get("n", 0),
                          "label": label + f"(触及x{conf}·{npv}pivots" + ("·历史达标)" if st.get("qualified") else ")"),
                          "kind": "sr"})

        # ── ③ 穿越 EMA100/200(0–20):今日穿越给大头,历史达标加成 ──
        emax_pts = 0.0
        crosses = []
        if d == "bull":
            for s in (ssp.get("bull") or []):
                if s.get("key") in ("X100", "X200") and s.get("today"):
                    crosses.append((f"上穿EMA{s['key'][1:]}", s))
        else:
            for s in tasigs:
                if s.get("key") in ("ema_dn100", "ema_dn200") and s.get("today"):
                    crosses.append((s["name"], s))
        for label, s in crosses:
            pts = p["emax_base"] + (p["emax_qual_bonus"] if s.get("qualified") else 0.0)
            emax_pts = max(emax_pts, pts)
            parts.append({"dir": d, "rate": s.get("rate", 0.0),
                          "adj": _adj(d, s.get("rate", 0.0), s.get("n", 0), "ema") if s.get("n") else None,
                          "n": s.get("n", 0),
                          "label": label + ("·历史达标" if s.get("qualified") else ""),
                          "kind": "emax"})
        emax_pts = min(p["emax_cap"], emax_pts)

        # ── ④ 大中小趋势(0–15):长7/中5/短3 同向 ──
        trend_pts = ((p["score_trend_long"] if trend.get("long") == d else 0.0)
                     + (p["score_trend_med"] if trend.get("medium") == d else 0.0)
                     + (p["score_trend_short"] if trend.get("short") == d else 0.0))

        # ── ⑤ MACD 趋势态(多头=bull/空头=bear);统计edge 已删(实验证 spread+1% 微弱)──
        macd_pts = 0.0
        mb = (row.get("tasig") or {}).get("macd_bull")
        if mb is not None and ((d == "bull" and mb) or (d == "bear" and not mb)):
            macd_pts = p["macd_pts"]

        # ⑥ 额外分(Finviz 形态 / Barchart screener)已删(作者:没用)→ extra 恒 0
        extra = 0.0
        pat_labels = []

        comp = {"sr": round(sr_pts, 1), "opt": 0.0, "emax": round(emax_pts, 1),
                "trend": round(trend_pts, 1), "edge": 0.0,
                "macd": round(macd_pts, 1), "pcr": 0.0, "gex": 0.0,
                "extra": round(extra, 1), "extra_pats": pat_labels}
        core = sr_pts + emax_pts + trend_pts + macd_pts
        return core, extra, comp, parts

    bull_core, bull_extra, bull_comp, bull_parts = _side("bull")
    if allow_bear:
        bear_core, bear_extra, bear_comp, bear_parts = _side("bear")
    else:                                   # bull-only(牛市/中性:不推荐 bear)
        bear_core, bear_extra, bear_comp, bear_parts = 0.0, 0.0, None, []
    bull_tot, bear_tot = bull_core + bull_extra, bear_core + bear_extra
    if bull_tot <= 0 and bear_tot <= 0:
        return {"score": 0.0, "core": 0.0, "extra": 0.0, "dir": None, "parts": [],
                "conflict": False, "gate": False,
                "components": {"sr": 0.0, "opt": 0.0, "emax": 0.0, "trend": 0.0, "edge": 0.0,
                               "macd": 0.0, "pcr": 0.0, "gex": 0.0, "extra": 0.0, "extra_pats": []}}
    if bull_tot >= bear_tot:
        d, core, extra, comp, parts = "bull", bull_core, bull_extra, bull_comp, bull_parts
    else:
        d, core, extra, comp, parts = "bear", bear_core, bear_extra, bear_comp, bear_parts
    # 额外分加在 base(≤100)之上,可超 100(scanner 还会把期权 opt 并进 base 后再加 extra)
    return {"score": round(min(100.0, core) + extra, 1), "core": round(core, 1),
            "extra": round(extra, 1), "dir": d, "parts": parts,
            "conflict": bool(bull_tot > 0 and bear_tot > 0), "gate": False, "components": comp}


def score_row(row: dict, p: dict = None) -> float:
    return score_parts(row, p)["score"]


# ══════════════════ 单票全量分析(给 scanner 调)═══════════════════════════
def analyze_ticker(daily: dict, weekly: dict, monthly: dict, p: dict = None) -> dict:
    """daily/weekly/monthly: {'high': np.array, 'low': ..., 'close': ..., 'open': ...}。
    返回 screener 行(不含外部信号,score 由 scanner 合并后再算)。"""
    p = {**PARAMS, **(p or {})}
    h, l, c = daily["high"], daily["low"], daily["close"]
    n = len(c)
    if n < 60:
        return {"error": "bars<60", "bars": n}
    piv = {
        "D": pivots(h, l, p["piv_len_d"], p["max_pivot"]),
        "W": pivots(weekly["high"], weekly["low"], p["piv_len_w"], p["max_pivot"]),
        "M": pivots(monthly["high"], monthly["low"], p["piv_len_m"], p["max_pivot"]),
    }
    zones = cluster_zones(piv, p["bin_pct"], p["min_conf_show"], p["max_zones"])
    close_t = float(c[-1])
    prev_close = float(c[-2]) if n >= 2 else close_t
    nearest = nearest_zones(close_t, zones)
    touch = zone_touch(float(l[-1]), float(h[-1]), prev_close, zones)
    edge = prob_edge(h, l, c, p)
    try:
        ssp = ssp_signals(daily.get("open"), h, l, c, daily.get("week_key"), p)
    except Exception:
        ssp = None
    try:
        sr_stats = sr_touch_stats(h, l, c, daily.get("week_key"), daily.get("month_key"), p)
    except Exception:
        sr_stats = None
    try:
        tasig = tasig_signals(daily.get("open"), h, l, c, daily.get("volume"), daily.get("week_key"), p)
    except Exception:
        tasig = None
    # ── 真 edge 检验(lift):全部实测信号的"达标"重判为 胜率超基线≥lift_min ──
    try:
        base = base_rates(h, l, c, daily.get("week_key"), p)
    except Exception:
        base = None
    if base:
        for s in ((ssp or {}).get("bull") or []):
            apply_lift(s, "bull", base, p["ssp_bull_min_n"], p)
        for s in ((ssp or {}).get("bear") or []):
            apply_lift(s, "bear", base, p["ssp_bear_min_n"], p)
        if ssp:
            ssp["bull_alert"] = any(s.get("alert") for s in (ssp.get("bull") or []))
            ssp["bear_alert"] = any(s.get("alert") for s in (ssp.get("bear") or []))
        for s in ((tasig or {}).get("signals") or []):
            apply_lift(s, s["dir"], base, p["tasig_min_n"], p)
        if sr_stats:
            apply_lift(sr_stats["sup"], "bull", base, p["sr_min_n"], p)
            apply_lift(sr_stats["res"], "bear", base, p["sr_min_n"], p)
    chg = (close_t / prev_close - 1.0) * 100.0 if prev_close else 0.0
    # 52 周位置(辅助显示)
    lookback = min(n, 252)
    hi52 = float(np.max(h[-lookback:]))
    lo52 = float(np.min(l[-lookback:]))
    pos52 = (close_t - lo52) / (hi52 - lo52) * 100.0 if hi52 > lo52 else 50.0
    # ── 多周期趋势(短/中/长)+ 抄底 setup ──
    sma20 = sma(c, p["trend_short_ma"])
    sma50 = sma(c, p["trend_med_ma"])
    sma200 = sma(c, p["trend_long_ma"])
    trend = {
        "short": _trend_state(close_t, sma20, p["trend_short_slope_win"], p["trend_flat"]),
        "medium": _trend_state(close_t, sma50, p["trend_med_slope_win"], p["trend_flat"]),
        "long": _trend_state(close_t, sma200, p["trend_long_slope_win"], p["trend_flat"]),
    }
    long_bull = trend["long"] == "bull"          # 长期趋势=bull(站上 200 线且 200 线上行)
    above_sma200_pct = round((close_t / sma200[-1] - 1.0) * 100.0, 1) if (n >= p["trend_long_ma"] and not math.isnan(sma200[-1])) else None
    ret10 = round((close_t / c[-11] - 1.0) * 100.0, 2) if n >= 11 else 0.0
    ret20 = round((close_t / c[-21] - 1.0) * 100.0, 2) if n >= 21 else 0.0
    big_down_today = chg <= p["dip_drop_today"]
    pulled_back = ret10 <= p["dip_pullback_10d"]
    at_support = bool(touch and touch.get("kind") == "sup")
    sup_q = bool(((sr_stats or {}).get("sup") or {}).get("qualified"))
    # 抄底 = 长期 bull(信趋势) + 跌到支撑 + 短/中回调(今日大跌或近10日跌一段)
    dip_buy = bool(long_bull and at_support and (big_down_today or pulled_back))
    setup = {
        "trend": trend, "long_bull": long_bull, "above_sma200_pct": above_sma200_pct,
        "ret_10d": ret10, "ret_20d": ret20,
        "big_down_today": bool(big_down_today), "pulled_back": bool(pulled_back),
        "at_support": at_support, "support_qualified": sup_q, "dip_buy": dip_buy,
    }
    return {
        "close": round(close_t, 2), "chg_pct": round(chg, 2), "bars": n,
        "zones": zones, "nearest": nearest, "touch": touch, "edge": edge,
        "ssp": ssp, "sr_stats": sr_stats, "tasig": tasig, "base_rates": base, "setup": setup,
        "pos52": round(pos52, 1),
        "max_conf": max([z["conf"] for z in zones], default=0),
    }

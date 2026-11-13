"""classify.py —— 期权量稳定性裁决(纯函数,不做 IO)。

吃某一票的每日 options-history 记录(任意顺序),按 date 降序取最近 recent_days 天,
算「达标覆盖率」分四档:

  🥇 gold   —— 最近 ≥90% 交易日 totalVolume >= 5000(每天都很活跃)
  🥈 silver —— 最近 ≥90% 交易日 totalVolume >= 2000(每天有量但没到 gold)
  ◻ aside  —— 两档都不到 90%(以前活跃现在变死的 DINO 类,放一边)
  ∅ nodata —— 最近窗口 <5 天(没爬到/新上市,信息不足)

口径来自 docs/superpowers/specs/2026-06-16-options-vol-tab-design.md
「作者已拍板的三个口径」+「数据契约」,严格实现。仿 checkup/recommend.py 模式:
常量置顶、小工具私有、主函数返回纯 dict 供 build_summary / API 消费。
"""
from __future__ import annotations

# ── 口径常量(可被调用方覆盖,默认即作者拍板值)──
RECENT_DAYS = 21          # 判定窗口 ≈ 最近 1 个月交易日
TIER_HI = 5000            # 🥇 gold 单日量门槛
TIER_LO = 2000            # 🥈 silver 单日量门槛
COVERAGE = 0.90           # ≥90% 交易日达标才算「稳定」(容忍 1-2 个清淡日/半日市)
MIN_DAYS = 5              # 最近窗口少于这么多天 → nodata


def _to_int(x) -> int:
    """量/OI 取整;None / 缺失 / 非法 一律按 0,绝不抛。"""
    if x is None:
        return 0
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return 0


def _to_float(x, default: float = 0.0) -> float:
    """IV / ratio 转 float;None / 非法 用 default。"""
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _median_int(vals: list[int]) -> int:
    """中位数 → int()。偶数个 = 中间两个的平均(再 int)。空 → 0。"""
    n = len(vals)
    if n == 0:
        return 0
    s = sorted(vals)
    mid = n // 2
    if n % 2 == 1:
        return int(s[mid])
    return int((s[mid - 1] + s[mid]) / 2)


def classify_ticker(rows, *, recent_days: int = RECENT_DAYS,
                    tier_hi: int = TIER_HI, tier_lo: int = TIER_LO,
                    coverage: float = COVERAGE) -> dict:
    """某票每日记录 → tier + 展示指标。

    参数:
      rows: 该票每日记录列表(任意顺序),每行至少含 date(YYYY-MM-DD)/totalVolume,
            可选 totalOpenInterest / weightedImpliedVolatility / impliedVolatilityRank1y /
            putCallVolumeRatio / historicalLastPrice。
      recent_days: 判定窗口天数(默认 21 ≈ 1 个月)。
      tier_hi / tier_lo: gold / silver 的单日量门槛。
      coverage: 达标覆盖率门槛(默认 0.90)。

    返回 dict(指标除注明外均算自最近窗口):
      tier: 'gold'|'silver'|'aside'|'nodata'
      pct_days_hi: 最近窗口内 totalVolume>=tier_hi 的天数占比(float)
      pct_days_lo: 同上,门槛 tier_lo
      min_vol / median_vol / latest_vol: 最近窗口的 min / 中位 / 最新(int)
      latest_oi: 最新一行的 totalOpenInterest(int)
      latest_iv: 最新一行的 weightedImpliedVolatility(float, 4dp)
      iv_rank: 最新一行的 impliedVolatilityRank1y(float)
      latest_pc_vol: 最新一行的 putCallVolumeRatio(float, 3dp)
      n_days_recent: 最近窗口实际天数(int)
      n_days_total: 全量天数 = len(rows)(int)

    规则: n_days_recent<5 → 'nodata'; elif pct_days_hi>=coverage → 'gold';
          elif pct_days_lo>=coverage → 'silver'; else → 'aside'。
    鲁棒: 缺/None totalVolume 当 0;空 rows → 'nodata' + 全零。
    """
    rows = list(rows or [])
    n_total = len(rows)

    # 空数据 → nodata,全零(不崩)
    if n_total == 0:
        return {
            "tier": "nodata",
            "pct_days_hi": 0.0, "pct_days_lo": 0.0,
            "min_vol": 0, "median_vol": 0, "latest_vol": 0,
            "latest_oi": 0, "latest_iv": 0.0, "iv_rank": 0.0,
            "latest_pc_vol": 0.0,
            "n_days_recent": 0, "n_days_total": 0,
        }

    # 按 date 降序(最新在前);date 缺失的排到最后,不让它冒充最新
    rows_sorted = sorted(rows, key=lambda r: (r.get("date") or ""), reverse=True)
    window = rows_sorted[:recent_days]
    n_recent = len(window)

    # 最近窗口每日量(None/缺 → 0)
    vols = [_to_int(r.get("totalVolume")) for r in window]
    n_hi = sum(1 for v in vols if v >= tier_hi)
    n_lo = sum(1 for v in vols if v >= tier_lo)
    pct_hi = (n_hi / n_recent) if n_recent else 0.0
    pct_lo = (n_lo / n_recent) if n_recent else 0.0

    # 最新一行(降序后的第 0 行)→ 取 OI / IV / rank / P-C
    latest = rows_sorted[0]

    # 分档
    if n_recent < MIN_DAYS:
        tier = "nodata"
    elif pct_hi >= coverage:
        tier = "gold"
    elif pct_lo >= coverage:
        tier = "silver"
    else:
        tier = "aside"

    return {
        "tier": tier,
        "pct_days_hi": round(pct_hi, 4),
        "pct_days_lo": round(pct_lo, 4),
        "min_vol": min(vols) if vols else 0,
        "median_vol": _median_int(vols),
        "latest_vol": vols[0] if vols else 0,
        "latest_oi": _to_int(latest.get("totalOpenInterest")),
        "latest_iv": round(_to_float(latest.get("weightedImpliedVolatility")), 4),
        "iv_rank": round(_to_float(latest.get("impliedVolatilityRank1y")), 2),
        "latest_pc_vol": round(_to_float(latest.get("putCallVolumeRatio")), 3),
        "n_days_recent": n_recent,
        "n_days_total": n_total,
    }

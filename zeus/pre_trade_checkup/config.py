"""开单前体检 —— 全部阈值集中在这里(改规则 = 改这一个文件,不用满仓库找数字)。

对齐口径:
  · EMA_PERIODS / SMA_TREND 与 backtest_factory/config.py 一致(20/50/100/200, 200)。
  · VIX_FAIL(本模块"宏观转紧"阈值)与 options_analyzer/checkup/regime.py 的
    VIX_HIGH=22("regime 降级为震荡"阈值)是两个不同的概念,数字有意不同,别混着改。
"""
from __future__ import annotations

# ── Layer 1: 长期趋势 ──
SMA_TREND = 200                       # 长期趋势判定用的 SMA 周期
EMA_PERIODS = [20, 50, 100, 200]      # EMA stack 周期

# ── Layer 2: EMA 触及(严格) ──
TOUCH_BAND_PCT = 1.5                  # 触及带宽:bull=[-1.5%,0%](low相对ema) bear=[0%,+1.5%](high相对ema)
EMA_TOUCH_PRIORITY = (200, 100, 50, 20)   # 多线同时触及 → 慢线优先("prefer slower")
# 作者 28k 笔回测:按 ema_touch 分组的 pnl 排名(1=最好),仅用于 CLI 展示的「强度」提示,
# 不参与"多线同时触及选哪条"的判定(那个用 EMA_TOUCH_PRIORITY,纯按周期从慢到快)。
EMA_PNL_RANK = {200: 1, 50: 2, 100: 3, 20: 4}

# ── Layer 3: IV / 财报 ──
IVR_MAX = 50.0                        # IV Rank(1年百分位) > 此值 → IV 太贵,fail
EARNINGS_BUFFER_DAYS = 3              # 财报日 <= DTE + 此值(天)→ fail
IVR_STALE_WARN_DAYS = 14              # IVR 数据 asof 距今超过此天数 → 提示"可能过期"

# ── Layer 4: 宏观压制 ──
VIX_FAIL = 20.0                       # VIX > 此值 → fail
VIX_WARN = 15.0                       # VIX ∈ [VIX_WARN, VIX_FAIL) → 警告但不 fail
SPY_5D_FAIL_PCT = -3.0                # SPY 5 个交易日涨跌% < 此值 → fail(急跌中)
SATURATION_FAIL = 60                  # risk_radar regime_saturation 分数 >= 此值 → fail
RED_FLAG_TOPICS = ("fed_fomc", "inflation_cpi", "jobs_nfp", "tariffs_trade", "banking_credit")
RED_FLAG_HIGH_LIFT = 2.0              # 话题 lift >= 此值时,今日计数达 P95 → 单独 fail(不止是警告)

# ── override("如果你一定要开") ──
OVERRIDE_MIN_REASON_LEN = 20          # override 理由最少字数,凑不满不给记
OVERRIDE_LOG_NAME = "override_log.jsonl"   # 相对 pre_trade_checkup/data/ 的文件名

# ── 默认参数 ──
DEFAULT_DTE = 40

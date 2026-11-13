"""stock_selector 全局配置 —— 阈值集中在这里(改规则 = 改这一个文件)。

CSV 路径 / 财报窗口天数 / 期权流动性阈值 / strike DTE-宽度 全部直接复用
stock_picker.config 里已经定好的同名数值(见各调用点),这里只放 stock_selector
两个策略独有的阈值,不重复定义会跟 stock_picker 打架的常量。
"""
from __future__ import annotations

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent

# ── 策略 A: 趋势跟踪 ──
A_TODAY_CRASH_PCT = -3.0        # %Change(今日)低于此值 = 今天跌太多, 丢
A_1M_CRASH_PCT = -8.0           # 1M %Chg 低于此值 = 近期有大跌, 丢
A_RSI_MAX = 70.0                # 14D RSI 高于此值 = 过热

# ── 策略 B: 抄底 / 回踩支撑 ──
B_TOUCH_THRESHOLD = 0.015                     # abs((Latest-EMA)/Latest) <= 此值 才算"贴线"
B_EMA_TOUCH_PRIORITY = (200, 100, 50, 20)     # 慢线优先(200 最强支撑, 见作者 28k 笔回测 pnl 排名)
B_RSI_MAX = 70.0                 # 不超买
B_RSI_MIN = 25.0                 # 不恐慌超卖
B_1M_CRASH_PCT = -12.0           # 1M %Chg 低于此值 = 崩盘(回踩允许, 崩盘不行)

# ── 打分排序(ranker.py) ──
TOP_N_PER_STRATEGY = 15          # 每个策略取 top N(供 Sonnet 分析 + 前端展示, ≤30 合计)
BONUS_ALL_BUY_TREND = 10.0       # 策略A可选加分: Short/Medium/Long Term 全含 "Buy"
TOUCH_EMA_SCORE = {200: 100.0, 100: 75.0, 50: 50.0, 20: 25.0}   # 策略B: touch 优先级基础分

# ── Strike / DTE / 仓位 (2026-07-30 作者调整: DTE 30-45, 宽度 5%) ──
# 早前是 35-45 / 10%; 作者改为 30-45 / 5% 让单笔 debit 更小 · 单笔潜在最大亏损更小
# · 同一 NLV 可开更多单 · 短腿更贵所以 debit 更低 · trade-off = max profit 也更小
DTE_TARGET = 37                # 30-45 中值(月度到期挑最接近这个)
DTE_MIN = 30
DTE_MAX = 45
WIDTH_PCT = 0.05               # 短腿 = ATM + 5%(比之前的 10% 窄, 单笔更保守)

# 单笔仓位 500-1500 区间, 按理论 POP 分档(BSM lognormal N(d2), 长腿 IV):
# POP 高 = 到期站上 BE 概率大 = 值得下大注; POP 低 = 彩票单收小
POSITION_MIN_USD = 500.0
POSITION_MAX_USD = 1500.0
def target_debit_by_pop(pop_pct: float) -> float:
    """POP % → target debit ($500-1500 之间线性插值)。"""
    if pop_pct is None or pop_pct != pop_pct:
        return 900.0    # 拿不到 POP 就居中
    # 30% POP → $500; 55% POP → $1500; 之间线性
    p = max(30.0, min(55.0, pop_pct))
    frac = (p - 30.0) / 25.0
    return POSITION_MIN_USD + frac * (POSITION_MAX_USD - POSITION_MIN_USD)

# 挂单价 = 从 bid 往 mid 25% 处 (aggressive fill, 若成交不了 GTC 明天再挂)
# user: 「bid=2, ask=4, mid=3, 挂 2.5」 → 2 + 0.25*(4-2) = 2.5 ✓
LIMIT_BID_INTO_SPREAD = 0.25    # 0 = 挂 bid 死等 / 0.5 = mid / 1.0 = ask
ORDER_TIME_IN_FORCE = "GTC"      # Good-Till-Cancelled, 不成交继续挂

# 旧 POSITION_USD_BY_TOUCH 已废弃 (POP 分档取代), 保留常量避免 import 断裂
POSITION_USD_BY_TOUCH = {}
TARGET_POSITION_USD = 900.0    # 默认 (无 POP + 无 touch_ema 时兜底)

# ── Sonnet CLI(经 claude -p 订阅调用, 姿势见 sonnet_analyzer.py, 照抄
#    stock_picker/sonnet_analyzer.py 已验证过的调用惯例)──
SONNET_MODEL = "sonnet"
SONNET_TIMEOUT = 120
SONNET_TOP_K_HEADLINES = 15

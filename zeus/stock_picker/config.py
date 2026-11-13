"""stock_picker 全局配置 —— 阈值 / 路径 / Sonnet CLI 参数集中在这里(改规则 = 改这
一个文件,不用满仓库找数字)。

复用而非重复:35-45 DTE / 10% 宽度这套价差参数已经在 ema_pop_backtest/config.py
定过(作者玩法的唯一真源:DTE_BAND=(35,45), SPREAD_WIDTH_PCT=0.10);这里的
DTE_TARGET/WIDTH_PCT 数值上与它保持一致,MIN_OI / MAX_REL_SPREAD 直接从那个模块
import 复用(见 strike_engine.py),不重新定义。

⚠️ EMA_TOUCH_PRIORITY 这里是 (200, 50, 100, 20) —— 按作者 28k 笔回测的 **pnl 排名**
(pre_trade_checkup/config.py 的 EMA_PNL_RANK = {200:1,50:2,100:3,20:4})排序,
不是 pre_trade_checkup.config.EMA_TOUCH_PRIORITY 那个纯按周期从慢到快的
(200,100,50,20)——那个用于 checkup 模块自己的"多线同时触及选哪条"判定,两者
用途不同,数字不同是有意的,别混着改。
"""
from __future__ import annotations

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
DATA_DIR = PKG_DIR / "data"
LOCAL_CSV_COPY = DATA_DIR / "barchart_base.csv"

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "data" / "barchart_downloads"
DEFAULT_CSV_NAME = "stocks-screener-base-07-30-2026.csv"   # PRIMARY INPUT 约定文件名
DEFAULT_CSV_SOURCE = DOWNLOADS_DIR / DEFAULT_CSV_NAME

# 与 backtest_factory.config.OPTIONS_DIR 同一份 78GB EOD 期权归档(外置硬盘)
OPTIONS_ARCHIVE_DIR = Path("/Volumes/4T_sandisk/options_history_organized")

# ── Layer T2: 多头趋势硬过滤 ──
EMA_COLS = ["20D EMA", "50D EMA", "100D EMA", "200D EMA"]
T2_200_VS_100_RATIO = 0.9        # 200D EMA > 0.9×100D EMA("200D在抬头"的粗略代理,没有前一日数据做真斜率)

# ── Layer T3: 非崩盘 / 非过热 ──
T3_1M_CHG_CRASH = -15.0           # 1M %Chg 低于此值 = 崩盘(SOXL/SMH 那种走势),丢
T3_1M_CHG_PARABOLIC = 40.0        # 1M %Chg 高于此值 = 抛物线式暴涨,容易均值回归,丢
T3_RSI_OVERBOUGHT = 78.0          # 14D RSI 高于此值 = 深度超买
T3_RSI_PANIC = 22.0               # 14D RSI 低于此值 = 恐慌超卖(作者数据显示追这个会输)

# ── Layer T4: 财报窗口 ──
T4_EARNINGS_BLOCK_DAYS = 47       # Earnings Date ∈ (today, today+47] → 丢

# ── Layer T6: 期权流动性 ──
T6_MIN_OPTIONS_VOL = 500          # Options Vol 低于此值 = 太不流动,价差买卖价差会很宽

# ── Layer T7: EMA 触及 ──
TOUCH_LOW = -0.020
TOUCH_HIGH = 0.015
EMA_TOUCH_PRIORITY = (200, 50, 100, 20)     # 见模块头注释:按 28k 笔回测 pnl 排名,不是周期顺序
EMA_TOUCH_SCORE = {200: 100, 50: 75, 100: 60, 20: 40}   # T9 打分用的 base score

# ── Layer T8: 宏观硬阻断(比 pre_trade_checkup.layer4_macro 自己的 fail 阈值更严的"全灭"门槛)──
MACRO_KILL_VIX = 25.0              # VIX 高于此值 → 清空所有候选(不只是标 fail)
MACRO_KILL_SATURATION = 80         # benzinga regime_saturation 分数达到此值 → 清空所有候选

# ── Layer T9: 打分 ──
T9_ALPHA_WEIGHT = 0.5
T9_ALPHA_CAP = 20.0
T9_INSTITUTIONAL_THRESHOLD = 60.0
T9_INSTITUTIONAL_BONUS = 5.0
T9_RSI_PENALTY_THRESHOLD = 70.0
T9_RSI_PENALTY = -10.0
T9_RETRACE_THRESHOLD = -8.0        # 1M %Chg 低于此值(但没低到 T3 崩盘线)= 回踩,作者想要的
T9_RETRACE_BONUS = 3.0
T9_BETA_THRESHOLD = 2.5
T9_BETA_PENALTY = -10.0
T9_TOP_N = 20

# ── Layer T10: 选 strike ──
DTE_TARGET = 40                    # 35-45 DTE 窗口中点(与 ema_pop_backtest.config.DTE_BAND 一致)
WIDTH_PCT = 0.10                   # 宽度 = 10% 标的价(作者玩法,ATM 长腿)

# ema200_touch(回测 win 50% pnl +15.5%)= 最强 → 上限 $1500
# ema50_touch (回测 win 47% pnl +12.0%)= 中强 → $900-1200
# ema100_touch(回测 win 45% pnl +7.8%) = 中弱 → $700-900
# ema20_touch (回测 win 45% pnl +7.7%) = 弱   → $700-900(样本大易过热,收着做)
POSITION_USD_BY_TOUCH = {
    200: 1500.0,   # 200D touch 是最强信号,值得配最大仓位
    50:  1100.0,
    100: 900.0,
    20:  800.0,
}
TARGET_POSITION_USD = 1200.0       # 默认(向后兼容); 建议用 POSITION_USD_BY_TOUCH[ema_touched]

# ── Layer T11: Sonnet CLI(经 claude -p 订阅调用,姿势见 sonnet_analyzer.py)──
SONNET_MODEL = "sonnet"            # claude -p --model sonnet(别用 --bare,会强制走 API key)
SONNET_TIMEOUT = 90

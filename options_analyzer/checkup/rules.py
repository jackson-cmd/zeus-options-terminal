"""阈值与 journal 派生规则——所有可调旋钮集中在此一处。

作者在 spec §7 拍板的默认值;线上 mirror/exit_advisor 全部读这里,
改阈值只动这一个文件。mistake_rules.json 由离线 derive_mistake_rules.py 产出。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MISTAKE_RULES_FILE = REPO_ROOT / "trade_journal" / "data" / "mistake_rules.json"

# ── 阈值(spec §7,全部可调)──────────────────────────────────────────────
THRESHOLDS = {
    # 联动度 / 集中度
    "pearson_red": 0.70,        # 候选与某持仓 Pearson 超此 → 红
    "pearson_warn": 0.50,
    "tail_red": 0.50,           # 下尾相关超此 → 红(一起暴跌)
    "tail_warn": 0.35,
    "fake_hedge_corr": 0.60,    # thesis=对冲 但正相关超此 → 假对冲
    "conc_red": 0.60,           # 加权集中度分数红线
    "conc_warn": 0.40,
    # 极度 OTM(借方/方向性)
    "otm_sigma_warn": 1.5,
    "otm_sigma_red": 2.0,
    "otm_delta_floor": 0.15,    # 无 IV 时 delta 兜底
    # DTE
    "dte_zero": 1,              # DTE < 此 → 0DTE 黑名单(≤0 天)
    "dte_short_warn": 5,        # DTE ≤ 此 → 短期提醒
    # 仓位大小(单笔风险 / NLV)
    "size_warn_pct": 2.0,
    "size_red_pct": 5.0,
    # 赔率
    "rr_min": 1.5,
    # 追高超买
    "rsi_warn": 70.0,
    "rsi_strong": 75.0,
    "extend_sma_sigma": 1.5,    # 离 SMA20 超几个 σ 算追高
    # 价格滑点
    "slippage_warn_pct": 5.0,
    "slippage_red_pct": 10.0,
    # ── 持仓止盈止损(出场口径,概率驱动)──
    "tp_profit_pct": 50.0,      # 止盈:收益 ≥ 此
    "tp_min_dte": 14,           # 且 DTE ≥ 此
    "tp_strong_dte": 20,        # DTE ≥ 此 → 更强烈止盈
    "tp_near_pct": 35.0,        # 接近止盈区下沿
    "cut_cop_dead": 15.0,       # COP < 此 → 基本判死,砍
    "cut_cop_lowtime": 25.0,    # COP < 此 且 DTE < cut_dte_lowtime → 砍
    "cut_dte_lowtime": 20,
    "stop_ref_pct": 50.0,       # 50% 止损线(仅参考显示,不自动触发)
}

# 别名表(开单前输入支持)
TICKER_ALIASES = {
    "MAG7": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"],
    "MAGNIFICENT7": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"],
    "FAANG": ["META", "AAPL", "AMZN", "NFLX", "GOOGL"],
    "FAAMG": ["META", "AAPL", "AMZN", "MSFT", "GOOGL"],
    "SEMIS": ["NVDA", "AMD", "AVGO", "MU", "TSM", "INTC", "QCOM"],
    "BANKS": ["JPM", "BAC", "WFC", "C", "GS", "MS"],
}

_MARKET_PROXY = "SPY"   # beta 基准


def threshold(key: str, default=None):
    return THRESHOLDS.get(key, default)


def load_mistake_rules() -> dict:
    """读离线挖掘产出的 journal 错误规则;缺文件/坏文件 → {}(fail-soft)。

    结构:{"by_ticker": {TICKER: {n, net_pnl, win_rate}},
          "by_mistake": {label: {n, net_pnl, avg_pnl, win_rate}},
          "headline": {...}, "generated_at": "..."}
    """
    try:
        return json.loads(MISTAKE_RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

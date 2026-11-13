"""Experiment configuration — single source of truth."""
from __future__ import annotations

import random
from pathlib import Path

import pandas as pd

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
RESULTS_DIR = PKG_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SEED = 42
N_RANDOM = 100
CORE = ["SPY", "QQQ"]

# 三个回测窗口(起始日期,结束由主程序取"到今为止")
WINDOWS = {
    "8y": pd.Timestamp("2018-01-01"),
    "4y": pd.Timestamp("2022-01-01"),
    "2y": pd.Timestamp("2024-01-01"),
}

# EMA periods to test
EMA_PERIODS = [20, 50, 100, 200]

DTE_BAND = (35, 45)              # 作者 35-45 DTE
SPREAD_WIDTH_PCT = 0.10          # 宽度 ≈ 10% 标的价
PROFIT_TAKE = 0.90               # 吃到 90% max profit 就平
LONG_OFF_PCT = 0.0               # ATM 长腿(作者默认)

# Tradability filters (与 backtest_factory 一致)
MIN_OI = 1
MAX_REL_SPREAD = 0.60


def pick_universe() -> list[str]:
    """稳定复现的 102 票池: SPY + QQQ + tickers.csv 里 seed 42 抽 100 只(排除 SPY/QQQ)。"""
    tickers = pd.read_csv(REPO_DIR / "data" / "tickers.csv")["ticker"].tolist()
    pool = [t for t in tickers if t not in CORE]
    rng = random.Random(SEED)
    sample = rng.sample(pool, N_RANDOM)
    return list(CORE) + sample

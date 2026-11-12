"""hold-to-expiry what-if pure functions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CLOSES = REPO_DIR / "trade_journal" / "data" / "entries_close.jsonl"
DEFAULT_OHLC = REPO_DIR / "data" / "daily_ohlc.parquet"


_LABEL_RE = re.compile(r"([\d.]+)/([\d.]+)([CP])\s+(\d+)/(\d+)/(\d+)")


def parse_spread_label(label: str) -> Optional[dict]:
    """'170/200C 8/14/26' -> {'k1':170.0,'k2':200.0,'cp':'C','exp':Timestamp(2026-08-14)}."""
    if not label:
        return None
    m = _LABEL_RE.match(str(label))
    if not m:
        return None
    k1, k2, cp, mm, dd, yy = m.groups()
    return {
        "k1": float(k1),
        "k2": float(k2),
        "cp": cp,
        "exp": pd.Timestamp(f"20{yy}-{int(mm):02d}-{int(dd):02d}"),
    }


def intrinsic_at_expiry(direction: str, long_K: float, short_K: float,
                        close_at_expiry: float) -> float:
    """到期日 spread 内在价值(每股),bull call / bear put。"""
    if direction == "BULL":
        # long_K < short_K (bull call debit)
        width = short_K - long_K
        return float(np.clip(close_at_expiry - long_K, 0, width))
    else:
        # bear put debit: long_K > short_K
        width = long_K - short_K
        return float(np.clip(long_K - close_at_expiry, 0, width))


def _expiry_close(ohlc: pd.DataFrame, ticker: str, exp: pd.Timestamp,
                  window: int = 5) -> Optional[float]:
    """取到期日 Close;若那天缺数据(周末/未来/未到)向前找最近 window 个交易日。
    None = 数据不足(通常因为到期日在未来)。"""
    sub = ohlc[ohlc["ticker"] == ticker.upper()]
    if sub.empty:
        return None
    sub = sub.sort_index()
    # 到期日或之前最近一天
    on_or_before = sub[sub.index <= exp]
    if on_or_before.empty:
        return None
    latest = on_or_before.iloc[-1]
    days_gap = (exp - latest.name).days
    if days_gap > window:
        return None
    return float(latest["Close"])


def compute_hold_pnl(trade: dict, ohlc: pd.DataFrame) -> dict:
    """一单 → {actual_pnl, hypothetical_pnl, regret, has_data, ...}。"""
    parsed = parse_spread_label(trade.get("spread_label"))
    if not parsed:
        return {"has_data": False, "reason": "cannot parse spread_label"}

    direction = (trade.get("direction") or "").upper()
    if direction not in ("BULL", "BEAR"):
        return {"has_data": False, "reason": f"unknown direction {direction!r}"}

    long_K = min(parsed["k1"], parsed["k2"]) if direction == "BULL" else max(parsed["k1"], parsed["k2"])
    short_K = max(parsed["k1"], parsed["k2"]) if direction == "BULL" else min(parsed["k1"], parsed["k2"])
    width = abs(short_K - long_K)

    qty = float(trade.get("quantity") or 1)
    # open_price 是账户里的进价成本(负数=借方付出的钱)
    debit_per_share = abs(float(trade.get("open_price") or 0)) / (100 * qty) if qty else 0
    if debit_per_share <= 0:
        return {"has_data": False, "reason": "cannot infer debit"}

    ticker = str(trade.get("ticker") or "").upper()
    exp = parsed["exp"]

    exp_close = _expiry_close(ohlc, ticker, exp)
    if exp_close is None:
        return {"has_data": False, "reason": f"no ohlc for {ticker} near {exp.date()}"}

    intrinsic = intrinsic_at_expiry(direction, long_K, short_K, exp_close)
    hypothetical_pnl = (intrinsic - debit_per_share) * 100 * qty
    # 防御:hypothetical 不能低于 -debit_per_share * 100 * qty (defined risk)
    max_loss = -debit_per_share * 100 * qty
    max_profit = (width - debit_per_share) * 100 * qty
    hypothetical_pnl = float(np.clip(hypothetical_pnl, max_loss, max_profit))

    actual_pnl = float(trade.get("net_pnl") or 0)
    regret = actual_pnl - hypothetical_pnl   # 正=砍单更好;负=白砍

    close_date = pd.to_datetime(trade.get("close_date"))
    dte_at_close = (exp - close_date).days if pd.notna(close_date) else None

    return {
        "has_data": True,
        "actual_pnl": actual_pnl,
        "hypothetical_pnl": hypothetical_pnl,
        "regret": regret,                    # >0 砍对了, <0 白砍
        "cut_was_wrong": regret < 0,
        "direction": direction,
        "long_K": long_K, "short_K": short_K, "width": width,
        "debit_per_share": debit_per_share,
        "qty": qty,
        "exp": exp,
        "exp_close": exp_close,
        "intrinsic_per_share": intrinsic,
        "dte_at_close": dte_at_close,
        "max_loss": max_loss,
        "max_profit": max_profit,
    }


def whatif_all(closes_path: Path = DEFAULT_CLOSES,
                ohlc_path: Path = DEFAULT_OHLC) -> pd.DataFrame:
    """全部平仓单跑一遍 what-if。返回 DataFrame 含所有原始字段 + what-if 列。"""
    trades = [json.loads(l) for l in open(closes_path)]
    ohlc = pd.read_parquet(ohlc_path)
    ohlc.index = pd.to_datetime(ohlc.index)

    rows = []
    for t in trades:
        res = compute_hold_pnl(t, ohlc)
        base = {
            "ticker": t.get("ticker"),
            "strategy": t.get("strategy"),
            "spread_label": t.get("spread_label"),
            "open_date": pd.to_datetime(t.get("open_date")),
            "close_date": pd.to_datetime(t.get("close_date")),
            "net_pnl_actual": float(t.get("net_pnl") or 0),
        }
        base.update(res)
        rows.append(base)
    df = pd.DataFrame(rows)
    return df


def summarize(df: pd.DataFrame, min_dte_at_close: int = 3,
              days_back: Optional[int] = None) -> dict:
    """算总结:多少笔白砍/白亏总额/按方向拆/最冤 10 单。"""
    d = df[df["has_data"] == True].copy()
    if days_back:
        cutoff = d["close_date"].max() - pd.Timedelta(days=days_back)
        d = d[d["close_date"] >= cutoff]
    # 只看提前砍的 (距到期还有 >= min_dte_at_close 天平仓)
    early = d[d["dte_at_close"] >= min_dte_at_close].copy()

    total = {
        "window_days_back": days_back,
        "n_trades_total": len(d),
        "n_early_cut": len(early),
        "actual_pnl_sum": float(early["net_pnl_actual"].sum()),
        "hypothetical_pnl_sum": float(early["hypothetical_pnl"].sum()),
        "regret_sum": float(early["regret"].sum()),
        "n_wrong_cuts": int((early["regret"] < 0).sum()),
        "pct_wrong_cuts": float((early["regret"] < 0).mean() * 100) if len(early) else 0.0,
    }

    by_direction = early.groupby("direction").agg(
        n=("regret", "count"),
        actual=("net_pnl_actual", "sum"),
        hypothetical=("hypothetical_pnl", "sum"),
        regret=("regret", "sum"),
        pct_wrong=("regret", lambda x: (x < 0).mean() * 100),
    ).round(1).to_dict("index")

    # 最冤 10 单 (regret 最负)
    biggest_regret = early.sort_values("regret").head(10)[
        ["ticker", "direction", "spread_label", "open_date", "close_date",
         "dte_at_close", "net_pnl_actual", "hypothetical_pnl", "regret", "exp_close"]
    ]

    # 按 ticker 汇总(only 白砍严重的)
    by_ticker = early.groupby("ticker").agg(
        n=("regret", "count"),
        regret=("regret", "sum"),
    ).sort_values("regret").head(10)

    # 按"砍时相对亏损比例"分桶 — 这是你真正的行为规律:
    # 小赚就砍 / 小亏就砍 / 深亏才砍
    early["cut_pnl_pct_of_debit"] = early["net_pnl_actual"] / (early["debit_per_share"] * 100 * early["qty"])
    bins = [-2.0, -0.7, -0.3, 0.0, 0.5, 2.0]
    labels = ["深亏(<-70%)", "中亏(-70~-30%)", "小亏(-30~0%)", "小赚(0~50%)", "大赚(50%+)"]
    early["loss_bucket"] = pd.cut(early["cut_pnl_pct_of_debit"], bins=bins, labels=labels)
    by_bucket = early.groupby("loss_bucket", observed=True).agg(
        n=("regret", "count"),
        actual=("net_pnl_actual", "sum"),
        hypothetical=("hypothetical_pnl", "sum"),
        regret=("regret", "sum"),
        pct_wrong=("regret", lambda x: (x < 0).mean() * 100),
        avg_regret=("regret", "mean"),
    ).round(1)

    return {
        "total": total,
        "by_direction": by_direction,
        "biggest_regret_trades": biggest_regret,
        "by_ticker_worst": by_ticker,
        "by_loss_bucket": by_bucket,
    }

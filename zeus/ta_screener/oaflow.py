"""TA Screener:异动期权流(Benzinga unusual options activity)→ 每票净多空偏置。

数据:data/market_events/option_activity_recent.parquet(benzinga_market_events 的
oa_archive daemon 每 15 分钟写,滚动 ~7 天)。Benzinga **已预分类** sentiment
(BULLISH/BEARISH/NEUTRAL),cost_basis=该笔权利金($)。

口径:一次读全量、近 N 日按 ticker 聚合 bull/bear 权利金 → signed bias(+ = 偏多)。
**修正不主导**(同 opt_bias):封顶 ±OA_CAP,且需最小总权利金 + 事件数门槛(单笔小单
不算数);净%(net_prem / 总prem)线性映射,±50% 打满。fail-soft 返回 {}。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OA_PARQUET = REPO_ROOT / "data" / "market_events" / "option_activity_recent.parquet"

OA_CAP = 5.0            # 评分修正封顶 ±5(比 opt_bias ±10 小:OA 噪声更大、只做佐证)
MIN_PREMIUM = 250_000  # 近 N 日总权利金门槛(<$25万视为信号不足 → total=0)
MIN_EVENTS = 5         # 事件数门槛


def oa_bias_map(days_back: int = 5) -> dict:
    """读 parquet 一次,返回 {TICKER: {n,bull_prem,bear_prem,net_prem,net_pct,bull_n,bear_n,total,qualified}}。

    total = signed 评分修正(+ 偏多),按 net_pct 映射封顶 ±OA_CAP;未达门槛=0。fail-soft。
    """
    try:
        import pandas as pd
        if not OA_PARQUET.exists():
            return {}
        df = pd.read_parquet(OA_PARQUET, columns=["ticker", "date", "sentiment", "cost_basis"])
    except Exception:
        return {}
    if df is None or not len(df):
        return {}
    try:
        df["date"] = df["date"].astype(str).str[:10]
        cutoff = (_dt.date.today() - _dt.timedelta(days=days_back)).isoformat()
        df = df[df["date"] >= cutoff]
        if not len(df):
            return {}
        df["cb"] = pd.to_numeric(df["cost_basis"], errors="coerce").fillna(0.0)
        s = df["sentiment"].astype(str).str.upper()
        df["is_bull"] = s.str.contains("BULL")
        df["is_bear"] = s.str.contains("BEAR")
        df["bull_cb"] = df["cb"].where(df["is_bull"], 0.0)
        df["bear_cb"] = df["cb"].where(df["is_bear"], 0.0)
        df["tk"] = df["ticker"].astype(str).str.upper()
        g = df.groupby("tk").agg(
            n=("cb", "size"),
            bull=("bull_cb", "sum"),
            bear=("bear_cb", "sum"),
            bull_n=("is_bull", "sum"),
            bear_n=("is_bear", "sum"),
        )
        out = {}
        for tk, row in g.iterrows():
            bull, bear, n = float(row["bull"]), float(row["bear"]), int(row["n"])
            tot = bull + bear
            net = bull - bear
            net_pct = (net / tot * 100.0) if tot > 0 else 0.0
            if tot >= MIN_PREMIUM and n >= MIN_EVENTS:
                total = max(-OA_CAP, min(OA_CAP, net_pct / 50.0 * OA_CAP))  # ±50% net → 满分
            else:
                total = 0.0
            out[str(tk)] = {
                "n": n, "bull_prem": round(bull), "bear_prem": round(bear),
                "net_prem": round(net), "net_pct": round(net_pct, 1),
                "bull_n": int(row["bull_n"]), "bear_n": int(row["bear_n"]),
                "total": round(total, 2),
                "qualified": bool(tot >= MIN_PREMIUM and n >= MIN_EVENTS),
            }
        return out
    except Exception:
        return {}


if __name__ == "__main__":
    m = oa_bias_map()
    print("tickers:", len(m))
    for tk in ("NVDA", "MSFT", "AAPL", "TSLA"):
        print(tk, m.get(tk))

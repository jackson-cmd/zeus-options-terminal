"""薄封装层 —— 把仓库里已有的数据源包成 4 层要用的简单函数。本模块只做 IO,不做判断
(判断逻辑在 layerN_*.py 里,拿到这里返回的数据当纯输入)。

设计原始文档假设的路径,核对后有两处跟实际不一样,这里记下来避免以后有人"照文档找不到
文件"又重新踩一遍坑:

  · VIX 不在 data/daily_ohlc.parquet 里(该 parquet 719 只票没有 ^VIX)。真实来源是
    options_analyzer/checkup/regime.py 的 market_regime()['vix'] —— 它自己也是
    yfinance 小拉 + 18h 本地缓存,这里直接复用,不重复起一个 yfinance 调用。
  · 财报日期不在 data/finviz_ai/(那个路径本身不存在;实际路径是
    benzinga_digest/finviz_ai/,核实过 —— 里面每条都是 {ticker, crawled_at, ai(几乎
    总是 null), news:[...]},从未带过财报日期字段)。真正的财报日历是
    trade_journal/data/calendar_cache.json(trade_journal/calendar_events.py 从 Nasdaq
    抓的,只覆盖 data/watchlist.txt 上的票)。ETF(SPY/QQQ 等)和不在这份清单上的票查
    不到是预期行为 —— Layer 3 按设计走 unknown → 不阻断,不是 bug。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
_BENZINGA_SRC = REPO_ROOT / "benzinga_digest" / "src"
for _p in (REPO_ROOT, _BENZINGA_SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

OPTIONS_VOL_HISTORY = REPO_ROOT / "data" / "options_vol" / "history.parquet"
CALENDAR_CACHE = REPO_ROOT / "trade_journal" / "data" / "calendar_cache.json"


# ── 日线 OHLC(data/daily_ohlc.parquet, 经 heatmap/daily_store.py) ──
def get_ohlc(ticker: str) -> Optional[pd.DataFrame]:
    """该 ticker 的全量日线 OHLCV(Open/High/Low/Close/Volume, tz-naive DatetimeIndex,
    只含已完成的交易日)。找不到 → None(由调用方决定 fail 还是跳过)。"""
    try:
        from heatmap import daily_store
        return daily_store.get_ohlc(ticker.upper())
    except Exception:
        return None


# ── VIX(复用 regime.py 已有的 yfinance 拉取 + 缓存) ──
def get_vix() -> Optional[float]:
    try:
        from options_analyzer.checkup import regime
        v = regime.market_regime().get("vix")
        return float(v) if v is not None else None
    except Exception:
        return None


# ── IV Rank(data/options_vol/history.parquet) ──
def get_ivr(ticker: str) -> dict:
    """{ivr, iv, asof} 取该 ticker 最新一行(按 date 排序取最后一条)。
    查不到 → 全 None(Layer 3 按"数据缺失不阻断"处理)。"""
    out: dict = {"ivr": None, "iv": None, "asof": None}
    try:
        df = pd.read_parquet(OPTIONS_VOL_HISTORY, columns=[
            "ticker", "date", "weightedImpliedVolatility", "impliedVolatilityRank1y"])
        sub = df[df["ticker"] == ticker.upper()].sort_values("date")
        if len(sub):
            row = sub.iloc[-1]
            ivr = row["impliedVolatilityRank1y"]
            iv = row["weightedImpliedVolatility"]
            out["ivr"] = float(ivr) if pd.notna(ivr) else None
            out["iv"] = float(iv) if pd.notna(iv) else None
            out["asof"] = str(row["date"])
    except Exception:
        pass
    return out


# ── 财报日期(trade_journal/data/calendar_cache.json) ──
def get_earnings(ticker: str, today: Optional[_dt.date] = None) -> dict:
    """该 ticker 在 today 当天或之后最近一次财报。只覆盖 data/watchlist.txt 上的票 ——
    ETF 和清单外的票查不到是预期行为(见模块头 docstring),返回 days_to_earnings=None。"""
    today = today or _dt.date.today()
    out: dict = {"earnings_date": None, "days_to_earnings": None, "source": None}
    try:
        cache = json.loads(CALENDAR_CACHE.read_text(encoding="utf-8"))
        rows = [e for e in (cache.get("earnings") or [])
                if (e.get("ticker") or "").upper() == ticker.upper()]
        future = sorted(
            (e for e in rows if _dt.date.fromisoformat(e["date"]) >= today),
            key=lambda e: e["date"])
        if future:
            ed = _dt.date.fromisoformat(future[0]["date"])
            out["earnings_date"] = future[0]["date"]
            out["days_to_earnings"] = (ed - today).days
            out["source"] = "trade_journal_calendar_cache"
    except Exception:
        pass
    return out


# ── benzinga digest(今日) ──
def get_digest_today(date_str: str) -> list:
    """当天 digest item 列表(无则 [])。date_str 形如 '2026-07-30'。"""
    try:
        from benzinga_mcp import digest_store
        return digest_store.load_day(date_str) or []
    except Exception:
        return []


# ── risk_radar(话题计数 / 饱和度 / 基线)——薄封装,函数本身仍是纯函数,只是把
#    "去哪 import"这件事集中在这一个文件里 ──
def risk_radar_baseline() -> dict:
    from options_analyzer.checkup import risk_radar
    return risk_radar.load_data()


def count_topics(items: list) -> dict:
    from options_analyzer.checkup import risk_radar
    return risk_radar.count_topics(items)


def regime_saturation(items: list, baseline: Optional[dict] = None) -> dict:
    from options_analyzer.checkup import risk_radar
    return risk_radar.regime_saturation(items, data=baseline)

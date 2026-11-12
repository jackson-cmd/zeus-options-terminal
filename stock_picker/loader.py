"""Layer T1 —— Barchart screener CSV 加载 + 类型转换 + 数据质量清洗。

只做 IO + 强制类型,不做任何交易判断(判断逻辑在 filters.py / ema_touch.py 里)。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from . import config

# 百分比字符串列("+18.70%" / "-9.39%" / "N/A") → 百分点数值(18.70 / -9.39 / NaN)
PCT_COLS = [
    "%Change", "% Institutional", "Short Float",
    "1M %Chg", "3M %Chg", "52W %Chg", "%Exp Move", "14D ADRP", "14D ATRP",
]
# 普通数值字符串列("100.07" / "N/A") → float(NaN)
NUM_COLS = [
    "Latest", "Beta", "20D EMA", "50D EMA", "100D EMA", "200D EMA",
    "Wtd Alpha", "Volume", "Options Vol", "14D RSI",
]
# Barchart 自家趋势信号("100% Sell" / "Hold" / "50% Buy" / N/A) —— 保持字符串
TREND_COLS = ["Short Term", "Medium Term", "Long Term"]


def resolve_default_csv_source() -> Path:
    """找"这次该用的" Barchart CSV 源文件,不依赖硬编码的今天日期文件名一直存在:
      1. PRIMARY INPUT 约定的文件名如果还在 Downloads 里 → 用它
      2. 否则 Downloads 里同前缀最新的一个 stocks-screener-base-*.csv
      3. 否则退回已经拷贝过的本地副本(作者可能把 Downloads 原件删了)
    三个都没有 → 抛 FileNotFoundError,由调用方(cli.py)决定怎么提示作者。
    """
    if config.DEFAULT_CSV_SOURCE.exists():
        return config.DEFAULT_CSV_SOURCE
    candidates = sorted(
        config.DOWNLOADS_DIR.glob("stocks-screener-base-*.csv"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    if config.LOCAL_CSV_COPY.exists():
        return config.LOCAL_CSV_COPY
    raise FileNotFoundError(
        f"找不到 Barchart CSV: {config.DEFAULT_CSV_SOURCE} 不存在,"
        f"{config.DOWNLOADS_DIR} 下也没有 stocks-screener-base-*.csv,"
        f"本地副本 {config.LOCAL_CSV_COPY} 也不存在。用 --csv 显式指定路径。")


def ensure_local_copy(source: Path) -> Path:
    """把 source 拷进 stock_picker/data/barchart_base.csv(仅当本地副本还不存在时,
    "on first run"语义)—— 确保就算作者之后把 Downloads 里的原始文件删了,这次跑过
    的输入还能复现。传 --csv 显式指定路径时不走这个函数(见 cli.py)。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not config.LOCAL_CSV_COPY.exists():
        shutil.copy2(source, config.LOCAL_CSV_COPY)
        print(f"已拷贝 Barchart CSV 到本地副本: {config.LOCAL_CSV_COPY}")
    return config.LOCAL_CSV_COPY


def _pct_to_float(series: pd.Series) -> pd.Series:
    """'+18.70%'/'−9.39%'/'0.00%' → 18.70/-9.39/0.0(百分点数值,不是小数)。"""
    s = series.astype(str).str.strip()
    s = s.str.replace("%", "", regex=False).str.replace("+", "", regex=False)
    return pd.to_numeric(s, errors="coerce")


def load_csv(path: Path) -> pd.DataFrame:
    """读 Barchart screener CSV → 类型强制转换后的 DataFrame。
    'N/A'(pandas 默认 na_values 已含,这里显式声明只为可读性)→ NaN;
    百分比列转 float(百分点数值);Earnings Date 转 datetime(NaT=missing)。"""
    df = pd.read_csv(path, na_values=["N/A"], keep_default_na=True)
    df.columns = [c.strip() for c in df.columns]

    for c in NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in PCT_COLS:
        if c in df.columns:
            df[c] = _pct_to_float(df[c])
    if "Earnings Date" in df.columns:
        df["Earnings Date"] = pd.to_datetime(df["Earnings Date"], errors="coerce")
    for c in TREND_COLS:
        if c in df.columns:
            df[c] = df[c].astype("string")
    if "Symbol" in df.columns:
        df["Symbol"] = df["Symbol"].astype(str).str.strip().str.upper()
    return df


def clean(df: pd.DataFrame, verbose: bool = False) -> tuple[pd.DataFrame, dict]:
    """Layer T1 剩余步骤:丢 Latest 缺失 / 全部 EMA 是 0-或-NaN 的行(Barchart glitch,
    如全部四条 EMA 都是 0 的行)。返回 (清洗后 df, {阶段名: 行数})。"""
    counts = {"T1_initial": len(df)}

    df = df[df["Latest"].notna()].copy()
    counts["T1_after_drop_missing_latest"] = len(df)
    if verbose:
        print(f"[T1] Latest 缺失: 丢至 {len(df)} 行")

    ema_cols = [c for c in config.EMA_COLS if c in df.columns]
    is_glitch = pd.Series(True, index=df.index)
    for c in ema_cols:
        is_glitch &= df[c].fillna(0).eq(0)
    df = df[~is_glitch].copy()
    counts["T1_after_drop_ema_glitch"] = len(df)
    if verbose:
        print(f"[T1] 全部EMA为0/NaN(Barchart glitch): 丢至 {len(df)} 行")

    return df.reset_index(drop=True), counts


def build_local_frame() -> pd.DataFrame:
    """No Barchart export? Build the same columns from local daily candles.

    Universe = the heatmap universe minus ETFs; candles = the local daily store
    (tastytrade DXLink or Yahoo Finance); earnings dates = the heatmap earnings
    cache. Barchart-only fields (options volume, institutional %, Barchart trend
    signals) are left empty and marked with `_local_source`, so filters that need
    them treat "unknown" as "keep" instead of silently dropping every ticker.
    Weighted alpha is approximated Barchart-style: the one-year return with more
    weight on recent days.
    """
    import numpy as np
    from heatmap import daily_store, metadata
    try:
        from heatmap import earnings as _earn
        emap = _earn.get_map()
    except Exception:
        emap = {}
    etfs = metadata.etf_set()
    spy = daily_store.get_ohlc("SPY")
    spy_ret = spy["Close"].pct_change() if spy is not None and len(spy) > 30 else None
    rows = []
    for t in metadata.tickers():
        if t in etfs:
            continue
        df = daily_store.get_ohlc(t)
        if df is None or len(df) < 60:
            continue
        c, h, lo, v = df["Close"].astype(float), df["High"].astype(float), df["Low"].astype(float), df["Volume"]
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])

        def chg(n):
            return (last / float(c.iloc[-n - 1]) - 1) * 100 if len(c) > n else np.nan
        delta = c.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = float(100 - 100 / (1 + gain.iloc[-1] / loss.iloc[-1])) if loss.iloc[-1] > 0 else 100.0
        tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
        logret = np.log(c).diff().dropna().tail(252)
        w = np.linspace(0.0, 2.0, len(logret)) if len(logret) else np.array([])
        wtd_alpha = float((np.exp((logret.values * w).sum()) - 1) * 100) if len(logret) > 60 else np.nan
        beta = np.nan
        if spy_ret is not None:
            pair = pd.concat([c.pct_change(), spy_ret], axis=1, keys=["s", "m"]).dropna().tail(252)
            if len(pair) > 60 and pair["m"].var() > 0:
                beta = float(pair["s"].cov(pair["m"]) / pair["m"].var())
        ed = (emap.get(t) or {}).get("date")
        rows.append({
            "Symbol": t, "Latest": last, "%Change": (last / prev - 1) * 100,
            "20D EMA": float(c.ewm(span=20, adjust=False).mean().iloc[-1]),
            "50D EMA": float(c.ewm(span=50, adjust=False).mean().iloc[-1]),
            "100D EMA": float(c.ewm(span=100, adjust=False).mean().iloc[-1]),
            "200D EMA": float(c.ewm(span=200, adjust=False).mean().iloc[-1]),
            "14D RSI": rsi, "1M %Chg": chg(21), "3M %Chg": chg(63), "52W %Chg": chg(252),
            "14D ATRP": float(tr.tail(14).mean() / last * 100),
            "14D ADRP": float(((h - lo) / c).tail(14).mean() * 100),
            "Wtd Alpha": wtd_alpha, "Beta": beta, "Volume": float(v.iloc[-1]),
            "Options Vol": np.nan, "% Institutional": np.nan, "Short Float": np.nan, "%Exp Move": np.nan,
            "Short Term": pd.NA, "Medium Term": pd.NA, "Long Term": pd.NA,
            "Earnings Date": pd.to_datetime(ed, errors="coerce") if ed else pd.NaT,
            "_local_source": True,
        })
    if not rows:
        raise FileNotFoundError("No local daily candles yet — open the 🎯 Picker tab once so they download, then retry.")
    out = pd.DataFrame(rows)
    for col in TREND_COLS:
        out[col] = out[col].astype("string")
    return out

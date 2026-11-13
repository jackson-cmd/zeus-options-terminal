"""价格/收益数据层——给相关性引擎喂【日对数收益】。

缓存优先(零网络):读 ta_screener 的 ohlc_cache.parquet(389 票×5年);
候选不在缓存就走 yfinance 兜底并写回。**别裸 `import data`**(会撞仓库根 data/),
统一走 `from ta_screener import data`(包限定,安全)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 保证无论 cwd 在 options_analyzer/ 还是 repo 根,都能导入 ta_screener 包
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ta_screener import data as _D  # noqa: E402  (包限定导入,不与 repo 根 data/ 冲突)

from . import rules


def expand_tickers(raw) -> list[str]:
    """把作者输入('AAPL MSFT', 'MAG7', ['nvda'])规整成大写去重 ticker 列表,展开别名。"""
    if isinstance(raw, str):
        toks = raw.replace(",", " ").split()
    else:
        toks = list(raw or [])
    out, seen = [], set()
    for tok in toks:
        sym = str(tok).strip().upper().replace(".", "-")
        if not sym:
            continue
        for t in rules.TICKER_ALIASES.get(sym, [sym]):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def load_closes(tickers, allow_fetch: bool = True) -> dict[str, pd.Series]:
    """{ticker: Close Series(DatetimeIndex)}。缓存优先;缺票且 allow_fetch 时 yfinance 兜底。"""
    tickers = list(dict.fromkeys(tickers))
    try:
        cache = _D._load_cache()
    except Exception:
        cache = {}
    out, missing = {}, []
    for t in tickers:
        df = cache.get(t)
        if df is not None and "Close" in df.columns and len(df) >= 60:
            out[t] = df["Close"].astype(float)
        else:
            missing.append(t)
    if missing and allow_fetch:
        try:
            fetched = _D.download_history(missing)  # 缓存优先 + yfinance 兜底 + 写回
        except Exception:
            fetched = {}
        for t in missing:
            d = fetched.get(t)
            if d is not None and "Close" in d.columns and len(d) >= 60:
                out[t] = d["Close"].astype(float)
    return out


def returns_frame(tickers, allow_fetch: bool = True) -> pd.DataFrame:
    """对齐到一张日对数收益表(index=日期, columns=ticker;缺处为 NaN)。

    相关性按列两两 dropna 对齐——pandas 自动按日期对齐,无需手动。
    """
    closes = load_closes(tickers, allow_fetch=allow_fetch)
    cols = {}
    for t, s in closes.items():
        r = np.log(s / s.shift(1))
        cols[t] = r
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).replace([np.inf, -np.inf], np.nan)
    return df


def available(tickers, allow_fetch: bool = False) -> set[str]:
    """哪些 ticker 拿得到历史(默认只查缓存,不触发下载)。"""
    return set(load_closes(tickers, allow_fetch=allow_fetch).keys())

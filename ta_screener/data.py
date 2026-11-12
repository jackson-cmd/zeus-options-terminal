"""TA Screener 数据层:heatmap universe 解析 + yfinance 批量日线 + 周/月重采样。"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
# TA screener 专属 universe(作者精选 392 票,抄底为主);分类标签从 heatmap 借。
UNIVERSE_FILE = REPO_ROOT / "data" / "ta_screener_universe.txt"
# 专属池:从 universe 筛掉「不会动/太便宜/太贵」的票(build_pool.py 生成);扫描只在池里跑。
POOL_FILE = REPO_ROOT / "data" / "ta_screener_pool.txt"
_CATEGORY_SOURCE = REPO_ROOT / "data" / "heatmap_universe.txt"
# 粗粒度 GICS sector(Technology/Industrials/…)从 heatmap metadata 借,供快速筛选。
_SECTOR_SOURCE = REPO_ROOT / "data" / "heatmap" / "metadata.json"

CHUNK = 120          # yfinance 每批票数
# 5 年日线:SSP EMA 胜率信号要凑样本(bull min N=20,X200 一年才几次),
# 同时让概率 edge 的条件 cell 样本更足。代价是下载慢一点(~2-3 分钟)。
PERIOD = "5y"
MIN_BARS = 60

# ── 本地 OHLC 缓存(作者:历史数据存本地,别每次全量请求)────────────────────
# 历史 K 线不变 → 存 parquet;每次只拉 DELTA_PERIOD 增量合并,全量下载只在
# 冷启动/缓存太陈旧/复权校验失败时发生。图表 spark 同源,自动受益。
CACHE_FILE = REPO_ROOT / "data" / "ta_screener" / "ohlc_cache.parquet"   # gitignored
DELTA_PERIOD = "21d"   # 增量窗口(~14 个交易日,重叠校验复权 + 补新bar)
OVERLAP_TOL = 0.005    # 重叠日 Close 偏差 >0.5% → 拆股/分红复权变了 → 该票全量重拉
STALE_DAYS = 12        # 缓存最新 bar 落后超过此(日历日)→ 增量窗口盖不住,全量重拉
MAX_BARS_KEEP = 1330   # ~5y+缓冲,防缓存无限膨胀


def _parse_file(path: Path):
    """单行逗号分隔:`###分类名` 标记 + `EXCHANGE:TICKER`。
    返回 (tickers 有序去重, {ticker: 分类})。yahoo 符号化:BRK.B→BRK-B。"""
    raw = Path(path).read_text(encoding="utf-8").strip()
    tickers, cat_map, seen, cat = [], {}, set(), ""
    for tok in raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("###"):
            cat = tok.lstrip("#").strip()
            continue
        sym = tok.split(":")[-1].strip().upper().replace(".", "-")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        tickers.append(sym)
        cat_map[sym] = cat
    return tickers, cat_map


def parse_universe(path: Path = UNIVERSE_FILE):
    """解析 TA universe;对没分类的票从 heatmap_universe 借分类标签(纯展示用)。"""
    tickers, cat_map = _parse_file(path)
    missing = [t for t in tickers if not cat_map.get(t)]
    if missing and _CATEGORY_SOURCE.exists():
        try:
            _, hm_cat = _parse_file(_CATEGORY_SOURCE)
            for t in missing:
                if hm_cat.get(t):
                    cat_map[t] = hm_cat[t]
        except Exception:
            pass
    return tickers, cat_map


def load_pool() -> set | None:
    """读专属池(build_pool.py 生成)→ 合格 ticker 集合;无文件 → None(退回全 universe)。
    yahoo 符号化与 universe 对齐(BRK.B→BRK-B)。"""
    if not POOL_FILE.exists():
        return None
    try:
        raw = POOL_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    out = set()
    for tok in raw.replace("\n", ",").split(","):
        sym = tok.strip().split(":")[-1].strip().upper().replace(".", "-")
        if sym and not sym.startswith("#"):
            out.add(sym)
    return out or None


def sector_map() -> dict:
    """{ticker: GICS sector}(借 heatmap metadata.json;ETF 多为 'Unknown',缺则 '')。
    纯本地读文件、零网络,fail-soft 返回空 dict。yahoo 符号化与 universe 对齐。"""
    try:
        import json
        m = json.loads(_SECTOR_SOURCE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for t, v in (m or {}).items():
        sym = str(t).strip().upper().replace(".", "-")
        out[sym] = ((v or {}).get("sector") or "").strip()
    return out


def industry_map() -> dict:
    """{ticker: 行业 industry}(借 heatmap metadata.json;比 sector 细,如 'Communication Equipment')。
    纯本地读文件、零网络,fail-soft 返回空 dict。yahoo 符号化与 universe 对齐。"""
    try:
        import json
        m = json.loads(_SECTOR_SOURCE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for t, v in (m or {}).items():
        sym = str(t).strip().upper().replace(".", "-")
        out[sym] = ((v or {}).get("industry") or "").strip()
    return out


def _fetch_batch(yf, batch, out, period, min_bars: int = MIN_BARS):
    """拉一批 → 把够长的票塞进 out。返回本批新增数。throttle 时返回的 df 常缺票
    (无异常),故由调用方据 out 缺口决定二次重试。"""
    df = None
    for attempt in (1, 2):
        try:
            df = yf.download(batch, period=period, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
            break
        except Exception:
            df = None
            if attempt == 1:
                time.sleep(3)
    if df is None or df.empty:
        return 0
    added = 0
    for t in batch:
        if t in out:
            continue
        try:
            sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            cols = ["Open", "High", "Low", "Close"] + (["Volume"] if "Volume" in sub.columns else [])
            sub = sub[cols].dropna(subset=["Open", "High", "Low", "Close"])
            if len(sub) >= min_bars:
                out[t] = sub
                added += 1
        except Exception:
            continue
    return added


def _download_full(tickers, progress_cb=None, period: str = PERIOD, min_bars: int = MIN_BARS):
    """yfinance 分块批量下载日线(原全量路径)。主跑 120/批;**失败票二次重试**
    (小批 50 + 退避),专治 Yahoo 限流导致整批缺票(无异常静默丢)。"""
    import yfinance as yf
    out = {}
    n_chunks = (len(tickers) + CHUNK - 1) // CHUNK
    for ci in range(n_chunks):
        batch = tickers[ci * CHUNK:(ci + 1) * CHUNK]
        _fetch_batch(yf, batch, out, period, min_bars)
        if progress_cb:
            progress_cb(ci + 1, n_chunks, f"已下载 {len(out)}/{len(tickers)} 票({ci+1}/{n_chunks} 批)")
    # 二次/三次重试缺失的票(限流恢复后小批补回),退避递增
    for rnd, (small, wait) in enumerate(((50, 8), (25, 20)), start=1):
        missing = [t for t in tickers if t not in out]
        if not missing:
            break
        if progress_cb:
            progress_cb(n_chunks, n_chunks, f"重试缺失 {len(missing)} 票(第{rnd}轮,等{wait}s)…")
        time.sleep(wait)
        for i in range(0, len(missing), small):
            _fetch_batch(yf, missing[i:i + small], out, period, min_bars)
            time.sleep(1)
    return out


def _load_cache() -> dict:
    """读本地 OHLC 缓存 → {ticker: DataFrame}。坏文件/缺文件 → {}(fail-soft)。"""
    if not CACHE_FILE.exists():
        return {}
    try:
        df = pd.read_parquet(CACHE_FILE)
        return {t: sub.drop(columns=["ticker"]).sort_index()
                for t, sub in df.groupby("ticker")}
    except Exception:
        return {}


def _save_cache(hist: dict) -> None:
    """原子写回缓存(tmp+replace);每票只留最近 MAX_BARS_KEEP 根。失败不抛(fail-soft)。"""
    try:
        parts = []
        for t, df in hist.items():
            sub = df.tail(MAX_BARS_KEEP).copy()
            sub["ticker"] = t
            parts.append(sub)
        if not parts:
            return
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        pd.concat(parts).to_parquet(tmp)
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


def download_history(tickers, progress_cb=None, period: str = PERIOD, use_cache: bool = True):
    """日线历史,**本地缓存 + 增量更新**(作者:历史数据存本地,别每次全量请求):
    1. 有缓存且最新 bar 落后 ≤STALE_DAYS 的票 → 只批量拉 DELTA_PERIOD(~21天)增量;
       用窗口内**最老的 3 个重叠日 Close** 校验复权(拆股/分红会回调整段历史,
       偏差 >OVERLAP_TOL → 该票转全量重拉),校验过 → 旧缓存 + 新窗口合并(同日新覆盖旧)。
    2. 无缓存 / 太陈旧 / 校验失败 / 增量没拉到 的票 → 走原全量路径(含限流重试)。
    3. 全量也失败的票 → 用旧缓存兜底(数据旧一点好过整票丢失)。
    缓存 = data/ta_screener/ohlc_cache.parquet(gitignored,~几MB);任何缓存层异常
    → 整体退回全量路径。period 非默认(调用方自定窗口)时不走缓存。"""
    if not use_cache or period != PERIOD:
        return _download_full(tickers, progress_cb, period)
    try:
        cache = _load_cache()
    except Exception:
        cache = {}
    today = pd.Timestamp.now().normalize()
    delta_ok, full_list = {}, []
    for t in tickers:
        c = cache.get(t)
        if c is None or len(c) < MIN_BARS or (today - c.index[-1].normalize()).days > STALE_DAYS:
            full_list.append(t)
        else:
            delta_ok[t] = c
    out = {}
    if delta_ok:
        if progress_cb:
            progress_cb(0, 1, f"增量更新 {len(delta_ok)} 票(本地缓存命中)…")
        try:
            delta = _download_full(list(delta_ok), progress_cb=None, period=DELTA_PERIOD, min_bars=1)
        except Exception:
            delta = {}
        for t, c in delta_ok.items():
            d = delta.get(t)
            if d is None or d.empty:
                full_list.append(t)                    # 增量没拉到 → 全量再试(最后还有缓存兜底)
                continue
            common = c.index.intersection(d.index).sort_values()
            if len(common) == 0:
                full_list.append(t)
                continue
            chk = common[:3]                           # 窗口内最老的重叠日,对复权最敏感
            a = c.loc[chk, "Close"].to_numpy(dtype=float)
            b = d.loc[chk, "Close"].to_numpy(dtype=float)
            if np.any(np.abs(a - b) > np.abs(b) * OVERLAP_TOL + 1e-9):
                full_list.append(t)                    # 复权变了(拆股/分红)→ 全量重拉
                continue
            merged = pd.concat([c[c.index < d.index[0]], d])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            out[t] = merged.tail(MAX_BARS_KEEP)
    if full_list:
        full_list = list(dict.fromkeys(full_list))     # 去重保序
        if progress_cb:
            progress_cb(0, 1, f"全量下载 {len(full_list)} 票(新票/复权变化/缓存陈旧)…")
        out.update(_download_full(full_list, progress_cb, period))
        for t in full_list:                            # 全量也失败 → 旧缓存兜底
            if t not in out and t in cache and len(cache[t]) >= MIN_BARS:
                out[t] = cache[t]
    cache.update(out)                                  # 写回(保留不在本次 universe 的旧票)
    _save_cache(cache)
    return {t: out[t] for t in tickers if t in out}


def to_arrays(df: pd.DataFrame) -> dict:
    iso = df.index.isocalendar()  # ISO 年×100+周 → SSP 周桶(与 Pine weekofyear 同粒度)
    return {
        "open": df["Open"].to_numpy(dtype=float),
        "high": df["High"].to_numpy(dtype=float),
        "low": df["Low"].to_numpy(dtype=float),
        "close": df["Close"].to_numpy(dtype=float),
        "volume": (df["Volume"].fillna(0).to_numpy(dtype=float) if "Volume" in df.columns else None),
        "week_key": (iso["year"].to_numpy(dtype=int) * 100 + iso["week"].to_numpy(dtype=int)),
        "month_key": (df.index.year.to_numpy(dtype=int) * 100 + df.index.month.to_numpy(dtype=int)),
    }


def resample_ohlc(df: pd.DataFrame, rule: str) -> dict:
    """日线 → 周(W-FRI)/ 月(ME)OHLC 数组。"""
    agg = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return to_arrays(agg)


def spark_series(df: pd.DataFrame, n: int = 520):
    """详情图数据:最近 n 根 (date, close)。~520根≈2年,供前端短/中/长窗口切换。"""
    sub = df["Close"].tail(n)
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
        "close": [round(float(v), 3) for v in sub.to_numpy()],
    }


def ohlc_chart(df: pd.DataFrame, n: int = 126):
    """📊 图视图自绘 candle 用:最近 n 根(~6个月)OHLC+量 + SMA50/200
    (SMA 从全序列算再切片,保证 SMA200 在窗口起点就有效)。"""
    c = df["Close"]
    sma50 = c.rolling(50).mean().tail(n)
    sma200 = c.rolling(200).mean().tail(n)
    sub = df.tail(n)

    def _a(s, nd=2):
        return [None if (v != v) else round(float(v), nd) for v in s.to_numpy()]

    has_vol = "Volume" in sub.columns
    return {
        "d": [d.strftime("%m-%d") for d in sub.index],
        "o": _a(sub["Open"]), "h": _a(sub["High"]), "l": _a(sub["Low"]), "c": _a(sub["Close"]),
        "v": [int(v) if (v == v) else 0 for v in (sub["Volume"].to_numpy() if has_vol else [0] * len(sub))],
        "sma50": _a(sma50), "sma200": _a(sma200),
    }

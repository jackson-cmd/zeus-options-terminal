"""EOD 派生的逐票 Put/Call volume & OI 聚合(免费,无需单独爬)。

洞察:EOD 爬虫每天落 `eod_crawler/data_eod/eod_chain.parquet`,逐 strike 含
call/put 的 Volume / OpenInterest / ImpliedVolatility。所以每票的 put/call
volume ratio、OI ratio、量加权 IV 可以直接从这份链「免费」派生,不必再单独爬。

本模块对 parquet 做**列投影 + 按 DataDate 过滤**(parquet 12M+ 行 ~560MB,
绝不全读 17 列),按 Symbol 聚合最近交易日(及最近 N≈20 个交易日的小历史画
sparkline),产出 `data/options_vol/eod_pc.json`。

纯函数(`aggregate_day` / `aggregate_window`)+ 一个 build 入口(`build`)。
读法参考 `ta_screener/backtest.py:load_chain`,但那里全读用于回测;此处只取
派生 P/C 需要的 6 列且按日期过滤,内存可控。

schema(eod_chain.parquet,见 eod_crawler/config.py CSV_COLUMNS):
  Symbol, ExpirationDate, PutCall(call/put), StrikePrice, Volume(int),
  OpenInterest(int), UnderlyingPrice, ImpliedVolatility, Delta/Gamma/Vega/Theta,
  DataDate(YYYY-MM-DD)。DTE>90 已在爬虫端裁掉,本模块不再管。
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import math as _math
from pathlib import Path as _Path
from typing import Any, Optional

# 仅聚合 P/C 需要的列 —— 列投影把 17 列砍到 6 列,大幅降内存。
_PROJ_COLS = [
    "Symbol",
    "PutCall",
    "Volume",
    "OpenInterest",
    "ImpliedVolatility",
    "DataDate",
]

# 默认小历史窗口(交易日数);画 sparkline 用。
DEFAULT_HISTORY_DAYS = 20

_PKG_DIR = _Path(__file__).resolve().parent          # options_analyzer/options_vol
_REPO_ROOT = _PKG_DIR.parent.parent                   # 仓库根
# EOD 链路径:repo eod_crawler 优先 → Downloads 旧位置兜底(与 backtest.py 对齐)
_EOD_CHAIN_REPO = _REPO_ROOT / "eod_crawler" / "data_eod" / "eod_chain.parquet"
_EOD_CHAIN_DOWNLOADS = (
    _Path(__file__).resolve().parents[2] / "data" / "eod_chain.parquet"
)
_DEFAULT_OUT = _REPO_ROOT / "data" / "options_vol" / "eod_pc.json"


def chain_path() -> _Path:
    """解析 eod_chain.parquet 位置(repo 内优先,Downloads 兜底)。"""
    for p in (_EOD_CHAIN_REPO, _EOD_CHAIN_DOWNLOADS):
        if p.exists():
            return p
    return _EOD_CHAIN_REPO


# ── 数值清洗(NaN/None/inf → None,round 给 JSON 用)─────────────────────────
def _round_or_none(v: Any, ndigits: int) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if _math.isnan(f) or _math.isinf(f):
        return None
    return round(f, ndigits)


def _ratio(num: float, den: float, ndigits: int = 3) -> Optional[float]:
    """num/den;分母为 0(或非有限)→ None(避免 inf/除零)。"""
    if not den or not _math.isfinite(den):
        return None
    try:
        return round(num / den, ndigits)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def list_trading_dates(path: Optional[_Path] = None) -> list[str]:
    """只读 DataDate 一列(最省),返回升序去重的 YYYY-MM-DD 列表。"""
    import pandas as pd

    p = path or chain_path()
    dd = pd.read_parquet(p, columns=["DataDate"])
    s = dd["DataDate"].astype(str).str.slice(0, 10)
    return sorted(s.dropna().unique().tolist())


def load_window(dates: list[str], path: Optional[_Path] = None):
    """列投影 + DataDate 谓词下推,只读给定交易日的 6 列。返回清洗后的 DataFrame。

    `Volume` / `OpenInterest` 可能为 null,这里统一 fillna(0)。
    `PutCall` 归一为小写。`DataDate` 截到 10 位。
    """
    import pandas as pd

    p = path or chain_path()
    if not dates:
        return pd.DataFrame(columns=_PROJ_COLS)
    # parquet filters 用集合做 in 谓词(pyarrow 谓词下推;dates 是字符串,直接比较)
    df = pd.read_parquet(
        p, columns=_PROJ_COLS, filters=[("DataDate", "in", set(dates))]
    )
    if not len(df):
        return df
    df["DataDate"] = df["DataDate"].astype(str).str.slice(0, 10)
    df["PutCall"] = df["PutCall"].astype(str).str.lower()
    # Volume / OI 可能为 null → 0;数值化以便 sum / 加权
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0)
    df["OpenInterest"] = pd.to_numeric(df["OpenInterest"], errors="coerce").fillna(0.0)
    df["ImpliedVolatility"] = pd.to_numeric(df["ImpliedVolatility"], errors="coerce")
    return df


# ── 纯聚合:给定「某一天的子帧」→ {symbol: 指标 dict} ───────────────────────
def aggregate_day(day_df) -> dict[str, dict]:
    """对单个 DataDate 的帧按 Symbol 聚合 P/C volume/OI + 量加权 IV。

    返回 {symbol: {call_vol, put_vol, pc_vol_ratio, call_oi, put_oi,
                   pc_oi_ratio, total_opt_vol, total_oi, weighted_iv}}。

    weighted_iv = Σ(IV*Volume)/Σ(Volume);某票 Volume 全 0 时退化为该票 IV 的
    简单均值(忽略 NaN),若仍无有效 IV → None。
    """
    import pandas as pd  # noqa: F401  (day_df 来自 pandas;此处仅确保依赖存在)

    out: dict[str, dict] = {}
    if day_df is None or not len(day_df):
        return out

    is_call = day_df["PutCall"] == "call"
    is_put = day_df["PutCall"] == "put"
    vol = day_df["Volume"]
    oi = day_df["OpenInterest"]
    iv = day_df["ImpliedVolatility"]

    # 量加权 IV 的分子项(IV*Volume,IV 为 NaN 的行不计权,分母对应也扣掉)
    iv_valid = iv.notna()
    g = day_df.groupby("Symbol", sort=False)

    call_vol = vol.where(is_call, 0.0).groupby(day_df["Symbol"], sort=False).sum()
    put_vol = vol.where(is_put, 0.0).groupby(day_df["Symbol"], sort=False).sum()
    call_oi = oi.where(is_call, 0.0).groupby(day_df["Symbol"], sort=False).sum()
    put_oi = oi.where(is_put, 0.0).groupby(day_df["Symbol"], sort=False).sum()

    # 加权 IV:分子 Σ(IV*Volume)(仅 IV 有效行),分母 Σ(Volume)(仅 IV 有效行)
    iv_x_vol = (iv.fillna(0.0) * vol).where(iv_valid, 0.0)
    wsum_num = iv_x_vol.groupby(day_df["Symbol"], sort=False).sum()
    wsum_den = vol.where(iv_valid, 0.0).groupby(day_df["Symbol"], sort=False).sum()
    # 退化:Volume 全 0 时用简单均值(IV 有效行的均值)
    iv_simple = iv.groupby(day_df["Symbol"], sort=False).mean()

    symbols = g.size().index
    for sym in symbols:
        cv = float(call_vol.get(sym, 0.0))
        pv = float(put_vol.get(sym, 0.0))
        coi = float(call_oi.get(sym, 0.0))
        poi = float(put_oi.get(sym, 0.0))
        num = float(wsum_num.get(sym, 0.0))
        den = float(wsum_den.get(sym, 0.0))
        if den > 0:
            wiv = num / den
        else:
            wiv = iv_simple.get(sym, None)  # 简单均值(可能仍为 NaN)
        out[str(sym)] = {
            "call_vol": int(round(cv)),
            "put_vol": int(round(pv)),
            "pc_vol_ratio": _ratio(pv, cv, 3),
            "call_oi": int(round(coi)),
            "put_oi": int(round(poi)),
            "pc_oi_ratio": _ratio(poi, coi, 3),
            "total_opt_vol": int(round(cv + pv)),
            "total_oi": int(round(coi + poi)),
            "weighted_iv": _round_or_none(wiv, 4),
        }
    return out


def aggregate_window(
    window_df, history_days: int = DEFAULT_HISTORY_DAYS
) -> tuple[str, dict[str, dict]]:
    """对一段窗口帧:最近交易日做完整聚合,每票附最近 history_days 日小历史。

    返回 (latest_date, {symbol: {完整指标..., "history":[{date,pc_vol_ratio,
    total_opt_vol,weighted_iv}, ...升序]}})。
    """
    if window_df is None or not len(window_df):
        return "", {}

    dates = sorted(window_df["DataDate"].dropna().unique().tolist())
    latest = dates[-1]
    hist_dates = dates[-history_days:]

    # 每个日期算一次聚合(只对历史窗口里的日期)
    per_day: dict[str, dict[str, dict]] = {}
    for ds in hist_dates:
        per_day[ds] = aggregate_day(window_df[window_df["DataDate"] == ds])

    latest_agg = per_day.get(latest, {})
    result: dict[str, dict] = {}
    for sym, metrics in latest_agg.items():
        history = []
        for ds in hist_dates:  # 升序
            dm = per_day.get(ds, {}).get(sym)
            if dm is None:
                continue
            history.append(
                {
                    "date": ds,
                    "pc_vol_ratio": dm.get("pc_vol_ratio"),
                    "total_opt_vol": dm.get("total_opt_vol"),
                    "weighted_iv": dm.get("weighted_iv"),
                }
            )
        rec = dict(metrics)
        rec["history"] = history
        result[sym] = rec
    return latest, result


# ── build 入口:读 → 聚合 → 写 JSON ─────────────────────────────────────────
def build(
    history_days: int = DEFAULT_HISTORY_DAYS,
    out_path: Optional[_Path] = None,
    chain: Optional[_Path] = None,
) -> dict:
    """读 parquet(列投影+最近 history_days 日过滤)→ 聚合 → 写 eod_pc.json。

    返回写入的 payload(也落盘)。无 parquet → 写空壳并返回。
    """
    out_p = out_path or _DEFAULT_OUT
    out_p.parent.mkdir(parents=True, exist_ok=True)

    src = chain or chain_path()
    now_iso = _dt.datetime.now().isoformat(timespec="seconds")

    if not src.exists():
        payload = {
            "generated_at": now_iso,
            "data_date": None,
            "n_tickers": 0,
            "tickers": {},
            "note": "eod_chain.parquet 缺失",
        }
        out_p.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    all_dates = list_trading_dates(src)
    window_dates = all_dates[-history_days:]
    window_df = load_window(window_dates, src)
    data_date, tickers = aggregate_window(window_df, history_days=history_days)

    payload = {
        "generated_at": now_iso,
        "data_date": data_date or None,
        "n_tickers": len(tickers),
        "tickers": tickers,
    }
    out_p.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


if __name__ == "__main__":  # pragma: no cover — 手动跑 build
    import sys

    days = DEFAULT_HISTORY_DAYS
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    p = build(history_days=days)
    sample = {k: p["tickers"][k] for k in list(p["tickers"])[:1]}
    print("data_date  =", p["data_date"])
    print("n_tickers  =", p["n_tickers"])
    print("out        =", _DEFAULT_OUT)
    for k, v in sample.items():
        print(
            f"{k}: call_vol={v['call_vol']} put_vol={v['put_vol']} "
            f"pc_vol={v['pc_vol_ratio']} pc_oi={v['pc_oi_ratio']} "
            f"wiv={v['weighted_iv']} hist={len(v['history'])}d"
        )

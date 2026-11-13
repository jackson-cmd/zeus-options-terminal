"""Layer T5(期权归档链是否存在)+ Layer T10(选 strike / 估算 debit)。

复用 ema_pop_backtest.options_bidir 已验证过的读取姿势(pyarrow pushdown filter、
同一组归档列名)和 ema_pop_backtest.config 的流动性哨兵(MIN_OI / MAX_REL_SPREAD),
但不直接调 options_bidir.pick_spread —— 那个是回测用的,按"某一天历史快照 + DTE
band"选;这里要的是"归档里最新一天快照 + 最靠近 target DTE 的到期",strike 的
参照价是 Barchart 当前 Latest,不是归档里的历史 UnderlyingPrice。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import config

try:
    from ema_pop_backtest import config as _exp_config
    MIN_OI = _exp_config.MIN_OI
    MAX_REL_SPREAD = _exp_config.MAX_REL_SPREAD
except Exception:
    MIN_OI = 1
    MAX_REL_SPREAD = 0.60

_COLS = ["ExpirationDate", "PutCall", "StrikePrice", "BidPrice", "AskPrice",
         "LastPrice", "UnderlyingPrice", "OpenInterest", "Volume", "Delta",
         "ImpliedVolatility", "DataDate"]


def archive_path(ticker: str, archive_dir: Optional[Path] = None) -> Path:
    root = archive_dir if archive_dir is not None else config.OPTIONS_ARCHIVE_DIR
    return root / f"{ticker.upper()}.parquet"


def layer_t5_options_chain_exists(df: pd.DataFrame, archive_dir: Optional[Path] = None,
                                    verbose: bool = False) -> pd.DataFrame:
    """Layer T5 —— 只留归档里有这只票 parquet 文件的行。"""
    before = len(df)
    if df.empty:
        if verbose:
            print(f"[T5 期权链存在] {before} -> 0 行")
        return df.copy()

    root = archive_dir if archive_dir is not None else config.OPTIONS_ARCHIVE_DIR
    if not root.exists():
        print(f"⚠️ 期权归档盘未挂载: {root} —— Layer T5 会清空所有候选,"
              f"插好硬盘后重跑")

    mask = df["Symbol"].apply(lambda t: archive_path(t, archive_dir).exists())
    out = df[mask].copy()
    if verbose:
        print(f"[T5 期权链存在] {before} -> {len(out)} 行")
    return out


def load_calls(ticker: str, archive_dir: Optional[Path] = None) -> Optional[pd.DataFrame]:
    """读该 ticker 的 call 链(全部快照日 × 全部到期日,列已裁剪+转型)。
    找不到文件/读失败/空数据 → None。"""
    path = archive_path(ticker, archive_dir)
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq
        tbl = pq.read_table(path, columns=_COLS, filters=[("PutCall", "==", "call")])
        df = tbl.to_pandas()
    except Exception:
        try:
            df = pd.read_parquet(path, columns=_COLS)
            df = df[df["PutCall"] == "call"]
        except Exception:
            return None
    if df.empty:
        return None

    df["DataDate"] = pd.to_datetime(df["DataDate"], errors="coerce")
    df["ExpirationDate"] = pd.to_datetime(df["ExpirationDate"], errors="coerce")
    df["IV"] = pd.to_numeric(df["ImpliedVolatility"], errors="coerce")
    for c in ("StrikePrice", "BidPrice", "AskPrice", "UnderlyingPrice", "OpenInterest"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["DataDate", "ExpirationDate", "StrikePrice"])
    return df if len(df) else None


def pick_strikes(calls: pd.DataFrame, ref_price: float, today: _dt.date,
                  dte_target: int = config.DTE_TARGET,
                  width_pct: float = config.WIDTH_PCT,
                  ema_touched: Optional[int] = None) -> Optional[dict]:
    """归档最新一天快照里,挑离 today+dte_target 最近的到期;long=最贴近 ref_price
    的 strike(ATM),short=最贴近 ref_price×(1+width_pct) 的更高 strike。
    任何 sanity 检查不过 → None(由调用方决定标 no_data,不抛异常)。"""
    if calls is None or not len(calls) or not np.isfinite(ref_price) or ref_price <= 0:
        return None

    last_date = calls["DataDate"].max()
    snap = calls[calls["DataDate"] == last_date]
    if snap.empty:
        return None

    today_ts = pd.Timestamp(today)
    target_expiry_date = today_ts + pd.Timedelta(days=dte_target)
    expiries = pd.Series(pd.to_datetime(snap["ExpirationDate"].dropna().unique()))
    if expiries.empty:
        return None
    expiry = expiries.loc[(expiries - target_expiry_date).abs().idxmin()]

    legs = snap[snap["ExpirationDate"] == expiry].copy()
    if len(legs) < 2:
        return None

    long_row = legs.loc[(legs["StrikePrice"] - ref_price).abs().idxmin()]
    long_K = float(long_row["StrikePrice"])

    short_target = ref_price * (1 + width_pct)
    short_cands = legs[legs["StrikePrice"] > long_K]
    if short_cands.empty:
        return None
    short_row = short_cands.loc[(short_cands["StrikePrice"] - short_target).abs().idxmin()]
    short_K = float(short_row["StrikePrice"])

    width = short_K - long_K
    if width <= 0:
        return None

    long_bid, long_ask = float(long_row["BidPrice"]), float(long_row["AskPrice"])
    short_bid, short_ask = float(short_row["BidPrice"]), float(short_row["AskPrice"])
    if not all(np.isfinite(x) and x >= 0 for x in (long_bid, long_ask, short_bid, short_ask)):
        return None
    if long_ask <= 0:
        return None

    long_oi = long_row.get("OpenInterest")
    long_oi = 0 if pd.isna(long_oi) else long_oi
    if long_oi < MIN_OI:
        return None

    long_mid = (long_bid + long_ask) / 2
    if long_mid > 0 and (long_ask - long_bid) / long_mid > MAX_REL_SPREAD:
        return None
    short_mid = (short_bid + short_ask) / 2
    debit_mid = long_mid - short_mid
    if not np.isfinite(debit_mid) or debit_mid <= 0 or debit_mid >= width:
        return None

    # 按信号质量分档定仓位 —— 200D touch 上大仓, 快线小仓
    target_usd = config.POSITION_USD_BY_TOUCH.get(
        int(ema_touched) if ema_touched is not None else 0,
        config.TARGET_POSITION_USD,
    )
    n_contracts = max(1, round(target_usd / (debit_mid * 100)))

    return {
        "expiry": expiry.date().isoformat(),
        "dte": int((expiry - today_ts).days),
        "snapshot_date": pd.Timestamp(last_date).date().isoformat(),
        "long_strike": long_K,
        "short_strike": short_K,
        "width": round(width, 2),
        "est_debit_per_share": round(debit_mid, 2),
        "est_max_loss_per_share": round(debit_mid, 2),
        "est_max_profit_per_share": round(width - debit_mid, 2),
        "n_contracts": int(n_contracts),
        "target_position_usd": target_usd,
    }


def enrich_with_strikes(df: pd.DataFrame, today: _dt.date,
                          archive_dir: Optional[Path] = None,
                          verbose: bool = False) -> pd.DataFrame:
    """给 T9 之后的候选(通常 ≤20 行)逐票读归档 + 选 strike。选不出来的行仍保留,
    但 strike_status='no_data'(不生成 ticket,由 ticket_writer 过滤)。"""
    if df.empty:
        out = df.copy()
        out["strike_status"] = pd.Series(dtype="object")
        if verbose:
            print("[T10 选strike] 0 行 -> 0 行有可用 strike/debit")
        return out

    rows = []
    for _, row in df.iterrows():
        d = row.to_dict()
        calls = load_calls(d["Symbol"], archive_dir)
        ema_t = d.get("ema_touched")
        ema_t = int(ema_t) if pd.notna(ema_t) else None
        picked = pick_strikes(calls, float(d["Latest"]), today,
                                ema_touched=ema_t) if calls is not None else None
        if picked is None:
            d["strike_status"] = "no_data"
        else:
            d["strike_status"] = "ok"
            d.update(picked)
        rows.append(d)

    out = pd.DataFrame(rows)
    ok = int((out["strike_status"] == "ok").sum())
    if verbose:
        print(f"[T10 选strike] {len(df)} 行 -> {ok} 行有可用 strike/debit(其余 no_data)")
    return out

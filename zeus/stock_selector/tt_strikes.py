"""TastyTrade 实时链取 strike (替代 SSD EOD 归档)。

复用 options_analyzer.strategy_builder_api.fetch_expirations / fetch_chain
(有 60s cache, 已经在 PWA 里用了很久, 走 REST /market-data/by-type + option chain)。

用法:
    enrich_with_tt(df, client, today_date) → df 每行加 strike_status='ok' + long/short_K + est_debit ...

如果 client 为 None → 全部标 no_client(不阻断, 由 SSD 兜底 or 前端展示"缺 client")。
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Optional

import pandas as pd

from . import config


_DTE_TARGET = config.DTE_TARGET if hasattr(config, "DTE_TARGET") else 40
_WIDTH_PCT = config.WIDTH_PCT if hasattr(config, "WIDTH_PCT") else 0.10
_LIMIT_BID_INTO_SPREAD = getattr(config, "LIMIT_BID_INTO_SPREAD", 0.25)
_TIF = getattr(config, "ORDER_TIME_IN_FORCE", "GTC")


def _pop_theoretical(spot: float, breakeven: float, iv: float,
                      dte: float, r: float = 0.045) -> Optional[float]:
    """Bull call spread 理论 POP: P(S_T > BE), lognormal N(d2)。用长腿 IV。"""
    if not (spot and breakeven and iv and dte) or spot <= 0 or breakeven <= 0 \
       or iv <= 0 or dte <= 0:
        return None
    T = dte / 365.0
    sig = iv * math.sqrt(T)
    if sig <= 0:
        return None
    d2 = (math.log(spot / breakeven) + (r - 0.5 * iv * iv) * T) / sig
    p = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    return round(max(0.0, min(100.0, p * 100.0)), 1)


def _pick_expiration(exps: list, today: _dt.date, target_dte: int = _DTE_TARGET) -> Optional[dict]:
    """从 fetch_expirations 返回的窗口内挑最接近 target_dte 的月度到期(prefer Regular)。"""
    if not exps:
        return None
    # 已在 filter_expirations_within_90_days 里过滤过, 直接选 dte 接近 target 的
    def _dte_val(e):
        return abs((e.get("dte") or 999) - target_dte)
    monthly = [e for e in exps if str(e.get("type", "")).lower() == "regular"]
    pool = monthly if monthly else exps
    return min(pool, key=_dte_val)


def _pick_strikes_from_chain(chain: dict, ref_price: float, dte: int,
                              width_pct: float = _WIDTH_PCT) -> Optional[dict]:
    """chain = {strikes:[{strike, call:{bid,ask,mid,iv}, put:{...}}, ...]}
       挑 ATM call (最近 ref_price 的 strike) + 短腿 (最近 ref_price*(1+width) 的更高 strike)。
       返回 dict 含: 两腿 K/bid/ask/mid, mid_debit, 攻击性 LMT 挂价, breakeven, POP。"""
    strikes = chain.get("strikes") or []
    if not strikes or ref_price <= 0:
        return None
    # 只留 call 有报价的
    call_rows = []
    for s in strikes:
        c = s.get("call") or {}
        bid, ask = float(c.get("bid") or 0), float(c.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2 if (c.get("mid") in (None, 0, 0.0)) else float(c["mid"])
        iv = float(c.get("iv") or 0)
        call_rows.append({"K": float(s["strike"]), "bid": bid, "ask": ask, "mid": mid, "iv": iv})
    call_rows.sort(key=lambda r: r["K"])
    if len(call_rows) < 2:
        return None

    long_row = min(call_rows, key=lambda r: abs(r["K"] - ref_price))
    short_target = ref_price * (1 + width_pct)
    higher = [r for r in call_rows if r["K"] > long_row["K"]]
    if not higher:
        return None
    short_row = min(higher, key=lambda r: abs(r["K"] - short_target))

    width = short_row["K"] - long_row["K"]
    debit_mid = long_row["mid"] - short_row["mid"]
    if debit_mid <= 0 or debit_mid >= width:
        return None

    # spread bid/ask: 保守撮合 = 长腿 ASK - 短腿 BID; 平仓卖 = 长腿 BID - 短腿 ASK
    debit_ask = long_row["ask"] - short_row["bid"]     # 立即 fill 最贵
    debit_bid = long_row["bid"] - short_row["ask"]     # 只当 exit 参考
    # spread mid = mid - mid; aggressive limit = bid + 25% * (ask - bid)
    lmt = debit_bid + _LIMIT_BID_INTO_SPREAD * (debit_ask - debit_bid)
    lmt = max(0.05, min(lmt, debit_ask))               # clamp
    # 90% max profit close LMT (spread bid + 0.9 * max_profit)
    max_profit = width - debit_mid
    tp_lmt = round(debit_mid + 0.9 * max_profit, 2)

    breakeven = long_row["K"] + debit_mid
    pop = _pop_theoretical(ref_price, breakeven, long_row["iv"], dte)

    return {
        "long_strike": long_row["K"],
        "short_strike": short_row["K"],
        "long_iv": round(long_row["iv"], 4) if long_row["iv"] else None,
        "long_bid": round(long_row["bid"], 2), "long_ask": round(long_row["ask"], 2),
        "short_bid": round(short_row["bid"], 2), "short_ask": round(short_row["ask"], 2),
        "width": round(width, 2),
        "est_debit_per_share": round(debit_mid, 2),   # mid, 用于 sizing 计算
        "debit_ask_per_share": round(debit_ask, 2),   # 保守撮合上限
        "aggressive_lmt": round(lmt, 2),               # 建议挂单价(bid + 25% spread)
        "tp_lmt_90pct": tp_lmt,                        # 90% max profit close 单价
        "breakeven": round(breakeven, 2),
        "be_offset_pct": round((breakeven / ref_price - 1) * 100, 2),
        "pop_pct": pop,                                # 理论盈利概率 %
        "est_max_loss_per_share": round(debit_mid, 2),
        "est_max_profit_per_share": round(max_profit, 2),
    }


def enrich_with_tt(df: pd.DataFrame, client, today: _dt.date,
                    dte_target: int = _DTE_TARGET,
                    width_pct: float = _WIDTH_PCT,
                    verbose: bool = False) -> pd.DataFrame:
    """给候选 df 逐票拉 TastyTrade 实时链选 strike。返回 df 加 strike_status +
    long_strike/short_strike/expiry/dte/est_debit/n_contracts/target_position_usd。

    client=None → 全行 strike_status='no_client' 不抛异常。"""
    if df.empty:
        out = df.copy()
        out["strike_status"] = pd.Series(dtype="object")
        return out
    if client is None:
        out = df.copy()
        out["strike_status"] = "no_client"
        return out

    # 迟绑定, 避免包顶部 import 循环 (server 起来才有 strategy_builder_api)
    from strategy_builder_api import fetch_expirations, fetch_chain

    rows = []
    ok, fail = 0, 0
    for _, r in df.iterrows():
        d = r.to_dict()
        tkr = str(d.get("Symbol") or "").upper()
        try:
            exps = fetch_expirations(client, tkr, today=today)
            exp = _pick_expiration(exps, today, dte_target)
            if not exp:
                d["strike_status"] = "no_exp"
                rows.append(d); fail += 1; continue
            chain = fetch_chain(client, tkr, exp["date"])
            spot = float(chain.get("spot") or 0) or float(d.get("Latest") or 0)
            dte_val = int(exp.get("dte") or 0)
            picked = _pick_strikes_from_chain(chain, spot, dte_val, width_pct)
            if not picked:
                d["strike_status"] = "no_strikes"
                rows.append(d); fail += 1; continue
            # 仓位按 POP 分档 —— 高 POP 值得大仓 · 低 POP 收着做彩票
            target_usd = config.target_debit_by_pop(picked.get("pop_pct"))
            debit_per_contract = picked["est_debit_per_share"] * 100
            n_contracts = max(1, round(target_usd / debit_per_contract))
            actual_debit = n_contracts * debit_per_contract
            # clamp: 张数往下调直到落进 [500, 1500]
            while n_contracts > 1 and actual_debit > config.POSITION_MAX_USD:
                n_contracts -= 1
                actual_debit = n_contracts * debit_per_contract
            d.update(picked)
            d["expiry"] = exp["date"]
            d["dte"] = dte_val
            d["n_contracts"] = int(n_contracts)
            d["target_position_usd"] = round(target_usd, 0)
            d["actual_debit_usd"] = round(actual_debit, 0)
            d["strike_status"] = "ok"
            # 完整可粘贴 ticket
            exp_short = exp["date"].replace("-", "")[2:]   # 260911 → yy-mm-dd 简写
            exp_short = f"{exp_short[:2]}-{exp_short[2:4]}-{exp_short[4:]}"
            d["ticket"] = (f"BUY +{n_contracts} {tkr} {exp_short} "
                            f"{picked['long_strike']:g}C / SELL {picked['short_strike']:g}C "
                            f"@ -{picked['aggressive_lmt']:.2f} LMT {_TIF} (借记)")
            d["ticket_tp"] = (f"[TP 挂单] SELL {picked['long_strike']:g}C / BUY "
                                f"{picked['short_strike']:g}C @ +{picked['tp_lmt_90pct']:.2f} LMT {_TIF}")
            rows.append(d); ok += 1
        except Exception as e:
            d["strike_status"] = "error"
            d["strike_error"] = str(e)[:120]
            rows.append(d); fail += 1
    if verbose:
        print(f"[TT strikes] {len(df)} tickers → ok={ok} fail={fail}")
    return pd.DataFrame(rows)

"""TA Screener 历史回测(真实 EOD 期权链,无未来函数)。

数据:eod_crawler/data_eod/eod_chain.parquet —— 28+ 个交易日的
真实 EOD 期权链(Symbol/Exp/Bid/Ask/Strike/OI/IV/Greeks/UnderlyingPrice/DataDate)。

方法(每个 DataDate D):
  1. 按 **as-of D** 的股票史(切到 ≤D)重算评分 — 复用 engine.analyze_ticker/score_parts
     (SSP/S/R/edge/MACD/RSI/Vol 全是 walk-forward,只用 ≤D 数据)。期权结构 bias
     用 **D 当天的链** 算(optmetrics)。评分口径 == 线上。
  2. 选 top_n(或全部 ≥ 阈值)。
  3. 用 **D 当天真实 EOD mid** 建推荐 debit spread(复用 spreads.build_candidates/
     pick_primary,链换成 EOD)。entry 只用 D 当天数据 → 无未来函数。
  4. 持有到期、不止损止盈:到期日标的真实收盘 → intrinsic 赔付 → PnL = 赔付 − 借记。
     到期日 > 最新股票日 → status=pending(不计入胜率)。
  5. 每单记全因子(哪些信号触发/lift/期权bias/RSI/趋势)→ 事后切因子胜率。

诚实边界(UI 标注):样本仅 ~28 日且每日增长;近期单未到期=pending;EOD mid 偏乐观;
universe 幸存者偏差。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
# EOD 链路径:repo eod_crawler(搬进来后)优先 → Downloads(旧位置)兜底 → 旧 fallback
EOD_CHAIN_REPO = REPO_ROOT / "eod_crawler" / "data_eod" / "eod_chain.parquet"
EOD_CHAIN_DOWNLOADS = Path(__file__).resolve().parent.parent / "data" / "eod_chain.parquet"
EOD_CHAIN_FALLBACK = REPO_ROOT / "data" / "ta_screener" / "eod_chain.parquet"
OUT = REPO_ROOT / "data" / "ta_screener" / "backtest.json"

STATE = {"running": False, "pct": 0, "msg": "空闲", "error": None, "generated_at": None}


def _imports():
    import sys
    d = str(Path(__file__).resolve().parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    import engine as E, optmetrics as OM, spreads as SP, data as D
    return E, OM, SP, D


def _set(pct=None, msg=None, **kw):
    if pct is not None:
        STATE["pct"] = int(pct)
    if msg is not None:
        STATE["msg"] = str(msg)
    STATE.update(kw)


def chain_path() -> Path:
    for p in (EOD_CHAIN_REPO, EOD_CHAIN_DOWNLOADS, EOD_CHAIN_FALLBACK):
        if p.exists():
            return p
    return EOD_CHAIN_FALLBACK


def load_chain() -> pd.DataFrame:
    df = pd.read_parquet(chain_path())
    df["DataDate"] = df["DataDate"].astype(str).str[:10]
    df["ExpirationDate"] = df["ExpirationDate"].astype(str).str[:10]
    df["PutCall"] = df["PutCall"].astype(str).str.lower()
    return df


def _is_third_friday(d: _dt.date) -> bool:
    return d.weekday() == 4 and 15 <= d.day <= 21


def _eod_to_chain(rows: pd.DataFrame, exp: str) -> dict:
    """把某 (symbol, DataDate, exp) 的 EOD 行 → fetch_chain 同构 dict(mid=(bid+ask)/2)。"""
    sub = rows[rows["ExpirationDate"] == exp]
    spot = float(sub["UnderlyingPrice"].iloc[0]) if len(sub) else 0.0
    by_k: dict = {}
    for _, r in sub.iterrows():
        k = float(r["StrikePrice"])
        bid, ask = float(r["BidPrice"] or 0), float(r["AskPrice"] or 0)
        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else float(r["LastPrice"] or 0)
        leg = {"bid": bid, "ask": ask, "mid": round(mid, 4), "iv": float(r["ImpliedVolatility"] or 0),
               "oi": float(r["OpenInterest"] or 0), "vol": float(r["Volume"] or 0),
               "delta": float(r["Delta"] or 0)}
        by_k.setdefault(k, {"strike": k, "call": {}, "put": {}})
        by_k[k]["call" if r["PutCall"] == "call" else "put"] = leg
    return {"spot": spot, "expiration": exp,
            "strikes": [by_k[k] for k in sorted(by_k)]}


def _exps_for(rows: pd.DataFrame, d: _dt.date) -> list:
    """该 symbol@D 的可用到期 [{date,dte,type}](type=Regular 当 3rd-Fri)。"""
    out = []
    for e in sorted(rows["ExpirationDate"].unique()):
        try:
            ed = _dt.date.fromisoformat(e)
        except Exception:
            continue
        dte = (ed - d).days
        if dte < 0:
            continue
        out.append({"date": e, "dte": dte, "type": "Regular" if _is_third_friday(ed) else "Weekly"})
    return out


def _exp_payoff(direction: str, long_k: float, short_k: float, width: float, debit: float, close_at_exp: float) -> float:
    """到期 intrinsic 赔付 − 借记($/份×100)。bull call / bear put debit spread。"""
    if direction == "bull":
        payoff = max(0.0, min(width, close_at_exp - long_k))
    else:
        payoff = max(0.0, min(width, long_k - close_at_exp))
    return round((payoff - debit) * 100.0, 2)


def _factor_tags(row: dict) -> list:
    """这单评分由哪些因子构成(供事后归因)。"""
    tags = []
    for p in (row.get("score_detail") or {}).get("parts", []):
        if p.get("kind") == "gate":
            tags.append("RSI门槛")
        else:
            tags.append(p.get("label", "?"))
    return tags


def run_backtest(threshold: float = 70.0, top_n: int = 1, log=print) -> dict:
    """全量回测。返回并写 backtest.json。"""
    E, OM, SP, D = _imports()
    _set(2, "读 EOD 期权链…")
    chain = load_chain()
    dates = sorted(chain["DataDate"].unique())
    chain_syms = sorted(chain["Symbol"].unique())
    # universe = TA 配置 ∩ EOD 链里有的
    uni, _cat = D.parse_universe()
    syms = [s for s in uni if s in set(chain_syms)]
    log(f"[backtest] EOD {len(dates)} 日 × universe∩链 {len(syms)} 票")

    _set(8, f"下载 {len(syms)} 票股票史(5y)…")
    hist = D.download_history(syms)
    last_stock = {t: df.index[-1].date() for t, df in hist.items() if len(df)}

    # ⑥ 额外分:只给"最新 EOD 日(今天)"附当前 finviz/barchart 形态(作者:backtest 只看今天的)。
    #   过去日期不附 —— 给历史日套当前形态是偷看未来,且 History 已被前瞻 paper log 取代,不需要。
    latest = dates[-1] if dates else None
    fv, bc = {}, {}
    try:
        try:
            from . import external as X
        except ImportError:
            import external as X  # type: ignore
        uset = set(syms)
        fv = X.finviz_patterns(uset)
        bc, _bcm = X.barchart_csv_signals(uset)
        log(f"[backtest] 额外分形态(仅今天 {latest}):finviz {sum(len(v) for v in fv.values())} · barchart {sum(len(v) for v in bc.values())}")
    except Exception as e:
        log(f"[backtest] 形态额外分附加失败(跳过): {e}")

    trades = []
    n_dates = len(dates)
    for di, ds in enumerate(dates):
        d = _dt.date.fromisoformat(ds)
        day_chain = chain[chain["DataDate"] == ds]
        scored = []
        for t in syms:
            df = hist.get(t)
            if df is None:
                continue
            sub = df[df.index.date <= d]
            if len(sub) < 120:
                continue
            try:
                daily = D.to_arrays(sub)
                weekly = D.resample_ohlc(sub, "W-FRI")
                monthly = D.resample_ohlc(sub, "ME")
                row = E.analyze_ticker(daily, weekly, monthly)
                if "error" in row:
                    continue
                row["ticker"] = t
                # 额外分形态只附给今天(ds==latest);过去日期留空 → extra=0(防偷看未来)
                _today = (ds == latest)
                row["finviz"] = fv.get(t, []) if _today else []
                row["barchart"] = bc.get(t, []) if _today else []
                sd = E.score_parts(row)
                row["score"] = sd["score"]
                row["score_detail"] = sd
                # 期权结构 bias(当日链,近2个月度)
                srows = day_chain[day_chain["Symbol"] == t]
                exps_all = _exps_for(srows, d)
                monthlies = [e for e in exps_all if e["type"] == "Regular" and e["dte"] >= 3][:2] or \
                            [e for e in exps_all if e["dte"] >= 14][:2]
                spot = float(srows["UnderlyingPrice"].iloc[0]) if len(srows) else float(row.get("close") or 0)
                if monthlies and spot > 0:
                    chains = [(_eod_to_chain(srows, e["date"]), max(1e-4, e["dte"] / 365.0)) for e in monthlies]
                    m = OM.compute_metrics(chains, spot)
                    if m:
                        # v3 口径(同 scanner 4.6):score = min(100, core+OI/Vol+PCR+GEX) + 额外分
                        dirn = sd.get("dir")
                        opt = OM.flow_points(m, dirn) + OM.pcr_points(m, dirn) + OM.gex_points(m, dirn)
                        row["score"] = round(min(100.0, sd.get("core", 0.0) + opt) + sd.get("extra", 0.0), 1)
                        ob = OM.bias(m, spot)
                        row["opt_bias_applied"] = round((ob["total"] if dirn == "bull" else -ob["total"]), 2) if dirn in ("bull", "bear") else None
                row["_exps"] = exps_all
                row["_spot_eod"] = spot
                scored.append(row)
            except Exception:
                continue
        if not scored:
            _set(int(8 + di / n_dates * 88), f"{ds}: 0 候选")
            continue
        scored.sort(key=lambda r: -r["score"])
        # 选单:所有评分 ≥ 阈值的(top_n<=0 = 全部;>0 = 取前 top_n)。
        # 不够阈值则当天不开单(不强行兜底,反映真实"没好机会就空仓")。
        qualified = [r for r in scored if r["score"] >= threshold]
        picks = qualified if (top_n is None or top_n <= 0) else qualified[:top_n]

        for rank, row in enumerate(picks):
            t = row["ticker"]
            dirn = SP.row_direction(row)
            if not dirn:
                continue
            lo, hi, tgt = SP.BULL_DTE if dirn == "bull" else SP.BEAR_DTE
            srows = day_chain[day_chain["Symbol"] == t]
            exp = SP.pick_expiration(row["_exps"], lo, hi, tgt, prefer_monthly=(dirn == "bull"))
            if not exp:
                continue
            ch = _eod_to_chain(srows, exp["date"])
            cands = SP.build_candidates(ch.get("strikes"), row["_spot_eod"], dirn, exp.get("dte"))
            if not cands:
                continue
            pi = SP.pick_primary(cands, dirn, row["_spot_eod"])
            spr = cands[pi]
            if not (_finite(spr.get("debit")) and _finite(spr.get("width")) and spr["debit"] > 0 and spr["width"] > 0):
                continue  # EOD 报价全空 → debit nan,跳过(否则 PnL 传染 nan)
            # 到期结算
            ed = _dt.date.fromisoformat(exp["date"])
            ls = last_stock.get(t)
            df = hist[t]
            status, pnl, close_exp = "pending", None, None
            if ls and ed <= ls:
                at = df[df.index.date <= ed]
                if len(at):
                    close_exp = round(float(at["Close"].iloc[-1]), 2)
                    pnl = _exp_payoff(dirn, spr["long_strike"], spr["short_strike"], spr["width"], spr["debit"], close_exp)
                    status = "closed"
            trades.append({
                "date": ds, "ticker": t, "rank": rank + 1, "dir": dirn, "score": row["score"],
                "spot_entry": row["_spot_eod"], "exp": exp["date"], "dte": exp["dte"],
                "long_strike": spr["long_strike"], "short_strike": spr["short_strike"],
                "width": spr["width"], "debit": spr["debit"], "max_payoff": round((spr["width"] - spr["debit"]) * 100, 2),
                "max_loss": round(spr["debit"] * 100, 2), "max_profit": round((spr["width"] - spr["debit"]) * 100, 2),
                "breakeven": spr.get("breakeven"), "pop": spr.get("pop"),
                "rr": spr.get("rr"),
                "status": status, "close_at_exp": close_exp, "pnl": pnl,
                "win": (pnl is not None and pnl > 0) if status == "closed" else None,
                "rsi": (row.get("tasig") or {}).get("rsi"),
                "trend_long": ((row.get("setup") or {}).get("trend") or {}).get("long"),
                "factors": _factor_tags(row),
                "opt_bias": row.get("opt_bias_applied"),
            })
        _set(int(8 + (di + 1) / n_dates * 88), f"{ds}: {len(picks)} 单(累计 {len(trades)})")

    result = _aggregate(trades, dates, threshold, top_n)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    # 清洗 NaN/Inf → None,别写出非法 JSON token(如 trades[].debit 的 NaN),
    # 否则下游 starlette JSONResponse(allow_nan=False) 读盘序列化会 500。
    tmp.write_text(json.dumps(_json_safe(result), ensure_ascii=False, default=float), encoding="utf-8")
    os.replace(tmp, OUT)
    _set(100, f"完成:{len(trades)} 单({result['n_closed']} 已结算)",
         generated_at=result["generated_at"])
    return result


def _finite(x) -> bool:
    """debit/last 全空时 EOD mid → nan,会传染 mean/sum;统计前一律过滤。"""
    try:
        return x is not None and np.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _json_safe(obj):
    """递归把 NaN/Inf → None,避免写盘产生非法 JSON token(trades[].debit 等残留 nan)。"""
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _aggregate(trades: list, dates: list, threshold: float, top_n: int) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if t["win"]]
    pnls = [t["pnl"] for t in closed if _finite(t["pnl"])]
    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], {"date": t["date"], "n": 0, "n_closed": 0, "pnl": 0.0, "wins": 0, "top": None})
        b = by_date[t["date"]]
        b["n"] += 1
        if t["rank"] == 1:
            b["top"] = {"ticker": t["ticker"], "dir": t["dir"], "score": t["score"],
                        "status": t["status"], "pnl": t["pnl"] if _finite(t["pnl"]) else None}
        if t["status"] == "closed":
            b["n_closed"] += 1
            if _finite(t["pnl"]):
                b["pnl"] += t["pnl"]
            b["wins"] += 1 if t["win"] else 0
    for b in by_date.values():
        b["pnl"] = round(b["pnl"], 1)
    # 因子归因:每个因子 tag 的已结算单胜率/期望
    fac = {}
    for t in closed:
        for tag in t["factors"]:
            fac.setdefault(tag, {"factor": tag, "n": 0, "wins": 0, "pnl": 0.0})
            fac[tag]["n"] += 1
            fac[tag]["wins"] += 1 if t["win"] else 0
            if _finite(t["pnl"]):
                fac[tag]["pnl"] += t["pnl"]
    fac_list = []
    for f in fac.values():
        f["win_rate"] = round(f["wins"] * 100.0 / f["n"], 1) if f["n"] else 0.0
        f["avg_pnl"] = round(f["pnl"] / f["n"], 1) if f["n"] else 0.0
        fac_list.append(f)
    fac_list.sort(key=lambda x: -x["n"])
    return {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"threshold": threshold, "top_n": top_n},
        "dates": dates,
        "n_trades": len(trades), "n_closed": len(closed), "n_pending": len(trades) - len(closed),
        "win_rate": round(len(wins) * 100.0 / len(closed), 1) if closed else None,
        "avg_pnl": round(float(np.mean(pnls)), 1) if pnls else None,
        "total_pnl": round(float(np.sum(pnls)), 1) if pnls else 0.0,
        "by_date": [by_date[d] for d in sorted(by_date)],
        "factor_attribution": fac_list,
        "trades": trades,
    }


def load_results() -> dict:
    if not OUT.exists():
        return {"empty": True, "msg": "还没跑过历史回测"}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception as e:
        return {"empty": True, "msg": f"结果损坏: {e}"}


if __name__ == "__main__":
    import sys
    th = float(sys.argv[1]) if len(sys.argv) > 1 else 70.0
    tn = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_backtest(threshold=th, top_n=tn)

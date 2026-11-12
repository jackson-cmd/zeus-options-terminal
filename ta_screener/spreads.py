"""Debit spread 推荐(作者唯一玩法:debit spread):

- **Bull Call**(方向=多):35-45 DTE(目标 40),优先月期权(expiration-type=Regular);
  腿型:ATM 长腿 / 短腿 +10%(默认主)或 +5%(可选);默认取更宽的 +10%。
- **Bear**(方向=空):只提示方向(看空),**不推荐开单**(作者设置);不生成 spread_rec。
- 方向判定 = 票面证据投票:触支撑/SSP多达标/edge显著多 → bull;
  触阻力/SSP空达标/edge显著空 → bear;平票不推荐。
- 财报日落在到期日之前 → ⚠️ earnings_warn(财报横跨持仓期)。
- 只对排序后 TOP_N 票拉链(REST 有 TTL 缓存);任何失败跳过该票,绝不抛。
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def spread_pop(spot: float, breakeven: float, iv: float, dte: float,
               direction: str, r: float = 0.045) -> Optional[float]:
    """debit 价差盈利概率(POP):到期价越过盈亏平衡点的概率(lognormal N(d2) 近似,
    用 long 腿隐波)。bull=P(S_T>BE)、bear=P(S_T<BE)。数据不足 → None。返回 0–100。"""
    if not (spot and breakeven and iv and dte) or spot <= 0 or breakeven <= 0 or iv <= 0 or dte <= 0:
        return None
    T = dte / 365.0
    sig = iv * math.sqrt(T)
    if sig <= 0:
        return None
    d2 = (math.log(spot / breakeven) + (r - 0.5 * iv * iv) * T) / sig
    p = _norm_cdf(d2) if direction == "bull" else _norm_cdf(-d2)
    return round(max(0.0, min(100.0, p * 100.0)), 1)

TOP_N = 400                  # 覆盖所有有方向的票(几乎全 universe);并发拉链够快
# 作者逻辑(统一):所有推荐订单固定 35-45 DTE(目标 40), bull/bear 都一样, 不随 regime 变。
BULL_DTE = (35, 45, 40)      # lo, hi, target
BEAR_DTE = (35, 45, 40)      # bear 也 35-45(原 ≤14 短线对冲已弃, 统一到 35-45)

BULL_DTE_BY_REGIME = {          # 固定 35-45(回测最优), regime 仅用于横幅文案
    "rising":  (35, 45, 40),
    "fell":    (35, 45, 40),
    "neutral": (35, 45, 40),
}
BULL_ALT_BY_REGIME = {          # 备选到期也锁 35-45(不再凑窗外候选)
    "rising":  (35, 45, 40),
    "fell":    (35, 45, 40),
    "neutral": (35, 45, 40),
}


def market_regime(spy_closes) -> dict:
    """SPY 近期状态 → Bull 到期 regime。
    rising: 近一周涨 ≥0.8% 且站上 10 日线 → 短到期;
    fell:   近一周跌 ≥1.2%,或一周净跌且 5 日内 ≥3 天下跌 → 长到期;
    neutral: 之间。返回 {regime,ret5,down_days,label}。"""
    seq = [] if spy_closes is None else list(spy_closes)
    c = [float(x) for x in seq if x == x]
    if len(c) < 11:
        return {"regime": "neutral", "ret5": None, "down_days": None, "label": "ATM 长腿 / +10%(默认)·+5% 短腿 · 35-45 DTE(你的逻辑)"}
    ret5 = c[-1] / c[-6] - 1.0
    rets = [c[-i] / c[-i - 1] - 1.0 for i in range(1, 6)]
    down_days = sum(1 for r in rets if r < 0)
    sma10 = sum(c[-10:]) / 10.0
    r5p = ret5 * 100.0
    if ret5 <= -0.012 or (ret5 < -0.003 and down_days >= 3):
        reg = "fell"
    elif ret5 >= 0.008 and c[-1] > sma10:
        reg = "rising"
    else:
        reg = "neutral"
    label = f"ATM 长腿 / +10%(默认)·+5% 短腿 · 35-45 DTE(你的逻辑)· 大盘近一周 {r5p:+.1f}%"
    return {"regime": reg, "ret5": round(r5p, 2), "down_days": down_days, "label": label}


# ── 方向投票(纯函数)────────────────────────────────────────────────────
def direction_of(row: dict) -> Optional[str]:
    bull = bear = 0
    t = row.get("touch")
    if t:
        if t.get("kind") == "sup":
            bull += 1
        elif t.get("kind") in ("res", "inside"):
            bear += 1
    ssp = row.get("ssp") or {}
    if ssp.get("bull_alert"):
        bull += 1
    if ssp.get("bear_alert"):
        bear += 1
    e = (row.get("edge") or {}).get("cur") or {}
    if e.get("sig"):
        if e.get("dir") == "多":
            bull += 1
        elif e.get("dir") == "空":
            bear += 1
    if bull > bear and bull >= 1:
        return "bull"
    if bear > bull and bear >= 1:
        return "bear"
    return None


# ── 到期挑选(纯函数)────────────────────────────────────────────────────
def pick_expiration(exps: list, lo: int, hi: int, target: int,
                    prefer_monthly: bool = True) -> Optional[dict]:
    """窗口内优先月期权(type=Regular),同类取最接近 target;窗口空了放宽到全集最近 target。"""
    cand = [e for e in (exps or []) if lo <= e.get("dte", -1) <= hi]
    if cand:
        if prefer_monthly:
            mon = [e for e in cand if str(e.get("type", "")).lower() == "regular"]
            if mon:
                cand = mon
        return min(cand, key=lambda e: abs(e.get("dte", 0) - target))
    pool = [e for e in (exps or []) if e.get("dte", -1) >= 0]
    return min(pool, key=lambda e: abs(e.get("dte", 0) - target)) if pool else None


# ── 腿挑选(纯函数,吃 fetch_chain 的 strikes)────────────────────────────
def _rows_with(chain_strikes: list, side: str) -> list:
    out = []
    for s in chain_strikes or []:
        k = s.get("strike")
        leg = s.get(side) or {}
        if k and k > 0:
            out.append({"strike": float(k), "mid": float(leg.get("mid") or 0.0),
                        "bid": float(leg.get("bid") or 0.0), "ask": float(leg.get("ask") or 0.0),
                        "iv": float(leg.get("iv") or 0.0)})
    out.sort(key=lambda r: r["strike"])
    return out


def _nearest(rows: list, target: float) -> Optional[dict]:
    return min(rows, key=lambda r: abs(r["strike"] - target)) if rows else None


def _build_vertical(long_row: dict, short_row: dict, spot: float, variant: str,
                    direction: str, dte: Optional[float] = None) -> Optional[dict]:
    """通用 debit vertical:bull=call(long 低 short 高),bear=put(long 高 short 低)。
    max_loss/max_profit = 每张($,×100);pop = 盈利概率(给了 dte 才算,用 long 腿隐波)。"""
    if not long_row or not short_row:
        return None
    if direction == "bull":
        if short_row["strike"] <= long_row["strike"]:
            return None
        width = short_row["strike"] - long_row["strike"]
        be = long_row["strike"] + (long_row["mid"] - short_row["mid"])
    else:
        if short_row["strike"] >= long_row["strike"]:
            return None
        width = long_row["strike"] - short_row["strike"]
        be = long_row["strike"] - (long_row["mid"] - short_row["mid"])
    debit = long_row["mid"] - short_row["mid"]
    if debit <= 0 or long_row["mid"] <= 0 or short_row["mid"] <= 0 or debit >= width:
        return None
    pop = spread_pop(spot, be, long_row.get("iv") or 0.0, dte, direction) if dte else None
    return {"variant": variant, "long_strike": long_row["strike"], "short_strike": short_row["strike"],
            "width": round(width, 4), "debit": round(debit, 4),
            "max_payoff": round(width - debit, 4),
            "max_loss": round(debit * 100.0, 2), "max_profit": round((width - debit) * 100.0, 2),
            "rr": round((width - debit) / debit, 2),
            "breakeven": round(be, 4),
            "be_vs_spot_pct": round((be / spot - 1) * 100.0, 2),
            "long_iv": round(long_row.get("iv") or 0.0, 4), "pop": pop}


# 作者逻辑:ATM 长腿,短腿 ±5%(默认主)/ ±10%(更宽)= 1-2 个候选(bull/bear 镜像)
BULL_VARIANTS = [("ATM/+5%", 1.00, 1.05), ("ATM/+10%", 1.00, 1.10)]
BEAR_VARIANTS = [("ATM/−5%", 1.00, 0.95), ("ATM/−10%", 1.00, 0.90)]


def build_candidates(chain_strikes: list, spot: float, direction: str,
                     dte: Optional[float] = None) -> list:
    """一条链上的全部腿型候选(bull 3 / bear 3),去重(同 strikes 只留一个)。
    给 dte → 候选带 pop(盈利概率)。"""
    side = "call" if direction == "bull" else "put"
    rows = _rows_with(chain_strikes, side)
    if len(rows) < 2 or spot <= 0:
        return []
    out, seen = [], set()
    for name, lo_m, hi_m in (BULL_VARIANTS if direction == "bull" else BEAR_VARIANTS):
        long_row = _nearest(rows, spot * lo_m)
        short_row = _nearest(rows, spot * hi_m)
        c = _build_vertical(long_row, short_row, spot, name, direction, dte)
        if c:
            key = (c["long_strike"], c["short_strike"])
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def pick_primary(cands: list, direction: str, spot: float) -> Optional[int]:
    """主推荐:bull = 默认取更宽的(+10%, 作者默认宽度 10%, +5% 留作候选);bear 不推荐(仅方向)。"""
    if not cands:
        return None
    if direction == "bull":
        return max(range(len(cands)), key=lambda i: cands[i]["width"])   # 作者:默认 10% 宽度
    return max(range(len(cands)), key=lambda i: cands[i]["rr"])


def _fmt_k(k: float) -> str:
    return str(int(k)) if abs(k - round(k)) < 1e-9 else f"{k:g}"


def make_ticket(ticker: str, direction: str, expiration: str, rec: dict) -> str:
    try:
        exp_s = _dt.datetime.strptime(expiration[:10], "%Y-%m-%d").strftime("%y-%m-%d")
    except Exception:
        exp_s = expiration
    cp = "C" if direction == "bull" else "P"
    return (f"BUY +1 {ticker} {exp_s} {_fmt_k(rec['long_strike'])}{cp} / "
            f"SELL {_fmt_k(rec['short_strike'])}{cp} @ -{rec['debit']:.2f} LMT (借记)")


def earnings_before_exp(earnings_date: Optional[str], expiration: str,
                        today: Optional[_dt.date] = None) -> bool:
    """财报落在持仓期内:今天 ≤ 财报日 ≤ 到期日。
    (TT 对未公布下次财报的票返回上次财报日 → 过去日期不算警告。)"""
    if not earnings_date:
        return False
    try:
        ed = _dt.date.fromisoformat(str(earnings_date)[:10])
        xd = _dt.date.fromisoformat(str(expiration)[:10])
        td = today or _dt.date.today()
        return td <= ed <= xd
    except Exception:
        return False


# ── 编排:给排序后的 top-N 行挂 spread_rec(并发拉链)──────────────────────
def row_direction(row: dict) -> Optional[str]:
    """方向(给推荐用):①证据制评分方向(score>0)→ ②今日信号投票 →
    ③**兜底:长期趋势**(长 bull→bull call,长 bear→bear put)。
    目的:只要这只票有明确长期趋势,就一定给 debit spread 推荐,不因今日无信号而空着。"""
    sd = row.get("score_detail") or {}
    if (sd.get("score") or 0) > 0 and sd.get("dir"):
        return sd["dir"]
    d = direction_of(row)
    if d:
        return d
    lt = ((row.get("setup") or {}).get("trend") or {}).get("long")
    if lt == "bull":
        return "bull"
    if lt == "bear":
        return "bear"
    return None


def _rec_for_row(row, client, fetch_expirations, fetch_chain,
                 bull_dte, bull_alt, dte_reason) -> Optional[dict]:
    """单票:选到期→拉链→建 3-5 候选→主推荐。返回 spread_rec 或 None(失败/无方向/无链)。"""
    d = row_direction(row)
    if d != "bull":          # 作者:bear 只提示方向, 不推荐开单 → 不生成 spread_rec
        return None
    t = row["ticker"]
    try:
        exps = fetch_expirations(client, t)
        lo, hi, tgt = bull_dte if d == "bull" else BEAR_DTE
        exp = pick_expiration(exps, lo, hi, tgt, prefer_monthly=(d == "bull"))
        if not exp:
            return None
        ew_date = (row.get("earnings") or {}).get("date")
        cands = []

        def add_from(exp_obj, variant_limit=None):
            chain = fetch_chain(client, t, exp_obj["date"])
            spot_ = float(chain.get("spot") or 0.0) or float(row.get("close") or 0.0)
            cs = build_candidates(chain.get("strikes"), spot_, d, exp_obj.get("dte"))
            if variant_limit:
                cs = cs[:variant_limit]
            for c in cs:
                c.update({"expiration": exp_obj["date"], "dte": exp_obj.get("dte"),
                          "exp_type": exp_obj.get("type", ""),
                          "earnings_warn": earnings_before_exp(ew_date, exp_obj["date"]),
                          "ticket": make_ticket(t, d, exp_obj["date"], c)})
            cands.extend(cs)
            return spot_

        spot = add_from(exp)
        # 作者逻辑:只 1 个到期(35-45)× ATM/+5%·+10% 两个宽度 = 1-2 候选,不再凑多到期
        if not cands:
            return None
        pi = pick_primary(cands, d, spot)
        primary = cands[pi]
        rest = sorted((c for j, c in enumerate(cands) if j != pi), key=lambda c: -(c.get("rr") or 0))
        # RSI 门槛警示:超买开 bull / 超卖开 bear(推荐照给,标 ⚠️ 提醒别追)
        rsi_now = (row.get("tasig") or {}).get("rsi")
        gate_warn = bool(rsi_now is not None and
                         ((d == "bull" and rsi_now >= 75) or (d == "bear" and rsi_now <= 25)))
        return {"dir": d, "n": len([primary] + rest), "primary": primary,
                "candidates": [primary] + rest,
                "dte_reason": dte_reason or "ATM 长腿 / +10%(默认)·+5% 短腿 · 35-45 DTE",
                "rsi_warn": gate_warn, "rsi_now": rsi_now,
                "earnings_warn": bool(primary.get("earnings_warn"))}
    except Exception:
        return None


def recommend_one(row: dict, client, market: dict = None) -> Optional[dict]:
    """单票按需推荐(前端展开行时调用),Bull 到期按 market regime。无 client/链 → None。"""
    if client is None:
        return None
    try:
        from strategy_builder_api import fetch_expirations, fetch_chain
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "options_analyzer"))
        from strategy_builder_api import fetch_expirations, fetch_chain
    reg = (market or {}).get("regime", "neutral")
    return _rec_for_row(row, client, fetch_expirations, fetch_chain,
                        BULL_DTE_BY_REGIME.get(reg, BULL_DTE),
                        BULL_ALT_BY_REGIME.get(reg, (35, 45, 40)),
                        (market or {}).get("label", ""))


def recommend_for_rows(rows: list, client, top_n: int = TOP_N,
                       bull_dte=None, bull_alt=None, dte_reason: str = "",
                       max_workers: int = 8, log=print) -> int:
    """给评分最高的前 top_n 个**有方向**的行并发挂 spread_rec(3-5 候选)。
    Bull 到期窗口由 bull_dte 自适应大盘(见 market_regime)。返回成功数。"""
    if client is None:
        return 0
    bull_dte = bull_dte or BULL_DTE
    bull_alt = bull_alt or (35, 45, 40)
    try:
        from strategy_builder_api import fetch_expirations, fetch_chain
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "options_analyzer"))
        from strategy_builder_api import fetch_expirations, fetch_chain
    # rows 已按评分降序:取前 top_n 个有方向的
    targets = []
    for row in rows:
        if row_direction(row) == "bull":   # 只给 bull 推荐(bear 不开单, 仅方向提示)
            targets.append(row)
        if len(targets) >= top_n:
            break
    if not targets:
        return 0
    import concurrent.futures as _cf
    done = 0
    try:
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_rec_for_row, row, client, fetch_expirations, fetch_chain,
                              bull_dte, bull_alt, dte_reason): row for row in targets}
            for fut in _cf.as_completed(futs):
                rec = None
                try:
                    rec = fut.result()
                except Exception:
                    rec = None
                if rec:
                    futs[fut]["spread_rec"] = rec
                    done += 1
    except Exception as e:
        log(f"[ta_screener] spread 并发推荐失败,退回串行: {e}")
        for row in targets:
            rec = _rec_for_row(row, client, fetch_expirations, fetch_chain, bull_dte, bull_alt, dte_reason)
            if rec:
                row["spread_rec"] = rec
                done += 1
    log(f"[ta_screener] spread 推荐: {done}/{len(targets)} 票(top_n={top_n})")
    return done

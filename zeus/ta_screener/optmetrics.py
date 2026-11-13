"""期权结构指标(自算,近 2 个月度到期合并)→ 有逻辑的算分修正。

数据:strategy_builder_api.fetch_chain 的 strikes(call/put 各有 oi/vol/iv)。
四个指标,每个都有明确的市场结构含义(非拍脑袋):

1. **PCR(OI)** put/call 未平仓比:<0.7 = call 重仓(看涨倾斜)、>1.3 = put 重仓。
   读数为持仓倾斜(非逆向情绪口径,统一、可解释)。
2. **OI 重心 vs 现价**:全部 OI 按行权价加权的中心。重心在现价上方 = 持仓
   布局偏上方(看涨);下方 = 偏下方。直接回答作者"近2个月度期权都在
   目前价格上方还是下方"。
3. **Max Pain 牵引**:到期"让期权买方总赔付最小"的价位;临近到期价格有向
   max pain 漂移的 pinning 效应。MP 在现价上方 → 向上牵引。两个月度按 OI 加权。
4. **GEX(dealer gamma)**:经典近似(call OI 记 +gamma、put OI 记 −gamma,
   dealer 持仓假设)。总 GEX>0 = 抑制波动(粘住),<0 = 放大波动;
   现价在翻转位(累计 GEX 变号的行权价)上方 = 正 gamma 区(稳/托底),
   下方 = 负 gamma 区(下跌自加速)。

**算分(bias)**:每项 ±2.5 封顶、线性、合计 ∈ [−10, +10]:
   max pain:  clamp(ΔMP%/5%) ×2.5      (5% 距离打满)
   OI 重心:   clamp(Δ重心%/5%) ×2.5
   GEX:       clamp((现价−翻转位)%/3%) ×2.5
   PCR:       clamp((1.0−PCR)/0.3) ×2.5 (0.7→+满,1.3→−满)
应用:方向 bull → +bias,bear → −bias(结构与方向一致加分、矛盾减分)。
只是证据分(实测胜率)的有界修正,排序主体仍是胜率。
"""
from __future__ import annotations

import math
from typing import Optional

CAP_EACH = 2.5      # 单项封顶
MP_FULL = 0.05      # max pain 距离 5% 打满
CENTER_FULL = 0.05  # OI 重心距离 5% 打满
GEX_FULL = 0.03     # 翻转位距离 3% 打满
PCR_BAND = 0.3      # PCR 1.0±0.3 线性带
# ── ② 期权 OI/Vol 方向结构(评分 v3 大块,0–20 只加不减)──
FLOW_DECAY = 0.15   # 距离衰减:exp(−|k/spot−1|/0.15),5%→0.72 / 10%→0.51 / 20%→0.26
FLOW_LO = 45.0      # 同向占比 ≤45% → 0 分(均衡或反向都不给,但不为负)
FLOW_HI = 80.0      # 占比 ≥80% → 打满
FLOW_CAP = 20.0


GEX_MAX_DTE = 60    # GEX 只用 ≤60 天到期的链(作者口径)
PCR_PTS = 5.0       # PCR 小块满分(>1 bear 加 / <1 bull 加,只加不减)
GEX_PTS = 5.0       # GEX 区位小块满分(现价高于翻转位 bull 加 / 低于 bear 加)


def flow_points(metrics: Optional[dict], direction: Optional[str]) -> float:
    """② 块得分:bull 看「上方 call 量」占比,bear 看「下方 put 量」占比。
    share = 同向侧 / (上call+下put),45%→0 线性到 80%→满 20。永不为负。"""
    if not metrics or direction not in ("bull", "bear"):
        return 0.0
    share = metrics.get("flow_bull_share")
    if share is None:
        return 0.0
    s = float(share) if direction == "bull" else 100.0 - float(share)
    return round(max(0.0, min(FLOW_CAP, (s - FLOW_LO) / (FLOW_HI - FLOW_LO) * FLOW_CAP)), 1)


def pcr_points(metrics: Optional[dict], direction: Optional[str]) -> float:
    """PCR(OI,近2月度) 小块:<1 → bull 加分、>1 → bear 加分(1.0±0.3 线性带打满),
    方向不合 = 0,永不为负。"""
    if not metrics or direction not in ("bull", "bear"):
        return 0.0
    pcr = metrics.get("pcr_oi")
    if pcr is None:
        return 0.0
    x = (1.0 - float(pcr)) / PCR_BAND          # <1 → 正(call 重仓), >1 → 负(put 重仓)
    if direction == "bear":
        x = -x
    return round(max(0.0, min(1.0, x)) * PCR_PTS, 1)


def gex_points(metrics: Optional[dict], direction: Optional[str]) -> float:
    """GEX 区位小块(≤60 天链):现价高于 gamma 翻转位 → bull 加分(正 gamma 稳),
    低于 → bear 加分(负 gamma 自加速)。3% 距离打满,方向不合 = 0,永不为负。"""
    if not metrics or direction not in ("bull", "bear"):
        return 0.0
    f_pct = metrics.get("gex_flip_pct")        # 现价相对翻转位 %(正=在上方)
    if f_pct is None:
        return 0.0
    x = (f_pct / 100.0) / GEX_FULL
    if direction == "bear":
        x = -x
    return round(max(0.0, min(1.0, x)) * GEX_PTS, 1)


def _bs_gamma(S: float, K: float, T: float, iv: float, r: float = 0.045) -> float:
    if S <= 0 or K <= 0 or T <= 0 or iv <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        return pdf / (S * iv * math.sqrt(T))
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0


def _f(x) -> float:
    """安全转 float:None/NaN/inf → 0(EOD 链零成交合约 Volume=NaN,
    而 `nan or 0` 仍是 nan,会把流向求和整段污染成 NaN)。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if (v == v and v not in (float("inf"), float("-inf"))) else 0.0


def _rows(chain: dict) -> list:
    out = []
    for s in (chain or {}).get("strikes", []):
        k = s.get("strike")
        if not k or k <= 0:
            continue
        c, p = s.get("call") or {}, s.get("put") or {}
        out.append({
            "k": float(k),
            "c_oi": _f(c.get("oi")), "p_oi": _f(p.get("oi")),
            "c_vol": _f(c.get("vol")), "p_vol": _f(p.get("vol")),
            "c_iv": _f(c.get("iv")), "p_iv": _f(p.get("iv")),
        })
    out.sort(key=lambda r: r["k"])
    return out


def _max_pain(rows: list) -> Optional[float]:
    """argmin_S Σ[c_oi·max(0,S−K) + p_oi·max(0,K−S)](标准 max pain,网格=行权价)。"""
    if not rows:
        return None
    best_k, best_pay = None, None
    for cand in rows:
        S = cand["k"]
        pay = 0.0
        for r in rows:
            pay += r["c_oi"] * max(0.0, S - r["k"]) + r["p_oi"] * max(0.0, r["k"] - S)
        if best_pay is None or pay < best_pay:
            best_pay, best_k = pay, S
    return best_k


def compute_metrics(chains: list, spot: float) -> Optional[dict]:
    """chains = [(chain_dict, T_years), ...](近 2 个月度)。OI 全空 → None。"""
    if not chains or spot <= 0:
        return None
    tot_c_oi = tot_p_oi = tot_c_vol = tot_p_vol = 0.0
    wsum = w = 0.0                      # OI 重心
    oi_above = oi_below = 0.0
    flow_up = flow_dn = 0.0             # ② 方向结构:上方 call / 下方 put 的(OI+Vol)·距离衰减
    mps = []                            # (max_pain, expiry_oi)
    gex_per_strike: dict[float, float] = {}
    for chain, T in chains:
        rows = _rows(chain)
        if not rows:
            continue
        e_oi = 0.0
        for r in rows:
            oi = r["c_oi"] + r["p_oi"]
            tot_c_oi += r["c_oi"]; tot_p_oi += r["p_oi"]
            tot_c_vol += r["c_vol"]; tot_p_vol += r["p_vol"]
            wsum += r["k"] * oi; w += oi; e_oi += oi
            if r["k"] > spot:
                oi_above += oi
            elif r["k"] < spot:
                oi_below += oi
            # 作者口径:call 堆现价上方=看涨证据 / put 堆现价下方=看跌证据,量×距离衰减
            dist = abs(r["k"] / spot - 1.0)
            wgt = math.exp(-dist / FLOW_DECAY)
            if r["k"] > spot:
                flow_up += (r["c_oi"] + r["c_vol"]) * wgt
            elif r["k"] < spot:
                flow_dn += (r["p_oi"] + r["p_vol"]) * wgt
            # GEX:call +、put −(dealer 假设);$ gamma 每 1% 现价变动。只用 ≤60 天链(作者口径)
            if T <= GEX_MAX_DTE / 365.0 + 1e-9:
                gc = _bs_gamma(spot, r["k"], T, r["c_iv"]) * r["c_oi"]
                gp = _bs_gamma(spot, r["k"], T, r["p_iv"]) * r["p_oi"]
                gex_per_strike[r["k"]] = gex_per_strike.get(r["k"], 0.0) + (gc - gp) * 100.0 * spot * spot * 0.01
        mp = _max_pain(rows)
        if mp is not None and e_oi > 0:
            mps.append((mp, e_oi))
    if w <= 0:
        return None
    pcr_oi = (tot_p_oi / tot_c_oi) if tot_c_oi > 0 else None
    pcr_vol = (tot_p_vol / tot_c_vol) if tot_c_vol > 0 else None
    center = wsum / w
    mp_blend = sum(m * o for m, o in mps) / sum(o for _, o in mps) if mps else None
    # GEX 总量 + 翻转位(累计变号近似)
    ks = sorted(gex_per_strike)
    total_gex = sum(gex_per_strike.values())
    flip = None
    cum = 0.0
    for k in ks:
        prev = cum
        cum += gex_per_strike[k]
        if prev < 0 <= cum or prev > 0 >= cum:
            flip = k                      # 第一次变号处
            break
    return {
        "pcr_oi": round(pcr_oi, 3) if pcr_oi is not None else None,
        "pcr_vol": round(pcr_vol, 3) if pcr_vol is not None else None,
        "oi_center": round(center, 2),
        "oi_center_pct": round((center / spot - 1) * 100.0, 2),
        "oi_above_share": round(oi_above / w * 100.0, 1),
        "max_pain": round(mp_blend, 2) if mp_blend is not None else None,
        "max_pain_pct": round((mp_blend / spot - 1) * 100.0, 2) if mp_blend is not None else None,
        "gex_total": round(total_gex, 0),
        "gex_flip": flip,
        "gex_flip_pct": round((spot / flip - 1) * 100.0, 2) if flip else None,
        "total_oi": int(w),
        # ② 方向结构:上方 call vs 下方 put 的(OI+Vol)·距离衰减占比(bull 视角 %)
        "flow_up_w": round(flow_up, 0),
        "flow_dn_w": round(flow_dn, 0),
        "flow_bull_share": round(flow_up / (flow_up + flow_dn) * 100.0, 1)
                           if (flow_up + flow_dn) > 0 else None,
    }


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def bias(metrics: Optional[dict], spot: float) -> dict:
    """{total ∈ [−10,10], parts:[{key,label,reading,pts}]}。metrics None → 0。"""
    out = {"total": 0.0, "parts": []}
    if not metrics or spot <= 0:
        return out
    parts = []
    mp_pct = metrics.get("max_pain_pct")
    if mp_pct is not None:
        pts = _clamp((mp_pct / 100.0) / MP_FULL) * CAP_EACH
        parts.append({"key": "maxpain", "label": "MaxPain牵引",
                      "reading": f"MP {metrics['max_pain']}({mp_pct:+.1f}%)", "pts": round(pts, 2)})
    c_pct = metrics.get("oi_center_pct")
    if c_pct is not None:
        pts = _clamp((c_pct / 100.0) / CENTER_FULL) * CAP_EACH
        parts.append({"key": "center", "label": "OI重心",
                      "reading": f"{metrics['oi_center']}({c_pct:+.1f}%·上方OI {metrics.get('oi_above_share')}%)",
                      "pts": round(pts, 2)})
    f_pct = metrics.get("gex_flip_pct")
    if f_pct is not None:
        pts = _clamp((f_pct / 100.0) / GEX_FULL) * CAP_EACH
        parts.append({"key": "gex", "label": "GEX区位",
                      "reading": f"翻转{metrics['gex_flip']}(现价{f_pct:+.1f}%·总GEX{'+' if metrics.get('gex_total',0)>=0 else '−'})",
                      "pts": round(pts, 2)})
    pcr = metrics.get("pcr_oi")
    if pcr is not None:
        pts = _clamp((1.0 - pcr) / PCR_BAND) * CAP_EACH
        parts.append({"key": "pcr", "label": "PCR(OI)",
                      "reading": f"{pcr}", "pts": round(pts, 2)})
    out["parts"] = parts
    out["total"] = round(max(-10.0, min(10.0, sum(p["pts"] for p in parts))), 2)
    return out


def attach_for_rows(rows: list, client, row_direction, top_n: int = 400,
                    max_workers: int = 8, log=print) -> int:
    """并发给有方向的行附 row['opt_metrics']/row['opt_bias']。近 2 个月度链
    (fetch_expirations type=Regular 前两个;TTL 缓存与 spread 推荐共享)。"""
    if client is None:
        return 0
    try:
        from strategy_builder_api import fetch_expirations, fetch_chain
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "options_analyzer"))
        from strategy_builder_api import fetch_expirations, fetch_chain

    targets = []
    for r in rows:
        if row_direction(r):
            targets.append(r)
        if len(targets) >= top_n:
            break

    def _one(row):
        t = row["ticker"]
        try:
            exps = fetch_expirations(client, t)
            monthlies = [e for e in exps if str(e.get("type", "")).lower() == "regular"
                         and e.get("dte", -1) >= 3][:2]
            if not monthlies:
                monthlies = [e for e in exps if e.get("dte", -1) >= 14][:2]
            if not monthlies:
                return False
            chains = []
            spot = 0.0
            for e in monthlies:
                ch = fetch_chain(client, t, e["date"])
                spot = float(ch.get("spot") or 0.0) or spot or float(row.get("close") or 0.0)
                chains.append((ch, max(1e-4, float(e.get("dte") or 1) / 365.0)))
            m = compute_metrics(chains, spot)
            if not m:
                return False
            row["opt_metrics"] = m
            row["opt_bias"] = bias(m, spot)
            return True
        except Exception:
            return False

    import concurrent.futures as _cf
    done = 0
    try:
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for ok in ex.map(_one, targets):
                if ok:
                    done += 1
    except Exception as e:
        log(f"[ta_screener] opt metrics 并发失败: {e}")
    log(f"[ta_screener] 期权结构: {done}/{len(targets)} 票")
    return done

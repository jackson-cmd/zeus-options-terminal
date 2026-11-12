"""市场 regime + 波动 → regime 自适应、两档(建议/强制)、按剩余DTE收紧的止盈带。

回测结论(backtest_factory CONCLUSIONS §9):看涨借方价差最优止盈档随 regime 移动——
牛市(SPY>200SMA)吃 70-80%、震荡/弱 50-60%;100% 死等每段都最差。本模块把它做成:
  · regime:SPY>200SMA = 牛;但 **VIX≥阈值(默认22)→ 降级为震荡**(波动大、利润跑得快→快止盈)
  · 两档:soft=建议落袋(🔵),forced=强制平(🔴)
  · 剩余 DTE<阈值(默认20)→ 时间不多,**提前到 soft 就强制**(别等 forced,gamma/反转风险高)

数据 cache 优先(SPY 复用 ta_screener ohlc 缓存;VIX 走 yfinance 小拉),日更缓存到
options_analyzer/data/regime_cache.json;任何失败回退上次已知,再不行回退中性。慢变量,日更即可。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

_CACHE = Path(__file__).resolve().parents[1] / "data" / "regime_cache.json"

DTE_GATE = 20        # 剩余 DTE < 此 → 提前到 soft 强制止盈
VIX_HIGH = 22.0      # SPY>200SMA 但 VIX ≥ 此 → 视为震荡(波动大、快止盈)

# 止盈带(占最大利润):near=接近, soft=建议落袋, forced=强制平
BANDS = {
    "bull": {"near": 0.60, "soft": 0.70, "forced": 0.80, "label": "牛市", "zone": "70-80%"},
    "chop": {"near": 0.40, "soft": 0.50, "forced": 0.60, "label": "震荡/弱", "zone": "50-60%"},
    None:   {"near": 0.55, "soft": 0.65, "forced": 0.75, "label": "中性", "zone": "65-75%"},
}


def band(regime: str | None) -> dict:
    b = dict(BANDS.get(regime, BANDS[None]))
    b["dte_gate"] = DTE_GATE
    return b


VIX_OTM = 25.0   # SPY>200SMA 且 VIX≥此 = 牛市恐慌回调 → OTM 抄底赌暴涨;否则 ITM


def strike_rec(above: bool, vix: float | None) -> dict:
    """regime 自适应行权价(VIX 分桶回测验证):
       · 正常牛市(低-中 VIX)→ 长腿略 ITM(-5%),breakeven 略低于现价:胜率高、拿得住。
       · 牛市恐慌回调(SPY>200SMA 但 VIX≥25)→ OTM(+2%),抄底赌暴涨:V 型反弹时 OTM 杠杆吃满(回测中位+125%)。
       · 跌破 200SMA(真熊)→ 仍 ITM,别 OTM 抄底(一路阴跌会打穿)。
    返回 {mode, long_off, label, detail, be_hint}。"""
    panic_dip = bool(above and vix is not None and vix >= VIX_OTM)
    if panic_dip:
        return {"mode": "otm", "long_off": 0.02,
                "label": "牛市恐慌回调 → OTM 抄底赌暴涨",
                "detail": f"SPY 仍在 200SMA 上但 VIX {vix:.0f}≥{VIX_OTM:.0f}:暴跌后多 V 型反弹,"
                          "长腿放 OTM 用杠杆博反弹(回测此情形 OTM 中位 +125%)。",
                "be_hint": "breakeven 放现价之上(+3~4%)"}
    if not above:
        return {"mode": "itm", "long_off": -0.05,
                "label": "跌破 200SMA → ITM 保守(别 OTM 抄底)",
                "detail": "真熊一路阴跌会打穿抄底单;长腿偏 ITM、breakeven 压现价之下,先求拿得住。",
                "be_hint": "breakeven 略低于现价(约 -2%)"}
    return {"mode": "itm", "long_off": -0.05,
            "label": "正常牛市 → ITM 稳扎",
            "detail": "低-中 VIX:长腿略 ITM(-5%)、breakeven 略低于现价,胜率高、跌了也拿得住。",
            "be_hint": "breakeven 略低于现价(约 -2%)"}


def force_at(b: dict, dte) -> float:
    """强制止盈下沿:DTE 充足用 forced;DTE<gate(时间不多)提前到 soft。"""
    try:
        if dte is not None and float(dte) < b.get("dte_gate", DTE_GATE):
            return b["soft"]
    except (TypeError, ValueError):
        pass
    return b["forced"]


def _vix_last() -> float | None:
    try:
        import numpy as np
        import yfinance as yf
        df = yf.download("^VIX", period="1mo", interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        vals = np.asarray(df["Close"]).ravel()          # robust to MultiIndex columns
        vals = vals[np.isfinite(vals)]
        return float(vals[-1]) if vals.size else None
    except Exception:
        return None


def _compute() -> dict | None:
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from ta_screener import data as D
    hist = D.download_history(["SPY"], use_cache=True)
    df = hist.get("SPY") if isinstance(hist, dict) else None
    if df is None or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 200:
        return None
    sma200 = float(close.tail(200).mean())
    spy = float(close.iloc[-1])
    if sma200 <= 0:
        return None
    above = spy > sma200
    vix = _vix_last()
    # regime:SPY 在 200SMA 上 = 牛;但 VIX 高 → 降级为震荡(波动大→快止盈)
    high_vol = (vix is not None and vix >= VIX_HIGH)
    regime = "chop" if (not above or high_vol) else "bull"
    reason = ("跌破200SMA" if not above else
              (f"VIX {vix:.0f}≥{VIX_HIGH:.0f} 高波动" if high_vol else "站上200SMA"))
    return {"regime": regime, "spy": round(spy, 2), "sma200": round(sma200, 2),
            "pct_above": round((spy / sma200 - 1) * 100, 1),
            "vix": round(vix, 1) if vix is not None else None,
            "vix_high": VIX_HIGH, "high_vol": high_vol, "reason": reason,
            "strike": strike_rec(above, vix),   # regime 自适应行权价(ITM / OTM抄底)
            "asof": str(close.index[-1].date())}


def market_regime(max_age_hours: float = 18.0) -> dict:
    """Return {regime:'bull'|'chop'|None, spy, sma200, vix, high_vol, reason, asof}. Never raises."""
    try:
        if _CACHE.exists():
            c = json.loads(_CACHE.read_text())
            ts = _dt.datetime.fromisoformat(c["_ts"])
            # schema guard: old caches lack 'vix'/'strike' → treat as stale, recompute
            if (_dt.datetime.now() - ts).total_seconds() < max_age_hours * 3600 \
                    and "strike" in (c.get("data") or {}):
                return c["data"]
    except Exception:
        pass
    try:
        d = _compute()
        if d:
            _CACHE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE.write_text(json.dumps({"_ts": _dt.datetime.now().isoformat(), "data": d}))
            return d
    except Exception:
        pass
    try:
        if _CACHE.exists():
            return json.loads(_CACHE.read_text())["data"]
    except Exception:
        pass
    return {"regime": None, "spy": None, "sma200": None, "vix": None,
            "high_vol": None, "reason": "数据缺失", "asof": None}


if __name__ == "__main__":
    print(market_regime())

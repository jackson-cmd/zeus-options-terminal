"""Server-side exit / take-profit watcher → native macOS push.

Fires a native notification (via osascript — the same channel the Weekly
Review reminder uses) when an open position has reached its profit target
while still having plenty of time left. The default rule:

    已实现浮盈 >= regime 自适应软档 of 理论最大利润  →  "止盈/强制止盈" push
    (牛市 70/80 · 震荡 50/60 · VIX高降级 · 剩余DTE<20提前强制;见 checkup/regime.py)

Captured a big chunk of the move early (lots of time still on the clock) is
exactly when locking it in tends to beat holding for the last sliver of
extrinsic. The thresholds are user-configurable from the PWA Alert panel,
synced to data/alert_config.json.

Why this lives server-side: optionstrat_server.py runs under launchd, so the
notification fires even when the PWA / browser is fully closed — which a
client-side `checkAlerts` cannot do.

Pure helpers (pnl %, predicate, config IO, evaluate) are split out here so
they can be unit-tested without the FastAPI app. optionstrat_server.py owns
the loop because it needs live access to `state.strategies` and
`is_market_hours()`.
"""
from __future__ import annotations

import asyncio
import json
import datetime as _dt
from pathlib import Path
from typing import Optional


# regime 自适应、两档止盈(占最大利润%)。soft=建议落袋下沿(触发推送);forced=强制平(soft+10)。
# 牛市 70/80、震荡 50/60、中性 65/75。剩余 DTE<DTE_GATE(20)→ 提前到 soft 就视为强制。
# 回测铁律见 backtest_factory CONCLUSIONS §9 / checkup/regime.py(VIX 高时 regime 已被降级为 chop)。
DEFAULT_CONFIG = {"enabled": True, "bullPct": 70, "chopPct": 50, "neutralPct": 65,
                  "sound": False, "freq": "daily"}
_ZONE = {"bull": "70-80%", "chop": "50-60%", None: "65-75%"}
DTE_GATE = 20


def default_config() -> dict:
    return dict(DEFAULT_CONFIG)


def threshold_for(cfg: dict, regime: str | None) -> float:
    """soft 触发下沿(0..1),按 regime 选档。"""
    if regime == "bull":
        return float(cfg.get("bullPct", 70)) / 100.0
    if regime == "chop":
        return float(cfg.get("chopPct", 50)) / 100.0
    return float(cfg.get("neutralPct", 65)) / 100.0


def forced_for(cfg: dict, regime: str | None) -> float:
    """强制下沿(0..1) = soft + 10pp(牛 80 / 震荡 60 / 中性 75)。"""
    return min(1.0, threshold_for(cfg, regime) + 0.10)


def load_config(path) -> dict:
    """Read the exit-rule config the PWA syncs to disk. Accepts either the full
    alert_config blob ({"enabled":.., "exit":{..}}) or a bare exit dict. Always
    returns a complete config, falling back to defaults on any problem."""
    cfg = default_config()
    try:
        p = Path(path)
        if not p.exists():
            return cfg
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            return cfg
        # Master "Alerts Enabled" switch turns the whole thing off.
        if raw.get("enabled") is False:
            cfg["enabled"] = False
        if raw.get("sound") is not None:
            cfg["sound"] = bool(raw["sound"])           # 声音(独立于总开关)
        ex = raw.get("exit", raw)
        if isinstance(ex, dict):
            if "enabled" in ex:
                cfg["enabled"] = cfg["enabled"] and bool(ex["enabled"])
            for k in ("bullPct", "chopPct", "neutralPct"):
                if ex.get(k) is not None:
                    cfg[k] = float(ex[k])
            if ex.get("freq") in ("daily", "always"):
                cfg["freq"] = ex["freq"]                 # 提醒频率
            # legacy single-threshold config → apply to both regimes
            if ex.get("minMaxProfitPct") is not None and "bullPct" not in ex:
                v = float(ex["minMaxProfitPct"])
                cfg["bullPct"] = cfg["chopPct"] = cfg["neutralPct"] = v
    except Exception:
        return default_config()
    return cfg


def position_pnl_pct(legs) -> Optional[float]:
    """Mark-to-market unrealized P/L as a % of |net debit|, matching the PWA's
    `analyzeStrategy().unrealizedPct`. Returns None when net debit is zero (can't
    express a %)."""
    net = 0.0
    cur = 0.0
    for leg in legs or []:
        try:
            q = float(leg.get("qty", 0) or 0)
            m = float(leg.get("mult", 100) or 100)
            net += float(leg.get("entry", 0) or 0) * q * m
            cur += float(leg.get("current", 0) or 0) * q * m
        except (TypeError, ValueError):
            continue
    if net == 0:
        return None
    return (cur - net) / abs(net) * 100.0


def _intrinsic(p: float, strike: float, typ: str) -> float:
    return max(0.0, p - strike) if (typ or "").upper().startswith("C") else max(0.0, strike - p)


def max_profit_progress(legs) -> Optional[float]:
    """已实现浮盈 / 理论最大利润, 与 PWA analyzeStrategy 同口径。
    用到期 payoff(分段线性, 折点在各 strike + 两端)求最大利润, 无需 BSM。
    None = 数据缺失 / 上方无界(裸多腿) / 最大利润<=0。"""
    rows, strikes = [], []
    for leg in legs or []:
        try:
            K = float(leg.get("strike") or 0)
            typ = (leg.get("type") or "").upper()[:1]
            q = float(leg.get("qty") or 0)
            m = float(leg.get("mult", 100) or 100)
            entry = float(leg.get("entry") or 0)
            cur = leg.get("current")
            cur = float(cur) if cur is not None else None
        except (TypeError, ValueError):
            return None
        if K <= 0 or q == 0 or typ not in ("C", "P"):
            return None
        rows.append((K, typ, q, m, entry, cur))
        strikes.append(K)
    if not rows or any(r[5] is None for r in rows):
        return None

    def payoff(p: float) -> float:
        return sum((_intrinsic(p, K, typ) - entry) * q * m for (K, typ, q, m, entry, _) in rows)

    top = max(strikes)
    max_profit = max(payoff(p) for p in ([0.0] + strikes + [top * 2 + 10]))
    if payoff(top * 4 + 10) > max_profit + 1e-6:   # 上方无界(裸多)→ 无法用"占最大利润%"
        return None
    if max_profit <= 0:
        return None
    unreal = sum((cur - entry) * q * m for (K, typ, q, m, entry, cur) in rows)
    return unreal / max_profit


def should_fire(prog: Optional[float], cfg: dict, regime: str | None = None) -> bool:
    """止盈预警:吃到 ≥ regime 自适应下沿的【最大利润】(牛 70 / 震荡 50 / 中性 65)。"""
    if not cfg.get("enabled", True):
        return False
    if prog is None:
        return False
    return prog >= threshold_for(cfg, regime)


def evaluate(strategies, cfg, fired_today: dict, now: _dt.datetime,
             regime: str | None = None) -> list:
    """Pure evaluation of every strategy against the regime-adaptive exit rule.

    At most ONE push per ticker per calendar day: `fired_today` maps ticker →
    the date it last fired; a ticker already fired today is skipped. The loop
    only runs on trading days during market hours (is_market_hours()), so
    weekends never fire. If a ticker has several qualifying spreads only the
    first is reported — one alert per ticker. Returns the {ticker, name, maxpct,
    dte, regime, zone} list to notify and mutates `fired_today`.
    """
    fires = []
    today = now.date()
    seen = set()
    zone = _ZONE.get(regime, _ZONE[None])
    soft = threshold_for(cfg, regime)
    forced = forced_for(cfg, regime)
    freq = cfg.get("freq", "daily")          # 'always'=每次(1分钟限流) / 'daily'=每天一次
    for s in strategies or []:
        try:
            tk = s.get("underlying", "") or ""
            if tk in seen:
                continue
            dte = float(s.get("dte", 0) or 0)
            prog = max_profit_progress(s.get("legs", []))
            if not should_fire(prog, cfg, regime):   # 进入 soft 区才推
                continue
            last = fired_today.get(tk)
            if freq == "always":                 # 每标的最快 1 分钟一次
                if isinstance(last, _dt.datetime) and (now - last).total_seconds() < 60:
                    continue
                fired_today[tk] = now
            else:                                # 每标的每天一次
                if last == today:
                    continue
                fired_today[tk] = today
            seen.add(tk)
            # DTE<gate 时 soft 即视为强制(时间不多);否则 prog>=forced 才强制
            fa = soft if dte < DTE_GATE else forced
            tier = "forced" if prog >= fa else "soft"
            fires.append({"ticker": tk, "name": s.get("name", ""),
                          "maxpct": round(prog * 100, 1), "dte": dte,
                          "regime": regime, "zone": zone, "tier": tier,
                          "forced_pct": round(forced * 100)})
        except Exception:
            continue
    return fires


async def send_macos_notification(title: str, message: str, sound: bool = False) -> None:
    """Native macOS notification via osascript. Inherits the server process's
    Terminal TCC, so it works under the existing launchd setup with no extra
    grants — identical mechanism to weekly_review_push. sound=True adds a chime."""
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    script = f'display notification "{m}" with title "{t}"'
    if sound:
        script += ' sound name "Glass"'
    cmd = ["osascript", "-e", script]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass

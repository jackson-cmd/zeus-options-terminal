"""开单前体检主引擎:跑 4 层 → 汇总 verdict → (可选)记录 override。

run_checkup() 是唯一的 IO 编排点:调 data_sources 拿数据,喂给 4 个纯函数层,拼 verdict。
本文件不 print —— 展示层在 cli.py。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Optional

from . import config
from . import data_sources as DS
from . import layer1_trend, layer2_touch, layer3_iv_earnings, layer4_macro

_DATA_DIR = Path(__file__).resolve().parent / "data"
_OVERRIDE_LOG = _DATA_DIR / config.OVERRIDE_LOG_NAME


def run_checkup(ticker: str, direction: str, dte: int = config.DEFAULT_DTE,
                 today: Optional[str] = None) -> dict:
    """direction: 'bull' | 'bear'。today: 'YYYY-MM-DD' 或 None(=用本地库里最新一根当"今天")。

    返回 {ticker, direction, dte, today, as_of, layers:{1..4}, failed_layers, pass, verdict}。
    """
    ticker = ticker.upper().strip()
    direction = direction.lower().strip()
    if direction not in ("bull", "bear"):
        raise ValueError(f"direction 必须是 'bull' 或 'bear',收到 {direction!r}")
    dte = int(dte)

    ticker_df = DS.get_ohlc(ticker)
    spy_df = DS.get_ohlc("SPY")

    l1 = layer1_trend.evaluate(ticker, direction, ticker_df, spy_df, today)
    l2 = layer2_touch.evaluate(ticker, direction, ticker_df, today)

    ivr_info = DS.get_ivr(ticker)
    today_date = _dt.date.fromisoformat(today) if today else _dt.date.today()
    earnings_info = DS.get_earnings(ticker, today_date)
    l3 = layer3_iv_earnings.evaluate(ticker, dte, ivr_info, earnings_info)

    digest_date = today or _dt.date.today().isoformat()
    items = DS.get_digest_today(digest_date)
    baseline = DS.risk_radar_baseline()
    saturation = DS.regime_saturation(items, baseline)
    counts = DS.count_topics(items)
    vix = DS.get_vix()
    l4 = layer4_macro.evaluate(vix, spy_df, saturation, counts, baseline.get("topics", {}))

    layers = {1: l1, 2: l2, 3: l3, 4: l4}
    failed = [n for n, l in layers.items() if not l["pass"]]

    as_of = None
    d = ticker_df if (today is None and ticker_df is not None and len(ticker_df)) else None
    if d is not None:
        as_of = str(d.index[-1].date())
    elif spy_df is not None and len(spy_df):
        as_of = str(spy_df.index[-1].date())

    return {
        "ticker": ticker, "direction": direction, "dte": dte,
        "today": today or _dt.date.today().isoformat(),
        "as_of": as_of,
        "layers": layers,
        "failed_layers": failed,
        "pass": len(failed) == 0,
        "verdict": "可以开单" if not failed else "别开单",
    }


def log_override(ticker: str, direction: str, dte: int, reason: str) -> dict:
    """记录一次"我知道有 fail 但还是要开"的 override。理由 < 20 字直接拒绝写入 ——
    本模块不拦真实下单(它本来就只是提示工具),但至少让你为这单写一段站得住脚的理由。
    """
    reason = (reason or "").strip()
    if len(reason) < config.OVERRIDE_MIN_REASON_LEN:
        raise ValueError(
            f"override 理由太短({len(reason)}字),至少要 {config.OVERRIDE_MIN_REASON_LEN} 字 —— "
            "认真想一下你在赌什么,再写一遍")
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker.upper(), "direction": direction, "dte": dte,
        "reason": reason,
    }
    with _OVERRIDE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry

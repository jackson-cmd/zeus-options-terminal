"""昨日开仓复盘 —— 回溯体检:把开仓当天的日期当 pre_trade_checkup 的 `today` 参数跑一遍
4 层体检,检验"当时"(而不是事后用今天的数据)技术面/IV/宏观是不是真的支持这笔单。
IO 只发生在 pre_trade_checkup.engine.run_checkup 内部(读本地 OHLC/IV/日历缓存),
这里不额外发起任何网络请求。
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from journal_expiry_whatif.engine import parse_spread_label
from pre_trade_checkup import config as _ptc_config


def _infer_dte(entry: dict) -> int:
    """优先从 spread_label 反解到期日算真实 DTE;解不出来就退化到
    pre_trade_checkup 自己的默认 DTE(40)。"""
    parsed = parse_spread_label(entry.get("spread_label"))
    od = entry.get("open_date")
    if parsed and od:
        try:
            open_date = dt.date.fromisoformat(od)
            return max(1, (parsed["exp"].date() - open_date).days)
        except ValueError:
            pass
    return _ptc_config.DEFAULT_DTE


def _find_override(overrides: list[dict], ticker: str, direction: str,
                    open_date: str) -> Optional[dict]:
    """override_log.jsonl 的记录没有 open_entry_id,只能按 ticker+方向配对,取该笔
    开仓当天或之后最近一条(目前该日志是空文件,这条路径靠 test_harness 的 mock 覆盖 ——
    现网还没有任何一条真实 override 可核对,这是已知局限,不是假装验证过)。"""
    cands = [
        o for o in overrides
        if (o.get("ticker") or "").upper() == ticker.upper()
        and (o.get("direction") or "").lower() == direction.lower()
        and (o.get("ts") or "")[:10] >= open_date
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda o: o.get("ts") or "")[0]


def build(opens: list[dict], yesterday: str,
          overrides: Optional[list[dict]] = None) -> list[dict]:
    from pre_trade_checkup import engine as _ptc_engine

    overrides = overrides or []
    rows = [o for o in opens if o.get("open_date") == yesterday]
    out = []
    for o in rows:
        ticker = (o.get("ticker") or "").strip()
        direction = (o.get("direction") or "").strip().lower()
        result = {
            "ticker": ticker,
            "direction": o.get("direction"),
            "spread_label": o.get("spread_label"),
            "retrospective_checkup_pass": None,
            "failed_layers_at_open": [],
            "was_override": False,
            "override_reason": None,
        }
        if direction in ("bull", "bear") and ticker:
            dte = _infer_dte(o)
            try:
                chk = _ptc_engine.run_checkup(ticker, direction, dte, today=yesterday)
                result["retrospective_checkup_pass"] = chk["pass"]
                result["failed_layers_at_open"] = chk["failed_layers"]
            except Exception as e:
                result["retrospective_checkup_error"] = str(e)
        ov = _find_override(overrides, ticker, direction, yesterday)
        if ov:
            result["was_override"] = True
            result["override_reason"] = ov.get("reason")
        out.append(result)
    return out

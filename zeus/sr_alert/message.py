"""通知文案 + 深链 —— 纯函数。

深链 http://localhost:8765/ta/?ticker=XXX 由 ta_screener/page.py:1313 接住:
读 ?ticker= 参数 → 自动 showTaDetail() 开该票 TA 详情。
"""
from __future__ import annotations

import os

from .detect import RES, SUP, Alert

_KIND_CN = {SUP: "回踩支撑", RES: "上攻阻力"}


def deep_link(ticker: str, port: int = 0) -> str:
    p = port or int(os.environ.get("PORT", "8765"))
    return f"http://localhost:{p}/ta/?ticker={ticker}"


def format_alert(a: Alert, port: int = 0) -> tuple:
    """→ (title, body, url),喂给 terminal-notifier / osascript。"""
    z = a.zone
    title = f"🔔 {a.ticker} {_KIND_CN.get(a.kind, a.kind)}"
    chg = ((a.price / a.prev_close - 1.0) * 100.0) if a.prev_close else 0.0
    body = (f"${a.price:.2f} ({chg:+.1f}%) 进入黄金 zone "
            f"{z['lo']:.2f}–{z['hi']:.2f} "
            f"({z.get('tfs', '?')} ×{z.get('n_pivots', '?')} pivot)")
    return title, body, deep_link(a.ticker, port)

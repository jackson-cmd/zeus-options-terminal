"""盘中 S/R 黄金 zone 实时触及告警 —— 纯函数包。

设计见 docs/superpowers/specs/2026-07-15-sr-zone-realtime-alert-design.md

数据流:
    EOD   : ta_screener/scanner.py → results.json (zones)  ─┐
    盘中   : heatmap DXLink → _reg["tickers"]{last,prev_close} ┴→ check_entry() → 通知

判定语义严格照搬 ta_screener/engine.py:303 zone_touch,不得改写。
"""
from .detect import Alert, check_entry, classify, fire_key
from .message import format_alert
from .zones import load_conf3_zones

__all__ = ["Alert", "check_entry", "classify", "fire_key",
           "format_alert", "load_conf3_zones"]

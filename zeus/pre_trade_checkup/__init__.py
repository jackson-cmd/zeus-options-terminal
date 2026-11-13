"""开单前体检 —— 四层过滤器,把作者自己写在  里的开单纪律做成代码闸门。

背景:2026-07-14~17 违反自己规则(下跌行情里开 bull call spread)+ 几天后恐慌平仓,
任何一层不过 → 直接说"别开单",不模棱两可。

四层:
  Layer 1 长期趋势  —— 票 + SPY 都要站在同一方向的 SMA200 上(大盘对齐)。
  Layer 2 EMA触及   —— 严格触及 EMA20/50/100/200 之一(回踩支撑/阻力,不是追涨杀跌)。
  Layer 3 IV/财报   —— IV 别太贵、财报别撞在 DTE 窗口里。
  Layer 4 宏观压制  —— VIX、SPY 短期急跌、benzinga 话题饱和度(作者特别要求这层用 benzinga)。

用法:python -m pre_trade_checkup.cli --ticker SPY --direction bull --dte 40
"""
from __future__ import annotations

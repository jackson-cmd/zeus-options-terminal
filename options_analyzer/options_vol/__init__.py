"""options_vol — 期权量稳定性筛选 tab。

合并 Barchart screener 导出 + TradingView watchlist 为 universe,复用 barchart 登录爬
options-history 每日量,按 ≥90% 交易日达标(5000/2000 两档,最近 1 个月窗口)分档,
稳定票画 vol/OI/IV 的 1M/3M 线段图。设计见
docs/superpowers/specs/2026-06-16-options-vol-tab-design.md。
"""

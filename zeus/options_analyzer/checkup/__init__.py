"""交易体检引擎(开单前预警 + 持仓止盈止损顾问)。

纯函数为主、可单测;IO(读 parquet / 抓链 / 读 journal)在各 loader 与
server 端点里做,核心算法只吃数据。设计见
docs/superpowers/specs/2026-06-13-trade-checkup-design.md。
"""

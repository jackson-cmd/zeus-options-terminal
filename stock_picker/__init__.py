"""stock_picker —— 技术优先选股引擎(给作者 bull call debit spread 出候选)。

12 层管线(见 engine.run()):
  T1  Barchart CSV 加载 + 类型转换 + 清洗(loader.py)
  T2  多头趋势硬过滤(filters.py) —— 预期在这层丢掉大多数行,是设计目标
  T3  非崩盘 / 非过热(filters.py)
  T4  财报窗口阻断(filters.py)
  T5  期权归档链是否存在(strike_engine.py)
  T6  期权流动性(filters.py)
  T7  EMA 触及信号(ema_touch.py)
  T8  宏观硬阻断 / 注解(filters.py, 复用 pre_trade_checkup.layer4_macro)
  T9  打分排序取 Top20(filters.py)
  T10 选 strike + 估算 debit(strike_engine.py)
  T11 Sonnet 新闻 / 宏观复核(macro_context.py + sonnet_analyzer.py)
  T12 出 ticket(ticket_writer.py)

用法:
  python -m stock_picker.cli [--csv path] [--today YYYY-MM-DD] [--no-sonnet] [--verbose]
自测:
  python -m stock_picker.test_harness
"""
from __future__ import annotations

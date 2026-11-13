"""stock_selector —— 双策略选股(端点 GET /api/stock_selector, 前端消费方是 heatmap
🚀AI选股 弹窗里的「⭐ 精选选股」tab, 见 heatmap/heatmap.py _hmPkPanelHtml)。

跟 stock_picker/ 是两个独立引擎,不是替换关系:
  · stock_picker/  —— 12 层单一漏斗(趋势跟踪一种打法),CLI-first,PWA 独立页面已删。
  · stock_selector/ —— 2 个并列策略(趋势跟踪 A / 抄底 B),各自独立打分排序。
    原内嵌在 PWA「🧭 市场判断」tab 的 sub-tab(mbSub('picker'))2026-07-30 已删,
    与 ai_picks/ 的广谱评级合并进同一个弹窗,避免两套选股入口并存。

CSV 加载 / 财报窗口 / 期权流动性 / 期权链读取 / strike 选择 全部直接 import 复用
stock_picker 的现成纯函数(见 engine.py 头注释),不重复实现 —— 这几层跟"选哪个
策略"无关,两个模块的差异只在策略过滤逻辑本身(strategies.py)。

流程(engine.run()):
  1. loader  —— 复用 stock_picker.loader(CSV 读取 + 清洗)
  2. 财报窗口 + 期权流动性 —— 复用 stock_picker.filters(两策略共用的前置过滤)
  3. strategies.py —— 策略 A(趋势跟踪)/ 策略 B(抄底)各自纯函数过滤
  4. ranker.py —— 各自打分排序取 top N
  5. strike_engine —— 复用 stock_picker.strike_engine(选 strike + 估算 debit)
  6. sonnet_analyzer.py —— 两策略候选去重合并, 一次性调 Sonnet 新闻/宏观复核
  7. engine.py 组装最终 dict(见 ENGINE OUTPUT 约定)

用法:
  python -m stock_selector.test_harness
"""
from __future__ import annotations

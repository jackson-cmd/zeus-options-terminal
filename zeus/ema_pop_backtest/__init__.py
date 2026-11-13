"""EMA × POP 校准回测(作者实验):

问题:
  1. Bull call / Bear put 借方价差, POP 理论(BSM lognormal) vs 实际胜率差多少?
  2. EMA 20/50/100/200 触及信号, 哪些真的能在 debit spread 上赚钱?
  3. 8y / 4y / 2y 三窗口结论一致吗?

设计:
  票池 = SPY + QQQ + tickers.csv 291 只里 seed 42 抽 100 只不含 SPY/QQQ = 102 只
  窗口 = 2018-01-01 / 2022-01-01 / 2024-01-01 起 (options archive 2018+ 完整)
  信号 = ema{20,50,100,200}_touch × {bull, bear} = 8 条
  策略 = ATM 长腿, 宽度 10%, DTE 35-45, 90% TP, 到期结算 intrinsic
"""

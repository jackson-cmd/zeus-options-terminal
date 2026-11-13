"""如果我拿到期会怎样 —— 每一单平仓的 hold-to-expiry what-if。

回答:
  - 我这单要是没砍,拿到到期日,会赚还是亏?
  - 白亏了多少?哪些单尤其冤?
  - 什么方向/什么 ticker/什么月份最爱早砍?

数据源:
  - trade_journal/data/entries_close.jsonl (实际平仓记录)
  - data/daily_ohlc.parquet (标的日线,用于到期日 Close)

方法:
  - 从 spread_label 解析 (long_K, short_K, C/P, exp_date)
  - 从 open_price 反推 debit_per_share (跟实际成本一致)
  - 用 exp_date 那天 (或最近一根) 的 Close 算 intrinsic
    * Bull call: intrinsic = clip(Close - long_K, 0, width)
    * Bear put:  intrinsic = clip(long_K - Close, 0, width)
  - hypothetical_pnl = (intrinsic - debit) * 100 * qty
  - regret = actual_pnl - hypothetical_pnl (正数 = 砍单更好, 负数 = 白砍)

注意:
  - 作者玩法是拿到期结算 intrinsic, 不算 TP GTC 触发的情况
  - 到期日缺 daily_ohlc 数据 = 标记 has_data=False, 不算平均
"""

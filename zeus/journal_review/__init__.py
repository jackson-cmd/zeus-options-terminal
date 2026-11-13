"""journal_review/ —— 「5 分钟开盘前复习」独立页面的引擎包。

跟 trade_journal/ 的关系:只读它的 entries_open.jsonl / entries_close.jsonl,
不改、不写。跟 pre_trade_checkup / journal_expiry_whatif 的关系:直接复用它们
已经验证过的纯函数(4 层体检 / 到期 what-if),不重新实现一遍判断逻辑。

唯一的 IO 编排点是 engine.build_review() —— 其余子模块(kpi/rule_check/
closed_yesterday/opened_yesterday/watchlist_today/insight)都是吃 dict 吐 dict
的纯函数,方便单测。
"""
from __future__ import annotations

#!/usr/bin/env python3
"""
barchart/pages.py — 要爬的 Barchart 列表页清单（配置驱动）

按 tab 分组。key 唯一，作为 CSV 文件名。
- 画图/技术图表类页面不在此列（按需求跳过）。
- Forex / Crypto / Futures 略（作者只看美股 + 期权）。
- 新增页面：照抄一行，填 url / tab / desc / prio 即可，无需改 scraper。
prio: 1=最高（重心），2=高，3=一般。
"""

PAGES: dict[str, dict] = {
    # ======================= Options（期权 — 重心）=======================
    # prio 经 2026-06-14 真实数据验证调整：unusual-activity 与 optionstrat 重复→降级；
    # iv_range(IV vs 已实现波动价差) 是真 edge→升 prio1；新增 upcoming-earnings。
    "opt_unusual_activity": {"url": "/options/unusual-activity",           "tab": "options", "prio": 3, "desc": "异动期权（与 optionstrat 重复, 降级）"},
    "opt_highest_iv":       {"url": "/options/highest-implied-volatility", "tab": "options", "prio": 2, "desc": "最高 IV（卖方机会）"},
    "opt_iv_rank":          {"url": "/options/iv-rank-percentile",         "tab": "options", "prio": 1, "desc": "IV Rank / Percentile（spread 择时）"},
    "opt_iv_change":        {"url": "/options/volatility-percent-change",  "tab": "options", "prio": 2, "desc": "IV 变化榜"},
    "opt_iv_range":         {"url": "/options/implied-volatility-range",   "tab": "options", "prio": 1, "desc": "★IV vs 已实现波动价差榜（贵/便宜择时, 含 IV/HV 比+IV rank）"},
    "opt_upcoming_earnings":{"url": "/options/upcoming-earnings",          "tab": "options", "prio": 1, "desc": "★临财报+预期波动+IV rank（催化剂择时）"},
    "opt_most_active":      {"url": "/options/most-active",                "tab": "options", "prio": 2, "desc": "期权成交量榜"},
    "opt_volume_change":    {"url": "/options/volume-change",              "tab": "options", "prio": 2, "desc": "成交量异动"},
    "opt_volume_leaders":   {"url": "/options/volume-leaders",             "tab": "options", "prio": 3, "desc": "量能领头"},
    "opt_highest_oi":       {"url": "/options/highest-open-positions",     "tab": "options", "prio": 3, "desc": "最高未平仓 OI"},
    "opt_oi_change":        {"url": "/options/open-interest-change",       "tab": "options", "prio": 3, "desc": "OI 变化"},
    "opt_oi_pct_change":    {"url": "/options/options-percent-change",     "tab": "options", "prio": 3, "desc": "OI %变化"},
    "opt_screener":         {"url": "/options/options-screener",           "tab": "options", "prio": 2, "desc": "期权通用筛选"},
    "opt_long_call":        {"url": "/options/long-call-options-screener", "tab": "options", "prio": 3, "desc": "Long Call 候选"},
    "opt_long_put":         {"url": "/options/long-put-options-screener",  "tab": "options", "prio": 3, "desc": "Long Put 候选"},
    "opt_covered_calls":    {"url": "/options/income-strategies/covered-calls", "tab": "options", "prio": 3, "desc": "Covered Call 收入策略"},
    "opt_naked_puts":       {"url": "/options/income-strategies/naked-puts",    "tab": "options", "prio": 3, "desc": "Naked Put 收入策略"},

    # ============================ Stocks ============================
    "stk_top100":           {"url": "/stocks/top-100-stocks",                        "tab": "stocks", "prio": 2, "desc": "综合最强 Top100"},
    "stk_new_recos":        {"url": "/stocks/signals",                               "tab": "stocks", "prio": 1, "desc": "信号升级列表（今日 Buy/Sell 升级）"},
    "stk_top_bottom":       {"url": "/stocks/signals/top-bottom",                    "tab": "stocks", "prio": 2, "desc": "信号强度榜（最该持有）"},
    "stk_signal_strength":  {"url": "/stocks/signals/direction-strength",            "tab": "stocks", "prio": 2, "desc": "Opinion 强度"},
    "stk_signal_direction": {"url": "/stocks/signals/direction",                     "tab": "stocks", "prio": 2, "desc": "Opinion 方向"},
    "stk_vol_leaders":      {"url": "/stocks/most-active/daily-volume-leaders",      "tab": "stocks", "prio": 2, "desc": "成交量榜"},
    "stk_price_vol_leaders":{"url": "/stocks/most-active/price-volume-leaders",      "tab": "stocks", "prio": 3, "desc": "价量榜"},
    "stk_pct_change":       {"url": "/stocks/performance/percent-change/advances",   "tab": "stocks", "prio": 3, "desc": "涨幅榜"},
    "stk_gap_up":           {"url": "/stocks/performance/gap/gap-up",                "tab": "stocks", "prio": 2, "desc": "跳空高开（事件驱动）"},
    "stk_gap_down":         {"url": "/stocks/performance/gap/gap-down",              "tab": "stocks", "prio": 2, "desc": "跳空低开（事件驱动）"},
    "stk_five_day_gainers": {"url": "/stocks/performance/five-day-gainers/advances", "tab": "stocks", "prio": 3, "desc": "连续上涨"},
    "stk_highs_lows":       {"url": "/stocks/highs-lows",                            "tab": "stocks", "prio": 3, "desc": "52周新高/新低"},
    "stk_industry_rankings":{"url": "/stocks/sectors/industry-rankings",             "tab": "stocks", "prio": 2, "desc": "★行业组 Weighted Alpha 排名（细分轮动）"},
    "stk_price_surprises":  {"url": "/stocks/price-surprises",                       "tab": "stocks", "prio": 2, "desc": "★相对自身波动的价格异动（非绝对涨跌）"},

    # ============= Investing（独一份 idea/信号 — 2026-06-14 验证有真实数据）=============
    # politician: 独一份数据(别处没有)但"独一份≠alpha"。文献混合偏否定(Eggers-Hainmueller
    # 2013 国会整体跑输/STOCK Act 后普通议员 alpha 消失/NANC 跑赢实为科技tilt非选股);
    # 45天披露滞后又磨掉残存 edge。→ 当独立"叙事/关注"idea, 非入场 alpha; 只盯领袖/委员会。
    "inv_politician":       {"url": "/investing-ideas/politician-insider-trading",  "tab": "investing", "prio": 2, "desc": "政客/议员交易(独一份数据但非已证alpha, 45天滞后, 仅当idea)"},
    # ⚠️ 下面 3 个是方向性技术扫描：backtest_factory 证「择时/方向 edge≈0、追突破=最差桶」
    # → 仅当 idea 漏斗, 不是入场 alpha; TTM squeeze 尤其是突破工具, 与「低IV回踩」edge 相反, 降级。
    "inv_ttm_squeeze":      {"url": "/investing-ideas/ttm-squeeze",                 "tab": "investing", "prio": 3, "desc": "TTM 挤压(突破=回测最差桶, 仅idea, 列里无方向/IV)"},
    "inv_rsi_bull_div":     {"url": "/investing-ideas/rsi-bullish-divergence",      "tab": "investing", "prio": 3, "desc": "RSI 底背离(方向择时 edge≈0, 仅idea漏斗)"},
    "inv_rsi_bear_div":     {"url": "/investing-ideas/rsi-bearish-divergence",      "tab": "investing", "prio": 3, "desc": "RSI 顶背离(同上, 仅idea)"},
    "inv_short_interest":   {"url": "/investing-ideas/short-interest-stocks",       "tab": "investing", "prio": 2, "desc": "高空头持仓榜（逼空候选）"},
    "inv_standout":         {"url": "/investing-ideas/standout-stocks",             "tab": "investing", "prio": 3, "desc": "持续跑赢 SPX 的成分股"},

    # ============ 我的 Saved Screener（PWA Barchart tab 多选交集用）============
    "my_screener":          {"url": "/stocks/stocks-screener?screener=492801&viewName=190773", "tab": "screener", "prio": 1, "desc": "我的动量筛选器(1年+50%/mcap>2B/optvol>5000)"},

    # ============================= ETFs =============================
    "etf_top100":           {"url": "/etfs-funds/top-100-etfs",                 "tab": "etfs", "prio": 3, "desc": "Top100 ETF"},
    "etf_pct_change":       {"url": "/etfs-funds/performance/percent-change",   "tab": "etfs", "prio": 3, "desc": "ETF 涨幅榜"},
    "etf_vol_leaders":      {"url": "/etfs-funds/volume-leaders/daily-leaders", "tab": "etfs", "prio": 3, "desc": "ETF 量能榜"},
    "etf_new_recos":        {"url": "/etfs-funds/signals",                      "tab": "etfs", "prio": 3, "desc": "ETF 信号升级列表"},
    "etf_top_bottom":       {"url": "/etfs-funds/signals/top-bottom",           "tab": "etfs", "prio": 3, "desc": "ETF 信号强度榜"},
}

TABS = sorted({v["tab"] for v in PAGES.values()})

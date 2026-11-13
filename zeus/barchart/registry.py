"""Barchart saved-screener 语义注册表 —— 本地固化"每个 screener 在筛什么/是什么信号"。

这是整套功能的契约核心: ai_picks(喂 AI)、consolidate(落库)、PWA 端点/UI 都 import 这里。

背景: barchart 的实际 filter 阈值定义抓不到(scrape 时被丢弃), 所以语义由人工按实测数据固化。
每条 = 一个 barchart 已保存 screener 的语义标签, AI 拿它当"这只票出现在什么信号池里"的先验。

分类依据(2026-07-08 实跑 11 个 screener 对照 RSI/均线/opinion 得出):
  · 抄底(dip)       : 跌破均线 / RSI 低 / 近期下跌, 超跌反弹或回调到位的看涨反转候选
  · 趋势追随(trend) : 站上均线 / 高 weighted alpha / opinion 一致买入, 看涨趋势延续
  · 领涨异动(gainer): 近期领涨突破, 含超跌反抽, 中性偏多需甄别
  · 回调卖出(pullback_sell): RSI 超买 / 价远离均线, 冲高回落的看跌信号
  · 观察池(watch)   : 未打标签的新 screener 默认归此(中性, 需自行判断)

⚠️ rsi超卖: 名字叫"超卖"但实测筛的是 RSI 71-77 的**超买**股(价全在均线上方), 是回调卖出信号 —— 别被名字骗。

纯数据 + 纯函数, 无 IO。matching 对 barchart 规范名(空格)和落盘文件名(safe_name)统一归一化, 两者都能查到。
"""
from __future__ import annotations
import re

# ── 类别定义 (颜色对齐 heatmap _HM_AI_META) ──────────────────────────────
CATEGORIES: dict[str, dict] = {
    "dip": {
        "label": "抄底",
        "bias": "bull_reversal",
        "color": "#3fb950",
        "verdict_hint": "看涨·低点抄底",
        "ai_note": "超跌/回调到位的看涨反转候选 —— 倾向在支撑/均线回踩处做 ATM call debit。",
    },
    "trend": {
        "label": "趋势追随",
        "bias": "bull_trend",
        "color": "#2ea043",
        "verdict_hint": "看涨·趋势追涨",
        "ai_note": "站上均线/动能领先/买入共识的看涨趋势票 —— 顺势做 ATM call debit。",
    },
    "gainer": {
        "label": "领涨异动",
        "bias": "bull_momentum",
        "color": "#d29922",
        "verdict_hint": "看涨·趋势追涨(需甄别超跌反抽)",
        "ai_note": "近期领涨/异动突破, 但可能是超跌死猫反抽 —— 需结合趋势与均线甄别是真突破还是反弹。",
    },
    "pullback_sell": {
        "label": "回调卖出",
        "bias": "bear_overbought",
        "color": "#f85149",
        "verdict_hint": "看跌·高点阻力",
        "ai_note": "RSI 超买/价远离均线, 冲高回落的看跌信号 —— 倾向做 ATM put debit 或回避多头。",
    },
    "watch": {
        "label": "观察池",
        "bias": "neutral",
        "color": "#8b949e",
        "verdict_hint": "中性, 需自行判断",
        "ai_note": "未分类信号池, 仅表示在关注范围内, 无明确方向。",
    },
}

DEFAULT_CATEGORY = "watch"

# 不纳入的 screener (base = universe 基础过滤, 另有专用链路)
EXCLUDE: set[str] = {"base"}

# ── 注册表: barchart 规范名(来自 MCP screener list) → 语义 ──────────────────
#   slug 是我方稳定 id(与 barchart 命名解耦), by_ticker 反查用 slug。
REGISTRY: dict[str, dict] = {
    # ── 抄底 ──
    "潜在抄底": {
        "slug": "dip_potential", "category": "dip",
        "desc": "RSI 36–44、跌破 EMA20/50 但中长期强, 回调到位的抄底候选",
    },
    "rsi反弹": {
        "slug": "rsi_bounce", "category": "dip",
        "desc": "RSI≈30 深度超卖、远低于均线, 超跌反弹候选",
    },
    "Loser recently": {
        "slug": "loser_recently", "category": "dip",
        "desc": "近期下跌的回调抄底池",
    },
    # ── 趋势追随 ──
    "趋势股票": {
        "slug": "trend_stocks", "category": "trend",
        "desc": "高 weighted alpha、均线多头排列的趋势股",
    },
    "weighted_alpha_ema": {
        "slug": "weighted_alpha_ema", "category": "trend",
        "desc": "weighted alpha 领先 + 站上均线, 强动能",
    },
    "short_medium_long_buy": {
        "slug": "smlt_buy", "category": "trend",
        "desc": "短/中/长 opinion 一致买入",
    },
    "Long Term 100% Buy Signals": {
        "slug": "lt_100_buy", "category": "trend",
        "desc": "Barchart 全周期 100% 买入信号",
    },
    "Hot Prospects": {
        "slug": "hot_prospects", "category": "trend",
        "desc": "Barchart 热门强势领涨股",
    },
    "Edge": {
        "slug": "edge", "category": "trend",
        "desc": "宽口径 edge 候选池, 趋势看涨候选",
    },
    # ── 领涨异动 ──
    "Gainer recently": {
        "slug": "gainer_recently", "category": "gainer",
        "desc": "近期领涨/异动, 含超跌反抽, 需结合趋势甄别",
    },
    # ── 回调卖出 (⚠️名为超卖实为超买) ──
    "rsi超卖": {
        "slug": "rsi_overbought", "category": "pullback_sell",
        "desc": "⚠️名为超卖实为 RSI 71–77 超买、价远离均线, 冲高回落卖出信号",
    },
}


# ── 归一化匹配 (规范名 与 safe_name 落盘文件名 统一) ─────────────────────────
def norm(name: str) -> str:
    """与 scrape_screener.py 的 safe_name 同规则: 非 [\\w-] → '_', 再 lower。

    'Gainer recently' / 'Gainer_recently' → 'gainer_recently' (两者相等 → 可互查)。
    'Long Term 100% Buy Signals' / 'Long_Term_100_Buy_Signals' → 'long_term_100_buy_signals'。
    中文名(潜在抄底/rsi超卖)在 \\w 下保留。
    """
    return re.sub(r"[^\w\-]", "_", (name or "").strip()).lower()


# norm(display_name) → (display_name, entry)
_INDEX: dict[str, tuple[str, dict]] = {norm(k): (k, v) for k, v in REGISTRY.items()}
_EXCLUDE_NORM: set[str] = {norm(x) for x in EXCLUDE}


def is_excluded(name: str) -> bool:
    """base(及 EXCLUDE 内)返回 True —— consolidate/universe 都跳过。"""
    return norm(name) in _EXCLUDE_NORM


def screener_meta(name: str) -> dict:
    """barchart 规范名 或 落盘文件名(safe_name/含_tickers 后缀) → 完整语义 dict。

    未知 screener → 归 watch(中性观察池), 不抛。返回结构::

        {name, slug, category, category_label, bias, color, desc, verdict_hint, ai_note, known}
    """
    key = norm(name)
    # 容错: 若传入的是 '<safe>_tickers' 文件 stem, 去尾再查
    if key not in _INDEX and key.endswith("_tickers"):
        key = key[: -len("_tickers")]

    if key in _INDEX:
        disp, entry = _INDEX[key]
        cat = entry["category"]
        cmeta = CATEGORIES[cat]
        return {
            "name": disp, "slug": entry["slug"], "category": cat,
            "category_label": cmeta["label"], "bias": cmeta["bias"],
            "color": cmeta["color"], "desc": entry["desc"],
            "verdict_hint": cmeta["verdict_hint"], "ai_note": cmeta["ai_note"],
            "known": True,
        }
    # 未知 → watch
    cmeta = CATEGORIES[DEFAULT_CATEGORY]
    disp = (name or "").strip()
    return {
        "name": disp, "slug": "watch_" + key, "category": DEFAULT_CATEGORY,
        "category_label": cmeta["label"], "bias": cmeta["bias"],
        "color": cmeta["color"], "desc": "未分类信号池", "verdict_hint": cmeta["verdict_hint"],
        "ai_note": cmeta["ai_note"], "known": False,
    }


def all_meta() -> list[dict]:
    """已注册的全部 screener 语义(不含 base), 按类别分组顺序返回。"""
    order = ["dip", "trend", "gainer", "pullback_sell", "watch"]
    metas = [screener_meta(k) for k in REGISTRY]
    return sorted(metas, key=lambda m: (order.index(m["category"]), m["name"]))


def category_order() -> list[str]:
    return ["dip", "trend", "gainer", "pullback_sell", "watch"]

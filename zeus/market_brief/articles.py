"""从当天 benzinga digest 里挑**有解读的**文章 —— 纯函数, 不碰网络。

设计思路: benzinga 很多文章自带解读, 筛出高质量的文章就能直接利用里面的观点。
因为最终要判断的是:

    消息的坏程度  vs  价格已经反映的坏程度

而「坏到什么程度」只能从**有机制分析的文章**里读出来 —— 光看标题和涨跌只能知道
「发生了什么」, 读不出「这事是一次性的还是结构性的、影响多大、多久」。

所以这里按两条打分:
  1. 频道 —— Analyst Color / Analyst Ratings / WIIM(为何异动) / Guidance /
             Macro 这些是**带观点**的; Movers / Mid-Day Update 只是播报涨跌。
  2. 正文长度 —— 一句话快讯读不出机制, 长文才有。
"""
from __future__ import annotations

# 频道权重: 越高 = 越可能含「为什么 + 影响多大」的判断, 而不只是「发生了什么」
CHANNEL_WEIGHT = {
    "Analyst Color": 5,          # 分析师完整评论 —— 信号最强
    "Analyst Ratings": 4,
    "Price Target": 3,
    "WIIM": 4,                   # Why Is It Moving —— 专门解释异动机制
    "Guidance": 4,               # 公司前瞻指引 = 对未来的判断
    "Macro Economic Events": 4,
    "Econ #s": 3,
    "Pre-Market Outlook": 3,
    "Top Stories": 3,            # 编辑精选
    "Trading Ideas": 2,
    "Earnings": 2,
    "Markets": 1,
    "Tech": 1,
}
# 纯播报, 没有解读 —— 直接排除(它们的信息 digest 总结里已经有了)
NOISE_CHANNELS = {"Movers", "Mid-Day Update", "Market Summary", "Penny Stocks"}

MIN_BODY = 600        # 正文短于这个基本读不出机制
LONG_BODY = 1800      # 长文加分


def body_text(item) -> str:
    """把 paras 拼成正文。cn 为空时回落 en(文章未翻译时 cn 为空)。"""
    out = []
    for p in ((item or {}).get("paras") or []):
        if not isinstance(p, dict):
            continue
        t = (p.get("cn") or "").strip() or (p.get("en") or "").strip()
        if t:
            out.append(t)
    return "\n".join(out)


def score(item) -> int:
    """这篇文章有多可能含「解读」。<=0 表示不值得喂给模型。"""
    if not isinstance(item, dict) or not item.get("title"):
        return 0
    chans = [str(c) for c in (item.get("channels") or [])]
    if any(c in NOISE_CHANNELS for c in chans):
        return 0
    ch_score = max((CHANNEL_WEIGHT.get(c, 0) for c in chans), default=0)
    if ch_score <= 0:
        return 0
    n = len(body_text(item))
    if n < MIN_BODY:
        return 0
    return ch_score + (2 if n >= LONG_BODY else 0)


def pick(items, limit: int = 12, max_chars: int = 1400) -> list:
    """挑出最有解读价值的 N 篇 → [{title, channels, tickers, sentiment, body}]。

    limit 不宜大: 喂太多长文会稀释注意力, 而且这些文章是用来**校准坏消息程度**的,
    不是用来穷举新闻(穷举那部分 digest 的 AI 总结已经做了)。
    """
    scored = []
    for it in (items or ()):
        s = score(it)
        if s > 0:
            scored.append((s, it))
    # 分数降序; 同分按正文长度(长的机制更全)
    scored.sort(key=lambda p: (-p[0], -len(body_text(p[1]))))
    out = []
    for s, it in scored[:limit]:
        out.append({
            "title": (it.get("title") or "").strip(),
            "channels": [str(c) for c in (it.get("channels") or [])][:4],
            "tickers": [str(t).upper() for t in (it.get("tickers") or [])][:6],
            "sentiment": it.get("sentiment"),
            "hhmm": it.get("hhmm", ""),
            "score": s,
            "body": body_text(it)[:max_chars],
        })
    return out


def render(picked) -> str:
    """拼成喂给模型的文本块。"""
    if not picked:
        return ""
    blocks = []
    for i, a in enumerate(picked, 1):
        head = f"[{i}] {a['title']}"
        meta = []
        if a["channels"]:
            meta.append("/".join(a["channels"]))
        if a["tickers"]:
            meta.append(" ".join(a["tickers"]))
        if meta:
            head += f"  ({' · '.join(meta)})"
        blocks.append(head + "\n" + a["body"])
    return "\n\n".join(blocks)

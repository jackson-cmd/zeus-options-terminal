"""AI 新闻总结的「每日生成 + 历史补齐」调度决策(纯函数, 不碰网络/LLM/磁盘)。

背景(2026-07-23 排查): 作者反映「每次都要重新总结, 历史的看不了」。
根因在 digest_ai_morning_loop:

    if _aisum_morning_last != today and past:
        await _run_morning_summary(today)
        _aisum_morning_last = today          # ← 无条件标记「今天已跑」
        _aisum_save_morning_state(today)

而 _run_morning_summary 有两条**不产出任何缓存**的早返回(当天 digest 还没数据 /
Haiku 调用出错), 两种情况都不写缓存文件, 但调用方照样把这天标记成已跑并落盘 →
这天从此永不重试, 永远没有总结。7 月 23 天里 12 个交易日就是这么丢的, 于是作者
每次打开历史某天都得现场重新生成。

修法: 只有**真的存下了总结**才记为完成; 失败则今天内有限次重试(避免死循环烧钱)。
外加历史补齐: 有 digest 数据却没有总结的最近若干天, 每轮补一点。
"""
from __future__ import annotations

MAX_ATTEMPTS_PER_DAY = 6      # 当天失败重试上限(每轮 loop 间隔 120s → 最多约 12 分钟)
BACKFILL_LOOKBACK_DAYS = 21   # 往回补多少天
BACKFILL_PER_ROUND = 1        # 每轮最多补几天(别一次性打爆 Haiku)


def should_attempt_today(now, last_done: str, attempts: int,
                         hhmm=(8, 30), max_attempts: int = MAX_ATTEMPTS_PER_DAY) -> bool:
    """今天这一轮要不要尝试生成当天总结。

    now         datetime(本地)
    last_done   最近一次**成功**生成的日期(ISO), 没有则空串
    attempts    今天已经失败过几次
    """
    today = now.date().isoformat()
    if last_done == today:
        return False                     # 今天已经成功过了
    if (now.hour, now.minute) < tuple(hhmm):
        return False                     # 还没到点
    return attempts < max_attempts       # 失败太多次就今天算了, 明天再来


def days_needing_backfill(available_days, cached, today: str,
                          lookback: int = BACKFILL_LOOKBACK_DAYS,
                          limit: int = BACKFILL_PER_ROUND) -> list:
    """哪些历史日该补总结 —— 有 digest 数据、却没有总结缓存的日子。

    available_days  有 digest 数据的日期(可迭代, ISO 串)
    cached          已有总结的日期(可迭代, ISO 串)
    today           今天(ISO); 今天由 should_attempt_today 那条路负责, 这里排除
    返回: 最近的 <=limit 天, 由新到旧(先补作者最可能翻的近几天)
    """
    cached = set(cached or ())
    days = sorted({d for d in (available_days or ()) if d and d < today}, reverse=True)
    missing = [d for d in days[:lookback] if d not in cached]
    return missing[:max(0, limit)]

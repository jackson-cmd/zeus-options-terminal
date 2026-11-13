"""Sat/Sun 20:00 macOS push reminder for the Weekly Review feature.

Decoupled from optionstrat_server.py so it can be unit-tested without
loading the FastAPI app. The server's startup just imports and registers
the coroutine.

Trigger logic (all must hold):
  * Local weekday is Sat (5) or Sun (6)
  * 20:00 <= now < 20:10 (the 5-min loop guarantees the window is sampled)
  * Current ISO week's review is not yet completed
  * In-memory dedupe: no push fired for this `week` in the last 6 hours

The 6h dedupe key is the ISO week itself, so Sat and Sun share the same
slot - at most one push per weekend. If Saturday's push fires, Sunday
stays silent. If the server is down Saturday, Sunday's push still fires
because the dedupe map is empty for that week.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
from pathlib import Path as _Path

# 与 PWA 头部 🔔 静音按钮同一个开关(options_analyzer/data/alert_config.json)。
_ALERT_CFG = _Path(__file__).resolve().parent.parent / "options_analyzer" / "data" / "alert_config.json"


def _muted() -> bool:
    try:
        return bool(_json.loads(_ALERT_CFG.read_text()).get("muted", False))
    except Exception:
        return False


def _should_fire_push(now, last_push_at, wr_module) -> bool:
    """Pure predicate - does the current moment warrant a push?

    Args:
      now: datetime in local time
      last_push_at: dict[str, datetime] keyed by ISO week
      wr_module: a module providing current_week_iso(now) and is_completed(week)
    """
    if now.weekday() not in (5, 6):
        return False
    if not (now.hour == 20 and now.minute < 10):
        return False
    week = wr_module.current_week_iso(now)
    if wr_module.is_completed(week):
        return False
    last = last_push_at.get(week)
    if last is not None and (now - last).total_seconds() <= 6 * 3600:
        return False
    return True


async def _send_macos_notification(title: str, message: str) -> None:
    """Fire a native macOS notification via osascript.

    Inherits the Terminal.app TCC of the optionstrat server process, so it
    works under the existing launchd setup without requiring extra grants.
    """
    t = title.replace('"', '\\"')
    m = message.replace('"', '\\"')
    cmd = [
        "osascript", "-e",
        f'display notification "{m}" with title "{t}"',
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _run_one_tick(now, last_push_at: dict, wr_module) -> None:
    """One iteration of the loop body. Extracted for unit testing."""
    if _muted():
        return
    if not _should_fire_push(now, last_push_at, wr_module):
        return
    week = wr_module.current_week_iso(now)
    await _send_macos_notification(
        title="ZEUS",
        message="该写本周 Weekly Review 了 — 过一下这周的交易。",
    )
    last_push_at[week] = now
    print(f"[WeeklyReview] pushed for {week}")


async def weekly_review_push_loop():
    """Main loop. Runs forever; sleeps 5 min between ticks. The server's
    startup registers this with `asyncio.create_task(weekly_review_push_loop())`."""
    import sys
    from pathlib import Path
    # Make sure `weekly_reviews` is importable. trade_journal/ is added to
    # sys.path elsewhere via _weekly_reviews_module() in the server, but
    # this coroutine can run standalone (e.g. in tests using the loop), so
    # we re-add defensively.
    tj_dir = str(Path(__file__).resolve().parent)
    if tj_dir not in sys.path:
        sys.path.insert(0, tj_dir)
    import weekly_reviews as wr_module  # noqa: E402

    await asyncio.sleep(60)  # let server settle
    last_push_at: dict[str, _dt.datetime] = {}
    while True:
        try:
            await _run_one_tick(_dt.datetime.now(), last_push_at, wr_module)
        except Exception as e:
            print(f"[WeeklyReview] push loop error: {e}")
        await asyncio.sleep(300)

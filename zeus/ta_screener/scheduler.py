"""TA Screener 交易日定时自动扫描(server 内 asyncio 循环,benzinga autofetch 同模式)。

槽位(ET,DST-aware):
  09:40  开盘后 10 分钟
  10:30  开盘后 1 小时
  12:00  开盘后 2.5 小时
  15:00  收盘前 1 小时

规则:
- 仅周一~周五(节假日照跑一次,代价只是 ~1 分钟空扫,不做交易日历依赖)。
- 每个槽位每天只触发一次;触发即调 scanner.start_scan()(自带"已在跑"守护)。
- 服务器重启:错过的槽位仅在 GRACE_MIN(20 分钟)宽限内补跑,超过即放弃,
  避免下午重启把上午 3 个槽位连环触发。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SLOTS = ((9, 40), (10, 30), (12, 0), (15, 0))
SLOTS_LABEL = "ET " + "/".join(f"{h:02d}:{m:02d}" for h, m in SLOTS)
GRACE_MIN = 20


def due_slots(now_et: _dt.datetime, done: set) -> tuple[list, list]:
    """纯函数(可测):返回 (该触发的槽位 idx 列表, 该标记为错过的 idx 列表)。
    周末一律空。宽限内 → 触发;超宽限 → 错过;未到 → 不动。"""
    if now_et.weekday() >= 5:
        return [], []
    fire, missed = [], []
    for idx, (h, m) in enumerate(SLOTS):
        if idx in done:
            continue
        slot = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_min = (now_et - slot).total_seconds() / 60.0
        if 0 <= delta_min <= GRACE_MIN:
            fire.append(idx)
        elif delta_min > GRACE_MIN:
            missed.append(idx)
    return fire, missed


async def run_scheduler(log=print):
    """常驻循环,每 60s 检查一次。挂在 optionstrat_server startup。"""
    try:  # 包内相对导入;直跑退回同目录
        from . import scanner
    except ImportError:
        import scanner  # type: ignore

    scanner.STATE["auto"] = SLOTS_LABEL          # /api/ta/status 可见"已武装"
    log(f"[ta_screener] auto-scan armed: {SLOTS_LABEL} (交易日,宽限 {GRACE_MIN}min)")
    fired: dict[str, set] = {}
    await asyncio.sleep(30)                       # 让 server 先稳定
    while True:
        try:
            now = _dt.datetime.now(ET)
            today = now.date().isoformat()
            for k in list(fired.keys()):          # 只保留今天的记录
                if k != today:
                    del fired[k]
            done = fired.setdefault(today, set())
            fire, missed = due_slots(now, done)
            for idx in missed:
                done.add(idx)
            for idx in fire:
                h, m = SLOTS[idx]
                r = scanner.start_scan()
                log(f"[ta_screener] auto-scan {h:02d}:{m:02d} ET → {r}")
                done.add(idx)
                scanner.STATE["auto_last"] = f"{today} {h:02d}:{m:02d} ET"
        except Exception as e:
            log(f"[ta_screener] scheduler error: {e}")
        await asyncio.sleep(60)

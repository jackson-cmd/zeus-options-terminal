"""DXLink 实时流的电源门控 — 纯决策,不碰 FastAPI/state。

power guard(scripts/pwa_power_guard.sh)每 30s 把目标档位写进 /tmp/pwa_power_mode
(full|lite|off)。server 读它 + 手动 override + 最近请求时间,决定 DXLink 实时流开关:

  full  → 恒开(插电,不省电)
  lite  → 默认关;作者在 PWA 点「开实时」后开,但 idle_secs 内无请求自动关回(懒复位)
  off   → 恒关(理论上进程已被 guard 杀)

"""
from __future__ import annotations
from pathlib import Path

MODE_FILE = "/tmp/pwa_power_mode"
IDLE_SECS = 300.0  # lite 下手动开实时后,无请求多久自动关回


def read_mode(mode_file: str = MODE_FILE) -> str:
    """读档位;文件缺失/空/读失败一律当 'full'(不误杀实时流)。"""
    try:
        v = Path(mode_file).read_text().strip()
        return v if v else "full"
    except Exception:
        return "full"


def live_on(mode: str, override_on: bool, last_request_ts: float,
            now: float, idle_secs: float = IDLE_SECS) -> tuple[bool, bool]:
    """实时流是否该开 + override 复位后的值。

    返回 (live, override_after):
      - live:            DXLink 实时流本拍是否该连
      - override_after:  归一化后的 override 标志(idle 超时会被懒复位成 False)

    lite 档:仅当 override_on 且 (now - last_request_ts) <= idle_secs 才开;
    超时则 (False, False) —— 顺手把 override 复位,UI 随之反映精简态。
    off 档:恒关。其余(full/未知)恒开,不改 override。
    """
    if mode == "lite":
        if override_on and (now - last_request_ts) <= idle_secs:
            return True, True
        return False, False
    if mode == "off":
        return False, False
    # full 或未知档位:不误杀,恒开
    return True, override_on

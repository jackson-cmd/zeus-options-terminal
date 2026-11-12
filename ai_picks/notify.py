"""跑完发 macOS 原生通知(osascript)。参照 project_pwa_push_is_macos_native。"""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

# 与 PWA 头部 🔔 静音按钮同一个开关(options_analyzer/data/alert_config.json)。
# ai_picks 由 launchd 直接起(绕过 server),故在此显式复查 muted,让静音全局生效。
_ALERT_CFG = Path(__file__).resolve().parent.parent / "options_analyzer" / "data" / "alert_config.json"


def _muted() -> bool:
    try:
        return bool(json.loads(_ALERT_CFG.read_text()).get("muted", False))
    except Exception:
        return False


def notify(title: str, message: str, sound: str = "Glass") -> None:
    if _muted():
        return
    try:
        safe_t = title.replace('"', "'")
        safe_m = message.replace('"', "'")
        script = (f'display notification "{safe_m}" with title "{safe_t}"'
                  f' sound name "{sound}"')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass

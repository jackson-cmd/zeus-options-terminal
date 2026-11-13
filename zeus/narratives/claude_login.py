"""检测 `claude -p` 订阅登录失效 + 弹一条可点的 macOS 通知提醒重登。

所有走订阅 CLI 的后台任务(digest🤖AI总结 / 多空叙事 / 🎯选股 / journal✦复盘)共用同一份
订阅 OAuth。一旦 Keychain 里的令牌被并发刷新踩踏冲空, 新起的 `claude -p` 就一律报
"Not logged in · Please run /login" 或 "Failed to authenticate: OAuth session expired
and could not be refreshed"。本模块把这类错误统一识别出来, 并(节流地)弹一条通知——
点一下自动开 Terminal 起 `claude`, 作者只需在里面 `/login` 重新授权。

⚠️ 登录失效是全局 OAuth 状态, 与 PWA「🔔 交易告警静音」无关 —— 这个提醒**不受 muted 影响**
(作者明确要求出现即提醒), 但自带 30 分钟节流防后台多任务刷屏。

⚠️ claude --output-format json 把认证错误放在 **stdout 的 JSON result 字段**(is_error:true),
stderr 是空的。所以只读 p.stderr 的调用者会把登录错误显示成空白 —— 用 cli_error() 归一化。
"""
from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# 命中任一即判定为「需要作者重新 /login」的订阅登录失效(全小写匹配)。
# 覆盖两种实测措辞 + 常见变体; 刻意不含 "rate limit"/"usage limit"(那是限流/额度, 非登录)。
_LOGIN_ERR_MARKERS = (
    "not logged in",
    "please run /login",
    "/login",
    "failed to authenticate",
    "could not be refreshed",
    "invalid_grant",
    "not authenticated",
    "oauth",
    "expired",
    "401",
)

# 30 分钟内最多弹一次(跨所有调用者共享同一张 stamp), 防 4 个后台任务同时炸出 4 条通知。
_THROTTLE_SEC = 1800
_STAMP = Path(tempfile.gettempdir()) / "claude_relogin_notify.stamp"

# 点通知 → 激活 Terminal 并新开一个跑 `claude` 的窗口(作者在里面 /login)。
_CLICK_CMD = (
    "osascript "
    "-e 'tell application \"Terminal\" to activate' "
    "-e 'tell application \"Terminal\" to do script \"claude\"'"
)


def is_login_error(text: str | None) -> bool:
    """claude -p 的错误文本是否属于「订阅登录失效, 需重新 /login」。"""
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in _LOGIN_ERR_MARKERS)


def _throttled() -> bool:
    """True = 距上次通知不足 _THROTTLE_SEC, 本次跳过。顺便打点(首次放行并记时)。"""
    try:
        if _STAMP.exists() and (time.time() - _STAMP.stat().st_mtime) < _THROTTLE_SEC:
            return True
    except Exception:
        pass
    try:
        _STAMP.touch()
    except Exception:
        pass
    return False


def notify_relogin(source: str = "") -> None:
    """弹一条可点通知提醒重登; 点击 → 开 Terminal 起 `claude`。节流 30 分钟, 永不抛异常。"""
    if _throttled():
        return
    title = "⚠️ Claude 订阅登录失效"
    subtitle = (f"{source} 已停摆".strip()) if source else "AI 总结 / 叙事已停摆"
    message = "点此开 Terminal 跑 claude → 输入 /login 重新授权"
    try:
        tn = shutil.which("terminal-notifier")
        if tn:
            # terminal-notifier 支持 -execute: 点通知才跑, 不点不跑
            subprocess.run(
                [tn, "-title", title, "-subtitle", subtitle, "-message", message,
                 "-sound", "Basso", "-ignoreDnD", "-execute", _CLICK_CMD],
                capture_output=True, timeout=10,
            )
        else:
            # 兜底: 纯 osascript 通知(不可点, 但至少提醒到)
            safe = lambda s: s.replace('"', "'")
            script = (f'display notification "{safe(message)}" with title "{safe(title)}"'
                      f' subtitle "{safe(subtitle)}" sound name "Basso"')
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def cli_error(p: subprocess.CompletedProcess, source: str = "") -> str:
    """把一个 returncode!=0 的 claude -p 结果归一化成错误文本。

    - 优先 stderr; 空则取 stdout 里 JSON 的 result 字段(认证错误走这条); 再空给通用兜底。
    - 若判定是订阅登录失效: 顺手弹重登通知(节流), 并返回可操作的中文提示。
    - 否则原样返回(截 400 字)。
    """
    raw = (getattr(p, "stderr", "") or "").strip()
    if not raw:
        try:
            raw = (json.loads(getattr(p, "stdout", "") or "{}").get("result") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raw = "claude 返回非零(无错误文本)"
    try:
        import zeus_ai as _hai
        _cli = _hai.provider() == "claude_cli"
    except Exception:
        _cli = True
    if _cli and is_login_error(raw):
        notify_relogin(source)
        return "Claude 订阅登录已失效 — 请在终端运行 `claude` 重新 /login(已弹通知提醒)"
    return raw[:400]

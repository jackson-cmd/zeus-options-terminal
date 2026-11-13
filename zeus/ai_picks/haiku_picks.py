"""调 Haiku(经 claude CLI 复用订阅) 把一批票分成 5 类。复用 narratives/haiku.py 的子进程约定。

⚠️ MAX_THINKING_TOKENS=0 关 thinking; 删 ANTHROPIC_API_KEY 走订阅; 空 mcp + 临时 cwd 避污染。
"""
from __future__ import annotations
import json, re, os, shutil, subprocess, tempfile
from pathlib import Path

from . import prompt as P


def _find_claude():
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    found = shutil.which("claude")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "claude"
    return str(home_bin) if home_bin.exists() else "/usr/local/bin/claude"


CLAUDE = _find_claude()
MODEL = "claude-haiku-4-5"
TIMEOUT = 300


def _parse_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _invoke(user_text):
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)
    for _k in list(env):
        if (_k.startswith("CLAUDE_CODE") or _k.startswith("CLAUDE_AGENT")
                or _k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(_k, None)
    cmd = [CLAUDE, "-p", "--model", MODEL, "--output-format", "json",
           "--system-prompt", P.SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, None, f"timeout after {TIMEOUT}s"
    except Exception as e:
        return None, None, f"subprocess fail: {e}"
    if p.returncode != 0:
        # 登录错误在 stdout JSON 里(stderr 为空)→ claude_login 统一识别 + 弹重登通知
        try:
            from narratives import claude_login as _cl
        except Exception:
            import claude_login as _cl
        return None, None, _cl.cli_error(p, "🎯选股")
    try:
        env_json = json.loads(p.stdout)
    except Exception as e:
        return None, None, f"envelope parse fail: {e}"
    return env_json.get("result"), env_json.get("usage"), None


def _normalize_verdict(v: str) -> str | None:
    v = (v or "").strip()
    if v in P.VERDICTS:
        return v
    # 容错: 去空格/标点后模糊匹配
    key = re.sub(r"[\s·:：|]", "", v)
    for cand in P.VERDICTS:
        if re.sub(r"[\s·:：|]", "", cand) == key:
            return cand
    return None


def run_batch(feats: dict, tickers: list[str], retries: int = 2):
    """→ (results_by_ticker {t:{verdict,confidence,tech,fundamental,news,summary}}, usage, err_or_None)"""
    have = [t for t in tickers if t in feats]
    if not have:
        return {}, None, "no features"
    user_text = P.build_user(feats, have)
    err = None
    for _ in range(retries + 1):
        result, usage, e = _invoke(user_text)
        if e:
            err = e
            continue
        arr = _parse_array(result)
        if not isinstance(arr, list):
            err = "JSON array parse fail"
            continue
        out = {}
        for x in arr:
            if not isinstance(x, dict):
                continue
            t = (x.get("ticker") or "").upper()
            verdict = _normalize_verdict(x.get("verdict"))
            if not t or not verdict:
                continue
            out[t] = {
                "verdict": verdict,
                "confidence": x.get("confidence"),
                "tech": x.get("tech") or "",
                "fundamental": x.get("fundamental") or "",
                "news": x.get("news") or "",
                "summary": x.get("summary") or "",
            }
        return out, usage, None
    return {}, None, err

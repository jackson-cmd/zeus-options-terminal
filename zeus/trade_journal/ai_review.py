"""✦ AI 交易复盘: 用 claude -p(Haiku, 复用订阅) 分析开/平单原因 + 常见错误 + 改进建议。

⚠️ MAX_THINKING_TOKENS=0 关 thinking; 删 ANTHROPIC_API_KEY 走订阅; 剥离继承的会话环境
"""
from __future__ import annotations
import json, os, shutil, subprocess, tempfile, datetime, hashlib
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CACHE_F = DATA / "ai_review_cache.json"
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/local/bin/claude"
MODEL = "claude-haiku-4-5"
CAP = 180   # 最近 N 笔已平仓(控 prompt 体积)

_SYSTEM = """你是资深期权交易复盘教练。下面是用户的真实交易记录(垂直价差为主), 每笔含开单原因(thesis/逻辑类型/信心/情绪/信号来源)与平仓结果(净盈亏/平仓逻辑/是否守计划/流程分/象限/自评错误/教训)。
请用简洁中文复盘, 输出 markdown, 严格按这四节:
## 开单习惯
他开单理由的模式: 信号来源/逻辑类型/情绪/信心 与盈亏的关系(1-3 条, 点出证据)。
## 平仓习惯
平仓逻辑/是否守纪律/过早或过晚 与盈亏的关系(1-3 条)。
## 最常见的错误
从 mistakes_made / lesson_learned + 数据归纳 top 3-5, 每条点出具体证据(票/笔数/盈亏)。
## 给你的改进建议
可执行、具体、针对上面问题, 3-6 条 bullet(用 - 开头)。
要求: 只基于所给数据不编造; 直接犀利、对事不对人; 别泛泛而谈, 多用具体数字。"""


def _load(name):
    p = DATA / name
    out = []
    if p.exists():
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _build_user(opens, closes):
    o_by_id = {o.get("id"): o for o in opens}
    rows = []
    for c in closes:
        o = o_by_id.get(c.get("open_entry_id"), {})
        rows.append({
            "tk": c.get("ticker"), "strat": c.get("strategy"), "dir": c.get("direction"),
            "thesis": (o.get("one_line_thesis") or "")[:120], "logic": o.get("logic_type"),
            "conf": o.get("confidence"), "emo": o.get("emotion"),
            "earn": o.get("is_earnings_bet"), "sig": o.get("signal_sources"),
            "pnl": c.get("net_pnl"), "closelogic": c.get("close_logic"),
            "plan": c.get("followed_plan"), "chk": c.get("followed_checklist"),
            "pscore": c.get("process_score"), "quad": c.get("quadrant"),
            "mistakes": c.get("mistakes_made"), "lesson": (c.get("lesson_learned") or "")[:120],
        })
    rows = rows[-CAP:]
    n = len(rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    tot = sum(r["pnl"] or 0 for r in rows)
    head = (f"共 {n} 笔已平仓, 盈利 {wins} 笔(胜率 {round(100*wins/n) if n else 0}%), "
            f"合计净盈亏 ${round(tot)}.\n\n逐笔(紧凑 JSON, 每行一笔):\n")
    return head + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), n


def _invoke(user_text):
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)
    for k in list(env):
        if (k.startswith("CLAUDE_CODE") or k.startswith("CLAUDE_AGENT")
                or k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(k, None)
    cmd = [CLAUDE, "-p", "--model", MODEL, "--output-format", "json",
           "--system-prompt", _SYSTEM, "--strict-mcp-config", "--mcp-config",
           '{"mcpServers":{}}',
           # 不加载 user 设置(~/.claude/settings.json) → 跳过 Stop/Notification 钩子,
           # 后台复盘不弹「Claude Cowork」通知; 订阅鉴权照常(非 --bare)。
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=300)
    except Exception as e:
        return None, f"调用失败: {e}"
    if p.returncode != 0:
        # 登录错误在 stdout JSON 里(stderr 为空)→ claude_login 统一识别 + 弹重登通知
        try:
            from narratives import claude_login as _cl
        except Exception:
            import claude_login as _cl
        return None, _cl.cli_error(p, "journal 复盘")
    try:
        return json.loads(p.stdout).get("result"), None
    except Exception as e:
        return None, f"解析失败: {e}"


def _closes():
    return [c for c in _load("entries_close.jsonl") if c.get("net_pnl") is not None]


def _fingerprint(closes):
    """指纹: 平仓笔数 + 关键字段哈希。新平仓 / 改了盈亏或复盘字段都会变 → 据此判 stale。"""
    h = hashlib.md5()
    for c in closes:
        h.update(("%s|%s|%s|%s|%s" % (
            c.get("id"), c.get("net_pnl"), c.get("close_logic"),
            c.get("mistakes_made"), c.get("lesson_learned"))).encode("utf-8"))
    return "%d:%s" % (len(closes), h.hexdigest()[:12])


def load_cached():
    """读本地缓存(不调模型)。返回 {text,n_closed,asof,fp,stale,n_now} 或 None。"""
    try:
        d = json.loads(CACHE_F.read_text())
    except Exception:
        return None
    closes = _closes()
    d["stale"] = (d.get("fp") != _fingerprint(closes))
    d["n_now"] = len(closes)
    return d


def build_review():
    """重新生成复盘(调 Haiku) 并写本地缓存。"""
    opens = _load("entries_open.jsonl")
    closes = _closes()
    if not closes:
        return {"error": "暂无已平仓记录"}
    user_text, n = _build_user(opens, closes)
    text, err = _invoke(user_text)
    if err:
        return {"error": err}
    obj = {"text": text or "", "n_closed": n,
           "asof": datetime.datetime.now().isoformat(timespec="seconds"),
           "fp": _fingerprint(closes)}
    try:
        CACHE_F.write_text(json.dumps(obj, ensure_ascii=False))
    except Exception as e:
        print("[ai_review] cache save err:", e)
    return obj

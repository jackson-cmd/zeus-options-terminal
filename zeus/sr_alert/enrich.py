"""SR 告警的新闻富化 —— digest 提及数 + 多空 + 异动 → Haiku 一两句中文原因。

纯函数(digest_ticker_count / has_news / build_prompt)可单测;gather_context / enrich_reason
读现有库(digest_store / narratives.store / finviz_ai_store,零网络)+ 调 Haiku(复用
narratives 的 claude -p 订阅姿势)。best-effort:任何一步失败都不抛,reason 留空不挡展示。
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_BZ_SRC = _REPO / "benzinga_digest" / "src"

MODEL = "claude-haiku-4-5"
TIMEOUT = 60
_SYSTEM = "你是简洁的金融新闻分析助手。只用简体中文,只陈述事实原因,不给投资建议。"


# ───────────────────────── 纯函数(单测) ─────────────────────────

def digest_ticker_count(articles: list, ticker: str) -> dict:
    """今天 digest 里提到 ticker 的文章数 + 至多 5 条标题(大小写不敏感)。"""
    tk = (ticker or "").upper()
    count = 0
    titles = []
    for a in articles or []:
        tks = [str(t).upper() for t in (a.get("tickers") or [])]
        if tk and tk in tks:
            count += 1
            t = a.get("title") or a.get("title_en")
            if t and len(titles) < 5:
                titles.append(t)
    return {"count": count, "titles": titles}


def _clean_reason(text: str) -> str:
    """去 Haiku 爱加的 markdown / 「根据…分析：」开场白 / 元标签,压成一行。"""
    if not text:
        return ""
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)      # 去粗体保内容
    t = re.sub(r"[#*`>]+", "", t)                     # 去零散 md 符号
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    while lines and re.match(r"^(根据|基于|以下是|我[看来]|好的|让我)", lines[0]):
        lines.pop(0)                                  # 丢开场白行
    t = " ".join(lines)
    m = re.match(r"^.{0,30}?(原因|分析|如下|结论)[^。]{0,10}[:：]\s*", t)
    if m:
        t = t[m.end():]                               # 丢「XX核心原因：」前缀
    return t.strip()


def has_news(ctx: dict) -> bool:
    """有任何一路新闻(digest/多空/异动)才值得烧 Haiku。"""
    return bool(ctx.get("digest_count") or ctx.get("narrative") or ctx.get("mover"))


def build_prompt(ticker: str, kind_cn: str, ctx: dict) -> str:
    lines = [
        f"股票 {ticker} 盘中触及技术位:{kind_cn}(日+周+月共振黄金 S/R zone)。",
        "下面是今天与该股相关的新闻/多空信息,请据此判断它今天为什么受关注。",
    ]
    if ctx.get("digest_titles"):
        lines.append(f"\n[新闻标题 ×{ctx.get('digest_count', 0)}]")
        for t in ctx["digest_titles"]:
            lines.append(f"- {t}")
    nar = ctx.get("narrative")
    if nar:
        if nar.get("bull"):
            lines.append("\n[多方] " + "；".join(nar["bull"][:4]))
        if nar.get("bear"):
            lines.append("[空方] " + "；".join(nar["bear"][:4]))
        if nar.get("net"):
            lines.append(f"[综合] {nar['net']}")
    if ctx.get("mover"):
        lines.append(f"\n[异动原因] {ctx['mover']}")
    lines.append("\n要求:用简体中文直接写 1–2 句话说该股今天受关注/波动的核心原因"
                 "(基本面或催化)。**只输出这 1–2 句本身**,不超过 60 字,"
                 "不要任何 markdown 符号(* # 等)、不要开场白(如「根据…分析」)、"
                 "不要小标题、不要投资建议、不要重复股价数字。")
    return "\n".join(lines)


# ───────────────────────── IO(读三个库) ─────────────────────────

def _bz_path():
    if str(_BZ_SRC) not in sys.path:
        sys.path.insert(0, str(_BZ_SRC))
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))


def gather_context(ticker: str, day: str) -> dict:
    """读 digest 提及 + 多空叙事 + 异动 finviz AI(全 best-effort,零网络)。"""
    ctx = {"digest_count": 0, "digest_titles": [], "narrative": None, "mover": None}
    _bz_path()
    tk = (ticker or "").upper()

    try:
        from benzinga_mcp import digest_store as _dgs
        dc = digest_ticker_count(_dgs.load_day(day) or [], tk)
        ctx["digest_count"] = dc["count"]
        ctx["digest_titles"] = dc["titles"]
    except Exception:
        pass

    try:
        from narratives import store as _narr
        for d in _narr.list_days()[:3]:
            hit = next((it for it in (_narr.load_day(d) or [])
                        if str(it.get("ticker", "")).upper() == tk), None)
            if hit:
                ctx["narrative"] = {"bull": hit.get("bull") or [],
                                    "bear": hit.get("bear") or [],
                                    "net": hit.get("net")}
                break
    except Exception:
        pass

    try:
        from benzinga_mcp import finviz_ai_store as _fvai
        hit = next((it for it in (_fvai.load_day(day) or [])
                    if str(it.get("ticker", "")).upper() == tk), None)
        if hit and isinstance(hit.get("ai"), dict):
            ai = hit["ai"]
            bits = [ai.get("title_cn") or ai.get("title_en"), ai.get("catalyst")]
            mv = " · ".join(b for b in bits if b)
            ctx["mover"] = mv or None
    except Exception:
        pass

    return ctx


# ───────────────────────── Haiku ─────────────────────────

def _find_claude():
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    import shutil
    return (shutil.which("claude")
            or str(Path.home() / ".local" / "bin" / "claude")
            or "/usr/local/bin/claude")


def _haiku(prompt: str):
    """→ (text|None, err|None)。复用 narratives 的干净订阅调用姿势。"""
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)
    for k in list(env):
        if (k.startswith("CLAUDE_CODE") or k.startswith("CLAUDE_AGENT")
                or k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(k, None)
    cmd = [_find_claude(), "-p", "--model", MODEL, "--output-format", "json",
           "--system-prompt", _SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local", "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
        if p.returncode != 0:
            return None, (p.stderr or "nonzero")[:200]
        result = json.loads(p.stdout).get("result")
        return (result or "").strip() or None, None
    except Exception as e:
        return None, str(e)[:200]


def enrich_reason(ticker: str, kind_cn: str, day: str) -> dict:
    """整合:gather → 无新闻则跳 Haiku → 有则生成中文原因。返回 set_enrichment 用的 dict。"""
    ctx = gather_context(ticker, day)
    out = {
        "digest_count": ctx["digest_count"],
        "digest_titles": ctx["digest_titles"],
        "has_narrative": ctx["narrative"] is not None,
        "has_mover": ctx["mover"] is not None,
        "reason_cn": None,
    }
    if not has_news(ctx):
        out["reason_cn"] = "无相关新闻"
        return out
    text, err = _haiku(build_prompt(ticker, kind_cn, ctx))
    cleaned = _clean_reason(text) if text else ""
    out["reason_cn"] = cleaned if cleaned else (None if err else "无相关新闻")
    return out

"""LLM 复核层 —— 两策略候选去重合并, 一次性调 Sonnet(经 claude -p 订阅)做新闻/
premarket/宏观复核。

CLI 调用姿势整体照抄 stock_picker/sonnet_analyzer.py(已在本仓库验证过的订阅态调用
惯例, flag 语义经 `claude --help` 核对过): MAX_THINKING_TOKENS=0 关 thinking 省
一半、清空 ANTHROPIC_API_KEY 走订阅不走付费 API、--strict-mcp-config(bool)搭配
--mcp-config '{"mcpServers":{}}' 不挂会话那堆 MCP 工具、cwd=临时目录避开项目
和候选去重逻辑(同一 ticker 若两个策略都命中, 只送一份, 用 strategies 字段标注)。

只调一次(≤30 个候选一次性打包进一个 prompt), 不是每票一次。任何失败(超时/非零
退出/JSON 解不出来)全部 fail-soft: 返回 status='error', 调用方(engine.py)照样只
出技术候选, 不因为 Sonnet 掉线就整体失败。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import config


def _find_claude() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "claude"
    if home_bin.exists():
        return str(home_bin)
    return "/usr/local/bin/claude"


CLAUDE = _find_claude()

_SYSTEM = """你是股票候选票的新闻/宏观复核员。用户只做 BULL CALL DEBIT SPREAD
(ATM长腿、10%宽度、35-45 DTE、90%止盈、不止损)。候选票分两组, 已各自通过硬性技术
面过滤: 策略A trend_follow「趋势跟踪」(近期强势多头排列)或策略B bottom_fish「抄
底」(长期上升趋势中回踩均线支撑)。你的任务只是查有没有"技术面看不出来但新闻/宏
观层面该躲开"的东西 —— 不是重新做技术分析, 也不要重复给技术面结论。

只输出一个 JSON 数组, 每个候选一项, 不要 markdown 代码块、不要任何解释性前后文、
不要在数组外加任何文字。"""


def _clean_env() -> dict:
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)          # 走订阅, 别走付费 API
    for k in list(env):                          # 清掉会话/PWA 继承的污染
        if (k.startswith("CLAUDE_CODE") or k.startswith("CLAUDE_AGENT")
                or k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(k, None)
    return env


def _invoke(user_text: str, model: str = config.SONNET_MODEL,
            timeout: int = config.SONNET_TIMEOUT):
    """跑一次 claude -p → (result_text, err_or_None, usage_dict)。"""
    env = _clean_env()
    cmd = [CLAUDE, "-p", "--model", model, "--output-format", "json",
           "--system-prompt", _SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "超时(claude -p)", {}
    except Exception as e:
        return None, f"调用失败: {e}", {}
    if p.returncode != 0:
        return None, (p.stderr or "claude 返回非零").strip()[:400], {}
    try:
        env_json = json.loads(p.stdout)
    except Exception as e:
        return None, f"envelope 解析失败: {e}", {}
    usage = env_json.get("usage") or {}
    return (env_json.get("result") or "").strip(), None, usage


def extract_json_array(text: Optional[str]):
    """模型偶尔会包 ```json 或加前言 —— 括号计数抠出第一个完整 JSON 数组(字符串里
    的方括号不会被误判为结构)。"""
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    i = s.find("[")
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    return None
    return None


def _fmt_headline_block(headlines: list) -> str:
    if not headlines:
        return "(今天没有 benzinga digest 数据)"
    return "\n".join(f"- {h}" for h in headlines)


def _fmt_candidate(c: dict, enrich: dict = None) -> str:
    """一票的技术面 + 可选的催化剂 / 基本面 (从 narratives + company_profiles 挂进来的)。"""
    tags = "/".join(c.get("strategies") or [c.get("strategy", "")])
    parts = [f"- {c.get('symbol')} [{tags}] price={c.get('latest')}"]
    if c.get("touch_ema") is not None:
        parts.append(f"touch_ema={c.get('touch_ema')}D")
    parts += [f"1m_chg={c.get('1m_chg')}%", f"3m_chg={c.get('3m_chg')}%",
              f"rsi={c.get('rsi')}", f"wtd_alpha={c.get('wtd_alpha')}"]
    head = " ".join(parts)
    e = (enrich or {}).get((c.get("symbol") or "").upper()) or {}
    extras = []
    if e.get("bull"):
        extras.append(f"  · 催化剂(利多): {' | '.join(e['bull'][:3])}")
    if e.get("bear"):
        extras.append(f"  · 风险(利空): {' | '.join(e['bear'][:2])}")
    if e.get("net"):
        extras.append(f"  · 多空净判断: {e['net']}")
    if e.get("profile"):
        extras.append(f"  · 业务简介: {e['profile']}")
    ue = e.get("upcoming_earnings")
    if ue:
        extras.append(f"  · 下次财报: {ue.get('date')} (距今 {ue.get('days')} 天)")
    return head + ("\n" + "\n".join(extras) if extras else "")


def _fmt_market_brief(mb: dict) -> list:
    """把今日 market_brief JSON 里最有用的两个字段 (tech_regime note + summary_day) 摊平。"""
    if not mb:
        return []
    lines = []
    note = mb.get("tech_regime_note")
    if note:
        lines.append(f"- 大盘技术态: {note}")
    tech = mb.get("tech") or {}
    if tech:
        parts = [f"{k}(vs50={v.get('vs_ma50')}%/vs200={v.get('vs_ma200')}%/drawdown={v.get('drawdown')}%)"
                 for k, v in tech.items()]
        lines.append(f"- 主要指数: {'  '.join(parts)}")
    return lines


def _fmt_rotation(rot: dict) -> list:
    if not rot:
        return []
    return [
        f"- 行业领涨: {', '.join(rot.get('leaders', [])[:5])}",
        f"- 行业落后: {', '.join(rot.get('laggards', [])[:5])}",
        f"- 转暖: {', '.join(rot.get('warming', [])[:4])}",
        f"- 转冷: {', '.join(rot.get('cooling', [])[:4])}",
    ]


def _fmt_options_pulse(op: dict) -> list:
    if not op:
        return []
    return [
        f"- 期权 IV 偏高板块: {', '.join(op.get('iv_high', [])[:5])}",
        f"- 期权 IV 偏低板块(便宜): {', '.join(op.get('iv_low', [])[:5])}",
        f"- 期权 skew 偏高(下方保护贵): {', '.join(op.get('skew_high', [])[:4])}",
    ]


def build_prompt(today: str, market_context: dict, candidates: list) -> str:
    enrich = market_context.get("ticker_enrichment") or {}
    lines = [
        f"今天日期: {today}",
        "任务: 对下列 BULL CALL DEBIT SPREAD 候选票做新闻/宏观复核 "
        "(策略A=trend_follow 趋势跟踪, 策略B=bottom_fish 抄底/回踩支撑)。",
        "",
        "市场环境:",
        f"- SPY 5日 {market_context.get('spy_5d')}% / 20日 {market_context.get('spy_20d')}%",
        f"- QQQ 5日 {market_context.get('qqq_5d')}%",
        f"- VIX {market_context.get('vix')}",
        f"- 热点话题: {', '.join(market_context.get('top_topics') or []) or '(无)'}",
    ]
    mb_lines = _fmt_market_brief(market_context.get("market_brief"))
    if mb_lines:
        lines += ["", "今日大盘判断 (market_brief):"] + mb_lines
    rot_lines = _fmt_rotation(market_context.get("industry_rotation"))
    if rot_lines:
        lines += ["", "行业轮动 (industry_rotation):"] + rot_lines
    op_lines = _fmt_options_pulse(market_context.get("options_pulse"))
    if op_lines:
        lines += ["", "期权情绪 (options_pulse):"] + op_lines
    lines += [
        "",
        "今日 benzinga 热点新闻标题:",
        _fmt_headline_block(market_context.get("headlines") or []),
    ]
    premarket = market_context.get("premarket_snippet") or ""
    if premarket:
        lines += ["", "盘前评论(benzinga chat 摘要, 可能被截断, 越靠后越新):", premarket]
    lines += [
        "",
        "候选票(已通过技术面硬过滤, 需要新闻/宏观层面复核; 每票附带催化剂+业务简介):",
        "\n".join(_fmt_candidate(c, enrich) for c in candidates),
        "",
        "对每个候选票, 返回 JSON, 字段:",
        "- symbol",
        '- verdict: "OPEN" | "SKIP"',
        "- confidence: 1-5 的整数",
        "- reason_zh: 一句话中文理由(可提及具体催化剂/行业/期权环境)",
        "  可提及即将到来的财报/事件是否会影响持仓期(35-45 DTE)",
        '- macro_risk: "low" | "med" | "high"',
        "- news_flags: 字符串数组(该票或所在板块今天有负面新闻才写, 否则给空数组)",
        "",
        "只返回 JSON 数组, 不要任何多余文字。",
    ]
    return "\n".join(lines)


def _dedupe_candidates(trend_candidates: list, bottom_candidates: list) -> list:
    """按 symbol 去重 —— 同一票如果两个策略都命中, 只送一份给 Sonnet, 用 strategies
    字段标注属于哪几个策略(供 prompt 展示 + 前端参考), 省 token 也避免同票被评两次
    可能得出不一致结论。"""
    merged: dict = {}
    for c in trend_candidates:
        sym = c["symbol"]
        entry = merged.setdefault(sym, dict(c))
        entry.setdefault("strategies", [])
        if "trend_follow" not in entry["strategies"]:
            entry["strategies"].append("trend_follow")
    for c in bottom_candidates:
        sym = c["symbol"]
        entry = merged.get(sym)
        if entry is None:
            entry = dict(c)
            entry.setdefault("strategies", [])
            merged[sym] = entry
        if "bottom_fish" not in entry["strategies"]:
            entry["strategies"].append("bottom_fish")
        if c.get("touch_ema") is not None:
            entry["touch_ema"] = c["touch_ema"]
    return list(merged.values())


def analyze(today: str, market_context: dict, trend_candidates: list,
            bottom_candidates: list) -> dict:
    """→ {status: 'ok'|'error', by_symbol: {SYM: {...}}, error, usage}。永不向上抛
    异常 —— 失败时 status='error', 调用方照样只出技术候选。"""
    merged = _dedupe_candidates(trend_candidates or [], bottom_candidates or [])
    if not merged:
        return {"status": "error", "by_symbol": {}, "error": "没有候选票", "usage": {}}
    try:
        prompt = build_prompt(today, market_context or {}, merged)
        raw, err, usage = _invoke(prompt)
        if err:
            print(f"stock_selector Sonnet 调用失败: {err}")
            return {"status": "error", "by_symbol": {}, "error": err, "usage": usage}
        arr = extract_json_array(raw)
        if arr is None:
            print(f"stock_selector Sonnet 输出不是合法 JSON 数组(前200字: {(raw or '')[:200]!r})")
            return {"status": "error", "by_symbol": {}, "error": "bad_json", "usage": usage}
        by_symbol = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper().strip()
            if not sym:
                continue
            conf = item.get("confidence")
            try:
                conf = int(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf = None
            by_symbol[sym] = {
                "verdict": str(item.get("verdict") or "").upper().strip(),
                "confidence": conf,
                "reason_zh": str(item.get("reason_zh") or item.get("reason") or "").strip(),
                "macro_risk": str(item.get("macro_risk") or "").lower().strip(),
                "news_flags": [str(x) for x in (item.get("news_flags") or [])],
            }
        return {"status": "ok", "by_symbol": by_symbol, "error": None, "usage": usage}
    except Exception as e:
        print(f"stock_selector Sonnet 分析层异常(不影响技术候选输出): {e}")
        return {"status": "error", "by_symbol": {}, "error": str(e), "usage": {}}

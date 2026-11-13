"""🤖 AI 持仓新闻分析: 读多空叙事 + 持仓状态 → 并行 claude -p Haiku → 逐仓判断催化变化。

每个持仓 ticker 一个 agent(并行 subprocess), 综合:
  1. 多空叙事(narratives/store 最新一天的 bull/bear/net/evidence)
  2. 持仓方向(bull/bear spread)、DTE、当前盈亏%、止盈进度
→ 判断: 新闻面是否有重大变化需要提前行动(止盈/止损/持有)。

用 claude -p --model claude-haiku-4-5, 走订阅(不花 API key)。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*\n?(.*?)\n?```', re.DOTALL)
_BRACE_RE = re.compile(r'\{[^{}]*"(?:news_sentiment|action|ticker)"[^{}]*\}', re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """从 Haiku 回复中提取 JSON 对象 — 处理 ```json 包裹、前后有文字等情况。"""
    if not text:
        return None
    text = text.strip()
    # 1. 直接就是 JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    # 2. ```json ... ``` 代码块
    for m in _JSON_BLOCK_RE.finditer(text):
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
    # 3. 文本中间找 { ... } 且含我们要的 key
    for m in re.finditer(r'\{', text):
        start = m.start()
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and ("news_sentiment" in obj or "action" in obj):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
    return None


CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/usr/local/bin/claude"
MODEL = "claude-haiku-4-5"
MAX_WORKERS = 6
TIMEOUT = 120

CACHE_DIR = Path(__file__).resolve().parent / "data" / "positions_ai_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_SYSTEM = """你是期权持仓新闻分析师。你只输出一个 JSON 对象, 不加任何其他文字。

输入: 仓位信息 + 该 ticker 的多空新闻叙事(可能为空)。

你必须输出且仅输出以下格式的 JSON(不要 markdown 代码块、不要解释、不要提问):
{"ticker":"XXX","news_sentiment":"bullish或bearish或neutral","catalyst":"简述催化事件或null","impact":"high或medium或low","action":"hold或take_profit或watch_risk","reason":"一两句中文,引用具体新闻证据"}

规则:
- 无新闻数据时: news_sentiment=neutral, catalyst=null, impact=low, 根据盈亏状态给 action
- impact=high 只在确实有大催化(财报/FDA/并购/做空报告/监管)时用
- action=take_profit: 盈利充分+利好已兑现, 或利空即将到来
- action=watch_risk: 利空新闻与持仓方向冲突, 或大催化不确定
- action=hold: 新闻面中性或利好持续, 无需行动
- 只基于给的数据, 不编造"""


def _invoke_one(ticker: str, user_text: str) -> dict:
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
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ticker": ticker, "error": "超时"}
    except Exception as e:
        return {"ticker": ticker, "error": str(e)[:200]}
    if p.returncode != 0:
        return {"ticker": ticker, "error": (p.stderr or "非零退出")[:200]}
    try:
        raw = json.loads(p.stdout).get("result", "")
        obj = _extract_json(raw)
        if obj and isinstance(obj, dict) and ("news_sentiment" in obj or "action" in obj):
            obj["ticker"] = ticker
            return obj
        return {"ticker": ticker, "error": "无有效 JSON", "raw": raw[:300]}
    except Exception:
        return {"ticker": ticker, "error": "解析失败", "raw": p.stdout[:500]}


def _build_prompt(strat: dict, narr: dict | None) -> str:
    tk = strat["underlying"]
    legs = strat.get("legs", [])
    direction = strat.get("name", "")
    dte = strat.get("dte")
    exp = strat.get("expiration", "")

    net_price = 0
    for lg in legs:
        net_price += (lg.get("entry") or 0) * (lg.get("qty") or 0) * (lg.get("mult") or 100)

    current_val = 0
    for lg in legs:
        current_val += (lg.get("mid") or lg.get("mark") or lg.get("current") or 0) * (lg.get("qty") or 0) * (lg.get("mult") or 100)

    pnl = current_val - net_price if net_price != 0 else 0
    pnl_pct = (pnl / abs(net_price) * 100) if net_price != 0 else 0

    strikes = [f"{lg.get('type','?')}{lg.get('strike',0)}" for lg in legs]
    is_bull = "bull" in direction.lower() or "call" in direction.lower()

    # underlying spot + 今日 %(caller 塞 _prev_close 进 strat dict)
    # 格式: underlying: NVDA($200.00, +2.00% today)
    spot = strat.get("current_price") or 0
    pc = strat.get("_prev_close") or 0
    spot_line = ""
    if spot and pc:
        pct = ((spot - pc) / pc) * 100 if pc else 0
        sign = "+" if pct >= 0 else ""
        spot_line = f"underlying: {tk}(${spot:.2f}, {sign}{pct:.2f}% today)"
    elif spot:
        spot_line = f"underlying: {tk}(${spot:.2f})"

    lines = [
        f"## 仓位: {tk}",
        f"策略: {direction}",
        f"行权价: {' / '.join(strikes)}",
        f"到期: {exp} (DTE={dte})",
        f"方向: {'看多' if is_bull else '看空'}",
        f"当前盈亏: ${pnl:.0f} ({pnl_pct:+.1f}%)",
    ]
    if spot_line:
        lines.append(spot_line)

    if narr and not narr.get("empty"):
        bull = narr.get("bull") or []
        bear = narr.get("bear") or []
        net = narr.get("net") or ""
        evidence = narr.get("evidence") or []

        lines.append(f"\n## 多空叙事 (截至 {narr.get('date', '?')})")
        if bull:
            lines.append("### 利多:")
            for b in bull[:8]:
                lines.append(f"- {b}")
        if bear:
            lines.append("### 利空:")
            for b in bear[:8]:
                lines.append(f"- {b}")
        if net:
            lines.append(f"### 综合判断: {net}")
        if evidence:
            lines.append("\n### 关键证据(最近新闻):")
            for ev in evidence[:12]:
                sent_label = {1: "利多", -1: "利空"}.get(ev.get("sent"), "中性")
                t = ev.get("title_cn") or ev.get("title_en") or ""
                lines.append(f"- [{sent_label}] {ev.get('date','')} {t}")
    else:
        lines.append("\n(该 ticker 无多空叙事数据)")

    return "\n".join(lines)


def analyze_positions(strategies: list) -> dict:
    """主入口: 接收 strategies 列表, 并行分析, 返回结果。"""
    from narratives import store as narr_store

    tickers = list({s["underlying"] for s in strategies if s.get("underlying")})
    if not tickers:
        return {"items": [], "error": "无持仓"}

    days = narr_store.list_days() or []
    today = datetime.date.today().isoformat()
    use_day = today if today in days else (days[0] if days else "")
    narr_all = {}
    if use_day:
        for e in (narr_store.load_day(use_day) or []):
            tk = str(e.get("ticker", "")).upper()
            if tk:
                narr_all[tk] = e

    tasks = []
    for strat in strategies:
        tk = strat["underlying"]
        narr = narr_all.get(tk)
        prompt = _build_prompt(strat, narr)
        tasks.append((tk, prompt))

    seen = set()
    deduped = []
    for tk, prompt in tasks:
        if tk not in seen:
            seen.add(tk)
            deduped.append((tk, prompt))
        else:
            for t, p in reversed(tasks):
                if t == tk:
                    prompt = p
                    break
            found = False
            for i, (t2, _) in enumerate(deduped):
                if t2 == tk:
                    found = True
                    break
            if not found:
                deduped.append((tk, prompt))

    results = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(deduped))) as pool:
        futs = {pool.submit(_invoke_one, tk, prompt): tk for tk, prompt in deduped}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"ticker": futs[fut], "error": str(e)[:200]})

    results.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(
        x.get("impact", "low"), 2))

    out = {
        "items": results,
        "n": len(results),
        "asof": datetime.datetime.now().isoformat(timespec="seconds"),
        "narrative_date": use_day,
    }

    try:
        cache_f = CACHE_DIR / f"{today}.json"
        tmp = cache_f.with_name(cache_f.name + ".tmp")   # renamed into place: never half a file
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        os.replace(tmp, cache_f)
    except Exception:
        pass

    return out


def load_cached() -> dict | None:
    today = datetime.date.today().isoformat()
    cache_f = CACHE_DIR / f"{today}.json"
    if cache_f.exists():
        try:
            return json.loads(cache_f.read_text())
        except Exception:
            pass
    return None

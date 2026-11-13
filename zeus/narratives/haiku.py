"""调 Haiku(经 claude CLI 复用订阅) 把一批票的新闻池整理成多空叙事。

⚠️ MAX_THINKING_TOKENS=0 关 thinking(纯抽取不需要, 省额度一半)。
⚠️ 删 ANTHROPIC_API_KEY → 走订阅 OAuth(有 key 会走付费 API)。
"""
from __future__ import annotations
import json, re, os, shutil, subprocess, tempfile
from pathlib import Path

# server(launchd 精简 PATH) 调用也要找得到: CLAUDE_BIN > PATH > ~/.local/bin > /usr/local/bin
def _find_claude():
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    found = shutil.which("claude")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "claude"
    if home_bin.exists():
        return str(home_bin)
    return "/usr/local/bin/claude"

CLAUDE = _find_claude()
MODEL = "claude-haiku-4-5"
TIMEOUT = 480   # 60 天窗口 + 正文体积更大, 单批时间放宽

_SYSTEM = """你是金融新闻整理助手。给定若干美股及其近 1-2 月新闻(finviz 带利好/利空原生情绪, benzinga 含标题+摘要+正文前段)。为每只股票整理成 bull/bear 双段,每条要点须包含「事实」+「逻辑/意义」两层。

【语言】所有 bull/bear/net 每一条(包括**加粗事实段**和 em-dash 之后的逻辑段)都必须是**简体中文陈述句**。源新闻是英文/韩文/日文时,必须翻译整合成中文,不得保留整段英文标题作为「事实」。只有**专有名词**(人名、公司名、产品/协议代号如 Cybercab/pTau217/STRC、金额单位如 $1B/¥100亿)可保留原文;其余分析/名词/动词/上下文必须中文。
✓ 正确: "**TD Cowen 将 MSTR 目标价从 $400 下调到 $260,BTC 年终预测降至 $100K(6月30日)** — 35% 目标价下修反映对 BTC 前景更悲观,分析师质疑 Saylor 杠杆策略"
✗ 错误: "**TD Cowen cut MSTR price target to $260 from $400** — 35% target reduction reflects bearish Bitcoin outlook"(事实段是英文违规)

规则:
- bull = 利多要点; bear = 利空要点。每条格式:「**核心事实(含日期/数字/主体)** — 为什么这对该公司重要或市场如何解读(一句话逻辑)」。事实必须来自给定新闻;逻辑是对新闻背后含义的简洁点评,可含常识性推理(如: FDA 批准 → 打开新市场; 大宗购买承诺 → 锁定长期收入; 关税豁免 → 成本压力缓解)。
- 忽略与公司无关的标题: 荐股/排行榜(如"3 Stocks to Buy")、"是否值得买"类观点、纯"股价涨跌X%"无原因、newsletter 推广。只整理实质公司消息(财报/指引/FDA/并购/订单/评级变动/诉讼/产品/高管/监管/宏观直接影响等)。
- 合并同一主题的重复消息成一条;同类小消息可合并成综合陈述。
- 只基于给定新闻+常识性金融/行业逻辑, 不编造具体数字, 不用先验事件。
- 每侧尽量 3-8 条(信息全面), 无实质消息则 []。
- net = 一句话综合判断多空谁占上风+主导叙事关键词, 中文。
只输出一个 JSON 数组, 每项 {"ticker":"..","bull":[".."],"bear":[".."],"net":".."}, 不要任何额外文字, 不要 markdown 代码围栏。"""

_TAG = {1: "利好", -1: "利空", 0: "中性"}


def _block(ticker, pool):
    lines = [f"### {ticker}"]
    ai = pool.get("ai")
    if ai:
        tag = _TAG.get(ai.get("sent"), "")
        cat = "|催化" if ai.get("catalyst") else ""
        lines.append(f"[finviz AI 为何异动|{tag}{cat}] {ai.get('title_en')}")
        if ai.get("summary"):
            lines.append(f"  摘要: {ai['summary']}")
    for n in pool.get("finviz", []):
        lines.append(f"[finviz|{_TAG.get(n.get('sent'),'')}|{n.get('raw') or n.get('date')}] {n.get('title_en')}")
    for b in pool.get("benzinga", []):
        head = f"[benzinga|{b.get('date')}] {b.get('title')} — {b.get('teaser')}"
        body = b.get("body") or ""
        if body:
            head += f"\n  正文: {body}"
        lines.append(head)
    return "\n".join(lines)


def build_user(pools, tickers):
    body = "\n\n".join(_block(t, pools[t]) for t in tickers)
    header = (f"请立即处理以下 {len(tickers)} 只股票的近 1-2 月新闻, 严格按系统提示输出"
              f" JSON 数组(每股一项, 含 ticker/bull/bear/net)。不要反问、不要解释、"
              f"不要额外文字。tickers 列表: {', '.join(tickers)}\n\n=== 新闻数据 ===\n\n")
    return header + body


def _parse_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\[.*\]", text, re.S)       # 兜底抽第一个数组
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _invoke(user_text):
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)          # 走订阅, 别走付费 API
    # 清掉「从交互式 Claude 会话/PWA 继承来的」环境污染(否则 CLAUDE_EFFORT=max 让
    # Haiku 狂想、CLAUDE_CODE_CHILD_SESSION 触发嵌套会话)→ 让它像「干净终端里 claude -p」。
    for _k in list(env):
        if (_k.startswith("CLAUDE_CODE") or _k.startswith("CLAUDE_AGENT")
                or _k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(_k, None)
    cmd = [CLAUDE, "-p", "--model", MODEL, "--output-format", "json",
           "--system-prompt", _SYSTEM,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           # 不加载 user 设置(~/.claude/settings.json) → 跳过 Stop/Notification 钩子,
           # 后台多空叙事不弹「Claude Cowork」通知; 订阅鉴权照常(非 --bare)。
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                       env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
    if p.returncode != 0:
        try:
            from narratives import claude_login as _cl
        except Exception:
            import claude_login as _cl
        return None, None, _cl.cli_error(p, "多空叙事")
    try:
        env_json = json.loads(p.stdout)
    except Exception as e:
        return None, None, f"envelope parse fail: {e}"
    return env_json.get("result"), env_json.get("usage"), None


def run_batch(pools, tickers, retries=2):
    """→ (results_by_ticker {t:{bull,bear,net}}, usage_dict, err_or_None)"""
    user_text = build_user(pools, tickers)
    err = None
    for _ in range(retries + 1):
        result, usage, e = _invoke(user_text)
        if e:
            err = e
            continue
        arr = _parse_array(result)
        if arr is None:
            err = "JSON array parse fail"
            continue
        out = {}
        for x in arr:
            if not isinstance(x, dict):
                continue
            t = (x.get("ticker") or "").upper()
            if t:
                out[t] = {"bull": x.get("bull") or [], "bear": x.get("bear") or [],
                          "net": x.get("net") or ""}
        return out, usage, None
    return {}, None, err

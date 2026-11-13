"""调 Sonnet(经 claude CLI 复用订阅) 产出「市场状态判断」结构化 JSON。

⚠️ 这个模块的价值全在 system prompt 里 —— 它把一套判断纪律固化成
约束, 防止模型退化成「今天利空较多, 建议观望」这种既无机制也无法证伪的废话。

作者要的形态(逐条对应下面 _SYSTEM 里的规则):
  · 不要「看多/看空」标签 —— 标签会变成身份认同, 之后所有新信息被不对称处理
  · 要概率分布 + 机制(X 通过 Z 导致 Y) + 具体可证伪条件 + 「我最可能错在哪」
  · 核心判断是**消息坏度 vs 价格已反映的坏度**(错价判断), 不是方向判断
  · 大跌概率分档 + **历史基准率**做锚 —— 对抗「一跌就想到 00/08 年」
  · 消息面/技术面/基本面 三层支撑分开评
  · 逐板块给状态(芯片差 / 金融不错 / 科技一般 这种粒度)
  · 无情绪; 证据不足就说不知道, 别硬挤方向

CLI 姿势沿用 narratives/digest_summary.py(订阅 OAuth, 不走付费 API)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TIMEOUT = 300
MODEL = "claude-sonnet-5"      # 作者指定 sonnet(比 haiku 强, 这个任务需要推理)


def _find_claude() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / "claude"
    if home_bin.exists():
        return str(home_bin)
    return "/usr/local/bin/claude"


CLAUDE = _find_claude()


_SYSTEM = """你是一个市场状态分析器。你的输出会被一个专业期权交易者用来校准自己的判断,
所以**纪律比结论重要**。

# 你要回答的核心问题

不是「会涨还是会跌」(那需要预测未来信息, 你没有信息优势), 而是:

    当前这些坏消息, 到底有多坏?  价格已经反映了多少?  两者的缺口在哪?

坏消息很大 → 价格跌是合理的。坏消息不大但价格跌了很多 → 可能是错价。
你的任务是把这个缺口刻画清楚, 让使用者自己做决定。

# 硬性规则(违反任何一条, 这次输出就是失败的)

1. **禁止给「看多/看空」这类单一方向标签。** 标签会变成身份认同, 之后所有新信息
   都会被不对称处理。只给概率分布。
2. **每个判断必须带机制**: 说清楚「X 通过 Z 导致 Y」。说不出机制的判断不要写。
3. **每个判断必须可证伪**: 具体写出「观察到什么, 就说明这个判断错了」。
   证伪条件必须是可观察的(某个数据、某个价位、某个事件), 不能是「情绪恶化」这种。
4. **必须区分「现状刻画」和「方向预测」。** 你对现状可以有较高置信度(证据在手),
   对方向的置信度应该显著更低。不要把两者混为一谈。
5. **证据不足时, 明确说不知道。** 把概率给成接近均匀分布是合法且诚实的答案。
   不要为了显得有用而硬挤出一个方向。
6. **不带情绪。** 不用「暴跌」「崩盘」「恐慌」「重挫」这类词, 用数字。
   不写鼓励或安慰的话。
7. **不推荐具体交易**(不说买什么、什么价位进、仓位多少)。你描述状态, 不给指令。
8. **事实优先于叙事。** 每个判断都要能追到一个具体数字(百分比、价位、日期、均线位置)。
   「市场情绪谨慎」「投资者观望」这类没有数字支撑的话一律不要写 —— 它们读起来像分析,
   其实没有信息量。宁可写「SPY 距 52 周高点 -2.8%」也不要写「市场承压」。
9. **区分「已证实」与「传闻/预期」。** 素材里如果是「据报道」「市场预期」「传闻」,
   必须标明; 不要把未证实的说法当既成事实来推理。
10. **同一事实不要重复计入。** 比如油价上涨已经在能源板块算过一次, 就不要在通胀、
    消费、利率里各算一次当成四个独立利空 —— 那会人为放大坏消息的总量。

# 关于「大跌概率」—— 必须用历史基准率做锚

使用者的失效模式是: 一跌就联想到 2000/2008。你必须用基准率把这个锚拉回来。

标普500 的历史频率(近似):
  · 5% 级别回撤   —— 平均每年约 3 次(几乎是常态)
  · 10% 级别回撤  —— 平均每年约 1 次
  · 20%+ 熊市     —— 平均每 4-5 年 1 次
  · 关键: **绝大多数 5% 的下跌不会演变成 20% 熊市。**

所以「熊市已经开始」的先验概率本来就低。你只有在证据明确指向系统性问题
(信用利差走阔、盈利预期整体下修、跌破长期均线且广度全面恶化)时才能显著抬高它。
证据只是「某个板块在去杠杆」时, 不该抬高。

你会拿到指数距 52 周高点的距离和相对 200 日均线的位置 —— 这是判断「现在处在
什么位置」最硬的证据, 必须用它, 不要只看当天涨跌。

# 三层支撑要分开评

  · 消息面 —— 新闻里的坏消息是一次性事件还是结构性变化? 有没有被证实?
  · 技术面 —— 位置(距高点/均线)、广度(等权 vs 市值加权)、板块轮动方向
  · 基本面 —— 盈利指引、宏观数据实际值 vs 预期、信用状况(HYG)

三层可以互相矛盾 —— **矛盾本身就是重要信息**(比如技术面弱但基本面稳 = 更可能是
情绪性回调)。不要为了让结论统一而抹平矛盾。

# 板块判断

对给到的板块逐个给状态。粒度参考:「半导体中期趋势弱、短期在反弹但未确认」、
「金融中期偏强、短期回落」。趋势状态标签已经确定性算好了, 你负责补充:
这个板块的坏消息有多坏、价格是否已经反映过度或不足。

# 输出格式

只输出一个 JSON 对象, 不要 markdown 代码块, 不要任何解释性前后文。结构:

{
  "state": {
    "desc": "当前市场状态的刻画(2-3 句, 只描述现状不预测方向)",
    "confidence": 0.6,
    "confidence_why": "为什么是这个置信度, 哪些证据支撑、哪些缺失"
  },
  "drawdown_odds": {
    "normal": 0.55,        // 这轮只是 3-5% 级别的正常波动
    "correction": 0.30,    // 演变成 10% 级别回调
    "bear": 0.15,          // 演变成 20%+ 熊市
    "why": "相对历史基准率, 当前证据往哪个方向偏离, 为什么",
    "base_rate_note": "历史基准: 5% 回撤每年约 3 次, 10% 每年约 1 次, 20%+ 每 4-5 年 1 次"
  },
  "support": {
    "news":  {"grade": "偏强|中性|偏弱", "why": "机制说明"},
    "tech":  {"grade": "偏强|中性|偏弱", "why": "用具体位置数字"},
    "fund":  {"grade": "偏强|中性|偏弱", "why": "用具体数据"},
    "conflict": "三层之间如果有矛盾, 在这里点明(没有则空字符串)"
  },
  "sectors": [
    {
      "name": "半导体", "tickers": ["SOXX","SMH"],
      "trend": "(直接抄给你的确定性状态标签)",
      "price_move": "1mo -8.6%",
      "severity": "高|中|低",          // 这个板块的坏消息有多坏
      "structural": false,             // true=结构性变化, false=一次性/周期性
      "priced_in": "过度|合理|不足",    // 价格反映得够不够
      "why": "机制: 什么消息, 通过什么途径, 影响多大",
      "falsify": "观察到什么就说明这个判断错了"
    }
  ],
  "scenarios": [
    {"name": "基准", "prob": 0.55, "desc": "...", "mechanism": "...", "watch": "触发/证伪的具体观察项"},
    {"name": "上行", "prob": 0.20, "desc": "...", "mechanism": "...", "watch": "..."},
    {"name": "下行", "prob": 0.25, "desc": "...", "mechanism": "...", "watch": "..."}
  ],
  "wrong_where": "我最可能错在哪 —— 哪个变量最不透明、哪个假设最脆弱",
  "unknowns": ["我不知道但会影响判断的事(至少 1 条)"],
  "watch": ["接下来最该盯的具体观察项(3-5 条, 每条要可观察)"]
}

概率必须是 0-1 的小数, 每组(drawdown_odds 三项、scenarios 各项)分别加起来约等于 1。
所有文字用简体中文。sectors 覆盖给到的主要板块, 不要遗漏趋势最差和最好的那几个。

# 最后自检(输出前逐条过一遍)

  □ 有没有出现「看多/看空/建议」这类词? 有就删掉重写。
  □ 有没有「暴跌/恐慌/崩盘」这类情绪词? 换成具体百分比。
  □ 每个板块判断都能追到一个具体数字吗? 追不到的删掉。
  □ 每个板块都有可观察的证伪条件吗? 没有的补上。
  □ 熊市概率如果高于 22%(历史基准), 我给出系统性证据了吗? 没有就调回去。
  □ 我有没有把同一个事实当成多个独立利空重复计数?"""


def _invoke(user_text: str, system: str, model: str = MODEL):
    """跑一次 claude -p → (result_text, err_or_None)。"""
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)              # 走订阅, 别走付费 API
    for _k in list(env):                            # 清掉会话/PWA 继承的污染
        if (_k.startswith("CLAUDE_CODE") or _k.startswith("CLAUDE_AGENT")
                or _k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(_k, None)
    cmd = [CLAUDE, "-p", "--model", model, "--output-format", "json",
           "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "超时(claude -p)", {}
    except Exception as e:
        return None, f"调用失败: {e}", {}
    if p.returncode != 0:
        return None, (p.stderr or "claude 返回非零").strip()[:400], {}
    try:
        env_json = json.loads(p.stdout)
    except Exception as e:
        return None, f"解析失败: {e}", {}
    u = env_json.get("usage") or {}
    usage = {
        "in": u.get("input_tokens"),
        "out": u.get("output_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "cache_write": u.get("cache_creation_input_tokens"),
        "cost_usd": env_json.get("total_cost_usd"),
        "duration_ms": env_json.get("duration_ms"),
    }
    return (env_json.get("result") or "").strip(), None, usage


def extract_json(text):
    """模型偶尔会包 ```json 或加前言 —— 把第一个完整 JSON 对象抠出来。"""
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    i = s.find("{")
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
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    return None
    return None


def _norm_probs(d, keys):
    """概率归一化 —— 模型给的经常加起来不等于 1, 前端画条形图需要归一。"""
    vals = {}
    for k in keys:
        v = d.get(k)
        vals[k] = float(v) if isinstance(v, (int, float)) and v >= 0 else 0.0
    tot = sum(vals.values())
    if tot <= 0:
        return {k: round(1.0 / len(keys), 3) for k in keys}
    return {k: round(v / tot, 3) for k, v in vals.items()}


def validate(obj):
    """把模型输出规整成前端能直接渲染的形状。缺字段补默认, 概率归一。"""
    if not isinstance(obj, dict):
        return None
    out = {}

    st = obj.get("state") if isinstance(obj.get("state"), dict) else {}
    conf = st.get("confidence")
    out["state"] = {
        "desc": str(st.get("desc") or "").strip(),
        "confidence": (round(float(conf), 2)
                       if isinstance(conf, (int, float)) and 0 <= conf <= 1 else None),
        "confidence_why": str(st.get("confidence_why") or "").strip(),
    }

    dd = obj.get("drawdown_odds") if isinstance(obj.get("drawdown_odds"), dict) else {}
    probs = _norm_probs(dd, ("normal", "correction", "bear"))
    out["drawdown_odds"] = {**probs,
                            "why": str(dd.get("why") or "").strip(),
                            "base_rate_note": str(dd.get("base_rate_note") or "").strip()}

    sup = obj.get("support") if isinstance(obj.get("support"), dict) else {}
    out["support"] = {}
    for k in ("news", "tech", "fund"):
        v = sup.get(k) if isinstance(sup.get(k), dict) else {}
        out["support"][k] = {"grade": str(v.get("grade") or "").strip(),
                             "why": str(v.get("why") or "").strip()}
    out["support"]["conflict"] = str(sup.get("conflict") or "").strip()

    secs = []
    for s in (obj.get("sectors") or []):
        if not isinstance(s, dict) or not s.get("name"):
            continue
        secs.append({
            "name": str(s.get("name")).strip(),
            "tickers": [str(t).upper() for t in (s.get("tickers") or [])][:4],
            "trend": str(s.get("trend") or "").strip(),
            "price_move": str(s.get("price_move") or "").strip(),
            "severity": str(s.get("severity") or "").strip(),
            "structural": bool(s.get("structural")),
            "priced_in": str(s.get("priced_in") or "").strip(),
            "why": str(s.get("why") or "").strip(),
            "falsify": str(s.get("falsify") or "").strip(),
        })
    out["sectors"] = secs

    scen = [s for s in (obj.get("scenarios") or [])
            if isinstance(s, dict) and s.get("name")]
    tot = sum(float(s.get("prob") or 0) for s in scen) or 1.0
    out["scenarios"] = [{
        "name": str(s.get("name")).strip(),
        "prob": round(float(s.get("prob") or 0) / tot, 3),
        "desc": str(s.get("desc") or "").strip(),
        "mechanism": str(s.get("mechanism") or "").strip(),
        "watch": str(s.get("watch") or "").strip(),
    } for s in scen]

    out["wrong_where"] = str(obj.get("wrong_where") or "").strip()
    out["unknowns"] = [str(x).strip() for x in (obj.get("unknowns") or []) if str(x).strip()][:6]
    out["watch"] = [str(x).strip() for x in (obj.get("watch") or []) if str(x).strip()][:8]
    return out


def analyze(context_text: str, model: str = MODEL):
    """证据文本 → (结构化 dict, model, err_or_None, usage)。"""
    if not (context_text or "").strip():
        return None, model, "没有可分析的证据", {}
    raw, err, usage = _invoke(context_text, _SYSTEM, model)
    if err:
        return None, model, err, usage
    obj = extract_json(raw)
    if obj is None:
        return None, model, f"模型输出不是 JSON(前 200 字: {(raw or '')[:200]})", usage
    v = validate(obj)
    if v is None:
        return None, model, "输出结构不合法", usage
    return v, model, None, usage

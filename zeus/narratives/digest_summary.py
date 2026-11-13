"""调 Haiku(经 claude CLI 复用订阅) 把一批新闻标题合并相似项 + 快速中文总结。

给 PWA Digest「🤖 AI总结」按钮用: 只看标题(不读正文), 用最便宜的当代模型(Haiku),
合并相似/重复消息, 按主题分组, 输出 markdown 中文给交易员速读。

- summarize(items)               全量总结一批标题
- summarize_incremental(old, new) 把新增标题并入已有摘要(刷新只总结增量, 省钱)

CLI 调用细节同 narratives/haiku.py:
- MAX_THINKING_TOKENS=0 关 thinking(纯整理不需要, 省额度一半)
- 删 ANTHROPIC_API_KEY → 走订阅 OAuth(有 key 会走付费 API)
- --strict-mcp-config + 空 mcp → 不加载会话那堆 MCP 工具
- 清掉继承的 CLAUDE_CODE_* / CLAUDE_EFFORT 污染(否则 Haiku 狂想 / 触发嵌套会话)
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, tempfile
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
MODEL = "claude-haiku-4-5"          # 最便宜的当代模型
TIMEOUT = 300
MAX_ITEMS = 500                     # 标题很短, 但仍封顶防极端日把 prompt 撑爆

_SYSTEM = """你是金融新闻速读助手。给定今天一批美股新闻标题(每行格式: [TICKER($价格, +/-x.x%)|情绪] 标题; 情绪=利好/利空/中性; 括号内的价格+涨跌幅是当时快照, 有的 ticker 没数据只有裸 ticker)。把它们快速整理成简洁中文摘要给交易员速读。规则:
- 合并相似/重复的消息: 同一事件不同来源、同一公司多条只保留要点, 不要逐条复述。
- 按主题/板块/重大事件分组, 每组用一个 **加粗中文小标题**, 下面用 - 列出 1-5 条要点。
- **每条要点句首必须标 ticker + 当时价 + 今日涨跌幅, 格式: `$NVDA($200.50, +2.3%)`; 输入里给了价的 ticker 一定要带上价 (千万别只写 $NVDA), 只有输入本身没价的才写裸 $ticker。多个 ticker 都要各自带自己那份 (价, %)**。
- 句末用 (利好)/(利空) 标方向(中性可省)。
- 重要的(财报/指引/并购/FDA/重大评级/监管/明确大涨大跌原因)排前面; 噪音(荐股榜单/泛泛"该不该买"观点/无原因涨跌)忽略或一句话带过。
- 若输入含【盘前评论】, 紧跟总览之后先输出一个 **🌅 盘前要点** 分组, 用 2-4 条提炼盘前关键观点(大盘方向/关注板块/重要数据或事件), 再输出各新闻分组。
- 最前面用一行以 > 开头的引用, 给出今日一句话总览。
- 只基于给定标题, 不编造、不用先验知识、不预测; 价格/涨跌幅只用输入里给的, 缺则不编。
- **无论给的标题有多少条(哪怕只有 1 条)都直接整理成摘要; 绝不要求用户补充更多、绝不寒暄反问、绝不说明你在做什么——直接输出摘要本身。**
输出 markdown, 简体中文, 不要代码围栏, 不要额外说明文字。"""

_SYSTEM_INCR = """你是金融新闻速读助手。下面给你「已有的中文新闻摘要」和「新增的美股新闻标题」(格式: [TICKER($价格, +/-x.x%)|情绪] 标题; 括号内的价格+涨跌是当时快照, 有的 ticker 只有裸 ticker 没数据)。把新增消息整合进已有摘要, 产出更新后的完整摘要。规则:
- 保留已有摘要的结构与内容; 把新增消息并入相关分组, 或在合适位置新增分组。
- 新增消息若与已有要点重复、或是同一事件的跟进, 就合并, 不要堆叠重复。
- **每条要点句首必须标 ticker + 当时价 + 今日涨跌幅, 格式: `$NVDA($200.50, +2.3%)`; 输入里给了价的 ticker 一定要带上价 (千万别只写 $NVDA), 只有输入本身没价的才写裸 $ticker**。
- 句末 (利好)/(利空); 最前面一行 > 今日总览(可据新增更新)。
- 忽略噪音(荐股榜单/泛泛观点/无原因涨跌)。只基于已有摘要和新增标题, 不编造; 价格/涨跌只用输入给的, 缺则不编。
- **无论新增多少条(哪怕只有 1 条)都直接产出更新后的完整摘要; 绝不要求用户补充、绝不寒暄反问、绝不说明你在做什么。**
输出 markdown, 简体中文, 不要代码围栏, 不要额外说明文字。"""

_SYSTEM_MOVERS = """你是金融异动新闻速读助手。给定今天美股的异动票(每只格式: $TK($价格, +/-x.x%) [板块] + 新闻/催化剂; 标 (无新闻) 的没找到原因; 极少数没价格的只有裸 $TK)。把这些异动票的**新闻**整理成简洁中文速读给交易员——重点是「发生了什么消息」, 不要按涨跌分多空两栏。规则:
- 按主题/事件/催化剂分组(财报/指引/评级/并购/FDA/产品/监管/题材/宏观数据等), 每组一个 **加粗中文小标题**。
- 同一条消息、同一题材涉及多只异动票的, 合并到一条说(如「AI 芯片需求 → $NVDA($200, +5%) / $AMD($120, +4%)」), 不要逐票复述。
- **每条要点里出现的 ticker 都必须带上「当时价 + 今日涨跌」, 格式: `$NVDA($200.50, +2.3%)`; 输入给了价的绝不能只写 $NVDA, 只有输入本身缺价的才允许裸 $ticker**。
- 每条要点用 1-2 句话讲明白「为什么动」的因果(这条新闻是什么 → 怎么推动了股价), 句末可标 (利好)/(利空)。
- 有新闻的优先、排前面; 没找到新闻、或新闻解释不了涨跌的异动, 统一归到最后一个 **❓ 原因未知** 分组逐条列(如「$XYZ($55, +3%)、$ABC($20, -4%)」), **绝不给它们编造原因——「原因未知」本身就是要如实告诉用户的信息**。
- 最前面一行以 > 开头的引用, 给今日异动一句话总览(主导题材 / 最大的异动)。
- 哪怕只有几只异动也直接逐条/分组总结、不寒暄反问。只基于给定数据, 不编造、不用先验知识、不预测; 价格/涨跌只用输入给的, 缺则不编。
输出 markdown, 简体中文, 不要代码围栏, 不要额外说明文字。"""

_SYSTEM_DIP = """你是美股抄底机会筛选助手。给定今天下跌(负异动)的股票列表,每只有跌幅%、板块、以及新闻/催化剂标题。你的任务是从中挑出**暂时性利空、可能值得抄底**的股票。规则:

**推荐抄底的(暂时性利空)**:
- 财报 beat 但"利好出尽"卖消息(基本面没问题,只是预期兑现回调)
- 短期事件冲击:CEO 换人、管理层变动、一次性减记、诉讼和解
- 板块轮动/获利了结(好公司跟着板块被杀,公司本身没坏消息)
- 评级下调但仅因估值过高(业务本身健康)
- 宏观恐慌(利率/关税/地缘)导致的无差别抛售
- 暂时性供应/产能问题、季节性波动
- 无明确坏消息但跌了(大概率是跟跌,可能有机会)

**不推荐的(结构性/严重问题)**:
- 核心产品/业务被技术颠覆(如被 AI 彻底取代)
- 严重财务造假/欺诈、SEC 调查
- 持续收入/利润大幅下滑、连续 miss + 下调指引
- 行业结构性衰退(非周期性下行)
- 重大安全事故/产品召回/永久性品牌损伤
- 破产风险/债务危机

输出格式:
> 一句话总结今天下跌异动的总体判断(有几只可能值得关注)

**🎯 抄底机会 · 暂时利空**
- $TICKER($价格, 跌幅%): 【为什么跌】哪条利空导致的 →【为什么是暂时的、可抄】1-2 句把因果讲明白

**⚠️ 继续跌 · 别接飞刀**
- $TICKER($价格, 跌幅%): 【为什么跌 / 为什么是结构性问题】1-2 句讲清因果

**格式硬约束: 输入里给了价的 ticker 必须以 `$TICKER($价格, 跌幅%)` 格式输出, 绝不能只写 `$TICKER (跌幅%)` 丢掉价格。**

讲明白因果(最重要的要求):
- 每只票都要用 1-2 句话说清「为什么跌」和「为什么是这个判断」的因果链,必须从给定新闻推理出来,别只给结论。
- **如果从给定新闻推不出下跌的因果(没新闻,或新闻和下跌对不上),理由就直接写「❓原因未知」,绝不编造一个看似合理的理由。** 仍可按跌幅/板块归类,但理由栏必须如实标「❓原因未知」——这本身就是要明确告诉用户的信息:它跌了、但 AI 没法解释为什么。

注意:
- 宁可多推荐抄底、少遗漏。美股长期上涨,暂时性利空大多能恢复。只有明确的结构性问题才放进「继续跌」。
- 无新闻的下跌票:跌幅不大(< 3%)归「抄底机会」、跌幅大归「继续跌」,但两者理由栏都标「❓原因未知」(无消息的下跌别强行解释)。
- **新闻是英文原文**(英文标题更精准);**绝不**因数据有问题/翻译生硬/标题不清晰而要求用户提供更多/更清晰的数据、绝不输出"建议方案"/"原始英文新闻"之类的反问。某条新闻看不懂就按「❓原因未知」处理,**始终按格式输出 🎯/⚠ 两段**,这是不可妥协的硬约束。
- 只基于给定数据,不编造。简洁中文。不要代码围栏。不要在「❓原因未知」前后加 ** 加粗。
- **无论多少只票直接输出分析,不寒暄不反问。**"""

_SYSTEM_MOMENTUM = """你是美股追涨 momentum 分析助手。给定今天上涨(正异动)的股票列表,每只有现价、涨幅%、板块、以及新闻/催化剂标题。从中分出**值得追涨**和**谨防回调势头不足**两类。规则:

**🚀 追涨(强催化剂、momentum 可能延续)**:
- 财报大幅 beat + 上调指引(基本面改善)
- 重大产品/技术突破(新产品发布/新药获批/重大合作协议)
- 被收购/并购消息(通常还有溢价空间)
- 行业政策利好(监管放松/补贴/关税豁免)
- 连续获分析师升级/目标价上调
- 大额回购/分红大增
- 进入新市场/获重大订单/客户
- IPO 上游/下游题材联动(如 SpaceX IPO 利好航天供应链)

**⏸️ 谨防回调(势头不足、追高危险)**:
- 无明确新闻的涨(可能轧空/技术反弹,不持续)
- 新闻是旧消息/已知事件炒作(已反映在价格里)
- meme/散户热炒(无基本面支撑)
- 纯低价股投机拉升
- 涨幅过大一日暴涨可能透支

输出格式:
> 一句话总结今天上涨异动的总体判断(主导题材/热门板块)

**🚀 追涨 · Momentum**
- $TICKER($价格, +涨幅%): 【为什么涨】哪条催化剂导致的 →【为什么还能追】1-2 句把因果讲明白

**⏸️ 谨防回调 · 势头不足**
- $TICKER($价格, +涨幅%): 【为什么涨 / 为什么追高有风险】1-2 句讲清因果

**格式硬约束: 输入里给了价的 ticker 必须以 `$TICKER($价格, +涨幅%)` 格式输出, 绝不能只写 `$TICKER (+涨幅%)` 丢掉价格。**

讲明白因果(最重要的要求):
- 每只票都要用 1-2 句话说清「为什么涨」和「为什么是这个判断」的因果链,必须从给定新闻推理出来,别只给结论。
- **如果从给定新闻推不出上涨的因果(没新闻,或新闻和上涨对不上),理由就直接写「❓原因未知」,绝不编造一个看似合理的理由。** 仍可按涨幅/板块归类,但理由栏必须如实标「❓原因未知」——这本身就是要明确告诉用户的信息:它动了、但 AI 没法解释为什么。

注意:
- 宁可多推荐追涨。有明确正面催化剂的都归「追涨」,只有明显没后劲/无催化的才放「谨防回调」。
- 无新闻的涨票一律归「谨防回调」且理由标「❓原因未知」(无消息的拉升别强行编催化剂)。
- 只基于给定数据,不编造。简洁中文。不要代码围栏。不要在「❓原因未知」前后加 ** 加粗。
- **无论多少只票直接输出分析,不寒暄不反问。**
- **新闻是英文原文**(英文标题更精准);**绝不**因数据有问题/翻译生硬/标题不清晰而要求用户提供更多/更清晰的数据、绝不输出"建议方案"/"原始英文新闻"之类的反问。某条新闻看不懂就按「❓原因未知」处理,**始终按格式输出 🚀/⏸ 两段**,这是不可妥协的硬约束。"""

_TAG = {1: "利好", -1: "利空", 0: "中性"}


def _build_user(items):
    lines = []
    for it in items[:MAX_ITEMS]:
        t = (it.get("t") or it.get("title") or "").strip()
        if not t:
            continue
        ks = it.get("k") or it.get("tickers") or []
        ks = [str(x).upper().lstrip("$") for x in ks if x][:4]
        # Ticker + 当前价 + 今日 %(前端 _dgAiCollect 快照,老新闻不带 q)
        # 格式: NVDA($212.01, +2.01%)
        q = it.get("q") or {}
        def _fmt(k):
            r = q.get(k)
            if not r or not r.get("p"):
                return k
            p = r.get("p"); c = r.get("c")
            if c is None:
                return f"{k}(${p:.2f})"
            sign = "+" if c >= 0 else ""
            return f"{k}(${p:.2f}, {sign}{c:.1f}%)"
        ktxt = ",".join(_fmt(k) for k in ks) if ks else "—"
        s = it.get("s")
        if s is None:
            s = it.get("sentiment")
        try:
            s = int(s)
        except (TypeError, ValueError):
            s = None
        stxt = _TAG.get(s, "")
        head = f"[{ktxt}{('|' + stxt) if stxt else ''}]"
        lines.append(f"{head} {t}")
    return "\n".join(lines)


def _prices_from_items(items):
    """从前端 payload 里 items[*].q 抽出 {TK: (price, pct)}。"""
    pm = {}
    for it in items or []:
        for tk, r in (it.get("q") or {}).items():
            if not isinstance(r, dict):
                continue
            p = r.get("p")
            c = r.get("c")
            if p and p > 0 and tk not in pm:
                pm[str(tk).upper().lstrip("$")] = (float(p), (None if c is None else float(c)))
    return pm


def _prices_from_movers(movers):
    """movers list → {TK: (last, change_pct)}。"""
    pm = {}
    for m in movers or []:
        tk = str(m.get("ticker") or "").upper().lstrip("$")
        if not tk:
            continue
        last = m.get("last") or 0
        ch = m.get("change_pct")
        if last and last > 0:
            pm[tk] = (float(last), (None if ch is None else float(ch)))
    return pm


_TK_RE = re.compile(r"\$([A-Z]{1,5})\b(?!\s*\(\s*\$)")


def _backfill_prices(text, prices_map):
    """扫 AI 输出里所有 `$TK`(后面不是 `($…`),用 prices_map 补成 `$TK($XX.XX, +Y.Y%)`。
    prices_map 缺该 ticker 就原样保留裸 `$TK`(输入本身就没数据)。
    也补公司名场景? 不,只补 `$TK` 明确 ticker,防止误伤(Netflix/Alphabet 等公司名不动)。"""
    if not text or not prices_map:
        return text or ""
    def _sub(m):
        tk = m.group(1)
        pr = prices_map.get(tk)
        if not pr:
            return m.group(0)          # 没数据,保留裸 $TK
        p, c = pr
        if c is None:
            return f"${tk}(${p:.2f})"
        sign = "+" if c >= 0 else ""
        return f"${tk}(${p:.2f}, {sign}{c:.1f}%)"
    return _TK_RE.sub(_sub, text)


def _strip_lead(text):
    """去掉模型偶尔加的开场白(首个结构化行 >/#/*/- 之前的寒暄), 保留从结构化内容起。"""
    if not text:
        return text
    lines = text.split("\n")
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if s[0] in ">#*-" or s[0] in "📈📉🌅":
            start = i
            break
        # 首个非空行是寒暄(非结构化)→ 继续找第一条结构化行
    if start is None or start == 0:
        return text          # 正常(以 > 开头)或全是散文 → 原样
    return "\n".join(lines[start:]).strip()


def _invoke(user_text, system):
    """跑一次 claude -p(给定 system) → (result_text, err_or_None)。"""
    env = dict(os.environ)
    env["MAX_THINKING_TOKENS"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)              # 走订阅, 别走付费 API
    for _k in list(env):                            # 清掉会话/PWA 继承的污染
        if (_k.startswith("CLAUDE_CODE") or _k.startswith("CLAUDE_AGENT")
                or _k in ("CLAUDE_EFFORT", "BAGGAGE")):
            env.pop(_k, None)
    cmd = [CLAUDE, "-p", "--model", MODEL, "--output-format", "json",
           "--system-prompt", system,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           # 只加载 project/local 设置, 不加载 user(~/.claude/settings.json) → 跳过作者的
           # Stop/Notification 钩子, 后台任务不再弹「Claude Cowork 任务完成」通知; 订阅鉴权照常(非 --bare)。
           "--setting-sources", "project,local",
           "--no-session-persistence"]
    try:
        p = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                           env=env, cwd=tempfile.gettempdir(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "超时(claude -p)"
    except Exception as e:
        return None, f"调用失败: {e}"
    if p.returncode != 0:
        # 认证/登录错误统一交给 claude_login: 归一化错误文本(登录错误在 stdout JSON 里,
        # 不走 stderr) + 登录失效则弹「重新 /login」通知。见 narratives/claude_login.py。
        try:
            from narratives import claude_login as _cl
        except Exception:                       # narratives/ 直接在 sys.path 上时
            import claude_login as _cl
        return None, _cl.cli_error(p, "AI 总结")
    try:
        env_json = json.loads(p.stdout)
    except Exception as e:
        return None, f"解析失败: {e}"
    return _strip_lead((env_json.get("result") or "").strip()), None


def summarize(items, premarket_text=""):
    """整理一批新闻标题(可选附盘前评论) → (summary_markdown, model, n_used, err_or_None)。"""
    body = _build_user(items or [])
    pm = (premarket_text or "").strip()
    if not body.strip() and not pm:
        return "", MODEL, 0, "没有可总结的标题"
    n = (body.count("\n") + 1) if body.strip() else 0
    if pm:
        user_text = ("【盘前评论 Chris Market Commentary】\n" + pm[:6000]
                     + "\n\n【今日新闻标题】\n" + (body if body.strip() else "(暂无)"))
    else:
        user_text = body
    out, err = _invoke(user_text, _SYSTEM)
    if err:
        return "", MODEL, n, err
    # Haiku 输出后 regex 补价 —— 兜底 model 漏掉 `$TK` 只写裸 ticker 的情况
    out = _backfill_prices(out, _prices_from_items(items))
    return out, MODEL, n, None


def summarize_incremental(old_summary, new_items):
    """把新增标题并入已有摘要 → (merged_markdown, model, n_new, err_or_None)。"""
    body = _build_user(new_items or [])
    if not body.strip():
        return (old_summary or ""), MODEL, 0, None      # 没有新增, 原样返回
    n_new = body.count("\n") + 1
    user_text = ("【已有摘要】\n" + ((old_summary or "(空)").strip())
                 + "\n\n【新增新闻标题】\n" + body)
    out, err = _invoke(user_text, _SYSTEM_INCR)
    if err:
        return (old_summary or ""), MODEL, n_new, err
    out = _backfill_prices(out, _prices_from_items(new_items))
    return out, MODEL, n_new, None


def _build_movers_user(movers):
    """异动票 → 文本(每行 $TK $价格 +x% [板块]: 新闻 / 新闻);不预分多空, 让模型按新闻主题归类。

    ⚠️ 优先英文原文(title_en)再回退中文(title_cn): Haiku 读英文同样流畅,
    而中文翻译对小众/技术性标题会出现"光学评估 算火花"这种语义错位的硬翻,
    模型读不懂会触发软拒绝(输出"请提供原文"而不是按格式归类), 整段输出作废。
    """
    lines = []
    for m in (movers or [])[:MAX_ITEMS]:
        tk = (m.get("ticker") or "").upper()
        if not tk:
            continue
        ch = m.get("change_pct") or 0.0
        last = m.get("last") or 0.0
        sec = m.get("sector") or ""
        titles = []
        for n in (m.get("news") or [])[:4]:
            t = (n.get("title_en") or n.get("title_cn") or "").strip()
            if t:
                titles.append(t)
        # 格式: $NVDA($212.01, +2.0%) [板块]: news
        if last:
            head = f"${tk}(${last:.2f}, {ch:+.1f}%)"
        else:
            head = f"${tk}({ch:+.1f}%)"
        if sec:
            head += f" [{sec}]"
        lines.append(head + ((": " + " / ".join(titles)) if titles else " (无新闻)"))
    return "\n".join(lines)


def _refusal_err(out: str, markers: tuple, label: str) -> str | None:
    """检测 Haiku 软拒绝:输出非空但不含任何预期 section header → 模型在评论/反问而非按格式输出。
    返回 err 字符串 或 None。markers 例:('🎯','⚠') 对 dip,('🚀','⏸') 对 up。"""
    if not out:
        return None  # 真空让上游用自己的 err
    if any(mk in out for mk in markers):
        return None  # 至少有一个区段头, 算正常
    # 拼一段提示, 截前 150 字让前端能看到 Haiku 到底说了啥
    head = out.strip().replace("\n", " ")[:150]
    return f"Haiku 软拒绝({label} 输出无 {'/'.join(markers)} 区段头): {head}"


MOVERS_CAP = 500   # 全量送(实测 260 只 ~11K tokens, Haiku 轻松)


def summarize_movers(movers):
    """按涨跌幅排序全量异动票的新闻 → (summary_markdown, model, n, err)。"""
    ms = sorted([m for m in (movers or []) if m.get("ticker")],
                key=lambda x: abs(x.get("change_pct") or 0.0), reverse=True)[:MOVERS_CAP]
    user_text = _build_movers_user(ms)
    if not user_text.strip():
        return "", MODEL, 0, "今天暂无异动票"
    out, err = _invoke(user_text, _SYSTEM_MOVERS)
    if err:
        return "", MODEL, len(ms), err
    out = _backfill_prices(out, _prices_from_movers(ms))
    return out, MODEL, len(ms), None


def summarize_dip_picks(movers):
    """只看下跌票 → Haiku 判断哪些值得抄底 → (summary_markdown, model, n, err)。"""
    downs = sorted([m for m in (movers or [])
                    if m.get("ticker") and (m.get("change_pct") or 0) < -1.0],
                   key=lambda x: x.get("change_pct") or 0)[:MOVERS_CAP]
    if not downs:
        return "", MODEL, 0, "今天没有跌超 1% 的票"
    user_text = _build_movers_user(downs)
    if not user_text.strip():
        return "", MODEL, 0, "今天没有跌超 1% 的票"
    out, err = _invoke(user_text, _SYSTEM_DIP)
    if err:
        return "", MODEL, len(downs), err
    soft = _refusal_err(out, ("🎯", "⚠"), "下跌")
    if soft:
        return "", MODEL, len(downs), soft
    out = _backfill_prices(out, _prices_from_movers(downs))
    return out, MODEL, len(downs), None


def summarize_momentum_up(movers):
    """只看上涨票 → Haiku 判断哪些有真catalyst值得追 → (summary_markdown, model, n, err)。"""
    ups = sorted([m for m in (movers or [])
                  if m.get("ticker") and (m.get("change_pct") or 0) > 1.0],
                 key=lambda x: x.get("change_pct") or 0, reverse=True)[:MOVERS_CAP]
    if not ups:
        return "", MODEL, 0, "今天没有涨超 1% 的票"
    user_text = _build_movers_user(ups)
    if not user_text.strip():
        return "", MODEL, 0, "今天没有涨超 1% 的票"
    out, err = _invoke(user_text, _SYSTEM_MOMENTUM)
    if err:
        return "", MODEL, len(ups), err
    soft = _refusal_err(out, ("🚀", "⏸"), "上涨")
    if soft:
        return "", MODEL, len(ups), soft
    out = _backfill_prices(out, _prices_from_movers(ups))
    return out, MODEL, len(ups), None


# ── 📈 大盘图「关节点」转折归因 ──────────────────────────────────────────────
# 给大盘图上每个 ZigZag 转折点(见顶/见底)配一段「是什么消息导致这个转折」。
# 输入 = 该转折点前后几天**已缓存的每日市场摘要**(便宜: 总结已经蒸馏过的摘要),
# 不重新读原始几百条标题。
_SYSTEM_PIVOT = """你是美股大盘转折点归因助手。给定一个指数 ETF(SPY=标普500 / QQQ=纳指100)的**价格转折点**(见顶或见底)和它前后几天的每日市场新闻摘要。你的任务:提炼**是什么消息/催化剂导致了这个转折**。

规则:
- 先输出一行以 > 开头的引用, 一句话点明这个转折的主因(如「> 见底反弹: 美联储紧急降息 + 财政刺激落地止住恐慌抛售」)。
- 然后 2-4 条要点, 每条讲清「哪条消息 → 怎么推动了这个转折」的因果链:
  - **见顶(TOP)**: 找终结上涨的利空催化(利率意外转鹰/通胀超预期/地缘冲突/财报暴雷/估值见顶获利了结)。
  - **见底(BOT)**: 找止跌反弹的利好或企稳催化(美联储转鸽/降息/刺激政策/关税缓和/超卖修复/危机解除)。
- 每条要点句末可标 (利空)/(利好)。重要催化排前面。
- **若从给定摘要里看不出明确的单一催化, 就如实说「无明确单一消息, 属技术性/情绪性/资金面调整(超卖反弹 / 获利了结 / 仓位再平衡)」——绝不硬编一个看似合理的原因。**
- 只基于给定的每日摘要, 不用先验知识、不编造具体数字。简体中文, markdown, 不要代码围栏, 不寒暄不反问。"""


def summarize_pivot(symbol, date, kind, move_pct, day_summaries):
    """转折点归因 → (summary_markdown, model, n_days, err)。
    symbol=SPY/QQQ; kind='high'(见顶)/'low'(见底); move_pct=转折前那段的涨跌幅;
    day_summaries=[(date, summary_text), ...] 转折点前后几天的每日市场摘要(按日期升序)。"""
    days = [(d, (s or "").strip()) for d, s in (day_summaries or []) if (s or "").strip()]
    if not days:
        return "", MODEL, 0, "该转折点附近没有可用的每日摘要"
    kind_cn = "见顶(从涨转跌)" if kind == "high" else "见底(从跌转涨)"
    lead = ""
    if move_pct is not None:
        try:
            mv = float(move_pct)
            lead = (f"此前约有 {mv:+.0f}% 的{'上涨' if mv >= 0 else '下跌'}, 在此转向。")
        except (TypeError, ValueError):
            pass
    header = (f"【转折点】{symbol} 于 {date} {kind_cn}。{lead}\n"
              f"【前后几天的每日市场摘要】(按日期):\n")
    blocks = []
    for d, s in days:
        blocks.append(f"—— {d} ——\n{s[:2500]}")
    user_text = header + "\n\n".join(blocks)
    out, err = _invoke(user_text, _SYSTEM_PIVOT)
    if err:
        return "", MODEL, len(days), err
    # 去掉 Haiku 偶发的开头分隔线 ---(_strip_lead 把 - 当结构行留下了)
    out = re.sub(r"^\s*-{3,}\s*\n", "", out or "")
    return out, MODEL, len(days), None

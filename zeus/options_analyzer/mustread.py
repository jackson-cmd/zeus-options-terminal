#!/usr/bin/env python3
"""Digest「必读」判定 —— 确定性纯函数，零外部依赖、永远免费。

两类必读：
- A 类：命中 399 票池 + 催化频道(财报/并购/FDA/合同/分析师/回购/内幕/异动…)。
- B 类(宁缺毋滥)：宏观频道 + 窄宏观关键词；纯政治不进。

LLM 不进此运行路径；最近 1 月由 `.mr.json` 覆盖层叠加(见 finalize_day)。
"""
from __future__ import annotations
import json, re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_UNIVERSE_PATH = _ROOT / "data" / "options_vol" / "core_universe.txt"
_DIGEST_DIR = _ROOT / "benzinga_digest" / "digests"

# Bucket A：催化频道 → 中文事实标签，按优先级 高→低（先命中先返回）
_REASON_TAGS = [
    ("Earnings", "财报"), ("Guidance", "指引"), ("M&A", "并购"),
    ("FDA", "FDA"), ("Contracts", "大合同"), ("Buyback", "回购"),
    ("Buybacks", "回购"), ("Offerings", "增发"), ("Dividends", "股息"),
    ("Insider Trades", "内幕"), ("Analyst Ratings", "分析师"),
    ("Price Target", "目标价"), ("Management", "管理层"),
    ("Legal", "法律"), ("Spinoffs", "分拆"),
    ("WIIM", "异动"), ("Movers", "异动"),
]

# Bucket B：宏观频道(白名单) —— 纯 Politics 不在内，实现「宁缺毋滥」
_MACRO_CH = {"Economics", "Macro Economic Events", "Econ #s",
             "Federal Reserve", "Treasuries", "Forex"}
# 窄宏观关键词 → 标签
_MACRO_KW = [
    ("利率", ["fed", "fomc", "powell", "rate cut", "rate hike", "interest rate",
              "rate decision", "central bank", "boj", "ecb", "treasury yield",
              "bond yield", "基准利率", "加息", "降息", "美联储"]),
    ("通胀", ["cpi", "inflation", "pce", "ppi", "通胀", "consumer price"]),
    ("就业", ["jobs report", "payroll", "nonfarm", "non-farm", "unemployment",
              "jobless claims", "非农", "失业率"]),
    ("关税", ["tariff", "trade war", "关税", "贸易战"]),
    ("地缘", ["opec", "crude oil", "oil price", "brent", "hormuz", "油价", "原油"]),
    ("增长", ["gdp", "recession", "经济衰退", "国内生产总值"]),
]

# 大盘综述流水账 + recap/listicle(标题命中即非必读)。
# 注意: 绝不收 "Why Is X" / "What's Going On With X" —— 那是作者要的 WIIM「讲原因」类。
_WRAP_RE = re.compile(
    r"stock market today|s&p\s*500 futures|will s&p\s*500 open|"
    r"market[- ]moving news for|leading and lagging sectors|"
    r"dow jones (hit|gains|falls|drops)|nasdaq (jumps|falls|gains)|"
    r"investor sentiment improves|stock market:|"
    r"what the analysts are saying about the market|"
    r"this week on wall street|best (month|week|quarter|year) since", re.I)

# ─── 黑名单 (标题命中即剔除, 无视票池/频道) ────────────────────────────
# 从 3 天 haiku drop=124 里归纳出的高频"标题即噪音"模式。原则:只砍
# **明确无 catalyst 上下文**的模板化 clickbait, 不砍 "Why Is X Falling"
# 这类真讲原因的 (WIIM)。
_NOISE_PATTERNS = [
    # 荐股 listicle: "5 Stocks In The Spotlight...", "10 Top Analyst Forecasts..."
    r"^\s*\d+\s+(?:top\s+)?(?:stocks?|etfs?|picks?|top\s+picks?|best\s+stocks?)\s+"
        r"(?:to\s+(?:buy|watch|sell|own|consider)|in\s+the\s+spotlight|"
        r"(?:for\s+)?(?:the\s+)?(?:week|month|monday|tuesday|wednesday|thursday|friday|today)|"
        r"under\s+\$\d+|from|that|on)",
    # "Top 5 Upgrades For Monday"
    r"\btop\s+\d+\s+(?:upgrades?|downgrades?|analyst\s+forecasts?|initiations?|"
        r"(?:most\s+)?(?:bullish|bearish|active|traded|accurate\s+analysts?))\s+"
        r"(?:for\s+)?(?:monday|tuesday|wednesday|thursday|friday|today|next\s+week|this\s+week)",
    # "How To Earn $500 A Month From Caterpillar Stock..."
    r"\bhow\s+to\s+(?:earn|make|collect|generate)\s+\$?\d+\+?\s*(?:a|per)\s+(?:day|week|month|year)\s+from",
    # "AMD Options Price A 9% Swing On Earnings" - 期权数据只是估值
    r"\boptions?\s+(?:price|imply|are\s+pricing|imply\s+a?)\s+a?\s*\d+%?\s+(?:swing|move|drop|jump|rally)",
    # "XXX Stock Is Gaining Monday: What's Going On?" - 星期几+无 catalyst
    r"\bstock\s+is\s+(?:gaining|falling|rising|dropping|surging|climbing|slipping|jumping|dipping|higher|lower)\s+"
        r"(?:monday|tuesday|wednesday|thursday|friday|today|premarket|pre-market)\s*[:\-–]\s*what.?s\s+going\s+on",
    # "Should You Buy X Now?" - 观点类
    r"\bshould\s+you\s+(?:buy|sell|hold|own|invest\s+in|avoid)\b",
    # "From Boom To Bust" 花哨叙事
    r"\bfrom\s+boom\s+to\s+bust\b",
    # "Worst Two Months of the Year" - 大盘泛观点
    r"\b(?:worst|best)\s+(?:month|week|quarter|year|two\s+months|three\s+months)\s+"
        r"(?:of\s+the\s+year|since|for|ahead)",
    # "These 9 Stocks Are Cashing In" - 隐晦荐股
    r"\bthese\s+\d+\s+(?:stocks?|etfs?|names?)\s+(?:are|will|could|might)\s+(?:cashing?\s+in|benefit|profit|gain|winning)",
    # "Stock Tests $310 Support: Can..." - 泛技术分析
    r"\bstock\s+tests?\s+\$\d+\s+(?:support|resistance)",
    # "AAPL Stock: xxx"/"MSFT Stock: xxx" 弱推 header (通常紧跟 clickbait)
    # 但这个模式太宽, 会误杀真新闻, 先不加
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.I)

# ─── 白名单 (标题命中强保, 带精细中文标签) ────────────────────────────
# 优先级: 越具体的越靠前 (第一命中的 tag 生效)。命中后仍要有票池∩tickers
# 才标 A 类 (纯宏观走 B 类原路径)。
_CATALYST_TAGS = [
    # ─── 财报超预期 (最优先, 明确 beat 关键词) ───
    (r"\b(?:double\s+beat|beat[- ]and[- ]raise|blockbuster\s+(?:beat|quarter))", "财报超预期"),
    (r"\b(?:earnings?|eps|revenue|profit|q[1-4])\s+(?:beat|top(?:ped|s)?|smash(?:es|ed)?|crush(?:es|ed)?)", "财报超预期"),
    (r"\bbeat(?:s|ing)?\s+(?:eps|earnings|revenue|estimate|expect|consensus|forecast|street|analyst)", "财报超预期"),
    (r"\b(?:posts?|delivers?|crushes?)\s+(?:strong|record|solid|robust)\s+q[1-4]", "财报超预期"),
    # ─── 财报不及 ───
    (r"\b(?:earnings?|eps|revenue|profit|q[1-4])\s+miss(?:es|ed)?", "财报不及"),
    (r"\bmiss(?:es|ed)?\s+(?:eps|earnings|revenue|estimate|expect|consensus|forecast|analyst)", "财报不及"),
    (r"\bdisappointing\s+q[1-4]", "财报不及"),
    # ─── 泛财报 (reports/preview/highlights 但没 beat/miss 上下文) ───
    (r"\b(?:reports?|posts?|delivers?|announces?)\s+q[1-4]\s+(?:20\d{2}\s+)?"
        r"(?:earnings|results|revenue|numbers|financials)", "财报"),
    (r"\bq[1-4]\s+(?:20\d{2}\s+)?(?:earnings|results|revenue|print|preview|highlights)", "财报"),
    (r"\b(?:earnings|q[1-4])\s+(?:preview|highlights|imminent)", "财报"),
    # 指引 (guidance) 上调/下调
    (r"\braises?\s+(?:its\s+)?(?:fy\s*)?(?:full[- ]year\s+)?(?:20\d{2}\s+)?"
        r"(?:sales|revenue|earnings?|eps|guidance|outlook|forecast)", "指引上调"),
    (r"\b(?:cuts?|lowers?|slashes?)\s+(?:its\s+)?(?:fy\s*)?(?:full[- ]year\s+)?(?:20\d{2}\s+)?"
        r"(?:sales|revenue|earnings?|eps|guidance|outlook|forecast)", "指引下调"),
    # 并购
    (r"\b(?:announce(?:s|d|ment)?|agrees?)\s+(?:to\s+acquire|merger\s+with|takeover\s+of|acquisition\s+of)",
     "并购"),
    (r"\bto\s+acquire\s+[A-Z][a-z]+", "并购"),
    (r"\bmerger\s+(?:buzz|talks|deal|announce)", "并购"),
    # FDA
    (r"\bfda\s+(?:approv(?:es?|al|ed)|clears?|grants?)\b", "FDA获批"),
    (r"\bfda\s+(?:reject|refuse|decline|crl\b|complete\s+response)", "FDA被拒"),
    (r"\bphase\s+(?:3|iii)\s+(?:trial|data|results?|success)", "临床数据"),
    # 大合同 (要求带金额, 避免误保小合同)
    (r"\b(?:awarded?|receives?|secures?|signs?|inks?)\s+.{0,80}?"
        r"(?:contract|order|deal|agreement).{0,40}?"
        r"\$\d+(?:\.\d+)?\s*(?:b(?:illion)?|m(?:illion)?)\b", "大合同"),
    (r"\bwins?\s+(?:pentagon|dod|army|navy|nasa)\s+contract", "大合同"),
    # 分析师升降级
    (r"\bupgrade(?:s|d)?\s+.{0,40}?to\s+(?:buy|overweight|outperform|strong\s+buy|conviction\s+buy)",
     "分析师升级"),
    (r"\bdowngrade(?:s|d)?\s+.{0,40}?to\s+(?:sell|underweight|underperform|reduce)", "分析师降级"),
    # 股票行动
    (r"\bfile(?:s|d)?\s+for\s+(?:chapter\s+\d+|bankruptcy|receivership)", "破产"),
    (r"\bstock\s+split\b|\bannounces?\s+.{0,20}?(?:stock\s+split|spin[- ]off)", "股票拆分"),
    (r"\b(?:announce(?:s|d)?|approves?)\s+.{0,40}?buyback|share\s+repurchase\s+program", "回购"),
    (r"\b(?:raises?|hikes?|boosts?|increases?)\s+.{0,20}?dividend", "股息上调"),
]
_CATALYST_COMPILED = [(re.compile(p, re.I), tag) for p, tag in _CATALYST_TAGS]


def _match_catalyst(title: str):
    """→ 中文标签 或 None (首个命中的规则生效)。"""
    for pat, tag in _CATALYST_COMPILED:
        if pat.search(title or ""):
            return tag
    return None


def is_noise(article: dict) -> bool:
    """True = 标题命中 clickbait 黑名单或大盘 recap 模板 (可默认过滤掉)。
    独立于 classify — 前端「✨ 去噪」chip 用它做主列表过滤,
    和「⭐必读」正交 (必读逻辑更严, 只留强 catalyst)。"""
    t = article.get("title") or ""
    return bool(_NOISE_RE.search(t) or _WRAP_RE.search(t))


_MIN_BODY = 120

# ── 票池 ──
_universe_cache = None
def load_universe(path=None) -> set:
    global _universe_cache
    if _universe_cache is None:
        p = Path(path) if path else _UNIVERSE_PATH
        try:
            _universe_cache = {l.strip().upper() for l in p.read_text(encoding="utf-8").splitlines()
                               if l.strip() and not l.lstrip().startswith("#")}
        except Exception:
            _universe_cache = set()
    return _universe_cache

def _body_text(article: dict) -> str:
    return " ".join((p.get("cn") or p.get("en") or "") for p in (article.get("paras") or []))

# ── 判定 ──
def classify(article: dict, universe: set) -> tuple:
    """→ (mr: 0/1, bucket: 'A'|'B'|'', tag: 中文标签)

    优先级:
      1. 黑名单 (_NOISE_RE / _WRAP_RE) — 命中即剔除, 无视票池/频道
      2. 白名单 (_CATALYST_COMPILED) — 命中且有票池票 → A 类强保, 用命中的精细 tag
      3. 原有 A 类规则 (票池 ∩ 催化频道 ∩ 正文≥120字)
      4. 原有 B 类规则 (宏观频道 ∩ 窄宏观关键词)
    """
    title = article.get("title") or ""
    # 1) 黑名单 (标题级, 不看正文长度): 明确 clickbait 模板
    if _NOISE_RE.search(title):
        return (0, "", "")
    if _WRAP_RE.search(title):
        return (0, "", "")

    tickers = {str(t).upper().lstrip("$") for t in (article.get("tickers") or [])}
    ov = tickers & universe

    # 2) 白名单: 标题里有明确 catalyst 词 + 有票池票 → 强保 + 精细 tag
    #    (纯宏观标题命中白名单但无票, 走后面的 B 类规则)
    if ov:
        cat = _match_catalyst(title)
        if cat:
            return (1, "A", cat)

    # 3) 原有 A/B 类规则 (要求正文长度)
    body = _body_text(article)
    if len(body) < _MIN_BODY:
        return (0, "", "")
    ch = set(article.get("channels") or [])
    if ov:
        for c, tag in _REASON_TAGS:
            if c in ch:
                return (1, "A", tag)
    if ch & _MACRO_CH:
        low = (title + " " + body).lower()
        for tag, kws in _MACRO_KW:
            if any(k in low for k in kws):
                return (1, "B", tag)
    return (0, "", "")

def _primary_ticker(item: dict, universe: set):
    for t in (item.get("tickers") or []):
        u = str(t).upper().lstrip("$")
        if u in universe:
            return u
    return None

_TAG_RANK = {tag: i for i, (_, tag) in enumerate(_REASON_TAGS)}

def _dedupe_ticker(items: list, universe: set, max_per_ticker: int = 3) -> None:
    """A 类同一票命中过多 → 只留 max_per_ticker 条(高优先频道+早时间)，其余 mr=0。"""
    by_tk = {}
    for it in items:
        if it.get("mr") == 1 and it.get("mr_b") == "A":
            k = _primary_ticker(it, universe)
            if k:
                by_tk.setdefault(k, []).append(it)
    for lst in by_tk.values():
        if len(lst) <= max_per_ticker:
            continue
        lst.sort(key=lambda it: (_TAG_RANK.get(it.get("mr_tag"), 99), it.get("hhmm", "")))
        for it in lst[max_per_ticker:]:
            it["mr"] = 0

def _dedupe_macro(items: list, max_per_macro: int = 2) -> None:
    """B 类同一宏观标签过多(如一篇Fed演讲被切成5条) → 只留 max_per_macro 条(早时间)，其余 mr=0。"""
    by_tag = {}
    for it in items:
        if it.get("mr") == 1 and it.get("mr_b") == "B":
            by_tag.setdefault(it.get("mr_tag"), []).append(it)
    for lst in by_tag.values():
        if len(lst) <= max_per_macro:
            continue
        lst.sort(key=lambda it: it.get("hhmm", ""))
        for it in lst[max_per_macro:]:
            it["mr"] = 0

def dedupe_day(items: list, universe: set, max_per_ticker: int = 3, max_per_macro: int = 2) -> None:
    """天级去重(就地修改 mr)：A 类按票去重 + B 类按宏观标签去重。"""
    _dedupe_ticker(items, universe, max_per_ticker)
    _dedupe_macro(items, max_per_macro)

# ── 覆盖层(最近 1 月 LLM 精修) ──
_overlay_cache = {}
def overlay_path(date: str) -> Path:
    return _DIGEST_DIR / date[:7] / f".{date}.mr.json"

def overlay_load(date: str) -> dict:
    if date in _overlay_cache:
        return _overlay_cache[date]
    p = overlay_path(date)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    _overlay_cache[date] = data
    return data

def overlay_save(date: str, mapping: dict) -> None:
    p = overlay_path(date)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    _overlay_cache.pop(date, None)

def apply_overlay_item(item: dict, date: str) -> None:
    """单篇按覆盖层调整(供 search 跨天用)。"""
    e = overlay_load(date).get(item.get("id"))
    if e is not None:
        item["mr"] = int(e.get("v", 0))
        if e.get("tag"):
            item["mr_tag"] = e["tag"]

def finalize_day(items: list, date: str, universe: set) -> list:
    """天级定稿(就地修改并返回)：
    - 有覆盖层：A 类以 LLM 裁决为准(清噪音/热票刷屏)；B 类宏观**不受 LLM 覆盖**、永远走规则(只做宏观去重)——
      因为作者定调 B=规则驱动+宁缺毋滥，避免 haiku 把真宏观(如央行加息)误当政治口水砍掉。
    - 无覆盖层：A+B 全走规则去重。"""
    ov = overlay_load(date)
    if ov:
        for it in items:
            if it.get("mr_b") == "B":
                continue                      # B 宏观纯规则驱动, 跳过 LLM 覆盖
            e = ov.get(it.get("id"))
            if e is not None:
                it["mr"] = int(e.get("v", 0))
                if e.get("tag"):
                    it["mr_tag"] = e["tag"]
        _dedupe_macro(items)                   # B 仍需去重(Fed演讲碎片等)
        return items
    dedupe_day(items, universe)
    return items

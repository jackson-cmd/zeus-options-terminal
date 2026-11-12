"""对模型输出做**偏差审计** —— 纯函数, 不调 LLM。

设计原则:「尽量减少情绪偏差, 追求事实」。光在 system prompt 里写规则是
不够的 —— 模型会漂移。这里把规则做成**可执行的检查**, 每次生成后自动跑, 结果
连同判断一起展示。发现问题不隐藏, 让使用者知道这次输出哪里不够硬。

检查项(每条都对应 prompt 里的一条硬性规则):
  1. 情绪词        —— 「暴跌/崩盘/恐慌」这类词会放大灾难化, 用数字代替
  2. 方向标签      —— 「看多/看空/牛市/熊市来了」会变成身份认同
  3. 机制缺失      —— 板块判断说不出「为什么」= 不是判断是感觉
  4. 证伪缺失      —— 说不出「什么情况算我错」= 不可证伪 = 信仰
  5. 概率合法性    —— 和为 1; 不出现 0/1 这种「确定」的断言
  6. 事实密度      —— 有没有引用具体数字(百分比/价位/日期), 空泛叙述不算判断
  7. 极端偏离基准  —— 熊市概率显著高于历史基准率却没给出系统性证据
"""
from __future__ import annotations

import re

# 会放大情绪的词。中性替代: 用具体百分比。
EMOTION_WORDS = [
    "暴跌", "崩盘", "崩溃", "恐慌", "重挫", "惨烈", "血洗", "雪崩", "灾难",
    "疯狂", "狂欢", "暴涨", "飙升", "末日", "危机四伏", "岌岌可危", "触目惊心",
]
# 会变成身份认同的方向标签
DIRECTION_LABELS = ["看多", "看空", "做多", "做空", "牛市来了", "熊市来了",
                    "建议买入", "建议卖出", "应该买", "应该卖", "建议观望"]
# 事实性标记: 百分比 / 价位 / 日期 / 倍数
_FACT_RE = re.compile(r"[-+]?\d+(\.\d+)?\s*(%|pp|bp|美元|倍)|\d{1,2}/\d{1,2}|"
                      r"\d{4}-\d{2}-\d{2}|MA\d+|\$\d")

BEAR_BASE_RATE = 0.22      # 20%+ 熊市在任一年份的历史近似频率(每 4-5 年 1 次)


def _texts(d) -> list:
    """把输出里所有自由文本摘出来(用于扫词)。"""
    out = []
    st = d.get("state") or {}
    out += [st.get("desc", ""), st.get("confidence_why", "")]
    dd = d.get("drawdown_odds") or {}
    out += [dd.get("why", "")]
    sup = d.get("support") or {}
    for k in ("news", "tech", "fund"):
        v = sup.get(k) or {}
        out += [v.get("why", "")]
    out += [sup.get("conflict", "")]
    for s in (d.get("sectors") or []):
        out += [s.get("why", ""), s.get("falsify", "")]
    for s in (d.get("scenarios") or []):
        out += [s.get("desc", ""), s.get("mechanism", ""), s.get("watch", "")]
    out += [d.get("wrong_where", "")]
    out += list(d.get("unknowns") or []) + list(d.get("watch") or [])
    return [str(x) for x in out if x]


def find_emotion(d) -> list:
    """命中的情绪词(去重)。"""
    blob = " ".join(_texts(d))
    return sorted({w for w in EMOTION_WORDS if w in blob})


def find_direction_labels(d) -> list:
    """命中的方向标签。⚠️「看多/看空」在描述**别人的**仓位或市场定价时是中性的,
    但这里宁可误报 —— 让人扫一眼确认, 好过让标签悄悄溜进判断。"""
    blob = " ".join(_texts(d))
    return sorted({w for w in DIRECTION_LABELS if w in blob})


def missing_mechanism(d) -> list:
    """哪些板块判断没给机制(why 太短 = 说不出为什么)。"""
    return [s.get("name", "?") for s in (d.get("sectors") or [])
            if len((s.get("why") or "").strip()) < 20]


def missing_falsify(d) -> list:
    """哪些板块判断没给证伪条件。"""
    return [s.get("name", "?") for s in (d.get("sectors") or [])
            if len((s.get("falsify") or "").strip()) < 10]


def prob_issues(d) -> list:
    """概率的合法性问题。"""
    issues = []
    dd = d.get("drawdown_odds") or {}
    vals = [dd.get(k) for k in ("normal", "correction", "bear")]
    if all(isinstance(v, (int, float)) for v in vals):
        tot = sum(vals)
        if abs(tot - 1.0) > 0.02:
            issues.append(f"大跌概率和为 {tot:.2f}(应为 1)")
        for k, v in zip(("normal", "correction", "bear"), vals):
            if v <= 0.0 or v >= 1.0:
                issues.append(f"{k}={v} 是「确定」断言(概率不该取 0 或 1)")
    else:
        issues.append("大跌概率缺失或非数字")
    scen = d.get("scenarios") or []
    if scen:
        tot = sum(float(s.get("prob") or 0) for s in scen)
        if abs(tot - 1.0) > 0.02:
            issues.append(f"情景概率和为 {tot:.2f}(应为 1)")
    return issues


def fact_density(d) -> dict:
    """引用了具体数字的文本块占比 —— 太低说明在讲故事而不是摆事实。"""
    ts = [t for t in _texts(d) if len(t) >= 25]
    if not ts:
        return {"n": 0, "with_facts": 0, "ratio": 0.0}
    n_fact = sum(1 for t in ts if _FACT_RE.search(t))
    return {"n": len(ts), "with_facts": n_fact, "ratio": round(n_fact / len(ts), 2)}


def base_rate_deviation(d) -> dict | None:
    """熊市概率相对历史基准的偏离。显著高于基准却说不出系统性证据 = 灾难化嫌疑。"""
    dd = d.get("drawdown_odds") or {}
    bear = dd.get("bear")
    if not isinstance(bear, (int, float)):
        return None
    why = (dd.get("why") or "")
    # 「系统性」证据的关键词 —— 有这些才算给了抬高的理由
    has_reason = any(k in why for k in
                     ("信用", "利差", "HYG", "跌破", "200日", "MA200", "广度",
                      "盈利下修", "系统性", "流动性"))
    return {"bear": round(bear, 3), "base": BEAR_BASE_RATE,
            "delta": round(bear - BEAR_BASE_RATE, 3),
            "elevated_without_reason": bool(bear > BEAR_BASE_RATE + 0.10 and not has_reason)}


def audit(d) -> dict:
    """跑全部检查 → {score, checks[], flags[]}。score 100 分制, 每个问题扣分。"""
    if not isinstance(d, dict):
        return {"score": 0, "checks": [], "flags": ["输出不是对象"]}
    emo = find_emotion(d)
    lbl = find_direction_labels(d)
    no_mech = missing_mechanism(d)
    no_fals = missing_falsify(d)
    probs = prob_issues(d)
    facts = fact_density(d)
    dev = base_rate_deviation(d)

    checks = [
        {"key": "emotion", "name": "无情绪词", "ok": not emo,
         "detail": ("命中: " + ", ".join(emo)) if emo else "未使用情绪化措辞"},
        {"key": "labels", "name": "无方向标签", "ok": not lbl,
         "detail": ("命中: " + ", ".join(lbl)) if lbl else "未出现看多/看空类标签"},
        {"key": "mechanism", "name": "判断都带机制", "ok": not no_mech,
         "detail": ("缺机制: " + ", ".join(no_mech[:5])) if no_mech else "每个板块都说明了机制"},
        {"key": "falsify", "name": "判断都可证伪", "ok": not no_fals,
         "detail": ("缺证伪条件: " + ", ".join(no_fals[:5])) if no_fals else "每个板块都给了证伪条件"},
        {"key": "probs", "name": "概率合法", "ok": not probs,
         "detail": ("; ".join(probs)) if probs else "概率归一且未出现 0/1 断言"},
        {"key": "facts", "name": "事实密度", "ok": facts["ratio"] >= 0.5,
         "detail": f"{facts['with_facts']}/{facts['n']} 段引用了具体数字({facts['ratio']:.0%})"},
    ]
    if dev:
        ok = not dev["elevated_without_reason"]
        checks.append({
            "key": "base_rate", "name": "熊市概率有据", "ok": ok,
            "detail": (f"熊市 {dev['bear']:.0%} vs 历史基准 {dev['base']:.0%}"
                       + ("(显著抬高但未给系统性证据)" if not ok else "")),
        })

    n = len(checks)
    score = round(100 * sum(1 for c in checks if c["ok"]) / n) if n else 0
    return {"score": score, "checks": checks,
            "flags": [c["name"] for c in checks if not c["ok"]],
            "fact_density": facts}

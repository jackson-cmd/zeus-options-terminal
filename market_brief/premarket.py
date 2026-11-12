"""从 Pre-market 聊天全量文本里切出最近 N 天的盘前评论 —— 纯函数。

data/benzinga/YYYY-MM-DD.json 的 chris-market-commentary.text 是**累积全量**
聊天记录(134 万字, 771 天), 不能整个喂给模型。这里按日期标记切块, 只取最近几天。

为什么值得加: 这是当天唯一带「市场结构」判断的素材 —— SPY 关键支撑位、gamma
状态、implied move、板块轮动的实时解读。而且是同一个人连续写的, 前后几天连起来
能看出**他的判断有没有变**(比任何单日快照都有信息量)。
"""
from __future__ import annotations

import datetime as _dt
import re

# 形如 "7/23/2026" 独占一行 —— Stream Chat 的日期分隔
_DATE_RE = re.compile(r"(?m)^(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def split_days(text: str) -> list:
    """全量文本 → [{date, body}], 按时间升序。解析不了就返回空(不猜)。"""
    if not text:
        return []
    marks = list(_DATE_RE.finditer(text))
    if not marks:
        return []
    out = []
    for i, m in enumerate(marks):
        try:
            mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            day = _dt.date(yy, mm, dd).isoformat()
        except Exception:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append({"date": day, "body": body})
    return out


def _clean(body: str) -> str:
    """去掉「Chris Capre / 8:14AM」这类署名行和多余空行, 只留正文。"""
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s == ".":
            continue
        if s in ("Chris Capre",) or re.fullmatch(r"\d{1,2}:\d{2}\s*[AP]M", s):
            continue
        lines.append(s)
    return "\n".join(lines)


def recent(text: str, days: int = 5, max_chars_per_day: int = 3500,
           today: str = None) -> list:
    """最近 N 天的盘前评论(升序) → [{date, body}]。

    只收 today 当天及之前的(防止解析出未来日期的脏数据)。
    """
    blocks = split_days(text)
    if not blocks:
        return []
    if today:
        blocks = [b for b in blocks if b["date"] <= today]
    picked = blocks[-days:]
    return [{"date": b["date"], "body": _clean(b["body"])[:max_chars_per_day]}
            for b in picked]


def render(blocks) -> str:
    """拼成喂给模型的文本块。"""
    if not blocks:
        return ""
    return "\n\n".join(f"—— {b['date']} ——\n{b['body']}" for b in blocks if b.get("body"))

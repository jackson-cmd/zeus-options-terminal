"""把本地各库拼成证据 → 调 Sonnet → 落缓存。

所有数据源都是**本地已有的**, 不新增任何爬取:
  · benzinga_digest/ai_summary/YYYY-MM-DD.json  —— digest 每天的 AI 总结(直接用, 不重复总结)
  · benzinga_digest 当天条目                     —— 挑有解读的文章(articles.pick)
  · data/heatmap/etf_trends.json                —— 25 板块 + 指数, 多周期
  · data/daily_ohlc.parquet                     —— 指数技术面位置(距高点/均线)
  · trade_journal/data/calendar_cache.json      —— 财报 + 宏观日历
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import articles as _articles  # noqa: E402
import audit as _audit  # noqa: E402
import brief as _brief  # noqa: E402
import context as _context  # noqa: E402
import premarket as _pm  # noqa: E402
import technicals as _tech  # noqa: E402

REPO = _HERE.parent
CACHE_DIR = REPO / "data" / "market_brief"

# 技术面看这几个 —— 判断「现在处在什么位置」用大盘/风格, 不用个股
TECH_TICKERS = ("SPY", "QQQ", "IWM", "SOXX")


def _today() -> str:
    return _dt.date.today().isoformat()


# ── 数据读取(每个都 fail-soft, 缺了就少一块证据) ──────────────────

def _load_digest_summary(day: str):
    """(summary, 实际取到的日期)。

    ⚠️ 当天的 digest AI 总结由早 8:30 的任务生成 —— 在那之前跑本分析会拿不到,
    新闻层就**静默消失**了(模型会以为今天没新闻)。所以回退到最近一份可用的,
    并把日期一起返回, 在证据里标明「这是哪天的新闻」, 不冒充今天。
    """
    d = REPO / "benzinga_digest" / "ai_summary"
    try:
        cands = sorted(p for p in d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")
                       if p.stem <= day)
    except Exception:
        return "", ""
    for p in reversed(cands[-5:]):        # 最多回看 5 天
        try:
            s = (json.loads(p.read_text(encoding="utf-8")) or {}).get("summary") or ""
            if s.strip():
                return s, p.stem
        except Exception:
            continue
    return "", ""


def _load_digest_items(day: str) -> list:
    try:
        src = REPO / "benzinga_digest" / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from benzinga_mcp import digest_store as ds
        return ds.load_day(day) or []
    except Exception:
        return []


def _load_json(rel: str):
    try:
        return json.loads((REPO / rel).read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_premarket(day: str, days: int = 5) -> list:
    """最近 N 天的盘前评论。data/benzinga/*.json 里是**累积全量**聊天(134 万字),
    premarket.recent 按日期标记切出最近几天 —— 同一个人连续写的判断, 前后连起来
    能看出他的看法有没有变, 比单日快照有信息量。"""
    import glob
    try:
        files = sorted(glob.glob(str(REPO / "data" / "benzinga" / "20*.json")))
        for f in reversed(files):          # 从最新的快照里取(旧的内容是子集)
            try:
                d = json.loads(Path(f).read_text(encoding="utf-8"))
            except Exception:
                continue
            txt = ((d.get("channels") or {}).get("chris-market-commentary") or {}).get("text") or ""
            if txt:
                return _pm.recent(txt, days=days, today=day)
    except Exception:
        pass
    return []


def _load_technicals(day: str) -> dict:
    """{ticker: read} —— 只收新鲜的(daily_ohlc 只对 universe 每日更新, 别的会陈旧)。"""
    out = {}
    try:
        import pandas as pd
        df = pd.read_parquet(REPO / "data" / "daily_ohlc.parquet")
    except Exception:
        return out
    for tk in TECH_TICKERS:
        try:
            d = df[df.ticker == tk].sort_index()
            if d.empty:
                continue
            last_date = str(d.index[-1])[:10]
            r = _tech.read(list(d["Close"]), today=day, last_date=last_date)
            if r:
                out[tk] = r
        except Exception:
            continue
    return out


# ── 组装 + 调用 ───────────────────────────────────────────────────

def build_evidence(day: str = None) -> dict:
    """收齐证据 → {context_text, evidence_rows, tech, picked, sources}。不调 LLM。"""
    day = day or _today()
    etf = _load_json("data/heatmap/etf_trends.json")
    cal = _load_json("trade_journal/data/calendar_cache.json")
    summary, summary_day = _load_digest_summary(day)
    items = _load_digest_items(day)
    picked = _articles.pick(items, limit=12)
    tech = _load_technicals(day)
    pmk = _load_premarket(day)

    ctx = _context.build_context(day, digest_summary=summary, etf_trends=etf, cal=cal,
                                 summary_day=summary_day)

    if tech:
        lines = [_tech.describe(tk, r) for tk, r in tech.items() if r]
        reg = _tech.regime(tech)
        block = "\n【技术面位置(距 52 周高点 / 相对均线 —— 判断「现在处在什么位置」最硬的证据)】\n"
        block += "\n".join("- " + x for x in lines if x)
        if reg.get("note"):
            block += "\n综合: " + reg["note"]
        ctx += "\n" + block

    pm_txt = _pm.render(pmk)
    if pm_txt:
        ctx += ("\n\n【最近一周盘前评论(同一分析师连续记录 —— 注意他的判断有没有变)】\n"
                + pm_txt)

    # 🎲 板块期权情绪 —— 这是证据链里唯一直接度量「已经定价了多少」的一块。
    # 新闻告诉你坏消息有多坏, IV 百分位告诉你市场已经把多少坏消息算进价格了;
    # 两者错位的地方才是机会(或风险)。读缓存, 不重算, 拿不到就不写(宁缺勿编)。
    try:
        import options_pulse_store as _ops
        import options_pulse as _op
        op_txt = _op.render_for_brief(_ops.get())
        if op_txt:
            ctx += ("\n\n【板块期权情绪(IV / 偏斜的自身历史百分位 —— 「已反映了多少」)】\n"
                    + op_txt)
    except Exception:
        pass

    art_txt = _articles.render(picked)
    if art_txt:
        ctx += ("\n\n【当日有解读的文章正文(已按「是否含机制分析」筛选)】\n"
                "用这些来校准「坏消息到底有多坏」——注意区分一次性事件与结构性变化。\n\n"
                + art_txt)

    return {
        "date": day,
        "context": ctx,
        "evidence": _context.evidence_rows(day, etf_trends=etf, cal=cal),
        "tech": tech,
        "tech_regime": _tech.regime(tech) if tech else {},
        "sources": [{"title": a["title"], "channels": a["channels"],
                     "tickers": a["tickers"], "hhmm": a["hhmm"]} for a in picked],
        "has_summary": bool(summary),
        "summary_day": summary_day,
        "n_articles": len(picked),
        "premarket_days": [b["date"] for b in pmk],
    }


AUDIT_MIN_SCORE = 85      # 低于此分自动重试一次(把具体问题喂回给模型)
# 这两项是**情绪偏差本身**, 不该被总分掩盖 —— 命中就必重试, 不看分数。
# (实测: 总分 86 但仍用了「暴跌/重挫/飙升」, 而作者明确要「尽量减少情绪偏差」)
HARD_GATES = ("emotion", "labels")


def _retry_note(a: dict) -> str:
    """把审计发现的问题转成给模型的具体订正要求。"""
    bad = {c["key"]: c for c in (a.get("checks") or []) if not c["ok"]}
    if not bad:
        return ""
    lines = ["", "", "═══ 上一版输出有以下问题, 请重新生成并修正 ═══"]
    if "emotion" in bad:
        lines.append(f"· 用了情绪词({bad['emotion']['detail']})。全部换成具体百分比或中性描述。")
    if "labels" in bad:
        lines.append(f"· 出现了方向标签({bad['labels']['detail']})。删掉, 只给概率。")
    if "mechanism" in bad:
        lines.append(f"· {bad['mechanism']['detail']} —— 每个板块的 why 必须说清机制, 写不出就别列这个板块。")
    if "falsify" in bad:
        lines.append(f"· {bad['falsify']['detail']} —— 每个板块必须有可观察的证伪条件。")
    if "probs" in bad:
        lines.append(f"· 概率有问题: {bad['probs']['detail']}。每组必须归一且不取 0/1。")
    if "facts" in bad:
        lines.append(f"· 事实密度不足({bad['facts']['detail']})。每段判断都要带具体数字"
                     "(百分比/价位/日期/均线位置), 没有数字支撑的话删掉。")
    if "base_rate" in bad:
        lines.append(f"· {bad['base_rate']['detail']} —— 要么给出系统性证据, 要么把概率调回基准附近。")
    return "\n".join(lines)


def run(day: str = None, model: str = _brief.MODEL, retry: bool = True) -> dict:
    """跑一次完整分析 → 可直接落盘/下发的 dict。

    审计不达标会自动重试一次(把问题喂回去) —— 作者要「尽量减少情绪偏差」,
    光报告问题不够, 得能自我纠正。重试最多 1 次, 免得无限烧 token。
    """
    ev = build_evidence(day)
    out, used_model, err, usage = _brief.analyze(ev["context"], model=model)
    n_calls = 1
    if not err and retry:
        a0 = _audit.audit(out)
        _failed = {c["key"] for c in (a0.get("checks") or []) if not c["ok"]}
        if a0.get("score", 0) < AUDIT_MIN_SCORE or (_failed & set(HARD_GATES)):
            note = _retry_note(a0)
            if note:
                out2, _m2, err2, u2 = _brief.analyze(ev["context"] + note, model=model)
                n_calls = 2
                if not err2:
                    a1 = _audit.audit(out2)
                    # 采用条件: 总分变高, 或硬门槛(情绪词/方向标签)被清掉
                    f1 = {c["key"] for c in (a1.get("checks") or []) if not c["ok"]}
                    hard0, hard1 = _failed & set(HARD_GATES), f1 & set(HARD_GATES)
                    if a1.get("score", 0) > a0.get("score", 0) or len(hard1) < len(hard0):
                        out = out2
                    for k in ("in", "out", "cache_write", "cache_read"):
                        if isinstance(usage.get(k), int) and isinstance(u2.get(k), int):
                            usage[k] = usage[k] + u2[k]
                    if isinstance(usage.get("cost_usd"), (int, float)) and isinstance(u2.get("cost_usd"), (int, float)):
                        usage["cost_usd"] = usage["cost_usd"] + u2["cost_usd"]
                    usage["retried"] = True
                    usage["score_before"] = a0.get("score")
    res = {
        "date": ev["date"],
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "model": used_model,
        "evidence": ev["evidence"],
        "tech": ev["tech"],
        "tech_regime": ev["tech_regime"],
        "sources": ev["sources"],
        "n_articles": ev["n_articles"],
        "has_summary": ev["has_summary"],
        "summary_day": ev["summary_day"],
        "usage": usage,
        "n_calls": n_calls,
        "context_chars": len(ev["context"]),
        "premarket_days": ev["premarket_days"],
    }
    if err:
        res["error"] = err
    else:
        res.update(out)
        res["audit"] = _audit.audit(out)   # 偏差审计: 纪律可强制, 不只写在 prompt 里
    return res


# ── 缓存(按天, 同 ai_summary 的口径) ──────────────────────────────

def cache_path(day: str) -> Path:
    return CACHE_DIR / f"{day}.json"


def load(day: str = None):
    try:
        return json.loads(cache_path(day or _today()).read_text(encoding="utf-8"))
    except Exception:
        return None


def save(res: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = cache_path(res.get("date") or _today())
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        print(f"[market_brief] save err: {e}")


def get(day: str = None, force: bool = False, model: str = _brief.MODEL) -> dict:
    """有缓存直接返回; 否则跑一次并落盘。**只有成功才落盘**(失败落盘会把错误钉住一天)。"""
    day = day or _today()
    if not force:
        c = load(day)
        if c and not c.get("error"):
            return c
    res = run(day, model=model)
    if not res.get("error"):
        save(res)
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--evidence-only", action="store_true", help="只打印证据, 不调模型")
    a = ap.parse_args()
    if a.evidence_only:
        ev = build_evidence(a.date)
        print(ev["context"])
        print(f"\n--- {len(ev['context'])} 字符, {ev['n_articles']} 篇文章, "
              f"digest 总结={'有' if ev['has_summary'] else '无'} ---", file=sys.stderr)
    else:
        r = get(a.date, force=a.force)
        print(json.dumps(r, ensure_ascii=False, indent=2))

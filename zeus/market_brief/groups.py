"""细分行业分组的短/中/长期趋势 —— 纯函数, 不碰磁盘/网络。

内置一份 81 组的细分分类(芯片代工/光模块/网络安全/大型制药…),
比现成的 25 个板块 ETF 细得多, 用来看**行业轮动**。

几个刻意的取舍:

1. **组收益用中位数, 不用均值。** 一只翻倍的小盘生技能把整组均值拉飞, 但那不代表
   「这个行业在涨」。中位数回答的是「这组里典型的那只票怎么样」—— 这才是轮动想问的。
   均值也一起给, 两者差很大本身就是信号(说明组内分化, 涨的是少数)。

2. **同时给组内广度**(多少成员在 MA50/MA200 上方)。收益是结果, 广度是结构 ——
   「组收益 +5% 但只有 30% 成员在均线上」= 少数几只扛着, 和「+5% 且 80% 在均线上」
   是完全不同的两件事。

3. **短/中/长分开, 不合成单一评分。** 合成会把「长期强+短期弱」和「长期弱+短期强」
   压成同一个数, 而这两者恰恰是最需要区分的(前者是回调, 后者是反弹未确认)。
"""
from __future__ import annotations

import statistics

# 交易日口径的窗口。短/中/长按作者说法分三档。
WINDOWS = [
    ("d5", 5, "短"),
    ("m1", 21, "短"),
    ("m3", 63, "中"),
    ("m6", 126, "长"),
    ("y1", 252, "长"),
]
HORIZON_KEY = {"短": ("d5", "m1"), "中": ("m3",), "长": ("m6", "y1")}


def parse_universe(text: str) -> dict:
    """`###组名,EXCHANGE:TICKER,...` → {组名: [ticker]}(保序, 去重)。"""
    groups, cur = {}, None
    for tok in (text or "").replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("###"):
            cur = tok[3:].strip()
            if cur and cur not in groups:
                groups[cur] = []
        elif cur:
            sym = tok.split(":")[-1].strip().upper().replace(".", "-")
            if sym and sym not in groups[cur]:
                groups[cur].append(sym)
    return {g: v for g, v in groups.items() if v}


def ret(closes, n: int):
    """n 个交易日的收益%。数据不够 → None(不外推)。"""
    xs = [c for c in (closes or []) if isinstance(c, (int, float)) and c > 0]
    if len(xs) < n + 1:
        return None
    return round((xs[-1] / xs[-1 - n] - 1.0) * 100.0, 2)


def ticker_stats(closes) -> dict | None:
    """单票的多窗口收益 + 均线位置。数据太短 → None。"""
    xs = [c for c in (closes or []) if isinstance(c, (int, float)) and c > 0]
    # 25 根足够算 1 个月(21日)收益。新上市票(CBRS 49根 / QNT 35 / SPCX 29)
    # 让它们参与短期读数, 长期窗口由 ret() 自动返回 None, 不外推。
    if len(xs) < 25:
        return None
    last = xs[-1]
    # ⚠️ 不足 50 根时**不要**拿 25 根的均值冒充 MA50 —— 那是另一个指标。
    ma50 = (sum(xs[-50:]) / 50) if len(xs) >= 50 else None
    ma200 = (sum(xs[-200:]) / 200) if len(xs) >= 200 else None
    out = {k: ret(xs, n) for k, n, _ in WINDOWS}
    out["above_ma50"] = (last >= ma50) if ma50 else None
    out["above_ma200"] = (last >= ma200) if ma200 else None
    out["n_bars"] = len(xs)
    return out


def _med(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(statistics.median(v), 2) if v else None


def _mean(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(sum(v) / len(v), 2) if v else None


def group_stats(name: str, members: dict) -> dict | None:
    """members = {ticker: ticker_stats}。→ 组级中位/均值收益 + 广度。

    要求至少 2 只有数据 —— 单只票的「行业趋势」没有意义。
    """
    rows = [s for s in (members or {}).values() if s]
    if len(rows) < 2:
        return None
    out = {"name": name, "n": len(rows), "n_listed": len(members or {})}
    for k, _n, _h in WINDOWS:
        vals = [r.get(k) for r in rows]
        out[k] = _med(vals)
        out[k + "_mean"] = _mean(vals)
    n50 = [r for r in rows if r.get("above_ma50") is not None]
    n200 = [r for r in rows if r.get("above_ma200") is not None]
    out["pct_above_ma50"] = (round(100 * sum(1 for r in n50 if r["above_ma50"]) / len(n50))
                             if n50 else None)
    out["pct_above_ma200"] = (round(100 * sum(1 for r in n200 if r["above_ma200"]) / len(n200))
                              if n200 else None)
    # 中位 vs 均值差得多 = 组内分化(涨的是少数)
    m3, m3a = out.get("m3"), out.get("m3_mean")
    out["skewed"] = bool(m3 is not None and m3a is not None and abs(m3a - m3) >= 5)
    return out


def classify(g) -> dict:
    """短(1个月) × 中(3个月) 四象限 —— 与 heatmap 板块分类同口径, 便于对照。"""
    s, m = g.get("m1"), g.get("m3")
    if not isinstance(s, (int, float)) or not isinstance(m, (int, float)):
        return {"state": "数据不足", "grade": "", "note": ""}
    if m >= 0 and s >= 0:
        state, note = "趋势向上", "中短周期同向走强"
    elif m >= 0 and s < 0:
        state, note = "高位回调", "中期仍强但短期回落"
    elif m < 0 and s >= 0:
        state, note = "超跌反弹", "中期仍弱, 短期在修复 —— 未确认转势"
    else:
        state, note = "趋势向下", "中短周期同向走弱"
    strength = max(abs(s), abs(m))
    grade = "强" if strength >= 10 else ("中" if strength >= 4 else "弱")
    return {"state": state, "grade": grade, "note": note}


def stale_tickers(last_dates: dict, max_lag_days: int = 5) -> set:
    """找出最后一根 K 线明显落后于全体的票。

    ⚠️ 这是本模块最容易出事的地方: 如果一部分票的数据停在一个月前, 它们的
    「3 个月收益」实际是「截至一个月前的 3 个月」—— 与新鲜票**不是同一段时间**,
    横向排名直接失效。实测踩过: CNC/HUM 停在 6-29 而 UNH 到 7-23, 医保组因此
    显示 +101.7% 的假领先。所以宁可剔除, 不要静默混算。
    """
    import datetime as _dt
    # ⚠️ 只在**能解析的**日期里取最新 —— 直接 max() 是字符串比较,
    #    "nope" > "2026-07-24" 会让烂数据变成「最新日期」, 整个守卫静默失效。
    parsed = []
    for d in (last_dates or {}).values():
        try:
            parsed.append(_dt.date.fromisoformat(str(d)[:10]))
        except Exception:
            continue
    if not parsed:
        return set()
    nd = max(parsed)
    out = set()
    for t, d in (last_dates or {}).items():
        try:
            if (nd - _dt.date.fromisoformat(str(d)[:10])).days > max_lag_days:
                out.add(t)
        except Exception:
            out.add(t)
    return out


def build(universe_text: str, closes_by_ticker: dict, last_dates: dict = None,
          max_lag_days: int = 5) -> list:
    """→ [组级统计], 按中期(3个月)中位收益降序。

    closes_by_ticker = {ticker: [收盘价...]}(升序)
    last_dates       = {ticker: 'YYYY-MM-DD'} —— 传了就剔除陈旧票(见 stale_tickers)
    """
    groups = parse_universe(universe_text)
    stale = stale_tickers(last_dates, max_lag_days) if last_dates else set()
    out = []
    for name, tickers in groups.items():
        members = {}
        for t in tickers:
            if t in stale:
                continue
            st = ticker_stats(closes_by_ticker.get(t))
            if st:
                members[t] = st
        g = group_stats(name, members)
        if not g:
            continue
        g.update(classify(g))
        # 组内最强/最弱(按中期), 供点开看细节
        ranked = sorted(((t, s.get("m3")) for t, s in members.items()
                         if isinstance(s.get("m3"), (int, float))),
                        key=lambda p: -p[1])
        g["top"] = [{"t": t, "r": r} for t, r in ranked[:3]]
        g["bottom"] = [{"t": t, "r": r} for t, r in ranked[-3:]][::-1]
        # 全部成员(按中期强弱排) —— 前端悬停时显示"这个组都有谁"
        g["members"] = [{"t": t, "r": s.get("m3"), "r1": s.get("m1")}
                        for t, s in sorted(members.items(),
                                           key=lambda p: -(p[1].get("m3") if isinstance(p[1].get("m3"), (int, float)) else -999))]
        g["missing"] = [t for t in tickers if t not in members]
        g["stale_excluded"] = [t for t in tickers if t in stale]
        out.append(g)
    out.sort(key=lambda g: (g.get("m3") is None, -(g.get("m3") or 0)))
    return out


def horizon_view(rows, horizon: str = "中") -> list:
    """按短/中/长重排 —— 作者要「short/medium/long 的趋势」。

    短期用 1 个月(5 日噪音太大, 只作参考列), 长期用 6 个月。
    """
    keys = HORIZON_KEY.get(horizon) or ("m3",)
    k = keys[-1] if horizon == "短" else keys[0]
    return sorted(rows, key=lambda g: (g.get(k) is None, -(g.get(k) or 0)))


def rotation(rows, top: int = 8) -> dict:
    """轮动摘要: 中期领先/落后, 以及**短中背离**最大的组(资金正在换手的地方)。"""
    ok = [g for g in rows if isinstance(g.get("m1"), (int, float))
          and isinstance(g.get("m3"), (int, float))]
    if not ok:
        return {}
    by_m3 = sorted(ok, key=lambda g: -g["m3"])
    # 短期减中期: 正 = 短期在补涨(可能刚启动); 负 = 短期在退潮(可能见顶)
    for g in ok:
        g["shift"] = round(g["m1"] - g["m3"] / 3.0, 2)   # m3 折成月度可比
    by_shift = sorted(ok, key=lambda g: -g["shift"])
    return {
        "leaders": [g["name"] for g in by_m3[:top]],
        "laggards": [g["name"] for g in by_m3[-top:]][::-1],
        "warming": [g["name"] for g in by_shift[:5]],     # 短期强于中期趋势 = 在升温
        "cooling": [g["name"] for g in by_shift[-5:]][::-1],
    }

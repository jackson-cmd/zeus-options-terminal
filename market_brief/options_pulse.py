"""板块级期权情绪 —— 纯函数, 不碰磁盘/网络。

用 EOD 期权链(eod_crawler/data_eod/eod_chain.parquet, 440 只 × 每日全链)算出
「这个板块的期权市场今天在定价什么」, 喂给 🧭 市场判断。

几个刻意的取舍, 每条都是为了不让这个指标变成又一个情绪放大器:

1. **只报相对自身历史的百分位, 不报绝对值。**
   P/C = 0.64 单独看什么也说明不了 —— 有的票常年 0.5, 有的常年 1.5。
   唯一可读的问法是「今天在它自己的历史里排第几」。绝对值一并给出, 但结论
   只由百分位驱动。

2. **IV 百分位就是「已反映了多少」。** 这是本模块对「消息坏度 vs 价格已反映」
   最直接的贡献: 坏消息 + IV 低百分位 = 市场还没定价(风险在你这边);
   坏消息 + IV 高百分位 = 已经定价了(这时候追空往往是接盘)。

   (财报周/被收购)能把整组均值拉飞, 但那不代表「这个行业在被对冲」。

4. **不给方向标签。** 高 put 量既可能是看空、也可能是持股方买保险、还可能是
   卖 put 收权利金 —— 单看成交量分不出来。所以本模块只陈述「贵不贵/偏不偏/
   仓位在哪边」, 方向判断留给上层。

5. **DTE 窗口锁在 20-60 天。** 0DTE 的量是赌博噪音, LEAPS 不反映当下情绪;
   20-60 天覆盖常见的价差交易区间(35-45 DTE), 情绪读数要和交易周期对齐。
"""
from __future__ import annotations

import statistics

DTE_LO, DTE_HI = 20, 60
# 25-delta 是业界看偏斜的标准位置(比"离 spot 5%"这种行权价近似稳健得多,
# 因为它自动随波动率调整距离)
SKEW_DELTA = 0.25
# 取最接近 25Δ 的 N 个合约取中位数, 避免单个烂报价决定整条偏斜
SKEW_N = 3
# ⚠️ 但必须先卡在 25Δ 附近的带内。只按"离 0.25 最近"取 N 个的话, 链子稀疏时
#    会把 50Δ(平值)甚至 5Δ(远期彩票)拉进来充数 —— 那算出来的就不是 25Δ 偏斜了,
#    而是"平值 put 减平值 call", 恒接近 0, 偏斜信号被稀释没。宁可返回 None。
SKEW_BAND = (0.15, 0.35)


def _f(x):
    """→ float 或 None。链里有 NaN/字符串/None, 一律挡在这。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v          # NaN


def _pos(x):
    v = _f(x)
    return v if (v is not None and v > 0) else None


def _median(vals):
    v = [x for x in vals if isinstance(x, (int, float))]
    return round(statistics.median(v), 6) if v else None


def ticker_metrics(rows) -> dict | None:
    """单票单日的期权情绪指标。

    rows = [{PutCall, StrikePrice, Volume, OpenInterest, UnderlyingPrice,
             ImpliedVolatility, Delta, dte}, ...]  —— 已是**同一票同一天**的全链。

    → {pc_vol, pc_oi, atm_iv, skew25, call_vol, put_vol, call_oi, put_oi, n}
      数据不足 → None(不猜)
    """
    win = [r for r in (rows or [])
           if (_f(r.get("dte")) is not None and DTE_LO <= _f(r.get("dte")) <= DTE_HI)]
    if not win:
        return None

    calls = [r for r in win if str(r.get("PutCall", "")).lower().startswith("c")]
    puts = [r for r in win if str(r.get("PutCall", "")).lower().startswith("p")]
    if not calls or not puts:
        return None

    def _sum(rs, key):
        return sum(_f(r.get(key)) or 0.0 for r in rs)

    cv, pv = _sum(calls, "Volume"), _sum(puts, "Volume")
    co, po = _sum(calls, "OpenInterest"), _sum(puts, "OpenInterest")

    out = {
        "call_vol": cv, "put_vol": pv, "call_oi": co, "put_oi": po,
        "n": len(win),
        # ⚠️ 分母为 0 时返回 None 而不是塞个大数 —— 0 成交量的票不该被当成
        #    "P/C 无穷大 = 极度看空", 那是没数据, 不是信号。
        "pc_vol": round(pv / cv, 4) if cv > 0 else None,
        "pc_oi": round(po / co, 4) if co > 0 else None,
    }

    # ATM IV: 取最接近 spot 的若干合约的 IV 中位数
    spot = _pos(win[0].get("UnderlyingPrice"))
    if spot:
        near = sorted(
            (r for r in win if _pos(r.get("ImpliedVolatility")) and _pos(r.get("StrikePrice"))),
            key=lambda r: abs(_f(r["StrikePrice"]) - spot))[:8]
        out["atm_iv"] = _median([_f(r["ImpliedVolatility"]) for r in near])
    else:
        out["atm_iv"] = None

    # 25Δ 偏斜 = IV(25Δ put) − IV(25Δ call)。正 = 下跌保护更贵(避险需求)。
    def _at_delta(rs, want_sign):
        lo, hi = SKEW_BAND
        cand = []
        for r in rs:
            d, iv = _f(r.get("Delta")), _pos(r.get("ImpliedVolatility"))
            if d is None or iv is None:
                continue
            if want_sign > 0 and d <= 0:
                continue
            if want_sign < 0 and d >= 0:
                continue
            ad = abs(d)
            if not (lo <= ad <= hi):      # 带外的一律不参与(见 SKEW_BAND 注释)
                continue
            cand.append((abs(ad - SKEW_DELTA), iv))
        if not cand:
            return None
        cand.sort(key=lambda t: t[0])
        return _median([iv for _, iv in cand[:SKEW_N]])

    p25, c25 = _at_delta(puts, -1), _at_delta(calls, +1)
    out["iv_put25"], out["iv_call25"] = p25, c25
    # 换算成 IV 百分点(0.4532-0.4148 → +3.84), 比小数好读
    out["skew25"] = round((p25 - c25) * 100.0, 3) if (p25 is not None and c25 is not None) else None
    return out


def pct_rank(history, value) -> int | None:
    """value 在 history 里的百分位(0-100)。history 不含今天也没关系。

    ⚠️ 用「≤ 的占比」而不是 scipy 那套插值 —— 样本才 ~59 天, 插值的精度是假的。
    少于 20 个样本直接返回 None: 拿 5 天历史算出来的"第 80 百分位"没有意义,
    宁可显示「样本不足」也不要给一个看着很确定的数。
    """
    if value is None:
        return None
    xs = [x for x in (history or []) if isinstance(x, (int, float))]
    if len(xs) < 20:
        return None
    return int(round(100.0 * sum(1 for x in xs if x <= value) / len(xs)))


def _breadth(vals, pred):
    v = [x for x in vals if x is not None]
    return round(100.0 * sum(1 for x in v if pred(x)) / len(v)) if v else None


# 覆盖率门槛: 低于这些就标 low_coverage, 不参与"最紧张/最松弛"排行。
# ⚠️ 这条守卫是实测逼出来的: 分类里「保险」有 7 只(BRK-B/PGR/AIG/AFL/UNM/
#    LMND/OSCR), 但期权池只覆盖到 LMND+OSCR 两只小盘保险科技 —— 它俩 IV 84%/96%,
#    中位数 90% 就被当成「保险板块 IV 90%」顶上了榜首。真正的大保险公司 IV 只有
#    20-30%。**不是数据脏, 是用两只微盘代表了一个板块** —— 与 groups.py 里
#    医保 +101.7% 是同一类错。所以覆盖不足的组照样算, 但要标出来 + 不上榜。
MIN_MEMBERS = 3
MIN_COVERAGE = 0.5


def group_pulse(name: str, members: dict, history: dict = None,
                n_listed: int = None) -> dict | None:
    """members = {ticker: ticker_metrics(今天)}  —— 只含有数据的票
       n_listed = 该组在分类表里的**全部**票数(用来算覆盖率; 不传就退化成 len(members))
       history  = {ticker: {metric: [历史值...]}}  —— 传了才有百分位

    → 组级中位数 + 百分位 + 组内广度。至少 2 只有数据(单票不叫"板块")。
    """
    rows = {t: m for t, m in (members or {}).items() if m}
    if len(rows) < 2:
        return None
    listed = int(n_listed) if n_listed else len(members or {})
    cov = (len(rows) / listed) if listed else 0.0
    out = {"name": name, "n": len(rows), "n_listed": listed,
           "coverage": round(cov, 3),
           "low_coverage": bool(len(rows) < MIN_MEMBERS or cov < MIN_COVERAGE)}

    for k in ("pc_vol", "pc_oi", "atm_iv", "skew25"):
        out[k] = _median([m.get(k) for m in rows.values()])

    # 组级百分位 = 成员各自百分位的中位数。
    # ⚠️ 不能先把组内中位数拼成一条"组的历史序列"再排名 —— 成员进出会让那条
    #    序列前后不同源。逐票在自己历史里排名再取中位, 每个数都是同源比较。
    if history:
        for k in ("pc_vol", "pc_oi", "atm_iv", "skew25"):
            pr = [pct_rank((history.get(t) or {}).get(k), m.get(k))
                  for t, m in rows.items()]
            pr = [x for x in pr if x is not None]
            out[k + "_pctl"] = int(round(statistics.median(pr))) if pr else None
            out[k + "_n_ranked"] = len(pr)

    # 广度: 多少成员的 IV / 偏斜处在自身高位
    if history:
        ivp = [pct_rank((history.get(t) or {}).get("atm_iv"), m.get("atm_iv"))
               for t, m in rows.items()]
        skp = [pct_rank((history.get(t) or {}).get("skew25"), m.get("skew25"))
               for t, m in rows.items()]
        out["pct_iv_high"] = _breadth(ivp, lambda x: x >= 80)
        out["pct_skew_high"] = _breadth(skp, lambda x: x >= 80)
    return out


def read_group(g: dict) -> dict:
    """把百分位翻成一句**机械的**中文陈述 —— 只说"贵不贵/偏不偏", 不给方向。

    刻意不写"看涨/看跌": 高 put 量既可能是看空、也可能是持股买保险、还可能是
    卖 put 收钱, 成交量本身分不出来。方向判断是上层(和人)的事。
    """
    # 没数据就明说"样本不足", 不要返回空串 —— 空串在界面上看起来像"没问题",
    # 而实际是"没算出来", 这两件事必须能分辨。
    g = g or {}
    ivp, skp = g.get("atm_iv_pctl"), g.get("skew25_pctl")
    pvp, pop = g.get("pc_vol_pctl"), g.get("pc_oi_pctl")

    def band(p, hi, mid, lo, na="样本不足"):
        if p is None:
            return na
        return hi if p >= 80 else (mid if p >= 20 else lo)

    iv = band(ivp, "期权贵(IV 在自身高位)", "IV 在常态区间", "期权便宜(IV 在自身低位)")
    skew = band(skp, "下跌保护被抢(偏斜高位)", "偏斜常态", "下跌保护便宜(偏斜低位)")
    flow = band(pvp, "put 成交异常多", "成交结构常态", "call 成交异常多")
    oi = band(pop, "put 持仓偏重", "持仓结构常态", "call 持仓偏重")

    # 「已反映了多少」—— 本模块对市场判断最有用的一句
    if ivp is None:
        priced = "样本不足, 无法判断已反映程度"
    elif ivp >= 80:
        priced = "坏消息大概率已被定价(IV 已在高位, 这时追空常是接盘)"
    elif ivp <= 20:
        priced = "市场尚未定价风险(IV 在低位, 真出事的话下跌空间没被保护)"
    else:
        priced = "定价处于常态, 无明显低估或高估"
    return {"iv": iv, "skew": skew, "flow": flow, "oi": oi, "priced_in": priced}


def rank_groups(rows, key: str = "atm_iv_pctl", top: int = 8) -> dict:
    """按某个百分位排前/后 N —— 给市场判断挑"最该看的几个板块"。

    ⚠️ 覆盖不足的组**不上榜**: 榜单是最容易被当成结论直接用的东西, 让一个
    「7 只票里只有 2 只小盘」的组顶在第一位, 比不显示它危害大得多。
    它仍然在完整列表里(带 low_coverage 标记), 只是不参与排名。
    """
    ok = [g for g in (rows or [])
          if isinstance(g.get(key), (int, float)) and not g.get("low_coverage")]
    if not ok:
        return {"high": [], "low": []}
    s = sorted(ok, key=lambda g: -g[key])
    return {
        "high": [{"name": g["name"], "v": g[key]} for g in s[:top]],
        "low": [{"name": g["name"], "v": g[key]} for g in s[-top:]][::-1],
    }


def render_for_brief(res: dict, top: int = 6) -> str:
    """喂给市场判断模型的一段文本。

    只给百分位和覆盖率, **不给绝对 IV** —— 模型看到「保险 IV 90%」会当成
    极端恐慌来推理, 而那其实只是两只小盘保险科技的常态。百分位自带"相对自己"
    的语义, 不需要模型再去脑补基准。
    """
    if not res or res.get("error"):
        return ""
    hi = (res.get("rank_iv") or {}).get("high") or []
    lo = (res.get("rank_iv") or {}).get("low") or []
    sk = (res.get("rank_skew") or {}).get("high") or []
    if not hi and not lo:
        return ""
    by = {g["name"]: g for g in (res.get("rows") or [])}

    def line(name):
        g = by.get(name) or {}
        return (f"- {name}: IV 百分位 {g.get('atm_iv_pctl')}, "
                f"25Δ 偏斜百分位 {g.get('skew25_pctl')} "
                f"(覆盖 {g.get('n')}/{g.get('n_listed')})")

    out = [f"截至 {res.get('asof','?')}, 基于 EOD 期权链 20-60 DTE, "
           f"{res.get('n_tickers','?')} 只 / 历史中位 {res.get('hist_days','?')} 个交易日。",
           "百分位 = 该板块成员各自在**自己历史**里的位置取中位数(0-100)。"
           "绝对 IV 在板块之间不可比, 故不提供。",
           "",
           "IV 百分位最高(坏消息已被定价得最多 —— 这里追空往往是接盘):"]
    out += [line(x["name"]) for x in hi[:top]]
    out += ["", "IV 百分位最低(风险尚未被定价 —— 真出事的话这里没有保护):"]
    out += [line(x["name"]) for x in lo[:top]]
    if sk:
        out += ["", "下跌保护需求最强(25Δ 偏斜处在自身高位, 有人在认真买保险):"]
        out += [f"- {x['name']}: 偏斜百分位 {x['v']}" for x in sk[:4]]
    out += ["",
            "⚠️ 读法: 高 put 成交量本身**不等于**看空 —— 可能是持股方买保险, 也可能是"
            "卖 put 收权利金, 成交量分不出方向。只把这些当作「已经定价了多少」的度量, "
            "不要当作方向信号。低覆盖的板块(成员大多不在期权池里)已从榜单剔除。"]
    return "\n".join(out)


def stale_tickers(last_dates: dict, max_lag_days: int = 3) -> set:
    """数据停更的票 —— 与 groups.py 同一个教训。

    ⚠️ 期权情绪是**横向比较**(这个板块 vs 那个板块), 全体必须对齐到同一天。
    某些票停在一周前, 它们的"今天 IV"其实是上周的 IV, 混进来整个排名就废了。
    只在**能解析的**日期里取最新 —— 直接 max() 是字符串比较, 烂数据会变成
    "最新日期", 守卫静默失效(groups.py 踩过)。
    """
    import datetime as _dt
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


def build(groups: dict, today: dict, history: dict = None,
          last_dates: dict = None, max_lag_days: int = 3) -> list:
    """groups = {组名: [ticker]}, today = {ticker: ticker_metrics}, →[组级]

    按 IV 百分位降序(最贵的排前面 —— 那是市场最紧张的地方)。
    """
    stale = stale_tickers(last_dates, max_lag_days) if last_dates else set()
    out = []
    for name, tickers in (groups or {}).items():
        members = {t: today.get(t) for t in tickers if t not in stale and today.get(t)}
        # ⚠️ n_listed 必须传**分类表里的全部票数**, 不能让 group_pulse 拿
        #    len(members) 兜底 —— members 已经滤掉没数据的票, 那样覆盖率恒 100%,
        #    守卫直接失效(第一版就是这么写的, 保险 2/7 显示成 2/2)。
        g = group_pulse(name, members, history, n_listed=len(tickers))
        if not g:
            continue
        g.update(read_group(g))
        g["members"] = sorted(
            [{"t": t, "iv": m.get("atm_iv"), "skew": m.get("skew25"),
              "pcv": m.get("pc_vol"), "pcoi": m.get("pc_oi")}
             for t, m in members.items()],
            key=lambda r: -(r["iv"] if isinstance(r["iv"], (int, float)) else -1))
        g["missing"] = [t for t in tickers if t not in members]
        g["stale_excluded"] = [t for t in tickers if t in stale]
        out.append(g)
    out.sort(key=lambda g: (g.get("atm_iv_pctl") is None, -(g.get("atm_iv_pctl") or 0)))
    return out

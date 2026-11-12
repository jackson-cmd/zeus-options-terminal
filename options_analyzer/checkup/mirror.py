"""错误镜子——开单前把作者反复犯的错怼到脸上(覆盖全谱,分自动检测 + 行为自检两层)。

每条旗标附 journal 实证(频次/累计亏损,来自 mistake_rules.json)。纯函数:吃
已经算好的 price/corr/trend 上下文,出旗标列表。检测口径见 spec §3.2 / §1。
"""
from __future__ import annotations

from .rules import THRESHOLDS as T


def _flag(fid, severity, icon, label, detail, hist=None):
    return {"id": fid, "severity": severity, "icon": icon, "label": label,
            "detail": detail, "hist": hist}


def _ticker_hist(mrules, ticker):
    return (mrules.get("by_ticker") or {}).get((ticker or "").upper())


def _mistake_hist(mrules, key):
    return (mrules.get("by_mistake") or {}).get(key)


def _hist_text(h):
    if not h:
        return None
    n = h.get("n")
    pnl = h.get("net_pnl")
    wr = h.get("win_rate")
    bits = []
    if n is not None and pnl is not None:
        bits.append(f"历史 {n} 单净{'亏' if pnl < 0 else '赚'} ${abs(pnl):,.0f}")
    if wr is not None:
        bits.append(f"胜率 {wr * 100:.0f}%")
    return " · ".join(bits) if bits else None


def evaluate(proposed, book, corr, price, trend_ob, mrules, nlv, candidates=None) -> list:
    """返回旗标列表。proposed: {ticker,direction,thesis_type,dte,earnings_in_window}。"""
    proposed = proposed or {}
    book = book or {}
    corr = corr or {}
    price = price or {}
    trend_ob = trend_ob or {}
    mrules = mrules or {}
    flags = []
    direction = (proposed.get("direction") or "").upper()
    dte = proposed.get("dte")
    ticker = (proposed.get("ticker") or "").upper()

    # ── A. 自动可检测 ──────────────────────────────────────────────
    # 高度联动(和现有持仓基本同一个注)—— pearson≥0.45 或 下尾≥0.40 就算
    hc = corr.get("high_corr") or []
    if hc:
        top = hc[0]
        is_red = (top.get("pearson") or 0) >= 0.7 or (top.get("tail") or 0) >= 0.55
        parts = []
        for p in hc[:4]:
            pe = f"{p['pearson']:.2f}" if p.get("pearson") is not None else "-"
            ta = f",下尾{p['tail']:.2f}" if p.get("tail") is not None else ""
            parts.append(f"{p['candidate']}↔{p['ticker']} {pe}{ta}")
        flags.append(_flag("high_corr", "red" if is_red else "warn", "🛑" if is_red else "⚠️",
                           "和现有持仓高度联动",
                           "、".join(parts) + " —— 基本是同一个注,加它是放大风险、不是分散"))
    # 同行业已重仓(用 industry,比 sector 细——治"8个科技股都算一起"的噪音)
    for sh in (corr.get("industry_hits") or [])[:3]:
        held = sh.get("holdings") or []
        flags.append(_flag("industry", "warn", "🏭", f"{sh['candidate']} 同行业已重仓 · {sh['industry']}",
                           f"你「{sh['industry']}」行业已有 {len(held)} 个仓({'、'.join(held[:6])})—— 同行业再加 = 押同一个细分赛道"))
    # 方向开反
    if price.get("ok") and direction:
        bullish = price.get("bullish")
        want_bull = direction in ("BULL", "BULLISH", "C", "CALL")
        want_bear = direction in ("BEAR", "BEARISH", "P", "PUT")
        if (want_bull and bullish is False) or (want_bear and bullish is True):
            flags.append(_flag("direction", "red", "🛑", "方向开反",
                               f"你说 {direction},但这个价差实际是{'看涨' if bullish else '看跌'}结构——方向反了",
                               _hist_text(_mistake_hist(mrules, "开反了单"))))

    # 财报在到期窗口
    if proposed.get("earnings_in_window"):
        flags.append(_flag("earnings", "red", "🛑", "财报在到期窗口内",
                           "到期前有财报——交易铁律:『永远不要赌财报,IV 已定价,方向赌错 -50% 起步』",
                           _hist_text(_mistake_hist(mrules, "财报前开单"))))

    # 0DTE / 短 DTE
    if dte is not None:
        if dte < T["dte_zero"]:
            flags.append(_flag("zerodte", "red", "🛑", "0DTE 赌博",
                               "交易铁律:『永远不要再碰 0DTE』",
                               _hist_text(_mistake_hist(mrules, "赌博"))))
        elif dte <= T["dte_short_warn"]:
            flags.append(_flag("shortdte", "warn", "⚠️", "短 DTE",
                               f"只剩 {dte} 天——你的『开单时间太短』毛病",
                               _hist_text(_mistake_hist(mrules, "开单时间太短"))))

    # 极度 OTM 彩票单(仅借方/方向性)
    if price.get("ok") and price.get("is_debit") and price.get("otm_sigma") is not None:
        s = price["otm_sigma"]
        if s >= T["otm_sigma_red"]:
            flags.append(_flag("otm", "red", "🛑", "极度OTM彩票单",
                               f"盈亏平衡离现价 {s:.1f}σ——到期前要走 >{s:.1f} 个标准差才回本,数学上就是低概率赌博"))
        elif s >= T["otm_sigma_warn"]:
            flags.append(_flag("otm", "warn", "⚠️", "偏 OTM",
                               f"盈亏平衡离现价 {s:.1f}σ,概率偏低"))

    # 追高 / 涨疯了进去了(看涨进场 + 超买)
    if trend_ob.get("flag") and direction in ("", "BULL", "BULLISH", "C", "CALL"):
        rsi = trend_ob.get("rsi")
        ext = trend_ob.get("ext_sigma")
        bits = []
        if rsi is not None:
            bits.append(f"RSI {rsi:.0f}")
        if ext is not None:
            bits.append(f"离 SMA20 {ext:.1f}σ")
        flags.append(_flag("chase", "warn", "⚠️", "追高 / 涨疯了进去了",
                           "标的已严重超买(" + ", ".join(bits) + ")——你的『涨疯了进去了』毛病",
                           _hist_text(_mistake_hist(mrules, "涨疯了进去了"))))

    # 仓位过大(单笔风险 / NLV)
    if price.get("ok") and price.get("max_loss") and nlv:
        pct = price["max_loss"] / float(nlv) * 100
        if pct > T["size_red_pct"]:
            flags.append(_flag("size", "red", "🛑", "仓位过大",
                               f"单笔最大亏损 ${price['max_loss']:,.0f} = NLV 的 {pct:.1f}%(超你『≤2%』规则一倍以上)"))
        elif pct > T["size_warn_pct"]:
            flags.append(_flag("size", "warn", "⚠️", "仓位偏大",
                               f"单笔最大亏损 = NLV 的 {pct:.1f}%(你的规则是 ≤2%)"))

    # 赔率太差
    if price.get("ok") and price.get("rr") is not None and price["rr"] < T["rr_min"]:
        flags.append(_flag("rr", "warn", "⚠️", "赔率太差",
                           f"R:R 仅 {price['rr']:.2f}(你的规则是 ≥1.5)"))

    # 追价 / 成交差
    if price.get("ok") and price.get("slippage_pct") is not None and price["slippage_pct"] > 0:
        sp = price["slippage_pct"]
        if sp >= T["slippage_red_pct"]:
            flags.append(_flag("slippage", "red", "🛑", "成交价太差",
                               f"你打算付的价比理论中间价贵 {sp:.0f}%——别追价"))
        elif sp >= T["slippage_warn_pct"]:
            flags.append(_flag("slippage", "warn", "⚠️", "成交价偏贵",
                               f"比理论中间价贵 {sp:.0f}%,挂单往中间价靠一靠"))

    # 假对冲
    if (proposed.get("thesis_type") or "").lower() in ("hedge", "对冲") and corr.get("max_pearson") is not None:
        if corr["max_pearson"] >= T["fake_hedge_corr"]:
            flags.append(_flag("fakehedge", "warn", "⚠️", "假对冲",
                               f"你说要对冲,但这票和持仓正相关 {corr['max_pearson']:.2f}——这不是对冲,是加注同方向",
                               _hist_text(_mistake_hist(mrules, "乱对冲"))))

    # 仓位集中 / 同票堆积
    same = book.get("same_ticker_count", 0)
    if same and same >= 1:
        flags.append(_flag("stack", "warn", "⚠️", "同标的堆积",
                           f"{ticker} 你已经有 {same} 个仓——再加就是同票加注",
                           _hist_text(_mistake_hist(mrules, "持仓数量过多"))))

    # 这票你之前亏过(只在亏损实质 或 胜率明显偏低时才提醒,$几十的不烦你)
    th = _ticker_hist(mrules, ticker)
    if th and (th.get("net_pnl", 0) <= -100
               or (th.get("win_rate") is not None and th["win_rate"] < 0.40 and th.get("n", 0) >= 3)):
        flags.append(_flag("loser", "warn", "⚠️", f"{ticker} 你之前亏过",
                           _hist_text(th) or f"{ticker} 历史净亏"))

    # ── B. 行为自检 / 摩擦(把历史代价怼出来,让你主动确认)──────────
    hl = mrules.get("headline") or {}
    flags.append(_flag("thesis", "selfcheck", "🧠", "有没有自主 thesis?",
                       "选『跟单/抄底』或 confidence=赌博就别开——这是你最大的洞",
                       _hist_text(hl.get("no_logic"))))
    flags.append(_flag("emotion", "selfcheck", "😤", "现在什么情绪?",
                       "FOMO / 复仇 / 愤怒 → 先停 5 分钟",
                       _hist_text(hl.get("emotion"))))
    flags.append(_flag("hold_plan", "selfcheck", "⏳", "持有计划?",
                       "别 1-2 天慌出(你 ≤5 天的单整体亏);也别赢了提前割成小牌",
                       _hist_text(hl.get("short_hold"))))
    flags.append(_flag("stop", "selfcheck", "📉", "不止损(限损价差)",
                       "回测铁律:借方价差不止损——砍掉的亏损单多半反弹;止盈看 regime:牛市吃 70-80%、震荡 50-60% 就平、腾资金开下单",
                       None))

    return flags

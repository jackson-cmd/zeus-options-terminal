"""stock_selector 主编排 —— 唯一 IO 编排点(仿 stock_picker/engine.py 的分工)。

run(today, no_sonnet) → dict:
  1. CSV 加载 + 清洗 —— 复用 stock_picker.loader(不重复实现)
  2. 财报窗口 + 期权流动性过滤 —— 复用 stock_picker.filters(两策略共用, 只跑一次)
  3. 策略 A / 策略 B 各自纯函数过滤 —— strategies.py
  4. 各自打分排序取 top N —— ranker.py
  5. 期权链存在性 + 选 strike —— 复用 stock_picker.strike_engine
  6. 两策略候选去重合并, 一次性调 Sonnet 复核 —— sonnet_analyzer.py
  7. 组装最终 dict, 用 json_safe() 兜底清洗 NaN/Timestamp(供 PWA 端点直接吐 JSON)

任何一步失败(CSV 没有/期权归档没挂载/Sonnet 挂了)都 fail-soft, 尽量给出能给的
部分, 不整体抛异常崩掉 PWA 请求(由调用方 optionstrat_server.py 的 try/except 兜底,
这里的 fail-soft 是第二道防线)。
"""
from __future__ import annotations

import datetime as _dt
import glob as _glob
import json as _json
import math
from pathlib import Path
from typing import Optional

import pandas as pd

from pre_trade_checkup import data_sources as DS
from stock_picker import loader as sp_loader
from stock_picker import filters as sp_filters
from stock_picker import strike_engine as sp_strike

from . import config, strategies, ranker, sonnet_analyzer, tt_strikes

REPO_ROOT = Path(__file__).resolve().parent.parent
PREMARKET_DIR = REPO_ROOT / "data" / "benzinga"
PREMARKET_CHANNEL = "chris-market-commentary"
PREMARKET_TAIL_CHARS = 2000
MARKET_BRIEF_DIR = REPO_ROOT / "data" / "market_brief"
CALENDAR_CACHE_FILE = REPO_ROOT / "trade_journal" / "data" / "calendar_cache.json"
EARNINGS_ENRICH_WINDOW_DAYS = 60   # 财报落在未来这么多天内才喂给 Sonnet(独立于 T4 硬过滤窗口)


def _load_market_brief_context(today: str) -> dict:
    """把三个 mbrief 缓存喂进 Sonnet:
       1. 今日 market_brief (Sonnet 自己每日 09:00 生成的大盘判断: tech_regime + summary)
       2. 行业轮动 (leaders/laggards/warming/cooling)
       3. 期权情绪 (rank_iv/rank_skew 高低分层)
       都是本地 JSON,读不到就跳过(不阻断 Sonnet 调用)。"""
    out: dict = {}
    # 1. 今日大盘判断
    try:
        brief_file = MARKET_BRIEF_DIR / f"{today}.json"
        if brief_file.exists():
            d = _json.loads(brief_file.read_text(encoding="utf-8"))
            out["market_brief"] = {
                "tech_regime_note": (d.get("tech_regime") or {}).get("note"),
                "summary_day": d.get("summary_day"),
                "tech": d.get("tech"),   # SPY/QQQ/IWM/DIA 快照
            }
    except Exception:
        pass
    # 2. 行业轮动
    try:
        ig_file = MARKET_BRIEF_DIR / "industry_groups.json"
        if ig_file.exists():
            d = _json.loads(ig_file.read_text(encoding="utf-8"))
            rot = d.get("rotation") or {}
            out["industry_rotation"] = {
                "leaders": rot.get("leaders", [])[:6],
                "laggards": rot.get("laggards", [])[:6],
                "warming": rot.get("warming", [])[:4],
                "cooling": rot.get("cooling", [])[:4],
                "as_of": d.get("as_of"),
            }
    except Exception:
        pass
    # 3. 期权情绪
    try:
        op_file = MARKET_BRIEF_DIR / "options_pulse.json"
        if op_file.exists():
            d = _json.loads(op_file.read_text(encoding="utf-8"))
            iv = d.get("rank_iv") or {}
            sk = d.get("rank_skew") or {}
            out["options_pulse"] = {
                "iv_high": [x.get("name") for x in (iv.get("high") or [])[:5]],
                "iv_low": [x.get("name") for x in (iv.get("low") or [])[:5]],
                "skew_high": [x.get("name") for x in (sk.get("high") or [])[:5]],
                "skew_low": [x.get("name") for x in (sk.get("low") or [])[:5]],
                "asof": d.get("asof"),
            }
    except Exception:
        pass
    return out


# ══════════════════════════ 数值/JSON 清洗小工具 ══════════════════════════
def _num(x) -> Optional[float]:
    """NaN / None / 非数值 → None, 其余转 float。避免 NaN 混进吐给前端的 JSON。"""
    try:
        if x is None:
            return None
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def json_safe(obj):
    """递归清洗 NaN / pd.Timestamp / datetime.date → None / ISO 字符串, 确保吐出去
    的是真合法 JSON(标准 json 库对 NaN 只会吐字面量 NaN, 浏览器 fetch().json() 严
    格解析会直接报错; 对 Timestamp 直接 TypeError)。逻辑照抄
    optionstrat_server.py 原 _sp_json_safe(stock_picker HTTP 壳废弃前的实现),
    本模块 + PWA 端点共用, 不重复维护两份。"""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, _dt.datetime, _dt.date)):
        try:
            return None if pd.isna(obj) else obj.isoformat()
        except TypeError:
            return obj.isoformat()
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


# ══════════════════════════ 市场环境(Sonnet prompt 素材) ══════════════════════════
def _pct_change(df: Optional[pd.DataFrame], n_days: int) -> Optional[float]:
    if df is None:
        return None
    close = df["Close"].dropna()
    if len(close) <= n_days:
        return None
    last, ago = float(close.iloc[-1]), float(close.iloc[-1 - n_days])
    if ago == 0:
        return None
    return round((last / ago - 1) * 100, 2)


def _latest_premarket_snippet() -> str:
    """glob data/benzinga/*.json 取最新一份(不假设今天的文件一定已经生成) , 抽
    chris-market-commentary 频道尾部片段(最新消息在文本末尾, 截尾不截头)。目录/
    文件不存在或解析失败 → 空字符串(不阻断, prompt 里就省略这块)。"""
    if not PREMARKET_DIR.exists():
        return ""
    files = sorted(_glob.glob(str(PREMARKET_DIR / "*.json")))
    if not files:
        return ""
    latest = Path(files[-1])
    try:
        data = _json.loads(latest.read_text(encoding="utf-8"))
        channels = data.get("channels") or {}
        text = (channels.get(PREMARKET_CHANNEL) or {}).get("text") or ""
        if not text:
            # 兜底: 没有惯例频道时, 看看有没有直接的 headlines/summary 字段
            text = data.get("summary") or "\n".join(data.get("headlines") or [])
        return text[-PREMARKET_TAIL_CHARS:].strip()
    except Exception:
        return ""


def build_market_context(today: str) -> dict:
    """SPY/QQQ/VIX + benzinga 今日热点 + 盘前素材, 一次性拿齐(T8 宏观展示 + Sonnet
    prompt 共用)。全部走 pre_trade_checkup.data_sources 的薄封装, 不重复起
    yfinance/digest 读取。"""
    spy_df = DS.get_ohlc("SPY")
    qqq_df = DS.get_ohlc("QQQ")
    vix = DS.get_vix()

    items = DS.get_digest_today(today) or []
    headlines = []
    for it in items[:config.SONNET_TOP_K_HEADLINES]:
        title = it.get("title_en") or it.get("title") or ""
        if title:
            headlines.append(title)

    top_topics: list = []
    try:
        baseline = DS.risk_radar_baseline()
        saturation = DS.regime_saturation(items, baseline)
        hot = sorted(saturation.get("hot_topics", []), key=lambda h: -h.get("contrib", 0))
        top_topics = [h["label"] for h in hot[:3]]
    except Exception:
        pass

    mb_ctx = _load_market_brief_context(today)

    return {
        "spy_5d": _pct_change(spy_df, 5),
        "spy_20d": _pct_change(spy_df, 20),
        "qqq_5d": _pct_change(qqq_df, 5),
        "vix": _num(vix),
        "top_topics": top_topics,
        "headlines": headlines,
        "premarket_snippet": _latest_premarket_snippet(),
        "n_digest_items": len(items),
        # 三个 mbrief 缓存 —— Sonnet 现在也拿到大盘判断/行业轮动/期权情绪 3 个视角
        "market_brief": mb_ctx.get("market_brief"),
        "industry_rotation": mb_ctx.get("industry_rotation"),
        "options_pulse": mb_ctx.get("options_pulse"),
    }


def _load_ticker_enrichment(today: str, tickers: list) -> dict:
    """给候选票逐个拉:
       1. bull / bear catalyst points (narratives.store 今日最新一份 —— 作者「催化剂」需求)
       2. company profile 中文简介 (data/company_profiles.json —— 业务/合作/近况)
       3. 下次财报日期 + 距今天数 (trade_journal/data/calendar_cache.json —— 让 Sonnet
          看到"持仓期内是否会跨过财报"这种具体催化剂, 不只是技术面)
       返回 {TICKER_UPPER: {bull:[], bear:[], net:str, profile:str, upcoming_earnings:{date,days}}}。
       任一票缺数据 → 那票 key 不出现(prompt 里也就没这段, 不阻断)。"""
    out: dict = {}
    tickers_up = {t.upper() for t in tickers if t}
    if not tickers_up:
        return out

    # narratives: 取今日或最近一天(narratives job 每天 8/18 点跑, 可能今天早晨还没生成)
    try:
        from narratives import store as narr_store
        days = narr_store.list_days()
        # 拿 <= today 里最近的
        recent = [d for d in days if d <= today]
        pick = recent[-1] if recent else (days[-1] if days else None)
        if pick:
            items = narr_store.load_day(pick) or []
            for it in items:
                tkr = (it.get("ticker") or "").upper()
                if tkr in tickers_up:
                    out.setdefault(tkr, {})
                    out[tkr]["bull"] = (it.get("bull") or [])[:3]  # 各留 top 3 省 token
                    out[tkr]["bear"] = (it.get("bear") or [])[:3]
                    out[tkr]["net"] = it.get("net") or ""
                    out[tkr]["narr_asof"] = pick
    except Exception:
        pass

    # company profile
    try:
        cp_file = REPO_ROOT / "data" / "company_profiles.json"
        if cp_file.exists():
            cp = _json.loads(cp_file.read_text(encoding="utf-8"))
            for tkr in tickers_up:
                if tkr in cp:
                    out.setdefault(tkr, {})
                    summary = cp[tkr].get("summary") or ""
                    # 只喂前 400 字给 Sonnet(业务/近况通常都在开头)
                    out[tkr]["profile"] = summary[:400]
    except Exception:
        pass

    # 下次财报日期(trade_journal 的 calendar_cache.json)。独立于 T4 财报窗口硬过滤——
    # 那层只负责"挡近期财报"的候选; 这里是软信息, 哪怕财报落在窗口外, 只要接近
    # 35-45 DTE 持仓期, Sonnet 也该看见, 好判断到期前会不会先跨过财报。
    try:
        if CALENDAR_CACHE_FILE.exists():
            cal = _json.loads(CALENDAR_CACHE_FILE.read_text(encoding="utf-8"))
            today_date = _dt.date.fromisoformat(today)
            earn_by_ticker: dict = {}
            for e in (cal.get("earnings") or []):
                tkr = (e.get("ticker") or "").upper()
                d = e.get("date")
                if not tkr or not d:
                    continue
                if tkr not in earn_by_ticker or d < earn_by_ticker[tkr]:
                    earn_by_ticker[tkr] = d   # 同票多条(改期等) → 取最早一条
            for tkr in tickers_up:
                d = earn_by_ticker.get(tkr)
                if not d:
                    continue
                try:
                    days = (_dt.date.fromisoformat(d) - today_date).days
                except ValueError:
                    continue
                if 0 <= days <= EARNINGS_ENRICH_WINDOW_DAYS:
                    out.setdefault(tkr, {})
                    out[tkr]["upcoming_earnings"] = {"date": d, "days": days}
    except Exception:
        pass

    return out


# ══════════════════════════ 候选行 → 输出 dict ══════════════════════════
def _strike_fields(row: dict) -> dict:
    status = row.get("strike_status")
    if status != "ok":
        return {"strike_status": status, "long_strike": None, "short_strike": None,
                "expiry": None, "dte": None, "est_debit": None, "n_contracts": None}
    return {
        "strike_status": "ok",
        "long_strike": _num(row.get("long_strike")),
        "short_strike": _num(row.get("short_strike")),
        "expiry": row.get("expiry"),
        "dte": row.get("dte"),
        "est_debit": _num(row.get("est_debit_per_share")),
        "n_contracts": row.get("n_contracts"),
        # 新增: POP + 挂单价 + 完整 ticket
        "pop_pct": _num(row.get("pop_pct")),
        "breakeven": _num(row.get("breakeven")),
        "be_offset_pct": _num(row.get("be_offset_pct")),
        "long_iv": _num(row.get("long_iv")),
        "long_bid": _num(row.get("long_bid")), "long_ask": _num(row.get("long_ask")),
        "short_bid": _num(row.get("short_bid")), "short_ask": _num(row.get("short_ask")),
        "aggressive_lmt": _num(row.get("aggressive_lmt")),
        "tp_lmt_90pct": _num(row.get("tp_lmt_90pct")),
        "actual_debit_usd": _num(row.get("actual_debit_usd")),
        "target_position_usd": _num(row.get("target_position_usd")),
        "ticket": row.get("ticket"),
        "ticket_tp": row.get("ticket_tp"),
    }


def _candidate_dict(row: dict, extra: Optional[dict] = None) -> dict:
    d = {
        "symbol": row["Symbol"],
        "latest": _num(row.get("Latest")),
        "1m_chg": _num(row.get("1M %Chg")),
        "3m_chg": _num(row.get("3M %Chg")),
        "rsi": _num(row.get("14D RSI")),
        "wtd_alpha": _num(row.get("Wtd Alpha")),
    }
    d.update(_strike_fields(row))
    if extra:
        d.update(extra)
    return d


def _apply_sonnet(cand: dict, sonnet_result: dict) -> None:
    info = (sonnet_result.get("by_symbol") or {}).get(cand["symbol"]) if sonnet_result else None
    if sonnet_result and sonnet_result.get("status") == "ok" and info:
        cand["sonnet_verdict"] = info.get("verdict") or None
        cand["confidence"] = info.get("confidence")
        cand["reason_zh"] = info.get("reason_zh") or ""
        cand["macro_risk"] = info.get("macro_risk") or None
        cand["news_flags"] = info.get("news_flags") or []
    elif sonnet_result and sonnet_result.get("status") == "ok":
        # Sonnet 跑了但没提这只票(理论上不该发生, 因为送进去的就是这些票; fail-soft 兜底)
        cand["sonnet_verdict"] = None
        cand["confidence"] = None
        cand["reason_zh"] = ""
        cand["macro_risk"] = None
        cand["news_flags"] = []
    else:
        cand["sonnet_verdict"] = None
        cand["confidence"] = None
        cand["reason_zh"] = ""
        cand["macro_risk"] = None
        cand["news_flags"] = []


# ══════════════════════════ 主编排 ══════════════════════════
def run(csv_path: Optional[str] = None, today: Optional[str] = None,
        no_sonnet: bool = False, verbose: bool = False,
        archive_dir: Optional[Path] = None,
        tt_client=None) -> dict:
    today_date = _dt.date.fromisoformat(today) if today else _dt.date.today()
    today_str = today_date.isoformat()

    if csv_path:
        df = sp_loader.load_csv(Path(csv_path))
    else:
        try:
            source = sp_loader.resolve_default_csv_source()
            source = sp_loader.ensure_local_copy(source)
            df = sp_loader.load_csv(source)
        except FileNotFoundError:
            # No Barchart export → same columns computed from local daily candles
            df = sp_loader.build_local_frame()
    df, _ = sp_loader.clean(df, verbose=verbose)

    # 财报窗口 + 期权流动性: 两策略共用, 复用 stock_picker 现成纯函数(只跑一次,
    # 不在 strategies.py 里重复实现)
    df = sp_filters.layer_t4_earnings_blocker(df, today_date, verbose=verbose)
    df = sp_filters.layer_t6_options_liquidity(df, verbose=verbose)

    trend_df = strategies.strategy_a_trend_follow(df, verbose=verbose)
    bottom_df = strategies.strategy_b_bottom_fish(df, verbose=verbose)

    trend_df = ranker.rank_trend_follow(trend_df)
    bottom_df = ranker.rank_bottom_fish(bottom_df)

    # 期权链存在性(T5)+ 选 strike(T10), 复用 stock_picker.strike_engine —— 只对
    # 已经打分裁到 top N 的候选做(通常 ≤15/策略), 避免对全池 408 票逐个读归档 parquet
    # Strike 挑选优先级: TastyTrade REST 实时链 > SSD EOD 归档 > 什么都没
    # 服务端调用时 optionstrat_server 会传 state.client 进来, 直接 TT 拿实时报价;
    # CLI 单独跑没 client 就退回 SSD; SSD 也没就标 no_data(候选仍保留, 不砍)
    if tt_client is not None:
        trend_df = tt_strikes.enrich_with_tt(trend_df, tt_client, today_date, verbose=verbose)
        bottom_df = tt_strikes.enrich_with_tt(bottom_df, tt_client, today_date, verbose=verbose)
    else:
        archive_root = archive_dir if archive_dir is not None else sp_strike.config.OPTIONS_ARCHIVE_DIR
        if archive_root.exists():
            trend_df = sp_strike.layer_t5_options_chain_exists(trend_df, archive_dir, verbose=verbose)
            bottom_df = sp_strike.layer_t5_options_chain_exists(bottom_df, archive_dir, verbose=verbose)
            trend_df = sp_strike.enrich_with_strikes(trend_df, today_date, archive_dir, verbose=verbose)
            bottom_df = sp_strike.enrich_with_strikes(bottom_df, today_date, archive_dir, verbose=verbose)
        else:
            print(f"⚠️ 无 TT client + SSD 未挂载 → 候选保留但缺 strike/debit")
            trend_df = trend_df.assign(strike_status="no_data")
            bottom_df = bottom_df.assign(strike_status="no_data")

    trend_candidates = [_candidate_dict(r) for r in trend_df.to_dict(orient="records")]
    bottom_candidates = [
        _candidate_dict(r, {"touch_ema": (int(r["touch_ema"]) if pd.notna(r.get("touch_ema")) else None)})
        for r in bottom_df.to_dict(orient="records")
    ]

    market_context = build_market_context(today_str)

    # 给候选票挂催化剂 + 基本面 (bull points / bear points / 业务简介) —— Sonnet
    # 判断潜在利好利空时能看见「HOOD 有 agentic trading」这种独一信息, 不是只凭大盘
    all_symbols = [c.get("symbol") for c in trend_candidates + bottom_candidates]
    ticker_enrich = _load_ticker_enrichment(today_str, all_symbols)
    market_context["ticker_enrichment"] = ticker_enrich

    sonnet_result = {"status": "skipped", "by_symbol": {}, "error": None, "usage": {}}
    if not no_sonnet and (trend_candidates or bottom_candidates):
        sonnet_result = sonnet_analyzer.analyze(today_str, market_context, trend_candidates, bottom_candidates)

    for c in trend_candidates:
        _apply_sonnet(c, sonnet_result)
    for c in bottom_candidates:
        _apply_sonnet(c, sonnet_result)

    result = {
        "today": today_str,
        "trend_follow": {"count": len(trend_candidates), "candidates": trend_candidates},
        "bottom_fish": {"count": len(bottom_candidates), "candidates": bottom_candidates},
        "market_context": {
            "spy_5d": market_context["spy_5d"],
            "spy_20d": market_context["spy_20d"],
            "qqq_5d": market_context["qqq_5d"],
            "vix": market_context["vix"],
            "top_topics": market_context["top_topics"],
        },
        "sonnet_status": sonnet_result["status"],
        "sonnet_error": sonnet_result.get("error"),
        "as_of": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    return json_safe(result)

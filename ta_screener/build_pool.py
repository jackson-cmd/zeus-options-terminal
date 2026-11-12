"""TA 扫描【专属池】构建:从全 universe 筛掉「不符合 bull call」的票,合格的写
data/ta_screener_pool.txt;扫描只在池里跑(不符合的压根不扫,省算力)。

合格 = 价格带 ∈ [PRICE_LO, PRICE_HI]  且  年化波动 ≥ VOL_FLOOR  且  近 NEWS_LOOKBACK 天有新闻
       且 (波动 ≥ VOL_CLEAR  或  新闻数 ≥ NEWS_MIN)
  · 价格:太便宜(<$40)% 买卖价差宽/手续费占比高/成交差(journal $40-100 段已亏、<$20 灾难
    净bid-ask 占权利金 87%);太贵(>$1500)资金占用大。贵票 % 滑点其实更小,故上限只挡极端。
  · 波动:不会动 → bull call 40 天吃不到 10% 止盈(BRK-B 14%/债券/防御股)。比 beta/sector 准:
    高波动电力股 VST(挂 Utilities)留、低波动银行 V/MA 不留。
    闷票(波动 20-30%)必须有新闻才留——治 AAPL(闷但 279 条新闻→留)vs V/MA(闷+少闻→剔)。
    明确会动的(波动≥30%)只要不是 0 新闻就留。

跑法(repo 根): PYTHONPATH=. python3 ta_screener/build_pool.py
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from . import data as D
except ImportError:
    import data as D  # type: ignore

REPO = Path(__file__).resolve().parent.parent
POOL_FILE = REPO / "data" / "ta_screener_pool.txt"
POOL_META = REPO / "data" / "ta_screener" / "pool_meta.json"
POOL_EXCLUDE = REPO / "data" / "ta_screener_pool_exclude.txt"   # 作者手动勾掉的票(永久挡池外)

PRICE_LO = 40.0         # 现价下限(journal:$40-100 已弱、<$40 更差;<$20 成交灾难)
PRICE_HI = 1500.0       # 现价上限(只挡极端贵;贵票 % 滑点反而更小)
VOL_FLOOR = 0.20        # 硬波动下限(<此=不会动:BRK-B 14%/债券/KO/JNJ);AAPL 23% 在线上
VOL_CLEAR = 0.30        # ≥此=明确会动,新闻只需 ≥1(高波动猛票/杠杆 ETF 免新闻门槛)
NEWS_MIN = 25           # 闷票(VOL_FLOOR~VOL_CLEAR)的新闻门槛(V 23 剔、AAPL 279 留)
NEWS_LOOKBACK = 60      # 新闻回看天数(作者:过去 30-60 天)
LOOKBACK = 252          # 算 vol/beta 的回看交易日


def _news_counts(lookback: int = NEWS_LOOKBACK) -> dict:
    """{ticker: 近 lookback 天 digest 文章数};digest 不可用 → {}(退回纯波动+价格过滤)。"""
    import sys
    p = str(REPO / "benzinga_digest" / "src")
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        from benzinga_mcp import digest_store as ds
    except Exception:
        return {}
    cnt: Counter = Counter()
    try:
        for d in ds.list_days()[:lookback]:
            for a in (ds.load_day(d) or []):
                for t in (a.get("tickers") or []):
                    cnt[str(t).upper()] += 1
    except Exception:
        pass
    return dict(cnt)


def _news(cnt: dict, t: str) -> int:
    return max(cnt.get(t, 0), cnt.get(t.replace("-", "."), 0), cnt.get(t.replace("-", ""), 0))


def _load_exclude() -> set:
    """手动排除名单 → set(yahoo 符号化);无文件 → 空集。按行去注释(整行/行尾 # 都去)。"""
    if not POOL_EXCLUDE.exists():
        return set()
    out = set()
    for line in POOL_EXCLUDE.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0]                    # 去掉整行/行尾注释
        for tok in line.split(","):
            tok = tok.strip()
            if tok:
                out.add(tok.upper().replace(".", "-"))
    return out


def _vol_beta(close: pd.Series, spy: pd.Series):
    r = np.diff(np.log(close.to_numpy(dtype=float)[-LOOKBACK:]))
    vol = float(r.std() * np.sqrt(252)) if len(r) > 20 else None
    beta = None
    if spy is not None:
        j = pd.concat([np.log(close).diff(), np.log(spy).diff()], axis=1).dropna().tail(LOOKBACK)
        if len(j) > 30 and j.iloc[:, 1].var() > 0:
            beta = float(j.cov().iloc[0, 1] / j.iloc[:, 1].var())
    return vol, beta


def build(log=print) -> dict:
    tickers, _cat = D.parse_universe()
    log(f"[pool] universe {len(tickers)} 票,下载历史(走缓存)+ digest 新闻雷达…")
    hist = D.download_history(tickers, progress_cb=None)
    sec = D.sector_map()
    news_cnt = _news_counts()
    news_on = bool(news_cnt)
    if not news_on:
        log("[pool] ⚠️ digest 新闻不可用 → 退回纯波动+价格过滤")
    spy = hist.get("SPY")
    spy_close = spy["Close"] if spy is not None else None

    exclude = _load_exclude()
    if exclude:
        log(f"[pool] 手动排除名单 {len(exclude)} 票")
    kept, cut, no_data = [], [], []
    meta = {}
    for t in tickers:
        if t in exclude:
            cut.append(t)
            meta[t] = {"in_pool": False, "reason": "手动排除"}
            continue
        df = hist.get(t)
        if df is None or len(df) < 120:
            no_data.append(t)
            meta[t] = {"in_pool": True, "reason": "数据不足(暂保留)"}
            continue
        px = float(df["Close"].to_numpy()[-1])
        vol, beta = _vol_beta(df["Close"], spy_close)
        nw = _news(news_cnt, t)
        reasons = []
        if not (PRICE_LO <= px <= PRICE_HI):
            reasons.append(f"价 ${px:.0f}")
        if vol is not None and vol < VOL_FLOOR:
            reasons.append(f"波动低 {vol:.0%}")
        if news_on:
            if nw < 1:
                reasons.append("0新闻")
            elif vol is not None and vol < VOL_CLEAR and nw < NEWS_MIN:
                reasons.append(f"闷+少闻 {nw}")
        meta[t] = {"price": round(px, 2), "vol": round(vol, 3) if vol else None,
                   "beta": round(beta, 2) if beta else None, "news": nw, "sector": sec.get(t, ""),
                   "in_pool": not reasons, "reason": "、".join(reasons) or "合格"}
        (cut if reasons else kept).append(t)

    pool = kept + no_data                      # 下载失败的暂留池中(别因瞬时拉取失败丢票)
    POOL_FILE.write_text(",".join(pool), encoding="utf-8")
    POOL_META.parent.mkdir(parents=True, exist_ok=True)
    POOL_META.write_text(json.dumps({
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "price_lo": PRICE_LO, "price_hi": PRICE_HI, "vol_floor": VOL_FLOOR,
        "vol_clear": VOL_CLEAR, "news_min": NEWS_MIN, "news_lookback": NEWS_LOOKBACK,
        "news_on": news_on, "n_universe": len(tickers), "n_pool": len(pool), "n_cut": len(cut),
        "tickers": meta,
    }, ensure_ascii=False), encoding="utf-8")

    # 剔除原因汇总
    def _cat(r):
        if "价 " in r:
            return "价格(太便宜/贵)"
        if "波动低" in r:
            return "波动太低(不会动)"
        if "0新闻" in r:
            return "0 新闻(没催化)"
        return "闷+少闻(V/MA类)"
    cc = Counter(_cat(meta[t]["reason"]) for t in cut)
    log(f"[pool] 池子 {len(pool)} 票(剔 {len(cut)},数据缺暂留 {len(no_data)})→ {POOL_FILE.name}")
    log(f"[pool] 剔除原因: {dict(cc)}")
    return {"n_pool": len(pool), "n_cut": len(cut), "reasons": dict(cc)}


if __name__ == "__main__":
    build()

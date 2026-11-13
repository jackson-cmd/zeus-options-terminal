"""行业分组的 OHLC 本地库 + 读取/刷新。

⚠️ 为什么不复用 data/daily_ohlc.parquet: 那个库只对 heatmap 的 160 只 universe
每日更新, 其余票冻在上次抓取时。实测踩过 —— CNC/HUM 停在 6-29 而 UNH 到 7-23,
于是「3 个月收益」measured 的不是同一段时间, 医保组假显示 +101.7% 领先全场。
行业轮动是**横向比较**, 全体必须对齐到同一天, 所以单独存一份全量刷新的库。
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import groups as _g  # noqa: E402

REPO = _HERE.parent
UNIVERSE = REPO / "data" / "industry_universe.txt"
STORE = REPO / "data" / "industry_ohlc.parquet"
CACHE = REPO / "data" / "market_brief" / "industry_groups.json"


def universe_text() -> str:
    try:
        return UNIVERSE.read_text(encoding="utf-8")
    except Exception:
        return ""


def all_tickers() -> list:
    return sorted({t for v in _g.parse_universe(universe_text()).values() for t in v})


def load_closes():
    """→ ({ticker: [close...]}, {ticker: 'YYYY-MM-DD'})。库不在就返回空。"""
    try:
        import pandas as pd
        df = pd.read_parquet(STORE)
    except Exception:
        return {}, {}
    closes, lasts = {}, {}
    for t, g in df.groupby("ticker"):
        g = g.sort_index()
        closes[t] = [float(x) for x in g["Close"] if x == x]
        lasts[t] = str(g.index[-1])[:10]
    return closes, lasts


def refresh(period: str = "2y", log=print) -> dict:
    """全量重拉所有票 → 落盘。**必须全量**, 否则新旧混算(见模块头注释)。"""
    tickers = all_tickers()
    if not tickers:
        return {"ok": 0, "bad": [], "error": "universe 为空"}
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as e:
        return {"ok": 0, "bad": [], "error": f"依赖缺失: {e}"}
    log(f"[industry] 全量刷新 {len(tickers)} 只…")
    try:
        d = yf.download(tickers, period=period, interval="1d", auto_adjust=False,
                        progress=False, group_by="ticker", threads=True)
    except Exception as e:
        return {"ok": 0, "bad": [], "error": f"下载失败: {e}"}
    rows, bad = [], []
    for t in tickers:
        try:
            sub = d[t].dropna(subset=["Close"])
            if sub.empty or len(sub) < 25:      # 新上市票也收(短期窗口仍可算)
                bad.append(t)
                continue
            x = sub[["Open", "High", "Low", "Close", "Volume"]].copy()
            x["ticker"] = t
            rows.append(x)
        except Exception:
            bad.append(t)
    if not rows:
        return {"ok": 0, "bad": bad, "error": "无有效数据"}
    out = pd.concat(rows)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".parquet.tmp")
    out.to_parquet(tmp)
    tmp.replace(STORE)
    log(f"[industry] 落库 {out.ticker.nunique()} 只 / {len(out):,} 行; 无效 {len(bad)}")
    return {"ok": int(out.ticker.nunique()), "bad": bad, "rows": int(len(out))}


def compute(max_lag_days: int = 5) -> dict:
    """读库 → 组级趋势 + 轮动摘要。"""
    closes, lasts = load_closes()
    if not closes:
        return {"error": "本地库为空, 先跑 refresh()", "rows": []}
    rows = _g.build(universe_text(), closes, last_dates=lasts, max_lag_days=max_lag_days)
    stale = sorted(_g.stale_tickers(lasts, max_lag_days))
    dates = [d for d in lasts.values() if d]
    return {
        "rows": rows,
        "rotation": _g.rotation(rows),
        "as_of": max(dates) if dates else "",
        "n_tickers": len(closes),
        "n_groups": len(rows),
        "stale": stale,
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
    }


def save(res: dict) -> None:
    import json
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE)
    except Exception as e:
        print(f"[industry] save err: {e}")


def load_cache():
    import json
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def get(force: bool = False) -> dict:
    """有当天缓存直接用; 否则重算(不重拉数据 —— 拉数据用 refresh)。"""
    if not force:
        c = load_cache()
        if c and not c.get("error"):
            # 库比缓存新 → 重算
            try:
                if STORE.stat().st_mtime <= Path(CACHE).stat().st_mtime:
                    return c
            except Exception:
                return c
    res = compute()
    if not res.get("error"):
        save(res)
    return res


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="重拉全量 OHLC")
    a = ap.parse_args()
    if a.refresh:
        print(json.dumps(refresh(), ensure_ascii=False)[:400])
    r = get(force=True)
    print(f"as_of={r.get('as_of')} 组={r.get('n_groups')} 票={r.get('n_tickers')} "
          f"陈旧剔除={len(r.get('stale') or [])}")
    for g in (r.get("rows") or [])[:10]:
        print(f"  {g['name'][:22]:24} 1mo {g.get('m1'):>7} 3mo {g.get('m3'):>7} "
              f"6mo {g.get('m6'):>7}  {g['state']}·{g['grade']}")

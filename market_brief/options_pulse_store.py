"""期权情绪的数据层: 从 EOD 全链派生「每票每日一行」的小汇总, 再滚到板块。

为什么要中间这一层: eod_chain.parquet 是 2200 万行 / 1GB(440 只 × 59 天 × 全链)。
每次进 tab 都全读要几十秒。派生出来的汇总只有 ~26000 行(440×59), 几百 KB,
读起来是毫秒级。汇总是**增量**的 —— 只算新出现的日期。

   某些票停更, 它的"今天 IV"其实是上周的 —— 混进排名整个就废了。
   所以汇总里存 last_date, 上层用 options_pulse.stale_tickers 剔除。
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import options_pulse as _op  # noqa: E402

REPO = _HERE.parent
CHAIN = REPO / "eod_crawler" / "data_eod" / "eod_chain.parquet"
DAILY = REPO / "data" / "market_brief" / "options_daily.parquet"
CACHE = REPO / "data" / "market_brief" / "options_pulse.json"

# 只读需要的列 —— 全表 17 列 1GB, 这 10 列约 1/3
COLS = ["Symbol", "ExpirationDate", "PutCall", "StrikePrice", "Volume",
        "OpenInterest", "UnderlyingPrice", "ImpliedVolatility", "Delta", "DataDate"]


def _universe_groups() -> dict:
    """复用行业分组 —— 期权情绪和行业轮动看的是同一套 79 组, 才对得上。"""
    try:
        import groups_store as _gs
        import groups as _g
        return _g.parse_universe(_gs.universe_text())
    except Exception:
        return {}


def existing_dates() -> set:
    try:
        import pandas as pd
        return set(pd.read_parquet(DAILY, columns=["date"]).date.astype(str).unique())
    except Exception:
        return set()


def chain_dates() -> list:
    try:
        import pandas as pd
        d = pd.read_parquet(CHAIN, columns=["DataDate"]).DataDate.astype(str)
        return sorted(d.unique())
    except Exception:
        return []


def build_daily(days: list = None, log=print) -> dict:
    """把全链按日汇总成「每票每日一行」。days=None → 只补缺的日期(增量)。"""
    try:
        import pandas as pd
    except Exception as e:
        return {"ok": 0, "error": f"依赖缺失: {e}"}
    if not CHAIN.exists():
        return {"ok": 0, "error": f"链库不在: {CHAIN}"}

    all_d = chain_dates()
    if not all_d:
        return {"ok": 0, "error": "链库读不出日期"}
    todo = sorted(set(days) & set(all_d)) if days else sorted(set(all_d) - existing_dates())
    if not todo:
        return {"ok": 0, "skipped": True, "have": len(existing_dates())}

    log(f"[optpulse] 需要汇总 {len(todo)} 天: {todo[0]} … {todo[-1]}")
    rows = []
    for i, day in enumerate(todo, 1):
        try:
            df = pd.read_parquet(CHAIN, columns=COLS,
                                 filters=[("DataDate", "=", day)])
        except Exception as e:
            log(f"[optpulse] {day} 读取失败: {e}")
            continue
        if df.empty:
            continue
        dd = pd.Timestamp(str(day)[:10])
        df["dte"] = (pd.to_datetime(df.ExpirationDate) - dd).dt.days
        # 先在 pandas 里砍到 DTE 窗口 —— 纯函数逐票遍历前把量降一个数量级
        df = df[(df.dte >= _op.DTE_LO) & (df.dte <= _op.DTE_HI)]
        for sym, sub in df.groupby("Symbol", sort=False):
            m = _op.ticker_metrics(sub.to_dict("records"))
            if not m:
                continue
            m["ticker"] = str(sym).upper()
            m["date"] = str(day)[:10]
            rows.append(m)
        if i % 10 == 0 or i == len(todo):
            log(f"[optpulse]   {i}/{len(todo)} 天, 累计 {len(rows):,} 行")
    if not rows:
        return {"ok": 0, "error": "没算出任何行"}

    new = pd.DataFrame(rows)
    if DAILY.exists():
        try:
            old = pd.read_parquet(DAILY)
            new = pd.concat([old, new], ignore_index=True)
            new = new.drop_duplicates(subset=["ticker", "date"], keep="last")
        except Exception:
            pass
    DAILY.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY.with_suffix(".parquet.tmp")
    new.to_parquet(tmp, index=False)
    tmp.replace(DAILY)
    log(f"[optpulse] 落库 {new.ticker.nunique()} 只 / {len(new):,} 行 / "
        f"{new.date.nunique()} 天")
    return {"ok": int(new.ticker.nunique()), "rows": int(len(new)),
            "days": int(new.date.nunique())}


def load_daily():
    """→ (today {ticker: metrics}, history {ticker: {metric: [...]}} , last_dates, asof)"""
    try:
        import pandas as pd
        df = pd.read_parquet(DAILY)
    except Exception:
        return {}, {}, {}, ""
    if df.empty:
        return {}, {}, {}, ""
    df = df.sort_values("date")
    asof = str(df.date.max())
    keys = ("pc_vol", "pc_oi", "atm_iv", "skew25")
    today, hist, last = {}, {}, {}
    for tk, sub in df.groupby("ticker"):
        last[tk] = str(sub.date.iloc[-1])
        cur = sub.iloc[-1]
        today[tk] = {k: (float(cur[k]) if k in sub.columns and cur[k] == cur[k] else None)
                     for k in keys}
        # 历史**不含今天** —— 含了会把今天的极端值稀释进自己的基准, 百分位偏低
        prev = sub.iloc[:-1]
        hist[tk] = {k: [float(x) for x in prev[k] if x == x] if k in prev.columns else []
                    for k in keys}
    return today, hist, last, asof


def compute(max_lag_days: int = 3) -> dict:
    groups = _universe_groups()
    if not groups:
        return {"error": "行业分组读不出(data/industry_universe.txt)", "rows": []}
    today, hist, last, asof = load_daily()
    if not today:
        return {"error": "期权日汇总为空, 先跑 build_daily()", "rows": []}
    rows = _op.build(groups, today, hist, last_dates=last, max_lag_days=max_lag_days)
    # ⚠️ 报中位数不是最小值。个别新上市/停更票只有几天历史(实测 CTRA 6 天),
    #    用 min 会显示「历史=5天」, 让人以为整个百分位都不可信 —— 实际 442 只里
    #    有 400+ 只是满 58 天的。同时给出"够格排名"的票数, 这才是真正的可信度。
    hl = sorted(len(v.get("atm_iv") or []) for v in hist.values())
    n_hist = hl[len(hl) // 2] if hl else 0
    return {
        "rows": rows,
        "rank_iv": _op.rank_groups(rows, "atm_iv_pctl"),
        "rank_skew": _op.rank_groups(rows, "skew25_pctl"),
        "asof": asof,
        "n_groups": len(rows),
        "n_tickers": len(today),
        "hist_days": n_hist,
        "n_rankable": sum(1 for x in hl if x >= 20),
        "n_low_coverage": sum(1 for g in rows if g.get("low_coverage")),
        "stale": sorted(_op.stale_tickers(last, max_lag_days)),
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
        print(f"[optpulse] save err: {e}")


def load_cache():
    import json
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None


def get(force: bool = False) -> dict:
    """有缓存且不比日汇总旧就直接用。"""
    if not force:
        c = load_cache()
        if c and not c.get("error"):
            try:
                if DAILY.stat().st_mtime <= CACHE.stat().st_mtime:
                    return c
            except Exception:
                return c
    res = compute()
    if not res.get("error"):
        save(res)
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="增量汇总 EOD 链")
    ap.add_argument("--all", action="store_true", help="全量重算(慢)")
    a = ap.parse_args()
    if a.build or a.all:
        print(build_daily(days=chain_dates() if a.all else None))
    r = get(force=True)
    if r.get("error"):
        print("ERROR:", r["error"])
        raise SystemExit(1)
    print(f"as_of={r['asof']}  组={r['n_groups']}  票={r['n_tickers']}  "
          f"历史中位={r['hist_days']}天  够格排名={r['n_rankable']}只  "
          f"停更剔除={len(r['stale'])}  低覆盖组={r['n_low_coverage']}")
    by = {g["name"]: g for g in r["rows"]}

    def show(title, names):
        print(f"\n{title}")
        for n in names:
            g = by.get(n) or {}
            print(f"  {n[:20]:22} IV%={str(g.get('atm_iv_pctl')):>4} "
                  f"偏斜%={str(g.get('skew25_pctl')):>4} "
                  f"IV={g.get('atm_iv') and round(g['atm_iv'] * 100, 1):<6} "
                  f"{g.get('n')}/{g.get('n_listed')}  {g.get('priced_in', '')[:32]}")

    # ⚠️ 走 rank_iv 而不是 rows[:8] —— 排行榜要过覆盖率守卫。
    #    直接切 rows 的话「保险」(7 只票只覆盖 2 只小盘保险科技, IV 90%)会顶到榜首。
    show("最紧张(IV 百分位最高, 已排除低覆盖组):", [x["name"] for x in r["rank_iv"]["high"]])
    show("最松弛(IV 百分位最低, 已排除低覆盖组):", [x["name"] for x in r["rank_iv"]["low"]][:6])
    low = [g for g in r["rows"] if g.get("low_coverage")]
    if low:
        print(f"\n低覆盖组(仍在完整列表, 但不上榜) {len(low)} 个:")
        for g in sorted(low, key=lambda x: -(x.get("atm_iv_pctl") or 0))[:6]:
            print(f"  {g['name'][:20]:22} {g['n']}/{g['n_listed']} "
                  f"IV%={str(g.get('atm_iv_pctl')):>4} IV={round((g.get('atm_iv') or 0)*100,1)}")

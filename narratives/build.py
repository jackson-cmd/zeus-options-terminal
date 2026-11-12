#!/usr/bin/env python3
"""多空叙事构建例程(每天 8点/18点 launchd 跑)。

  个股票池 → 每票组装近30天新闻池 → 增量(hash未变跳过) → 分批调 Haiku → 写库。

用法:
  python build.py                         # 全个股, 增量
  python build.py --tickers MRNA,AAPL     # 指定票
  python build.py --limit 40              # 前40只(调试)
  python build.py --force                 # 忽略 hash, 全重算
  python build.py --batch-size 20
"""
from __future__ import annotations
import sys, os, argparse, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import universe, pool as poolmod, haiku, store, search_pool


def _evidence(p):
    ev = []
    ai = p.get("ai")
    if ai:
        ev.append({"date": "", "src": "finviz_ai", "title_cn": ai.get("title_cn"),
                   "title_en": ai.get("title_en"), "sent": ai.get("sent"),
                   "link": ai.get("link"),
                   "side": "bull" if (ai.get("sent") or 0) > 0 else "bear" if (ai.get("sent") or 0) < 0 else None})
    for n in p.get("finviz", []):
        s = n.get("sent") or 0
        ev.append({"date": n.get("date"), "raw": n.get("raw"), "src": "finviz",
                   "title_cn": n.get("title_cn"), "title_en": n.get("title_en"),
                   "sent": s, "link": n.get("link"),
                   "side": "bull" if s > 0 else "bear" if s < 0 else None})
    for b in p.get("benzinga", []):
        ev.append({"date": b.get("date"), "src": "benzinga", "title_en": b.get("title"),
                   "teaser": b.get("teaser"), "side": None})
    return ev


def build_one(ticker, date=None, deep=False, finviz_entry=None):
    """按需重建单票多空叙事(PWA 搜索 / 「🔄更新」按钮)。无视 hash 强制重算。
    返回 item dict, 无新闻/生成失败返 None。同步 ~10-15s(claude -p)。

    deep=True         → 走 search_pool 深度池(全文 + primary 分层, 取 30 篇)
    finviz_entry 给了 → 用服务端实时抓的 finviz(票池外的票, 本地库没有)
    ⚠️ 每日批量的 main() 不走这里, 仍用 pool.build_pools 的 600 字浅池省 token。
    """
    t = (ticker or "").strip().lstrip("$").upper()
    if not t:
        return None
    date = date or datetime.date.today().isoformat()
    if deep:
        pools = {t: search_pool.build_deep_pool(t, finviz_entry=finviz_entry)}
    else:
        pools = poolmod.build_pools([t])
    if not poolmod.has_news(pools[t]):
        return None
    res, usage, err = haiku.run_batch(pools, [t])
    r = res.get(t)
    if not r:
        return None
    item = {"ticker": t, "asof": datetime.datetime.now().isoformat(timespec="seconds"),
            "pool_hash": poolmod.pool_hash(pools[t]),
            "bull": r["bull"], "bear": r["bear"], "net": r["net"],
            "evidence": _evidence(pools[t])}
    store.upsert_day(date, [item])
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=8)   # 20→8: 批太大 Haiku 注意力被稀释,
    #                                                        会对部分票漏出多空(只给 net), 实测
    #                                                        18 只"无叙事"里 8 只是被 batch=20 漏的
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = universe.individual_stocks()
        if args.limit:
            tickers = tickers[:args.limit]

    print(f"[{datetime.datetime.now():%H:%M:%S}] 个股 {len(tickers)} 只, 组装新闻池…")
    pools = poolmod.build_pools(tickers)
    with_news = [t for t in tickers if poolmod.has_news(pools[t])]
    print(f"  有新闻: {len(with_news)} / {len(tickers)}")

    existing = {it.get("ticker"): it for it in (store.load_day(args.date) or [])}
    hashes = {t: poolmod.pool_hash(pools[t]) for t in with_news}
    if args.force:
        changed = with_news
    else:
        changed = [t for t in with_news if existing.get(t, {}).get("pool_hash") != hashes[t]]
    print(f"  需(重)生成: {len(changed)} (其余沿用上轮)")

    asof = datetime.datetime.now().isoformat(timespec="seconds")
    bs = args.batch_size
    tot_in = tot_out = 0
    done = []
    for i in range(0, len(changed), bs):
        batch = changed[i:i + bs]
        print(f"  [batch {i//bs+1}/{(len(changed)+bs-1)//bs}] {len(batch)} 票 → Haiku…", flush=True)
        res, usage, err = haiku.run_batch(pools, batch)
        if err:
            print(f"    ⚠️ {err} — 跳过本批(沿用上轮)")
            continue
        if usage:
            tot_in += (usage.get("cache_creation_input_tokens", 0) + usage.get("input_tokens", 0))
            tot_out += usage.get("output_tokens", 0)
        items = []
        for t in batch:
            r = res.get(t)
            if not r:
                continue
            items.append({"ticker": t, "asof": asof, "pool_hash": hashes[t],
                          "bull": r["bull"], "bear": r["bear"], "net": r["net"],
                          "evidence": _evidence(pools[t])})
        if items:
            store.upsert_day(args.date, items)
            done += [it["ticker"] for it in items]
        print(f"    ✓ {len(items)} 写入 (in≈{tot_in} out≈{tot_out} tok)")

    print(f"[{datetime.datetime.now():%H:%M:%S}] 完成: {len(done)} 票更新, 输入≈{tot_in} 输出≈{tot_out} tok")
    return done


if __name__ == "__main__":
    main()

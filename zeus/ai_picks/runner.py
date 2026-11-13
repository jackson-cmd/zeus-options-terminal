"""AI 选股编排: build_features → 40 并发 × 4 票/批 Haiku 分类 → 存当天缓存(+通知)。

用法(自动/命令行): python -m ai_picks.runner [--workers 40] [--batch-size 4] [--notify]
"""
from __future__ import annotations
import sys, time, datetime, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import features as F
from . import haiku_picks as H
from . import store
from . import notify as N


def _process_batch(batch, feats):
    t0 = time.time()
    res, usage, err = H.run_batch(feats, batch)
    return batch, res, usage, err, time.time() - t0


def run(date: str | None = None, tickers: list[str] | None = None,
        workers: int = 40, batch_size: int = 4, notify_on_done: bool = False,
        progress: dict | None = None, log=print) -> dict:
    date = date or datetime.date.today().isoformat()
    prog = progress if progress is not None else {}
    prog.update(running=True, pct=0, n_total=0, n_done=0, error=None,
                started=datetime.datetime.now().isoformat(timespec="seconds"))

    if tickers:
        log(f"[ai_picks] universe {len(tickers)} 只(显式传入), 装配特征…")
    else:
        base = F.parse_universe()
        tickers = F.effective_universe(base=base)
        log(f"[ai_picks] universe {len(base)} 只 + barchart 并入 {len(tickers) - len(base)} 只 "
            f"→ 共 {len(tickers)} 只, 装配特征…")
    feats = F.build_features(tickers, date)
    have = [t for t in tickers if t in feats]
    prog["n_total"] = len(have)
    log(f"[ai_picks] 有数据 {len(have)}/{len(tickers)} 只, 分 {batch_size} 票/批, {workers} 并发 → Haiku")

    batches = [have[i:i + batch_size] for i in range(0, len(have), batch_size)]
    picks: dict = {}
    fails, tot_in, tot_out = [], 0, 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_process_batch, b, feats): b for b in batches}
        for done_n, fut in enumerate(as_completed(futs), start=1):
            batch = futs[fut]
            try:
                _b, res, usage, err, dt = fut.result()
            except Exception as e:
                fails.append(batch)
                log(f"[ai_picks] batch {','.join(batch)} EXC: {e}")
            else:
                if err:
                    fails.append(batch)
                    log(f"[ai_picks] batch {','.join(batch)} ⚠️ {err[:80]} ({dt:.0f}s)")
                else:
                    picks.update(res)
                    if usage:
                        tot_in += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                        tot_out += usage.get("output_tokens", 0)
                    log(f"[ai_picks] [{done_n}/{len(batches)}] {','.join(batch)} → {len(res)} ({dt:.0f}s)")
            prog["n_done"] = len(picks)
            prog["pct"] = int(done_n / max(1, len(batches)) * 100)

    # 失败批串行重试一次(异常只记日志, 绝不外抛以免跳过后面的 store.save)
    for batch in fails:
        time.sleep(1.5)
        try:
            _b, res, usage, err, dt = _process_batch(batch, feats)
        except Exception as e:
            log(f"[ai_picks] retry {','.join(batch)} EXC: {e}")
            continue
        if not err and res:
            picks.update(res)
            log(f"[ai_picks] retry {','.join(batch)} → {len(res)}")
    prog["n_done"] = len(picks)

    store.save(date, picks, meta={"n_universe": len(tickers), "in_tok": tot_in, "out_tok": tot_out})
    total = time.time() - t0
    log(f"[ai_picks] 完成 {len(picks)}/{len(have)} 票, {total:.0f}s, in≈{tot_in} out≈{tot_out}")

    if notify_on_done:
        from collections import Counter
        c = Counter(v.get("verdict") for v in picks.values())
        bull = c.get("看涨·低点抄底", 0) + c.get("看涨·趋势追涨", 0)
        bear = c.get("看跌·高点阻力", 0) + c.get("看跌·颓势追跌", 0)
        N.notify("🚀 AI选股完成",
                 f"{len(picks)} 只 · 看涨 {bull} / 看跌 {bear} / 不交易 {c.get('不交易', 0)}")

    prog.update(running=False, pct=100,
                finished=datetime.datetime.now().isoformat(timespec="seconds"))
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--tickers", default="")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args()
    tks = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    run(date=args.date, tickers=tks, workers=args.workers,
        batch_size=args.batch_size, notify_on_done=args.notify)


if __name__ == "__main__":
    main()

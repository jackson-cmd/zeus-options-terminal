#!/usr/bin/env python3
"""
barchart/scrape_pages.py — 爬 Barchart 所有公开列表页（异动期权 / IV 排行 / 信号 / 榜单…）

原理见 core.py：拦截 proxies/core-api 的 XHR 响应拿结构化 JSON。

用法:
  python3 scrape_pages.py                          # 爬 pages.py 里全部
  python3 scrape_pages.py --tab options            # 只爬某 tab (options/stocks/etfs)
  python3 scrape_pages.py --prio 1                 # 只爬优先级<=1 的页
  python3 scrape_pages.py --only opt_highest_iv stk_new_recos
  python3 scrape_pages.py --list                   # 只列页面清单不爬
  python3 scrape_pages.py --debug                  # 打印拦截到的 API URL
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

import core
from pages import PAGES, TABS

BASE_DIR = Path(__file__).parent
import sys as _sys_hc
_sys_hc.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zeus_config as _hc  # noqa: E402
# Barchart login comes from Settings (written to ~/.zeus-terminal/barchart_credentials.csv)
CRED_FILE = _hc.barchart_credentials_file()
OUT_DIR = BASE_DIR / "downloads" / "pages"
SNAP_DIR = BASE_DIR / "snapshots"
CONCURRENCY = 5  # 别拉太高，避免 Barchart 限速 / 账号风控


async def scrape_one(context, key: str, cfg: dict, sem, progress, debug=False):
    async with sem:
        page = await context.new_page()
        captured: list = []
        page.on("response", core.make_listener(captured, debug, key))
        try:
            await page.goto(f"{core.BASE_URL}{cfg['url']}",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)

            # Screener 页(?screener=<id>): 结果需点 "See Results" 才查询; 否则只拿到 screener 定义列表
            if "stocks-screener" in cfg["url"]:
                try:
                    btn = page.locator(".bc-screener__see-results-button").first
                    if await btn.is_visible(timeout=6000):
                        captured.clear()  # 丢掉定义列表, 只留点击后的结果
                        await btn.click()
                        await asyncio.sleep(5)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=20000)
                        except PlaywrightTimeout:
                            pass
                        await asyncio.sleep(2)
                except Exception:
                    pass

            rows = await core.collect_with_pagination(page, captured, max_pages=8, label=key)
            if not rows:
                return {"key": key, "tab": cfg["tab"], "rows": 0, "tickers": 0,
                        "note": "无数据（会员墙/挑战页/空榜）"}

            df, path = core.save_csv(rows, OUT_DIR, key)
            tickers = core.extract_tickers(df)
            if tickers:
                (OUT_DIR / f"{key}_tickers.txt").write_text("\n".join(tickers), encoding="utf-8")
            return {"key": key, "tab": cfg["tab"], "rows": len(rows),
                    "tickers": len(tickers), "cols": len(df.columns), "file": path.name}
        except Exception as e:
            return {"key": key, "tab": cfg["tab"], "rows": 0, "tickers": 0, "note": f"ERR: {e}"}
        finally:
            await page.close()
            progress["done"] += 1
            d, t = progress["done"], progress["total"]
            el = time.time() - progress["t0"]
            import sys
            sys.stderr.write(f"\r  进度 {d}/{t}  {el:.0f}s  ")
            sys.stderr.flush()


async def async_main(only=None, tab=None, prio=None, debug=False, concurrency=CONCURRENCY):
    pages = dict(PAGES)
    if tab:
        pages = {k: v for k, v in pages.items() if v["tab"] == tab}
    if prio is not None:
        pages = {k: v for k, v in pages.items() if v.get("prio", 3) <= prio}
    if only:
        pages = {k: v for k, v in pages.items() if k in only}

    if not pages:
        print("没有匹配的页面"); return

    print(f"准备爬 {len(pages)} 个页面 (并发={concurrency}):")
    for k, v in pages.items():
        print(f"  [{v['tab']:>7}] {k:<24} {v['desc']}")
    print()

    user, pw = core.load_credentials(CRED_FILE)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900}, user_agent=core.CHROME_UA)

        login_page = await context.new_page()
        ok = await core.login(login_page, user, pw)
        await login_page.close()
        if not ok:
            print("✗ 登录失败（检查 credentials.csv）"); await browser.close(); return
        print("✓ 登录成功\n")

        sem = asyncio.Semaphore(concurrency)
        progress = {"done": 0, "total": len(pages), "t0": time.time()}
        results = await asyncio.gather(
            *[scrape_one(context, k, v, sem, progress, debug) for k, v in pages.items()])
        await browser.close()
    print()

    # ── 汇总 ──
    ok_rows = [r for r in results if r["rows"] > 0]
    empty = [r for r in results if r["rows"] == 0]
    print("=" * 70)
    print(f"{'页面':<26}{'tab':>8}{'行数':>7}{'ticker':>8}  说明")
    print("-" * 70)
    for r in sorted(results, key=lambda x: (-x["rows"], x["key"])):
        note = r.get("note", r.get("file", ""))
        print(f"{r['key']:<26}{r['tab']:>8}{r['rows']:>7}{r.get('tickers',0):>8}  {note}")
    print("=" * 70)
    print(f"✓ {len(ok_rows)}/{len(results)} 页拿到数据, 共 {sum(r['rows'] for r in ok_rows)} 行")
    if empty:
        print(f"✗ 空/失败 {len(empty)}: {', '.join(r['key'] for r in empty)}")
    print(f"产物: {OUT_DIR}/")

    # ── 每日 parquet 快照（ticker 历史，供下游消费）──
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        snap_rows = []
        for r in ok_rows:
            tf = OUT_DIR / f"{r['key']}_tickers.txt"
            if tf.exists():
                for t in tf.read_text(encoding="utf-8").split():
                    if t.strip():
                        snap_rows.append({"date": today, "page": r["key"],
                                          "tab": r["tab"], "ticker": t.strip()})
        if snap_rows:
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(snap_rows).to_parquet(SNAP_DIR / f"pages_{today}.parquet", index=False)
            print(f"快照: snapshots/pages_{today}.parquet ({len(snap_rows)} rows)")
    except Exception as e:
        print(f"快照跳过: {e}")


def main():
    ap = argparse.ArgumentParser(description="Barchart 列表页爬虫")
    ap.add_argument("--only", nargs="*", help="只爬指定 key")
    ap.add_argument("--tab", choices=TABS, help="只爬某 tab")
    ap.add_argument("--prio", type=int, help="只爬优先级 <= N 的页 (1最高)")
    ap.add_argument("--list", action="store_true", help="只列清单不爬")
    ap.add_argument("--debug", action="store_true", help="打印拦截到的 API URL")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = ap.parse_args()

    if args.list:
        for k, v in PAGES.items():
            print(f"[{v['tab']:>7}] prio{v['prio']} {k:<24} {v['url']:<48} {v['desc']}")
        print(f"\n共 {len(PAGES)} 页, tabs: {', '.join(TABS)}")
        return

    asyncio.run(async_main(only=args.only, tab=args.tab, prio=args.prio,
                           debug=args.debug, concurrency=args.concurrency))


if __name__ == "__main__":
    main()

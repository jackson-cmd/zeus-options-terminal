#!/usr/bin/env python3
"""
Barchart Screener Scraper

自动发现账号里所有Saved Screeners，逐个选择→See Results→拦截API→保存CSV。
用法:
  python3 scrape_screener.py              # 跑账号里所有saved screeners
  python3 scrape_screener.py --list       # 只列出screener名称，不跑
  python3 scrape_screener.py --name "bull spread"  # 只跑指定名称的screener
"""

import asyncio
import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_DIR = Path(__file__).parent
import sys as _sys_hc
_sys_hc.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zeus_config as _hc  # noqa: E402
CREDENTIALS_FILE = _hc.barchart_credentials_file()   # from Settings, outside the repo
DOWNLOAD_DIR = BASE_DIR / "downloads" / "screeners"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
LOGIN_URL = "https://www.barchart.com/login"
SCREENER_URL = "https://www.barchart.com/stocks/stocks-screener"

JS_GET_SAVED_SCREENERS = """
() => {
    const selects = document.querySelectorAll('select[name="screenerSavedPresets"]');
    if (selects.length === 0) return [];
    const sel = selects[0];
    const items = [];
    for (let i = 0; i < sel.options.length; i++) {
        const opt = sel.options[i];
        const val = opt.value;
        const text = opt.text.trim();
        if (val && text && val !== '' && text !== 'Saved Screeners') {
            items.push({index: i, value: val, name: text});
        }
    }
    return items;
}
"""


def load_credentials():
    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        row = next(reader)
        return row["username"].strip(), row["password"].strip()


async def login_barchart(page, username, password):
    print("登录 Barchart...")  # 不打印账号邮箱
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    await page.locator('input[name="email"], input[type="email"]').first.fill(username)
    await page.locator('input[name="password"], input[type="password"]').first.fill(password)
    await page.locator('button[type="submit"], input[type="submit"]').first.click()
    await asyncio.sleep(5)
    if "login" in page.url.lower():
        return False
    print("  登录成功!")
    return True


def extract_best_rows(api_data):
    """从API响应中提取列数最多的数据"""
    best_rows = []
    best_cols = 0

    for entry in api_data:
        data = entry["data"]
        if not isinstance(data, dict):
            continue
        raw_data = data.get("data")
        if isinstance(raw_data, dict):
            items = []
            for v in raw_data.values():
                if isinstance(v, list):
                    items.extend(v)
            rows_list = items
        elif isinstance(raw_data, list):
            rows_list = raw_data
        else:
            continue
        if not rows_list:
            continue

        if isinstance(rows_list[0], dict) and "raw" in rows_list[0]:
            clean = [r["raw"] for r in rows_list if isinstance(r.get("raw"), dict)]
        else:
            clean = [r for r in rows_list if isinstance(r, dict)]
        if not clean:
            continue

        n_cols = len(clean[0])
        total = data.get("total", len(clean))
        if n_cols > best_cols or (n_cols == best_cols and len(clean) > len(best_rows)):
            best_rows = clean
            best_cols = n_cols

    return best_rows


async def scrape_single_screener(page, screener_name, screener_index):
    """选择一个screener下拉选项→点See Results→拦截API数据"""
    safe_name = re.sub(r'[^\w\-]', '_', screener_name)

    api_data = []

    async def on_response(response):
        resp_url = response.url
        if ("proxies/core-api" in resp_url or "api/v1" in resp_url) and response.status == 200:
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    api_data.append({"url": resp_url, "data": body})
            except Exception:
                pass

    page.on("response", on_response)
    try:
        # 选择下拉选项（通过index设置select）
        await page.evaluate(f"""
            () => {{
                const sel = document.querySelector('select[name="screenerSavedPresets"]');
                if (!sel) return;
                sel.selectedIndex = {screener_index};
                sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        """)
        await asyncio.sleep(3)

        # 点击 See Results
        see_results = page.locator('text="See Results"').first
        try:
            if await see_results.is_visible(timeout=5000):
                await see_results.click()
                await asyncio.sleep(5)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeout:
                    pass
                await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(5)

        # 提取数据
        all_rows = extract_best_rows(api_data)
        if not all_rows:
            await asyncio.sleep(5)
            all_rows = extract_best_rows(api_data)

        if not all_rows:
            print(f"  [{screener_name}] 无数据")
            return None

        # 翻页
        total_from_api = 0
        for entry in api_data:
            d = entry.get("data", {})
            if isinstance(d, dict) and d.get("total"):
                t = d["total"]
                if isinstance(t, int) and t > total_from_api:
                    total_from_api = t

        page_num = 1
        while total_from_api > 0 and len(all_rows) < total_from_api:
            next_btn = page.locator('.pagination-controls a.next').first
            try:
                visible = await next_btn.is_visible(timeout=3000)
            except Exception:
                visible = False
            if not visible:
                break

            api_data.clear()
            await next_btn.click()
            await asyncio.sleep(4)

            new_rows = extract_best_rows(api_data)
            if not new_rows:
                await asyncio.sleep(3)
                new_rows = extract_best_rows(api_data)
            if not new_rows:
                break
            page_num += 1
            all_rows.extend(new_rows)
            print(f"  [{screener_name}] 第{page_num}页: +{len(new_rows)}, 累计 {len(all_rows)}/{total_from_api}")

        # 保存
        csv_path = DOWNLOAD_DIR / f"{safe_name}.csv"
        df = pd.DataFrame(all_rows)
        df.to_csv(csv_path, index=False, encoding="utf-8")

        # 提取ticker列
        ticker_col = None
        for col in ["symbol", "baseSymbol", "ticker", "Symbol"]:
            if col in df.columns:
                ticker_col = col
                break
        tickers = []
        if ticker_col:
            tickers = df[ticker_col].dropna().unique().tolist()
            ticker_path = DOWNLOAD_DIR / f"{safe_name}_tickers.txt"
            with open(ticker_path, "w") as f:
                f.write("\n".join(str(t) for t in tickers))

        return {"name": screener_name, "count": len(all_rows), "tickers": len(tickers), "file": csv_path.name}

    except Exception as e:
        return None
    finally:
        page.remove_listener("response", on_response)


CONCURRENCY = 6  # 同时开几个tab


async def _scrape_one(context, sc, semaphore, progress):
    """在独立page上跑单个screener（受semaphore控制）"""
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(SCREENER_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)

            r = await scrape_single_screener(page, sc["name"], sc["index"])
            return r
        except Exception as e:
            return None
        finally:
            await page.close()
            progress["done"] += 1
            total = progress["total"]
            done = progress["done"]
            elapsed = __import__("time").time() - progress["t0"]
            per = elapsed / done if done else 0
            eta = per * (total - done)
            bar_len = 30
            filled = int(bar_len * done / total)
            bar = "█" * filled + "░" * (bar_len - filled)
            __import__("sys").stderr.write(f"\r  Barchart {bar} {done}/{total}  {elapsed:.0f}s elapsed  ETA {eta:.0f}s  ")
            __import__("sys").stderr.flush()


async def async_main(list_only=False, name_filter=None, concurrency=CONCURRENCY):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    username, password = load_credentials()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # 登录（用临时page）
        login_page = await context.new_page()
        login_ok = await login_barchart(login_page, username, password)
        if not login_ok:
            print("登录失败")
            await browser.close()
            return

        # 在登录page上获取screener列表
        print("\n获取 Screener 列表...")
        await login_page.goto(SCREENER_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(8)
        try:
            await login_page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(3)

        screeners = await login_page.evaluate(JS_GET_SAVED_SCREENERS)
        await login_page.close()

        print(f"\n找到 {len(screeners)} 个 Saved Screeners:")
        for s in screeners:
            print(f"  [{s['index']:>2}] {s['name']}")

        if list_only:
            await browser.close()
            return

        # 筛选
        if name_filter:
            filters = [n.strip().lower() for n in name_filter]
            screeners = [s for s in screeners if any(f in s["name"].lower() for f in filters)]
            print(f"\n筛选后: {len(screeners)} 个")

        # 并发跑
        import time as _time
        print(f"\n并发爬取 {len(screeners)} 个 screener (并发={concurrency})...")
        semaphore = asyncio.Semaphore(concurrency)
        progress = {"done": 0, "total": len(screeners), "t0": _time.time()}
        tasks = [_scrape_one(context, sc, semaphore, progress) for sc in screeners]
        raw_results = await asyncio.gather(*tasks)
        print()  # 换行结束进度条

        await browser.close()

    # 清理已删除的 screener 残留文件
    if not name_filter:
        scraped_keys = set()
        for sc in screeners:
            scraped_keys.add(re.sub(r'[^\w\-]', '_', sc["name"]))
        for old_f in DOWNLOAD_DIR.glob("*_tickers.txt"):
            key = old_f.stem.replace("_tickers", "")
            if key not in scraped_keys:
                csv_f = DOWNLOAD_DIR / f"{key}.csv"
                old_f.unlink(missing_ok=True)
                csv_f.unlink(missing_ok=True)
                print(f"  [清理] 已删除旧screener: {key}")

    # 汇总
    results = [r for r in raw_results if r]
    print(f"\n{'='*60}")
    print(f"Screener Results Summary")
    print(f"{'='*60}")
    total_tickers = 0
    for r in sorted(results, key=lambda x: -x["count"]):
        total_tickers += r["tickers"]
        print(f"  {r['name']:<40} {r['count']:>4} rows  {r['tickers']:>4} tickers  → {r['file']}")
    print(f"{'='*60}")
    print(f"  共 {len(results)} 个screener, {total_tickers} 个ticker (含重复)")
    print(f"  CSV保存在: {DOWNLOAD_DIR}/")
    print(f"{'='*60}")

    # ── Daily parquet snapshot for history (replaces old HTML/CSV reports) ──
    try:
        if results:
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            rows = []
            for r in results:
                safe = re.sub(r'[^\w\-]', '_', r["name"])
                ticker_path = DOWNLOAD_DIR / f"{safe}_tickers.txt"
                if not ticker_path.exists():
                    continue
                tickers = [t.strip() for t in ticker_path.read_text().split() if t.strip()]
                for t in tickers:
                    rows.append({"date": today, "screener": r["name"], "ticker": t})
            if rows:
                snap_df = pd.DataFrame(rows)
                snap_path = SNAPSHOTS_DIR / f"{today}.parquet"
                snap_df.to_parquet(snap_path, index=False)
                print(f"  Snapshot: {snap_path} ({len(rows)} rows)")
    except Exception as e:
        print(f"  Snapshot skipped: {e}")


def main():
    parser = argparse.ArgumentParser(description="Barchart Screener Scraper")
    parser.add_argument("--list", action="store_true", help="只列出screener名称")
    parser.add_argument("--name", nargs="*", help="只跑包含指定关键词的screener")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help=f"并发数 (默认{CONCURRENCY})")
    args = parser.parse_args()
    asyncio.run(async_main(list_only=args.list, name_filter=args.name, concurrency=args.concurrency))


if __name__ == "__main__":
    main()

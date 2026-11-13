#!/usr/bin/env python3
"""scrape_history.py — 复用 barchart 登录,爬每票 options-history 每日量 → history.parquet。

原理见 barchart/core.py:不解析会变的 DOM,而是拦截页面发往 /proxies/core-api 的 XHR
响应直接拿结构化 JSON。这里目标端点是
  GET /proxies/core-api/v1/options-historical/get?symbol={T}&fields=...
返回 ~65 个交易日(≈3 个月)行,每行取 `raw` dict,抽 8 个字段。

**不改 barchart/ 任何文件**:把 barchart 目录加进 sys.path 后 `import core`(仿 scrape_pages.py)。

用法(从 options_analyzer/ 目录):
  python -m options_vol.scrape_history          # 全 universe(440 票,并发 5,约 5-8 分钟)

产物:
  data/options_vol/history.parquet   长表 ~440×65
  data/options_vol/scrape_meta.json  生成时间/成功失败计数
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 复用 barchart.core(绝不改原码):把 barchart 目录前插 sys.path 再 import ──
PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent.parent              # options_analyzer/options_vol → 仓库根
BARCHART_DIR = REPO_ROOT / "barchart"
sys.path.insert(0, str(BARCHART_DIR))
import core  # noqa: E402  (来自 barchart/core.py)

from options_vol.universe import load_universe  # noqa: E402

# ── 路径 ──
DATA_DIR = REPO_ROOT / "data" / "options_vol"
HISTORY_PARQUET = DATA_DIR / "history.parquet"
META_JSON = DATA_DIR / "scrape_meta.json"

import sys as _sys_hc
_sys_hc.path.insert(0, str(Path(__file__).resolve().parents[2]))
import zeus_config as _hc  # noqa: E402
# ── Barchart login comes from Settings (~/.zeus-terminal/barchart_credentials.csv) ──
CRED_FILE = _hc.barchart_credentials_file()

CONCURRENCY = 5  # 别拉太高,避免 Barchart 限速 / 账号风控(沿用 scrape_pages.py)

# parquet 列顺序(严格遵守数据契约,见 spec「数据契约」)
COLUMNS = [
    "ticker", "date", "totalVolume", "totalOpenInterest",
    "weightedImpliedVolatility", "impliedVolatilityRank1y",
    "putCallVolumeRatio", "putCallOpenInterestRatio", "historicalLastPrice",
]
# 取整数的字段(成交量/未平仓);其余为 float
_INT_FIELDS = {"totalVolume", "totalOpenInterest"}
_FLOAT_FIELDS = {
    "weightedImpliedVolatility", "impliedVolatilityRank1y",
    "putCallVolumeRatio", "putCallOpenInterestRatio", "historicalLastPrice",
}


def _coerce_int(v):
    """转 int,缺失/非法 → None。"""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _coerce_float(v):
    """转 float,缺失/非法 → None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _find_history_response(captured: list) -> list | None:
    """从拦截响应里找 options-historical/get 那条,返回它的 data['data'] 列表。

    注意 core.make_listener 也会抓到 ads.adthrive.com 的 /api/v1 噪声,所以严格按
    url 含 'options-historical/get' 过滤,而非用 core.extract_best_rows。
    """
    for c in captured:
        if "options-historical/get" in c.get("url", ""):
            data = c.get("data")
            if isinstance(data, dict):
                rows = data.get("data")
                if isinstance(rows, list):
                    return rows
    return None


def _parse_rows(raw_rows: list, ticker: str) -> list[dict]:
    """把 core-api 行列表(每行套一层 raw)解析成契约长表行,打上 ticker。"""
    out: list[dict] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        raw = r.get("raw") if isinstance(r.get("raw"), dict) else r  # 优先 raw(数值)
        date = raw.get("date")
        if not date:
            continue
        row = {"ticker": ticker, "date": str(date)}
        for f in _INT_FIELDS:
            row[f] = _coerce_int(raw.get(f))
        for f in _FLOAT_FIELDS:
            row[f] = _coerce_float(raw.get(f))
        out.append(row)
    return out


async def _scrape_one(context, ticker: str, sem, progress, results: list):
    """爬单票:新开 page → 挂 listener → goto options-history → 抓 options-historical/get。

    per-ticker try/except,单票失败不拖垮整批;记录 0 行 / 报错票。
    """
    async with sem:
        page = await context.new_page()
        captured: list = []
        page.on("response", core.make_listener(captured, False, ticker))
        status = {"ticker": ticker, "rows": 0, "note": ""}
        try:
            await page.goto(f"{core.BASE_URL}/stocks/quotes/{ticker}/options-history",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)

            raw_rows = _find_history_response(captured)
            if raw_rows is None:
                # 偶发慢:再等一拍重试一次
                await asyncio.sleep(3)
                raw_rows = _find_history_response(captured)

            if not raw_rows:
                status["note"] = "无数据(会员墙/空/未捕获)"
            else:
                parsed = _parse_rows(raw_rows, ticker)
                results.extend(parsed)
                status["rows"] = len(parsed)
        except Exception as e:  # noqa: BLE001  单票兜底,记录后继续
            status["note"] = f"ERR: {type(e).__name__}: {e}"
        finally:
            await page.close()
            progress["done"] += 1
            d, t = progress["done"], progress["total"]
            el = time.time() - progress["t0"]
            sys.stderr.write(f"\r  进度 {d}/{t}  {el:.0f}s  {ticker:<8}({status['rows']}行) ")
            sys.stderr.flush()
        return status


async def scrape(tickers: list[str], concurrency: int = CONCURRENCY) -> list[dict]:
    """登录一次 → 并发爬全部 tickers → 返回长表行 list[dict](契约列)。

    成功/失败统计写进模块级 `scrape.last_stats`(供 main 落 meta.json)。
    """
    user, pw = core.load_credentials(CRED_FILE)
    rows: list[dict] = []
    statuses: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900}, user_agent=core.CHROME_UA)
        try:
            login_page = await context.new_page()
            ok = await core.login(login_page, user, pw)
            await login_page.close()
            if not ok:
                raise RuntimeError("Barchart 登录失败(检查 credentials.csv)")
            print("✓ 登录成功", file=sys.stderr)

            sem = asyncio.Semaphore(concurrency)
            progress = {"done": 0, "total": len(tickers), "t0": time.time()}
            statuses = await asyncio.gather(
                *[_scrape_one(context, t, sem, progress, rows) for t in tickers])
        finally:
            await browser.close()
    print(file=sys.stderr)  # 进度行换行

    # 成功 = 至少抓到 1 行;失败 = 0 行
    failed = [s["ticker"] for s in statuses if s["rows"] == 0]
    scrape.last_stats = {  # type: ignore[attr-defined]
        "universe_size": len(tickers),
        "n_scraped": len(tickers) - len(failed),
        "n_failed": len(failed),
        "failed_tickers": sorted(failed),
    }
    return rows


def _write_outputs(rows: list[dict], stats: dict):
    """写 history.parquet(契约列序)+ scrape_meta.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=COLUMNS)  # 强制列顺序;空行也保证 schema
    df.to_parquet(HISTORY_PARQUET, index=False)

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),  # 本地时间 ISO8601
        "universe_size": stats["universe_size"],
        "n_scraped": stats["n_scraped"],
        "n_failed": stats["n_failed"],
        "failed_tickers": stats["failed_tickers"],
    }
    META_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return df, meta


def main():
    tickers = load_universe()
    print(f"准备爬 {len(tickers)} 票 options-history (并发={CONCURRENCY})", file=sys.stderr)
    rows = asyncio.run(scrape(tickers))
    stats = getattr(scrape, "last_stats", {
        "universe_size": len(tickers), "n_scraped": 0,
        "n_failed": len(tickers), "failed_tickers": sorted(tickers)})
    df, meta = _write_outputs(rows, stats)

    print("=" * 60)
    print(f"✓ history.parquet: {len(df)} 行, {df['ticker'].nunique() if len(df) else 0} 票")
    print(f"  universe={meta['universe_size']}  成功={meta['n_scraped']}  失败={meta['n_failed']}")
    if meta["failed_tickers"]:
        shown = ", ".join(meta["failed_tickers"][:30])
        more = "" if len(meta["failed_tickers"]) <= 30 else f" …(+{len(meta['failed_tickers'])-30})"
        print(f"  失败票: {shown}{more}")
    print(f"  → {HISTORY_PARQUET}")
    print(f"  → {META_JSON}")


if __name__ == "__main__":
    main()

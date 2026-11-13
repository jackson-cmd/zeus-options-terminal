#!/usr/bin/env python3
"""
barchart/core.py — Barchart 爬虫公共件

所有 Barchart 爬虫共享：登录、core-api 响应拦截、JSON 提取、翻页、导出。

核心思路（验证自旧 screener_scraper/screener.py）：
  不解析会变的 DOM 表格，而是拦截页面发往 /proxies/core-api 的 XHR 响应，
  直接拿结构化 JSON。一个监听器通吃所有列表页，对 Barchart 改版容忍度高。
"""
from __future__ import annotations

import asyncio
import csv
import re
from pathlib import Path

import pandas as pd
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

BASE_URL = "https://www.barchart.com"
LOGIN_URL = f"{BASE_URL}/login"
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ticker 列候选名（用于识别『真榜单』vs 聚合统计表）
TICKER_COLS = {"symbol", "baseSymbol", "ticker", "Symbol"}


# ---------------------------------------------------------------------------
# 凭证 & 登录
# ---------------------------------------------------------------------------
def load_credentials(cred_file: Path) -> tuple[str, str]:
    """读 credentials.csv（首行 username,password）。绝不打印内容。"""
    with open(cred_file, "r", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
        return row["username"].strip(), row["password"].strip()


async def login(page: Page, username: str, password: str) -> bool:
    """登录 Barchart。成功返回 True（URL 不再含 'login'）。"""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    await page.locator('input[name="email"], input[type="email"]').first.fill(username)
    await page.locator('input[name="password"], input[type="password"]').first.fill(password)
    await page.locator('button[type="submit"], input[type="submit"]').first.click()
    await asyncio.sleep(5)
    return "login" not in page.url.lower()


# ---------------------------------------------------------------------------
# core-api 响应拦截
# ---------------------------------------------------------------------------
def make_listener(captured: list, debug: bool = False, label: str = ""):
    """返回一个 on_response 回调，把 core-api 的 JSON 响应塞进 captured。

    用法：
        captured = []
        page.on("response", make_listener(captured, debug, key))
    """
    async def on_response(response):
        url = response.url
        if ("proxies/core-api" in url or "/api/v1" in url) and response.status == 200:
            ct = response.headers.get("content-type", "")
            if "json" in ct:
                try:
                    body = await response.json()
                    captured.append({"url": url, "data": body})
                    if debug:
                        print(f"  [{label}] API: {url[:120]}")
                except Exception:
                    pass

    return on_response


def extract_best_rows(captured: list) -> list[dict]:
    """从拦截到的 core-api 响应里取『列数最多』的那批行（即主数据表）。

    兼容两种响应形态：
      - data.data 是 list： [{...}, ...]
      - data.data 是分组 dict： {"groupKey": [...], ...}
    每行可能套一层 {"raw": {...}, "formatted": {...}}，优先取 raw（数值）。
    """
    best_rows: list[dict] = []
    best_key = (False, 0, 0)  # (含ticker列, 行数, 列数) — 让真正的榜单列表胜过聚合/单行表
    for entry in captured:
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        raw = data.get("data")
        if isinstance(raw, dict):
            rows_list = [x for v in raw.values() if isinstance(v, list) for x in v]
        elif isinstance(raw, list):
            rows_list = raw
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

        # signal-recommendations 等聚合端点返回 1 行无 ticker 的统计 → 永远输给真列表
        has_ticker = bool(TICKER_COLS & set(clean[0].keys()))
        key = (has_ticker, len(clean), len(clean[0]))
        if key > best_key:
            best_rows, best_key = clean, key
    return best_rows


def total_from_captured(captured: list) -> int:
    """从响应里读 total 字段（用于判断是否还要翻页）。"""
    best = 0
    for e in captured:
        d = e.get("data")
        if isinstance(d, dict) and isinstance(d.get("total"), int):
            best = max(best, d["total"])
    return best


async def collect_with_pagination(page: Page, captured: list,
                                  max_pages: int = 8, label: str = "") -> list[dict]:
    """首屏 + 翻页累积所有行。captured 由调用方挂的 listener 持续填充。

    翻页靠点击 `.pagination-controls a.next`；多数榜单单页即够。
    """
    rows = extract_best_rows(captured)
    if not rows:
        await asyncio.sleep(4)
        rows = extract_best_rows(captured)

    total = total_from_captured(captured)
    pages_done = 1
    while total and len(rows) < total and pages_done < max_pages:
        nxt = page.locator(".pagination-controls a.next").first
        try:
            if not await nxt.is_visible(timeout=3000):
                break
        except Exception:
            break
        captured.clear()
        try:
            await nxt.click()
        except Exception:
            break
        await asyncio.sleep(4)
        more = extract_best_rows(captured)
        if not more:
            await asyncio.sleep(3)
            more = extract_best_rows(captured)
        if not more:
            break
        rows.extend(more)
        pages_done += 1
        if label:
            print(f"  [{label}] 第{pages_done}页: 累计 {len(rows)}/{total}")
    return rows


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)


def save_csv(rows: list[dict], out_dir: Path, name: str):
    """写 CSV，返回 (DataFrame, path)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    path = out_dir / f"{name}.csv"
    df.to_csv(path, index=False, encoding="utf-8")
    return df, path


def extract_tickers(df: pd.DataFrame) -> list[str]:
    """从结果表里抽 ticker 列（兼容 symbol / baseSymbol / ticker / Symbol）。"""
    for col in ("symbol", "baseSymbol", "ticker", "Symbol"):
        if col in df.columns:
            return df[col].dropna().astype(str).str.strip().unique().tolist()
    return []

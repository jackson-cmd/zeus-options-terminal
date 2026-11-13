#!/usr/bin/env python3
"""screener_base.py —— 爬作者的 Barchart「base」筛选器 493417,落 screener_base.csv。

这是 universe 的「基底」:493417 是作者在 Barchart 维护的那个 saved screener,
周更跑一遍就拿到当前所有满足条件的票(及其 optionsTotalVolume / symbolType / hasOptions)。
refresh.py 用它 - (已爬过的票) 算出「新候选」再补爬 options-history。

原理见 barchart/core.py:不解析会变的 DOM,而是拦截页面发往 /proxies/core-api 的 XHR
响应直接拿结构化 JSON。screener 页(?screener=<id>)需点 "See Results" 才发查询请求,
否则只拿到 screener 定义列表 —— 所以先 captured.clear() 丢掉定义,再点按钮,再
core.collect_with_pagination 翻完所有页(列表可能 >100 票分多页)。

**不改 barchart/ 任何文件**:把 barchart 目录前插 sys.path 后 `import core`(仿 scrape_history.py)。

用法(从 options_analyzer/ 目录):
  python -m options_vol.screener_base

产物:
  data/options_vol/screener_base.csv   493417 全部行(symbol/symbolName/optionsTotalVolume/symbolType/hasOptions/…)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 复用 barchart.core(绝不改原码):把 barchart 目录前插 sys.path 再 import ──
PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent.parent              # options_analyzer/options_vol → 仓库根
BARCHART_DIR = REPO_ROOT / "barchart"
sys.path.insert(0, str(BARCHART_DIR))
import core  # noqa: E402  (来自 barchart/core.py)

# ── 路径 ──
DATA_DIR = REPO_ROOT / "data" / "options_vol"
SCREENER_BASE_CSV = DATA_DIR / "screener_base.csv"

import sys as _sys_hc
_sys_hc.path.insert(0, str(Path(__file__).resolve().parents[2]))
import zeus_config as _hc  # noqa: E402
# ── Barchart login comes from Settings (~/.zeus-terminal/barchart_credentials.csv) ──
CRED_FILE = _hc.barchart_credentials_file()

# Your own Barchart saved screener + view (ids from the screener URL). Optional.
SCREENER_ID = os.environ.get("ZEUS_BARCHART_SCREENER_ID", "")
VIEW_NAME = os.environ.get("ZEUS_BARCHART_VIEW_ID", "")
SCREENER_URL = (
    f"/stocks/stocks-screener?viewName={VIEW_NAME}&screener={SCREENER_ID}"
    "&orderBy=symbol&orderDir=asc"
)

# 至少要保证 CSV 里有这些列(契约;Barchart 实际可能多给)。缺的列补空,保证下游稳定。
REQUIRED_COLS = ["symbol", "symbolName", "optionsTotalVolume", "symbolType", "hasOptions"]


async def scrape_base() -> list[dict]:
    """登录一次 → 打开 493417 screener → 点 See Results → 翻完所有页 → 返回行 list[dict]。

    单页查询响应里每行套一层 {"raw":..., "formatted":...},core.extract_best_rows 已取 raw。
    """
    user, pw = core.load_credentials(CRED_FILE)
    rows: list[dict] = []

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

            page = await context.new_page()
            captured: list = []
            page.on("response", core.make_listener(captured, False, "screener_base"))

            await page.goto(f"{core.BASE_URL}{SCREENER_URL}",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)

            # Screener 页:结果需点 "See Results" 才查询;先 clear 丢掉 screener 定义列表
            clicked = False
            try:
                btn = page.locator(".bc-screener__see-results-button").first
                if await btn.is_visible(timeout=8000):
                    captured.clear()
                    await btn.click()
                    clicked = True
                    await asyncio.sleep(5)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except PlaywrightTimeout:
                        pass
                    await asyncio.sleep(2)
            except Exception as e:  # noqa: BLE001  按钮形态变了也别整批崩,继续用已捕获的
                print(f"  See Results 点击异常(忽略继续): {type(e).__name__}: {e}", file=sys.stderr)
            if not clicked:
                print("  ⚠️ 未点到 See Results 按钮,使用已捕获响应(可能是 0 行)", file=sys.stderr)

            rows = await core.collect_with_pagination(
                page, captured, max_pages=10, label="screener_base")
            await page.close()
        finally:
            await browser.close()

    return rows


def _write_csv(rows: list[dict]) -> pd.DataFrame:
    """落 screener_base.csv;保证 REQUIRED_COLS 都在(缺的补空、且排前)。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    # 缺失的契约列补空,保证下游(adr.load_name_map / refresh)稳定读到
    for c in REQUIRED_COLS:
        if c not in df.columns:
            df[c] = None
    # 契约列排前,其余列保持原序跟在后面
    ordered = REQUIRED_COLS + [c for c in df.columns if c not in REQUIRED_COLS]
    df = df[ordered]
    df.to_csv(SCREENER_BASE_CSV, index=False, encoding="utf-8")
    return df


def main():
    print(f"准备爬 Barchart screener {SCREENER_ID}(base)", file=sys.stderr)
    t0 = time.time()
    rows = asyncio.run(scrape_base())
    df = _write_csv(rows)
    n = len(df)
    n_tickers = df["symbol"].dropna().astype(str).str.strip().nunique() if n else 0
    print("=" * 60)
    print(f"✓ screener_base.csv: {n} 行, {n_tickers} 票  ({time.time()-t0:.0f}s)")
    print(f"  列: {', '.join(df.columns)}")
    print(f"  → {SCREENER_BASE_CSV}")


if __name__ == "__main__":
    main()

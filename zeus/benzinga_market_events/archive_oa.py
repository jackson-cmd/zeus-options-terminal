"""Archive today's unusual options activity for the idea watchlist into a
space-efficient, month-partitioned parquet. Run every 15 min during US market
hours (background job).

Why chunked + accumulate (user design 2026-06-04):
  * A single market-wide / big-list option_activity query is capped ~5000 by
    Benzinga — busy tickers (SNDK/MU/QQQ…) crowd out the rest, so small tickers
    (e.g. SCHW) get 0 rows. Querying the 700-ticker watchlist in chunks of 150
    keeps each chunk's daily trade count under the cap → complete per ticker
    (chunked got 16k distinct vs 5k for the single query).
  * Polling every 15 min and upserting (dedupe by `id`) backstops the cap for
    busy chunks late in the day: a trade is captured by the poll right after it
    happens (while still in that chunk's recent window).

Storage: keep only ~13 essential columns + zstd + one file per month →
  data/market_events/option_activity_YYYY-MM.parquet.

Usage: python -m benzinga_market_events.archive_oa [--force]
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from benzinga_api import calendar as cal  # noqa: E402

WL_FILE = REPO / "data" / "oa_watchlist.txt"   # 期权归档专用清单(~392 票, 作者 2026-06-04 指定)
OUT_DIR = REPO / "data" / "market_events"
LOG = OUT_DIR / "oa_archive.log"
CHUNK = 25      # 批25: 连高量票也单次完整(SMH=542✓/MU=1202; 批50 只 339/678)。16 批×~1.2s≈19s
THROTTLE = 1.2  # 分批间隔(秒): 14 批摊到 ~17s 而非爆发 → 防 429(底层 get_json 还有退避兜底)
KEEP = ["id", "ticker", "date", "time", "put_call", "sentiment", "cost_basis",
        "strike_price", "date_expiration", "option_activity_type", "size",
        "price", "underlying_price"]


def _log(m: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {m}"
    print(line, file=sys.stderr, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _market_hours() -> bool:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:        # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins <= 16 * 60 + 30   # 09:00–16:30 ET (含盘前盘后一点)


def _watchlist() -> list[str]:
    return [l.strip().upper() for l in WL_FILE.read_text().splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def fetch_today() -> list[dict]:
    """Chunked watchlist query for today; dedupe by id."""
    today = datetime.date.today().isoformat()
    wl = _watchlist()
    rows, seen = [], set()
    for i in range(0, len(wl), CHUNK):
        if i:
            time.sleep(THROTTLE)        # 节流: 分批不爆发, 防 429
        try:
            batch = cal.fetch("option_activity", tickers=",".join(wl[i:i + CHUNK]),
                              date_from=today, date_to=today, pagesize=20000)
        except Exception as e:
            _log(f"chunk {i // CHUNK} failed: {e}")
            continue
        for r in batch:
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(r)
    return rows


ARCHIVE = OUT_DIR / "option_activity_recent.parquet"  # 单文件滚动, 只留近一周
RETAIN_DAYS = 7   # 保留 今天 + 过去 7 天(作者: 就存一周)


def upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    for c in KEEP:
        if c not in df.columns:
            df[c] = None
    df = df[KEEP].copy()
    if ARCHIVE.exists():
        df = pd.concat([pd.read_parquet(ARCHIVE), df], ignore_index=True)
    df = df.drop_duplicates(subset=["id"], keep="last")
    cutoff = (datetime.date.today() - datetime.timedelta(days=RETAIN_DAYS)).isoformat()
    df = df[df["date"].astype(str) >= cutoff].reset_index(drop=True)   # 滚动: 丢掉一周前的
    df.to_parquet(ARCHIVE, compression="zstd", index=False)
    return len(df)


def main() -> int:
    force = "--force" in sys.argv
    if not force and not _market_hours():
        _log("skip: outside US market hours")
        return 0
    rows = fetch_today()
    n = upsert(rows)
    _log(f"fetched {len(rows)} watchlist trades today → archive now {n} rows (current month)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

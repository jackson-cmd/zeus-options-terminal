"""Parquet-backed storage for Benzinga news headlines — month-partitioned.

Files: `data/news_radar/news_YYYY-MM.parquet`, one per calendar month.
Each row is one (news_id, ticker) — items with N tickers explode to N rows.

Why partition by month:
  - Range queries (e.g. last 7 days) only need to open 1–2 month files
  - Easy archival / backup per month
  - File size stays bounded as history grows

Columns (per file):
    news_id    (int)
    created_at (UTC ISO str)
    title      (str)
    teaser     (str, ≤500 chars)
    ticker     (str, uppercase, exchange prefix stripped)
    sentiment  (int): +1 bullish / -1 bearish / 0 neutral
    source     (str)
    url        (str, nullable)
    updated_at (ISO timestamp of last write)

Upserts dedupe by (news_id, ticker) inside each month file. A news_id is
always slotted by its created_at month — never split across files.
"""
from __future__ import annotations

import datetime as _dt
import glob
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "news_radar"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Optional watchlist filter. If `data/news_radar/watchlist.txt` exists
# (one bare ticker per line, comments with #), the summary endpoint
# will hide everything outside it by default.
WATCHLIST_FILE = DATA_DIR / "watchlist.txt"


def load_watchlist() -> set[str]:
    """Return the user's ticker watchlist as an uppercased set. Empty set
    if the file doesn't exist or is empty."""
    if not WATCHLIST_FILE.exists():
        return set()
    out: set[str] = set()
    for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # accept "NYSE:RDDT" / "RDDT" / "rddt" alike
        t = line.split(":", 1)[-1].strip().upper()
        if t:
            out.add(t)
    return out

# Legacy single-file path (pre-partition). If present, _ensure_migrated()
# splits it into per-month files on first read/write.
LEGACY_PARQUET = DATA_DIR / "news.parquet"

COLUMNS = [
    "news_id", "created_at", "title", "teaser",
    "ticker", "sentiment", "source", "url", "updated_at",
]

_MONTH_RE = re.compile(r"news_(\d{4})-(\d{2})\.parquet$")


def _month_file(year: int, month: int) -> Path:
    return DATA_DIR / f"news_{year:04d}-{month:02d}.parquet"


def _month_of(iso_ts: str) -> tuple[int, int] | None:
    """Extract (year, month) from an ISO timestamp. None if unparseable."""
    if not iso_ts or len(iso_ts) < 7:
        return None
    try:
        y = int(iso_ts[:4])
        m = int(iso_ts[5:7])
        if 1 <= m <= 12:
            return y, m
    except ValueError:
        pass
    return None


def list_month_files() -> list[Path]:
    """All `news_YYYY-MM.parquet` files, chronologically sorted."""
    files = sorted(glob.glob(str(DATA_DIR / "news_*.parquet")))
    # filter to ones matching the exact pattern (skip e.g. backup files)
    return [Path(p) for p in files if _MONTH_RE.search(p)]


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def _ensure_migrated():
    """Split the legacy single-file news.parquet into per-month files (one-time)."""
    if not LEGACY_PARQUET.exists():
        return
    try:
        df = pd.read_parquet(LEGACY_PARQUET)
    except Exception:
        return
    if df.empty:
        LEGACY_PARQUET.unlink(missing_ok=True)
        return
    # Group by created_at year-month and write each chunk
    months: dict[tuple[int, int], list] = {}
    for _, row in df.iterrows():
        ym = _month_of(str(row.get("created_at") or ""))
        if not ym:
            continue
        months.setdefault(ym, []).append(row)
    for (y, m), rows in months.items():
        path = _month_file(y, m)
        chunk = pd.DataFrame(rows)
        for c in COLUMNS:
            if c not in chunk.columns:
                chunk[c] = pd.NA
        chunk = chunk[COLUMNS]
        if path.exists():
            existing = pd.read_parquet(path)
            chunk = pd.concat([existing, chunk], ignore_index=True).drop_duplicates(
                subset=["news_id", "ticker"], keep="last"
            ).reset_index(drop=True)
        chunk.to_parquet(path, compression="snappy", index=False)
    # Archive the legacy file (rename, don't delete, so we have a fallback)
    LEGACY_PARQUET.rename(LEGACY_PARQUET.with_suffix(".parquet.migrated"))


def _read_files(paths: list[Path]) -> pd.DataFrame:
    if not paths:
        return _empty_df()
    frames = []
    for p in paths:
        try:
            df = pd.read_parquet(p)
            for c in COLUMNS:
                if c not in df.columns:
                    df[c] = pd.NA
            frames.append(df[COLUMNS])
        except Exception:
            continue
    if not frames:
        return _empty_df()
    return pd.concat(frames, ignore_index=True)


def read_all() -> pd.DataFrame:
    """Read every month file and concatenate. Use this for full-history scans."""
    _ensure_migrated()
    return _read_files(list_month_files())


def read_window(days: int) -> pd.DataFrame:
    """Read only the month files that could contain rows ≥ cutoff(now - days)."""
    _ensure_migrated()
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=days)
    # Determine which (year, month) pairs to load. Iterate from cutoff month
    # to current month inclusive.
    needed: set[tuple[int, int]] = set()
    cur = _dt.date(cutoff.year, cutoff.month, 1)
    end = _dt.date(now.year, now.month, 1)
    while cur <= end:
        needed.add((cur.year, cur.month))
        # advance one month
        if cur.month == 12:
            cur = _dt.date(cur.year + 1, 1, 1)
        else:
            cur = _dt.date(cur.year, cur.month + 1, 1)
    paths = [_month_file(y, m) for (y, m) in sorted(needed) if _month_file(y, m).exists()]
    df = _read_files(paths)
    if df.empty:
        return df
    return df[df["created_at"] >= cutoff.isoformat()]


def _norm_ticker(t: str) -> str:
    """Drop exchange prefix and normalize case: 'NASDAQ:TSLA' → 'TSLA'."""
    if not t:
        return ""
    t = str(t).upper().strip()
    if ":" in t:
        t = t.split(":", 1)[-1]
    return t


def upsert_items(items: Iterable[dict]) -> int:
    """Insert/merge a list of Benzinga news items (raw MCP shape).

    Items are bucketed by their created_at month and written to the
    corresponding `news_YYYY-MM.parquet`. Each file dedupes by
    (news_id, ticker). Returns total rows written across files.
    """
    _ensure_migrated()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # year-month → list of rows
    buckets: dict[tuple[int, int], list[dict]] = {}
    for it in items:
        tickers = [_norm_ticker(t) for t in (it.get("tickers") or []) if t]
        tickers = [t for t in tickers if t]
        if not tickers:
            continue
        sentiment = it.get("sentiment")
        if sentiment is None:
            sentiment = 0
        created_at = it.get("created_at") or ""
        ym = _month_of(created_at)
        if not ym:
            continue
        for tk in tickers:
            buckets.setdefault(ym, []).append({
                "news_id": int(it.get("id") or 0),
                "created_at": created_at,
                "title": it.get("title") or "",
                "teaser": (it.get("teaser") or "")[:500],
                "ticker": tk,
                "sentiment": int(sentiment),
                "source": it.get("source") or "",
                "url": it.get("uri") or "",
                "updated_at": now,
            })

    total = 0
    for (y, m), rows in buckets.items():
        path = _month_file(y, m)
        new_df = pd.DataFrame(rows)[COLUMNS]
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, new_df], ignore_index=True)
            merged = combined.drop_duplicates(
                subset=["news_id", "ticker"], keep="last"
            ).reset_index(drop=True)
        else:
            merged = new_df
        merged.to_parquet(path, compression="snappy", index=False)
        total += len(rows)
    return total


def summary(days: int = 7, sentiment: str = "signaled",
            watchlist_only: bool = True) -> list[dict]:
    df = read_window(days)
    if df.empty:
        return []
    if sentiment == "signaled":
        df = df[df["sentiment"] != 0]
    elif sentiment == "bullish":
        df = df[df["sentiment"] == 1]
    elif sentiment == "bearish":
        df = df[df["sentiment"] == -1]
    if df.empty:
        return []
    if watchlist_only:
        wl = load_watchlist()
        if wl:
            df = df[df["ticker"].isin(wl)]
            if df.empty:
                return []
    df = df.drop_duplicates(subset=["news_id", "ticker"])
    out = []
    for ticker, sub in df.groupby("ticker"):
        bull = int((sub["sentiment"] == 1).sum())
        bear = int((sub["sentiment"] == -1).sum())
        neutral = int((sub["sentiment"] == 0).sum())
        out.append({
            "ticker": ticker,
            "count": int(len(sub)),
            "bull": bull,
            "bear": bear,
            "neutral": neutral,
            "latest_at": sub["created_at"].max(),
        })
    out.sort(key=lambda r: (-r["count"], r["ticker"]))
    return out


def by_ticker(ticker: str, days: int = 30, sentiment: str = "any") -> list[dict]:
    ticker = _norm_ticker(ticker)
    df = read_window(days)
    if df.empty:
        return []
    df = df[df["ticker"] == ticker]
    if df.empty:
        return []
    if sentiment == "signaled":
        df = df[df["sentiment"] != 0]
    elif sentiment == "bullish":
        df = df[df["sentiment"] == 1]
    elif sentiment == "bearish":
        df = df[df["sentiment"] == -1]
    df = df.sort_values("created_at", ascending=False)
    return [
        {
            "news_id": int(r.news_id),
            "created_at": r.created_at,
            "title": r.title,
            "teaser": r.teaser,
            "sentiment": int(r.sentiment),
            "source": r.source,
            "url": r.url,
        }
        for r in df.itertuples()
    ]


def recent_by_ticker(days: int = 2, max_per_ticker: int = 3,
                     watchlist_only: bool = False) -> dict:
    """Bulk per-ticker latest headlines for the heatmap "News" toggle.

    Returns {TICKER: [{news_id, title, sentiment, created_at, url}, ...]}
    with at most `max_per_ticker` newest items per ticker. A single network
    round-trip serves the entire table — no N+1 fetch per row.
    """
    df = read_window(days)
    if df.empty:
        return {}
    if watchlist_only:
        wl = load_watchlist()
        if wl:
            df = df[df["ticker"].isin(wl)]
            if df.empty:
                return {}
    df = df.drop_duplicates(subset=["news_id", "ticker"])
    df = df.sort_values("created_at", ascending=False)
    out: dict[str, list[dict]] = {}
    for r in df.itertuples():
        t = getattr(r, "ticker", None)
        if not t:
            continue
        bucket = out.setdefault(t, [])
        if len(bucket) >= max_per_ticker:
            continue
        bucket.append({
            "news_id": int(r.news_id),
            "title": r.title,
            "sentiment": int(r.sentiment),
            "created_at": r.created_at,
            "url": getattr(r, "url", "") or "",
        })
    return out


def stats() -> dict:
    _ensure_migrated()
    files = list_month_files()
    if not files:
        return {"rows": 0, "unique_news": 0, "unique_tickers": 0,
                "bytes_on_disk": 0, "months": []}
    df = _read_files(files)
    month_breakdown = []
    for p in files:
        m = _MONTH_RE.search(str(p))
        if not m:
            continue
        month = f"{m.group(1)}-{m.group(2)}"
        try:
            sub = pd.read_parquet(p)
            month_breakdown.append({
                "month": month,
                "rows": int(len(sub)),
                "unique_news": int(sub["news_id"].nunique()),
                "bytes": p.stat().st_size,
            })
        except Exception:
            continue
    return {
        "rows": int(len(df)),
        "unique_news": int(df["news_id"].nunique()) if not df.empty else 0,
        "unique_tickers": int(df["ticker"].nunique()) if not df.empty else 0,
        "bytes_on_disk": sum(p.stat().st_size for p in files),
        "date_range": ([df["created_at"].min(), df["created_at"].max()]
                       if not df.empty else None),
        "months": month_breakdown,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))

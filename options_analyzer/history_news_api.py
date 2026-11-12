"""History News tab — DuckDB-backed read-only API.

Two endpoints:
  GET /api/history-news/search       — paginated, body-less
  GET /api/history-news/article/{id} — full row including body

Data source: data/news_radar/benzinga_news_*.parquet (per-year files of a
Benzinga archive). DuckDB scans the glob lazily with predicate pushdown — no
eager load, no RAM-resident DataFrame.

Without that archive (the free setup), both endpoints search the news this
terminal has collected itself: Finviz, WSJ and MarketWatch headlines kept for
30 days in options_analyzer/data/bilingual_news.json. A ticker matches its
symbol or its company name.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

# ─── DuckDB lazy singleton ─────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ARCHIVE_DIR = _REPO_ROOT / "data" / "news_radar"
_PARQUET_GLOB = str(_ARCHIVE_DIR / "benzinga_news_*.parquet")
_FREE_STORE = _REPO_ROOT / "options_analyzer" / "data" / "bilingual_news.json"
FREE_NOTE = ("免费模式:在本机收集的新闻里搜索(Finviz、WSJ、MarketWatch,保留 30 天) · "
             "Free mode: searching the news this terminal has collected (Finviz, WSJ, MarketWatch; last 30 days)")
_NAME_SUFFIX = re.compile(r"[,\s]+(inc|corp|corporation|co|company|ltd|limited|plc|holdings?|group|"
                          r"n\.?v|s\.?a|ag|se|class [a-c]|the)\.?$", re.I)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

_con = None  # populated on first call


def _get_con():
    """Lazy single DuckDB connection — opened on first query."""
    global _con
    if _con is None:
        import duckdb
        _con = duckdb.connect(":memory:")
    return _con


# ─── Free fallback (no Benzinga archive) ────────────────────────────────────

def _has_archive() -> bool:
    try:
        return any(_ARCHIVE_DIR.glob("benzinga_news_*.parquet"))
    except OSError:
        return False


def _free_id(url: str) -> int:
    return int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:12], 16)


def _company_name(ticker: str) -> str:
    """'AAPL' → 'Apple' from the heatmap's company data; '' if unknown or too short."""
    try:
        from heatmap import metadata
        name = str((metadata.get_all().get(ticker) or {}).get("name") or "")
    except Exception:
        return ""
    prev = None
    while name and name != prev:
        prev = name
        name = _NAME_SUFFIX.sub("", name).strip(" ,.")
    name = re.sub(r"^the\s+", "", name, flags=re.I)
    return name if len(name) >= 4 and name.upper() != ticker else ""


def _free_articles() -> list[dict]:
    try:
        st = json.loads(_FREE_STORE.read_text(encoding="utf-8"))
    except Exception:
        return []
    gone = st.get("deleted") or {}
    out = []
    for a in st.get("articles") or []:
        url = a.get("url") or ""
        if not url or url in gone:
            continue
        ts = int(a.get("ts") or 0)
        when = (datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts
                else str(a.get("date") or ""))
        out.append({"id": _free_id(url), "title": a.get("title") or "", "teaser": a.get("teaser") or "",
                    "title_zh": a.get("titleZh") or "", "teaser_zh": a.get("teaserZh") or "",
                    "tickers": [], "sentiment": 0, "source": a.get("source") or "",
                    "type": a.get("section") or "", "published_at": when, "url": url})
    return out


def _free_search(ticker, keyword, since, until, sentiment, offset, limit) -> dict:
    items: list[dict] = []
    if sentiment not in ("bull", "bear"):          # free headlines carry no sentiment score
        pats = []
        if ticker:
            bare = rf"(?<![A-Za-z0-9$]){re.escape(ticker)}(?![A-Za-z0-9])|" if len(ticker) > 1 else ""
            pats.append(re.compile(bare + rf"\${re.escape(ticker)}\b"))   # one-letter symbols need "$V"
            name = _company_name(ticker)
            if name:   # "NVIDIA" in the company data is "Nvidia" in headlines; "Apple" stays case-sensitive
                pats.append(re.compile(rf"\b{re.escape(name)}\b", re.I if name.isupper() else 0))
        kw = keyword.lower() if keyword else None
        for a in _free_articles():
            text = a["title"] + " " + a["teaser"]
            if pats and not any(pt.search(text) for pt in pats):
                continue
            if kw and kw not in a["title"].lower() and kw not in a["title_zh"].lower():
                continue
            day = a["published_at"][:10]
            if (since and day < since[:10]) or (until and day >= until[:10]):
                continue
            items.append(a)
        items.sort(key=lambda x: x["published_at"], reverse=True)
    page = items[offset:offset + limit]
    cols = ("id", "title", "teaser", "tickers", "sentiment", "source", "type", "published_at")
    return {"items": [{**{k: a[k] for k in cols}, "tickers": [ticker] if ticker else []} for a in page],
            "total": len(items), "offset": offset, "limit": limit,
            "has_more": offset + len(page) < len(items), "archive": False, "note": FREE_NOTE}


def _free_article(article_id: int) -> dict:
    for a in _free_articles():
        if a["id"] == int(article_id):
            body = "\n\n".join(x for x in (a["teaser"], a["title_zh"], a["teaser_zh"],
                                             "Source / 原文: " + a["url"]) if x)
            return {"id": a["id"], "title": a["title"], "teaser": a["teaser"], "body": body, "tickers": [],
                    "sentiment": 0, "source": a["source"], "type": a["type"], "published_at": a["published_at"],
                    "url": a["url"]}
    raise KeyError(article_id)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _validate_ticker(ticker: str) -> str:
    """Upper-case, validate shape. Raise ValueError on bad input."""
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(t):
        raise ValueError(f"ticker must match [A-Z]{{1,5}} (got: {ticker!r})")
    return t


def _sentiment_clause(sentiment: Optional[str]) -> str:
    """Return SQL fragment for the sentiment filter, or '' if any."""
    if sentiment in (None, "", "any"):
        return ""
    if sentiment == "bull":
        return "AND sentiment > 0"
    if sentiment == "bear":
        return "AND sentiment < 0"
    if sentiment == "neutral":
        return "AND sentiment = 0"
    raise ValueError(f"sentiment must be one of bull/bear/neutral/any (got: {sentiment!r})")


def _row_to_dict(row, cols) -> dict:
    """Convert a DuckDB row tuple to a JSON-safe dict.

    DuckDB returns tickers as a Python list already; published_at comes
    back as a datetime — serialize to ISO 8601 with trailing Z (UTC).
    """
    out = {}
    for col, val in zip(cols, row):
        if col == "published_at" and val is not None:
            out[col] = val.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            out[col] = val
    return out


# ─── Core functions ────────────────────────────────────────────────────────

def _history_news_search(
    ticker: Optional[str] = None,
    keyword: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    sentiment: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Search the Benzinga archive. Spec §3.2 / §3.3.

    Returns {items, total, offset, limit, has_more}.
    Raises ValueError on bad input (router translates to HTTP 400).
    """
    ticker_norm = _validate_ticker(ticker) if ticker else None
    keyword = (keyword or "").strip() or None

    if not ticker_norm and not keyword:
        raise ValueError("must supply at least one of ticker, keyword")

    if since and until and since > until:
        raise ValueError(f"since ({since}) must be <= until ({until})")

    sent_sql = _sentiment_clause(sentiment)
    limit = max(1, min(int(limit), 200))
    offset = max(0, min(int(offset), 50_000))

    if not _has_archive():
        return _free_search(ticker_norm, keyword, since, until, sentiment, offset, limit)

    # Build WHERE incrementally so unused predicates don't pollute the plan
    where_parts = []
    params: list = []

    if ticker_norm:
        where_parts.append(
            "(list_contains(tickers, ?) OR regexp_matches(title, ?))"
        )
        params += [ticker_norm, fr"\b{ticker_norm}\b"]
    if keyword:
        where_parts.append("lower(title) LIKE ?")
        params.append(f"%{keyword.lower()}%")
    if since:
        where_parts.append("published_at >= CAST(? AS TIMESTAMP)")
        params.append(since)
    if until:
        where_parts.append("published_at < CAST(? AS TIMESTAMP)")
        params.append(until)

    where = " AND ".join(where_parts)
    if sent_sql:
        where = where + " " + sent_sql

    con = _get_con()
    # Count total (cheap; DuckDB caches the parquet metadata after first scan)
    total_sql = f"SELECT COUNT(*) FROM read_parquet('{_PARQUET_GLOB}') WHERE {where}"
    total = con.execute(total_sql, params).fetchone()[0]

    # Page
    cols = ["id", "title", "teaser", "tickers", "sentiment", "source", "type", "published_at"]
    page_sql = (
        f"SELECT {', '.join(cols)} "
        f"FROM read_parquet('{_PARQUET_GLOB}') "
        f"WHERE {where} "
        f"ORDER BY published_at DESC "
        f"LIMIT {limit} OFFSET {offset}"
    )
    rows = con.execute(page_sql, params).fetchall()
    items = [_row_to_dict(r, cols) for r in rows]

    return {
        "items": items,
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


def _history_news_article(article_id: int) -> dict:
    """Point-lookup by Benzinga NodeID — returns the full row including body.

    Raises KeyError if no row matches (router → HTTP 404).
    """
    if not _has_archive():
        return _free_article(article_id)
    cols = ["id", "title", "teaser", "body", "tickers", "sentiment",
            "source", "type", "published_at"]
    sql = (
        f"SELECT {', '.join(cols)} "
        f"FROM read_parquet('{_PARQUET_GLOB}') "
        f"WHERE id = ? LIMIT 1"
    )
    row = _get_con().execute(sql, [int(article_id)]).fetchone()
    if row is None:
        raise KeyError(article_id)
    return _row_to_dict(row, cols)


# ─── FastAPI router ────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/history-news", tags=["history-news"])


@router.get("/search")
def search_endpoint(
    ticker: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None, max_length=100),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    sentiment: Optional[str] = Query(None),
    offset: int = Query(0, ge=0, le=50_000),
    limit: int = Query(50, ge=1, le=200),
):
    try:
        return _history_news_search(
            ticker=ticker, keyword=keyword,
            since=since, until=until,
            sentiment=sentiment, offset=offset, limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/article/{article_id}")
def article_endpoint(article_id: int):
    try:
        return _history_news_article(article_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"article {article_id} not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

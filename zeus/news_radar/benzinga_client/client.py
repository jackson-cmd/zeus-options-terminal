"""
Benzinga data access layer.

Two kinds of data fetch here:

1. REST notification inbox  (sync, via httpx)
   api.benzinga.com/api/v1.1/notification/history
   → the user's last ~100 watchlist alerts.
   → the `offset` query param is IGNORED server-side, so only the most
     recent page is reachable — we only fetch one page and rely on dedup
     because the raw feed has the same article echoed per portfolio.

2. News DB WebSocket search  (async, via `websockets`)
   wss://api-pro.benzinga.com/api/v3/news/advanced/ws
   → searches Benzinga's news database for ANY ticker/time/keyword,
     not limited to the user's watchlist.
   → payload schema uses Capitalized keys (Title, CreatedAt, Tickers…);
     we normalize to lower-case internal shape via simplify_ws_item.

All helper functions below (dedupe, filters, simplify) are pure — tested
in tests/test_client_units.py without touching the network.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from .cookies import load_cookie_jar, COOKIES_PATH

# ---------- constants ----------

NOTIFICATIONS_URL = "https://api.benzinga.com/api/v1.1/notification/history"
NEWS_WS_URL = "wss://api-pro.benzinga.com/api/v3/news/advanced/ws"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HTTP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": _UA,
    "Referer": "https://pro.benzinga.com/",
    "Origin": "https://pro.benzinga.com",
}

# ---- Source-type groups ----
# These map user-facing source names to Benzinga's `Type` field values,
# derived from inspecting Benzinga Pro's own UI-driven WS queries.
#
# Groups are intentionally granular so the user can opt in/out of noisy
# aggregators (tipranks, cnbc algo-summaries) vs. signal sources (Benzinga's
# own editorial, WSJ/Barrons/investing.com, PR wires, SEC filings).

# Benzinga's own wire — English only. Translations (`benzinga_wire_spanish`
# etc.) are always stripped by `english_only`, so we intentionally exclude
# them here. `benzinga_wire_english` is included in case Benzinga ever uses
# it as a distinct type (currently everything English is just `story`).
BENZINGA_TYPES = ["story", "benzinga_wire_english", "benzinga_partner"]
# Top-tier English financial/business media that Benzinga syndicates.
MAJOR_MEDIA_TYPES = [
    "barrons", "investingcom", "thewallstreetjournal",
    "nytimes", "bloombergbusiness", "businessinsider",
]
CNBC_TYPES = ["cnbc"]
TIPRANKS_TYPES = ["tipranks_headlines"]
SEC_FILING_TYPES = ["pr_secfilings"]
PRESS_RELEASE_TYPES = [
    "pr_story",
    "businesswire_story",
    "globenewswire_story",
    "marketwire_story",
    "accesswire_pr",
    "prweb_story",
    "newswire_story",
    "newswire_pr",
    "newsfile_story",
    "thenewswire_pr",
    "abnewswire",
    "acnnewswire_story",
    "bz_pr_thomson_reuters",
    "comtex_story",
    "kisspr",
    "newsdirect_pr",
    "webwire_story",
]

SOURCE_GROUPS: dict[str, list[str]] = {
    "benzinga":      BENZINGA_TYPES,
    "major_media":   MAJOR_MEDIA_TYPES,
    "cnbc":          CNBC_TYPES,
    "tipranks":      TIPRANKS_TYPES,
    "sec_filings":   SEC_FILING_TYPES,
    "press_release": PRESS_RELEASE_TYPES,
}

# Hard-coded high-signal default per user policy.
# Includes Benzinga editorial, major financial media, SEC filings, and
# PR wires. EXCLUDED by default: cnbc (aggregated) and tipranks (algo).
DEFAULT_SOURCE_GROUPS = ["benzinga", "major_media", "sec_filings", "press_release"]
DEFAULT_SOURCE_TYPES: list[str] = [
    t for g in DEFAULT_SOURCE_GROUPS for t in SOURCE_GROUPS[g]
]

# Translated-wire types — pushed to the server as a `not Type any [...]`
# exclusion so we never receive non-English duplicates. Enumerated from
# observed traffic; stays up-to-date because the WS filter with `sources`
# default already excludes them by whitelist, this is belt-and-suspenders
# for `sources=["all"]` mode.
TRANSLATED_TYPES = [
    "benzinga_wire_spanish",
    "benzinga_wire_german",
    "benzinga_wire_italian",
    "benzinga_wire_japanese",
    "benzinga_wire_japan",
    "benzinga_wire_korean",
    "benzinga_wire_korea",
    "benzinga_wire_french",
    "benzinga_wire_chinese_simplified",
    "benzinga_wire_arabic",
    "globe_newswire_spanish",
    "globe_newswire_french",
    "globe_newswire_japanese",
    "globe_newswire_korean",
    "globe_newswire_malay",
    "globe_newswire_hebrew",
    "globe_newswire_portuguese",
    "globe_newswire_chinese_simplifie",
    "globe_newswire_chinese_tradition",
]

# ---------- errors ----------


class AuthError(RuntimeError):
    """Server rejected our cookies — user needs to re-login."""


# ---------- pure helpers (tested) ----------


def dedupe_by_article_id(items: list[dict]) -> list[dict]:
    """Preserve first-seen order; drop later items whose `id` we've seen."""
    seen: set = set()
    out: list[dict] = []
    for it in items:
        k = it.get("id") if isinstance(it, dict) else None
        if k is None:
            # can't key it — pass through as-is (unique by position)
            out.append(it)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def english_only(items: list[dict]) -> list[dict]:
    """Drop articles whose `type` marks them as a translation.

    Benzinga publishes each story in ~9 languages as `benzinga_wire_<lang>`
    with type `benzinga_wire_spanish`, `_korean`, etc. The English original
    is type `story` (or `benzinga_wire_english` when explicit). Strip the
    translated variants — they're noise and drown out English coverage.
    """
    out = []
    for it in items:
        t = (it.get("type") or "").lower()
        if t.startswith("benzinga_wire_") and t != "benzinga_wire_english":
            continue
        out.append(it)
    return out


# Block-level HTML tags that should become a newline when stripped so
# paragraph breaks survive in the plain-text output.
_BLOCK_TAGS = (
    "p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "table", "tr", "td", "th",
    "hr", "article", "section", "header", "footer", "figure",
)
_BLOCK_OPEN_RE = re.compile(
    r"<\s*/?\s*(" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def html_to_text(s: str | None) -> str:
    """
    Strip HTML tags and entities out of a Benzinga article body so the
    result is easy for an LLM to read. Preserves paragraph breaks.

    Not a general-purpose HTML sanitizer — scoped to what Benzinga's WYSIWYG
    emits: <p>, <strong>, <em>, <a class="ticker-link">, <h2>, etc.
    """
    if not s:
        return ""
    # 1. Block tags → newline marker (so paragraphs don't get glued together).
    out = _BLOCK_OPEN_RE.sub("\n", s)
    # 2. All remaining tags → stripped.
    out = _TAG_RE.sub("", out)
    # 3. HTML entities → characters (&#39; → ', &amp; → &, etc.)
    out = html.unescape(out)
    # 4. Normalize whitespace: kill trailing-space-before-newline, collapse
    #    runs of blank lines to at most one blank line.
    out = _TRAILING_WS_RE.sub("\n", out)
    out = _MULTI_NEWLINE_RE.sub("\n\n", out)
    return out.strip()


def filter_by_ticker(items: list[dict], ticker: str) -> list[dict]:
    """Case-insensitive contains-match on `tickers` list."""
    sym = ticker.upper()
    out = []
    for it in items:
        tks = it.get("tickers") or []
        if any(isinstance(t, str) and t.upper() == sym for t in tks):
            out.append(it)
    return out


def simplify_notification(n: dict) -> dict:
    """Reduce a `/notification/history` row to our internal shape."""
    data = n.get("data") or {}
    meta = data.get("metadata") or {}
    params = meta.get("params") or {}
    article = params.get("article") or {}

    raw_tickers = article.get("tickers") or []
    tickers = [t.get("name") for t in raw_tickers
               if isinstance(t, dict) and t.get("name")]

    raw_quotes = article.get("quotes") or []
    quotes = [
        {"symbol": q.get("symbol"), "price": q.get("price"), "volume": q.get("volume")}
        for q in raw_quotes if isinstance(q, dict)
    ]

    return {
        "id": article.get("id") or n.get("id"),
        "created_at": n.get("created_at"),
        "title": data.get("title") or article.get("title"),
        "teaser": None,  # notification inbox doesn't carry teaser
        "body": None,    # nor body — fetch via news_query for that
        "tickers": tickers,
        "sentiment": article.get("sentiment"),
        "quotes": quotes,
        "type": n.get("type"),
        "uri": data.get("uri"),
        "source": "watchlist_inbox",
    }


def simplify_ws_item(item: dict) -> dict:
    """Reduce a WS `news_query_resp` item (Capitalized keys) to our shape."""
    raw_tickers = item.get("Tickers") or []
    tickers = [t.get("name") for t in raw_tickers
               if isinstance(t, dict) and t.get("name")]

    raw_quotes = item.get("Quotes") or []
    quotes = [
        {"symbol": q.get("symbol"), "price": q.get("price"), "volume": q.get("volume")}
        for q in raw_quotes if isinstance(q, dict)
    ]

    body_raw = item.get("Body")
    body_text = html_to_text(body_raw) if body_raw else None

    return {
        "id": item.get("NodeID") or item.get("ID"),
        "created_at": item.get("CreatedAt"),
        "title": item.get("Title"),
        "teaser": item.get("TeaserText"),
        "body": body_text,
        "tickers": tickers,
        "sentiment": item.get("Sentiment"),
        "quotes": quotes,
        "type": item.get("Type"),
        "uri": None,
        "source": "news_db",
    }


def _parse_ts(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def filter_by_window(
    items: list[dict], start: datetime, end: datetime | None
) -> list[dict]:
    out = []
    for it in items:
        ts = _parse_ts(it.get("created_at"))
        if ts is None:
            continue
        if ts < start:
            continue
        if end is not None and ts >= end:
            continue
        out.append(it)
    return out


# ---------- REST client ----------


class BenzingaRest:
    """Thin sync wrapper around httpx with cookie jar lazy-loaded."""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    def _ensure(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                cookies=load_cookie_jar(),
                headers=_HTTP_HEADERS,
                timeout=30,
                follow_redirects=True,
            )
        return self._client

    def notifications(self, limit: int = 100) -> dict:
        """Fetch the last `limit` watchlist-alert rows."""
        r = self._ensure().get(NOTIFICATIONS_URL, params={"limit": limit, "offset": 0})
        if r.status_code in (401, 403):
            raise AuthError(
                f"Benzinga rejected our session ({r.status_code}). "
                "Paste fresh Benzinga Pro cookies in ZEUS Settings → News sources, then retry."
            )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ---------- WebSocket news_query ----------


def _cookie_header_for_ws() -> str:
    """
    Serialize benzinga.* cookies into a single `Cookie:` header value
    for the WebSocket handshake. Same cookie jar as REST, different shape.
    """
    raw = json.loads(Path(COOKIES_PATH).read_text())
    parts = []
    for c in raw:
        dom = c.get("domain") or ""
        if dom.lstrip(".") in ("benzinga.com", "api-pro.benzinga.com"):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def build_news_where(
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    sentiments: list[int] | None = None,
    keywords: str | None = None,
    exclude_types: list[str] | None = None,
) -> list[dict]:
    """
    Build the `where` clause for news_query in Benzinga's MongoDB-ish dialect.

    Documented by reverse-engineering the browser's own queries. Operators:
      * `{"$": [field, op, value]}`       — leaf predicate
      * `{"not": <leaf-or-compound>}`     — negation
      * `{"_": [..., {"and": {...}}]}`    — AND compound

    We push EVERY user-visible filter to the server so the client only has
    to dedup across pagination boundaries and simplify the shape.
    """
    leaves: list[dict] = []
    if types:
        leaves.append({"$": ["Type", "any", list(types)]})
    if exclude_types:
        leaves.append({"not": {"$": ["Type", "any", list(exclude_types)]}})
    if tickers:
        upper = [t.upper() for t in tickers]
        leaves.append({"$": ["Tickers.name", "any", upper]})
    if sentiments:
        leaves.append({"$": ["Sentiment", "any", list(sentiments)]})
    if keywords:
        # Benzinga supports `phrase` full-text match on Title (seen in UI
        # queries like {"$":["Title","phrase","Top"]}). Case-insensitive.
        leaves.append({"$": ["Title", "phrase", keywords]})
    if since_utc:
        leaves.append({"$": ["CreatedAt", "gte", since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")]})
    if until_utc:
        leaves.append({"$": ["CreatedAt", "lt", until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")]})

    if not leaves:
        return []
    if len(leaves) == 1:
        return [leaves[0]]
    # First leaf stays unwrapped; subsequent leaves get "and" wrappers.
    combined = [leaves[0]] + [{"and": leaf} for leaf in leaves[1:]]
    return [{"_": combined}]


_BASE_FIELDS = ["CreatedAt", "ID", "NodeID", "Title", "TeaserText",
                "Tickers", "Sentiment", "Type", "Channels", "Quotes"]


def _build_query(
    query_id: str,
    tickers: list[str] | None,
    types: list[str] | None,
    since_utc: datetime | None,
    until_utc: datetime | None,
    limit: int,
    sentiments: list[int] | None = None,
    keywords: str | None = None,
    exclude_types: list[str] | None = None,
    include_body: bool = False,
) -> dict:
    fields = list(_BASE_FIELDS)
    if include_body:
        # Body is 5-7KB per article of HTML. Only add to the field list when
        # the caller explicitly opts in — response size is the constraint.
        fields.append("Body")
    return {
        "type": "news_query",
        "data": {
            "id": query_id,
            "fields": fields,
            "limit": max(1, min(limit, 100)),
            "offset": 0,
            "sort": {"CreatedAt": -1},
            "where": build_news_where(
                tickers=tickers, types=types,
                since_utc=since_utc, until_utc=until_utc,
                sentiments=sentiments, keywords=keywords,
                exclude_types=exclude_types,
            ),
        },
    }


async def _connect_ws_with_retry():
    """Open a WS handshake with exponential backoff on Cloudflare 429s."""
    headers = {
        "Cookie": _cookie_header_for_ws(),
        "Origin": "https://pro.benzinga.com",
        "User-Agent": _UA,
    }
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            # max_size: default is 1 MiB. With include_body=True a single page
            # (100 articles × ~5-7 KiB body each + headers/quotes) can exceed
            # that. 16 MiB covers any realistic page; the upstream caps far
            # below this anyway.
            return await ws_connect(
                NEWS_WS_URL,
                additional_headers=headers,
                max_size=16 * 1024 * 1024,
            ).__aenter__()
        except InvalidStatus as e:
            last_exc = e
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code != 429 or attempt == 4:
                raise
            await asyncio.sleep(1.5 * (2 ** attempt))  # 1.5, 3, 6, 12, 24
    if last_exc:
        raise last_exc
    raise RuntimeError("ws_connect exhausted retries")


async def _recv_query_response(ws, query_id: str) -> list[dict]:
    """Read until we get the `news_query_resp` for our id."""
    for _ in range(10):
        msg = await ws.recv()
        data = json.loads(msg)
        if data.get("type") != "news_query_resp":
            continue
        resp_id = (data.get("data") or {}).get("id")
        # Some responses don't echo id; accept first news_query_resp if none.
        if resp_id is None or resp_id == query_id:
            return (data.get("data") or {}).get("content") or []
    return []


async def news_query(
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    limit: int = 100,
    sentiments: list[int] | None = None,
    keywords: str | None = None,
    exclude_types: list[str] | None = None,
    include_body: bool = False,
) -> list[dict]:
    """Single-shot query: open WS, send one query, close. Kept for tests/debug."""
    ws = await _connect_ws_with_retry()
    try:
        q = _build_query(
            "mcp-single", tickers, types, since_utc, until_utc,
            limit, sentiments, keywords, exclude_types, include_body,
        )
        await ws.send(json.dumps(q))
        return await _recv_query_response(ws, "mcp-single")
    finally:
        await ws.close()


async def news_query_multi(
    tickers: list[str] | None = None,
    types: list[str] | None = None,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    limit: int = 100,
    max_pages: int = 4,
    sentiments: list[int] | None = None,
    keywords: str | None = None,
    exclude_types: list[str] | None = None,
    include_body: bool = False,
) -> list[dict]:
    """
    Page through news_query on a SINGLE persistent WS connection.

    One handshake per call, then N query/response round-trips. This is
    critical: opening a fresh WS per page gets throttled by Cloudflare
    (HTTP 429) fast. Shared connection is both faster and politer.
    """
    collected: list[dict] = []
    cur_until = until_utc

    ws = await _connect_ws_with_retry()
    try:
        for i in range(max_pages):
            remaining = (limit - len(collected)) if limit else 100
            if remaining <= 0:
                break
            qid = f"mcp-p{i}"
            q = _build_query(
                qid, tickers, types, since_utc, cur_until,
                min(100, remaining), sentiments, keywords, exclude_types,
                include_body,
            )
            await ws.send(json.dumps(q))
            page = await _recv_query_response(ws, qid)
            if not page:
                break
            collected.extend(page)
            if limit and len(collected) >= limit:
                break
            # advance cursor: next page is strictly older than this page's oldest.
            oldest_ts = None
            for it in page:
                ts = _parse_ts(it.get("CreatedAt"))
                if ts and (oldest_ts is None or ts < oldest_ts):
                    oldest_ts = ts
            if oldest_ts is None:
                break
            cur_until = oldest_ts - timedelta(seconds=1)
    finally:
        await ws.close()

    return collected[:limit] if limit else collected

"""Vendored Benzinga Pro WebSocket client (originally from benzinga_mcp).

Public API:
    news_query_multi(...)      — paginated news fetch
    DEFAULT_SOURCE_TYPES       — high-quality source-type whitelist
    simplify_ws_item(raw)      — normalize WS payload to lowercase dict
    dedupe_by_article_id(list) — strip duplicates across pages

Cookies are read from the Benzinga cookies file (Settings → News sources) (same file the
chat fetcher uses).
"""
from .client import (  # noqa: F401
    news_query,
    news_query_multi,
    simplify_ws_item,
    dedupe_by_article_id,
    DEFAULT_SOURCE_TYPES,
    DEFAULT_SOURCE_GROUPS,
    TRANSLATED_TYPES,
)

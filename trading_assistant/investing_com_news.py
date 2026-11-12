"""
Investing.com RSS News Collector
=================================
Fetches market news from Investing.com free RSS feeds.
No API key required.

Available feeds:
  - Stock Market News (news_25)
  - Economy News (news_14)
  - Market Overview (market_overview)
  - Breaking Headlines (news_462)
  - Earnings Reports (news_1062)
  - Analyst Ratings (news_1061)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import requests

from news_collector import NewsItem, _simple_sentiment


# RSS feed URLs - all free, no authentication required
INVESTING_FEEDS: dict[str, str] = {
    "stock_market": "https://www.investing.com/rss/news_25.rss",
    "economy": "https://www.investing.com/rss/news_14.rss",
    "market_overview": "https://www.investing.com/rss/market_overview.rss",
    "all_news": "https://www.investing.com/rss/news.rss",
    "earnings": "https://www.investing.com/rss/news_1062.rss",
    "analyst_ratings": "https://www.investing.com/rss/news_1061.rss",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def fetch_investing_feed(feed_key: str, max_items: int = 10) -> list[NewsItem]:
    """Fetch news from a single Investing.com RSS feed.

    Args:
        feed_key: Key from INVESTING_FEEDS dict
        max_items: Maximum number of items to return
    """
    url = INVESTING_FEEDS.get(feed_key)
    if not url:
        print(f"  [WARN] Unknown Investing.com feed key: {feed_key}")
        return []

    items: list[NewsItem] = []
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        if not resp.ok:
            print(f"  [WARN] Investing.com {feed_key}: HTTP {resp.status_code}")
            return []

        root = ET.fromstring(resp.content)
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()

            if not title:
                continue

            items.append(NewsItem(
                title=title,
                source=f"Investing.com ({feed_key})",
                url=link,
                published=pub_date[:25] if pub_date else "",
                ticker=None,
                sentiment=_simple_sentiment(title),
            ))
    except ET.ParseError as e:
        print(f"  [WARN] Investing.com {feed_key} XML parse error: {e}")
    except requests.RequestException as e:
        print(f"  [WARN] Investing.com {feed_key} request failed: {e}")
    except Exception as e:
        print(f"  [WARN] Investing.com {feed_key}: {e}")

    return items


def fetch_all_investing_news(
    max_per_feed: int = 5,
    feeds: list[str] | None = None,
) -> list[NewsItem]:
    """Fetch news from all (or specified) Investing.com RSS feeds.

    Args:
        max_per_feed: Max items per feed (default 5)
        feeds: Optional list of feed keys. Defaults to all feeds.

    Returns:
        Deduplicated list of NewsItem.
    """
    if feeds is None:
        feeds = list(INVESTING_FEEDS.keys())

    all_items: list[NewsItem] = []
    for key in feeds:
        all_items.extend(fetch_investing_feed(key, max_items=max_per_feed))

    # Deduplicate by lowercased title
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in all_items:
        title_key = item.title.lower().strip()
        if title_key not in seen:
            seen.add(title_key)
            unique.append(item)

    return unique

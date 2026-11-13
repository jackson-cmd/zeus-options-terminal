"""
News Collector
==============
Aggregates financial news from multiple free sources (priority order):
  1. Finviz        – primary; scrapes finviz.com, no API key needed
                     Install: pip install finviz
  2. Yahoo Finance – fallback when Finviz returns nothing
  3. Google News RSS – secondary supplement
  4. Finnhub       – optional (free-tier API key required)
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote

import requests


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: str
    ticker: str | None = None
    sentiment: str = "neutral"  # "positive", "negative", "neutral"

    def __str__(self):
        icon = {"positive": "+", "negative": "-", "neutral": "~"}.get(self.sentiment, "~")
        return f"[{icon}] {self.title} ({self.source}, {self.published})"


def _simple_sentiment(text: str) -> str:
    """Ultra-simple keyword-based sentiment."""
    text_lower = text.lower()
    positive_words = [
        "surge", "rally", "gain", "rise", "beat", "exceed", "upgrade",
        "bullish", "record", "growth", "profit", "strong", "soar", "jump",
        "outperform", "buy", "positive", "breakout", "momentum",
    ]
    negative_words = [
        "drop", "fall", "decline", "crash", "miss", "downgrade", "bearish",
        "loss", "weak", "plunge", "sell", "warning", "risk", "cut",
        "layoff", "recession", "lawsuit", "investigation", "fraud",
    ]
    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)

    if pos_count > neg_count:
        return "positive"
    elif neg_count > pos_count:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# Finviz News (primary source — no API key required)
# ---------------------------------------------------------------------------

def fetch_finviz_news(ticker: str, max_items: int = 5) -> list[NewsItem]:
    """Fetch news from Finviz.com (primary source).

    Finviz returns rows like:
        [date_str, time_str, title, url, source]
    or, in some versions of the library:
        {"date": ..., "title": ..., "link": ..., "source": ...}

    We handle both shapes defensively.
    Requires:  pip install finviz
    """
    items: list[NewsItem] = []
    try:
        import finviz  # type: ignore
        raw = finviz.get_news(ticker)
        for row in raw[:max_items]:
            # ---- list/tuple shape ----------------------------------------
            if isinstance(row, (list, tuple)):
                # [date, time, title, url, source]  (5-element)
                # [date, title, url, source]         (4-element)
                if len(row) >= 5:
                    date_s, time_s, title, url, source = (
                        str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4])
                    )
                    pub = f"{date_s} {time_s}".strip()
                elif len(row) >= 4:
                    date_s, title, url, source = (
                        str(row[0]), str(row[1]), str(row[2]), str(row[3])
                    )
                    pub = date_s
                elif len(row) >= 3:
                    date_s, title, url = str(row[0]), str(row[1]), str(row[2])
                    source, pub = "Finviz", date_s
                else:
                    continue
            # ---- dict shape ----------------------------------------------
            elif isinstance(row, dict):
                title  = row.get("title") or row.get("headline", "")
                url    = row.get("url") or row.get("link", "")
                source = row.get("source", "Finviz")
                pub    = str(row.get("date") or row.get("datetime", ""))
            else:
                continue

            if not title:
                continue

            items.append(NewsItem(
                title=title,
                source=source,
                url=url,
                published=pub,
                ticker=ticker,
                sentiment=_simple_sentiment(title),
            ))
    except ImportError:
        # finviz library not installed — caller will fall back to Yahoo
        pass
    except Exception as e:
        print(f"  [WARN] Finviz news for {ticker}: {e}")
    return items


# ---------------------------------------------------------------------------
# Yahoo Finance News (fallback)
# ---------------------------------------------------------------------------

def fetch_yahoo_news(ticker: str, max_items: int = 5) -> list[NewsItem]:
    """Fetch news for a ticker via Yahoo Finance search API."""
    items = []
    try:
        from yahoo_api import get_ticker_news
        news = get_ticker_news(ticker, max_items=max_items)
        for article in news[:max_items]:
            title = article.get("title", "")
            url = article.get("link", "")
            publisher = article.get("publisher", "Yahoo Finance")
            pub_time = article.get("providerPublishTime", 0)
            if pub_time:
                pub_str = datetime.datetime.fromtimestamp(pub_time).strftime("%Y-%m-%d %H:%M")
            else:
                pub_str = ""

            items.append(NewsItem(
                title=title,
                source=publisher,
                url=url,
                published=pub_str,
                ticker=ticker,
                sentiment=_simple_sentiment(title),
            ))
    except Exception as e:
        print(f"  [WARN] Yahoo news for {ticker}: {e}")
    return items


# ---------------------------------------------------------------------------
# Google News RSS
# ---------------------------------------------------------------------------

def fetch_google_news(query: str, max_items: int = 5) -> list[NewsItem]:
    """Fetch news via Google News RSS feed."""
    items = []
    try:
        url = f"https://news.google.com/rss/search?q={quote(query)}+stock&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.ok:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:max_items]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate", "")
                source = item.findtext("source", "Google News")

                items.append(NewsItem(
                    title=title,
                    source=source,
                    url=link,
                    published=pub_date[:16] if pub_date else "",
                    sentiment=_simple_sentiment(title),
                ))
    except Exception as e:
        print(f"  [WARN] Google News for '{query}': {e}")
    return items


# ---------------------------------------------------------------------------
# Finnhub (free tier)
# ---------------------------------------------------------------------------

def fetch_finnhub_news(ticker: str, api_key: str = "", max_items: int = 5) -> list[NewsItem]:
    """Fetch company news from Finnhub (requires free API key)."""
    if not api_key:
        return []

    items = []
    try:
        today = datetime.date.today()
        week_ago = today - datetime.timedelta(days=7)
        url = (
            f"https://finnhub.io/api/v1/company-news?"
            f"symbol={ticker}&from={week_ago}&to={today}&token={api_key}"
        )
        resp = requests.get(url, timeout=10)
        if resp.ok:
            for article in resp.json()[:max_items]:
                title = article.get("headline", "")
                items.append(NewsItem(
                    title=title,
                    source=article.get("source", "Finnhub"),
                    url=article.get("url", ""),
                    published=datetime.datetime.fromtimestamp(
                        article.get("datetime", 0)
                    ).strftime("%Y-%m-%d %H:%M"),
                    ticker=ticker,
                    sentiment=_simple_sentiment(title),
                ))
    except Exception as e:
        print(f"  [WARN] Finnhub for {ticker}: {e}")
    return items


# ---------------------------------------------------------------------------
# Market-wide news
# ---------------------------------------------------------------------------

def fetch_market_news() -> list[NewsItem]:
    """Fetch general market news from Google News + Investing.com RSS."""
    items = []

    # Google News RSS
    queries = ["stock market today", "S&P 500", "Federal Reserve", "earnings report"]
    for q in queries:
        items.extend(fetch_google_news(q, max_items=3))

    # Investing.com RSS feeds
    try:
        from investing_com_news import fetch_all_investing_news
        items.extend(fetch_all_investing_news(max_per_feed=5))
    except Exception as e:
        print(f"  [WARN] Investing.com news: {e}")

    # Deduplicate by title
    seen = set()
    unique = []
    for item in items:
        key = item.title.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

def collect_news_for_tickers(tickers: list[str], finnhub_key: str = "") -> dict[str, list[NewsItem]]:
    """Collect news for all tickers + market-wide news.

    Priority per ticker:
      1. Finviz      (primary — fast, no key, real-time)
      2. Yahoo Finance (fallback when Finviz returns nothing)
      3. Google News RSS (supplemental, always added)
      4. Finnhub     (optional, only when api key provided)
    """
    all_news = {"MARKET": fetch_market_news()}

    for ticker in tickers:
        ticker_news: list[NewsItem] = []

        # 1. Finviz — primary source
        finviz_items = fetch_finviz_news(ticker, max_items=5)
        ticker_news.extend(finviz_items)

        # 2. Yahoo Finance — fallback if Finviz returned nothing
        if not finviz_items:
            ticker_news.extend(fetch_yahoo_news(ticker, max_items=5))

        # 3. Google News RSS — always supplement with a couple more headlines
        ticker_news.extend(fetch_google_news(f"{ticker} stock", max_items=2))

        # 4. Finnhub — optional extra source
        if finnhub_key:
            ticker_news.extend(fetch_finnhub_news(ticker, finnhub_key, max_items=2))

        # Deduplicate by lowercased title
        seen: set[str] = set()
        unique: list[NewsItem] = []
        for item in ticker_news:
            key = item.title.lower().strip()
            if key not in seen:
                seen.add(key)
                item.ticker = ticker
                unique.append(item)

        if unique:
            all_news[ticker] = unique

    return all_news


def collect_market_news_only() -> dict[str, list[NewsItem]]:
    """Collect only market-wide news (Google + Investing.com).

    Convenience function for news-mode skill.
    """
    return {"MARKET": fetch_market_news()}


def summarize_sentiment(news: dict[str, list[NewsItem]]) -> dict[str, dict]:
    """Summarize sentiment per ticker."""
    summary = {}
    for key, items in news.items():
        pos = sum(1 for i in items if i.sentiment == "positive")
        neg = sum(1 for i in items if i.sentiment == "negative")
        neu = sum(1 for i in items if i.sentiment == "neutral")
        total = len(items)
        score = (pos - neg) / total if total > 0 else 0

        summary[key] = {
            "positive": pos,
            "negative": neg,
            "neutral": neu,
            "total": total,
            "score": round(score, 2),
            "label": "Bullish" if score > 0.2 else "Bearish" if score < -0.2 else "Neutral",
        }
    return summary

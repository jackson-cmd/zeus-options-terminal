"""
Yahoo Finance direct HTTP API - replaces yfinance library.
Uses the public Yahoo Finance v8/v7 API endpoints.
"""
from __future__ import annotations

import datetime
import time

import pandas as pd
import requests

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
})


def fetch_daily_data(ticker: str, lookback_days: int = 500) -> pd.DataFrame:
    """Fetch daily OHLCV data from Yahoo Finance chart API."""
    end_ts = int(time.time())
    start_ts = end_ts - (lookback_days * 86400)

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1": start_ts,
        "period2": end_ts,
        "interval": "1d",
        "events": "history",
    }

    try:
        resp = _SESSION.get(url, params=params, timeout=15)
        if not resp.ok:
            # Fallback to v7
            url = f"https://query2.finance.yahoo.com/v7/finance/chart/{ticker}"
            resp = _SESSION.get(url, params=params, timeout=15)

        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return pd.DataFrame()

        chart = result[0]
        timestamps = chart.get("timestamp", [])
        indicators = chart.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        adj_close = indicators.get("adjclose", [{}])

        df = pd.DataFrame({
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        }, index=pd.to_datetime(timestamps, unit="s"))

        # Use adjusted close if available
        if adj_close and adj_close[0].get("adjclose"):
            df["close"] = adj_close[0]["adjclose"]

        df = df.dropna(subset=["close"])
        df.index = df.index.normalize()  # strip time component
        return df

    except Exception as e:
        print(f"  [WARN] Yahoo API error for {ticker}: {e}")
        return pd.DataFrame()


def get_current_price(ticker: str) -> float | None:
    """Get the latest price for a ticker."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "1d"}
        resp = _SESSION.get(url, params=params, timeout=10)
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            meta = result[0].get("meta", {})
            return float(meta.get("regularMarketPrice", 0))
    except Exception:
        pass
    return None


def get_ticker_news(ticker: str, max_items: int = 5) -> list[dict]:
    """Fetch news for a ticker from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v1/finance/search"
        params = {"q": ticker, "newsCount": max_items, "quotesCount": 0}
        resp = _SESSION.get(url, params=params, timeout=10)
        data = resp.json()
        return data.get("news", [])
    except Exception:
        return []

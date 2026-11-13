"""Unified Benzinga Pro client — every reverse-engineered data source in one place.

All REST sub-modules authenticate with the shared cookie jar (benzinga/cookies.json)
and are headless-ready. See benzinga_api/README.md for usage and
benzinga/BENZINGA_API.md for the raw endpoint reference.

    from benzinga_api import calendar, scanner, account, signals

    calendar.earnings(tickers=["AAPL"])      # Market Events family (15 types)
    account.watchlists()                      # your watchlists
    scanner.scan(limit=50)                    # equity scanner (needs apikey)
    # signals.stream(...)                     # async WS push (needs sessionId)
"""
from . import account, calendar, scanner, signals  # noqa: F401

__all__ = ["calendar", "scanner", "account", "signals"]

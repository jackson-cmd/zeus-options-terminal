"""The signed-in user's own Benzinga config: watchlists + saved layouts.

    from benzinga_api import account
    account.watchlists()          # your watchlists (cookie-auth)
    account.layout("<uuid>")      # a saved dashboard layout
"""
from __future__ import annotations

from ._http import ACCOUNTS_HOST, API_HOST, get_json


def watchlists() -> dict:
    """All of the user's watchlists (names + symbols). Cookie-authenticated."""
    return get_json(f"{API_HOST}/api/v2/watchlists/list")


def layout(layout_id: str) -> dict:
    """A saved dashboard layout by its UUID (from accounts.benzinga.com)."""
    return get_json(f"{ACCOUNTS_HOST}/api/v1/layouts/{layout_id}/")

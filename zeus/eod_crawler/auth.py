"""
TastyTrade OAuth2 — refresh_token grant.

Setup (one-time):
  1. Portal: https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
     → Manage app → Create Grant → all scopes → copy refresh_token
  2. Save the refresh_token into refresh_token.txt (in this folder)
  3. Save the OAuth app's client secret into client_secret.txt (in this folder)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from config import (
    CLIENT_SECRET_FILE,
    OAUTH_TOKEN_URL,
    REFRESH_TOKEN_FILE,
    TOKEN_CACHE_FILE,
)


def _read_secret(path: Path) -> str:
    import zeus_config as _hc
    key = {"client_secret.txt": "tt_client_secret", "refresh_token.txt": "tt_refresh_token"}.get(path.name, "")
    v = str(_hc.get(key) or "").strip() if key else ""
    if v:
        return v
    raise FileNotFoundError(f"tastytrade is not connected: open Settings and fill in {key}.")
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path.name}. See auth.py docstring for setup instructions."
        )
    val = path.read_text().strip()
    if not val or val.startswith("your_"):
        raise ValueError(f"{path.name} contains a placeholder; replace with real value.")
    return val


class TastyAuth:
    """Manages a cached, auto-refreshing tastytrade access token."""

    def __init__(self) -> None:
        self.client_secret = _read_secret(CLIENT_SECRET_FILE)
        self.refresh_token = _read_secret(REFRESH_TOKEN_FILE)
        self._cache: dict = {}
        self._load_cache()

    def get_access_token(self) -> str:
        if self._token_valid():
            return self._cache["access_token"]
        token_data = self._exchange()
        self._save_cache(token_data)
        return self._cache["access_token"]

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
        }

    def _token_valid(self) -> bool:
        return bool(
            self._cache.get("access_token")
            and time.time() < self._cache.get("expires_at", 0) - 30
        )

    def _exchange(self) -> dict:
        resp = requests.post(
            OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"Token exchange failed ({resp.status_code}): {detail}",
                response=resp,
            )
        return resp.json()

    def _save_cache(self, token_data: dict) -> None:
        token_data["expires_at"] = time.time() + token_data.get("expires_in", 900)
        self._cache = token_data
        TOKEN_CACHE_FILE.write_text(json.dumps(token_data, indent=2))

    def _load_cache(self) -> None:
        if TOKEN_CACHE_FILE.exists():
            try:
                self._cache = json.loads(TOKEN_CACHE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                self._cache = {}

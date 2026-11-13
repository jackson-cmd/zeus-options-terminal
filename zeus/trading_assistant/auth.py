"""
TastyTrade OAuth2 Authentication - reads credentials from config.
NEVER logs or prints tokens.
"""
import json
import time
from pathlib import Path

import requests

from config import (
    TASTYTRADE_BASE_URL,
    TOKEN_CACHE_FILE,
    get_client_secret,
    get_refresh_token,
)

OAUTH_TOKEN_URL = f"{TASTYTRADE_BASE_URL}/oauth/token"


class TastyAuth:
    def __init__(self):
        self.client_secret = get_client_secret()
        self.refresh_token = get_refresh_token()
        self._cache: dict = {}
        self._load_cache()

    def get_access_token(self) -> str:
        if self._token_valid():
            return self._cache["access_token"]
        print("[Auth] Refreshing access token...")
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
            raise requests.HTTPError(
                f"Token exchange failed ({resp.status_code}). Check credentials."
            )
        return resp.json()

    def _save_cache(self, token_data: dict):
        token_data["expires_at"] = time.time() + token_data.get("expires_in", 900)
        self._cache = token_data
        Path(TOKEN_CACHE_FILE).write_text(json.dumps(token_data, indent=2))

    def _load_cache(self):
        if Path(TOKEN_CACHE_FILE).exists():
            try:
                self._cache = json.loads(Path(TOKEN_CACHE_FILE).read_text())
            except (json.JSONDecodeError, OSError):
                self._cache = {}

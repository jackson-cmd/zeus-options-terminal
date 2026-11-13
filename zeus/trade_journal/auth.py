"""
TastyTrade OAuth2 Authentication
=================================
TastyTrade does NOT use a browser-redirect authorization code flow.
Instead, you manually create a "Grant" in the portal to get a refresh_token,
then use it programmatically to obtain short-lived access tokens.

Setup (one-time):
  1. Go to: https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications
  2. Find your app → click Manage → Create Grant → select all scopes
  3. Copy the refresh_token shown and paste it into refresh_token.txt
  4. Make sure client_secret.txt contains your app's client secret

Token exchange (automatic):
  POST /oauth/token  {grant_type, client_secret, refresh_token}
  → access_token (valid 15 min, auto-refreshed by this module)
  Note: client_id is NOT used in the token exchange request.
"""

import json
import time
from pathlib import Path

import requests

from config import BASE_URL, TOKEN_CACHE_FILE

OAUTH_TOKEN_URL = f"{BASE_URL}/oauth/token"


_CRED_KEYS = {"client_id.txt": "tt_client_id", "client_secret.txt": "tt_client_secret",
              "refresh_token.txt": "tt_refresh_token"}


def _read_file(filename: str) -> str:
    """tastytrade OAuth values come from Settings (~/.zeus-terminal/config.json)."""
    import zeus_config as _hc
    key = _CRED_KEYS.get(filename, "")
    value = str(_hc.get(key) or "").strip() if key else ""
    if value:
        return value
    raise FileNotFoundError(f"tastytrade is not connected: open Settings and fill in {key}.")
    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing file: {filename}\n"
            f"Please create it — see auth.py docstring for setup instructions."
        )
    value = path.read_text().strip()
    if not value or value.startswith("your_"):
        raise ValueError(
            f"{filename} still contains a placeholder. "
            f"Please replace it with the real value."
        )
    return value


class TastyAuth:
    """
    Manages TastyTrade OAuth2 access tokens.

    Reads credentials from:
      client_secret.txt   – your OAuth app's client secret
      refresh_token.txt   – the refresh token created in the portal (never expires)

    Access tokens are cached in .token_cache.json (valid ~15 min).
    """

    def __init__(self):
        self.client_secret = _read_file("client_secret.txt")
        self.refresh_token  = _read_file("refresh_token.txt")
        self._cache: dict   = {}
        self._load_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        """Return a valid access token, fetching a new one if necessary."""
        if self._token_valid():
            return self._cache["access_token"]

        print("[Auth] Fetching new access token via refresh_token grant...")
        token_data = self._exchange()
        self._save_cache(token_data)
        return self._cache["access_token"]

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _token_valid(self) -> bool:
        return bool(
            self._cache.get("access_token")
            and time.time() < self._cache.get("expires_at", 0) - 30
        )

    def _exchange(self) -> dict:
        """POST to /oauth/token using refresh_token grant."""
        resp = requests.post(
            OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            # Authorization header must NOT be present for this request
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"Token exchange failed ({resp.status_code}): {detail}\n\n"
                "Checklist:\n"
                "  1. client_secret.txt — correct app secret?\n"
                "  2. refresh_token.txt — token created via portal Grant?\n"
                "     Portal: https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications\n"
                "             → Manage → Create Grant → copy refresh token",
                response=resp,
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

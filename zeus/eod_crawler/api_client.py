"""HTTP client for the tastytrade endpoints we need."""

from __future__ import annotations

import time

import requests

from auth import TastyAuth
from config import BASE_URL, HTTP_TIMEOUT
import zeus_config as _hc
import local_broker as _lb


class TastyClient:
    def __init__(self) -> None:
        self._auth = None
        self.session = requests.Session()

    @property
    def local(self) -> bool:
        """Anything but tastytrade (Alpaca, Public, uploaded trades) is answered by local_broker in
        tastytrade's shapes. Read on every call, so switching brokers in Settings needs no restart."""
        return _hc.account_source() != "tastytrade"

    @property
    def auth(self):
        if self._auth is None and not self.local:
            self._auth = TastyAuth()
        return self._auth

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with one retry on 429 (rate limit) using exponential backoff."""
        if self.local:
            return _lb.handle_get(path, params)
        url = f"{BASE_URL}{path}"
        delay = 1.0
        for attempt in range(3):
            resp = self.session.get(
                url, headers=self.auth.headers(), params=params, timeout=HTTP_TIMEOUT
            )
            if resp.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            self._raise_for_status(resp)
            return resp.json()
        self._raise_for_status(resp)
        return resp.json()

    def get_option_chain_nested(self, symbol: str) -> dict:
        return self.get(f"/option-chains/{symbol}/nested")

    def get_market_data(self, symbols: list[str]) -> dict:
        return self.get("/market-data", params={"symbols[]": symbols})

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"HTTP {resp.status_code} — {resp.url}\n{detail}",
                response=resp,
            )

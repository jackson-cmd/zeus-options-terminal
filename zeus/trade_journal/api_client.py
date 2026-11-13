"""
Thin HTTP client that attaches auth headers and handles common errors.
"""
from __future__ import annotations

import requests

from auth import TastyAuth
from config import BACKTEST_URL, BASE_URL
import zeus_config as _hc
import local_broker as _lb


class TastyClient:
    def __init__(self):
        self._auth = None
        self.session = requests.Session()
        self._backtest_token: str | None = None

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

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return self.auth.headers()

    def _backtest_headers(self) -> dict:
        """Return headers for the backtester API, obtaining a session token if needed."""
        if not self._backtest_token:
            self._backtest_token = self._exchange_backtest_token()
        return {
            "Authorization": f"Bearer {self._backtest_token}",
            "Content-Type": "application/json",
        }

    def _exchange_backtest_token(self) -> str:
        """Exchange the OAuth access token for a backtester-specific JWT."""
        access_token = self.auth.get_access_token()
        resp = self.session.post(
            f"{BACKTEST_URL}/sessions",
            json={"tastytradeToken": access_token},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not resp.ok:
            raise requests.HTTPError(
                f"Backtest session exchange failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()["token"]

    def get(self, path: str, params: dict | None = None, base: str = BASE_URL) -> dict:
        if self.local:
            return _lb.handle_get(path, params)
        url = f"{base}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        self._raise_for_status(resp)
        return resp.json()

    def post(self, path: str, payload: dict, base: str = BASE_URL) -> dict:
        if self.local:
            raise _lb.LocalModeError("This action needs a tastytrade connection.")
        url = f"{base}{path}"
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=60)
        self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get_option_chain(self, symbol: str) -> dict:
        """Fetch nested option chain (all expirations + strikes)."""
        return self.get(f"/option-chains/{symbol}/nested")

    def get_market_metrics(self, symbols: list[str]) -> dict:
        """IV rank, IV percentile, liquidity metrics."""
        joined = ",".join(symbols)
        return self.get("/market-metrics", params={"symbols[]": symbols})

    def get_option_quotes(self, option_symbols: list[str]) -> dict:
        """Fetch bid/ask/last/IV/greeks for a list of option symbols."""
        return self.get("/market-data", params={"symbols[]": option_symbols})

    def get_account(self) -> dict:
        return self.get("/customers/me/accounts")

    # ------------------------------------------------------------------
    # Backtester API (separate base URL)
    # ------------------------------------------------------------------

    def get_backtest_available_dates(self, symbol: str) -> dict:
        url = f"{BACKTEST_URL}/available-dates"
        resp = self.session.get(
            url, headers=self._backtest_headers(),
            params={"underlying-symbol": symbol}, timeout=30,
        )
        self._raise_for_status(resp)
        return resp.json()

    def create_backtest(self, payload: dict) -> dict:
        url = f"{BACKTEST_URL}/backtests"
        resp = self.session.post(url, headers=self._backtest_headers(), json=payload, timeout=60)
        self._raise_for_status(resp)
        return resp.json()

    def get_backtest(self, backtest_id: str) -> dict:
        url = f"{BACKTEST_URL}/backtests/{backtest_id}"
        resp = self.session.get(url, headers=self._backtest_headers(), timeout=30)
        self._raise_for_status(resp)
        return resp.json()

    def get_backtest_logs(self, backtest_id: str) -> dict:
        url = f"{BACKTEST_URL}/backtests/{backtest_id}/logs"
        resp = self.session.get(url, headers=self._backtest_headers(), timeout=30)
        self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for_status(resp: requests.Response):
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"HTTP {resp.status_code} — {resp.url}\n{detail}",
                response=resp,
            )

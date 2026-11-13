"""
TastyTrade API client - handles auth, options chain, positions, account info.
"""
from __future__ import annotations

import os
import requests
from auth import TastyAuth
from config import TASTYTRADE_BASE_URL, TASTYTRADE_BACKTEST_URL
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

    def _headers(self) -> dict:
        return self.auth.headers()

    def _backtest_headers(self) -> dict:
        if not self._backtest_token:
            self._backtest_token = self._exchange_backtest_token()
        return {
            "Authorization": f"Bearer {self._backtest_token}",
            "Content-Type": "application/json",
        }

    def _exchange_backtest_token(self) -> str:
        if self.local:
            raise _lb.LocalModeError("The tastytrade backtester needs a tastytrade connection.")
        access_token = self.auth.get_access_token()
        resp = self.session.post(
            f"{TASTYTRADE_BACKTEST_URL}/sessions",
            json={"tastytradeToken": access_token},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if not resp.ok:
            raise requests.HTTPError(f"Backtest session failed ({resp.status_code})")
        return resp.json()["token"]

    def get(self, path: str, params: dict | None = None, base: str = TASTYTRADE_BASE_URL) -> dict:
        if self.local:
            return _lb.handle_get(path, params)
        url = f"{base}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        self._raise(resp)
        return resp.json()

    def post(self, path: str, payload: dict, base: str = TASTYTRADE_BASE_URL) -> dict:
        if self.local:
            raise _lb.LocalModeError("This action needs a tastytrade connection.")
        url = f"{base}{path}"
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=60)
        self._raise(resp)
        return resp.json()

    # --- Account & Positions ---

    def get_accounts(self) -> list[dict]:
        data = self.get("/customers/me/accounts")
        return data.get("data", {}).get("items", [])

    def pick_active_account(self) -> tuple[dict, str]:
        """Return (account_wrapper, account_number) for the active account.

        TT can return multiple accounts (e.g. an empty shell account); ordering
        is not guaranteed. Selection: TT_ACCOUNT_NUMBER env override > first
        with positions > first. Raises if no accounts.
        """
        accounts = self.get_accounts()
        if not accounts:
            raise RuntimeError("No TastyTrade accounts returned.")

        def _num(a: dict) -> str:
            return a.get("account", {}).get("account-number", "") or a.get("account-number", "")

        pinned = (str(_hc.get("tt_account_number") or "") or os.environ.get("TT_ACCOUNT_NUMBER", "")).strip()
        if pinned:
            for a in accounts:
                if _num(a) == pinned:
                    return a, pinned

        for a in accounts:
            num = _num(a)
            if not num:
                continue
            try:
                if self.get_positions(num):
                    return a, num
            except Exception:
                continue

        a = accounts[0]
        return a, _num(a)

    def get_positions(self, account_number: str) -> list[dict]:
        data = self.get(f"/accounts/{account_number}/positions")
        return data.get("data", {}).get("items", [])

    def get_balances(self, account_number: str) -> dict:
        data = self.get(f"/accounts/{account_number}/balances")
        return data.get("data", {})

    def get_live_orders(self, account_number: str) -> list[dict]:
        """Working orders on the account — the "live" bucket TT surfaces on the
        web UI's orders panel. Includes Received / Live / In-Flight / etc.
        (Fully-filled or cancelled orders drop out of this endpoint.)

        Used by the missing-GTC watcher to check whether an open spread has a
        take-profit closing order sitting on the book."""
        data = self.get(f"/accounts/{account_number}/orders/live")
        return data.get("data", {}).get("items", [])

    # --- Options Chain ---

    def get_option_chain(self, symbol: str) -> dict:
        return self.get(f"/option-chains/{symbol}/nested")

    def get_market_metrics(self, symbols: list[str]) -> dict:
        return self.get("/market-metrics", params={"symbols[]": symbols})

    def get_option_quotes(self, option_symbols: list[str]) -> dict:
        """Fetch bid/ask/last/IV/greeks for a list of option symbols."""
        return self.get("/market-data", params={"symbols[]": option_symbols})

    def get_equity_quote(self, symbol: str) -> dict:
        """Get current equity quote."""
        data = self.get(f"/market-data/{symbol}/quotes")
        return data.get("data", {})

    # --- Backtest ---

    def create_backtest(self, payload: dict) -> dict:
        url = f"{TASTYTRADE_BACKTEST_URL}/backtests"
        resp = self.session.post(url, headers=self._backtest_headers(), json=payload, timeout=60)
        self._raise(resp)
        return resp.json()

    @staticmethod
    def _raise(resp: requests.Response):
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise requests.HTTPError(
                f"HTTP {resp.status_code} — {resp.url}\n{detail}",
                response=resp,
            )

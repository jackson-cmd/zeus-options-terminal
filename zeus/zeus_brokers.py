"""Brokers and market data besides tastytrade: Alpaca and Public.

ZEUS speaks tastytrade's REST shapes everywhere. When the account is not
tastytrade, local_broker answers those calls; this module hands it real
positions, balances, fills and open orders from Alpaca or Public, and stock
quotes, option quotes and Greeks from their market-data APIs. Nothing here
places, changes or cancels an order.

  Alpaca  alpaca.markets. Free account with paper trading. Free data: stocks
          from IEX in real time (or every exchange 15 min delayed), options
          from the 'indicative' feed with Greeks and IV. Keys: PK… paper, AK… live.
  Public  public.com. Live brokerage accounts only (no paper). A secret key
          from the account's API settings is exchanged for a short-lived token;
          account holders get quotes, option chains and Greeks.

Every call has a timeout, answers are cached briefly, and the keys never leave
this computer except in the request headers to the broker itself.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import zeus_config as hc

ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_LIVE = "https://api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"
PUBLIC_BASE = "https://api.public.com"

_OPT_RE = re.compile(r"^([A-Z.\-/0-9]{1,6})\s*(\d{6})([CP])(\d{8})$")


class BrokerAPIError(RuntimeError):
    """A broker answered with an error (bad key, no permission, rate limit…)."""


# ---------------------------------------------------------------------------
# HTTP + a tiny TTL cache (tests replace `send`)
# ---------------------------------------------------------------------------
def _requests_send(method: str, url: str, headers: dict, params: dict | None, body: Any, timeout: float):
    import requests
    r = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {"message": r.text[:300]}
    return r.status_code, data


def _num(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return default if x != x else x      # NaN → default
    except (TypeError, ValueError):
        return default


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data.get("detail") or data)[:200]
    return str(data)[:200]


class _Cache:
    def __init__(self):
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key, ttl: float):
        with self._lock:
            hit = self._d.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = (time.time(), value)
        return value

    def clear(self):
        with self._lock:
            self._d.clear()


def compact_occ(sym: str) -> str:
    """'AAPL  261218C00200000' → 'AAPL261218C00200000' (Alpaca and Public use the unpadded form)."""
    return re.sub(r"\s+", "", sym or "").upper()


def padded_occ(sym: str) -> str:
    """'AAPL261218C00200000' → 'AAPL  261218C00200000' (tastytrade's padded OCC form, used inside ZEUS)."""
    m = _OPT_RE.match(compact_occ(sym))
    if not m:
        return (sym or "").upper()
    root, ymd, cp, k = m.groups()
    return f"{root:<6}{ymd}{cp}{k}"


def is_option_symbol(sym: str) -> bool:
    return bool(_OPT_RE.match(compact_occ(sym)))


def _occ_parts(sym: str) -> dict | None:
    m = _OPT_RE.match(compact_occ(sym))
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    return {"underlying": root, "expiration": f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}", "type": cp,
            "strike": int(k) / 1000.0}


def _iso(ts: str | None) -> str:
    """Any ISO timestamp → tastytrade style 'YYYY-MM-DDTHH:MM:SS.000+00:00' (UTC)."""
    if not ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    s = str(ts).replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)       # nanoseconds → microseconds
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def _eastern_wall(ts: str | None) -> str:
    """ISO timestamp → New York wall-clock 'YYYY-MM-DDTHH:MM:SS' (the format local_broker's trades use)."""
    iso = _iso(ts)
    dt = datetime.fromisoformat(iso)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return (dt - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")


def _tt_action(side: str, effect: str) -> str:
    side = "Buy" if side.lower().startswith("b") else "Sell"
    return f"{side} to {'Close' if effect == 'close' else 'Open'}"


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------
_ALPACA_WORKING = {"new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding",
                   "pending_replace", "calculated", "done_for_day"}


class Alpaca:
    name = "alpaca"
    label = "Alpaca"

    def __init__(self, key: str, secret: str, paper: bool = True, stock_feed: str = "iex",
                 options_feed: str = "indicative", send: Callable | None = None, timeout: float = 20):
        self.key, self.secret, self.paper = key, secret, bool(paper)
        self.stock_feed = (stock_feed or "iex").strip().lower()
        self.options_feed = (options_feed or "indicative").strip().lower()
        self.base = ALPACA_PAPER if self.paper else ALPACA_LIVE
        self.send = send or _requests_send
        self.timeout = timeout
        self.cache = _Cache()

    @property
    def account_label(self) -> str:
        return "ALPACA-PAPER" if self.paper else "ALPACA"

    def _get(self, base: str, path: str, **params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        status, data = self.send("GET", base + path, {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
                                                      "Accept": "application/json"}, params or None, None, self.timeout)
        if status >= 400:
            hint = ""
            if status in (401, 403) and base != ALPACA_DATA:
                hint = " (check the key and secret, and whether they are paper or live keys)"
            raise BrokerAPIError(f"Alpaca {status}: {_msg(data)}{hint}")
        return data

    # ---- account ----
    def account(self) -> dict:
        hit = self.cache.get("account", 20)
        return hit if hit is not None else self.cache.put("account", self._get(self.base, "/v2/account") or {})

    def positions(self) -> list[dict]:
        hit = self.cache.get("positions", 20)
        return hit if hit is not None else self.cache.put("positions", self._get(self.base, "/v2/positions") or [])

    def open_orders(self) -> list[dict]:
        hit = self.cache.get("orders", 30)
        if hit is not None:
            return hit
        return self.cache.put("orders", self._get(self.base, "/v2/orders", status="open", nested="true", limit=500) or [])

    def fills(self, days: int = 400, max_pages: int = 40) -> list[dict]:
        """Every FILL activity of the last `days` days, oldest first (cached 5 min)."""
        hit = self.cache.get(("fills", days), 300)
        if hit is not None:
            return hit
        after = (date.today() - timedelta(days=days)).isoformat()
        out, token = [], None
        for _ in range(max_pages):
            page = self._get(self.base, "/v2/account/activities", activity_types="FILL", direction="desc",
                             page_size=100, after=after, page_token=token) or []
            if not isinstance(page, list) or not page:
                break
            out.extend(page)
            if len(page) < 100:
                break
            token = page[-1].get("id")
        out.sort(key=lambda a: (a.get("transaction_time") or "", a.get("id") or ""))
        return self.cache.put(("fills", days), out)

    # ---- market data ----
    def stock_snapshots(self, symbols: Iterable[str]) -> dict:
        """{ZEUS symbol: quote dict} for stocks and ETFs (indexes such as SPX or VIX are not on Alpaca)."""
        want = [s.upper() for s in dict.fromkeys(symbols) if s and not str(s).startswith(("$", "^"))]
        out, missing = {}, []
        for s in want:
            hit = self.cache.get(("eq", s), 15)
            if hit is not None:
                out[s] = hit
            else:
                missing.append(s)
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            amap = {self._asym(s): s for s in chunk}
            try:
                data = self._get(ALPACA_DATA, "/v2/stocks/snapshots", symbols=",".join(amap), feed=self.stock_feed) or {}
            except BrokerAPIError as batch_err:
                if " 429" in str(batch_err) or " 401" in str(batch_err) or " 403" in str(batch_err):
                    raise                              # rate limit or keys: retrying one by one won't help
                data, last_err = {}, batch_err
                for a, s in amap.items():             # one unknown symbol fails the whole batch: retry one by one
                    try:
                        data.update(self._get(ALPACA_DATA, "/v2/stocks/snapshots", symbols=a, feed=self.stock_feed) or {})
                    except BrokerAPIError as e:
                        last_err = e
                if not data:
                    raise last_err
            for a, snap in (data or {}).items():
                s = amap.get(a)
                q = alpaca_stock_quote(snap) if s else None
                if s and q and q.get("last"):
                    out[s] = self.cache.put(("eq", s), q)
        return out

    @staticmethod
    def _asym(sym: str) -> str:
        return sym.upper().replace("/", ".").replace("-", ".")

    def option_snapshots(self, symbols: Iterable[str]) -> dict:
        """{padded OCC: quote dict}. Up to 100 contracts per request."""
        want = [padded_occ(s) for s in dict.fromkeys(symbols) if is_option_symbol(s)]
        out, missing = {}, []
        for s in want:
            hit = self.cache.get(("opt", s), 15)
            if hit is not None:
                out[s] = hit
            else:
                missing.append(s)
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            data = self._get(ALPACA_DATA, "/v1beta1/options/snapshots", symbols=",".join(compact_occ(s) for s in chunk),
                             feed=self.options_feed) or {}
            for csym, snap in (data.get("snapshots") or {}).items():
                q = alpaca_option_quote(snap)
                oi = self._oi(csym)
                if oi is not None:
                    q["oi"] = oi
                out[padded_occ(csym)] = self.cache.put(("opt", padded_occ(csym)), q)
        return out

    def contracts(self, underlying: str, days: int = 400) -> list[dict]:
        """Listed contracts of one underlying (expiration, strike, type, open interest), cached 1 h."""
        und = underlying.upper().lstrip("$^")
        hit = self.cache.get(("contracts", und), 3600)
        if hit is not None:
            return hit
        today = date.today()
        out, token = [], None
        for _ in range(10):
            data = self._get(self.base, "/v2/options/contracts", underlying_symbols=und,
                             expiration_date_gte=today.isoformat(),
                             expiration_date_lte=(today + timedelta(days=days)).isoformat(),
                             limit=10000, page_token=token) or {}
            out.extend(data.get("option_contracts") or [])
            token = data.get("next_page_token")
            if not token:
                break
        return self.cache.put(("contracts", und), out)

    def _oi(self, csym: str):
        p = _occ_parts(csym)
        if not p:
            return None
        rows = self.cache.get(("contracts", p["underlying"]), 3600)     # only if the chain was already loaded
        if not rows:
            return None
        for c in rows:
            if c.get("symbol") == csym:
                return int(_num(c.get("open_interest")))
        return None

    def strikes(self, underlying: str) -> dict:
        """{expiration ISO: set(strikes)} from the contract list."""
        out: dict = {}
        for c in self.contracts(underlying):
            exp = c.get("expiration_date")
            if exp:
                out.setdefault(exp, set()).add(_num(c.get("strike_price")))
        return out

    # ---- tastytrade-shaped answers ----
    def tt_positions(self, opened: dict | None = None) -> list[dict]:
        out = []
        for p in self.positions():
            sym = str(p.get("symbol") or "")
            is_opt = p.get("asset_class") == "us_option" or is_option_symbol(sym)
            qty = _num(p.get("qty"))
            if qty == 0:
                continue
            short = qty < 0 or str(p.get("side") or "").lower() == "short"
            key = padded_occ(sym) if is_opt else sym.replace(".", "/") if "." in sym else sym
            parts = _occ_parts(sym) if is_opt else None
            und = parts["underlying"] if parts else key
            prev = _num(p.get("lastday_price")) or _num(p.get("current_price"))
            when = (opened or {}).get(key) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
            item = {
                "account-number": self.account_label, "symbol": key,
                "instrument-type": "Equity Option" if is_opt else "Equity", "underlying-symbol": und,
                "quantity": _int_if_whole(abs(qty)), "quantity-direction": "Short" if short else "Long",
                "close-price": f"{prev:.4f}", "average-open-price": f"{_num(p.get('avg_entry_price')):.4f}",
                "multiplier": 100 if is_opt else 1, "cost-effect": "Credit" if short else "Debit",
                "created-at": when, "updated-at": when, "is-suppressed": False, "is-frozen": False,
                "realized-day-gain": "0.0", "realized-today": "0.0",
            }
            if parts:
                item["streamer-symbol"] = _streamer(parts)
                item["expires-at"] = parts["expiration"] + "T20:15:00.000+00:00"
            else:
                item["streamer-symbol"] = key
            out.append(item)
        return out

    def tt_balances(self) -> dict:
        a = self.account()
        nlv = _num(a.get("equity")) or _num(a.get("portfolio_value"))
        cash = _num(a.get("cash"))
        return {"account-number": self.account_label, "cash-balance": f"{cash:.2f}",
                "net-liquidating-value": f"{nlv:.2f}",
                "long-equity-value": f"{_num(a.get('long_market_value')):.2f}",
                "short-equity-value": f"{abs(_num(a.get('short_market_value'))):.2f}",
                "derivative-buying-power": f"{_num(a.get('options_buying_power'), _num(a.get('buying_power'))):.2f}",
                "equity-buying-power": f"{_num(a.get('buying_power')):.2f}",
                "day-trading-buying-power": f"{_num(a.get('daytrading_buying_power')):.2f}",
                "maintenance-requirement": f"{_num(a.get('maintenance_margin')):.2f}",
                "previous-day-net-liquidating-value": f"{_num(a.get('last_equity')):.2f}",
                "currency": a.get("currency") or "USD"}

    def trades(self) -> list[dict]:
        """Fills as local_broker trades (date, symbol, side, qty, price…); open/close is left to the replay."""
        out = []
        for f in self.fills():
            sym = str(f.get("symbol") or "")
            qty = _num(f.get("qty"))
            if not sym or qty <= 0:
                continue
            side = "buy" if str(f.get("side") or "").lower().startswith("b") else "sell"
            t = {"id": "alpaca-" + str(f.get("id") or "")[-24:], "date": _eastern_wall(f.get("transaction_time")),
                 "side": side, "effect": "auto", "qty": qty, "price": _num(f.get("price")), "fees": 0.0,
                 "order_id": str(f.get("order_id") or ""), "note": ""}
            p = _occ_parts(sym)
            if p:
                t.update(asset="option", symbol=p["underlying"], option_type=p["type"], strike=p["strike"],
                         expiration=p["expiration"], multiplier=100.0)
            else:
                t.update(asset="stock", symbol=sym.replace(".", "/") if "." in sym else sym, multiplier=1.0)
            out.append(t)
        return out

    def tt_orders(self) -> list[dict]:
        out = []
        for o in self.open_orders():
            if str(o.get("status") or "").lower() not in _ALPACA_WORKING:
                continue
            legs_raw = o.get("legs") if (o.get("order_class") == "mleg" and o.get("legs")) else [o]
            legs, und = [], ""
            for leg in legs_raw:
                sym = str(leg.get("symbol") or "")
                p = _occ_parts(sym)
                und = und or (p["underlying"] if p else sym)
                intent = str(leg.get("position_intent") or "")
                if "_to_" in intent:
                    side, eff = intent.split("_to_", 1)
                    action = _tt_action(side, "close" if eff == "close" else "open")
                else:
                    action = "Buy" if str(leg.get("side") or "").lower().startswith("b") else "Sell"
                legs.append({"symbol": padded_occ(sym) if p else sym, "action": action,
                             "quantity": _num(leg.get("qty") or o.get("qty")),
                             "instrument-type": "Equity Option" if p else "Equity"})
            out.append({"id": o.get("id"), "account-number": self.account_label, "underlying-symbol": und,
                        "status": "Live", "time-in-force": str(o.get("time_in_force") or "").upper(),
                        "order-type": str(o.get("type") or o.get("order_type") or "").title(),
                        "price": o.get("limit_price"), "legs": legs, "received-at": _iso(o.get("submitted_at"))})
        return out

    def describe(self) -> dict:
        a = self.account()
        return {"account": self.account_label, "paper": self.paper, "status": a.get("status"),
                "options_level": a.get("options_trading_level") or a.get("options_approved_level"),
                "stock_feed": self.stock_feed, "options_feed": self.options_feed}


def alpaca_stock_quote(snap: dict) -> dict | None:
    if not isinstance(snap, dict):
        return None
    tr, qt = snap.get("latestTrade") or {}, snap.get("latestQuote") or {}
    day, prev = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    last = _num(tr.get("p")) or _num(day.get("c"))
    if last <= 0:
        return None
    bid, ask = _num(qt.get("bp")), _num(qt.get("ap"))
    if not (0 < bid <= ask) or ask > last * 1.5 or bid < last / 1.5:   # IEX quotes outside the session can be one-sided
        bid = ask = 0.0
    return {"last": last, "prev_close": _num(prev.get("c")), "high": _num(day.get("h")), "low": _num(day.get("l")),
            "volume": _num(day.get("v")), "bid": bid, "ask": ask, "time": tr.get("t") or qt.get("t") or ""}


def alpaca_option_quote(snap: dict) -> dict:
    qt, tr = (snap or {}).get("latestQuote") or {}, (snap or {}).get("latestTrade") or {}
    g = (snap or {}).get("greeks") or {}
    day, prev = (snap or {}).get("dailyBar") or {}, (snap or {}).get("prevDailyBar") or {}
    iv = _num((snap or {}).get("impliedVolatility"))
    q = {"bid": _num(qt.get("bp")), "ask": _num(qt.get("ap")), "last": _num(tr.get("p")),
         "prev_close": _num(prev.get("c")), "iv": iv if 0.01 < iv < 5 else 0.0, "volume": int(_num(day.get("v"))),
         "oi": 0}
    for k in ("delta", "gamma", "theta", "vega", "rho"):
        if k in g and g[k] is not None:
            q[k] = _num(g[k])
    return q


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
_PUBLIC_WORKING = {"NEW", "PARTIALLY_FILLED", "PENDING_NEW", "PENDING_REPLACE", "PENDING_CANCEL", "QUEUED",
                   "OPEN", "ACCEPTED", "WORKING"}


class Public:
    name = "public"
    label = "Public"
    account_label = "PUBLIC"
    paper = False

    def __init__(self, secret: str, account_id: str = "", send: Callable | None = None, timeout: float = 20,
                 token_minutes: int = 60):
        self.secret, self.account_id = secret, (account_id or "").strip()
        self.send = send or _requests_send
        self.timeout = timeout
        self.token_minutes = token_minutes
        self._token, self._token_exp = "", 0.0
        self._lock = threading.Lock()
        self.cache = _Cache()

    def _auth(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            status, data = self.send("POST", PUBLIC_BASE + "/userapiauthservice/personal/access-tokens",
                                     {"Content-Type": "application/json", "Accept": "application/json"}, None,
                                     {"validityInMinutes": self.token_minutes, "secret": self.secret}, self.timeout)
            tok = (data or {}).get("accessToken") if isinstance(data, dict) else None
            if status >= 400 or not tok:
                raise BrokerAPIError(f"Public {status}: {_msg(data)} (check the secret key)")
            self._token, self._token_exp = tok, time.time() + self.token_minutes * 60
            return tok

    def _call(self, method: str, path: str, params: dict | None = None, body: Any = None, retry: bool = True):
        tok = self._auth()
        status, data = self.send(method, PUBLIC_BASE + path, {"Authorization": f"Bearer {tok}",
                                                              "Content-Type": "application/json",
                                                              "Accept": "application/json"}, params, body, self.timeout)
        if status == 401 and retry:
            self._token = ""
            return self._call(method, path, params, body, retry=False)
        if status >= 400:
            raise BrokerAPIError(f"Public {status}: {_msg(data)}")
        return data

    def _acct(self) -> str:
        if self.account_id:
            return self.account_id
        accts = (self._call("GET", "/userapigateway/trading/account") or {}).get("accounts") or []
        pick = [a for a in accts if a.get("accountType") == "BROKERAGE"] or accts
        if not pick:
            raise BrokerAPIError("Public: this key has no accounts")
        self.account_id = str(pick[0].get("accountId"))
        return self.account_id

    # ---- account ----
    def portfolio(self) -> dict:
        hit = self.cache.get("portfolio", 20)
        if hit is not None:
            return hit
        return self.cache.put("portfolio", self._call("GET", f"/userapigateway/trading/{self._acct()}/portfolio/v2") or {})

    def history(self, days: int = 400, max_pages: int = 40) -> list[dict]:
        hit = self.cache.get(("history", days), 300)
        if hit is not None:
            return hit
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        out, token = [], None
        for _ in range(max_pages):
            params = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "pageSize": 100}
            if token:
                params["nextToken"] = token
            data = self._call("GET", f"/userapigateway/trading/{self._acct()}/history", params=params) or {}
            out.extend(data.get("transactions") or [])
            token = data.get("nextToken")
            if not token:
                break
        out.sort(key=lambda t: t.get("timestamp") or "")
        return self.cache.put(("history", days), out)

    # ---- market data ----
    def quotes(self, symbols: Iterable[str]) -> dict:
        """{ZEUS symbol / padded OCC: quote dict} for stocks and options (one request per 100)."""
        want = [s.upper() for s in dict.fromkeys(symbols) if s and not str(s).startswith(("$", "^"))]
        out, missing = {}, []
        for s in want:
            k = padded_occ(s) if is_option_symbol(s) else s
            hit = self.cache.get(("q", k), 15)
            if hit is not None:
                out[k] = hit
            else:
                missing.append(k)
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            inst, back = [], {}
            for k in chunk:
                if is_option_symbol(k):
                    sym = compact_occ(k)
                    inst.append({"symbol": sym, "type": "OPTION"})
                else:
                    sym = k.replace("/", ".")
                    inst.append({"symbol": sym, "type": "EQUITY"})
                back[sym] = k
            data = self._call("POST", f"/userapigateway/marketdata/{self._acct()}/quotes", body={"instruments": inst}) or {}
            for q in data.get("quotes") or []:
                sym = str((q.get("instrument") or {}).get("symbol") or "").upper()
                k = back.get(sym) or back.get(compact_occ(sym))
                if not k or q.get("outcome") not in (None, "SUCCESS"):
                    continue
                out[k] = self.cache.put(("q", k), public_quote(q))
        return out

    def expirations(self, underlying: str) -> list[str]:
        und = underlying.upper().lstrip("$^")
        hit = self.cache.get(("exps", und), 3600)
        if hit is not None:
            return hit
        data = self._call("POST", f"/userapigateway/marketdata/{self._acct()}/option-expirations",
                          body={"instrument": {"symbol": und, "type": "EQUITY"}}) or {}
        return self.cache.put(("exps", und), sorted(str(x)[:10] for x in (data.get("expirations") or [])))

    def chain(self, underlying: str, expiration: str) -> dict:
        und = underlying.upper().lstrip("$^")
        hit = self.cache.get(("chain", und, expiration), 60)
        if hit is not None:
            return hit
        data = self._call("POST", f"/userapigateway/marketdata/{self._acct()}/option-chain",
                          body={"instrument": {"symbol": und, "type": "EQUITY"}, "expirationDate": expiration}) or {}
        out = {}
        for q in (data.get("calls") or []) + (data.get("puts") or []):
            sym = str((q.get("instrument") or {}).get("symbol") or "")
            if is_option_symbol(sym):
                out[padded_occ(sym)] = public_quote(q)
                self.cache.put(("q", padded_occ(sym)), out[padded_occ(sym)])
        return self.cache.put(("chain", und, expiration), out)

    def strikes(self, underlying: str, max_expirations: int = 8) -> dict:
        out: dict = {}
        for exp in self.expirations(underlying)[:max_expirations]:
            for k in self.chain(underlying, exp):
                p = _occ_parts(k)
                if p:
                    out.setdefault(exp, set()).add(p["strike"])
        return out

    # ---- tastytrade-shaped answers ----
    def tt_positions(self, opened: dict | None = None) -> list[dict]:
        out = []
        for p in self.portfolio().get("positions") or []:
            inst = p.get("instrument") or {}
            sym, typ = str(inst.get("symbol") or ""), str(inst.get("type") or "EQUITY").upper()
            if typ not in ("EQUITY", "OPTION") or not sym:
                continue               # crypto, bonds, treasuries: not shown
            qty = _num(p.get("quantity"))
            if qty == 0:
                continue
            is_opt = typ == "OPTION" or is_option_symbol(sym)
            mult = 100 if is_opt else 1
            cost = p.get("costBasis") or {}
            avg = abs(_num(cost.get("totalCost"))) / (abs(qty) * mult) if cost.get("totalCost") not in (None, "") \
                else abs(_num(cost.get("unitCost")))
            last = _num((p.get("lastPrice") or {}).get("lastPrice"))
            day = p.get("positionDailyGain") or {}
            # yesterday's price = today's price minus today's gain per share/contract
            prev = last - _num(day.get("gainValue")) / (qty * mult) if (last and day.get("gainValue") not in (None, "")) else last
            key = padded_occ(sym) if is_opt else sym.replace(".", "/")
            parts = _occ_parts(sym) if is_opt else None
            when = (opened or {}).get(key) or _iso(p.get("openedAt"))
            item = {"account-number": self.account_label, "symbol": key,
                    "instrument-type": "Equity Option" if is_opt else "Equity",
                    "underlying-symbol": parts["underlying"] if parts else key,
                    "quantity": _int_if_whole(abs(qty)), "quantity-direction": "Short" if qty < 0 else "Long",
                    "close-price": f"{max(prev, 0.0):.4f}", "average-open-price": f"{avg:.4f}", "multiplier": mult,
                    "cost-effect": "Credit" if qty < 0 else "Debit", "created-at": when, "updated-at": when,
                    "is-suppressed": False, "is-frozen": False, "realized-day-gain": "0.0", "realized-today": "0.0"}
            if parts:
                item["streamer-symbol"] = _streamer(parts)
                item["expires-at"] = parts["expiration"] + "T20:15:00.000+00:00"
            else:
                item["streamer-symbol"] = key
            out.append(item)
        return out

    def tt_balances(self) -> dict:
        pf = self.portfolio()
        bp = pf.get("buyingPower") or {}
        eq = pf.get("equity") or []
        cash = pf.get("cash")
        if cash in (None, ""):
            cash = next((e.get("value") for e in eq if str(e.get("type")).upper() == "CASH"), 0)
        total = _num(pf.get("totalAccountValue")) or sum(_num(e.get("value")) for e in eq)
        return {"account-number": self.account_label, "cash-balance": f"{_num(cash):.2f}",
                "net-liquidating-value": f"{total:.2f}",
                "derivative-buying-power": f"{_num(bp.get('optionsBuyingPower'), _num(bp.get('buyingPower'))):.2f}",
                "equity-buying-power": f"{_num(bp.get('buyingPower')):.2f}",
                "day-trading-buying-power": f"{_num(bp.get('dayTradeBuyingPower')):.2f}",
                "maintenance-requirement": "0.00", "currency": "USD"}

    def trades(self) -> list[dict]:
        out = []
        for t in self.history():
            if str(t.get("type") or "").upper() != "TRADE":
                continue
            sec = str(t.get("securityType") or "").upper()
            sym = str(t.get("symbol") or "")
            qty = abs(_num(t.get("quantity")))
            if sec not in ("EQUITY", "OPTION") or not sym or qty <= 0:
                continue
            p = _occ_parts(sym) if (sec == "OPTION" or is_option_symbol(sym)) else None
            mult = 100.0 if p else 1.0
            side = str(t.get("side") or "").lower()
            if side not in ("buy", "sell"):
                side = "buy" if _num(t.get("netAmount")) < 0 else "sell"
            principal = abs(_num(t.get("principalAmount"))) or abs(_num(t.get("netAmount")))
            row = {"id": "public-" + str(t.get("id") or "")[-24:], "date": _eastern_wall(t.get("timestamp")),
                   "side": side, "effect": "auto", "qty": qty, "price": principal / (qty * mult) if qty else 0.0,
                   "fees": abs(_num(t.get("fees"))), "order_id": "", "note": ""}
            if p:
                row.update(asset="option", symbol=p["underlying"], option_type=p["type"], strike=p["strike"],
                           expiration=p["expiration"], multiplier=100.0)
            else:
                row.update(asset="stock", symbol=sym.replace(".", "/"), multiplier=1.0)
            out.append(row)
        return out

    def tt_orders(self) -> list[dict]:
        out = []
        for o in self.portfolio().get("orders") or []:
            if str(o.get("status") or "").upper() not in _PUBLIC_WORKING:
                continue
            legs_raw = o.get("legs") or [{"instrument": o.get("instrument") or {}, "side": o.get("side"),
                                          "openCloseIndicator": o.get("openCloseIndicator")}]
            legs, und = [], ""
            for leg in legs_raw:
                sym = str((leg.get("instrument") or {}).get("symbol") or "")
                p = _occ_parts(sym)
                und = und or (p["underlying"] if p else sym)
                oc = str(leg.get("openCloseIndicator") or "").upper()
                action = _tt_action(str(leg.get("side") or "BUY"), "close" if oc == "CLOSE" else "open") if oc else \
                    ("Buy" if str(leg.get("side") or "").upper() == "BUY" else "Sell")
                legs.append({"symbol": padded_occ(sym) if p else sym, "action": action,
                             "instrument-type": "Equity Option" if p else "Equity"})
            tif = str((o.get("expiration") or {}).get("timeInForce") or "").upper()
            out.append({"id": o.get("orderId"), "account-number": self.account_label, "underlying-symbol": und,
                        "status": "Live", "time-in-force": "GTC" if tif in ("GTD", "GTC") else tif,
                        "order-type": str(o.get("type") or "").title(), "price": o.get("limitPrice"), "legs": legs,
                        "received-at": _iso(o.get("createdAt"))})
        return out

    def describe(self) -> dict:
        pf = self.portfolio()
        return {"account": self.account_label, "paper": False, "account_type": pf.get("accountType"),
                "positions": len(pf.get("positions") or [])}


def public_quote(q: dict) -> dict:
    od = (q or {}).get("optionDetails") or {}
    g = od.get("greeks") or {}
    iv = _num(g.get("impliedVolatility"))
    out = {"last": _num(q.get("last")), "bid": _num(q.get("bid")), "ask": _num(q.get("ask")),
           "prev_close": _num(q.get("previousClose")), "volume": int(_num(q.get("volume"))),
           "oi": int(_num(q.get("openInterest"))), "iv": iv if 0.01 < iv < 5 else 0.0}
    for k in ("delta", "gamma", "theta", "vega", "rho"):
        if g.get(k) not in (None, ""):
            out[k] = _num(g[k])
    if not out["last"] and out["bid"] and out["ask"]:
        out["last"] = round((out["bid"] + out["ask"]) / 2, 4)
    return out


# ---------------------------------------------------------------------------
# Helpers + the configured instances
# ---------------------------------------------------------------------------
def _int_if_whole(x: float):
    return int(x) if float(x).is_integer() else x


def _streamer(p: dict) -> str:
    """dxfeed style option symbol, e.g. .AAPL261218C200 (what the rest of ZEUS keys streaming quotes on)."""
    k = p["strike"]
    ks = str(int(k)) if float(k).is_integer() else f"{k:g}"
    return f".{p['underlying']}{p['expiration'][2:4]}{p['expiration'][5:7]}{p['expiration'][8:10]}{p['type']}{ks}"


_instances: dict = {}
_inst_lock = threading.Lock()


def alpaca() -> Alpaca | None:
    if not hc.has_alpaca():
        return None
    cfg = (hc.get("alpaca_key_id"), hc.get("alpaca_secret_key"), hc.get_bool("alpaca_paper"),
           hc.get("alpaca_stock_feed"), hc.get("alpaca_options_feed"))
    with _inst_lock:
        cur = _instances.get("alpaca")
        if cur is None or cur[0] != cfg:
            cur = (cfg, Alpaca(cfg[0], cfg[1], paper=cfg[2], stock_feed=cfg[3], options_feed=cfg[4]))
            _instances["alpaca"] = cur
        return cur[1]


def public() -> Public | None:
    if not hc.has_public():
        return None
    cfg = (hc.get("public_secret_key"), hc.get("public_account_id"))
    with _inst_lock:
        cur = _instances.get("public")
        if cur is None or cur[0] != cfg:
            cur = (cfg, Public(cfg[0], cfg[1] or ""))
            _instances["public"] = cur
        return cur[1]


def get(name: str):
    return {"alpaca": alpaca, "public": public}.get(name, lambda: None)()


def account() -> Alpaca | Public | None:
    """The broker that holds the account, when it is Alpaca or Public."""
    src = hc.account_source()
    return get(src) if src in ("alpaca", "public") else None


def market() -> Alpaca | Public | None:
    """The broker that serves quotes, when it is Alpaca or Public (None = the free sources)."""
    src = hc.market_source()
    return get(src) if src in ("alpaca", "public") else None


def reset() -> None:
    """Forget cached answers (after settings change)."""
    with _inst_lock:
        for _cfg, inst in _instances.values():
            inst.cache.clear()


def test(name: str) -> dict:
    """What the Settings page's test button shows: the account, then a stock quote and an option chain."""
    b = get(name)
    if b is None:
        return {"ok": False, "error": "fill in the keys and save first"}
    t0 = time.time()
    out: dict = {"ok": True}
    out.update(b.describe())
    try:
        out["positions"] = len(b.tt_positions())
    except BrokerAPIError as e:
        out["positions"] = f"error: {e}"
    if name == "alpaca":
        q = b.stock_snapshots(["AAPL", "SPY"])
        out["AAPL"] = q.get("AAPL", {}).get("last")
        try:
            today = date.today()
            exps = sorted(b.strikes("SPY"))
            out["SPY_expirations"] = len(exps)
            spot = q.get("SPY", {}).get("last") or 0
            data = b._get(ALPACA_DATA, "/v1beta1/options/snapshots/SPY", feed=b.options_feed, limit=200,
                          expiration_date_gte=today.isoformat(),
                          expiration_date_lte=(today + timedelta(days=45)).isoformat(),
                          strike_price_gte=round(spot * 0.95) if spot else None,
                          strike_price_lte=round(spot * 1.05) if spot else None)
            snaps = (data or {}).get("snapshots") or {}
            out["SPY_option_quotes"] = len(snaps)
            out["with_greeks"] = sum(1 for v in snaps.values() if v.get("greeks"))
        except BrokerAPIError as e:
            out["options"] = f"error: {e}"
    else:
        q = b.quotes(["AAPL", "SPY"])
        out["AAPL"] = q.get("AAPL", {}).get("last")
        try:
            exps = b.expirations("SPY")
            out["SPY_expirations"] = len(exps)
            if exps:
                ch = b.chain("SPY", exps[0])
                out["SPY_option_quotes"] = len(ch)
                out["with_greeks"] = sum(1 for v in ch.values() if "delta" in v)
        except BrokerAPIError as e:
            out["options"] = f"error: {e}"
    out["ok"] = bool(out.get("AAPL"))
    out["ms"] = int((time.time() - t0) * 1000)
    return out

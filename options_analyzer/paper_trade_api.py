"""
Paper Trade API — storage CRUD, option chain fetching, batch quotes.
"""

import json
import os
import time
import uuid
import datetime as _dt
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
STORE_PATH = DATA_DIR / "paper_trades.json"

# ── Cache for chain data (60s TTL) ────────────────────────────────

_chain_cache: dict = {}   # (ticker, exp) -> (timestamp, data)
_CHAIN_TTL = 60


# ── Storage Layer ─────────────────────────────────────────────────

def _load_store() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"groups": ["Default"], "trades": []}


def _save_store(store: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, default=str))
    os.replace(str(tmp), str(STORE_PATH))


def list_trades(group: str = None, status: str = None) -> dict:
    store = _load_store()
    trades = store["trades"]
    if group:
        trades = [t for t in trades if t.get("group") == group]
    if status and status != "all":
        trades = [t for t in trades if t.get("status") == status]
    trades.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return {"groups": store["groups"], "trades": trades}


def open_trade(trade_data: dict) -> dict:
    store = _load_store()
    trade = {
        "id": str(uuid.uuid4()),
        "group": trade_data.get("group", "Default"),
        "status": "active",
        "underlying": trade_data["underlying"],
        "name": trade_data.get("name", "Custom"),
        "current_price": trade_data.get("current_price", 0),
        "iv": trade_data.get("iv", 0.3),
        "risk_free_rate": trade_data.get("risk_free_rate", 0.045),
        "earnings_date": trade_data.get("earnings_date", ""),
        "earnings_time": trade_data.get("earnings_time", ""),
        "expiration": trade_data["expiration"],
        "created_at": _dt.datetime.now().isoformat(),
        "closed_at": None,
        "close_price": None,
        "legs": trade_data["legs"],
        "notes": trade_data.get("notes", ""),
    }
    # Ensure group exists
    if trade["group"] not in store["groups"]:
        store["groups"].append(trade["group"])
    store["trades"].append(trade)
    _save_store(store)
    return trade


def close_trade(trade_id: str, close_prices: dict) -> dict:
    store = _load_store()
    for t in store["trades"]:
        if t["id"] == trade_id:
            t["status"] = "closed"
            t["closed_at"] = _dt.datetime.now().isoformat()
            # Update leg current prices from close_prices
            for leg in t["legs"]:
                sym = leg.get("symbol", "")
                if sym in close_prices:
                    leg["current"] = close_prices[sym]
                    leg["mid"] = close_prices[sym]
            if "close_stock_price" in close_prices:
                t["close_price"] = close_prices["close_stock_price"]
            _save_store(store)
            return t
    return {"error": f"Trade {trade_id} not found"}


def delete_trade(trade_id: str) -> bool:
    store = _load_store()
    before = len(store["trades"])
    store["trades"] = [t for t in store["trades"] if t["id"] != trade_id]
    if len(store["trades"]) < before:
        _save_store(store)
        return True
    return False


def update_trade(trade_id: str, updates: dict) -> dict:
    store = _load_store()
    allowed = {"group", "notes", "name"}
    for t in store["trades"]:
        if t["id"] == trade_id:
            for k, v in updates.items():
                if k in allowed:
                    t[k] = v
            if "group" in updates and updates["group"] not in store["groups"]:
                store["groups"].append(updates["group"])
            _save_store(store)
            return t
    return {"error": f"Trade {trade_id} not found"}


def manage_groups(action: str, name: str, new_name: str = None) -> list:
    store = _load_store()
    if action == "create":
        if name not in store["groups"]:
            store["groups"].append(name)
    elif action == "rename" and new_name:
        if name in store["groups"]:
            idx = store["groups"].index(name)
            store["groups"][idx] = new_name
            for t in store["trades"]:
                if t.get("group") == name:
                    t["group"] = new_name
    elif action == "delete":
        if name in store["groups"] and name != "Default":
            store["groups"].remove(name)
            for t in store["trades"]:
                if t.get("group") == name:
                    t["group"] = "Default"
    _save_store(store)
    return store["groups"]


# ── Option Chain Fetching ─────────────────────────────────────────

def compute_streamer_symbol(occ: str) -> str:
    """Convert OCC 'AAPL  261218C00200000' → '.AAPL261218C200'"""
    parts = occ.strip().split()
    if len(parts) < 2:
        return ""
    underlying = parts[0]
    rest = parts[-1]
    opt_type = date_part = strike_raw = ""
    for i, ch in enumerate(rest):
        if ch in ("C", "P"):
            date_part = rest[:i]
            opt_type = ch
            strike_raw = rest[i + 1:]
            break
    if not opt_type:
        return ""
    strike_val = float(strike_raw) / 1000
    strike_str = f"{strike_val:.0f}" if strike_val == int(strike_val) else str(strike_val)
    return f".{underlying}{date_part}{opt_type}{strike_str}"


def fetch_chain_for_builder(client, ticker: str, expiration: str = None,
                            quotes_cache: dict = None, metrics_cache: dict = None) -> dict:
    """
    Fetch option chain for the strategy builder.
    If expiration is None, return list of all expirations.
    If expiration is given, return full strike data with bid/ask/vol/OI.
    """
    ticker = ticker.upper()
    cache_key = (ticker, expiration or "__list__")
    now = time.time()

    # Check cache
    if cache_key in _chain_cache:
        ts, cached = _chain_cache[cache_key]
        if now - ts < _CHAIN_TTL:
            return cached

    result = {
        "ticker": ticker,
        "price": 0,
        "iv": 0.3,
        "earnings_date": "",
        "earnings_time": "",
        "expirations": [],
        "strikes": [],
    }

    # Get current price — try cache, then TastyTrade, then yfinance
    try:
        if quotes_cache and quotes_cache.get(ticker):
            result["price"] = quotes_cache[ticker]
        else:
            eq = client.get_equity_quote(ticker)
            last = eq.get("last") or eq.get("last-price") or eq.get("close") or 0
            result["price"] = float(last)
    except Exception as e:
        print(f"  [paper-trade] equity quote error: {e}")
    # Fallback to yfinance if price is still 0
    if not result["price"]:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            info = t.fast_info
            result["price"] = float(info.get("lastPrice", 0) or info.get("last_price", 0))
        except Exception as e:
            print(f"  [paper-trade] yfinance fallback error: {e}")

    # Get metrics (IV, earnings)
    try:
        if metrics_cache and ticker in metrics_cache:
            m = metrics_cache[ticker]
            result["iv"] = m.get("iv", 0.3)
            result["earnings_date"] = m.get("earnings_date", "")
            result["earnings_time"] = m.get("earnings_time", "")
        else:
            mm = client.get_market_metrics([ticker])
            items = mm.get("data", {}).get("items", [])
            if items:
                mi = items[0]
                iv_val = mi.get("implied-volatility-index") or mi.get("impliedVolatilityIndex")
                result["iv"] = float(iv_val) if iv_val else 0.3
                result["earnings_date"] = (mi.get("earnings", {}).get("expected-report-date")
                                           or mi.get("earnings", {}).get("expectedReportDate", ""))
                result["earnings_time"] = (mi.get("earnings", {}).get("time-of-day")
                                           or mi.get("earnings", {}).get("timeOfDay", ""))
    except Exception as e:
        print(f"  [paper-trade] metrics error: {e}")

    # Get option chain
    try:
        chain_data = client.get_option_chain(ticker)
        top_items = chain_data.get("data", {}).get("items", [])
        all_exps = []
        for chain in top_items:
            all_exps.extend(chain.get("expirations", []))
        if not all_exps:
            all_exps = chain_data.get("data", {}).get("expirations", [])

        today = _dt.date.today()
        seen = set()
        parsed_exps = []

        for item in sorted(all_exps, key=lambda x: x.get("expiration-date", x.get("expirationDate", ""))):
            exp_str = item.get("expiration-date") or item.get("expirationDate", "")
            exp_type = item.get("expiration-type") or item.get("expirationType", "")
            if exp_str in seen:
                continue
            try:
                exp_date = _dt.date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0:
                continue
            seen.add(exp_str)
            parsed_exps.append({
                "date": exp_str,
                "dte": dte,
                "type": exp_type,
                "strikes_raw": item.get("strikes", []),
            })

        # Return expiration list
        result["expirations"] = [
            {"date": e["date"], "dte": e["dte"], "type": e["type"]}
            for e in parsed_exps
        ]

        # If a specific expiration is requested, fetch full strike data
        if expiration:
            target = None
            for e in parsed_exps:
                if e["date"] == expiration:
                    target = e
                    break
            if target:
                rows = []
                all_syms = []
                for strike in target["strikes_raw"]:
                    sp = float(strike.get("strike-price", 0))
                    call_sym = strike.get("call", "")
                    put_sym = strike.get("put", "")
                    rows.append({
                        "strike": sp,
                        "call_sym": call_sym, "put_sym": put_sym,
                        "call": {"bid": 0, "ask": 0, "mid": 0, "vol": 0, "oi": 0, "iv": 0},
                        "put": {"bid": 0, "ask": 0, "mid": 0, "vol": 0, "oi": 0, "iv": 0},
                    })
                    if call_sym:
                        all_syms.append(call_sym)
                    if put_sym:
                        all_syms.append(put_sym)

                # Batch fetch quotes
                quote_map = {}
                BATCH = 100
                for i in range(0, len(all_syms), BATCH):
                    batch = all_syms[i:i + BATCH]
                    try:
                        resp = client.get_option_quotes(batch)
                        for qi in resp.get("data", {}).get("items", []):
                            quote_map[qi.get("symbol", "")] = qi
                    except Exception:
                        pass

                def _num(v, default=0):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return default

                def _int(v):
                    try:
                        return int(float(v))
                    except (TypeError, ValueError):
                        return 0

                for r in rows:
                    for side, sym_key in [("call", "call_sym"), ("put", "put_sym")]:
                        q = quote_map.get(r[sym_key], {})
                        bid = _num(q.get("bid") or q.get("bid-price"))
                        ask = _num(q.get("ask") or q.get("ask-price"))
                        mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0
                        r[side] = {
                            "symbol": r[sym_key],
                            "streamer": compute_streamer_symbol(r[sym_key]),
                            "bid": bid,
                            "ask": ask,
                            "mid": mid,
                            "vol": _int(q.get("volume") or q.get("day-volume")),
                            "oi": _int(q.get("open-interest") or q.get("openInterest")),
                            "iv": _num(q.get("implied-volatility") or q.get("impliedVolatility")),
                        }
                    # Remove raw sym keys
                    del r["call_sym"]
                    del r["put_sym"]

                result["strikes"] = rows

    except Exception as e:
        print(f"  [paper-trade] chain error: {e}")
        import traceback
        traceback.print_exc()

    _chain_cache[cache_key] = (now, result)
    return result


_LIQ_CACHE: dict = {}      # ticker -> (timestamp, data)
_LIQ_TTL = 45


def fetch_liquidity_scan(client, ticker: str, max_dte: int = 50,
                         lo_pct: float = 0.90, hi_pct: float = 1.15,
                         quotes_cache: dict = None, metrics_cache: dict = None,
                         market_hours: bool = None) -> dict:
    """流动性体检数据源:一次链 + 一批报价(只抓 0≤dte≤max_dte、行权价在
    [spot*lo_pct, spot*hi_pct] 窗口内的 call/put,避免 N 次全链)。

    → {ticker, spot, asof, market_hours, earnings_date,
       expirations:[{date, dte, type,
                     strikes:[{strike, call:{bid,ask,mid,vol,oi,iv}, put:{...}}]}]}
    喂给 checkup.liquidity.build_scan() 出网格 / ticker 裁决 / 跨周对比。
    """
    ticker = ticker.upper()
    now = time.time()
    cached = _LIQ_CACHE.get(ticker)
    if cached and now - cached[0] < _LIQ_TTL:
        return cached[1]

    result = {"ticker": ticker, "spot": 0.0,
              "asof": _dt.datetime.now().isoformat(timespec="seconds"),
              "market_hours": market_hours, "earnings_date": "", "expirations": []}

    # spot: cache → TastyTrade → yfinance(与 fetch_chain_for_builder 同口径)
    try:
        if quotes_cache and quotes_cache.get(ticker):
            result["spot"] = float(quotes_cache[ticker])
        else:
            eq = client.get_equity_quote(ticker)
            result["spot"] = float(eq.get("last") or eq.get("last-price") or eq.get("close") or 0)
    except Exception as e:
        print(f"  [liq-scan] equity quote error: {e}")
    if not result["spot"]:
        try:
            import yfinance as yf
            fi = yf.Ticker(ticker).fast_info
            result["spot"] = float(fi.get("lastPrice", 0) or fi.get("last_price", 0))
        except Exception as e:
            print(f"  [liq-scan] yfinance fallback error: {e}")
    try:
        if metrics_cache and ticker in metrics_cache:
            result["earnings_date"] = metrics_cache[ticker].get("earnings_date", "")
    except Exception:
        pass

    spot = result["spot"]
    if not spot:
        return result
    lo, hi = spot * lo_pct, spot * hi_pct

    try:
        chain_data = client.get_option_chain(ticker)
        top_items = chain_data.get("data", {}).get("items", [])
        all_exps = []
        for chain in top_items:
            all_exps.extend(chain.get("expirations", []))
        if not all_exps:
            all_exps = chain_data.get("data", {}).get("expirations", [])

        today = _dt.date.today()
        kept, seen, all_syms = [], set(), []
        for item in sorted(all_exps, key=lambda x: x.get("expiration-date", x.get("expirationDate", ""))):
            exp_str = item.get("expiration-date") or item.get("expirationDate", "")
            if not exp_str or exp_str in seen:
                continue
            try:
                exp_date = _dt.date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > max_dte:
                continue
            seen.add(exp_str)
            exp_type = item.get("expiration-type") or item.get("expirationType", "")
            rows = []
            for strike in item.get("strikes", []):
                try:
                    sp = float(strike.get("strike-price", 0))
                except (TypeError, ValueError):
                    continue
                if sp < lo or sp > hi:
                    continue
                call_sym = strike.get("call", "")
                put_sym = strike.get("put", "")
                rows.append({"strike": sp, "call_sym": call_sym, "put_sym": put_sym})
                if call_sym:
                    all_syms.append(call_sym)
                if put_sym:
                    all_syms.append(put_sym)
            if rows:
                kept.append({"date": exp_str, "dte": dte, "type": exp_type, "rows": rows})

        quote_map = {}
        BATCH = 100
        for i in range(0, len(all_syms), BATCH):
            try:
                resp = client.get_option_quotes(all_syms[i:i + BATCH])
                for qi in resp.get("data", {}).get("items", []):
                    quote_map[qi.get("symbol", "")] = qi
            except Exception:
                pass

        def _num(v, d=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        def _int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        def _side(sym):
            q = quote_map.get(sym, {})
            bid = _num(q.get("bid") or q.get("bid-price"))
            ask = _num(q.get("ask") or q.get("ask-price"))
            return {"bid": bid, "ask": ask,
                    "mid": round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0,
                    "vol": _int(q.get("volume") or q.get("day-volume")),
                    "oi": _int(q.get("open-interest") or q.get("openInterest")),
                    "iv": _num(q.get("implied-volatility") or q.get("impliedVolatility"))}

        for e in kept:
            strikes = [{"strike": r["strike"], "call": _side(r["call_sym"]),
                        "put": _side(r["put_sym"])} for r in e["rows"]]
            result["expirations"].append({"date": e["date"], "dte": e["dte"],
                                          "type": e["type"], "strikes": strikes})
    except Exception as e:
        print(f"  [liq-scan] chain error: {e}")
        import traceback
        traceback.print_exc()

    _LIQ_CACHE[ticker] = (now, result)
    return result


def fetch_leg_quotes(client, symbols: list) -> dict:
    """Batch fetch current quotes for paper trade leg symbols."""
    result = {}
    BATCH = 100
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        try:
            resp = client.get_option_quotes(batch)
            for qi in resp.get("data", {}).get("items", []):
                sym = qi.get("symbol", "")
                bid = float(qi.get("bid") or qi.get("bid-price") or 0)
                ask = float(qi.get("ask") or qi.get("ask-price") or 0)
                result[sym] = {
                    "bid": bid,
                    "ask": ask,
                    "mid": round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0,
                    "vol": int(float(qi.get("volume") or qi.get("day-volume") or 0)),
                    "oi": int(float(qi.get("open-interest") or qi.get("openInterest") or 0)),
                }
        except Exception as e:
            print(f"  [paper-trade] quote batch error: {e}")
    return result

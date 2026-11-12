#!/usr/bin/env python3
"""
Local OptionStrat-like P/L analyzer.
Fetches positions from TastyTrade, generates interactive HTML with BSM-based P/L analysis.

Usage:
    python optionstrat_local.py
"""

import sys
import os
import json
import datetime
import time
import webbrowser
from pathlib import Path

# Add trading_assistant to path
sys.path.insert(0, str(Path(__file__).parent.parent / "trading_assistant"))
from api_client import TastyClient

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

RISK_FREE_RATE = 0.045


def main():
    print("Connecting to TastyTrade...")
    client = TastyClient()

    # Fetch accounts (multi-account safe — see api_client.pick_active_account)
    try:
        _, acct_num = client.pick_active_account()
    except RuntimeError:
        print("No TastyTrade accounts found.")
        return
    print(f"Account: {acct_num[:4]}****")

    # Fetch positions
    raw_positions = client.get_positions(acct_num)
    option_positions = [p for p in raw_positions if p.get("instrument-type") == "Equity Option"]
    equity_positions = [p for p in raw_positions if p.get("instrument-type") == "Equity"]

    if not option_positions:
        print("No option positions found.")
        return

    # Get unique underlyings
    underlyings = sorted(set(p.get("underlying-symbol", "") for p in option_positions if p.get("underlying-symbol")))
    print(f"Found {len(option_positions)} option legs across {len(underlyings)} underlyings: {', '.join(underlyings)}")

    # Fetch equity quotes via yfinance (TastyTrade quote API is unreliable)
    print("Fetching market data...")
    quotes = {}
    if HAS_YF:
        try:
            tickers_str = " ".join(underlyings)
            yf_data = yf.download(tickers_str, period="2d", progress=False)
            # yfinance returns MultiIndex columns for multi-ticker: ('Close', 'AAPL')
            if "Close" in yf_data.columns.get_level_values(0):
                close_data = yf_data["Close"]
                last_row = close_data.iloc[-1]
                for und in underlyings:
                    try:
                        price = float(last_row[und])
                        if price > 0 and not (price != price):  # not NaN
                            quotes[und] = round(price, 2)
                            print(f"  {und}: ${quotes[und]:.2f}")
                    except (KeyError, TypeError):
                        pass
        except Exception as e:
            print(f"  [WARN] yfinance bulk fetch failed: {e}")

    # Fallback for any missing quotes - try TastyTrade with rate limiting
    for und in underlyings:
        if und in quotes and quotes[und] > 0:
            continue
        try:
            time.sleep(0.5)
            q = client.get_equity_quote(und)
            last = _safe_float(q, "last", "last-price", "lastPrice")
            bid = _safe_float(q, "bid", "bid-price", "bidPrice")
            ask = _safe_float(q, "ask", "ask-price", "askPrice")
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            quotes[und] = mid if mid > 0 else last
            if quotes[und] > 0:
                print(f"  {und}: ${quotes[und]:.2f} (TastyTrade)")
        except Exception:
            pass

    # Last resort fallback: derive from option strikes/close prices
    for und in underlyings:
        if und in quotes and quotes[und] > 0:
            continue
        for p in option_positions:
            if p.get("underlying-symbol") == und:
                strike = float(p.get("strike-price", 0) or 0)
                if strike > 0:
                    quotes[und] = strike  # rough approximation
                    print(f"  {und}: ~${quotes[und]:.2f} (estimated from strike)")
                    break
        if und not in quotes or quotes[und] == 0:
            quotes[und] = 100  # absolute fallback

    # Fetch market metrics (IV, earnings) with retry
    metrics_data = {}
    try:
        time.sleep(1)
        # TastyTrade market-metrics wants comma-separated symbols param
        metrics_resp = client.get("/market-metrics", params={"symbols": ",".join(underlyings)})
        items = metrics_resp.get("data", {}).get("items", [])
        for item in items:
            symbol = item.get("symbol", "")
            iv = _safe_float(item, "implied-volatility-index", "implied-volatility-index-5-day-change")
            # Handle percentage vs decimal
            if iv > 5:  # likely percentage
                iv = iv / 100.0
            earnings = item.get("earnings", {}) or {}
            metrics_data[symbol] = {
                "iv": iv,
                "earnings_date": earnings.get("expected-report-date", "") if earnings else "",
                "earnings_time": earnings.get("time-of-day", "") if earnings else "",
            }
            if iv > 0:
                print(f"  {symbol} IV: {iv:.1%}, Earnings: {metrics_data[symbol]['earnings_date'] or 'N/A'}")
    except Exception as e:
        print(f"  [WARN] Could not get market metrics: {e}")

    # Estimate IV from yfinance if missing
    if HAS_YF:
        import math
        import numpy as np
        for und in underlyings:
            if und in metrics_data and metrics_data[und].get("iv", 0) > 0:
                continue
            try:
                hist = yf.download(und, period="60d", progress=False)
                if not hist.empty:
                    returns = hist["Close"].pct_change().dropna()
                    iv_est = float(returns.std() * math.sqrt(252))
                    metrics_data[und] = metrics_data.get(und, {})
                    metrics_data[und]["iv"] = iv_est
                    metrics_data[und].setdefault("earnings_date", "")
                    metrics_data[und].setdefault("earnings_time", "")
                    print(f"  {und} IV (historical): {iv_est:.1%}")
            except Exception:
                pass

    # Group positions into strategies
    strategies = group_into_strategies(option_positions, quotes, metrics_data)
    print(f"\nIdentified {len(strategies)} option strategies:")
    for s in strategies:
        leg_desc = " / ".join(f"{'+'if l['qty']>0 else ''}{l['qty']} {l['strike']}{l['type']}" for l in s["legs"])
        print(f"  {s['underlying']} {s['name']}: {leg_desc} exp {s['expiration']} ({s['dte']}d)")

    # Fetch option mid prices via TastyTrade DXLink streamer (bid/ask)
    enrich_with_mid_prices(client, strategies)

    # Build equity data for context
    equity_data = []
    for ep in equity_positions:
        equity_data.append({
            "symbol": ep.get("symbol", ""),
            "quantity": int(ep.get("quantity", 0)),
            "direction": ep.get("quantity-direction", "Long"),
            "entry": float(ep.get("average-open-price", 0) or 0),
            "current": float(ep.get("close-price", 0) or 0),
        })

    # Generate HTML
    print("\nGenerating HTML report...")
    html = generate_html(strategies, equity_data)

    output_path = Path(__file__).parent.parent / "trading_assistant" / "reports" / "optionstrat.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report saved to: {output_path}")

    webbrowser.open(f"file://{output_path}")


def _safe_float(d, *keys):
    """Extract first non-zero float from dict by trying multiple keys."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                fv = float(v)
                if fv != 0:
                    return fv
            except (ValueError, TypeError):
                pass
    return 0.0


def build_opening_order_map(transactions, positions):
    """Map each currently-open option leg (OCC symbol) → the order-id that
    opened the lot(s) it holds right now, reconstructed by FIFO over the trade
    history.

    Why this exists: TastyTrade's position ``created-at`` is *sticky* — it
    reports when a symbol was FIRST opened in the account, not when the current
    lot was opened. A spread whose legs were closed then re-opened (same day or
    later), or legged in across different days, therefore carries mismatched
    created-at dates, and grouping by open-date splits it into separate cards.
    The opening order-id is reliable: TastyTrade fills a multi-leg spread as ONE
    order, so legs sharing an opening order-id are one strategy.

    Uses the *most recent* still-open lot's order-id (``lots[-1]``), which is
    also robust to a truncated history window — closes only pop older lots off
    the front, so the latest open is always the current strategy's order.

    Returns {occ_symbol: order_id} for symbols that are current positions and
    whose opening order could be reconstructed. Symbols not present make the
    caller fall back to date-based grouping."""
    from collections import defaultdict, deque

    pos_syms = {(p.get("symbol") or "").strip() for p in positions}

    # Gate on instrument-type + the self-filtering `action` (only real
    # open/close trades end in "to Open"/"to Close") — matches how
    # trade_journal reads TT transactions; avoids depending on the
    # transaction-type field name.
    tx_by_sym = defaultdict(list)
    for t in transactions:
        if t.get("instrument-type") != "Equity Option":
            continue
        sym = (t.get("symbol") or "").strip()
        if sym and sym in pos_syms:
            tx_by_sym[sym].append(t)

    out = {}
    for sym, txs in tx_by_sym.items():
        txs.sort(key=lambda t: t.get("executed-at") or t.get("created-at") or "")
        lots = deque()  # FIFO of [remaining_qty, order_id]
        for t in txs:
            action = t.get("action") or ""
            try:
                qty = abs(float(t.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue
            if action.endswith("to Open"):
                lots.append([qty, t.get("order-id")])
            elif action.endswith("to Close"):
                rem = qty
                while rem > 1e-9 and lots:
                    if lots[0][0] <= rem + 1e-9:
                        rem -= lots[0][0]
                        lots.popleft()
                    else:
                        lots[0][0] -= rem
                        rem = 0.0
        if lots and lots[-1][1] is not None:
            out[sym] = lots[-1][1]
    return out


def group_into_strategies(positions, quotes, metrics, opening_orders=None):
    """Group option positions into strategies by underlying + OPENING ORDER.

    TastyTrade fills a multi-leg spread as one order, so the opening order-id is
    the reliable strategy key: it keeps a spread's legs together (even when they
    were closed+reopened or legged in on different days, which makes the sticky
    per-leg ``created-at`` disagree), and it correctly separates two distinct
    verticals that share an expiration. ``opening_orders`` is {occ_symbol:
    order_id} from build_opening_order_map().

    Falls back per-leg to underlying + OPEN DATE (created-at) when no order-id is
    available — legs opened the same day stay one strategy, so calendars and
    diagonals remain merged; expiration is the last-resort key."""
    opening_orders = opening_orders or {}
    groups = {}
    for pos in positions:
        und = pos.get("underlying-symbol", "")
        sym = (pos.get("symbol") or "").strip()
        oid = opening_orders.get(sym)
        if oid is not None:
            key = f"{und}|ord:{oid}"
        else:
            created = pos.get("created-at", "") or pos.get("opened-at", "")
            open_day = created[:10] if created else _parse_expiration(pos)
            key = f"{und}|{open_day}"
        if key not in groups:
            groups[key] = []
        groups[key].append(pos)

    strategies = []
    for key, legs_raw in groups.items():
        und = key.split("|", 1)[0]
        stock_price = quotes.get(und, 0)
        m = metrics.get(und, {})
        iv = m.get("iv", 0) if m else 0
        if iv <= 0:
            iv = 0.30
        earnings_date = m.get("earnings_date", "") if m else ""
        earnings_time = m.get("earnings_time", "") if m else ""

        legs = []
        for leg in legs_raw:
            qty = int(leg.get("quantity", 0))
            direction = leg.get("quantity-direction", "Long")
            signed_qty = qty if direction == "Long" else -qty

            symbol = leg.get("symbol", "").strip()
            streamer = leg.get("streamer-symbol", "")
            option_type = _parse_option_type(symbol)
            strike_price = _parse_strike(symbol, streamer)
            entry_price = float(leg.get("average-open-price", 0) or 0)
            current_price = float(leg.get("close-price", 0) or 0)
            multiplier = int(float(leg.get("multiplier", 100) or 100))

            created_at = leg.get("created-at", "") or leg.get("opened-at", "")

            legs.append({
                "symbol": symbol,
                "streamer_symbol": streamer,
                "type": option_type,
                "strike": strike_price,
                "qty": signed_qty,
                "entry": entry_price,
                "current": current_price,
                "prev_close": current_price,  # close-price from position = yesterday's settle
                "exp": _parse_expiration(leg),  # per-leg now (calendars span exps)
                "mult": multiplier,
                "created_at": created_at,
            })

        # Sort by expiration then strike (calendars/diagonals span expirations)
        legs.sort(key=lambda l: (l["exp"], l["type"], l["strike"]))

        # Strategy-level expiration/dte = NEAREST (front) expiration — the most
        # actionable date (e.g. a calendar's short leg). Per-leg exps are kept on
        # each leg so analyzeStrategy prices each at its own expiry.
        _exps = sorted({l["exp"] for l in legs if l.get("exp")})
        exp = _exps[0] if _exps else ""
        try:
            dte = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
        except ValueError:
            dte = 30

        name = _name_strategy(legs)

        # Earliest created_at from all legs
        leg_dates = [l["created_at"] for l in legs if l.get("created_at")]
        strat_created = min(leg_dates) if leg_dates else ""

        strategies.append({
            "name": name,
            "underlying": und,
            "current_price": stock_price,
            "iv": round(iv, 4),
            "risk_free_rate": RISK_FREE_RATE,
            "earnings_date": earnings_date,
            "earnings_time": earnings_time,
            "expiration": exp,
            "expirations": _exps,
            "dte": max(dte, 0),
            "legs": legs,
            "created_at": strat_created,
        })

    # Sort by underlying then expiration
    strategies.sort(key=lambda s: (s["underlying"], s["expiration"]))
    return strategies


def _parse_expiration(pos):
    """Parse expiration date from TastyTrade position data."""
    # Try expires-at field first: "2026-04-17T20:00:00.000+00:00"
    expires_at = pos.get("expires-at", "")
    if expires_at:
        return expires_at[:10]  # "2026-04-17"

    # Try OCC symbol: "AAPL  261218P00180000"
    symbol = pos.get("symbol", "").strip()
    parts = symbol.split()
    if len(parts) >= 2:
        rest = parts[-1]  # "260417P00135000"
        if len(rest) >= 6:
            yy = rest[0:2]
            mm = rest[2:4]
            dd = rest[4:6]
            return f"20{yy}-{mm}-{dd}"

    return ""


def _parse_strike(symbol, streamer=""):
    """Parse strike price from OCC symbol or streamer symbol.

    OCC format: "AAPL  261218C00200000" → strike = 00200000 / 1000 = 200.0
    Streamer format: ".AAPL261218C200" → strike = 200
    """
    # Try OCC symbol first
    symbol = symbol.strip()
    parts = symbol.split()
    if len(parts) >= 2:
        rest = parts[-1]  # "260515C00265000"
        # Find C or P position, strike is after it
        for i, ch in enumerate(rest):
            if ch in ("C", "P") and i + 1 < len(rest):
                strike_str = rest[i + 1:]
                try:
                    return float(strike_str) / 1000.0
                except ValueError:
                    pass

    # Try streamer symbol: ".AAPL261218C200"
    if streamer:
        s = streamer.lstrip(".")
        for i, ch in enumerate(s):
            if ch in ("C", "P") and i + 1 < len(s):
                try:
                    return float(s[i + 1:])
                except ValueError:
                    pass

    return 0.0


def _parse_option_type(symbol):
    """Parse C or P from TastyTrade OCC option symbol."""
    # OCC format: "AAPL  261218C00200000"
    symbol = symbol.strip()
    parts = symbol.split()
    if len(parts) >= 2:
        rest = parts[-1]  # "260515C00265000"
        for ch in rest:
            if ch in ("C", "P"):
                return ch
    # Fallback
    for ch in reversed(symbol):
        if ch == "C":
            return "C"
        if ch == "P":
            return "P"
    return "C"


def _name_strategy(legs):
    """Name a strategy based on its legs."""
    calls = [l for l in legs if l["type"] == "C"]
    puts = [l for l in legs if l["type"] == "P"]

    # Multi-expiration → calendar (same strike) / diagonal (different strike).
    # Grouping is by open-date, so a same-day calendar arrives here as ONE
    # strategy spanning two expirations.
    if len({l.get("exp") for l in legs}) > 1:
        if len(legs) == 2 and len(calls) == 2:
            return "Call Calendar" if calls[0]["strike"] == calls[1]["strike"] else "Call Diagonal"
        if len(legs) == 2 and len(puts) == 2:
            return "Put Calendar" if puts[0]["strike"] == puts[1]["strike"] else "Put Diagonal"
        return f"Custom Calendar ({len(legs)} legs)"

    if len(legs) == 1:
        l = legs[0]
        d = "Long" if l["qty"] > 0 else "Short"
        t = "Call" if l["type"] == "C" else "Put"
        return f"{d} {t}"

    if len(calls) == 2 and not puts:
        sc = sorted(calls, key=lambda l: l["strike"])
        if sc[0]["qty"] > 0 and sc[1]["qty"] < 0:
            return "Bull Call Spread"
        if sc[0]["qty"] < 0 and sc[1]["qty"] > 0:
            return "Bear Call Spread"
        return "Call Vertical"

    if len(puts) == 2 and not calls:
        sp = sorted(puts, key=lambda l: l["strike"])
        if sp[0]["qty"] < 0 and sp[1]["qty"] > 0:
            return "Bear Put Spread"
        if sp[0]["qty"] > 0 and sp[1]["qty"] < 0:
            return "Bull Put Spread"
        return "Put Vertical"

    if len(calls) == 1 and len(puts) == 1:
        if calls[0]["qty"] > 0 and puts[0]["qty"] > 0:
            return "Long Straddle" if calls[0]["strike"] == puts[0]["strike"] else "Long Strangle"
        if calls[0]["qty"] < 0 and puts[0]["qty"] < 0:
            return "Short Straddle" if calls[0]["strike"] == puts[0]["strike"] else "Short Strangle"

    if len(calls) == 2 and len(puts) == 2:
        sc = sorted(calls, key=lambda l: l["strike"])
        sp = sorted(puts, key=lambda l: l["strike"])
        if (sp[0]["qty"] > 0 and sp[1]["qty"] < 0 and
                sc[0]["qty"] < 0 and sc[1]["qty"] > 0):
            return "Iron Condor"
        if (sp[0]["qty"] < 0 and sp[1]["qty"] > 0 and
                sc[0]["qty"] > 0 and sc[1]["qty"] < 0):
            return "Iron Condor"

    return f"Custom ({len(legs)} legs)"


def enrich_with_mid_prices(client, strategies):
    """Fetch option mid prices from TastyTrade DXLink streamer."""
    import asyncio

    # Collect all streamer symbols we need quotes for
    symbols_map = {}  # streamer_symbol -> [(strat_idx, leg_idx)]
    for si, strat in enumerate(strategies):
        for li, leg in enumerate(strat["legs"]):
            sym = leg["symbol"].strip()
            # Build streamer symbol from OCC: "AAPL  261218C00200000" -> ".AAPL261218C200"
            parts = sym.split()
            if len(parts) >= 2:
                underlying = parts[0].strip()
                rest = parts[-1]  # "260515C00245000"
                opt_type = ""
                date_part = ""
                strike_raw = ""
                for i, ch in enumerate(rest):
                    if ch in ("C", "P"):
                        date_part = rest[:i]
                        opt_type = ch
                        strike_raw = rest[i + 1:]
                        break
                if opt_type:
                    strike_val = float(strike_raw) / 1000
                    # Format strike: remove trailing zeros
                    strike_str = f"{strike_val:g}"
                    streamer = f".{underlying}{date_part}{opt_type}{strike_str}"
                    if streamer not in symbols_map:
                        symbols_map[streamer] = []
                    symbols_map[streamer].append((si, li))

    if not symbols_map:
        return

    print(f"\nFetching mid prices from TastyTrade DXLink ({len(symbols_map)} options)...")

    try:
        # Get DXLink token
        token_resp = client.get("/api-quote-tokens")
        token_data = token_resp.get("data", {})
        dxlink_url = token_data.get("dxlink-url", "")
        token = token_data.get("token", "")
        if not dxlink_url or not token:
            print("  [WARN] Could not get DXLink token")
            return

        quotes = asyncio.get_event_loop().run_until_complete(
            _fetch_dxlink_quotes(dxlink_url, token, list(symbols_map.keys()))
        )
    except RuntimeError:
        # No event loop running
        quotes = asyncio.run(
            _fetch_dxlink_quotes(
                token_data.get("dxlink-url", ""),
                token_data.get("token", ""),
                list(symbols_map.keys()),
            )
        )
    except Exception as e:
        print(f"  [WARN] DXLink fetch failed: {e}")
        return

    # Apply mid prices to strategy legs
    updated = 0
    for sym, quote in quotes.items():
        bid = quote.get("bid", 0)
        ask = quote.get("ask", 0)
        if bid > 0 and ask > 0:
            mid = round((bid + ask) / 2, 2)
        elif bid > 0:
            mid = bid
        elif ask > 0:
            mid = ask
        else:
            continue

        if sym in symbols_map:
            for si, li in symbols_map[sym]:
                old = strategies[si]["legs"][li]["current"]
                strategies[si]["legs"][li]["mid"] = mid
                strategies[si]["legs"][li]["bid"] = bid
                strategies[si]["legs"][li]["ask"] = ask
                strategies[si]["legs"][li]["current"] = mid  # use mid as current
                updated += 1

    print(f"  Updated {updated}/{len(symbols_map)} options with mid prices")
    # Show a sample
    for strat in strategies[:2]:
        for leg in strat["legs"]:
            if "mid" in leg:
                print(f"    {strat['underlying']} {leg['strike']}{leg['type']}: "
                      f"bid={leg.get('bid',0):.2f} ask={leg.get('ask',0):.2f} mid={leg['mid']:.2f}")


async def _fetch_dxlink_quotes(url, token, symbols):
    """Connect to DXLink WebSocket and fetch option quotes."""
    import websockets
    import json as _json

    quotes = {}
    try:
        async with websockets.connect(url, close_timeout=5) as ws:
            # SETUP
            await ws.send(_json.dumps({
                "type": "SETUP",
                "channel": 0,
                "version": "0.1-js/1.0.0",
                "keepaliveTimeout": 60,
                "acceptKeepaliveTimeout": 60,
            }))
            await ws.recv()  # SETUP response

            # AUTH
            await ws.send(_json.dumps({
                "type": "AUTH",
                "channel": 0,
                "token": token,
            }))
            await ws.recv()  # AUTH_STATE

            # CHANNEL_REQUEST for FEED
            await ws.send(_json.dumps({
                "type": "CHANNEL_REQUEST",
                "channel": 1,
                "service": "FEED",
                "parameters": {"contract": "AUTO"},
            }))
            await ws.recv()  # CHANNEL_OPENED

            # FEED_SETUP - request Quote fields
            await ws.send(_json.dumps({
                "type": "FEED_SETUP",
                "channel": 1,
                "acceptAggregationPeriod": 0,
                "acceptDataFormat": "COMPACT",
                "acceptEventFields": {
                    "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"]
                },
            }))

            # Subscribe to all option symbols
            subs = [{"type": "Quote", "symbol": s} for s in symbols]
            await ws.send(_json.dumps({
                "type": "FEED_SUBSCRIPTION",
                "channel": 1,
                "add": subs,
            }))

            # Collect responses with timeout
            import asyncio
            deadline = asyncio.get_event_loop().time() + 8  # 8 second timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    data = _json.loads(msg)
                    if data.get("type") == "FEED_DATA" and data.get("channel") == 1:
                        # COMPACT format: ["Quote", [sym, bid, ask, bidSz, askSz, sym2, ...]]
                        feed = data.get("data", [])
                        if (isinstance(feed, list) and len(feed) >= 2
                                and feed[0] == "Quote" and isinstance(feed[1], list)):
                            arr = feed[1]
                            # 5 fields per quote: eventSymbol, bidPrice, askPrice, bidSize, askSize
                            chunk = 5
                            for i in range(0, len(arr) - chunk + 1, chunk):
                                sym = arr[i]
                                bid = arr[i + 1]
                                ask = arr[i + 2]
                                if isinstance(sym, str):
                                    try:
                                        b = float(bid) if bid != "NaN" and bid is not None else 0
                                        a = float(ask) if ask != "NaN" and ask is not None else 0
                                        if b > 0 or a > 0:
                                            quotes[sym] = {"bid": b, "ask": a}
                                    except (ValueError, TypeError):
                                        pass

                    # Check if we have all quotes
                    if len(quotes) >= len(symbols):
                        break
                except asyncio.TimeoutError:
                    break

    except Exception as e:
        print(f"  [WARN] DXLink WebSocket error: {e}")

    return quotes


def generate_html(strategies, equity_data):
    """Generate self-contained HTML with all data and interactive JS."""
    data = {
        "generated_at": datetime.datetime.now().isoformat(),
        "strategies": strategies,
        "equities": equity_data,
    }
    data_json = json.dumps(data, indent=2)
    return HTML_TEMPLATE.replace("/*__DATA_PLACEHOLDER__*/", data_json)


# ──────────────────────────────────────────────────────────────────────
#  HTML Template
# ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZEUS Terminal · 宙斯期权终端</title>
<style>
:root {
  --bg: #0d1117;
  --bg2: #161b22;
  --bg3: #1c2333;
  --border: #30363d;
  --text: #e6edf3;
  --text2: #8b949e;
  --green: #3fb950;
  --red: #f85149;
  --blue: #58a6ff;
  --yellow: #d29922;
  --accent: #1f6feb;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;          /* inner scrollers (dashboard-scroll) own scrolling */
  display: flex;
  flex-direction: column;
}
/* Sidebar */
.layout { display: flex; flex: 1; min-height: 0; position: relative; }
.sidebar {
  width: 150px; min-width: 150px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  overflow-y: auto;
  padding: 2px 0;
  transition: width 0.2s, min-width 0.2s, padding 0.2s, opacity 0.2s;
}
.sidebar.collapsed {
  width: 0; min-width: 0; padding: 0; overflow: hidden; opacity: 0;
  border-right: none;
}
.sidebar-toggle {
  position: absolute; left: 150px; top: 50%; transform: translateY(-50%);
  z-index: 10; width: 20px; height: 40px;
  background: var(--bg3); border: 1px solid var(--border);
  border-left: none; border-radius: 0 6px 6px 0;
  color: var(--text2); cursor: pointer; display: flex;
  align-items: center; justify-content: center; font-size: 14px;
  transition: left 0.2s;
}
.sidebar-toggle:hover { background: var(--accent); color: #fff; }
.sidebar.collapsed + .main .sidebar-toggle,
.sidebar.collapsed ~ .sidebar-toggle { left: 0; }

.sidebar-title {
  padding: 2px 8px;
  font-size: 9px;
  text-transform: uppercase;
  color: var(--text2);
  letter-spacing: 1px;
}
.strat-item {
  padding: 3px 8px;
  cursor: pointer;
  border-left: 2px solid transparent;
  line-height: 1.2;
}
.strat-item:hover { background: var(--bg3); }
.strat-item.active {
  background: var(--bg3);
  border-left-color: var(--blue);
}
.strat-ticker { font-weight: 700; font-size: 12px; }
.strat-name { font-size: 9px; color: var(--text2); }
.strat-pnl { font-size: 10px; }
.strat-pnl.pos { color: var(--green); }
.strat-pnl.neg { color: var(--red); }

/* Main */
.main { flex: 1; display: flex; flex-direction: column; overflow-y: auto; min-height: 0; scroll-behavior: smooth; }

/* Summary bar */
.summary-bar {
  display: flex;
  gap: 0;
  padding: 12px 16px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  align-items: center;
}
.summary-item {
  flex: 1;
  min-width: 140px;
  padding: 4px 16px;
  border-right: 1px solid var(--border);
  text-align: center;
}
.summary-item:last-child { border-right: none; }
.summary-label {
  font-size: 10px;
  text-transform: uppercase;
  color: var(--text2);
  letter-spacing: 0.5px;
  margin-bottom: 2px;
}
.summary-value {
  font-size: 16px;
  font-weight: 700;
}
.summary-sub { font-size: 11px; color: var(--text2); }

/* Tabs */
.tab-bar {
  display: flex;
  gap: 0;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  padding: 0 16px;
}
.tab {
  padding: 10px 20px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--text2);
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active {
  color: var(--text);
  border-bottom-color: var(--blue);
}
.tab.accent {
  background: var(--accent);
  color: #fff;
  border-radius: 6px 6px 0 0;
  border-bottom-color: var(--accent);
}

/* Content area */
.content { flex: 0 1 auto; overflow: visible; padding: 0 24px; position: relative; }

/* Table view — compact, auto-size to rows */
.pl-table-wrap {
  overflow-x: auto;
  max-width: 880px;
  margin: 0 auto;
}
.pl-table {
  border-collapse: collapse;
  font-size: 11px;
  width: 100%;
  table-layout: fixed;
}
.pl-table th, .pl-table td {
  padding: 3px 2px;
  text-align: right;
  white-space: nowrap;
  border: 1px solid rgba(48,54,61,0.4);
  overflow: hidden;
  text-overflow: ellipsis;
}
.pl-table th {
  position: sticky;
  top: 0;
  background: var(--bg2);
  z-index: 2;
  font-weight: 600;
  font-size: 11px;
  color: var(--text2);
}
.pl-table th.month-header {
  text-align: center;
  font-size: 12px;
  color: var(--text);
  background: var(--bg3);
}
.pl-table .price-col {
  position: sticky;
  left: 0;
  background: var(--bg2);
  z-index: 3;
  font-weight: 700;
  text-align: left;
  width: 60px; min-width: 60px; max-width: 60px;
}
.pl-table .pct-col {
  position: sticky;
  left: 60px;
  background: var(--bg2);
  z-index: 3;
  color: var(--text2);
  font-size: 9px;
  width: 38px; min-width: 38px; max-width: 38px;
}
/* breakeven row - no special styling, just current price row */
.pl-table .current-price-row td {
  border-top: 2px solid rgba(255,255,255,0.6) !important;
  border-bottom: 2px solid rgba(255,255,255,0.6) !important;
}
/* Thin white indicator at the EXACT spot price — matches Strategy Builder. */
.pl-table tr.sb-spot-line td {
  padding: 0 !important;
  height: 1px !important;
  background: #e6edf3 !important;
  border: none !important;
  line-height: 0 !important;
  font-size: 0 !important;
}
.pl-table .current-price-row td.price-col,
.pl-table .current-price-row td.pct-col {
  font-weight: 700;
  color: var(--cyan) !important;
}
.pl-table .custom-price-row td {
  border-top: 2px solid #f0a500 !important;
  border-bottom: 2px solid #f0a500 !important;
}
.pl-table td.earnings-col {
  position: relative;
}
.pl-table td.earnings-col::after {
  content: '';
  position: absolute;
  top: 0; bottom: 0;
  right: -1px;
  width: 2px;
  background: var(--yellow);
}
.earnings-icon { color: var(--yellow); font-size: 10px; }

/* Graph view */
/* Graph height matches table — 15 price rows + 2 header rows × ~22px ≈ 400px,
   plus 30px for the date slider. Keeps Table/Graph swap visually stable. */
.graph-container { padding: 8px; height: 400px; max-width: 880px; margin: 0 auto; }
#plChart { width: 100%; height: 100%; }

/* Controls bar */
.controls-bar {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 8px 16px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.control-group {
  display: flex;
  align-items: center;
  gap: 8px;
}
.control-label { font-size: 12px; color: var(--text2); font-weight: 600; }
/* Tabular numerics + fixed widths so dragging the IV/Range slider doesn't
   reflow the row (was: "EM: ±$18.10 (2.5%)" → "EM: ±$148.10 (49.5%)"
   stretched the EM label, pushed the IV slider, and the whole controls-bar
   visibly shifted/jittered). */
.control-value { font-size: 13px; font-weight: 700; min-width: 50px;
                 text-align: right; font-variant-numeric: tabular-nums; display: inline-block; }
#expectedMoveLabel { min-width: 160px; display: inline-block;
                     font-variant-numeric: tabular-nums; white-space: nowrap; }
input[type="range"] {
  -webkit-appearance: none;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  outline: none;
  width: 180px;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px; height: 14px;
  background: var(--blue);
  border-radius: 50%;
  cursor: pointer;
}

/* Greeks bar */
.greeks-bar {
  display: flex;
  gap: 24px;
  padding: 8px 16px;
  margin-bottom: 50px;
  background: var(--bg3);
  border-top: 1px solid var(--border);
  font-size: 13px;
  position: sticky;
  bottom: 0;
  z-index: 5;
  flex-shrink: 0;
}
.greek-item { display: flex; gap: 6px; align-items: center; }
.greek-symbol { color: var(--text2); font-weight: 600; }
.greek-value { font-weight: 700; }

/* Value mode tabs */
.value-tabs {
  display: flex;
  gap: 0;
  padding: 0 16px;
  background: var(--bg2);
}
.vtab {
  padding: 6px 14px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  color: var(--text2);
  border-radius: 6px 6px 0 0;
  transition: all 0.15s;
}
.vtab:hover { color: var(--text); }
.vtab.active { background: var(--accent); color: #fff; }

/* Date slider for graph */
.date-slider-wrap {
  padding: 8px 16px;
  background: var(--bg2);
  display: flex;
  align-items: center;
  gap: 12px;
}
#dateSlider { flex: 1; }

/* Responsive */
@media (max-width: 900px) {
  .sidebar { width: 140px; min-width: 140px; }
  .summary-item { min-width: 100px; padding: 4px 8px; }
}

/* Strategy legs detail */
.legs-detail {
  display: flex;
  gap: 16px;
  padding: 6px 16px;
  background: var(--bg3);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.leg-chip {
  background: var(--bg);
  padding: 3px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
}
.leg-long { border-color: var(--green); }
.leg-short { border-color: var(--red); }

/* ── Dashboard ── */
.dashboard { padding: 0; flex: 1; min-height: 0; display: flex; flex-direction: column; }
.dashboard-header {
  padding: 8px 20px 4px;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 30;
  background: var(--bg);
}
.dashboard-header h1 { font-size: 16px; font-weight: 700; }
.dashboard-table { width: 100%; border-collapse: collapse; }
.dashboard-table th {
  padding: 3px 10px; text-align: left; font-size: 10px; color: var(--text2);
  text-transform: uppercase; letter-spacing: 0.45px; border-bottom: 1px solid var(--border);
  background: var(--bg2); position: sticky; top: 0; z-index: 1;
}
.dashboard-table td { padding: 3px 10px; border-bottom: 1px solid rgba(48,54,61,0.2); cursor: pointer; line-height: 1.25; }
.dashboard-table tr:hover td { background: var(--bg3); }
.dash-name { font-weight: 600; font-size: 11.5px; }
.dash-return { font-weight: 700; font-size: 11.5px; }
.dash-return.pos { color: var(--green); }
.dash-return.neg { color: var(--red); }
.dash-meta { color: var(--text2); font-size: 10.5px; }
.dash-dte { font-weight: 700; font-size: 11.5px; display: inline; }
.dash-dte-sub { color: var(--text2); font-size: 9.5px; display: inline; margin-left: 4px; }
.dash-progress { display: flex; align-items: center; gap: 6px; min-width: 120px; }
.dash-progress-bar { flex: 1; height: 8px; background: rgba(99,110,123,0.25); border-radius: 4px; overflow: hidden; }
.dash-progress-fill { height: 100%; border-radius: 4px; transition: width 0.3s ease; }
.dash-progress-label { font-size: 11px; font-weight: 700; color: var(--text2); min-width: 38px; text-align: right; }

/* ── Dashboard summary stats (header strip) ── */
.dash-summary {
  display: flex; align-items: center; gap: 14px;
  font-size: 12px; color: var(--text2);
  margin: 0 18px 0 24px;
  flex-wrap: wrap;
}
.dash-summary .dash-stat {
  display: flex; align-items: baseline; gap: 5px;
  padding: 3px 9px; border-radius: 4px;
  background: rgba(99,110,123,0.10);
  white-space: nowrap;
}
.dash-summary .dash-stat strong { color: var(--text1); font-weight: 700; font-size: 13px; }
.dash-summary .dash-stat .lbl { color: var(--text3); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.dash-summary .dash-stat.warn {
  background: rgba(214, 153, 0, 0.18);
  border: 1px solid rgba(214, 153, 0, 0.45);
  color: var(--yellow);
}
.dash-summary .dash-stat.warn strong { color: var(--yellow); }
.dash-summary .dash-stat.danger {
  background: rgba(248, 81, 73, 0.18);
  border: 1px solid rgba(248, 81, 73, 0.50);
  color: var(--red);
}
.dash-summary .dash-stat.danger strong { color: var(--red); }
.dash-summary .warn-icon { margin-right: 3px; }

/* ── Spread visualization view (compact) ── */
#dashViz { padding: 2px 0 8px; }
.viz-row {
  display: grid;
  grid-template-columns: 200px 1fr 160px;
  gap: 12px;
  align-items: center;
  padding: 6px 18px 8px;
  border-bottom: 1px solid rgba(48, 54, 61, 0.35);
  cursor: pointer;
}
.viz-row:hover { background: rgba(99,110,123,0.06); }
.viz-name { font-weight: 600; font-size: 11px; line-height: 1.25; }
.viz-name-sub { color: var(--text2); font-size: 9px; font-weight: 400; margin-top: 1px; letter-spacing: 0.2px; }
.viz-bar-wrap { position: relative; height: 50px; }
.viz-bar {
  position: absolute;
  top: 22px;
  left: 0; right: 0;
  height: 10px;
  background: rgba(99,110,123,0.20);
  border-radius: 3px;
  overflow: visible;
}
.viz-bar-zone {
  position: absolute;
  top: 0; bottom: 0;
}
/* Profit + loss zones use both color AND texture for redundancy — the loss
   zone is striped so it stays distinct from the profit zone for users with
   red/green color vision deficiency. */
.viz-bar-zone-profit {
  background: rgba(46, 200, 130, 0.55);   /* brighter, slightly cyan-shifted green */
}
.viz-bar-zone-loss {
  background: repeating-linear-gradient(
    135deg,
    rgba(248, 81, 73, 0.55) 0px,
    rgba(248, 81, 73, 0.55) 3px,
    rgba(248, 81, 73, 0.18) 3px,
    rgba(248, 81, 73, 0.18) 7px
  );
}
.viz-bar-strike {
  position: absolute;
  top: -2px; bottom: -2px;
  width: 1px;
  background: rgba(230, 237, 243, 0.55);
}
.viz-bar-be {
  position: absolute;
  top: -3px; bottom: -3px;
  width: 2px;
  background: var(--yellow);
}
.viz-bar-current {
  position: absolute;
  top: -3px; bottom: -3px;
  width: 2px;
  background: #58a6ff;
  box-shadow: 0 0 4px rgba(88, 166, 255, 0.7);
}
.viz-bar-current::before {
  content: '';
  position: absolute;
  top: -5px; left: -3px;
  width: 0; height: 0;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid #58a6ff;
}
.viz-bar-tick {
  position: absolute;
  font-size: 9px;
  white-space: nowrap;
  transform: translateX(-50%);
  color: var(--text3);
  line-height: 1.05;
}
.viz-bar-tick-bottom {
  bottom: 1px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
}
.viz-bar-tick-bottom span { display: block; }
.viz-strike-role {
  font-size: 7px !important;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
}
.viz-bar-tick-current {
  top: 11px;       /* sit just above the bar (top:22) */
  color: #58a6ff;
  font-weight: 700;
  font-size: 10px;
}
.viz-bar-tick-be {
  top: 0;
  color: var(--yellow);
  font-weight: 600;
  font-size: 8px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.viz-stats {
  text-align: right;
  font-size: 11px;
  line-height: 1.3;
}
.viz-stats .stat-status { font-weight: 700; font-size: 11px; }
.viz-stats .stat-status.pos { color: var(--green); }
.viz-stats .stat-status.neg { color: var(--red); }
.viz-stats .stat-be { font-size: 11px; color: var(--text2); margin-top: 1px; }
.viz-stats .stat-be strong { color: var(--text1); font-weight: 700; }
.viz-stats .stat-days { font-size: 9px; color: var(--text3); margin-top: 1px; }

/* Freeze button visual state — when frozen, show that the user is in a
   "paused" state so the auto-thaw on navigation isn't surprising. */
.back-btn.frozen {
  background: rgba(214, 153, 0, 0.20);
  border-color: rgba(214, 153, 0, 0.55);
  color: var(--yellow);
}

/* ── Overview view: sector pie + correlation bars + events, three columns ── */
.overview-grid {
  display: grid;
  grid-template-columns: minmax(310px, 1fr) minmax(330px, 1fr) minmax(280px, 0.85fr);
  gap: 14px;
  align-items: flex-start;
}
.ov-panel {
  background: var(--bg2);
  border-radius: 8px;
  padding: 16px;
}
.ov-title {
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 12px;
}
.ov-title .ov-sub {
  font-size: 10px;
  color: var(--text2);
  font-weight: 400;
  letter-spacing: 0.3px;
  margin-left: 4px;
}
.ov-sector-svg { display: block; margin: 0 auto; }
.ov-sector-legend {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px 14px;
  margin-top: 12px;
  font-size: 11px;
}
.ov-sector-legend-item { display: flex; align-items: center; gap: 6px; }
.ov-sector-swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.ov-sector-name { color: var(--text); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-sector-pct { color: var(--text2); font-variant-numeric: tabular-nums; }
/* Floating tooltip for pie slice hover — follows cursor, no native delay */
.ov-sector-tip {
  position: fixed;
  display: none;
  pointer-events: none;
  z-index: 9999;
  background: rgba(13, 17, 23, 0.96);
  border: 1px solid var(--border);
  border-left-width: 3px;
  border-radius: 4px;
  padding: 6px 10px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
  max-width: 240px;
  white-space: normal;
}
.ov-sector-tip strong { font-size: 12px; display: block; margin-bottom: 2px; }
.ov-sector-tip .tickers { color: var(--text2); font-size: 10px; }
.ov-sector-tip .val { color: var(--text2); font-size: 10px; margin-top: 3px; font-variant-numeric: tabular-nums; }
.ov-sector-tip .val strong { display: inline; font-size: 11px; color: var(--text); margin: 0; }
.ov-sector-pct.warn { color: var(--yellow); font-weight: 700; }
.ov-warn {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 4px;
  background: rgba(214, 153, 0, 0.18);
  border: 1px solid rgba(214, 153, 0, 0.45);
  color: var(--yellow);
  font-size: 11px;
  line-height: 1.5;
}
.ov-corr-row {
  display: grid;
  grid-template-columns: 60px 1fr 70px;
  gap: 10px;
  align-items: center;
  padding: 4px 0;
  font-size: 11px;
}
.ov-corr-name { font-weight: 600; }
.ov-corr-bar-track {
  position: relative;
  height: 12px;
  background: rgba(99,110,123,0.18);
  border-radius: 3px;
  overflow: hidden;
}
.ov-corr-bar-zero {
  position: absolute;
  left: 50%; top: 0; bottom: 0;
  width: 1px;
  background: rgba(230, 237, 243, 0.4);
}
.ov-corr-bar-fill {
  position: absolute;
  top: 0; bottom: 0;
}
.ov-corr-val { text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; }
.ov-corr-loading { color: var(--text2); font-size: 11px; padding: 12px 0; }
.ov-corr-row.dim .ov-corr-name,
.ov-corr-row.dim .ov-corr-val { color: var(--text3); font-weight: 500; }
.ov-corr-toggle { float: right; display: inline-flex; gap: 2px; }
.ov-corr-toggle button {
  background: var(--bg1); color: var(--text2); border: 1px solid var(--border);
  font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 4px;
  cursor: pointer; line-height: 1.4;
}
.ov-corr-toggle button:hover { color: var(--text); border-color: var(--text3); }
.ov-corr-toggle button.active {
  background: var(--text3); color: var(--bg1); border-color: var(--text3);
}

/* Events panel — list of upcoming macro/earnings events */
.ov-event-row {
  display: grid;
  grid-template-columns: 56px 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid rgba(48, 54, 61, 0.30);
  font-size: 11px;
}
.ov-event-row:last-child { border-bottom: none; }
.ov-event-date {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  font-size: 11px;
  text-align: center;
  line-height: 1.15;
}
.ov-event-date small { display: block; font-size: 9px; font-weight: 500; color: var(--text2); }
.ov-event-date.this-week { color: var(--yellow); }
.ov-event-date.today { color: var(--red); }
.ov-event-name { line-height: 1.3; }
.ov-event-name .type {
  font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--text3); font-weight: 600; margin-right: 4px;
}
.ov-event-tier {
  font-size: 9px; font-weight: 700;
  padding: 1px 5px; border-radius: 3px;
  background: rgba(99,110,123,0.20); color: var(--text2);
  white-space: nowrap;
}
.ov-event-tier.t1 { background: rgba(248, 81, 73, 0.18); color: var(--red); }
.ov-event-tier.t2 { background: rgba(214, 153, 0, 0.18); color: var(--yellow); }
.ov-event-tier.attn {
  background: var(--red);
  color: #fff;
  animation: pulse-attn 1.6s ease-in-out infinite;
}
@keyframes pulse-attn {
  0%, 100% { box-shadow: 0 0 0 0 rgba(248,81,73,0.55); }
  50%      { box-shadow: 0 0 0 4px rgba(248,81,73,0.0); }
}
.ov-events-summary {
  font-size: 10px;
  color: var(--text2);
  margin-bottom: 6px;
  line-height: 1.4;
}
.ov-events-summary .attn-badge {
  display: inline-block;
  padding: 1px 6px; border-radius: 3px;
  background: rgba(248,81,73,0.20);
  color: var(--red);
  font-weight: 700;
  margin-right: 4px;
}
.ov-corr-band {
  position: absolute;
  top: 0; bottom: 0;
  background: rgba(99,110,123,0.10);
  border-left: 1px dashed rgba(230, 237, 243, 0.20);
  border-right: 1px dashed rgba(230, 237, 243, 0.20);
}
.ov-corr-legend {
  display: grid;
  grid-template-columns: 60px 1fr 70px;
  gap: 10px;
  font-size: 9px;
  color: var(--text3);
  margin-bottom: 6px;
}
.ov-corr-legend-axis {
  display: flex;
  justify-content: space-between;
  padding: 0 1px;
  font-variant-numeric: tabular-nums;
}
.back-btn.compact-on {
  background: rgba(88, 166, 255, 0.18);
  border-color: rgba(88, 166, 255, 0.50);
  color: #58a6ff;
}

/* ── Compact viz mode — strip everything but bar + name + status% ── */
.viz-compact .viz-row {
  padding: 2px 18px;
  grid-template-columns: 170px 1fr 130px;
  gap: 12px;
}
.viz-compact .viz-name { font-size: 11px; line-height: 1.2; }
.viz-compact .viz-name-sub { display: none; }
.viz-compact .viz-bar-wrap { height: 22px; }
.viz-compact .viz-bar { top: 6px; height: 10px; }
.viz-compact .viz-bar-current { top: -2px; bottom: -2px; }
.viz-compact .viz-bar-current::before { display: none; }   /* drop triangle */
.viz-compact .viz-bar-be { top: -2px; bottom: -2px; }
.viz-compact .viz-bar-strike { top: -1px; bottom: -1px; }
/* Hide all bar text labels in compact mode — markers only */
.viz-compact .viz-bar-tick { display: none; }
.viz-compact .viz-stats { font-size: 11px; }
.viz-compact .viz-stats .stat-status { font-size: 11px; }
.viz-compact .viz-stats .stat-be,
.viz-compact .viz-stats .stat-days { display: none; }
.risk-summary { background: var(--bg2); border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; }
.risk-summary-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(48,54,61,0.3); font-size: 13px; }
.risk-summary-row:last-child { border-bottom: none; padding-top: 8px; font-size: 14px; font-weight: 700; }
.risk-summary-label { color: var(--text2); }
.risk-summary-value { font-family: 'SF Mono', Menlo, monospace; font-weight: 700; }
.dashboard-scroll { flex: 1; min-height: 0; overflow-y: auto; }

/* Back button */
.back-btn {
  padding: 4px 14px; border-radius: 6px; background: var(--bg3);
  border: 1px solid var(--border); color: var(--text); cursor: pointer;
  font-size: 13px; display: none; align-items: center; gap: 4px;
}
.back-btn:hover { background: var(--bg2); border-color: var(--blue); }

/* Compact action-button group in the positions dashboard header — denser
   padding/font so the extra "P/L" button fits on one line. Scoped to
   .dash-actions so plain Back buttons elsewhere keep their original size. */
.dash-actions { gap: 5px; }
.dash-actions .back-btn { padding: 2px 7px; font-size: 11px; gap: 3px; }

/* ── Modal ── */
.modal-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.7); display: none; align-items: center;
  justify-content: center; z-index: 100;
}
.modal-overlay.active { display: flex; }
.modal {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 12px; padding: 24px; min-width: 480px; max-width: 560px;
  position: relative; max-height: 80vh; overflow-y: auto;
}
.modal-close {
  position: absolute; top: 12px; right: 16px; background: none;
  border: none; color: var(--text2); font-size: 20px; cursor: pointer;
}
.modal-close:hover { color: var(--text); }
.modal h2 { font-size: 18px; margin-bottom: 16px; }
.modal-total { color: var(--green); font-size: 16px; font-weight: 700; margin-bottom: 16px; }
.modal-total.neg { color: var(--red); }
.modal-leg {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 8px; background: var(--bg);
  margin-bottom: 8px; font-size: 13px;
}
.modal-leg-tag {
  font-weight: 700; font-size: 11px; padding: 2px 8px; border-radius: 4px;
}
.modal-leg-tag.bto { background: rgba(63,185,80,0.2); color: var(--green); }
.modal-leg-tag.sto { background: rgba(248,81,73,0.2); color: var(--red); }
.modal-leg-detail { flex: 1; }
.modal-leg-pnl { font-weight: 700; }
.modal-section { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.modal-section h3 { font-size: 13px; color: var(--text2); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
.modal-row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
.modal-row .label { color: var(--text2); }
.modal-toggle { display: flex; border-radius: 6px; overflow: hidden; border: 1px solid var(--border); margin-top: 8px; }
.modal-toggle button {
  flex: 1; padding: 8px 16px; background: var(--bg); border: none;
  color: var(--text2); cursor: pointer; font-size: 13px; font-weight: 600;
}
.modal-toggle button.active { background: var(--accent); color: #fff; }

/* ── Graph tooltip ── */
.graph-tooltip {
  position: absolute; pointer-events: none; display: none;
  background: rgba(22,27,34,0.95); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 12px; font-size: 12px;
  white-space: nowrap; z-index: 10;
}
.graph-tooltip .tt-price { font-weight: 700; font-size: 13px; }
.graph-tooltip .tt-pnl { font-weight: 700; }
.graph-tooltip .tt-pnl.pos { color: var(--green); }
.graph-tooltip .tt-pnl.neg { color: var(--red); }

/* Gear icon */
.gear-icon {
  cursor: pointer; opacity: 0.5; font-size: 14px; margin-left: 8px;
  transition: opacity 0.15s;
}
.gear-icon:hover { opacity: 1; }
</style>
</head>
<body>
<!-- __TAB_INJECT__ -->

<!-- Dashboard view (main page) -->
<div class="dashboard" id="dashboardView">
  <div class="dashboard-header">
    <div class="dash-summary" id="dashSummary"></div>
    <div class="dash-actions" style="display:flex;margin-left:auto">
      <button class="back-btn" style="display:flex;background:#6e40c9;color:#fff;border-color:#8957e5" onclick="openPositionsAi()" id="posAiBtn" title="AI 新闻分析: 逐仓分析多空新闻催化, 判断是否需要行动">&#129302; AI</button>
      <!-- 📡雷达 已移到 Events tab; 🩺体检 已删(出场规则经验移入 Journal Analysis);
           Overview 暂 comment(不用); Spreads 已删(用不上); Heatmap+P/L 合并为一个循环切换按钮。 -->
      <!-- <button class="back-btn" style="display:flex" onclick="openOverviewView()" id="overviewBtn" title="Overview" data-tip="Overview — 板块集中度 + SPY 相关">&#128200;</button> -->
      <button class="back-btn" style="display:flex" onclick="toggleHeatmapPnl()" id="hmPlBtn" title="视图循环: 表格 → 🔥热图 → 💵P/L bars" data-tip="🔥热图 / 💵P/L bars(点击循环)">&#128293;/&#128178;</button>
      <button class="back-btn" style="display:none" onclick="toggleDashCompact()" id="vizCompactBtn" title="Switch to a denser one-line layout — fits 20+ positions without scrolling.">&#8801; Compact</button>
      <button class="back-btn" style="display:none" onclick="toggleDashFreeze()" id="vizFreezeBtn" title="Freeze the spread visualization to analyze without it shifting on every quote update. Auto-thaws when you navigate away.">&#10052; Freeze</button>
      <button class="back-btn" style="display:flex" onclick="openRiskView()" id="riskBtn" title="Risk" data-tip="Risk — 压测(1σ/2σ/VaR)+ 到期阶梯/Theta + 情景网格">&#9888;</button>
      <button class="back-btn" style="display:flex" onclick="refreshPositions()" id="refreshBtn" title="Refresh" data-tip="刷新持仓">&#8635;</button>
      <button class="back-btn" style="display:flex" onclick="openAlertPanel()" id="alertBtn" title="Alerts" data-tip="告警设置">&#128276;</button>
    </div>
  </div>
  <div id="macroEventBanner" style="display:none;background:linear-gradient(90deg,#0d1929,#132035);border-bottom:1px solid #1f3a5f;padding:8px 14px;font-size:12px;color:#b8d4f0;line-height:1.55;cursor:pointer" onclick="switchTopTab('events')"></div>
  <div class="dashboard-scroll">
    <table class="dashboard-table" id="dashTable">
      <thead><tr>
        <th data-sort="name" data-label="Name" onclick="sortDash('name')" style="cursor:pointer">Name</th>
        <th data-sort="under" data-label="Underlying" onclick="sortDash('under')" style="cursor:pointer" title="Underlying spot today (live via dxlink) + today's %">Underlying</th>
        <th data-sort="total" data-label="Total Return" onclick="sortDash('total')" style="cursor:pointer">Total Return</th>
        <th data-sort="today" data-label="Today's Return" onclick="sortDash('today')" style="cursor:pointer">Today's Return</th>
        <th data-sort="be" data-label="B/E %" onclick="sortDash('be')" style="cursor:pointer">B/E %</th>
        <th data-sort="maxprofit" data-label="Max Profit" onclick="sortDash('maxprofit')" style="cursor:pointer">Max Profit</th>
        <th data-sort="maxloss" data-label="Max Loss" onclick="sortDash('maxloss')" style="cursor:pointer">Max Loss</th>
        <th data-sort="cop" data-label="CoP" onclick="sortDash('cop')" style="cursor:pointer">CoP</th>
        <th data-sort="dte" data-label="DTE" onclick="sortDash('dte')" style="cursor:pointer">DTE</th>
        <th data-sort="progress" data-label="Progress" onclick="sortDash('progress')" style="cursor:pointer" title="盈利:已实现占 Max Profit 的 % · 亏损:已亏占 Max Loss 的 %">Progress</th>
      </tr></thead>
      <tbody id="dashBody"></tbody>
    </table>
    <div id="dashViz" style="display:none"></div>
  </div>
</div>

<!-- Risk Analysis view (hidden by default) — 子tab: 压测 / 到期 / 情景 -->
<div class="dashboard" id="riskView" style="display:none">
  <style>
    #riskView .risk-tab { background:var(--bg3);color:var(--text2);border:1px solid var(--border);border-radius:6px;padding:4px 14px;font-size:12px;cursor:pointer;font-weight:600;display:inline-flex;align-items:center;gap:5px }
    #riskView .risk-tab.active { background:#1f6feb;color:#fff;border-color:#1f6feb }
  </style>
  <div class="dashboard-header">
    <h1>&#9888; Risk Analysis</h1>
    <div style="display:flex;gap:8px;margin-left:auto">
      <button class="back-btn" style="display:flex" onclick="exitRiskView()">&#8592; Back</button>
    </div>
  </div>
  <div class="dashboard-scroll" style="padding:0 16px">
    <div style="display:flex;gap:8px;margin:12px 0 4px">
      <button class="risk-tab active" id="riskTabStress" onclick="setRiskTab('stress')" title="明日 1σ/2σ 最差、VaR/CVaR、组合级希腊">&#9888; 压测</button>
      <button class="risk-tab" id="riskTabTime" onclick="setRiskTab('time')" title="到期阶梯 + Theta跑道 — 哪天到期压多少风险、横盘下时间价值每天赚/亏多少">&#128197; 到期</button>
      <button class="risk-tab" id="riskTabScenario" onclick="setRiskTab('scenario')" title="情景网格 — SPY涨跌×时间推进 的组合盈亏热图(β加权)">&#127919; 情景</button>
      <button class="risk-tab" id="riskTabProb" onclick="setRiskTab('prob')" title="蒙特卡洛到期盈亏分布 — 多少概率赚多少(β相关 + IV/DTE 对数正态)">&#128202; 盈亏概率</button>
    </div>
    <div id="riskPaneTime" style="display:none"><div id="timeBody"></div></div>
    <div id="riskPaneScenario" style="display:none"><div id="scenarioBody"></div></div>
    <div id="riskPaneProb" style="display:none"><div id="probBody"></div></div>
    <div id="riskPaneStress">
    <div id="riskSummary" class="risk-summary"></div>
    <div id="portfolioGreeksBlock" style="margin:16px 0"></div>
    <h3 style="margin:16px 0 8px 0;font-size:14px;color:var(--text2)">Per-Position Breakdown <span style="font-size:11px;font-weight:400">(click row to open detail, click header to sort)</span></h3>
    <div style="overflow-x:auto">
    <table class="dashboard-table" id="riskTable" style="min-width:1450px">
      <thead><tr>
        <th data-rsort="name" onclick="sortRisk('name')" style="cursor:pointer">Position</th>
        <th data-rsort="iv" onclick="sortRisk('iv')" style="cursor:pointer">IV</th>
        <th data-rsort="dte" onclick="sortRisk('dte')" style="cursor:pointer">DTE</th>
        <th data-rsort="move" onclick="sortRisk('move')" title="1σ daily move in $" style="cursor:pointer">1σ Move</th>
        <th data-rsort="worst1" onclick="sortRisk('worst1')" title="Worst P/L tomorrow at ±1σ (~68% band)" style="cursor:pointer">1σ Worst</th>
        <th data-rsort="worst2" onclick="sortRisk('worst2')" title="Worst P/L tomorrow at ±2σ (~95% band, stress)" style="cursor:pointer">2σ Worst</th>
        <th data-rsort="var" onclick="sortRisk('var')" title="VaR 95% 1-day" style="cursor:pointer">VaR 95%</th>
        <th data-rsort="cvar" onclick="sortRisk('cvar')" title="CVaR 95% — expected loss in worst 5% tail" style="cursor:pointer">CVaR 95%</th>
        <th data-rsort="maxloss" onclick="sortRisk('maxloss')" title="Max theoretical loss at expiration" style="cursor:pointer">Max Loss</th>
        <th data-rsort="maxprofit" onclick="sortRisk('maxprofit')" title="持有到期的理论最大盈利" style="cursor:pointer">Max Profit</th>
        <th data-rsort="captured" onclick="sortRisk('captured')" title="已吃到最大盈利的 %(浮盈 / 最大盈利)" style="cursor:pointer">已吃到</th>
        <th data-rsort="pop" onclick="sortRisk('pop')" title="到期盈利概率(对数正态, IV/DTE 隐含)" style="cursor:pointer">盈利概率</th>
        <th data-rsort="pmax" onclick="sortRisk('pmax')" title="到期达到最大盈利的概率" style="cursor:pointer">→满利</th>
      </tr></thead>
      <tbody id="riskBody"></tbody>
    </table>
    </div>
    <div style="margin-top:24px;padding:12px;background:var(--bg2);border-radius:6px;font-size:11px;color:var(--text2);line-height:1.6">
      <strong>Notes:</strong>
      <ul style="margin:4px 0;padding-left:20px">
        <li><strong>1σ move</strong> = S × IV × √(1/365). Assumes lognormal returns — ~68% of days stay within this band.</li>
        <li><strong>Worst case</strong> = min(P/L at S+move, P/L at S-move). No correlation assumed between positions — all are summed as independent worst cases (conservative).</li>
        <li><strong>VaR 95%</strong> uses 1-day horizon, 1.645σ move. <strong>CVaR</strong> is the average P/L beyond VaR (1.645σ to 3.5σ).</li>
      </ul>
    </div>
    </div><!-- /riskPaneStress -->
  </div>
</div>

<!-- Overview view (hidden by default) — sector pie + SPY correlation bars -->
<div class="dashboard" id="overviewView" style="display:none">
  <div class="dashboard-header">
    <h1>&#128200; Portfolio Overview</h1>
    <div style="display:flex;gap:8px;margin-left:auto">
      <button class="back-btn" style="display:flex" onclick="exitOverviewView()">&#8592; Back</button>
    </div>
  </div>
  <div class="dashboard-scroll" style="padding:12px 24px">
    <div class="overview-grid">
      <!-- Left: sector pie -->
      <div class="ov-panel">
        <div class="ov-title">Sector Concentration <span class="ov-sub">(by capital at risk)</span></div>
        <div id="ovSectorChart"></div>
        <div id="ovSectorWarn"></div>
      </div>
      <!-- Floating tooltip — shown on slice hover, follows the cursor -->
      <div id="ovSectorTip" class="ov-sector-tip"></div>
      <!-- Middle: SPY correlation -->
      <div class="ov-panel">
        <div class="ov-title">
          Correlation vs SPY
          <span class="ov-sub">(Pearson, daily returns)</span>
          <span class="ov-corr-toggle" id="ovCorrToggle">
            <button data-w="30">30d</button>
            <button data-w="90" class="active">90d</button>
            <button data-w="252">52W</button>
          </span>
        </div>
        <div id="ovCorrChart"></div>
      </div>
      <!-- Right: upcoming major events -->
      <div class="ov-panel">
        <div class="ov-title">Upcoming Events <span class="ov-sub">(next 30d · macro + earnings)</span></div>
        <div id="ovEventsList"></div>
      </div>
    </div>
    <!-- Full-width: pairwise correlation among held tickers (hidden concentration) -->
    <div class="ov-panel" style="margin-top:12px">
      <div class="ov-title">
        Holdings Correlation
        <span class="ov-sub">持仓两两 · 只高亮 |相关|&ge;0.5（红=同向/集中风险），弱相关留白</span>
      </div>
      <div id="ovCorrMatrix"><div class="ov-corr-loading">Loading…</div></div>
    </div>
  </div>
</div>

<!-- Position Heatmap view (hidden by default) — treemap of strategies sized by max loss/profit -->
<div class="dashboard" id="heatmapView" style="display:none;flex-direction:column">
  <div class="dashboard-header">
    <h1>&#128293; Position Heatmap</h1>
    <div style="display:flex;gap:8px;align-items:center;margin-left:auto">
      <label style="color:var(--text2);font-size:11px">Size</label>
      <select id="phmSize" onchange="renderHeatmapView()"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="maxloss" selected>Max Loss (capital at risk)</option>
        <option value="maxprofit">Max Profit</option>
      </select>
      <label style="color:var(--text2);font-size:11px">Color</label>
      <select id="phmColor" onchange="renderHeatmapView()"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="stock_today" selected>Stock today %</option>
        <option value="option_today">Option today %</option>
        <option value="option_total">Option since entry %</option>
        <option value="today_abs">Today P/L ($)</option>
      </select>
      <label style="color:var(--text2);font-size:11px">Group</label>
      <select id="phmGroup" onchange="renderHeatmapView()"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="sector" selected>Sector</option>
        <option value="underlying">Underlying</option>
        <option value="none">None</option>
      </select>
      <button class="back-btn" style="display:flex" onclick="exitHeatmapView();openPnlBarsView()" title="切到 P/L bars">&#128178; P/L</button>
      <button class="back-btn" style="display:flex" onclick="exitHeatmapView()">&#8592; Back</button>
    </div>
  </div>
  <div id="phmCanvas" style="flex:1;min-height:0;position:relative;background:#010409;margin:0 12px 12px 12px;border-radius:6px;border:1px solid #21262d">
    <svg id="phmSvg" style="width:100%;height:100%;display:block"></svg>
    <div id="phmTip"
      style="position:absolute;pointer-events:none;background:#161b22;
             border:1px solid #30363d;border-radius:5px;padding:7px 10px;
             font-size:11px;color:#e6edf3;line-height:1.45;display:none;
             box-shadow:0 4px 14px rgba(0,0,0,0.5);z-index:5;
             font-variant-numeric:tabular-nums;white-space:nowrap"></div>
    <div id="phmEmpty" style="position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--text2);font-size:13px">
      No positions to display.
    </div>
  </div>
</div>

<!-- P/L Bars view (hidden by default) — every position as a diverging bar:
     profit above the zero axis (green), loss below (red), sorted high→low.
     Click a bar → that strategy's detail (same enterDetail() as the heatmap). -->
<div class="dashboard" id="pnlBarsView" style="display:none;flex-direction:column">
  <div class="dashboard-header">
    <h1>&#128178; P/L Bars</h1>
    <div style="display:flex;gap:8px;align-items:center;margin-left:auto">
      <label style="color:var(--text2);font-size:11px">Metric</label>
      <select id="pbMetric" onchange="renderPnlBarsView()"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="option_total" selected>Total Return %</option>
        <option value="option_total_abs">Total Return $</option>
        <option value="option_today">Today %</option>
        <option value="today_abs">Today $</option>
        <option value="stock_today">Today Equity %</option>
      </select>
      <button class="back-btn" style="display:flex" onclick="exitPnlBarsView();openHeatmapView()" title="切到热图">&#128293; 热图</button>
      <button class="back-btn" style="display:flex" onclick="exitPnlBarsView()">&#8592; Back</button>
    </div>
  </div>
  <div id="pbCanvas" style="flex:1;min-height:0;position:relative;background:#010409;margin:0 12px 12px 12px;border-radius:6px;border:1px solid #21262d">
    <svg id="pbSvg" style="width:100%;height:100%;display:block"></svg>
    <div id="pbTip"
      style="position:absolute;pointer-events:none;background:#161b22;
             border:1px solid #30363d;border-radius:5px;padding:7px 10px;
             font-size:11px;color:#e6edf3;line-height:1.45;display:none;
             box-shadow:0 4px 14px rgba(0,0,0,0.5);z-index:5;
             font-variant-numeric:tabular-nums;white-space:nowrap"></div>
    <div id="pbEmpty" style="position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--text2);font-size:13px">
      No positions to display.
    </div>
  </div>
</div>

<!-- Detail view (hidden by default) -->
<div class="layout" id="detailView" style="display:none">
  <!-- Sidebar -->
  <div class="sidebar" id="sidebar">
    <div class="sidebar-title" style="display:flex;align-items:center;gap:8px">
      <button class="back-btn" id="backBtn" style="display:flex" onclick="exitDetail()">&#8592; Back</button>
    </div>
    <div class="sidebar-title">Strategies</div>
  </div>
  <button class="sidebar-toggle" id="sidebarToggle" onclick="toggleSidebar()">&#9664;</button>

  <!-- Main content -->
  <div class="main">
    <!-- Credit-spread direction-mistake banner -->
    <div id="creditSpreadBanner" style="display:none"></div>
    <!-- Legs detail -->
    <div class="legs-detail" id="legsDetail"></div>

    <!-- Summary bar -->
    <div class="summary-bar" id="summaryBar"></div>

    <!-- View tabs -->
    <div class="tab-bar" id="viewTabs">
      <div class="tab active" data-view="table">Table</div>
      <div class="tab" data-view="graph">Graph</div>
    </div>

    <!-- Value mode tabs -->
    <div class="value-tabs" id="valueTabs">
      <div class="vtab active" data-mode="pnl">Profit / Loss $</div>
      <div class="vtab" data-mode="pnlpct">Profit / Loss %</div>
      <div class="vtab" data-mode="value">Contract Value</div>
      <div class="vtab" data-mode="maxrisk">% of Max Risk</div>
    </div>

    <!-- Content -->
    <div class="content" id="content">
      <div class="pl-table-wrap" id="tableView">
        <table class="pl-table" id="plTable"></table>
      </div>
      <div class="graph-container" id="graphView" style="display:none">
        <div style="position:relative;width:100%;height:100%">
          <canvas id="plChart" style="width:100%;height:100%"></canvas>
          <canvas id="plChartOverlay" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none"></canvas>
        </div>
      </div>
    </div>

    <!-- Controls bar -->
    <div class="controls-bar" id="controlsBar">
      <div class="control-group">
        <span class="control-label">RANGE:</span>
        <span class="control-value" id="rangeVal">±10%</span>
        <input type="range" id="rangeSlider" min="2" max="50" value="10" step="1">
        <span id="expectedMoveLabel" style="font-size:11px;color:var(--text2);margin-left:4px"></span>
      </div>
      <div class="control-group">
        <span class="control-label">PRICE:</span>
        <input type="number" id="priceInput" style="width:70px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px" placeholder="Center" onkeydown="if(event.key==='Enter')goToPrice()">
        <button onclick="goToPrice()" style="background:var(--accent);color:#fff;border:none;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer">Go</button>
        <button id="priceClearBtn" onclick="clearPrice()" style="background:var(--bg3);color:var(--text2);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;display:none">Clear</button>
      </div>
      <div class="control-group">
        <span class="control-label">IMPLIED VOLATILITY:</span>
        <span class="control-value" id="ivVal">27%</span>
        <input type="range" id="ivSlider" min="5" max="150" value="27" step="1">
        <button id="ivResetBtn" onclick="resetIV()" style="background:var(--bg3);color:var(--text2);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;display:none">Reset</button>
      </div>
    </div>

    <!-- Date slider (for graph) -->
    <div class="date-slider-wrap" id="dateSliderWrap" style="display:none">
      <span class="control-label">DATE:</span>
      <span class="control-value" id="dateVal"></span>
      <input type="range" id="dateSlider" min="0" max="100" value="0">
    </div>

    <!-- Greeks bar -->
    <div class="greeks-bar" id="greeksBar"></div>
  </div>
</div>

<!-- Ticker news drawer disabled — code preserved in case it's needed later.
<div id="tickerNewsDrawer" style="display:none;position:fixed;bottom:40px;right:12px;width:380px;z-index:300">
  <div id="newsToggleBtn" onclick="toggleNewsDrawer()" style="display:flex;align-items:center;gap:6px;padding:6px 12px;background:rgba(22,27,34,0.92);border:1px solid #30363d;border-radius:8px;cursor:pointer;width:fit-content;margin-left:auto;margin-bottom:4px;backdrop-filter:blur(8px)">
    <span id="newsDrawerTitle" style="font-size:12px;font-weight:600;color:#e6edf3">News</span>
    <span id="newsDrawerToggle" style="color:#8b949e;font-size:14px">&#9650;</span>
  </div>
  <div id="newsPanel" style="display:none;background:rgba(13,17,23,0.88);border:1px solid #30363d;border-radius:10px;backdrop-filter:blur(12px);overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.5)">
    <div id="tickerNews" style="overflow-y:auto;max-height:350px;padding:6px 10px"></div>
  </div>
</div>
-->

<!-- Alert Config Panel -->
<div id="alertOverlay" onclick="closeAlertPanel()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:400;backdrop-filter:blur(4px)">
  <div onclick="event.stopPropagation()" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:680px;max-width:90vw;max-height:80vh;background:#0d1117;border:1px solid #30363d;border-radius:12px;overflow:hidden;box-shadow:0 16px 64px rgba(0,0,0,0.6);display:flex;flex-direction:column">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d">
      <span style="font-size:16px;font-weight:600;color:#e6edf3">&#128276; Alert Settings</span>
      <div style="display:flex;gap:12px;align-items:center">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#8b949e">
          <input type="checkbox" id="alertEnabled" onchange="saveAlertConfig()" checked> Alerts Enabled
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#8b949e" title="提醒时是否出声(独立于总开关)">
          <input type="checkbox" id="alertSound" onchange="saveAlertConfig()"> &#128266; 声音
        </label>
        <button onclick="closeAlertPanel()" style="background:none;border:none;color:#8b949e;font-size:22px;cursor:pointer">&times;</button>
      </div>
    </div>
    <div style="padding:16px 20px;overflow-y:auto;flex:1">
      <!-- 止盈 = 下面的 regime 退出止盈警戒(唯一止盈源);不设成本止损(借方价差不止损) -->
      <!-- Exit / take-profit guard — server-side native push even when PWA closed -->
      <div style="margin-bottom:20px;padding:12px 14px;background:rgba(248,81,73,0.06);border:1px solid rgba(248,81,73,0.35);border-radius:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-size:13px;font-weight:700;color:#f85149">&#128680; 退出止盈警戒 <span style="font-weight:400;color:#8b949e;font-size:11px">— 后台推送，PWA 关着也提醒</span></span>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#8b949e">
            <input type="checkbox" id="alertExitEnabled" onchange="saveAlertConfig()" checked> 启用
          </label>
        </div>
        <div style="display:flex;gap:16px;align-items:flex-end">
          <div style="flex:1">
            <label style="font-size:11px;color:#8b949e;text-transform:uppercase">牛市(SPY&gt;200SMA)吃到 &ge; %</label>
            <input type="number" id="alertExitBullPct" value="70" min="40" max="100" step="5" onchange="saveAlertConfig()"
              style="width:100%;margin-top:4px;background:#0d1117;color:#3fb950;border:1px solid #30363d;border-radius:6px;padding:6px 8px;font-size:14px;text-align:right;box-sizing:border-box">
          </div>
          <div style="flex:1">
            <label style="font-size:11px;color:#8b949e;text-transform:uppercase">震荡/弱(&le;200SMA)吃到 &ge; %</label>
            <input type="number" id="alertExitChopPct" value="50" min="30" max="100" step="5" onchange="saveAlertConfig()"
              style="width:100%;margin-top:4px;background:#0d1117;color:#d29922;border:1px solid #30363d;border-radius:6px;padding:6px 8px;font-size:14px;text-align:right;box-sizing:border-box">
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:14px;margin-top:10px;font-size:11px;color:#c9d1d9">
          <span style="color:#8b949e">提醒频率:</span>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type="radio" name="alertExitFreq" value="daily" onchange="saveAlertConfig()" checked> 每标的每天一次</label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type="radio" name="alertExitFreq" value="always" onchange="saveAlertConfig()"> 每次达成都提醒<span style="color:#8b949e">(最快 1 分钟一次)</span></label>
        </div>
        <div id="alertRegimeNow" style="font-size:10px;color:#8b949e;margin-top:8px"><b>这是唯一的止盈源</b>(不设成本止损)。<b>regime 自适应 · 两档</b>(真实期权链回测 §9)— 上面是<b>软止盈</b>(建议落袋);<b>强制</b>=软+10%(牛 80 / 震荡 60)。牛市=SPY&gt;200SMA;<b>VIX&ge;22</b> 或跌破 200SMA &rarr; 按震荡(波动大、利润跑得快 &rarr; 早止盈)。<b>剩余 DTE&lt;20</b> &rarr; 提前到软档就强制。100% 死等每段都最差,借方价差不止损。仅交易日。</div>
      </div>
      <!-- Credit-spread direction-mistake guard — native push even when PWA closed -->
      <div style="margin-bottom:20px;padding:12px 14px;background:rgba(255,159,10,0.07);border:1px solid rgba(255,159,10,0.40);border-radius:8px">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <span style="font-size:13px;font-weight:700;color:#ff9f0a">&#9888;&#65039; Credit Spread 开反提醒 <span style="font-weight:400;color:#8b949e;font-size:11px">— 新开的 2 腿垂直价差若是净 credit 立即推送</span></span>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#8b949e">
            <input type="checkbox" id="alertCreditEnabled" onchange="saveAlertConfig()" checked> 启用
          </label>
        </div>
        <div style="font-size:10px;color:#8b949e;margin-top:8px">你常把想开的 debit spread 开成了 credit spread（方向反了）。开单后若检测到新的净-credit 垂直价差，立刻推送哪个 ticker，让你重新检查。每个价差只提醒一次。</div>
      </div>
    </div>
    <div style="padding:10px 20px;background:#161b22;border-top:1px solid #30363d;display:flex;justify-content:space-between;align-items:center">
      <span id="alertPermStatus" style="font-size:11px;color:#8b949e"></span>
      <button onclick="requestNotifyPermission()" style="background:#1f6feb;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">Enable Notifications</button>
    </div>
  </div>
</div>

<!-- Alert toast container -->
<div id="alertToasts" style="position:fixed;top:60px;right:16px;z-index:500;display:flex;flex-direction:column;gap:8px"></div>

<!-- 秒出 tooltip(替代原生 title 的 ~1s 延迟)-->
<div id="ckTip" style="display:none;position:fixed;z-index:600;max-width:340px;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:6px 9px;font-size:11px;color:#e6edf3;box-shadow:0 4px 16px rgba(0,0,0,.5);pointer-events:none;line-height:1.45"></div>

<!-- 交易体检 modal(开单前预警 + 持仓止盈止损) -->
<div id="checkupOverlay" onclick="closeCheckup()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:401;backdrop-filter:blur(5px)">
  <div onclick="event.stopPropagation()" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:920px;max-width:95vw;max-height:90vh;background:#0d1117;border:1px solid #30363d;border-radius:14px;overflow:hidden;box-shadow:0 16px 64px rgba(0,0,0,.6);display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:16px;padding:12px 18px;background:#161b22;border-bottom:1px solid #30363d">
      <span style="font-size:16px;font-weight:700;color:#e6edf3">&#129658; 交易体检</span>
      <div style="display:flex;gap:6px">
        <button id="ckTabPre" onclick="switchCheckupTab('pre')" style="background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer">开单前</button>
        <button id="ckTabPos" onclick="switchCheckupTab('pos')" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer">持仓止盈止损</button>
      </div>
      <button onclick="closeCheckup()" style="margin-left:auto;background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer;line-height:1">&times;</button>
    </div>
    <!-- ⚠ 未挂止盈 GTC 提醒(读 /api/missing_gtc,由 loadMissingGtc 填充) -->
    <div id="missingGtcBanner" style="display:none;background:#3d2411;border-bottom:1px solid #58421e;padding:9px 18px;font-size:12px;color:#f0b429;line-height:1.5"></div>
    <div style="overflow-y:auto;flex:1;padding:16px 18px">
      <div id="ckPre">
        <div id="ckCommon" style="margin-bottom:12px"></div>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
          <input id="ckTickers" placeholder="想买什么? 例: AAPL MSFT   或   MAG7" onkeydown="if(event.key==='Enter')runCheckup()" style="flex:1;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:9px 12px;font-size:14px">
          <button onclick="runCheckup()" style="background:#1f6feb;color:#fff;border:none;border-radius:8px;padding:9px 22px;font-size:14px;font-weight:600;cursor:pointer">体检</button>
          <button onclick="openLiquidity()" title="期权流动性扫描:逐行权价 bid/ask/OI 网格 + 同一价差跨周对比,看这票/这个行权价/这周能不能按 mid 成交" style="background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:8px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer">&#127754; 流动性</button>
        </div>
        <div style="border:1px solid #30363d;border-radius:8px;margin-bottom:12px;overflow:hidden">
          <div onclick="toggleCkSpread()" id="ckSpreadHdr" style="cursor:pointer;padding:8px 12px;background:#161b22;color:#8b949e;font-size:12px">&#9656; 我要开的价差(选填,填了才做价格体检 + 方向/OTM/赔率检查)</div>
          <div id="ckSpreadBody" style="display:none;padding:12px;background:#0d1117">
            <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px">
              <input id="ckSpreadTicker" placeholder="Ticker" style="width:90px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px">
              <button onclick="ckLoadChain()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 12px;cursor:pointer">拉期权链</button>
              <select id="ckType" onchange="ckLoadStrikes()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px"><option value="C">Call 看涨价差</option><option value="P">Put 看跌价差</option></select>
              <select id="ckExp" onchange="ckLoadStrikes()" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px"></select>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px;margin-top:8px">
              <span style="color:#8b949e">买</span><select id="ckLong" style="background:#0d1117;color:#3fb950;border:1px solid #30363d;border-radius:6px;padding:6px 8px;max-width:220px"></select>
              <span style="color:#8b949e">卖</span><select id="ckShort" style="background:#0d1117;color:#f85149;border:1px solid #30363d;border-radius:6px;padding:6px 8px;max-width:220px"></select>
              <span style="color:#8b949e">张数</span><input id="ckQty" type="number" value="1" min="1" style="width:56px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px">
              <span style="color:#8b949e">打算付 $</span><input id="ckNet" type="number" step="0.01" placeholder="留空=中间价" style="width:120px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 8px">
            </div>
            <div id="ckSpot" style="color:#8b949e;font-size:11px;margin-top:6px"></div>
          </div>
        </div>
        <div id="ckRecResult"></div>
        <div id="ckPreResult"></div>
      </div>
      <div id="ckPos" style="display:none"><div id="ckPosResult"></div></div>
    </div>
  </div>
</div>

<!-- 流动性体检(全屏:逐行权价 bid/ask/OI 网格 + 同一价差跨周对比) -->
<div id="liquidityOverlay" onclick="closeLiquidity()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:402;backdrop-filter:blur(6px)">
  <div onclick="event.stopPropagation()" style="position:absolute;inset:3vh 2vw;background:#0d1117;border:1px solid #30363d;border-radius:14px;overflow:hidden;box-shadow:0 16px 64px rgba(0,0,0,.6);display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:10px;padding:11px 16px;background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap">
      <span style="font-size:15px;font-weight:700;color:#e6edf3;white-space:nowrap">&#127754; 流动性体检</span>
      <input id="liqTicker" placeholder="Ticker" onkeydown="if(event.key==='Enter')loadLiquidity()" style="width:110px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:7px 10px;font-size:14px;text-transform:uppercase">
      <button onclick="loadLiquidity()" style="background:#1f6feb;color:#fff;border:none;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600;cursor:pointer">扫描</button>
      <span id="liqSpot" style="color:#8b949e;font-size:13px;white-space:nowrap"></span>
      <span style="margin-left:auto;display:flex;gap:8px;align-items:center">
        <button id="liqSideCall" onclick="liqSwitchSide('call')" style="background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer">Calls</button>
        <button id="liqSidePut" onclick="liqSwitchSide('put')" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer">Puts</button>
        <button onclick="closeLiquidity()" style="background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer;line-height:1;padding:0 2px">&times;</button>
      </span>
    </div>
    <div id="liqBody" style="overflow:auto;flex:1;padding:14px 16px"></div>
  </div>
</div>

<!-- 风险雷达(独立 modal,不在体检里) -->
<div id="riskRadarOverlay" onclick="closeRiskRadar()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:401;backdrop-filter:blur(5px)">
  <div onclick="event.stopPropagation()" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:920px;max-width:95vw;max-height:90vh;background:#0d1117;border:1px solid #30363d;border-radius:14px;overflow:hidden;box-shadow:0 16px 64px rgba(0,0,0,.6);display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:14px;padding:12px 18px;background:#161b22;border-bottom:1px solid #30363d">
      <span style="font-size:16px;font-weight:700;color:#e6edf3">&#128225; 风险雷达</span>
      <span style="color:#6e7681;font-size:11px">今日新闻风险 + 未来30天事件密度 · research/drop_events 实证</span>
      <button onclick="closeRiskRadar()" style="margin-left:auto;background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer;line-height:1">&times;</button>
    </div>
    <div id="rrTabBar" style="display:flex;gap:6px;padding:8px 18px 0;background:#161b22;border-bottom:1px solid #30363d">
      <button id="rrTabRadar" onclick="setRRTab('radar')" style="background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600">&#128225; 实时雷达</button>
      <button id="rrTabPlaybook" onclick="setRRTab('playbook')" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600">&#128202; 异动原因</button>
    </div>
    <div style="overflow-y:auto;flex:1;padding:16px 18px">
      <div id="rrPaneRadar"><div id="rrResult"></div></div>
      <div id="rrPanePlaybook" style="display:none"></div>
    </div>
  </div>
</div>

<!-- TradingView chart floating modal -->
<div id="tvOverlay" onclick="closeTvChart()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:400;backdrop-filter:blur(4px)">
  <div onclick="event.stopPropagation()" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:85vw;height:75vh;max-width:1100px;max-height:650px;background:rgba(15,15,15,0.95);border:1px solid #30363d;border-radius:12px;overflow:hidden;box-shadow:0 16px 64px rgba(0,0,0,0.6)">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 16px;background:#161b22;border-bottom:1px solid #30363d">
      <span id="tvChartTitle" style="font-size:14px;font-weight:600;color:#e6edf3"></span>
      <button onclick="closeTvChart()" style="background:none;border:none;color:#8b949e;font-size:22px;cursor:pointer;padding:0 4px">&times;</button>
    </div>
    <div id="tvChartBody" style="width:100%;height:calc(100% - 40px)"></div>
  </div>
</div>

<!-- Position details modal -->
<div class="modal-overlay" id="positionModal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <h2>Positions</h2>
    <div id="modalContent"></div>
  </div>
</div>

<!-- Earnings history modal: injected at body level by server -->

<!-- Graph tooltip -->
<div class="graph-tooltip" id="graphTooltip"></div>

<script>
// ── Embedded Data ──
const RAW_DATA = /*__DATA_PLACEHOLDER__*/;

// ── BSM Model (JavaScript) ──
// Absolute instant of 16:00 America/New_York ("the close") for a YYYY-MM-DD
// expiration. Correct across EST/EDT and INDEPENDENT of the machine's own
// timezone: 16:00 ET is 20:00 UTC under EDT (Mar–Nov) and 21:00 UTC under EST,
// so we probe both and keep whichever actually reads as 16:00 in New York.
//
// ⚠️ Why this exists: bare `new Date(dateStr + 'T16:00:00')` parses as LOCAL
// 16:00. On a non-ET machine (this box runs Central) that lands an hour+ past
// the true 4pm-ET expiry, so 0DTE P/L / COP / Greeks / expected-move kept
// residual time value and expiration formations (flat max profit/loss) never
// showed — a 0DTE vertical read ~590–990 instead of a flat ±max below/above
// the strikes. Every expiry-INSTANT computation must route through here; only
// LOCAL clock LABELS (column headers) use marketClose. See gotcha memo.
function nyClose(dateStr) {
  // Memoized + one shared formatter. A per-call new Intl.DateTimeFormat is
  // ~330x slower than new Date (~236us/call); called per-leg every P/L render
  // it froze the page. Same date -> same absolute instant, so cache it.
  const key = String(dateStr);
  const C = nyClose._cache || (nyClose._cache = {});
  if (C[key] !== undefined) return new Date(C[key]);
  const parts = key.split('-');
  const Y = +parts[0], M = +parts[1], D = +parts[2];
  if (!(Y > 0 && M > 0 && D > 0)) { C[key] = NaN; return new Date(NaN); }
  const F = nyClose._fmt || (nyClose._fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', hour12: false, hour: '2-digit'
  }));
  let ms = Date.UTC(Y, M - 1, D, 20, 0, 0);
  for (const uh of [20, 21]) {
    const cand = new Date(Date.UTC(Y, M - 1, D, uh, 0, 0));
    if ((parseInt(F.format(cand), 10) % 24) === 16) { ms = cand.getTime(); break; }
  }
  C[key] = ms;
  return new Date(ms);
}

function normCDF(x) {
  const a1=0.254829592,a2=-0.284496736,a3=1.421413741,a4=-1.453152027,a5=1.061405429,p=0.3275911;
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x) / Math.sqrt(2);
  const t = 1.0 / (1.0 + p * x);
  const y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * Math.exp(-x*x);
  return 0.5 * (1.0 + sign * y);
}

function normPDF(x) {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

function bsmPrice(S, K, T, r, sigma, type) {
  if (T <= 0) return type === 'C' ? Math.max(0, S - K) : Math.max(0, K - S);
  if (sigma <= 0.001) sigma = 0.001;
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*sqrtT);
  const d2 = d1 - sigma*sqrtT;
  if (type === 'C') return S*normCDF(d1) - K*Math.exp(-r*T)*normCDF(d2);
  return K*Math.exp(-r*T)*normCDF(-d2) - S*normCDF(-d1);
}

function bsmGreeks(S, K, T, r, sigma, type) {
  if (T <= 0) {
    const itm = type === 'C' ? S > K : S < K;
    return {delta: itm ? (type==='C'?1:-1) : 0, gamma:0, theta:0, vega:0, rho:0};
  }
  if (sigma <= 0.001) sigma = 0.001;
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*sqrtT);
  const d2 = d1 - sigma*sqrtT;
  const nd1 = normPDF(d1);
  let delta, theta, rho;
  if (type === 'C') {
    delta = normCDF(d1);
    theta = (-S*nd1*sigma/(2*sqrtT) - r*K*Math.exp(-r*T)*normCDF(d2)) / 365;
    rho = K*T*Math.exp(-r*T)*normCDF(d2) / 100;
  } else {
    delta = normCDF(d1) - 1;
    theta = (-S*nd1*sigma/(2*sqrtT) + r*K*Math.exp(-r*T)*normCDF(-d2)) / 365;
    rho = -K*T*Math.exp(-r*T)*normCDF(-d2) / 100;
  }
  return {
    delta, theta, rho,
    gamma: nd1 / (S * sigma * sqrtT),
    vega: S * nd1 * sqrtT / 100
  };
}

// Implied volatility solver (Newton-Raphson on BSM price)
function impliedVol(S, K, T, r, marketPrice, type, guess) {
  let sigma = guess || 0.3;
  for (let i = 0; i < 50; i++) {
    const price = bsmPrice(S, K, T, r, sigma, type);
    const vega = S * normPDF((Math.log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*Math.sqrt(T))) * Math.sqrt(T);
    if (vega < 1e-10) break;
    const diff = price - marketPrice;
    if (Math.abs(diff) < 0.001) break;
    sigma -= diff / vega;
    if (sigma <= 0.001) sigma = 0.001;
    if (sigma > 5) sigma = 5;
  }
  return sigma;
}

// ── Strategy Analysis ──
function analyzeStrategy(strat, ivOverride) {
  const S = strat.current_price;
  const r = strat.risk_free_rate;
  const iv = ivOverride != null ? ivOverride : strat.iv;
  const today = new Date();
  const expDate = nyClose(strat.expiration);
  const dte = Math.max(0, (expDate - today) / 86400000);
  const T = dte / 365;

  // Net debit (positive = paid, negative = received)
  let netDebit = 0;
  strat.legs.forEach(l => { netDebit += l.entry * l.qty * l.mult; });

  // Current value & unrealized P/L — pure mark-to-market, matches TastyTrade's
  // "P/L Opn" column. Fees are realized separately on close, not in unrealized.
  let currentValue = 0;
  strat.legs.forEach(l => { currentValue += l.current * l.qty * l.mult; });
  const totalContracts = strat.legs.reduce((s, l) => s + Math.abs(l.qty), 0);
  const unrealizedPnl = currentValue - netDebit;
  const unrealizedPct = netDebit !== 0 ? (unrealizedPnl / Math.abs(netDebit)) * 100 : 0;

  // Today's P/L = sum((current - prev_close) * qty * mult). Falls back to 0 if
  // prev_close is missing (e.g., position opened today — TT shows same as P/L Opn).
  let todayPnl = 0;
  let haveAnyPrevClose = false;
  strat.legs.forEach(l => {
    const pc = l.prev_close || 0;
    if (pc > 0) {
      todayPnl += (l.current - pc) * l.qty * l.mult;
      haveAnyPrevClose = true;
    }
  });
  if (!haveAnyPrevClose) todayPnl = unrealizedPnl;

  // Greeks (sum across legs, using per-leg implied vol)
  const greeks = {delta:0, gamma:0, theta:0, vega:0, rho:0};
  strat.legs.forEach(l => {
    // Solve per-leg IV from market mid price for accurate Greeks
    const legMid = l.mid || l.current;
    const legIV = (legMid > 0 && T > 0) ? impliedVol(S, l.strike, T, r, legMid, l.type, iv) : iv;
    const g = bsmGreeks(S, l.strike, T, r, legIV, l.type);
    greeks.delta += g.delta * l.qty * l.mult;
    greeks.gamma += g.gamma * l.qty * l.mult;
    greeks.theta += g.theta * l.qty * l.mult;
    greeks.vega += g.vega * l.qty * l.mult;
    greeks.rho += g.rho * l.qty * l.mult;
  });

  // Max profit / loss — evaluated at the EARLIEST leg expiration.
  // For single-expiration strategies (verticals, condors), every leg is at
  // intrinsic. For calendars/diagonals, the front-month leg expires while
  // the back-month leg still has time value (BSM theo). This matches what
  // OptionStrat shows under "Max Profit/Loss".
  const _legExps = strat.legs.map(l => nyClose(l.exp || strat.expiration));
  // IV slider override comes in as `iv`. Apply as a multiplicative SCALE
  // on each leg's own iv so skew is preserved while the slider still
  // moves all legs uniformly. If the leg has no iv (or strat.iv is 0)
  // fall back to the override directly.
  const _ivScale = (strat.iv > 0 && iv > 0) ? iv / strat.iv : 1;
  const _legIvs = strat.legs.map(l => (+l.iv > 0 ? +l.iv * _ivScale : iv));
  const earliestExp = new Date(Math.min(..._legExps.map(d => d.getTime())));
  // Scan range MUST span every strike (with margin), not just ±50% of spot.
  // A far-OTM spread (e.g. SOXS spot 5.67, strikes 8/10) never reaches its flat
  // max-profit region within ±50% of spot, so maxProfit/maxLoss would clip at
  // the moving range edge and DRIFT with spot. Anchor the bounds to the strikes
  // ∪ spot so the payoff's flat regions (above the top strike / below the
  // bottom) are always covered → stable, correct max/min for defined-risk legs.
  const _ks = strat.legs.map(l => l.strike).filter(k => k > 0);
  const _loP = Math.max(0.01, Math.min(S, ..._ks) * 0.5);
  const _hiP = Math.max(S, ..._ks) * 1.5;
  const _nP = 400;
  const _stepP = (_hiP - _loP) / _nP;
  const priceRange = [];
  for (let _i = 0; _i <= _nP; _i++) priceRange.push(_loP + _i * _stepP);
  let maxProfit = -Infinity, maxLoss = Infinity;
  priceRange.forEach(price => {
    let pnl = 0;
    strat.legs.forEach((l, li) => {
      const T_at = Math.max(0, (_legExps[li] - earliestExp) / (365 * 86400000));
      let val;
      if (T_at <= 0) {
        val = l.type === 'C' ? Math.max(0, price - l.strike)
                             : Math.max(0, l.strike - price);
      } else {
        val = bsmPrice(price, l.strike, T_at, r, _legIvs[li], l.type);
      }
      pnl += (val - l.entry) * l.qty * l.mult;
    });
    if (pnl > maxProfit) maxProfit = pnl;
    if (pnl < maxLoss) maxLoss = pnl;
  });

  // Breakeven at expiration (find zero crossings)
  const breakevens = findBreakevens(strat, iv);

  // Chance of profit (lognormal probability)
  let cop = 0;
  if (breakevens.length > 0 && T > 0 && iv > 0) {
    cop = calcChanceOfProfit(S, breakevens, T, r, iv, strat);
  }

  return {
    netDebit, maxProfit, maxLoss, breakevens, cop,
    unrealizedPnl, unrealizedPct, todayPnl, greeks, iv, dte, T
  };
}

// ── Risk analysis (stress test, VaR/CVaR) ──

// Compute P/L at a future stock price `priceFwd` and remaining time `T_fwd` years.
// Uses BSM at leg IV (from analyzeStrategy via arg). At expiration (T_fwd<=0),
// uses intrinsic value. Returns P/L in dollars.
function pnlAtFuture(strat, priceFwd, T_fwd, r, iv) {
  let pnl = 0;
  strat.legs.forEach(l => {
    let val;
    if (T_fwd > 0) {
      val = bsmPrice(priceFwd, l.strike, T_fwd, r, iv, l.type);
    } else {
      val = l.type === 'C' ? Math.max(0, priceFwd - l.strike) : Math.max(0, l.strike - priceFwd);
    }
    pnl += (val - l.entry) * l.qty * l.mult;
  });
  return pnl;
}

// Stress test: worst P/L at ±sigma_mult × daily σ move, 1 trading day forward.
function computeStressTest(strat, analysis, sigmaMult) {
  const S = strat.current_price;
  const iv = analysis.iv || strat.iv;
  const r = strat.risk_free_rate;
  const T_tmrw = Math.max(0, analysis.T - 1/365);
  const dailySigma = iv * Math.sqrt(1/365);
  const move = S * dailySigma * sigmaMult;
  const pnlUp = pnlAtFuture(strat, S + move, T_tmrw, r, iv);
  const pnlDown = pnlAtFuture(strat, Math.max(0.01, S - move), T_tmrw, r, iv);
  return { move, pnlUp, pnlDown, worst: Math.min(pnlUp, pnlDown), best: Math.max(pnlUp, pnlDown) };
}

// VaR and CVaR (95% confidence, 1-day horizon).
function computeVarCvar(strat, analysis) {
  const Z_VAR = 1.645; // 95% one-tail
  const Z_MAX = 3.5;   // ~99.98% — tail bound for CVaR average
  const S = strat.current_price;
  const iv = analysis.iv || strat.iv;
  const r = strat.risk_free_rate;
  const T_tmrw = Math.max(0, analysis.T - 1/365);
  const dailySigma = iv * Math.sqrt(1/365);

  // VaR: worst-direction P/L at Z_VAR σ
  const moveVar = S * dailySigma * Z_VAR;
  const pUp = pnlAtFuture(strat, S + moveVar, T_tmrw, r, iv);
  const pDown = pnlAtFuture(strat, Math.max(0.01, S - moveVar), T_tmrw, r, iv);
  const varValue = Math.min(pUp, pDown);

  // CVaR: average worst-direction P/L sampled across [Z_VAR, Z_MAX]
  const N = 12;
  let sum = 0;
  for (let i = 1; i <= N; i++) {
    const z = Z_VAR + (Z_MAX - Z_VAR) * (i / N);
    const m = S * dailySigma * z;
    const u = pnlAtFuture(strat, S + m, T_tmrw, r, iv);
    const d = pnlAtFuture(strat, Math.max(0.01, S - m), T_tmrw, r, iv);
    sum += Math.min(u, d);
  }
  return { var: varValue, cvar: sum / N };
}

function openRiskView() {
  appNavInto();
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('riskView').style.display = '';
  renderRiskView();
}

function exitRiskView() {
  document.getElementById('riskView').style.display = 'none';
  document.getElementById('dashboardView').style.display = '';
}

// Risk 子tab：stress(压测) / time(到期阶梯+Theta跑道) / scenario(情景网格)
let riskTab = 'stress';
let _probForce = false, _probSig = null, _probLastRun = 0;   // 盈亏概率:防每个tick重算(闪);仅切tab/结构变/手动/每小时 重算
function setRiskTab(tab) {
  riskTab = tab;
  if (tab === 'prob') _probForce = true;   // 只有主动切到本tab才重算
  const panes = { stress: 'riskPaneStress', time: 'riskPaneTime', scenario: 'riskPaneScenario', prob: 'riskPaneProb' };
  const btns = { stress: 'riskTabStress', time: 'riskTabTime', scenario: 'riskTabScenario', prob: 'riskTabProb' };
  Object.keys(panes).forEach(k => {
    const p = document.getElementById(panes[k]); if (p) p.style.display = k === tab ? '' : 'none';
    const b = document.getElementById(btns[k]); if (b) b.classList.toggle('active', k === tab);
  });
  renderRiskView();
}

// ════════ 到期阶梯 + Theta 跑道（Risk 子tab，纯客户端，复用 analyzeStrategy/pnlAtFuture）═══
function renderTimeView() {
  const body = document.getElementById('timeBody');
  const strats = (RAW_DATA.strategies || []).filter(s => s.legs && s.legs.length);
  if (!strats.length) { body.innerHTML = '<div style="padding:24px;color:var(--text2)">当前无持仓</div>'; return; }
  const NLV = liveNLV();
  const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)).toLocaleString();
  const col = v => v >= 0 ? 'var(--green)' : 'var(--red)';
  const rows = [];
  strats.forEach(s => { let a; try { a = analyzeStrategy(s); } catch (e) { return; } rows.push({ s, a }); });

  // ── 到期阶梯：按到期日分组 ──
  const byExp = {};
  rows.forEach(({ s, a }) => { const e = s.expiration || '?'; (byExp[e] = byExp[e] || []).push({ s, a }); });
  let netTheta = 0; rows.forEach(({ a }) => netTheta += a.greeks.theta);
  let ladderRows = '';
  Object.keys(byExp).sort().forEach(e => {
    const grp = byExp[e]; const dte = grp[0].a.dte; const hot = dte <= 7;
    let ml = 0, up = 0, th = 0;
    grp.forEach(({ a }) => { ml += (a.maxLoss <= -1e9 ? 0 : a.maxLoss); up += a.unrealizedPnl; th += a.greeks.theta; });
    const names = grp.map(g => g.s.underlying).join(' ');
    ladderRows += '<tr style="' + (hot ? 'background:rgba(248,81,73,0.08)' : '') + '">' +
      '<td style="padding:6px 10px;font-weight:700;color:' + (hot ? 'var(--red)' : 'var(--text)') + '">' + e + (hot ? ' &#9888;' : '') + '</td>' +
      '<td style="text-align:right;padding:6px 10px">' + Math.round(dte) + 'd</td>' +
      '<td style="text-align:right;padding:6px 10px">' + grp.length + '</td>' +
      '<td style="text-align:right;padding:6px 10px;color:var(--red)">' + money(ml) + '</td>' +
      '<td style="text-align:right;padding:6px 10px;color:' + col(up) + '">' + money(up) + '</td>' +
      '<td style="text-align:right;padding:6px 10px;color:' + col(th) + '">' + money(th) + '</td>' +
      '<td style="padding:6px 10px;color:var(--text2);font-size:11px">' + names + '</td></tr>';
  });
  const ladder =
    '<h3 style="margin:14px 0 6px;font-size:14px">&#128197; 到期阶梯（按到期日，≤7天标红）</h3>' +
    '<div style="overflow-x:auto"><table class="dashboard-table" style="width:100%;border-collapse:collapse;font-size:12px">' +
    '<thead><tr style="color:var(--text2)"><th style="text-align:left;padding:6px 10px">到期日</th><th style="text-align:right;padding:6px 10px">DTE</th>' +
    '<th style="text-align:right;padding:6px 10px">仓数</th><th style="text-align:right;padding:6px 10px">合计最大亏损</th>' +
    '<th style="text-align:right;padding:6px 10px">未实现盈亏</th><th style="text-align:right;padding:6px 10px">净Theta/天</th>' +
    '<th style="text-align:left;padding:6px 10px">标的</th></tr></thead><tbody>' + ladderRows + '</tbody></table></div>';

  // ── Theta 跑道：未来14天横盘累计时间衰减盈亏 ──
  const base = rows.reduce((t, { s, a }) => t + pnlAtFuture(s, s.current_price, a.T, s.risk_free_rate, a.iv), 0);
  const days = [], cum = [];
  for (let d = 0; d <= 14; d++) {
    let v = 0;
    rows.forEach(({ s, a }) => { const T2 = Math.max(0, a.T - d / 365); v += pnlAtFuture(s, s.current_price, T2, s.risk_free_rate, a.iv); });
    days.push(d); cum.push(v - base);
  }
  const d7 = cum[7], d14 = cum[14];
  const runwayNote = '横盘下未来 <b>7天</b> 约 <b style="color:' + col(d7) + '">' + money(d7) + '</b>' +
    (NLV > 0 ? '（' + (d7 / NLV * 100).toFixed(1) + '% NLV）' : '') +
    '、<b>14天</b> 约 <b style="color:' + col(d14) + '">' + money(d14) + '</b>。净 Theta <b style="color:' + col(netTheta) + '">' + money(netTheta) + '/天</b>（>0=收时间价值，<0=付）。';

  let thetaRows = '';
  rows.slice().sort((x, y) => Math.abs(y.a.greeks.theta) - Math.abs(x.a.greeks.theta)).slice(0, 8).forEach(({ s, a }) => {
    thetaRows += '<tr><td style="padding:3px 10px;color:var(--blue);font-weight:700">' + s.underlying + '</td>' +
      '<td style="padding:3px 10px;color:var(--text2);font-size:11px">' + (s.name || '') + '</td>' +
      '<td style="text-align:right;padding:3px 10px;color:' + col(a.greeks.theta) + '">' + money(a.greeks.theta) + '/天</td>' +
      '<td style="text-align:right;padding:3px 10px">' + Math.round(a.dte) + 'd</td></tr>';
  });

  body.innerHTML = ladder +
    '<h3 style="margin:18px 0 6px;font-size:14px">&#9203; Theta 跑道（横盘·只算时间衰减）</h3>' +
    '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:13px;line-height:1.7;margin-bottom:10px">' + runwayNote + '</div>' +
    '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px"><canvas id="thetaCanvas" style="width:100%;height:200px;display:block"></canvas></div>' +
    '<h3 style="margin:14px 0 6px;font-size:13px;color:var(--text2)">Theta 贡献最大的仓</h3>' +
    '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px"><tbody>' + thetaRows + '</tbody></table></div>';
  drawThetaRunway(days, cum);
}
function drawThetaRunway(days, cum) {
  const cv = document.getElementById('thetaCanvas'); if (!cv) return;
  const ctx = cv.getContext('2d'), dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect(), W = rect.width || 600, H = rect.height || 200;
  cv.width = W * dpr; cv.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const pad = { l: 56, r: 14, t: 12, b: 24 }, gW = W - pad.l - pad.r, gH = H - pad.t - pad.b;
  let yMin = Math.min(0, ...cum), yMax = Math.max(0, ...cum); const yp = (yMax - yMin) * 0.12 || 50; yMin -= yp; yMax += yp;
  const X = d => pad.l + d / 14 * gW, Y = v => pad.t + gH - (v - yMin) / (yMax - yMin) * gH;
  ctx.font = '10px monospace'; ctx.strokeStyle = 'rgba(120,130,150,0.15)';
  for (let i = 0; i <= 4; i++) { const v = yMin + (yMax - yMin) * i / 4, y = Y(v); ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke(); ctx.fillStyle = '#8b949e'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText((v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)), pad.l - 6, y); }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  [0, 3, 7, 10, 14].forEach(d => { const x = X(d); ctx.fillStyle = '#8b949e'; ctx.fillText('+' + d + 'd', x, pad.t + gH + 5); });
  ctx.strokeStyle = 'rgba(200,200,210,0.3)'; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(pad.l, Y(0)); ctx.lineTo(W - pad.r, Y(0)); ctx.stroke(); ctx.setLineDash([]);
  ctx.lineWidth = 2; ctx.strokeStyle = '#d29922'; ctx.beginPath();
  days.forEach((d, i) => { const x = X(d), y = Y(cum[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
}

// ════════ 情景网格 · 2D 压力热图（Risk 子tab，SPY 移动 × 时间，β加权）════════════════
function _gaussRand() {
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
// 蒙特卡洛:到期组合盈亏分布。每标的按 IV/DTE 对数正态、用 β 与大盘(SPY)单因子相关 + 独立项;
// SPY 年化波动优先用实时 VIX。返回排序后的盈亏样本 + 分位数 + 盈利概率。
function portfolioPnlMC(nSims) {
  const strats = (RAW_DATA.strategies || []).filter(s => s.legs && s.legs.length && s.current_price > 0);
  if (!strats.length) return null;
  const SPY_VOL = (_ckRegime && _ckRegime.vix) ? _ckRegime.vix / 100 : 0.16;
  const P = []; let curMark = 0;
  for (const s of strats) {
    let a; try { a = analyzeStrategy(s); } catch (e) { continue; }
    curMark += a.unrealizedPnl || 0;
    const S = s.current_price, iv = (a.iv || s.iv || 0.3), r = s.risk_free_rate || 0.04;
    const T = Math.max(1 / 365, (a.dte || 1) / 365);
    const beta = (_pfBeta.betas && _pfBeta.betas[s.underlying] != null) ? _pfBeta.betas[s.underlying] : 1.0;
    let rho = iv > 0 ? beta * SPY_VOL / iv : 0;
    rho = Math.max(-0.95, Math.min(0.95, rho));
    P.push({ legs: s.legs, S, sigT: iv * Math.sqrt(T), drift: (r - 0.5 * iv * iv) * T, rho, idio: Math.sqrt(Math.max(0, 1 - rho * rho)) });
  }
  if (!P.length) return null;
  const intr = (p, k, t) => t === 'C' ? Math.max(0, p - k) : Math.max(0, k - p);
  const pnls = new Float64Array(nSims);
  for (let n = 0; n < nSims; n++) {
    const Zm = _gaussRand(); let pnl = 0;
    for (const q of P) {
      const z = q.rho * Zm + q.idio * _gaussRand();
      const ST = q.S * Math.exp(q.drift + q.sigT * z);
      for (const l of q.legs) pnl += (intr(ST, l.strike, l.type) - l.entry) * l.qty * l.mult;
    }
    pnls[n] = pnl;
  }
  pnls.sort();
  const pct = q => pnls[Math.min(nSims - 1, Math.max(0, Math.floor(q * nSims)))];
  let sum = 0, nProf = 0, nBeat = 0;
  for (let i = 0; i < nSims; i++) { sum += pnls[i]; if (pnls[i] > 0) nProf++; if (pnls[i] >= curMark) nBeat++; }
  return { pnls, nSims, SPY_VOL, curMark, mean: sum / nSims, pProfit: nProf / nSims, pBeat: nBeat / nSims,
    p5: pct(0.05), p25: pct(0.25), p50: pct(0.50), p75: pct(0.75), p95: pct(0.95) };
}
function renderProbView() {
  const body = document.getElementById('probBody'); if (!body) return;
  // full_refresh/softRefresh 每个价格 tick 都调 renderRiskView → 这里若每次重跑随机MC会一直闪。
  // 只在 ① 主动切到本tab(_probForce) ② 持仓结构变化 ③ 手动🔄 时重算;否则保持现状不动。
  const rv = document.getElementById('riskView');
  if (rv && rv.style.display === 'none') return;
  const sig = (RAW_DATA.strategies || []).map(s => s.underlying + ':' + (s.legs || []).length + ':' + (s.expiration || '')).join('|');
  const force = _probForce; _probForce = false;
  const fresh = _probLastRun && (Date.now() - _probLastRun) < 3600000;   // 1 小时内算新鲜 → 不重算
  if (!force && body.dataset.done === '1' && sig === _probSig && fresh) return;
  _probSig = sig; _probLastRun = Date.now();
  body.innerHTML = '<div style="padding:24px;color:var(--text2)">⏳ 蒙特卡洛模拟到期盈亏…</div>';
  setTimeout(() => {
    let R; try { R = portfolioPnlMC(8000); } catch (e) { body.innerHTML = '<div style="padding:24px;color:var(--red)">模拟失败: ' + e + '</div>'; return; }
    if (!R) { body.innerHTML = '<div style="padding:24px;color:var(--text2)">当前无可模拟持仓</div>'; return; }
    const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)).toLocaleString();
    const pctS = x => (x * 100).toFixed(0) + '%';
    const spread = (R.p95 - R.p5) || 1, lo = R.p5 - spread * 0.12, hi = R.p95 + spread * 0.12;
    const NB = 41, bw = (hi - lo) / NB, bins = new Array(NB).fill(0);
    for (let i = 0; i < R.nSims; i++) { const x = R.pnls[i]; if (x >= lo && x < hi) bins[Math.min(NB - 1, Math.floor((x - lo) / bw))]++; }
    const maxB = Math.max(1, ...bins);
    const W = 700, H = 210, padL = 8, padB = 18, plotH = H - padB - 8;
    const xOf = v => padL + (v - lo) / (hi - lo) * (W - 2 * padL);
    let bars = '';
    for (let i = 0; i < NB; i++) {
      const bx = padL + i / NB * (W - 2 * padL), bwpx = (W - 2 * padL) / NB - 1, bh = bins[i] / maxB * plotH, mid = lo + (i + 0.5) * bw;
      bars += `<rect x="${bx.toFixed(1)}" y="${(H - padB - bh).toFixed(1)}" width="${bwpx.toFixed(1)}" height="${bh.toFixed(1)}" fill="${mid >= 0 ? 'rgba(63,185,80,.7)' : 'rgba(248,81,73,.7)'}"/>`;
    }
    const vline = (v, c, lbl) => (v >= lo && v <= hi) ? `<line x1="${xOf(v).toFixed(1)}" y1="8" x2="${xOf(v).toFixed(1)}" y2="${H - padB}" stroke="${c}" stroke-width="1.5" stroke-dasharray="3,2"/><text x="${xOf(v).toFixed(1)}" y="6" fill="${c}" font-size="9" text-anchor="middle">${lbl}</text>` : '';
    const svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;background:#0d1117;border:1px solid #30363d;border-radius:8px">${bars}<line x1="${xOf(0).toFixed(1)}" y1="8" x2="${xOf(0).toFixed(1)}" y2="${H - padB}" stroke="#8b949e" stroke-width="1"/>${vline(R.curMark, '#58a6ff', '现在')}${vline(R.p50, '#e3b341', '中位')}<text x="${padL}" y="${H - 4}" fill="#8b949e" font-size="9">${money(lo)}</text><text x="${W - padL}" y="${H - 4}" fill="#8b949e" font-size="9" text-anchor="end">${money(hi)}</text></svg>`;
    const tr = (lbl, v, note) => `<tr><td style="padding:4px 10px;color:var(--text2)">${lbl}</td><td style="padding:4px 10px;text-align:right;color:${v >= 0 ? 'var(--green)' : 'var(--red)'};font-weight:700">${money(v)}</td><td style="padding:4px 10px;color:var(--text2);font-size:11px">${note || ''}</td></tr>`;
    const tbl = `<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px">
      ${tr('最坏 5% (P5)', R.p5, '95% 概率好于此')}${tr('悲观 (P25)', R.p25, '')}${tr('中位 (P50)', R.p50, '一半概率好于此')}${tr('乐观 (P75)', R.p75, '')}${tr('最好 5% (P95)', R.p95, '仅 5% 概率达到')}
      <tr><td colspan="3" style="border-top:1px solid #30363d;padding-top:4px"></td></tr>
      ${tr('期望(均值)', R.mean, '概率加权平均')}
      <tr><td style="padding:4px 10px;color:var(--text2)">盈利概率</td><td style="padding:4px 10px;text-align:right;color:var(--green);font-weight:700">${pctS(R.pProfit)}</td><td style="padding:4px 10px;color:var(--text2);font-size:11px">到期为正</td></tr>
      <tr><td style="padding:4px 10px;color:var(--text2)">扛到期 &gt; 现在</td><td style="padding:4px 10px;text-align:right;color:var(--text);font-weight:700">${pctS(R.pBeat)}</td><td style="padding:4px 10px;color:var(--text2);font-size:11px">现在 ${money(R.curMark)},继续拿能更好的概率</td></tr>
    </table>`;
    const updatedAt = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    body.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin:10px 0 6px"><div style="flex:1;font-size:12px;color:var(--text2)">蒙特卡洛 ${R.nSims.toLocaleString()} 次模拟「持有到期」组合盈亏:每标的按 IV/DTE 对数正态、用 β 与大盘相关(SPY 波动≈${pctS(R.SPY_VOL)},取自实时 VIX);无方向假设。<b>每小时自动重算一次</b>,或点 🔄。绿=赚 / 红=亏,<span style="color:#58a6ff">蓝线</span>=现在浮盈,<span style="color:#e3b341">黄线</span>=中位。</div><span style="flex:none;font-size:10px;color:#6e7681">更新于 ${updatedAt}</span><button onclick="_probForce=true;renderProbView()" style="flex:none;background:#21262d;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:3px 10px;font-size:11px;cursor:pointer">&#128260; 重新模拟</button></div>${svg}${tbl}`;
    body.dataset.done = '1';
  }, 30);
}
async function renderScenarioView() {
  const body = document.getElementById('scenarioBody');
  const strats = (RAW_DATA.strategies || []).filter(s => s.legs && s.legs.length);
  if (!strats.length) { body.innerHTML = '<div style="padding:24px;color:var(--text2)">当前无持仓</div>'; return; }
  // β(vs SPY)：与组合希腊面板共用 _pfBeta 缓存（标的集合没变就不重 fetch，60s full_refresh 重渲染时不闪占位）;缺则 1:1
  const unders = Array.from(new Set(strats.map(s => s.underlying).filter(Boolean))).sort();
  const key = unders.join(',');
  if (key !== _pfBeta.key) {
    body.innerHTML = '<div style="padding:20px;color:var(--text2);font-size:13px">&#9203; 正在计算情景网格（取 β）…</div>';
    try {
      const r = await fetch('/api/portfolio/betas?symbols=' + encodeURIComponent(key));
      const d = await r.json();
      _pfBeta = { betas: d.betas || {}, spy: d.spy || null, missing: d.missing || [], key };
    } catch (e) { _pfBeta = { betas: {}, spy: null, missing: [], key }; }
    if (document.getElementById('riskView').style.display === 'none' || riskTab !== 'scenario') return;
  }
  const betas = _pfBeta.betas;
  const betaOf = u => (betas[u] != null ? betas[u] : 1.0);
  const NLV = liveNLV();
  const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)).toLocaleString();

  const rows = [];
  strats.forEach(s => { let a; try { a = analyzeStrategy(s); } catch (e) { return; } rows.push({ s, a, b: betaOf(s.underlying) }); });
  const moves = [-0.08, -0.05, -0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.03, 0.05, 0.08];
  const horizons = [{ label: '即刻', d: 0 }, { label: '+3天', d: 3 }, { label: '+7天', d: 7 }];
  const cell = (mv, d) => {
    let v = 0;
    rows.forEach(({ s, a, b }) => {
      const Sx = Math.max(0.01, s.current_price * (1 + b * mv));
      const T2 = Math.max(0, a.T - d / 365);
      v += pnlAtFuture(s, Sx, T2, s.risk_free_rate, a.iv);
    });
    return v;
  };
  const base = cell(0, 0);
  const grid = moves.map(mv => horizons.map(h => cell(mv, h.d) - base));
  const mx = Math.max(1, ...grid.flat().map(Math.abs));
  const bg = v => { const t = Math.max(-1, Math.min(1, v / mx)); return t >= 0 ? 'rgba(63,185,80,' + (0.10 + 0.55 * t).toFixed(3) + ')' : 'rgba(248,81,73,' + (0.10 + 0.55 * -t).toFixed(3) + ')'; };

  let head = '<tr><th style="padding:6px 10px;text-align:left;color:var(--text2)">SPY \\ 时间</th>';
  horizons.forEach(h => head += '<th style="padding:6px 14px;text-align:center;color:var(--text2)">' + h.label + '</th>'); head += '</tr>';
  let trs = '';
  moves.forEach((mv, i) => {
    const hot = (mv === -0.05);
    trs += '<tr><td style="padding:6px 10px;font-weight:700;' + (hot ? 'color:var(--text)' : 'color:var(--text2)') + '">SPY ' + (mv > 0 ? '+' : '') + (mv * 100).toFixed(0) + '%</td>';
    horizons.forEach((h, j) => {
      const v = grid[i][j];
      const pct = NLV > 0 ? '<div style="font-size:10px;opacity:0.7">' + (v / NLV * 100).toFixed(1) + '% NLV</div>' : '';
      trs += '<td style="padding:6px 14px;text-align:center;background:' + bg(v) + ';font-variant-numeric:tabular-nums" title="SPY ' + (mv * 100).toFixed(0) + '% · ' + h.label + ' → ' + money(v) + '">' +
        '<div style="font-weight:700">' + money(v) + '</div>' + pct + '</td>';
    });
    trs += '</tr>';
  });
  const nBeta = rows.filter(r => betas[r.s.underlying] != null).length;
  body.innerHTML =
    '<div style="font-size:12px;color:var(--text2);margin:12px 0 8px;line-height:1.6">每格 = 组合相对现在的盈亏。行 = SPY 涨跌（各标的按自身 β 放大），列 = 时间推进（含 Theta）。绿=赚红=亏，越深越大。β 命中 ' + nBeta + '/' + rows.length + ' 票（缺则按 1.0）。<b>仅估算,不含 IV 变化。</b></div>' +
    '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:12px;min-width:420px"><thead>' + head + '</thead><tbody>' + trs + '</tbody></table></div>' +
    '<div style="font-size:11px;color:var(--text2);margin-top:10px">提示:大跌情景看最上几行;若净多头,−5% 那行应明显为红。要把 IV 上涨也叠进来可后续加一列「IV +5pt」。</div>';
}

// ── Sector taxonomy (consolidated from idea_e16af.txt → ~28 categories) ──
// Granular enough to surface concentration risk (AI Semis vs Memory vs Mag7
// rather than just "Tech"), coarse enough to fit a pie chart legend.
// Tickers not listed → "Other / Uncategorized" (encourages keeping the map
// updated as the watchlist grows).
const SECTOR_MAP = {
  // Mag 7 — kept separate because they dominate single-name risk
  NVDA:'Mag 7', META:'Mag 7', AAPL:'Mag 7', AMZN:'Mag 7',
  GOOG:'Mag 7', GOOGL:'Mag 7', TSLA:'Mag 7', MSFT:'Mag 7',
  // AI / Fabless semis
  AMD:'AI Semis', AVGO:'AI Semis', MRVL:'AI Semis', QCOM:'AI Semis',
  ALAB:'AI Semis', NVTS:'AI Semis', CRDO:'AI Semis', ARM:'AI Semis',
  // Foundry + Equipment
  TSM:'Semi - Foundry/Equip', UMC:'Semi - Foundry/Equip', GFS:'Semi - Foundry/Equip', TSEM:'Semi - Foundry/Equip',
  ASML:'Semi - Foundry/Equip', AMAT:'Semi - Foundry/Equip', LRCX:'Semi - Foundry/Equip',
  KLAC:'Semi - Foundry/Equip', ACMR:'Semi - Foundry/Equip', AEHR:'Semi - Foundry/Equip', TER:'Semi - Foundry/Equip',
  AXTI:'Semi - Foundry/Equip', ENTG:'Semi - Foundry/Equip', ASX:'Semi - Foundry/Equip', AMKR:'Semi - Foundry/Equip',
  CDNS:'Semi - Foundry/Equip', SNPS:'Semi - Foundry/Equip',
  // Memory + Analog/IDM
  MU:'Semi - Memory/IDM', SNDK:'Semi - Memory/IDM', WDC:'Semi - Memory/IDM', STX:'Semi - Memory/IDM',
  INTC:'Semi - Memory/IDM', ADI:'Semi - Memory/IDM', NXPI:'Semi - Memory/IDM', TXN:'Semi - Memory/IDM',
  MCHP:'Semi - Memory/IDM', ON:'Semi - Memory/IDM', RMBS:'Semi - Memory/IDM', STM:'Semi - Memory/IDM',
  // AI Infra + Data Center hardware
  APLD:'AI Infra / Data Center', CRWV:'AI Infra / Data Center', NBIS:'AI Infra / Data Center',
  SMCI:'AI Infra / Data Center', DELL:'AI Infra / Data Center', HPE:'AI Infra / Data Center',
  VRT:'AI Infra / Data Center', NVT:'AI Infra / Data Center', MOD:'AI Infra / Data Center',
  APH:'AI Infra / Data Center', IOT:'AI Infra / Data Center', FLEX:'AI Infra / Data Center',
  JBL:'AI Infra / Data Center', CLS:'AI Infra / Data Center', SYM:'AI Infra / Data Center',
  PLTR:'AI Infra / Data Center',
  // Networking / Optical
  ANET:'Networking / Optical', CSCO:'Networking / Optical', NOK:'Networking / Optical',
  GLW:'Networking / Optical', COHR:'Networking / Optical', LITE:'Networking / Optical',
  AAOI:'Networking / Optical', VIAV:'Networking / Optical', CIEN:'Networking / Optical',
  AKAM:'Networking / Optical',
  // Software / SaaS / Cloud
  ORCL:'Software / SaaS', NOW:'Software / SaaS', SAP:'Software / SaaS', CRM:'Software / SaaS',
  INTU:'Software / SaaS', ADBE:'Software / SaaS', ADSK:'Software / SaaS', FIG:'Software / SaaS',
  DDOG:'Software / SaaS', SNOW:'Software / SaaS', MDB:'Software / SaaS', GTLB:'Software / SaaS',
  FSLY:'Software / SaaS', FROG:'Software / SaaS', TEAM:'Software / SaaS', DOCN:'Software / SaaS',
  PAYX:'Software / SaaS', WDAY:'Software / SaaS', PAYC:'Software / SaaS', ADP:'Software / SaaS',
  BILL:'Software / SaaS', DOCU:'Software / SaaS', MNDY:'Software / SaaS', WIX:'Software / SaaS',
  ZM:'Software / SaaS', HUBS:'Software / SaaS', ZETA:'Software / SaaS', TTD:'Software / SaaS',
  BRZE:'Software / SaaS', TWLO:'Software / SaaS', APP:'Software / SaaS', SRAD:'Software / SaaS',
  DOCS:'Software / SaaS', CWAN:'Software / SaaS', TOST:'Software / SaaS', TEM:'Software / SaaS',
  LRN:'Software / SaaS', DUOL:'Software / SaaS', PATH:'Software / SaaS',
  // Cybersecurity
  PANW:'Cybersecurity', CRWD:'Cybersecurity', FTNT:'Cybersecurity', OKTA:'Cybersecurity',
  ZS:'Cybersecurity', NET:'Cybersecurity', S:'Cybersecurity', RBRK:'Cybersecurity',
  // Old hardware / IT services
  HPQ:'IT Services / Hardware', IBM:'IT Services / Hardware', KD:'IT Services / Hardware',
  NTNX:'IT Services / Hardware', INFY:'IT Services / Hardware', CTSH:'IT Services / Hardware',
  ACN:'IT Services / Hardware',
  // Quantum + Space (small but distinct)
  IONQ:'Quantum / Space', RGTI:'Quantum / Space', QBTS:'Quantum / Space',
  INFQ:'Quantum / Space', LASR:'Quantum / Space', Q:'Quantum / Space',
  RKLB:'Quantum / Space', ASTS:'Quantum / Space', GSAT:'Quantum / Space',
  SATS:'Quantum / Space', IRDM:'Quantum / Space', VSAT:'Quantum / Space',
  FLY:'Quantum / Space', PL:'Quantum / Space', LUNR:'Quantum / Space',
  // Fintech / Payments
  V:'Fintech / Payments', MA:'Fintech / Payments', AXP:'Fintech / Payments',
  PYPL:'Fintech / Payments', AFRM:'Fintech / Payments', SOFI:'Fintech / Payments',
  FOUR:'Fintech / Payments', GPN:'Fintech / Payments', XYZ:'Fintech / Payments',
  FISV:'Fintech / Payments', UPST:'Fintech / Payments', KLAR:'Fintech / Payments',
  CHYM:'Fintech / Payments', FIGR:'Fintech / Payments',
  // Banks
  JPM:'Banks', BAC:'Banks', WFC:'Banks', C:'Banks', USB:'Banks', PNC:'Banks',
  TFC:'Banks', FITB:'Banks', RF:'Banks', FHN:'Banks', KEY:'Banks', HBAN:'Banks',
  FLG:'Banks', VLY:'Banks', BCS:'Banks', UBS:'Banks', DB:'Banks', HSBC:'Banks', SAN:'Banks',
  // Brokers / Asset Mgmt / Exchanges
  GS:'Brokers / Asset Mgmt', MS:'Brokers / Asset Mgmt', SCHW:'Brokers / Asset Mgmt',
  HOOD:'Brokers / Asset Mgmt', IBKR:'Brokers / Asset Mgmt', NDAQ:'Brokers / Asset Mgmt',
  CME:'Brokers / Asset Mgmt', SPGI:'Brokers / Asset Mgmt', BLK:'Brokers / Asset Mgmt',
  KKR:'Brokers / Asset Mgmt', BX:'Brokers / Asset Mgmt', APO:'Brokers / Asset Mgmt',
  ARES:'Brokers / Asset Mgmt', BAM:'Brokers / Asset Mgmt', TROW:'Brokers / Asset Mgmt',
  CG:'Brokers / Asset Mgmt', SEI:'Brokers / Asset Mgmt', IVZ:'Brokers / Asset Mgmt',
  AB:'Brokers / Asset Mgmt', JEF:'Brokers / Asset Mgmt', STT:'Brokers / Asset Mgmt',
  // Insurance + Consumer Credit
  PGR:'Insurance / Cons Credit', AIG:'Insurance / Cons Credit', MET:'Insurance / Cons Credit',
  AFL:'Insurance / Cons Credit', PRU:'Insurance / Cons Credit', COF:'Insurance / Cons Credit',
  ALLY:'Insurance / Cons Credit', SYF:'Insurance / Cons Credit', LMND:'Insurance / Cons Credit',
  MDLN:'Insurance / Cons Credit', 'BRK.B':'Insurance / Cons Credit',
  ARCC:'Insurance / Cons Credit', HTGC:'Insurance / Cons Credit',
  OBDC:'Insurance / Cons Credit', FSK:'Insurance / Cons Credit',
  // Crypto / Bitcoin (incl miners + AI mining hybrids)
  MSTR:'Crypto / Bitcoin', COIN:'Crypto / Bitcoin', BMNR:'Crypto / Bitcoin',
  IBIT:'Crypto / Bitcoin', GBTC:'Crypto / Bitcoin', FBTC:'Crypto / Bitcoin',
  ETHA:'Crypto / Bitcoin', ETHE:'Crypto / Bitcoin',
  MARA:'Crypto / Bitcoin', RIOT:'Crypto / Bitcoin', CLSK:'Crypto / Bitcoin',
  CIFR:'Crypto / Bitcoin', IREN:'Crypto / Bitcoin', CORZ:'Crypto / Bitcoin',
  WULF:'Crypto / Bitcoin', BTDR:'Crypto / Bitcoin', HUT:'Crypto / Bitcoin',
  GLXY:'Crypto / Bitcoin', BLSH:'Crypto / Bitcoin', CRCL:'Crypto / Bitcoin',
  // Energy - Oil/Gas (integrated, E&P, services)
  XOM:'Energy - Oil & Gas', CVX:'Energy - Oil & Gas', COP:'Energy - Oil & Gas',
  OXY:'Energy - Oil & Gas', PSX:'Energy - Oil & Gas', MPC:'Energy - Oil & Gas',
  VLO:'Energy - Oil & Gas', SHEL:'Energy - Oil & Gas', BP:'Energy - Oil & Gas',
  TTE:'Energy - Oil & Gas', SU:'Energy - Oil & Gas', CNQ:'Energy - Oil & Gas',
  CVE:'Energy - Oil & Gas', PBR:'Energy - Oil & Gas', EQNR:'Energy - Oil & Gas',
  EC:'Energy - Oil & Gas', YPF:'Energy - Oil & Gas', PBF:'Energy - Oil & Gas',
  DINO:'Energy - Oil & Gas', CLMT:'Energy - Oil & Gas',
  EOG:'Energy - Oil & Gas', FANG:'Energy - Oil & Gas', EQT:'Energy - Oil & Gas',
  AR:'Energy - Oil & Gas', APA:'Energy - Oil & Gas', DVN:'Energy - Oil & Gas',
  PR:'Energy - Oil & Gas', CRGY:'Energy - Oil & Gas', CRK:'Energy - Oil & Gas',
  SM:'Energy - Oil & Gas', CTRA:'Energy - Oil & Gas', EXE:'Energy - Oil & Gas',
  VIST:'Energy - Oil & Gas',
  SLB:'Energy - Oil & Gas', HAL:'Energy - Oil & Gas', BKR:'Energy - Oil & Gas', PTEN:'Energy - Oil & Gas',
  // Energy - Midstream / LNG / Coal
  LNG:'Energy - Midstream/LNG', ET:'Energy - Midstream/LNG', KMI:'Energy - Midstream/LNG',
  EPD:'Energy - Midstream/LNG', ENB:'Energy - Midstream/LNG', MPLX:'Energy - Midstream/LNG',
  WMB:'Energy - Midstream/LNG', OKE:'Energy - Midstream/LNG', VG:'Energy - Midstream/LNG',
  PAA:'Energy - Midstream/LNG', PAGP:'Energy - Midstream/LNG',
  CMBT:'Energy - Midstream/LNG', BTU:'Energy - Midstream/LNG',
  // Utilities + Nuclear + Renewable
  NEE:'Utilities / Nuclear / Renewable', DUK:'Utilities / Nuclear / Renewable',
  AEP:'Utilities / Nuclear / Renewable', EXC:'Utilities / Nuclear / Renewable',
  SO:'Utilities / Nuclear / Renewable', NRG:'Utilities / Nuclear / Renewable',
  VST:'Utilities / Nuclear / Renewable', CEG:'Utilities / Nuclear / Renewable',
  TLN:'Utilities / Nuclear / Renewable', GEV:'Utilities / Nuclear / Renewable',
  D:'Utilities / Nuclear / Renewable', PPL:'Utilities / Nuclear / Renewable',
  AES:'Utilities / Nuclear / Renewable', PCG:'Utilities / Nuclear / Renewable',
  OKLO:'Utilities / Nuclear / Renewable', SMR:'Utilities / Nuclear / Renewable',
  CCJ:'Utilities / Nuclear / Renewable', NXE:'Utilities / Nuclear / Renewable',
  UEC:'Utilities / Nuclear / Renewable', UUUU:'Utilities / Nuclear / Renewable',
  LEU:'Utilities / Nuclear / Renewable', BE:'Utilities / Nuclear / Renewable',
  GNRC:'Utilities / Nuclear / Renewable',
  RUN:'Utilities / Nuclear / Renewable', ENPH:'Utilities / Nuclear / Renewable',
  SEDG:'Utilities / Nuclear / Renewable', FSLR:'Utilities / Nuclear / Renewable',
  FLNC:'Utilities / Nuclear / Renewable', BW:'Utilities / Nuclear / Renewable',
  AMPX:'Utilities / Nuclear / Renewable',
  // Precious metals (ETF + miners)
  GLD:'Precious Metals', IAU:'Precious Metals', SLV:'Precious Metals',
  SIVR:'Precious Metals', PPLT:'Precious Metals',
  NEM:'Precious Metals', AEM:'Precious Metals', GFI:'Precious Metals',
  AU:'Precious Metals', WPM:'Precious Metals', AGI:'Precious Metals',
  KGC:'Precious Metals', SSRM:'Precious Metals', IAG:'Precious Metals',
  HMY:'Precious Metals', ORLA:'Precious Metals', EQX:'Precious Metals',
  NG:'Precious Metals', HYMC:'Precious Metals',
  PAAS:'Precious Metals', AG:'Precious Metals', FSM:'Precious Metals',
  SVM:'Precious Metals', HL:'Precious Metals', CDE:'Precious Metals',
  SBSW:'Precious Metals',
  GDX:'Precious Metals', GDXJ:'Precious Metals', SIL:'Precious Metals', SILJ:'Precious Metals',
  PPTA:'Precious Metals',
  // Industrial Metals & Mining (incl lithium, rare earth, uranium ETFs)
  FCX:'Industrial Metals & Mining', AA:'Industrial Metals & Mining', SCCO:'Industrial Metals & Mining',
  BHP:'Industrial Metals & Mining', RIO:'Industrial Metals & Mining', VALE:'Industrial Metals & Mining',
  TECK:'Industrial Metals & Mining', NUE:'Industrial Metals & Mining',
  HBM:'Industrial Metals & Mining', ERO:'Industrial Metals & Mining',
  ALB:'Industrial Metals & Mining', SGML:'Industrial Metals & Mining',
  MP:'Industrial Metals & Mining', USAR:'Industrial Metals & Mining',
  URA:'Industrial Metals & Mining', URNM:'Industrial Metals & Mining',
  COPX:'Industrial Metals & Mining',
  // Materials / Chemicals
  DOW:'Materials / Chemicals', LYB:'Materials / Chemicals', OLN:'Materials / Chemicals',
  HUN:'Materials / Chemicals', MEOH:'Materials / Chemicals', CE:'Materials / Chemicals',
  FMC:'Materials / Chemicals', CC:'Materials / Chemicals', SOLS:'Materials / Chemicals',
  LIN:'Materials / Chemicals', MOS:'Materials / Chemicals', NTR:'Materials / Chemicals',
  CF:'Materials / Chemicals',
  // Pharma / Biotech
  LLY:'Pharma / Biotech', PFE:'Pharma / Biotech', MRK:'Pharma / Biotech',
  JNJ:'Pharma / Biotech', AZN:'Pharma / Biotech', ABBV:'Pharma / Biotech',
  BMY:'Pharma / Biotech', GSK:'Pharma / Biotech', NVO:'Pharma / Biotech',
  NVS:'Pharma / Biotech', AMGN:'Pharma / Biotech', GILD:'Pharma / Biotech',
  VRTX:'Pharma / Biotech', UTHR:'Pharma / Biotech', ZTS:'Pharma / Biotech',
  TEVA:'Pharma / Biotech', VTRS:'Pharma / Biotech',
  MRNA:'Pharma / Biotech', CRSP:'Pharma / Biotech', RVMD:'Pharma / Biotech',
  ADMA:'Pharma / Biotech', MESO:'Pharma / Biotech', SRPT:'Pharma / Biotech',
  DAWN:'Pharma / Biotech', ERAS:'Pharma / Biotech', SMMT:'Pharma / Biotech',
  MLYS:'Pharma / Biotech', IDYA:'Pharma / Biotech', VKTX:'Pharma / Biotech',
  CAPR:'Pharma / Biotech', TGTX:'Pharma / Biotech', LQDA:'Pharma / Biotech',
  QGEN:'Pharma / Biotech', TVTX:'Pharma / Biotech', SLNO:'Pharma / Biotech',
  TERN:'Pharma / Biotech', XENE:'Pharma / Biotech', CYTK:'Pharma / Biotech',
  NKTR:'Pharma / Biotech', ABVX:'Pharma / Biotech',
  // MedTech + Healthcare Services
  ISRG:'MedTech / HC Services', MDT:'MedTech / HC Services', ABT:'MedTech / HC Services',
  DHR:'MedTech / HC Services', TMO:'MedTech / HC Services', BSX:'MedTech / HC Services',
  BAX:'MedTech / HC Services', DXCM:'MedTech / HC Services',
  UNH:'MedTech / HC Services', CI:'MedTech / HC Services', HUM:'MedTech / HC Services',
  CVS:'MedTech / HC Services', CNC:'MedTech / HC Services', OSCR:'MedTech / HC Services',
  HIMS:'MedTech / HC Services',
  // Consumer Discretionary - Retail (mass + discount + apparel/beauty)
  COST:'Consumer Disc - Retail', WMT:'Consumer Disc - Retail', TGT:'Consumer Disc - Retail',
  KR:'Consumer Disc - Retail', SFM:'Consumer Disc - Retail',
  HD:'Consumer Disc - Retail', LOW:'Consumer Disc - Retail', BLDR:'Consumer Disc - Retail',
  FBIN:'Consumer Disc - Retail', BBY:'Consumer Disc - Retail', RH:'Consumer Disc - Retail',
  DKS:'Consumer Disc - Retail', FND:'Consumer Disc - Retail', TSCO:'Consumer Disc - Retail',
  QXO:'Consumer Disc - Retail',
  TJX:'Consumer Disc - Retail', ROST:'Consumer Disc - Retail', BURL:'Consumer Disc - Retail',
  DG:'Consumer Disc - Retail', DLTR:'Consumer Disc - Retail', M:'Consumer Disc - Retail',
  NKE:'Consumer Disc - Retail', LULU:'Consumer Disc - Retail', ULTA:'Consumer Disc - Retail',
  DECK:'Consumer Disc - Retail', CROX:'Consumer Disc - Retail', AS:'Consumer Disc - Retail',
  ONON:'Consumer Disc - Retail', GAP:'Consumer Disc - Retail', ANF:'Consumer Disc - Retail',
  AEO:'Consumer Disc - Retail', BBWI:'Consumer Disc - Retail', VFC:'Consumer Disc - Retail',
  CPRI:'Consumer Disc - Retail', LEVI:'Consumer Disc - Retail', SIG:'Consumer Disc - Retail',
  VSCO:'Consumer Disc - Retail', FIGS:'Consumer Disc - Retail', WRBY:'Consumer Disc - Retail',
  // Consumer Staples (food/bev/tobacco/personal care)
  KO:'Consumer Staples', PEP:'Consumer Staples', PG:'Consumer Staples',
  MNST:'Consumer Staples', CL:'Consumer Staples', KMB:'Consumer Staples',
  CLX:'Consumer Staples', STZ:'Consumer Staples', PM:'Consumer Staples',
  HSY:'Consumer Staples', MO:'Consumer Staples', BTI:'Consumer Staples',
  UL:'Consumer Staples', EL:'Consumer Staples', MDLZ:'Consumer Staples',
  KDP:'Consumer Staples', CELH:'Consumer Staples', GIS:'Consumer Staples',
  TAP:'Consumer Staples', LW:'Consumer Staples', MKC:'Consumer Staples',
  ADM:'Consumer Staples', BUD:'Consumer Staples', SYY:'Consumer Staples',
  KVUE:'Consumer Staples', JBS:'Consumer Staples', KHC:'Consumer Staples',
  CPB:'Consumer Staples', HRL:'Consumer Staples', PRMB:'Consumer Staples',
  CAG:'Consumer Staples', MAT:'Consumer Staples', ELF:'Consumer Staples',
  // Restaurants / Travel / Gaming
  MCD:'Restaurants / Travel / Gaming', SBUX:'Restaurants / Travel / Gaming',
  CMG:'Restaurants / Travel / Gaming', BROS:'Restaurants / Travel / Gaming',
  QSR:'Restaurants / Travel / Gaming', CAVA:'Restaurants / Travel / Gaming',
  SHAK:'Restaurants / Travel / Gaming',
  BKNG:'Restaurants / Travel / Gaming', ABNB:'Restaurants / Travel / Gaming',
  MAR:'Restaurants / Travel / Gaming', EXPE:'Restaurants / Travel / Gaming',
  CAR:'Restaurants / Travel / Gaming', LYFT:'Restaurants / Travel / Gaming',
  TCOM:'Restaurants / Travel / Gaming', UBER:'Restaurants / Travel / Gaming',
  DASH:'Restaurants / Travel / Gaming',
  MGM:'Restaurants / Travel / Gaming', LVS:'Restaurants / Travel / Gaming',
  WYNN:'Restaurants / Travel / Gaming', CZR:'Restaurants / Travel / Gaming',
  PENN:'Restaurants / Travel / Gaming', DKNG:'Restaurants / Travel / Gaming',
  FLUT:'Restaurants / Travel / Gaming', SGHC:'Restaurants / Travel / Gaming',
  RBLX:'Restaurants / Travel / Gaming', U:'Restaurants / Travel / Gaming',
  EA:'Restaurants / Travel / Gaming', TTWO:'Restaurants / Travel / Gaming',
  GME:'Restaurants / Travel / Gaming',
  NCLH:'Restaurants / Travel / Gaming', CCL:'Restaurants / Travel / Gaming',
  RCL:'Restaurants / Travel / Gaming',
  // Industrials - Aerospace/Defense
  BA:'Aerospace / Defense', RTX:'Aerospace / Defense', LMT:'Aerospace / Defense',
  GE:'Aerospace / Defense', HWM:'Aerospace / Defense', HON:'Aerospace / Defense',
  AXON:'Aerospace / Defense', KTOS:'Aerospace / Defense', AVAV:'Aerospace / Defense',
  AMTM:'Aerospace / Defense', ONDS:'Aerospace / Defense',
  // Industrials - Machinery / Construction
  CAT:'Industrials - Machinery', DE:'Industrials - Machinery', ETN:'Industrials - Machinery',
  EMR:'Industrials - Machinery', MMM:'Industrials - Machinery', JCI:'Industrials - Machinery',
  CARR:'Industrials - Machinery', DAR:'Industrials - Machinery', WHR:'Industrials - Machinery',
  FLR:'Industrials - Machinery', FAST:'Industrials - Machinery', NN:'Industrials - Machinery',
  IP:'Industrials - Machinery', B:'Industrials - Machinery', URI:'Industrials - Machinery',
  PWR:'Industrials - Machinery',
  DHI:'Industrials - Machinery', LEN:'Industrials - Machinery', CRH:'Industrials - Machinery',
  WMS:'Industrials - Machinery',
  // Auto / EV / Logistics
  GM:'Auto / Logistics', F:'Auto / Logistics', RIVN:'Auto / Logistics',
  KMX:'Auto / Logistics', AAP:'Auto / Logistics', RACE:'Auto / Logistics',
  CVNA:'Auto / Logistics', HOG:'Auto / Logistics',
  AAL:'Auto / Logistics', LUV:'Auto / Logistics', ALK:'Auto / Logistics',
  DAL:'Auto / Logistics', UAL:'Auto / Logistics',
  UPS:'Auto / Logistics', FDX:'Auto / Logistics', UNP:'Auto / Logistics',
  CSX:'Auto / Logistics', FTAI:'Auto / Logistics', CHRW:'Auto / Logistics',
  ZIM:'Auto / Logistics', FRO:'Auto / Logistics', STNG:'Auto / Logistics',
  DHT:'Auto / Logistics', SBLK:'Auto / Logistics', GLNG:'Auto / Logistics',
  // Streaming / Media / Telecom
  NFLX:'Streaming / Media / Telecom', DIS:'Streaming / Media / Telecom',
  SPOT:'Streaming / Media / Telecom', ROKU:'Streaming / Media / Telecom',
  T:'Streaming / Media / Telecom', VZ:'Streaming / Media / Telecom',
  CMCSA:'Streaming / Media / Telecom', CHTR:'Streaming / Media / Telecom',
  TMUS:'Streaming / Media / Telecom', WBD:'Streaming / Media / Telecom',
  SIRI:'Streaming / Media / Telecom', PSKY:'Streaming / Media / Telecom',
  RDDT:'Streaming / Media / Telecom', PINS:'Streaming / Media / Telecom',
  MTCH:'Streaming / Media / Telecom', LION:'Streaming / Media / Telecom',
  TME:'Streaming / Media / Telecom', VOD:'Streaming / Media / Telecom',
  SONY:'Streaming / Media / Telecom', CNK:'Streaming / Media / Telecom',
  LYV:'Streaming / Media / Telecom',
  // China / EM ADRs
  BABA:'China / EM ADRs', JD:'China / EM ADRs', PDD:'China / EM ADRs',
  BIDU:'China / EM ADRs', BILI:'China / EM ADRs', KC:'China / EM ADRs',
  LI:'China / EM ADRs', XPEV:'China / EM ADRs', BEKE:'China / EM ADRs',
  SE:'China / EM ADRs', FUTU:'China / EM ADRs', NU:'China / EM ADRs',
  STNE:'China / EM ADRs', HSAI:'China / EM ADRs', VISN:'China / EM ADRs',
  GGAL:'China / EM ADRs', XP:'China / EM ADRs', SKM:'China / EM ADRs',
  DLO:'China / EM ADRs', PONY:'China / EM ADRs',
  // REITs / Real Estate
  PLD:'REITs / Real Estate', AMT:'REITs / Real Estate', O:'REITs / Real Estate',
  IRM:'REITs / Real Estate', WY:'REITs / Real Estate', AGNC:'REITs / Real Estate',
  NLY:'REITs / Real Estate', STWD:'REITs / Real Estate', RKT:'REITs / Real Estate',
  GEO:'REITs / Real Estate', Z:'REITs / Real Estate', CSGP:'REITs / Real Estate',
  // Index ETFs / Bonds
  SPY:'Index / Bond ETFs', QQQ:'Index / Bond ETFs', VOO:'Index / Bond ETFs',
  VTI:'Index / Bond ETFs', DIA:'Index / Bond ETFs', IWM:'Index / Bond ETFs',
  MAGS:'Index / Bond ETFs', MTUM:'Index / Bond ETFs', SPYM:'Index / Bond ETFs',
  RSP:'Index / Bond ETFs', JEPQ:'Index / Bond ETFs', QQQI:'Index / Bond ETFs',
  SCHD:'Index / Bond ETFs',
  TLT:'Index / Bond ETFs', IEF:'Index / Bond ETFs', EMB:'Index / Bond ETFs',
  LQD:'Index / Bond ETFs', BKLN:'Index / Bond ETFs', HYG:'Index / Bond ETFs',
  TQQQ:'Index / Bond ETFs', SQQQ:'Index / Bond ETFs', SOXL:'Index / Bond ETFs',
  // Sector / theme ETFs (kept as a single bucket; users mostly hold these for tactical exposure)
  XLU:'Sector / Theme ETFs', XLB:'Sector / Theme ETFs', XLF:'Sector / Theme ETFs',
  XLE:'Sector / Theme ETFs', XLP:'Sector / Theme ETFs', XLC:'Sector / Theme ETFs',
  XLY:'Sector / Theme ETFs', XLV:'Sector / Theme ETFs', XLK:'Sector / Theme ETFs',
  XLI:'Sector / Theme ETFs',
  KRE:'Sector / Theme ETFs', ARKK:'Sector / Theme ETFs', IGV:'Sector / Theme ETFs',
  ITB:'Sector / Theme ETFs', IYR:'Sector / Theme ETFs', XME:'Sector / Theme ETFs',
  XBI:'Sector / Theme ETFs', USO:'Sector / Theme ETFs', XOP:'Sector / Theme ETFs',
  IBB:'Sector / Theme ETFs', SOXX:'Sector / Theme ETFs', SMH:'Sector / Theme ETFs',
  KWEB:'Sector / Theme ETFs', FXI:'Sector / Theme ETFs', EWZ:'Sector / Theme ETFs',
  INDA:'Sector / Theme ETFs', EWC:'Sector / Theme ETFs', EEM:'Sector / Theme ETFs',
  FEZ:'Sector / Theme ETFs', EWT:'Sector / Theme ETFs', EWJ:'Sector / Theme ETFs',
  EFA:'Sector / Theme ETFs', EWY:'Sector / Theme ETFs',
};
function sectorOf(ticker) { return SECTOR_MAP[ticker] || 'Other / Uncategorized'; }

// Distinct, accessible palette for sector slices. Order is stable so the
// same sector always gets the same color across renders.
const SECTOR_COLOR_PALETTE = [
  '#58a6ff', '#f97583', '#56d364', '#e3b341', '#bc8cff', '#79c0ff',
  '#ff9e64', '#7ee787', '#ff7b72', '#a5d6ff', '#d2a8ff', '#ffa657',
  '#3fb950', '#db6d28', '#1f6feb', '#a371f7', '#ff8087', '#33b1ff',
  '#84d2ff', '#ffb86c', '#8be9fd', '#50fa7b', '#bd93f9', '#ff79c6',
  '#f1fa8c', '#6272a4', '#ffb454', '#73d216',
];
function sectorColor(name, allNames) {
  const i = allNames.indexOf(name);
  return SECTOR_COLOR_PALETTE[i % SECTOR_COLOR_PALETTE.length];
}

// ── Overview view ──
const OV_SECTOR_WARN_PCT = 40;     // any sector > 40% of capital → highlight
const OV_CORR_ATTN = 0.5;          // |corr| > this → outside neutral band
// Events warning gating — only flag when BP commitment makes the event matter
const OV_EVENT_T1_BP_PCT = 25;     // Tier 1 + this week + BP > 25% → ATTN
const OV_EVENT_T2_BP_PCT = 40;     // Tier 2 + this week + BP > 40% → ATTN
let _ovEventsCache = null;         // {events, today}
let _ovEventsInflight = null;
let _ovCorrCache = {};             // "ticker:window" -> corr or null
let _ovCorrWindow = 90;            // active lookback in trading days
let _ovCorrMatrixCache = {};       // window -> {matrix, symbols} (pairwise heatmap)

function openOverviewView() {
  appNavInto();
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('overviewView').style.display = '';
  renderOverviewView();
}
function exitOverviewView() {
  document.getElementById('overviewView').style.display = 'none';
  document.getElementById('dashboardView').style.display = '';
}

function renderOverviewView() {
  renderSectorPie();
  renderCorrelationBars();
  renderCorrMatrix();
  renderEventsPanel();
  _bindCorrToggleOnce();
}

// ── Position Heatmap view ──────────────────────────────────────────────────
// Treemap of every strategy in RAW_DATA.strategies:
//   rect size  = |maxLoss| (default) or |maxProfit|
//   rect color = today's P/L, normalized so it's comparable across positions
//   click rect → open that strategy's detail view
let _phmD3Ready = null;
let _phmResizeObs = null;
function _phmEnsureD3() {
  if (window.d3) return Promise.resolve(window.d3);
  if (_phmD3Ready) return _phmD3Ready;
  _phmD3Ready = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js';
    s.onload = () => res(window.d3);
    s.onerror = () => rej(new Error('d3 failed to load'));
    document.head.appendChild(s);
  });
  return _phmD3Ready;
}

function openHeatmapView() {
  appNavInto();
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('heatmapView').style.display = 'flex';
  renderHeatmapView();
  // Re-render on container resize
  const canvas = document.getElementById('phmCanvas');
  if (canvas && !_phmResizeObs) {
    _phmResizeObs = new ResizeObserver(() => renderHeatmapView());
    _phmResizeObs.observe(canvas);
  }
}
function exitHeatmapView() {
  document.getElementById('heatmapView').style.display = 'none';
  document.getElementById('dashboardView').style.display = '';
}

// Heatmap + P/L 合并的循环切换按钮: 表格 → 🔥热图 → 💵P/L → 表格。
// 读实际 DOM 可见性(防浏览器返回/back-nav 让状态失步)。
function toggleHeatmapPnl() {
  const hv = document.getElementById('heatmapView');
  const pv = document.getElementById('pnlBarsView');
  const hmOn = hv && getComputedStyle(hv).display !== 'none';
  const plOn = pv && getComputedStyle(pv).display !== 'none';
  if (hmOn) { exitHeatmapView(); openPnlBarsView(); }      // 热图 → P/L
  else if (plOn) { exitPnlBarsView(); }                    // P/L → 表格
  else { openHeatmapView(); }                              // 表格 → 热图
}

function _phmFmtMoney(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const a = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (a >= 1e6) return sign + '$' + (a / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return sign + '$' + (a / 1e3).toFixed(2) + 'k';
  return sign + '$' + a.toFixed(0);
}
function _phmFmtPct(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}
function _phmColorFor(value, scale) {
  if (value === null || value === undefined || isNaN(value)) return '#21262d';
  const x = Math.max(-1, Math.min(1, value / scale));
  if (Math.abs(x) < 0.001) return '#30363d';
  if (x > 0) {
    const r = Math.round(34  + (63 - 34)  * (1 - x));
    const g = Math.round(96  + (185 - 96) * x);
    const b = Math.round(34  + (80 - 34)  * (1 - x));
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  } else {
    const a = -x;
    const r = Math.round(96  + (248 - 96) * a);
    const g = Math.round(34  + (81 - 34)  * (1 - a));
    const b = Math.round(34  + (73 - 34)  * (1 - a));
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }
}

// Build per-strategy datum: feed `analyzeStrategy()` to get pnl/maxLoss/etc.
function _phmBuildData(sizeMode, colorMode) {
  const strats = (RAW_DATA && RAW_DATA.strategies) || [];
  const prevCloseMap = (RAW_DATA && RAW_DATA.equity_prev_close) || {};
  const items = [];
  strats.forEach((s, idx) => {
    let a;
    try { a = analyzeStrategy(s); } catch (e) { return; }
    if (!a) return;
    const maxLoss = Math.abs(a.maxLoss || 0);
    const maxProfit = Math.abs(a.maxProfit || 0);
    const size = (sizeMode === 'maxprofit' ? maxProfit : maxLoss);
    if (!isFinite(size) || size <= 0) return;

    // Underlying today % change — from spot vs prev-close
    const prev = prevCloseMap[s.underlying];
    const spot = s.current_price;
    const stockTodayPct = (prev > 0 && spot > 0) ? ((spot - prev) / prev * 100) : null;

    // Option spread today % change — todayPnl / current spread value
    const netDebit = a.netDebit || 0;
    const currentValue = netDebit + (a.unrealizedPnl || 0);
    const optionTodayPct = (Math.abs(currentValue) > 1e-6)
      ? ((a.todayPnl || 0) / Math.abs(currentValue) * 100) : null;

    // Option spread since-entry % — already exposed as unrealizedPct
    const optionTotalPct = a.unrealizedPct;

    let cval;
    if (colorMode === 'stock_today')           cval = stockTodayPct;
    else if (colorMode === 'option_today')     cval = optionTodayPct;
    else if (colorMode === 'option_total')     cval = optionTotalPct;
    else if (colorMode === 'option_total_abs') cval = a.unrealizedPnl || 0;
    else /* today_abs */                       cval = a.todayPnl || 0;

    items.push({
      idx,
      name: s.name || '',
      underlying: s.underlying || '?',
      sector: sectorOf(s.underlying || ''),
      maxLoss, maxProfit,
      todayPnl: a.todayPnl || 0,
      unrealizedPnl: a.unrealizedPnl || 0,
      unrealizedPct: a.unrealizedPct || 0,
      stockTodayPct, optionTodayPct, optionTotalPct,
      dte: a.dte,
      size,
      cval: (cval === null || cval === undefined || !isFinite(cval)) ? 0 : cval,
      cvalMissing: (cval === null || cval === undefined || !isFinite(cval)),
    });
  });
  return items;
}

function _phmBuildHierarchy(items, groupBy) {
  const root = { name: 'Positions', children: [] };
  if (groupBy === 'none' || !groupBy) {
    items.forEach(it => root.children.push({ name: it.underlying, value: it.size, it }));
    return root;
  }
  const key = (groupBy === 'sector') ? 'sector' : 'underlying';
  const groups = new Map();
  items.forEach(it => {
    const g = it[key] || '?';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(it);
  });
  for (const [g, list] of groups) {
    root.children.push({
      name: g,
      children: list.map(it => ({ name: it.underlying, value: it.size, it })),
    });
  }
  return root;
}

async function renderHeatmapView() {
  const view = document.getElementById('heatmapView');
  if (!view || view.style.display === 'none') return;

  const sizeMode  = document.getElementById('phmSize').value;
  const colorMode = document.getElementById('phmColor').value;
  const groupBy   = document.getElementById('phmGroup').value;

  const items = _phmBuildData(sizeMode, colorMode);
  const canvas = document.getElementById('phmCanvas');
  const svgEl  = document.getElementById('phmSvg');
  const tip    = document.getElementById('phmTip');
  const empty  = document.getElementById('phmEmpty');

  if (!items.length) {
    empty.style.display = 'flex';
    svgEl.innerHTML = '';
    return;
  }
  empty.style.display = 'none';

  // Color scale — auto-pick based on the actual distribution so single-digit
  // moves don't all wash out to dark green/red.
  const cvals = items.map(it => Math.abs(it.cval)).filter(v => v > 0).sort((a,b) => a - b);
  let scale;
  if (colorMode === 'today_abs') {
    // Dollar P/L — use 90th-percentile abs value, floor at $100, ceil at $5000
    const p90 = cvals.length ? cvals[Math.floor(cvals.length * 0.9)] : 100;
    scale = Math.max(100, Math.min(5000, p90 * 1.1));
  } else {
    // Percent — sensible bands: ±10% default, expand if larger moves dominate
    const p90 = cvals.length ? cvals[Math.floor(cvals.length * 0.9)] : 10;
    scale = Math.max(5, Math.min(50, p90 * 1.1));
  }

  const d3 = await _phmEnsureD3();
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w < 20 || h < 20) return;

  const root = d3.hierarchy(_phmBuildHierarchy(items, groupBy))
    .sum(d => d.value || 0)
    .sort((a, b) => (b.value || 0) - (a.value || 0));

  d3.treemap()
    .size([w, h])
    .paddingOuter(2)
    .paddingTop(d => (groupBy !== 'none' && d.depth === 1) ? 16 : 1)
    .paddingInner(1)
    .round(true)(root);

  const svg = d3.select(svgEl).attr('viewBox', '0 0 ' + w + ' ' + h)
                 .attr('preserveAspectRatio', 'none');
  svg.selectAll('*').remove();

  // Group frames (only when grouping is active)
  if (groupBy !== 'none') {
    const frames = root.descendants().filter(d => d.depth === 1 && d.children);
    svg.selectAll('g.phm-frame')
      .data(frames)
      .enter().append('g').attr('class', 'phm-frame')
      .each(function (d) {
        const g = d3.select(this);
        g.append('rect')
          .attr('x', d.x0).attr('y', d.y0)
          .attr('width', Math.max(0, d.x1 - d.x0))
          .attr('height', Math.max(0, d.y1 - d.y0))
          .attr('fill', 'none')
          .attr('stroke', '#58a6ff').attr('stroke-width', 1.2).attr('rx', 2);
        if (d.x1 - d.x0 > 60) {
          g.append('text')
            .attr('x', d.x0 + 5).attr('y', d.y0 + 11)
            .attr('fill', '#58a6ff').attr('font-size', 11)
            .attr('font-weight', 700).attr('font-family', 'system-ui, sans-serif')
            .text(d.data.name);
        }
      });
  }

  // Leaves
  const leaves = root.leaves();
  const leafG = svg.selectAll('g.phm-leaf')
    .data(leaves)
    .enter().append('g').attr('class', 'phm-leaf');

  leafG.append('rect')
    .attr('x', d => d.x0).attr('y', d => d.y0)
    .attr('width',  d => Math.max(0, d.x1 - d.x0))
    .attr('height', d => Math.max(0, d.y1 - d.y0))
    .attr('fill', d => _phmColorFor(d.data.it.cval, scale))
    .attr('stroke', '#010409').attr('stroke-width', 0.6)
    .style('cursor', 'pointer')
    .on('click', function (event, d) {
      // enterDetail records the source view (heatmap) BEFORE hiding it, so
      // "back" returns here. Don't hide heatmapView first — that would make
      // enterDetail see 'dash' as the source and send back to the main table.
      if (typeof enterDetail === 'function') enterDetail(d.data.it.idx);
    })
    .on('mousemove', function (event, d) {
      const it = d.data.it;
      const rect = canvas.getBoundingClientRect();
      const stockLine = (it.stockTodayPct === null || !isFinite(it.stockTodayPct))
        ? '<div style="color:#6e7681">Stock today: —</div>'
        : '<div style="color:' + (it.stockTodayPct >= 0 ? '#3fb950' : '#f85149') + '">' +
          'Stock today: ' + _phmFmtPct(it.stockTodayPct) + '</div>';
      const optTodayLine = (it.optionTodayPct === null || !isFinite(it.optionTodayPct))
        ? ''
        : '<div style="color:' + (it.optionTodayPct >= 0 ? '#3fb950' : '#f85149') + '">' +
          'Option today: ' + _phmFmtPct(it.optionTodayPct) +
          '  (' + _phmFmtMoney(it.todayPnl) + ')</div>';
      const optTotalLine = (it.optionTotalPct === null || !isFinite(it.optionTotalPct))
        ? ''
        : '<div style="color:' + (it.optionTotalPct >= 0 ? '#3fb950' : '#f85149') + '">' +
          'Option since entry: ' + _phmFmtPct(it.optionTotalPct) +
          '  (' + _phmFmtMoney(it.unrealizedPnl) + ')</div>';
      tip.innerHTML =
        '<div style="font-weight:700;color:#58a6ff;font-size:12px">' + it.underlying + '</div>' +
        '<div style="color:#8b949e">' + (it.name || '—') + '</div>' +
        stockLine + optTodayLine + optTotalLine +
        '<div style="margin-top:3px">Max Loss: ' + _phmFmtMoney(it.maxLoss) +
        '  ·  Max Profit: ' + _phmFmtMoney(it.maxProfit) +
        (isFinite(it.dte) ? '  ·  DTE ' + it.dte : '') + '</div>';
      tip.style.display = 'block';
      const tx = event.clientX - rect.left + 12;
      const ty = event.clientY - rect.top + 12;
      tip.style.left = Math.min(tx, rect.width - tip.offsetWidth - 8) + 'px';
      tip.style.top  = Math.min(ty, rect.height - tip.offsetHeight - 8) + 'px';
    })
    .on('mouseleave', () => { tip.style.display = 'none'; });

  // Labels — show underlying + today's P/L if rect is big enough
  leafG.append('text')
    .attr('x', d => (d.x0 + d.x1) / 2)
    .attr('y', d => (d.y0 + d.y1) / 2 - 2)
    .attr('text-anchor', 'middle')
    .attr('font-family', 'system-ui, sans-serif')
    .attr('font-weight', 700)
    .attr('fill', '#ffffff')
    .attr('pointer-events', 'none')
    .each(function (d) {
      const w = d.x1 - d.x0, h = d.y1 - d.y0;
      if (w < 28 || h < 18) { this.remove(); return; }
      const fs = Math.min(15, Math.max(9, Math.min(w / 4.2, h / 2.6)));
      d3.select(this).attr('font-size', fs).text(d.data.it.underlying);
    });

  leafG.append('text')
    .attr('x', d => (d.x0 + d.x1) / 2)
    .attr('y', d => (d.y0 + d.y1) / 2 + 12)
    .attr('text-anchor', 'middle')
    .attr('font-family', 'system-ui, sans-serif')
    .attr('font-weight', 600)
    .attr('fill', 'rgba(255,255,255,0.92)')
    .attr('pointer-events', 'none')
    .each(function (d) {
      const w = d.x1 - d.x0, h = d.y1 - d.y0;
      if (w < 60 || h < 42) { this.remove(); return; }
      const it = d.data.it;
      const txt = (colorMode === 'today_abs')
        ? _phmFmtMoney(it.todayPnl)
        : (it.cvalMissing ? '—' : _phmFmtPct(it.cval));
      d3.select(this).attr('font-size', Math.min(12, w / 6)).text(txt);
    });
}

// ── P/L Bars view ──────────────────────────────────────────────────────────
// Diverging bar chart of every strategy: bar height = return on the chosen
// metric, profit above the zero axis (green) / loss below (red), sorted high→low.
// Reuses the heatmap data builder (_phmBuildData), D3 loader, and money/pct
// formatters; clicking a bar drills into that strategy's detail via the same
// enterDetail() the heatmap tiles use, so "Back" returns here (see exitDetail).
let _pbResizeObs = null;

function openPnlBarsView() {
  appNavInto();
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('pnlBarsView').style.display = 'flex';
  renderPnlBarsView();
  const canvas = document.getElementById('pbCanvas');
  if (canvas && !_pbResizeObs) {
    _pbResizeObs = new ResizeObserver(() => renderPnlBarsView());
    _pbResizeObs.observe(canvas);
  }
}
function exitPnlBarsView() {
  document.getElementById('pnlBarsView').style.display = 'none';
  document.getElementById('dashboardView').style.display = '';
}

async function renderPnlBarsView() {
  const view = document.getElementById('pnlBarsView');
  if (!view || view.style.display === 'none') return;

  const metric  = document.getElementById('pbMetric').value;   // option_total|option_total_abs|option_today|today_abs|stock_today
  const isMoney = (metric === 'today_abs' || metric === 'option_total_abs');
  // _phmBuildData sets it.cval to the chosen metric. sizeMode only gates which
  // positions survive (size>0); 'maxloss' is the most inclusive. Drop positions
  // whose metric can't be computed (e.g. no prev-close) — can't rank a blank.
  const items = _phmBuildData('maxloss', metric)
    .filter(it => !it.cvalMissing && isFinite(it.cval))
    .sort((a, b) => b.cval - a.cval);

  const canvas = document.getElementById('pbCanvas');
  const svgEl  = document.getElementById('pbSvg');
  const tip    = document.getElementById('pbTip');
  const empty  = document.getElementById('pbEmpty');

  if (!items.length) {
    empty.style.display = 'flex';
    if (svgEl) svgEl.innerHTML = '';
    return;
  }
  empty.style.display = 'none';

  const d3 = await _phmEnsureD3();
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w < 20 || h < 20) return;

  const fmtVal = isMoney ? _phmFmtMoney : _phmFmtPct;
  const GREEN = '#3fb950', RED = '#f85149';

  // Reserve a bottom strip for vertical ticker labels so loss bars (which point
  // down) never collide with them.
  const M = { top: 24, right: 14, bottom: 52, left: 14 };
  const plotW = Math.max(10, w - M.left - M.right);
  const plotH = Math.max(10, h - M.top - M.bottom);

  // y domain — always include 0 so the zero axis is meaningful. Pad 12%.
  let vmax = d3.max(items, d => d.cval);
  let vmin = d3.min(items, d => d.cval);
  if (vmax < 0) vmax = 0;
  if (vmin > 0) vmin = 0;
  if (vmax === vmin) { vmax = vmax || 1; vmin = vmin || -1; }
  const pad = (vmax - vmin) * 0.12 || 1;
  const y = d3.scaleLinear().domain([vmin - pad, vmax + pad]).range([M.top + plotH, M.top]);
  const x = d3.scaleBand().domain(items.map((_, i) => i))
              .range([M.left, M.left + plotW]).padding(0.18);
  const bw = x.bandwidth();
  const zeroY = y(0);

  const svg = d3.select(svgEl).attr('viewBox', '0 0 ' + w + ' ' + h)
                 .attr('preserveAspectRatio', 'none');
  svg.selectAll('*').remove();

  // Zero axis line
  svg.append('line')
    .attr('x1', M.left).attr('x2', M.left + plotW)
    .attr('y1', zeroY).attr('y2', zeroY)
    .attr('stroke', '#484f58').attr('stroke-width', 1);

  const showLabels = bw >= 16;   // only label bars/tickers when columns are wide enough

  const bars = svg.selectAll('g.pb-bar').data(items).enter()
    .append('g').attr('class', 'pb-bar').style('cursor', 'pointer')
    .on('click', (event, d) => { if (typeof enterDetail === 'function') enterDetail(d.idx); })
    .on('mousemove', function (event, d) {
      const rect = canvas.getBoundingClientRect();
      const stockLine = (d.stockTodayPct === null || !isFinite(d.stockTodayPct)) ? '' :
        '<div style="color:' + (d.stockTodayPct >= 0 ? GREEN : RED) + '">Stock today: ' +
        _phmFmtPct(d.stockTodayPct) + '</div>';
      const optTodayLine = (d.optionTodayPct === null || !isFinite(d.optionTodayPct)) ? '' :
        '<div style="color:' + (d.optionTodayPct >= 0 ? GREEN : RED) + '">Option today: ' +
        _phmFmtPct(d.optionTodayPct) + '  (' + _phmFmtMoney(d.todayPnl) + ')</div>';
      const optTotalLine = (d.optionTotalPct === null || !isFinite(d.optionTotalPct)) ? '' :
        '<div style="color:' + (d.optionTotalPct >= 0 ? GREEN : RED) + '">Option since entry: ' +
        _phmFmtPct(d.optionTotalPct) + '  (' + _phmFmtMoney(d.unrealizedPnl) + ')</div>';
      tip.innerHTML =
        '<div style="font-weight:700;color:#58a6ff;font-size:12px">' + d.underlying + '</div>' +
        '<div style="color:#8b949e">' + (d.name || '—') + '</div>' +
        stockLine + optTodayLine + optTotalLine +
        '<div style="margin-top:3px">Max Loss: ' + _phmFmtMoney(d.maxLoss) +
        '  ·  Max Profit: ' + _phmFmtMoney(d.maxProfit) +
        (isFinite(d.dte) ? '  ·  DTE ' + d.dte : '') + '</div>';
      tip.style.display = 'block';
      const tx = event.clientX - rect.left + 12;
      const ty = event.clientY - rect.top + 12;
      tip.style.left = Math.min(tx, rect.width - tip.offsetWidth - 8) + 'px';
      tip.style.top  = Math.min(ty, rect.height - tip.offsetHeight - 8) + 'px';
    })
    .on('mouseleave', () => { tip.style.display = 'none'; });

  // Bar: from the zero line out to the value.
  bars.append('rect')
    .attr('x', (d, i) => x(i))
    .attr('y', d => Math.min(zeroY, y(d.cval)))
    .attr('width', bw)
    .attr('height', d => Math.max(1, Math.abs(y(d.cval) - zeroY)))
    .attr('rx', Math.min(2, bw / 3))
    .attr('fill', d => d.cval >= 0 ? GREEN : RED);

  if (showLabels) {
    // Value at the bar tip (above for profit, below for loss).
    bars.append('text')
      .attr('x', (d, i) => x(i) + bw / 2)
      .attr('y', d => d.cval >= 0 ? y(d.cval) - 4 : y(d.cval) + 11)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('font-size', Math.min(11, Math.max(8, bw / 3.2)))
      .attr('font-weight', 700)
      .attr('fill', d => d.cval >= 0 ? GREEN : RED)
      .attr('pointer-events', 'none')
      .text(d => fmtVal(d.cval));

    // Ticker — vertical, in the reserved bottom strip (below all bars).
    bars.append('text')
      .attr('transform', (d, i) => 'translate(' + (x(i) + bw / 2) + ',' + (M.top + plotH + 6) + ') rotate(90)')
      .attr('text-anchor', 'start')
      .attr('dominant-baseline', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('font-size', Math.min(10, Math.max(8, bw / 2.8)))
      .attr('font-weight', 600)
      .attr('fill', '#8b949e')
      .attr('pointer-events', 'none')
      .text(d => d.underlying);
  } else {
    // Too many positions to label every bar — say so (hover still has detail)
    // rather than leaving the axis mysteriously blank.
    svg.append('text')
      .attr('x', M.left).attr('y', M.top - 9)
      .attr('font-family', 'system-ui, sans-serif').attr('font-size', 10)
      .attr('fill', '#6e7681')
      .text(items.length + ' positions · hover a bar for detail');
  }
}

// Wire up the 30/90/52W toggle. Idempotent — safe to call on every overview
// open since the buttons live inside a static container.
let _ovCorrToggleBound = false;
function _bindCorrToggleOnce() {
  if (_ovCorrToggleBound) return;
  const root = document.getElementById('ovCorrToggle');
  if (!root) return;
  _ovCorrToggleBound = true;
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-w]');
    if (!btn) return;
    const w = parseInt(btn.dataset.w, 10);
    if (!w || w === _ovCorrWindow) return;
    _ovCorrWindow = w;
    root.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', parseInt(b.dataset.w, 10) === w));
    renderCorrelationBars();
    renderCorrMatrix();
  });
}

// Current BP usage as % of NLV — same source as the dashboard summary.
function _currentBpPct() {
  const balance = (RAW_DATA && RAW_DATA.balance) || {};
  const NLV = balance.net_liquidating_value || 0;
  const DBP = balance.derivative_buying_power || 0;
  if (NLV <= 0 || DBP <= 0) return 0;
  return Math.max(0, NLV - DBP) / NLV * 100;
}

// ── Events panel ──
function renderEventsPanel() {
  const host = document.getElementById('ovEventsList');
  if (!host) return;
  if (_ovEventsCache == null) {
    host.innerHTML = '<div class="ov-corr-loading">Loading events…</div>';
    if (!_ovEventsInflight) {
      _ovEventsInflight = fetch('/api/major-events?days=30')
        .then(r => r.json())
        .then(d => { _ovEventsCache = d; _ovEventsInflight = null; renderEventsPanel(); })
        .catch(() => { _ovEventsInflight = null; host.innerHTML = '<div class="ov-corr-loading">Failed to load events.</div>'; });
    }
    return;
  }
  const events = _ovEventsCache.events || [];
  const todayStr = _ovEventsCache.today || new Date().toISOString().slice(0, 10);
  if (!events.length) {
    host.innerHTML = '<div class="ov-corr-loading">No major events in next 30 days.</div>';
    return;
  }
  const bpPct = _currentBpPct();

  // Per-event attention rule:
  //   Tier 1 + (this week, ≤ 7d away) + BP > 25%  → ATTN
  //   Tier 2 + (this week, ≤ 7d away) + BP > 40%  → ATTN
  const isThisWeek = (daysAway) => daysAway >= 0 && daysAway <= 7;
  const attnFor = (ev) => {
    if (!isThisWeek(ev.days_away)) return false;
    if (ev.tier === 1) return bpPct > OV_EVENT_T1_BP_PCT;
    if (ev.tier === 2) return bpPct > OV_EVENT_T2_BP_PCT;
    return false;
  };

  // Top summary line
  const attnEvents = events.filter(attnFor);
  const summary = attnEvents.length
    ? `<div class="ov-events-summary"><span class="attn-badge">&#9888; ${attnEvents.length} ATTN</span>this-week events vs current BP ${bpPct.toFixed(1)}%</div>`
    : `<div class="ov-events-summary">${events.length} events tracked · BP ${bpPct.toFixed(1)}% (Tier 1 attn ≥ ${OV_EVENT_T1_BP_PCT}%, Tier 2 ≥ ${OV_EVENT_T2_BP_PCT}%)</div>`;

  let html = summary;
  events.forEach(ev => {
    const d = new Date(ev.date + 'T12:00:00');
    const dayLabel = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
    const dowLabel = d.toLocaleDateString('en-US', {weekday: 'short'});
    let dateCls = '';
    if (ev.days_away === 0) dateCls = 'today';
    else if (ev.days_away >= 1 && ev.days_away <= 7) dateCls = 'this-week';
    const isAttn = attnFor(ev);
    let tierCls = ev.tier === 1 ? 't1' : 't2';
    if (isAttn) tierCls += ' attn';
    const tierLbl = isAttn ? '&#9888; ATTN'
                  : ev.tier === 1 ? 'T1' : 'T2';
    const typeLbl = ev.type;
    html += `<div class="ov-event-row">
      <div class="ov-event-date ${dateCls}">${dayLabel}<small>${dowLabel} · ${ev.days_away === 0 ? 'today' : 'in ' + ev.days_away + 'd'}</small></div>
      <div class="ov-event-name"><span class="type">${typeLbl}</span>${ev.name}</div>
      <div class="ov-event-tier ${tierCls}">${tierLbl}</div>
    </div>`;
  });
  host.innerHTML = html;
}

// Sum |max_loss| per sector → render donut + legend.
function renderSectorPie() {
  const host = document.getElementById('ovSectorChart');
  const warnEl = document.getElementById('ovSectorWarn');
  if (!host) return;
  const strategies = (RAW_DATA && RAW_DATA.strategies) || [];
  // Aggregate sector → {risk, tickers: Set}
  const bySector = {};
  let total = 0;
  strategies.forEach(s => {
    let risk = 0;
    try {
      const a = analyzeStrategy(s);
      if (Math.abs(a.maxLoss) < 1e9) risk = Math.abs(a.maxLoss);
    } catch (e) { /* ignore */ }
    if (risk <= 0) return;
    const sec = sectorOf(s.underlying);
    if (!bySector[sec]) bySector[sec] = {val: 0, tickers: new Set()};
    bySector[sec].val += risk;
    bySector[sec].tickers.add(s.underlying);
    total += risk;
  });
  if (total <= 0) {
    host.innerHTML = '<div style="color:var(--text2);font-size:11px">No risk-weighted positions to chart.</div>';
    warnEl.innerHTML = '';
    return;
  }
  const entries = Object.entries(bySector)
    .map(([name, info]) => ({
      name,
      val: info.val,
      pct: info.val / total * 100,
      tickers: Array.from(info.tickers).sort(),
    }))
    .sort((a, b) => b.val - a.val);
  const names = entries.map(e => e.name);

  // Build SVG donut. Each slice carries data-* attributes so the JS hover
  // handler (showSectorTip) can pull them out without re-aggregating.
  const SIZE = 220, R_OUT = 100, R_IN = 60, CX = SIZE / 2, CY = SIZE / 2;
  let acc = 0;
  let arcs = '';
  entries.forEach((e, i) => {
    const startA = acc / 100 * 360;
    const endA = (acc + e.pct) / 100 * 360;
    acc += e.pct;
    const color = sectorColor(e.name, names);
    arcs += donutArcPath(CX, CY, R_IN, R_OUT, startA - 90, endA - 90, color, {
      name: e.name, tickers: e.tickers.join(', '),
      val: e.val, pct: e.pct, color
    });
  });

  let legend = '';
  entries.forEach(e => {
    const cls = e.pct >= OV_SECTOR_WARN_PCT ? 'warn' : '';
    legend += `<div class="ov-sector-legend-item">
      <span class="ov-sector-swatch" style="background:${sectorColor(e.name, names)}"></span>
      <span class="ov-sector-name">${e.name}</span>
      <span class="ov-sector-pct ${cls}">${e.pct.toFixed(1)}%</span>
    </div>`;
  });

  host.innerHTML = `
    <svg class="ov-sector-svg" width="${SIZE}" height="${SIZE}" viewBox="0 0 ${SIZE} ${SIZE}">
      ${arcs}
      <text x="${CX}" y="${CY - 4}" text-anchor="middle" font-size="11" fill="var(--text2)">Total Risk</text>
      <text x="${CX}" y="${CY + 14}" text-anchor="middle" font-size="16" fill="var(--text)" font-weight="700">$${total.toFixed(0)}</text>
    </svg>
    <div class="ov-sector-legend">${legend}</div>
  `;
  // Warning panel for sectors over 20%
  const breaches = entries.filter(e => e.pct >= OV_SECTOR_WARN_PCT);
  if (breaches.length) {
    const lines = breaches.map(e =>
      `<strong>${e.name}</strong>: ${e.pct.toFixed(1)}% ($${e.val.toFixed(0)})`).join(' · ');
    warnEl.innerHTML = `<div class="ov-warn">&#9888; Sector concentration > ${OV_SECTOR_WARN_PCT}%: ${lines}</div>`;
  } else {
    warnEl.innerHTML = '';
  }
}

// SVG donut slice path. angles are in degrees, 0 = right (we offset by -90
// elsewhere so 0 = top). innerR / outerR allow the donut hole. `meta` is
// {name, tickers, val, pct, color} — embedded as data-* attrs so the
// hover handler can pull info without re-aggregating.
function donutArcPath(cx, cy, innerR, outerR, startDeg, endDeg, color, meta) {
  const startRad = startDeg * Math.PI / 180;
  const endRad = endDeg * Math.PI / 180;
  const largeArc = (endDeg - startDeg) > 180 ? 1 : 0;
  const x1 = cx + outerR * Math.cos(startRad);
  const y1 = cy + outerR * Math.sin(startRad);
  const x2 = cx + outerR * Math.cos(endRad);
  const y2 = cy + outerR * Math.sin(endRad);
  const x3 = cx + innerR * Math.cos(endRad);
  const y3 = cy + innerR * Math.sin(endRad);
  const x4 = cx + innerR * Math.cos(startRad);
  const y4 = cy + innerR * Math.sin(startRad);
  const d = `M${x1},${y1} A${outerR},${outerR} 0 ${largeArc} 1 ${x2},${y2} L${x3},${y3} A${innerR},${innerR} 0 ${largeArc} 0 ${x4},${y4} Z`;
  const esc = (s) => String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  const handlers = meta
    ? ` data-sec="${esc(meta.name)}" data-tickers="${esc(meta.tickers)}" data-val="${meta.val.toFixed(0)}" data-pct="${meta.pct.toFixed(1)}" data-color="${meta.color}"
        onmouseenter="showSectorTip(event)" onmousemove="moveSectorTip(event)" onmouseleave="hideSectorTip()"`
    : '';
  return `<path d="${d}" fill="${color}" stroke="var(--bg2)" stroke-width="1" style="cursor:pointer;transition:opacity 0.12s"${handlers}/>`;
}

// Custom floating tooltip for pie slice hover — fast (no native delay),
// follows the cursor, color-coded to the slice.
function showSectorTip(ev) {
  const tip = document.getElementById('ovSectorTip');
  if (!tip) return;
  const t = ev.currentTarget;
  const sec = t.getAttribute('data-sec') || '';
  const tickers = t.getAttribute('data-tickers') || '';
  const val = t.getAttribute('data-val') || '0';
  const pct = t.getAttribute('data-pct') || '0';
  const color = t.getAttribute('data-color') || 'var(--text)';
  tip.style.borderLeftColor = color;
  tip.innerHTML = `<strong style="color:${color}">${sec}</strong>` +
                  `<div class="tickers">${tickers}</div>` +
                  `<div class="val"><strong>$${val}</strong> · ${pct}% of total</div>`;
  tip.style.display = 'block';
  // Slight slice highlight
  t.style.opacity = '0.85';
  moveSectorTip(ev);
}
function moveSectorTip(ev) {
  const tip = document.getElementById('ovSectorTip');
  if (!tip || tip.style.display === 'none') return;
  // Offset so tooltip doesn't sit under the cursor; flip to left edge if
  // we'd run off the right side of the viewport.
  const pad = 14;
  const tipW = tip.offsetWidth || 200;
  const tipH = tip.offsetHeight || 60;
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + tipW > window.innerWidth - 4) x = ev.clientX - pad - tipW;
  if (y + tipH > window.innerHeight - 4) y = ev.clientY - pad - tipH;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
function hideSectorTip() {
  const tip = document.getElementById('ovSectorTip');
  if (tip) tip.style.display = 'none';
  // Restore any highlighted slice
  document.querySelectorAll('#ovSectorChart svg path').forEach(p => p.style.opacity = '1');
}

// Heat color for a pairwise correlation cell. Equities are almost all weakly
// positive (shared market beta), so coloring from 0 drowns the signal in red.
// Instead: weak |corr| stays BLANK (transparent) and only meaningful pairs get
// color — red ramps from +0.5, blue from −0.3 — so the eye jumps to the
// clusters. (Thresholds mirror HL_POS / HL_NEG in renderCorrMatrix.)
function _corrCellColor(c) {
  if (c == null) return 'transparent';
  if (c >= 0.5) { const e = (Math.min(1, c) - 0.5) / 0.5; return 'rgba(248,81,73,' + (0.18 + 0.72 * e).toFixed(3) + ')'; }
  if (c <= -0.3) { const e = (Math.min(1, -c) - 0.3) / 0.7; return 'rgba(56,139,253,' + (0.18 + 0.62 * e).toFixed(3) + ')'; }
  return 'transparent';  // noise band → blank
}

// Pairwise correlation heatmap among the held tickers (NOT vs SPY). Surfaces
// hidden concentration: several "separate" rows in the dashboard that actually
// move as one bet. Shares the 30/90/52W window toggle with the SPY bars above.
function renderCorrMatrix() {
  const host = document.getElementById('ovCorrMatrix');
  if (!host) return;
  const strategies = (RAW_DATA && RAW_DATA.strategies) || [];
  const tickers = Array.from(new Set(strategies.map(s => s.underlying))).filter(Boolean);
  if (tickers.length < 2) {
    host.innerHTML = '<div class="ov-corr-loading">需要至少 2 个不同标的才能算两两相关。</div>';
    return;
  }
  const w = _ovCorrWindow;
  const wLabel = w === 252 ? '52W' : (w + 'd');
  const cached = _ovCorrMatrixCache[w];
  if (!cached) {
    host.innerHTML = '<div class="ov-corr-loading">Loading ' + wLabel + ' holdings correlation…</div>';
    fetch('/api/correlation-matrix?window=' + w + '&symbols=' + encodeURIComponent(tickers.join(',')))
      .then(r => r.json())
      .then(d => {
        _ovCorrMatrixCache[w] = { matrix: d.matrix || {}, symbols: d.symbols || tickers };
        if (_ovCorrWindow === w) renderCorrMatrix();
      })
      .catch(() => { host.innerHTML = '<div class="ov-corr-loading">Failed to load holdings correlation.</div>'; });
    return;
  }
  const matrix = cached.matrix || {};
  // Only chart tickers we currently hold; order so the most-correlated names
  // cluster at the top-left (sum of |corr| with everyone else, descending).
  const symbols = (cached.symbols || tickers).filter(t => tickers.includes(t));
  const concSum = t => symbols.reduce((a, o) =>
    a + (o !== t && matrix[t] && matrix[t][o] != null ? Math.abs(matrix[t][o]) : 0), 0);
  symbols.sort((a, b) => concSum(b) - concSum(a));
  // Highlight thresholds — below these a cell stays blank (noise). Keep in
  // sync with _corrCellColor.
  const HL_POS = 0.5, HL_NEG = -0.3;
  // Strong positive pairs → "重点" summary line, highest first.
  const strongPairs = [];
  for (let i = 0; i < symbols.length; i++) for (let j = i + 1; j < symbols.length; j++) {
    const a = symbols[i], b = symbols[j];
    const c = matrix[a] && matrix[a][b] != null ? matrix[a][b]
            : (matrix[b] && matrix[b][a] != null ? matrix[b][a] : null);
    if (c != null && c >= HL_POS) strongPairs.push({ a, b, c });
  }
  strongPairs.sort((x, y) => y.c - x.c);
  const CW = 46;
  let html = '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:10px;font-variant-numeric:tabular-nums">';
  html += '<tr><td style="position:sticky;left:0;background:var(--bg2);min-width:52px"></td>';
  symbols.forEach(t => { html += '<th style="padding:3px 0;width:' + CW + 'px;color:var(--text2);font-weight:600;text-align:center">' + t + '</th>'; });
  html += '</tr>';
  symbols.forEach(rt => {
    html += '<tr><td style="position:sticky;left:0;background:var(--bg2);padding:3px 8px 3px 0;color:var(--text2);font-weight:600;text-align:right;white-space:nowrap">' + rt + '</td>';
    symbols.forEach(ct => {
      if (rt === ct) {
        html += '<td style="width:' + CW + 'px;height:26px;text-align:center;color:var(--text3);background:rgba(110,118,129,0.12)">—</td>';
        return;
      }
      const c = matrix[rt] && matrix[rt][ct] != null ? matrix[rt][ct]
              : (matrix[ct] && matrix[ct][rt] != null ? matrix[ct][rt] : null);
      const colored = c != null && (c >= HL_POS || c <= HL_NEG);
      const strong = c != null && Math.abs(c) >= 0.7;
      // Sub-threshold (noise) cells: blank bg + faint number so they recede;
      // the eye lands only on the colored ones.
      const tcol = colored ? '#fff' : 'var(--text3)';
      const fw = strong ? '800' : (colored ? '700' : '400');
      const txt = c == null ? '·' : c.toFixed(2);
      html += '<td style="width:' + CW + 'px;height:26px;text-align:center;background:' + _corrCellColor(c) +
              ';color:' + tcol + ';font-weight:' + fw + '">' + txt + '</td>';
    });
    html += '</tr>';
  });
  html += '</table></div>';
  if (strongPairs.length) {
    const items = strongPairs.map(p =>
      '<span style="color:' + (p.c >= 0.7 ? 'var(--red)' : '#e8a33d') + '">' + p.a + '↔' + p.b +
      ' <strong>' + p.c.toFixed(2) + '</strong></span>').join('&nbsp;&nbsp;·&nbsp;&nbsp;');
    html += '<div style="font-size:11px;margin-top:10px;color:var(--text2);line-height:1.9">' +
            '高相关对（&ge;' + HL_POS.toFixed(2) + '，共 ' + strongPairs.length + ' 对，注意叠加风险）：' + items + '</div>';
  } else {
    html += '<div style="font-size:11px;margin-top:10px;color:var(--text2)">无 &ge;0.5 的高相关对 — 分散度尚可。</div>';
  }
  host.innerHTML = html;
}

// Correlation bars — fetch on first open, then re-render. Sorted descending
// so the "high beta with SPY" names surface and the diversifiers sink.
function renderCorrelationBars() {
  const host = document.getElementById('ovCorrChart');
  if (!host) return;
  const strategies = (RAW_DATA && RAW_DATA.strategies) || [];
  const tickers = Array.from(new Set(strategies.map(s => s.underlying))).filter(Boolean);
  if (!tickers.length) {
    host.innerHTML = '<div class="ov-corr-loading">No tickers to chart.</div>';
    return;
  }
  const w = _ovCorrWindow;
  const wLabel = w === 252 ? '52W' : (w + 'd');
  // Per-window cache key — switching windows doesn't show stale values from
  // a different lookback.
  const missing = tickers.filter(t => !((t + ':' + w) in _ovCorrCache));
  if (missing.length) {
    host.innerHTML = `<div class="ov-corr-loading">Loading ${wLabel} correlation vs SPY…</div>`;
    fetch('/api/correlation?window=' + w + '&symbols=' + encodeURIComponent(tickers.join(',')))
      .then(r => r.json())
      .then(d => {
        const corr = d.corr || {};
        // Stash under per-window keys; ignore servers that echo a different
        // window than requested (shouldn't happen, but be defensive).
        const respW = (d.window || w);
        for (const t of Object.keys(corr)) _ovCorrCache[t + ':' + respW] = corr[t];
        // Only re-render if the user hasn't already switched away.
        if (_ovCorrWindow === w) renderCorrelationBars();
      })
      .catch(() => { host.innerHTML = '<div class="ov-corr-loading">Failed to load correlation.</div>'; });
    return;
  }
  // Build sorted rows
  const rows = tickers.map(t => ({t, c: _ovCorrCache[t + ':' + w]}))
    .sort((a, b) => {
      const av = a.c == null ? -2 : a.c;
      const bv = b.c == null ? -2 : b.c;
      return bv - av;
    });
  let html = '';
  rows.forEach(({t, c}) => {
    if (c == null) {
      html += `<div class="ov-corr-row dim">
        <div class="ov-corr-name">${t}</div>
        <div class="ov-corr-bar-track">${corrBandHtml()}<div class="ov-corr-bar-zero"></div></div>
        <div class="ov-corr-val" style="color:var(--text3)">n/a</div>
      </div>`;
      return;
    }
    // Map -1..+1 to 0..100% with center line at 50%. Bar grows right for
    // positive corr (red — moves with SPY = no diversification), left for
    // negative (green — actual diversifier).
    const pctWidth = Math.abs(c) * 50;
    const left = c >= 0 ? 50 : 50 - pctWidth;
    // Anything with |corr| ≤ OV_CORR_ATTN sits in the neutral band — dim it
    // so the eye goes to the rows outside the band (real attention items).
    const inBand = Math.abs(c) <= OV_CORR_ATTN;
    const color = c >= OV_CORR_ATTN  ? 'rgba(248, 81, 73, 0.65)'   // high beta with SPY
                : c <= -OV_CORR_ATTN ? 'rgba(46, 200, 130, 0.65)'  // negative — true diversifier
                                     : 'rgba(99, 110, 123, 0.40)'; // in band — neutral grey
    html += `<div class="ov-corr-row${inBand ? ' dim' : ''}">
      <div class="ov-corr-name">${t}</div>
      <div class="ov-corr-bar-track">
        ${corrBandHtml()}
        <div class="ov-corr-bar-zero"></div>
        <div class="ov-corr-bar-fill" style="left:${left}%;width:${pctWidth}%;background:${color}"></div>
      </div>
      <div class="ov-corr-val">${c >= 0 ? '+' : ''}${c.toFixed(2)}</div>
    </div>`;
  });
  // Axis legend at top so the dashed-band meaning is obvious.
  const legend = `<div class="ov-corr-legend">
    <span></span>
    <div class="ov-corr-legend-axis"><span>−1</span><span>−${OV_CORR_ATTN}</span><span>0</span><span>+${OV_CORR_ATTN}</span><span>+1</span></div>
    <span></span>
  </div>`;
  host.innerHTML = legend + html;
}

// Background shading for the |corr| ≤ OV_CORR_ATTN band — visually marks the
// "doesn't need attention" zone, with dashed edges at ±OV_CORR_ATTN.
function corrBandHtml() {
  const left = (1 - OV_CORR_ATTN) * 50;
  const width = OV_CORR_ATTN * 100;
  return `<div class="ov-corr-band" style="left:${left}%;width:${width}%"></div>`;
}

function fmtSign(v, digits) {
  digits = digits || 0;
  const s = v >= 0 ? '+' : '-';
  return `${s}$${Math.abs(v).toFixed(digits)}`;
}

// Sort state for risk table
let riskSort = { col: 'worst2', dir: 'asc' };  // most-negative (worst) first by default

function sortRisk(col) {
  if (riskSort.col === col) riskSort.dir = riskSort.dir === 'asc' ? 'desc' : 'asc';
  else { riskSort.col = col; riskSort.dir = col === 'name' ? 'asc' : 'asc'; }
  renderRiskView();
}

// Build rows as data first, then sort, then render
function buildRiskRows() {
  const strategies = RAW_DATA.strategies || [];
  const rows = [];
  strategies.forEach((s, i) => {
    let a;
    try { a = analyzeStrategy(s); } catch (e) { return; }
    const st1 = computeStressTest(s, a, 1.0);
    const st2 = computeStressTest(s, a, 2.0);
    const vc = computeVarCvar(s, a);
    const ep = expectedProfitAtExpiry(s, s.current_price, s.risk_free_rate, a.iv || s.iv || 0.3, a.maxProfit);
    rows.push({
      idx: i,
      name: `${s.underlying} ${s.name}`,
      iv: a.iv || s.iv || 0,
      dte: a.dte,
      move: st1.move,
      worst1: st1.worst,
      worst2: st2.worst,
      var: vc.var,
      cvar: vc.cvar,
      maxloss: a.maxLoss,
      maxprofit: a.maxProfit,
      captured: (a.maxProfit > 0 && a.maxProfit < 1e9) ? (a.unrealizedPnl / a.maxProfit) : (a.maxProfit >= 1e9 ? Infinity : 0),
      pnl: a.unrealizedPnl,
      netDebit: a.netDebit,
      ev: ep.ev,
      pmax: ep.pMax,
      pop: a.cop,
    });
  });
  return rows;
}

function renderRiskView() {
  // 子tab 分发：full_refresh/softRefresh 只调这一个入口，按当前子tab重渲染
  if (riskTab === 'time') { renderTimeView(); return; }
  if (riskTab === 'scenario') { renderScenarioView(); return; }
  if (riskTab === 'prob') { renderProbView(); return; }
  const NLV = liveNLV();
  const cash = (RAW_DATA.balance && RAW_DATA.balance.cash_balance) || 0;
  const rows = buildRiskRows();
  const b = _ckBand();   // regime 自适应止盈带(牛 70/80 · 震荡 50/60 · 中性 65/75)

  // Sort
  const {col, dir} = riskSort;
  const mul = dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    if (col === 'name') return mul * a.name.localeCompare(b.name);
    return mul * ((a[col]||0) - (b[col]||0));
  });

  // Update header arrows
  const arw = dir === 'asc' ? ' &#9650;' : ' &#9660;';
  document.querySelectorAll('#riskTable th').forEach(th => {
    const c = th.dataset.rsort;
    const base = th.innerHTML.replace(/ [&#▲▼9650;▼9660;]+$/, '').replace(/ ▲$| ▼$/, '');
    if (!th.dataset.rlabel) th.dataset.rlabel = base;
    th.innerHTML = th.dataset.rlabel + (c === col ? arw : '');
  });

  // Compute totals (order-independent so use unsorted sum)
  let totPnl = 0, tot1s = 0, tot2s = 0, totVar = 0, totCvar = 0, totMaxLoss = 0, totActualRisk = 0;
  let totMaxProfit = 0, totPnlBounded = 0, nUnbounded = 0, totEv = 0;
  rows.forEach(r => {
    totPnl += r.pnl; tot1s += r.worst1; tot2s += r.worst2;
    totVar += r.var; totCvar += r.cvar; totMaxLoss += r.maxloss;
    totActualRisk += Math.abs(r.maxloss <= -1e9 ? 0 : r.maxloss);
    if (r.maxprofit != null && r.maxprofit > 0 && r.maxprofit < 1e9) { totMaxProfit += r.maxprofit; totPnlBounded += r.pnl; }
    else if (r.maxprofit >= 1e9) { nUnbounded++; }
    if (r.ev != null && isFinite(r.ev)) totEv += r.ev;
  });

  // Render rows
  const col2 = v => v >= 0 ? 'var(--green)' : 'var(--red)';
  let rowsHtml = '';
  rows.forEach(r => {
    const dteStr = r.dte < 1 ? r.dte.toFixed(1)+'d' : Math.round(r.dte)+'d';
    const mlStr = r.maxloss <= -1e9 ? '&minus;∞' : fmtSign(r.maxloss);
    rowsHtml += `<tr onclick="enterDetail(${r.idx})" style="cursor:pointer">
      <td><div class="dash-name">${r.name}</div></td>
      <td>${(r.iv*100).toFixed(0)}%</td>
      <td>${dteStr}</td>
      <td>±$${r.move.toFixed(2)}</td>
      <td style="color:${col2(r.worst1)}">${fmtSign(r.worst1)}</td>
      <td style="color:${col2(r.worst2)}">${fmtSign(r.worst2)}</td>
      <td style="color:${col2(r.var)}">${fmtSign(r.var)}</td>
      <td style="color:${col2(r.cvar)}">${fmtSign(r.cvar)}</td>
      <td style="color:var(--red)">${mlStr}</td>
      <td style="color:var(--green)">${r.maxprofit >= 1e9 ? '∞' : (r.maxprofit > 0 ? '$'+r.maxprofit.toFixed(0) : fmtSign(r.maxprofit))}</td>
      <td style="color:${r.captured===Infinity?'var(--text2)':(r.captured>=b.soft?'var(--green)':r.captured>=b.near?'#d29922':'var(--text)')};font-weight:600">${r.captured===Infinity?'∞':((r.maxprofit>0&&r.maxprofit<1e9)?(r.captured*100).toFixed(0)+'%':'—')}</td>
      <td style="color:${(r.pop||0)>=60?'var(--green)':(r.pop||0)>=40?'#d29922':'var(--text)'}">${r.pop!=null?r.pop.toFixed(0)+'%':'—'}</td>
      <td style="color:${(r.pmax!=null&&r.pmax>=0.5)?'var(--green)':'var(--text2)'}">${r.pmax!=null?(r.pmax*100).toFixed(0)+'%':'—'}</td>
    </tr>`;
  });

  const nlvStr = NLV > 0 ? `$${NLV.toFixed(0)}` : 'N/A';
  const cashStr = cash > 0 ? `$${cash.toFixed(0)}` : 'N/A';
  const riskPctNlv = NLV > 0 ? (totActualRisk / NLV * 100).toFixed(1) : '—';
  const tot2sPctNlv = NLV > 0 ? (Math.abs(tot2s) / NLV * 100).toFixed(1) : '—';

  const sumHtml = `
    <div class="risk-summary-row"><span class="risk-summary-label">Account Net Liquidating Value (bankroll)</span><span class="risk-summary-value">${nlvStr}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">Cash available</span><span class="risk-summary-value">${cashStr}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">Current unrealized P/L</span><span class="risk-summary-value" style="color:${col2(totPnl)}">${fmtSign(totPnl)}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">Tomorrow 1σ worst (~68% band)</span><span class="risk-summary-value" style="color:${col2(tot1s)}">${fmtSign(tot1s)}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">Tomorrow 2σ worst (~95% band, stress) &nbsp;<span style="color:var(--text2);font-weight:400">${tot2sPctNlv}% of NLV</span></span><span class="risk-summary-value" style="color:${col2(tot2s)}">${fmtSign(tot2s)}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">VaR 95% (1-day)</span><span class="risk-summary-value" style="color:${col2(totVar)}">${fmtSign(totVar)}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">CVaR 95% (1-day expected shortfall)</span><span class="risk-summary-value" style="color:${col2(totCvar)}">${fmtSign(totCvar)}</span></div>
    <div class="risk-summary-row"><span class="risk-summary-label">Max theoretical loss &nbsp;<span style="color:var(--text2);font-weight:400">${riskPctNlv}% of NLV</span></span><span class="risk-summary-value" style="color:var(--red)">${fmtSign(totMaxLoss)}</span></div>
  `;
  const captured = totMaxProfit > 0 ? (totPnlBounded / totMaxProfit * 100) : 0;
  const remaining = totMaxProfit - totPnlBounded;
  const unbNote = nUnbounded > 0 ? ` <span style="color:var(--text2);font-weight:400">(${nUnbounded} 个无限上限未计入)</span>` : '';
  const upHtml = `
    <div style="background:rgba(63,185,80,0.08);border:1px solid rgba(63,185,80,0.35);border-radius:8px;padding:8px 14px;margin-bottom:12px">
      <div class="risk-summary-row"><span class="risk-summary-label">&#128176; 理想最大盈利(全部持有到期)${unbNote}</span><span class="risk-summary-value" style="color:var(--green)">$${totMaxProfit.toFixed(0)}</span></div>
      <div class="risk-summary-row"><span class="risk-summary-label">&#127922; 概率加权期望(到期) <span style="color:var(--text2);font-weight:400">— IV/DTE 对数正态加权,无方向假设</span></span><span class="risk-summary-value" style="color:${col2(totEv)}">${fmtSign(totEv)}</span></div>
      <div class="risk-summary-row"><span class="risk-summary-label">当前浮盈 · 已吃到 <b style="color:var(--green)">${captured.toFixed(0)}%</b> 最大利润</span><span class="risk-summary-value" style="color:${col2(totPnlBounded)}">${fmtSign(totPnlBounded)}</span></div>
      <div class="risk-summary-row"><span class="risk-summary-label">还能赚到顶(剩余空间)</span><span class="risk-summary-value" style="color:var(--green)">+$${remaining.toFixed(0)}</span></div>
      <div class="risk-summary-row"><span class="risk-summary-label">&#127919; ${Math.round(b.soft*100)}% 软止盈线 <span style="color:var(--text2);font-weight:400">— ${b.label} ${b.zone} 建议落袋(强制 ${Math.round(b.forced*100)}%);随 regime 自适应</span></span><span class="risk-summary-value" style="color:var(--green)">$${(totMaxProfit*b.soft).toFixed(0)}</span></div>
    </div>`;
  document.getElementById('riskSummary').innerHTML = upHtml + sumHtml;
  document.getElementById('riskBody').innerHTML = rowsHtml;
  renderPortfolioGreeks();
}

// ════════ 组合级希腊 · Beta加权Delta · SPY情景盈亏曲线 ════════════════════
// 全账户合计 vs SPY:净 β加权Delta(SPY当量)、净Theta/天、净Vega、净Gamma,
// 外加"明天 SPY 涨跌% → 组合盈亏"折线图(逐策略在 β移动后的标的价上用 BSM 重定价)。
let _pfBeta = { betas: {}, spy: null, missing: [], key: '' };

async function renderPortfolioGreeks() {
  const host = document.getElementById('portfolioGreeksBlock');
  if (!host) return;
  const strats = (RAW_DATA.strategies || []).filter(s => s.legs && s.legs.length && s.current_price > 0);
  if (!strats.length) { host.innerHTML = ''; return; }
  const NLV = liveNLV();

  // 取 beta(按标的集合缓存;变了才重新 fetch)
  const unders = Array.from(new Set(strats.map(s => s.underlying).filter(Boolean))).sort();
  const key = unders.join(',');
  if (key !== _pfBeta.key) {
    host.innerHTML = '<div style="padding:14px;color:var(--text2);font-size:12px">⏳ 正在计算组合级希腊 + Beta加权…</div>';
    try {
      const r = await fetch('/api/portfolio/betas?symbols=' + encodeURIComponent(key));
      const d = await r.json();
      _pfBeta = { betas: d.betas || {}, spy: d.spy || null, missing: d.missing || [], key };
    } catch (e) {
      _pfBeta = { betas: {}, spy: null, missing: [], key };
    }
    if (document.getElementById('riskView').style.display === 'none') return; // 期间切走了
  }
  const betaOf = u => (_pfBeta.betas[u] != null ? _pfBeta.betas[u] : 1.0);

  // 逐策略:greeks 聚合 + 预存情景重定价参数
  let netDelta = 0, netTheta = 0, netVega = 0, netGamma = 0, bwdNotional = 0;
  const params = [];
  strats.forEach(s => {
    let a; try { a = analyzeStrategy(s); } catch (e) { return; }
    const g = a.greeks;
    netDelta += g.delta; netTheta += g.theta; netVega += g.vega; netGamma += g.gamma;
    const b = betaOf(s.underlying);
    bwdNotional += g.delta * s.current_price * b;     // $SPY 当量名义
    params.push({ s, S: s.current_price, r: s.risk_free_rate, iv: a.iv, T: a.T, b });
  });
  const per1pct = bwdNotional * 0.01;                  // SPY 每 +1% 的组合 $ 变化(线性近似)
  const spy = _pfBeta.spy;
  const spyShares = spy ? bwdNotional / spy : null;

  // 情景曲线:SPY -6%..+6%,逐策略在 β移动后价位重定价(纯价格冲击,过原点)
  const moves = [];
  for (let m = -0.06; m <= 0.0601; m += 0.0025) moves.push(Math.round(m * 10000) / 10000);
  const base = params.reduce((acc, p) => acc + pnlAtFuture(p.s, p.S, p.T, p.r, p.iv), 0);
  const curve = moves.map(m => {
    let v = 0;
    params.forEach(p => { v += pnlAtFuture(p.s, Math.max(0.01, p.S * (1 + p.b * m)), p.T, p.r, p.iv); });
    return v - base;
  });
  const pnlAt = mv => { const i = moves.findIndex(x => Math.abs(x - mv) < 1e-9); return i >= 0 ? curve[i] : null; };
  const dn2 = pnlAt(-0.02), up2 = pnlAt(0.02);

  // ── 渲染 ──
  const money = v => (v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)).toLocaleString();
  const col = v => v >= 0 ? 'var(--green)' : 'var(--red)';
  const shares = v => (v >= 0 ? '+' : '-') + Math.round(Math.abs(v));
  const pctNlv = v => NLV > 0 ? (v / NLV * 100).toFixed(1) + '%' : '—';

  const tile = (label, big, bigColor, sub) =>
    '<div style="flex:1;min-width:148px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px">' +
      '<div style="font-size:11px;color:var(--text2);margin-bottom:3px">' + label + '</div>' +
      '<div style="font-size:19px;font-weight:800;color:' + bigColor + ';font-variant-numeric:tabular-nums">' + big + '</div>' +
      '<div style="font-size:10px;color:var(--text2);margin-top:2px;line-height:1.4">' + sub + '</div>' +
    '</div>';

  const tiles =
    tile('β加权 Delta（SPY当量）', money(bwdNotional), col(bwdNotional),
         '≈ ' + (spyShares != null ? shares(spyShares) + ' 股SPY' : '—') + ' · SPY每+1%≈ <b style="color:' + col(per1pct) + '">' + money(per1pct) + '</b>') +
    tile('净 Theta / 天', money(netTheta), col(netTheta),
         netTheta >= 0 ? '每天<b>收</b>时间价值' : '每天<b>付</b>时间价值') +
    tile('净 Vega（每+1vol点）', money(netVega), col(netVega),
         netVega >= 0 ? '净<b>做多</b>波动率' : '净<b>做空</b>波动率') +
    tile('净 Gamma', (netGamma >= 0 ? '+' : '') + netGamma.toFixed(1), col(netGamma),
         '曲率 · 市场涨则Delta变' + (netGamma >= 0 ? '多' : '空'));

  const dirWord = bwdNotional >= 0 ? '多' : '空';
  const concPct = NLV > 0 ? Math.abs(bwdNotional) / NLV * 100 : 0;
  let concNote;
  if (concPct >= 50) concNote = ' <b style="color:var(--red)">⚠️ 方向性敞口偏大</b>(净敞口≈NLV的' + concPct.toFixed(0) + '%),小心无意中押同一边。';
  else if (concPct >= 25) concNote = ' 方向性敞口中等(≈NLV的' + concPct.toFixed(0) + '%)。';
  else concNote = ' 方向上相对均衡。';

  const narr =
    '你整个仓位 β加权后 <b style="color:' + col(bwdNotional) + '">净' + dirWord + '头 ≈ ' + money(bwdNotional) + ' SPY当量</b>' +
    (spyShares != null ? '(≈ ' + shares(spyShares) + ' 股SPY)' : '') + '。' +
    'SPY 明天每 ±1%,组合约 <b style="color:' + col(per1pct) + '">' + money(per1pct) + '</b>;' +
    (dn2 != null ? '若 SPY −2% 约 <b style="color:' + col(dn2) + '">' + money(dn2) + '</b>(' + pctNlv(dn2) + ' NLV),+2% 约 <b style="color:' + col(up2) + '">' + money(up2) + '</b>。' : '') +
    concNote +
    ' 另外时间价值每天 <b style="color:' + col(netTheta) + '">' + money(netTheta) + '</b>,IV每+1点 <b style="color:' + col(netVega) + '">' + money(netVega) + '</b>(净' + (netVega >= 0 ? '做多' : '做空') + '波动率)。';

  host.innerHTML =
    '<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px;flex-wrap:wrap">' +
      '<h3 style="margin:0;font-size:14px;color:var(--text)">📊 组合级希腊 · Beta加权（全账户合计 vs SPY）</h3>' +
      '<span style="font-size:11px;color:var(--text2)">' + strats.length + ' 个策略 · ' + unders.length + ' 个标的' +
        (_pfBeta.missing && _pfBeta.missing.length ? ' · ' + _pfBeta.missing.length + ' 个无beta按1.0' : '') + '</span>' +
    '</div>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">' + tiles + '</div>' +
    '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:12.5px;line-height:1.8;color:var(--text);margin-bottom:12px">' + narr + '</div>' +
    '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px 6px">' +
      '<div style="font-size:12px;color:var(--text2);margin-bottom:4px">明天 SPY 涨跌% → 组合盈亏($,β加权·纯价格冲击)<span style="float:right;font-size:10px;color:var(--text2)">悬停看具体数值</span></div>' +
      '<div style="position:relative">' +
        '<canvas id="spyScenCanvas" style="width:100%;height:230px;display:block"></canvas>' +
        '<div id="spyScenTip" style="position:absolute;pointer-events:none;display:none;background:#0d1117;border:1px solid var(--border);border-radius:4px;padding:3px 7px;font-size:11px;color:var(--text);white-space:nowrap;z-index:6"></div>' +
      '</div>' +
    '</div>';

  drawSpyScenario(moves, curve, NLV);
}

function drawSpyScenario(moves, curve, NLV) {
  const canvas = document.getElementById('spyScenCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = rect.width || 600, H = rect.height || 230;
  canvas.width = W * dpr; canvas.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 60, r: 16, t: 12, b: 26 };
  const gW = W - pad.l - pad.r, gH = H - pad.t - pad.b;
  const xMin = moves[0], xMax = moves[moves.length - 1];
  let yMin = Math.min(0, ...curve), yMax = Math.max(0, ...curve);
  const yPad = (yMax - yMin) * 0.14 || 100; yMin -= yPad; yMax += yPad;
  const X = m => pad.l + (m - xMin) / (xMax - xMin) * gW;
  const Y = v => pad.t + gH - (v - yMin) / (yMax - yMin) * gH;
  const fmtY = v => (v >= 0 ? '+$' : '-$') + Math.abs(Math.round(v)).toLocaleString();

  ctx.font = '10px -apple-system,system-ui,monospace';
  // 横网格 + y 轴标签
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * i / 4, y = Y(v);
    ctx.strokeStyle = 'rgba(120,130,150,0.13)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = '#8b949e'; ctx.fillText(fmtY(v), pad.l - 6, y);
  }
  // 竖网格 + x 轴标签(整 %)
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (let p = -6; p <= 6; p += 2) {
    const m = p / 100; if (m < xMin - 1e-9 || m > xMax + 1e-9) continue;
    const x = X(m);
    ctx.strokeStyle = 'rgba(120,130,150,0.10)'; ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + gH); ctx.stroke();
    ctx.fillStyle = '#8b949e'; ctx.fillText((p > 0 ? '+' : '') + p + '%', x, pad.t + gH + 6);
  }
  // 盈/亏填充(绿上红下)
  const fillHalf = (above, color) => {
    ctx.save(); ctx.beginPath();
    if (above) ctx.rect(pad.l, pad.t, gW, Math.max(0, Y(0) - pad.t));
    else ctx.rect(pad.l, Y(0), gW, Math.max(0, pad.t + gH - Y(0)));
    ctx.clip();
    ctx.beginPath(); ctx.moveTo(X(moves[0]), Y(0));
    moves.forEach((m, i) => ctx.lineTo(X(m), Y(curve[i])));
    ctx.lineTo(X(moves[moves.length - 1]), Y(0)); ctx.closePath();
    ctx.fillStyle = color; ctx.fill(); ctx.restore();
  };
  fillHalf(true, 'rgba(63,185,80,0.13)');
  fillHalf(false, 'rgba(248,81,73,0.13)');
  // 零线(P&L=0 横、move=0 竖)
  ctx.setLineDash([4, 3]); ctx.strokeStyle = 'rgba(205,210,220,0.4)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, Y(0)); ctx.lineTo(W - pad.r, Y(0)); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(X(0), pad.t); ctx.lineTo(X(0), pad.t + gH); ctx.stroke();
  ctx.setLineDash([]);
  // 曲线
  ctx.lineWidth = 2.2; ctx.strokeStyle = '#58a6ff'; ctx.beginPath();
  moves.forEach((m, i) => { const x = X(m), y = Y(curve[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
  // ±1/±2% 标记点
  [-0.02, -0.01, 0.01, 0.02].forEach(mv => {
    const i = moves.findIndex(x => Math.abs(x - mv) < 1e-9); if (i < 0) return;
    ctx.fillStyle = curve[i] >= 0 ? '#3fb950' : '#f85149';
    ctx.beginPath(); ctx.arc(X(moves[i]), Y(curve[i]), 3, 0, 6.3); ctx.fill();
  });
  canvas._scen = { moves, curve, X, Y, xMin, xMax, NLV, gW, pad };
  _attachScenHover(canvas);
}

function _attachScenHover(canvas) {
  if (canvas._hover) return; canvas._hover = true;
  canvas.addEventListener('mousemove', ev => {
    const sc = canvas._scen; const tip = document.getElementById('spyScenTip');
    if (!sc || !tip) return;
    const rect = canvas.getBoundingClientRect();
    let m = sc.xMin + (ev.clientX - rect.left - sc.pad.l) / sc.gW * (sc.xMax - sc.xMin);
    m = Math.max(sc.xMin, Math.min(sc.xMax, m));
    let bi = 0, bd = 1e9;
    sc.moves.forEach((mm, i) => { const d = Math.abs(mm - m); if (d < bd) { bd = d; bi = i; } });
    const pnl = sc.curve[bi];
    tip.style.display = 'block';
    tip.style.left = Math.min(rect.width - 130, Math.max(0, sc.X(sc.moves[bi]) + 8)) + 'px';
    tip.style.top = Math.max(0, sc.Y(pnl) - 26) + 'px';
    const pct = sc.NLV > 0 ? ' (' + (pnl / sc.NLV * 100).toFixed(1) + '% NLV)' : '';
    tip.innerHTML = 'SPY ' + (sc.moves[bi] > 0 ? '+' : '') + (sc.moves[bi] * 100).toFixed(2) + '% → ' +
      (pnl >= 0 ? '+$' : '-$') + Math.abs(Math.round(pnl)).toLocaleString() + pct;
  });
  canvas.addEventListener('mouseleave', () => { const t = document.getElementById('spyScenTip'); if (t) t.style.display = 'none'; });
}

function generatePriceRange(S, pctRange, steps) {
  steps = steps || 200;
  const low = S * (1 - pctRange);
  const high = S * (1 + pctRange);
  const step = (high - low) / steps;
  const prices = [];
  for (let p = low; p <= high + 0.01; p += step) prices.push(p);
  return prices;
}

// Multi-expiration P/L at evaluation date — front-month legs use intrinsic
// once expired, back-month legs use BSM theo at their remaining T. Same
// model as analyzeStrategy's max profit/loss block. Required for breakeven
// and chance-of-profit on calendar/diagonal (single-T intrinsic gave 0
// values that never crossed zero, hence empty BE / 0% POP).
function _pnlAtEarliestExp(strat, price, fallbackIv) {
  const r = strat.risk_free_rate;
  const legExps = strat.legs.map(l => nyClose(l.exp || strat.expiration));
  // Slider override (fallbackIv) scales each leg's IV proportionally so
  // the per-strike skew survives but moving the slider still changes the
  // P/L curve / breakevens / POP.
  const ivScale = (strat.iv > 0 && fallbackIv > 0) ? fallbackIv / strat.iv : 1;
  const legIvs = strat.legs.map(l => (+l.iv > 0 ? +l.iv * ivScale : fallbackIv));
  const earliestExp = new Date(Math.min(...legExps.map(d => d.getTime())));
  let pnl = 0;
  strat.legs.forEach((l, li) => {
    const T_at = Math.max(0, (legExps[li] - earliestExp) / (365 * 86400000));
    let val;
    if (T_at <= 0) {
      val = l.type === 'C' ? Math.max(0, price - l.strike)
                           : Math.max(0, l.strike - price);
    } else {
      val = bsmPrice(price, l.strike, T_at, r, legIvs[li], l.type);
    }
    pnl += (val - l.entry) * l.qty * l.mult;
  });
  return pnl;
}

// pnlOffset shifts the whole curve down by a price-independent cost before
// solving for zero — used to fold commissions into the breakeven so it agrees
// with the fee-adjusted Max Loss / Max Profit shown next to it. Defaults to 0,
// so every existing caller keeps the raw mid-price breakeven.
function findBreakevens(strat, fallbackIv, pnlOffset) {
  const S = strat.current_price;
  const prices = generatePriceRange(S, 0.5, 1000);
  const breakevens = [];
  let prevPnl = null;
  const off = +pnlOffset || 0;
  const ivFb = (fallbackIv != null) ? fallbackIv : (strat.iv || 0.30);
  for (const price of prices) {
    const pnl = _pnlAtEarliestExp(strat, price, ivFb) - off;
    if (prevPnl !== null && ((prevPnl < 0 && pnl >= 0) || (prevPnl >= 0 && pnl < 0))) {
      const prevPrice = price - (prices[1] - prices[0]);
      const ratio = Math.abs(prevPnl) / (Math.abs(prevPnl) + Math.abs(pnl));
      breakevens.push(prevPrice + ratio * (price - prevPrice));
    }
    prevPnl = pnl;
  }
  return breakevens;
}

function calcChanceOfProfit(S, breakevens, T, r, iv, strat) {
  // Sample prices, check which are profitable using multi-expiration P/L,
  // weight by lognormal density to T_eval (earliest leg expiration).
  // Guard iv=0 (sigma=0 → density divides by zero → NaN → POP=0%); use a
  // sensible default so the user still sees a meaningful number.
  const ivEff = (iv > 0.001) ? iv : 0.30;
  const legExps = strat.legs.map(l => nyClose(l.exp || strat.expiration));
  const earliestExp = new Date(Math.min(...legExps.map(d => d.getTime())));
  const T_eval = Math.max(1e-6, (earliestExp - new Date()) / (365 * 86400000));
  const prices = generatePriceRange(S, 0.5, 500);
  let profitableProb = 0;
  for (let i = 0; i < prices.length - 1; i++) {
    const pMid = (prices[i] + prices[i+1]) / 2;
    const width = prices[i+1] - prices[i];
    const pnl = _pnlAtEarliestExp(strat, pMid, ivEff);
    if (pnl > 0) {
      const mu = Math.log(S) + (r - 0.5*ivEff*ivEff)*T_eval;
      const sig = ivEff * Math.sqrt(T_eval);
      const lnP = Math.log(pMid);
      const density = Math.exp(-0.5*Math.pow((lnP-mu)/sig, 2)) / (pMid * sig * Math.sqrt(2*Math.PI));
      profitableProb += density * width;
    }
  }
  if (!isFinite(profitableProb)) return 0;
  return Math.min(100, Math.max(0, profitableProb * 100));
}

// 概率加权:到期 P/L 的期望值 + 到达最大盈利的概率。
// 按对数正态(风险中性 drift=r, IV 隐含分布,无方向性假设)对到期 payoff 积分。
// 网格 [0.02S, 4S] 比 COP 的 ±50% 宽,基本覆盖全部概率质量;按捕获质量归一化纠正截断。
function expectedProfitAtExpiry(strat, S, r, iv, maxProfit) {
  if (!(S > 0) || !strat.legs || !strat.legs.length) return { ev: null, pMax: null };
  const ivEff = (iv > 0.001) ? iv : 0.30;
  const legExps = strat.legs.map(l => nyClose(l.exp || strat.expiration));
  const T = Math.max(1e-6, (new Date(Math.min(...legExps.map(d => d.getTime()))) - new Date()) / (365 * 86400000));
  const mu = Math.log(S) + (r - 0.5 * ivEff * ivEff) * T;
  const sig = ivEff * Math.sqrt(T);
  if (!(sig > 0)) return { ev: null, pMax: null };
  const intr = (p, k, t) => t === 'C' ? Math.max(0, p - k) : Math.max(0, k - p);
  const lo = S * 0.02, hi = S * 4, N = 600, dp = (hi - lo) / N;
  const bounded = maxProfit > 0 && maxProfit < 1e9;
  const maxThresh = bounded ? maxProfit - Math.max(1, maxProfit * 0.01) : Infinity;
  let ev = 0, pMax = 0, tot = 0;
  for (let i = 0; i < N; i++) {
    const p = lo + (i + 0.5) * dp;
    const z = (Math.log(p) - mu) / sig;
    const prob = (Math.exp(-0.5 * z * z) / (p * sig * Math.sqrt(2 * Math.PI))) * dp;
    if (!isFinite(prob) || prob <= 0) continue;
    let payoff = 0;
    for (const l of strat.legs) payoff += (intr(p, l.strike, l.type) - l.entry) * l.qty * l.mult;
    ev += payoff * prob; tot += prob;
    if (bounded && payoff >= maxThresh) pMax += prob;
  }
  if (tot <= 0) return { ev: null, pMax: null };
  return { ev: ev / tot, pMax: bounded ? pMax / tot : null };
}

// ── P/L Matrix Calculation ──
function computePLMatrix(strat, rangePct, ivOverride) {
  const S = strat.current_price;
  const r = strat.risk_free_rate;
  const iv = ivOverride != null ? ivOverride : strat.iv;
  const today = new Date();
  const expDate = nyClose(strat.expiration);
  const totalDTE_exact = Math.max(0.01, (expDate - today) / 86400000);  // raw float for calculations
  const totalDTE = Math.max(1, Math.round(totalDTE_exact));

  // Generate date/time columns — 20 cols fixed
  // Compact column count — ~10 time slices keeps the table from sprawling
  // horizontally. Includes the expiration as the last column.
  const FIXED_COLS = 10;
  const dates = [];
  const dateLabels = [];  // sub-header (time or day number)
  const monthLabels = []; // group header (day+weekday or month)
  const dayNames = ['Su','M','Tu','W','Th','F','Sa'];

  const totalHours = (expDate - today) / 3600000;

  // Convert NY ET market hours into the user's local clock — so a CT
  // user sees columns 8:30am–3:00pm, a PT user sees 6:30am–1:00pm, etc.
  // ET users see 9:30am–4:00pm unchanged.
  const _nyToLocalOffsetHrs = (function () {
    try {
      const now = new Date();
      // Same instant, formatted in NY tz
      const nyStr = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(now);
      const [nyH, nyM] = nyStr.split(':').map(Number);
      const local = now.getHours() + now.getMinutes() / 60;
      const ny    = nyH + nyM / 60;
      let diff = local - ny;
      if (diff > 12)  diff -= 24;  // wrap (e.g. Asia)
      if (diff < -12) diff += 24;
      return diff;
    } catch (_) { return 0; }
  })();
  const marketOpen  = 9.5 + _nyToLocalOffsetHrs;  // 9:30 AM ET → user-local
  const marketClose = 16  + _nyToLocalOffsetHrs;  // 4:00 PM ET → user-local
  const marketHoursPerDay = marketClose - marketOpen; // 6.5h

  function _fmt12hr(hr) {
    let h = Math.floor(hr) % 24;
    if (h < 0) h += 24;
    const m = Math.round((hr - Math.floor(hr)) * 60);
    const ampm = h >= 12 ? 'pm' : 'am';
    const h12 = h > 12 ? h - 12 : (h === 0 ? 12 : h);
    return h12 + ':' + (m < 10 ? '0' : '') + m + ampm;
  }

  // Use intra-day sampling whenever trading-day count would fall short of
  // 10 columns. ~14 calendar days ≈ 10 trading days; beyond that the day
  // grid has enough headroom for 9 sampled days + 1 expiration = 10 cols.
  if (totalDTE <= 14) {
    // SHORT DTE: time-based columns (like OptionStrat)
    // Collect all remaining market-hours slots in 1-minute granularity so
    // even a single-day window (DTE=1, 6.5h × 60 = 390 slots) has enough
    // resolution to sample 9 unique columns + expiration.
    const slots = [];
    // Bound the day loop by the expiration CALENDAR day, not the rounded
    // `totalDTE`. A 0DTE's raw span is <1 day, so Math.round→0 then
    // Math.max(1,…)→1 forced dayOff to reach 1 = TOMORROW, injecting phantom
    // next-day columns (14 Tu → 15 W → 14 Tu) whose dt sat past the 4pm-ET
    // expiry (T_at clamped to 0 → flat intrinsic). Anchor to expDate's date.
    const _expCal   = new Date(expDate.getFullYear(), expDate.getMonth(), expDate.getDate());
    const _todayCal = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const _daySpan  = Math.max(0, Math.round((_expCal - _todayCal) / 86400000));
    for (let dayOff = 0; dayOff <= _daySpan; dayOff++) {
      const dayDate = new Date(today.getFullYear(), today.getMonth(), today.getDate() + dayOff);
      if (dayDate.getDay() === 0 || dayDate.getDay() === 6) continue; // skip weekends
      const dayLabel = dayDate.getDate() + ' ' + dayNames[dayDate.getDay()];
      const monthLabel = dayDate.toLocaleString('en', {month: 'short'});
      // Start hour: if today, from now; else from market open
      let startH = marketOpen;
      if (dayOff === 0) {
        const nowH = today.getHours() + today.getMinutes() / 60;
        startH = Math.max(marketOpen, nowH);
      }
      const endH = marketClose;
      // 1-minute step gives ~390 slots per full trading day — plenty for
      // the 9-slot sample to land on distinct timestamps.
      for (let h = startH; h <= endH; h += 1/60) {
        const dt = new Date(dayDate.getFullYear(), dayDate.getMonth(), dayDate.getDate(), Math.floor(h), Math.round((h % 1) * 60));
        slots.push({dt, dayLabel, monthLabel});
      }
    }
    // Sample (FIXED_COLS - 1) slots EVENLY using fractional indexing —
    // floor-step lost columns on small slot pools (5 slots → 5 cols
    // instead of forcing 9 distinct samples). round(i × (N−1) / 8)
    // distributes 9 picks across [0, N−1] with no clipping.
    const _slotTarget = Math.max(1, FIXED_COLS - 1);
    if (slots.length > 0) {
      const denom = Math.max(1, _slotTarget - 1);
      const seenIdx = new Set();
      for (let i = 0; i < _slotTarget && dates.length < _slotTarget; i++) {
        const idx = Math.min(slots.length - 1,
                             Math.round(i * (slots.length - 1) / denom));
        if (seenIdx.has(idx)) continue;
        seenIdx.add(idx);
        const s = slots[idx];
        dates.push(s.dt);
        const hh = s.dt.getHours();
        const mm = s.dt.getMinutes();
        const ampm = hh >= 12 ? 'pm' : 'am';
        const h12 = hh > 12 ? hh - 12 : (hh === 0 ? 12 : hh);
        dateLabels.push(h12 + ':' + (mm < 10 ? '0' : '') + mm + ampm);
        monthLabels.push(s.dayLabel);
      }
    }
    // Always include expiration as last column — the true 4pm-ET close instant
    // (nyClose, == expDate). Same epoch as legExpDates below, so a same-day
    // (0DTE) leg lands exactly on expiry (daysToExp=0 → intrinsic). The column
    // LABEL still uses marketClose (local clock, e.g. 3:00pm CT).
    const _expDt = nyClose(strat.expiration);
    if (dates.length > 0 && dates[dates.length-1].getTime() !== _expDt.getTime()) {
      dates.push(_expDt);
      dateLabels.push(_fmt12hr(marketClose));
      const expDay = _expDt.getDate() + ' ' + dayNames[_expDt.getDay()];
      monthLabels.push(expDay);
    }
  } else {
    // NORMAL DTE: date-based columns.
    // Skip today's column when the market is closed — at/after 4pm local
    // (or weekend), the live BSM curve already represents end-of-today, so
    // a "today" column adds no information vs. the live overlay.
    const nowH_today = today.getHours() + today.getMinutes() / 60;
    const dow_today = today.getDay();
    const isPostClose = (nowH_today >= 16) || dow_today === 0 || dow_today === 6;
    const startBase = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    if (isPostClose) startBase.setDate(startBase.getDate() + 1);

    // Build the list of TRADING days only (Mon–Fri) between startBase and
    // expDate. Saturdays and Sundays were showing up as columns ("16Sa",
    // "17Su") even though the market is closed — confusing and a waste of
    // a column. Sampling from a weekday-only array fixes that without
    // changing the per-leg BSM math (which already uses calendar T).
    const tradingDays = [];
    {
      const cursor = new Date(startBase.getTime());
      while (cursor <= expDate) {
        const dow = cursor.getDay();
        if (dow !== 0 && dow !== 6) {
          tradingDays.push(new Date(cursor.getTime()));
        }
        cursor.setDate(cursor.getDate() + 1);
      }
    }
    if (tradingDays.length === 0) tradingDays.push(new Date(startBase.getTime()));

    // Sample exactly (FIXED_COLS - 1) distinct trading days using
    // fractional indexing across [0, lastIdx] where lastIdx excludes
    // expiration day (reserved for the 10th column). This guarantees
    // 10 unique-day columns total — no "13W, 13W" duplicate header
    // when the last sampled day equals the expiration day.
    const _colTarget = Math.max(1, FIXED_COLS - 1);
    const _denom = Math.max(1, _colTarget - 1);
    const _lastIdx = Math.max(0, tradingDays.length - 2);  // exclude exp day
    const _seenDayIdx = new Set();
    for (let i = 0; i < _colTarget; i++) {
      const idx = (_lastIdx <= 0) ? 0
                : Math.min(_lastIdx, Math.round(i * _lastIdx / _denom));
      if (_seenDayIdx.has(idx)) continue;
      _seenDayIdx.add(idx);
      const dt = tradingDays[idx];
      dates.push(dt);
      dateLabels.push(dt.getDate() + dayNames[dt.getDay()]);
      monthLabels.push(dt.toLocaleString('en', {month: 'short'}));
    }
    // Always include expiration as last column. Dedupe — fractional
    // sampling can land its 9th pick on the expiration day for short
    // calendar windows; double-pushing would give 11 cols and a stuttery
    // last header.
    if (dates.length === 0 ||
        dates[dates.length - 1].getTime() !== expDate.getTime()) {
      dates.push(expDate);
      dateLabels.push(expDate.getDate() + dayNames[expDate.getDay()]);
      monthLabels.push(expDate.toLocaleString('en', {month: 'short'}));
    }
  }

  // Generate price rows — 15-row grid (7 above spot + spot + 7 below).
  // Slightly taller than the previous 11-row "compact" — gives better
  // strike resolution while still fitting within the graph's height.
  const FIXED_ROWS = 15;
  const ROWS_ABOVE = 7;
  const ROWS_BELOW = 7;
  const pctRange = rangePct / 100;
  const totalRange = S * pctRange * 2;
  let rawStep = totalRange / (FIXED_ROWS - 1);

  // Round step to a "nice" increment
  let priceInc;
  if (rawStep >= 10) priceInc = Math.round(rawStep / 5) * 5;
  else if (rawStep >= 2) priceInc = Math.round(rawStep / 2.5) * 2.5 || 2.5;
  else if (rawStep >= 0.5) priceInc = Math.round(rawStep * 2) / 2;
  else priceInc = Math.round(rawStep * 10) / 10 || 0.1;
  if (priceInc <= 0) priceInc = 0.5;

  // Center on current stock price, always include exact S in grid
  const centerPrice = Math.round(S / priceInc) * priceInc;
  const prices = [];
  for (let i = -ROWS_ABOVE; i <= ROWS_BELOW; i++) {
    const p = centerPrice - i * priceInc;
    if (p > 0) prices.push(Math.round(p * 100) / 100);
  }
  // Ensure exact current price is in the grid (replace nearest point)
  let nearestIdx = 0, nearestDist = Infinity;
  prices.forEach((p, i) => { const d = Math.abs(p - S); if (d < nearestDist) { nearestDist = d; nearestIdx = i; } });
  prices[nearestIdx] = S;

  // Per-leg expiration dates — calendars/diagonals have legs at different
  // expirations. Falls back to strat.expiration when leg.exp is missing.
  // Uses nyClose() = true 4pm-ET close instant (see nyClose for the non-ET
  // machine bug this prevents). Same epoch as the expiration column `_expDt`,
  // so a 0DTE leg lands exactly on expiry → daysToExp=0 → intrinsic max profit.
  const legExpDates = strat.legs.map(l => nyClose(l.exp || strat.expiration));

  // Per-leg IV — when each leg has its own implied vol from the chain
  // (skew-aware), use it. Otherwise fall back to strat.iv. The single
  // `iv` arg is treated as a multiplicative SCALE on top of skew so the
  // IV slider still moves the whole table uniformly — without this, an
  // override like 60% gets ignored because every leg has its own iv.
  const _ivScalePm = (strat.iv > 0 && iv > 0) ? iv / strat.iv : 1;
  const legIvs = strat.legs.map(l => (+l.iv > 0 ? +l.iv * _ivScalePm : iv));

  // Per-leg calibration: adjust BSM so today's column matches actual market
  // mids. For each leg we calibrate at ITS OWN remaining T_today.
  const _now = new Date();
  const legAdj = strat.legs.map((l, li) => {
    const T_today = Math.max(0, (legExpDates[li] - _now) / (365 * 86400000));
    const bsmToday = bsmPrice(S, l.strike, T_today, r, legIvs[li], l.type);
    const actualMid = l.mid || l.current || bsmToday;
    return actualMid - bsmToday;  // model error per share
  });

  // Matrix table matches unrealized P/L (pure mark-to-market, no fees) — TastyTrade style
  const tableFees = 0;

  // Compute P/L matrix — per-leg T per column. After a leg's expiration
  // date, that leg collapses to intrinsic; longer-dated legs keep BSM
  // theo value (this is what makes calendar/diagonal P/L move with price).
  const values = [];
  for (const price of prices) {
    const row = [];
    for (const dt of dates) {
      let pnl = 0;
      strat.legs.forEach((l, li) => {
        const daysToExp_leg = Math.max(0, (legExpDates[li] - dt) / 86400000);
        const T_leg = daysToExp_leg / 365;
        let theoPrice;
        if (T_leg <= 0) {
          theoPrice = l.type === 'C' ? Math.max(0, price - l.strike)
                                     : Math.max(0, l.strike - price);
        } else {
          theoPrice = bsmPrice(price, l.strike, T_leg, r, legIvs[li], l.type);
        }
        // Decay calibration toward zero as each leg approaches its own exp.
        const T_today_leg = Math.max(0, (legExpDates[li] - _now) / (365 * 86400000));
        const decay = T_today_leg > 0 ? Math.min(1, T_leg / T_today_leg) : 0;
        const calibrated = theoPrice + legAdj[li] * decay;
        pnl += (calibrated - l.entry) * l.qty * l.mult;
      });
      row.push(Math.round((pnl - tableFees) * 100) / 100);
    }
    values.push(row);
  }

  // Find breakeven row index
  let beIdx = -1;
  for (let i = 0; i < prices.length - 1; i++) {
    const lastCol = values[i][values[i].length - 1];
    const nextLastCol = values[i+1][values[i+1].length - 1];
    if ((lastCol >= 0 && nextLastCol < 0) || (lastCol <= 0 && nextLastCol > 0)) {
      beIdx = lastCol >= 0 ? i : i + 1;
      break;
    }
  }

  // Find earnings date column
  let earningsColIdx = -1;
  if (strat.earnings_date) {
    const eDate = new Date(strat.earnings_date + 'T16:00:00');
    for (let i = 0; i < dates.length; i++) {
      if (Math.abs(dates[i] - eDate) < 86400000) {
        earningsColIdx = i;
        break;
      }
    }
  }

  // Find current price row index (closest to S)
  let currentIdx = 0;
  let minDist = Infinity;
  prices.forEach((p, i) => {
    const dist = Math.abs(p - S);
    if (dist < minDist) { minDist = dist; currentIdx = i; }
  });

  return { dates, dateLabels, monthLabels, prices, values, beIdx, earningsColIdx, currentIdx, tableFees };
}

// ── Rendering ──
let state = {
  selectedIdx: 0,
  view: 'table',
  valueMode: 'pnl',
  rangePct: 10,
  ivPct: null,  // null = use strategy IV
  graphDateIdx: 0,
  pricingMode: 'mid',
  customPrice: null,  // user-entered price to highlight in table
};

function init() {
  const strategies = RAW_DATA.strategies;
  if (!strategies || strategies.length === 0) {
    document.getElementById('dashBody').innerHTML = '<tr><td colspan="10" style="padding:40px;text-align:center;color:var(--text2)">No option strategies found.</td></tr>';
    return;
  }
  renderDashboard(strategies);
}

// 默认按 underlying 今日涨跌幅 ↓(涨得最猛的仓位放最上面)
let dashSort = (function () {
  try { const s = localStorage.getItem('dashSort'); if (s) return JSON.parse(s); } catch (e) {}
  return { col: 'under', dir: 'desc' };
})();
let dashViewMode = 'table';  // 'table' | 'viz'

// When true, renderDashViz becomes a no-op so the user can read the bars
// without them re-laying out on every quote. Auto-cleared whenever the user
// navigates away (toggle to table, enter detail, switch tabs, refresh).
let dashVizFrozen = false;

// Compact mode strips the sub-line (DTE/IV), days estimates, and BE label
// from the bar — leaves just name + bar + status %. Lets ~20 positions fit
// on a 1080p monitor without scrolling.
let dashVizCompact = false;

// Warning thresholds — surface in the summary strip and on the buttons
const DASH_BP_WARN_PCT = 50;     // BP used > 50% of NLV → yellow
const DASH_TICKER_WARN = 15;     // distinct underlyings >= 15 → yellow
const DASH_CASH_WARN_PCT = 50;   // cash < 50% of NLV → yellow(留半仓现金对冲急跌);< 30% → red

// Update the summary strip (count of spreads, count of tickers, BP used).
// BP Used = NLV − derivative_buying_power_remaining, matching how TT itself
// computes "buying power consumed" (cash committed to debit spreads + margin
// reserved for credit spreads + house overhead). Falls back to Σ|max_loss|
// when DBP isn't reported (paper / partial data).
// Adds .warn / .danger classes when thresholds breached.
// ── 出场分类快捷筛选(主列表)─────────────────────────
let dashExitFilter = '';   // '' = 全部 | cut | tp | near | hold
function setExitFilter(code) {
  dashExitFilter = (dashExitFilter === code) ? '' : code;   // 再点同一个 = 清除
  if (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) renderDashboard(RAW_DATA.strategies);
}
document.addEventListener('keydown', function (e) {
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.metaKey || e.ctrlKey || e.altKey) return;
  const dash = document.getElementById('dashboardView');
  if (!dash || dash.style.display === 'none') return;            // 仅主列表可见时
  const ck = document.getElementById('checkupOverlay');
  if (ck && ck.style.display === 'block') return;                // 体检弹窗开着不拦
  const map = { '1': 'cut', '2': 'tp', '3': 'near', '4': 'hold', '0': '' };
  if (Object.prototype.hasOwnProperty.call(map, e.key)) { e.preventDefault(); setExitFilter(map[e.key]); }
});

// Total Return 列 $/% 主显示切换(切到 % 即按 % 排序)
let dashTotalMode = 'amt';   // 'amt' = $在前(default) | 'pct' = %在前
function toggleTotalMode() {
  dashTotalMode = (dashTotalMode === 'amt') ? 'pct' : 'amt';
  if (typeof dashSort !== 'undefined') dashSort.col = 'total';   // 切换即按本列(主显示)排序
  if (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) renderDashboard(RAW_DATA.strategies);
}

// ── macOS 两指左滑 = 浏览器后退 → app 内返回(SPA history 集成,跟普通网页一样)──
// 进任何子视图/detail/overlay 时 pushState;popstate(滑动/后退键)退一层。
function appNavInto() { try { history.pushState({ app: 1 }, ''); } catch (e) {} }
function _navVisFlow(id) { const el = document.getElementById(id); return !!el && el.offsetParent !== null; }       // 普通流元素(detail/子视图)
function _navVisFixed(id) { const el = document.getElementById(id); return !!el && el.style.display && el.style.display !== 'none'; }  // fixed overlay
let _lastBackT = 0;
function appBack() {
  let _now = 0; try { _now = Date.now(); } catch (e) {}
  if (_now - _lastBackT < 350) return false;   // 防 popstate + wheel 同一手势双触发
  _lastBackT = _now;
  // 优先级:overlay → detail → 子视图。返回是否退了一层。
  if (_navVisFixed('liquidityOverlay') && typeof closeLiquidity === 'function') { closeLiquidity(); return true; }
  if (_navVisFixed('checkupOverlay') && typeof closeCheckup === 'function') { closeCheckup(); return true; }
  if (_navVisFixed('riskRadarOverlay') && typeof closeRiskRadar === 'function') { closeRiskRadar(); return true; }
  if (_navVisFixed('alertOverlay')) { if (typeof closeAlertPanel === 'function') closeAlertPanel(); else document.getElementById('alertOverlay').style.display = 'none'; return true; }
  if (_navVisFixed('tvOverlay')) { if (typeof closeTvChart === 'function') closeTvChart(); else document.getElementById('tvOverlay').style.display = 'none'; return true; }
  if (_navVisFlow('detailView') && typeof exitDetail === 'function') { exitDetail(); return true; }
  const subs = { overviewView: 'exitOverviewView', heatmapView: 'exitHeatmapView', pnlBarsView: 'exitPnlBarsView', riskView: 'exitRiskView' };
  for (const id in subs) {
    if (_navVisFlow(id) && typeof window[subs[id]] === 'function') { window[subs[id]](); return true; }
  }
  if (typeof dashViewMode !== 'undefined' && dashViewMode === 'viz' && typeof toggleDashView === 'function') { toggleDashView(); return true; }
  return false;
}
window.addEventListener('popstate', function () { appBack(); });
// 两指横扫 → 返回 已移除:宽表格(Risk 逐持仓表 / TA / Heatmap / Digest)横滑到边缘
// 常被误判成"返回" → 跳回主屏。改为只用 tab 导航 + 浏览器原生后退(上面的 popstate);
// 横向两指滑动只用来滚动表格,不再触发任何返回。(作者多次反馈此误触,确认去掉。)

// ── 实时 NLV(取代 /balances 滞后字段)─────────────────────────────────
// = 现金 + Σ(每条腿 signed_qty × current × mult) + Σ股票市值。
// leg.current 由 quote_update(逐 tick=DXLink mid)+ marks_refresh(每30s=REST mark)实时维护,
// 与持仓卡未实现盈亏同源、对得上 TastyTrade App。balances 的 net-liq 后台整块慢更新,急涨可滞后
function liveNLV() {
  try {
    const bal = (typeof RAW_DATA !== 'undefined' && RAW_DATA.balance) || {};
    const booked = bal.net_liquidating_value || 0;
    const cash = bal.cash_balance;
    const strategies = (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) || [];
    if (cash == null || !strategies.length) return booked;
    let opt = 0;
    for (const s of strategies) {
      for (const l of (s.legs || [])) {
        const px = (l.current != null) ? l.current : (l.mark != null ? l.mark : l.mid);
        if (px == null || !isFinite(px)) return booked;   // 任一腿缺价 → 回退 booked,别给半截数
        opt += (l.qty || 0) * px * (l.mult || 100);
      }
    }
    let eq = 0;
    const eqs = (typeof RAW_DATA !== 'undefined' && RAW_DATA.equities) || [];
    for (const e of eqs) {
      const q = (e.direction === 'Short' ? -1 : 1) * (e.quantity || 0);
      eq += q * (e.current || 0);
    }
    const live = cash + opt + eq;
    if (!isFinite(live) || live <= 0) return booked;
    if (booked > 0 && Math.abs(live - booked) / booked > 0.5) return booked;  // 异常偏离 → 防灾回退
    return live;
  } catch (e) {
    return (typeof RAW_DATA !== 'undefined' && RAW_DATA.balance && RAW_DATA.balance.net_liquidating_value) || 0;
  }
}

function updateDashSummary() {
  const el = document.getElementById('dashSummary');
  if (!el) return;
  const strategies = (RAW_DATA && RAW_DATA.strategies) || [];
  const balance = (RAW_DATA && RAW_DATA.balance) || {};
  const NLV = liveNLV();                                  // 头条 NLV + 百分比分母 = 实时口径
  const NLV_booked = balance.net_liquidating_value || 0;  // BP Used 仍配 booked(与 DBP 同一快照)
  const DBP = balance.derivative_buying_power || 0;
  const tickers = new Set(strategies.map(s => s.underlying));
  const spreadCount = strategies.length;
  const tickerCount = tickers.size;

  let bpUsed = (NLV_booked > 0 && DBP > 0) ? Math.max(0, NLV_booked - DBP) : 0;
  if (bpUsed === 0) {
    // Fallback: Σ capital at risk per spread.
    strategies.forEach(s => {
      try {
        const a = analyzeStrategy(s);
        if (Math.abs(a.maxLoss) < 1e9) bpUsed += Math.abs(a.maxLoss);
      } catch (e) { /* ignore malformed */ }
    });
  }
  const bpPct = NLV > 0 ? (bpUsed / NLV * 100) : 0;

  const tickerCls = tickerCount >= DASH_TICKER_WARN ? 'warn' : '';
  const tickerWarn = tickerCount >= DASH_TICKER_WARN ? '<span class="warn-icon">&#9888;</span>' : '';
  const bpCls = bpPct >= DASH_BP_WARN_PCT * 1.5 ? 'danger'
              : bpPct >= DASH_BP_WARN_PCT      ? 'warn' : '';
  const bpWarn = bpPct >= DASH_BP_WARN_PCT ? '<span class="warn-icon">&#9888;</span>' : '';

  // Cash chip — 现金 + 占 NLV 百分比。低于 50% 黄警(留半仓现金对冲急跌),低于 30% 红警。
  // Cash 来自 TT balances 的 cash-balance,与 liveNLV 分子里的 cash 同源。
  const cash = balance.cash_balance || 0;
  const cashPct = NLV > 0 ? (cash / NLV * 100) : 0;
  const cashCls = cashPct < DASH_CASH_WARN_PCT * 0.6 ? 'danger'
                : cashPct < DASH_CASH_WARN_PCT       ? 'warn' : '';
  const cashWarn = cashPct < DASH_CASH_WARN_PCT ? '<span class="warn-icon">&#9888;</span>' : '';

  // 出场四态计数(主列表快捷筛选 chip)
  const vc = { cut: 0, tp: 0, near: 0, hold: 0 };
  strategies.forEach(s => {
    try {
      const a = analyzeStrategy(s);
      const be = a.breakevens || []; let bp = null;
      if (be.length && s.current_price > 0) { let c = be[0]; be.forEach(b => { if (Math.abs(b - s.current_price) < Math.abs(c - s.current_price)) c = b; }); bp = (c - s.current_price) / s.current_price * 100; }
      const ev = ckExitVerdict(a.unrealizedPnl, a.maxProfit, s.dte, a.unrealizedPct);
      if (vc[ev.code] != null) vc[ev.code]++;
    } catch (e) { /* ignore */ }
  });
  const F = (typeof dashExitFilter !== 'undefined') ? dashExitFilter : '';
  const _chip = (code, emoji, label, key) => {
    const on = F === code;
    return `<span onclick="setExitFilter('${code}')" data-tip="只看${label}的仓位(快捷键 ${key};再按一次清除)" style="cursor:pointer;display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:5px;font-size:11px;border:1px solid ${on ? '#58a6ff' : '#30363d'};background:${on ? 'rgba(88,166,255,0.14)' : 'transparent'};color:#c9d1d9">${emoji} ${label} <strong style="color:#e6edf3">${vc[code]}</strong></span>`;
  };
  const filters = `<span style="display:inline-flex;gap:5px;margin-left:8px;align-items:center">`
    + _chip('cut', '🔴', '深亏', '1') + _chip('tp', '🔵', '止盈', '2')
    + _chip('near', '🟡', '接近', '3') + _chip('hold', '🟢', '持有', '4')
    + `<span onclick="setExitFilter('')" data-tip="显示全部(快捷键 0)" style="cursor:pointer;padding:2px 7px;border-radius:5px;font-size:11px;border:1px solid ${F ? '#30363d' : '#58a6ff'};background:${F ? 'transparent' : 'rgba(88,166,255,0.14)'};color:#8b949e">全部</span></span>`;
  el.innerHTML =
    `<span class="dash-stat ${tickerCls}">${tickerWarn}<span class="lbl">Tickers</span><strong>${tickerCount}</strong></span>` +
    `<span class="dash-stat ${bpCls}" title="NLV − remaining derivative buying power (matches TastyTrade's BP-consumed calc)">${bpWarn}<span class="lbl">BP Used</span><strong>$${bpUsed.toFixed(0)}</strong> <span style="color:var(--text3);font-size:10px">${bpPct.toFixed(1)}% of NLV</span></span>` +
    `<span class="dash-stat ${cashCls}" title="账户现金余额 · 低于 50% NLV 触发预警(留半仓现金对冲急跌,低于 30% 转红)">${cashWarn}<span class="lbl">Cash</span><strong>$${cash.toFixed(0)}</strong> <span style="color:var(--text3);font-size:10px">${cashPct.toFixed(1)}% of NLV</span></span>` +
    `<span class="dash-stat"><span class="lbl">NLV</span><strong>$${NLV.toFixed(0)}</strong></span>` +
    filters;
}

// P/L at expiration at any underlying price. Pure leg payoffs minus net debit.
// Works for any combination (verticals, condors, calendars treated as expiry).
function plAtExpiration(strat, S) {
  let netDebit = 0;
  let payoff = 0;
  strat.legs.forEach(l => {
    netDebit += l.entry * l.qty * l.mult;
    const intrinsic = l.type === 'C' ? Math.max(0, S - l.strike) : Math.max(0, l.strike - S);
    payoff += intrinsic * l.qty * l.mult;
  });
  return payoff - netDebit;
}

// Switch between the table view and the spread-bar visualization view.
function toggleDashView() {
  // Any view switch counts as "navigating away" — clear freeze either way.
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  dashViewMode = dashViewMode === 'table' ? 'viz' : 'table';
  const tbl = document.getElementById('dashTable');
  const viz = document.getElementById('dashViz');
  const btn = document.getElementById('vizBtn');
  const freezeBtn = document.getElementById('vizFreezeBtn');
  const compactBtn = document.getElementById('vizCompactBtn');
  // Spreads is its own focused view: hide the main-interface nav buttons
  // (Overview / Heatmap / Risk) and turn the Spreads toggle into a back arrow.
  const navBtns = ['overviewBtn', 'heatmapViewBtn', 'pnlBarsBtn', 'riskBtn']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (dashViewMode === 'viz') {
    appNavInto();   // 进 Spreads 也压历史态 → 两指滑动 / 后退可退回表格
    tbl.style.display = 'none';
    viz.style.display = 'block';
    btn.innerHTML = '&#8592; 返回';   // back to main (table)
    navBtns.forEach(b => b.style.display = 'none');
    if (freezeBtn) freezeBtn.style.display = 'flex';
    if (compactBtn) compactBtn.style.display = 'flex';
    renderDashViz(RAW_DATA.strategies);
    // Kick off ATR fetch in background — re-render once results arrive so the
    // ATR-based days-to-BE row populates without blocking the initial paint.
    loadDashAtr().then(updated => { if (updated) renderDashViz(RAW_DATA.strategies); });
  } else {
    tbl.style.display = '';
    viz.style.display = 'none';
    btn.innerHTML = '&#128202; Spreads';
    navBtns.forEach(b => b.style.display = 'flex');
    if (freezeBtn) freezeBtn.style.display = 'none';
    if (compactBtn) compactBtn.style.display = 'none';
    renderDashboard(RAW_DATA.strategies);
  }
}

function toggleDashCompact() {
  dashVizCompact = !dashVizCompact;
  const btn = document.getElementById('vizCompactBtn');
  if (btn) {
    btn.classList.toggle('compact-on', dashVizCompact);
    btn.innerHTML = dashVizCompact ? '&#9783; Detailed' : '&#8801; Compact';
  }
  // Class on container — drives CSS density rules without re-rendering DOM.
  const viz = document.getElementById('dashViz');
  if (viz) viz.classList.toggle('viz-compact', dashVizCompact);
  // Re-render so per-row HTML reflects what we now omit / include.
  if (!dashVizFrozen && dashViewMode === 'viz' && RAW_DATA && RAW_DATA.strategies) {
    renderDashViz(RAW_DATA.strategies);
  }
}

// Update freeze state + button styling. `silent=true` skips the post-thaw
// re-render (used when we're navigating away anyway).
function _setDashVizFrozen(frozen, silent) {
  dashVizFrozen = !!frozen;
  const btn = document.getElementById('vizFreezeBtn');
  if (btn) {
    btn.classList.toggle('frozen', dashVizFrozen);
    btn.innerHTML = dashVizFrozen ? '&#128274; Frozen' : '&#10052; Freeze';
  }
  // Pull in the latest data the moment the user un-freezes (unless caller
  // says skip — e.g., tab switch where we'd waste the work).
  if (!frozen && !silent && dashViewMode === 'viz' && RAW_DATA && RAW_DATA.strategies) {
    renderDashViz(RAW_DATA.strategies);
  }
}

function toggleDashFreeze() {
  _setDashVizFrozen(!dashVizFrozen, false);
}

// ATR-14 cache, populated lazily when viz mode opens. ATR captures realized
// daily volatility from price history; pairs with IV's implied move so the
// user can compare. 1h server cache + permanent client cache (refresh page
// to refetch on next session).
let dashAtrCache = {};   // ticker -> atr_dollars (or null if fetch failed)
let _dashAtrInflight = null;
function loadDashAtr() {
  const strategies = (RAW_DATA && RAW_DATA.strategies) || [];
  const tickers = Array.from(new Set(strategies.map(s => s.underlying)))
    .filter(t => !(t in dashAtrCache));
  if (!tickers.length) return Promise.resolve(false);
  if (_dashAtrInflight) return _dashAtrInflight;
  _dashAtrInflight = fetch('/api/atr?symbols=' + encodeURIComponent(tickers.join(',')))
    .then(r => r.json())
    .then(d => {
      Object.assign(dashAtrCache, d.atr || {});
      _dashAtrInflight = null;
      return true;
    })
    .catch(() => { _dashAtrInflight = null; return false; });
  return _dashAtrInflight;
}

// Single dispatch — picks the active view + always updates summary.
function renderDashView() {
  updateDashSummary();
  if (dashViewMode === 'viz') renderDashViz(RAW_DATA.strategies);
  else renderDashboard(RAW_DATA.strategies);
}

// Render the spread-bar visualization. One row per strategy: name + bar with
// loss/profit zones, strike ticks (with Lower/Upper roles), BE markers,
// current-price triangle, and stats (in-profit?, % to BE, days at 1σ /
// days at ATR). Sorted: Below-BE positions first (need attention), then
// by days-to-BE descending so the hardest-to-recover positions surface.
function renderDashViz(strategies) {
  updateDashSummary();
  // While frozen the user is reading the bars — don't shift them under their
  // cursor. Summary above the bars still updates so they can see live BP/etc.
  if (dashVizFrozen) return;
  const container = document.getElementById('dashViz');
  if (!container) return;
  if (!strategies || !strategies.length) {
    container.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text2)">No option strategies found.</div>';
    return;
  }

  // Decorate each strategy with the metrics we need for both sort + render.
  // Doing it once avoids re-running analyzeStrategy / sampling per render path.
  const enriched = strategies.map((s, idx) => {
    const a = analyzeStrategy(s);
    const strikes = Array.from(new Set(s.legs.map(l => l.strike))).sort((x,y) => x - y);
    const S = s.current_price;
    const beArr = (a.breakevens || []).filter(b => b > 0);
    const inProfit = plAtExpiration(s, S) > 0;
    const nearestBE = beArr.length
      ? beArr.reduce((acc, b) => Math.abs(b - S) < Math.abs(acc - S) ? b : acc, beArr[0])
      : null;
    const ivDailyMove = S * (s.iv || 0) * Math.sqrt(1 / 252);
    const daysIv = (nearestBE != null && ivDailyMove > 0)
      ? Math.abs(nearestBE - S) / ivDailyMove : null;
    const atr = dashAtrCache[s.underlying];
    const daysAtr = (nearestBE != null && atr && atr > 0)
      ? Math.abs(nearestBE - S) / atr : null;
    return { idx, s, a, strikes, S, beArr, inProfit, nearestBE, daysIv, daysAtr };
  });

  // Sort by "concern":
  //   Below BE first  → days descending (hardest to recover at top)
  //   In Profit next  → days ascending  (smallest cushion at top of group,
  //                                       i.e. closest to flipping back to loss)
  // Both directions surface the most-at-risk position at the top of its group.
  enriched.sort((a, b) => {
    if (a.inProfit !== b.inProfit) return a.inProfit ? 1 : -1;
    const ad = a.daysIv == null ? Infinity : a.daysIv;
    const bd = b.daysIv == null ? Infinity : b.daysIv;
    return a.inProfit ? (ad - bd) : (bd - ad);
  });

  let html = '';
  enriched.forEach(({idx, s, strikes, S, beArr, inProfit, nearestBE, daysIv, daysAtr}) => {
    const expD = new Date(s.expiration + 'T12:00:00');
    const expLabel = expD.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});

    // Compute bar bounds: include current, all strikes, all BEs + 7% padding.
    const allPoints = [S, ...strikes, ...beArr].filter(p => p > 0);
    const minBound = Math.min(...allPoints);
    const maxBound = Math.max(...allPoints);
    const padding = Math.max(1, (maxBound - minBound) * 0.07);
    const barLow = Math.max(0.01, minBound - padding);
    const barHigh = maxBound + padding;
    const barRange = barHigh - barLow;
    const pct = (p) => Math.max(0, Math.min(100, ((p - barLow) / barRange) * 100));

    // Sample 120 points to find continuous profit / loss zones at expiration.
    const samples = 120;
    const zones = [];
    let zStart = barLow, zProfit = plAtExpiration(s, barLow) > 0;
    for (let i = 1; i <= samples; i++) {
      const p = barLow + (barRange * i / samples);
      const isProfit = plAtExpiration(s, p) > 0;
      if (isProfit !== zProfit) {
        zones.push({start: zStart, end: p, profit: zProfit});
        zStart = p; zProfit = isProfit;
      }
    }
    zones.push({start: zStart, end: barHigh, profit: zProfit});

    // Format the BE summary line + days estimates (IV-implied + ATR-realized).
    let beLabel = '–', daysIvLabel = '', daysAtrLabel = '';
    if (nearestBE != null) {
      const beDistPct = ((nearestBE - S) / S) * 100;
      const sign = beDistPct >= 0 ? '+' : '';
      beLabel = `<strong>${sign}${beDistPct.toFixed(1)}%</strong> to BE $${nearestBE.toFixed(2)}`;
      const verb = inProfit ? 'to lose' : 'to reach';
      const fmtDays = (d) => d < 0.1 ? '< 0.1' : d < 10 ? d.toFixed(1) : Math.round(d).toString();
      if (daysIv != null) daysIvLabel = `~${fmtDays(daysIv)}d @ 1σ ${verb} BE`;
      if (daysAtr != null) daysAtrLabel = `~${fmtDays(daysAtr)}d @ ATR-14`;
      else if (s.underlying in dashAtrCache) daysAtrLabel = 'ATR n/a';
      else daysAtrLabel = 'ATR loading…';
    }

    // Build the bar's children: zones first (markers paint on top).
    let zoneHtml = '';
    zones.forEach(z => {
      const left = pct(z.start);
      const w = Math.max(0, pct(z.end) - left);
      if (w <= 0) return;
      const cls = z.profit ? 'viz-bar-zone-profit' : 'viz-bar-zone-loss';
      zoneHtml += `<div class="viz-bar-zone ${cls}" style="left:${left}%;width:${w}%"></div>`;
    });
    // Strike ticks below bar — first/last get a "Lower"/"Upper" role tag.
    // (Both labels and the role tag are hidden by CSS in compact mode.)
    let strikeHtml = '';
    strikes.forEach((k, i) => {
      let role = '';
      if (i === 0 && strikes.length > 1) role = 'Lower';
      else if (i === strikes.length - 1 && strikes.length > 1) role = 'Upper';
      const roleHtml = role ? `<span class="viz-strike-role">${role}</span>` : '';
      strikeHtml += `<div class="viz-bar-strike" style="left:${pct(k)}%"></div>` +
                    `<div class="viz-bar-tick viz-bar-tick-bottom" style="left:${pct(k)}%">${roleHtml}<span>$${k}</span></div>`;
    });
    // BE marker(s) + label above the bar.
    let beHtml = '';
    beArr.forEach(b => {
      beHtml += `<div class="viz-bar-be" style="left:${pct(b)}%"></div>` +
                `<div class="viz-bar-tick viz-bar-tick-be" style="left:${pct(b)}%">Break Even $${b.toFixed(2)}</div>`;
    });
    const curHtml =
      `<div class="viz-bar-current" style="left:${pct(S)}%"></div>` +
      `<div class="viz-bar-tick viz-bar-tick-current" style="left:${pct(S)}%">$${S.toFixed(2)}</div>`;

    const strikeStr = strikes.join('/');
    const ivPct = ((s.iv || 0) * 100).toFixed(0);

    // Compact-mode stat: single chip — "✓ In Profit -2.7%" or "✗ Below BE +4.3%".
    // Icons (✓/✗) carry the meaning so red/green isn't the only distinguishing
    // signal — important for users with color-vision deficiency.
    const beDistPct = (nearestBE != null && S > 0) ? ((nearestBE - S) / S) * 100 : null;
    const compactPct = beDistPct == null ? '' :
      ` ${beDistPct >= 0 ? '+' : ''}${beDistPct.toFixed(1)}%`;
    const statusIcon = inProfit ? '&#10003; ' : '&#10007; ';   // ✓ / ✗
    const compactStat = `<div class="stat-status ${inProfit ? 'pos' : 'neg'}">${statusIcon}${inProfit ? 'In Profit' : 'Below BE'}${compactPct}</div>`;

    if (dashVizCompact) {
      html += `
        <div class="viz-row" onclick="enterDetail(${idx})">
          <div class="viz-name">${s.underlying} ${strikeStr}</div>
          <div class="viz-bar-wrap">
            <div class="viz-bar">${zoneHtml}${strikeHtml}${beHtml}${curHtml}</div>
          </div>
          <div class="viz-stats">${compactStat}</div>
        </div>`;
    } else {
      html += `
        <div class="viz-row" onclick="enterDetail(${idx})">
          <div>
            <div class="viz-name">${s.underlying} ${strikeStr} ${s.name}</div>
            <div class="viz-name-sub">Exp ${expLabel} · ${Math.round(s.dte)}d DTE · IV ${ivPct}%</div>
          </div>
          <div class="viz-bar-wrap">
            <div class="viz-bar">${zoneHtml}${strikeHtml}${beHtml}${curHtml}</div>
          </div>
          <div class="viz-stats">
            <div class="stat-status ${inProfit ? 'pos' : 'neg'}">${statusIcon}${inProfit ? 'In Profit' : 'Below BE'}</div>
            <div class="stat-be">${beLabel}</div>
            <div class="stat-days">${daysIvLabel}</div>
            <div class="stat-days">${daysAtrLabel}</div>
          </div>
        </div>`;
    }
  });
  container.innerHTML = html;
}

function buildDashRows(strategies) {
  // Build data rows with sort values
  return strategies.map((s, i) => {
    const a = analyzeStrategy(s);
    const strikes = s.legs.map(l => l.strike).sort((a,b) => a-b).join('/');
    const expD = new Date(s.expiration + 'T12:00:00');
    const expLabel = expD.toLocaleDateString('en-US', {month:'short', day:'numeric'});
    const ordinal = expD.getDate() <= 3 ? ['st','nd','rd'][expD.getDate()-1]||'th' : 'th';

    // Breakeven % from current price
    const beArr = a.breakevens || [];
    let bePct = null;
    if (beArr.length > 0 && s.current_price > 0) {
      // Pick the closest breakeven
      let closest = beArr[0];
      beArr.forEach(b => { if (Math.abs(b - s.current_price) < Math.abs(closest - s.current_price)) closest = b; });
      bePct = ((closest - s.current_price) / s.current_price) * 100;
    }

    // Earnings before expiration?
    let hasEarnings = false;
    if (s.earnings_date) {
      const eD = new Date(s.earnings_date + 'T16:00:00');
      if (eD <= expD && eD >= new Date()) hasEarnings = true;
    }

    const _ev = ckExitVerdict(a.unrealizedPnl, a.maxProfit, s.dte, a.unrealizedPct);
    const _pc = (typeof RAW_DATA !== 'undefined' && RAW_DATA.equity_prev_close && RAW_DATA.equity_prev_close[s.underlying]) || 0;
    const _underPct = _pc > 0 ? ((s.current_price - _pc) / _pc) * 100 : null;
    return {
      idx: i,
      exitBadge: _ev,
      name: `${s.underlying} ${expLabel}${ordinal} ${strikes} ${s.name}`,
      underlying: s.underlying,
      underPrice: s.current_price,
      underPct: _underPct,
      pnl: a.unrealizedPnl,
      pct: a.unrealizedPct,
      today: a.todayPnl,
      todayPct: a.netDebit !== 0 ? (a.todayPnl / Math.abs(a.netDebit)) * 100 : 0,
      created: s.created_at ? new Date(s.created_at).getTime() : 0,
      createdStr: s.created_at ? new Date(s.created_at).toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}) + ', ' + new Date(s.created_at).toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'}) : '',
      dte: s.dte,
      expiration: s.expiration,
      bePct: bePct,
      maxProfit: a.maxProfit,
      maxLoss: a.maxLoss,
      cop: a.cop,
      hasEarnings: hasEarnings,
      earningsDate: s.earnings_date || '',
      earningsTime: s.earnings_time || '',
    };
  });
}

// Shared dashboard comparator — used by both the main table and the detail
// sidebar so the left list matches the table's current sort.
function _dashCmp(a, b, col, mul) {
  if (col === 'name') return mul * a.name.localeCompare(b.name);
  if (col === 'under') return mul * ((a.underPct==null ? -9999 : a.underPct) - (b.underPct==null ? -9999 : b.underPct));
  if (col === 'total') return mul * (dashTotalMode === 'pct' ? ((a.pct||0) - (b.pct||0)) : (a.pnl - b.pnl));
  if (col === 'today') return mul * ((a.today||0) - (b.today||0));
  if (col === 'created') return mul * (a.created - b.created);
  if (col === 'dte') return mul * (a.dte - b.dte);
  if (col === 'be') return mul * ((a.bePct||0) - (b.bePct||0));
  if (col === 'maxprofit') return mul * ((a.maxProfit||0) - (b.maxProfit||0));
  if (col === 'maxloss') return mul * ((a.maxLoss||0) - (b.maxLoss||0));
  if (col === 'cop') return mul * ((a.cop||0) - (b.cop||0));
  if (col === 'progress') {
    const pa = _dashProg(a), pb = _dashProg(b);
    return mul * ((pa.avail ? pa.pct : -999) - (pb.avail ? pb.pct : -999));
  }
  return 0;
}

// Progress 占比(作者口径):盈利按 max profit 算占比、亏损按 max loss 算占比。
// 返回 {pct, avail};max profit/loss 为 ∞(≥1e9)时该侧无法算占比 → avail=false。
function _dashProg(r) {
  if (r.pnl >= 0) {
    const mp = r.maxProfit;
    const avail = (mp > 0 && mp < 1e9);
    return { pct: avail ? (r.pnl / mp * 100) : 0, avail };
  }
  const ml = Math.abs(r.maxLoss);
  const avail = (ml > 0 && ml < 1e9);
  return { pct: avail ? (r.pnl / ml * 100) : 0, avail };   // r.pnl<0 → 负
}

function renderDashboard(strategies) {
  updateDashSummary();
  const rows = buildDashRows(strategies);

  // Sort
  const {col, dir} = dashSort;
  const mul = dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => _dashCmp(a, b, col, mul));

  // Update header arrows
  const arw = dir === 'asc' ? ' &#9650;' : ' &#9660;';
  document.querySelectorAll('#dashTable th').forEach(th => {
    const c = th.dataset.sort;
    const tog = (c === 'total')
      ? ` <span onclick="event.stopPropagation();toggleTotalMode()" data-tip="切换 $/% 主显示(并按主显示排序)" style="cursor:pointer;font-size:9px;padding:1px 4px;border:1px solid var(--border);border-radius:3px;color:var(--text2);margin-left:5px">${dashTotalMode === 'pct' ? '%▸$' : '$▸%'}</span>`
      : '';
    th.innerHTML = th.dataset.label + (c === col ? arw : '') + tog;
  });

  const tbody = document.getElementById('dashBody');
  const _view = (typeof dashExitFilter !== 'undefined' && dashExitFilter)
    ? rows.filter(r => r.exitBadge && r.exitBadge.code === dashExitFilter) : rows;
  let html = '';
  if (!_view.length) html = '<tr><td colspan="10" style="padding:30px;text-align:center;color:var(--text2)">没有符合该筛选的仓位(按 0 看全部)</td></tr>';
  _view.forEach(r => {
    const cls = r.pnl >= 0 ? 'pos' : 'neg';
    const todayCls = r.today >= 0 ? 'pos' : 'neg';
    const dteStr = r.dte < 1 ? r.dte.toFixed(1)+'d' : Math.round(r.dte)+'d';
    const earTicker = r.name.split(' ')[0];
    const etLabel = r.earningsTime === 'BMO' ? ' BM' : r.earningsTime === 'AMC' ? ' AM' : '';
    const earningsIcon = r.hasEarnings ? ` <span title="Earnings ${r.earningsDate}${etLabel} — Click for history" style="color:var(--yellow);font-size:11px;cursor:pointer" onclick="event.stopPropagation();showEarningsHistory('${earTicker}','${r.earningsDate}')">&#128227;${etLabel}</span>` : '';
    const beStr = r.bePct != null ? `${r.bePct >= 0 ? '+' : ''}${r.bePct.toFixed(1)}%` : '-';
    const beCls = r.bePct != null ? (Math.abs(r.bePct) < 2 ? 'color:var(--yellow)' : '') : '';
    const mpStr = r.maxProfit >= 1e9 ? '∞' : `$${Math.abs(r.maxProfit).toFixed(0)}`;
    const mlStr = r.maxLoss <= -1e9 ? '∞' : `$${Math.abs(r.maxLoss).toFixed(0)}`;
    // Show each as % of current account NLV. Color the loss % aggressively
    // because that's the relevant risk metric (≥5% red, ≥2% amber, else grey).
    const NLV = liveNLV();
    const fmtPctNlv = (v) => (NLV > 0 && Math.abs(v) < 1e9)
      ? (Math.abs(v) / NLV * 100).toFixed(2) + '%'
      : '';
    const mpPctStr = fmtPctNlv(r.maxProfit);
    const mlPct = NLV > 0 && Math.abs(r.maxLoss) < 1e9 ? Math.abs(r.maxLoss) / NLV * 100 : 0;
    const mlPctColor = mlPct >= 5 ? 'var(--red)'
                      : mlPct >= 2 ? 'var(--yellow)' : 'var(--text2)';
    const mlPctStr = fmtPctNlv(r.maxLoss);
    const copStr = r.cop > 0 ? `${r.cop.toFixed(0)}%` : '-';
    const copCls = r.cop >= 50 ? 'color:var(--green)' : r.cop > 0 ? 'color:var(--red)' : '';
    // Progress:盈利=占 max profit%、亏损=占 max loss%(clamp 到 [-100%,100%])
    const _pg = _dashProg(r);
    const progPct = _pg.pct;
    const progClamped = Math.max(-100, Math.min(100, progPct));
    const progFill = Math.min(100, Math.abs(progClamped));
    const progColor = progPct >= 0 ? 'var(--green)' : 'var(--red)';
    const progLabel = _pg.avail ? `${progPct >= 0 ? '+' : ''}${progPct.toFixed(0)}%` : '-';
    const progHtml = `<div class="dash-progress"><div class="dash-progress-bar"><div class="dash-progress-fill" style="width:${progFill}%;background:${progColor}"></div></div><div class="dash-progress-label">${progLabel}</div></div>`;
    const underCls = r.underPct == null ? '' : (r.underPct >= 0 ? 'color:var(--green)' : 'color:var(--red)');
    const underPctStr = r.underPct == null ? '' : ` <span style="font-size:10px;font-weight:600;${underCls}">${r.underPct >= 0 ? '+' : ''}${r.underPct.toFixed(2)}%</span>`;
    const underPriceStr = r.underPrice > 0 ? `<span style="font-weight:700">$${r.underPrice.toFixed(2)}</span>` : '<span style="color:var(--text3)">-</span>';
    html += `<tr onclick="enterDetail(${r.idx})">
      <td><div class="dash-name">${r.exitBadge?('<span data-tip="'+(r.exitBadge.label+': '+r.exitBadge.reason).replace(/"/g,'')+'" style="margin-right:5px;cursor:help">'+r.exitBadge.emoji+'</span>'):''}${r.name}${earningsIcon}</div></td>
      <td><div style="font-size:11.5px"><span class="dash-meta" style="font-weight:600">${r.underlying||''}</span> ${underPriceStr}${underPctStr}</div></td>
      <td><div class="dash-return ${cls}">${dashTotalMode==='pct' ? `${r.pct>=0?'+':'-'}${Math.abs(r.pct).toFixed(0)}% <span style="opacity:.55;font-weight:600">(${r.pnl>=0?'+':'-'}$${Math.abs(r.pnl).toFixed(0)})</span>` : `${r.pnl>=0?'+':'-'}$${Math.abs(r.pnl).toFixed(0)} <span style="opacity:.55;font-weight:600">(${r.pct>=0?'+':'-'}${Math.abs(r.pct).toFixed(0)}%)</span>`}</div></td>
      <td><div class="dash-return ${todayCls}">${r.today>=0?'+':'-'}$${Math.abs(r.today).toFixed(0)} (${r.todayPct>=0?'+':'-'}${Math.abs(r.todayPct).toFixed(0)}%)</div></td>
      <td><div class="dash-meta" style="${beCls}">${beStr}</div></td>
      <td><div class="dash-meta" style="color:var(--green)">${mpStr}</div>${mpPctStr ? `<div style="font-size:9px;color:var(--text3)">${mpPctStr} of NLV</div>` : ''}</td>
      <td><div class="dash-meta" style="color:var(--red)">-${mlStr}</div>${mlPctStr ? `<div style="font-size:9px;color:${mlPctColor};font-weight:600">${mlPctStr} of NLV</div>` : ''}</td>
      <td><div class="dash-meta" style="${copCls}">${copStr}</div></td>
      <td><div class="dash-dte">${dteStr}</div></td>
      <td>${progHtml}</td>
    </tr>`;
  });
  tbody.innerHTML = html;
}

function sortDash(col) {
  if (dashSort.col === col) dashSort.dir = dashSort.dir === 'asc' ? 'desc' : 'asc';
  else { dashSort.col = col; dashSort.dir = col === 'name' ? 'asc' : 'desc'; }
  try { localStorage.setItem('dashSort', JSON.stringify(dashSort)); } catch (e) {}
  renderDashboard(RAW_DATA.strategies);
}

// Which positions sub-view is showing right now? Used so "back" from a single
// ticker returns to wherever you drilled in from (table/spreads vs heatmap vs
// overview), not always the table.
function _currentPosView() {
  const hv = document.getElementById('heatmapView');
  const ov = document.getElementById('overviewView');
  const pb = document.getElementById('pnlBarsView');
  if (pb && pb.style.display === 'flex') return 'pnlbars';
  if (hv && hv.style.display === 'flex') return 'heatmap';
  if (ov && ov.style.display === '') return 'overview';
  return 'dash';   // dashboard (Table or Spreads, restored by renderDashView)
}
let _posDrillSrc = 'dash';

function enterDetail(idx) {
  appNavInto();
  // Remember where we came from BEFORE hiding anything, so exitDetail returns
  // to that sub-view (heatmap→heatmap, overview→overview, table/spreads→dash).
  _posDrillSrc = _currentPosView();
  // Navigation away from the dashboard — clear freeze so re-entry is fresh.
  if (dashVizFrozen) _setDashVizFrozen(false, true);
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('heatmapView').style.display = 'none';
  document.getElementById('overviewView').style.display = 'none';
  { const _pb = document.getElementById('pnlBarsView'); if (_pb) _pb.style.display = 'none'; }
  document.getElementById('detailView').style.display = 'flex';
  // News drawer disabled — element no longer in DOM. Null-guard every
  // toggle so removal doesn't cascade into JS errors that kill the
  // strategy-detail render (POP=0%, Graph button dead, Back → empty list).
  { const _nd = document.getElementById('tickerNewsDrawer'); if (_nd) _nd.style.display = 'block'; }
  const strategies = RAW_DATA.strategies;
  state.ivPct = Math.round(strategies[idx].iv * 100);
  renderSidebar(strategies);
  setupTabs();
  setupControls();
  selectStrategy(idx);
}

function exitDetail() {
  document.getElementById('detailView').style.display = 'none';
  { const _nd = document.getElementById('tickerNewsDrawer'); if (_nd) _nd.style.display = 'none'; }
  // Return to whichever sub-view we drilled in from.
  if (_posDrillSrc === 'pnlbars') {
    document.getElementById('pnlBarsView').style.display = 'flex';
    renderPnlBarsView();
  } else if (_posDrillSrc === 'heatmap') {
    document.getElementById('heatmapView').style.display = 'flex';
    renderHeatmapView();
  } else if (_posDrillSrc === 'overview') {
    document.getElementById('overviewView').style.display = '';
    renderOverviewView();
  } else {
    document.getElementById('dashboardView').style.display = '';
    renderDashView();
  }
}

let _newsOpen = false;
function toggleNewsDrawer() {
  // News drawer removed; keep function as no-op so any stale onclick stubs
  // don't ReferenceError.
  _newsOpen = !_newsOpen;
  const panel = document.getElementById('newsPanel');
  const arrow = document.getElementById('newsDrawerToggle');
  if (panel) panel.style.display = _newsOpen ? 'block' : 'none';
  if (arrow) arrow.innerHTML = _newsOpen ? '&#9660;' : '&#9650;';
}

function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  const btn = document.getElementById('sidebarToggle');
  sb.classList.toggle('collapsed');
  const hidden = sb.classList.contains('collapsed');
  btn.innerHTML = hidden ? '&#9654;' : '&#9664;';
  btn.style.left = hidden ? '0' : '150px';
}

function refreshPositions() {
  const btn = document.getElementById('refreshBtn');
  btn.textContent = '⟳ Loading...';
  btn.disabled = true;
  fetch('/api/refresh', {method:'POST'})
    .then(r => r.json())
    .then(() => fetch('/api/data'))
    .then(r => r.json())
    .then(d => {
      RAW_DATA.strategies = d.strategies;
      RAW_DATA.equities = d.equities;
      RAW_DATA.equity_prev_close = d.equity_prev_close;
      if (d.balance) RAW_DATA.balance = d.balance;
      renderDashView();
      try { checkAlerts(RAW_DATA.strategies || []); } catch(e) {}
      btn.textContent = '↻ Refresh';
      btn.disabled = false;
    })
    .catch(e => {
      btn.textContent = '↻ Refresh';
      btn.disabled = false;
      alert('Refresh failed: ' + e.message);
    });
}

function openModal(stratIdx) {
  const strat = RAW_DATA.strategies[stratIdx];
  const a = analyzeStrategy(strat);
  const pnl = a.unrealizedPnl;
  const pct = a.unrealizedPct;
  const cls = pnl >= 0 ? '' : ' neg';

  let html = `<div class="modal-total${cls}">Total unrealized: ${pnl >= 0 ? '+' : '-'}$${Math.abs(pnl).toFixed(2)} (${pct >= 0 ? '+' : '-'}${Math.abs(pct).toFixed(0)}%)</div>`;

  strat.legs.forEach(l => {
    const isBuy = l.qty > 0;
    const tag = isBuy ? 'BTO' : 'STO';
    const tagCls = isBuy ? 'bto' : 'sto';
    const absQty = Math.abs(l.qty);
    const typeName = l.type === 'C' ? 'Call' : 'Put';
    const legPnl = (l.current - l.entry) * l.qty * l.mult;
    const legCls = legPnl >= 0 ? 'color:var(--green)' : 'color:var(--red)';
    const expShort = l.exp.substring(5).replace('-', '/');
    html += `<div class="modal-leg">
      <span class="modal-leg-tag ${tagCls}">${tag}</span>
      <span class="modal-leg-detail">${absQty}&times; ${strat.underlying} ${l.strike}${l.type} ${expShort} at $${l.entry.toFixed(2)}</span>
      <span class="modal-leg-pnl" style="${legCls}">${legPnl >= 0 ? '+' : ''}$${legPnl.toFixed(2)}</span>
    </div>`;
  });

  // Cost breakdown
  const totalContracts = strat.legs.reduce((sum, l) => sum + Math.abs(l.qty), 0);
  let commission = 0;
  strat.legs.forEach(l => { commission += Math.min(Math.abs(l.qty) * 1.00, 10); });
  html += `<div class="modal-section"><h3>Cost Breakdown</h3>`;
  html += `<div class="modal-row"><span class="label">Net Debit/Credit</span><span>$${Math.abs(a.netDebit).toFixed(2)}</span></div>`;
  html += `<div class="modal-row"><span class="label">Commissions (est.)</span><span style="color:var(--red)">$${commission.toFixed(2)}</span></div>`;
  html += `<div class="modal-row"><span class="label">Per trade</span><span>$0.00</span></div>`;
  html += `<div class="modal-row"><span class="label">Per contract (open)</span><span>$1.00 &times; ${totalContracts} (cap $10/leg)</span></div>`;
  html += `</div>`;

  // Pricing mode
  html += `<div class="modal-section"><h3>Pricing Mode</h3>`;
  html += `<div class="modal-toggle"><button class="${state.pricingMode !== 'bidask' ? 'active' : ''}" onclick="setPricingMode('mid')">Midpoint</button><button class="${state.pricingMode === 'bidask' ? 'active' : ''}" onclick="setPricingMode('bidask')">Bid / Ask</button></div>`;
  html += `</div>`;

  // Per-leg details
  html += `<div class="modal-section"><h3>Leg Details</h3>`;
  strat.legs.forEach(l => {
    const bid = l.bid || 0;
    const ask = l.ask || 0;
    const mid = l.mid || l.current;
    html += `<div class="modal-row"><span class="label">${l.strike}${l.type}</span><span>Bid $${bid.toFixed(2)} / Ask $${ask.toFixed(2)} / Mid $${mid.toFixed(2)}</span></div>`;
  });
  html += `</div>`;

  document.getElementById('modalContent').innerHTML = html;
  document.getElementById('positionModal').classList.add('active');
}

function closeModal() {
  document.getElementById('positionModal').classList.remove('active');
}

// ── Earnings History Modal ──
async function showEarningsHistory(ticker, nextDate) {
  const modal = document.getElementById('earningsModal');
  const content = document.getElementById('earningsModalContent');
  modal.classList.add('active');
  content.innerHTML = `<h2 style="margin:0 0 12px">&#128227; ${ticker} Earnings History</h2><div style="color:var(--text2);padding:20px;text-align:center">Loading 5 years of earnings data...</div>`;

  try {
    const r = await fetch('/api/earnings-history/' + ticker);
    const d = await r.json();
    if (d.error) { content.innerHTML = `<h2>&#128227; ${ticker}</h2><div style="color:var(--red);padding:20px">${d.error}</div>`; return; }
    if (!d.rows || !d.rows.length) { content.innerHTML = `<h2>&#128227; ${ticker}</h2><div style="color:var(--text2);padding:20px">No earnings history found.</div>`; return; }

    let html = `<h2 style="margin:0 0 4px">&#128227; ${ticker} Earnings History</h2>`;
    html += `<div style="display:flex;gap:16px;margin-bottom:12px;font-size:12px;color:var(--text2)">`;
    html += `<span>Next: <strong style="color:var(--yellow)">${nextDate || 'TBD'}</strong></span>`;
    html += `<span>Avg Day: <strong style="color:${d.avg_day_pct >= 0 ? 'var(--green)' : 'var(--red)'}">${d.avg_day_pct >= 0 ? '+' : ''}${d.avg_day_pct}%</strong></span>`;
    html += `<span>Avg Next: <strong style="color:${d.avg_next_pct >= 0 ? 'var(--green)' : 'var(--red)'}">${d.avg_next_pct >= 0 ? '+' : ''}${d.avg_next_pct}%</strong></span>`;
    html += `<span>Next-Day Down: <strong style="color:${d.down_prob >= 50 ? 'var(--red)' : 'var(--green)'}">${d.down_prob}%</strong> (${d.down_count}/${d.total_count})</span>`;
    html += `</div>`;

    html += `<table style="width:100%;border-collapse:collapse;font-size:12px">`;
    html += `<thead><tr style="border-bottom:1px solid var(--border)">`;
    html += `<th style="text-align:left;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">Date</th>`;
    html += `<th style="text-align:right;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">Day %</th>`;
    html += `<th style="text-align:right;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">Next %</th>`;
    html += `<th style="text-align:right;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">EPS</th>`;
    html += `<th style="text-align:right;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">Est</th>`;
    html += `<th style="text-align:right;padding:4px 8px;color:var(--text2);font-size:10px;text-transform:uppercase">Surprise</th>`;
    html += `</tr></thead><tbody>`;

    d.rows.forEach(row => {
      const dayClr = row.day_pct >= 0 ? 'var(--green)' : 'var(--red)';
      const nxtClr = row.next_pct == null ? 'var(--text2)' : row.next_pct >= 0 ? 'var(--green)' : 'var(--red)';
      const surpClr = row.surprise == null ? 'var(--text2)' : row.surprise >= 0 ? 'var(--green)' : 'var(--red)';
      html += `<tr style="border-bottom:1px solid rgba(48,54,61,0.3)">`;
      html += `<td style="padding:3px 8px;color:var(--text1)">${row.date}</td>`;
      html += `<td style="padding:3px 8px;text-align:right;color:${dayClr};font-weight:600">${row.day_pct >= 0 ? '+' : ''}${row.day_pct.toFixed(2)}%</td>`;
      html += `<td style="padding:3px 8px;text-align:right;color:${nxtClr};font-weight:600">${row.next_pct != null ? (row.next_pct >= 0 ? '+' : '') + row.next_pct.toFixed(2) + '%' : 'n/a'}</td>`;
      html += `<td style="padding:3px 8px;text-align:right;color:var(--text1)">${row.eps_act != null ? row.eps_act.toFixed(2) : '-'}</td>`;
      html += `<td style="padding:3px 8px;text-align:right;color:var(--text2)">${row.eps_est != null ? row.eps_est.toFixed(2) : '-'}</td>`;
      html += `<td style="padding:3px 8px;text-align:right;color:${surpClr};font-weight:600">${row.surprise != null ? (row.surprise >= 0 ? '+' : '') + row.surprise.toFixed(1) + '%' : '-'}</td>`;
      html += `</tr>`;
    });
    html += `</tbody></table>`;
    content.innerHTML = html;
  } catch(e) {
    content.innerHTML = `<h2>&#128227; ${ticker}</h2><div style="color:var(--red);padding:20px">Error: ${e.message}</div>`;
  }
}

function closeEarningsModal() {
  document.getElementById('earningsModal').classList.remove('active');
}

function setPricingMode(mode) {
  state.pricingMode = mode;
  openModal(state.selectedIdx);
  renderStrategy();
}

function renderSidebar(strategies) {
  const sb = document.getElementById('sidebar');
  sb.innerHTML = '<div class="sidebar-title" style="display:flex;align-items:center;gap:8px"><button class="back-btn" style="display:flex" onclick="exitDetail()">&#8592;</button> Strategies</div>';
  // Order the sidebar the SAME way the dashboard table is sorted, so the left
  // list matches what you clicked from (sort by COP/maxloss → sidebar follows).
  // Keep original indices for selection/active state.
  let order = strategies.map((s, i) => i);
  try {
    const rows = buildDashRows(strategies);
    const {col, dir} = dashSort;
    const mul = dir === 'asc' ? 1 : -1;
    rows.sort((a, b) => _dashCmp(a, b, col, mul));
    order = rows.map(r => r.idx);
  } catch (_) {}
  order.forEach(i => {
    const s = strategies[i];
    const analysis = analyzeStrategy(s);
    const pnl = analysis.unrealizedPnl;
    const div = document.createElement('div');
    div.className = 'strat-item' + (i === state.selectedIdx ? ' active' : '');
    div.dataset.idx = i;
    const credWarn = isCreditVertical(s)
      ? ' <span title="Credit spread — 方向可能开反，请检查" style="color:#ff9f0a;font-size:12px">⚠️</span>'
      : '';
    // ⚠ 未挂止盈 GTC — MISSING_GTC.indexSet 由 loadMissingGtc() 填充
    const _mgSet = (window.MISSING_GTC && window.MISSING_GTC.indexSet) || null;
    const noGtc = _mgSet && _mgSet.has(i)
      ? ' <span class="strat-nogtc" title="TastyTrade 上没有这个 (ticker, exp) 的 working closing GTC 单 —— 别忘了挂 90% 止盈" style="color:#f85149;font-size:11px;font-weight:700">⚠未挂止盈</span>'
      : '';
    div.innerHTML = `
      <div class="strat-ticker">${s.underlying}${credWarn}${noGtc}</div>
      <div class="strat-name">${s.name}</div>
      <div class="strat-pnl ${pnl >= 0 ? 'pos' : 'neg'}">${pnl >= 0 ? '+' : '-'}$${Math.abs(pnl).toFixed(0)} (${analysis.unrealizedPct >= 0 ? '+' : '-'}${Math.abs(analysis.unrealizedPct).toFixed(1)}%)</div>
    `;
    div.onclick = () => selectStrategy(i);
    sb.appendChild(div);
  });
}

// ── 未挂止盈 GTC 检测(server-side missing_gtc_loop 60s 轮询,前端读快照)──
// 显示:①sidebar 每个缺 GTC 的仓位挂 ⚠ 红徽章  ②体检 modal 顶部 banner 列出所有缺单
// 数据源: GET /api/missing_gtc → {items:[{ticker,exp,strategy_indices}], at}
window.MISSING_GTC = {items: [], expiring_today: [], indexSet: new Set(), expIndexSet: new Set(), at: null};
async function bzReloginFlow(ev) {
  if (ev) ev.stopPropagation();
  try {
    const r = await fetch('/api/benzinga/relogin', {method: 'POST'});
    const d = await r.json();
    if (!d.started) {
      alert('启动失败:' + (d.error || '未知') + '\\n手动跑:node premarket/login.mjs');
      return;
    }
    alert('已在 Terminal 打开登录窗口 — 登录完成后回来点 Refresh 抓今天。');
  } catch (e) {
    alert('触发失败:' + e.message);
  }
}

async function loadMissingGtc() {
  try {
    const r = await fetch('/api/missing_gtc');
    if (!r.ok) return;
    const d = await r.json();
    const items = d.items || [];
    const expiring = d.expiring_today || [];
    const idxSet = new Set();
    items.forEach(g => (g.strategy_indices || []).forEach(i => idxSet.add(i)));
    const expIdxSet = new Set();
    expiring.forEach(g => (g.strategy_indices || []).forEach(i => expIdxSet.add(i)));
    window.MISSING_GTC = {items, expiring_today: expiring, indexSet: idxSet, expIndexSet: expIdxSet, at: d.at};
    _mgUpdateSidebarBadges();
    _mgUpdateBanner();
    _mgUpdateTopBar();
  } catch (_) { /* silent — 后端 loop 会重试 */ }
}

// 只更新徽章(不重画整个 sidebar,避免打断 P/L 闪烁的 updateSidebarPnl)。
function _mgUpdateSidebarBadges() {
  const idxSet = window.MISSING_GTC.indexSet;
  document.querySelectorAll('.strat-item').forEach(item => {
    const idx = parseInt(item.dataset.idx, 10);
    const tickerEl = item.querySelector('.strat-ticker');
    if (!tickerEl) return;
    const existing = tickerEl.querySelector('.strat-nogtc');
    if (idxSet.has(idx)) {
      if (!existing) {
        const s = document.createElement('span');
        s.className = 'strat-nogtc';
        s.title = 'TastyTrade 上没有这个 (ticker, exp) 的 working closing GTC 单 —— 别忘了挂 90% 止盈';
        s.setAttribute('style', 'color:#f85149;font-size:11px;font-weight:700;margin-left:4px');
        s.textContent = '⚠未挂止盈';
        tickerEl.appendChild(s);
      }
    } else if (existing) {
      existing.remove();
    }
  });
}

function _mgUpdateBanner() {
  const el = document.getElementById('missingGtcBanner');
  if (!el) return;
  const items = window.MISSING_GTC.items || [];
  const expiring = window.MISSING_GTC.expiring_today || [];
  if (!items.length && !expiring.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const chip = (g, palette) => {
    const idx = (g.strategy_indices || [])[0];
    const s = (RAW_DATA && RAW_DATA.strategies && RAW_DATA.strategies[idx]) || {};
    const name = s.name || '';
    return `<span style="display:inline-block;background:${palette.bg};border:1px solid ${palette.br};border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0;color:${palette.fg}"><b>${g.ticker}</b> <span style="color:#8b949e">exp ${g.exp}</span>${name ? ` <span style="color:#6e7681;font-size:11px">(${name})</span>` : ''}</span>`;
  };
  let h = '';
  if (items.length) {
    h += `<div style="font-weight:700;margin-bottom:4px;color:#f85149">⚠ ${items.length} 个 spread 没有对应的 closing GTC —— 顺手把 90% 止盈挂上:</div>`;
    h += items.map(g => chip(g, {bg:'#1f1409', br:'#58421e', fg:'#f0b429'})).join('');
  }
  if (expiring.length) {
    if (items.length) h += `<div style="height:8px"></div>`;
    h += `<div style="font-weight:700;margin-bottom:4px;color:#ff9f0a">⏰ ${expiring.length} 个 spread 今天(ET)到期:</div>`;
    h += expiring.map(g => chip(g, {bg:'#1c1409', br:'#5a3a10', fg:'#ffa657'})).join('');
  }
  el.innerHTML = h;
  el.style.display = 'block';
}

// 顶部全局告警条 —— 无论切到哪个 tab 都能看见, 点击=开体检 modal 查详情
window.MACRO_ALERTS = [];
function _mgUpdateTopBar() {
  const bar = document.getElementById('posAlertBar');
  if (!bar) return;
  const items = (window.MISSING_GTC && window.MISSING_GTC.items) || [];
  const expiring = (window.MISSING_GTC && window.MISSING_GTC.expiring_today) || [];
  const total = items.length + expiring.length;
  if (!total) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  const short = (g) => `<span style="display:inline-block;padding:1px 7px;margin:0 4px 0 0;background:rgba(0,0,0,.28);border-radius:4px"><b>${g.ticker}</b> <span style="color:#f0b0a8">${g.exp.slice(5)}</span></span>`;
  const parts = [];
  if (items.length) parts.push(`<span style="color:#ffb4b4"><b style="color:#f85149">⚠ ${items.length}</b> 未挂止盈</span> ${items.slice(0,6).map(short).join('')}${items.length>6?`<span style="color:#8b949e">+${items.length-6}</span>`:''}`);
  if (expiring.length) parts.push(`<span style="color:#ffdda8"><b style="color:#ff9f0a">⏰ ${expiring.length}</b> 今日到期</span> ${expiring.slice(0,6).map(short).join('')}${expiring.length>6?`<span style="color:#8b949e">+${expiring.length-6}</span>`:''}`);
  bar.innerHTML = parts.join(' · ') + ` <span style="float:right;color:#f85149;font-size:11px">点开体检 &#8250;</span>`;
  bar.style.display = 'block';
}

function _updateMacroBanner() {
  const el = document.getElementById('macroEventBanner');
  if (!el) return;
  const macros = window.MACRO_ALERTS || [];
  if (!macros.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const byEvent = {};
  macros.forEach(m => {
    if (!byEvent[m.event]) byEvent[m.event] = [];
    byEvent[m.event].push(m);
  });
  const chip = (m) => `<span style="display:inline-block;padding:1px 7px;margin:0 3px 2px 0;background:rgba(88,166,255,.12);border:1px solid rgba(88,166,255,.25);border-radius:4px"><b>${m.ticker}</b> <span style="color:#8b949e">${m.exp.slice(5)}</span></span>`;
  let h = '';
  for (const [ev, items] of Object.entries(byEvent)) {
    h += `<div style="margin-bottom:4px"><span style="color:#58a6ff;font-weight:700">📅 ${ev}</span> <span style="color:#8b949e">→</span> ${items.map(chip).join('')}</div>`;
  }
  h += `<div style="font-size:10px;color:#6e7681;margin-top:2px">点击查看完整日历 →</div>`;
  el.innerHTML = h;
  el.style.display = 'block';
}

async function loadMacroAlerts() {
  try {
    if (!RAW_DATA || !RAW_DATA.strategies || !RAW_DATA.strategies.length) return;
    const r = await fetch('/api/major-events?days=90');
    if (!r.ok) return;
    const d = await r.json();
    const events = (d.events || []).filter(e => e.tier === 1 && ['FOMC','CPI','NFP','PCE','GDP'].includes(e.type));
    const todayStr = d.today || new Date().toISOString().slice(0, 10);
    const alerts = [];
    const seen = new Set();
    for (const s of RAW_DATA.strategies) {
      if (!s.expiration || s.expiration <= todayStr) continue;
      const matching = events.filter(e => e.date > todayStr && e.date <= s.expiration);
      if (!matching.length) continue;
      const key = s.underlying + '|' + s.expiration;
      if (seen.has(key)) continue;
      seen.add(key);
      const nearest = matching[0];
      alerts.push({ticker: s.underlying, exp: s.expiration, event: nearest.type + ' ' + nearest.date.slice(5)});
    }
    window.MACRO_ALERTS = alerts;
    _updateMacroBanner();
  } catch (_) {}
}

function calcExpectedMove(strat) {
  const iv = strat.iv;
  const expDate = nyClose(strat.expiration);
  const dte = Math.max(1, (expDate - new Date()) / 86400000);
  const emPct = iv * Math.sqrt(dte / 365) * 100;  // 1 SD move as %
  const emDollar = strat.current_price * emPct / 100;
  return {pct: emPct, dollar: emDollar};
}

// Paint the LIVE bar (ticker + price + today's change) for the given
// strategy. Called from sidebar clicks so the user doesn't see the previous
// ticker's price for up to 1s while waiting for the next DXLink push.
function updateLivePriceBar(strat) {
  const livePriceEl = document.getElementById('livePrice');
  if (!livePriceEl || !strat) return;
  const price = strat.current_price || 0;
  const prevClose = (RAW_DATA && RAW_DATA.equity_prev_close) || {};
  const pc = prevClose[strat.underlying] || 0;
  const chg = pc > 0 ? price - pc : 0;
  const chgPct = pc > 0 ? (chg / pc * 100) : 0;
  const chgCls = chg >= 0 ? 'color:var(--green)' : 'color:var(--red)';
  const sign = chg >= 0 ? '+' : '';
  let priceHtml = strat.underlying + ' <strong>$' + price.toFixed(2) + '</strong>';
  if (pc > 0) {
    priceHtml += ' <span style="' + chgCls + ';font-weight:700">' + sign + '$' + chg.toFixed(2) +
                 ' (' + sign + chgPct.toFixed(2) + '%)</span>';
  }
  livePriceEl.innerHTML = priceHtml;
}

function selectStrategy(idx) {
  state.selectedIdx = idx;
  const strat = RAW_DATA.strategies[idx];

  // Repaint LIVE bar immediately with the new ticker — RAW_DATA already has
  // fresh prices, no need to wait for the next DXLink push (~1s batch).
  updateLivePriceBar(strat);

  // Reset overrides when switching strategy. Floor to 30% if the server
  // somehow handed back a zero IV (DXLink Greeks not yet streamed +
  // metrics-API miss) so downstream BSM/POP math doesn't degenerate.
  state.ivPct = Math.max(5, Math.round((strat.iv || 0.30) * 100));
  state.customPrice = null;
  _ivSliderFit(state.ivPct);
  document.getElementById('ivVal').textContent = state.ivPct + '%';
  document.getElementById('ivResetBtn').style.display = 'none';
  document.getElementById('priceInput').value = '';

  // Set range to expected move × 1.5 (e.g., EM 15.6% → range ±23%)
  const em = calcExpectedMove(strat);
  state.rangePct = Math.max(2, Math.min(50, Math.round(em.pct * 1.5)));
  document.getElementById('rangeSlider').value = state.rangePct;
  document.getElementById('rangeVal').textContent = '\u00B1' + state.rangePct + '%';
  document.getElementById('expectedMoveLabel').textContent =
    'EM: \u00B1$' + em.dollar.toFixed(2) + ' (' + em.pct.toFixed(1) + '%)';

  // Update sidebar active
  document.querySelectorAll('.strat-item').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });

  renderStrategy();
  // Fetch news for this ticker
  const _tk = RAW_DATA.strategies[idx].underlying || RAW_DATA.strategies[idx].symbol || '';
  if (_tk) fetchTickerNews(_tk);
}

function renderStrategy() {
  const strat = RAW_DATA.strategies[state.selectedIdx];
  const iv = state.ivPct / 100;
  const analysis = analyzeStrategy(strat, iv);

  updateCreditSpreadBanner(strat);
  renderLegs(strat);
  renderSummary(strat, analysis);
  renderGreeks(analysis);

  if (state.view === 'table') {
    renderTable(strat, analysis, iv);
  } else {
    renderGraph(strat, analysis, iv);
  }
}

function renderLegs(strat) {
  const el = document.getElementById('legsDetail');

  // Per-leg with entry, current, and P/L from TastyTrade
  const legsHtml = strat.legs.map(l => {
    const dir = l.qty > 0 ? 'long' : 'short';
    const action = l.qty > 0 ? 'Buy' : 'Sell';
    const absQty = Math.abs(l.qty);
    const typeName = l.type === 'C' ? 'Call' : 'Put';
    // ⚠️ 这里曾经把佣金摊进腿的 P/L, 还把**成交价本身**改成了"含费有效价"
    //    (l.entry ± fee/share)。后果: 同一条腿, 头部 chip 显示 @$46.04 / −$2552,
    //    而详情弹窗和真实成交是 @$46.03 / −$2551, 且几条腿加起来对不上下面两行的
    //    UNREALIZED P/L(实测每个仓位都差 $1×手数×腿数)。
    //    全仓其余四处(analyzeStrategy 摘要/仪表盘/侧栏/风险视图、openModal 详情)
    //    都是纯 mark-to-market 不含费 —— 见 analyzeStrategy 注释"Fees are realized
    //    separately on close", 也是对齐 TastyTrade 的 P/L Opn 列。chip 是唯一的异类,
    //    改成跟其余一致。改口径的是显示, 不是钱。
    const legPnl = (l.current - l.entry) * l.qty * l.mult;
    const pnlColor = legPnl >= 0 ? 'var(--green)' : 'var(--red)';
    const pnlSign = legPnl >= 0 ? '+' : '';
    return `<span class="leg-chip leg-${dir}">
      ${action} ${absQty} ${strat.underlying} $${l.strike} ${typeName}
      <span style="color:var(--text2)">@ $${l.entry.toFixed(2)}</span>
      <span style="color:var(--text2)">&rarr;</span>
      <span>$${l.current.toFixed(2)}</span>
      <span style="color:${pnlColor};font-weight:700">(${pnlSign}$${legPnl.toFixed(0)})</span>
    </span>`;
  }).join('');

  const _etLbl = strat.earnings_time === 'BMO' ? 'BM' : strat.earnings_time === 'AMC' ? 'AM' : strat.earnings_time || '';
  const earningsHtml = strat.earnings_date
    ? `<span style="margin-left:auto;color:var(--yellow);font-size:11px;cursor:pointer" onclick="event.stopPropagation();showEarningsHistory('${strat.underlying}','${strat.earnings_date}')">&#128227; Earnings: ${strat.earnings_date}${_etLbl ? ' <span style="background:#d2992244;padding:1px 5px;border-radius:3px;font-size:10px">' + _etLbl + '</span>' : ''}</span>`
    : '';

  el.innerHTML = `
    <span style="font-weight:700;font-size:14px;cursor:pointer;text-decoration:underline;text-decoration-color:#30363d;text-underline-offset:3px" onclick="openTvChart('${strat.underlying}')" title="Click for chart">${strat.underlying} ${strat.name}</span>
    <span style="color:var(--text2);font-size:12px">Exp: ${strat.expiration} (${Math.round(strat.dte)}d)</span>
    ${legsHtml}
    <span class="gear-icon" onclick="openModal(${state.selectedIdx})" title="Position Details">&#9881;</span>
    ${earningsHtml}
  `;
}

function renderSummary(strat, analysis) {
  const S = strat.current_price;
  const be = analysis.breakevens;
  const beStr = be.length > 0
    ? be.map(b => `$${b.toFixed(2)}`).join(' / ')
    : 'N/A';
  const bePctStr = be.length > 0
    ? be.map(b => {
        const pct = ((b - S) / S * 100);
        return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
      }).join(' / ')
    : '';

  const isDebit = analysis.netDebit > 0;
  const debitLabel = isDebit ? 'NET DEBIT' : 'NET CREDIT';
  const debitVal = Math.abs(analysis.netDebit);

  // Determine breakeven direction: check if P/L is positive ABOVE the breakeven
  let beDirection = '';
  if (be.length > 0) {
    let pnlAbove = 0;
    const testPrice = be[0] * 1.05;
    strat.legs.forEach(l => {
      const expVal = l.type === 'C' ? Math.max(0, testPrice - l.strike) : Math.max(0, l.strike - testPrice);
      pnlAbove += (expVal - l.entry) * l.qty * l.mult;
    });
    beDirection = pnlAbove > 0 ? 'Above' : 'Below';
  }

  document.getElementById('summaryBar').innerHTML = `
    <div class="summary-item">
      <div class="summary-label">${debitLabel}</div>
      <div class="summary-value">${isDebit ? '&#x1F4B0;' : '&#x1F4B5;'} $${debitVal.toFixed(2)}</div>
    </div>
    <div class="summary-item">
      <div class="summary-label">MAX LOSS</div>
      <div class="summary-value" style="color:var(--red)">&#8600; $${Math.abs(analysis.maxLoss).toFixed(2)}</div>
    </div>
    <div class="summary-item">
      <div class="summary-label">MAX PROFIT</div>
      <div class="summary-value" style="color:var(--green)">&#8599; $${analysis.maxProfit.toFixed(2)}</div>
    </div>
    <div class="summary-item">
      <div class="summary-label">CHANCE OF PROFIT</div>
      <div class="summary-value">&#127922; ${Math.round(analysis.cop)}%</div>
    </div>
    <div class="summary-item">
      <div class="summary-label">BREAKEVEN</div>
      <div class="summary-value">&#8594; ${beDirection} ${beStr}</div>
      <div class="summary-sub">(${bePctStr})</div>
    </div>
    <div class="summary-item">
      <div class="summary-label">UNREALIZED P/L</div>
      <div class="summary-value" style="color:${analysis.unrealizedPnl >= 0 ? 'var(--green)' : 'var(--red)'}">
        ${analysis.unrealizedPnl >= 0 ? '&#128200;' : '&#128201;'} ${analysis.unrealizedPnl >= 0 ? '+' : ''}$${analysis.unrealizedPnl.toFixed(2)}
        (${analysis.unrealizedPct >= 0 ? '+' : ''}${analysis.unrealizedPct.toFixed(1)}%)
      </div>
      <div class="summary-sub">entry vs mid (TastyTrade)</div>
    </div>
  `;
}

function renderGreeks(analysis) {
  const g = analysis.greeks;
  document.getElementById('greeksBar').innerHTML = `
    <div class="greek-item"><span class="greek-symbol">&Delta;</span> <span class="greek-value">${g.delta.toFixed(1)}</span></div>
    <div class="greek-item"><span class="greek-symbol">&Gamma;</span> <span class="greek-value">${g.gamma.toFixed(3)}</span></div>
    <div class="greek-item"><span class="greek-symbol">&Theta;</span> <span class="greek-value" style="color:${g.theta < 0 ? 'var(--red)' : 'var(--green)'}">${g.theta.toFixed(3)}</span></div>
    <div class="greek-item"><span class="greek-symbol">&nu;</span> <span class="greek-value">${g.vega.toFixed(2)}</span></div>
    <div class="greek-item"><span class="greek-symbol">&rho;</span> <span class="greek-value">${g.rho.toFixed(2)}</span></div>
    <div style="margin-left:auto;color:var(--text2);font-size:12px">
      Stock: $${RAW_DATA.strategies[state.selectedIdx].current_price.toFixed(2)} | IV: ${(analysis.iv*100).toFixed(0)}% | DTE: ${Math.round(analysis.dte)}
    </div>
  `;
}

// ── Ticker News (below Greeks) ──
let _newsCache = {};
let _newsTimers = {};
const _BULL_WORDS = ['surge','rally','gain','rise','beat','exceed','upgrade','bullish','record','growth','profit','strong','soar','jump','outperform','buy','positive','breakout','momentum'];
const _BEAR_WORDS = ['drop','fall','decline','crash','miss','downgrade','bearish','loss','weak','plunge','sell','warning','risk','cut','layoff','recession','lawsuit','investigation','fraud'];

function _sentiment(title) {
  const t = title.toLowerCase();
  let p = 0, n = 0;
  _BULL_WORDS.forEach(w => { if (t.includes(w)) p++; });
  _BEAR_WORDS.forEach(w => { if (t.includes(w)) n++; });
  return p > n ? 'bull' : n > p ? 'bear' : 'neutral';
}

async function fetchTickerNews(ticker) {
  const box = document.getElementById('tickerNews');
  if (!box) return;
  box.style.display = 'block';
  box.innerHTML = '<div style="color:var(--text2);font-size:12px;padding:4px">Loading news...</div>';
  try {
    const r = await fetch('/api/finviz/' + ticker);
    const d = await r.json();
    const news = d.news || [];
    _newsCache[ticker] = news;
    renderTickerNews(ticker, news);
    // Auto-refresh every 30 min
    if (_newsTimers[ticker]) clearInterval(_newsTimers[ticker]);
    _newsTimers[ticker] = setInterval(() => fetchTickerNews(ticker), 30 * 60 * 1000);
  } catch(e) {
    box.innerHTML = '<div style="color:var(--red);font-size:12px;padding:4px">News unavailable</div>';
  }
}

function renderTickerNews(ticker, news) {
  const box = document.getElementById('tickerNews');
  const title = document.getElementById('newsDrawerTitle');
  // Only keep news with bull/bear sentiment
  const filtered = (news||[]).filter(n => {const s=_sentiment(n.title||''); return s==='bull'||s==='bear'});
  if (title) title.innerHTML = ticker + ' News <span style="color:var(--text2);font-weight:400;font-size:11px">(' + filtered.length + ' with sentiment)</span>';
  if (!box || !filtered.length) { if(box) box.innerHTML = '<div style="color:var(--text2);font-size:12px;padding:8px">No sentiment news</div>'; return; }
  let h = '';
  filtered.forEach(n => {
    const t = n.title || '';
    const s = _sentiment(t);
    const bg = s === 'bull' ? 'rgba(63,185,80,0.08)' : 'rgba(248,81,73,0.08)';
    const border = s === 'bull' ? 'rgba(63,185,80,0.3)' : 'rgba(248,81,73,0.3)';
    const badge = s === 'bull' ? '<span style="color:#3fb950;font-size:10px;font-weight:600;margin-right:4px">BULL</span>' :
                                 '<span style="color:#f85149;font-size:10px;font-weight:600;margin-right:4px">BEAR</span>';
    h += '<div style="padding:5px 8px;margin-bottom:3px;border-radius:4px;background:' + bg + ';border-left:2px solid ' + border + '">';
    h += badge + '<a href="' + (n.url||'') + '" target="_blank" style="color:var(--text);text-decoration:none;font-size:12px">' + t + '</a>';
    h += '<div style="color:var(--text2);font-size:10px;margin-top:1px">' + (n.source||'') + ' · ' + (n.date||'') + '</div>';
    h += '</div>';
  });
  box.innerHTML = h;
}

// ── TradingView Chart Modal ──
function openTvChart(ticker) {
  const overlay = document.getElementById('tvOverlay');
  const title = document.getElementById('tvChartTitle');
  const body = document.getElementById('tvChartBody');
  title.textContent = ticker;
  body.innerHTML = '';
  const container = document.createElement('div');
  container.className = 'tradingview-widget-container';
  container.style.cssText = 'width:100%;height:100%';
  const widget = document.createElement('div');
  widget.className = 'tradingview-widget-container__widget';
  container.appendChild(widget);
  const script = document.createElement('script');
  script.type = 'text/javascript';
  script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-symbol-overview.js';
  script.async = true;
  script.textContent = JSON.stringify({
    symbols: [[ticker, ticker + '|1D']],
    lineWidth: 2, lineType: 0, chartType: 'area',
    fontColor: 'rgb(106,109,120)', gridLineColor: 'rgba(242,242,242,0.06)',
    volumeUpColor: 'rgba(34,171,148,0.5)', volumeDownColor: 'rgba(247,82,95,0.5)',
    backgroundColor: '#0F0F0F', widgetFontColor: '#DBDBDB',
    upColor: '#22ab94', downColor: '#f7525f',
    borderUpColor: '#22ab94', borderDownColor: '#f7525f',
    wickUpColor: '#22ab94', wickDownColor: '#f7525f',
    colorTheme: 'dark', isTransparent: false, locale: 'en',
    chartOnly: false, scalePosition: 'right', scaleMode: 'Normal',
    fontFamily: '-apple-system,BlinkMacSystemFont,Trebuchet MS,Roboto,Ubuntu,sans-serif',
    valuesTracking: '1', changeMode: 'price-and-percent',
    dateRanges: ['1d|1','1m|30','3m|60','12m|1D','60m|1W','all|1M'],
    fontSize: '10', headerFontSize: 'medium',
    autosize: true, width: '100%', height: '100%',
    noTimeScale: false, hideDateRanges: false, hideMarketStatus: false, hideSymbolLogo: false
  });
  container.appendChild(script);
  body.appendChild(container);
  overlay.style.display = 'block';
}

function closeTvChart() {
  const overlay = document.getElementById('tvOverlay');
  const body = document.getElementById('tvChartBody');
  overlay.style.display = 'none';
  body.innerHTML = '';
}

// ── Alert System ──
const ALERT_COOLDOWN = 5 * 60 * 1000; // 5 min cooldown per alert
// regime 自适应止盈下沿(占最大利润%):牛市 70-80%→70 触发, 震荡 50-60%→50, 中性 65。真实期权链回测 §9。
// freq: 'daily'=每标的每天一次 / 'always'=每次达成都提醒(1 分钟限流)。
const EXIT_DEFAULTS = { enabled: true, bullPct: 70, chopPct: 50, neutralPct: 65, freq: 'daily' };
let _alertCfg = { enabled: true, sound: false, exit: Object.assign({}, EXIT_DEFAULTS), creditSpread: { enabled: true }, custom: {}, cooldowns: {} };

function loadAlertConfig() {
  try {
    const raw = localStorage.getItem('alert_config');
    if (raw) _alertCfg = JSON.parse(raw);
  } catch(e) {}
  // Migrate configs saved before the Exit rule existed.
  if (!_alertCfg.exit) _alertCfg.exit = Object.assign({}, EXIT_DEFAULTS);
  // Migrate legacy single %-of-max-profit (minMaxProfitPct) → regime-adaptive bull/chop.
  if (_alertCfg.exit.bullPct == null) {
    _alertCfg.exit = { enabled: _alertCfg.exit.enabled !== false,
                       bullPct: 70, chopPct: 50, neutralPct: 65 };
  }
  if (!_alertCfg.exit.freq) _alertCfg.exit.freq = 'daily';      // 提醒频率
  if (_alertCfg.sound == null) _alertCfg.sound = false;         // 声音(独立于总开关)
  if (!_alertCfg.creditSpread) _alertCfg.creditSpread = { enabled: true };
}
function saveAlertConfig() {
  _alertCfg.enabled = document.getElementById('alertEnabled')?.checked ?? true;
  _alertCfg.sound = document.getElementById('alertSound')?.checked || false;
  // Exit / take-profit guard (the ONLY take-profit; no cost-% stop-loss anymore).
  // Only read when its inputs are mounted — avoids clobbering saved values.
  const _exEn = document.getElementById('alertExitEnabled');
  if (_exEn) {
    const _freq = document.querySelector('input[name=alertExitFreq]:checked')?.value || 'daily';
    _alertCfg.exit = {
      enabled: _exEn.checked,
      bullPct: parseInt(document.getElementById('alertExitBullPct')?.value || EXIT_DEFAULTS.bullPct),
      chopPct: parseInt(document.getElementById('alertExitChopPct')?.value || EXIT_DEFAULTS.chopPct),
      neutralPct: (_alertCfg.exit && _alertCfg.exit.neutralPct) || EXIT_DEFAULTS.neutralPct,
      freq: _freq,
    };
  }
  const _csEn = document.getElementById('alertCreditEnabled');
  if (_csEn) _alertCfg.creditSpread = { enabled: _csEn.checked };
  localStorage.setItem('alert_config', JSON.stringify(_alertCfg));
  // Mirror to the server so the native push watchers use the same settings when closed.
  try {
    fetch('/api/alert-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: _alertCfg.enabled, sound: _alertCfg.sound,
                             exit: _alertCfg.exit, creditSpread: _alertCfg.creditSpread,
                             muted: _alertCfg.muted || false }),
    }).catch(() => {});
  } catch (e) {}
}

function toggleMute() {
  fetch('/api/alert-config').then(r => r.json()).then(cfg => {
    cfg.muted = !cfg.muted;
    return fetch('/api/alert-config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg),
    }).then(() => cfg.muted);
  }).then(muted => {
    _alertCfg.muted = muted;
    _syncMuteBtn();
  }).catch(() => {});
}
function _syncMuteBtn() {
  const b = document.getElementById('muteBtn');
  if (!b) return;
  const m = _alertCfg.muted;
  b.innerHTML = m ? '&#128277;' : '&#128276;';
  b.title = m ? '通知已静音 — 点击恢复' : '静音所有通知(止盈/异动/风险/AI持仓)';
  b.style.color = m ? '#f85149' : '';
}
fetch('/api/alert-config').then(r => r.json()).then(cfg => {
  _alertCfg.muted = cfg.muted || false;
  _syncMuteBtn();
}).catch(() => {});

// ═══════════════ AI 暂停开关(定时选股/叙事)═══════════════
let _aiPaused = false;
function toggleAiPause() {
  fetch('/api/ai-pause', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ paused: !_aiPaused }),
  }).then(r => r.json()).then(d => {
    _aiPaused = d.paused;
    _syncAiPauseBtn();
  }).catch(() => {});
}
function _syncAiPauseBtn() {
  const b = document.getElementById('aiPauseBtn');
  if (!b) return;
  b.title = _aiPaused ? 'AI 定时已暂停 — 点击恢复' : '暂停定时 AI(选股/叙事)';
  b.textContent = 'AI';
  b.style.color = _aiPaused ? '#f85149' : '#8b949e';
  b.style.borderColor = _aiPaused ? '#f85149' : '';
}
fetch('/api/ai-pause').then(r => r.json()).then(d => {
  _aiPaused = d.paused;
  _syncAiPauseBtn();
}).catch(() => {});

// ═══════════════ 电源模式药丸(full/lite/off + 手动开实时)═══════════════
// 后端 power guard 每 30s 判档写 /tmp/pwa_power_mode;server _live_on() 门控 DXLink。
// 药丸:full=⚡实时(信息态);lite 未开=🔋精简(点开实时);lite 已开=⚡实时(点回精简,
// 空闲 5 分钟或电源翻档自动回)。off 档进程已被杀,看不到页面。
let _powerLive = { mode: 'full', live_on: true, override_on: false };
function _syncPowerPill() {
  const b = document.getElementById('powerPill');
  if (!b) return;
  const mode = _powerLive.mode;
  const live = _powerLive.live_on;
  if (mode === 'full') {
    b.style.display = 'none';   // 插电常态不打扰
    return;
  }
  b.style.display = '';
  if (live) {
    b.innerHTML = '&#9889; 实时';                 // ⚡
    b.style.color = '#3fb950';
    b.title = '电池·实时流开着 — 点击回精简省电(空闲 5 分钟自动回)';
  } else {
    b.innerHTML = '&#128267; 精简';               // 🔋
    b.style.color = '#d29922';
    b.title = '电池·精简模式(实时流已停)— 点击开实时';
  }
}
function _refreshPowerPill() {
  fetch('/api/power/live').then(r => r.json()).then(d => {
    _powerLive = d; _syncPowerPill();
  }).catch(() => {});
}
function togglePowerLive() {
  if (_powerLive.mode !== 'lite') return;         // 只有 lite 档可切
  const want = !_powerLive.live_on;
  fetch('/api/power/live', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ on: want }),
  }).then(r => r.json()).then(d => {
    _powerLive = d; _syncPowerPill();
  }).catch(() => {});
}
_refreshPowerPill();
setInterval(_refreshPowerPill, 20000);            // 20s 轮询跟随档位变化

// ═══════════════ 交易体检(checkup)═══════════════
// 出场判定 — 与后端 exit_advisor.py 同口径。**regime 自适应止盈**(真实期权链回测 §9):
// 牛市(SPY>200SMA)吃到 70-80% 最大利润、震荡/弱(SPY≤200SMA)吃到 50-60%;100% 死等每段都最差;借方价差不止损。
// 进度 = 已实现浮盈 / 理论最大利润(0..1);maxProfit 无界(≥1e9,如裸腿)则不可算。
// 两档止盈:soft=建议落袋(🔵)、forced=强制平(🔴);剩余 DTE<gate(20)→ 提前到 soft 强制(时间不多)。
// VIX 高时后端已把 regime 降级为震荡(波动大→快止盈),前端只读 regime 的 band。
let _ckRegime = null;  // /api/checkup/regime → {regime, vix, reason, band:{near,soft,forced,zone,label,dte_gate}}
function _ckBand() {
  // regime 决定 bull/chop/neutral;soft 用【作者在 Alert 面板设的值】(bull/chop/neutralPct),
  // 强制 = 软 + 10。这样面板 ↔ 体检卡 ↔ 告警三处一致(以前这里用的是后端硬编码档,和面板脱节)。
  const rg = (_ckRegime && _ckRegime.regime) || null;       // 'bull'|'chop'|null
  const ex = (_alertCfg && _alertCfg.exit) || EXIT_DEFAULTS;
  const pick = rg === 'bull' ? ex.bullPct : rg === 'chop' ? ex.chopPct : ex.neutralPct;
  const dflt = rg === 'bull' ? 70 : rg === 'chop' ? 50 : 65;
  const soft = (pick != null ? pick : dflt) / 100;
  const forced = Math.min(1, soft + 0.10);
  const meta = (_ckRegime && _ckRegime.band) || {};
  const label = meta.label || (rg === 'bull' ? '牛市' : rg === 'chop' ? '震荡/弱' : '中性');
  return { near: Math.max(0, soft - 0.10), soft, forced, label,
           zone: Math.round(soft*100) + '-' + Math.round(forced*100) + '%',
           dte_gate: meta.dte_gate || 20 };
}
function ckLoadRegime() {
  fetch('/api/checkup/regime').then(x => x.json()).then(r => { if (r) _ckRegime = r; }).catch(() => {});
}
ckLoadRegime();
setInterval(ckLoadRegime, 30 * 60 * 1000);   // regime 半小时刷新一次
// 后台轮询(PWA 开着时):每 60s 用最新 /api/data 评估止盈告警。checkAlerts 内含
// 每天一次 / 每次1分钟限流。以前 checkAlerts 是孤儿(没人调)→ 开着 PWA 永远不弹,这里接上。
function _alertPoll() {
  try {
    if (!_alertCfg || !_alertCfg.enabled || _alertCfg.muted) return;
    fetch('/api/data').then(r => r.json())
      .then(d => { if (d && d.strategies) checkAlerts(d.strategies); }).catch(() => {});
  } catch (e) {}
}
setInterval(_alertPoll, 60000);
setTimeout(_alertPoll, 4000);   // 首次:等 regime 加载后查一遍
// ⚠ 未挂止盈 GTC:后端 loop 60s 轮询,前端也 60s 拉快照更新 badges/banner
setInterval(loadMissingGtc, 60000);
setTimeout(loadMissingGtc, 5000);   // 首次:等 sidebar render 完再挂徽章
setTimeout(loadMacroAlerts, 6000);  // 宏观事件 vs 持仓到期交叉告警
setInterval(loadMacroAlerts, 300000); // 5 分钟刷一次(事件表很少变)
function ckExitVerdict(unrealizedPnl, maxProfit, dte, pnlPct) {
  const prog = (maxProfit != null && maxProfit > 0 && maxProfit < 1e9) ? (unrealizedPnl / maxProfit) : null;
  const b = _ckBand(); const rg = b.label, zone = b.zone;
  const forcedPct = Math.round(b.forced*100);
  const shortT = (dte != null && dte < (b.dte_gate || 20));
  const fa = shortT ? b.soft : b.forced;   // 强制下沿:时间少则提前到 soft
  if (prog != null && prog >= fa)
    return {code:'force_tp', emoji:'🔴', label:'强制止盈', sev:'good',
      reason: shortT ? `已 ${(prog*100).toFixed(0)}% 最大利润 · 剩${dte}天时间不多、利润易回吐 → 现在就平、开下单`
                     : `已 ${(prog*100).toFixed(0)}% 最大利润 → ${rg}到 ${forcedPct}% 强制档,现在就平、别再等`};
  if (prog != null && prog >= b.soft)
    return {code:'tp', emoji:'🔵', label:'止盈', sev:'good',
      reason:`已吃到 ${(prog*100).toFixed(0)}% 最大利润 → ${rg}进入 ${zone} 止盈区,建议落袋;到 ${forcedPct}% 强制`};
  if (prog != null && prog >= b.near)
    return {code:'near', emoji:'🟡', label:'接近', sev:'warn',
      reason:`已 ${(prog*100).toFixed(0)}% 最大利润,${rg}快到 ${zone} 止盈区 → 别提前跑`};
  if (pnlPct != null && pnlPct <= -50)
    return {code:'cut', emoji:'🔴', label:'深亏', sev:'bad',
      reason:`亏 ${Math.abs(pnlPct).toFixed(0)}% —— 但别砍!借方价差已限损,历史这类多半反弹,持有到 ${zone} 或到期`};
  const why = (prog != null) ? `已 ${(prog*100).toFixed(0)}% 最大利润,${rg}持有到 ${zone} 再走`
                             : `${rg}:持有到 ${zone} 最大利润或到期,不止损`;
  return {code:'hold', emoji:'🟢', label:'持有', sev:'ok', reason:why};
}

let _ckState = {tab:'pre', spreadOpen:false};

function openCheckup() {
  appNavInto();
  const o = document.getElementById('checkupOverlay');
  if (o) o.style.display = 'block';
  switchCheckupTab('pre');
  ckLoadRegime();  // refresh bull/chop band so 止盈区 is current
  fetch('/api/checkup/profile').then(x => x.json()).then(renderCommonMistakes).catch(() => {});
  loadMissingGtc();  // ⚠ 未挂止盈 GTC —— 打开体检时立即刷一次,banner 拿最新
  setTimeout(() => { const t = document.getElementById('ckTickers'); if (t) t.focus(); }, 50);
}
// 你的高频错误 bullet 列表(开单前一打开就照镜子)
function renderCommonMistakes(prof) {
  const el = document.getElementById('ckCommon'); if (!el) return;
  const cm = (prof && prof.common_mistakes) || [];
  if (!cm.length) { el.innerHTML = ''; return; }
  const hl = (prof && prof.headline) || {};
  const money = (v) => '$' + Math.abs(Math.round(v)).toLocaleString();
  let h = `<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:11px 13px">
    <div style="color:#f85149;font-weight:700;font-size:13px;margin-bottom:7px">📋 你的高频错误(按累计亏损 — 开单前先照照镜子)</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 18px">`;
  h += cm.map(m => {
    const pnl = m.net_pnl || 0; const wr = m.win_rate != null ? (m.win_rate * 100).toFixed(0) + '%' : '-';
    return `<div style="font-size:11px;color:#c9d1d9">• <b>${m.label}</b> <span style="color:#8b949e">${m.n}单 · </span><span style="color:${pnl < 0 ? '#f85149' : '#3fb950'}">${pnl < 0 ? '亏' : '赚'}${money(pnl)}</span> <span style="color:#8b949e">· 胜率${wr}</span></div>`;
  }).join('');
  h += `</div>`;
  const hb = [];
  if (hl.no_logic) hb.push(`无自主逻辑 ${hl.no_logic.n}单亏${money(hl.no_logic.net_pnl)}`);
  if (hl.short_hold) hb.push(`持有≤5天 ${hl.short_hold.n}单亏${money(hl.short_hold.net_pnl)}`);
  if (hl.emotion) hb.push(`情绪化 ${hl.emotion.n}单亏${money(hl.emotion.net_pnl)}`);
  if (hb.length) h += `<div style="margin-top:8px;padding-top:7px;border-top:1px solid #21262d;color:#ff9f0a;font-size:11px">⚠ 最大的洞:${hb.join('  ·  ')}</div>`;
  h += `</div>`;
  el.innerHTML = h;
}
function closeCheckup() {
  const o = document.getElementById('checkupOverlay');
  if (o) o.style.display = 'none';
}
function openRiskRadar() {
  const o = document.getElementById('riskRadarOverlay');
  // 雷达已移到 Events tab: overlay 原本嵌在 #tab-positions 内(切 tab 会被藏),
  // 首次打开时 re-parent 到 body → 任何 tab 都能弹(position:fixed z-index:401)。
  if (o && o.parentElement !== document.body) document.body.appendChild(o);
  appNavInto();
  if (o) o.style.display = 'block';
  setRRTab('radar');
  renderRiskRadar();
}
function closeRiskRadar() {
  const o = document.getElementById('riskRadarOverlay');
  if (o) o.style.display = 'none';
}
function setRRTab(t) {
  const pr = document.getElementById('rrPaneRadar'), pp = document.getElementById('rrPanePlaybook');
  if (pr) pr.style.display = (t === 'radar') ? '' : 'none';
  if (pp) pp.style.display = (t === 'playbook') ? '' : 'none';
  const on = 'background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600';
  const off = 'background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600';
  const br = document.getElementById('rrTabRadar'), bp = document.getElementById('rrTabPlaybook');
  if (br) br.style.cssText = (t === 'radar') ? on : off;
  if (bp) bp.style.cssText = (t === 'playbook') ? on : off;
  if (t === 'playbook') renderWiimCauses();
}
async function renderWiimCauses() {
  const el = document.getElementById('rrPanePlaybook'); if (!el) return;
  let ref = window._rrCauses;
  if (!ref || !ref.causes) {
    el.innerHTML = '<div style="padding:30px;text-align:center;color:#8b949e">加载异动原因…</div>';
    try { const r = await fetch('/api/checkup/risk-radar'); const d = await r.json();
          ref = window._rrCauses = (d && d.wiim_causes) || {}; }
    catch (e) { el.innerHTML = `<div style="padding:20px;color:#f85149">加载失败: ${e}</div>`; return; }
  }
  if (!ref || !ref.causes) { el.innerHTML = '<div style="padding:20px;color:#8b949e">暂无数据</div>'; return; }
  const m = ref.meta || {};
  const pct = x => (x == null ? '—' : Math.round(x * 100) + '%');
  const card = (title, body, sub) => `<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;margin-bottom:12px">
     <div style="font-size:13px;font-weight:700;color:#e6edf3;margin-bottom:${sub ? 2 : 8}px">${title}</div>
     ${sub ? `<div style="font-size:11px;color:#6e7681;margin-bottom:8px">${sub}</div>` : ''}${body}</div>`;
  const rules = (ref.key_rules || []).map(r => `<div style="padding:6px 0;border-top:1px solid #21262d;font-size:12px;color:#c9d1d9;line-height:1.6">${r}</div>`).join('');
  const row = c => {
    const us = (c.up_share == null) ? 0.5 : c.up_share;
    const upcol = us >= 0.65 ? '#3fb950' : (us <= 0.35 ? '#f85149' : '#8b949e');
    const bar = `<div style="display:flex;height:8px;width:84px;border-radius:3px;overflow:hidden;background:#21262d"><div style="width:${Math.round(us*100)}%;background:#3fb950"></div><div style="flex:1;background:#f85149"></div></div>`;
    return `<tr style="border-top:1px solid #21262d">
       <td style="padding:4px 6px;color:#e6edf3">${c.cause}${c.nonfund ? ' <span style="color:#ff9f0a;font-size:10px">⚠噪音</span>' : ''}</td>
       <td style="padding:4px 6px;text-align:right;color:#e6edf3">${pct(c.share)}</td>
       <td style="padding:4px 6px;text-align:right;color:#6e7681">${(c.n||0).toLocaleString()}</td>
       <td style="padding:4px 6px"><div style="display:flex;align-items:center;gap:6px;justify-content:flex-end">${bar}<span style="color:${upcol};min-width:32px;text-align:right">${pct(c.up_share)}↑</span></div></td></tr>`;
  };
  const tbl = `<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px">
     <thead><tr style="color:#6e7681;font-size:11px"><th style="text-align:left;padding:2px 6px">异动原因</th><th style="text-align:right;padding:2px 6px">占比</th><th style="text-align:right;padding:2px 6px">条数</th><th style="text-align:right;padding:2px 6px">涨↑/跌↓</th></tr></thead>
     <tbody>${(ref.causes || []).map(row).join('')}</tbody></table></div>`;
  const bl = (ref.bullish || []).map(r => `<div style="display:flex;gap:8px;padding:3px 0;border-top:1px solid #21262d;font-size:12px"><span style="flex:1;color:#e6edf3">${r.cause}</span><span style="color:#3fb950">${pct(r.up_share)}↑</span></div>`).join('');
  const br = (ref.bearish || []).map(r => `<div style="display:flex;gap:8px;padding:3px 0;border-top:1px solid #21262d;font-size:12px"><span style="flex:1;color:#e6edf3">${r.cause}</span><span style="color:#f85149">${pct(r.down_share)}↓</span></div>`).join('');
  const dirBody = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
     <div><div style="font-size:11px;color:#3fb950;margin-bottom:2px">📈 看到≈偏多</div>${bl}</div>
     <div><div style="font-size:11px;color:#f85149;margin-bottom:2px">📉 看到≈偏空</div>${br}</div></div>`;
  el.innerHTML =
    `<div style="font-size:11px;color:#6e7681;margin-bottom:10px">来源: ${m.source || 'WIIM'} · 共 ${(m.total||0).toLocaleString()} 条 · 方向(按文章措辞): <span style="color:#3fb950">涨 ${pct(m.up_share)}</span> / <span style="color:#f85149">跌 ${pct(1-(m.up_share||0))}</span> · 约 ${pct(m.nonfund_share)} 无公司催化</div>`
    + card('📌 核心规律', rules)
    + card('🎯 催化剂方向性 — 看到这类新闻偏多还是偏空', dirBody, '涨占比 = 该原因的 WIIM 里文章说 trading higher 的比例(样本≥200)')
    + card('📊 全部异动原因分布', tbl, '占比 = 该原因占全部 WIIM 的比例; 条形 = 涨(绿)/跌(红)拆分');
  el.dataset.rendered = '1';
}

function switchCheckupTab(t) {
  _ckState.tab = t;
  document.getElementById('ckPre').style.display = (t === 'pre') ? '' : 'none';
  document.getElementById('ckPos').style.display = (t === 'pos') ? '' : 'none';
  const on = 'background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer';
  const off = 'background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer';
  document.getElementById('ckTabPre').style.cssText = (t === 'pre') ? on : off;
  document.getElementById('ckTabPos').style.cssText = (t === 'pos') ? on : off;
  if (t === 'pos') renderExitAdvisor();
}
async function renderRiskRadar() {
  const el = document.getElementById('rrResult'); if (!el) return;
  el.innerHTML = '<div style="padding:30px;text-align:center;color:#8b949e">加载风险雷达…</div>';
  let d;
  try { const r = await fetch('/api/checkup/risk-radar'); d = await r.json(); }
  catch (e) { el.innerHTML = `<div style="padding:20px;color:#f85149">加载失败: ${e}</div>`; return; }
  if (!d || d.error) { el.innerHTML = `<div style="padding:20px;color:#f85149">${(d && d.error) || '无数据'}</div>`; return; }
  const sevC = {red:'#f85149', warn:'#ff9f0a', info:'#58a6ff'};
  const dot = s => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${sevC[s]||'#8b949e'};flex:none"></span>`;
  const g = d.gauge || {};
  const score = g.score || 0;
  const col = score >= 60 ? '#f85149' : score >= 30 ? '#ff9f0a' : '#3fb950';
  const lvl = g.level === 'high' ? '高危' : g.level === 'elevated' ? '偏高' : '平静';
  window._rrRef = d.reference || {};
  window._rrCauses = d.wiim_causes || {};
  const rc = d.recent || {};
  const _rd = (o,k) => (o && o[k] != null) ? o[k] : '—';
  const rcLine = (rc.error || !rc.spy_m1) ? '' : `
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid #21262d;font-size:11px;color:#8b949e">
        <span>近1月 SPY 跌≥1%: <b style="color:#e6edf3">${_rd(rc.spy_m1,'d1')}</b> · ≥2%: <b style="color:${(_rd(rc.spy_m1,'d2')||0)>0?'#f85149':'#e6edf3'}">${_rd(rc.spy_m1,'d2')}</b></span>
        <span>近3月 ≥1%: <b style="color:#e6edf3">${_rd(rc.spy_m3,'d1')}</b> · ≥2%: <b style="color:#e6edf3">${_rd(rc.spy_m3,'d2')}</b></span>
        ${(rc.recent2pct||[]).length ? `<span>近期≥2%跌: <b style="color:#e6edf3">${rc.recent2pct.slice(0,4).map(x=>x.date.slice(5)).join('、')}</b></span>` : ''}
      </div>`;
  let hot = (g.hot_topics || []).map(h => `
    <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid #21262d">
      <span style="flex:1;color:#e6edf3;font-size:13px">${h.label}</span>
      <span style="color:#8b949e;font-size:11px">${h.count}条·占${Math.round((h.share||0)*100)}%</span>
      <span style="color:${col};font-size:11px">${h.percentile}</span>
      <span style="background:#21262d;color:#ff9f0a;border-radius:4px;padding:1px 6px;font-size:11px">lift ${h.lift}×</span>
    </div>`).join('');
  if (!hot) hot = '<div style="color:#3fb950;font-size:13px;padding:8px 0">今日无明显风险话题在烧 · 新闻面平静</div>';
  const ctx = (g.context_topics || []).map(c => `<div style="color:#6e7681;font-size:11px;padding:2px 0">· ${c.label} ${c.count}条 — ${c.note||''}</div>`).join('');
  const f = d.forward || {};
  const fscore = f.score || 0;
  const fcol = fscore >= 60 ? '#f85149' : fscore >= 30 ? '#ff9f0a' : '#3fb950';
  const flvl = f.level === 'high' ? '高危' : f.level === 'elevated' ? '偏高' : '平静';
  const miniGauge = (title, sc, c, lv, sub) => `
    <div style="flex:1;min-width:0;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px">
      <div style="display:flex;align-items:baseline;gap:8px">
        <div style="font-size:30px;font-weight:800;color:${c};line-height:1">${sc}</div>
        <div style="color:${c};font-weight:700;font-size:13px">${title} · ${lv}</div>
      </div>
      <div style="height:6px;background:#21262d;border-radius:4px;overflow:hidden;margin-top:7px"><div style="height:100%;width:${sc}%;background:${c}"></div></div>
      <div style="color:#8b949e;font-size:11px;margin-top:7px;line-height:1.5">${sub}</div>
    </div>`;
  const busy = (f.busiest_week_count >= 3) ? `⚠️ 最挤一周 ${f.busiest_week_count} 个事件扎堆` : `${f.n_events||0} 个高 lift 事件`;
  const gauges = `<div style="display:flex;gap:10px;margin-bottom:12px">
    ${miniGauge('今日新闻风险', score, col, lvl, g.top ? `主导：${g.top.label}（这类日子跌≥1% ${g.top.lift}×）` : '今日新闻面平静')}
    ${miniGauge('未来30天事件密度', fscore, fcol, flvl, busy + (fscore>=60 ? ' — 格外小心，别满仓撞上去' : ''))}
  </div>`;
  const hotCard = `
    <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;margin-bottom:12px">
      <div style="color:#e6edf3;font-weight:700;font-size:13px;margin-bottom:2px">今日在烧的风险话题</div>
      ${hot}
      ${ctx ? `<div style="margin-top:8px">${ctx}</div>` : ''}
      <div style="color:#6e7681;font-size:10px;margin-top:8px">今日 digest 累计 ${g.n_items||0} 条 · 基线截至 ${g.baseline_through||'—'} · 盘前偏低</div>
      ${rcLine}
    </div>`;
  const lm = (d.landmines || []).slice(0, 14).map(e => `
    <div style="display:flex;align-items:center;gap:8px;padding:6px 0 1px;border-top:1px solid #21262d">
      ${dot(e.severity)}
      <span style="color:#8b949e;font-size:11px;width:88px;flex:none">${e.date}${e.days_away!=null?(' ·T+'+e.days_away):''}</span>
      <span style="flex:1;color:#e6edf3;font-size:13px">${e.name}</span>
      <span style="background:#21262d;color:#ff9f0a;border-radius:4px;padding:1px 6px;font-size:11px">${e.lift}×</span>
    </div>
    <div style="color:#6e7681;font-size:11px;padding:0 0 3px 16px">${e.note||''}</div>`).join('');
  const lmCard = `
    <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-bottom:12px">
      <div style="color:#e6edf3;font-weight:700;font-size:14px;margin-bottom:3px">📅 未来 30 天 landmine 日历</div>
      <div style="color:#6e7681;font-size:11px;margin-bottom:2px;display:flex;align-items:center;gap:6px">${dot('red')} 你的持仓 ${dot('warn')} 计划内/龙头 ${dot('info')} 一般</div>
      ${lm || '<div style="color:#8b949e;font-size:13px;padding:8px 0">未来 3 周无重大事件</div>'}
    </div>`;
  let nlv = liveNLV(), dep=0, big=0;
  ((typeof RAW_DATA!=='undefined' && RAW_DATA.strategies) || []).forEach(s => { try { const a=analyzeStrategy(s); const ml=(a.maxLoss==null||a.maxLoss<=-1e9)?0:Math.abs(a.maxLoss); dep+=ml; if(ml>big)big=ml; } catch(e){} });
  const depPct = nlv>0?Math.round(dep/nlv*100):null, bigPct = nlv>0?Math.round(big/nlv*100):null;
  const dCol = depPct==null?'#8b949e':depPct<=60?'#3fb950':depPct<=70?'#ff9f0a':'#f85149';
  const bCol = bigPct==null?'#8b949e':bigPct<=5?'#3fb950':bigPct<=8?'#ff9f0a':'#f85149';
  const tail = `
    <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px">
      <div style="color:#e6edf3;font-weight:700;font-size:14px;margin-bottom:6px">🛡 尾部防御（研究铁律）</div>
      <div style="color:#8b949e;font-size:12px;line-height:1.6;margin-bottom:8px">暴跌是<b style="color:#e6edf3">隔夜砸</b>的（≤-5% 日均开盘 gap −5.5%）——盘中来不及反应，<b style="color:#e6edf3">止损防不住、只有事前现金能防</b>。</div>
      <div style="display:flex;gap:18px;margin-bottom:8px;font-size:12px">
        <div><span style="color:#6e7681">部署</span> <b style="color:${dCol}">${depPct==null?'—':depPct+'%'}</b> <span style="color:#6e7681;font-size:10px">≤60绿</span></div>
        <div><span style="color:#6e7681">单笔最大</span> <b style="color:${bCol}">${bigPct==null?'—':bigPct+'%'}</b> <span style="color:#6e7681;font-size:10px">≤5绿</span></div>
      </div>
      <div style="color:#8b949e;font-size:12px">手动盯的慢变量：<span style="color:#c9d1d9">美债收益率曲线 · 信用利差 · 部署% · VIX</span> <span style="color:#6e7681">（"AI 泡沫"喊话 lift 0.85 = 非择时信号，别被带节奏）</span></div>
    </div>`;
  const statsBtn = `<button onclick="toggleRiskStats()" id="ckStatsBtn" style="width:100%;margin-top:10px;background:#161b22;color:#58a6ff;border:1px solid #30363d;border-radius:8px;padding:9px;font-size:13px;cursor:pointer">📊 风险数据 & 规律（点开看全部研究结论）</button>`;
  el.innerHTML = gauges + hotCard + lmCard + tail + statsBtn + '<div id="ckRiskStats" style="display:none;margin-top:10px"></div>'
    + '<div style="color:#6e7681;font-size:10px;text-align:center;margin-top:10px">雷达 = 高危窗口提示(lift 1.2–3.3)，非预测哪天跌 · 来自 research/drop_events 实证</div>';
}
function toggleRiskStats() {
  const el = document.getElementById('ckRiskStats'); if (!el) return;
  const open = el.style.display === 'none';
  document.getElementById('ckStatsBtn').textContent = open ? '📊 风险数据 & 规律（收起）' : '📊 风险数据 & 规律（点开看全部研究结论）';
  el.style.display = open ? 'block' : 'none';
  if (open && !el.dataset.rendered) { renderRiskStats(window._rrRef || {}); el.dataset.rendered = '1'; }
}
function renderRiskStats(ref) {
  const el = document.getElementById('ckRiskStats'); if (!el) return;
  const card = (t, body) => `<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;margin-bottom:10px">
    <div style="color:#e6edf3;font-weight:700;font-size:13px;margin-bottom:6px">${t}</div>${body}</div>`;
  const lim = v => v>=2.5?'#f85149':v>=1.5?'#ff9f0a':'#8b949e';
  // 剂量反应
  const dr = (ref.dose_response||[]).map(r => `
    <div style="display:flex;align-items:center;gap:8px;padding:3px 0;border-top:1px solid #21262d;font-size:12px">
      <span style="flex:1;color:#e6edf3">${r.label}</span>
      <span style="color:#6e7681;font-size:11px">${r.kind||''}</span>
      <span style="color:#8b949e">普通 ${r.p80}×</span><span style="color:${lim(r.p95)};font-weight:700">饱和 ${r.p95}×</span>
    </div>`).join('');
  // 事件→板块
  const es = (ref.event_sectors||[]).map(r => `<div style="font-size:12px;padding:3px 0;border-top:1px solid #21262d"><b style="color:#58a6ff">${r.event}</b> <span style="color:#c9d1d9">→ ${r.sectors}</span></div>`).join('');
  // 板块主因
  const sd = (ref.sector_drivers||[]).map(r => `<div style="font-size:12px;padding:3px 0;border-top:1px solid #21262d"><span style="color:#e6edf3">${r.etf}</span> <span style="color:#6e7681">(基础跌率 ${r.base})</span> <span style="color:#c9d1d9">${r.top}</span></div>`).join('');
  // 财报溢出
  const e = ref.earnings_spillover || {};
  const earn = `<div style="font-size:12px;color:#c9d1d9;line-height:1.7">${e.chip_bad||''}<br>${e.chip_hit||''}<br>${e.mega_bad||''}<br><span style="color:#8b949e">${e.note||''}</span></div>`;
  // 未解释跌 taxonomy
  const u = ref.unknown_taxonomy || {};
  const unk = `<div style="font-size:12px;color:#c9d1d9;line-height:1.6"><b style="color:#3fb950">${u.explained_pct||''}% 后续1-2周可归因</b> — ${u.blurb||''}<br>${(u.types||[]).map(x=>'· '+x).join('<br>')}</div>`;
  // 铁律
  const kr = (ref.key_rules||[]).map(r => `<div style="font-size:12px;color:#c9d1d9;padding:2px 0;line-height:1.5">${r}</div>`).join('');
  el.innerHTML =
    card('📈 事件剂量反应（新闻越饱和，当日跌≥1% lift 越高）', dr || '—')
  + card('🎯 哪类事件砸哪个板块', es || '—')
  + card('🏷 各板块 ETF 的头号下跌驱动', sd || '—')
  + card('💥 龙头财报板块溢出（只 MAG7+芯片，普通财报无用）', earn)
  + card('🔎 “找不到原因”的跌其实是什么', unk)
  + card('📜 六条铁律', kr || '—');
}
function toggleCkSpread() {
  _ckState.spreadOpen = !_ckState.spreadOpen;
  document.getElementById('ckSpreadBody').style.display = _ckState.spreadOpen ? 'block' : 'none';
  document.getElementById('ckSpreadHdr').innerHTML =
    (_ckState.spreadOpen ? '▾' : '▸') + ' 我要开的价差(选填,填了才做价格体检 + 方向/OTM/赔率检查)';
  // 默认把第一个候选填进 ticker
  const st = document.getElementById('ckSpreadTicker');
  if (_ckState.spreadOpen && st && !st.value) {
    const first = (document.getElementById('ckTickers').value || '').trim().split(/[\s,]+/)[0];
    if (first) { st.value = first.toUpperCase(); ckLoadChain(); }
  }
}

async function ckLoadChain() {
  const tk = (document.getElementById('ckSpreadTicker').value || '').trim().toUpperCase();
  if (!tk) return;
  const exp = document.getElementById('ckExp');
  exp.innerHTML = '<option>加载中…</option>';
  try {
    const r = await fetch('/api/checkup/chain/' + encodeURIComponent(tk)).then(x => x.json());
    const exps = (r.expirations || []).filter(e => e.dte >= 0).slice(0, 18);
    exp.innerHTML = exps.map(e => `<option value="${e.date}">${e.date} (${e.dte}d)</option>`).join('')
      || '<option value="">无到期</option>';
    if (exps.length) ckLoadStrikes();
  } catch (e) { exp.innerHTML = '<option value="">链加载失败</option>'; }
}
async function ckLoadStrikes() {
  const tk = (document.getElementById('ckSpreadTicker').value || '').trim().toUpperCase();
  const exp = document.getElementById('ckExp').value;
  if (!tk || !exp) return;
  const side = document.getElementById('ckType').value === 'C' ? 'call' : 'put';
  try {
    const r = await fetch('/api/checkup/chain/' + encodeURIComponent(tk) + '?expiration=' + encodeURIComponent(exp)).then(x => x.json());
    const opts = (r.strikes || []).map(s => {
      const mid = (s[side] && s[side].mid) || 0;
      return `<option value="${s.strike}">${s.strike}  (mid $${mid.toFixed(2)})</option>`;
    }).join('');
    document.getElementById('ckLong').innerHTML = opts;
    document.getElementById('ckShort').innerHTML = opts;
    document.getElementById('ckSpot').textContent = r.price
      ? `现价 $${(+r.price).toFixed(2)} · IV ${((r.iv||0)*100).toFixed(0)}%` + (r.earnings_date ? ` · ⚠ 财报 ${r.earnings_date}` : '')
      : '';
  } catch (e) {}
}

async function runCheckup() {
  const tickers = (document.getElementById('ckTickers').value || '').trim();
  let proposed = null;
  const stk = (document.getElementById('ckSpreadTicker').value || '').trim().toUpperCase();
  const expEl = document.getElementById('ckExp');
  const exp = expEl ? expEl.value : '';
  const longK = parseFloat(document.getElementById('ckLong').value || '');
  const shortK = parseFloat(document.getElementById('ckShort').value || '');
  if (_ckState.spreadOpen && stk && exp && longK && shortK) {
    const type = document.getElementById('ckType').value;
    const qty = parseInt(document.getElementById('ckQty').value || '1') || 1;
    const np = parseFloat(document.getElementById('ckNet').value || '');
    proposed = {ticker: stk, direction: (type === 'C' ? 'BULL' : 'BEAR'), expiration: exp,
      legs: [{type, strike: longK, qty}, {type, strike: shortK, qty: -qty}],
      net_price: isNaN(np) ? null : np};
  }
  if (!tickers && !stk) { document.getElementById('ckPreResult').innerHTML =
    '<div style="color:#8b949e;padding:20px;text-align:center">输入想买的 ticker(或别名 MAG7)</div>'; return; }
  document.getElementById('ckPreResult').innerHTML =
    '<div style="padding:30px;text-align:center;color:#8b949e">体检中…(算相关性 + 调你的 journal)</div>';
  document.getElementById('ckRecResult').innerHTML =
    '<div style="padding:14px;text-align:center;color:#8b949e;font-size:12px">🎯 找最优 Bull Call 价差中…(拉链 + 比 5-8 单)</div>';
  try {
    const res = await fetch('/api/checkup/pretrade', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({tickers: tickers || stk, proposed_spread: proposed})}).then(x => x.json());
    renderCheckupResult(res);
    const prim = (res && res.primary) || ((tickers || stk).split(/[\s,]+/)[0] || '').toUpperCase();
    loadRecommendations(prim);
  } catch (e) {
    document.getElementById('ckPreResult').innerHTML = '<div style="color:#f85149;padding:16px">出错: ' + e + '</div>';
    document.getElementById('ckRecResult').innerHTML = '';
  }
}

let _ckRecs = [];
let _ckRecTicker = '';

async function loadRecommendations(ticker) {
  const el = document.getElementById('ckRecResult');
  if (!el) return;
  if (!ticker || /^(MAG7|FAANG|FANG|MAG|SEMIS|INDEX)$/i.test(ticker)) { el.innerHTML = ''; return; }
  _ckRecTicker = ticker.toUpperCase();
  try {
    const res = await fetch('/api/checkup/recommend/' + encodeURIComponent(_ckRecTicker)).then(x => x.json());
    renderRecommendations(res);
  } catch (e) { el.innerHTML = ''; }
}

function renderRecommendations(res) {
  const el = document.getElementById('ckRecResult');
  if (!el) return;
  const recs = (res && res.recommendations) || [];
  _ckRecs = recs;
  if (!recs.length) {
    el.innerHTML = (res && res.spot)
      ? `<div style="padding:12px;text-align:center;color:#6e7681;font-size:12px">🎯 ${res.ticker}:35-45DTE 内没找到够流动的 Bull Call 候选</div>`
      : '';
    return;
  }
  const spot = res.spot;
  let h = `<div style="border:1px solid #1f6feb;border-radius:10px;overflow:hidden;margin-bottom:14px">
    <div style="background:#161b22;padding:9px 12px;font-size:13px;font-weight:700;color:#e6edf3">🎯 自动推荐 Bull Call <span style="font-weight:400;color:#6e7681;font-size:11px">· ${res.ticker} $${spot?(+spot).toFixed(2):'-'} · 35-45DTE · regime 自适应行权价 · 排了 ${recs.length} 单</span></div>
    ${(res.strike&&res.strike.label)?`<div style="margin:6px 8px;padding:8px 10px;border-radius:8px;background:${res.strike.mode==='otm'?'rgba(255,159,10,.08)':'rgba(63,185,80,.08)'};border:1px solid ${res.strike.mode==='otm'?'#ff9f0a':'#3fb950'}66">
      <div style="font-size:12px;font-weight:700;color:${res.strike.mode==='otm'?'#ff9f0a':'#3fb950'}">📐 ${res.strike.label}${res.vix!=null?` <span style="color:#8b949e;font-weight:400">· VIX ${res.vix}</span>`:''}</div>
      <div style="font-size:11px;color:#8b949e;margin-top:3px">${res.strike.detail||''}${res.strike.be_hint?(' · '+res.strike.be_hint):''}</div></div>`:''}
    <div style="padding:4px 0">`;
  recs.forEach((c, i) => {
    const top = c.top;
    const scoreCol = c.score >= 80 ? '#3fb950' : c.score >= 60 ? '#ff9f0a' : '#8b949e';
    const execCol = c.scores.exec >= 80 ? '#3fb950' : c.scores.exec >= 50 ? '#ff9f0a' : '#f85149';
    const oi = c.oi_min >= 1000 ? (c.oi_min / 1000).toFixed(1) + 'k' : c.oi_min;
    const be = c.be_dist_pct == null ? '-' : (c.be_dist_pct > 0 ? '+' : '') + c.be_dist_pct + '%';
    h += `<div onclick="ckUseRec(${i})" style="display:flex;align-items:center;gap:10px;padding:8px 12px;margin:3px 8px;border:1px solid ${top ? '#1f6feb' : '#30363d'};border-radius:8px;cursor:pointer;background:${top ? 'rgba(31,111,235,.08)' : '#0d1117'}">
      <div style="font-size:17px;font-weight:800;color:${scoreCol};width:32px;text-align:center">${c.score}</div>
      <div style="flex:1;min-width:0">
        <div style="color:#e6edf3;font-size:13px;font-weight:600">${top ? '⭐ ' : ''}${c.long}/${c.short} C · ${c.dte}d${c.earnings_in_window ? ' <span style="color:#f85149">⚠️财报</span>' : ''}</div>
        <div style="color:#8b949e;font-size:11px;margin-top:2px">${c.reason}</div>
      </div>
      <div style="text-align:right;font-size:11px;white-space:nowrap;color:#8b949e">
        <div>付 <b style="color:#e6edf3">$${(+c.debit).toFixed(2)}</b> · RR <b style="color:${c.rr >= 1.3 ? '#3fb950' : '#e6edf3'}">${(+c.rr).toFixed(1)}</b></div>
        <div>成交<span style="color:${execCol}">${c.scores.exec}</span> · OI ${oi} · B/E ${be}</div>
      </div></div>`;
  });
  h += `</div><div style="padding:5px 12px 9px;color:#6e7681;font-size:10px">分=成交35%+流动30%+赔率20%+契合15% · 点一行回填下方做完整价格体检</div></div>`;
  el.innerHTML = h;
}

async function ckUseRec(i) {
  const c = _ckRecs[i]; if (!c) return;
  if (!_ckState.spreadOpen) toggleCkSpread();
  document.getElementById('ckSpreadTicker').value = _ckRecTicker;
  document.getElementById('ckType').value = 'C';
  await ckLoadChain();
  const expEl = document.getElementById('ckExp');
  if (expEl) expEl.value = c.exp;
  await ckLoadStrikes();
  const setSel = (id, val) => {
    const s = document.getElementById(id); if (!s) return;
    const opt = Array.from(s.options).find(o => Math.abs(parseFloat(o.value) - val) < 1e-6);
    if (opt) s.value = opt.value;
  };
  setSel('ckLong', c.long);
  setSel('ckShort', c.short);
  const hdr = document.getElementById('ckSpreadHdr');
  if (hdr) hdr.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function _ckCorrColor(p) { return p>=0.7?'#f85149':p>=0.5?'#ff9f0a':p>=0.3?'#d29922':'#3fb950'; }

// ═══════════════ 流动性体检(全屏:逐行权价网格 + 跨周对比)═══════════════
let _liqData = null, _liqSide = 'call';

function _liqFmtStrike(s) { if (s == null) return '—'; return (+s).toFixed(2).replace(/\.?0+$/, ''); }

function _liqTierStyle(t) {
  if (t === 'good') return { bg: 'rgba(63,185,80,.16)', fg: '#3fb950', bd: 'rgba(63,185,80,.4)' };
  if (t === 'warn') return { bg: 'rgba(210,153,34,.18)', fg: '#e3b341', bd: 'rgba(210,153,34,.45)' };
  if (t === 'bad')  return { bg: 'rgba(248,81,73,.16)', fg: '#f85149', bd: 'rgba(248,81,73,.4)' };
  return { bg: 'transparent', fg: '#6e7681', bd: '#30363d' };
}

function openLiquidity(tk) {
  if (!tk) {
    const raw = (document.getElementById('ckTickers') || {}).value || '';
    tk = (raw.trim().split(/\s+/)[0] || '').toUpperCase();
  }
  if (!tk || /^(MAG7|FAANG|FANG|MAG|SEMIS|INDEX)$/i.test(tk)) {
    tk = (prompt('输入单个 ticker 做流动性扫描:') || '').trim().toUpperCase();
  }
  if (!tk) return;
  const o = document.getElementById('liquidityOverlay'); if (o) o.style.display = 'block';
  const inp = document.getElementById('liqTicker'); if (inp) inp.value = tk;
  if (typeof appNavInto === 'function') appNavInto();
  loadLiquidity(tk);
}

function closeLiquidity() { const o = document.getElementById('liquidityOverlay'); if (o) o.style.display = 'none'; }

async function loadLiquidity(tk) {
  tk = (tk || (document.getElementById('liqTicker') || {}).value || '').trim().toUpperCase();
  if (!tk) return;
  const body = document.getElementById('liqBody'); if (!body) return;
  body.innerHTML = '<div style="padding:44px;text-align:center;color:#8b949e">扫描期权链 + 拉报价中…</div>';
  try {
    const d = await fetch('/api/checkup/liquidity/' + encodeURIComponent(tk)).then(x => x.json());
    if (d && d.error) { body.innerHTML = '<div style="padding:30px;color:#f85149;text-align:center">出错: ' + d.error + '</div>'; return; }
    _liqData = d; _liqSide = 'call';
    _liqRender();
  } catch (e) { body.innerHTML = '<div style="padding:30px;color:#f85149;text-align:center">请求失败: ' + e + '</div>'; }
}

function liqSwitchSide(s) { _liqSide = s; _liqRender(); }

function _liqSyncSideBtns() {
  const on = 'background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer';
  const off = 'background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer';
  const c = document.getElementById('liqSideCall'), p = document.getElementById('liqSidePut');
  if (c) c.style.cssText = (_liqSide === 'call') ? on : off;
  if (p) p.style.cssText = (_liqSide === 'put') ? on : off;
}

function _liqBanner(d) {
  const tv = d.ticker_verdict || {};
  const m = { good: { c: '#3fb950', bg: 'rgba(63,185,80,.10)', ic: '🟢' },
              warn: { c: '#e3b341', bg: 'rgba(210,153,34,.10)', ic: '🟡' },
              bad:  { c: '#f85149', bg: 'rgba(248,81,73,.10)', ic: '🔴' },
              unknown: { c: '#8b949e', bg: '#161b22', ic: '⚪' } }[tv.tier] || { c: '#8b949e', bg: '#161b22', ic: '⚪' };
  const note = (d.market_hours === false)
    ? '<div style="color:#d29922;font-size:11px;margin-top:5px">⚠️ 盘后报价常单边,部分 🔴 可能只是收盘后没双边报价,以开盘为准</div>' : '';
  return '<div style="border:1px solid ' + m.c + '66;background:' + m.bg + ';border-radius:10px;padding:12px 14px;margin-bottom:12px">'
    + '<div style="font-size:15px;font-weight:700;color:' + m.c + '">' + m.ic + ' ' + (tv.label || '—')
    + (d.earnings_date ? ' <span style="color:#f85149;font-weight:400;font-size:12px">· 财报 ' + d.earnings_date + '</span>' : '') + '</div>'
    + '<div style="font-size:12px;color:#8b949e;margin-top:3px">' + (tv.detail || '') + '</div>' + note + '</div>';
}

function _liqGrid(d) {
  const g = d[_liqSide === 'call' ? 'calls' : 'puts'];
  const cols = d.cols || [];
  if (!g || !g.strikes || !g.strikes.length || !cols.length)
    return '<div style="color:#6e7681;padding:24px;text-align:center">没有窗口内(ATM-10%~+15%、≤50天)的行权价数据</div>';
  let h = '<div style="overflow-x:auto;padding-bottom:4px"><table style="border-collapse:separate;border-spacing:3px;font-size:12px">';
  h += '<tr><th style="position:sticky;left:0;background:#0d1117;z-index:2;text-align:right;padding:4px 8px;color:#8b949e;font-weight:600">行权价</th>';
  cols.forEach(c => {
    const wk = c.type && /week/i.test(c.type);
    h += '<th style="padding:3px 8px;text-align:center;color:#c9d1d9;font-weight:600;min-width:58px">' + c.date.slice(5)
      + '<div style="font-size:10px;color:' + (wk ? '#d29922' : '#6e7681') + ';font-weight:400">' + c.dte + 'd' + (wk ? '·周' : '') + '</div></th>';
  });
  h += '</tr>';
  g.strikes.forEach((s, ri) => {
    const atm = (s === d.atm_strike);
    h += '<tr><td style="position:sticky;left:0;background:#0d1117;z-index:1;text-align:right;padding:4px 8px;white-space:nowrap;color:' + (atm ? '#58a6ff' : '#c9d1d9') + ';font-weight:' + (atm ? 700 : 400) + '">' + _liqFmtStrike(s) + (atm ? ' ◀' : '') + '</td>';
    const cells = g.cells[ri] || [], best = g.best_col[ri];
    cells.forEach((cell, ci) => {
      if (!cell) { h += '<td style="text-align:center;color:#21262d">·</td>'; return; }
      const st = _liqTierStyle(cell.tier);
      const txt = (cell.spread_pct == null) ? '单边' : Math.round(cell.spread_pct) + '%';
      const star = (ci === best) ? '★' : '';
      const tip = 'bid ' + cell.bid + ' / ask ' + cell.ask + ' · mid ' + cell.mid + ' · vol ' + cell.vol + ' · OI ' + cell.oi
        + (cell.reasons && cell.reasons.length ? ' · ' + cell.reasons.join(' · ') : '');
      h += '<td onclick="liqCell(' + ri + ',' + ci + ')" title="' + tip.replace(/"/g, '&quot;') + '" style="cursor:pointer;text-align:center;background:' + st.bg + ';color:' + st.fg + ';border:1px solid ' + st.bd + ';border-radius:5px;padding:4px 6px;min-width:50px;font-weight:' + (star ? 700 : 400) + '">' + star + txt + '</td>';
    });
    h += '</tr>';
  });
  h += '</table></div>';
  return h;
}

function liqCell(ri, ci) {
  const d = _liqData; if (!d) return;
  const g = d[_liqSide === 'call' ? 'calls' : 'puts'];
  const cell = (g.cells[ri] || [])[ci]; if (!cell) return;
  const s = g.strikes[ri], col = (d.cols || [])[ci] || {}, st = _liqTierStyle(cell.tier);
  const wrap = document.getElementById('liqDetailWrap'); if (!wrap) return;
  let bar = '';
  if (cell.bid > 0 && cell.ask > 0) {
    bar = '<div style="margin-top:8px"><div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e"><span>bid $' + cell.bid + '</span><span>mid $' + cell.mid + '</span><span>ask $' + cell.ask + '</span></div>'
      + '<div style="position:relative;height:8px;background:#21262d;border-radius:4px;margin-top:3px;overflow:hidden"><div style="position:absolute;inset:0;background:linear-gradient(90deg,#3fb950,#e3b341,#f85149);opacity:.3"></div><div style="position:absolute;left:50%;top:-3px;width:2px;height:14px;background:#58a6ff"></div></div>'
      + '<div style="font-size:11px;color:#6e7681;margin-top:5px">💡 别直接挂 ask:从 mid $' + cell.mid + ' 往 bid $' + cell.bid + ' 方向试,常能成交在 mid 下方</div></div>';
  }
  wrap.innerHTML = '<div style="border:1px solid ' + st.bd + ';background:#161b22;border-radius:8px;padding:10px 12px;margin-bottom:10px">'
    + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span style="font-weight:700;color:' + st.fg + '">' + (_liqSide === 'call' ? 'Call' : 'Put') + ' ' + _liqFmtStrike(s) + ' · ' + (col.date || '') + (col.dte != null ? ' (' + col.dte + 'd)' : '') + '</span>'
    + '<span style="font-size:11px;color:#8b949e">spread ' + (cell.spread_pct == null ? '单边报价' : Math.round(cell.spread_pct) + '% ($' + (cell.spread_abs != null ? cell.spread_abs.toFixed(2) : '-') + ')') + ' · vol ' + cell.vol + ' · OI ' + cell.oi + (cell.iv ? ' · IV ' + cell.iv + '%' : '') + '</span></div>'
    + (cell.reasons && cell.reasons.length ? '<div style="font-size:11px;color:#e3b341;margin-top:4px">⚠️ ' + cell.reasons.join(' · ') + '</div>' : '')
    + bar + '</div>';
  wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _liqCrossWeek(d) {
  const cw = d.cross_week;
  if (!cw || !cw.rows || !cw.rows.length) return '';
  const cell = (v, color, b) => '<td style="text-align:center;padding:2px 6px;color:' + color + ';font-weight:' + (b ? 700 : 400) + '">' + v + (b ? ' ★' : '') + '</td>';
  let h = '<div style="border:1px solid #30363d;border-radius:10px;margin-top:16px;overflow:hidden">'
    + '<div style="background:#161b22;padding:9px 12px;font-size:13px;font-weight:600;color:#e6edf3">📊 同一价差跨周对比 <span style="color:#6e7681;font-weight:400;font-size:11px">· ' + _liqFmtStrike(cw.long) + '/+' + cw.width_pct + '% Call Spread · 越低越对、net 越紧越好挂,★=最便宜的周</span></div>'
    + '<div style="overflow-x:auto;padding:10px 12px"><table style="border-collapse:collapse;font-size:12px"><tr><td style="color:#8b949e;padding:2px 6px">到期</td>';
  cw.rows.forEach(r => { h += '<td style="text-align:center;padding:2px 6px;color:#c9d1d9">' + r.date.slice(5) + '<div style="font-size:10px;color:#6e7681">' + r.dte + 'd</div></td>'; });
  h += '</tr><tr><td style="color:#8b949e;padding:2px 6px">腿</td>';
  cw.rows.forEach(r => { h += '<td style="text-align:center;padding:2px 6px;color:#8b949e;font-size:11px">' + (r.long && r.short ? _liqFmtStrike(r.long) + '/' + _liqFmtStrike(r.short) : '—') + '</td>'; });
  h += '</tr><tr><td style="color:#8b949e;padding:2px 6px">净付 debit</td>';
  cw.rows.forEach(r => { h += cell(r.debit == null ? '—' : '$' + r.debit.toFixed(2), r.debit == null ? '#6e7681' : (r.best ? '#3fb950' : '#e6edf3'), r.best); });
  h += '</tr><tr><td style="color:#8b949e;padding:2px 6px">net 买卖差</td>';
  cw.rows.forEach(r => { h += '<td style="text-align:center;padding:2px 6px;color:#8b949e">' + (r.net_width == null ? '—' : '$' + r.net_width.toFixed(2)) + '</td>'; });
  h += '</tr></table></div></div>';
  return h;
}

function _liqRender() {
  const d = _liqData, body = document.getElementById('liqBody');
  if (!d || !body) return;
  _liqSyncSideBtns();
  const sp = document.getElementById('liqSpot');
  if (sp) sp.textContent = (d.ticker || '') + (d.spot ? ' $' + d.spot : '') + (d.atm_strike ? '  ATM ' + _liqFmtStrike(d.atm_strike) : '');
  body.innerHTML = _liqBanner(d)
    + '<div style="font-size:11px;color:#6e7681;margin-bottom:8px">🟢 好成交 · 🟡 偏(宽/OI薄/低量) · 🔴 差(单边/极宽,mid难成交) · ★ 该行最佳成交周 · 点格子看真实 bid/ask 区间</div>'
    + '<div id="liqDetailWrap"></div>'
    + _liqGrid(d)
    + _liqCrossWeek(d);
}

function renderCheckupResult(res) {
  const el = document.getElementById('ckPreResult');
  if (!res || res.error) { el.innerHTML = `<div style="color:#f85149;padding:16px">出错: ${(res && res.error) || '未知'}</div>`; return; }
  let h = '';
  const hv = res.hard_violations || [];
  if (hv.length) {
    h += `<div style="background:rgba(248,81,73,.1);border:1px solid #f85149;border-radius:8px;padding:11px 13px;margin-bottom:12px">
      <div style="color:#f85149;font-weight:700;font-size:13px;margin-bottom:5px">🚨 硬违规 ${hv.length} 条 — 强烈建议别开</div>` +
      hv.map(f => `<div style="font-size:12px;color:#e6edf3;margin-top:3px">${f.icon} <b>${f.label}</b> — ${f.detail}</div>`).join('') + `</div>`;
  }
  const c = res.concentration || {};
  if (c.headline) {
    const lc = c.level === 'red' ? '#f85149' : c.level === 'warn' ? '#ff9f0a' : '#3fb950';
    h += `<div style="background:#161b22;border:1px solid #30363d;border-left:3px solid ${lc};border-radius:8px;padding:11px 13px;margin-bottom:12px">
      <div style="color:${lc};font-weight:700;font-size:13px">集中度 · ${c.headline}</div>
      <div style="color:#8b949e;font-size:11px;margin-top:4px">加权相关 ${c.weighted_pearson!=null?c.weighted_pearson:'-'} · 候选对SPY beta ${c.candidate_beta!=null?c.candidate_beta:'-'}${c.candidate_internal!=null?(' · 候选彼此相关 '+c.candidate_internal+'(买全套=同一注)'):''}</div></div>`;
  }
  const cm = res.comovement || {}; const book = cm.book || []; const rows = cm.rows || [];
  h += `<div style="margin-bottom:6px;color:#58a6ff;font-weight:600;font-size:13px">① 联动度(候选 × 你的持仓,Pearson;悬停看下尾相关)</div>`;
  if (rows.length && book.length) {
    h += `<div style="overflow-x:auto;margin-bottom:12px"><table style="border-collapse:collapse;font-size:11px;width:100%">
      <thead><tr><th style="text-align:left;padding:4px 8px;color:#8b949e">候选↓ 持仓→</th>` +
      book.map(b => `<th style="padding:4px 6px;color:#8b949e">${b}</th>`).join('') +
      `<th style="padding:4px 6px;color:#8b949e">βSPY</th></tr></thead><tbody>`;
    rows.forEach(r => {
      if (!r.available) { h += `<tr><td style="padding:4px 8px;color:#8b949e">${r.candidate}</td><td colspan="${book.length+1}" style="color:#6e7681;padding:4px 8px">无历史数据</td></tr>`; return; }
      const byT = {}; (r.vs_book || []).forEach(v => byT[v.ticker] = v);
      h += `<tr><td style="padding:4px 8px;color:#e6edf3;font-weight:600">${r.candidate}</td>` +
        book.map(b => { const v = byT[b];
          if (!v || v.pearson == null) return `<td style="padding:4px 6px;text-align:center;color:#6e7681">-</td>`;
          return `<td style="padding:4px 6px;text-align:center;color:${_ckCorrColor(v.pearson)};font-weight:600;cursor:help" data-tip="下尾相关(一起暴跌概率) ${v.tail!=null?v.tail:'-'}">${v.pearson.toFixed(2)}</td>`;
        }).join('') +
        `<td style="padding:4px 6px;text-align:center;color:#8b949e">${r.beta!=null?r.beta:'-'}</td></tr>`;
    });
    h += `</tbody></table></div>`;
  } else {
    h += `<div style="color:#8b949e;font-size:12px;margin-bottom:12px">${book.length?'候选无历史数据':'你当前没有持仓可对比(只看候选自身 beta / 彼此相关)'}</div>`;
  }
  const p = res.price;
  if (p && p.ok) {
    const sc = p.slippage_pct>=10?'#f85149':p.slippage_pct>=5?'#ff9f0a':'#3fb950';
    const rc = (p.rr!=null && p.rr<1.5)?'#ff9f0a':'#3fb950';
    const card = (x) => `<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 12px">${x}</div>`;
    h += `<div style="margin-bottom:6px;color:#58a6ff;font-weight:600;font-size:13px">③ 价格合理性</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;font-size:12px;color:#c9d1d9">` +
      card(`理论中间价 <b style="color:#e6edf3">$${p.theo_mid!=null?p.theo_mid:'-'}</b>`) +
      card(`你付 <b style="color:#e6edf3">$${p.net_price}</b> · 滑点 <b style="color:${sc}">${p.slippage_pct!=null?(p.slippage_pct>=0?'+':'')+p.slippage_pct+'%':'-'}</b>`) +
      card(`赔率 R:R <b style="color:${rc}">${p.rr!=null?p.rr:'-'}</b>`) +
      card(`max盈 <b style="color:#3fb950">$${p.max_profit}</b> / max亏 <b style="color:#f85149">$${p.max_loss}</b>`) +
      card(`BE离现价 ${p.dist_be_pct!=null?p.dist_be_pct+'%':'-'} (${p.otm_sigma!=null?p.otm_sigma+'σ':'-'})`) +
      `</div>`;
  }
  const flags = res.mirror || [];
  const auto = flags.filter(f => f.severity !== 'selfcheck');
  const self = flags.filter(f => f.severity === 'selfcheck');
  h += `<div style="margin-bottom:6px;color:#58a6ff;font-weight:600;font-size:13px">② 错误镜子</div>`;
  if (auto.length) {
    h += auto.map(f => { const col = f.severity === 'red' ? '#f85149' : '#ff9f0a';
      return `<div style="display:flex;gap:8px;padding:8px 10px;border:1px solid #30363d;border-left:3px solid ${col};border-radius:6px;margin-bottom:5px;background:#161b22">
        <span>${f.icon}</span><div style="flex:1"><span style="color:${col};font-weight:600;font-size:12px">${f.label}</span>
        <div style="color:#c9d1d9;font-size:11px;margin-top:1px">${f.detail}</div>${f.hist?`<div style="color:#8b949e;font-size:10px;margin-top:2px">〔${f.hist}〕</div>`:''}</div></div>`;
    }).join('');
  } else {
    h += `<div style="color:#3fb950;font-size:12px;padding:4px 0 8px">✅ 自动检查没发现硬伤</div>`;
  }
  if (self.length) {
    h += `<div style="margin:10px 0 6px;color:#8b949e;font-weight:600;font-size:12px">开单前自检(把你的历史代价怼出来)</div>`;
    h += self.map(f => `<div style="display:flex;gap:8px;padding:5px 10px;font-size:11px;color:#8b949e">
      <span>${f.icon}</span><div style="flex:1"><b style="color:#c9d1d9">${f.label}</b> ${f.detail}${f.hist?`<span style="color:#f85149"> 〔${f.hist}〕</span>`:''}</div></div>`).join('');
  }
  el.innerHTML = h;
}

function renderExitAdvisor() {
  const el = document.getElementById('ckPosResult'); if (!el) return;
  const strats = (typeof RAW_DATA !== 'undefined' && RAW_DATA.strategies) || [];
  if (!strats.length) { el.innerHTML = '<div style="padding:30px;text-align:center;color:#8b949e">暂无持仓</div>'; return; }
  const rows = strats.map((s, i) => {
    const a = analyzeStrategy(s);
    const be = a.breakevens || []; let bePct = null;
    if (be.length && s.current_price > 0) {
      let c = be[0]; be.forEach(b => { if (Math.abs(b - s.current_price) < Math.abs(c - s.current_price)) c = b; });
      bePct = (c - s.current_price) / s.current_price * 100;
    }
    const ev = ckExitVerdict(a.unrealizedPnl, a.maxProfit, s.dte, a.unrealizedPct);
    const strikes = s.legs.map(l => l.strike).sort((x, y) => x - y).join('/');
    const risk = (a.maxLoss == null || a.maxLoss <= -1e9) ? 0 : Math.abs(a.maxLoss);
    // 进度 = 盈利占【最大利润】% / 亏损占【最大亏损】%(与 ckExitVerdict 止盈判定、主表 Progress 列同口径)。
    // 止盈看这个,不是 total return(浮盈/净权利金)—— 后者会高估离封顶的距离、容易提前喊止盈。
    const pg = _dashProg({pnl: a.unrealizedPnl, maxProfit: a.maxProfit, maxLoss: a.maxLoss});
    return {i, ev, risk, name: `${s.underlying} ${strikes} ${s.name}`, pct: a.unrealizedPct,
            progPct: pg.avail ? pg.pct : null, dte: s.dte, cop: a.cop, bePct};
  });
  const order = {cut:0, near:1, hold:2, tp:3};
  rows.sort((x, y) => (order[x.ev.code] - order[y.ev.code]) || ((x.cop||999) - (y.cop||999)));
  const cnt = {cut:0, near:0, tp:0, hold:0}; rows.forEach(r => cnt[r.ev.code]++);
  const sevColor = {bad:'#f85149', warn:'#ff9f0a', good:'#3fb950', ok:'#8b949e'};
  // ── 交易规律卡 + 当前账户体检(纯展示,无 alert;口径=30万票×3年开法回测 + 组合 MC 仓位实验)──
  const _NLV = liveNLV();
  const _deployed = rows.reduce((s, r) => s + r.risk, 0);
  const _largest = rows.reduce((m, r) => Math.max(m, r.risk), 0);
  const _depPct = _NLV > 0 ? _deployed / _NLV * 100 : null;
  const _bigPct = _NLV > 0 ? _largest / _NLV * 100 : null;
  const _col = (v, g, y) => v == null ? '#8b949e' : (v <= g ? '#3fb950' : (v <= y ? '#ff9f0a' : '#f85149'));
  const _pj = v => v == null ? '—' : v.toFixed(0) + '%';
  const _perLo = _NLV > 0 ? '$' + Math.round(_NLV * 0.03).toLocaleString() : '—';
  const _perHi = _NLV > 0 ? '$' + Math.round(_NLV * 0.05).toLocaleString() : '';
  let h = `<div style="border:1px solid #30363d;border-radius:10px;overflow:hidden;margin-bottom:14px">
      <div style="background:#161b22;padding:8px 12px;font-size:12px;font-weight:700;color:#e6edf3">📋 交易规律 <span style="font-weight:400;color:#6e7681;font-size:11px">· 30万票×3年开法 + 组合 MC 仓位回测验证</span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;font-size:11.5px">
        <div style="padding:10px 12px;border-right:1px solid #21262d;line-height:1.8">
          <div><b style="color:#58a6ff">开法</b> ATM · 35-45 DTE · 10%宽<br><span style="color:#6e7681">避深 ITM(资金低效)/远 OTM(彩票单)</span></div>
          <div style="margin-top:7px"><b style="color:#58a6ff">出场</b> 90%最大利润止盈 · <b style="color:#3fb950">不止损</b> · 否则持到期</div>
        </div>
        <div style="padding:10px 12px;line-height:1.8">
          <div><b style="color:#58a6ff">每单</b> 3-5% NLV (${_perLo}–${_perHi}) · 同时 ≤10-12 仓</div>
          <div style="margin-top:7px"><b style="color:#58a6ff">现金</b> 部署 ≤60%(顶 70%)、留 40%+ 现金<br><span style="color:#6e7681">现金即对冲,别买 put(回测:常年放血、不降回撤)</span></div>
        </div>
      </div>
      <div style="display:flex;border-top:1px solid #21262d;background:#0d1117;font-size:11px">
        <div style="flex:1;padding:8px 12px;text-align:center;border-right:1px solid #21262d">
          <div style="color:#6e7681">当前部署</div><div style="font-size:17px;font-weight:700;color:${_col(_depPct,60,70)}">${_pj(_depPct)}</div><div style="color:#6e7681;font-size:10px">目标 ≤60%</div></div>
        <div style="flex:1;padding:8px 12px;text-align:center;border-right:1px solid #21262d">
          <div style="color:#6e7681">单笔最大</div><div style="font-size:17px;font-weight:700;color:${_col(_bigPct,5,8)}">${_pj(_bigPct)}</div><div style="color:#6e7681;font-size:10px">目标 ≤5%</div></div>
        <div style="flex:1;padding:8px 12px;text-align:center">
          <div style="color:#6e7681">持仓数</div><div style="font-size:17px;font-weight:700;color:#e6edf3">${rows.length}</div><div style="color:#6e7681;font-size:10px">目标 10-12</div></div>
      </div>
    </div>`;
  h += `<div style="display:flex;gap:14px;margin-bottom:12px;font-size:12px;font-weight:600">
    <span style="color:#f85149">🔴 深亏 ${cnt.cut}</span><span style="color:#3fb950">🔵 止盈 ${cnt.tp}</span>
    <span style="color:#ff9f0a">🟡 接近 ${cnt.near}</span><span style="color:#8b949e">🟢 持有 ${cnt.hold}</span></div>`;
  h += rows.map(r => {
    const col = sevColor[r.ev.sev];
    return `<div onclick="closeCheckup();enterDetail(${r.i})" style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid #30363d;border-left:3px solid ${col};border-radius:8px;margin-bottom:6px;cursor:pointer;background:#161b22">
      <span style="font-size:18px">${r.ev.emoji}</span>
      <div style="flex:1;min-width:0">
        <div style="color:#e6edf3;font-size:13px;font-weight:600">${r.name}</div>
        <div style="color:#8b949e;font-size:11px;margin-top:2px">${r.ev.reason}</div>
      </div>
      <div style="text-align:right;font-size:11px;color:#8b949e;white-space:nowrap">
        <div title="盈利占【最大利润】% · 亏损占【最大亏损】%(与下方止盈判定同口径,不是 total return)" style="color:${(r.progPct!=null&&r.progPct<0)?'#f85149':'#3fb950'};font-weight:700">${r.progPct!=null ? ((r.progPct>=0?'+':'') + r.progPct.toFixed(0) + '% <span style="font-weight:400;color:#6e7681;font-size:10px">' + (r.progPct>=0?'最大利润':'最大亏损') + '</span>') : '<span style="color:#6e7681">最大利润 —</span>'}</div>
        <div>总收益 ${r.pct>=0?'+':''}${r.pct.toFixed(0)}% · COP ${r.cop!=null?r.cop.toFixed(0):'-'}% · ${Math.round(r.dte)}d</div>
      </div></div>`;
  }).join('');
  el.innerHTML = h;
}

function openAlertPanel() {
  appNavInto();
  loadAlertConfig();
  document.getElementById('alertEnabled').checked = _alertCfg.enabled;
  const _snd = document.getElementById('alertSound'); if (_snd) _snd.checked = !!_alertCfg.sound;
  // Exit rule inputs (the only take-profit; no SL / per-ticker overrides anymore)
  const _ex = _alertCfg.exit || EXIT_DEFAULTS;
  if (document.getElementById('alertExitEnabled')) {
    document.getElementById('alertExitEnabled').checked = _ex.enabled !== false;
    document.getElementById('alertExitBullPct').value = _ex.bullPct != null ? _ex.bullPct : 70;
    document.getElementById('alertExitChopPct').value = _ex.chopPct != null ? _ex.chopPct : 50;
    const _fr = (_ex.freq === 'always') ? 'always' : 'daily';
    document.querySelectorAll('input[name=alertExitFreq]').forEach(r => { r.checked = (r.value === _fr); });
  }
  const _csEl = document.getElementById('alertCreditEnabled');
  if (_csEl) _csEl.checked = !_alertCfg.creditSpread || _alertCfg.creditSpread.enabled !== false;
  // Update permission status
  const ps = document.getElementById('alertPermStatus');
  if ('Notification' in window) {
    ps.textContent = 'Permission: ' + Notification.permission;
  } else {
    ps.textContent = 'Notifications not supported';
  }
  document.getElementById('alertOverlay').style.display = 'block';
}

function closeAlertPanel() {
  document.getElementById('alertOverlay').style.display = 'none';
}

// (toggleCustomAlert removed — per-position SL/TP overrides retired with the
//  consolidation to the single regime take-profit guard.)

function requestNotifyPermission() {
  if ('Notification' in window) {
    Notification.requestPermission().then(p => {
      document.getElementById('alertPermStatus').textContent = 'Permission: ' + p;
    });
  }
}

function checkAlerts(strategies) {
  if (!_alertCfg.enabled) return;
  // Trading days only — no weekend alerts (markets closed, nothing actionable).
  const _d = new Date();
  if (_d.getDay() < 1 || _d.getDay() > 5) return;  // Sat/Sun → fully silent
  // At most ONE alert per ticker per type per calendar day (SL / TP / exit all
  // share this). `fired` resets each new day, so a position sitting past a
  // threshold pings once a day, not every few minutes.
  const today = _d.getFullYear() + '-' + (_d.getMonth() + 1) + '-' + _d.getDate();
  if (!_alertCfg.fired || _alertCfg.fired._day !== today) _alertCfg.fired = { _day: today };
  const once = (key, fn) => {
    if (_alertCfg.fired[key]) return;
    _alertCfg.fired[key] = 1;
    localStorage.setItem('alert_config', JSON.stringify(_alertCfg));
    fn();
  };
  // 频率门:'always' = 每次达成都提醒(每标的最快 1 分钟一次);否则每标的每天一次。
  const _now = Date.now();
  const gate = (key, fn) => {
    const freq = (_alertCfg.exit && _alertCfg.exit.freq) || 'daily';
    if (freq !== 'always') return once(key, fn);
    _alertCfg.lastFired = _alertCfg.lastFired || {};
    if (_now - (_alertCfg.lastFired[key] || 0) < 60000) return;   // 1-min 限流
    _alertCfg.lastFired[key] = _now;
    localStorage.setItem('alert_config', JSON.stringify(_alertCfg));
    fn();
  };
  strategies.forEach(s => {
    const tk = s.underlying;
    const a = analyzeStrategy(s);
    // Credit-spread direction mistake — meant a debit vertical, opened a credit one.
    const csOn = !_alertCfg.creditSpread || _alertCfg.creditSpread.enabled !== false;
    if (csOn && isCreditVertical(s)) {
      once(tk + '_credit', () => fireCreditSpreadAlert(tk, s.name));
    }
    // 唯一止盈:regime 自适应(牛 70 / 震荡 50 软档),进软档就提醒;无成本止损,借方价差不止损。
    const exCfg = _alertCfg.exit;
    const _mp = (a.maxProfit > 0 && a.maxProfit < 1e9) ? (a.unrealizedPnl / a.maxProfit) : null;
    if (exCfg && exCfg.enabled !== false && _mp != null && _mp >= _ckBand().soft) {
      gate(tk + '_exit', () => fireExitAlert(tk, _mp, a.dte));
    }
  });
}

// (fireAlert removed — cost-% Stop-Loss / Take-Profit pings retired; the regime
//  退出止盈警戒 / fireExitAlert is now the only position alert.)

// Big-red URGENT exit/take-profit alert — distinct from the standard SL/TP
// toast above. Separate function so the existing fireAlert path is untouched.
// 短促提示音(WebAudio,声音开关打开时才响)。
function _alertBeep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
    const ctx = new AC(); const o = ctx.createOscillator(); const g = ctx.createGain();
    o.type = 'sine'; o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    o.start(); o.stop(ctx.currentTime + 0.36);
  } catch (e) {}
}
function fireExitAlert(ticker, prog, dte) {
  const b = _ckBand(); const forcedPct = Math.round(b.forced * 100);
  const shortT = (dte != null && dte < (b.dte_gate || 20));
  const forced = (prog >= (shortT ? b.soft : b.forced));
  const act = forced ? '强制止盈' : '止盈';
  const title = (forced ? '🔴 ' : '🔵 ') + ticker + ' ' + act;
  const tail = forced ? (shortT ? '剩' + Math.round(dte) + '天时间不多 — 现在就平' : '现在就平、别再等')
                      : ('建议落袋;到 ' + forcedPct + '% 强制');
  const bodyTxt = '已吃到 ' + (prog * 100).toFixed(0) + '% 最大利润 · 还 ' + Math.round(dte) + ' 天 · ' + b.label + b.zone + '区 — ' + tail;
  // 系统级 macOS 通知现在【只】由后端 exit_alert_loop 发(osascript),无论 PWA
  // 前台 / 后台 / 完全关闭都会响 —— 后台标签的 JS 定时器被浏览器节流/挂起,客户端
  // 推送本就不可靠。这里只在你正盯着页面时弹个即时 in-page toast(前台便利),
  // 不再发原生通知,免得和后端那条重复。title/bodyTxt 仍用于下面的 toast。
  // In-page toast — large, red, glowing.
  const toasts = document.getElementById('alertToasts');
  if (toasts) {
    const toast = document.createElement('div');
    toast.style.cssText = 'padding:14px 18px;border-radius:10px;background:rgba(45,10,10,0.97);border:2px solid #ff3b30;backdrop-filter:blur(8px);box-shadow:0 0 24px rgba(255,59,48,0.55);animation:slideIn 0.3s ease;min-width:300px';
    toast.innerHTML = '<div style="font-weight:800;color:' + (forced ? '#ff3b30' : '#3fb950') + ';font-size:15px;letter-spacing:0.3px">' + title + '</div>' +
                      '<div style="color:#fff;font-size:13px;margin-top:4px;font-weight:600">' + bodyTxt + '</div>';
    toasts.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }
  // (sound intentionally removed — silent notifications)
}

// 秒出 tooltip:鼠标进入任何 [data-tip] 元素立即显示(无原生 title 的 ~1s 延迟)
document.addEventListener('mouseover', function (e) {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (!t) return;
  const tip = document.getElementById('ckTip'); if (!tip) return;
  tip.textContent = t.getAttribute('data-tip'); tip.style.display = 'block';
});
document.addEventListener('mousemove', function (e) {
  const tip = document.getElementById('ckTip');
  if (tip && tip.style.display === 'block') {
    tip.style.left = Math.min(e.clientX + 12, window.innerWidth - 350) + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  }
});
document.addEventListener('mouseout', function (e) {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (t) { const tip = document.getElementById('ckTip'); if (tip) tip.style.display = 'none'; }
});

// 概率止损告警 — COP 太低、数学上难翻盘(与亏损%无关)。红色,区别于止盈警戒。
// 客户端 once/天/票;PWA 开着时提醒(服务端关着也推是后续,需服务端 COP)。
function fireCutAlert(ticker, cop, dte, reason) {
  const title = '🔴 ' + ticker + ' 止损信号';
  const bodyTxt = reason || ('COP ' + (cop != null ? cop.toFixed(0) : '-') + '%,概率太小');
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification(title, { body: bodyTxt, icon: '/icon.svg', tag: ticker + '_CUT', requireInteraction: true }); } catch (e) {}
  }
  const toasts = document.getElementById('alertToasts');
  if (toasts) {
    const toast = document.createElement('div');
    toast.style.cssText = 'padding:14px 18px;border-radius:10px;background:rgba(45,10,10,0.97);border:2px solid #f85149;backdrop-filter:blur(8px);box-shadow:0 0 24px rgba(248,81,73,0.5);animation:slideIn 0.3s ease;min-width:300px';
    toast.innerHTML = '<div style="font-weight:800;color:#f85149;font-size:15px">🔴 ' + ticker + ' 止损信号</div>' +
                      '<div style="color:#fff;font-size:13px;margin-top:4px;font-weight:600">' + bodyTxt + '</div>';
    toasts.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }
}

// ── Credit-spread direction-mistake helpers ──
// The user routinely *means* a debit vertical (e.g. bull call spread) but opens
// a credit vertical (bear call spread) — net credit, wrong direction. Mirrors
// the server-side credit_spread_alert.is_credit_vertical so the badge/banner the
// PWA shows matches the native push the server fires.
function isCreditVertical(strat) {
  const legs = (strat && strat.legs) || [];
  if (legs.length !== 2) return false;
  const a = legs[0], b = legs[1];
  if ((a.type || '') !== (b.type || '')) return false;        // same option type
  if ((a.exp || '') !== (b.exp || '')) return false;          // same expiration
  const qa = +a.qty || 0, qb = +b.qty || 0;
  if (qa === 0 || qb === 0 || (qa > 0) === (qb > 0)) return false;  // one long, one short
  let nd = 0;
  legs.forEach(l => { nd += (+l.entry || 0) * (+l.qty || 0) * (+l.mult || 100); });
  return nd < 0;                                               // net credit received
}

// Toggle the red "opened as a credit spread" banner above the legs detail for
// the currently-selected strategy. Called from renderStrategy + renderPositionsLive.
function updateCreditSpreadBanner(strat) {
  const el = document.getElementById('creditSpreadBanner');
  if (!el) return;
  if (strat && isCreditVertical(strat)) {
    let credit = 0;
    strat.legs.forEach(l => { credit += (+l.entry || 0) * (+l.qty || 0) * (+l.mult || 100); });
    credit = Math.abs(credit);
    el.style.cssText = 'margin-bottom:10px;padding:10px 14px;border-radius:8px;background:rgba(255,159,10,0.10);border:1px solid #ff9f0a;color:#ffd60a;font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px';
    el.innerHTML = '⚠️ <span>这是一个 <b>Credit Spread</b>（净收 +$' + credit.toFixed(2) +
      '）— 如果你本想开 <b>Debit</b>，方向开反了，请检查！</span>';
    el.style.display = 'flex';
  } else {
    el.style.display = 'none';
  }
}

// In-page warning toast when a credit vertical is detected while the PWA is open
// (the server fires the OS-level push). Toast-only here to avoid a double OS
// notification. Mirrors fireExitAlert styling, amber instead of red.
function fireCreditSpreadAlert(ticker, name) {
  const bodyTxt = (name || '') + ' — 收了净权利金，方向可能开反了，去检查仓位';
  const toasts = document.getElementById('alertToasts');
  if (toasts) {
    const toast = document.createElement('div');
    toast.style.cssText = 'padding:14px 18px;border-radius:10px;background:rgba(50,35,5,0.97);border:2px solid #ff9f0a;backdrop-filter:blur(8px);box-shadow:0 0 24px rgba(255,159,10,0.5);animation:slideIn 0.3s ease;min-width:300px';
    toast.innerHTML = '<div style="font-weight:800;color:#ff9f0a;font-size:15px">⚠️ ' + ticker + ' 开成 Credit Spread?</div>' +
                      '<div style="color:#fff;font-size:13px;margin-top:4px;font-weight:600">' + bodyTxt + '</div>';
    toasts.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }
}

// Load config on startup
loadAlertConfig();

// ── Color helpers ──
function pnlColor(val, maxAbs) {
  if (maxAbs === 0) return 'rgba(30,35,42,0.6)';
  // Heat-map gradient — fades to dark grey near zero, saturated bright
  // green/red at the extremes (matches Strategy Builder's table). √ratio
  // gives perceptual easing so the middle range still gets visible color.
  const ratio = Math.min(1, Math.abs(val) / maxAbs);
  const e = Math.sqrt(ratio);
  if (val > 0) {
    const r = Math.round(20 + (63 - 20) * e);
    const g = Math.round(40 + (185 - 40) * e);
    const b = Math.round(30 + (80 - 30) * e);
    const a = (0.10 + 0.85 * e).toFixed(3);
    return `rgba(${r},${g},${b},${a})`;
  } else if (val < 0) {
    const r = Math.round(40 + (248 - 40) * e);
    const g = Math.round(20 + (81 - 20) * e);
    const b = Math.round(20 + (73 - 20) * e);
    const a = (0.10 + 0.85 * e).toFixed(3);
    return `rgba(${r},${g},${b},${a})`;
  }
  return 'rgba(30,35,42,0.6)';
}

// ── Table rendering ──
function renderTable(strat, analysis, iv) {
  document.getElementById('tableView').style.display = '';
  document.getElementById('graphView').style.display = 'none';
  document.getElementById('dateSliderWrap').style.display = 'none';

  const matrix = computePLMatrix(strat, state.rangePct, iv);
  const table = document.getElementById('plTable');
  const netDebit = Math.abs(analysis.netDebit);

  // Find max absolute value for coloring
  let maxAbs = 0;
  matrix.values.forEach(row => row.forEach(v => { if (Math.abs(v) > maxAbs) maxAbs = Math.abs(v); }));

  // Group dates by month
  let html = '<thead>';

  // Month header row
  html += '<tr><th class="price-col" rowspan="2" style="vertical-align:bottom">Price</th><th class="pct-col" rowspan="2" style="vertical-align:bottom">%</th>';
  let prevMonth = '';
  let monthSpans = [];
  let currentSpan = {month: '', count: 0};
  matrix.monthLabels.forEach((m, i) => {
    if (m !== currentSpan.month) {
      if (currentSpan.count > 0) monthSpans.push({...currentSpan});
      currentSpan = {month: m, count: 1};
    } else {
      currentSpan.count++;
    }
  });
  if (currentSpan.count > 0) monthSpans.push(currentSpan);
  monthSpans.forEach(ms => {
    html += `<th class="month-header" colspan="${ms.count}">${ms.month}</th>`;
  });
  html += '</tr>';

  // Date header row
  html += '<tr>';
  matrix.dateLabels.forEach((dl, i) => {
    const isEarnings = i === matrix.earningsColIdx;
    html += `<th${isEarnings ? ' style="color:var(--yellow)"' : ''}>${dl}${isEarnings ? ' &#128227;' : ''}</th>`;
  });
  html += '</tr></thead><tbody>';

  // Data rows
  const S = strat.current_price;
  const expDate = nyClose(strat.expiration);
  const totalDTE_exact2 = Math.max(0.01, (expDate - new Date()) / 86400000);
  const totalDTE = Math.max(1, Math.round(totalDTE_exact2));

  // Build custom price row values if user entered a price
  let customRow = null;
  const cp = state.customPrice;
  if (cp && cp > 0) {
    const T_t = totalDTE_exact2 / 365;
    const legAdj2 = strat.legs.map(l => {
      const bsmT = bsmPrice(S, l.strike, T_t, strat.risk_free_rate, iv, l.type);
      return (l.mid || l.current || bsmT) - bsmT;
    });
    const customVals = [];
    for (const dt of matrix.dates) {
      const dte2 = Math.max(0, (expDate - dt) / 86400000);
      const T2 = dte2 / 365;
      const decay2 = totalDTE_exact2 > 0 ? Math.min(1, dte2 / totalDTE_exact2) : 0;
      let pnl2 = 0;
      strat.legs.forEach((l, li) => {
        const tp = bsmPrice(cp, l.strike, T2, strat.risk_free_rate, iv, l.type);
        pnl2 += (tp + legAdj2[li] * decay2 - l.entry) * l.qty * l.mult;
      });
      customVals.push(Math.round((pnl2 - (matrix.tableFees || 0)) * 100) / 100);
    }
    customRow = {price: cp, vals: customVals};
  }

  let customInserted = false;

  // Strike-style price formatter: integer when whole, else trim trailing 0s.
  function _fmtPx(p) {
    if (Math.abs(p - Math.round(p)) < 1e-9) return String(Math.round(p));
    return (+p).toFixed(2).replace(/\.?0+$/, '');
  }

  function renderRow(price, vals, isCurrent, isCustom) {
    const pctVal = (price - S) / S * 100;
    const pct = (pctVal >= 0 ? '+' : '') + pctVal.toFixed(1);
    const cls = isCustom ? 'custom-price-row' : (isCurrent ? 'current-price-row' : '');
    let rh = `<tr${cls ? ` class="${cls}"` : ''}${isCustom ? ' id="customPriceRow"' : ''}>`;
    rh += `<td class="price-col" style="${isCustom?'color:#f0a500;font-weight:700':''}">`;
    rh += `${isCurrent ? '&#9654; ' : ''}$${_fmtPx(price)}</td>`;
    rh += `<td class="pct-col" style="${isCustom?'color:#f0a500':''}">${pct}%</td>`;
    vals.forEach((val, di) => {
      let dv;
      if (state.valueMode === 'pnl') dv = Math.round(val).toLocaleString();
      else if (state.valueMode === 'pnlpct') dv = netDebit > 0 ? (val/netDebit*100).toFixed(0)+'%' : 'N/A';
      else if (state.valueMode === 'value') dv = (val+netDebit).toFixed(0);
      else { const ml = Math.abs(analysis.maxLoss); dv = ml > 0 ? (val/ml*100).toFixed(0)+'%' : 'N/A'; }
      const isE = di === matrix.earningsColIdx;
      rh += `<td style="background:${pnlColor(val,maxAbs)};color:${Math.abs(val)<maxAbs*0.05?'var(--text2)':'#fff'}${isCustom?';font-weight:700':''}"${isE?' class="earnings-col"':''}>${dv}</td>`;
    });
    rh += '</tr>';
    return rh;
  }

  // Thin white horizontal indicator at the EXACT current spot price —
  // matches Strategy Builder's table. Slips between the two grid rows
  // that bracket the spot.
  const colCount = 2 + matrix.dates.length;
  const spotLine = `<tr class="sb-spot-line"><td colspan="${colCount}"></td></tr>`;
  let spotInserted = false;

  matrix.prices.forEach((price, pi) => {
    // Insert custom row in correct position (prices are descending)
    if (customRow && !customInserted && customRow.price > price) {
      html += renderRow(customRow.price, customRow.vals, false, true);
      customInserted = true;
    }
    if (!spotInserted && S > 0 && S > price) {
      html += spotLine;
      spotInserted = true;
    }

    const isCurrent = pi === matrix.currentIdx;
    html += renderRow(price, matrix.values[pi], isCurrent, false);

    // Also handle if custom price equals a grid price
    if (customRow && !customInserted && pi === matrix.prices.length - 1) {
      html += renderRow(customRow.price, customRow.vals, false, true);
      customInserted = true;
    }
    if (!spotInserted && pi === matrix.prices.length - 1) {
      html += spotLine;
      spotInserted = true;
    }
  });

  // Skip the old per-cell rendering — it's now in renderRow
  html += '</tbody>';
  table.innerHTML = html;

}

// ── Graph rendering ──
function lognormPDF(x, mu, sigma) {
  if (x <= 0) return 0;
  const d = (Math.log(x) - mu) / sigma;
  return Math.exp(-0.5 * d * d) / (x * sigma * Math.sqrt(2 * Math.PI));
}

function lognormCDF(x, mu, sigma) {
  if (x <= 0) return 0;
  return normCDF((Math.log(x) - mu) / sigma);
}

function renderGraph(strat, analysis, iv) {
  document.getElementById('tableView').style.display = 'none';
  document.getElementById('graphView').style.display = '';
  document.getElementById('dateSliderWrap').style.display = '';

  const matrix = computePLMatrix(strat, state.rangePct, iv);
  const canvas = document.getElementById('plChart');
  const ctx = canvas.getContext('2d');
  const netDebit = Math.abs(analysis.netDebit);
  const maxLossAbs = Math.abs(analysis.maxLoss);
  const S = strat.current_price;
  const r = strat.risk_free_rate;
  const expDate = nyClose(strat.expiration);

  // Setup date slider
  const slider = document.getElementById('dateSlider');
  slider.max = matrix.dates.length - 1;
  if (state.graphDateIdx >= matrix.dates.length) state.graphDateIdx = 0;
  slider.value = state.graphDateIdx;
  const dt = matrix.dates[state.graphDateIdx];
  const daysRem = Math.max(0, (expDate - dt) / 86400000);
  document.getElementById('dateVal').textContent = dt.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric'})
    + ' (' + (daysRem < 1 ? daysRem.toFixed(1) : Math.round(daysRem)) + 'd remaining)';

  // Resize canvas with HiDPI support
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + 'px'; canvas.style.height = rect.height + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width, H = rect.height;
  const pad = {top: 30, right: 30, bottom: 40, left: 70};
  const gW = W - pad.left - pad.right;
  const gH = H - pad.top - pad.bottom;

  const dateIdx = state.graphDateIdx;
  const prices = matrix.prices.slice().reverse();
  const rawPnls = matrix.values.map(row => row[dateIdx]).reverse();
  const expIdx = matrix.values[0].length - 1;
  const rawExpPnls = matrix.values.map(row => row[expIdx]).reverse();
  const midIdx = Math.min(expIdx, Math.max(dateIdx, Math.round((dateIdx + expIdx) / 2)));
  const rawMidPnls = (midIdx !== dateIdx && midIdx !== expIdx) ? matrix.values.map(row => row[midIdx]).reverse() : null;

  // Value mode transform
  function txVal(v) {
    if (state.valueMode === 'pnlpct') return netDebit > 0 ? (v / netDebit * 100) : 0;
    if (state.valueMode === 'value') return v + netDebit;
    if (state.valueMode === 'maxrisk') return maxLossAbs > 0 ? (v / maxLossAbs * 100) : 0;
    return v;
  }
  function fmtVal(v) {
    if (state.valueMode === 'pnlpct' || state.valueMode === 'maxrisk') return v.toFixed(0) + '%';
    return '$' + v.toFixed(0);
  }
  const pnls = rawPnls.map(txVal);
  const expPnls = rawExpPnls.map(txVal);
  const midPnls = rawMidPnls ? rawMidPnls.map(txVal) : null;
  const zeroVal = txVal(0);

  const minPrice = Math.min(...prices); const maxPrice = Math.max(...prices);
  const allV = [...pnls, ...expPnls, ...(midPnls||[])];
  const minPnl = Math.min(...allV); const maxPnl = Math.max(...allV);
  const pnlPad = (maxPnl - minPnl) * 0.12 || 100;

  const scaleX = p => pad.left + (p - minPrice) / (maxPrice - minPrice) * gW;
  const scaleY = v => pad.top + gH - (v - (minPnl - pnlPad)) / ((maxPnl + pnlPad) - (minPnl - pnlPad)) * gH;
  const zeroY = scaleY(zeroVal);

  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);

  // Probability distribution
  const T_sel = Math.max(0.001, daysRem / 365);
  const mu = Math.log(S) + (r - 0.5*iv*iv)*T_sel;
  const sigma = iv * Math.sqrt(T_sel);
  let maxPdf = 0;
  const pdfVals = prices.map(p => { const v = lognormPDF(p, mu, sigma); if (v > maxPdf) maxPdf = v; return v; });
  if (maxPdf > 0) {
    const pdfSc = gH * 0.4 / maxPdf;
    ctx.beginPath(); ctx.moveTo(scaleX(prices[0]), H - pad.bottom);
    for (let i = 0; i < prices.length; i++) ctx.lineTo(scaleX(prices[i]), H - pad.bottom - pdfVals[i]*pdfSc);
    ctx.lineTo(scaleX(prices[prices.length-1]), H - pad.bottom); ctx.closePath();
    const pg = ctx.createLinearGradient(0, H-pad.bottom-gH*0.4, 0, H-pad.bottom);
    pg.addColorStop(0, 'rgba(31,111,235,0.22)'); pg.addColorStop(1, 'rgba(31,111,235,0.02)');
    ctx.fillStyle = pg; ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < prices.length; i++) { const x = scaleX(prices[i]); i===0?ctx.moveTo(x,H-pad.bottom-pdfVals[i]*pdfSc):ctx.lineTo(x,H-pad.bottom-pdfVals[i]*pdfSc); }
    ctx.strokeStyle = 'rgba(88,166,255,0.3)'; ctx.lineWidth = 1; ctx.stroke();
  }

  // Grid
  ctx.strokeStyle = 'rgba(48,54,61,0.6)'; ctx.lineWidth = 1;
  const pnlStepG = niceStep(maxPnl-minPnl, 5);
  for (let v = Math.ceil((minPnl-pnlPad)/pnlStepG)*pnlStepG; v <= maxPnl+pnlPad; v += pnlStepG) {
    const y = scaleY(v); if (y<pad.top||y>H-pad.bottom) continue;
    ctx.beginPath(); ctx.moveTo(pad.left,y); ctx.lineTo(W-pad.right,y); ctx.stroke();
    ctx.fillStyle='#8b949e'; ctx.font='11px monospace'; ctx.textAlign='right';
    ctx.fillText(fmtVal(v), pad.left-6, y+4);
  }
  const prStepG = niceStep(maxPrice-minPrice, 6);
  for (let p = Math.ceil(minPrice/prStepG)*prStepG; p <= maxPrice; p += prStepG) {
    const x = scaleX(p);
    ctx.beginPath(); ctx.moveTo(x,pad.top); ctx.lineTo(x,H-pad.bottom); ctx.stroke();
    ctx.fillStyle='#8b949e'; ctx.font='11px monospace'; ctx.textAlign='center';
    ctx.fillText('$'+p.toFixed(0), x, H-pad.bottom+16);
  }

  // Zero line
  ctx.strokeStyle='rgba(255,255,255,0.3)'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.left,zeroY); ctx.lineTo(W-pad.right,zeroY); ctx.stroke(); ctx.setLineDash([]);

  // Fill profit/loss areas
  function fillAr(vals, alpha) {
    ctx.beginPath(); let inP=false;
    for (let i=0;i<prices.length;i++){const x=scaleX(prices[i]),y=scaleY(vals[i]);
      if(vals[i]>=zeroVal){if(!inP){if(i>0&&vals[i-1]<zeroVal){const r2=Math.abs(vals[i-1]-zeroVal)/(Math.abs(vals[i-1]-zeroVal)+Math.abs(vals[i]-zeroVal));ctx.moveTo(scaleX(prices[i-1]+r2*(prices[i]-prices[i-1])),zeroY)}else ctx.moveTo(x,zeroY);inP=true}ctx.lineTo(x,y)}
      else if(inP){const r2=Math.abs(vals[i-1]-zeroVal)/(Math.abs(vals[i-1]-zeroVal)+Math.abs(vals[i]-zeroVal));ctx.lineTo(scaleX(prices[i-1]+r2*(prices[i]-prices[i-1])),zeroY);ctx.closePath();inP=false}}
    if(inP){ctx.lineTo(scaleX(prices[prices.length-1]),zeroY);ctx.closePath()}
    ctx.fillStyle=`rgba(63,185,80,${alpha})`;ctx.fill();
    ctx.beginPath();let inL=false;
    for(let i=0;i<prices.length;i++){const x=scaleX(prices[i]),y=scaleY(vals[i]);
      if(vals[i]<zeroVal){if(!inL){if(i>0&&vals[i-1]>=zeroVal){const r2=Math.abs(vals[i-1]-zeroVal)/(Math.abs(vals[i-1]-zeroVal)+Math.abs(vals[i]-zeroVal));ctx.moveTo(scaleX(prices[i-1]+r2*(prices[i]-prices[i-1])),zeroY)}else ctx.moveTo(x,zeroY);inL=true}ctx.lineTo(x,y)}
      else if(inL){const r2=Math.abs(vals[i-1]-zeroVal)/(Math.abs(vals[i-1]-zeroVal)+Math.abs(vals[i]-zeroVal));ctx.lineTo(scaleX(prices[i-1]+r2*(prices[i]-prices[i-1])),zeroY);ctx.closePath();inL=false}}
    if(inL){ctx.lineTo(scaleX(prices[prices.length-1]),zeroY);ctx.closePath()}
    ctx.fillStyle=`rgba(248,81,73,${alpha})`;ctx.fill();
  }
  fillAr(pnls, 0.15);

  // Expiration line (dashed red)
  if(dateIdx!==expIdx){ctx.strokeStyle='rgba(248,81,73,0.6)';ctx.lineWidth=2;ctx.setLineDash([6,4]);ctx.beginPath();
    for(let i=0;i<prices.length;i++){const x=scaleX(prices[i]),y=scaleY(expPnls[i]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)}ctx.stroke();ctx.setLineDash([])}
  // Mid-date line (dashed green)
  if(midPnls){ctx.strokeStyle='rgba(63,185,80,0.4)';ctx.lineWidth=1.5;ctx.setLineDash([4,4]);ctx.beginPath();
    for(let i=0;i<prices.length;i++){const x=scaleX(prices[i]),y=scaleY(midPnls[i]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)}ctx.stroke();ctx.setLineDash([])}
  // Selected date line — color transitions: red → orange → yellow → green based on P/L
  ctx.lineWidth=3;
  for(let i=1;i<prices.length;i++){
    const x0=scaleX(prices[i-1]),y0=scaleY(pnls[i-1]);
    const x1=scaleX(prices[i]),y1=scaleY(pnls[i]);
    const avgVal=(pnls[i-1]+pnls[i])/2;
    // Color: deep red → orange → yellow → green
    let r2,g2,b2;
    const ratio=Math.min(1,Math.max(-1,(avgVal-zeroVal)/(Math.max(Math.abs(maxPnl-zeroVal),Math.abs(minPnl-zeroVal))||1)));
    if(ratio<-0.1){r2=220;g2=50;b2=50}        // red
    else if(ratio<0.05){r2=220;g2=160;b2=30}  // orange/yellow
    else{r2=63;g2=185;b2=80}                   // green
    ctx.strokeStyle=`rgb(${r2},${g2},${b2})`;
    ctx.beginPath();ctx.moveTo(x0,y0);ctx.lineTo(x1,y1);ctx.stroke();
  }

  // Dynamic P/L value labels at curve max/min peaks
  {
    let maxIdx = 0, minIdx = 0;
    pnls.forEach((v, i) => { if (v > pnls[maxIdx]) maxIdx = i; if (v < pnls[minIdx]) minIdx = i; });
    ctx.font = 'bold 11px monospace';
    [maxIdx, minIdx].forEach(ci => {
      const lx = scaleX(prices[ci]), ly = scaleY(pnls[ci]);
      const rawV = rawPnls[ci];
      const label = fmtVal(pnls[ci]);
      const tw = ctx.measureText(label).width + 8;
      const above = ci === maxIdx ? (ly > pad.top + 20) : false;
      const labelY = above ? ly - 10 : ly + 16;
      ctx.fillStyle = 'rgba(22,27,34,0.85)';
      ctx.fillRect(lx - tw / 2, labelY - 10, tw, 16);
      ctx.fillStyle = rawV >= 0 ? '#3fb950' : '#f85149';
      ctx.textAlign = 'center';
      ctx.fillText(label, lx, labelY);
    });
  }

  // Custom price vertical dashed line with P/L annotation
  if (state.customPrice && state.customPrice > 0 && state.customPrice >= minPrice && state.customPrice <= maxPrice) {
    const cpx = scaleX(state.customPrice);
    // Interpolate P/L at custom price
    let cpPnl = pnls[0];
    for (let i = 1; i < prices.length; i++) {
      if ((prices[i-1] <= state.customPrice && prices[i] >= state.customPrice) ||
          (prices[i-1] >= state.customPrice && prices[i] <= state.customPrice)) {
        const t = (state.customPrice - prices[i-1]) / (prices[i] - prices[i-1]);
        cpPnl = pnls[i-1] + t * (pnls[i] - pnls[i-1]);
        break;
      }
    }
    const cpRaw = state.valueMode === 'pnl' ? cpPnl : cpPnl; // raw for color
    const cpy = scaleY(cpPnl);
    // Vertical dashed line
    ctx.strokeStyle = '#f0a500'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cpx, pad.top); ctx.lineTo(cpx, H - pad.bottom); ctx.stroke(); ctx.setLineDash([]);
    // Dot on curve
    ctx.beginPath(); ctx.arc(cpx, cpy, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#f0a500'; ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    // Price label (x) at bottom
    ctx.fillStyle = '#f0a500'; ctx.font = 'bold 11px monospace'; ctx.textAlign = 'center';
    ctx.fillText('$' + state.customPrice.toFixed(2), cpx, H - pad.bottom + 30);
    // P/L label (y) next to dot
    const cpLabel = fmtVal(cpPnl);
    const cptw = ctx.measureText(cpLabel).width + 8;
    ctx.fillStyle = 'rgba(22,27,34,0.9)';
    ctx.fillRect(cpx + 10, cpy - 8, cptw, 16);
    ctx.fillStyle = '#f0a500'; ctx.font = 'bold 11px monospace'; ctx.textAlign = 'left';
    ctx.fillText(cpLabel, cpx + 14, cpy + 4);
  }

  // Breakeven vertical lines with price + % from current
  analysis.breakevens.forEach(be=>{if(be>=minPrice&&be<=maxPrice){const bx=scaleX(be);
    ctx.strokeStyle='rgba(255,255,255,0.6)';ctx.lineWidth=1.5;ctx.setLineDash([]);
    ctx.beginPath();ctx.moveTo(bx,pad.top+20);ctx.lineTo(bx,H-pad.bottom);ctx.stroke();
    const bePct=((be-S)/S*100);const bePctStr=(bePct>=0?'+':'')+bePct.toFixed(1)+'%';
    ctx.fillStyle='#3fb950';ctx.font='bold 12px monospace';ctx.textAlign='center';
    ctx.fillText('$'+be.toFixed(2)+' ('+bePctStr+')',bx,pad.top+16);
  }});

  // Probability arrows at breakeven: ◄56%  44%►
  if(maxPdf>0){
    analysis.breakevens.forEach(be=>{if(be>=minPrice&&be<=maxPrice){
      const bx=scaleX(be);
      const pB=lognormCDF(be,mu,sigma);const pA=1-pB;
      const hiPnl=rawExpPnls[rawExpPnls.length-1];
      const profSide=hiPnl>0?'above':'below';
      const profProb=profSide==='above'?pA:pB;const lossProb=1-profProb;
      const arrY=zeroY+18;
      ctx.font='bold 12px monospace';
      // Left arrow (loss side for bull spread)
      ctx.textAlign='right';ctx.fillStyle='#f85149';
      ctx.fillText('\u25C4'+Math.round(profSide==='above'?pB*100:pA*100)+'%',bx-8,arrY);
      // Right arrow (profit side for bull spread)
      ctx.textAlign='left';ctx.fillStyle='#3fb950';
      ctx.fillText(Math.round(profSide==='above'?pA*100:pB*100)+'%\u25BA',bx+8,arrY);
    }});
  }

  // Current price line (dotted, subtle)
  const curX=scaleX(S);ctx.strokeStyle='rgba(255,255,255,0.3)';ctx.lineWidth=1;ctx.setLineDash([2,4]);
  ctx.beginPath();ctx.moveTo(curX,pad.top+20);ctx.lineTo(curX,H-pad.bottom);ctx.stroke();ctx.setLineDash([]);
  // Label at bottom to avoid overlap with breakeven
  ctx.fillStyle='#8b949e';ctx.font='11px monospace';ctx.textAlign='center';
  ctx.fillText('$'+S.toFixed(2),curX,H-pad.bottom+30);

  // Current P/L dot on curve — shows value in current display mode
  let cIdx=0,cD=Infinity;prices.forEach((p,i)=>{const d=Math.abs(p-S);if(d<cD){cD=d;cIdx=i}});
  const cPY=scaleY(pnls[cIdx]);
  // Dot
  ctx.beginPath();ctx.arc(curX,cPY,7,0,Math.PI*2);
  ctx.fillStyle=pnls[cIdx]>=zeroVal?'#3fb950':'#f85149';ctx.fill();
  ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.stroke();
  // Value label next to dot (matches current value mode)
  const dotLabel=fmtVal(pnls[cIdx]);
  ctx.fillStyle='#fff';ctx.font='bold 14px monospace';ctx.textAlign='left';
  ctx.fillText(dotLabel,curX+12,cPY+5);

  // Legend
  const lx=W-pad.right-200,ly=pad.top+10;ctx.font='11px monospace';ctx.textAlign='left';
  ctx.strokeStyle='#3fb950';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(lx,ly);ctx.lineTo(lx+20,ly);ctx.stroke();
  ctx.fillStyle='#8b949e';ctx.fillText('Current',lx+26,ly+4);
  if(dateIdx!==expIdx){ctx.strokeStyle='rgba(248,81,73,0.6)';ctx.lineWidth=2;ctx.setLineDash([6,4]);ctx.beginPath();ctx.moveTo(lx,ly+18);ctx.lineTo(lx+20,ly+18);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#8b949e';ctx.fillText('Expiration',lx+26,ly+22)}
  if(midPnls){ctx.strokeStyle='rgba(63,185,80,0.4)';ctx.lineWidth=1.5;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(lx,ly+36);ctx.lineTo(lx+20,ly+36);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#8b949e';ctx.fillText('Mid-term',lx+26,ly+40)}
  ctx.fillStyle='rgba(31,111,235,0.3)';ctx.fillRect(lx,ly+50,20,10);ctx.fillStyle='#8b949e';ctx.fillText('Probability',lx+26,ly+58);

  // Interactive crosshair overlay (HiDPI)
  const overlay = document.getElementById('plChartOverlay');
  overlay.width = rect.width * dpr; overlay.height = rect.height * dpr;
  overlay.style.width = rect.width + 'px'; overlay.style.height = rect.height + 'px';
  canvas._gd = {prices, pnls, rawPnls, expPnls, scaleX, scaleY, pad, W, H,
    minPrice, maxPrice, zeroY, zeroVal, S, netDebit, mu, sigma, iv, rawExpPnls,
    maxPdf, pdfVals: maxPdf > 0 ? pdfVals : null, pdfSc: maxPdf > 0 ? gH*0.4/maxPdf : 0,
    valueMode: state.valueMode, maxLossAbs};

  if (!canvas._hasHover) {
    canvas._hasHover = true;
    // Listen on the wrapper div (parent of both canvases)
    const wrapper = canvas.parentElement;

    wrapper.addEventListener('mousemove', function(e) {
      const d = canvas._gd; if (!d) return;
      const ov = document.getElementById('plChartOverlay');
      const octx = ov.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const cr = canvas.getBoundingClientRect();
      const mx = e.clientX - cr.left;  // in CSS pixels

      // Clear with DPR
      octx.setTransform(1, 0, 0, 1, 0, 0);
      octx.clearRect(0, 0, ov.width, ov.height);
      octx.setTransform(dpr, 0, 0, dpr, 0, 0);

      if (mx < d.pad.left || mx > d.W - d.pad.right) return;

      // Map to continuous price (not snapping to grid points)
      const hoverPrice = d.minPrice + (mx - d.pad.left) / (d.W - d.pad.left - d.pad.right) * (d.maxPrice - d.minPrice);
      const hx = d.scaleX(hoverPrice);

      // Interpolate P/L between nearest price points (prices are ascending after reverse)
      let i0 = 0, i1 = d.prices.length - 1;
      for (let i = 0; i < d.prices.length - 1; i++) {
        if (d.prices[i] <= hoverPrice && d.prices[i+1] >= hoverPrice) { i0 = i; i1 = i+1; break; }
      }
      const p0 = d.prices[i0], p1 = d.prices[i1];
      const t = (p0 !== p1) ? (hoverPrice - p1) / (p0 - p1) : 0;
      const rawPnl = d.rawPnls[i0] * t + d.rawPnls[i1] * (1 - t);
      const val = d.pnls[i0] * t + d.pnls[i1] * (1 - t);
      const expV = d.expPnls[i0] * t + d.expPnls[i1] * (1 - t);
      const pnlPct = d.netDebit > 0 ? (rawPnl / d.netDebit * 100) : 0;
      const pricePct = ((hoverPrice - d.S) / d.S * 100);
      const hy = d.scaleY(val);
      const ehy = d.scaleY(expV);

      // Vertical crosshair line (full height)
      octx.strokeStyle = 'rgba(255,255,255,0.35)'; octx.lineWidth = 1;
      octx.setLineDash([4, 3]);
      octx.beginPath(); octx.moveTo(hx, d.pad.top); octx.lineTo(hx, d.H - d.pad.bottom); octx.stroke();
      octx.setLineDash([]);

      // Dot on current date curve
      octx.beginPath(); octx.arc(hx, hy, 5, 0, Math.PI * 2);
      octx.fillStyle = val >= d.zeroVal ? '#3fb950' : '#f85149'; octx.fill();
      octx.strokeStyle = '#fff'; octx.lineWidth = 1.5; octx.stroke();

      // Y-value label above/below the dot on curve
      {
        let yLabel;
        if (d.valueMode === 'pnlpct' || d.valueMode === 'maxrisk') yLabel = val.toFixed(0) + '%';
        else yLabel = '$' + val.toFixed(0);
        octx.font = 'bold 11px monospace';
        const ytw = octx.measureText(yLabel).width + 8;
        const yLabelY = hy > d.pad.top + 25 ? hy - 12 : hy + 18;
        octx.fillStyle = 'rgba(22,27,34,0.9)';
        octx.fillRect(hx - ytw / 2, yLabelY - 10, ytw, 15);
        octx.fillStyle = val >= d.zeroVal ? '#3fb950' : '#f85149';
        octx.textAlign = 'center';
        octx.fillText(yLabel, hx, yLabelY);
      }

      // Dot on expiration curve
      octx.beginPath(); octx.arc(hx, ehy, 3.5, 0, Math.PI * 2);
      octx.fillStyle = 'rgba(248,81,73,0.6)'; octx.fill();

      // === All info in bottom bar (below X axis) — no overlap with curves ===
      const barY = d.H - d.pad.bottom + 4;
      octx.font = 'bold 11px monospace'; octx.textAlign = 'center';

      // Background bar
      octx.fillStyle = 'rgba(22,27,34,0.95)';
      octx.fillRect(d.pad.left, barY, d.W - d.pad.left - d.pad.right, 34);

      // Price + %
      const pctStr = (pricePct >= 0 ? '+' : '') + pricePct.toFixed(1) + '%';
      octx.fillStyle = '#e6edf3';
      octx.fillText('$' + hoverPrice.toFixed(2) + '  (' + pctStr + ')', hx, barY + 13);

      // P/L
      const sign = rawPnl >= 0 ? '+' : '';
      const pctSign = pnlPct >= 0 ? '+' : '';
      octx.fillStyle = rawPnl >= 0 ? '#3fb950' : '#f85149';
      octx.fillText('P/L: ' + sign + '$' + rawPnl.toFixed(0) + '  (' + pctSign + pnlPct.toFixed(1) + '%)', hx, barY + 28);

      // Probability left/right of crosshair
      if (d.sigma > 0) {
        const pBelow = lognormCDF(hoverPrice, d.mu, d.sigma);
        const pAbove = 1 - pBelow;
        octx.font = 'bold 11px monospace';
        octx.textAlign = 'left'; octx.fillStyle = '#f85149';
        octx.fillText('\u25C4 ' + Math.round(pBelow * 100) + '%', d.pad.left + 8, barY + 13);
        octx.textAlign = 'right'; octx.fillStyle = '#3fb950';
        octx.fillText(Math.round(pAbove * 100) + '% \u25BA', d.W - d.pad.right - 8, barY + 13);
      }
    });

    wrapper.addEventListener('mouseleave', function() {
      const ov = document.getElementById('plChartOverlay');
      const octx = ov.getContext('2d');
      octx.setTransform(1, 0, 0, 1, 0, 0);
      octx.clearRect(0, 0, ov.width, ov.height);
    });
  }
}

function niceStep(range, targetTicks) {
  const rough = range / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const residual = rough / mag;
  let nice;
  if (residual <= 1.5) nice = 1;
  else if (residual <= 3) nice = 2;
  else if (residual <= 7) nice = 5;
  else nice = 10;
  return nice * mag;
}

// ── Tab & Control Setup ──
function setupTabs() {
  document.querySelectorAll('#viewTabs .tab').forEach(tab => {
    tab.onclick = () => {
      state.view = tab.dataset.view;
      document.querySelectorAll('#viewTabs .tab').forEach(t => t.classList.toggle('active', t === tab));
      renderStrategy();
    };
  });

  document.querySelectorAll('#valueTabs .vtab').forEach(vtab => {
    vtab.onclick = () => {
      state.valueMode = vtab.dataset.mode;
      document.querySelectorAll('#valueTabs .vtab').forEach(v => v.classList.toggle('active', v === vtab));
      renderStrategy();
    };
  });
}

function setupControls() {
  const rangeSlider = document.getElementById('rangeSlider');
  const ivSlider = document.getElementById('ivSlider');
  const dateSlider = document.getElementById('dateSlider');

  rangeSlider.oninput = () => {
    state.rangePct = parseInt(rangeSlider.value);
    document.getElementById('rangeVal').textContent = `\u00B1${state.rangePct}%`;
    renderStrategy();
  };

  ivSlider.oninput = () => {
    state.ivPct = parseInt(ivSlider.value);
    document.getElementById('ivVal').textContent = state.ivPct + '%';
    // Show reset button if IV changed from strategy default
    const strat = RAW_DATA.strategies[state.selectedIdx];
    const stratIV = Math.round(strat.iv * 100);
    document.getElementById('ivResetBtn').style.display = state.ivPct !== stratIV ? '' : 'none';
    // Update expected move with new IV
    const overrideStrat = {...strat, iv: state.ivPct / 100};
    const em = calcExpectedMove(overrideStrat);
    document.getElementById('expectedMoveLabel').textContent =
      'EM: \u00B1$' + em.dollar.toFixed(2) + ' (' + em.pct.toFixed(1) + '%)';
    renderStrategy();
  };

  dateSlider.oninput = () => {
    state.graphDateIdx = parseInt(dateSlider.value);
    renderStrategy();
  };
}

function goToPrice() {
  const val = parseFloat(document.getElementById('priceInput').value);
  if (!val || val <= 0) return;
  state.customPrice = val;
  document.getElementById('priceClearBtn').style.display = '';
  renderStrategy();
}

function clearPrice() {
  state.customPrice = null;
  document.getElementById('priceInput').value = '';
  document.getElementById('priceClearBtn').style.display = 'none';
  renderStrategy();
}

// ⚠️ IV 滑杆的 max 在 HTML 里写死 150, 但真实持仓的 IV 会超过它
//    158% 而 slider.value 其实是 "150" —— 只要一碰滑杆, oninput 把这个 150 读回
//    state.ivPct, IV 就被静默砍掉 8 个点, 而 COP / 平衡点 / P&L 表 / 图全吃这个值。
//    这偏偏是「IV 压力测试」控件, 对最波动的票反而失效。按当前 IV 抬高上限, 并留
//    出向上压测的余量; 常规 IV(≤100%) 时上限仍是 150, 手感不变。
function _ivSliderFit(pct) {
  const el = document.getElementById('ivSlider');
  if (!el) return;
  el.max = Math.max(150, Math.ceil((pct * 1.5) / 10) * 10);
  el.value = pct;
}

function resetIV() {
  const strat = RAW_DATA.strategies[state.selectedIdx];
  state.ivPct = Math.round(strat.iv * 100);
  _ivSliderFit(state.ivPct);
  document.getElementById('ivVal').textContent = state.ivPct + '%';
  document.getElementById('ivResetBtn').style.display = 'none';
  renderStrategy();
}

// ── Init ──
window.addEventListener('DOMContentLoaded', init);
window.addEventListener('resize', () => { if (state.view === 'graph') renderStrategy(); });
</script>
<!-- __TAB_END__ -->
</body>
</html>"""


if __name__ == "__main__":
    main()

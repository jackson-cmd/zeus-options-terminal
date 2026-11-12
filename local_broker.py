"""Manual mode broker — ZEUS without a tastytrade account.

You upload (or type in) your own stock and option trades. This module replays
them and answers the exact tastytrade REST calls the rest of the app makes:

    /customers/me/accounts            → one account called MANUAL
    /accounts/MANUAL/positions        → open lots, FIFO, tastytrade schema
    /accounts/MANUAL/balances         → cash + mark-to-market NLV
    /accounts/MANUAL/transactions     → every fill (+ synthetic expirations)
    /accounts/MANUAL/orders/live      → empty
    /market-data/by-type, /market-data, /market-data/{sym}/quotes
    /market-metrics                   → IV (Cboe IV30 or ATM ~30 DTE), HV30, next earnings
    /option-chains/{sym}/nested       → chain built from Cboe (Yahoo Finance fallback)

Option quotes come from Cboe's free delayed-quotes file (~15 min delayed; after
the close it keeps the closing bid/ask, IV and Greeks), with Yahoo Finance
through yfinance as the fallback. Stock quotes come from Yahoo Finance, with
Finviz when Yahoo rate-limits. Live streaming (DXLink) needs tastytrade; in
manual mode the app refreshes on a timer instead.

Trades file: ~/.zeus-terminal/trades.json (outside the repository).
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import zeus_config as hc

ACCOUNT = "MANUAL"
_lock = threading.RLock()


class LocalModeError(RuntimeError):
    """Raised for calls that only a real broker can serve (e.g. streaming)."""


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
_OCC_RE = re.compile(r"^([A-Z.\-/^0-9]{1,6})\s*(\d{6})([CP])(\d{8})$")
_LOOSE_RE = re.compile(
    r"^\s*([A-Za-z.\-]{1,6})\s+(\d{1,2})/(\d{1,2})/(\d{2,4})\s+\$?([\d.]+)\s*(C|P|CALL|PUT)\b", re.I)
_LOOSE2_RE = re.compile(
    r"^\s*([A-Za-z.\-]{1,6})\s+(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(CALL|PUT)\s+\$?([\d.]+)", re.I)
_COMPACT_RE = re.compile(r"^\.?([A-Z.\-]{1,6})(\d{6})([CP])([\d.]+)$")


def occ_symbol(underlying: str, expiration: str, opt_type: str, strike: float) -> str:
    """tastytrade/OCC 21-char symbol, e.g. 'AAPL  260618C00200000'."""
    d = date.fromisoformat(expiration)
    root = underlying.upper().ljust(6)[:6]
    return f"{root}{d:%y%m%d}{opt_type.upper()[0]}{int(round(float(strike) * 1000)):08d}"


def streamer_symbol(underlying: str, expiration: str, opt_type: str, strike: float) -> str:
    d = date.fromisoformat(expiration)
    s = float(strike)
    s_txt = str(int(s)) if s.is_integer() else (f"{s:.3f}".rstrip("0").rstrip("."))
    return f".{underlying.upper()}{d:%y%m%d}{opt_type.upper()[0]}{s_txt}"


def yahoo_option_symbol(occ: str) -> str:
    return occ.replace(" ", "")


def parse_option_symbol(text: str) -> dict | None:
    """Accepts OCC ('AAPL  260618C00200000' / 'AAPL260618C00200000'), streamer
    ('.AAPL260618C200'), or broker descriptions ('AAPL 06/18/2026 200.00 C',
    'AAPL 6/18/2026 Call $200.00')."""
    t = (text or "").strip().upper()
    if not t:
        return None
    m = _OCC_RE.match(t.replace("  ", " ").replace(" ", "")) if len(t.replace(" ", "")) >= 15 else None
    if m and len(m.group(4)) == 8:
        root, ymd, cp, k = m.groups()
        return {"underlying": root.strip(), "expiration": _ymd(ymd), "type": cp, "strike": int(k) / 1000.0}
    m = _COMPACT_RE.match(t)
    if m:
        root, ymd, cp, k = m.groups()
        return {"underlying": root, "expiration": _ymd(ymd), "type": cp, "strike": float(k)}
    m = _LOOSE_RE.match(t)
    if m:
        root, mo, dd, yy, k, cp = m.groups()
        return {"underlying": root, "expiration": _mdy(mo, dd, yy), "type": cp[0], "strike": float(k)}
    m = _LOOSE2_RE.match(t)
    if m:
        root, mo, dd, yy, cp, k = m.groups()
        return {"underlying": root, "expiration": _mdy(mo, dd, yy), "type": cp[0], "strike": float(k)}
    return None


def _ymd(s: str) -> str:
    return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}"


def _mdy(mo, dd, yy) -> str:
    y = int(yy)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(mo):02d}-{int(dd):02d}"


# ---------------------------------------------------------------------------
# Trade store
# ---------------------------------------------------------------------------
def trades_path() -> Path:
    return hc.home() / "trades.json"


def load_trades() -> list[dict]:
    with _lock:
        p = trades_path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def save_trades(trades: list[dict]) -> None:
    with _lock:
        p = trades_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(trades, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        _invalidate()


def _num(v, default=0.0) -> float:
    if v is None:
        return default
    s = str(v).strip().replace(",", "").replace("$", "")
    if s in ("", "-", "--"):
        return default
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return default
    return -x if neg else x


def _norm_date(v) -> str:
    """→ ISO 'YYYY-MM-DDTHH:MM:SS' (local market time assumed)."""
    s = str(v or "").strip()
    if not s:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
                "%m/%d/%y", "%Y/%m/%d", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(hours=4)
            if fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y"):
                dt = dt.replace(hour=10, minute=0)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise ValueError(f"unrecognised date: {v!r}")


def normalize_trade(raw: dict) -> dict:
    """Validate one trade (from the form or a CSV row already mapped to our keys)."""
    t: dict[str, Any] = {}
    sym_text = str(raw.get("symbol") or "").strip()
    asset = str(raw.get("asset") or "").strip().lower()
    opt = None
    if raw.get("option_symbol"):
        opt = parse_option_symbol(str(raw["option_symbol"]))
    if not opt and sym_text and (asset.startswith("opt") or len(sym_text) > 8 or " " in sym_text):
        opt = parse_option_symbol(sym_text)
    if opt:
        asset = "option"
        t.update(symbol=opt["underlying"], option_type=opt["type"], strike=opt["strike"],
                 expiration=opt["expiration"])
    else:
        if not asset:
            asset = "option" if raw.get("strike") not in (None, "") else "stock"
        asset = "option" if asset.startswith(("opt", "equity option")) else "stock"
        t["symbol"] = sym_text.upper().split()[0] if sym_text else ""
        if asset == "option":
            cp = str(raw.get("option_type") or "").strip().upper()[:1]
            if cp not in ("C", "P"):
                raise ValueError("option_type must be C or P")
            t["option_type"] = cp
            t["strike"] = _num(raw.get("strike"))
            t["expiration"] = _norm_date(raw.get("expiration"))[:10]
    if not t["symbol"]:
        raise ValueError("symbol is required")
    t["asset"] = asset
    side = str(raw.get("side") or "").strip().lower()
    effect = str(raw.get("effect") or "").strip().lower()
    # broker-style actions: BTO / STO / BTC / STC / Buy to Open / Sell to Close …
    a = side.replace("_", " ")
    if a in ("bto", "buy to open"):
        side, effect = "buy", "open"
    elif a in ("sto", "sell to open", "sell short", "short"):
        side, effect = "sell", "open"
    elif a in ("btc", "buy to close", "buy to cover", "cover"):
        side, effect = "buy", "close"
    elif a in ("stc", "sell to close"):
        side, effect = "sell", "close"
    elif a.startswith("b"):
        side = "buy"
    elif a.startswith("s"):
        side = "sell"
    qty = _num(raw.get("qty") if raw.get("qty") not in (None, "") else raw.get("quantity"))
    if side not in ("buy", "sell"):
        # a signed quantity is also accepted
        if qty == 0:
            raise ValueError("side must be buy or sell")
        side = "buy" if qty > 0 else "sell"
    if effect not in ("open", "close"):
        effect = "auto"
    t.update(side=side, effect=effect, qty=abs(qty))
    if t["qty"] <= 0:
        raise ValueError("quantity must be > 0")
    t["price"] = abs(_num(raw.get("price")))
    t["fees"] = abs(_num(raw.get("fees")))
    t["date"] = _norm_date(raw.get("date"))
    mult = _num(raw.get("multiplier"), 0)
    t["multiplier"] = mult if mult > 0 else (100.0 if asset == "option" else 1.0)
    t["order_id"] = str(raw.get("order_id") or "").strip()
    t["note"] = str(raw.get("note") or "").strip()[:300]
    t["id"] = str(raw.get("id") or uuid.uuid4().hex[:12])
    return t


# CSV header aliases → our keys. Lower-cased, spaces/underscores ignored.
_ALIASES = {
    "date": ["date", "time", "datetime", "tradedate", "executedat", "filldate", "transactiondate", "rundate",
             "activitydate", "tradetime", "dateacquired"],
    "symbol": ["symbol", "ticker", "underlying", "underlyingsymbol", "rootsymbol", "instrument", "stock"],
    "option_symbol": ["optionsymbol", "occsymbol", "contract", "contractsymbol", "description"],
    "asset": ["asset", "assettype", "type", "instrumenttype", "securitytype", "assetclass"],
    "side": ["side", "action", "buysell", "transcode", "transactiontype", "direction", "buy/sell"],
    "effect": ["effect", "openclose", "positioneffect", "open/close"],
    "qty": ["qty", "quantity", "shares", "contracts", "size", "filledqty"],
    "price": ["price", "fillprice", "averageprice", "avgprice", "tradeprice", "executionprice", "pricepershare"],
    "fees": ["fees", "fee", "commission", "commissions", "commissionsandfees", "totalfees", "commfee"],
    "option_type": ["optiontype", "callorput", "putcall", "right", "cp", "call/put"],
    "strike": ["strike", "strikeprice"],
    "expiration": ["expiration", "expirationdate", "expiry", "exp", "expirydate", "maturity"],
    "multiplier": ["multiplier", "contractsize", "mult"],
    "order_id": ["orderid", "order", "order#", "group", "strategy", "tradeid"],
    "note": ["note", "notes", "memo", "comment"],
}


def _key(h: str) -> str:
    return re.sub(r"[\s_\-\.]", "", (h or "").strip().lower())


def parse_csv(text: str) -> tuple[list[dict], list[str]]:
    """Parse a CSV of trades. Returns (trades, errors). Understands the ZEUS
    template, tastytrade's own CSV export, and most broker exports that have
    date / symbol / side / quantity / price columns."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    # skip preamble lines until a header-looking row
    hdr_i = 0
    for i, r in enumerate(rows[:30]):
        keys = {_key(c) for c in r}
        if keys & set(_ALIASES["symbol"]) and (keys & set(_ALIASES["qty"]) or keys & set(_ALIASES["side"])):
            hdr_i = i
            break
    if not rows:
        return [], ["empty file"]
    header = [_key(c) for c in rows[hdr_i]]
    colmap: dict[str, int] = {}
    for ours, names in _ALIASES.items():
        for i, h in enumerate(header):
            if h in names and ours not in colmap:
                colmap[ours] = i
    is_tastytrade = "subtype" in header and "instrumenttype" in header
    trades, errors = [], []
    for ln, r in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
        if not any(c.strip() for c in r):
            continue
        raw = {k: (r[i] if i < len(r) else "") for k, i in colmap.items()}
        try:
            if is_tastytrade:
                ttype = r[header.index("type")] if "type" in header else ""
                sub = r[header.index("subtype")] if "subtype" in header else ""
                if ttype.strip() not in ("Trade",) and "Expir" not in sub:
                    continue  # money movements, dividends… not trades
                if "Expir" in sub:
                    continue  # recomputed by the engine
                raw["asset"] = r[header.index("instrumenttype")]
                if "symbol" in colmap:
                    raw["option_symbol"] = r[colmap["symbol"]]
                if "underlyingsymbol" in header:
                    raw["symbol"] = r[header.index("underlyingsymbol")]
                if "averageprice" in header:
                    mult = _num(r[header.index("multiplier")]) if "multiplier" in header else 1
                    raw["price"] = abs(_num(r[header.index("averageprice")])) / (mult or 1)
                fees = 0.0
                for name in ("commissions", "fees"):
                    if name in header:
                        fees += abs(_num(r[header.index(name)]))
                raw["fees"] = fees
            if raw.get("asset", "").strip().lower() in ("cash", "money movement", "dividend"):
                continue
            trades.append(normalize_trade(raw))
        except Exception as e:
            errors.append(f"line {ln}: {e}")
    return trades, errors


def template_csv() -> str:
    return (
        "date,symbol,asset,side,effect,qty,price,fees,option_type,strike,expiration,multiplier,order_id,note\n"
        "2026-03-02 10:15,AAPL,stock,buy,open,50,231.40,0,,,,,,long stock\n"
        "2026-03-03 11:02,NVDA,option,buy,open,1,12.50,1.10,C,180,2026-04-17,100,nvda-bcs-1,bull call spread long leg\n"
        "2026-03-03 11:02,NVDA,option,sell,open,1,6.20,1.10,C,190,2026-04-17,100,nvda-bcs-1,bull call spread short leg\n"
        "2026-03-20 15:40,NVDA,option,sell,close,1,15.80,1.10,C,180,2026-04-17,100,nvda-bcs-2,take profit\n"
        "2026-03-20 15:40,NVDA,option,buy,close,1,8.05,1.10,C,190,2026-04-17,100,nvda-bcs-2,take profit\n"
    )


def add_trades(new: list[dict], replace: bool = False) -> dict:
    with _lock:
        cur = [] if replace else load_trades()
        seen = {_fingerprint(t) for t in cur}
        added = 0
        for t in new:
            fp = _fingerprint(t)
            if fp in seen:
                continue
            seen.add(fp)
            cur.append(t)
            added += 1
        cur.sort(key=lambda t: (t.get("date", ""), t.get("id", "")))
        save_trades(cur)
        return {"added": added, "total": len(cur), "duplicates": len(new) - added}


def delete_trade(trade_id: str) -> bool:
    with _lock:
        cur = load_trades()
        nxt = [t for t in cur if t.get("id") != trade_id]
        if len(nxt) == len(cur):
            return False
        save_trades(nxt)
        return True


def _fingerprint(t: dict) -> tuple:
    return (t.get("date"), t.get("symbol"), t.get("asset"), t.get("side"), round(float(t.get("qty", 0)), 6),
            round(float(t.get("price", 0)), 6), t.get("option_type"), t.get("strike"), t.get("expiration"))


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------
_state_cache: dict = {"key": None, "value": None}


def _invalidate():
    _state_cache["key"] = None


def _inst_key(t: dict) -> str:
    if t["asset"] == "option":
        return occ_symbol(t["symbol"], t["expiration"], t["option_type"], t["strike"])
    return t["symbol"].upper()


def _iso_tt(local_iso: str) -> str:
    """'2026-03-03T11:02:00' (US/Eastern wall clock) → tastytrade style UTC string."""
    dt = datetime.fromisoformat(local_iso)
    utc = dt + timedelta(hours=4)  # EDT approximation is enough for ordering/grouping
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def replay(today: date | None = None) -> dict:
    """→ {'transactions': [...], 'positions': {key: info}, 'cash_flow': float}"""
    trades = load_trades()
    today = today or date.today()
    key = (trades_path().stat().st_mtime if trades_path().exists() else 0, today.isoformat())
    with _lock:
        if _state_cache["key"] == key and _state_cache["value"] is not None:
            return _state_cache["value"]
    lots: dict[str, deque] = defaultdict(deque)  # key -> deque[[signed_qty, price, open_iso, order_id]]
    meta: dict[str, dict] = {}
    txns: list[dict] = []
    orphans: list[dict] = []
    cash = 0.0
    realized = 0.0
    txn_id = 1
    auto_orders: dict[tuple, int] = {}
    order_seq = [100000]

    def order_for(t: dict, eff: str) -> int:
        if t.get("order_id"):
            k = ("u", t["order_id"])
        else:
            k = ("a", t["symbol"], t["date"][:16], eff)
        if k not in auto_orders:
            order_seq[0] += 1
            auto_orders[k] = order_seq[0]
        return auto_orders[k]

    events: list[tuple] = [(t["date"], 0, t) for t in trades]
    # synthetic expirations are inserted when an option's expiry passes
    expiries = {}
    for t in trades:
        if t["asset"] == "option":
            expiries[_inst_key(t)] = t
    for k, t in expiries.items():
        exp = t["expiration"]
        if date.fromisoformat(exp) < today:
            events.append((exp + "T16:00:00", 1, {"_expire": k, **t}))
    events.sort(key=lambda e: (e[0], e[1]))

    for when, kind, t in events:
        k = t.get("_expire") or _inst_key(t)
        mult = float(t.get("multiplier") or (100 if t["asset"] == "option" else 1))
        meta.setdefault(k, {"asset": t["asset"], "symbol": t["symbol"], "option_type": t.get("option_type"),
                            "strike": t.get("strike"), "expiration": t.get("expiration"), "multiplier": mult})
        if kind == 1:  # expiration of whatever is still open
            q = sum(l[0] for l in lots[k])
            if abs(q) < 1e-9:
                continue
            intrinsic = _intrinsic_at_expiry(t["symbol"], t["option_type"], float(t["strike"]), t["expiration"])
            value = intrinsic * abs(q) * mult
            # long position receives intrinsic, short pays it
            signed_cash = value if q > 0 else -value
            cash += signed_cash
            for lot in lots[k]:
                realized += (intrinsic - lot[1]) * lot[0] * mult
            lots[k].clear()
            txns.append(_txn(txn_id, k, meta[k], "Receive Deliver", "Expiration",
                             "Sell to Close" if q > 0 else "Buy to Close", abs(q), intrinsic, value,
                             "Credit" if signed_cash >= 0 else "Debit", 0.0, when, None,
                             f"Expired {t['symbol']} {t['expiration']} {t['strike']}{t['option_type']} "
                             f"(settled at estimated intrinsic {intrinsic:.2f})"))
            txn_id += 1
            continue
        signed = t["qty"] if t["side"] == "buy" else -t["qty"]
        cur = sum(l[0] for l in lots[k])
        eff = t["effect"]
        if eff == "auto":
            eff = "close" if (cur != 0 and (cur > 0) != (signed > 0)) else "open"
        cash_flow = -signed * t["price"] * mult - t["fees"]
        cash += cash_flow
        oid = order_for(t, eff)
        remaining = signed
        if eff == "close" or (cur != 0 and (cur > 0) != (signed > 0)):
            # reduce FIFO
            while remaining != 0 and lots[k] and (lots[k][0][0] > 0) != (remaining > 0):
                lot = lots[k][0]
                take = min(abs(lot[0]), abs(remaining))
                sgn = 1 if lot[0] > 0 else -1
                realized += (t["price"] - lot[1]) * take * sgn * mult
                lot[0] -= take * sgn
                remaining += take * sgn
                if abs(lot[0]) < 1e-9:
                    lots[k].popleft()
        if abs(remaining) > 1e-9:
            if t["effect"] == "close":      # explicit close only; "auto" may flip through zero
                # An explicit close with nothing (or not enough) to close — usually a
                # history export that starts mid-position. Never turn it into a new
                # opposite position; the fill still counts toward cash.
                orphans.append({"symbol": k, "qty": abs(remaining), "date": t["date"]})
            else:
                lots[k].append([remaining, t["price"], t["date"], oid])
        action = ("Buy" if signed > 0 else "Sell") + (" to Open" if eff == "open" else " to Close")
        txns.append(_txn(txn_id, k, meta[k], "Trade", action, action, t["qty"], t["price"],
                         t["qty"] * t["price"] * mult, "Debit" if signed > 0 else "Credit", t["fees"],
                         t["date"], oid, t.get("note") or ""))
        txn_id += 1

    positions = {}
    for k, dq in lots.items():
        q = sum(l[0] for l in dq)
        if abs(q) < 1e-9:
            continue
        cost = sum(abs(l[0]) * l[1] for l in dq)
        positions[k] = {**meta[k], "qty": q, "avg": cost / max(abs(q), 1e-9),
                        "opened": min(l[2] for l in dq), "order_id": dq[-1][3]}
    starting = hc.get("manual_starting_cash")
    try:
        starting_cash = float(starting) if str(starting).strip() not in ("", "0", "0.0") else None
    except ValueError:
        starting_cash = None
    if starting_cash is None:
        # smallest account that could have funded these trades
        running, low = 0.0, 0.0
        for tx in txns:
            v = float(tx["net-value"]) * (1 if tx["net-value-effect"] == "Credit" else -1)
            running += v
            low = min(low, running)
        starting_cash = -low
    value = {"transactions": txns, "positions": positions, "cash": starting_cash + cash,
             "starting_cash": starting_cash, "realized": realized, "orphan_closes": orphans}
    with _lock:
        _state_cache["key"], _state_cache["value"] = key, value
    return value


def _txn(tid, key, meta, ttype, subtype, action, qty, price, value, effect, fees, when, oid, desc):
    is_opt = meta["asset"] == "option"
    net = value - fees if effect == "Credit" else value + fees
    return {
        "id": tid, "account-number": ACCOUNT, "symbol": key,
        "instrument-type": "Equity Option" if is_opt else "Equity",
        "underlying-symbol": meta["symbol"], "transaction-type": ttype, "transaction-sub-type": subtype,
        "action": action, "description": desc, "quantity": f"{qty:.4f}", "price": f"{price:.4f}",
        "value": f"{value:.2f}", "value-effect": effect,
        "net-value": f"{abs(net):.2f}", "net-value-effect": "Credit" if (effect == "Credit" and value >= fees) else "Debit",
        "commission": f"{fees:.2f}", "commission-effect": "Debit" if fees else "None",
        "clearing-fees": "0.00", "clearing-fees-effect": "None",
        "regulatory-fees": "0.00", "regulatory-fees-effect": "None",
        "proprietary-index-option-fees": "0.00", "proprietary-index-option-fees-effect": "None",
        "other-charge": "0.00", "other-charge-effect": "None",
        "executed-at": _iso_tt(when), "transaction-date": when[:10],
        "order-id": oid, "multiplier": meta["multiplier"],
        "expiration-date": meta.get("expiration"), "strike-price": meta.get("strike"),
        "option-type": meta.get("option_type"),
    }


# ---------------------------------------------------------------------------
# Market data (Yahoo Finance via yfinance), cached
# ---------------------------------------------------------------------------
_cache: dict = {}
_INDEX_MAP = {"VIX": "^VIX", "SPX": "^GSPC", "NDX": "^NDX", "RUT": "^RUT", "DJX": "^DJI", "VIX9D": "^VIX9D",
              "VVIX": "^VVIX", "TNX": "^TNX", "XSP": "^XSP"}


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def _yf():
    import yfinance as yf
    return yf


def _ysym(sym: str) -> str:
    s = sym.upper().lstrip("$")
    return _INDEX_MAP.get(s, s.replace("/", "-").replace(".", "-") if not s.startswith("^") else s)


def equity_quote(sym: str) -> dict:
    def fetch():
        try:
            fi = _yf().Ticker(_ysym(sym)).fast_info
            last = float(fi.get("lastPrice") or fi.get("last_price") or 0)
            # The close before the latest session, the same bar equity_quotes_batch uses.
            # Yahoo's previousClose can be days out of date, so it is only a fallback.
            prev = float(fi.get("regularMarketPreviousClose") or fi.get("regular_market_previous_close")
                         or fi.get("previousClose") or fi.get("previous_close") or 0)
            hi = float(fi.get("dayHigh") or 0)
            lo = float(fi.get("dayLow") or 0)
            vol = float(fi.get("lastVolume") or 0)
            if last > 0:
                return {"last": last, "prev_close": prev, "high": hi, "low": lo, "volume": vol}
        except Exception:
            pass
        try:
            import zeus_finviz as _fv     # Yahoo rate-limited → Finviz (free, delayed)
            q = _fv.quotes([sym], max_age=60).get(_fv._norm(sym))
            if q:
                return {"last": q["last"], "prev_close": q["prev_close"]}
        except Exception:
            pass
        return {"last": 0.0, "prev_close": 0.0}
    return _cached(("eq", sym.upper()), 30, fetch)


def equity_quotes_batch(symbols: list[str]) -> dict:
    """Many equities at once (one Yahoo request per 80 tickers), cached 60 s.
    Uses the last two daily bars: latest close (= live-ish price during the
    session) and the previous session's close."""
    syms = [s.upper() for s in dict.fromkeys(symbols) if s]
    out: dict = {}
    missing = []
    now = time.time()
    with _lock:
        for s in syms:
            hit = _cache.get(("eq", s))
            if hit and now - hit[0] < 60:
                out[s] = hit[1]
            else:
                missing.append(s)
    for i in range(0, len(missing), 80):
        chunk = missing[i:i + 80]
        ymap = {s: _ysym(s) for s in chunk}
        try:
            df = _yf().download(list(ymap.values()), period="5d", interval="1d", group_by="ticker",
                                auto_adjust=False, progress=False, threads=True)
        except Exception:
            df = None
        for s, ys in ymap.items():
            q = {"last": 0.0, "prev_close": 0.0}
            try:
                sub = df[ys] if (df is not None and hasattr(df.columns, "levels")) else df
                closes = sub["Close"].dropna()
                if len(closes) >= 1:
                    q["last"] = float(closes.iloc[-1])
                if len(closes) >= 2:
                    q["prev_close"] = float(closes.iloc[-2])
                q["high"] = float(sub["High"].dropna().iloc[-1])
                q["low"] = float(sub["Low"].dropna().iloc[-1])
                q["volume"] = float(sub["Volume"].dropna().iloc[-1])
            except Exception:
                pass
            out[s] = q
            with _lock:
                _cache[("eq", s)] = (now, q)
    zero = [s for s in missing if not out.get(s, {}).get("last")]
    if zero:
        try:
            import zeus_finviz as _fv     # Yahoo rate-limited → Finviz (free, delayed)
            for t, q in _fv.quotes(zero, max_age=60).items():
                s = next((z for z in zero if _fv._norm(z) == t), t)
                out[s] = {"last": q["last"], "prev_close": q["prev_close"]}
                with _lock:
                    _cache[("eq", s)] = (now, out[s])
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Option data: Cboe delayed quotes first, Yahoo Finance as the fallback.
# Cboe publishes every listed contract of an underlying in one free JSON file
# (~15 min delayed during the session). After the close it keeps the closing
# bid/ask, IV and Greeks, which Yahoo blanks to zero at night and on weekends,
# and one request covers the whole chain instead of one per expiration.
# ---------------------------------------------------------------------------
_CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
_CBOE_INDEX_ROOTS = {"SPX": ("SPX", "SPXW"), "NDX": ("NDX", "NDXP"), "RUT": ("RUT", "RUTW"),
                     "VIX": ("VIX", "VIXW"), "XSP": ("XSP",), "DJX": ("DJX",), "OEX": ("OEX",), "XEO": ("XEO",)}
_CBOE_WEEKLY_PARENT = {"SPXW": "SPX", "NDXP": "NDX", "RUTW": "RUT", "VIXW": "VIX"}
_CBOE_SYM_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


def _cboe_json(sym: str) -> dict | None:
    req = urllib.request.Request(_CBOE_URL.format(sym), headers={"User-Agent": "Mozilla/5.0",
                                                                  "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _cboe_parent(und: str) -> str:
    u = und.upper().lstrip("$^")
    return _CBOE_WEEKLY_PARENT.get(u, u)


def _cboe_roots(und: str) -> tuple:
    u = und.upper().lstrip("$^")
    if u in _CBOE_WEEKLY_PARENT:
        return (u,)
    return _CBOE_INDEX_ROOTS.get(u) or (re.sub(r"[^A-Z0-9]", "", u),)


def _cboe_book(und: str) -> dict | None:
    """Whole option chain of one underlying from Cboe, cached 2 min.
    {"spot", "iv30", "rows": {(root, yymmdd, "C"/"P", strike×1000):
    (bid, ask, last, prev_close, iv, volume, oi, delta, gamma, theta, vega)}}"""
    u = _cboe_parent(und)

    def fetch():
        sym = ("_" + u) if u in _CBOE_INDEX_ROOTS else u.replace("/", ".").replace("-", ".")
        try:
            data = (_cboe_json(sym) or {}).get("data") or {}
        except Exception:
            return None

        def g(o, k):
            x = _num(o.get(k))
            return x if x == x else 0.0        # NaN → 0
        rows = {}
        for o in data.get("options") or []:
            m = _CBOE_SYM_RE.match(str(o.get("option") or ""))
            if not m:
                continue
            root, ymd, cp, k = m.groups()
            rows[(root, ymd, cp, int(k))] = (
                g(o, "bid"), g(o, "ask"), g(o, "last_trade_price"), g(o, "prev_day_close"), g(o, "iv"),
                g(o, "volume"), g(o, "open_interest"), g(o, "delta"), g(o, "gamma"), g(o, "theta"), g(o, "vega"))
        if not rows:
            return None
        return {"spot": g(data, "current_price") or g(data, "close"), "iv30": g(data, "iv30") / 100.0,
                "rows": rows}
    return _cached(("cboe", u), 120, fetch)


def _cboe_row(p: dict):
    book = _cboe_book(p["underlying"])
    if not book:
        return None
    ymd = date.fromisoformat(p["expiration"]).strftime("%y%m%d")
    k = int(round(float(p["strike"]) * 1000))
    for root in _cboe_roots(p["underlying"]):
        row = book["rows"].get((root, ymd, p["type"], k))
        if row:
            return row
    return None


def _cboe_strikes(und: str) -> dict:
    """{expiration ISO: set(strikes)} from Cboe, today and later; {} if unavailable."""
    book = _cboe_book(und)
    if not book:
        return {}
    roots = set(_cboe_roots(und))
    today = date.today().strftime("%y%m%d")
    out: dict = defaultdict(set)
    for (root, ymd, _cp, k) in book["rows"]:
        if root in roots and ymd >= today:
            out[_ymd(ymd)].add(k / 1000.0)
    return dict(out)


def _chain(und: str, exp: str):
    def fetch():
        try:
            oc = _yf().Ticker(_ysym(und)).option_chain(exp)
            return oc.calls, oc.puts
        except Exception:
            return None, None
    return _cached(("chain", und.upper(), exp), 60, fetch)


def expirations(und: str) -> list[str]:
    def fetch():
        ex = sorted(_cboe_strikes(und))
        if ex:
            return ex
        try:
            return list(_yf().Ticker(_ysym(und)).options or [])
        except Exception:
            return []
    return _cached(("exps", und.upper()), 3600, fetch)


def _clean_iv(iv: float) -> float:
    """Yahoo reports ~0 IV outside market hours; treat anything implausible as unknown."""
    return iv if 0.01 < iv < 5 else 0.0


def option_quote(occ: str) -> dict:
    p = parse_option_symbol(occ)
    if not p:
        return {}
    out = {"bid": 0.0, "ask": 0.0, "last": 0.0, "prev_close": 0.0, "iv": 0.0, "volume": 0, "oi": 0}
    row = _cboe_row(p)
    if row and (row[0] > 0 or row[1] > 0 or row[2] > 0 or row[3] > 0):
        bid, ask, last, prev, iv, vol, oi, delta, gamma, theta, vega = row
        out.update(bid=bid, ask=ask, last=last or prev, prev_close=prev, iv=_clean_iv(iv), volume=int(vol),
                   oi=int(oi), delta=delta, gamma=gamma, theta=theta, vega=vega)
        return out
    calls, puts = _chain(p["underlying"], p["expiration"])
    df = calls if p["type"] == "C" else puts
    if df is None or getattr(df, "empty", True):
        return out
    ys = yahoo_option_symbol(occ_symbol(p["underlying"], p["expiration"], p["type"], p["strike"]))
    row = df[df["contractSymbol"] == ys]
    if row.empty:
        row = df[(df["strike"] - p["strike"]).abs() < 1e-6]
    if row.empty:
        return out
    r = row.iloc[0]

    def f(c):
        try:
            v = float(r.get(c))
            return 0.0 if math.isnan(v) else v
        except Exception:
            return 0.0
    last, chg = f("lastPrice"), f("change")
    out.update(bid=f("bid"), ask=f("ask"), last=last, prev_close=max(last - chg, 0.0),
               iv=_clean_iv(f("impliedVolatility")), volume=int(f("volume")), oi=int(f("openInterest")))
    return out


def _intrinsic_at_expiry(und: str, cp: str, strike: float, exp: str) -> float:
    def fetch():
        try:
            d = date.fromisoformat(exp)
            h = _yf().Ticker(_ysym(und)).history(start=(d - timedelta(days=6)).isoformat(),
                                                 end=(d + timedelta(days=1)).isoformat(), auto_adjust=False)
            if h is None or h.empty:
                return None
            return float(h["Close"].iloc[-1])
        except Exception:
            return None
    px = _cached(("expclose", und.upper(), exp), 86400 * 30, fetch)
    if px is None:
        return 0.0
    return max(px - strike, 0.0) if cp == "C" else max(strike - px, 0.0)


def _mark(bid, ask, last):
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    return last or bid or ask or 0.0


def _quote_item(sym: str) -> dict:
    p = parse_option_symbol(sym) if (len(sym) > 8 or sym.startswith(".")) else None
    if p:
        occ = occ_symbol(p["underlying"], p["expiration"], p["type"], p["strike"])
        q = option_quote(occ)
        m = _mark(q.get("bid", 0), q.get("ask", 0), q.get("last", 0))
        return {"symbol": occ, "instrument-type": "Equity Option", "bid": q.get("bid", 0), "ask": q.get("ask", 0),
                "mid": m, "mark": m, "last": q.get("last", 0), "prev-close": q.get("prev_close", 0),
                "close": q.get("prev_close", 0), "volatility": q.get("iv", 0), "implied-volatility": q.get("iv", 0),
                "volume": q.get("volume", 0), "open-interest": q.get("oi", 0), "day-volume": q.get("volume", 0),
                "delta": q.get("delta"), "gamma": q.get("gamma"), "theta": q.get("theta"), "vega": q.get("vega"),
                "updated-at": datetime.now(timezone.utc).isoformat()}
    q = equity_quote(sym)
    last = q.get("last", 0)
    return {"symbol": sym.upper(), "instrument-type": "Equity", "bid": last, "ask": last, "mid": last, "mark": last,
            "last": last, "prev-close": q.get("prev_close", 0), "close": last, "day-high-price": q.get("high", 0),
            "day-low-price": q.get("low", 0), "volume": q.get("volume", 0),
            "updated-at": datetime.now(timezone.utc).isoformat()}


def _metrics(sym: str) -> dict:
    def fetch():
        out = {"symbol": sym.upper()}
        yf = _yf()
        tk = yf.Ticker(_ysym(sym))
        # IV index: Cboe IV30; otherwise ATM IV of the expiration closest to 30 DTE
        book = _cboe_book(sym)
        if book and 0.01 < book["iv30"] < 5:
            out["implied-volatility-index"] = round(book["iv30"], 4)
        try:
            exps = [] if "implied-volatility-index" in out else expirations(sym)
            if exps:
                target = date.today() + timedelta(days=30)
                exp = min(exps, key=lambda e: abs((date.fromisoformat(e) - target).days))
                calls, puts = _chain(sym, exp)
                spot = equity_quote(sym).get("last") or 0
                ivs = []
                for df in (calls, puts):
                    if df is not None and not df.empty and spot:
                        r = df.iloc[(df["strike"] - spot).abs().argsort()[:2]]
                        ivs += [float(x) for x in r["impliedVolatility"] if x and x == x and 0.01 < x < 5]
                if ivs:
                    out["implied-volatility-index"] = round(sum(ivs) / len(ivs), 4)
        except Exception:
            pass
        try:
            h = tk.history(period="3mo", auto_adjust=False)
            if h is not None and len(h) > 21:
                rets = h["Close"].pct_change().dropna().tail(21)
                out["historical-volatility-30-day"] = round(float(rets.std() * math.sqrt(252)), 4)
        except Exception:
            pass
        try:
            cal = tk.calendar
            ed = None
            if isinstance(cal, dict):
                v = cal.get("Earnings Date")
                if isinstance(v, (list, tuple)) and v:
                    ed = v[0]
                elif v:
                    ed = v
            if ed is not None:
                ed = ed if isinstance(ed, date) else datetime.fromisoformat(str(ed)[:10]).date()
                if ed >= date.today() - timedelta(days=1):
                    out["earnings"] = {"expected-report-date": ed.isoformat(), "time-of-day": ""}
        except Exception:
            pass
        return out
    return _cached(("metrics", sym.upper()), 3600, fetch)


def _nested_chain(und: str) -> dict:
    def fetch():
        by_exp = _cboe_strikes(und)
        if not by_exp:                                   # Yahoo: one request per expiration
            for e in expirations(und):
                if (date.fromisoformat(e) - date.today()).days > 400:
                    continue
                calls, puts = _chain(und, e)
                by_exp[e] = set(([] if calls is None else list(calls["strike"])) +
                                ([] if puts is None else list(puts["strike"])))
        items = []
        for e in sorted(by_exp):
            d = date.fromisoformat(e)
            if not 0 <= (d - date.today()).days <= 400:
                continue
            strikes = sorted(by_exp[e])
            items.append({
                "expiration-date": e, "days-to-expiration": (d - date.today()).days,
                "expiration-type": "Regular" if (d.weekday() == 4 and 15 <= d.day <= 21) else "Weekly",
                "settlement-type": "PM",
                "strikes": [{"strike-price": f"{k:.3f}".rstrip("0").rstrip("."),
                             "call": occ_symbol(und, e, "C", k), "put": occ_symbol(und, e, "P", k),
                             "call-streamer-symbol": streamer_symbol(und, e, "C", k),
                             "put-streamer-symbol": streamer_symbol(und, e, "P", k)} for k in strikes],
            })
        return {"data": {"items": [{"underlying-symbol": und.upper(), "root-symbol": und.upper(),
                                    "option-chain-type": "Standard", "shares-per-contract": 100,
                                    "expirations": items}]}}
    return _cached(("nested", und.upper()), 600, fetch)


# ---------------------------------------------------------------------------
# tastytrade-shaped REST emulation
# ---------------------------------------------------------------------------
def _split(params: dict | None, *names) -> list[str]:
    out: list[str] = []
    for n in names:
        v = (params or {}).get(n)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for x in v:
                out += [s for s in str(x).split(",") if s.strip()]
        else:
            out += [s for s in str(v).split(",") if s.strip()]
    return [s.strip() for s in out]


def positions_payload() -> list[dict]:
    st = replay()
    out = []
    for k, p in st["positions"].items():
        q = p["qty"]
        is_opt = p["asset"] == "option"
        prev = 0.0
        if is_opt:
            oq = option_quote(k)
            prev = oq.get("prev_close") or _mark(oq.get("bid", 0), oq.get("ask", 0), oq.get("last", 0))
        else:
            prev = equity_quote(p["symbol"]).get("prev_close", 0)
        opened = _iso_tt(p["opened"])
        item = {
            "account-number": ACCOUNT, "symbol": k, "instrument-type": "Equity Option" if is_opt else "Equity",
            "underlying-symbol": p["symbol"], "quantity": abs(q) if not float(abs(q)).is_integer() else int(abs(q)),
            "quantity-direction": "Long" if q > 0 else "Short", "close-price": f"{prev:.4f}",
            "average-open-price": f"{p['avg']:.4f}", "multiplier": int(p["multiplier"]),
            "cost-effect": "Debit" if q > 0 else "Credit", "created-at": opened, "updated-at": opened,
            "is-suppressed": False, "is-frozen": False, "realized-day-gain": "0.0", "realized-today": "0.0",
        }
        if is_opt:
            item["streamer-symbol"] = streamer_symbol(p["symbol"], p["expiration"], p["option_type"], p["strike"])
            item["expires-at"] = p["expiration"] + "T20:15:00.000+00:00"
        else:
            item["streamer-symbol"] = p["symbol"]
        out.append(item)
    return out


def balances_payload() -> dict:
    st = replay()
    mv = 0.0
    for k, p in st["positions"].items():
        if p["asset"] == "option":
            oq = option_quote(k)
            px = _mark(oq.get("bid", 0), oq.get("ask", 0), oq.get("last", 0))
        else:
            px = equity_quote(p["symbol"]).get("last", 0)
        mv += p["qty"] * px * p["multiplier"]
    cash = st["cash"]
    nlv = cash + mv
    return {"account-number": ACCOUNT, "cash-balance": f"{cash:.2f}", "net-liquidating-value": f"{nlv:.2f}",
            "long-equity-value": f"{max(mv, 0):.2f}", "derivative-buying-power": f"{max(cash, 0):.2f}",
            "equity-buying-power": f"{max(cash, 0):.2f}", "maintenance-requirement": "0.00",
            "day-trading-buying-power": f"{max(cash, 0):.2f}", "realized-profit-loss": f"{st['realized']:.2f}",
            "starting-cash": f"{st['starting_cash']:.2f}"}


def handle_get(path: str, params: dict | None = None) -> dict:
    path = path.split("?", 1)[0].rstrip("/")
    if path == "/customers/me/accounts":
        return {"data": {"items": [{"account": {"account-number": ACCOUNT, "nickname": "Manual",
                                                "account-type-name": "Individual", "margin-or-cash": "Cash"},
                                    "authority-level": "owner"}]}}
    m = re.match(r"^/accounts/[^/]+/(positions|balances|transactions|orders/live|orders)$", path)
    if m:
        what = m.group(1)
        if what == "positions":
            return {"data": {"items": positions_payload()}}
        if what == "balances":
            return {"data": balances_payload()}
        if what.startswith("orders"):
            return {"data": {"items": []}, "pagination": {"total-items": 0}}
        txns = replay()["transactions"]
        sd = (params or {}).get("start-date")
        ed = (params or {}).get("end-date")
        if sd:
            txns = [t for t in txns if t["transaction-date"] >= str(sd)[:10]]
        if ed:
            txns = [t for t in txns if t["transaction-date"] <= str(ed)[:10]]
        if str((params or {}).get("sort", "Desc")).lower().startswith("d"):
            txns = list(reversed(txns))
        per = int((params or {}).get("per-page") or 250)
        page = int((params or {}).get("page-offset") or 0)
        chunk = txns[page * per:(page + 1) * per]
        return {"data": {"items": chunk}, "pagination": {"total-items": len(txns), "per-page": per,
                                                        "page-offset": page}}
    if path == "/market-data/by-type":
        eqs = _split(params, "equity", "index")
        if len(eqs) > 3:
            equity_quotes_batch(eqs)          # warm the cache in one request
        syms = eqs + _split(params, "equity-option", "cryptocurrency", "future", "future-option")
        return {"data": {"items": [_quote_item(s) for s in syms]}}
    if path == "/market-data":
        syms = _split(params, "symbols[]", "symbols", "symbol")
        return {"data": {"items": [_quote_item(s) for s in syms]}}
    m = re.match(r"^/market-data/([^/]+)/quotes$", path)
    if m:
        return {"data": _quote_item(m.group(1))}
    if path == "/market-metrics":
        syms = _split(params, "symbols", "symbols[]")
        return {"data": {"items": [_metrics(s) for s in syms]}}
    m = re.match(r"^/option-chains/([^/]+)(/nested)?$", path)
    if m:
        return _nested_chain(m.group(1))
    if path in ("/api-quote-tokens", "/quote-streamer-tokens"):
        raise LocalModeError("Live streaming needs a tastytrade connection (manual mode refreshes on a timer).")
    raise LocalModeError(f"{path} is not available in manual mode")


def summary() -> dict:
    st = replay()
    return {"trades": len(load_trades()), "open_positions": len(st["positions"]),
            "cash": round(st["cash"], 2), "starting_cash": round(st["starting_cash"], 2),
            "realized": round(st["realized"], 2), "orphan_closes": len(st.get("orphan_closes") or [])}

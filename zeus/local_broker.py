"""Everything that is not tastytrade: uploaded trades, Alpaca, Public, free quotes.

The rest of ZEUS makes tastytrade REST calls. Without a tastytrade account this
module answers them in tastytrade's shapes. Positions, balances, fills and open
orders come from the account source (Settings → Broker & data):

    manual   your uploaded or typed-in trades, replayed FIFO (below)
    alpaca   your Alpaca account (paper or live), through zeus_brokers
    public   your Public account, through zeus_brokers

and quotes, chains and Greeks from the market-data source: Alpaca or Public
when one is chosen, with the free sources below for anything it lacks.

With uploaded trades this module replays them and answers:

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


def _account_broker():
    """The Alpaca / Public connector when it holds the account, else None (uploaded trades)."""
    if hc.account_source() not in ("alpaca", "public"):
        return None
    try:
        import zeus_brokers
        return zeus_brokers.account()
    except Exception:
        return None


def _market_broker():
    """The Alpaca / Public connector when it serves quotes, else None (free sources).
    After a rate-limit answer (429) the broker rests for a minute and the free sources answer."""
    src = hc.market_source()
    if src not in ("alpaca", "public"):
        return None
    last = _broker_errors.get(src)
    if last and " 429" in last[1] and time.time() - last[0] < 60:
        return None
    try:
        import zeus_brokers
        return zeus_brokers.market()
    except Exception:
        return None


_broker_errors: dict = {}


def _broker_failed(b, e: Exception) -> None:
    """Remember the last error per broker (Settings shows it) and carry on with the free sources."""
    _broker_errors[getattr(b, "name", "?")] = (time.time(), f"{type(e).__name__}: {e}"[:300])


def broker_errors() -> dict:
    now = time.time()
    return {k: v[1] for k, v in _broker_errors.items() if now - v[0] < 900}


def account_label() -> str:
    b = _account_broker()
    return b.account_label if b else ACCOUNT


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
    try:
        from zoneinfo import ZoneInfo
        utc = dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:                  # no time-zone database (some Windows installs): EDT is close enough
        utc = dt + timedelta(hours=4)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def replay(today: date | None = None) -> dict:
    """→ {'transactions': [...], 'positions': {key: info}, 'cash': float, …} for the account source:
    uploaded trades, or the fills of the Alpaca / Public account."""
    today = today or date.today()
    b = _account_broker()
    if b is not None:
        try:
            trades = b.trades()
        except Exception as e:
            _broker_failed(b, e)
            trades = []
        key = (b.name, len(trades), trades[-1]["id"] if trades else "", today.isoformat())
        return replay_trades(trades, key, today, label=b.account_label)
    key = ("manual", trades_path().stat().st_mtime if trades_path().exists() else 0, today.isoformat())
    return replay_trades(load_trades(), key, today)


def replay_trades(trades: list[dict], key, today: date | None = None, label: str = ACCOUNT) -> dict:
    today = today or date.today()
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
            txns.append(_txn(label, txn_id, k, meta[k], "Receive Deliver", "Expiration",
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
        txns.append(_txn(label, txn_id, k, meta[k], "Trade", action, action, t["qty"], t["price"],
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


def _txn(label, tid, key, meta, ttype, subtype, action, qty, price, value, effect, fees, when, oid, desc):
    is_opt = meta["asset"] == "option"
    net = value - fees if effect == "Credit" else value + fees
    return {
        "id": tid, "account-number": label, "symbol": key,
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


_INDEXES = {"VIX", "SPX", "NDX", "RUT", "DJX", "VIX9D", "VVIX", "TNX", "XSP", "OEX", "XEO"}


def _is_index(sym: str) -> bool:
    s = sym.upper()
    return s.startswith(("^", "$")) or s in _INDEXES


def _broker_equities(symbols: list[str]) -> dict:
    """Stock quotes from Alpaca / Public when that is the market-data source ({} otherwise)."""
    b = _market_broker()
    syms = [s for s in symbols if s and not _is_index(s)]
    if b is None or not syms:
        return {}
    try:
        got = b.stock_snapshots(syms) if b.name == "alpaca" else b.quotes(syms)
    except Exception as e:
        _broker_failed(b, e)
        return {}
    return {k: v for k, v in got.items() if v and v.get("last")}


def equity_quote(sym: str) -> dict:
    q = _broker_equities([sym.upper()]).get(sym.upper())
    if q:
        return q
    return _free_equity_quote(sym)


def _free_equity_quote(sym: str) -> dict:
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
    out: dict = _broker_equities(syms)
    syms = [s for s in syms if s not in out]
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
    book = _cached(("cboe", u), 120, fetch)
    if book is None:                      # Cboe busy (429) or down: try again in 15 s, not 2 min
        with _lock:
            hit = _cache.get(("cboe", u))
            if hit and hit[1] is None:
                _cache[("cboe", u)] = (time.time() - 105, None)
    return book


def _yahoo_book(und: str) -> dict | None:
    """The shape of _cboe_book from Yahoo Finance option chains: one expiration near each of 25, 40,
    55, 75 and 95 days out. Used for the sample portfolio when Cboe is busy."""
    u = und.upper().lstrip("$^")
    spot = _free_equity_quote(u).get("last") or 0
    if not spot:
        return None
    today = date.today()
    rows = {}
    window = [e for e in expirations(u) if 15 <= (date.fromisoformat(e) - today).days <= 110]
    picks = []
    for target in (25, 40, 55, 75, 95):    # one chain near each horizon the sample uses
        if window:
            e = min(window, key=lambda x: abs((date.fromisoformat(x) - today).days - target))
            if e not in picks:
                picks.append(e)
    for e in picks:
        calls, puts = _chain(u, e)
        ymd = date.fromisoformat(e).strftime("%y%m%d")
        for cp, df in (("C", calls), ("P", puts)):
            if df is None or getattr(df, "empty", True):
                continue
            for _, r in df.iterrows():
                try:
                    k = int(round(float(r["strike"]) * 1000))
                    bid, ask, last = (float(r.get(c) or 0) for c in ("bid", "ask", "lastPrice"))
                    iv = float(r.get("impliedVolatility") or 0)
                except (TypeError, ValueError):
                    continue
                vals = [x if x == x else 0.0 for x in (bid, ask, last, iv)]   # NaN → 0
                rows[(u, ymd, cp, k)] = (vals[0], vals[1], vals[2], vals[2], vals[3], 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return {"spot": spot, "iv30": 0.0, "rows": rows} if rows else None


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


def _broker_strikes(und: str) -> dict:
    b = _market_broker()
    if b is None or _is_index(und):
        return {}
    try:
        return {e: k for e, k in b.strikes(und).items() if e >= date.today().isoformat()}
    except Exception as e:
        _broker_failed(b, e)
        return {}


def expirations(und: str) -> list[str]:
    def fetch():
        ex = sorted(_cboe_strikes(und)) or sorted(_broker_strikes(und))
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


def _broker_options(occs: list[str]) -> dict:
    """{padded OCC: quote} from Alpaca / Public when that is the market-data source ({} otherwise)."""
    b = _market_broker()
    if b is None or not occs:
        return {}
    try:
        got = b.option_snapshots(occs) if b.name == "alpaca" else b.quotes(occs)
    except Exception as e:
        _broker_failed(b, e)
        return {}
    return {k: v for k, v in got.items() if v and (v.get("bid") or v.get("ask") or v.get("last"))}


def option_quote(occ: str) -> dict:
    p = parse_option_symbol(occ)
    if not p:
        return {}
    out = {"bid": 0.0, "ask": 0.0, "last": 0.0, "prev_close": 0.0, "iv": 0.0, "volume": 0, "oi": 0}
    key = occ_symbol(p["underlying"], p["expiration"], p["type"], p["strike"])
    bq = _broker_options([key]).get(key)
    if bq:
        out.update(bq)
        if not out.get("last"):
            out["last"] = out.get("prev_close") or 0.0
        if not out.get("prev_close") or not out.get("iv") or "delta" not in out:
            row = _cboe_row(p)          # fill what the broker left out (prior close, IV, Greeks) from Cboe
            if row:
                bid, ask, last, prev, iv, vol, oi, delta, gamma, theta, vega = row
                out["prev_close"] = out.get("prev_close") or prev
                out["iv"] = out.get("iv") or _clean_iv(iv)
                out["oi"] = out.get("oi") or int(oi)
                if "delta" not in out:
                    out.update(delta=delta, gamma=gamma, theta=theta, vega=vega)
        return out
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
        by_exp = _cboe_strikes(und) or _broker_strikes(und)
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


def _opened_map(st: dict) -> dict:
    return {k: _iso_tt(p["opened"]) for k, p in (st.get("positions") or {}).items() if p.get("opened")}


def positions_payload() -> list[dict]:
    b = _account_broker()
    if b is not None:
        try:
            opened = _opened_map(replay())
        except Exception:
            opened = {}
        return b.tt_positions(opened)
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
    b = _account_broker()
    if b is not None:
        return b.tt_balances()
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


def _warm(equities: list[str], options: list[str]) -> None:
    """Fetch a whole batch of quotes in as few requests as possible before answering item by item."""
    if len(equities) > 3:
        equity_quotes_batch(equities)
    if options and _market_broker() is not None:
        occs = []
        for o in options:
            p = parse_option_symbol(o)
            if p:
                occs.append(occ_symbol(p["underlying"], p["expiration"], p["type"], p["strike"]))
        _broker_options(occs)


def handle_get(path: str, params: dict | None = None) -> dict:
    path = path.split("?", 1)[0].rstrip("/")
    if path == "/customers/me/accounts":
        b = _account_broker()
        nick = (b.label + (" (paper)" if getattr(b, "paper", False) else "")) if b else "Manual"
        return {"data": {"items": [{"account": {"account-number": account_label(), "nickname": nick,
                                                "account-type-name": "Individual",
                                                "margin-or-cash": "Margin" if b else "Cash"},
                                    "authority-level": "owner"}]}}
    m = re.match(r"^/accounts/[^/]+/(positions|balances|transactions|orders/live|orders)$", path)
    if m:
        what = m.group(1)
        if what == "positions":
            return {"data": {"items": positions_payload()}}
        if what == "balances":
            return {"data": balances_payload()}
        if what.startswith("orders"):
            b = _account_broker()
            items = []
            if b is not None:
                try:
                    items = b.tt_orders()
                except Exception as e:
                    _broker_failed(b, e)
            return {"data": {"items": items}, "pagination": {"total-items": len(items)}}
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
        opts = _split(params, "equity-option")
        _warm(eqs, opts)
        syms = eqs + opts + _split(params, "cryptocurrency", "future", "future-option")
        return {"data": {"items": [_quote_item(s) for s in syms]}}
    if path == "/market-data":
        syms = _split(params, "symbols[]", "symbols", "symbol")
        opts = [x for x in syms if parse_option_symbol(x) and (len(x) > 8 or x.startswith("."))]
        _warm([x for x in syms if x not in opts], opts)
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
    b = _account_broker()
    if b is not None:
        out = {"source": b.name, "account": b.account_label, "paper": bool(getattr(b, "paper", False))}
        try:
            out["open_positions"] = len(b.tt_positions())
            bal = b.tt_balances()
            out["nlv"] = float(bal.get("net-liquidating-value") or 0)
            out["cash"] = float(bal.get("cash-balance") or 0)
        except Exception as e:
            _broker_failed(b, e)
            out["error"] = str(e)[:200]
        return out
    st = replay()
    return {"source": "manual", "trades": len(load_trades()), "sample": sample_count(), "open_positions": len(st["positions"]),
            "cash": round(st["cash"], 2), "starting_cash": round(st["starting_cash"], 2),
            "realized": round(st["realized"], 2), "orphan_closes": len(st.get("orphan_closes") or [])}


def free_test() -> dict:
    """Settings → test the free sources: a Yahoo stock quote and Cboe option chains (no account needed)."""
    t0 = time.time()
    out: dict = {"ok": False, "delay": "~15 min"}
    q = _free_equity_quote("AAPL")
    out["AAPL"] = q.get("last") or None
    book = _cboe_book("AAPL")
    out["AAPL_option_quotes"] = len(book["rows"]) if book else 0
    out["with_greeks"] = sum(1 for r in book["rows"].values() if r[7]) if book else 0
    spx = _cboe_book("SPX")
    out["SPX_option_quotes"] = len(spx["rows"]) if spx else 0
    out["ok"] = bool(out["AAPL"]) and bool(out["AAPL_option_quotes"])
    out["ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------------------------------------------------------------------
# Sample portfolio: a first look at ZEUS that is not an empty table.
# Strikes, expirations and prices are picked from today's free Cboe quotes, so
# P/L, Greeks and the risk views show sensible numbers. Every sample trade has
# an id starting with "sample-" and is removed in one click.
# ---------------------------------------------------------------------------
SAMPLE_PREFIX = "sample-"
SAMPLE_CASH = "50000"


def _sample_contract(book: dict, und: str, cp: str, target: float, dte_lo: int, dte_hi: int, exp: str | None = None):
    """(expiration ISO, strike, mid) of the listed contract nearest `target` in the DTE window, or None."""
    today = date.today()
    best = None
    for (root, ymd, c, k), row in book["rows"].items():
        if root != und or c != cp:
            continue
        e = _ymd(ymd)
        if exp and e != exp:
            continue
        dte = (date.fromisoformat(e) - today).days
        if not dte_lo <= dte <= dte_hi:
            continue
        bid, ask, last = row[0], row[1], row[2]
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif last > 0:                    # Yahoo blanks bid/ask at night and on weekends
            mid = last
        else:
            continue
        monthly = date.fromisoformat(e).weekday() == 4 and 15 <= date.fromisoformat(e).day <= 21
        score = (abs(k / 1000.0 - target) / max(target, 1e-9), 0 if monthly else 1, dte)
        if best is None or score < best[0]:
            best = (score, e, k / 1000.0, round(mid, 2))
    return best[1:] if best else None


def sample_trades() -> list[dict]:
    """Build the sample portfolio from live free quotes. Raises if the quotes can't be fetched."""
    today = date.today()

    def when(days_ago: int, hhmm: str = "10:30") -> str:
        d = today - timedelta(days=days_ago)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return f"{d.isoformat()}T{hhmm}:00"

    out: list[dict] = []
    n = [0]

    def add(group: str, **kw):
        n[0] += 1
        t = {"id": f"{SAMPLE_PREFIX}{group}-{n[0]}", "order_id": f"{SAMPLE_PREFIX}{group}", "note": "sample",
             "fees": 0.0, "effect": "open", **kw}
        out.append(normalize_trade(t))

    books = {}
    for und in ("SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"):
        b = _cboe_book(und)
        if not (b and b.get("spot")):
            try:
                b = _yahoo_book(und)          # Cboe busy: Yahoo's chains will do
            except Exception:
                b = None
        if b and b.get("spot"):
            books[und] = b
    if len(books) < 3:
        raise LocalModeError("could not reach the free option quotes (Cboe or Yahoo) to price the sample portfolio")

    # 1. stock: AAPL shares bought three weeks ago, a little below today's price
    if "AAPL" in books:
        spot = books["AAPL"]["spot"]
        add("aapl-stock", date=when(21), symbol="AAPL", asset="stock", side="buy", qty=30, price=round(spot * 0.95, 2))
    # 2. SPY bull put spread (credit), opened ten days ago for more than it is worth now
    if "SPY" in books:
        b, spot = books["SPY"], books["SPY"]["spot"]
        sp = _sample_contract(b, "SPY", "P", spot * 0.95, 25, 60)
        lp = sp and _sample_contract(b, "SPY", "P", spot * 0.93, 25, 60, exp=sp[0])
        if sp and lp and sp[1] > lp[1]:
            add("spy-bps", date=when(10), symbol="SPY", asset="option", option_type="P", strike=sp[1], expiration=sp[0],
                side="sell", qty=2, price=round(sp[2] * 1.3, 2))
            add("spy-bps", date=when(10), symbol="SPY", asset="option", option_type="P", strike=lp[1], expiration=lp[0],
                side="buy", qty=2, price=round(max(lp[2] * 1.3, 0.05), 2))
    # 3. NVDA bull call spread (debit), a bit underwater
    if "NVDA" in books:
        b, spot = books["NVDA"], books["NVDA"]["spot"]
        lc = _sample_contract(b, "NVDA", "C", spot, 25, 70)
        sc = lc and _sample_contract(b, "NVDA", "C", spot * 1.08, 25, 70, exp=lc[0])
        if lc and sc and sc[1] > lc[1]:
            add("nvda-bcs", date=when(8, "11:05"), symbol="NVDA", asset="option", option_type="C", strike=lc[1],
                expiration=lc[0], side="buy", qty=3, price=round(lc[2] * 1.12, 2))
            add("nvda-bcs", date=when(8, "11:05"), symbol="NVDA", asset="option", option_type="C", strike=sc[1],
                expiration=sc[0], side="sell", qty=3, price=round(sc[2] * 1.05, 2))
    # 4. QQQ iron condor
    if "QQQ" in books:
        b, spot = books["QQQ"], books["QQQ"]["spot"]
        sp = _sample_contract(b, "QQQ", "P", spot * 0.94, 30, 60)
        if sp:
            e = sp[0]
            lp = _sample_contract(b, "QQQ", "P", spot * 0.91, 30, 60, exp=e)
            sc = _sample_contract(b, "QQQ", "C", spot * 1.06, 30, 60, exp=e)
            lc = _sample_contract(b, "QQQ", "C", spot * 1.09, 30, 60, exp=e)
            if lp and sc and lc and lp[1] < sp[1] < sc[1] < lc[1]:
                for leg, side in ((sp, "sell"), (lp, "buy"), (sc, "sell"), (lc, "buy")):
                    cp = "P" if leg in (sp, lp) else "C"
                    add("qqq-ic", date=when(12, "14:20"), symbol="QQQ", asset="option", option_type=cp, strike=leg[1],
                        expiration=e, side=side, qty=1, price=round(leg[2] * (1.15 if side == "sell" else 1.0), 2))
    # 5. MSFT long call
    if "MSFT" in books:
        b, spot = books["MSFT"], books["MSFT"]["spot"]
        c = _sample_contract(b, "MSFT", "C", spot, 40, 100)
        if c:
            add("msft-call", date=when(15), symbol="MSFT", asset="option", option_type="C", strike=c[1], expiration=c[0],
                side="buy", qty=1, price=round(c[2] * 0.9, 2))
    # 6. TSLA cash-secured put
    if "TSLA" in books:
        b, spot = books["TSLA"], books["TSLA"]["spot"]
        p = _sample_contract(b, "TSLA", "P", spot * 0.88, 20, 50)
        if p:
            add("tsla-csp", date=when(6, "09:50"), symbol="TSLA", asset="option", option_type="P", strike=p[1],
                expiration=p[0], side="sell", qty=1, price=round(p[2] * 1.25, 2))
    # 7. a finished trade for the journal: AMD shares, bought and sold at a profit
    amd = _free_equity_quote("AMD").get("last") or 0
    if amd:
        add("amd-swing", date=when(60), symbol="AMD", asset="stock", side="buy", qty=20, price=round(amd * 0.86, 2))
        t = {"id": f"{SAMPLE_PREFIX}amd-swing-close", "order_id": f"{SAMPLE_PREFIX}amd-swing-close", "note": "sample",
             "date": when(28, "15:30"), "symbol": "AMD", "asset": "stock", "side": "sell", "effect": "close", "qty": 20,
             "price": round(amd * 0.95, 2), "fees": 0.0}
        out.append(normalize_trade(t))
    return out


def sample_count() -> int:
    return sum(1 for t in load_trades() if str(t.get("id", "")).startswith(SAMPLE_PREFIX))


def load_sample() -> dict:
    trades = sample_trades()
    with _lock:
        cur = [t for t in load_trades() if not str(t.get("id", "")).startswith(SAMPLE_PREFIX)]
        save_trades(sorted(cur + trades, key=lambda t: (t.get("date", ""), t.get("id", ""))))
    # a round starting balance, so buying power and position sizes look like a real account
    cash_set = False
    if str(hc.get("manual_starting_cash") or "0").strip() in ("", "0", "0.0"):
        hc.update({"manual_starting_cash": SAMPLE_CASH})
        cash_set = True
        (hc.home() / "sample_cash").write_text("1", encoding="utf-8")
    _invalidate()
    return {"ok": True, "added": len(trades), "starting_cash_set": cash_set}


def remove_sample() -> dict:
    with _lock:
        cur = load_trades()
        keep = [t for t in cur if not str(t.get("id", "")).startswith(SAMPLE_PREFIX)]
        save_trades(keep)
    marker = hc.home() / "sample_cash"
    if marker.exists():
        if str(hc.get("manual_starting_cash") or "") == SAMPLE_CASH:
            hc.update({"manual_starting_cash": ""})
        marker.unlink()
    _invalidate()
    return {"ok": True, "removed": len(cur) - len(keep)}

"""Trade Journal — Auto-generated decision entries for real TastyTrade trades.

Two append-only JSONL files in trade_journal/data/:
  entries_open.jsonl   — one row per opened trade (pending until user fills)
  entries_close.jsonl  — one row per closed trade (pending until user fills)
A separate JSON file tracks user-added custom chip options:
  entries_options.json — {section_name: [user-added chip values]}

Source of trades: trade_journal/trades_raw.json (refreshed hourly by
fetch_trades.py). We group transactions into "trades" by symbol, then
match opens with closes FIFO. For each trade we ensure exactly one Open
Entry exists, and (if closed) exactly one Close Entry.
"""
from __future__ import annotations

import datetime
import json
import threading
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import trade_economics as _te

_THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = _THIS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OPEN_PATH = DATA_DIR / "entries_open.jsonl"
CLOSE_PATH = DATA_DIR / "entries_close.jsonl"
OPTIONS_PATH = DATA_DIR / "entries_options.json"
TRADES_RAW_PATH = DATA_DIR / "trades_raw.json"   # inside the runtime data folder (outside the code)
MERGES_PATH = DATA_DIR / "entry_merges.json"   # 作者把同票同天的多单合并成一单(可逆)
SCREENSHOTS_DIR = DATA_DIR / "screenshots"

_save_lock = threading.Lock()


# ── Default chip options (visible by default; user can add more) ───────────

DEFAULT_OPTIONS: dict[str, list[str]] = {
    # Open Entry sections
    "logic_type": [
        "Momentum", "Mean Reversion", "Dip Buy", "Earnings Play",
        "Hedge", "IV Crush", "Theta Decay",
    ],
    "signal_sources": [
        "Winrate", "Barchart", "Benzinga", "OptionStrat",
        "Technical Analysis", "Fundamental", "News", "Chart Pattern",
    ],
    "confidence": [
        "Gambling", "Low", "Medium", "High", "System Signal",
    ],
    "checklist": [
        "Followed system rules",
        "Risk ≤2% of account",
        "R:R ≥1.5",
        "Stop level defined",
        "No earnings in expiry window",
        "Under daily loss limit",
        "Not a revenge trade (after big loss)",
        "Not a FOMO trade (after big win)",
    ],
    "emotion_keys": [
        "excitement", "anxiety", "fomo", "revenge",
        "panic", "extreme_confidence",      # added by user
        "slept_7h", "stable_today",
    ],
    # Close Entry sections
    "close_logic": [
        "Stop loss hit",
        "Take profit hit",
        "Held to expiration",
        "Reduced risk (close part)",
        "Time stop (DTE)",
        "News-driven exit",
    ],
    "mistakes_made": [
        "Chased price",
        "Sized too big",
        "No clear stop",
        "Against trend",
        "Earnings within window",
        "Overconfidence",
        "Doubled down after a loss",      # 亏损后以小博大
        "Followed someone else's idea",   # 听从别人的订单
        "Skipped research",               # 没有做好research
        "Too many open positions",        # 开仓太多
        "Risk too concentrated",          # 风险太集中
        "Moved stop after entry",
        "Closed early on noise",
        "Held losers too long",
    ],
    "rule_changes_midway": [
        "Moved stop further",
        "Added size",
        "Closed early (winner)",
        "Closed early (panic)",
        "Extended target",
        "Nothing — followed plan",
    ],
}


# ── Persistence helpers ───────────────────────────────────────────────────

def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _save_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite_jsonl(path: Path, rows: list[dict]) -> None:
    with _save_lock:
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")


def load_open_entries() -> list[dict]:
    return _load_jsonl(OPEN_PATH)


def load_close_entries() -> list[dict]:
    return _load_jsonl(CLOSE_PATH)


def upsert_open_entry(entry: dict) -> None:
    """Append if no entry has the same trade_id; else replace in place."""
    rows = load_open_entries()
    found = False
    for i, r in enumerate(rows):
        if r.get("trade_id") == entry.get("trade_id"):
            rows[i] = entry
            found = True
            break
    if not found:
        rows.append(entry)
    _rewrite_jsonl(OPEN_PATH, rows)


def upsert_close_entry(entry: dict) -> None:
    rows = load_close_entries()
    found = False
    for i, r in enumerate(rows):
        if r.get("trade_id") == entry.get("trade_id"):
            rows[i] = entry
            found = True
            break
    if not found:
        rows.append(entry)
    _rewrite_jsonl(CLOSE_PATH, rows)


def update_entry_fields(entry_id: str, updates: dict) -> bool:
    """Patch fields on an entry by id (open or close). Returns True if updated."""
    for path, loader in ((OPEN_PATH, load_open_entries), (CLOSE_PATH, load_close_entries)):
        rows = loader()
        for i, r in enumerate(rows):
            if r.get("id") == entry_id:
                rows[i].update(updates)
                rows[i]["updated_at"] = datetime.datetime.now().isoformat()
                # Auto-mark complete if user filled the required summary
                if rows[i].get("one_line_thesis") or rows[i].get("lesson_learned"):
                    rows[i].setdefault("status", "completed")
                _rewrite_jsonl(path, rows)
                return True
    return False


# ── User-added option management ──────────────────────────────────────────

def _load_user_options() -> dict[str, list[str]]:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_user_options(opts: dict[str, list[str]]) -> None:
    with _save_lock:
        OPTIONS_PATH.write_text(json.dumps(opts, indent=2), encoding="utf-8")


def get_all_options() -> dict[str, dict]:
    """Return {section: {defaults: [...], custom: [...]}} for the UI.
    Defaults are immutable; custom entries are user-added and × removable.
    """
    user = _load_user_options()
    out = {}
    for section, defaults in DEFAULT_OPTIONS.items():
        out[section] = {
            "defaults": list(defaults),
            "custom": list(user.get(section, [])),
        }
    return out


def add_custom_option(section: str, value: str) -> bool:
    """Add a user-defined chip to a section. No-op if already present (default
    or custom). Returns True if newly added."""
    value = (value or "").strip()
    if not value or section not in DEFAULT_OPTIONS:
        return False
    if value in DEFAULT_OPTIONS[section]:
        return False
    user = _load_user_options()
    custom = user.setdefault(section, [])
    if value in custom:
        return False
    custom.append(value)
    _save_user_options(user)
    return True


def add_screenshot(entry_id: str, blob_bytes: bytes,
                    caption: str = "") -> Optional[dict]:
    """Save a screenshot blob for an entry, append metadata to the entry's
    `screenshots` list. Returns the screenshot record dict, or None if the
    entry doesn't exist."""
    if not blob_bytes:
        return None
    # Find which file (open or close) the entry lives in
    for path, loader in ((OPEN_PATH, load_open_entries),
                         (CLOSE_PATH, load_close_entries)):
        rows = loader()
        for i, r in enumerate(rows):
            if r.get("id") == entry_id:
                # Persist file under data/screenshots/<entry_id>/<ts>.png
                d = SCREENSHOTS_DIR / entry_id
                d.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                # add a short hex tag so multi-clicks-same-second don't collide
                tag = uuid.uuid4().hex[:6]
                fname = f"{ts}_{tag}.png"
                (d / fname).write_bytes(blob_bytes)
                rec = {
                    "filename": fname,
                    "caption": caption or "",
                    "added_at": datetime.datetime.now().isoformat(),
                    "size": len(blob_bytes),
                }
                rows[i].setdefault("screenshots", []).append(rec)
                rows[i]["updated_at"] = datetime.datetime.now().isoformat()
                _rewrite_jsonl(path, rows)
                return rec
    return None


def remove_screenshot(entry_id: str, filename: str) -> bool:
    """Delete a screenshot file and remove its record from the entry."""
    # safety: keep filenames within the entry's dir
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return False
    for path, loader in ((OPEN_PATH, load_open_entries),
                         (CLOSE_PATH, load_close_entries)):
        rows = loader()
        for i, r in enumerate(rows):
            if r.get("id") == entry_id:
                shots = r.get("screenshots") or []
                new_shots = [s for s in shots if s.get("filename") != filename]
                if len(new_shots) == len(shots):
                    return False
                rows[i]["screenshots"] = new_shots
                rows[i]["updated_at"] = datetime.datetime.now().isoformat()
                _rewrite_jsonl(path, rows)
                fpath = SCREENSHOTS_DIR / entry_id / filename
                try:
                    fpath.unlink()
                except (FileNotFoundError, OSError):
                    pass
                return True
    return False


def screenshot_path(entry_id: str, filename: str) -> Optional[Path]:
    """Resolve a screenshot file path with directory traversal protection."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return None
    p = SCREENSHOTS_DIR / entry_id / filename
    try:
        # Ensure resolved path stays within SCREENSHOTS_DIR
        p_resolved = p.resolve()
        if not str(p_resolved).startswith(str(SCREENSHOTS_DIR.resolve())):
            return None
        if not p_resolved.is_file():
            return None
        return p_resolved
    except (OSError, ValueError):
        return None


def remove_custom_option(section: str, value: str) -> bool:
    """Remove a user-added chip. Defaults cannot be removed (return False)."""
    if section not in DEFAULT_OPTIONS:
        return False
    if value in DEFAULT_OPTIONS[section]:
        return False  # protect defaults
    user = _load_user_options()
    custom = user.get(section, [])
    if value not in custom:
        return False
    custom.remove(value)
    user[section] = custom
    _save_user_options(user)
    return True


# ── Trade grouping: cluster legs into spreads by order-id ─────────────────

def _signed_value(v: Any, effect: str) -> float:
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    e = (effect or "").lower()
    if e == "debit":
        return -abs(x)
    if e == "credit":
        return abs(x)
    return x


def _parse_option_symbol(sym: str) -> Optional[dict]:
    """Parse OCC-style option symbol like 'AAPL  261218C00200000'.
    Returns {underlying, exp(YYMMDD), type(C/P), strike(float)} or None."""
    s = (sym or "").strip()
    if len(s) < 18:
        return None
    try:
        strike = int(s[-8:]) / 1000.0
        opt_type = s[-9]
        exp = s[-15:-9]
        underlying = s[:-15].strip()
        if opt_type not in ("C", "P") or not exp.isdigit() or not underlying:
            return None
        return {"underlying": underlying, "exp": exp,
                "type": opt_type, "strike": strike}
    except (ValueError, IndexError):
        return None


def _short_exp(exp_yymmdd: str) -> str:
    """260618 → 6/18/26"""
    if len(exp_yymmdd) != 6 or not exp_yymmdd.isdigit():
        return exp_yymmdd
    yy = exp_yymmdd[:2]
    mm = int(exp_yymmdd[2:4])
    dd = int(exp_yymmdd[4:6])
    return f"{mm}/{dd}/{yy}"


def _fmt_strike(s: float) -> str:
    return f"{s:.0f}" if s == int(s) else f"{s:g}"


def _make_cluster(order_id: Any, leg_txs: list[dict]) -> Optional[dict]:
    """Aggregate same-order-id transactions into one spread cluster.
    Multiple fills on the same leg (e.g. broker split 2+3) collapse together.
    """
    if not leg_txs:
        return None
    legs_by_key: dict[tuple, dict] = {}
    for raw in leg_txs:
        sym = (raw.get("symbol", "") or "").strip()
        action = raw.get("action", "") or ""
        if not sym or not action:
            continue
        key = (sym, action)
        leg = legs_by_key.setdefault(key, {
            "symbol": sym, "action": action, "quantity": 0.0,
            "value": 0.0, "commission": 0.0, "fees": 0.0, "price": 0.0,
            "_first_at": raw.get("executed-at", ""),
        })
        leg["quantity"] += float(raw.get("quantity", 0) or 0)
        leg["value"] += _signed_value(raw.get("value"), raw.get("value-effect", ""))
        leg["commission"] += _signed_value(raw.get("commission"), raw.get("commission-effect", ""))
        leg["fees"] += (_signed_value(raw.get("clearing-fees"), raw.get("clearing-fees-effect", ""))
                         + _signed_value(raw.get("regulatory-fees"), raw.get("regulatory-fees-effect", ""))
                         + _signed_value(raw.get("other-charge"), raw.get("other-charge-effect", "")))
        # last fill price is fine
        try:
            leg["price"] = float(raw.get("price", 0) or 0)
        except (TypeError, ValueError):
            pass

    legs: list[dict] = []
    for (sym, action), leg in legs_by_key.items():
        info = _parse_option_symbol(sym) or {}
        legs.append({
            "symbol": sym,
            "action": action,
            "underlying": info.get("underlying", sym.split()[0] if sym.split() else ""),
            "type": info.get("type", ""),
            "exp": info.get("exp", ""),
            "strike": info.get("strike", 0.0),
            "quantity": leg["quantity"],
            "price": leg["price"],
            "value": leg["value"],
            "commission": leg["commission"],
            "fees": leg["fees"],
        })
    if not legs:
        return None

    underlying = legs[0]["underlying"]
    is_open = all("Open" in l["action"] for l in legs)
    is_close = all(("Close" in l["action"]) or ("Expir" in l["action"]) for l in legs)
    qty = max(l["quantity"] for l in legs) or 0
    executed_at = min(l.get("_first_at", "") or leg_txs[0].get("executed-at", "")
                       for l in legs_by_key.values()) if legs_by_key else leg_txs[0].get("executed-at", "")

    cluster = {
        "order_id": order_id,
        "executed_at": executed_at,
        "underlying": underlying,
        "is_open": is_open,
        "is_close": is_close,
        "legs": legs,
        "quantity": qty,
        "net_value": round(sum(l["value"] for l in legs), 2),
        "commission": round(sum(l["commission"] for l in legs), 2),
        "fees": round(sum(l["fees"] for l in legs), 2),
    }
    cluster["strategy"] = _classify_cluster(cluster)
    cluster["spread_label"] = _spread_label(cluster)
    cluster["direction"] = _direction(cluster)
    cluster["signature"] = _cluster_signature(cluster)
    return cluster


def _classify_cluster(cluster: dict) -> str:
    legs = cluster["legs"]
    n = len(legs)
    if n == 0:
        return "Empty"
    if n == 1:
        l = legs[0]
        side = "Long" if "Buy" in l["action"] else "Short"
        return f"{side} {l['type'] or 'Option'}"
    if n == 2:
        l0, l1 = sorted(legs, key=lambda x: (x["strike"], x["exp"]))
        same_exp = l0["exp"] == l1["exp"] and l0["exp"]
        same_type = l0["type"] == l1["type"]
        if same_exp and same_type:
            return f"Vertical {l0['type']}"
        if same_exp and not same_type:
            return "Straddle" if l0["strike"] == l1["strike"] else "Strangle"
        if not same_exp and l0["strike"] == l1["strike"]:
            return "Calendar"
        return "Diagonal"
    if n == 3:
        return "Butterfly"
    if n == 4:
        types = sorted([l["type"] for l in legs])
        if types == ["C", "C", "P", "P"]:
            # Check if Iron Butterfly (matching middle strikes) vs Iron Condor
            calls = sorted([l for l in legs if l["type"] == "C"], key=lambda x: x["strike"])
            puts = sorted([l for l in legs if l["type"] == "P"], key=lambda x: x["strike"])
            if calls and puts and calls[0]["strike"] == puts[-1]["strike"]:
                return "Iron Butterfly"
            return "Iron Condor"
        return f"{n}-leg"
    return f"{n}-leg"


def _spread_label(cluster: dict) -> str:
    legs = cluster["legs"]
    if not legs:
        return ""
    if len(legs) == 1:
        l = legs[0]
        return f"{_fmt_strike(l['strike'])}{l['type']} {_short_exp(l['exp'])}"
    if len(legs) == 2:
        l0, l1 = sorted(legs, key=lambda x: x["strike"])
        if l0["type"] == l1["type"] and l0["exp"] == l1["exp"]:
            return f"{_fmt_strike(l0['strike'])}/{_fmt_strike(l1['strike'])}{l0['type']} {_short_exp(l0['exp'])}"
        if l0["exp"] == l1["exp"]:
            return f"{_fmt_strike(l0['strike'])}{l0['type']}+{_fmt_strike(l1['strike'])}{l1['type']} {_short_exp(l0['exp'])}"
        # different exp
        return f"{_fmt_strike(l0['strike'])}{l0['type']} {_short_exp(l0['exp'])}/{_short_exp(l1['exp'])}"
    if len(legs) == 4:
        srt = sorted(legs, key=lambda x: x["strike"])
        parts = [f"{_fmt_strike(l['strike'])}{l['type']}" for l in srt]
        return "/".join(parts) + f" {_short_exp(srt[0]['exp'])}"
    parts = [f"{_fmt_strike(l['strike'])}{l['type']}"
             for l in sorted(legs, key=lambda x: x["strike"])]
    return "/".join(parts) + f" {_short_exp(legs[0]['exp'])}"


def _direction(cluster: dict) -> str:
    """BULL / BEAR / NEUTRAL / ?  — only meaningful for *open* clusters."""
    legs = cluster["legs"]
    if not legs or not cluster.get("is_open"):
        return ""
    if len(legs) == 1:
        l = legs[0]
        is_buy = "Buy" in l["action"]
        if l["type"] == "C":
            return "BULL" if is_buy else "BEAR"
        if l["type"] == "P":
            return "BEAR" if is_buy else "BULL"
        return ""
    if len(legs) == 2 and legs[0]["exp"] == legs[1]["exp"] and legs[0]["type"] == legs[1]["type"]:
        srt = sorted(legs, key=lambda x: x["strike"])
        buy_lower = "Buy" in srt[0]["action"]
        if srt[0]["type"] == "C":
            # Buy lower call = Bull Call Debit; Buy higher call = Bear Call Credit
            return "BULL" if buy_lower else "BEAR"
        # Puts: Buy lower (=sell higher) = Bull Put Credit; Buy higher = Bear Put Debit
        return "BULL" if buy_lower else "BEAR"
    strat = cluster.get("strategy", "")
    if strat in ("Iron Condor", "Iron Butterfly", "Straddle", "Strangle"):
        return "NEUTRAL"
    return ""


def _cluster_signature(cluster: dict) -> str:
    """Stable hashable signature of the *legs* (strike+exp+type set) — used to
    pair an open cluster with its close cluster regardless of order-id."""
    sig = sorted(((l.get("strike") or 0.0), l.get("exp") or "", l.get("type") or "")
                  for l in cluster["legs"])
    # We don't include action — opens become closes when the trade is closed.
    return "|".join(f"{_fmt_strike(s)}{t}{e}" for s, e, t in sig)


# ── 同票同天「合并成一单」(可逆;只重写开仓 tx 的 order-id,复用全部聚类逻辑)──
def load_merge_keys() -> set:
    """{(ticker, open_date)} —— 作者要合并的组。文件缺/坏 → 空集。"""
    try:
        data = json.loads(MERGES_PATH.read_text(encoding="utf-8"))
        return {(str(k[0]).upper(), str(k[1])) for k in data if len(k) == 2}
    except Exception:
        return set()


def save_merge_keys(keys) -> None:
    MERGES_PATH.write_text(json.dumps(sorted([list(k) for k in keys]), ensure_ascii=False),
                           encoding="utf-8")


def add_merge(ticker: str, open_date: str) -> set:
    keys = load_merge_keys(); keys.add((str(ticker).upper(), str(open_date))); save_merge_keys(keys); return keys


def remove_merge(ticker: str, open_date: str) -> set:
    keys = load_merge_keys(); keys.discard((str(ticker).upper(), str(open_date))); save_merge_keys(keys); return keys


def _apply_open_merges(transactions_raw: list[dict], keys: set = None) -> list[dict]:
    """把命中 (ticker, open_date) 的**开仓** tx 重写到共享 order-id,使它们聚成一单。
    只动开仓、不动平仓(平仓按腿 FIFO 自然分摊);原始数据不变(返回新列表)。可逆:
    去掉 merge key 后重同步即拆回。"""
    keys = keys if keys is not None else load_merge_keys()
    if not keys:
        return transactions_raw
    out = []
    for raw in transactions_raw:
        action = raw.get("action") or ""
        sym = (raw.get("symbol") or "")
        und = (raw.get("underlying-symbol") or (sym.split()[0] if sym.split() else "")).upper()
        date = (raw.get("executed-at") or "")[:10]
        if "Open" in action and (und, date) in keys:
            raw = {**raw, "order-id": f"_merged_{und}_{date}"}
        out.append(raw)
    return out


def _group_clusters(transactions_raw: list[dict]) -> list[dict]:
    """Group transactions by order-id → list of clusters (one per spread order)."""
    by_order: dict[Any, list[dict]] = defaultdict(list)
    for raw in transactions_raw:
        if raw.get("instrument-type") not in (None, "Equity Option", "Equity"):
            continue
        oid = raw.get("order-id")
        # Skip non-options for now
        if (raw.get("instrument-type") or "") != "Equity Option":
            continue
        if oid is None:
            # Fall back: treat each tx as its own "order" so we don't drop it
            oid = f"_solo_{raw.get('id', id(raw))}"
        by_order[oid].append(raw)

    clusters = []
    for oid, leg_txs in by_order.items():
        c = _make_cluster(oid, leg_txs)
        if c:
            clusters.append(c)
    clusters.sort(key=lambda c: c.get("executed_at", ""))
    return clusters


def _leg_key(leg: dict) -> tuple:
    """Stable hashable identity of a single leg (regardless of buy/sell direction)."""
    return (
        round(float(leg.get("strike") or 0), 4),
        leg.get("exp") or "",
        leg.get("type") or "",
    )


def group_trades(transactions_raw: list[dict]) -> list[dict]:
    """Cluster legs by order-id into spreads, then track close fills *per leg*
    against open positions.

    Why per-leg: brokers sometimes split a spread close into two single-leg
    orders (different order-ids, sometimes seconds apart). A
    cluster-signature pairing fails on that pattern; per-leg accounting still
    drains the right open position correctly.
    """
    clusters = _group_clusters(transactions_raw)

    # Bucket clusters by underlying — closes can hit any earlier open of the
    # same underlying that still has the matching leg open.
    by_underlying: dict[str, list[dict]] = defaultdict(list)
    for c in clusters:
        by_underlying[c["underlying"]].append(c)

    trades = []
    for underlying, bucket in by_underlying.items():
        # Each bundle: {open, closes:[partial_close_cluster, ...],
        #               leg_remaining: {leg_key → qty open},
        #               total_remaining: float}
        open_bundles: list[dict] = []
        for c in bucket:  # time-sorted
            if c["is_open"]:
                rem = {_leg_key(l): float(l["quantity"] or 0) for l in c["legs"]}
                open_bundles.append({
                    "open": c,
                    "closes": [],
                    "leg_remaining": rem,
                    "total_remaining": c["quantity"],
                })
            elif c["is_close"]:
                # For each close-leg, FIFO-drain matching open-legs across
                # all open bundles.
                # Track per-bundle attribution so we can build a partial
                # close cluster proportional to what each bundle absorbed.
                attrib: dict[int, dict[tuple, float]] = defaultdict(
                    lambda: defaultdict(float))
                for cl in c["legs"]:
                    lk = _leg_key(cl)
                    qty_left = float(cl["quantity"] or 0)
                    for i, bundle in enumerate(open_bundles):
                        if qty_left <= 1e-9:
                            break
                        avail = bundle["leg_remaining"].get(lk, 0)
                        if avail <= 1e-9:
                            continue
                        take = min(avail, qty_left)
                        bundle["leg_remaining"][lk] = avail - take
                        # Update aggregate spread quantity remaining = max
                        # leg_remaining (keeps consistency for label display)
                        bundle["total_remaining"] = max(
                            bundle["leg_remaining"].values()) if bundle["leg_remaining"] else 0
                        attrib[i][lk] += take
                        qty_left -= take
                # Build a partial close cluster per bundle
                for bundle_idx, leg_takes in attrib.items():
                    bundle = open_bundles[bundle_idx]
                    bundle["closes"].append(_partial_close(c, leg_takes))
                # Sweep fully-closed bundles
                still_open = []
                for bundle in open_bundles:
                    fully_closed = (
                        bundle["closes"] and
                        all(q <= 1e-9 for q in bundle["leg_remaining"].values())
                    )
                    if fully_closed:
                        trades.append(_finalize_trade(bundle))
                    else:
                        still_open.append(bundle)
                open_bundles = still_open
        # Anything left is still open
        for bundle in open_bundles:
            trades.append(_finalize_trade(bundle))

    trades.sort(key=lambda t: t.get("open_at", ""), reverse=True)
    return trades


def _partial_close(close_cluster: dict, leg_takes: dict[tuple, float]) -> dict:
    """Build a cluster representing only the `leg_takes` portion of
    close_cluster, attributed to one open bundle. Per-leg quantity-scaled."""
    scaled_legs = []
    total_take_qty = 0.0
    for l in close_cluster["legs"]:
        lk = _leg_key(l)
        take = leg_takes.get(lk, 0)
        if take <= 1e-9:
            continue
        leg_full = float(l["quantity"] or take or 1.0)
        leg_scale = take / leg_full if leg_full else 1.0
        sl = dict(l)
        sl["quantity"] = take
        sl["value"] = round(l["value"] * leg_scale, 4)
        sl["commission"] = round(l["commission"] * leg_scale, 4)
        sl["fees"] = round(l["fees"] * leg_scale, 4)
        scaled_legs.append(sl)
        total_take_qty = max(total_take_qty, take)
    scaled = dict(close_cluster)
    scaled["quantity"] = total_take_qty
    scaled["legs"] = scaled_legs
    scaled["net_value"] = round(sum(l["value"] for l in scaled_legs), 2)
    scaled["commission"] = round(sum(l["commission"] for l in scaled_legs), 4)
    scaled["fees"] = round(sum(l["fees"] for l in scaled_legs), 4)
    return scaled


def _proportional_close(close_cluster: dict, take_qty: float) -> dict:
    """Create a copy of `close_cluster` scaled to `take_qty` (for partial closes)."""
    full_qty = close_cluster.get("quantity") or take_qty or 1.0
    scale = take_qty / full_qty if full_qty else 1.0
    scaled = dict(close_cluster)
    scaled["quantity"] = take_qty
    scaled["net_value"] = round(close_cluster["net_value"] * scale, 2)
    scaled["commission"] = round(close_cluster["commission"] * scale, 2)
    scaled["fees"] = round(close_cluster["fees"] * scale, 2)
    # Scale per-leg quantity/value too so leg display is accurate
    scaled_legs = []
    for l in close_cluster["legs"]:
        leg_full = l["quantity"] or take_qty or 1.0
        leg_scale = take_qty / leg_full if leg_full else 1.0
        sl = dict(l)
        sl["quantity"] = take_qty
        sl["value"] = round(l["value"] * leg_scale, 2)
        sl["commission"] = round(l["commission"] * leg_scale, 2)
        sl["fees"] = round(l["fees"] * leg_scale, 2)
        scaled_legs.append(sl)
    scaled["legs"] = scaled_legs
    return scaled


def _net_credit_per_spread(cluster: dict) -> float:
    """Return per-spread cost in option-quote dollars (positive = credit, negative = debit).
    Uses leg prices: sum of (price * sign) where Buy = -1, Sell = +1.
    """
    if not cluster["legs"]:
        return 0.0
    qty = cluster["quantity"] or 1.0
    # cluster.net_value is total $ for whole order — divide by qty to get per-spread.
    # net_value already has effect-sign baked in (debit negative, credit positive).
    return round(cluster["net_value"] / qty, 4)


def _merge_split_close_clusters(closes: list[dict], window_sec: int = 300) -> list[dict]:
    """Merge close clusters that TastyTrade split into per-leg orders.

    When a broker fills a spread close as two separate single-leg orders
    (different order-ids, but seconds apart), `group_clusters` keeps them
    separate. To the *user* it was one close action — merge them so each
    appears as ONE partial-close cluster.

    Heuristic: two clusters merge iff
      - their executed_at timestamps are within `window_sec` of each other
      - their leg sets are DISJOINT (they cover different legs of the spread)
    """
    if len(closes) <= 1:
        return closes
    closes = sorted(closes, key=lambda c: c.get("executed_at") or "")
    out: list[dict] = []
    i = 0
    while i < len(closes):
        cur = dict(closes[i])
        cur_leg_keys = {_leg_key(l) for l in cur.get("legs") or []}
        j = i + 1
        while j < len(closes):
            nxt = closes[j]
            t1 = cur.get("executed_at") or ""
            t2 = nxt.get("executed_at") or ""
            if not t1 or not t2:
                break
            try:
                # parse ISO timestamps (Z or +00:00 — both supported by fromisoformat
                # after stripping trailing Z)
                from datetime import datetime as _dt
                _to_dt = lambda s: _dt.fromisoformat(s.replace("Z", "+00:00"))
                gap = abs((_to_dt(t2) - _to_dt(t1)).total_seconds())
            except Exception:
                break
            if gap > window_sec:
                break
            nxt_leg_keys = {_leg_key(l) for l in nxt.get("legs") or []}
            if cur_leg_keys & nxt_leg_keys:
                # same leg in both → not a split; they're real separate fills
                break
            # Merge nxt into cur
            cur["legs"] = list(cur["legs"]) + list(nxt["legs"])
            cur["net_value"] = round(float(cur.get("net_value") or 0)
                                     + float(nxt.get("net_value") or 0), 2)
            cur["commission"] = round(float(cur.get("commission") or 0)
                                      + float(nxt.get("commission") or 0), 4)
            cur["fees"] = round(float(cur.get("fees") or 0)
                                + float(nxt.get("fees") or 0), 4)
            # quantity = spread count = max of leg quantities (consistent with
            # how partial closes already define quantity in _partial_close)
            cur["quantity"] = max(float(l.get("quantity") or 0) for l in cur["legs"])
            # Use the LATER timestamp so cluster_close_at reflects when the
            # whole close completed.
            if t2 > t1:
                cur["executed_at"] = t2
            cur_leg_keys |= nxt_leg_keys
            j += 1
        out.append(cur)
        i = j
    return out


def _finalize_trade(bundle: dict) -> dict:
    """Build the final trade dict from one open cluster + matched close clusters."""
    # Merge any TT-split closes (e.g. spread close filled as 2 single-leg orders)
    # before any downstream logic sees them — both PnL math and per-cluster
    # close entries should treat a split as one logical close.
    bundle["closes"] = _merge_split_close_clusters(bundle.get("closes") or [])
    o = bundle["open"]
    closes = bundle["closes"]
    open_legs = o["legs"]
    underlying = o["underlying"]

    open_at = o["executed_at"]
    open_qty = o["quantity"]
    open_per_spread = _net_credit_per_spread(o)
    open_strategy = o["strategy"]
    open_label = o["spread_label"]
    direction = o["direction"]
    sig = o["signature"]

    # PnL: open net + sum close nets + commissions/fees on both sides
    gross = o["net_value"] + sum(c["net_value"] for c in closes)
    fees = (o["commission"] + o["fees"]
            + sum(c["commission"] + c["fees"] for c in closes))
    net_pnl = round(gross + fees, 2)

    # If this trade is fully closed, the most recent close gives close_at + price
    leg_rem = bundle.get("leg_remaining") or {}
    fully_closed = (len(closes) > 0 and
                     all(q <= 1e-9 for q in leg_rem.values()))
    closed = fully_closed
    close_at = closes[-1]["executed_at"] if closes else ""
    close_action_summary = closes[-1]["legs"][0]["action"] if closes and closes[-1]["legs"] else ""
    close_per_spread = _net_credit_per_spread(closes[-1]) if closes else 0.0

    # Multi-leg display fields
    open_action = "Open " + open_strategy
    return {
        "trade_id": f"{underlying}|{sig}|{open_at}|{open_qty:g}",
        "ticker": underlying,
        "underlying": underlying,
        "spread_label": open_label,
        "strategy": open_strategy,
        "direction": direction,
        "signature": sig,
        "symbol": ", ".join(sorted(set(l["symbol"] for l in open_legs))),
        "open_date": open_at[:10] if open_at else "",
        "open_at": open_at,
        "close_date": close_at[:10] if close_at else "",
        "close_at": close_at,
        "status": "closed" if closed else "open",
        "open_action": open_action,
        "close_action": ("Close " + open_strategy) if closes else "",
        "quantity": open_qty,                    # spread quantity
        "open_price": open_per_spread,           # per-spread net
        "close_price": close_per_spread,
        "gross_pnl": round(gross, 2),
        "commissions": round(o["commission"] + sum(c["commission"] for c in closes), 2),
        "fees": round(o["fees"] + sum(c["fees"] for c in closes), 2),
        "net_pnl": net_pnl,
        "open_legs": open_legs,
        "close_clusters": closes,
    }


# ── Entry generation from grouped trades ──────────────────────────────────

def _legs_summary(legs: list[dict]) -> list[dict]:
    """Compact leg list for storage/display in the entry."""
    out = []
    for l in legs:
        out.append({
            "action": l.get("action"),
            "symbol": l.get("symbol"),
            "type": l.get("type"),
            "strike": l.get("strike"),
            "exp": l.get("exp"),
            "quantity": l.get("quantity"),
            "price": l.get("price"),
        })
    return out


def _new_open_entry(trade: dict) -> dict:
    """Build a pending Open Entry skeleton for a trade (spread-level)."""
    return {
        "id": "open-" + uuid.uuid4().hex[:12],
        "trade_id": trade["trade_id"],
        "created_at": datetime.datetime.now().isoformat(),
        "updated_at": "",
        "status": "pending",
        # Auto-filled (read-only in UI) — spread-level
        "ticker": trade["underlying"],
        "strategy": trade["strategy"],          # e.g. "Vertical C", "Iron Condor"
        "spread_label": trade["spread_label"],  # e.g. "44/46C 6/18/26"
        "direction": trade["direction"],        # BULL / BEAR / NEUTRAL
        "symbol": trade["symbol"],              # comma-joined leg symbols
        "open_date": trade["open_date"],
        "open_at": trade["open_at"],
        "open_action": trade["open_action"],
        "quantity": trade["quantity"],          # number of spreads
        "open_price": trade["open_price"],      # per-spread net debit/credit
        "legs": _legs_summary(trade.get("open_legs") or []),
        # User-filled fields (initially empty)
        "logic_type": [],
        "is_earnings_bet": None,
        "signal_sources": [],
        "confidence": None,
        "checklist": [],          # checked items
        "emotion": {},            # {key: 1-10}
        "one_line_thesis": "",
        "notes": "",
        # Greeks/IV/POP at open — auto-computed via Black-Scholes (or
        # broker-fetched for new opens; see journal_greeks.compute_entry_greeks).
        "pop_at_open":   None,    # % probability of profit (0-100)
        "iv_at_open":    None,    # % implied vol of the spread
        "delta_at_open": None,    # net delta of the spread
        "theta_at_open": None,    # net theta of the spread
        "gamma_at_open": None,    # net gamma of the spread
        "vega_at_open":  None,    # net vega of the spread
        "spot_at_open":  None,    # underlying price at open
    }


def _cluster_pnl(trade: dict, cluster: dict) -> tuple[float, float]:
    """Per-cluster (gross_pnl, net_pnl). The cluster only covers a portion of
    the open's quantity, so the open's value/commission/fees are allocated
    proportionally; the cluster's own value/commission/fees are added in full.
    """
    open_qty = float(trade.get("quantity") or 1) or 1.0
    cluster_qty = float(cluster.get("quantity") or 0)
    ratio = (cluster_qty / open_qty) if open_qty else 0.0
    # Reconstruct open's per-trade aggregates from the trade dict
    # (open totals = trade totals minus close totals).
    closes = trade.get("close_clusters") or []
    open_value = float(trade.get("gross_pnl") or 0) - sum(float(c.get("net_value") or 0) for c in closes)
    open_fees_total = float(trade.get("commissions") or 0) + float(trade.get("fees") or 0)
    closes_fees = sum(float(c.get("commission") or 0) + float(c.get("fees") or 0) for c in closes)
    open_fees = open_fees_total - closes_fees
    gross = round(open_value * ratio + float(cluster.get("net_value") or 0), 2)
    fees_share = open_fees * ratio + float(cluster.get("commission") or 0) + float(cluster.get("fees") or 0)
    net = round(gross + fees_share, 2)
    return gross, net


def _new_close_entry(trade: dict, open_entry_id: str, cluster_idx: int = -1) -> dict:
    """Build a pending Close Entry skeleton for one close cluster.

    cluster_idx selects which close cluster this entry represents (default last).
    A trade can have multiple close clusters (partial closes); each gets its
    own close entry, so the user can reflect on every close decision
    independently.
    """
    closes = trade.get("close_clusters") or []
    cluster = closes[cluster_idx] if closes else {}
    cluster_legs = _legs_summary(cluster.get("legs") or [])
    cluster_qty = float(cluster.get("quantity") or trade.get("quantity") or 0)
    cluster_close_at = cluster.get("executed_at") or trade.get("close_at") or ""
    cluster_close_date = cluster_close_at[:10] if cluster_close_at else trade.get("close_date", "")
    cluster_per_spread = _net_credit_per_spread(cluster) if cluster else trade.get("close_price", 0)
    gross_pnl, net_pnl = _cluster_pnl(trade, cluster) if cluster else (trade.get("gross_pnl", 0), trade.get("net_pnl", 0))
    return {
        "id": "close-" + uuid.uuid4().hex[:12],
        "trade_id": trade["trade_id"],
        "open_entry_id": open_entry_id,
        # cluster_close_at uniquely identifies WHICH close cluster of this
        # trade this entry is for. Two entries can share trade_id but must
        # differ on cluster_close_at.
        "cluster_close_at": cluster_close_at,
        "created_at": datetime.datetime.now().isoformat(),
        "updated_at": "",
        "status": "pending",
        # Auto-filled (spread-level, cluster-scoped where applicable)
        "ticker": trade["underlying"],
        "strategy": trade["strategy"],
        "spread_label": trade["spread_label"],
        "direction": trade["direction"],
        "symbol": trade["symbol"],
        "open_date": trade["open_date"],
        "close_date": cluster_close_date,
        "net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "close_action": trade["close_action"],
        "quantity": cluster_qty,
        "open_price": trade["open_price"],
        "close_price": cluster_per_spread,
        "close_legs": cluster_legs,
        # User-filled
        "close_logic": [],          # multi-select: stop loss / take profit / etc
        "followed_checklist": None,
        "followed_plan": None,
        "rule_changes_midway": [],  # multi-select
        "process_score": None,      # 1-10
        "decision_still_valid": None,  # fully / partially / not_at_all
        "quadrant": None,           # good_dec_good_res / good_dec_bad_res / etc
        "mistakes_made": [],        # multi-select — only on close
        "lesson_learned": "",
        # Greeks/IV/POP at close — same fields as open, captured for the
        # close timestamp so the user can see how the spread aged.
        "pop_at_close":   None,
        "iv_at_close":    None,
        "delta_at_close": None,
        "theta_at_close": None,
        "gamma_at_close": None,
        "vega_at_close":  None,
        "spot_at_close":  None,
    }


_OPEN_USER_FIELDS = (
    "logic_type", "is_earnings_bet", "signal_sources", "confidence",
    "checklist", "emotion", "one_line_thesis", "notes",
    "pop_at_open", "iv_at_open",
    "delta_at_open", "theta_at_open", "gamma_at_open", "vega_at_open",
    "spot_at_open", "open_price",
    "status", "updated_at",
)
_CLOSE_USER_FIELDS = (
    "close_logic", "followed_checklist", "followed_plan",
    "rule_changes_midway", "process_score", "decision_still_valid",
    "quadrant", "mistakes_made", "lesson_learned",
    "pop_at_close", "iv_at_close",
    "delta_at_close", "theta_at_close", "gamma_at_close", "vega_at_close",
    "spot_at_close", "close_price",
    "status", "updated_at",
    # economics block — must survive sync rebuild
    "max_profit", "max_loss", "spread_width", "net_premium",
    "is_credit", "dte_at_open", "econ_note",
)


def _legacy_open_at(entry: dict) -> str:
    """Recover the open timestamp from a legacy entry — either from `open_at`
    or by parsing the legacy trade_id format `<symbol>|<open_at_iso>|<qty>`."""
    if entry.get("open_at"):
        return entry["open_at"]
    tid = entry.get("trade_id") or ""
    parts = tid.split("|")
    if len(parts) >= 2:
        return parts[1]
    return ""


def _migrate_legacy_open(entry: dict, trades_by_id: dict, trades_by_open_at: dict) -> Optional[str]:
    """If the entry's old (leg-level) trade_id no longer matches any
    spread-level trade, find a spread trade that contains the same leg + same
    open_at, and remap the entry to it. Returns the new trade_id or None.
    """
    if entry.get("trade_id") in trades_by_id:
        return entry["trade_id"]
    open_at = _legacy_open_at(entry)
    leg_sym = entry.get("symbol") or ""
    if not open_at:
        return None
    for t in trades_by_open_at.get(open_at, []):
        if leg_sym and any(l.get("symbol") == leg_sym for l in t.get("open_legs", [])):
            return t["trade_id"]
        if t.get("underlying") == entry.get("ticker"):
            return t["trade_id"]
    return None


def _apply_forward_splits(transactions: list[dict]) -> list[dict]:
    """Collapse 'Forward Split' broker adjustments so a pre-split open and the
    post-split close are recognized as the same position.

    A forward split appears as paired 'Receive Deliver / Forward Split' txns
    per leg: 'Close qty=A old_sym' + 'Open qty=B new_sym' with identical $
    value and matching executed-at second.

    We rewrite the *post-split* fills back to the original (pre-split) symbol
    so the journal keeps showing the strikes/qty the user actually opened
    with. To avoid touching legitimate fresh trades on the new symbol after
    the split, each rename has a qty budget equal to the split-converted
    quantity; once that pool is exhausted, later fills on the new symbol
    pass through untouched.

    The 4 split events themselves are dropped so they don't generate phantom
    journal entries.
    """
    splits = [t for t in transactions
              if t.get("transaction-sub-type") == "Forward Split"]
    if not splits:
        return transactions
    closes = [t for t in splits if "Close" in (t.get("action") or "")]
    opens  = [t for t in splits if "Open"  in (t.get("action") or "")]
    # new_sym → list of {"old_sym","new_per_old","budget_remaining","after_at"}
    pools: dict[str, list[dict]] = defaultdict(list)
    used_open_ids: set = set()
    for c in closes:
        c_val = str(c.get("value"))
        c_at  = c.get("executed-at", "")[:19]
        c_qty = float(c.get("quantity") or 0) or 1.0
        old_sym = (c.get("symbol") or "").strip()
        for o in opens:
            if o.get("id") in used_open_ids:
                continue
            if str(o.get("value")) != c_val:
                continue
            if o.get("executed-at", "")[:19] != c_at:
                continue
            new_sym = (o.get("symbol") or "").strip()
            if not new_sym or new_sym == old_sym:
                continue
            n_qty = float(o.get("quantity") or 0) or 1.0
            pools[new_sym].append({
                "old_sym": old_sym,
                "new_per_old": n_qty / c_qty,  # e.g. 5 for a 1:5 split
                "budget_remaining": n_qty,
                "after_at": o.get("executed-at", ""),
            })
            used_open_ids.add(o.get("id"))
            break
    if not pools:
        return transactions
    drop_ids = {t.get("id") for t in splits}
    out: list[dict] = []
    # Walk in time order so we consume the pool in the right order.
    for t in sorted(transactions, key=lambda x: x.get("executed-at", "")):
        if t.get("id") in drop_ids:
            continue
        sym = (t.get("symbol") or "").strip()
        pool_list = pools.get(sym)
        if not pool_list:
            out.append(t)
            continue
        t_at = t.get("executed-at", "")
        try:
            t_qty = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            t_qty = 0.0
        # Find first pool whose split occurred before this txn and still has budget.
        pool = next((p for p in pool_list
                     if p["budget_remaining"] > 1e-9 and p["after_at"] <= t_at),
                    None)
        if pool is None or t_qty <= 0:
            out.append(t)
            continue
        take_new = min(t_qty, pool["budget_remaining"])
        ratio = pool["new_per_old"]
        if abs(take_new - t_qty) < 1e-9:
            # Whole txn fits in the pool — rewrite in place.
            new_t = dict(t)
            new_t["symbol"] = pool["old_sym"]
            new_t["quantity"] = str(t_qty / ratio)
            try:
                p = t.get("price")
                if p is not None and p != "":
                    new_t["price"] = str(float(p) * ratio)
            except (TypeError, ValueError):
                pass
            pool["budget_remaining"] -= take_new
            out.append(new_t)
        else:
            # Partial — split the txn into a rewritten chunk and a passthrough.
            ratio_v = take_new / t_qty
            def _scale(part: dict, q: float, rewrite: bool) -> dict:
                new_t = dict(part)
                new_t["quantity"] = str(q)
                for k in ("value", "net-value", "commission",
                          "clearing-fees", "regulatory-fees", "proprietary-index-option-fees"):
                    if k in new_t and new_t[k] not in (None, ""):
                        try:
                            new_t[k] = str(float(new_t[k]) * (q / t_qty))
                        except (TypeError, ValueError):
                            pass
                if rewrite:
                    new_t["symbol"] = pool["old_sym"]
                    new_t["quantity"] = str(q / ratio)
                    try:
                        p = part.get("price")
                        if p is not None and p != "":
                            new_t["price"] = str(float(p) * ratio)
                    except (TypeError, ValueError):
                        pass
                return new_t
            out.append(_scale(t, take_new, rewrite=True))
            out.append(_scale(t, t_qty - take_new, rewrite=False))
            pool["budget_remaining"] -= take_new
    return out


def sync_entries_from_trades_raw(report_fn=None) -> dict:
    """Group transactions into spread-level trades and ensure each has an Open
    Entry (and Close Entry if closed). Preserves user-filled fields across
    leg→spread migration: any legacy entry whose trade_id no longer exists
    at spread level is matched to the new spread by open_at + leg symbol.
    """
    if not TRADES_RAW_PATH.exists():
        if report_fn: report_fn("trades_raw.json missing — run fetch_trades.py first")
        return {"open_added": 0, "close_added": 0, "total_trades": 0, "error": "no trades_raw"}
    raw = json.loads(TRADES_RAW_PATH.read_text(encoding="utf-8"))
    txs = raw.get("transactions", [])
    if not txs:
        return {"open_added": 0, "close_added": 0, "total_trades": 0}

    txs = _apply_forward_splits(txs)
    txs = _apply_open_merges(txs)          # 作者合并的同票同天单 → 聚成一单
    trades = group_trades(txs)
    trades_by_id = {t["trade_id"]: t for t in trades}
    trades_by_open_at: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("open_at"):
            trades_by_open_at[t["open_at"]].append(t)

    legacy_opens = load_open_entries()
    legacy_closes = load_close_entries()

    # ── Migration: rebuild open entries keyed by new spread-level trade_id
    new_opens: dict[str, dict] = {}  # trade_id → entry
    migrated_open = 0
    dropped_open = 0
    for old in legacy_opens:
        new_tid = _migrate_legacy_open(old, trades_by_id, trades_by_open_at)
        if not new_tid or new_tid not in trades_by_id:
            dropped_open += 1
            continue
        if new_tid in new_opens:
            # Two legacy leg-entries collapsing into one spread — prefer the one
            # that has user-filled content.
            existing = new_opens[new_tid]
            old_filled = bool(old.get("one_line_thesis") or old.get("logic_type")
                               or old.get("confidence") or old.get("status") == "completed")
            existing_filled = bool(existing.get("one_line_thesis") or existing.get("logic_type")
                                    or existing.get("confidence") or existing.get("status") == "completed")
            if old_filled and not existing_filled:
                new_opens[new_tid] = _build_migrated_open(trades_by_id[new_tid], old)
            continue
        # Build the spread-level skeleton, then copy user fields from legacy
        new_opens[new_tid] = _build_migrated_open(trades_by_id[new_tid], old)
        if new_tid != old.get("trade_id"):
            migrated_open += 1

    # Ensure every new trade has an open entry (even ones with no legacy)
    open_added = 0
    for t in trades:
        if t["trade_id"] not in new_opens:
            new_opens[t["trade_id"]] = _new_open_entry(t)
            open_added += 1

    # ── Close entries: one per (trade_id, close_cluster.executed_at). A trade
    # with multiple partial closes now produces multiple close entries so the
    # user can reflect on each close decision independently.
    new_closes: dict[tuple[str, str], dict] = {}  # (trade_id, cluster_close_at) → entry
    migrated_close = 0
    dropped_close = 0

    def _pick_cluster_for_legacy(t: dict, old: dict) -> int | None:
        """Match a legacy close entry (no cluster_close_at) to one of the
        trade's close clusters. Strategy: explicit cluster_close_at first;
        otherwise close_date match; finally last cluster as fallback."""
        clusters = t.get("close_clusters") or []
        if not clusters:
            return None
        explicit = old.get("cluster_close_at")
        if explicit:
            for i, c in enumerate(clusters):
                if (c.get("executed_at") or "") == explicit:
                    return i
        old_date = old.get("close_date") or ""
        if old_date:
            for i, c in enumerate(clusters):
                if (c.get("executed_at") or "")[:10] == old_date:
                    return i
        return len(clusters) - 1  # default to last

    for old in legacy_closes:
        new_tid = _migrate_legacy_close(old, trades_by_id, trades_by_open_at)
        if not new_tid or new_tid not in trades_by_id:
            dropped_close += 1
            continue
        t = trades_by_id[new_tid]
        if not (t.get("close_clusters") or []):
            # Trade has no closes anymore (e.g. broker re-reported) — drop
            dropped_close += 1
            continue
        idx = _pick_cluster_for_legacy(t, old)
        if idx is None:
            dropped_close += 1
            continue
        cluster_at = (t["close_clusters"][idx].get("executed_at") or "")
        key = (new_tid, cluster_at)
        open_id = new_opens[new_tid]["id"] if new_tid in new_opens else ""
        if key in new_closes:
            old_filled = bool(old.get("lesson_learned") or old.get("close_logic")
                               or old.get("status") == "completed")
            existing = new_closes[key]
            existing_filled = bool(existing.get("lesson_learned") or existing.get("close_logic")
                                    or existing.get("status") == "completed")
            if old_filled and not existing_filled:
                new_closes[key] = _build_migrated_close(t, old, open_id, idx)
            continue
        new_closes[key] = _build_migrated_close(t, old, open_id, idx)
        if new_tid != old.get("trade_id") or not old.get("cluster_close_at"):
            migrated_close += 1

    close_added = 0
    for t in trades:
        clusters = t.get("close_clusters") or []
        for idx, c in enumerate(clusters):
            cluster_at = c.get("executed_at") or ""
            key = (t["trade_id"], cluster_at)
            if key in new_closes:
                continue
            open_id = new_opens[t["trade_id"]]["id"] if t["trade_id"] in new_opens else ""
            new_closes[key] = _new_close_entry(t, open_id, idx)
            close_added += 1

    # ── Auto-compute Greeks/IV/POP for any newly-created entries.
    # Best-effort: failures (e.g. no IV for an expired contract) are
    # silently skipped — the user can re-trigger via the backfill button.
    try:
        import journal_greeks as _jg
        for entry in new_opens.values():
            if entry.get("delta_at_open") is None:
                r = _jg.compute_entry_greeks(entry, side="open")
                if "error" not in r:
                    for k, v in r.items():
                        if not k.startswith("_"):
                            entry[k] = v
        for entry in new_closes.values():
            if entry.get("delta_at_close") is None:
                r = _jg.compute_entry_greeks(entry, side="close")
                if "error" not in r:
                    for k, v in r.items():
                        if not k.startswith("_"):
                            entry[k] = v
    except Exception as e:
        if report_fn:
            report_fn(f"[journal] sync: greeks compute skipped — {e}")

    # ── Persist (atomic rewrite)
    _rewrite_jsonl(OPEN_PATH, list(new_opens.values()))
    _rewrite_jsonl(CLOSE_PATH, list(new_closes.values()))

    pending_open = sum(1 for e in new_opens.values() if e.get("status") == "pending")
    pending_close = sum(1 for e in new_closes.values() if e.get("status") == "pending")

    # ── Backfill economics for any newly-synced close entries lacking the block.
    # backfill_economics() re-reads from disk, so calling it after the writes
    # above is correct and does not risk double-write or recursion.
    econ_filled = backfill_economics(report_fn)

    if report_fn:
        report_fn(f"[journal] sync: {open_added} new open, {close_added} new close, "
                   f"{len(trades)} spread-trades total "
                   f"(migrated {migrated_open}+{migrated_close} legacy, "
                   f"dropped {dropped_open}+{dropped_close}, "
                   f"econ backfilled {econ_filled})")
    return {
        "open_added": open_added,
        "close_added": close_added,
        "migrated_open": migrated_open,
        "migrated_close": migrated_close,
        "dropped_open": dropped_open,
        "dropped_close": dropped_close,
        "total_trades": len(trades),
        "pending_open": pending_open,
        "pending_close": pending_close,
        "econ_filled": econ_filled,
    }


def _build_migrated_open(trade: dict, legacy_entry: dict) -> dict:
    """Build a spread-level Open Entry that *carries forward* any user-filled
    fields from a legacy leg-level entry."""
    fresh = _new_open_entry(trade)
    fresh["id"] = legacy_entry.get("id", fresh["id"])  # preserve URL stability
    fresh["created_at"] = legacy_entry.get("created_at", fresh["created_at"])
    for k in _OPEN_USER_FIELDS:
        if k in legacy_entry and legacy_entry[k] not in (None, "", [], {}):
            fresh[k] = legacy_entry[k]
    return fresh


def _build_migrated_close(trade: dict, legacy_entry: dict, open_id: str,
                          cluster_idx: int = -1) -> dict:
    fresh = _new_close_entry(trade, open_id, cluster_idx)
    fresh["id"] = legacy_entry.get("id", fresh["id"])
    fresh["created_at"] = legacy_entry.get("created_at", fresh["created_at"])
    for k in _CLOSE_USER_FIELDS:
        if k in legacy_entry and legacy_entry[k] not in (None, "", [], {}):
            fresh[k] = legacy_entry[k]
    return fresh


def _migrate_legacy_close(entry: dict, trades_by_id: dict,
                            trades_by_open_at: dict) -> Optional[str]:
    if entry.get("trade_id") in trades_by_id:
        return entry["trade_id"]
    open_at = _legacy_open_at(entry)
    leg_sym = entry.get("symbol") or ""
    if not open_at:
        return None
    for t in trades_by_open_at.get(open_at, []):
        if leg_sym and any(l.get("symbol") == leg_sym for l in t.get("open_legs", [])):
            return t["trade_id"]
        if t.get("underlying") == entry.get("ticker"):
            return t["trade_id"]
    return None


# ── Economics backfill ────────────────────────────────────────────────────

ECON_FIELDS = ("max_profit", "max_loss", "spread_width", "net_premium",
               "is_credit", "dte_at_open")


def _econ_for_pair(open_entry: dict) -> dict:
    """Compute economics block from an open entry's legs."""
    legs = open_entry.get("legs") or []
    e = _te.spread_economics(legs)
    return {
        "max_profit": e["max_profit"],
        "max_loss": e["max_loss"],
        "spread_width": e["spread_width"],
        "net_premium": e["net_premium"],
        "is_credit": e["is_credit"],
        "dte_at_open": _te.dte_at_open(legs, open_entry.get("open_date") or ""),
        "econ_note": e["note"],
    }


def backfill_economics(report_fn=None) -> int:
    """Idempotently add the economics block to close entries lacking it.

    Joins close entries to their matching open entry by open_entry_id
    (falling back to trade_id). Returns the count of entries updated.
    """
    opens = load_open_entries()
    closes = load_close_entries()
    by_id = {o.get("id"): o for o in opens}
    by_tid = {o.get("trade_id"): o for o in opens}
    changed = 0
    for c in closes:
        if "econ_note" in c:           # idempotency marker
            continue
        o = by_id.get(c.get("open_entry_id")) or by_tid.get(c.get("trade_id"))
        if not o:
            continue
        c.update(_econ_for_pair(o))
        changed += 1
    if changed:
        _rewrite_jsonl(CLOSE_PATH, closes)
        if report_fn:
            report_fn(f"backfilled economics on {changed} entries")
    return changed


# ── Aggregate / list helpers (for API + stats) ────────────────────────────

def list_entries(status: str = "all", kind: str = "all") -> list[dict]:
    """Return entries optionally filtered. kind: 'open'/'close'/'all'."""
    out = []
    if kind in ("open", "all"):
        out.extend({**e, "_kind": "open"} for e in load_open_entries())
    if kind in ("close", "all"):
        out.extend({**e, "_kind": "close"} for e in load_close_entries())
    if status != "all":
        out = [e for e in out if e.get("status") == status]
    out.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return out


def get_entry(entry_id: str) -> Optional[dict]:
    for e in load_open_entries():
        if e.get("id") == entry_id:
            return {**e, "_kind": "open"}
    for e in load_close_entries():
        if e.get("id") == entry_id:
            return {**e, "_kind": "close"}
    return None


def get_paired_entries(trade_id: str) -> dict:
    """Used by the close detail view to display open snapshot side-by-side."""
    open_e = next((e for e in load_open_entries() if e.get("trade_id") == trade_id), None)
    close_e = next((e for e in load_close_entries() if e.get("trade_id") == trade_id), None)
    return {"open": open_e, "close": close_e}


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true",
                    help="Sync entries from trades_raw.json (backfill all)")
    args = ap.parse_args()
    if args.sync:
        result = sync_entries_from_trades_raw(report_fn=print)
        print(json.dumps(result, indent=2))
    else:
        # Default: print summary
        opens = load_open_entries()
        closes = load_close_entries()
        print(f"Open entries: {len(opens)} ({sum(1 for e in opens if e.get('status')=='pending')} pending)")
        print(f"Close entries: {len(closes)} ({sum(1 for e in closes if e.get('status')=='pending')} pending)")

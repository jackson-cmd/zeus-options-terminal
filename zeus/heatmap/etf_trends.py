"""ETF trend strength for the heatmap — multi-period returns (not real-time).

For a curated, hand-editable ETF list (data/etf_trends.txt, two groups: SECTOR
and GLOBAL) compute % return over 3d / 7d / 1mo / 3mo / 1yr from yfinance daily
closes. Cached to disk and refreshed at most a few times a day — the user
explicitly does NOT want this live. Used to rank leaders vs laggards.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIST_FILE = _REPO_ROOT / "data" / "etf_trends.txt"
_CACHE = _REPO_ROOT / "data" / "heatmap" / "etf_trends.json"
_CACHE_TTL = 6 * 60 * 60  # 6h — "not real-time"

# (label, key, trading-day lookback)
_PERIODS = [("3d", "r3d", 3), ("7d", "r7d", 5), ("1mo", "r1mo", 21),
            ("3mo", "r3mo", 63), ("1yr", "r1yr", 252)]

_mem: Optional[dict] = None
_mem_at: float = 0.0
_list_cache: Optional[list] = None
_list_mtime: float = -1.0


def _parse_list() -> list[tuple[str, str, str]]:
    """(category, ticker, name) from the curated file. Cached by mtime."""
    global _list_cache, _list_mtime
    if not _LIST_FILE.exists():
        return []
    try:
        mtime = _LIST_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _list_cache is not None and mtime == _list_mtime:
        return _list_cache
    out: list[tuple[str, str, str]] = []
    cat = "SECTOR"
    for ln in _LIST_FILE.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") and not ln.startswith("###"):
            continue
        if ln.startswith("###"):
            cat = ln[3:].strip().upper() or cat
            continue
        ticker = ln.split("|")[0].strip().upper()
        name = ln.split("|", 1)[1].strip() if "|" in ln else ticker
        if ticker:
            out.append((cat, ticker, name))
    _list_cache, _list_mtime = out, mtime
    return out


def tickers() -> list[str]:
    return [t for _, t, _ in _parse_list()]


def _compute() -> dict:
    """Download daily closes and compute per-ETF multi-period returns."""
    rows = _parse_list()
    syms = [t for _, t, _ in rows]
    closes: dict[str, list] = {}
    if syms:
        try:
            import yfinance as yf
            df = yf.download(syms, period="400d", interval="1d",
                             progress=False, auto_adjust=True, threads=True)
            close = df["Close"]
            single = (getattr(close, "ndim", 1) == 1)
            for t in syms:
                try:
                    s = close.dropna() if single else close[t].dropna()
                    closes[t] = [float(x) for x in s.tolist()]
                except Exception:
                    closes[t] = []
        except Exception as e:
            print(f"[heatmap.etf_trends] yfinance failed: {e}")

    items = []
    for cat, t, name in rows:
        c = closes.get(t, [])
        item = {"ticker": t, "name": name, "category": cat,
                "last": round(c[-1], 4) if c else 0.0, "has_data": bool(c)}
        for _, key, n in _PERIODS:
            if len(c) > n and c[-1 - n] > 0:
                item[key] = round((c[-1] / c[-1 - n] - 1.0) * 100.0, 2)
            else:
                item[key] = None
        items.append(item)

    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "periods": [{"label": lbl, "key": key} for lbl, key, _ in _PERIODS],
            "count": len(items), "items": items}


def get(force: bool = False) -> dict:
    """Cached payload; recompute if forced, missing, or stale (>6h)."""
    global _mem, _mem_at
    now = time.time()
    if not force and _mem is not None and (now - _mem_at) < _CACHE_TTL:
        return _mem
    if not force and _CACHE.exists():
        try:
            disk = json.loads(_CACHE.read_text())
            if (now - disk.get("_ts", 0)) < _CACHE_TTL:
                _mem, _mem_at = disk, now
                return disk
        except Exception:
            pass
    payload = _compute()
    payload["_ts"] = now
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(payload))
    except Exception:
        pass
    _mem, _mem_at = payload, now
    return payload

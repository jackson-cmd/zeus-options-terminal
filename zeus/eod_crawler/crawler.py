"""
Core EOD options-chain crawler.

For each ticker in screener.csv:
  1. Fetch nested option chain
  2. Filter expirations: type ∈ {Regular, Weekly} and 0 ≤ DTE ≤ MAX_DTE
  3. Fetch underlying market-data (for UnderlyingPrice)
  4. Fetch option market-data in batches of QUOTE_BATCH_SIZE
  5. Append rows to the single consolidated store (config.DATA_FILE)
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import math
import time
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from api_client import TastyClient
from config import (
    ALLOWED_EXP_TYPES,
    CSV_COLUMNS,
    DATA_EOD_DIR,
    DATA_FILE,
    INTER_TICKER_SLEEP,
    MAX_DTE,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    QUOTE_BATCH_SIZE,
    SCREENER_CSV,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def crawl_one(client: TastyClient, symbol: str, data_date: dt.date) -> pa.Table | None:
    """Crawl a single ticker and return its rows as an Arrow table.

    Returns:
        pa.Table — the ticker's option rows for this date
        None     — empty chain (ticker has no contracts in window)
    Raises on API errors; the caller (crawl_all) catches and tags as a failure.
    """
    contracts = _list_contracts(client, symbol, data_date)
    if not contracts:
        log.warning("[%s] no contracts in window", symbol)
        return None

    underlying_px = _fetch_underlying_price(client, symbol)
    quote_map = _fetch_quotes(client, [c["occ_symbol"] for c in contracts])

    data_date_str = data_date.isoformat()
    rows = [
        _build_row(symbol, c, quote_map.get(c["occ_symbol"], {}), underlying_px, data_date_str)
        for c in contracts
    ]
    table = _rows_to_table(rows)
    log.info("[%s] %d rows", symbol, table.num_rows)
    return table


def crawl_all(
    symbols: Iterable[str],
    data_date: dt.date,
    progress_cb=None,
) -> tuple[dict[str, int], list[pa.Table]]:
    """Crawl every symbol.

    Returns (summary, tables) where summary maps {symbol: rows} with
    0 for an empty chain and -1 for a failure, and tables is the list of
    non-empty per-ticker tables to be appended to the store in one shot.
    Failures are logged, not raised.

    progress_cb(done, total, current, summary) — optional; called once before
    each ticker (with `current` = the ticker about to crawl and `done` = how
    many already finished) and once at the end (current=None, done=total). It
    must never raise; exceptions are swallowed so a flaky reporter can't abort
    the crawl. Default None keeps crawl_all pure (used by tests).
    """
    DATA_EOD_DIR.mkdir(exist_ok=True)
    client = TastyClient()
    results: dict[str, int] = {}
    tables: list[pa.Table] = []
    syms = list(symbols)
    total = len(syms)
    for i, sym in enumerate(syms):
        if progress_cb is not None:
            try:
                progress_cb(i, total, sym, results)
            except Exception:
                log.warning("progress_cb raised (ignored)", exc_info=True)
        try:
            tbl = crawl_one(client, sym, data_date)
            if tbl is None:
                results[sym] = 0
            else:
                results[sym] = tbl.num_rows
                tables.append(tbl)
        except Exception as exc:
            log.exception("[%s] FAILED: %s", sym, exc)
            results[sym] = -1
        time.sleep(INTER_TICKER_SLEEP)
    if progress_cb is not None:
        try:
            progress_cb(total, total, None, results)
        except Exception:
            log.warning("progress_cb raised (ignored)", exc_info=True)
    return results, tables


def load_symbols() -> list[str]:
    """Read tickers from screener.csv (Barchart format, capital 'Symbol' column)."""
    with SCREENER_CSV.open() as f:
        reader = csv.DictReader(f)
        symbols: list[str] = []
        for row in reader:
            sym = (row.get("Symbol") or "").strip().upper()
            if sym and sym != "N/A":
                symbols.append(sym)
        return symbols


# ---------------------------------------------------------------------------
# Chain parsing
# ---------------------------------------------------------------------------

def _list_contracts(client: TastyClient, symbol: str, today: dt.date) -> list[dict]:
    """Fetch the nested chain and flatten to a list of {occ_symbol, expiration, strike, type}."""
    payload = client.get_option_chain_nested(symbol)
    items = payload.get("data", {}).get("items", [])

    contracts: list[dict] = []
    for chain in items:
        for exp in chain.get("expirations", []):
            exp_type = exp.get("expiration-type", "Regular")
            if exp_type not in ALLOWED_EXP_TYPES:
                continue
            exp_str = exp.get("expiration-date", "")
            try:
                exp_date = dt.date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > MAX_DTE:
                continue

            for strike in exp.get("strikes", []):
                strike_price = _to_float(strike.get("strike-price"))
                if strike_price is None:
                    continue
                for side in ("call", "put"):
                    occ = strike.get(side)
                    if not occ:
                        continue
                    contracts.append({
                        "occ_symbol": occ,
                        "expiration": exp_str,
                        "strike": strike_price,
                        "type": side,
                    })
    return contracts


# ---------------------------------------------------------------------------
# Quote fetching
# ---------------------------------------------------------------------------

def _fetch_underlying_price(client: TastyClient, symbol: str) -> float | None:
    try:
        resp = client.get_market_data([symbol])
        items = resp.get("data", {}).get("items", [])
        if not items:
            return None
        item = items[0]
        bid = _to_float(item.get("bid"))
        ask = _to_float(item.get("ask"))
        last = _to_float(item.get("last"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return round((bid + ask) / 2, 4)
        return last
    except Exception as exc:
        log.warning("[%s] underlying quote failed: %s", symbol, exc)
        return None


def _fetch_quotes(client: TastyClient, occ_symbols: list[str]) -> dict[str, dict]:
    quote_map: dict[str, dict] = {}
    for i in range(0, len(occ_symbols), QUOTE_BATCH_SIZE):
        batch = occ_symbols[i : i + QUOTE_BATCH_SIZE]
        try:
            resp = client.get_market_data(batch)
            for item in resp.get("data", {}).get("items", []):
                quote_map[item.get("symbol", "")] = item
        except Exception as exc:
            log.warning("quote batch %d-%d failed: %s", i, i + len(batch), exc)
    return quote_map


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

def _build_row(
    symbol: str,
    contract: dict,
    quote: dict,
    underlying_px: float | None,
    data_date: str,
) -> dict:
    bid = _to_float(quote.get("bid"))
    ask = _to_float(quote.get("ask"))
    last = _to_float(quote.get("last"))
    iv = _to_float(quote.get("volatility") or quote.get("implied-volatility"))
    delta = _to_float(quote.get("delta"))
    gamma = _to_float(quote.get("gamma"))
    theta = _to_float(quote.get("theta"))
    vega = _to_float(quote.get("vega"))
    volume = _to_int(quote.get("volume") or quote.get("day-volume"))
    open_interest = _to_int(quote.get("open-interest"))

    return {
        "OptionKey": _option_key(symbol, contract["expiration"], contract["type"], contract["strike"], data_date),
        "Symbol": symbol,
        "ExpirationDate": contract["expiration"],
        "AskPrice": ask,
        "BidPrice": bid,
        "LastPrice": last,
        "PutCall": contract["type"],
        "StrikePrice": float(contract["strike"]),
        "Volume": volume,
        "OpenInterest": open_interest,
        "UnderlyingPrice": underlying_px,
        "ImpliedVolatility": iv,
        "Delta": delta,
        "Gamma": gamma,
        "Vega": vega,
        "Theta": theta,
        "DataDate": data_date,
    }


def _option_key(symbol: str, expiration: str, side: str, strike: float, data_date: str) -> str:
    """Match the prior CSV's composite key: e.g. AAPL2026-05-16C150.0002026-04-24."""
    cp = "C" if side == "call" else "P"
    return f"{symbol}{expiration}{cp}{strike:.3f}{data_date}"


# ---------------------------------------------------------------------------
# Parquet write
# ---------------------------------------------------------------------------

# Parquet schema: strings stay strings, numerics are float64 (Greeks/prices) or
# int64 (Volume/OpenInterest). Nullable everywhere — missing quotes show as NaN/None.
_PARQUET_SCHEMA = pa.schema([
    ("OptionKey", pa.string()),
    ("Symbol", pa.string()),
    ("ExpirationDate", pa.string()),
    ("AskPrice", pa.float64()),
    ("BidPrice", pa.float64()),
    ("LastPrice", pa.float64()),
    ("PutCall", pa.string()),
    ("StrikePrice", pa.float64()),
    ("Volume", pa.int64()),
    ("OpenInterest", pa.int64()),
    ("UnderlyingPrice", pa.float64()),
    ("ImpliedVolatility", pa.float64()),
    ("Delta", pa.float64()),
    ("Gamma", pa.float64()),
    ("Vega", pa.float64()),
    ("Theta", pa.float64()),
    ("DataDate", pa.string()),
])


def _rows_to_table(rows: list[dict]) -> pa.Table:
    columns = {col: [r.get(col) for r in rows] for col in CSV_COLUMNS}
    return pa.Table.from_pydict(columns, schema=_PARQUET_SCHEMA)


def date_present(data_date: dt.date) -> bool:
    """True if the consolidated store already contains rows for this DataDate.

    Uses per-row-group DataDate statistics to skip groups that cannot contain the
    date, only reading the DataDate column of groups whose [min, max] spans it.
    """
    if not DATA_FILE.exists():
        return False
    target = data_date.isoformat()
    pf = pq.ParquetFile(DATA_FILE)
    col = _PARQUET_SCHEMA.get_field_index("DataDate")
    for rg in range(pf.num_row_groups):
        stats = pf.metadata.row_group(rg).column(col).statistics
        if stats is not None and stats.has_min_max and not (stats.min <= target <= stats.max):
            continue
        batch = pf.read_row_group(rg, columns=["DataDate"])
        if target in set(batch.column(0).to_pylist()):
            return True
    return False


def append_day(data_date: dt.date, tables: list[pa.Table]) -> int:
    """Append the day's rows to the single consolidated store (config.DATA_FILE).

    parquet has no in-place append, so we stream the existing file row-group by
    row-group into a fresh temp file — dropping any rows already tagged with this
    DataDate so re-runs replace rather than duplicate — then write the new rows
    and atomically rename. Memory stays bounded to one row group plus the day.

    Returns the number of rows written for this date.
    """
    if not tables:
        log.info("[%s] no rows to append", data_date.isoformat())
        return 0

    DATA_EOD_DIR.mkdir(exist_ok=True)
    today = pa.concat_tables(tables).cast(_PARQUET_SCHEMA)
    target = DATA_FILE
    tmp = target.with_suffix(".parquet.tmp")
    target_str = data_date.isoformat()

    writer = pq.ParquetWriter(
        tmp,
        _PARQUET_SCHEMA,
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
    )
    try:
        if target.exists():
            pf = pq.ParquetFile(target)
            for rg in range(pf.num_row_groups):
                t = pf.read_row_group(rg)
                kept = t.filter(pc.not_equal(t.column("DataDate"), target_str))
                if kept.num_rows:
                    writer.write_table(kept.cast(_PARQUET_SCHEMA))
        writer.write_table(today)
    finally:
        writer.close()

    tmp.replace(target)
    log.info("[%s] appended %d rows -> %s", target_str, today.num_rows, target.name)
    return today.num_rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None

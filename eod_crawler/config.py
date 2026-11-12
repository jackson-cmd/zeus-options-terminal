"""Static config for the EOD options-chain crawler."""

from __future__ import annotations

from pathlib import Path

# tastytrade API
BASE_URL = "https://api.tastyworks.com"
OAUTH_TOKEN_URL = f"{BASE_URL}/oauth/token"

# File locations (all under this folder)
HERE = Path(__file__).parent
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(HERE.parent))
import zeus_config as _hc  # noqa: E402
CLIENT_SECRET_FILE = HERE / "client_secret.txt"   # legacy names; values come from Settings
REFRESH_TOKEN_FILE = HERE / "refresh_token.txt"
TOKEN_CACHE_FILE = _hc.home() / "tt_token_cache_eod.json"
SCREENER_CSV = HERE / "screener.csv"
DATA_EOD_DIR = HERE / "data_eod"
# Single consolidated store: all tickers, all days, one file. Appended to daily.
DATA_FILE = DATA_EOD_DIR / "eod_chain.parquet"
# Plain-text pointer to the most recent crawled date (first line = YYYY-MM-DD).
LAST_CRAWLED_FILE = DATA_EOD_DIR / "LAST_CRAWLED.txt"
LOG_DIR = HERE / "logs"

# Crawl parameters
MAX_DTE = 90
ALLOWED_EXP_TYPES = {"Regular", "Weekly"}
QUOTE_BATCH_SIZE = 100
INTER_TICKER_SLEEP = 0.2
HTTP_TIMEOUT = 30

# Parquet compression for the consolidated store. zstd compresses the repetitive
# option columns far better than snappy; level 9 favors ratio for a daily batch job.
PARQUET_COMPRESSION = "zstd"
PARQUET_COMPRESSION_LEVEL = 9

# Schedule (US Central — Chicago). 15:30 CT == 16:30 ET, after option settlement.
SCHEDULE_HOUR_CT = 15
SCHEDULE_MIN_CT = 30
TIMEZONE = "America/Chicago"

# Parquet schema column order — same names as the previous CSV layout.
CSV_COLUMNS = [
    "OptionKey",
    "Symbol",
    "ExpirationDate",
    "AskPrice",
    "BidPrice",
    "LastPrice",
    "PutCall",
    "StrikePrice",
    "Volume",
    "OpenInterest",
    "UnderlyingPrice",
    "ImpliedVolatility",
    "Delta",
    "Gamma",
    "Vega",
    "Theta",
    "DataDate",
]

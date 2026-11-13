"""
Configuration for the Trading Assistant.
Reads credentials from files - NEVER hardcode tokens.
"""
import os
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zeus_config as _hc  # noqa: E402

# --- Paths ---
BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = _hc.home()
REPORTS_DIR = BASE_DIR / "reports"
DATA_DIR = BASE_DIR / "data"

for d in [REPORTS_DIR, DATA_DIR]:
    d.mkdir(exist_ok=True)

# --- TastyTrade API ---
TASTYTRADE_BASE_URL = "https://api.tastyworks.com"
TASTYTRADE_BACKTEST_URL = "https://backtester.vast.tastyworks.com"
TOKEN_CACHE_FILE = str(_hc.home() / "tt_token_cache.json")


_CRED_KEYS = {"client_id.txt": "tt_client_id", "client_secret.txt": "tt_client_secret",
              "refresh_token.txt": "tt_refresh_token"}


def _read_credential(filename: str) -> str:
    """tastytrade OAuth values come from Settings (~/.zeus-terminal/config.json)."""
    key = _CRED_KEYS.get(filename, "")
    value = str(_hc.get(key) or "").strip() if key else ""
    if not value:
        raise FileNotFoundError(
            f"tastytrade is not connected: open Settings and fill in {key} "
            f"(ZEUS runs in manual-upload mode until then).")
    return value


def get_client_id() -> str:
    return _read_credential("client_id.txt")


def get_client_secret() -> str:
    return _read_credential("client_secret.txt")


def get_refresh_token() -> str:
    return _read_credential("refresh_token.txt")


# --- Trading Parameters ---
# Bull Call Spread defaults
BULL_DTE_MIN = 30
BULL_DTE_MAX = 45
BULL_SPREAD_NARROW = 10   # ATM to ATM+10
BULL_SPREAD_WIDE = 20     # ATM-10 to ATM+10

# Bear Put Spread defaults
BEAR_DTE_MIN = 1
BEAR_DTE_MAX = 14
BEAR_SPREAD_WIDTH = 10

# Risk Management
DEFAULT_STOP_LOSS_PCT = 50       # 50% of premium paid
DEFAULT_PROFIT_TARGET_MIN = 100  # 100% gain
DEFAULT_PROFIT_TARGET_MAX = 200  # 200% gain

# Scanner thresholds (matching PineScript defaults)
SCANNER_DEFAULTS = {
    "mr_bb_len": 20,
    "mr_bb_mult": 2.0,
    "mr_min_samples": 40,
    "mr_min_close_pct": 60.0,
    "mr_min_any_pct": 70.0,
    "long_ema_len": 200,
    "long_rsi_len": 14,
    "long_rsi_threshold": 30.0,
    "long_bb_len": 20,
    "long_bb_mult": 2.0,
    "long_min_close_rate": 70.0,
    "long_min_high_rate": 80.0,
    "long_min_samples": 20,
    "bear_rsi_len": 14,
    "bear_rsi_overbought": 70.0,
    "bear_bb_len": 20,
    "bear_bb_mult": 2.0,
    "bear_close_pct": 65.0,
    "bear_low_pct": 75.0,
    "pat_min_samples": 30,
    "pat_min_any_pct": 85.0,
    "pat_min_close_pct": 75.0,
    "gap_min_pct": 2.0,
    "gap_no_fill_bars": 5,
    "gap_any_threshold": 85.0,
    "gap_close_threshold": 75.0,
}

# Tickers file
TICKERS_FILE = BASE_DIR.parent / "data" / "tickers.csv"

# Email config (optional)
EMAIL_FROM = str(_hc.get("email_from") or "")
EMAIL_TO = str(_hc.get("email_to") or "")
EMAIL_SMTP_SERVER = str(_hc.get("email_smtp_server") or "smtp.gmail.com")
EMAIL_SMTP_PORT = int(_hc.get("email_smtp_port") or 587)
EMAIL_APP_PASSWORD = str(_hc.get("email_app_password") or "")

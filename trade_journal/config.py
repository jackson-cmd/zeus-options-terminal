BASE_URL     = "https://api.tastyworks.com"
BACKTEST_URL = "https://backtester.vast.tastyworks.com"

# Token exchange endpoint (used in auth.py)
# Authorization URL and browser redirect are NOT needed — TastyTrade uses
# a portal-issued refresh_token flow instead of authorization_code flow.
OAUTH_TOKEN_URL = "https://api.tastyworks.com/oauth/token"

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import zeus_config as _hc  # noqa: E402
TOKEN_CACHE_FILE = str(_hc.home() / "tt_token_cache_journal.json")
REDIRECT_URI = "http://localhost:8182/callback"  # kept for reference only

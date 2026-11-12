"""Shared cookie-authenticated HTTP layer for all Benzinga REST endpoints.

Every Benzinga REST resource (calendar family, scanner, watchlists, layouts)
authenticates with the same SSO cookie jar the news_radar client already
loads from `benzinga/cookies.json`. We build one shared httpx.Client and reuse
it across every sub-client so connection pooling + the cookie jar are shared.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# Make news_radar/benzinga_client importable for the cookie loader (one SSO
# cookie jar serves every Benzinga property — see benzinga/BENZINGA_API.md §1).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_NR = _REPO_ROOT / "news_radar"
if str(_NR) not in sys.path:
    sys.path.insert(0, str(_NR))

import time  # noqa: E402
import httpx  # noqa: E402
from benzinga_client.cookies import load_cookie_jar, COOKIES_PATH  # noqa: E402,F401

API_HOST = "https://api.benzinga.com"
DATA_HOST = "https://data-api-pro.benzinga.com"
ACCOUNTS_HOST = "https://accounts.benzinga.com"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://pro.benzinga.com/",
    "Origin": "https://pro.benzinga.com",
}

_client: httpx.Client | None = None
_lock = threading.Lock()


class BenzingaAuthError(RuntimeError):
    """Server rejected our cookies — re-export benzinga/cookies.json from Chrome."""


def get_client() -> httpx.Client:
    global _client
    with _lock:
        if _client is None:
            _client = httpx.Client(cookies=load_cookie_jar(), headers=HEADERS,
                                   timeout=30, follow_redirects=True)
        return _client


def get_json(url: str, params: dict | None = None) -> dict:
    # 429(限流)/503 → 读 Retry-After 退避重试(最多 4 次), 别因瞬时限流丢数据或硬失败。
    for attempt in range(4):
        r = get_client().get(url, params=params)
        if r.status_code in (401, 403):
            raise BenzingaAuthError(
                f"Benzinga rejected the session ({r.status_code}); re-export "
                f"{COOKIES_PATH} from a logged-in Chrome.")
        if r.status_code in (429, 503) and attempt < 3:
            ra = r.headers.get("Retry-After")
            wait = float(ra) if (ra and ra.replace(".", "").isdigit()) else min(20.0, 1.5 * (attempt + 1))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()   # 重试耗尽仍 429 → 抛出
    return r.json()


# data-api-pro apikey — a key embedded in pro.benzinga.com's frontend (shipped
# to every browser; a client-side key, not a user secret). Needed for the
# scanner REST and the signals WS. We never commit it: env first, then a
# gitignored file. Same key serves both endpoints.
_APIKEY_FILE = _REPO_ROOT / "benzinga" / "data_apikey.txt"


def data_apikey() -> str | None:
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        import zeus_config as _hc
        k = str(_hc.get("benzinga_data_apikey") or "") or None
    except Exception:
        k = None
    k = k or os.environ.get("BENZINGA_DATA_APIKEY") or os.environ.get("BENZINGA_SCANNER_APIKEY")
    if not k and _APIKEY_FILE.exists():
        k = _APIKEY_FILE.read_text().strip() or None
    return k

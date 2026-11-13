"""
Cookie loader.

Design: the user logs in to Benzinga Pro once (cookies are pasted in ZEUS Settings or refreshed by premarket/login.mjs). Historically a relogin helper did two things:
  1. Saves the Chromium profile to `.profile/` (so next login is trivial)
  2. Exports Benzinga cookies to `cookies.json` (what the MCP actually reads)

The MCP only reads `cookies.json` at runtime — it never spawns Playwright.
This avoids the "sync Playwright inside asyncio loop" incompatibility with
FastMCP's event-loop-based tool dispatch, and also makes tool calls fast
(no browser startup per call).

If `cookies.json` is missing we fall back to extracting it via a subprocess
(so the sync Playwright API isn't called from inside the MCP's event loop).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent.parent  # benzinga_mcp/
_DEFAULT_PROFILE = _ROOT / ".profile"
_DEFAULT_COOKIES = _ROOT / "cookies.json"

PROFILE_DIR = Path(os.environ.get("BENZINGA_PROFILE_DIR", _DEFAULT_PROFILE))
COOKIES_PATH = Path(os.environ.get("BENZINGA_COOKIES_PATH", _DEFAULT_COOKIES))

# How old cookies.json can get before we proactively re-extract from the
# Playwright profile. cf_clearance can rotate within hours on busy accounts.
COOKIE_FRESHNESS_SECONDS = 60 * 60 * 6  # 6 hours


class ProfileMissingError(RuntimeError):
    """Raised when neither cookies.json nor a usable profile exists."""


def _read_cookies_json(path: Path) -> list[dict]:
    with path.open() as f:
        return json.load(f)


def _extract_via_subprocess() -> list[dict]:
    """
    Run a tiny subprocess that boots Chromium with the saved profile,
    dumps cookies to stdout as JSON, exits. This isolates Playwright's
    sync API from whatever event loop is hosting us.
    """
    script = r"""
import json, sys
from playwright.sync_api import sync_playwright
from pathlib import Path

profile = Path(sys.argv[1])
if not profile.exists() or not any(profile.iterdir()):
    print("__NO_PROFILE__", file=sys.stderr)
    sys.exit(2)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(user_data_dir=str(profile), headless=True)
    cookies = ctx.cookies()
    ctx.close()

# only benzinga.* cookies
filtered = [c for c in cookies if "benzinga" in (c.get("domain") or "")]
json.dump(filtered, sys.stdout)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(PROFILE_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 2 or "__NO_PROFILE__" in result.stderr:
        raise ProfileMissingError(
            f"No Playwright profile at {PROFILE_DIR}. "
            "Open ZEUS Settings → News sources and paste your Benzinga Pro cookies (or run premarket/login.mjs)."
        )
    if result.returncode != 0:
        raise RuntimeError(f"cookie extraction failed: {result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"cookie extraction produced invalid JSON: {e}")


def _write_cookies_json(path: Path, cookies: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(cookies, f)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def export_cookies_from_profile(cookies_path: Path = COOKIES_PATH) -> int:
    """
    Legacy helper: extract cookies via subprocess and write to disk.
    Returns count written. Caller runs this in a sync context (no asyncio).
    """
    cookies = _extract_via_subprocess()
    _write_cookies_json(cookies_path, cookies)
    return len(cookies)


def _cookies_to_jar(raw: list[dict]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in raw:
        domain = c.get("domain") or ""
        if "benzinga" not in domain:
            continue
        try:
            jar.set(c["name"], c["value"], domain=domain, path=c.get("path") or "/")
        except Exception:
            # SameSite etc. httpx won't take — skip, only session cookies matter.
            pass
    return jar


def load_cookie_jar() -> httpx.Cookies:
    """
    Preferred order:
      1. Read `cookies.json` if it exists and is fresh.
      2. Otherwise extract via subprocess from `.profile/` and cache.
      3. Otherwise raise ProfileMissingError.
    """
    if COOKIES_PATH.exists():
        age = time.time() - COOKIES_PATH.stat().st_mtime
        if age < COOKIE_FRESHNESS_SECONDS:
            return _cookies_to_jar(_read_cookies_json(COOKIES_PATH))

    # stale or missing → try to regenerate from profile
    if PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir()):
        cookies = _extract_via_subprocess()
        _write_cookies_json(COOKIES_PATH, cookies)
        return _cookies_to_jar(cookies)

    # last resort: use a stale cookies.json if present — might still work
    if COOKIES_PATH.exists():
        return _cookies_to_jar(_read_cookies_json(COOKIES_PATH))

    raise ProfileMissingError(
        f"No cookies at {COOKIES_PATH} and no profile at {PROFILE_DIR}. "
        "Open ZEUS Settings → News sources and paste your Benzinga Pro cookies (or run premarket/login.mjs)."
    )

#!/usr/bin/env python3
"""
TastyTrade Historical Trades Fetcher
=====================================
Downloads ALL historical transactions from the TastyTrade API and saves
them to  trades_raw.json  for use with dashboard.html.

Prerequisites:
    client_secret.txt   – your OAuth app client secret
    refresh_token.txt   – your portal-issued refresh token
    (both must already exist – they're used by api_client.py / auth.py)

Usage:
    cd /path/to/tastytrade/folder
    python fetch_trades.py

Output:
    trades_raw.json  – transactions, open positions, and balance
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Always run from the script's directory so relative imports work
os.chdir(Path(__file__).parent)

try:
    from api_client import TastyClient
except ImportError as exc:
    print(f"\n❌ Cannot import api_client: {exc}")
    print("   Make sure api_client.py, auth.py, and config.py are in the same folder.\n")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_account_number(client: TastyClient) -> str:
    """Return the active account number.

    TT can return multiple accounts (e.g. an empty shell account); ordering
    is not guaranteed. Pick: env override > first with any transactions >
    first. Set TT_ACCOUNT_NUMBER to pin a specific account.
    """
    import os
    data = client.get_account()
    items = data.get("data", {}).get("items", [])
    if not items:
        raise RuntimeError(
            "No accounts returned. Check client_secret.txt and refresh_token.txt."
        )

    pinned = os.environ.get("TT_ACCOUNT_NUMBER", "").strip()
    chosen = None
    if pinned:
        chosen = next((it for it in items if it.get("account", {}).get("account-number") == pinned), None)
    if chosen is None:
        for it in items:
            num = it.get("account", {}).get("account-number", "")
            if not num:
                continue
            try:
                resp = client.get(f"/accounts/{num}/transactions", params={"per-page": 1, "page-offset": 0})
                if int(resp.get("pagination", {}).get("total-items", 0)) > 0:
                    chosen = it
                    break
            except Exception:
                continue
    if chosen is None:
        chosen = items[0]

    acct = chosen["account"]
    num = acct["account-number"]
    label = acct.get("nickname") or acct.get("account-type-name", "")
    print(f"  Account : {num}  ({label})  [of {len(items)} accounts]")
    return num


def fetch_all_transactions(client: TastyClient, account: str) -> list[dict]:
    """Paginate /accounts/{account}/transactions until all items are fetched.

    Note: TastyTrade's ``page-offset`` is a **page number** (0-based),
    not an item offset.  We increment by 1 each iteration.
    """
    results: list[dict] = []
    page = 0
    per_page = 250

    while True:
        resp = client.get(
            f"/accounts/{account}/transactions",
            params={
                "per-page": per_page,
                "page-offset": page,
                "sort": "Asc",
            },
        )
        items = resp.get("data", {}).get("items", [])
        total = int(resp.get("pagination", {}).get("total-items", 0))
        results.extend(items)

        pct = int(len(results) / max(total, 1) * 100)
        print(f"  [{pct:3d}%]  {len(results):>5} / {total} transactions", end="\r")

        if not items or len(results) >= total:
            break
        page += 1

    print()  # newline after carriage-return progress
    return results


def fetch_positions(client: TastyClient, account: str) -> list[dict]:
    """Fetch currently open positions (best-effort)."""
    try:
        resp = client.get(f"/accounts/{account}/positions")
        return resp.get("data", {}).get("items", [])
    except Exception as exc:
        print(f"  ⚠  Positions unavailable: {exc}")
        return []


def fetch_balance(client: TastyClient, account: str) -> dict:
    """Fetch current account balance snapshot (best-effort)."""
    try:
        resp = client.get(f"/accounts/{account}/balances")
        return resp.get("data", {})
    except Exception as exc:
        print(f"  ⚠  Balance unavailable: {exc}")
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    sep = "─" * 52
    print(f"\n  TastyTrade Historical Trades Fetcher\n{sep}")

    # ① Authenticate
    print("\n①  Authenticating …")
    try:
        client = TastyClient()
        print("   ✓  Connected to TastyTrade API")
    except Exception as exc:
        print(f"   ✗  {exc}")
        sys.exit(1)

    # ② Account
    print("\n②  Locating account …")
    try:
        account = get_account_number(client)
    except Exception as exc:
        print(f"   ✗  {exc}")
        sys.exit(1)

    # ③ Transactions
    print("\n③  Downloading all transactions …")
    transactions = fetch_all_transactions(client, account)
    print(f"   ✓  {len(transactions)} transactions fetched")

    # ④ Positions
    print("\n④  Open positions …")
    positions = fetch_positions(client, account)
    print(f"   ✓  {len(positions)} open positions")

    # ⑤ Balance
    print("\n⑤  Account balance …")
    balance = fetch_balance(client, account)
    net_liq = balance.get("net-liquidating-value", "N/A")
    print(f"   ✓  Net Liq: ${net_liq}")

    # ⑥ Save
    output = {
        "fetched_at":     datetime.now().isoformat(),
        "account_number": account,
        "balance":        balance,
        "positions":      positions,
        "transactions":   transactions,
    }

    out_path = Path(__file__).parent / "data" / "trades_raw.json"   # runtime folder, outside the code
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))

    print(f"\n{sep}")
    print(f"  ✅  Saved → trades_raw.json")
    print(f"      {len(transactions)} transactions  |  {len(positions)} open positions")
    print()
    print("  Next steps:")
    print("    Option A – serve locally (recommended):")
    print("      python -m http.server 8080")
    print("      then open http://localhost:8080/dashboard.html")
    print()
    print("    Option B – open directly:")
    print("      open dashboard.html  (use the file-picker to load trades_raw.json)")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()

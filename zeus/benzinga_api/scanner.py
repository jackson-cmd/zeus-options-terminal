"""Benzinga Pro stock scanner (data-api-pro).

    from benzinga_api import scanner
    scanner.scan(sort_field="changePercent", limit=50)

⚠ Auth: this endpoint uses an embedded apikey (NOT the session cookies). The
key is baked into pro.benzinga.com's frontend; per project policy we never
commit it. Provide it via the BENZINGA_SCANNER_APIKEY env var, or pass
apikey=... explicitly (grab the value from a logged-in pro.benzinga.com
network request).
"""
from __future__ import annotations

from typing import Iterable, Optional, Union

from ._http import DATA_HOST, data_apikey, get_json

DEFAULT_FIELDS = ("symbol", "price", "change", "changePercent", "marketCap", "dayVolume")


def scan(*, fields: Union[str, Iterable[str]] = DEFAULT_FIELDS,
         filter: str = "subtype_in_ADR,COMMON_SHARE,ETF",
         sort_field: str = "changePercent", sort_dir: str = "DESC",
         limit: int = 100, offset: int = 0, apikey: Optional[str] = None) -> dict:
    """Run the equity scanner. Returns the raw JSON (list of matched rows).

    `filter` / `sort_field` follow Benzinga's scanner DSL (e.g.
    `filter="subtype_in_COMMON_SHARE,marketCap_gt_1e9"`).
    """
    apikey = apikey or data_apikey()
    if not apikey:
        raise RuntimeError(
            "scanner needs an apikey — set BENZINGA_DATA_APIKEY (or "
            "BENZINGA_SCANNER_APIKEY, or benzinga/data_apikey.txt), or pass apikey=. "
            "The value is embedded in pro.benzinga.com's frontend and is "
            "intentionally not stored in this repo (see BENZINGA_API.md §5).")
    fields_csv = fields if isinstance(fields, str) else ",".join(fields)
    params = {
        "apikey": apikey, "fields": fields_csv, "filter": filter,
        "sortField": sort_field, "sortDir": sort_dir,
        "limit": limit, "offset": offset,
    }
    return get_json(f"{DATA_HOST}/rest/v2/scanner/us/equity/scan", params)

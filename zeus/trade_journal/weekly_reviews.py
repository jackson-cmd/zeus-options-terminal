"""JSONL-backed store for weekend review entries.

Schema (one row per ISO week — `week` is the unique key):

    {
      "week":          "2026-W22",
      "week_start":    "2026-05-25",   # Monday, YYYY-MM-DD
      "week_end":      "2026-05-31",   # Sunday
      "completed_at":  "2026-05-30T20:15:00",   # first-save ISO timestamp; frozen on subsequent saves
      "content":       "free text",
      "linked_trades": [{"id": "T7", "ticker": "TSLA",
                         "open_at": "2026-03-26T16:17:17.888Z",
                         "label": "TSLA 375/385P Vertical P"}],
      "created_at":    "...",
      "updated_at":    "..."
    }

The store is small (~52 rows/year), so we rewrite the whole file on each
upsert — keeps the code simple and avoids JSONL append + dedup logic.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent / "data" / "weekly_reviews.jsonl"
_LOCK = threading.Lock()


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _ensure_parent():
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def read_all() -> list[dict]:
    """Return every row, in insertion order. Missing file = empty list."""
    if not STORE_PATH.exists():
        return []
    rows = []
    with STORE_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip corrupt lines rather than crashing the whole store.
                continue
    return rows


def _write_all(rows: list[dict]) -> None:
    _ensure_parent()
    tmp = STORE_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(STORE_PATH)


def by_week(week: str) -> dict | None:
    """Return the single row matching `week`, or None."""
    for r in read_all():
        if r.get("week") == week:
            return r
    return None


def upsert(payload: dict) -> dict:
    """Insert or merge by `week`. `completed_at` is set on first save and
    NEVER updated thereafter; `updated_at` always refreshes."""
    if "week" not in payload:
        raise ValueError("upsert payload missing 'week'")
    with _LOCK:
        now = _now_iso()
        rows = read_all()
        existing_idx = next((i for i, r in enumerate(rows) if r["week"] == payload["week"]), None)
        if existing_idx is None:
            row = {
                "week": payload["week"],
                "week_start": payload.get("week_start"),
                "week_end": payload.get("week_end"),
                "completed_at": now,
                "content": payload.get("content", ""),
                "linked_trades": payload.get("linked_trades", []),
                "created_at": now,
                "updated_at": now,
            }
            rows.append(row)
        else:
            prev = rows[existing_idx]
            row = {
                **prev,
                "week_start": payload.get("week_start", prev.get("week_start")),
                "week_end": payload.get("week_end", prev.get("week_end")),
                "content": payload.get("content", prev.get("content", "")),
                "linked_trades": payload.get("linked_trades", prev.get("linked_trades", [])),
                "updated_at": now,
                # completed_at intentionally NOT touched
            }
            rows[existing_idx] = row
        _write_all(rows)
        return row


def list_summary() -> list[dict]:
    """Minimal projection for the calendar UI (no `content`, no `linked_trades`)."""
    return [
        {
            "week": r["week"],
            "completed_at": r.get("completed_at"),
            "linked_count": len(r.get("linked_trades", []) or []),
        }
        for r in read_all()
    ]


def delete(week: str) -> bool:
    """Remove the row for `week`. Returns True if a row was removed."""
    with _LOCK:
        rows = read_all()
        new_rows = [r for r in rows if r.get("week") != week]
        if len(new_rows) == len(rows):
            return False
        _write_all(new_rows)
        return True


def iso_week_from_date(d: _dt.date) -> str:
    """Return 'YYYY-Www' for the ISO 8601 week containing `d`. Mon–Sun."""
    iso = d.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def week_range_for_iso(week: str) -> tuple[_dt.date, _dt.date]:
    """Return (monday, sunday) date objects for an ISO week like '2026-W22'."""
    year_s, _, wk_s = week.partition("-W")
    year, wk = int(year_s), int(wk_s)
    # `fromisocalendar` with day=1 gives Monday of that ISO week.
    monday = _dt.date.fromisocalendar(year, wk, 1)
    sunday = monday + _dt.timedelta(days=6)
    return monday, sunday


def current_week_iso(now: _dt.datetime | None = None) -> str:
    """ISO week string for `now` (defaults to wall clock)."""
    now = now or _dt.datetime.now()
    return iso_week_from_date(now.date())


def is_completed(week: str) -> bool:
    """True iff a row exists for `week` with a non-empty `completed_at`."""
    row = by_week(week)
    return bool(row and row.get("completed_at"))

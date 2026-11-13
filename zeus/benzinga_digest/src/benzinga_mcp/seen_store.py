"""
Persistent store of already-shown Benzinga article ids, so scheduled digest
runs never re-show the same article. One JSON file: {id: first_seen_iso_utc}.

Atomic writes (tmp + os.replace) so overlapping runs can't corrupt it.
Ids are normalized to str (JSON keys are strings; article ids may be int).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# State lives inside the project so everything (code + output + dedup state)
# stays under the benzinga_mcp folder. Resolved relative to this package file,
# so it's correct regardless of the process cwd.
#   src/benzinga_mcp/seen_store.py -> parents[2] == benzinga_mcp/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _PROJECT_ROOT / "digests" / ".seen.json"


def _norm(id_: object) -> str:
    return str(id_)


def _parseable(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        datetime.fromisoformat(iso)
        return True
    except (ValueError, TypeError):
        return False


def load_seen(path: Path = DEFAULT_PATH) -> dict[str, str]:
    path = Path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # corrupt file: back it up, start fresh (next run may repeat, not crash)
        try:
            path.rename(Path(str(path) + ".corrupt"))
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, data: dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def mark_seen(ids: list[object], path: Path = DEFAULT_PATH, now_iso: str | None = None) -> int:
    """Record ids as seen. Returns count of NEW ids added (existing untouched).

    Note: value stays at first-seen iso for unseen ids. Use `mark_seen_updated`
    when you have an UpdatedAt to track (so future fetches can detect UPDATE)."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    data = load_seen(path)
    added = 0
    for raw_id in ids:
        k = _norm(raw_id)
        if k not in data:
            data[k] = now_iso
            added += 1
    _atomic_write(path, data)
    return added


def mark_seen_updated(records: dict, path: Path = DEFAULT_PATH,
                      now_iso: str | None = None) -> int:
    """Record ids as seen with their Benzinga UpdatedAt timestamp.

    `records` = {id: UpdatedAt_iso_or_empty}. Unlike `mark_seen`, this OVERRIDES
    the stored value when the new UpdatedAt is later (string compare on ISO works),
    so subsequent fetches can detect "same id, newer UpdatedAt → it was updated".
    For ids with empty UpdatedAt, falls back to first-seen iso (legacy behavior).

    Returns count of entries inserted OR advanced."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    data = load_seen(path)
    changed = 0
    for raw_id, upd in records.items():
        k = _norm(raw_id)
        prev = data.get(k)
        new_val = (upd or "").strip() or prev or now_iso
        # Advance only when the incoming value strictly beats the stored one
        # (ISO 8601 strings compare correctly lexicographically when normalised).
        if prev is None or (new_val and new_val > prev):
            data[k] = new_val
            changed += 1
    _atomic_write(path, data)
    return changed


def is_seen(id_: object, seen: dict[str, str]) -> bool:
    return _norm(id_) in seen


def seen_updated_at(id_: object, seen: dict[str, str]) -> str:
    """Return the stored UpdatedAt(or first-seen) iso for id, or empty string."""
    return seen.get(_norm(id_)) or ""


def filter_unseen(items: list[dict], path: Path = DEFAULT_PATH) -> list[dict]:
    """Return only items whose id is NOT in the seen store. Does not mark."""
    seen = load_seen(path)
    return [it for it in items if not is_seen(it.get("id"), seen)]


def prune(path: Path = DEFAULT_PATH, max_age_days: int = 14, now_iso: str | None = None) -> int:
    """Drop ids first seen more than max_age_days ago. Unparseable ts are kept."""
    now = datetime.fromisoformat(now_iso) if _parseable(now_iso) else datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    data = load_seen(path)
    kept: dict[str, str] = {}
    removed = 0
    for k, ts in data.items():
        try:
            seen_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            kept[k] = ts  # unparseable: keep it
            continue
        if seen_at < cutoff:
            removed += 1
        else:
            kept[k] = ts
    if removed:
        _atomic_write(path, kept)
    return removed

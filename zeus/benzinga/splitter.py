"""Split a full Benzinga chat scrape into per-date rows in `history.parquet`.

Reads `data/benzinga/YYYY-MM-DD.json` (the full snapshot from fetcher.py) and
upserts per-date rows into `data/benzinga/history.parquet` (one row per
(date, channel_key)). Overlapping scrapes merge — the longer text wins, PDFs
are preserved if present in either copy.

The scraped chat text uses `M/D/YYYY` lines as date dividers, plus
`Today` / `Yesterday` which are resolved against the snapshot's date.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "benzinga"

# Local import: store.py lives next to splitter.py
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import store as _store  # type: ignore  # noqa: E402

DATE_LINE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", re.M)
# Stream Chat renders "Today" / "Yesterday" for recent-day dividers instead of
# the literal date. We treat them as markers to be resolved against today_iso.
TODAY_LINE = re.compile(r"^(Today|Yesterday)$", re.M)

# Backfill window — emit per-date rows for any date >= this. Set wide so
# we capture the full Stream Chat history (Chris commentary ~6 months,
# OFR ~2 years back).
MIN_DATE = _dt.date(2024, 1, 1)


def _iso(m, d, y):
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def split_text_by_date(text: str, today_iso: str | None = None):
    """Return list of (iso_date or None, block_text).

    Recognises both `M/D/YYYY` markers and `Today` / `Yesterday` markers (the
    latter need today_iso to resolve to a real date).
    """
    today_d = _dt.date.fromisoformat(today_iso) if today_iso else _dt.date.today()
    yesterday_d = today_d - _dt.timedelta(days=1)

    # Collect all marker positions with their resolved iso date
    markers = []  # list of (start, end, iso)
    for m in DATE_LINE.finditer(text):
        markers.append((m.start(), m.end(), _iso(m.group(1), m.group(2), m.group(3))))
    for m in TODAY_LINE.finditer(text):
        iso = today_d.isoformat() if m.group(1).lower() == "today" else yesterday_d.isoformat()
        markers.append((m.start(), m.end(), iso))
    markers.sort()

    if not markers:
        return [(None, text)]

    out = []
    if markers[0][0] > 0:
        prelude = text[: markers[0][0]].strip()
        if prelude:
            out.append((None, prelude))
    for i, (s, e, iso) in enumerate(markers):
        nxt = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        block = text[e:nxt].strip()
        out.append((iso, block))
    return out


def split_snapshot(snapshot_path: Path, today_iso: str | None = None):
    """Read full snapshot → write per-date files. Returns list of dates written."""
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    today_iso = today_iso or snapshot_path.stem  # YYYY-MM-DD
    fetched_at = snap.get("fetched_at")

    # date -> { channel_key -> { label, text } }
    by_date: dict[str, dict] = {}

    for ch_key, ch in (snap.get("channels") or {}).items():
        text = (ch.get("text") or "").strip()
        if not text:
            continue
        label = ch.get("label") or ch_key
        # strip the channel header (first 2 lines: "# channel-name\n\nN Users Online")
        text_stripped = re.sub(r"^#\s*\S+\s*\n+\d+\s+Users Online\s*\n+", "", text)
        blocks = split_text_by_date(text_stripped, today_iso=today_iso)

        # Treat any "None" (prelude/no-date-marker) block as belonging to today
        for date_iso, block in blocks:
            target = date_iso or today_iso
            by_date.setdefault(target, {})[ch_key] = {
                "label": label,
                "text": block,
            }

        # Pair each extracted PDF with the date it was posted under. In the chat
        # text, every PDF attachment leaves a `.pdf` line inside its message
        # block. We walk blocks chronologically counting blocks-that-contain-a-pdf
        # and pair the N-th such block with ch["pdfs"][N]. Falls back to the
        # old latest_pdf→last-date behavior if `pdfs` isn't populated.
        pdf_list = ch.get("pdfs") or ([ch["latest_pdf"]] if ch.get("latest_pdf") else [])
        if pdf_list:
            blocks_with_pdf = [(date, blk) for (date, blk) in blocks
                               if date and ".pdf" in (blk or "").lower()]
            # Align from the tail — both arrays are chronological and the newest
            # PDF in ch["pdfs"] is the freshest one in the chat.
            n = min(len(blocks_with_pdf), len(pdf_list))
            for i in range(1, n + 1):
                pdf_date = blocks_with_pdf[-i][0]
                pdf = pdf_list[-i]
                by_date.setdefault(pdf_date, {}).setdefault(ch_key, {"label": label, "text": ""})
                # Don't overwrite if this date already got a PDF (shouldn't happen
                # but be defensive against duplicate same-day OFRs).
                by_date[pdf_date][ch_key].setdefault("latest_pdf", pdf)

    # upsert into parquet
    rows = []
    min_d = MIN_DATE
    for date_iso, channels in by_date.items():
        try:
            d = _dt.date.fromisoformat(date_iso)
        except ValueError:
            continue
        if d < min_d:
            continue
        for ch_key, ch in channels.items():
            pdf = ch.get("latest_pdf") or {}
            rows.append({
                "date": date_iso,
                "channel_key": ch_key,
                "channel_label": ch.get("label") or ch_key,
                "text": ch.get("text") or "",
                "pdf_name": pdf.get("name"),
                "pdf_text": pdf.get("text"),
                "pdf_chars": int(pdf.get("text_chars")) if pdf.get("text_chars") is not None else None,
                "pdf_url_redacted": pdf.get("url_redacted"),
                "source_snapshot": snapshot_path.name,
                "source_fetched_at": fetched_at,
            })

    if rows:
        _store.upsert_rows(rows)
    return sorted({r["date"] for r in rows})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", nargs="?", help="path to full snapshot JSON (default: latest in data/benzinga/)")
    args = ap.parse_args()

    if args.snapshot:
        snap = Path(args.snapshot)
    else:
        files = sorted(DATA_DIR.glob("[0-9]*.json"))
        if not files:
            print("No snapshot found", file=sys.stderr)
            sys.exit(1)
        snap = files[-1]

    print(f"[info] splitting {snap}", file=sys.stderr)
    written = split_snapshot(snap)
    print(f"[info] upserted {len(written)} dates into {_store.PARQUET}", file=sys.stderr)
    for d in written:
        print(" ", d)


if __name__ == "__main__":
    main()

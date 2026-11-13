"""Parquet-backed storage for per-date Benzinga snapshots.

One file: `data/benzinga/history.parquet`. Schema is one row per (date, channel),
columns:

    date              YYYY-MM-DD (string)
    channel_key       e.g. 'chris-market-commentary'
    channel_label     human label
    text              chat text for that day on that channel
    pdf_name          (nullable) attached PDF filename
    pdf_text          (nullable) extracted PDF text
    pdf_chars         (nullable) char count
    pdf_url_redacted  (nullable) signed URL with signature stripped
    source_snapshot   originating YYYY-MM-DD.json file
    source_fetched_at ISO timestamp of original scrape
    updated_at        ISO timestamp of last write

Upserts merge by (date, channel_key) and keep the longer text payload — so
overlapping scrapes naturally grow more complete instead of clobbering.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "benzinga"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PARQUET = DATA_DIR / "history.parquet"

COLUMNS = [
    "date",
    "channel_key",
    "channel_label",
    "text",
    "pdf_name",
    "pdf_text",
    "pdf_chars",
    "pdf_url_redacted",
    "source_snapshot",
    "source_fetched_at",
    "updated_at",
]


def _empty_df() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
    df["pdf_chars"] = df["pdf_chars"].astype("Int64")
    return df


def read_all() -> pd.DataFrame:
    if not PARQUET.exists():
        return _empty_df()
    df = pd.read_parquet(PARQUET)
    # ensure all expected columns exist
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[COLUMNS]


def read_dates() -> list[str]:
    df = read_all()
    if df.empty:
        return []
    return sorted(df["date"].dropna().unique().tolist())


def read_by_date(date: str) -> dict:
    """Return {channel_key: {label, text, latest_pdf?}} for the given date."""
    df = read_all()
    if df.empty:
        return {}
    sub = df[df["date"] == date]
    out: dict = {}
    for _, row in sub.iterrows():
        ch: dict = {
            "label": row.get("channel_label") or row["channel_key"],
            "text": row.get("text") or "",
        }
        pdf_text = row.get("pdf_text")
        if isinstance(pdf_text, str) and pdf_text:
            ch["latest_pdf"] = {
                "name": row.get("pdf_name") or "",
                "text": pdf_text,
                "text_chars": int(row["pdf_chars"]) if pd.notna(row.get("pdf_chars")) else len(pdf_text),
                "url_redacted": row.get("pdf_url_redacted") or "",
            }
        out[row["channel_key"]] = ch
    return out


def read_latest_per_channel() -> dict:
    """Return {channel_key: {date, label, text, latest_pdf?}} where each
    channel's row is its most-recent date present in the parquet.

    Chris commentary posts daily; the weekly OFR posts on Mondays — so the
    "latest per channel" can have different dates. Used by the PWA Benzinga
    'Today' tab to surface freshest content per channel without falling back
    to a single date (which loses the channel with no row on that date).
    """
    df = read_all()
    if df.empty:
        return {}
    df = df.sort_values("date")
    latest = df.drop_duplicates(subset=["channel_key"], keep="last")
    out: dict = {}
    for _, row in latest.iterrows():
        ch: dict = {
            "date": row["date"],
            "label": row.get("channel_label") or row["channel_key"],
            "text": row.get("text") or "",
        }
        pdf_text = row.get("pdf_text")
        if isinstance(pdf_text, str) and pdf_text:
            ch["latest_pdf"] = {
                "name": row.get("pdf_name") or "",
                "text": pdf_text,
                "text_chars": int(row["pdf_chars"]) if pd.notna(row.get("pdf_chars")) else len(pdf_text),
                "url_redacted": row.get("pdf_url_redacted") or "",
            }
        out[row["channel_key"]] = ch
    return out


def upsert_rows(rows: list[dict]) -> int:
    """Insert/merge a list of row dicts. Returns rows written."""
    if not rows:
        return 0
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    new_df = pd.DataFrame(rows)
    for c in COLUMNS:
        if c not in new_df.columns:
            new_df[c] = pd.NA
    new_df["updated_at"] = now
    new_df = new_df[COLUMNS]

    existing = read_all()
    if existing.empty:
        merged = new_df
    else:
        # combine, then for each (date, channel_key) keep the row with longest
        # text. Then patch any (date, channel_key) that has a PDF in the OTHER
        # copy but not the kept one.
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined["_textlen"] = combined["text"].fillna("").str.len()
        combined["_pdftext_len"] = combined["pdf_text"].fillna("").str.len()
        # winner row per (date, channel_key) = max text length
        combined = combined.sort_values("_textlen", ascending=True)
        text_winners = combined.drop_duplicates(subset=["date", "channel_key"], keep="last").copy()
        # winner pdf per (date, channel_key) = max pdf_text length
        pdf_rows = combined[combined["_pdftext_len"] > 0]
        if not pdf_rows.empty:
            pdf_winners = pdf_rows.sort_values("_pdftext_len", ascending=True).drop_duplicates(
                subset=["date", "channel_key"], keep="last"
            )
            pdf_winners = pdf_winners.set_index(["date", "channel_key"])[
                ["pdf_name", "pdf_text", "pdf_chars", "pdf_url_redacted"]
            ]
            text_winners = text_winners.set_index(["date", "channel_key"])
            for col in pdf_winners.columns:
                text_winners[col] = pdf_winners[col].combine_first(text_winners[col])
            text_winners = text_winners.reset_index()
        merged = text_winners.drop(columns=["_textlen", "_pdftext_len"], errors="ignore")[COLUMNS]

    # write with snappy compression
    merged.to_parquet(PARQUET, compression="snappy", index=False)
    return len(rows)


def stats() -> dict:
    df = read_all()
    if df.empty:
        return {"rows": 0, "bytes_on_disk": 0, "dates": 0}
    return {
        "rows": len(df),
        "bytes_on_disk": PARQUET.stat().st_size if PARQUET.exists() else 0,
        "dates": int(df["date"].nunique()),
        "channels": df["channel_key"].unique().tolist(),
    }


if __name__ == "__main__":
    import json, sys
    print(json.dumps(stats(), indent=2))
    print("dates:", read_dates())

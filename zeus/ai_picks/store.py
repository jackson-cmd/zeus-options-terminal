"""AI 选股结果按天缓存 data/ai_picks/YYYY-MM-DD.json。"""
from __future__ import annotations
import json, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIR = REPO / "data" / "ai_picks"


def _path(date: str) -> Path:
    return DIR / f"{date}.json"


def save(date: str, picks: dict, meta: dict | None = None) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date,
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "n": len(picks),
        "picks": picks,
    }
    if meta:
        payload.update(meta)
    _path(date).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load(date: str) -> dict | None:
    p = _path(date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def latest() -> dict | None:
    """最新一天的缓存(无 date 时给前端兜底)。"""
    try:
        files = sorted(DIR.glob("20??-??-??.json"), reverse=True)
    except Exception:
        return None
    for f in files:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def dates() -> list[str]:
    try:
        return sorted((f.stem for f in DIR.glob("20??-??-??.json")), reverse=True)
    except Exception:
        return []

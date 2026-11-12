"""SR 告警落库 —— 纯函数,无网络。仿 digest_liked 套路。

sr_zone_alert_loop 每命中一条黄金 zone → build_entry + add + save,**独立于静音**
(静音只关弹窗,不关落库)。富化(digest 提及数 + Haiku 中文原因)后台回填 set_enrichment。
store 文件 options_analyzer/data/sr_alert_log.json(gitignore)。
见 spec docs/superpowers/specs/2026-07-20-sr-alert-log-tab-design.md
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path

from .detect import Alert

_KIND_CN = {"sup": "回踩支撑", "res": "上攻阻力"}
_ZONE_FIELDS = ("lo", "hi", "avg", "tfs", "n_pivots")


def _dedup_key(entry: dict):
    """同票同 zone 同交易日一条(对齐 sr_alert.fire_key)。"""
    return (entry.get("ticker"), round(float(entry["zone"]["avg"]), 4), entry.get("day"))


def build_entry(alert: Alert, ts: float, hhmm: str, day: str) -> dict:
    """Alert → 落库 entry(纯)。hhmm 由调用方按 ET 预算好传入。"""
    chg = ((alert.price / alert.prev_close - 1.0) * 100.0) if alert.prev_close else 0.0
    return {
        "day": day,
        "ts": ts,
        "hhmm": hhmm,
        "ticker": alert.ticker,
        "kind": alert.kind,
        "kind_cn": _KIND_CN.get(alert.kind, alert.kind),
        "price": alert.price,
        "prev_close": alert.prev_close,
        "chg_pct": round(chg, 2),
        "zone": {k: alert.zone.get(k) for k in _ZONE_FIELDS},
        # 富化(后台回填)
        "enriched": False,
        "digest_count": 0,
        "digest_titles": [],
        "reason_cn": None,
        "has_narrative": False,
        "has_mover": False,
    }


def load(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except Exception:
        pass
    return {"entries": []}


def add(store: dict, entry: dict):
    """幂等追加;同 dedup_key 已存在 → (store, False)。"""
    entries = store.setdefault("entries", [])
    key = _dedup_key(entry)
    for e in entries:
        if _dedup_key(e) == key:
            return store, False
    entries.append(entry)
    return store, True


def list_recent(store: dict) -> list:
    """按 ts 倒序(最新在前)。"""
    return sorted(store.get("entries", []), key=lambda e: e.get("ts", 0), reverse=True)


def set_enrichment(store: dict, ticker: str, day: str, enrichment: dict) -> dict:
    """把富化结果回填该票当天所有 entry(重复告警共用一份),置 enriched=True。"""
    for e in store.get("entries", []):
        if e.get("ticker") == ticker and e.get("day") == day:
            e.update(enrichment)
            e["enriched"] = True
    return store


def pending(store: dict) -> list:
    """未富化的 (ticker, day) 去重列表 —— 供后台 worker。"""
    seen = set()
    for e in store.get("entries", []):
        if not e.get("enriched"):
            seen.add((e.get("ticker"), e.get("day")))
    return list(seen)


def prune(store: dict, cutoff_day: str) -> dict:
    """删掉 day < cutoff_day 的 entry(ISO 日期串直接比较)。"""
    store["entries"] = [e for e in store.get("entries", []) if e.get("day", "") >= cutoff_day]
    return store


def save(path: str, store: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise

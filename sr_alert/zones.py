"""从 TA screener 的 results.json 载入 conf==3 黄金 zone。

conf == 3 = 日+周+月三周期共振(tfs="DMW")。实测(2026-07-15, 131 票):
全部 1552 个 zone 中 conf3 占 445 (29%),今日 EOD 触及 35 条中 conf3 仅 14 条
→ 该门槛确有筛选力,加去重后 ~14 条/天。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = _ROOT / "data" / "ta_screener" / "results.json"

GOLD_CONF = 3


def _valid_zone(z: dict) -> bool:
    lo, hi, avg = z.get("lo"), z.get("hi"), z.get("avg")
    return (isinstance(lo, (int, float)) and isinstance(hi, (int, float))
            and isinstance(avg, (int, float)) and 0 < lo <= hi)


def load_conf3_zones(path: Optional[Path] = None) -> dict:
    """→ {ticker: [zone]},只保留 conf==3 且边界合法的 zone。

    读不到 / 坏文件 → 返回 {} 而非抛异常:告警是附属功能,
    绝不能因为 results.json 缺失就拖垮 PWA 启动。
    """
    p = Path(path) if path else RESULTS_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict = {}
    for row in (data.get("rows") or []):
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        if not ticker:
            continue
        zs = [z for z in (row.get("zones") or [])
              if isinstance(z, dict) and z.get("conf") == GOLD_CONF and _valid_zone(z)]
        if zs:
            out[ticker] = zs
    return out

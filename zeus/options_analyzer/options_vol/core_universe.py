"""core_universe.py —— 全系统唯一权威 universe。

= summary.json 里 tier∈{gold,silver} 且 **非 ADR** 的票  +  FORCE_INCLUDE(刚上市等手工加的)
  − 即:放一边(aside/nodata)去掉、ADR 去掉、gold+silver 合并成一份。

产物 data/options_vol/core_universe.txt(每行一个 ticker)是 heatmap / TA / Options tab 共用的票池
(heatmap_universe.txt、ta_screener_universe.txt 由各自的同步步骤从这份再生)。

跑: cd options_analyzer && python -m options_vol.core_universe
"""
from __future__ import annotations

import json
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR.parent.parent / "data" / "options_vol"
SUMMARY_PATH = DATA_DIR / "summary.json"
CORE_OUT = DATA_DIR / "core_universe.txt"

# 不靠扫描结果、强制纳入的票(刚上市数据太薄但作者要盯,如 SpaceX 壳 SPCX)
FORCE_INCLUDE = ["SPCX"]


def build_core(summary_path: Path = SUMMARY_PATH, out_path: Path = CORE_OUT,
               force_include=FORCE_INCLUDE) -> list[str]:
    """读 summary.json → gold+silver 非 ADR + force_include → 排序写 core_universe.txt。"""
    summary_path = Path(summary_path)
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    keep = {
        t["ticker"].upper()
        for t in data.get("tickers", [])
        if t.get("tier") in ("gold", "silver") and not t.get("is_adr")
    }
    keep.update(s.upper() for s in (force_include or []))
    core = sorted(keep)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(core) + "\n", encoding="utf-8")
    return core


def load_core(path: Path = CORE_OUT) -> list[str]:
    """下游(heatmap/TA 同步、Options tab)读 core_universe.txt;不存在则现算。"""
    path = Path(path)
    if path.exists():
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return build_core()


def main() -> None:
    core = build_core()
    print(f"core universe = {len(core)} 票(gold+silver − ADR + {FORCE_INCLUDE}) → {CORE_OUT}")


if __name__ == "__main__":
    main()

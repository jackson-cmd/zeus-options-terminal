"""个股票池 = core_universe − ETF/ETP。

ETF/ETP 集 = heatmap_universe.txt 里 section 名含「ETF」段下所有 ticker
            ∪ CURATED_ETP(杠杆/反向/商品 ETP, section 名不含 ETF 的漏网者)。
"""
from __future__ import annotations
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "data" / "options_vol" / "core_universe.txt"
HEATMAP = REPO / "data" / "heatmap_universe.txt"

# 杠杆/反向/商品 ETP, 在 heatmap "Options 核心" 等非 ETF 段里, 名字看不出
CURATED_ETP = {"NVDL", "TSLL", "SCO", "TMF", "UPRO", "NASA", "SPCX", "DRAM"}


def _core() -> list:
    out = []
    for ln in CORE.read_text().splitlines():
        s = ln.strip().upper()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def etf_etp_set() -> set:
    """heatmap_universe.txt 里 ###…ETF… 段下的 ticker + CURATED_ETP。"""
    etfs = set(CURATED_ETP)
    try:
        raw = HEATMAP.read_text()
    except Exception:
        return etfs
    is_etf_section = False
    for tok in raw.replace("\n", ",").split(","):
        t = tok.strip()
        if not t:
            continue
        if t.startswith("###"):
            is_etf_section = "ETF" in t.upper()
            continue
        sym = t.split(":")[-1].strip().upper()   # 去 EXCHANGE: 前缀
        if is_etf_section and sym:
            etfs.add(sym)
    return etfs


def individual_stocks() -> list:
    """core_universe 里的个股(排除 ETF/ETP), 保持原顺序。"""
    etfs = etf_etp_set()
    return [t for t in _core() if t not in etfs]


if __name__ == "__main__":
    core = _core()
    etfs = etf_etp_set()
    indiv = individual_stocks()
    excluded = sorted(t for t in core if t in etfs)
    print(f"core={len(core)}  个股={len(indiv)}  排除(ETF/ETP)={len(excluded)}")
    print("排除列表:", ", ".join(excluded))

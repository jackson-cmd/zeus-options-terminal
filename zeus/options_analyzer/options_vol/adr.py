"""adr.py —— ADR 识别(把外国存托凭证从 universe 里剔出去)。

口径(可复现、不依赖在线 API,便于周末 cron):
  1. 公司名含 "ADR" → ADR(Barchart 命名很一致:"...Plc ADR" / "...Inc ADR" / "...Sa ADR")。
     名字来源:data/options_vol/sources/screener.csv(Symbol,Name)+ screener_base.csv(symbol,symbolName,
     即 493417 周更基底,存在才读)。
  2. CURATED_ADR:已知是 ADR 但 Barchart 名里不带 "ADR"(多为 watchlist 带进来、不在 screener 里的票,
     如 TSM/UMC/HIMX/RIO)→ 手工兜底。新发现的可往里加。

用法:
  from options_vol.adr import is_adr, load_name_map, split_adr
  nm = load_name_map(); is_adr("BABA", nm) -> True
"""
from __future__ import annotations

import csv
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR.parent.parent / "data" / "options_vol"
SCREENER_CSV = DATA_DIR / "sources" / "screener.csv"
SCREENER_BASE_CSV = DATA_DIR / "screener_base.csv"   # 493417 周更爬出(可能不存在)

# 已知 ADR 但名字里不带 "ADR" 的票(名字源缺失/Barchart 没标)——手工兜底,可扩
CURATED_ADR = {
    "TSM", "UMC", "HIMX", "RIO", "NVS", "BHP", "AU", "TM",
}


def load_name_map() -> dict[str, str]:
    """{TICKER(大写): 公司名}。合并 screener.csv + screener_base.csv(若有)。"""
    nm: dict[str, str] = {}
    # screener.csv: 列 Symbol, Name
    if SCREENER_CSV.exists():
        with open(SCREENER_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                s = (r.get("Symbol") or "").strip().upper()
                n = (r.get("Name") or "").strip()
                if s and n and s not in nm:
                    nm[s] = n
    # screener_base.csv: 列 symbol, symbolName(493417 周更基底)
    if SCREENER_BASE_CSV.exists():
        with open(SCREENER_BASE_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                s = (r.get("symbol") or r.get("Symbol") or "").strip().upper()
                n = (r.get("symbolName") or r.get("name") or "").strip()
                if s and n:
                    nm.setdefault(s, n)
    return nm


def is_adr(ticker: str, name_map: dict[str, str] | None = None) -> bool:
    """该票是不是 ADR。name_map 不传则现加载(批量请预先 load_name_map 复用)。"""
    t = (ticker or "").strip().upper()
    if not t:
        return False
    if t in CURATED_ADR:
        return True
    if name_map is None:
        name_map = load_name_map()
    name = name_map.get(t, "")
    return "ADR" in name.upper()


def split_adr(tickers, name_map: dict[str, str] | None = None):
    """(non_adr, adr) 两个列表,保持入参顺序。"""
    if name_map is None:
        name_map = load_name_map()
    non_adr, adr = [], []
    for t in tickers:
        (adr if is_adr(t, name_map) else non_adr).append(t)
    return non_adr, adr

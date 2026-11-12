#!/usr/bin/env python3
"""universe.py — 合并两份 ticker 清单为待爬 universe。

输入(都在 data/options_vol/sources/,从作者 Downloads 拷入):
  screener.csv   Barchart screener 导出(首列 Symbol;末行有 "Downloaded from..." footer 要跳过)
  watchlist.txt  TradingView watchlist(EXCHANGE:TICKER 逗号/换行分隔)

输出: data/options_vol/universe.txt(每行一个 ticker,去重排序)。

用法: python -m options_vol.universe   (从 options_analyzer/ 目录)
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
# options_analyzer/options_vol/ → 仓库根/data/options_vol/
DATA_DIR = PKG_DIR.parent.parent / "data" / "options_vol"
SRC_DIR = DATA_DIR / "sources"
CSV_SRC = SRC_DIR / "screener.csv"
TXT_SRC = SRC_DIR / "watchlist.txt"
UNIVERSE_OUT = DATA_DIR / "universe.txt"

# 合法 ticker: 大写字母,允许一个点(BRK.B);剔除 footer / 表头噪声
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")


def _clean(sym: str) -> str | None:
    """剥 EXCHANGE: 前缀、大写、校验。非法返回 None。"""
    if not sym:
        return None
    sym = sym.strip().upper()
    if ":" in sym:
        sym = sym.split(":")[-1].strip()
    return sym if _TICKER_RE.match(sym) else None


def read_csv_symbols(path: Path = CSV_SRC) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = _clean(row.get("Symbol") or "")
            if t:
                out.add(t)
    return out


def read_txt_symbols(path: Path = TXT_SRC) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    raw = path.read_text(encoding="utf-8").replace("\n", ",")
    for tok in raw.split(","):
        t = _clean(tok)
        if t:
            out.add(t)
    return out


def build_universe() -> list[str]:
    csv_syms = read_csv_symbols()
    txt_syms = read_txt_symbols()
    union = sorted(csv_syms | txt_syms)
    UNIVERSE_OUT.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_OUT.write_text("\n".join(union) + "\n", encoding="utf-8")
    print(f"CSV={len(csv_syms)}  TXT={len(txt_syms)}  overlap={len(csv_syms & txt_syms)}"
          f"  union={len(union)}")
    print(f"→ {UNIVERSE_OUT}")
    return union


def load_universe(path: Path = UNIVERSE_OUT) -> list[str]:
    """下游(爬虫)读 universe.txt。不存在则现算。"""
    if path.exists():
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return build_universe()


if __name__ == "__main__":
    build_universe()

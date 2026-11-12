"""Barchart saved-screener 落库 —— 把 scrape_screener.py 抓下来的散落 CSV/TXT 合并成一份

统一 JSON(data/barchart_screeners.json), 给 ai_picks / PWA / by_ticker 反查共用。

语义全部委托 registry(唯一契约源): 分类/颜色/bias/desc 都从 R.screener_meta 取, 本模块只做
"读文件 → 提 ticker → 组装 JSON"的 IO 与聚合, 不重复任何语义判断。

产物三大件:
  · screeners     : 每个 screener 的语义 + 命中 ticker 列表(按 category_order + name 排序)
  · by_ticker     : {TICKER: [slug,...]} 反查 —— 这只票出现在哪些信号池
  · ai_universe_add: 所有 screener 里 STK(排除 ETF)的去重大写并集, 排序 —— 并入 AI universe 的股票集

健壮性: 单个 csv 坏了只跳过它、不影响其余; 文件缺失(旧快照没有新 screener)有几个处理几个。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from barchart import registry as R

# ── 路径 ────────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent
SCREENERS_DIR = REPO / "barchart" / "downloads" / "screeners"
OUT_JSON = REPO / "data" / "barchart_screeners.json"


def _empty() -> dict:
    """空骨架 —— load 失败且不重建时返回, 结构与 build 产物一致。"""
    return {
        "asof": None,
        "categories": R.CATEGORIES,
        "screeners": [],
        "by_ticker": {},
        "ai_universe_add": [],
    }


def _read_csv(path: Path) -> list[tuple[str, str]]:
    """读 screener csv, 返回 [(SYMBOL, symbolCode), ...](大写、保序、去重)。

    坏文件/缺列只降级为空, 不外抛。symbolCode 缺失当空串(会被当非 STK 处理)。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip().upper()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                code = (row.get("symbolCode") or "").strip().upper()
                out.append((sym, code))
    except Exception as e:  # noqa: BLE001 —— 单文件坏不拖垮整体
        print(f"[consolidate] 跳过坏 csv {path.name}: {e}")
    return out


def _read_txt(path: Path) -> list[tuple[str, str]]:
    """读 <safe>_tickers.txt 兜底(csv 不存在时), 每票当 STK 处理。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            sym = line.strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append((sym, "STK"))
    except Exception as e:  # noqa: BLE001
        print(f"[consolidate] 跳过坏 txt {path.name}: {e}")
    return out


def build(screeners_dir=None, out_path=None) -> dict:
    """遍历 screeners 目录, 合并成统一 JSON 并落盘, 返回产物 dict。

    优先 csv(含 symbolCode 用于 ETF 判定); 只有 <safe>_tickers.txt 没有 csv 的 screener 退 txt
    (此时全票当 STK)。base 及 EXCLUDE 内的 screener 跳过。
    """
    sdir = Path(screeners_dir) if screeners_dir else SCREENERS_DIR
    out_p = Path(out_path) if out_path else OUT_JSON

    # stem → [(SYMBOL, code), ...]; 优先 csv, 记录已处理 stem 避免 txt 重复
    per_screener: dict[str, list[tuple[str, str]]] = {}
    handled: set[str] = set()

    # 1) csv 优先
    for csv_path in sorted(sdir.glob("*.csv")) if sdir.is_dir() else []:
        stem = csv_path.stem
        if R.is_excluded(stem):
            continue
        per_screener[stem] = _read_csv(csv_path)
        handled.add(stem)

    # 2) 只有 txt 无 csv 的 screener 退 txt(去掉 _tickers 后缀当 stem)
    for txt_path in sorted(sdir.glob("*_tickers.txt")) if sdir.is_dir() else []:
        stem = txt_path.stem[: -len("_tickers")]
        if stem in handled or R.is_excluded(stem):
            continue
        per_screener[stem] = _read_txt(txt_path)
        handled.add(stem)

    # 3) 组装 screeners + by_ticker + ai_universe_add
    screeners: list[dict] = []
    by_ticker: dict[str, list[str]] = {}
    ai_add: set[str] = set()

    for stem, rows in per_screener.items():
        meta = R.screener_meta(stem)
        slug = meta["slug"]
        tickers = [sym for sym, _ in rows]
        screeners.append({
            "name": meta["name"], "slug": slug, "category": meta["category"],
            "category_label": meta["category_label"], "bias": meta["bias"],
            "color": meta["color"], "desc": meta["desc"],
            "count": len(tickers), "tickers": tickers,
        })
        for sym, code in rows:
            slugs = by_ticker.setdefault(sym, [])
            if slug not in slugs:
                slugs.append(slug)
            if code == "STK":  # ETF/其他类型不并入 AI universe
                ai_add.add(sym)

    # screeners 按 category_order + name 排序
    order = R.category_order()

    def _sort_key(s: dict):
        cat = s["category"]
        return (order.index(cat) if cat in order else len(order), s["name"])

    screeners.sort(key=_sort_key)

    result = {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "categories": R.CATEGORIES,
        "screeners": screeners,
        "by_ticker": by_ticker,
        "ai_universe_add": sorted(ai_add),
    }

    # 4) 落盘(建父目录, 写失败只降级不外抛)
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[consolidate] 写 {out_p} 失败: {e}")

    return result


def _is_stale(out_p: Path, screeners_dir: Path) -> bool:
    """json 比目录里最新的 CSV/txt 还旧 → 过期(scrape 每天更新 CSV, 得跟着重建)。"""
    try:
        if not out_p.exists():
            return True
        j = out_p.stat().st_mtime
        newest = max(
            (f.stat().st_mtime for f in screeners_dir.glob("*") if f.is_file()),
            default=0.0,
        )
        return newest > j
    except Exception:  # noqa: BLE001
        return False


def load(out_path=None, rebuild_if_missing=True, refresh_if_stale=True) -> dict:
    """读 OUT_JSON; 缺失且 rebuild_if_missing 则 build(); 过期且 refresh_if_stale 则重建。

    refresh_if_stale: scrape 每天更新 CSV, 若 json 比最新 CSV 旧就重建, 保证端点/AI 拿到当日榜单。
    """
    out_p = Path(out_path) if out_path else OUT_JSON
    try:
        if out_p.exists():
            if refresh_if_stale and out_path is None and _is_stale(out_p, SCREENERS_DIR):
                return build(out_path=out_p)
            return json.loads(out_p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[consolidate] 读 {out_p} 失败: {e}")
    if rebuild_if_missing:
        return build(out_path=out_p)
    return _empty()


def ai_universe_add(out_path=None) -> list[str]:
    """便捷函数: 直接拿并入 AI universe 的股票列表。"""
    return load(out_path=out_path).get("ai_universe_add", [])


if __name__ == "__main__":
    data = build()
    sc = data["screeners"]
    print(f"[consolidate] {SCREENERS_DIR}")
    print(f"  screeners : {len(sc)} 个")
    for s in sc:
        flag = "" if s["slug"].startswith("watch_") else " "
        print(f"    · {s['name']:18} [{s['category_label']}] {s['count']:>4} 票  ({s['slug']})")
    print(f"  by_ticker : 覆盖 {len(data['by_ticker'])} 只票")
    print(f"  ai_universe_add : {len(data['ai_universe_add'])} 只 STK")
    print(f"  → {OUT_JSON}")

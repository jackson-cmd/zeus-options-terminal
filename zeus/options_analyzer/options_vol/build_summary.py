"""build_summary.py —— 读爬好的 history.parquet,逐票跑 classify,落 summary.json。

数据流(见 spec「架构」):
  data/options_vol/history.parquet  (长表,scrape_history.py 产出)
        │  按 ticker 分组 → list-of-dicts → classify_ticker
        ▼
  data/options_vol/summary.json     (每票 tier + 展示指标,前端表格/分档读它)

universe_size / n_failed 优先读 scrape_meta.json(爬虫侧写);没有就从 parquet 推
(universe_size = 票数, n_failed = 0)。n_scraped 始终 = parquet 里实际有数据的票数。

跑: cd options_analyzer && python -m options_vol.build_summary
可测: build(history_path=..., out_path=..., meta_path=...) 接受自定义路径。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from options_vol.classify import classify_ticker
from options_vol.adr import load_name_map, is_adr

PKG_DIR = Path(__file__).resolve().parent
# options_analyzer/options_vol/ → 仓库根/data/options_vol/
DATA_DIR = PKG_DIR.parent.parent / "data" / "options_vol"
HISTORY_PATH = DATA_DIR / "history.parquet"
SUMMARY_PATH = DATA_DIR / "summary.json"
META_PATH = DATA_DIR / "scrape_meta.json"


def _load_meta(meta_path: Path) -> dict:
    """读爬虫侧 scrape_meta.json(可能没有)。坏文件/不存在 → 空 dict,不崩。"""
    try:
        if meta_path and Path(meta_path).exists():
            return json.loads(Path(meta_path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        pass
    return {}


def _rows_for_group(df_group) -> list[dict]:
    """某票的 DataFrame 分组 → list-of-dicts(classify 吃的形态)。

    只挑 classify 用得到的列,缺列容忍(用 .get 取);date 统一成 YYYY-MM-DD str。
    """
    cols = [c for c in (
        "date", "totalVolume", "totalOpenInterest", "weightedImpliedVolatility",
        "impliedVolatilityRank1y", "putCallVolumeRatio", "historicalLastPrice",
    ) if c in df_group.columns]
    out = []
    for rec in df_group[cols].to_dict("records"):
        d = rec.get("date")
        # date 可能是 Timestamp / date / str → 统一成 YYYY-MM-DD
        if d is not None and not isinstance(d, str):
            try:
                d = d.strftime("%Y-%m-%d")
            except AttributeError:
                d = str(d)[:10]
        rec["date"] = d
        out.append(rec)
    return out


def build(history_path=HISTORY_PATH, out_path=SUMMARY_PATH, meta_path=META_PATH) -> dict:
    """读 parquet → 逐票 classify → 组装并落 summary.json。返回该 summary dict。

    参数全部可覆盖,便于用临时合成 parquet 单测(绝不依赖真实 data/options_vol/)。
    """
    import pandas as pd

    history_path = Path(history_path)
    out_path = Path(out_path)

    df = pd.read_parquet(history_path)

    name_map = load_name_map()   # ADR 识别用(名字源:screener.csv + screener_base.csv)
    tickers_out: list[dict] = []
    if not df.empty and "ticker" in df.columns:
        # 按 ticker 名排序输出,稳定可读;每组转 list-of-dicts 调纯函数
        for ticker, grp in df.groupby("ticker", sort=True):
            tk = str(ticker)
            res = classify_ticker(_rows_for_group(grp))
            tickers_out.append({"ticker": tk, "is_adr": is_adr(tk, name_map), **res})

    n_scraped = len(tickers_out)   # 实际有数据(在 parquet 里出现)的票数

    # universe_size / n_failed:优先 meta,否则从 parquet 推
    meta = _load_meta(meta_path)
    universe_size = int(meta.get("universe_size", n_scraped))
    n_failed = int(meta.get("n_failed", 0))

    summary = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "universe_size": universe_size,
        "n_scraped": n_scraped,
        "n_failed": n_failed,
        "tickers": tickers_out,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    s = build()
    # 各档计数 + 落盘提示(日终刷新看日志用)
    from collections import Counter
    tiers = Counter(t["tier"] for t in s["tickers"])
    print(f"universe={s['universe_size']}  scraped={s['n_scraped']}  failed={s['n_failed']}")
    print(f"tiers: gold={tiers.get('gold', 0)}  silver={tiers.get('silver', 0)}  "
          f"aside={tiers.get('aside', 0)}  nodata={tiers.get('nodata', 0)}")
    print(f"→ {SUMMARY_PATH}")


if __name__ == "__main__":
    main()

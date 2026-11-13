#!/usr/bin/env python3
"""refresh.py —— Options-vol 周更编排(每周日 ~07:00 由 launchd 跑)。

一条龙:重爬 493417 base → 找新候选 → 补爬它们的 options-history(增量 append)→
重建 summary.json → 重建 core_universe.txt → 同步下游 universe 文件(heatmap/TA)。

为什么是「增量」而非全量重爬:
  - 493417 是作者维护的 base,周更可能新进几只票(刚上市/刚达标)。
  - 已经爬过的票(history.parquet 里有的)不必每周全重爬(慢且无谓);只补「新候选」。
  - 但 ADR 不进系统(core_universe = gold+silver − ADR),所以新候选先剔 ADR 再爬。
  - 补爬到的行 concat 进老 parquet,按 (ticker,date) 去重保最新 —— 同一天重爬以新值为准。

  ⚠️ 今天大概率 0 新候选(493417 ⊆ 现 universe),那就是 no-op 补爬、只重建
     summary/core/sync,这是预期且正确的。

下游同步(sync_consumers.sync_all)由另一 agent 实现;**懒导入 + try/except 包**,
缺模块只告警不崩(本编排其余步骤照常完成)。

跑(从 options_analyzer/ 目录):
  python -m options_vol.refresh
或:bash options_vol/run_weekly.sh
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd

from options_vol import screener_base
from options_vol import scrape_history
from options_vol import build_summary
from options_vol import core_universe
from options_vol.adr import is_adr, load_name_map

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent.parent
DATA_DIR = REPO_ROOT / "data" / "options_vol"
HISTORY_PARQUET = DATA_DIR / "history.parquet"

# scrape_history 的契约列顺序(append 时对齐 schema,避免列错位)
HISTORY_COLUMNS = scrape_history.COLUMNS


def _log(msg: str) -> None:
    print(f"[refresh] {msg}", flush=True)


def _scanned_tickers() -> set[str]:
    """已爬过的票 = history.parquet 里出现过的 ticker(大写)。文件缺失则空集。"""
    if not HISTORY_PARQUET.exists():
        return set()
    try:
        df = pd.read_parquet(HISTORY_PARQUET, columns=["ticker"])
    except Exception as e:  # noqa: BLE001  坏 parquet 不该让整条编排崩
        _log(f"⚠️ 读 history.parquet 失败,按空已爬集处理: {type(e).__name__}: {e}")
        return set()
    return {str(t).strip().upper() for t in df["ticker"].dropna().unique()}


def _base_symbols(rows: list[dict]) -> list[str]:
    """从 493417 行里抽 symbol 列(大写去重,保留出现顺序)。"""
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        s = str(r.get("symbol") or r.get("Symbol") or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _append_history(new_rows: list[dict]) -> int:
    """把补爬到的长表行 concat 进老 history.parquet,按 (ticker,date) 去重保最新。

    「保最新」= 新爬的行(append 在后)在重复 key 上覆盖老行 → keep='last'。
    返回去重后 parquet 总行数。
    """
    new_df = pd.DataFrame(new_rows, columns=HISTORY_COLUMNS)
    if HISTORY_PARQUET.exists():
        old_df = pd.read_parquet(HISTORY_PARQUET)
        # 对齐列顺序(老文件理论上已是这套列,稳妥起见 reindex)
        old_df = old_df.reindex(columns=HISTORY_COLUMNS)
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df
    # 同一 (ticker,date) 多行 → 保后出现的(新爬覆盖老值)
    combined["date"] = combined["date"].astype(str)
    combined = combined.drop_duplicates(subset=["ticker", "date"], keep="last")
    combined = combined.sort_values(["ticker", "date"]).reset_index(drop=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(HISTORY_PARQUET, index=False)
    return len(combined)


def _sync_consumers() -> dict:
    """懒导入 sync_consumers.sync_all 并调用(从 core_universe 再生 heatmap/TA universe)。

    sync_consumers.py 由另一 agent 写,签名 sync_all(core_path=None)->dict。
    缺模块/出错只告警不崩 —— 本编排其余步骤已完成,同步失败可单独补跑。
    """
    try:
        from options_vol.sync_consumers import sync_all  # 懒导入:缺了也不影响前面步骤
    except Exception as e:  # noqa: BLE001  ModuleNotFoundError 等
        _log(f"⚠️ sync_consumers 不可用,跳过下游同步(另行处理): {type(e).__name__}: {e}")
        return {}
    try:
        res = sync_all() or {}
        _log(f"下游同步完成: {res}")
        return res
    except Exception as e:  # noqa: BLE001  同步内部出错也不该让整条编排算失败
        _log(f"⚠️ sync_all() 执行出错(已捕获,不影响其余产物): {type(e).__name__}: {e}")
        return {}


def refresh() -> dict:
    """周更主流程。返回各步统计 dict(供脚本/日志查看,不抛异常即视为成功)。"""
    result: dict = {}

    # a. 爬 493417 → screener_base.csv
    _log("1/6 爬 493417 base screener …")
    base_rows = asyncio.run(screener_base.scrape_base())
    screener_base._write_csv(base_rows)
    base_syms = _base_symbols(base_rows)
    result["base_count"] = len(base_syms)
    _log(f"    493417 = {len(base_syms)} 票 → screener_base.csv")

    # b. 新候选 = (493417 symbols) − 已爬票,再剔 ADR
    scanned = _scanned_tickers()
    name_map = load_name_map()   # 名字源含刚写的 screener_base.csv → ADR 识别更全
    raw_new = [s for s in base_syms if s not in scanned]
    new_candidates = [s for s in raw_new if not is_adr(s, name_map)]
    n_adr_skipped = len(raw_new) - len(new_candidates)
    result["new_candidates"] = new_candidates
    result["n_new_candidates"] = len(new_candidates)
    result["n_adr_skipped"] = n_adr_skipped
    _log(f"    已爬 {len(scanned)} 票;493417 里未爬 {len(raw_new)} 个,"
         f"剔 ADR {n_adr_skipped} 个 → 新候选 {len(new_candidates)} 个")
    if new_candidates:
        _log(f"    新候选: {', '.join(new_candidates[:30])}"
             f"{'' if len(new_candidates) <= 30 else f' …(+{len(new_candidates)-30})'}")

    # c. 补爬新候选的 options-history,append 进 parquet(去重保最新)
    if new_candidates:
        _log(f"2/6 补爬 {len(new_candidates)} 个新候选的 options-history(并发 5)…")
        new_rows = asyncio.run(
            scrape_history.scrape(new_candidates, concurrency=scrape_history.CONCURRENCY))
        stats = getattr(scrape_history.scrape, "last_stats", {})
        total_rows = _append_history(new_rows)
        result["appended_rows"] = len(new_rows)
        result["history_total_rows"] = total_rows
        result["scrape_stats"] = stats
        _log(f"    补爬 {len(new_rows)} 行,history.parquet 现 {total_rows} 行"
             f"(成功 {stats.get('n_scraped','?')}/失败 {stats.get('n_failed','?')})")
    else:
        _log("2/6 无新候选 → 跳过补爬(no-op,符合预期)")
        result["appended_rows"] = 0
        if HISTORY_PARQUET.exists():
            try:
                result["history_total_rows"] = len(
                    pd.read_parquet(HISTORY_PARQUET, columns=["ticker"]))
            except Exception:  # noqa: BLE001
                result["history_total_rows"] = None

    # d. 重建 summary.json
    _log("3/6 重建 summary.json …")
    summary = build_summary.build()
    result["summary_tickers"] = len(summary.get("tickers", []))
    _log(f"    summary.json: {result['summary_tickers']} 票")

    # e. 重建 core_universe.txt
    _log("4/6 重建 core_universe.txt …")
    core = core_universe.build_core()
    result["core_size"] = len(core)
    _log(f"    core_universe.txt: {len(core)} 票")

    # f. 同步下游 universe 文件(heatmap / TA)
    _log("5/6 同步下游(sync_consumers.sync_all)…")
    result["sync"] = _sync_consumers()

    _log("6/6 周更完成 ✓")
    return result


def main() -> None:
    res = refresh()
    print("=" * 70)
    print("✓ refresh() 完成")
    print(f"  493417 base 票数      = {res.get('base_count')}")
    print(f"  新候选(剔ADR后)       = {res.get('n_new_candidates')}"
          f"  (跳过 ADR {res.get('n_adr_skipped')} 个)")
    print(f"  本次补爬行数          = {res.get('appended_rows')}")
    print(f"  history.parquet 总行  = {res.get('history_total_rows')}")
    print(f"  summary.json 票数     = {res.get('summary_tickers')}")
    print(f"  core_universe 票数    = {res.get('core_size')}")
    print(f"  下游同步              = {res.get('sync')}")


if __name__ == "__main__":
    main()

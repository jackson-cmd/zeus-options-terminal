"""TA Screener 扫描编排:universe → 下载 → 引擎分析 → 外部信号合并 → 排序 → JSON。

线程模型:start_scan() 起后台线程,STATE 暴露进度;结果原子写
data/ta_screener/results.json。任何一票失败只记 failed,不中断。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "ta_screener"
RESULTS_FILE = OUT_DIR / "results.json"

STATE = {
    "running": False, "pct": 0, "msg": "空闲", "error": None,
    "started_at": None, "finished_at": None, "generated_at": None,
}
_LOCK = threading.Lock()


def _np_default(o):
    """numpy 标量 → 原生类型(json 不认 np.bool_/np.int64/np.float64)。"""
    import numpy as np
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        v = float(o)
        return None if v != v else v   # NaN → null
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not serializable: {type(o)}")


def _set(pct=None, msg=None, **kw):
    with _LOCK:
        if pct is not None:
            STATE["pct"] = int(pct)
        if msg is not None:
            STATE["msg"] = str(msg)
        STATE.update(kw)


def _maybe_rebuild_pool(log=print) -> None:
    """专属池缺失 / 非今日构建 → 重建(波动+新闻+价格);失败不抛(退回旧池/全 universe)。"""
    meta_p = OUT_DIR / "pool_meta.json"
    if meta_p.exists():
        try:
            ga = json.loads(meta_p.read_text(encoding="utf-8")).get("generated_at", "")
            if ga.startswith(_dt.date.today().isoformat()):
                return                              # 今天已建,跳过
        except Exception:
            pass
    try:
        from . import build_pool as BP
    except ImportError:
        import build_pool as BP  # type: ignore
    _set(2, "构建专属池(波动+新闻+价格)…")
    BP.build(log=log)


def run_scan(log=print) -> dict:
    """同步全量扫描(由后台线程调用)。返回写盘的结果 dict。"""
    try:  # 包内相对导入;直跑时退回同目录(import data 会撞仓库根 data/ 目录,故须优先包内)
        from . import data as D, engine as E, external as X
    except ImportError:
        import data as D, engine as E, external as X  # type: ignore

    _set(0, "解析 universe…")
    tickers, cat_map = D.parse_universe()
    sec_map = D.sector_map()   # 粗粒度 GICS sector(借 heatmap metadata,快速筛选用)
    ind_map = D.industry_map()  # 细粒度 industry(Heatmap 视图 industry 分组用)
    full_n = len(tickers)
    try:
        _maybe_rebuild_pool(log)   # 池子陈旧/缺失 → 先重建(自维护,每天一次)
    except Exception as e:
        log(f"[ta_screener] 池子重建跳过(用旧池/全universe): {e}")
    pool = D.load_pool()       # 专属池:只扫「会动 + 价格带合格」的票(不符合的压根不跑)
    if pool:
        tickers = [t for t in tickers if t in pool]
        log(f"[ta_screener] 专属池过滤: {len(tickers)}/{full_n} 票(剔掉不会动/太便宜/太贵的)")
    universe = set(tickers)
    log(f"[ta_screener] universe: {len(tickers)} tickers")

    # 1) 下载(0→55%)
    def dl_cb(done, total, msg):
        _set(pct=int(done / total * 55), msg=msg)
    hist = D.download_history(tickers, progress_cb=dl_cb)
    failed = [t for t in tickers if t not in hist]
    log(f"[ta_screener] history ok={len(hist)} failed={len(failed)}")

    # 1.5) 大盘 regime(早算):bull/neutral → 评分 bull-only;只有 bear 市才允许 bear 方向。
    try:
        from . import spreads as _SP0
    except ImportError:
        import spreads as _SP0  # type: ignore
    try:
        _spy0 = hist.get("SPY")
        mkt = _SP0.market_regime(_spy0["Close"].to_numpy() if _spy0 is not None else None)
    except Exception as e:
        log(f"[ta_screener] market_regime 失败: {e}")
        mkt = {"regime": "neutral", "ret5": None, "down_days": None, "label": "大盘状态未知 → 默认"}
    allow_bear = (mkt.get("regime") == "bear")
    log(f"[ta_screener] regime={mkt.get('regime')} → {'允许 bear' if allow_bear else 'bull-only(不推荐 bear)'}")

    # 2) 引擎分析(55→85%)
    rows = []
    n_ok = len(hist)
    for i, (t, df) in enumerate(hist.items()):
        try:
            daily = D.to_arrays(df)
            weekly = D.resample_ohlc(df, "W-FRI")
            monthly = D.resample_ohlc(df, "ME")
            r = E.analyze_ticker(daily, weekly, monthly)
            if "error" in r:
                failed.append(t)
                continue
            r["ticker"] = t
            r["category"] = cat_map.get(t, "")
            r["sector"] = sec_map.get(t, "")
            r["industry"] = ind_map.get(t, "")
            r["spark"] = D.spark_series(df)
            rows.append(r)
        except Exception as e:
            failed.append(t)
            log(f"[ta_screener] {t} 分析失败: {e}")
        if i % 25 == 0:
            _set(pct=55 + int(i / max(1, n_ok) * 30), msg=f"技术分析 {i}/{n_ok}…")

    # 3) 外部信号(85→95%,fail-soft)—— finviz/barchart 已删(作者:没用),只留财报日
    fv, bc, bc_meta = {}, {}, {}
    _set(94, "财报日…")
    try:
        earn = X.fetch_earnings(universe, log=log)
    except Exception as e:
        log(f"[ta_screener] earnings 整体失败: {e}")
        earn = {}
    today_d = _dt.date.today()
    for r in rows:
        r["finviz"] = []
        r["barchart"] = []
        ei = earn.get(r["ticker"])
        if ei:
            try:
                days = (_dt.date.fromisoformat(ei["date"]) - today_d).days
            except Exception:
                days = None
            r["earnings"] = {**ei, "days": days}
        else:
            r["earnings"] = None

    # 4) 评分 + 排序(v4 化简:只价格信号+催化;bull 偏向由 allow_bear 控)
    _set(96, "评分…")
    for r in rows:
        sd = E.score_parts(r, allow_bear=allow_bear)
        r["score"] = sd["score"]
        r["score_detail"] = sd

    try:
        from . import spreads as SP
        from . import optmetrics as OM
    except ImportError:
        import spreads as SP  # type: ignore
        import optmetrics as OM  # type: ignore

    # 4.5) 期权结构指标(PCR/OI重心/MaxPain/GEX + OI/Vol 方向结构,近2月度)
    #      计分走 4.6 的 flow/pcr/gex 三块;opt_bias(±10)只展示不计分
    _set(96, "期权结构(OI/Vol·PCR·GEX)…")
    n_opt = 0
    try:
        client = None
        try:
            from optionstrat_server import state as _st0
            client = _st0.client
        except Exception:
            client = None
        n_opt = OM.attach_for_rows(rows, client, SP.row_direction, log=log)
    except Exception as e:
        log(f"[ta_screener] 期权结构整体失败(跳过): {e}")
    for r in rows:
        ob = r.get("opt_bias")
        if not ob or not ob.get("total"):
            continue
        d = SP.row_direction(r)
        if not d:
            continue
        applied = ob["total"] if d == "bull" else -ob["total"]
        r["opt_bias_applied"] = round(applied, 2)   # 仅记录;合成进 score 见 4.6
    # 异动期权流(Benzinga unusual options activity 近5日净多空)→ 按方向修正(封顶±5,佐证不主导)
    _set(96, "异动期权流(net 多空)…")
    n_oa = 0
    try:
        try:
            from . import oaflow as OA
        except ImportError:
            import oaflow as OA  # type: ignore
        oa_map = OA.oa_bias_map(days_back=5)
        for r in rows:
            oa = oa_map.get(r["ticker"])
            if not oa:
                continue
            r["oa_flow"] = oa
            n_oa += 1
            if oa.get("total"):
                d = SP.row_direction(r)
                if d:
                    applied = oa["total"] if d == "bull" else -oa["total"]
                    r["oa_applied"] = round(applied, 2)   # 仅记录;合成进 score 见 4.6
    except Exception as e:
        log(f"[ta_screener] 异动期权流整体失败(跳过): {e}")

    # 4.6) 期权三块(OI/Vol·PCR·GEX)v4 化简后【只算供展示、不计分】——
    #      实验(归档 10316 样本):对未来20日收益 spread≈±1%、ρ≈0 = 噪音(别人仓位不预测方向)。
    #      opt_metrics 仍在 detail 完整展示;score = core(已含)+ extra。components 三块归 0。
    for r in rows:
        sd = r.get("score_detail") or {}
        c = sd.setdefault("components", {})
        c["opt"] = 0.0
        c["pcr"] = 0.0
        c["gex"] = 0.0

    rows.sort(key=lambda r: (-r["score"],
                             -((r.get("touch") or {}).get("conf", 0)),
                             -((r.get("score_detail") or {}).get("core", 0.0))))

    # 5) 高分票 debit spread 推荐(TastyTrade 链;无 client/盘外无报价 → 自然跳过)
    #    复用 1.5) 早算的 mkt regime(同源,Bull 到期自适应:涨→短/跌→长)
    _set(97, "期权推荐(top 高分票)…")
    n_rec = 0
    try:
        client = None
        try:
            from optionstrat_server import state as _st   # 懒 import 防循环
            client = _st.client
        except Exception:
            client = None
        n_rec = SP.recommend_for_rows(
            rows, client,
            bull_dte=SP.BULL_DTE_BY_REGIME[mkt["regime"]],
            bull_alt=SP.BULL_ALT_BY_REGIME[mkt["regime"]],
            dte_reason=mkt["label"], log=log)
        log(f"[ta_screener] 大盘 regime={mkt['regime']} ({mkt['label']}) · spread 推荐: {n_rec} 票")
    except Exception as e:
        log(f"[ta_screener] spread 推荐整体失败(跳过): {e}")
    result = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_universe": full_n,
        "n_pool": len(tickers),
        "pool_active": bool(pool),
        "n_ok": len(rows),
        "failed": sorted(set(failed)),
        "finviz_date": _dt.date.today().isoformat() if fv else None,
        "barchart_meta": bc_meta,
        "n_earnings": sum(1 for r in rows if r.get("earnings")),
        "n_spread_rec": n_rec,
        "n_opt_metrics": n_opt,
        "market": mkt,
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RESULTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, default=_np_default), encoding="utf-8")
    os.replace(tmp, RESULTS_FILE)
    _set(100, f"完成:{len(rows)} 票", finished_at=result["generated_at"],
         generated_at=result["generated_at"])
    return result


def _thread_main():
    try:
        run_scan()
    except Exception as e:
        traceback.print_exc()
        _set(msg=f"扫描失败: {e}", error=str(e))
    finally:
        with _LOCK:
            STATE["running"] = False


def start_scan() -> dict:
    with _LOCK:
        if STATE["running"]:
            return {"ok": False, "msg": "已有扫描在跑"}
        STATE.update(running=True, pct=0, error=None,
                     started_at=_dt.datetime.now().strftime("%H:%M:%S"), finished_at=None)
    threading.Thread(target=_thread_main, daemon=True, name="ta_scan").start()
    return {"ok": True}


def load_results() -> dict:
    if not RESULTS_FILE.exists():
        return {"empty": True, "msg": "还没扫描过 — 点「跑扫描」"}
    try:
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"empty": True, "msg": f"结果文件损坏: {e}"}

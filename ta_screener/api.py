"""TA Screener FastAPI router(挂进 optionstrat_server:app.include_router)。

端点:
  GET  /ta/                  — iframe 页面(自包含 HTML)
  GET  /api/ta/results       — 最新扫描结果 JSON
  POST /api/ta/run           — 后台起全量扫描
  GET  /api/ta/status        — 扫描进度
  POST /api/ta/barchart_pull — 按需跑 barchart saved screeners(子进程)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

try:  # 包内相对导入(server 走这);直跑脚本时退回同目录导入
    from . import external, page, scanner, spreads, backtest
except ImportError:
    import external, page, scanner, spreads, backtest  # type: ignore

import datetime as _dt
import json as _json
import math as _math
import subprocess as _subprocess
import threading as _threading
import time as _time
from pathlib import Path as _Path

router = APIRouter()


def _json_safe(obj):
    """递归把 NaN/Inf → None。

    starlette JSONResponse 用 allow_nan=False 序列化,遇到 NaN/Inf 会抛
    ValueError → HTTP 500。回测结果里可能残留 NaN(如 trades[].debit),返回前必须清洗。
    """
    if isinstance(obj, float):
        return obj if _math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj

# --- EOD「现在盘后爬取」: 起子进程跑 crawl_server.py --once,累积 EOD 期权链历史库 ---
_EOD_DIR = _Path(__file__).resolve().parent.parent / "eod_crawler"
_EOD_PROGRESS_FILE = _EOD_DIR / "data_eod" / "CRAWL_PROGRESS.json"
# 和 launchd / run_eod.sh 同一个 system python,保证依赖与定时任务完全一致
_EOD_PY = "/usr/bin/python3"

# 本进程内的爬取任务状态(单一写者 = _eod_crawl_job 线程)
EOD_CRAWL: dict = {"running": False, "phase": None, "pct": 0, "msg": "",
                   "error": None, "done": None, "total": None, "current": None}


def _read_eod_progress() -> dict | None:
    """读 crawl_server 写的实时进度 JSON(零网络,fail-soft)。"""
    try:
        return _json.loads(_EOD_PROGRESS_FILE.read_text())
    except Exception:
        return None


def _eod_progress_fresh(prog: dict, secs: int = 120) -> bool:
    """进度文件是否新鲜(用于判断「是否有爬虫正在跑」,含 launchd 起的)。"""
    try:
        t = _dt.datetime.fromisoformat(prog.get("updated_at"))
        return (_dt.datetime.now() - t).total_seconds() < secs
    except Exception:
        return False


def _eod_crawl_job() -> None:
    """后台线程:起 crawl_server.py --once 子进程 → 读进度文件喂进度条。
    只爬取(累积 EOD 期权链历史库),不再接 paper 管线。"""
    try:
        EOD_CRAWL.update(running=True, phase="crawl", pct=0, msg="启动爬取…",
                         error=None, done=None, total=None, current=None)
        py = _EOD_PY if _Path(_EOD_PY).exists() else _sys_executable()
        proc = _subprocess.Popen([py, "crawl_server.py", "--once"], cwd=str(_EOD_DIR),
                                 stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL)
        # 子进程跑期间轮询进度文件,把爬取映射到进度条 0–100%
        while proc.poll() is None:
            prog = _read_eod_progress()
            if prog and prog.get("phase") == "crawl":
                done, total = prog.get("done") or 0, prog.get("total") or 0
                EOD_CRAWL.update(phase="crawl", pct=int(prog.get("pct") or 0),
                                 done=done, total=total, current=prog.get("current"),
                                 msg=f"爬取中 {done}/{total} · {prog.get('current') or '…'}")
            _time.sleep(2)
        rc = proc.returncode
        prog = _read_eod_progress() or {}
        if rc != 0:
            err = prog.get("error") or f"爬取进程退出 rc={rc}"
            EOD_CRAWL.update(running=False, phase="error", error=err, msg=f"爬取失败: {err}")
            return
        # rc==0:真正爬完 或 今日已爬/休市跳过
        EOD_CRAWL.update(running=False, phase="done", pct=100, msg="完成")
    except Exception as e:  # noqa: BLE001 — 后台线程兜底,绝不抛
        EOD_CRAWL.update(running=False, phase="error", error=str(e), msg=f"失败: {e}")


def _sys_executable() -> str:
    import sys as _s
    return _s.executable


def _digest_news(ticker: str, days: int = 6, limit: int = 30) -> list:
    """从 benzinga digest 取该 ticker 近 days 天的新闻(fail-soft,本地读、零网络)。"""
    try:
        import sys as _sys
        p = str(_Path(__file__).resolve().parent.parent / "benzinga_digest" / "src")
        if p not in _sys.path:
            _sys.path.insert(0, p)
        from benzinga_mcp import digest_store as _ds
        from benzinga_mcp.client import filter_by_ticker as _fbt
    except Exception:
        return []
    out = []
    try:
        for ds in _ds.list_days()[:days]:
            arts = _ds.load_day(ds) or []
            for a in _fbt(arts, ticker):
                out.append({
                    "date": ds, "hhmm": a.get("hhmm"),
                    "title": a.get("title") or a.get("title_en") or "",
                    "title_en": a.get("title_en") or "",
                    "teaser": a.get("teaser") or "",
                    "sentiment": a.get("sentiment"),
                    "tickers": a.get("tickers") or [],
                    "channels": a.get("channels") or [],
                    "link": (a.get("links") or [None])[0],
                    "paras": [(pp.get("cn") or pp.get("en") or "") for pp in (a.get("paras") or [])][:3],
                })
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


@router.get("/ta/")
def ta_page():
    return HTMLResponse(page.PAGE_HTML)


@router.get("/api/ta/results")
def ta_results():
    """列表视图:剥掉 spark/zones(占 ~80% 体积),详情按需 /api/ta/detail 取。"""
    d = scanner.load_results()
    rows = d.get("rows")
    if rows:
        slim = [{k: v for k, v in r.items() if k not in ("spark", "zones")} for r in rows]
        d = {**d, "rows": slim, "slim": True}
    return JSONResponse(d)


@router.get("/api/ta/detail/{ticker}")
def ta_detail(ticker: str):
    """单票详情重数据(180日 spark + zones),展开行时按需拉。"""
    d = scanner.load_results()
    tk = ticker.upper()
    for r in (d.get("rows") or []):
        if r.get("ticker") == tk:
            return JSONResponse({"ticker": tk, "spark": r.get("spark"), "zones": r.get("zones") or []})
    return JSONResponse({"error": "not found", "ticker": tk})


@router.get("/api/ta/spread/{ticker}")
def ta_spread(ticker: str):
    """单票按需 debit spread 推荐(展开行时,批量没算到的也能现拉)。"""
    d = scanner.load_results()
    tk = ticker.upper()
    row = next((r for r in (d.get("rows") or []) if r.get("ticker") == tk), None)
    if not row:
        return JSONResponse({"error": "not found"})
    if row.get("spread_rec"):
        return JSONResponse({"spread_rec": row["spread_rec"]})
    client = None
    try:
        from optionstrat_server import state as _st
        client = _st.client
    except Exception:
        client = None
    rec = spreads.recommend_one(row, client, d.get("market"))
    return JSONResponse({"spread_rec": rec} if rec
                        else {"error": "无可用期权链/报价(盘外或流动性不足)", "dir": spreads.row_direction(row)})


@router.post("/api/ta/run")
def ta_run():
    return JSONResponse(scanner.start_scan())


@router.get("/api/ta/status")
def ta_status():
    return JSONResponse(dict(scanner.STATE))


_OHLC_MEMO = {"mtime": 0.0, "data": {}}
# On-demand fetch cache for tickers OUTSIDE the TA pool parquet (e.g. heatmap
# 📈 Graph view 的大盘股不在 391 票 TA universe 里)。yfinance 拉到后驻留 + 落盘,
# 重启不丢;LRU-ish 上限防无界增长。预热(warm_heatmap_ohlc)批量灌进来避免渲染时并发单拉被限流。
_OHLC_FETCH_MEMO: dict = {}
_OHLC_FETCH_CAP = 800


def _hm_ohlc_file(D):
    """heatmap 大盘股 OHLC 落盘缓存(独立于 TA 池 parquet,不被 TA scanner 覆盖)。"""
    return D.CACHE_FILE.parent / "heatmap_ohlc_cache.parquet"


def _load_persisted_fetch(D) -> None:
    """启动时把上次落盘的 fetch 缓存读回 memo(重启不必重拉)。"""
    if _OHLC_FETCH_MEMO:
        return
    try:
        import pandas as pd
        f = _hm_ohlc_file(D)
        if not f.exists():
            return
        df = pd.read_parquet(f)
        for t, sub in df.groupby("ticker"):
            _OHLC_FETCH_MEMO[str(t)] = sub.drop(columns=["ticker"]).sort_index()
        print(f"[hmOHLC] loaded {len(_OHLC_FETCH_MEMO)} persisted大盘股 OHLC")
    except Exception as e:
        print(f"[hmOHLC] load err: {e}")


def _persist_fetch(D) -> None:
    """memo → 落盘(原子替换)。"""
    try:
        import pandas as pd
        if not _OHLC_FETCH_MEMO:
            return
        frames = []
        for t, df in list(_OHLC_FETCH_MEMO.items()):
            d = df.copy()
            d["ticker"] = t
            frames.append(d)
        out = pd.concat(frames)
        f = _hm_ohlc_file(D)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        out.to_parquet(tmp)
        tmp.replace(f)
    except Exception as e:
        print(f"[hmOHLC] persist err: {e}")


def warm_heatmap_ohlc(min_mcap: float = 100e9) -> dict:
    """**预热**:把 heatmap 大盘股(≥min_mcap、且不在 TA 池缓存里的)用 TA 数据层的分块
    下载(自带限流重试/退避)一次性灌进 fetch-memo + 落盘。这样 Graph 渲染时全是缓存命中,
    不会再因并发单拉被 Yahoo throttle 出现「无数据」。阻塞,放后台线程跑。"""
    try:
        from . import data as D
    except ImportError:
        import data as D  # type: ignore
    info = {"want": 0, "missing": 0, "got": 0}
    try:
        import sys
        import pathlib
        repo = pathlib.Path(__file__).resolve().parents[1]
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from heatmap import metadata as M
        _load_persisted_fetch(D)
        ta_cache = set(D._load_cache().keys())
        try:
            from heatmap import daily_store as _DS
            store_cov = set(_DS.load().keys())   # authoritative DXLink store
        except Exception:
            store_cov = set()
        meta = M.get_all() or {}
        want = [t for t, m in meta.items() if (m.get("market_cap") or 0) >= min_mcap]
        # store covers the chart endpoint's primary read → don't yfinance-warm those
        missing = [t for t in want if t not in ta_cache and t not in _OHLC_FETCH_MEMO
                   and t not in store_cov]
        info["want"], info["missing"] = len(want), len(missing)
        if not missing:
            print(f"[hmOHLC] warm: {len(want)} 大盘股已全覆盖, 无需拉取")
            return info
        print(f"[hmOHLC] warm: 批量下载 {len(missing)} 只缺失大盘股…")
        got = D._download_full(missing, min_bars=30)
        for t, df in got.items():
            _OHLC_FETCH_MEMO[t] = df
        info["got"] = len(got)
        _persist_fetch(D)
        print(f"[hmOHLC] warm: +{len(got)} 已缓存 (memo={len(_OHLC_FETCH_MEMO)})")
    except Exception as e:
        print(f"[hmOHLC] warm err: {e}")
    return info


def _ohlc_fetch_one(D, tk: str):
    """TA 缓存未命中时按需拉单票(heatmap 大盘股)。失败回 None,不抛。"""
    _load_persisted_fetch(D)   # 首次命中前把落盘缓存读回
    df = _OHLC_FETCH_MEMO.get(tk)
    if df is not None and len(df) >= 2:
        return df
    try:
        import yfinance as yf
        tmp: dict = {}
        D._fetch_batch(yf, [tk], tmp, D.PERIOD, min_bars=2)
        df = tmp.get(tk)
    except Exception:
        df = None
    if df is not None and len(df) >= 2:
        if len(_OHLC_FETCH_MEMO) >= _OHLC_FETCH_CAP:
            try:
                _OHLC_FETCH_MEMO.pop(next(iter(_OHLC_FETCH_MEMO)))
            except StopIteration:
                pass
        _OHLC_FETCH_MEMO[tk] = df
    return df


@router.get("/api/ta/ohlc/{ticker}")
def ta_ohlc(ticker: str, days: int = 126):
    """📊 图视图自绘 candle:最近 days 根 OHLC+量+SMA50/200 + S/R zones(读本地缓存,memoize by mtime)。
    days:63≈3月 / 126≈6月 / 252≈12月 / 504≈2年。
    TA 池(391 票)读 parquet 缓存;不在池里的大盘股(heatmap Graph 视图)按需 yfinance 拉一次驻留。"""
    try:
        from . import data as D, engine as E
    except ImportError:
        import data as D, engine as E  # type: ignore
    import os
    try:
        tk = ticker.strip().upper().replace(".", "-")
        # 1) 权威本地日线库(DXLink Candle;覆盖 heatmap universe、含最近交易日、不滞后)。
        #    是 heatmap 涨跌%/Graph 图墙的同一份数据,图表与色块一致、与 TradingView 口径一致。
        df = None
        try:
            from heatmap import daily_store as _DS
            df = _DS.get_ohlc(tk)
        except Exception:
            df = None
        # 2) 退回 TA 池 parquet 缓存(评分引擎复权口径) 3) 池外按需拉
        #    只在新源数据更多时替换(新上市票可能仅几根 bar,别被覆盖成 None)
        if df is None or len(df) < 30:
            mt = os.path.getmtime(D.CACHE_FILE) if D.CACHE_FILE.exists() else 0.0
            if mt != _OHLC_MEMO["mtime"]:
                _OHLC_MEMO["data"] = D._load_cache()
                _OHLC_MEMO["mtime"] = mt
            alt = _OHLC_MEMO["data"].get(tk)
            if alt is not None and (df is None or len(alt) > len(df)):
                df = alt
        if df is None or len(df) < 30:
            alt = _ohlc_fetch_one(D, tk)
            if alt is not None and (df is None or len(alt) > len(df)):
                df = alt
        if df is None or len(df) < 2:
            return JSONResponse({"error": "no data", "ticker": ticker})
        n = max(20, min(520, int(days)))   # 20≈1月 ... 504≈2年(SMA 仍用全历史算后再切窗)
        out = D.ohlc_chart(df, n)
        # S/R zones(复用引擎 pivots + cluster;失败不影响图)
        try:
            dly = D.to_arrays(df)
            wk = D.resample_ohlc(df, "W-FRI")
            mo = D.resample_ohlc(df, "ME")
            p = E.PARAMS
            piv = {"D": E.pivots(dly["high"], dly["low"], p["piv_len_d"], p["max_pivot"]),
                   "W": E.pivots(wk["high"], wk["low"], p["piv_len_w"], p["max_pivot"]),
                   "M": E.pivots(mo["high"], mo["low"], p["piv_len_m"], p["max_pivot"])}
            zs = E.cluster_zones(piv, p["bin_pct"], p["min_conf_show"], p["max_zones"])
            out["zones"] = [{"lo": round(float(z["lo"]), 2), "hi": round(float(z["hi"]), 2),
                             "avg": round(float(z["avg"]), 2), "conf": int(z["conf"]),
                             "tfs": z.get("tfs", "")} for z in zs]
        except Exception:
            out["zones"] = []
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": str(e), "ticker": ticker})


@router.get("/api/ta/backtest")
def ta_backtest():
    return JSONResponse(_json_safe(backtest.load_results()))


@router.get("/api/ta/backtest/status")
def ta_backtest_status():
    return JSONResponse(dict(backtest.STATE))


@router.post("/api/ta/backtest/run")
def ta_backtest_run(threshold: float = 70.0, top_n: int = 1):
    if backtest.STATE.get("running"):
        return JSONResponse({"ok": False, "msg": "回测已在跑"})
    backtest.STATE.update(running=True, pct=0, error=None)

    def _job():
        try:
            backtest.run_backtest(threshold=threshold, top_n=top_n)
        except Exception as e:
            backtest.STATE.update(error=str(e), msg=f"失败: {e}")
        finally:
            backtest.STATE["running"] = False
    _threading.Thread(target=_job, daemon=True, name="ta_backtest").start()
    return JSONResponse({"ok": True})


def _eod_status() -> dict:
    """EOD 期权链爬虫状态(只读文件、零网络、不触发爬取):最新已爬日 + 摘要 +
    今日是否该爬(ET 交易日) + 数据文件新鲜度。供 PWA 顶部状态条展示。"""
    import datetime as _dt
    eod_dir = _Path(__file__).resolve().parent.parent / "eod_crawler" / "data_eod"
    last_file = eod_dir / "LAST_CRAWLED.txt"
    parquet = eod_dir / "eod_chain.parquet"
    try:
        before_close = False
        try:
            from zoneinfo import ZoneInfo
            now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
            today = now_et.date()
            # 收盘前防呆:交易日 & 现在 < 16:00 ET(= 德州 15:00 CT)→ 前端弹窗确认
            before_close = now_et.hour < 16
        except Exception:
            today = _dt.date.today()
        # 交易日判定:复用 eod_crawler/market_calendar(非包,按文件加载)
        is_td = today.weekday() < 5
        try:
            import importlib.util as _il
            mc_path = eod_dir.parent / "market_calendar.py"
            spec = _il.spec_from_file_location("_eod_mktcal", mc_path)
            mc = _il.module_from_spec(spec)
            spec.loader.exec_module(mc)
            is_td = bool(mc.is_trading_day(today))
        except Exception:
            pass
        latest, summary = None, []
        if last_file.exists():
            lines = [ln.rstrip() for ln in last_file.read_text().splitlines()]
            latest = lines[0].strip() if lines else None
            summary = [ln.strip() for ln in lines[1:] if ln.strip()]
        mtime = None
        if parquet.exists():
            mtime = _dt.datetime.fromtimestamp(parquet.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        today_str = today.isoformat()
        today_done = latest == today_str
        if not is_td:
            state, label = "closed", f"今日休市 · 最新已爬 {latest or '—'}"
        elif today_done:
            state, label = "done", f"今日 {today_str} EOD 已爬"
        else:
            state, label = "pending", f"今日 {today_str} 待爬(收盘后自动)· 最新 {latest or '—'}"
        return {"ok": True, "state": state, "label": label, "latest_date": latest,
                "today": today_str, "is_trading_day": is_td, "today_done": today_done,
                "before_close": bool(is_td and before_close),
                "summary": summary, "file_mtime": mtime}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/ta/eod_status")
def ta_eod_status():
    return JSONResponse(_eod_status())


@router.get("/api/ta/ticker/{sym}")
def ta_ticker(sym: str):
    """单票详情聚合:TA 评分/信号/spread/spark/zones + digest 新闻。
    (finviz 技术/基本面由前端另拉 /api/finviz/{sym},因含网络抓取较慢。)"""
    tk = sym.upper()
    d = scanner.load_results()
    row = next((r for r in (d.get("rows") or []) if r.get("ticker") == tk), None)
    # 若批量没算 spread,按需补一发(展开行同款逻辑)
    if row and not row.get("spread_rec"):
        client = None
        try:
            from optionstrat_server import state as _st
            client = _st.client
        except Exception:
            client = None
        try:
            rec = spreads.recommend_one(row, client, d.get("market"))
            if rec:
                row = {**row, "spread_rec": rec}
        except Exception:
            pass
    news = _digest_news(tk)
    return JSONResponse({
        "ticker": tk, "found": row is not None, "row": row,
        "market": d.get("market"), "generated_at": d.get("generated_at"),
        "news": news,
    })


@router.post("/api/ta/eod_crawl/run")
def ta_eod_crawl_run():
    """「现在盘后爬取」:起子进程爬今天的 EOD 链(累积历史库,后台线程)。
    已有爬取在跑(本按钮起的 或 定时任务起的)→ 不再起第二个,直接让前端挂进度。"""
    if EOD_CRAWL.get("running"):
        return JSONResponse({"ok": True, "attached": True, "msg": "已有爬取在跑"})
    prog = _read_eod_progress()
    if prog and prog.get("running") and _eod_progress_fresh(prog):
        return JSONResponse({"ok": True, "attached": True,
                             "msg": "已有爬取在跑(可能是定时任务),已挂上进度"})
    _threading.Thread(target=_eod_crawl_job, daemon=True, name="ta_eod_crawl").start()
    return JSONResponse({"ok": True})


@router.get("/api/ta/eod_crawl/status")
def ta_eod_crawl_status():
    """进度条数据源:优先本进程任务状态;若本进程没在跑但进度文件显示有爬虫
    正在跑(launchd 起的),合成一个 running 视图挂上去。"""
    out = dict(EOD_CRAWL)
    if not out.get("running"):
        prog = _read_eod_progress()
        if prog and prog.get("running") and _eod_progress_fresh(prog):
            done, total = prog.get("done") or 0, prog.get("total") or 0
            out = {"running": True, "phase": "crawl", "external": True,
                   "pct": int(prog.get("pct") or 0),
                   "done": done, "total": total, "current": prog.get("current"),
                   "msg": f"爬取中(定时任务) {done}/{total} · {prog.get('current') or '…'}"}
    return JSONResponse(out)

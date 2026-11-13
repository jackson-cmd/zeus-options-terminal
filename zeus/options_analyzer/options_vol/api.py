"""options_vol FastAPI router(挂进 optionstrat_server:app.include_router)。

端点:
  GET  /optvol/                  — iframe 页面(自包含 HTML,page.render_page())
  GET  /api/optvol/summary       — data/options_vol/summary.json(缺失→空壳,不 404 崩)
  GET  /api/optvol/history/{T}   — 该票每日序列(画图用,升序 by date)
  GET  /api/optvol/status        — 最近爬取时间 + 计数(scrape_meta.json / summary.json)
  POST /api/optvol/scrape        — 后台触发 run_optvol.sh 重爬(best-effort,非阻塞)
  GET  /api/optvol/eod_pc        — EOD 派生逐票 P/C volume/OI/加权IV(缺失→空壳)
  POST /api/optvol/eod_pc/refresh— 后台重算 eod_pc.json(eod_pc.build,非阻塞)

数据目录可被环境变量 OPTVOL_DATA_DIR 覆盖(测试时指向 fixture)。
仿 ta_screener/api.py 的包内相对导入 + 子进程触发 + parquet memo-by-mtime 模式。
"""
from __future__ import annotations

import json as _json
import os as _os
import subprocess as _subprocess
from pathlib import Path as _Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

try:  # 包内相对导入(server 走这);直跑/测试时退回同目录导入
    from . import page
except ImportError:  # pragma: no cover - 仅直跑脚本时命中
    import page  # type: ignore

router = APIRouter()

# ── 路径解析(全部相对本文件;DATA_DIR 可被 env 覆盖以便测试指向 fixture)──
_PKG_DIR = _Path(__file__).resolve().parent              # options_analyzer/options_vol
_REPO_ROOT = _PKG_DIR.parent.parent                       # 仓库根
_DEFAULT_DATA_DIR = _REPO_ROOT / "data" / "options_vol"


def _data_dir() -> _Path:
    """每次现读 env(测试里可在 import 后再设 OPTVOL_DATA_DIR)。"""
    return _Path(_os.environ.get("OPTVOL_DATA_DIR", str(_DEFAULT_DATA_DIR)))


def _summary_path() -> _Path:
    return _data_dir() / "summary.json"


def _meta_path() -> _Path:
    return _data_dir() / "scrape_meta.json"


def _history_path() -> _Path:
    return _data_dir() / "history.parquet"


def _run_script() -> _Path:
    return _PKG_DIR / "run_optvol.sh"


def _eod_pc_path() -> _Path:
    return _data_dir() / "eod_pc.json"


# ── summary.json 读取(缺失/损坏 → 空壳,绝不 500)──
def _load_summary() -> dict:
    p = _summary_path()
    if not p.exists():
        return {"generated_at": None, "tickers": [], "status": "未爬取"}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 文件损坏也回空壳
        return {"generated_at": None, "tickers": [], "status": "解析失败"}


# ── eod_pc.json 读取(EOD 派生 P/C;缺失/损坏 → 空壳,绝不 500)──
def _empty_eod_pc() -> dict:
    return {"generated_at": None, "data_date": None, "n_tickers": 0, "tickers": {}}


def _load_eod_pc() -> dict:
    p = _eod_pc_path()
    if not p.exists():
        return _empty_eod_pc()
    try:
        d = _json.loads(p.read_text(encoding="utf-8"))
        # 容错:确保关键键存在,缺则补空壳形状
        if not isinstance(d, dict) or "tickers" not in d:
            return _empty_eod_pc()
        return d
    except Exception:  # noqa: BLE001 — 文件损坏也回空壳
        return _empty_eod_pc()


# ── history.parquet memo:按 (路径, mtime) 缓存,避免每次点击全量重读 ──
# 单写者足够(GIL 下 dict 赋值原子);形如 {"key": (path,mtime), "by_ticker": {T: [rows..]}}
_HIST_MEMO: dict = {"key": None, "by_ticker": {}}


def _load_history_by_ticker() -> dict:
    """读 history.parquet → {ticker: [ {date,vol,oi,iv} 升序 ] }。
    按 mtime memoize:同一份文件多次点击只解析一次。缺失/读失败 → 空 dict。"""
    p = _history_path()
    if not p.exists():
        _HIST_MEMO["key"] = None
        _HIST_MEMO["by_ticker"] = {}
        return _HIST_MEMO["by_ticker"]
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    key = (str(p), mtime)
    if _HIST_MEMO.get("key") == key:
        return _HIST_MEMO["by_ticker"]
    by_ticker: dict = {}
    try:
        import pandas as pd  # 延迟导入:无 pandas 环境也能起 server(只是图为空)
        df = pd.read_parquet(p)
        if len(df):
            # 契约列:totalVolume/totalOpenInterest(int)、weightedImpliedVolatility(float)
            for tk, sub in df.groupby("ticker"):
                sub = sub.sort_values("date")  # 升序 by date
                series = []
                for _, r in sub.iterrows():
                    series.append({
                        "date": str(r.get("date")),
                        "vol": _to_int(r.get("totalVolume")),
                        "oi": _to_int(r.get("totalOpenInterest")),
                        "iv": _to_float(r.get("weightedImpliedVolatility")),
                    })
                by_ticker[str(tk)] = series
    except Exception:  # noqa: BLE001 — parquet 读失败 → 视为无图数据
        by_ticker = {}
    _HIST_MEMO["key"] = key
    _HIST_MEMO["by_ticker"] = by_ticker
    return by_ticker


def _to_int(v):
    try:
        import math
        if v is None:
            return None
        f = float(v)
        if math.isnan(f):
            return None
        return int(round(f))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        import math
        if v is None:
            return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


# ── 路由 ──
@router.get("/optvol/")
def optvol_page():
    """iframe 目标页(自包含 HTML/CSS/JS,无 CDN 依赖)。"""
    return HTMLResponse(page.render_page())


@router.get("/api/optvol/summary")
def optvol_summary():
    """每票 tier + 展示指标。缺 summary.json → {"generated_at":null,"tickers":[],"status":"未爬取"}。"""
    return JSONResponse(_load_summary())


@router.get("/api/optvol/history/{ticker}")
def optvol_history(ticker: str):
    """单票每日序列(画 vol/OI/IV 线段图用),升序 by date。无数据 → series 空。"""
    tk = ticker.strip().upper()
    by_ticker = _load_history_by_ticker()
    series = by_ticker.get(tk, [])
    return JSONResponse({"ticker": tk, "series": series})


@router.get("/api/optvol/status")
def optvol_status():
    """顶部状态条:最近爬取时间 + universe/成功/失败计数。
    优先 scrape_meta.json,缺则回退 summary.json,都没有 → 空壳。"""
    meta_p = _meta_path()
    if meta_p.exists():
        try:
            m = _json.loads(meta_p.read_text(encoding="utf-8"))
            return JSONResponse({
                "generated_at": m.get("generated_at"),
                "universe_size": m.get("universe_size"),
                "n_scraped": m.get("n_scraped"),
                "n_failed": m.get("n_failed"),
            })
        except Exception:  # noqa: BLE001
            pass
    s = _load_summary()
    return JSONResponse({
        "generated_at": s.get("generated_at"),
        "universe_size": s.get("universe_size"),
        "n_scraped": s.get("n_scraped"),
        "n_failed": s.get("n_failed"),
    })


def _kick_scrape() -> dict:
    """后台起 run_optvol.sh(非阻塞);脚本不存在 → 不报错,回 {"started":false,"note":"manual"}。"""
    script = _run_script()
    if not script.exists():
        return {"started": False, "note": "manual"}
    try:
        # detach:重定向到 /dev/null,不等待(爬取耗时数分钟)
        _subprocess.Popen(
            ["/bin/bash", str(script)],
            cwd=str(_PKG_DIR.parent),  # options_analyzer/(供 `python -m options_vol.*`)
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"started": True}
    except Exception as e:  # noqa: BLE001 — best-effort,失败也不 500
        return {"started": False, "note": f"launch failed: {e}"}


@router.post("/api/optvol/scrape")
def optvol_scrape():
    """「⚡ 现在重爬」:后台触发 run_optvol.sh,立即返回(耗时数分钟,前端轮询 /status)。
    Popen detach 本身就是非阻塞的;脚本不存在则回 {"started":false,"note":"manual"}。"""
    return JSONResponse(_kick_scrape())


@router.get("/api/optvol/eod_pc")
def optvol_eod_pc():
    """EOD 派生逐票 P/C(volume/OI ratio + 量加权 IV + 最近~20日小历史)。
    读 data/options_vol/eod_pc.json。缺失/损坏 → 空壳 {"tickers":{},...},绝不 500。"""
    return JSONResponse(_load_eod_pc())


def _kick_eod_pc_refresh() -> dict:
    """后台起 `python -m options_vol.eod_pc` 重算 eod_pc.json(非阻塞,detach)。
    读 ~560MB parquet 数十秒,故不在 event loop 里同步跑。失败也不 500。"""
    import sys as _sys

    try:
        _subprocess.Popen(
            [_sys.executable, "-m", "options_vol.eod_pc"],
            cwd=str(_PKG_DIR.parent),  # options_analyzer/(供 `python -m options_vol.eod_pc`)
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"started": True}
    except Exception as e:  # noqa: BLE001 — best-effort,失败也不 500
        return {"started": False, "note": f"launch failed: {e}"}


@router.post("/api/optvol/eod_pc/refresh")
def optvol_eod_pc_refresh():
    """后台触发 eod_pc 重算(读 EOD 链聚合,耗时数十秒),立即返回。
    前端可随后轮询 GET /api/optvol/eod_pc 的 generated_at 看是否更新。"""
    return JSONResponse(_kick_eod_pc_refresh())


# 直跑自检:打印解析到的路径,方便调试(不触发任何网络)
if __name__ == "__main__":  # pragma: no cover
    print("OPTVOL_DATA_DIR =", _data_dir())
    print("summary exists  =", _summary_path().exists())
    print("history exists  =", _history_path().exists())
    print("eod_pc exists   =", _eod_pc_path().exists())
    print("run script      =", _run_script(), _run_script().exists())

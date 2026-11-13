"""外部信号(全部 fail-soft,挂了只少一列,绝不阻塞扫描):

1. Finviz pattern:pip 包 finviz.Screener 按 signal 拉名单,与 universe 求交。
   结果按天缓存到 data/ta_screener/finviz_cache.json(每个交易日抓一次)。
2. Barchart saved screener:读 barchart_mcp/downloads/screeners/*.csv(作者用
   MCP/screener.py 跑出来的),取每个 screener 最新一份的 Symbol 列做标记。
   另提供 trigger_barchart_pull() 起子进程跑他们的 screener.py(按需,不默认)。
"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "ta_screener"
BARCHART_DIR = Path(__file__).resolve().parent.parent / "barchart"
BARCHART_DL = BARCHART_DIR / "downloads" / "screeners"

# finviz 信号名(pip 包 Screener(signal=...) 的合法值)→ 短中文标签
FINVIZ_SIGNALS = {
    "Double Bottom": "双底",
    "Double Top": "双顶",
    "Wedge Up": "上升楔形",
    "Wedge Down": "下降楔形",
    "Channel Up": "上升通道",
    "Channel Down": "下降通道",
    "TL Support": "趋势线支撑",
    "TL Resistance": "趋势线阻力",
}
FINVIZ_ROWS_PER_SIGNAL = 100   # 每信号最多拉 100 行(5 页),控请求量


def _today() -> str:
    return _dt.date.today().isoformat()


def finviz_patterns(universe: set, force: bool = False, log=print) -> dict:
    """{ticker: [中文 pattern, ...]}(只含 universe 内的)。当日缓存。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = CACHE_DIR / "finviz_cache.json"
    if not force and cache_f.exists():
        try:
            c = json.loads(cache_f.read_text())
            if c.get("date") == _today():
                return {t: v for t, v in c.get("hits", {}).items() if t in universe}
        except Exception:
            pass
    hits: dict[str, list] = {}
    try:
        from finviz.screener import Screener
    except Exception as e:
        log(f"[ta_screener] finviz 包不可用: {e}")
        return {}
    for sig, label in FINVIZ_SIGNALS.items():
        try:
            scr = Screener(signal=sig, table="Overview", rows=FINVIZ_ROWS_PER_SIGNAL)
            for row in scr.data:
                t = str(row.get("Ticker", "")).upper().replace(".", "-")
                # 缓存存全量命中(不按 universe 过滤!),返回时才过滤 ——
                # 否则小 universe 的一次调用会把当日缓存污染成只剩它那几票。
                hits.setdefault(t, []).append(label)
            log(f"[ta_screener] finviz '{sig}': {len(scr.data)} 行")
        except Exception as e:
            log(f"[ta_screener] finviz '{sig}' 失败(跳过): {e}")
            continue
    try:
        cache_f.write_text(json.dumps({"date": _today(), "hits": hits}, ensure_ascii=False))
    except Exception:
        pass
    return {t: v for t, v in hits.items() if t in universe}


def barchart_csv_signals(universe: set, log=print) -> tuple[dict, dict]:
    """读 barchart saved-screener CSV → ({ticker: [screener名,...]}, meta)。
    每个 screener(按文件名前缀去重)取 mtime 最新一份。没有目录/文件 → 空。
    中性 screener(engine.BARCHART_DROP)整批丢弃 —— 不抓不显不计分(作者口径)。"""
    try:
        from . import engine as _E
    except ImportError:
        import engine as _E  # type: ignore
    hits: dict[str, list] = {}
    meta = {"files": 0, "latest": None}
    if not BARCHART_DL.is_dir():
        return hits, meta
    latest_per_name: dict[str, Path] = {}
    for f in BARCHART_DL.glob("*.csv"):
        # 文件名形如 <screener_name>_YYYYMMDD[_HHMM].csv;前缀做 key
        stem = f.stem
        name = stem.rsplit("_", 1)[0] if stem.rsplit("_", 1)[-1].isdigit() else stem
        if name in _E.BARCHART_DROP:        # 中性 screener:丢弃
            continue
        cur = latest_per_name.get(name)
        if cur is None or f.stat().st_mtime > cur.stat().st_mtime:
            latest_per_name[name] = f
    import csv as _csv
    newest = 0.0
    for name, f in latest_per_name.items():
        try:
            with open(f, newline="", encoding="utf-8-sig") as fh:
                rd = _csv.DictReader(fh)
                sym_col = None
                for c in (rd.fieldnames or []):
                    if c.strip().lower() in ("symbol", "ticker", "symbol "):
                        sym_col = c
                        break
                if not sym_col:
                    continue
                for row in rd:
                    t = str(row.get(sym_col, "")).strip().upper().replace(".", "-")
                    if t in universe:
                        hits.setdefault(t, []).append(name)
            meta["files"] += 1
            newest = max(newest, f.stat().st_mtime)
        except Exception as e:
            log(f"[ta_screener] barchart CSV {f.name} 读取失败: {e}")
    if newest:
        meta["latest"] = _dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
    return hits, meta


EARNINGS_TTL_DAYS = 7   # 财报日提前数周公布,变化慢 → 一周一刷(作者口径)


def fetch_earnings(universe: set, force: bool = False, log=print) -> dict:
    """{ticker: {date, time}}(TastyTrade market-metrics 批量,**7 天 TTL 缓存**——
    财报日变化慢,不必每天拉;依赖 trading_assistant/ticker_metrics,失败 → 空 dict)。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = CACHE_DIR / "earnings_cache.json"
    if not force and cache_f.exists():
        try:
            c = json.loads(cache_f.read_text())
            age = (_dt.date.today() - _dt.date.fromisoformat(c.get("date", "1970-01-01"))).days
            # 空 map 视为缓存缺失(防把一次失败缓存成一周空白)
            if 0 <= age < EARNINGS_TTL_DAYS and c.get("map"):
                return {t: v for t, v in c.get("map", {}).items() if t in universe}
        except Exception:
            pass
    try:
        ta_dir = REPO_ROOT / "trading_assistant"
        if str(ta_dir) not in sys.path:
            sys.path.insert(0, str(ta_dir))
        from ticker_metrics import fetch_ticker_metrics
        # ⚠️ 必须分块:全 universe(699)塞一个 GET 会 HTTP 414(URI 过长),
        # 而 fetch_ticker_metrics 绝不抛 → 静默返回空。150/批 ≈ 5 次调用。
        out = {}
        syms = sorted(universe)
        for i in range(0, len(syms), 150):
            m = fetch_ticker_metrics(syms[i:i + 150])
            for t, d in (m or {}).items():
                ed = (d or {}).get("earnings_date")
                if ed:
                    out[t.upper().replace("/", "-")] = {"date": str(ed)[:10],
                                                        "time": (d or {}).get("earnings_time") or ""}
        if out:   # 空结果不落缓存,下次扫描重试
            cache_f.write_text(json.dumps({"date": _today(), "map": out}, ensure_ascii=False))
        log(f"[ta_screener] earnings: {len(out)} 票有财报日")
        return {t: v for t, v in out.items() if t in universe}
    except Exception as e:
        log(f"[ta_screener] earnings 拉取失败(跳过): {e}")
        return {}


def trigger_barchart_pull(log=print) -> dict:
    """按需起子进程跑 barchart_mcp/screener.py(playwright,~几分钟)。
    非阻塞:Popen 后立即返回;完成后下次扫描自然读到新 CSV。"""
    script = BARCHART_DIR / "screener.py"
    if not script.exists():
        return {"ok": False, "error": "barchart_mcp/screener.py 不存在"}
    try:
        venv_py = BARCHART_DIR / ".venv" / "bin" / "python3"
        py = str(venv_py) if venv_py.exists() else sys.executable
        subprocess.Popen([py, str(script)], cwd=str(BARCHART_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "msg": "Barchart screener 子进程已启动(几分钟后重扫即可合并)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

"""Market Events REST proxy — thin wrapper over benzinga_api.calendar with a
short server cache so multiple PWA clients don't hammer Benzinga (→ no 429).

GET /api/market-events/{kind}?tickers=AAPL,NVDA&days_back=30&days_fwd=60&limit=200
    kind ∈ {ipos, ma, offerings, splits, option_activity}
→ {"kind", "count", "events": [...]}
"""
from __future__ import annotations

import datetime
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benzinga_api import calendar as cal          # noqa: E402
from benzinga_api._http import BenzingaAuthError  # noqa: E402

# user-facing kind -> benzinga_api.calendar kind
_KINDS = {
    "ipos": "ipos",
    "ma": "ma",
    "offerings": "offerings",
    "splits": "splits",
    "option_activity": "option_activity",
}

# IPOs are brand-new companies — not in a watchlist of established names — so
# they must NOT be filtered by the idea watchlist (that's why the tab was empty).
# Show all, market-wide.
_NO_DEFAULT_TICKER = {"ipos"}
# Per-kind market-cap floor (field, min$). IPOs: only show ≥ $2B.
_MIN_MCAP = {"ipos": ("market_cap_at_offer", 2_000_000_000.0)}
# Sorting: per-kind "size" field for value-sort; date fields tried in order.
_VALUE_FIELD = {"ipos": "market_cap_at_offer", "ma": "deal_size",
                "offerings": "proceeds", "option_activity": "cost_basis"}
_DATE_FIELDS = ("date", "date_ex", "date_announced", "date_completed",
                "pricing_date", "time")
_DEFAULT_SORT = {"ipos": "value_desc"}   # others default to date_desc

_TTL = 30  # seconds
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# Default ticker universe = the user's idea list (data/watchlist_ideas.txt,
# one symbol per line). Used when the caller doesn't pass tickers=.
_WATCHLIST_FILE = _REPO_ROOT / "data" / "watchlist_ideas.txt"


def _default_tickers() -> Optional[str]:
    try:
        syms = [l.strip() for l in _WATCHLIST_FILE.read_text().splitlines() if l.strip()]
        return ",".join(syms) or None
    except Exception:
        return None


def _importance_ok(row: dict, target: Optional[int]) -> bool:
    """Keep row if its importance == target. Rows with NO importance field
    (e.g. option_activity, ipos) always pass — that filter doesn't apply to them."""
    if target is None:
        return True
    v = row.get("importance")
    if v in (None, ""):
        return True
    try:
        return int(v) == target
    except (TypeError, ValueError):
        return True


_ARCHIVE_DIR = _REPO_ROOT / "data" / "market_events"
_arch_cache: dict[str, tuple[float, list]] = {}


def _load_archive(kind: str):
    """Full-history rows from data/market_events/<kind>.parquet (mtime-cached).
    None if no archive (e.g. option_activity)."""
    p = _ARCHIVE_DIR / f"{kind}.parquet"
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    hit = _arch_cache.get(kind)
    if hit and hit[0] == mt:
        return hit[1]
    # to_json → json.loads yields plain JSON-safe dicts (NaN→None, numpy→native)
    # so FastAPI can serialize them.
    recs = json.loads(pd.read_parquet(p).to_json(orient="records"))
    _arch_cache[kind] = (mt, recs)
    return recs


def _row_date(r) -> str:
    for f in _DATE_FIELDS:
        v = r.get(f)
        if v:
            return str(v)[:10]
    return ""


def _row_has_ticker(r, qs) -> bool:
    blob = " ".join(str(r.get(f, "")) for f in
                    ("ticker", "target_ticker", "acquirer_ticker", "name", "target_name")).upper()
    return any(q in blob for q in qs)


router = APIRouter(prefix="/api/market-events", tags=["market-events"])


def _bz_connected() -> bool:
    try:
        import zeus_config as _zc
        return _zc.has_benzinga()
    except Exception:
        return True


def _bz_status() -> int:
    return 502 if _bz_connected() else 503


def _bz_detail(e, prefix: str = "") -> str:
    """Friendly message when Benzinga Pro isn't connected (no local paths in errors)."""
    if not _bz_connected():
        return ("Benzinga Pro 未连接:在 ⚙ 设置 → 新闻源 粘贴 Benzinga Pro cookie 后可用 · "
                "Benzinga Pro is not connected: paste your cookies in ⚙ Settings → News sources")
    return f"{prefix}: {e}" if prefix else str(e)


_OA_ARCHIVE = _ARCHIVE_DIR / "option_activity_recent.parquet"  # archive_oa 写的近一周滚动归档


def _agg_oa(rows) -> tuple[list, list]:
    """逐笔 → 按 ticker 拆看多/看空两栏(净 premium 归唯一一侧, 不重合), 各按 premium 降序。"""
    agg: dict[str, dict] = {}
    for r in rows:
        t = r.get("ticker") or "?"
        a = agg.setdefault(t, {"ticker": t, "count": 0, "premium": 0.0, "calls": 0,
                               "puts": 0, "bull_prem": 0.0, "bear_prem": 0.0})
        a["count"] += 1
        try:
            cb = float(r.get("cost_basis") or 0)
        except (TypeError, ValueError):
            cb = 0.0
        a["premium"] += cb
        if str(r.get("put_call") or "").upper().startswith("C"):
            a["calls"] += 1
        else:
            a["puts"] += 1
        s = str(r.get("sentiment") or "").upper()
        if "BULL" in s:
            a["bull_prem"] += cb
        elif "BEAR" in s:
            a["bear_prem"] += cb
    bull, bear = [], []
    for a in agg.values():
        a["net"] = round(a["bull_prem"] - a["bear_prem"], 2)
        if a["bull_prem"] <= 0 and a["bear_prem"] <= 0:
            continue
        (bull if a["bull_prem"] >= a["bear_prem"] else bear).append(a)
    bull.sort(key=lambda x: x["bull_prem"], reverse=True)
    bear.sort(key=lambda x: x["bear_prem"], reverse=True)
    return bull, bear


def _read_archive_day(day: str) -> list:
    if not _OA_ARCHIVE.exists():
        return []
    try:
        df = pd.read_parquet(_OA_ARCHIVE)
        return df[df["date"].astype(str) == day].to_dict("records")
    except Exception:
        return []


def _parse_day(date: Optional[str]) -> str:
    today = datetime.date.today().isoformat()
    if not date:
        return today
    try:
        return datetime.date.fromisoformat(date).isoformat()
    except ValueError:
        return today


def _filter_oa(rows, min_premium=0.0, max_dte=None, ref_day=None):
    """逐笔级过滤(summary 与 ticker 共用 → drill-in 永远 = 过滤后的汇总):
    单笔 premium(cost_basis) >= min_premium 且 0 <= DTE(到期-成交日) <= max_dte。
    min_premium<=0 不限金额; max_dte=None 不限到期。开了某项过滤时, 该项不可解析的笔被排除。"""
    if (not min_premium or min_premium <= 0) and max_dte is None:
        return rows
    out = []
    for r in rows:
        if min_premium and min_premium > 0:
            try:
                if float(r.get("cost_basis") or 0) < min_premium:
                    continue
            except (TypeError, ValueError):
                continue
        if max_dte is not None:
            de = str(r.get("date_expiration") or "")[:10]
            td = str(r.get("date") or ref_day or "")[:10]
            try:
                dte = (datetime.date.fromisoformat(de) - datetime.date.fromisoformat(td)).days
            except ValueError:
                continue
            if dte < 0 or dte > max_dte:
                continue
        out.append(r)
    return out


@router.get("/option_activity/summary")
def oa_summary(date: Optional[str] = Query(None, description="YYYY-MM-DD; 空=今天"),
               tickers: Optional[str] = Query(None),
               min_premium: float = Query(0.0, description="只保留单笔 premium($) >= 此值; 0=不限"),
               max_dte: Optional[int] = Query(None, description="只保留 DTE(天) <= 此值; 空=不限")):
    """异动期权 看多/看空两栏(不重合, 各按 premium 降序, 不截断)。
    数据=watchlist 完整归档(option_activity_recent.parquet, 按 id 去重 → 笔数准确); 今天若归档
    尚空则用实时全市场采样兜底。点 ticker → /option_activity/ticker 拿该票当天完整逐笔。
    min_premium/max_dte 在聚合前对逐笔过滤 → 汇总随过滤变, 且与 drill 一致。"""
    today = datetime.date.today().isoformat()
    req = _parse_day(date)
    rows = _read_archive_day(req)
    src = "archive"
    if not rows and req >= today:          # 今天归档还没数据 → 实时全市场兜底(采样)
        try:
            rows = cal.fetch("option_activity", tickers=tickers,
                             date_from=today, date_to=today, pagesize=20000)
            src = "live"
        except BenzingaAuthError as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e))
        except Exception as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e, "Benzinga 拉取失败"))
    rows = _filter_oa(rows, min_premium, max_dte, req)   # 过滤后再聚合
    bull, bear = _agg_oa(rows)
    return {"date": req, "source": src, "bull": bull, "bear": bear}


@router.get("/option_activity/ticker")
def oa_ticker(ticker: str = Query(...), date: Optional[str] = Query(None),
              min_premium: float = Query(0.0), max_dte: Optional[int] = Query(None)):
    """单票当天完整逐笔(无截断、无日历过滤)。今天=实时单票查询(单票远<5000上限→完整);
    过去=读归档。修掉旧 drill 走通用端点被 limit=500/importance/days_back 误伤的 bug。
    min_premium/max_dte 与 summary 同义同源 → drill-in 永远和过滤后的汇总一致。"""
    today = datetime.date.today().isoformat()
    req = _parse_day(date)
    tk = ticker.strip().upper()
    # 与 summary 同源(归档)→ drill 笔数永远 = summary。今天归档还空时才实时单票兜底。
    rows = [r for r in _read_archive_day(req) if str(r.get("ticker", "")).upper() == tk]
    src = "archive"
    if not rows and req >= today:
        try:
            rows = cal.fetch("option_activity", tickers=tk,
                             date_from=today, date_to=today, pagesize=20000)
            src = "live"
        except BenzingaAuthError as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e))
        except Exception as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e, "Benzinga 拉取失败"))
    rows = _filter_oa(rows, min_premium, max_dte, req)    # 与 summary 同一过滤
    rows.sort(key=lambda r: str(r.get("time") or r.get("date") or ""), reverse=True)
    return {"ticker": tk, "date": req, "source": src, "count": len(rows), "events": rows}


@router.post("/option_activity/refresh")
def oa_refresh():
    """手动刷新: 抓今天的 watchlist 期权流分批累积进归档(节流+429退避)。返回归档总笔数。"""
    from benzinga_market_events import archive_oa
    rows = archive_oa.fetch_today()
    n = archive_oa.upsert(rows)
    return {"fetched": len(rows), "archive_rows": n}


_CORP_KINDS = ["ma", "ipos", "offerings", "splits"]
_CORP_LABEL = {"ma": "🤝 M&A", "ipos": "🆕 IPO", "offerings": "📤 增发", "splits": "✂️ 拆股"}


@router.get("/corp/all")
def corp_all(tickers: Optional[str] = Query(None),
             days_back: int = Query(31, ge=0, le=8000),
             days_fwd: int = Query(3, ge=0, le=365),
             importance: Optional[int] = Query(5, ge=0, le=5),
             sort: Optional[str] = Query(None),
             limit: int = Query(500, ge=1, le=2000)):
    """合并 M&A / IPO / 增发 / 拆股 为统一列表，每行带 _type 字段。"""
    merged: list[dict] = []
    for kind in _CORP_KINDS:
        try:
            result = events(kind, tickers=tickers, days_back=days_back,
                            days_fwd=days_fwd, importance=importance,
                            sort=sort, limit=limit)
            for row in result.get("events", []):
                row["_type"] = kind
                row["_type_label"] = _CORP_LABEL[kind]
                merged.append(row)
        except Exception:
            pass
    for r in merged:
        d = ""
        for f in _DATE_FIELDS:
            v = r.get(f)
            if v:
                d = str(v)
                break
        r["_sort_date"] = d
    if sort == "date_asc":
        merged.sort(key=lambda r: r["_sort_date"])
    else:
        merged.sort(key=lambda r: r["_sort_date"], reverse=True)
    merged = merged[:limit]
    return {"kind": "corp", "count": len(merged), "events": merged}


@router.get("/{kind}")
def events(kind: str,
           tickers: Optional[str] = Query(None, description="逗号分隔;留空=用 idea 自选清单"),
           days_back: int = Query(31, ge=0, le=8000),   # 时间范围;"全部"传大值
           days_fwd: int = Query(3, ge=0, le=365),
           importance: Optional[int] = Query(5, ge=0, le=5,
                                             description="只保留该 importance(默认5);设负数=不过滤"),
           min_mcap: Optional[float] = Query(None, description="市值下限$(IPO 默认 2e9)"),
           sort: Optional[str] = Query(None, description="date_desc|date_asc|value_desc"),
           limit: int = Query(300, ge=1, le=2000)):
    if kind not in _KINDS:
        raise HTTPException(status_code=404,
                            detail=f"unknown kind '{kind}'; valid: {sorted(_KINDS)}")
    # IPOs show ALL (market-wide, no watchlist); others default to idea watchlist.
    if kind in _NO_DEFAULT_TICKER:
        tk = tickers   # only filter if the caller explicitly passes tickers
        if kind == "ipos":
            days_fwd = max(days_fwd, 45)    # also catch upcoming IPOs
    else:
        tk = tickers if tickers else _default_tickers()
    if kind == "option_activity":
        days_back = min(days_back, 31)      # dense per-trade flow — cap window
    imp = importance if (importance is not None and importance >= 0) else None
    sort = sort or _DEFAULT_SORT.get(kind, "date_desc")
    cache_key = f"{kind}|{tk}|{days_back}|{days_fwd}|{imp}|{min_mcap}|{sort}|{limit}"
    now = time.time()
    with _lock:
        hit = _cache.get(cache_key)
        if hit and (now - hit[0]) < _TTL:
            return hit[1]

    today = datetime.date.today()
    d_from = (today - datetime.timedelta(days=days_back)).isoformat()
    d_to = (today + datetime.timedelta(days=days_fwd)).isoformat()

    # Deep ranges (>~3 months) read the local full-history parquet archive — the
    # live API caps a single response and loses old rows. Recent ranges hit the
    # live API for freshness. option_activity has no archive → always live.
    arch = _load_archive(kind) if days_back > 100 else None
    if arch is not None:
        rows = [r for r in arch if d_from <= _row_date(r) <= d_to]
        if tickers:
            qs = [t.strip().upper() for t in tickers.split(",") if t.strip()]
            rows = [r for r in rows if _row_has_ticker(r, qs)]
    else:
        try:
            rows = cal.fetch(_KINDS[kind], tickers=tk, date_from=d_from, date_to=d_to,
                             pagesize=max(limit * 3, 1500))
        except BenzingaAuthError as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e))
        except Exception as e:
            raise HTTPException(status_code=_bz_status(), detail=_bz_detail(e, "Benzinga 拉取失败"))

    rows = [r for r in rows if _importance_ok(r, imp)]
    if kind in _MIN_MCAP:
        _mf, _default_floor = _MIN_MCAP[kind]
        _floor = min_mcap if (min_mcap is not None) else _default_floor
        def _mc(r):
            try:
                return float(r.get(_mf) or 0)
            except (TypeError, ValueError):
                return 0.0
        rows = [r for r in rows if _mc(r) >= _floor]
    def _date_key(r):
        for f in _DATE_FIELDS:
            v = r.get(f)
            if v:
                return str(v)
        return ""
    def _val_key(r):
        f = _VALUE_FIELD.get(kind)
        try:
            return float(r.get(f) or 0) if f else 0.0
        except (TypeError, ValueError):
            return 0.0
    if sort == "date_asc":
        rows.sort(key=_date_key)
    elif sort == "value_desc":
        rows.sort(key=_val_key, reverse=True)
    else:  # date_desc (default)
        rows.sort(key=_date_key, reverse=True)
    rows = rows[:limit]
    out = {"kind": kind, "count": len(rows), "importance": imp, "events": rows}
    with _lock:
        _cache[cache_key] = (now, out)
    return out

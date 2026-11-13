"""heatmap 财报日历 —— 本地长存 + 按需重拉。

作者口径(2026-07-23): 财报日期不必天天拉, 存本地就行, 过了那天再拉。
但公司**会改期**(尤其临近时从「估计」变「确认」), 所以分两档:

    没记录            → 拉
    存的日期已过去     → 拉(取下一季)
    日期 ≤21 天且记录 >3 天没更新 → 拉(临近的容易变)
    日期 >21 天        → 不拉(几个月后的事, 改了也不影响现在开仓)

于是每天真正要请求的只有临近财报那几十只, 400 只里绝大多数一动不动。

数据源按批量效率排(全部 fail-soft, 一个不行换下一个):
  1. TastyTrade /market-metrics —— 2 次 REST 拉 600+ 符号, 亚秒级, **自带盘前/盘后**
  2. yfinance                   —— 逐票, 慢, 兜底
  3. finviz                     —— 逐票, 再兜底

⚠️ ETF 没有财报, 不参与拉取也永不参与过滤(调用方按 is_etf 排除)。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import zeus_shutdown

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = REPO_ROOT / "data" / "heatmap" / "earnings.json"

# 基金/信托类标的:本身没有财报, 但在 universe 里没被归进 ###ETF 段(归进去会从
# K线图墙消失 —— 图墙按 !is_etf 过滤, 而这些主题标的作者要看图)。所以单列一张
# 名单: 不去拉财报、不显示财报行、也永不被财报筛选影响。
NO_EARNINGS = {"GDX", "COPX", "URA", "IBIT"}

NEAR_DAYS = 21      # 「临近」阈值:这之内才考虑重拉
STALE_DAYS = 3      # 临近的记录超过这么多天没更新就重拉
TT_CHUNK = 150      # ⚠️ 一次 GET 塞太多符号会 HTTP 414(见 ta_screener/external.py)

# TastyTrade / yfinance / finviz 的时段字段五花八门, 统一成 am(盘前) / pm(盘后)
_AM_CODES = {"BTO", "BMO", "BEFORE", "PRE", "AM", "BEFORE_MARKET_OPEN"}
_PM_CODES = {"AMC", "AFTER", "POST", "PM", "AFTER_MARKET_CLOSE"}


def normalize_time(raw) -> str:
    """→ 'am'(盘前) / 'pm'(盘后) / ''(不详)。"""
    s = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not s:
        return ""
    if s in _AM_CODES:
        return "am"
    if s in _PM_CODES:
        return "pm"
    if s.startswith("B"):        # BTO/BMO 之类的变体
        return "am"
    if s.startswith("A"):        # AMC 之类
        return "pm"
    return ""


def days_until(date_str: str, today: str) -> int | None:
    """财报还有几天(负数=已过去)。解析失败 → None。"""
    try:
        d = _dt.date.fromisoformat(str(date_str)[:10])
        t = _dt.date.fromisoformat(str(today)[:10])
    except Exception:
        return None
    return (d - t).days


def is_upcoming(rec, today: str) -> bool:
    """这条记录是不是「还没开的财报」。

    源返回上一季日期时必须判无效 —— 已经报过的财报对「别撞上财报」没有意义,
    留着还会让 UI 显示一个早就过去的日期。
    """
    if not isinstance(rec, dict) or not rec.get("date"):
        return False
    left = days_until(rec["date"], today)
    return left is not None and left >= 0


def needs_refresh(rec, today: str,
                  near_days: int = NEAR_DAYS, stale_days: int = STALE_DAYS) -> bool:
    """这条记录要不要重新拉。rec=None / 无日期 → 要。"""
    if not isinstance(rec, dict):
        return True
    date = rec.get("date")
    if not date:
        return True
    left = days_until(date, today)
    if left is None:
        return True
    if left < 0:
        return True                       # 财报已开过 → 该取下一季了
    if left > near_days:
        return False                      # 还早, 存着就行
    age = days_until(today, rec.get("fetched") or "")   # today - fetched
    if age is None:
        return True
    return age >= stale_days              # 临近且好几天没刷 → 刷


def select_stale(records: dict, tickers, today: str,
                 near_days: int = NEAR_DAYS, stale_days: int = STALE_DAYS) -> list:
    """这一批票里哪些需要重拉(排序后返回, 便于测试与稳定分块)。"""
    recs = records or {}
    return sorted(t for t in (tickers or ())
                  if needs_refresh(recs.get(t), today, near_days, stale_days))


def decorate(rec, today: str) -> dict | None:
    """给前端用的形态: {date, time, days}。无日期 → None。"""
    if not isinstance(rec, dict) or not rec.get("date"):
        return None
    return {
        "date": str(rec["date"])[:10],
        "time": rec.get("time") or "",
        "days": days_until(rec["date"], today),
    }


QUARTER_DAYS = 91     # 美股大盘股基本按季报, 上一次 + 91 天是常用近似


def estimate_next(last_date: str, today: str, quarter: int = QUARTER_DAYS):
    """按季度从「上一次财报」推下一次(粗估)。已经推到过去就再加一季。"""
    base = days_until(last_date, today)
    if base is None:
        return None
    try:
        d = _dt.date.fromisoformat(str(last_date)[:10])
    except Exception:
        return None
    for k in range(1, 5):
        nxt = d + _dt.timedelta(days=quarter * k)
        if (nxt - _dt.date.fromisoformat(str(today)[:10])).days >= 0:
            return nxt.isoformat()
    return None


def decorate_any(rec, today: str) -> dict | None:
    """decorate 的超集: 没有下一次日期时, 回落成 {last_date} —— 让 UI 能说
    「Jul 14 已报」(= 短期无财报风险), 而不是一个什么都没说的「?」。"""
    d = decorate(rec, today)
    if d:
        return d
    last = (rec or {}).get("last_date") if isinstance(rec, dict) else None
    if not last:
        return None
    est = estimate_next(last, today)
    if est:
        return {"date": est, "time": "", "days": days_until(est, today),
                "estimated": True, "last_date": str(last)[:10]}
    return {"date": None, "time": "", "days": None,
            "last_date": str(last)[:10],
            "last_days": days_until(last, today)}


# ── 存储 ────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        recs = d.get("records")
        return recs if isinstance(recs, dict) else {}
    except Exception:
        return {}


def save(records: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"records": records,
             "updated": _dt.datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        print(f"[heatmap-earnings] save err: {e}")


# ── 抓取(三级 fallback, 全部 fail-soft) ─────────────────────────────

CALENDAR_CACHE = REPO_ROOT / "trade_journal" / "data" / "calendar_cache.json"


def parse_calendar_cache(payload) -> dict:
    """trade_journal 的 Nasdaq 日历(本地、服务器每天自刷、**带 BMO/AMC**)→ {ticker: {date,time,src}}。

    TastyTrade 的 time-of-day 缺得厉害(实测 144 只里 90 只为空), 而这份本地文件
    154 条里 119 条有 session, 且零网络成本 → 拿它当「盘前/盘后」的权威源。
    """
    out = {}
    for e in ((payload or {}).get("earnings") or []):
        t = str((e or {}).get("ticker") or "").upper().strip()
        d = str((e or {}).get("date") or "")[:10]
        if not t or not d:
            continue
        out[t] = {"date": d, "time": normalize_time(e.get("session")), "src": "nasdaq"}
    return out


def _fetch_calendar_cache(log=print) -> dict:
    try:
        return parse_calendar_cache(json.loads(CALENDAR_CACHE.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _fetch_tastytrade(tickers: list, log=print) -> dict:
    """批量, 首选。{ticker: {date, time}}"""
    import sys
    out = {}
    try:
        ta_dir = REPO_ROOT / "trading_assistant"
        if str(ta_dir) not in sys.path:
            sys.path.insert(0, str(ta_dir))
        from ticker_metrics import fetch_ticker_metrics
        for i in range(0, len(tickers), TT_CHUNK):
            if zeus_shutdown.stopping():
                break
            m = fetch_ticker_metrics(tickers[i:i + TT_CHUNK]) or {}
            for t, d in m.items():
                ed = (d or {}).get("earnings_date")
                if ed:
                    out[t.upper().replace("/", "-")] = {
                        "date": str(ed)[:10],
                        "time": normalize_time((d or {}).get("earnings_time")),
                        "src": "tastytrade",
                    }
    except Exception as e:
        log(f"[heatmap-earnings] tastytrade 失败: {e}")
    return out


def pick_future_ts(stamps, today: str):
    """一堆 epoch 时间戳里挑「最早的未来日期」→ (date_iso, hour) 或 None。

    ⚠️ yfinance 的 earningsTimestamp 是**上一次**已报的财报, 下一季(预估)在
    earningsTimestampStart/End 里。旧实现 `earningsTimestamp or …Start` 会
    优先取到上一次 → 整条记录被 is_upcoming 判废。实测 GEV 上次 07-22 / 下次
    10-21、NFLX 07-16 / 10-20、NKE 06-30 / 09-29 都是这么丢掉的。
    """
    best = None
    for ts in stamps:
        try:
            dt = _dt.datetime.fromtimestamp(int(ts))
        except Exception:
            continue
        iso = dt.date().isoformat()
        if days_until(iso, today) is None or days_until(iso, today) < 0:
            continue
        if best is None or iso < best[0]:
            best = (iso, dt.hour)
    return best


def _fetch_yfinance(tickers: list, log=print) -> dict:
    """逐票兜底(慢), 只对前面的源没给的票跑。"""
    out = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    today = _dt.date.today().isoformat()
    for t in tickers:
        if zeus_shutdown.stopping():
            break
        try:
            info = yf.Ticker(t).info or {}
            got = pick_future_ts([info.get("earningsTimestampStart"),
                                  info.get("earningsTimestampEnd"),
                                  info.get("earningsTimestamp")], today)
            if not got:
                continue
            iso, hour = got
            when = "am" if hour < 12 else ("pm" if hour >= 16 else "")
            out[t] = {"date": iso, "time": when, "src": "yfinance"}
        except Exception:
            continue
    return out


def _fetch_finviz(tickers: list, log=print) -> dict:
    """最后兜底。finviz 的 Earnings 字段形如 'Aug 20/a'(a=after, b=before)。"""
    out = {}
    try:
        import finviz
    except Exception:
        return out
    today = _dt.date.today()
    for t in tickers:
        if zeus_shutdown.stopping():
            break
        try:
            raw = (finviz.get_stock(t) or {}).get("Earnings") or ""
            if not raw or raw == "-":
                continue
            part, _, ap = raw.partition("/")
            when = "pm" if ap.strip().lower().startswith("a") else (
                "am" if ap.strip().lower().startswith("b") else "")
            d = _dt.datetime.strptime(part.strip(), "%b %d").date()
            d = d.replace(year=today.year)
            if (d - today).days < -180:        # 跨年:'Jan 15' 在 12 月看到 → 明年
                d = d.replace(year=today.year + 1)
            out[t] = {"date": d.isoformat(), "time": when, "src": "finviz"}
        except Exception:
            continue
    return out


def refresh(tickers, etfs=None, force: bool = False, log=print) -> dict:
    """按需刷新并落盘, 返回全量 records。ETF 直接跳过(没有财报)。"""
    today = _dt.date.today().isoformat()
    recs = load()
    skip = set(etfs or ()) | NO_EARNINGS
    cands = [t for t in (tickers or ()) if t not in skip]
    todo = sorted(cands) if force else select_stale(recs, cands, today)
    if not todo:
        return recs

    log(f"[heatmap-earnings] {len(todo)}/{len(cands)} 只需要刷新")
    cal = _fetch_calendar_cache(log)          # 本地免费, 且是盘前/盘后的权威源

    # ⚠️ 源给的日期**可能是上一季**(实测 TastyTrade 对 30 只返回已过去的日期,
    #    如 COST 2026-05-28)。过去的日期对「别撞上财报」毫无用处 → 视为没拿到,
    #    继续往下一个源找。
    got = {}
    _last_seen = {}      # {ticker: 源给出的最近一次「已过去」的财报日}
    chain = (
        ("tastytrade", lambda ts: _fetch_tastytrade(ts, log)),
        ("nasdaq", lambda ts: {t: dict(cal[t]) for t in ts if t in cal}),
        ("yfinance", lambda ts: _fetch_yfinance(ts, log)),
        ("finviz", lambda ts: _fetch_finviz(ts, log)),
    )
    for name, fn in chain:
        miss = [t for t in todo if t not in got]
        if not miss or zeus_shutdown.stopping():
            break
        try:
            fresh = fn(miss) or {}
        except Exception as e:
            log(f"[heatmap-earnings] {name} 失败: {e}")
            continue
        for t, v in fresh.items():                     # 过期的即「上一次财报日」, 留作兜底信息
            if v and v.get("date") and not is_upcoming(v, today):
                prev = _last_seen.get(t)
                if not prev or str(v["date"]) > str(prev):
                    _last_seen[t] = str(v["date"])[:10]
        usable = {t: v for t, v in fresh.items() if is_upcoming(v, today)}
        if usable:
            log(f"[heatmap-earnings]   {name}: +{len(usable)} 只"
                + (f" (丢弃 {len(fresh) - len(usable)} 只过期日期)" if len(fresh) > len(usable) else ""))
        got.update(usable)

    for t, v in got.items():
        v["fetched"] = today
        recs[t] = v

    # 补 AM/PM: 凡是有日期却没时段的(不限本轮刷新的), 用本地日历补 —— 零成本。
    filled = 0
    for t, c in cal.items():
        r = recs.get(t)
        if r and r.get("date") and not r.get("time") and c.get("time") \
                and r["date"] == c["date"]:
            r["time"] = c["time"]
            filled += 1
    if filled:
        log(f"[heatmap-earnings] 用本地日历补了 {filled} 只的盘前/盘后")
    # 四个源都没给出「未来日期」的:多半是**刚报完**, 下一季日期还没公布。
    # 这种票对「别撞上财报」而言恰恰最安全(三个月内没有财报), 所以把源给的
    # 「上一次财报日」留在 last_date 里, UI 显示「Jul 14 已报」而不是无信息的「?」。
    # The server is stopping: the sources were cut short, so a ticker without a date
    # was not looked up everywhere. Keep its old record; the next start asks again.
    for t in ([] if zeus_shutdown.stopping() else todo):
        if t not in got:
            last = _last_seen.get(t) or (recs.get(t) or {}).get("last_date")
            recs[t] = {"date": None, "last_date": last, "time": "",
                       "src": "none", "fetched": today}
    if got or filled:
        save(recs)
    log(f"[heatmap-earnings] 刷新到 {len(got)} 只, 库里共 {len(recs)} 只")
    return recs


def get_map(tickers=None, today: str = None) -> dict:
    """{ticker: {date, time, days}} —— 只读, 给 _build_response 用。"""
    today = today or _dt.date.today().isoformat()
    recs = load()
    keys = tickers if tickers is not None else recs.keys()
    out = {}
    for t in keys:
        if t in NO_EARNINGS:      # 没有财报这回事, 别给前端任何财报字段
            continue
        d = decorate_any(recs.get(t), today)
        if d:
            out[t] = d
    return out

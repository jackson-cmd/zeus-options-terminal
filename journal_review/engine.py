"""build_review(today) -> dict —— 「5 分钟开盘前复习」页面唯一的 IO 编排点。

子模块(kpi / rule_check / closed_yesterday / opened_yesterday / watchlist_today /
insight)全是纯函数,吃这里准备好的数据,不各自读文件 —— 跟 pre_trade_checkup/engine.py
"IO 只在 engine 这一层"的规矩一致。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

import pandas as pd

from . import closed_yesterday as _cy
from . import insight as _insight
from . import kpi as _kpi
from . import opened_yesterday as _oy
from . import rule_check as _rc
from . import watchlist_today as _wt

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOSES_PATH = REPO_ROOT / "trade_journal" / "data" / "entries_close.jsonl"
OPENS_PATH = REPO_ROOT / "trade_journal" / "data" / "entries_open.jsonl"
OVERRIDE_LOG_PATH = REPO_ROOT / "pre_trade_checkup" / "data" / "override_log.jsonl"
OHLC_PATH = REPO_ROOT / "data" / "daily_ohlc.parquet"
CALENDAR_PATH = REPO_ROOT / "trade_journal" / "data" / "calendar_cache.json"

# NYSE 全天休市日 —— 跟 trade_journal/price_tracker.py 的 _NYSE_HOLIDAYS 同源(直接复制
# 而不是跨包 import 私有变量,避免这边改了那边不知道;两边都要每年更新一次)。
_NYSE_HOLIDAYS = frozenset({
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})

_EMPTY_OHLC_COLUMNS = ["Open", "High", "Low", "Close", "Volume", "ticker"]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def prev_business_day(d: dt.date) -> dt.date:
    """跳过周末 + NYSE 假日往前找上一个交易日(周一开盘前复习时"昨天"该指回上周五)。"""
    cur = d - dt.timedelta(days=1)
    while cur.weekday() >= 5 or cur.isoformat() in _NYSE_HOLIDAYS:
        cur -= dt.timedelta(days=1)
    return cur


def _load_ohlc() -> pd.DataFrame:
    try:
        ohlc = pd.read_parquet(OHLC_PATH)
        ohlc.index = pd.to_datetime(ohlc.index)
        return ohlc
    except Exception:
        return pd.DataFrame(columns=_EMPTY_OHLC_COLUMNS)


def _load_calendar() -> dict:
    try:
        if CALENDAR_PATH.exists():
            return json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _load_mtm_lookup(open_positions: list[dict]) -> dict:
    """读 trade_journal/price_tracker.py 已有的每小时快照缓存(不发起新的实时请求)。
    刚开仓、还没被下一次快照捕获到的仓位会查不到 —— 留空,watchlist_today 自己会把
    current_pnl_pct 处理成 None,不报错。

    ⚠️实测踩过的坑:spread_mtm_history(legs) 不传 since 时会扫 option_prices/ 下*全部*
    历史快照文件(现网 3000+ 个,每张持仓一次全量扫描要 15+ 秒 —— 对一个"5 分钟开盘前
    复习"页面完全不能接受)。这里只要最新一个点,传 since=最近几天就够(load_history 靠
    文件名前缀提前跳过太旧的文件,不用整个都读),经验证同样数据下从 16s 降到 <1s。"""
    try:
        from trade_journal import price_tracker as pt
    except Exception:
        return {}
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
    out = {}
    for o in open_positions:
        legs = o.get("legs") or []
        if not legs:
            continue
        try:
            hist = pt.spread_mtm_history(legs, since=since)
        except Exception:
            hist = []
        if hist:
            out[o.get("id")] = {"current_value": hist[-1]["mtm"]}
    return out


def build_review(today: Optional[str] = None) -> dict:
    """today: 'YYYY-MM-DD' 或 None(=用系统今天)。"""
    today_date = dt.date.fromisoformat(today) if today else dt.date.today()
    today_str = today_date.isoformat()
    yesterday_date = prev_business_day(today_date)
    yesterday_str = yesterday_date.isoformat()

    opens = _load_jsonl(OPENS_PATH)
    closes = _load_jsonl(CLOSES_PATH)
    overrides = _load_jsonl(OVERRIDE_LOG_PATH)
    calendar = _load_calendar()

    open_positions = _kpi.positions_snapshot(opens, closes)

    rule_violations = _rc.all_violations(opens, closes, today_str, days_back=7)
    kpi_dict = _kpi.build_kpi(today_str, yesterday_str, closes, open_positions, rule_violations)

    ohlc = _load_ohlc()
    yesterday_closed = _cy.build(closes, ohlc, yesterday_str)
    yesterday_opened = _oy.build(opens, yesterday_str, overrides)

    mtm_lookup = _load_mtm_lookup(open_positions)
    watchlist = _wt.build(open_positions, today_date, calendar, mtm_lookup)

    insight_text = _insight.build(kpi_dict, rule_violations, yesterday_closed, watchlist)

    quick_links = [
        {"label": "跑今日体检", "url": "/pretrade-checkup"},
        {"label": "看今日选股", "url": "/?tab=mbrief"},   # 独立页面已删, 现在是「🧭 市场判断」tab 里的「🎯 选股」sub-tab
        {"label": "全时段砍单复盘", "cmd": "python -m journal_expiry_whatif.cli"},
    ]

    return {
        "as_of": today_str,
        "yesterday": yesterday_str,
        "kpi": kpi_dict,
        "yesterday_closed": yesterday_closed,
        "yesterday_opened": yesterday_opened,
        "watchlist_today": watchlist,
        "rule_violations_7d": rule_violations,
        "insight": insight_text,
        "quick_links": quick_links,
    }

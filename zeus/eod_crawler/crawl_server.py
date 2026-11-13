"""
Long-running EOD options-chain crawl server.

Schedule: every weekday at 15:15 CT (= 16:15 ET, just after option settlement).
Skips US market holidays automatically using the NYSE calendar.

Manual run:
  python crawl_server.py --once                # crawl once, today
  python crawl_server.py --once --date 2026-04-24
  python crawl_server.py --once --symbols AAPL,NVDA
  python crawl_server.py                       # daemon: wait until 15:15 CT, run, repeat

Data is appended to a single parquet store (config.DATA_FILE); not committed to git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import sys
import time

import pytz
import schedule

from config import (
    DATA_EOD_DIR,
    LAST_CRAWLED_FILE,
    LOG_DIR,
    SCHEDULE_HOUR_CT,
    SCHEDULE_MIN_CT,
    TIMEZONE,
)
from crawler import append_day, crawl_all, date_present, load_symbols
from market_calendar import is_trading_day

CT = pytz.timezone(TIMEZONE)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{dt.date.today().isoformat()}.log"
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


log = logging.getLogger("crawl_server")


# ---------------------------------------------------------------------------
# Live progress + cross-process lock (read by the PWA Paper Log progress bar)
# ---------------------------------------------------------------------------

PROGRESS_FILE = DATA_EOD_DIR / "CRAWL_PROGRESS.json"
LOCK_FILE = DATA_EOD_DIR / "crawl.lock"


def _write_progress(d: dict) -> None:
    """Atomically write the live-progress JSON (best-effort; never raises).

    The PWA polls this single file, so progress shows regardless of whether the
    crawl was launched by launchd or by the 'crawl now' button. Lives under
    data_eod/ (gitignored)."""
    try:
        DATA_EOD_DIR.mkdir(exist_ok=True)
        out = {**d, "updated_at": dt.datetime.now().isoformat(timespec="seconds")}
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        tmp.replace(PROGRESS_FILE)
    except Exception:
        log.warning("failed to write %s (ignored)", PROGRESS_FILE.name, exc_info=True)


# ---------------------------------------------------------------------------
# Last-crawled pointer
# ---------------------------------------------------------------------------

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def write_last_crawled(
    data_date: dt.date,
    summary: dict[str, int],
    duration_sec: float,
) -> None:
    """Overwrite data_eod/LAST_CRAWLED.txt — first line is the date (YYYY-MM-DD)."""
    succeeded = sum(1 for v in summary.values() if v > 0)
    failed_syms = [s for s, v in summary.items() if v == -1]
    empty_syms = [s for s, v in summary.items() if v == 0]
    total_rows = sum(v for v in summary.values() if v > 0)

    minutes = duration_sec / 60.0
    weekday = _WEEKDAY_CN[data_date.weekday()]

    lines = [
        data_date.isoformat(),
        f"爬取完成: {weekday}, 用时 {minutes:.1f} 分钟",
        f"Tickers: {len(summary)} "
        f"(成功 {succeeded} / 失败 {len(failed_syms)} / 空链 {len(empty_syms)})",
        f"本次写入行数: {total_rows:,}",
    ]
    if failed_syms:
        lines.append(f"失败 tickers: {', '.join(sorted(failed_syms))}")

    LAST_CRAWLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_CRAWLED_FILE.write_text("\n".join(lines) + "\n")
    log.info("wrote %s -> %s", LAST_CRAWLED_FILE.name, data_date.isoformat())


# ---------------------------------------------------------------------------
# Run logic
# ---------------------------------------------------------------------------

def do_crawl(
    data_date: dt.date | None = None,
    symbols: list[str] | None = None,
    *,
    skip_trading_day_check: bool = False,
) -> None:
    """Crawl one day's EOD chain, guarded by an exclusive cross-process lock.

    Two crawls writing the consolidated parquet at once would corrupt it (each
    does read-all -> temp -> rename, last writer wins), so a non-blocking flock
    on data_eod/crawl.lock makes overlapping invocations a no-op. This protects
    against launchd's own overlapping windows AND the manual 'crawl now' button
    racing a scheduled run.
    """
    DATA_EOD_DIR.mkdir(exist_ok=True)
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning("another crawl holds the lock — skipping this invocation")
        lock_fp.close()
        return

    try:
        _do_crawl_locked(data_date, symbols, skip_trading_day_check=skip_trading_day_check)
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fp.close()


def _do_crawl_locked(
    data_date: dt.date | None,
    symbols: list[str] | None,
    *,
    skip_trading_day_check: bool,
) -> None:
    today_local = dt.datetime.now(CT).date()
    target_date = data_date or today_local
    forced = data_date is not None  # explicit --date re-fetches and replaces the day
    date_str = target_date.isoformat()

    if not skip_trading_day_check and target_date == today_local:
        if not is_trading_day(target_date):
            log.info("[%s] market closed — skipping", date_str)
            _write_progress({"running": False, "phase": "skipped", "date": date_str,
                             "msg": "今日休市,跳过"})
            return

    if not forced and date_present(target_date):
        log.info("[%s] already in store — skipping", date_str)
        _write_progress({"running": False, "phase": "skipped", "date": date_str,
                         "today_done": True, "msg": "今日已爬,跳过"})
        return

    syms = symbols if symbols else load_symbols()
    total = len(syms)
    log.info("[%s] starting crawl: %d symbols", date_str, total)

    try:
        _write_progress({"running": True, "phase": "crawl", "date": date_str,
                         "done": 0, "total": total, "current": None, "pct": 0,
                         "ok": 0, "empty": 0, "fail": 0})

        def _cb(done: int, total_: int, current, summary: dict) -> None:
            ok = sum(1 for v in summary.values() if v > 0)
            empty = sum(1 for v in summary.values() if v == 0)
            fail = sum(1 for v in summary.values() if v == -1)
            pct = int(done * 100 / total_) if total_ else 100
            _write_progress({"running": True, "phase": "crawl", "date": date_str,
                             "done": done, "total": total_, "current": current,
                             "ok": ok, "empty": empty, "fail": fail, "pct": pct})

        started = time.time()
        summary, tables = crawl_all(syms, target_date, progress_cb=_cb)
        written = append_day(target_date, tables)
        duration = time.time() - started

        succeeded = sum(1 for v in summary.values() if v > 0)
        failed = sum(1 for v in summary.values() if v == -1)
        empty = sum(1 for v in summary.values() if v == 0)
        log.info(
            "[%s] done: %d ok, %d fail, %d rows in %.1f min",
            date_str, succeeded, failed, written, duration / 60,
        )

        write_last_crawled(target_date, summary, duration)
        _write_progress({"running": False, "phase": "done", "date": date_str,
                         "done": total, "total": total, "current": None, "pct": 100,
                         "ok": succeeded, "empty": empty, "fail": failed,
                         "rows": written, "minutes": round(duration / 60, 1)})
    except Exception as exc:
        log.exception("[%s] crawl failed", date_str)
        _write_progress({"running": False, "phase": "error", "date": date_str,
                         "error": str(exc), "msg": f"爬取失败: {exc}"})
        raise


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def scheduled_run() -> None:
    """Wrapper used by the scheduler — always uses today's CT date."""
    try:
        do_crawl()
    except Exception:
        log.exception("scheduled crawl raised")


def run_daemon() -> None:
    """Block forever, firing the crawl at SCHEDULE_HOUR_CT:SCHEDULE_MIN_CT each day."""
    target = f"{SCHEDULE_HOUR_CT:02d}:{SCHEDULE_MIN_CT:02d}"
    log.info("daemon starting; schedule: %s CT (=%s local)", target, _local_equiv(target))

    schedule.every().day.at(target, TIMEZONE).do(scheduled_run)

    while True:
        schedule.run_pending()
        time.sleep(20)


def _local_equiv(ct_hhmm: str) -> str:
    """Return the local-time equivalent of a CT HH:MM string, for log readability."""
    try:
        h, m = (int(x) for x in ct_hhmm.split(":"))
        today = dt.datetime.now(CT).date()
        ct_dt = CT.localize(dt.datetime(today.year, today.month, today.day, h, m))
        return ct_dt.astimezone().strftime("%H:%M %Z")
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="EOD options-chain crawl server")
    p.add_argument("--once", action="store_true", help="run a single crawl and exit")
    p.add_argument("--date", help="override DataDate (YYYY-MM-DD); skips trading-day check")
    p.add_argument("--symbols", help="comma-separated list to crawl (default: full screener)")
    args = p.parse_args()

    setup_logging()

    if args.once:
        date = dt.date.fromisoformat(args.date) if args.date else None
        syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
        do_crawl(date, syms, skip_trading_day_check=bool(args.date))
        return

    run_daemon()


if __name__ == "__main__":
    main()

"""On yfinance before 1.4.0, every yfinance download in ZEUS runs one at a time (zeus/zeus_yf.py).

There yfinance.download keeps the state of a call in module globals and clears them when a call
starts, so downloads that overlap in different threads lose their own tickers, come back with
another call's tickers, or fail with "dictionary changed size during iteration". yfinance 1.4.0
and later keep each call's state to that call, so there downloads start at once and only the
heatmap table refreshes take turns. yfinance is replaced here by a fake module that reports a
version and counts the downloads running at the same moment."""
import ast
import asyncio
import datetime
import importlib
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import zeus_yf

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "zeus"


def _frame(tickers, group_by="column", days=60):
    """Daily bars the way yf.download returns them: (field, ticker) columns, or (ticker, field)
    with group_by="ticker"."""
    idx = pd.bdate_range(end="2026-11-13", periods=days)
    px = 100 + np.arange(days, dtype=float) * 0.1
    cols = {}
    for t in tickers:
        for f in ("Open", "High", "Low", "Close", "Volume"):
            cols[(t, f) if group_by == "ticker" else (f, t)] = px * (1000 if f == "Volume" else 1)
    return pd.DataFrame(cols, index=idx)


class FakeYF:
    def __init__(self):
        self.delay = 0.0
        self.calls = []        # the tickers of each download, in the order they started
        self.active = 0
        self.peak = 0          # most downloads running at the same moment
        self._lock = threading.Lock()

    def download(self, tickers, group_by="column", **kwargs):
        names = tickers.split() if isinstance(tickers, str) else list(tickers)
        with self._lock:
            self.calls.append(names)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return _frame(names, group_by)
        finally:
            with self._lock:
                self.active -= 1


def _install_fake(monkeypatch, version):
    fake = FakeYF()
    module = types.ModuleType("yfinance")
    module.__version__ = version
    module.download = fake.download
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return fake


@pytest.fixture()
def yf(monkeypatch):
    """yfinance whose download() keeps its state in module globals (0.2.54 to 1.3.0)."""
    return _install_fake(monkeypatch, "0.2.65")


@pytest.fixture()
def yf_new(monkeypatch):
    """yfinance 1.4.0 or later: each download() call keeps its own state."""
    return _install_fake(monkeypatch, "1.8.0")


def _queued() -> int:
    return len(zeus_yf._STATE.lock._queue)


def _wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


def _first_tickers(fake):
    return [c[0] for c in fake.calls]


# ── the call sites, run for real against the fake yfinance ────────────────────────────────────

@pytest.fixture()
def call_sites(tmp_path, monkeypatch):
    """One job per module that downloads, set up to stay offline and out of the real data."""
    from heatmap import daily_candles, etf_trends, heatmap_api, metadata, stock_perf
    from market_brief import groups_store
    from options_analyzer.checkup import regime
    from ta_screener import data as ta_data
    import local_broker

    monkeypatch.setattr(metadata, "tickers", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(metadata, "etf_set", lambda: set())
    monkeypatch.setattr(metadata, "get_all", lambda: {})
    monkeypatch.setattr(etf_trends, "_parse_list", lambda: [("SECTOR", "XLK", "Technology")])
    for mod in (stock_perf, etf_trends):
        monkeypatch.setattr(mod, "_CACHE", tmp_path / mod._CACHE.name)
        monkeypatch.setattr(mod, "_state", {})
    monkeypatch.setattr(heatmap_api, "_yf_cache", {"ts": 0.0, "data": {}})
    monkeypatch.setattr(local_broker, "_cache", {})
    monkeypatch.setattr(groups_store, "all_tickers", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(groups_store, "STORE", tmp_path / "industry_ohlc.parquet")
    return {
        "zeus_yf.download": lambda: zeus_yf.download(["AAA"], period="5d", progress=False),
        "heatmap quotes": lambda: heatmap_api._yf_quotes(["AAA", "BBB"]),
        "heatmap closed-session rebuild": lambda: heatmap_api._reconstruct_reg_from_yf(
            datetime.date(2026, 11, 12), only=["AAA"]),
        "daily candles (manual mode)": lambda: daily_candles._yf_daily(["AAA", "BBB"], 30),
        "TA screener batch": lambda: ta_data._fetch_batch(["AAA", "BBB"], {}, "1y", min_bars=2),
        "manual-mode quotes": lambda: local_broker.equity_quotes_batch(["Q1", "Q2", "Q3", "Q4"]),
        "industry groups": lambda: groups_store.refresh(log=lambda *a: None),
        "VIX for the take-profit band": regime._vix_last,
        "Gainers / Losers table": lambda: stock_perf.get(force=True),
        "ETF trends table": lambda: etf_trends.get(force=True),
    }


def _run_together(jobs):
    """Start every job at the same moment, each in its own thread; return the errors."""
    errors = {}
    start = threading.Barrier(len(jobs))

    def run(name, job):
        start.wait()
        try:
            job()
        except Exception as e:          # pragma: no cover - reported below
            errors[name] = e

    threads = [threading.Thread(target=run, args=item) for item in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


def test_downloads_from_ten_call_sites_never_overlap(yf, call_sites):
    yf.delay = 0.05
    errors = _run_together(call_sites)
    assert not errors
    assert len(yf.calls) == len(call_sites)
    assert yf.peak == 1


def test_the_fake_sees_overlap_without_the_lock(yf, call_sites, monkeypatch):
    """The test above would catch overlapping downloads: with the lock taken out they overlap."""
    class NoLock:
        def acquire(self, wait):
            return True

        def release(self):
            pass

    monkeypatch.setattr(zeus_yf._STATE, "lock", NoLock())
    yf.delay = 0.05
    _run_together(call_sites)
    assert yf.peak > 1


# ── yfinance 1.4.0 and later ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version, own_state", [
    ("0.2.54", False), ("0.2.65", False), ("1.3.0", False),
    ("1.4.0", True), ("1.8.0", True), ("1.10.2", True), ("2.0.0", True),
    ("", False), (None, False), ("unknown", False),
])
def test_only_yfinance_1_4_and_later_count_as_keeping_each_download_to_itself(version, own_state):
    """An older or unreadable version keeps the lock."""
    assert zeus_yf.per_call_state(types.SimpleNamespace(__version__=version)) is own_state
    assert zeus_yf.per_call_state(types.SimpleNamespace()) is False


def test_on_yfinance_1_4_downloads_from_the_call_sites_start_at_once(yf_new, call_sites):
    yf_new.delay = 0.05
    errors = _run_together(call_sites)
    assert not errors
    assert len(yf_new.calls) == len(call_sites)
    assert yf_new.peak > 1


def test_on_yfinance_1_4_a_download_does_not_wait_for_a_table_refresh(yf_new):
    """A heatmap table refresh holds the turn for the length of its download; quotes, the daily
    store and the screener do not wait for it, and never get DownloadBusy."""
    held, done = threading.Event(), threading.Event()

    def refresh():
        zeus_yf.acquire()
        held.set()
        done.wait(5)
        zeus_yf.release()

    holder = threading.Thread(target=refresh)
    holder.start()
    held.wait(5)
    try:
        start = time.monotonic()
        zeus_yf.download(["AAA"], lock_wait=0)
        assert time.monotonic() - start < 1.0
    finally:
        done.set()
        holder.join(timeout=5)
    assert _first_tickers(yf_new) == ["AAA"]


def test_on_yfinance_1_4_the_heatmap_tables_still_refresh_one_at_a_time_and_once(yf_new, call_sites):
    """Concurrent requests for the two tables: one download per table, never two at once."""
    from heatmap import etf_trends, stock_perf
    yf_new.delay = 0.05
    jobs = {f"{name} {i}": mod.get for name, mod in (("Gainers / Losers", stock_perf),
                                                       ("ETF trends", etf_trends)) for i in range(3)}
    errors = _run_together(jobs)
    assert not errors
    assert sorted(_first_tickers(yf_new)) == ["AAA", "XLK"]
    assert yf_new.peak == 1


def test_on_yfinance_1_4_a_download_on_the_event_loop_runs_and_is_reported(yf_new, capsys):
    held, done = threading.Event(), threading.Event()

    def hold():
        zeus_yf.acquire()
        held.set()
        done.wait(5)
        zeus_yf.release()

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)

    async def on_the_loop():
        return zeus_yf.download(["AAA"])        # no turn to wait for: it runs, and is reported

    try:
        assert not asyncio.run(on_the_loop()).empty
    finally:
        done.set()
        holder.join(timeout=5)
    assert "on the event loop" in capsys.readouterr().out
    assert _first_tickers(yf_new) == ["AAA"]


# ── the lock ─────────────────────────────────────────────────────────────────────────────────

def test_waiting_downloads_go_in_arrival_order(yf):
    assert zeus_yf.acquire()
    threads = []
    try:
        for name in ("B", "C", "D"):
            t = threading.Thread(target=zeus_yf.download, args=([name],))
            t.start()
            threads.append(t)
            _wait_until(lambda: _queued() == len(threads))
    finally:
        zeus_yf.release()
    for t in threads:
        t.join(timeout=5)
    assert _first_tickers(yf) == ["B", "C", "D"]


def test_a_thread_that_downloads_again_goes_behind_the_waiting_ones(yf):
    """A loop of chunked downloads (the TA screener) cannot starve a single quote."""
    assert zeus_yf.acquire()
    waiting = threading.Thread(target=zeus_yf.download, args=(["B"],))
    waiting.start()
    _wait_until(lambda: _queued() == 1)
    zeus_yf.release()
    zeus_yf.download(["A"])                     # the same thread asks again at once
    waiting.join(timeout=5)
    assert _first_tickers(yf) == ["B", "A"]


def test_a_thread_holding_its_turn_downloads_straight_through(yf):
    """heatmap/table_cache.py holds the turn from its cache check to the end of its download."""
    assert zeus_yf.acquire()
    try:
        zeus_yf.download(["AAA"], lock_wait=0)
    finally:
        zeus_yf.release()
    assert _first_tickers(yf) == ["AAA"]
    with pytest.raises(RuntimeError):
        zeus_yf.release()                       # one release per acquire


def test_a_download_that_waits_too_long_gives_up_and_leaves_the_line(yf):
    assert zeus_yf.acquire()
    errors = []

    def impatient():
        try:
            zeus_yf.download(["B"], lock_wait=0.2)
        except zeus_yf.DownloadBusy as e:
            errors.append(e)

    try:
        t = threading.Thread(target=impatient)
        t.start()
        t.join(timeout=5)
    finally:
        zeus_yf.release()
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    assert _queued() == 0
    zeus_yf.download(["C"], lock_wait=0)        # nobody in line: no wait
    assert _first_tickers(yf) == ["C"]


def test_the_event_loop_never_waits_for_a_download(yf, capsys):
    held, done = threading.Event(), threading.Event()

    def hold():
        zeus_yf.acquire()
        held.set()
        done.wait(5)
        zeus_yf.release()

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)

    async def on_the_loop():
        start = time.monotonic()
        with pytest.raises(zeus_yf.DownloadBusy):
            zeus_yf.download(["AAA"])           # minutes off the loop, no wait on it
        return time.monotonic() - start

    try:
        assert asyncio.run(on_the_loop()) < 1.0
    finally:
        done.set()
        holder.join(timeout=5)
    assert "on the event loop" in capsys.readouterr().out
    assert yf.calls == []

    async def nobody_downloading():
        return zeus_yf.download(["AAA"])

    asyncio.run(nobody_downloading())           # a free turn is taken, as before
    assert _first_tickers(yf) == ["AAA"]


def test_one_lock_even_under_two_module_names(monkeypatch):
    """zeus_yf is imported as `zeus_yf` (zeus/ on sys.path); from the repository root it can
    also be `zeus.zeus_yf`, a second module object. Both must share one lock."""
    monkeypatch.syspath_prepend(str(ROOT))
    had_zeus = "zeus" in sys.modules
    try:
        other = importlib.import_module("zeus.zeus_yf")
        assert other is not zeus_yf
        assert other._STATE.lock is zeus_yf._STATE.lock
        assert other.DownloadBusy is zeus_yf.DownloadBusy
        assert zeus_yf.acquire()
        try:
            got = []
            t = threading.Thread(target=lambda: got.append(other.acquire(0.1)))
            t.start()
            t.join(timeout=5)
            assert got == [False]
        finally:
            zeus_yf.release()
    finally:
        sys.modules.pop("zeus.zeus_yf", None)
        if not had_zeus:
            sys.modules.pop("zeus", None)


# ── the source ───────────────────────────────────────────────────────────────────────────────

# Top-level functions that may call yf.download directly, and why.
DIRECT_OK = {
    ("zeus/zeus_yf.py", "download"): "the wrapper itself",
    ("zeus/options_analyzer/optionstrat_analyzer.py", "main"):
        "the static HTML report, run as its own program (python optionstrat_analyzer.py) with no "
        "other download in that process; the server imports helpers from this file, never main()",
}


def _top_level_owner(tree):
    """node → name of the top-level function or class it sits in ("<module>" otherwise)."""
    owner = {}
    for top in tree.body:
        name = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
            else "<module>"
        for node in ast.walk(top):
            owner[node] = name
    return owner


def test_no_code_calls_yfinance_download_directly():
    found = []
    for path in sorted(CODE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        owner = _top_level_owner(tree)
        for node in ast.walk(tree):
            direct = (
                (isinstance(node, ast.Attribute) and node.attr == "download"
                 and not (isinstance(node.value, ast.Name) and node.value.id == "zeus_yf"))
                or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "yfinance"
                    and any(a.name in ("download", "*") for a in node.names))
                or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == "download"))
            if direct and (rel, owner[node]) not in DIRECT_OK:
                found.append(f"{rel}:{node.lineno} in {owner[node]}()")
    assert not found, ("call zeus_yf.download(...) instead of yfinance's download (one download at a "
                       "time, see zeus/zeus_yf.py):\n  " + "\n  ".join(found))


# Calls that block on the network and may wait for a yfinance download in progress: the wrapper
# itself, and broker quote requests, which manual mode answers with a yfinance download
# (local_broker.equity_quotes_batch).
_HANDOFF = {"run_in_executor", "to_thread", "submit", "Thread", "create_task", "ensure_future"}


def _blocking(call):
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "zeus_yf" \
            and f.attr in ("download", "acquire"):
        return f"zeus_yf.{f.attr}()"
    if isinstance(f, ast.Attribute) and f.attr == "get" and call.args \
            and "market-data" in ast.unparse(call.args[0]):
        return f"client.get({ast.unparse(call.args[0])})"
    return None


def _calls(fn):
    """(name of a plain-name call or None, blocking description or None) for each call `fn`
    makes on its own thread: not in a lambda or nested def, not handed to a worker or a task."""
    out, todo = [], list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in _HANDOFF:
                todo.append(node.func)
                continue
            out.append((node.func.id if isinstance(node.func, ast.Name) else None, _blocking(node)))
        todo.extend(ast.iter_child_nodes(node))
    return out


def _chain(fn, top, seen):
    """Plain calls from `fn` that end in a blocking call, through functions of the same module
    (its own nested functions first, then the module's), or None."""
    nested = {n.name: n for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n is not fn}
    for name, blocking in _calls(fn):
        if blocking:
            return [blocking]
        target = nested.get(name) or top.get(name)
        if isinstance(target, ast.FunctionDef) and target not in seen:
            sub = _chain(target, top, seen | {target})
            if sub:
                return [name + "()"] + sub
    return None


def test_async_functions_hand_downloads_to_a_worker_thread():
    """An `async def` that reaches a download through plain calls in its own module would hold
    up the whole server while the download waits for its turn."""
    found = []
    for path in sorted(CODE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                chain = _chain(fn, top, frozenset())
                if chain:
                    found.append(f"{path.relative_to(ROOT).as_posix()}: async {fn.name}() → "
                                 + " → ".join(chain))
    assert not found, ("run these in a worker thread (await loop.run_in_executor(None, ...)):\n  "
                       + "\n  ".join(found))

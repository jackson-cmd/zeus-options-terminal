"""Ctrl+C and SIGTERM stop the server within seconds, even while long jobs run.

uvicorn stops serving at once, but Python then waits for every job still running in a
worker thread: asyncio.run() waits for its default executor and the interpreter for every
ThreadPoolExecutor thread. A cold start runs jobs that take minutes (the heatmap earnings
refresh, the Finviz crawl), so Ctrl+C used to leave the server running for minutes.
zeus_shutdown.serve() ends the process a few seconds after the signal; long jobs stop
between tickers, and the files background jobs write survive a job that is cut off.
"""
import ast
import asyncio
import datetime
import importlib
import json
import signal
import socket
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

import pytest

import zeus_shutdown

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "zeus"
SERVER = CODE / "options_analyzer" / "optionstrat_server.py"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="sends POSIX signals to a child process")


@pytest.fixture(autouse=True)
def not_stopping(monkeypatch):
    """Every test starts with no stop request and leaves none behind."""
    monkeypatch.setattr(zeus_shutdown._STATE, "since", None)
    monkeypatch.setattr(zeus_shutdown._STATE, "signal", None)


# ── the process ──────────────────────────────────────────────────────────────────────────────

APP = '''
import asyncio, sys, time
from contextlib import asynccontextmanager
sys.path.insert(0, {code!r})
import zeus_shutdown
from fastapi import FastAPI

how, port, mark = sys.argv[1], int(sys.argv[2]), sys.argv[3]


def stuck():                    # a request that never returns, like a page that stops answering
    time.sleep(600)


def long_loop():                # a refresh that checks stopping() between tickers
    for _ in range(12000):
        if zeus_shutdown.stopping():
            with open(mark, "w") as f:
                f.write("stopped early")
            return
        time.sleep(0.05)


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, stuck)
    loop.run_in_executor(None, long_loop)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/ping")
def ping():
    return "pong"


if how == "zeus":
    zeus_shutdown.serve(app, host="127.0.0.1", port=port, exit_after=2.0)
else:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path, how):
    """A server whose startup leaves a stuck job and a long loop in the default executor."""
    script = tmp_path / "app.py"
    script.write_text(APP.format(code=str(CODE)), encoding="utf-8")
    port, mark, log = _free_port(), tmp_path / "long_loop.txt", tmp_path / "server.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, str(script), how, str(port), str(mark)],
                                stdout=out, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2).close()
            return proc, mark
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                pytest.fail("the test server did not start:\n" + log.read_text(encoding="utf-8"))
            time.sleep(0.1)


@POSIX_ONLY
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
def test_server_ends_soon_after_the_signal_while_jobs_run(tmp_path, sig):
    proc, mark = _start(tmp_path, "zeus")
    start = time.monotonic()
    proc.send_signal(sig)
    try:
        code = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("the server was still running 20 s after the signal")
    took = time.monotonic() - start
    assert took < 2.0 + 4.0, f"took {took:.1f} s"
    if sig == signal.SIGINT:
        assert code == 0
        assert mark.read_text() == "stopped early"      # the long loop saw stopping() and returned
    else:
        assert code in (-signal.SIGTERM, 128 + signal.SIGTERM)


@POSIX_ONLY
def test_plain_uvicorn_run_waits_for_the_jobs(tmp_path):
    """What serve() fixes: started with uvicorn.run(), as the server was, the same app is
    still running after Ctrl+C for as long as its executor jobs run."""
    proc, _ = _start(tmp_path, "uvicorn")
    proc.send_signal(signal.SIGINT)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=2.0 + 2.0)
    finally:
        proc.kill()
        proc.wait()


def _calls(node) -> set:
    return {ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}


def test_the_server_starts_through_serve():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    main = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    assert len(main) == 1 and "zeus_shutdown.serve" in _calls(main[0])
    assert "uvicorn.run" not in _calls(tree)


def test_one_stop_request_under_two_module_names(monkeypatch):
    """zeus/ on sys.path gives `zeus_shutdown`; the repository root gives `zeus.zeus_shutdown`,
    a second module object. A stop request must reach both."""
    monkeypatch.syspath_prepend(str(ROOT))
    had_zeus = "zeus" in sys.modules
    try:
        other = importlib.import_module("zeus.zeus_shutdown")
        assert other is not zeus_shutdown
        assert not other.stopping()
        zeus_shutdown.request_stop(signal.SIGINT)
        assert other.stopping()
    finally:
        sys.modules.pop("zeus.zeus_shutdown", None)
        if not had_zeus:
            sys.modules.pop("zeus", None)


# ── long jobs stop between tickers ───────────────────────────────────────────────────────────

def test_earnings_refresh_keeps_what_it_got_and_the_old_records(tmp_path, monkeypatch):
    from heatmap import earnings
    monkeypatch.setattr(earnings, "CACHE_FILE", tmp_path / "earnings.json")
    today = datetime.date.today()
    soon = (today + datetime.timedelta(days=10)).isoformat()
    later = (today + datetime.timedelta(days=40)).isoformat()
    old = {"date": soon, "time": "pm", "src": "tastytrade",
           "fetched": (today - datetime.timedelta(days=5)).isoformat()}   # near and stale: asked again
    earnings.save({"CCC": dict(old)})
    asked = []

    def first_source(tickers, log=print):             # Ctrl+C while the first source answers
        asked.append(("tastytrade", list(tickers)))
        zeus_shutdown.request_stop(signal.SIGINT)
        return {"AAA": {"date": later, "time": "am", "src": "tastytrade"},
                "BBB": {"date": later, "time": "pm", "src": "tastytrade"}}

    monkeypatch.setattr(earnings, "_fetch_tastytrade", first_source)
    monkeypatch.setattr(earnings, "_fetch_calendar_cache", lambda log=print: {})
    monkeypatch.setattr(earnings, "_fetch_yfinance", lambda ts, log=print: asked.append(("yfinance", ts)) or {})
    monkeypatch.setattr(earnings, "_fetch_finviz", lambda ts, log=print: asked.append(("finviz", ts)) or {})

    recs = earnings.refresh(["AAA", "BBB", "CCC", "DDD"], log=lambda *_: None)

    assert [name for name, _ in asked] == ["tastytrade"]          # no other source after the stop
    saved = earnings.load()
    assert saved == recs
    assert saved["AAA"]["date"] == later and saved["BBB"]["date"] == later
    assert saved["CCC"] == old              # not replaced by "no date found": it was never looked up
    assert "DDD" not in saved               # asked again on the next start


def test_metadata_refresh_stops_between_tickers_and_keeps_progress(tmp_path, monkeypatch):
    from heatmap import metadata
    monkeypatch.setattr(metadata, "_CACHE_PATH", tmp_path / "metadata.json")
    monkeypatch.setattr(metadata, "_mem_cache", None)
    monkeypatch.setattr(metadata, "_load_tickers", lambda: [f"T{i}" for i in range(8)])
    monkeypatch.setattr(metadata, "_universe_groups", lambda: {})
    monkeypatch.setattr(metadata, "etf_set", lambda: set())
    monkeypatch.setitem(sys.modules, "zeus_finviz", types.SimpleNamespace(rows=lambda *a, **k: {}))
    monkeypatch.setattr(metadata, "_finviz_fetch_one", lambda t: None)
    asked = []

    def yahoo(t):
        asked.append(t)
        if len(asked) == 3:
            zeus_shutdown.request_stop(signal.SIGINT)
        return {"ticker": t, "name": t, "sector": "Technology", "industry": "Software", "market_cap": 1e9}

    monkeypatch.setattr(metadata, "_yf_fetch_one", yahoo)
    cache = metadata.refresh(log=False)
    assert asked == ["T0", "T1", "T2"]
    assert sorted(cache) == ["T0", "T1", "T2"]
    assert sorted(json.loads((tmp_path / "metadata.json").read_text())) == ["T0", "T1", "T2"]


def server_functions(*names, **env):
    """The named top-level functions of the server, without their decorators, in a namespace
    that holds only what they need (importing the whole server starts its background jobs)."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ns = {"zeus_shutdown": zeus_shutdown, "Path": Path, **env}
    for name in names:
        node = found[name]
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec"), ns)
    return ns


def test_finviz_crawl_stops_between_tickers_and_writes_nothing():
    asked, touched = [], []

    def news(t, hours=36, force=False, allow_fetch=True):
        asked.append(t)
        if len(asked) == 2:
            zeus_shutdown.request_stop(signal.SIGINT)
        return [{"id": t, "title_en": t}]

    srv = server_functions("_mv_gather_finviz", "_finviz_ai_crawl_once",
                           _FINVIZ_PAGE_CACHE={}, _FINVIZ_PAGE_TTL=900, _mv_finviz_news=news,
                           _finviz_ai_universe=lambda: [f"T{i}" for i in range(10)],
                           _digest_tr_load=lambda date: touched.append("translations") or {},
                           _fvai=types.SimpleNamespace(upsert_day=lambda *a: touched.append("store")),
                           asyncio=asyncio, datetime=datetime, time=time)
    got = srv["_mv_gather_finviz"]([f"T{i}" for i in range(10)], batch_pause=0)
    assert asked == ["T0", "T1"] and sorted(got) == ["T0", "T1"]

    zeus_shutdown._STATE.since = None
    asked.clear()
    original = srv["_mv_gather_finviz"]
    srv["_mv_gather_finviz"] = lambda tickers, *a, **k: original(tickers, batch_pause=0)
    assert asyncio.run(srv["_finviz_ai_crawl_once"]()) == 0
    assert asked == ["T0", "T1"] and touched == []                # no translations, no store write


# ── a write cut off by the exit leaves the previous file ─────────────────────────────────────

class Killed(BaseException):
    """The process ending in the middle of a write."""


def _writes_die_halfway(monkeypatch):
    real = Path.write_text

    def write_text(self, data, *args, **kwargs):
        real(self, data[: len(data) // 2], *args, **kwargs)
        raise Killed()

    monkeypatch.setattr(Path, "write_text", write_text)


def _metadata(tmp_path, monkeypatch):
    from heatmap import metadata
    path = tmp_path / "metadata.json"
    monkeypatch.setattr(metadata, "_CACHE_PATH", path)
    return path, lambda data: metadata._write_cache(data)


def _snapshot(tmp_path, monkeypatch):
    from heatmap import heatmap_api
    path = tmp_path / "snapshot.json"
    monkeypatch.setattr(heatmap_api, "_SNAPSHOT_PATH", path)

    def write(data):
        monkeypatch.setattr(heatmap_api, "_reg", data)
        heatmap_api._save_persisted()
    return path, write


def _calendar(tmp_path, monkeypatch):
    from trade_journal import calendar_events
    path = tmp_path / "calendar_cache.json"
    monkeypatch.setattr(calendar_events, "CACHE_PATH", path)
    monkeypatch.setattr(calendar_events, "load_watchlist", lambda: ["AAA"])
    monkeypatch.setattr(calendar_events, "fetch_econ_events", lambda: [])

    def write(data):
        monkeypatch.setattr(calendar_events, "fetch_earnings", lambda tickers: [data])
        calendar_events.refresh()
    return path, write


def _alerts_seen(tmp_path, monkeypatch):
    sys.path.insert(0, str(CODE / "options_analyzer"))
    import credit_spread_alert
    path = tmp_path / "credit_spread_seen.json"
    return path, lambda data: credit_spread_alert.save_seen(path, data["tickers"])


def _server_state(tmp_path, monkeypatch):           # AI summaries, popup and notification state
    path = tmp_path / "state" / "ai_summary.json"
    srv = server_functions("_write_text_atomic", os=__import__("os"), json=json)
    return path, lambda data: srv["_write_text_atomic"](path, json.dumps(data))


@pytest.mark.parametrize("target", [_metadata, _snapshot, _calendar, _alerts_seen, _server_state],
                         ids=["heatmap metadata", "heatmap snapshot", "earnings calendar",
                              "credit spread alerts", "server state files"])
def test_a_write_cut_off_by_the_exit_leaves_the_previous_file(tmp_path, monkeypatch, target):
    path, write = target(tmp_path, monkeypatch)
    write({"tickers": ["OLD"] * 50})
    before = path.read_text(encoding="utf-8")
    json.loads(before)
    _writes_die_halfway(monkeypatch)
    with pytest.raises(Killed):
        write({"tickers": ["NEW"] * 50})
    assert path.read_text(encoding="utf-8") == before

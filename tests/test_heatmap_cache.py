"""The slow heatmap tables (涨跌榜 stock_perf, ETF trends, risk stats) keep their last good
data when a yfinance download fails.

On 2026-11-13 the server started all three refreshes at once, one download failed with
"dictionary changed size during iteration", and stock_perf.json was written with 0 tickers
and kept for a day. etf_trends.json, which the market brief also reads, could go empty the
same way. yfinance is replaced here by a fake module, and the clock of the cache by a fake one."""
import json
import sys
import threading
import time
import types

import numpy as np
import pandas as pd
import pytest

from heatmap import etf_trends, metadata, risk_stats, stock_perf
from heatmap import table_cache as tc

STOCKS = ["AAA", "BBB"]
ETFS = [("SECTOR", "XLK", "Technology"), ("GLOBAL", "EWJ", "Japan")]


def _frame(tickers):
    """What yf.download returns for daily bars: a ("Close", ticker) column per ticker."""
    idx = pd.bdate_range(end="2026-11-13", periods=300)
    rng = np.random.default_rng(7)
    close = pd.DataFrame({t: 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))) for t in tickers},
                         index=idx)
    return pd.concat({"Close": close}, axis=1)


class FakeYF:
    def __init__(self):
        self.fail = False
        self.delay = 0.0
        self.calls = 0
        self.active = 0
        self.peak = 0          # most downloads running at the same time
        self._lock = threading.Lock()

    def download(self, tickers, **kwargs):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise RuntimeError("dictionary changed size during iteration")
            return _frame([tickers] if isinstance(tickers, str) else list(tickers))
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture()
def yf(monkeypatch):
    fake = FakeYF()
    module = types.ModuleType("yfinance")
    module.download = fake.download
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return fake


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(tc, "time", types.SimpleNamespace(time=lambda: now["t"]))
    return now


@pytest.fixture(autouse=True)
def tables(tmp_path, monkeypatch):
    """Small universes, cache files in tmp_path, empty memory caches."""
    monkeypatch.setattr(metadata, "tickers", lambda: list(STOCKS))
    monkeypatch.setattr(metadata, "etf_set", lambda: set())
    monkeypatch.setattr(metadata, "get_all", lambda: {})
    monkeypatch.setattr(etf_trends, "_parse_list", lambda: list(ETFS))
    for mod in (stock_perf, etf_trends, risk_stats):
        monkeypatch.setattr(mod, "_CACHE", tmp_path / mod._CACHE.name)
    monkeypatch.setattr(stock_perf, "_state", {})
    monkeypatch.setattr(etf_trends, "_state", {})
    monkeypatch.setattr(risk_stats, "_state_by", {"stocks": {}, "etf": {}})


# name → (get, cache file, TTL in seconds)
TABLES = {
    "stock_perf": (stock_perf.get, lambda: stock_perf._CACHE, stock_perf._CACHE_TTL),
    "etf_trends": (etf_trends.get, lambda: etf_trends._CACHE, etf_trends._CACHE_TTL),
    "risk_stats": (risk_stats.get, lambda: risk_stats._cache_path("stocks"), risk_stats._CACHE_TTL),
    "risk_stats_etf": (lambda **kw: risk_stats.get(universe="etf", **kw),
                       lambda: risk_stats._cache_path("etf"), risk_stats._CACHE_TTL),
}


@pytest.fixture(params=sorted(TABLES))
def table(request, tables):
    get, path, ttl = TABLES[request.param]
    return types.SimpleNamespace(get=get, path=path(), ttl=ttl)


def test_failed_refresh_keeps_the_good_table(table, yf, clock):
    good = table.get()
    assert tc.has_data(good)
    on_disk = table.path.read_text(encoding="utf-8")
    yf.fail = True
    again = table.get(force=True)            # the Refresh button, or the next day's refresh
    assert yf.calls == 2
    assert again["items"] == good["items"]
    assert table.path.read_text(encoding="utf-8") == on_disk


def test_stale_table_is_served_until_a_refresh_works(table, yf, clock):
    good = table.get()
    clock["t"] += table.ttl + 1                # the table is now older than its TTL
    yf.fail = True
    assert table.get()["items"] == good["items"]
    assert yf.calls == 2
    clock["t"] += tc.RETRY_SECONDS - 1
    assert table.get()["items"] == good["items"]
    assert yf.calls == 2                       # no new download inside the retry window
    clock["t"] += 2
    yf.fail = False
    fresh = table.get()
    assert yf.calls == 3 and fresh["_ts"] == clock["t"]
    assert json.loads(table.path.read_text(encoding="utf-8"))["_ts"] == clock["t"]


def test_without_a_good_table_the_empty_result_lasts_five_minutes(table, yf, clock):
    yf.fail = True
    assert not tc.has_data(table.get())
    assert not table.path.exists()             # an empty table never reaches the disk
    clock["t"] += tc.RETRY_SECONDS - 1
    table.get()
    assert yf.calls == 1
    clock["t"] += 2
    yf.fail = False
    assert tc.has_data(table.get())
    assert yf.calls == 2 and table.path.exists()


def test_an_empty_table_already_on_disk_is_not_served(table, yf, clock):
    # what older versions wrote after a failed download
    table.path.write_text(json.dumps({"_ts": clock["t"], "count": 0, "items": []}), encoding="utf-8")
    assert tc.has_data(table.get())
    assert yf.calls == 1


def test_the_tables_refresh_one_at_a_time(yf, clock):
    yf.delay = 0.05
    jobs = [lambda: stock_perf.get(force=True), lambda: etf_trends.get(force=True),
            lambda: risk_stats.get(force=True), lambda: risk_stats.get(force=True, universe="etf")]
    threads = [threading.Thread(target=job) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert yf.calls == 4 and yf.peak == 1


def test_a_waiting_request_uses_the_table_refreshed_meanwhile(yf, clock):
    yf.delay = 0.05
    threads = [threading.Thread(target=stock_perf.get) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert yf.calls == 1


def test_an_error_outside_the_download_falls_back_or_is_raised(tmp_path, clock):
    path, state = tmp_path / "t.json", {}

    def broken():
        raise ValueError("metadata file unreadable")

    with pytest.raises(ValueError):
        tc.get(state, path, 60, broken)       # nothing to fall back on
    good = tc.get(state, path, 60, lambda: {"items": [{"ticker": "AAA"}]})
    assert tc.get(state, path, 60, broken, force=True) == good

"""yfinance 0.2.48 and later return (Price, Ticker) columns even when download() gets one ticker,
so frame["Close"] is a frame with one column, not a series. The positions IV fallback took
float() of that column's std(): pandas 2 only warned, pandas 3 raises TypeError, and the
`except Exception` around it hid the error, so positions without an IV from the broker silently
went without one. The correlation of SPY alone came back empty for the same reason.

The functions are taken from the two files with ast and run on their own, because importing the
server starts its background jobs; yfinance is a fake that returns one-ticker frames shaped like
the real ones. pandas 2's warning is turned into an error, so the old code fails here on pandas 2
as well as on pandas 3."""
import ast
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYZER = ROOT / "zeus" / "options_analyzer" / "optionstrat_analyzer.py"
SERVER = ROOT / "zeus" / "options_analyzer" / "optionstrat_server.py"

pytestmark = pytest.mark.filterwarnings("error:Calling .* on a single element Series:FutureWarning")

DAYS = 60
CLOSES = 100 * np.cumprod(1 + 0.02 * np.sin(np.arange(DAYS)))
EXPECTED_VOL = float(np.std(CLOSES[1:] / CLOSES[:-1] - 1, ddof=1) * math.sqrt(252))


def functions(path, *names, **env):
    """The named top-level functions of a file, without their decorators, in a namespace that
    holds only what they need."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ns = {"math": math, **env}
    for name in names:
        node = found[name]
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    return ns


historical_vol = functions(ANALYZER, "historical_vol")["historical_vol"]


def download_frame(ticker, closes=CLOSES, group_by="column"):
    """Daily bars the way yf.download returns them for one ticker: (Price, Ticker) columns, or
    (Ticker, Price) with group_by="ticker"."""
    idx = pd.bdate_range(end="2026-11-13", periods=len(closes))
    cols = {}
    for field in ("Open", "High", "Low", "Close", "Volume"):
        values = np.asarray(closes, dtype=float) * (1000 if field == "Volume" else 1)
        cols[(ticker, field) if group_by == "ticker" else (field, ticker)] = values
    frame = pd.DataFrame(cols, index=idx)
    frame.columns.names = ["Ticker", "Price"] if group_by == "ticker" else ["Price", "Ticker"]
    return frame


def history_frame(closes=CLOSES):
    """Daily bars the way Ticker.history returns them: one level of columns."""
    return download_frame("X", closes).droplevel("Ticker", axis=1)


class FakeYF:
    """download() of one ticker → a frame shaped like yfinance's; records the tickers asked for."""

    def __init__(self, frames=None, fail=()):
        self.frames = frames or {}
        self.fail = set(fail)
        self.calls = []

    def download(self, tickers, group_by="column", **kwargs):
        self.calls.append(tickers)
        names = tickers.split() if isinstance(tickers, str) else list(tickers)
        if any(t in self.fail for t in names):
            raise RuntimeError("Yahoo Finance did not answer")
        if len(names) == 1 and names[0] in self.frames:
            return self.frames[names[0]]
        return pd.concat([download_frame(t, group_by=group_by) for t in names], axis=1)


# ── the IV estimate ─────────────────────────────────────────────────────────────────────────

def test_the_iv_estimate_reads_a_one_ticker_download():
    assert historical_vol(download_frame("AMD")) == pytest.approx(EXPECTED_VOL)


def test_the_iv_estimate_reads_one_level_of_columns_as_before():
    assert historical_vol(history_frame()) == pytest.approx(EXPECTED_VOL)


def test_missing_closes_are_skipped():
    closes = CLOSES.copy()
    closes[[10, 30]] = np.nan
    kept = closes[~np.isnan(closes)]
    expected = float(np.std(kept[1:] / kept[:-1] - 1, ddof=1) * math.sqrt(252))
    assert historical_vol(download_frame("AMD", closes)) == pytest.approx(expected)


@pytest.mark.parametrize("hist", [
    None,
    pd.DataFrame(),
    download_frame("AMD", CLOSES[:2]),                  # one daily return: no spread to measure
    download_frame("AMD", [np.nan] * 5),
    download_frame("AMD").drop(columns="Close", level="Price"),
], ids=["nothing", "empty", "two days", "no closes", "no Close column"])
def test_too_little_data_gives_no_estimate(hist):
    assert historical_vol(hist) is None


# ── the server: positions IV fallback and correlation with SPY ─────────────────────────────

def server(fake, *names):
    return functions(SERVER, *names, HAS_YF=True, zeus_yf=fake, historical_vol=historical_vol)


def test_positions_without_a_broker_iv_get_one_from_a_one_ticker_download():
    fake = FakeYF(frames={"DDD": download_frame("DDD").iloc[:0]}, fail={"CCC"})
    metrics = {"BBB": {"iv": 0.5, "earnings_date": "2026-11-20", "earnings_time": "AMC"},
               "EEE": {"iv": 0.0, "earnings_date": "2026-12-01", "earnings_time": "BMO"}}
    server(fake, "_fill_iv_from_history")["_fill_iv_from_history"](
        ["AAA", "BBB", "CCC", "DDD", "EEE"], metrics)

    assert fake.calls == ["AAA", "CCC", "DDD", "EEE"]          # one ticker per download
    assert metrics["AAA"] == {"iv": pytest.approx(EXPECTED_VOL), "earnings_date": "",
                              "earnings_time": ""}
    assert metrics["BBB"] == {"iv": 0.5, "earnings_date": "2026-11-20", "earnings_time": "AMC"}
    assert metrics["EEE"] == {"iv": pytest.approx(EXPECTED_VOL), "earnings_date": "2026-12-01",
                              "earnings_time": "BMO"}
    assert "CCC" not in metrics and "DDD" not in metrics     # failed / empty download: no guess


def test_the_correlation_of_spy_alone_is_one():
    """The overview's correlation chart shows SPY against itself as its reference bar."""
    fake = FakeYF()
    srv = server(fake, "_fetch_yf_single_close", "_compute_corr_vs_spy_batch")
    assert srv["_compute_corr_vs_spy_batch"](["SPY"]) == {"SPY": 1.0}
    assert fake.calls == ["SPY"]

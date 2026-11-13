"""The ticker list the TA screener scans, on a fresh install.

The first start seeds data/ta_screener_universe.txt from the default list (never over a
list you already have). Before that file was seeded, the auto-scan on an empty ZEUS_HOME
died in its thread with FileNotFoundError; the scan now falls back to the heatmap list and
then to the default list when the file is missing."""
from pathlib import Path

import pytest

import zeus_config as zc
from ai_picks import features as ai_features
from options_analyzer.options_vol import sync_consumers
from ta_screener import data as ta_data
from ta_screener import scanner

DEFAULT_LIST = Path(__file__).resolve().parent.parent / "zeus" / "defaults" / "heatmap_universe.txt"


def default_tickers():
    out = []
    for tok in DEFAULT_LIST.read_text(encoding="utf-8").replace("\n", ",").split(","):
        tok = tok.strip()
        sym = tok.split(":")[-1].upper().replace(".", "-")
        if tok and not tok.startswith("###") and sym not in out:
            out.append(sym)
    return out


@pytest.fixture()
def fresh_home(tmp_path, monkeypatch):
    """An empty data folder, with the seeding and every reader of the lists pointed into it."""
    data = tmp_path / "data"

    def data_dir(*parts):
        p = data.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    monkeypatch.setattr(zc, "data_dir", data_dir)
    monkeypatch.setattr(ta_data, "UNIVERSE_FILE", data / "ta_screener_universe.txt")
    monkeypatch.setattr(ta_data, "POOL_FILE", data / "ta_screener_pool.txt")
    monkeypatch.setattr(ta_data, "_CATEGORY_SOURCE", data / "heatmap_universe.txt")
    monkeypatch.setattr(ta_data, "_SECTOR_SOURCE", data / "heatmap" / "metadata.json")
    monkeypatch.setattr(ai_features, "UNIVERSE_FILE", data / "ta_screener_universe.txt")
    data.mkdir()
    return data


def test_first_start_seeds_the_ta_list(fresh_home):
    zc.ensure_default_data()
    seeded = (fresh_home / "ta_screener_universe.txt").read_text(encoding="utf-8")
    tokens = seeded.split(",")
    assert "###" not in seeded and "\n" not in seeded and all(":" in t for t in tokens)

    tickers, categories = ta_data.parse_universe()
    assert tickers == default_tickers() and "SPY" in tickers     # SPY gives the market regime
    assert all(categories[t] for t in tickers)                    # borrowed from heatmap_universe.txt
    assert ai_features.parse_universe() == tickers                # AI picks read the same file

    # The same line the options-vol sync writes from the seeded core list.
    core = (fresh_home / "options_vol" / "core_universe.txt").read_text(encoding="utf-8").split()
    exchanges = {}
    for tok in (fresh_home / "heatmap_universe.txt").read_text(encoding="utf-8").replace("\n", ",").split(","):
        tok = tok.strip()
        if ":" in tok and not tok.startswith("###"):
            exchanges.setdefault(sync_consumers._norm(tok), tok)
    assert seeded == ",".join(sync_consumers._exch_token(t, exchanges) for t in core)


def test_seeding_keeps_your_own_ta_list(fresh_home):
    (fresh_home / "ta_screener_universe.txt").write_text("NASDAQ:AAPL,NYSE:KO", encoding="utf-8")
    zc.ensure_default_data()
    assert (fresh_home / "ta_screener_universe.txt").read_text(encoding="utf-8") == "NASDAQ:AAPL,NYSE:KO"
    assert ta_data.parse_universe()[0] == ["AAPL", "KO"]


def test_missing_ta_list_falls_back_to_the_heatmap_list_then_the_defaults(fresh_home):
    assert ta_data.parse_universe()[0] == default_tickers()       # nothing in the data folder yet

    (fresh_home / "heatmap_universe.txt").write_text("###Mine,NASDAQ:AAPL,NYSE:BRK.B", encoding="utf-8")
    assert ta_data.parse_universe() == (["AAPL", "BRK-B"], {"AAPL": "Mine", "BRK-B": "Mine"})

    (fresh_home / "ta_screener_universe.txt").write_text(" \n", encoding="utf-8")   # no tickers
    assert ta_data.parse_universe()[0] == ["AAPL", "BRK-B"]

    (fresh_home / "ta_screener_universe.txt").write_bytes(b"\xff\xfeA\x00")        # not UTF-8
    assert ta_data.parse_universe()[0] == ["AAPL", "BRK-B"]


def test_scan_on_an_empty_home_reaches_the_download(fresh_home, monkeypatch):
    """run_scan is what the 09:40 / 10:30 / 12:00 / 15:00 auto-scan thread runs."""
    asked = {}

    class Downloading(Exception):
        pass

    def download_history(tickers, progress_cb=None):
        asked["tickers"] = list(tickers)
        raise Downloading

    monkeypatch.setattr(ta_data, "download_history", download_history)
    monkeypatch.setattr(scanner, "_maybe_rebuild_pool", lambda log=print: None)
    with pytest.raises(Downloading):
        scanner.run_scan(log=lambda *a, **k: None)
    assert asked["tickers"] == default_tickers()

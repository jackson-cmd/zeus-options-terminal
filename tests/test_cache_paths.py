"""Cache files are named from values that arrive with a request (a date, a ticker, a scope, a
kind). None of them may lead outside the cache folder, on macOS, Linux or Windows: a page on
another site can make the browser send a GET to the local server, and /api/stock_selector used
to write its result to a path taken from ?today=.

The server functions are taken from optionstrat_server.py with ast and run on their own,
because importing the whole server starts its background jobs."""
import ast
import asyncio
import datetime
import sys
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "zeus" / "options_analyzer" / "optionstrat_server.py"
sys.path.insert(0, str(ROOT / "zeus" / "options_analyzer"))

TRICKY = ["../../../tmp/zeus-x", "/tmp/zeus-x", "..\\..\\zeus-x", "C:\\Users\\Public\\zeus-x",
          "C:zeus-x", "2026-11-12/../../x", "2026-11-12\0x"]
BAD_DAYS = TRICKY + ["", "today", "2026-13-01", "2026-11-31"]


def server_functions(*names, **env):
    """The named top-level functions of the server, without their decorators, in a namespace
    that holds only what they need."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ns = {"datetime": datetime, "Path": Path, "Optional": Optional, "__file__": str(SERVER), **env}
    from fastapi import Query
    ns["Query"] = Query
    for name in names:
        node = found[name]
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec"), ns)
    return ns


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cache")
    return server_functions("_safe_day", "_safe_part", "_ss_cache_path", "_aisum_path", "_pivot_path",
                            "_digest_tr_path", "_digest_premarket_text", "api_stock_selector",
                            _ss_cache_dir=tmp / "stock_selector_cache", _DIGEST_DIR=tmp / "digests")


def test_dates_are_rebuilt_or_refused(srv):
    assert srv["_safe_day"]("2026-11-12") == "2026-11-12"
    for bad in BAD_DAYS:
        with pytest.raises(ValueError):
            srv["_safe_day"](bad)


def test_names_lose_every_path_character(srv):
    for bad in TRICKY:
        name = srv["_safe_part"](bad)
        assert not any(c in name for c in "/\\:\0") and ".." not in name, name


def test_cache_paths_keep_their_old_names_and_folders(srv):
    """Ordinary values give the same file names as before, so existing caches stay valid."""
    ai = srv["_aisum_path"]("2026-11-12")
    assert ai.name == "2026-11-12.json" and ai.parent.name == "ai_summary"
    assert srv["_aisum_path"]("2026-11-12", "movers").name == "2026-11-12__movers.json"
    assert srv["_aisum_path"]("adhoc", "a1b2").name == "adhoc__a1b2.json"
    pv = srv["_pivot_path"]("BRK/B", "2026-11-12", "high")
    assert pv.name == "BRK_B__2026-11-12__high.json" and pv.parent.name == "pivot_summary"
    tr = srv["_digest_tr_path"]("2026-11-12")
    assert tr == srv["_DIGEST_DIR"] / "2026-11" / ".2026-11-12.tr.json"
    ss = srv["_ss_cache_path"]("2026-11-12", True)
    assert ss == srv["_ss_cache_dir"] / "2026-11-12_nosonnet.json"


def test_request_values_never_leave_the_cache_folder(srv):
    ai_dir = srv["_aisum_path"]("2026-11-12").parent
    pv_dir = srv["_pivot_path"]("SPY", "2026-11-12", "low").parent
    for bad in TRICKY:
        assert srv["_aisum_path"]("2026-11-12", bad).parent == ai_dir
        assert srv["_pivot_path"](bad, "2026-11-12", bad).parent == pv_dir
    for bad in BAD_DAYS:
        for build in (lambda d: srv["_aisum_path"](d), lambda d: srv["_aisum_path"](d, "s"),
                      lambda d: srv["_pivot_path"]("SPY", d, "low"), srv["_digest_tr_path"],
                      lambda d: srv["_ss_cache_path"](d, False)):
            with pytest.raises(ValueError):
                build(bad)
        assert srv["_digest_premarket_text"](bad) == ""


def test_stock_selector_refuses_a_path_in_today(srv):
    from fastapi import HTTPException
    for bad in TRICKY + ["today"]:
        with pytest.raises(HTTPException) as e:
            asyncio.run(srv["api_stock_selector"](today=bad, no_sonnet=False, refresh=False))
        assert e.value.status_code == 400
    assert not srv["_ss_cache_dir"].exists()          # refused before anything was read or written


def test_day_stores_refuse_paths():
    from benzinga_mcp import digest_store, finviz_ai_store
    for mod in (digest_store, finviz_ai_store):
        assert mod.plain_path("2026-11-12").name == ".2026-11-12.json"
        assert mod.plain_path("2026-11-12").parent.name == "2026-11"
        for bad in BAD_DAYS:
            assert mod.load_day(bad) is None and mod.day_path(bad) is None
            with pytest.raises(ValueError):
                mod.plain_path(bad)


def test_ai_picks_and_must_read_refuse_paths():
    import mustread
    from ai_picks import store
    for bad in BAD_DAYS:
        assert store.load(bad) is None
        assert mustread.overlay_load(bad) == {}
        with pytest.raises(ValueError):
            mustread.overlay_path(bad)
    assert store._path("2026-11-12").name == "2026-11-12.json"


def test_market_events_archive_kind_is_a_plain_name():
    from benzinga_market_events import market_events_api as me
    for bad in TRICKY + ["", "Earnings"]:
        assert me._load_archive(bad) is None


def test_news_links_lose_tracking_only_on_their_own_hosts():
    import zeus_news
    drop = zeus_news._drop_tracking
    assert drop("https://www.wsj.com/articles/x?mod=rss") == "https://www.wsj.com/articles/x"
    assert drop("https://www.marketwatch.com/story/y?mod=mw_rss") == "https://www.marketwatch.com/story/y"
    assert drop("https://wsj.com/a?b=1") == "https://wsj.com/a"
    for kept in ("https://evil.example/?u=wsj.com", "https://wsj.com.evil.example/a?b=1",
                 "https://notmarketwatch.com/a?b=1", "https://finance.yahoo.com/x?y=1"):
        assert drop(kept) == kept

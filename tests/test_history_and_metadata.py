"""History search without a Benzinga archive, and the company-data cache."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zeus" / "options_analyzer"))
import history_news_api as hn  # noqa: E402


def _ms(y, m, d):
    return int(datetime(y, m, d, 15, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture()
def free_store(tmp_path, monkeypatch):
    store = tmp_path / "bilingual_news.json"
    arts = [
        {"url": "https://x/1", "title": "Apple shares rise after iPhone launch", "teaser": "", "source": "WSJ",
         "section": "Markets", "ts": _ms(2026, 11, 10), "date": "2026-11-10", "titleZh": "苹果股价上涨"},
        {"url": "https://x/2", "title": "Traders pile into $AAPL calls", "teaser": "Options volume spikes",
         "source": "Finviz", "section": "Blogs", "ts": _ms(2026, 11, 11), "date": "2026-11-11"},
        {"url": "https://x/3", "title": "Pineapple prices hit record", "teaser": "", "source": "MarketWatch",
         "section": "Top", "ts": _ms(2026, 11, 11), "date": "2026-11-11"},
        {"url": "https://x/4", "title": "Apple deleted story", "teaser": "", "source": "WSJ", "section": "Tech",
         "ts": _ms(2026, 11, 9), "date": "2026-11-09"},
    ]
    store.write_text(json.dumps({"articles": arts, "deleted": {"https://x/4": 1}}), encoding="utf-8")
    monkeypatch.setattr(hn, "_FREE_STORE", store)
    monkeypatch.setattr(hn, "_ARCHIVE_DIR", tmp_path / "no_archive")
    monkeypatch.setattr(hn, "_company_name", lambda t: "Apple" if t == "AAPL" else "")
    return store


def test_free_search_matches_symbol_and_company(free_store):
    r = hn._history_news_search(ticker="aapl")
    assert r["archive"] is False and "Free mode" in r["note"]
    assert [i["title"] for i in r["items"]] == ["Traders pile into $AAPL calls", "Apple shares rise after iPhone launch"]
    assert r["items"][0]["tickers"] == ["AAPL"] and r["total"] == 2 and not r["has_more"]


def test_free_search_filters(free_store):
    assert hn._history_news_search(keyword="pineapple")["total"] == 1
    assert hn._history_news_search(keyword="苹果")["total"] == 1                 # Chinese title counts too
    assert hn._history_news_search(ticker="AAPL", sentiment="bull")["total"] == 0
    assert hn._history_news_search(ticker="AAPL", since="2026-11-11")["total"] == 1
    assert hn._history_news_search(ticker="AAPL", until="2026-11-11")["total"] == 1
    page = hn._history_news_search(ticker="AAPL", limit=1)
    assert len(page["items"]) == 1 and page["has_more"]
    with pytest.raises(ValueError):
        hn._history_news_search()


def test_free_article_and_routes(free_store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(hn.router)
    c = TestClient(app)
    r = c.get("/api/history-news/search", params={"ticker": "AAPL"})
    assert r.status_code == 200 and r.json()["total"] == 2
    art_id = r.json()["items"][1]["id"]
    a = c.get(f"/api/history-news/article/{art_id}").json()
    assert "苹果股价上涨" in a["body"] and "https://x/1" in a["body"]
    assert c.get("/api/history-news/article/12345").status_code == 404


def test_company_name_trimming(monkeypatch):
    import heatmap.metadata as md
    monkeypatch.setattr(md, "get_all", lambda: {"DIS": {"name": "The Walt Disney Co"}, "GOOGL": {"name": "Alphabet Inc. Class A"},
                                                "V": {"name": "Visa Inc"}, "IBM": {"name": "IBM"}})
    assert hn._company_name("DIS") == "Walt Disney"
    assert hn._company_name("GOOGL") == "Alphabet"
    assert hn._company_name("V") == "Visa"
    assert hn._company_name("IBM") == ""                                         # same as the symbol → no extra pattern


def test_metadata_cache_saved_after_finviz_fill(tmp_path, monkeypatch):
    import heatmap.metadata as md
    import zeus_finviz
    monkeypatch.setattr(md, "_CACHE_PATH", tmp_path / "metadata.json")
    monkeypatch.setattr(md, "_load_tickers", lambda: ["AAPL", "SPY"])
    monkeypatch.setattr(md, "etf_set", lambda: {"SPY"})
    monkeypatch.setattr(md, "_universe_groups", lambda: {"SPY": "Index ETFs"})
    monkeypatch.setattr(zeus_finviz, "rows", lambda tickers, max_age=0: {
        "AAPL": {"name": "Apple Inc", "sector": "Technology", "industry": "Consumer Electronics", "market_cap": 3e12},
        "SPY": {"name": "SPDR S&P 500 ETF", "sector": "", "industry": "", "market_cap": 0}})
    out = md.refresh(log=False)
    saved = json.loads((tmp_path / "metadata.json").read_text())
    assert saved["AAPL"]["sector"] == "Technology" and saved["SPY"]["sector"] == "Index ETFs"
    assert out["AAPL"]["name"] == "Apple Inc"


def test_all_caps_company_name_matches_any_case(free_store, monkeypatch):
    import json as _j
    st = _j.loads(free_store.read_text())
    st["articles"].append({"url": "https://x/5", "title": "Nvidia unveils new chip", "teaser": "", "source": "WSJ",
                           "section": "Tech", "ts": _ms(2026, 11, 11), "date": "2026-11-11"})
    free_store.write_text(_j.dumps(st))
    monkeypatch.setattr(hn, "_company_name", lambda t: {"AAPL": "Apple", "NVDA": "NVIDIA"}.get(t, ""))
    assert hn._history_news_search(ticker="NVDA")["total"] == 1
    assert hn._history_news_search(keyword="apple")["total"] == 2     # substring, any case, like the archive SQL

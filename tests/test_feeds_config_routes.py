"""Finviz / news parsing, settings secrecy, HTTP guards, privacy patterns."""
import json
import os
import stat
from datetime import datetime

import pytest

import zeus_config as zc
import zeus_finviz as fv
import zeus_news as zn


# --------------------------------------------------------------------------- Finviz
FINVIZ_ROW = (
    '<div data-ticker="{t}"><a>{t}</a></div>'
    '<div data-column-id="company" role="gridcell">{name}</div>'
    '<div data-column-id="sector" role="gridcell">{sector}</div>'
    '<div data-column-id="industry" role="gridcell">Semiconductors</div>'
    '<div data-column-id="country" role="gridcell">USA</div>'
    '<div data-column-id="marketCap" role="gridcell">{cap}</div>'
    '<div data-column-id="PE" role="gridcell">21.96</div>'
    '<div data-column-id="price" role="gridcell">{price}</div>'
    '<div data-column-id="change" role="gridcell">{chg}</div>'
    '<div data-column-id="volume" role="gridcell">339,233,268</div>'
)


def test_finviz_parse_rows():
    page = FINVIZ_ROW.format(t="NVDA", name="NVIDIA Corp", sector="Technology", cap="4186.89B", price="173.73",
                             chg="2.87%") + FINVIZ_ROW.format(t="SPY", name="SPDR S&amp;P 500", sector="",
                                                               cap="-", price="759.81", chg="-1.00%")
    rows = fv._parse(page)
    assert rows["NVDA"]["market_cap"] == pytest.approx(4186.89e9)
    assert rows["NVDA"]["prev_close"] == pytest.approx(173.73 / 1.0287, rel=1e-4)
    assert rows["SPY"]["name"] == "SPDR S&P 500" and rows["SPY"]["market_cap"] == 0.0
    assert rows["SPY"]["change_pct"] == pytest.approx(-1.0)


@pytest.mark.parametrize("s,v", [("932.05B", 932.05e9), ("1.53T", 1.53e12), ("12.3M", 12.3e6), ("-", 0.0), ("", 0.0)])
def test_parse_cap(s, v):
    assert fv.parse_cap(s) == pytest.approx(v)


def test_finviz_rows_retry_missing(monkeypatch):
    calls = []

    def fake_fetch(chunk):
        calls.append(list(chunk))
        # first pass drops SPY, second pass (SPY alone) returns it
        return {t: {"ticker": t, "last": 1.0, "prev_close": 1.0, "change_pct": 0.0} for t in chunk
                if not (t == "SPY" and len(chunk) > 1)}
    fv._cache.clear()
    monkeypatch.setattr(fv, "_fetch", fake_fetch)
    out = fv.rows(["AAPL", "SPY"], max_age=0)
    assert set(out) == {"AAPL", "SPY"} and calls[-1] == ["SPY"]


# --------------------------------------------------------------------------- news
def test_finviz_time_parsing():
    now = datetime(2026, 11, 11, 15, 0, tzinfo=zn._ET)
    assert zn._finviz_time("10:13AM", now).hour == 10
    assert zn._finviz_time("10:13PM", now).day == 10           # later than now → yesterday
    assert zn._finviz_time("Nov-09", now).day == 9


def test_rss_parse(monkeypatch):
    xml = ("<?xml version='1.0'?><rss><channel>"
           "<item><title>Stocks &amp; bonds rally</title><link>https://www.wsj.com/a?mod=rss</link>"
           "<description>&lt;p&gt;Teaser&lt;/p&gt;</description><pubDate>Wed, 11 Nov 2026 14:00:00 GMT</pubDate></item>"
           "</channel></rss>")
    monkeypatch.setattr(zn, "_get", lambda url, timeout=20: xml)
    items = zn._rss("https://example.com/rss", "WSJ", "Markets")
    assert items[0]["title"] == "Stocks & bonds rally" and items[0]["teaser"] == "Teaser"
    assert items[0]["url"] == "https://www.wsj.com/a" and items[0]["date"] == "2026-11-11"


def test_refresh_merges_and_keeps_read_state(monkeypatch):
    art = {"url": "https://x/1", "title": "Oil jumps", "teaser": "", "source": "WSJ", "section": "Markets",
           "ts": int(datetime.now().timestamp() * 1000), "date": "2026-11-11"}
    monkeypatch.setitem(zn.FETCHERS, "wsj", lambda: [art])
    monkeypatch.setattr(zn, "enabled_sources", lambda: ["wsj"])
    monkeypatch.setattr(zn, "_translate", lambda texts: ["中文" for _ in texts])
    st, rep = zn.refresh({"articles": [], "seen": {"https://x/1": 1}, "saved": {}, "deleted": {}})
    assert rep["new"] == 1 and st["articles"][0]["titleZh"] == "中文" and st["seen"] == {"https://x/1": 1}
    st2, rep2 = zn.refresh(st)
    assert rep2["new"] == 0 and len(st2["articles"]) == 1


# --------------------------------------------------------------------------- settings
def test_secrets_are_write_only_and_file_is_private():
    zc.update({"tt_refresh_token": "abc-secret-value", "port": "9999", "not_a_key": "x"})
    view = zc.public_view()["settings"]
    assert view["tt_refresh_token"] == {**view["tt_refresh_token"], "is_set": True}
    assert "value" not in view["tt_refresh_token"]
    assert "abc-secret-value" not in json.dumps(zc.public_view())
    assert view["port"]["value"] == "9999"
    mode = stat.S_IMODE(os.stat(zc.config_path()).st_mode)
    assert mode == 0o600
    assert "not_a_key" not in json.loads(zc.config_path().read_text())
    zc.update({"tt_refresh_token": "", "port": ""})
    assert zc.broker_mode() == "manual"


def test_env_override(monkeypatch):
    monkeypatch.setenv("ZEUS_FINVIZ_ELITE_TOKEN", "from-env")
    assert zc.get("finviz_elite_token") == "from-env"


# --------------------------------------------------------------------------- HTTP guards
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import zeus_routes
    app = FastAPI()
    app.include_router(zeus_routes.router)
    return TestClient(app)


def test_write_requires_header_and_same_origin(client):
    assert client.post("/api/settings", json={"values": {}}).status_code == 403
    r = client.post("/api/settings", json={"values": {}}, headers={"X-Zeus-Client": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/settings", json={"values": {"benzinga_password": "p@ss"}}, headers={"X-Zeus-Client": "1"})
    assert r.status_code == 200 and "p@ss" not in r.text


def test_trades_import_list_delete(client):
    import local_broker as lb
    lb.save_trades([])
    h = {"X-Zeus-Client": "1"}
    r = client.post("/api/trades/import", json={"csv": lb.template_csv()}, headers=h).json()
    assert r["ok"] and r["added"] == 5
    trades = client.get("/api/trades").json()["trades"]
    assert len(trades) == 5
    assert client.delete(f"/api/trades/{trades[0]['id']}", headers=h).json()["ok"]
    assert client.get("/api/trades/template.csv").text.startswith("date,symbol")
    lb.save_trades([])


# --------------------------------------------------------------------------- privacy patterns
def test_privacy_patterns():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("pc", Path(__file__).resolve().parent.parent / "tools" / "privacy_check.py")
    pc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pc)
    import re
    assert re.search(pc.SECRET_PATTERNS["GitHub token"], "ghp_" + "a" * 36)
    assert re.search(pc.SECRET_PATTERNS["Google API key"], "AIza" + "b" * 35)
    assert pc.BAD_NAMES.search("trade_journal/data/trades_raw.json")
    assert pc.BAD_NAMES.search("config/.env") and not pc.BAD_NAMES.search("local_broker.py")


def test_privacy_check_allows_handle_only_in_github_urls(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("pc2", Path(__file__).resolve().parent.parent / "tools" / "privacy_check.py")
    pc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pc)
    monkeypatch.setattr(pc, "ROOT", tmp_path)
    monkeypatch.setattr(pc, "identity_markers", lambda: {"your global git name": "octo-cat"})
    (tmp_path / "ok.md").write_text("git clone https://github.com/octo-cat/zeus\nsite: https://octo-cat.github.io/zeus/")
    (tmp_path / "bad.md").write_text("written by octo-cat")
    problems = pc.check([("100644", "ok.md"), ("100644", "bad.md")], staged=False)
    assert problems == ["bad.md: contains your global git name"]

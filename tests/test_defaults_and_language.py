"""Seeded default data (industry groups) and the interface-language switch on the TA page."""
import json
import re
import sys
from pathlib import Path

import pytest

import zeus_config as zc

ROOT = Path(__file__).resolve().parent.parent / "zeus"   # the code
sys.path.insert(0, str(ROOT / "market_brief"))

import groups  # noqa: E402

CJK = re.compile(r"[一-鿿]")


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    def data_dir(*parts):
        p = tmp_path.joinpath("data", *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(zc, "data_dir", data_dir)
    return tmp_path / "data"


def test_first_run_seeds_industry_groups(tmp_data):
    zc.ensure_default_data()
    seeded = (tmp_data / "industry_universe.txt").read_text(encoding="utf-8")
    assert seeded == (ROOT / "defaults" / "industry_universe.txt").read_text(encoding="utf-8")
    assert (tmp_data / "heatmap_universe.txt").exists() and (tmp_data / "watchlist.txt").exists()


def test_seeding_never_overwrites_your_lists(tmp_data):
    tmp_data.mkdir(parents=True, exist_ok=True)
    (tmp_data / "industry_universe.txt").write_text("###Mine,AAPL", encoding="utf-8")
    zc.ensure_default_data()
    assert (tmp_data / "industry_universe.txt").read_text(encoding="utf-8") == "###Mine,AAPL"


def test_industry_groups_parse_and_have_english_names():
    parsed = groups.parse_universe((ROOT / "defaults" / "industry_universe.txt").read_text(encoding="utf-8"))
    assert len(parsed) == 80 and sum(len(v) for v in parsed.values()) >= 500
    assert all(all(re.fullmatch(r"[A-Z0-9-]+", t) for t in v) for v in parsed.values())
    english = json.loads((ROOT / "options_analyzer" / "static" / "i18n_en.json").read_text(encoding="utf-8"))
    for name in parsed:
        assert name in english and not CJK.search(english[name]), name


def test_ta_page_loads_the_language_switch():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ta_screener import api

    app = FastAPI()
    app.include_router(api.router)
    html = TestClient(app).get("/ta/").text
    assert '<script src="/static/zeus.js"></script></body>' in html
    assert 'href="/static/zeus.css"' in html

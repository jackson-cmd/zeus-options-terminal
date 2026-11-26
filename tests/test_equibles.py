"""Optional Equibles source: offline, on responses recorded from the API (tests/equibles_responses.json).

No key means no request and no change; with a key the 📞 transcript and the checkup's IV rank
come from Equibles, and a 401 / 403 / 429 falls back quietly and pauses that route for the day."""
import json
from pathlib import Path

import pytest

import zeus_config as hc
import zeus_equibles as eq

REC = json.loads((Path(__file__).resolve().parent / "equibles_responses.json").read_text(encoding="utf-8"))
EVENT_ID = REC["investor_events"]["data"][0]["id"]


class FakeApi:
    """Stands in for requests.get: answers by path, records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        path = url.replace(eq.API, "")
        self.calls.append((path, dict(params or {}), headers or {}))
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                status, body = answer.pop(0) if isinstance(answer, list) else answer
                return _Resp(status, body)
        return _Resp(404, REC["not_found"])


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def api(monkeypatch, tmp_path):
    import requests

    def data_dir(*parts):
        p = tmp_path.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(hc, "data_dir", data_dir)

    def install(routes, key="eq_test"):
        monkeypatch.setattr(hc, "get", lambda k, d=None: key if k == "equibles_api_key" else d)
        fake = FakeApi(routes)
        monkeypatch.setattr(requests, "get", fake)
        return fake
    return install


TRANSCRIPT_ROUTES = {
    "/stocks/TSLA/investor-events": (200, REC["investor_events"]),
    f"/investor-events/{EVENT_ID}/speakers": (200, REC["speakers"]),
}


def test_no_key_sends_nothing(api):
    fake = api(TRANSCRIPT_ROUTES, key="")
    assert eq.latest_transcript("TSLA") is None
    assert eq.iv_rank("TSLA") is None
    assert fake.calls == []


def test_latest_transcript_has_speaker_names_and_roles(api):
    fake = api(TRANSCRIPT_ROUTES)
    tr = eq.latest_transcript("tsla")
    assert (tr["fiscalYear"], tr["fiscalQuarter"]) == (2026, 3)
    assert tr["turns"][1]["speakerName"] == "Elon Musk" and tr["turns"][1]["speakerRole"] == "CEO"
    # one click: the newest call with a transcript, no year or quarter asked
    assert fake.calls[0][1] == {"eventType": "EarningsCall", "limit": 8}
    assert fake.calls[0][2]["Authorization"] == "Bearer eq_test"


def test_paging_reads_every_turn(api):
    first = {**REC["speakers"], "hasMore": True}
    last = {**REC["speakers"], "hasMore": False, "offset": 3}
    fake = api({"/stocks/TSLA/investor-events": (200, REC["investor_events"]),
                f"/investor-events/{EVENT_ID}/speakers": [(200, first), (200, last)]})
    assert len(eq.latest_transcript("TSLA")["turns"]) == 6
    assert [c[1]["offset"] for c in fake.calls if "/speakers" in c[0]] == [0, 3]


def test_transcript_is_cached_per_ticker_and_quarter(api):
    fake = api(TRANSCRIPT_ROUTES)
    eq.latest_transcript("TSLA")
    n = len(fake.calls)
    assert eq.latest_transcript("TSLA")["turns"]
    assert len(fake.calls) == n          # reload spends no request
    assert any(p.name == "TSLA_2026Q3.json" for p in Path(hc.data_dir("equibles")).iterdir())


@pytest.mark.parametrize("status", [401, 403, 429])
def test_refused_key_falls_back_and_pauses_for_the_day(api, status):
    fake = api({"/stocks/TSLA/investor-events": (status, REC["unauthorized"])})
    assert eq.latest_transcript("TSLA") is None
    assert eq.is_paused("transcripts")
    calls = len(fake.calls)
    assert eq.latest_transcript("MSFT") is None
    assert len(fake.calls) == calls      # paused: no second request today


def test_page_renders_each_speaker_above_the_words(api, monkeypatch):
    api(TRANSCRIPT_ROUTES)
    from sec_filings import transcript
    monkeypatch.setattr(transcript, "_yt_dlp", lambda: pytest.fail("YouTube must not be asked"))
    html = transcript.get_transcript_html("TSLA")
    assert '<div class="tr-sp">Elon Musk · CEO</div>' in html
    assert "Tesla, Inc. Q3 FY2026 Earnings Call" in html and "2026-10-21" in html


def test_unnamed_speaker_gets_a_numbered_label(api):
    unnamed = {"speakerIndex": 14, "speakerName": None, "speakerRole": None, "text": "Thank you and good morning."}
    api({"/stocks/TSLA/investor-events": (200, REC["investor_events"]),
         f"/investor-events/{EVENT_ID}/speakers": (200, {**REC["speakers"], "data": [unnamed]})})
    from sec_filings import transcript
    assert '<div class="tr-sp">Speaker 14</div>' in transcript.get_transcript_html("TSLA")


def test_page_falls_back_to_youtube_without_equibles(api, monkeypatch):
    api({"/stocks/TSLA/investor-events": (403, REC["unauthorized"])})
    from sec_filings import transcript
    monkeypatch.setattr(transcript, "_CACHE_DIR", Path(hc.data_dir("yt")))
    monkeypatch.setattr(transcript, "_yt_dlp", lambda: "yt-dlp")
    monkeypatch.setattr(transcript, "_search", lambda t, yt: [{"id": "abc", "title": "TSLA earnings call", "dur": 3600, "up": "20261021"}])
    monkeypatch.setattr(transcript, "_fetch_captions", lambda vid, yt: ["Good afternoon everyone."])
    html = transcript.get_transcript_html("TSLA")
    assert "youtube.com/watch?v=abc" in html


def test_iv_rank_matches_the_parquet_shape(api):
    fake = api({"/stocks/TSLA/options/volatility": (200, REC["volatility"])})
    s = REC["volatility"]["sessions"][0]
    assert eq.iv_rank("TSLA") == {"ivr": s["ivRank"], "iv": s["derivedIv30"], "asof": s["session"]}
    eq.iv_rank("TSLA")
    assert len(fake.calls) == 1          # once per ticker per day


def test_free_key_iv_rank_403_keeps_the_parquet(api, monkeypatch):
    fake = api({"/stocks/TSLA/options/volatility": (403, REC["unauthorized"])})
    from pre_trade_checkup import data_sources as DS
    monkeypatch.setattr(DS, "OPTIONS_VOL_HISTORY", Path(hc.data_dir("none")) / "missing.parquet")
    assert DS.get_ivr("TSLA") == {"ivr": None, "iv": None, "asof": None}
    DS.get_ivr("NVDA")
    assert len(fake.calls) == 1          # paused after the 403


def test_settings_test_reports_both_routes(api):
    api({"/stocks/AAPL/investor-events": (200, REC["investor_events"]),
         "/stocks/AAPL/options/volatility": (403, REC["unauthorized"])})
    res = eq.self_test()
    assert res["ok"] and res["transcripts"].startswith("AAPL 2026 Q3")
    assert "own IV data" in res["iv_rank"]


def test_settings_test_rejected_key(api):
    api({"/stocks/AAPL/investor-events": (401, REC["unauthorized"])})
    assert eq.self_test() == {"ok": False, "error": "key rejected (401): check it in Settings"}

"""MCP server (/mcp): both protocol eras, the security checks, every tool against a fake
terminal, and the stdio bridge end to end through a real HTTP server."""
import datetime as dt
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent / "zeus"   # the code
sys.path.insert(0, str(ROOT / "options_analyzer"))

import paper_trade_api as pta  # noqa: E402
import paper_trade_routes as ptr  # noqa: E402
import zeus_config as hc  # noqa: E402
import zeus_mcp as zm  # noqa: E402

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"}}
TODAY = dt.date.today()
EXP = (TODAY + dt.timedelta(days=36)).isoformat()
EARN = (TODAY + dt.timedelta(days=20)).isoformat()   # before EXP: the AAPL spread holds through it
NOW_MS = time.time() * 1000
SYM = {k: f"AAPL  {EXP[2:4]}{EXP[5:7]}{EXP[8:10]}C{int(k * 1000):08d}" for k in range(330, 405, 5)}
PUT = {k: SYM[k].replace("C", "P", 1) for k in SYM}


# ====================================================================== fake terminal
def chain_rows():
    rows = []
    for k in range(330, 405, 5):
        intrinsic = max(0.0, 355 - k)
        mid = round(intrinsic + 8.0 * max(0.15, 1 - abs(355 - k) / 60), 2)
        call = {"symbol": SYM[k], "streamer": f".AAPL{k}C", "bid": round(mid - 0.1, 2), "ask": round(mid + 0.1, 2),
                "mid": mid, "vol": 500, "oi": 5000, "iv": 0.21}
        if k == 385:   # thin and wide: analyze_strategy must warn
            call.update(bid=1.0, ask=1.6, mid=1.3, oi=40)
        put_mid = round(max(0.0, k - 355) + 8.0 * max(0.15, 1 - abs(355 - k) / 60), 2)
        rows.append({"strike": float(k), "call": call,
                     "put": {"symbol": PUT[k], "streamer": f".AAPL{k}P", "bid": put_mid - 0.1, "ask": put_mid + 0.1,
                             "mid": put_mid, "vol": 300, "oi": 3000, "iv": 0.23}})
    return rows


def fake_chain(client, ticker, expiration=None, quotes_cache=None, metrics_cache=None):
    return {"ticker": ticker, "price": 355.0, "iv": 0.21, "earnings_date": EARN, "earnings_time": "AMC",
            "expirations": [{"date": EXP, "dte": 36, "type": "Regular"}],
            "strikes": chain_rows() if expiration else []}


def strategies():
    return [
        {"name": "Bull Call Spread", "underlying": "AAPL", "current_price": 355.0, "iv": 0.21, "risk_free_rate": 0.045,
         "earnings_date": EARN, "earnings_time": "", "expiration": EXP, "expirations": [EXP], "dte": 36,
         "created_at": "2026-11-03T14:12:00+00:00",
         "legs": [{"symbol": SYM[360], "type": "C", "strike": 360.0, "qty": 2, "entry": 7.01, "current": 8.2,
                   "prev_close": 8.0, "exp": EXP, "mult": 100, "iv": 0.21, "mid": 8.2},
                  {"symbol": SYM[380], "type": "C", "strike": 380.0, "qty": -2, "entry": 2.26, "current": 1.94,
                   "prev_close": 2.0, "exp": EXP, "mult": 100, "iv": 0.2, "mid": 1.94}]},
        {"name": "Short Call", "underlying": "MSFT", "current_price": 521.0, "iv": 0.25, "risk_free_rate": 0.045,
         "earnings_date": "", "expiration": EXP, "expirations": [EXP], "dte": 36, "created_at": "2026-11-01",
         "legs": [{"symbol": "MSFT  C540", "type": "C", "strike": 540.0, "qty": -1, "entry": 4.0, "current": 3.0,
                   "prev_close": 3.2, "exp": EXP, "mult": 100, "iv": 0.25}]},
    ]


MARKET = {"options": {r["call"]["symbol"]: {"mark": r["call"]["mid"], "last": r["call"]["mid"],
                                             "prev_close": r["call"]["mid"] - 0.2} for r in chain_rows()},
          "stocks": {"AAPL": {"price": 355.0, "prev_close": 352.0}, "MSFT": {"price": 521.0, "prev_close": 519.0}}}


def build_app() -> FastAPI:
    app = FastAPI()
    calls = app.state.calls = []

    @app.get("/api/data")
    def data():
        return {"generated_at": "2026-11-12T15:00:00", "strategies": strategies(),
                "equities": [{"symbol": "MSFT", "quantity": 100, "direction": "Long", "entry": 505.2, "current": 521.0}],
                "equity_prev_close": {"MSFT": 519.0},
                "balance": {"net_liquidating_value": 128538.0, "cash_balance": 51596.15,
                            "derivative_buying_power": 51596.15, "maintenance_requirement": 0.0}}

    @app.get("/api/checkup/regime")
    def regime():
        return {"regime": "bull", "spy": 760.0, "sma200": 729.0, "pct_above": 4.3, "vix": 17.5, "high_vol": False,
                "reason": "站上200SMA", "strike": {"label": "正常牛市 → ITM 稳扎", "detail": "…"},
                "band": {"near": 0.6, "soft": 0.7, "forced": 0.8, "zone": "70-80%", "label": "牛市"}, "asof": "2026-11-11"}

    @app.get("/api/zeus/status")
    def status():
        return {"broker_mode": "manual", "benzinga": False, "ai": {"provider": "off"},
                "config_file": "/Users/someone/.zeus-terminal/config.json",
                "news": {"sources": ["finviz", "wsj", "marketwatch"]},
                "manual": {"trades": 12, "open_positions": 3, "cash": 51596.15, "starting_cash": 120000.0, "realized": 4681.45}}

    @app.get("/api/bilingual/data")
    def bilingual():
        return {"articles": [
            {"url": "https://example.com/1", "title": "Nvidia shares dive after CEO remarks", "source": "Finviz",
             "section": "Finviz", "ts": NOW_MS - 600_000, "titleZh": "英伟达股价下跌"},
            {"url": "https://example.com/2", "title": "NVDA options traders load up on calls", "source": "WSJ",
             "section": "Markets", "ts": NOW_MS - 3_600_000},
            {"url": "https://example.com/3", "title": "Navidia? No: an unrelated story", "source": "WSJ",
             "section": "World", "ts": NOW_MS - 3_600_000},
            {"url": "https://example.com/4", "title": "Nvidia story from last week", "source": "MarketWatch",
             "section": "Top", "ts": NOW_MS - 5 * 86_400_000}]}

    @app.get("/api/digest/day")
    def digest_day():
        return {"items": []}

    @app.get("/api/digest/by_ticker")
    def digest_by_ticker(ticker: str = "", days: int = 1):
        calls.append(("by_ticker", ticker, days))
        return {"items": [{"ts": NOW_MS - 120_000, "title": "NVDA: unusual call buying", "tickers": ["NVDA"],
                           "channels": ["Options"], "sentiment": 1, "title_cn": "NVDA 异常看涨期权买入"}]}

    @app.get("/api/pre_trade_checkup")
    def pretrade(ticker: str, direction: str, dte: int = 40):
        calls.append(("pretrade", ticker, direction, dte))
        return {"ticker": ticker, "direction": direction, "dte": dte, "as_of": "2026-11-11", "pass": False,
                "failed_layers": [2], "verdict": "别开单", "suggestion": "等下次真正触及 EMA",
                "layers": {"1": {"name": "长期趋势", "pass": True, "lines": ["AAPL > SMA200"]},
                           "2": {"name": "EMA触及", "pass": False, "reasons_fail": ["未触及任何 EMA"]}}}

    @app.get("/api/checkup/recommend/{ticker}")
    def recommend(ticker: str):
        recs = [{"exp": EXP, "dte": 36, "long": 350.0, "short": 385.0, "debit": 12.66, "max_profit": 2234.0,
                 "max_loss": 1266.0, "rr": 1.76, "breakeven": 362.66, "score": 96 - i, "reason": "好成交",
                 "scores": {"exec": 100}} for i in range(7)]
        return {"ticker": ticker, "spot": 355.0, "earnings_date": EARN, "regime": "bull", "vix": 17.5,
                "strike": {"mode": "itm"}, "recommendations": recs}

    @app.get("/api/checkup/liquidity/{ticker}")
    def liquidity(ticker: str, max_dte: int = 50):
        cell = {"tier": "good", "reasons": [], "bid": 1.9, "ask": 2.0, "spread_pct": 3.5, "vol": 900, "oi": 9000, "iv": 23.1}
        return {"ticker": ticker, "spot": 355.0, "asof": "2026-11-12T14:31", "market_hours": True, "atm_strike": 355.0,
                "cols": [{"date": EXP, "dte": 36, "type": "Regular"}],
                "ticker_verdict": {"tier": "good", "label": "流动性良好", "spread_pct": 2.3, "exp": EXP, "oi": 2337},
                "calls": {"strikes": [350.0, 355.0, 360.0], "cells": [[cell], [cell], [cell]], "best_col": [0, 0, 0]},
                "puts": {"strikes": [355.0], "cells": [[cell]], "best_col": [0]},
                "cross_week": {"side": "call", "long": 355.0, "width_pct": 10,
                               "rows": [{"date": EXP, "dte": 36, "long": 355.0, "short": 390.0, "debit": 9.1, "best": True}]}}

    @app.get("/api/quote_simple")
    def quote(symbol: str):
        if symbol == "BAD":
            raise RuntimeError("no such ticker")
        return {"symbol": symbol, "price": 100.5, "change": 1.0, "changePct": 1.0}

    @app.get("/api/major-events")
    def major(days: int = 30):
        return {"events": [{"date": (TODAY + dt.timedelta(days=3)).isoformat(), "type": "CPI", "name": "CPI",
                            "tier": 1, "days_away": 3},
                           {"date": (TODAY + dt.timedelta(days=90)).isoformat(), "type": "FOMC", "name": "FOMC",
                            "tier": 1, "days_away": 90}]}

    @app.get("/api/calendar_events")
    def calendar():
        return {"econ": [{"title": "CPI", "country": "USD", "date": (TODAY + dt.timedelta(days=3)).isoformat(),
                          "time_local": "08:30", "forecast": 0.3, "previous": 0.4, "actual": None}],
                "earnings": [{"ticker": "AAPL", "date": EARN, "session": "AMC"}]}

    @app.get("/api/checkup/risk-radar")
    def radar():
        return {"gauge": {"score": 10, "level": "low", "hot_topics": []}, "forward": {"score": 82, "level": "high",
                "n_events": 7, "top_events": []}, "landmines": [{"date": EARN, "name": "AAPL earnings", "ticker": "AAPL",
                "severity": "red", "days_away": 20, "note": "你的持仓"}]}

    @app.get("/api/market_brief")
    def brief():
        return {"status": "none", "date": TODAY.isoformat()}

    @app.get("/api/industry_groups")
    def groups():
        rows = [{"name": f"组{i}", "n": 4, "d5": i * 0.5, "m1": float(i), "m3": 2.0 * i, "m6": 3.0, "y1": 4.0,
                 "pct_above_ma50": 50, "pct_above_ma200": 60, "state": "趋势向上", "grade": "强", "note": "",
                 "top": [{"t": "AAA", "r": 4.2}]} for i in range(12)]
        return {"rows": rows, "rotation": {"leaders": ["组11"], "laggards": ["组0"], "warming": [], "cooling": []},
                "as_of": "2026-11-12", "n_groups": 12, "n_tickers": 48}

    @app.get("/api/options_pulse")
    def pulse():
        raise RuntimeError("no daily summaries yet")   # one source failing must not sink the overview

    @app.get("/api/ai_picks")
    def ai_picks():
        return {"date": TODAY.isoformat(), "picks": {}, "empty": True}

    @app.get("/api/ta/results")
    def ta_results():
        return {"ts": "2026-11-12T15:00", "rows": [{"ticker": "AAPL", "score": 61, "spark": list(range(100))},
                                                   {"ticker": "NVDA", "score": 88, "zones": [1, 2]}]}

    @app.get("/api/finviz/{t}")
    def finviz(t: str):
        return {"fundamentals": {"P/E": "30.1", "Market Cap": "5.17T"}, "analysts": [{"date": "Nov-10", "rating": "Buy"}],
                "description": "Makes phones."}

    @app.get("/api/earnings-history/{t}")
    def earnings_history(t: str):
        return {"rows": [{"date": "2026-11-02", "day_pct": -1.8, "next_pct": -1.9}], "down_prob": 0.5,
                "avg_day_pct": 0.4, "avg_next_pct": -0.2}

    @app.get("/api/ta/detail/{t}")
    def ta_detail(t: str):
        return {"empty": True}

    @app.get("/api/journal_stats")
    def journal_stats(from_: str = Query("", alias="from"), to: str = "", ticker: str = ""):
        calls.append(("journal", ticker))
        return {"n_trades": 26, "kpi": {"win_rate": 0.65, "profit_factor": 2.1},
                "by_strategy": [{"strategy": "Vertical P", "n": 12, "win_rate": 0.75, "net_pnl": 1229.6}],
                "by_ticker": [{"ticker": t, "n": 3, "net_pnl": p} for t, p in (("AMZN", 773.0), ("TSLA", -300.0))],
                "by_direction": [], "by_holdtime": [], "by_dte_at_open": [], "equity_curve": [1, 2, 3]}

    @app.get("/api/journal_entries/list")
    def entries(status: str = "all"):
        return {"entries": [
            {"id": "close-1", "_kind": "close", "ticker": "AAPL", "strategy": "Vertical C", "close_date": "2026-11-10",
             "net_pnl": 217.0, "status": "completed", "lesson_learned": "Took profit at 75 %"},
            {"id": "open-1", "_kind": "open", "ticker": "AAPL", "status": "pending"}]}

    app.include_router(ptr.router)
    app.include_router(zm.router)
    return app


def configure_paper(monkeypatch, tmp_path):
    monkeypatch.setattr(pta, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pta, "STORE_PATH", tmp_path / "paper_trades.json")
    monkeypatch.setattr(pta, "fetch_chain_for_builder", fake_chain)
    ptr.configure(client=lambda: object(),
                  option_marks=lambda c, syms: {s: MARKET["options"][s] for s in syms if s in MARKET["options"]},
                  equity_quotes=lambda c, tks: {t: MARKET["stocks"][t] for t in tks if t in MARKET["stocks"]},
                  quotes=lambda: {}, metrics=lambda: {})


@pytest.fixture()
def app(tmp_path, monkeypatch):
    configure_paper(monkeypatch, tmp_path)
    a = build_app()
    zm.configure(a)
    hc.update({"mcp_server": "", "mcp_paper_trades": "", "language": ""})
    yield a
    hc.update({"mcp_server": "", "mcp_paper_trades": "", "language": ""})


@pytest.fixture()
def client(app):
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def rpc(c, method, params=None, *, modern=True, headers=None, mid=1):
    p = dict(params or {})
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if modern:
        p["_meta"] = dict(META)
        h.update({"MCP-Protocol-Version": MODERN, "Mcp-Method": method})
        if method in ("tools/call", "prompts/get"):
            h["Mcp-Name"] = str(p.get("name", ""))
    h.update(headers or {})
    return c.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": p}), headers=h)


def call(c, name, args=None):
    body = rpc(c, "tools/call", {"name": name, "arguments": args or {}}).json()
    res = body["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


# ====================================================================== protocol: modern
def test_discover_names_versions_capabilities_and_server(client):
    r = rpc(client, "server/discover")
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["resultType"] == "complete"
    assert res["supportedVersions"][0] == MODERN and "2025-06-18" in res["supportedVersions"]
    assert set(res["capabilities"]) == {"tools", "prompts"}
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "zeus-terminal"
    assert res["ttlMs"] > 0 and res["cacheScope"] == "public" and "get_status" in res["instructions"]


def test_tool_list_is_valid_and_ordered(client):
    res = rpc(client, "tools/list").json()["result"]
    assert res["resultType"] == "complete" and res["ttlMs"] > 0 and res["cacheScope"] == "public"
    tools = res["tools"]
    assert [t["name"] for t in tools] == list(zm.TOOLS) and len(tools) == 18
    for t in tools:
        assert set(t) == {"name", "title", "description", "inputSchema", "annotations"}
        assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False
        writes = t["name"].startswith("paper_trade_")
        assert t["annotations"]["readOnlyHint"] is (not writes)
    assert rpc(client, "tools/list").json()["result"]["tools"] == tools   # deterministic


def test_prompts(client):
    prompts = rpc(client, "prompts/list").json()["result"]["prompts"]
    assert [p["name"] for p in prompts] == ["portfolio_review", "pretrade_check", "morning_brief", "journal_coach"]
    msg = rpc(client, "prompts/get", {"name": "pretrade_check", "arguments": {"ticker": "nvda"}}).json()["result"]
    text = msg["messages"][0]["content"]["text"]
    assert msg["messages"][0]["role"] == "user" and "NVDA" in text and "pretrade_checkup" in text
    assert text.endswith("Answer in the language I write in.")
    hc.update({"language": "zh"})
    zh = rpc(client, "prompts/get", {"name": "morning_brief"}).json()["result"]["messages"][0]["content"]["text"]
    assert zh.endswith("Answer in Chinese (simplified).")
    err = rpc(client, "prompts/get", {"name": "pretrade_check", "arguments": {}}).json()["error"]
    assert err["code"] == -32602 and "ticker" in err["message"]


@pytest.mark.parametrize("params, headers, status, code", [
    ({"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
     {"MCP-Protocol-Version": "1900-01-01"}, 400, -32022),
    (None, {"Mcp-Method": "tools/call"}, 400, -32020),            # header says another method
    (None, {"MCP-Protocol-Version": "2025-11-25"}, 400, -32020),  # header and _meta disagree
    ({"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}}, None, 400, -32602),  # no clientCapabilities
])
def test_modern_request_validation(client, params, headers, status, code):
    p = params if params is not None else {}
    h = {"Content-Type": "application/json", "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
    h.update(headers or {})
    meta = p.get("_meta", META)
    r = client.post("/mcp", headers=h, content=json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"_meta": meta}}))
    assert r.status_code == status and r.json()["error"]["code"] == code
    if code == -32022:
        assert r.json()["error"]["data"]["supported"][0] == MODERN


def test_modern_unknown_and_removed_methods_are_404(client):
    for method in ("resources/list", "ping", "initialize"):
        r = rpc(client, method)
        assert r.status_code == 404 and r.json()["error"]["code"] == -32601, method


def test_tool_call_needs_matching_name_header(client):
    h = {"Content-Type": "application/json", "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_status", "arguments": {}, "_meta": META}}
    assert client.post("/mcp", headers=h, content=json.dumps(body)).json()["error"]["code"] == -32020
    h["Mcp-Name"] = "get_positions"
    assert client.post("/mcp", headers=h, content=json.dumps(body)).json()["error"]["code"] == -32020
    h["Mcp-Name"] = "=?base64?Z2V0X3N0YXR1cw==?="   # get_status, encoded
    assert client.post("/mcp", headers=h, content=json.dumps(body)).json()["result"]["isError"] is False


# ====================================================================== protocol: legacy
def test_legacy_handshake_and_calls(client):
    init = client.post("/mcp", headers={"Content-Type": "application/json"}, content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}}))
    res = init.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "zeus-terminal"
    assert "resultType" not in res and set(res["capabilities"]) == {"tools", "prompts"}
    h = {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    note = client.post("/mcp", headers=h, content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert note.status_code == 202 and note.content == b""
    tools = client.post("/mcp", headers=h, content=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()
    assert len(tools["result"]["tools"]) == 18 and "resultType" not in tools["result"]
    assert client.post("/mcp", headers=h, content=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})).json()["result"] == {}
    newer = client.post("/mcp", headers={"Content-Type": "application/json"}, content=json.dumps({
        "jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": "2099-01-01", "capabilities": {}}}))
    assert newer.json()["result"]["protocolVersion"] == zm.LEGACY[0]


def test_legacy_without_version_header_and_batches(client):
    h = {"Content-Type": "application/json"}   # clients before 2025-06-18 send no MCP-Protocol-Version
    r = client.post("/mcp", headers=h, content=json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "nope"}]))
    replies = {m["id"]: m for m in r.json()}
    assert len(replies[1]["result"]["tools"]) == 18 and replies[2]["error"]["code"] == -32601
    bad = client.post("/mcp", headers={**h, "MCP-Protocol-Version": "1999-01-01"},
                      content=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))
    assert bad.status_code == 400


# ====================================================================== security
def test_requests_from_other_sites_and_machines_are_refused(app, client):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ok = {"Content-Type": "application/json"}
    assert client.post("/mcp", headers={**ok, "Origin": "https://evil.example"}, content=body).status_code == 403
    assert client.post("/mcp", headers={**ok, "Origin": "null"}, content=body).status_code == 403
    assert client.post("/mcp", headers={**ok, "Origin": "http://localhost:8765"}, content=body).status_code == 200
    assert client.post("/mcp", headers={**ok, "Host": "evil.example:8765"}, content=body).status_code == 403
    lan = TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
    assert lan.post("/mcp", headers=ok, content=body).status_code == 403
    assert client.post("/mcp", headers={"Content-Type": "text/plain"}, content=body).status_code == 415
    assert client.post("/mcp", headers=ok, content=b"{not json").json()["error"]["code"] == -32700
    assert client.post("/mcp", headers=ok, content=b"x" * (zm.MAX_BODY + 1)).status_code == 413
    get = client.get("/mcp")
    assert get.status_code == 405 and get.headers["allow"] == "POST"
    hc.update({"mcp_server": "off"})
    off = client.post("/mcp", headers=ok, content=body)
    assert off.status_code == 403 and "switched off" in off.json()["error"]["message"]


def test_docker_host_is_refused_unless_the_container_opts_in(app, monkeypatch):
    """Behind Docker's port mapping a program on the host arrives from the container's gateway.
    MCP answers it only inside the image and only with -e ZEUS_MCP_FROM_HOST=1; the Host and
    Origin checks still apply, and a normal install ignores the variable."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ok = {"Content-Type": "application/json"}
    gateway = TestClient(app, base_url="http://127.0.0.1", client=("172.17.0.1", 50000))
    neighbour = TestClient(app, base_url="http://127.0.0.1", client=("172.17.0.5", 50000))
    monkeypatch.setattr(hc, "container_gateway", lambda route_file="/proc/net/route": "172.17.0.1")

    monkeypatch.setenv("ZEUS_MCP_FROM_HOST", "1")
    monkeypatch.setattr(hc, "in_container", lambda: False)       # a normal install
    r = gateway.post("/mcp", headers=ok, content=body)
    assert r.status_code == 403 and r.json()["error"]["message"] == "MCP answers only programs on this computer"

    monkeypatch.setattr(hc, "in_container", lambda: True)        # the image, opted in
    assert gateway.post("/mcp", headers=ok, content=body).status_code == 200
    assert neighbour.post("/mcp", headers=ok, content=body).status_code == 403          # another container
    assert gateway.post("/mcp", headers={**ok, "Host": "evil.example:8765"}, content=body).status_code == 403
    assert gateway.post("/mcp", headers={**ok, "Origin": "https://evil.example"}, content=body).status_code == 403
    assert gateway.get("/api/zeus/mcp").json()["container"]["http"] is True

    monkeypatch.delenv("ZEUS_MCP_FROM_HOST")                     # the image, default
    r = gateway.post("/mcp", headers=ok, content=body)
    msg = r.json()["error"]["message"]
    assert r.status_code == 403 and "docker exec -i zeus python" in msg and "ZEUS_MCP_FROM_HOST=1" in msg
    info = gateway.get("/api/zeus/mcp").json()["container"]
    assert info["http"] is False and info["stdio"][:4] == ["docker", "exec", "-i", "zeus"]
    assert info["stdio"][-1].endswith("zeus_mcp_stdio.py")


def test_settings_info_has_no_container_part_on_a_normal_install(client, monkeypatch):
    monkeypatch.delenv("ZEUS_CONTAINER", raising=False)
    monkeypatch.setenv("ZEUS_MCP_FROM_HOST", "1")
    assert client.get("/api/zeus/mcp").json()["container"] is None


def test_status_never_leaks_paths_or_secrets(client):
    err, st = call(client, "get_status")
    text = json.dumps(st)
    assert not err and "/Users/" not in text and "config" not in text
    assert st["agent_permissions"] == {"read": True, "paper_trades": False, "real_orders": False}
    assert st["account"]["net_liquidating_value"] == 128538.0 and st["open_option_strategies"] == 2


# ====================================================================== tools
def test_positions_match_the_page_and_count_covered_calls(client):
    err, p = call(client, "get_positions")
    assert not err
    spread = next(s for s in p["option_strategies"] if s["underlying"] == "AAPL")
    assert spread["cost_basis"] == 950.0 and spread["max_profit"] == 3050.0 and spread["max_loss"] == -950.0
    assert spread["pnl_open"] == pytest.approx(302.0) and spread["breakevens"] == [pytest.approx(364.75, abs=0.01)]
    assert spread["earnings_before_expiration"] is True and spread["exit_advice"]["code"] == "hold"
    assert {"one_sd_move", "worst_1sd", "worst_2sd", "var95", "cvar95"} <= set(spread["risk_1d"])
    assert spread["legs"][0] == {"type": "call", "action": "buy", "quantity": 2, "strike": 360.0, "expiration": EXP,
                                 "entry": 7.01, "mark": 8.2, "iv": 0.21}
    short_call = next(s for s in p["option_strategies"] if s["underlying"] == "MSFT")
    assert short_call["max_loss"] == "unlimited"
    msft = next(r for r in p["risk_by_underlying"] if r["underlying"] == "MSFT")
    assert msft["max_loss"] != "unlimited"   # 100 shares cover the short call
    assert p["stocks"][0]["pnl_today"] == pytest.approx(200.0)
    only = call(client, "get_positions", {"underlying": "aapl", "include_legs": False})[1]
    assert [s["underlying"] for s in only["option_strategies"]] == ["AAPL"] and "legs" not in only["option_strategies"][0]


def test_analyze_strategy_prices_legs_from_the_chain(client):
    legs = [{"type": "call", "action": "buy", "quantity": 1, "strike": 350, "expiration": EXP},
            {"type": "call", "action": "sell", "quantity": 1, "strike": 385, "expiration": EXP}]
    err, a = call(client, "analyze_strategy", {"ticker": "aapl", "legs": legs})
    assert not err and a["strategy"] == "Bull Call Spread" and a["net"] == "debit"
    debit = [r for r in chain_rows() if r["strike"] == 350][0]["call"]["mid"] - 1.3
    assert a["cost_basis"] == pytest.approx(debit * 100, abs=0.01)
    assert a["max_profit"] == pytest.approx((35 - debit) * 100, abs=0.01)
    assert a["max_loss"] == pytest.approx(-debit * 100, abs=0.01)
    assert a["breakevens"] == [pytest.approx(350 + debit, abs=0.02)]
    assert a["earnings_before_expiration"] is True
    assert any("thin open interest" in w for w in a["warnings"]) and any("wide market" in w for w in a["warnings"])
    covered = [{"type": "stock", "action": "buy", "quantity": 100},
               {"type": "call", "action": "sell", "quantity": 1, "strike": 380, "expiration": EXP, "price": 3.0}]
    err, c = call(client, "analyze_strategy", {"ticker": "AAPL", "legs": covered})
    assert not err and c["strategy"] == "Covered Call" and c["max_profit"] == pytest.approx(2800.0)
    err, msg = call(client, "analyze_strategy", {"ticker": "AAPL", "legs": [{**legs[0], "strike": 351.5}]})
    assert err and "Nearest strikes: 340, 345, 350, 355, 360" in msg
    err, msg = call(client, "analyze_strategy", {"ticker": "AAPL", "legs": [{**legs[0], "expiration": "2031-01-17"}]})
    assert err and EXP in msg


@pytest.mark.parametrize("name, args, needle", [
    ("get_positions", {"bogus": 1}, "unknown field"),
    ("get_quotes", {"tickers": "AAPL"}, "must be a list"),
    ("get_news", {"hours": 0}, "at least 1"),
    ("pretrade_checkup", {}, "'ticker' is required"),
    ("pretrade_checkup", {"ticker": "AAPL", "direction": "sideways"}, "one of bull, bear"),
    ("analyze_strategy", {"ticker": "AAPL", "legs": []}, "needs 1 to 8 items"),
    ("analyze_strategy", {"ticker": "AAPL", "legs": [{"type": "call", "action": "buy", "quantity": 1}]},
     "needs strike and expiration"),
    ("get_option_chain", {"ticker": "$$$"}, "does not look right"),
])
def test_bad_arguments_come_back_as_tool_errors(client, name, args, needle):
    err, msg = call(client, name, args)
    assert err and needle in msg


def test_unknown_tool_is_a_protocol_error(client):
    err = rpc(client, "tools/call", {"name": "place_real_order", "arguments": {}}).json()["error"]
    assert err["code"] == -32602 and "Unknown tool" in err["message"]


def test_pretrade_suggestions_and_liquidity(app, client):
    err, c = call(client, "pretrade_checkup", {"ticker": "aapl", "direction": "bear", "dte": 30})
    assert not err and ("pretrade", "AAPL", "bear", 30) in app.state.calls
    assert c["pass"] is False and c["failed_gates"] == [2] and [g["gate"] for g in c["gates"]] == [1, 2]
    assert c["gates"][1]["lines"] == ["未触及任何 EMA"]
    err, s = call(client, "suggest_spreads", {"ticker": "AAPL", "top": 3})
    assert not err and len(s["spreads"]) == 3 and "scores" not in s["spreads"][0]
    err, liq = call(client, "check_liquidity", {"ticker": "AAPL"})
    assert not err and liq["verdict"]["quality"] == "good" and liq["calls"]["at_the_money"][0]["strike"] == 355.0
    assert liq["same_spread_across_weeks"]["rows"][0]["best"] is True


def test_quotes_and_option_chain(client):
    err, q = call(client, "get_quotes", {"tickers": ["aapl", "^vix", "BAD", "AAPL"]})
    assert not err and [x["ticker"] for x in q["quotes"]] == ["AAPL", "^VIX", "BAD"]
    assert q["quotes"][2]["error"] == "no quote"
    err, ch = call(client, "get_option_chain", {"ticker": "AAPL", "strikes": 4, "side": "call"})
    assert not err and ch["expiration"] == EXP and [r["strike"] for r in ch["chain"]] == [345.0, 350.0, 355.0, 360.0]
    atm = ch["chain"][2]["call"]
    assert "put" not in ch["chain"][0] and 0.45 < atm["delta"] < 0.6


def test_news_matches_tickers_and_company_names(app, client, monkeypatch):
    from heatmap import metadata
    monkeypatch.setattr(metadata, "get_all", lambda: {"NVDA": {"name": "NVIDIA Corp"}})
    err, n = call(client, "get_news", {"ticker": "nvda", "hours": 48})
    assert not err and n["filter"]["also_matched"] == ["NVIDIA"]
    titles = [i["title"] for i in n["items"]]
    assert titles == ["NVDA: unusual call buying", "Nvidia shares dive after CEO remarks",
                      "NVDA options traders load up on calls"]
    assert n["items"][0]["source"] == "Benzinga" and n["items"][1]["title_zh"] == "英伟达股价下跌"
    assert ("by_ticker", "NVDA", 2) in app.state.calls
    err, q = call(client, "get_news", {"query": "options", "hours": 24, "limit": 5})
    assert not err and [i["title"] for i in q["items"]] == ["NVDA options traders load up on calls"]


def test_events_market_sectors_ideas_and_journal(client):
    err, ev = call(client, "get_events", {"days": 30, "tickers": ["msft"]})
    assert not err and [e["type"] for e in ev["key_events"]] == ["CPI"]
    aapl = next(h for h in ev["earnings_of_holdings"] if h["ticker"] == "AAPL")
    assert aapl["positions_through_earnings"] == [f"Bull Call Spread exp {EXP}"]
    assert ev["requested_tickers"] == [{"ticker": "MSFT", "next_earnings": EARN, "time": "AMC"}]
    err, mk = call(client, "get_market_overview")
    assert not err and mk["regime"]["take_profit_zone"] == "70-80%" and "sector_option_sentiment" not in mk
    assert mk["risk_radar"]["event_risk_next_30_days"]["score"] == 82 and len(mk["quotes"]) == 4
    err, sec = call(client, "get_sector_rotation", {"limit": 3, "sort": "m1"})
    assert not err and [g["return_pct"]["m1"] for g in sec["groups"]] == [11.0, 10.0, 9.0]
    weakest = call(client, "get_sector_rotation", {"limit": 1, "weakest": True})[1]
    assert weakest["groups"][0]["return_pct"]["m1"] == 0.0
    err, ideas = call(client, "get_trade_ideas", {"limit": 5})
    assert not err and "none yet" in ideas["ai_picks"]
    assert [r["ticker"] for r in ideas["ta_signals"]["rows"]] == ["NVDA", "AAPL"]
    assert "spark" not in json.dumps(ideas) and "zones" not in json.dumps(ideas)
    err, rep = call(client, "get_ticker_report", {"ticker": "aapl"})
    assert not err and rep["fundamentals"]["P/E"] == "30.1" and rep["earnings_reactions"]["recent"][0]["day_pct"] == -1.8
    err, j = call(client, "get_trade_journal", {"days": 30, "recent": 5})
    assert not err and j["trades"] == 26 and j["worst_tickers"][0]["ticker"] == "TSLA"
    assert j["recent_closed"][0]["lesson_learned"] == "Took profit at 75 %" and j["decision_logs_waiting"] == 1


def test_paper_trading_needs_permission(client):
    legs = [{"type": "call", "action": "buy", "quantity": 1, "strike": 350, "expiration": EXP},
            {"type": "call", "action": "sell", "quantity": 1, "strike": 385, "expiration": EXP}]
    err, msg = call(client, "paper_trade_open", {"ticker": "AAPL", "legs": legs})
    assert err and "Settings" in msg and "paper trades" in msg
    assert call(client, "list_paper_trades")[1]["count"] == 0
    hc.update({"mcp_paper_trades": "on"})
    err, opened = call(client, "paper_trade_open", {"ticker": "AAPL", "legs": legs, "notes": "agent test"})
    assert not err and "nothing was sent to a broker" in opened["note"]
    trade = opened["opened"]
    assert trade["name"] == "Bull Call Spread" and trade["group"] == "Agents" and trade["status"] == "open"
    stored = pta.list_trades()["trades"][0]
    assert [leg["symbol"] for leg in stored["legs"]] == [SYM[350], SYM[385]] and stored["notes"] == "agent test"
    lst = call(client, "list_paper_trades")[1]
    assert lst["count"] == 1 and lst["agents_may_trade"] is True and lst["trades"][0]["max_profit"] > 0
    err, closed = call(client, "paper_trade_close", {"trade_id": trade["id"]})
    assert not err and closed["closed"]["status"] == "closed" and "realized_pnl" in closed["closed"]
    calendar = [legs[0], {**legs[1], "expiration": (TODAY + dt.timedelta(days=60)).isoformat()}]
    err, msg = call(client, "paper_trade_open", {"ticker": "AAPL", "legs": calendar})
    assert err


def test_self_test_and_settings_info(app, client):
    import asyncio
    res = asyncio.run(zm.self_test())
    assert res["ok"] is True and res["tools"] == 18 and res["prompts"] == 4
    info = client.get("/api/zeus/mcp").json()
    assert info["url"].startswith("http://127.0.0.1:") and info["url"].endswith("/mcp")
    assert Path(info["bridge"]).name == "zeus_mcp_stdio.py" and Path(info["bridge"]).exists()
    assert sum(t["write"] for t in info["tools"]) == 2


# ====================================================================== stdio bridge, end to end
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_url(tmp_path_factory):
    import uvicorn
    mp = pytest.MonkeyPatch()
    configure_paper(mp, tmp_path_factory.mktemp("paper"))
    live = build_app()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(live, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                                           ws="none"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    live.state.url = f"http://127.0.0.1:{port}/mcp"
    yield live
    server.should_exit = True
    thread.join(10)
    mp.undo()


def bridge(url, lines, *args):
    payload = "".join((m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)) + "\n"
                      for m in lines).encode("utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "zeus_mcp_stdio.py"), "--url", url, *args],
                         input=payload, capture_output=True, timeout=120)
    out = [json.loads(line) for line in run.stdout.decode("utf-8").splitlines() if line.strip()]
    return run, out


def test_bridge_modern_requests(live_url):
    zm.configure(live_url)
    hc.update({"mcp_server": ""})
    meta = dict(META)
    run, out = bridge(live_url.state.url, [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_status", "arguments": {}, "_meta": meta}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "工具", "arguments": {}, "_meta": meta}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
    ])
    replies = {m["id"]: m for m in out}
    assert run.returncode == 0 and sorted(replies) == [1, 2, 3, 4]
    assert replies[1]["result"]["supportedVersions"][0] == MODERN
    assert len(replies[2]["result"]["tools"]) == 18
    assert replies[3]["result"]["isError"] is False
    # a non-ASCII name travels Base64-encoded in Mcp-Name, so the server sees no header mismatch
    assert replies[4]["error"]["code"] == -32602


def test_bridge_legacy_handshake(live_url):
    zm.configure(live_url)
    run, out = bridge(live_url.state.url, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ])
    replies = {m["id"]: m for m in out}
    assert sorted(replies) == [1, 2] and replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[2]["result"] == {}


def test_bridge_says_when_zeus_is_not_running():
    run, out = bridge(f"http://127.0.0.1:{_free_port()}/mcp",
                      [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "not json"])
    assert {m["id"] for m in out} == {1, None}
    down = next(m for m in out if m["id"] == 1)
    assert "not running" in down["error"]["message"]
    assert next(m for m in out if m["id"] is None)["error"]["code"] == -32700


def test_bridge_check_command(live_url):
    zm.configure(live_url)
    run = subprocess.run([sys.executable, str(ROOT / "zeus_mcp_stdio.py"), "--check", "--url", live_url.state.url],
                         capture_output=True, timeout=60)
    text = run.stdout.decode("utf-8")
    assert run.returncode == 0 and "18 tools" in text and "2026-07-28" in text
    down = subprocess.run([sys.executable, str(ROOT / "zeus_mcp_stdio.py"), "--check", "--url",
                           f"http://127.0.0.1:{_free_port()}/mcp"], capture_output=True, timeout=60)
    assert down.returncode == 1 and "not reachable" in down.stdout.decode("utf-8")

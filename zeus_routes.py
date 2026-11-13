"""HTTP routes added by ZEUS on top of the original app:

  /settings                   bilingual settings page (credentials, AI, news, trades)
  /api/settings               read (public view) / write settings
  /api/trades*                manual mode: add, import CSV, list, delete, export, sample portfolio
  /api/zeus/status         mode + capabilities for the UI (gear tooltip, settings cards)
  /api/zeus/test/*         connection tests (tastytrade, Alpaca, Public, free quotes, AI, news,
                              Benzinga, e-mail, MCP)
  /api/freenews/*             Finviz / WSJ / MarketWatch wire status and refresh

Security: the server listens on 127.0.0.1 by default. Write endpoints require
the X-Zeus-Client header (cross-site pages cannot send it without a CORS
preflight that this server refuses) and reject foreign Origin headers.
Secrets are write-only: no endpoint ever returns a stored secret.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

import zeus_config as hc
import zeus_ai
import zeus_news
import local_broker as lb

router = APIRouter()
HOOKS: dict[str, Callable] = {}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _guard(request: Request) -> None:
    if request.headers.get("x-zeus-client") != "1":
        raise HTTPException(403, "missing X-Zeus-Client header")
    origin = request.headers.get("origin")
    if origin:
        host = (urlparse(origin).hostname or "").lower()
        req_host = (request.url.hostname or "").lower()
        if host not in _LOCAL_HOSTS and host != req_host:
            raise HTTPException(403, "cross-origin request refused")


async def _run_hook(name: str, *args):
    fn = HOOKS.get(name)
    if not fn:
        return None
    res = fn(*args)
    if asyncio.iscoroutine(res):
        return await res
    return res


# ----------------------------------------------------------------- status
@router.get("/api/zeus/status")
async def zeus_status():
    st = hc.status()
    st.update(app=hc.APP_NAME, app_zh=hc.APP_NAME_ZH, tagline=hc.APP_TAGLINE, tagline_zh=hc.APP_TAGLINE_ZH,
              language=hc.get("language"), news=zeus_news.status())
    if st["broker_mode"] != "tastytrade":
        try:
            summ = await asyncio.get_event_loop().run_in_executor(None, lb.summary)
        except Exception as e:
            summ = {"error": str(e)}
        st["manual" if st["broker_mode"] == "manual" else "broker"] = summ
    st["broker_errors"] = lb.broker_errors()
    return st


# ----------------------------------------------------------------- settings
@router.get("/api/settings")
async def settings_get():
    return hc.public_view()


@router.post("/api/settings")
async def settings_post(request: Request, payload: dict = Body(...)):
    _guard(request)
    before_mode = (hc.account_source(), hc.market_source())
    view = hc.update(payload.get("values") or {})
    try:
        import zeus_brokers
        zeus_brokers.reset()
    except Exception:
        pass
    hc.benzinga_cookies_path()  # materialise pasted Benzinga cookies for the Benzinga clients
    hc.barchart_credentials_file()
    hc.apply_notification_shims()
    lb._invalidate()
    after_mode = (hc.account_source(), hc.market_source())
    restart = any(k in (payload.get("values") or {}) for k in ("host", "port"))
    if before_mode != after_mode or "manual_starting_cash" in (payload.get("values") or {}):
        asyncio.create_task(_run_hook("refresh_positions"))
    return {**view, "restart_needed": restart, "mode_changed": before_mode != after_mode}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    p = Path(__file__).resolve().parent / "options_analyzer" / "static" / "settings.html"
    return HTMLResponse(p.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- tests
@router.post("/api/zeus/test/{what}")
async def test_connection(what: str, request: Request):
    _guard(request)
    if what == "mcp":
        import zeus_mcp
        return await zeus_mcp.self_test()
    loop = asyncio.get_event_loop()

    def run():
        if what in ("alpaca", "public"):
            import zeus_brokers
            return zeus_brokers.test(what)
        if what == "free":
            return lb.free_test()
        if what == "tastytrade":
            if not hc.has_tastytrade():
                return {"ok": False, "error": "fill in client secret + refresh token first"}
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent / "trading_assistant"))
            from api_client import TastyClient
            c = TastyClient()
            accts = c.get_accounts()
            return {"ok": True, "accounts": len(accts)}
        if what == "ai":
            text, usage, err = zeus_ai.complete("You are a connectivity check.", "Reply with the single word OK.",
                                                   model="haiku", max_tokens=20, timeout=90)
            return {"ok": not err and bool(text), "provider": zeus_ai.provider(),
                    "model": zeus_ai.resolve_model("haiku"), "reply": (text or "")[:60], "error": err}
        if what == "news":
            out = {}
            for src in zeus_news.enabled_sources():
                try:
                    out[src] = len(zeus_news.FETCHERS[src]())
                except Exception as e:
                    out[src] = f"error: {e}"
            return {"ok": all(isinstance(v, int) and v > 0 for v in out.values()), "sources": out}
        if what == "benzinga":
            if not hc.has_benzinga():
                return {"ok": False, "error": "paste your Benzinga Pro cookies first"}
            import json as _json
            data = _json.loads(hc.get("benzinga_cookies_json"))
            if isinstance(data, dict):
                data = data.get("cookies", [])
            names = {c.get("name") for c in data if isinstance(c, dict)}
            hc.benzinga_cookies_path()
            return {"ok": bool(names), "cookies": len(names)}
        if what == "email":
            import zeus_mail
            if not hc.email_configured():
                return {"ok": False, "error": "fill in sender, recipient, app password and SMTP server, then save"}
            zeus_mail.send("Test e-mail", "If you can read this, ZEUS e-mail alerts work.")
            return {"ok": True, "sent_to": f"{len(zeus_mail.recipients())} address(es)"}
        return {"ok": False, "error": "unknown test"}

    try:
        return await loop.run_in_executor(None, run)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------------- manual trades
@router.get("/api/trades")
async def trades_list():
    loop = asyncio.get_event_loop()
    summary = await loop.run_in_executor(None, lb.summary)
    return {"trades": lb.load_trades(), "summary": summary, "mode": hc.broker_mode()}


@router.post("/api/trades")
async def trades_add(request: Request, payload: dict = Body(...)):
    _guard(request)
    try:
        t = lb.normalize_trade(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    res = lb.add_trades([t])
    asyncio.create_task(_run_hook("refresh_positions"))
    asyncio.create_task(_run_hook("refresh_journal"))
    return {"ok": True, **res, "trade": t}


@router.post("/api/trades/import")
async def trades_import(request: Request, payload: dict = Body(...)):
    _guard(request)
    text = str(payload.get("csv") or "")
    if len(text) > 20_000_000:
        return JSONResponse({"ok": False, "error": "file too large"}, status_code=413)
    trades, errors = lb.parse_csv(text)
    res = lb.add_trades(trades, replace=bool(payload.get("replace"))) if trades else {"added": 0, "total": len(lb.load_trades()), "duplicates": 0}
    if trades:
        asyncio.create_task(_run_hook("refresh_positions"))
        asyncio.create_task(_run_hook("refresh_journal"))
    return {"ok": bool(trades), **res, "parsed": len(trades), "errors": errors[:50], "error_count": len(errors)}


@router.delete("/api/trades/{trade_id}")
async def trades_delete(trade_id: str, request: Request):
    _guard(request)
    ok = lb.delete_trade(trade_id)
    if ok:
        asyncio.create_task(_run_hook("refresh_positions"))
        asyncio.create_task(_run_hook("refresh_journal"))
    return {"ok": ok}


@router.post("/api/trades/clear")
async def trades_clear(request: Request):
    _guard(request)
    lb.save_trades([])
    asyncio.create_task(_run_hook("refresh_positions"))
    asyncio.create_task(_run_hook("refresh_journal"))
    return {"ok": True}


@router.post("/api/trades/sample")
async def trades_sample_load(request: Request):
    """Load the sample portfolio (priced from today's free quotes)."""
    _guard(request)
    try:
        res = await asyncio.get_event_loop().run_in_executor(None, lb.load_sample)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    await _refresh_now()
    return res


@router.delete("/api/trades/sample")
async def trades_sample_remove(request: Request):
    """Remove the sample trades only; your own trades stay."""
    _guard(request)
    res = lb.remove_sample()
    await _refresh_now()
    return res


async def _refresh_now():
    """Recompute positions before answering, so the page shows the change as soon as it reloads."""
    try:
        await asyncio.wait_for(_run_hook("refresh_positions"), timeout=45)
    except Exception:
        pass
    asyncio.create_task(_run_hook("refresh_journal"))


@router.get("/api/trades/template.csv")
async def trades_template():
    return Response(lb.template_csv(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="zeus_trades_template.csv"'})


@router.get("/api/trades/export.csv")
async def trades_export():
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    cols = ["date", "symbol", "asset", "side", "effect", "qty", "price", "fees", "option_type", "strike",
            "expiration", "multiplier", "order_id", "note"]
    w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for t in lb.load_trades():
        w.writerow(t)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="zeus_trades.csv"'})


# ----------------------------------------------------------------- free news
@router.get("/api/freenews/status")
async def freenews_status():
    return zeus_news.status()


@router.post("/api/freenews/refresh")
async def freenews_refresh(request: Request):
    _guard(request)
    res = await _run_hook("refresh_freenews")
    return {"ok": True, "report": res}


@router.get("/api/zeus/i18n/{lang}")
async def i18n(lang: str):
    if lang not in ("en",):
        return PlainTextResponse("{}", media_type="application/json")
    p = Path(__file__).resolve().parent / "options_analyzer" / "static" / f"i18n_{lang}.json"
    if not p.exists():
        return PlainTextResponse("{}", media_type="application/json")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="application/json",
                             headers={"Cache-Control": "max-age=300"})

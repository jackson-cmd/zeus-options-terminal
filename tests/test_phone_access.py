"""Phone access: this computer never logs in; a phone (through Tailscale Serve, another proxy or the
Wi-Fi) logs in once with the access code; settings stay on this computer; with phone access off the
old localhost-only rule holds."""
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import zeus_config as hc
import zeus_remote as zr

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "zeus" / "options_analyzer" / "static"
SERVER = ROOT / "zeus" / "options_analyzer" / "optionstrat_server.py"
TAILNET = "zeus-mac.tail1234.ts.net"
PAGE = {"Accept": "text/html,application/xhtml+xml"}


def make_app():
    app = FastAPI()
    zr.install(app)

    @app.get("/")
    def home():
        return HTMLResponse("<p>terminal</p>")

    @app.get("/api/positions")
    def positions():
        return {"positions": []}

    @app.get("/settings")
    def settings_page():
        return HTMLResponse("<p>settings</p>")

    @app.get("/api/settings")
    def settings_api():
        return {"settings": {}}

    @app.get("/static/manifest.json")
    def manifest():
        return {"name": "ZEUS"}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_text("hi")
        await sock.close()

    return app


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(zr, "_state", None)
    monkeypatch.setattr(zr, "keep_awake_command", lambda pid: None)
    (hc.home() / "remote.json").unlink(missing_ok=True)
    zr._fails.clear()
    zr._all_fails.clear()
    yield
    hc.update({"phone_access": "", "host": "", "keep_awake": ""})


def phone_on():
    hc.update({"phone_access": "on"})


def local(app):
    return TestClient(app, base_url="http://localhost:8765", client=("127.0.0.1", 50001), follow_redirects=False)


def phone(app, ip="100.64.0.7"):
    """A phone through Tailscale Serve: it arrives from 127.0.0.1, addressed to the tailnet name."""
    return TestClient(app, base_url="https://" + TAILNET, client=("127.0.0.1", 50002), follow_redirects=False,
                      headers={"X-Forwarded-For": ip, "X-Forwarded-Proto": "https",
                               "X-Forwarded-Host": TAILNET, "Tailscale-User-Login": "me@example.com"})


def wifi(app):
    return TestClient(app, base_url="http://192.168.1.20:8765", client=("192.168.1.30", 50003), follow_redirects=False)


def log_in(c, code=None):
    return c.post("/login", json={"code": code or zr.access_code(), "next": "/"})


# ------------------------------------------------------------------ who is local
@pytest.mark.parametrize("client,headers,expected", [
    ("127.0.0.1", {"host": "localhost:8765"}, True),
    ("::1", {"host": "[::1]:8765"}, True),
    ("::ffff:127.0.0.1", {"host": "127.0.0.1:8765"}, True),
    ("127.0.0.1", {}, True),
    ("127.0.0.1", {"host": TAILNET}, False),                                   # Tailscale Serve keeps the name
    ("127.0.0.1", {"host": "localhost:8765", "x-forwarded-for": "100.64.0.7"}, False),
    ("127.0.0.1", {"host": "localhost:8765", "cf-connecting-ip": "1.2.3.4"}, False),
    ("127.0.0.1", {"host": "localhost:8765", "tailscale-user-login": "me@x"}, False),
    ("192.168.1.30", {"host": "localhost:8765"}, False),                       # Host is the sender's to choose
    ("127.0.0.1", {"host": "evil.example:8765"}, False),                       # DNS rebinding
    ("testclient", {"host": "testserver"}, False),
])
def test_who_is_local(client, headers, expected):
    assert zr.is_local(client, headers) is expected


def test_in_docker_the_bridge_gateway_is_this_computer(monkeypatch):
    monkeypatch.setattr(zr.hc, "in_container", lambda: True)
    monkeypatch.setattr(zr, "_gateway", lambda: "172.17.0.1")
    assert zr.is_local("172.17.0.1", {"host": "localhost:8765"})
    assert not zr.is_local("172.17.0.1", {"host": "localhost:8765", "x-forwarded-for": "100.64.0.7"})
    assert not zr.is_local("172.17.0.5", {"host": "localhost:8765"})


# ------------------------------------------------------------------ phone access off: the old rule
def test_off_this_computer_and_the_test_host_get_in():
    app = make_app()
    assert local(app).get("/").status_code == 200
    assert TestClient(app).get("/api/positions").status_code == 200      # Host: testserver, as before


def test_off_a_phone_is_refused():
    r = phone(make_app()).get("/", headers=PAGE)
    assert r.status_code == 403 and r.json() == {"error": "host not allowed"}


def test_off_listening_on_all_interfaces_answers_the_wifi_as_before():
    hc.update({"host": "0.0.0.0"})
    assert wifi(make_app()).get("/api/positions").status_code == 200


def test_off_the_login_page_just_opens_zeus():
    hc.update({"host": "0.0.0.0"})
    assert wifi(make_app()).get("/login").headers["location"] == "/"


# ------------------------------------------------------------------ phone access on
def test_on_this_computer_never_logs_in():
    phone_on()
    c = local(make_app())
    assert c.get("/").status_code == 200
    assert c.get("/settings").status_code == 200
    assert c.get("/login").headers["location"] == "/"


def test_on_a_phone_is_sent_to_the_login_page():
    phone_on()
    c = phone(make_app())
    r = c.get("/", headers=PAGE)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.get("/", params={"tab": "journal"}, headers=PAGE)
    assert r.headers["location"] == "/login?next=%2F%3Ftab%3Djournal"
    r = c.get("/api/positions")
    assert r.status_code == 401 and r.headers["x-zeus-login"] == "/login"
    assert c.get("/static/manifest.json").status_code == 200       # installing the app needs it
    page = c.get("/login")
    assert page.status_code == 200 and "访问码" in page.text and "Access code" in page.text


def test_on_login_with_the_code_then_everything_but_settings():
    phone_on()
    c = phone(make_app())
    assert log_in(c, "WRONG-CODE").status_code == 401
    code = zr.access_code()
    r = log_in(c, code.lower().replace("-", " "))              # typed any which way
    assert r.status_code == 200 and r.json()["next"] == "/"
    cookie = r.headers["set-cookie"]
    for part in ("zeus_session=", "HttpOnly", "SameSite=Lax", "Secure", "Max-Age=15552000", "Path=/"):
        assert part in cookie
    assert c.get("/", headers=PAGE).status_code == 200
    assert c.get("/api/positions").json() == {"positions": []}
    # settings and the controls stay on the computer
    r = c.get("/settings", headers=PAGE)
    assert r.status_code == 403 and "Settings open only on the computer" in r.text
    assert c.get("/api/settings").status_code == 403
    assert c.get("/api/remote/status").status_code == 403
    assert c.post("/api/remote/code", headers={"X-Zeus-Client": "1"}).status_code == 403


def test_on_the_wifi_cookie_is_not_marked_secure_over_http():
    phone_on()
    hc.update({"host": "0.0.0.0"})
    c = wifi(make_app())
    assert c.get("/api/positions").status_code == 401
    r = log_in(c)
    assert r.status_code == 200 and "Secure" not in r.headers["set-cookie"]
    assert c.get("/api/positions").status_code == 200


def test_next_cannot_leave_zeus():
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil.example", "/login?next=/", "", None, "x",
                "/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/ /evil.example", "/x\\..\\/evil"):
        assert zr._safe_next(bad) == "/"
    assert zr._safe_next("/?tab=journal") == "/?tab=journal"


def test_wrong_codes_are_rate_limited_per_device_and_overall():
    phone_on()
    app = make_app()
    c = phone(app)
    for _ in range(5):
        assert log_in(c, "AAAA-AAAA").status_code == 401
    r = log_in(c)                                               # even the right code waits now
    assert r.status_code == 429 and 0 < r.json()["retry_after"] <= 600
    for i in range(15):                                          # 20 wrong codes from anywhere
        assert log_in(phone(app, ip=f"100.64.1.{i}"), "AAAA-AAAA").status_code == 401
    assert log_in(phone(app, ip="100.64.2.1")).status_code == 429
    zr.new_access_code()                                         # a new code clears the counters
    assert log_in(phone(app, ip="100.64.2.1")).status_code == 200


def test_which_device_is_counted():
    # on the network: its own address, whatever it claims
    assert zr._client_key("192.168.1.30", {"x-forwarded-for": "1.2.3.4", "cf-connecting-ip": "5.6.7.8"}) == "192.168.1.30"
    # through Tailscale Serve: the address the proxy saw, not what the phone put in front of it
    assert zr._client_key("127.0.0.1", {"x-forwarded-for": "1.2.3.4, 100.64.0.7"}) == "100.64.0.7"
    assert zr._client_key("127.0.0.1", {"cf-connecting-ip": "5.6.7.8"}) == "127.0.0.1"


def test_a_burst_of_wrong_codes_at_once_is_still_limited():
    import asyncio
    import httpx
    phone_on()
    app = make_app()

    async def burst():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50009))
        async with httpx.AsyncClient(transport=transport, base_url="https://" + TAILNET,
                                     headers={"X-Forwarded-For": "100.64.0.9"}) as c:
            rs = await asyncio.gather(*[c.post("/login", json={"code": "AAAA-AAAA"}) for _ in range(30)])
        return [r.status_code for r in rs]
    codes = asyncio.run(burst())
    assert codes.count(401) == 5 and codes.count(429) == 25


def test_sign_out_and_a_new_code_end_every_session():
    phone_on()
    c = phone(make_app())
    old = zr.access_code()
    log_in(c)
    assert c.get("/api/positions").status_code == 200
    zr.sign_out_all()
    assert c.get("/api/positions").status_code == 401
    assert zr.access_code() == old
    log_in(c)
    assert c.get("/api/positions").status_code == 200
    zr.new_access_code()
    assert c.get("/api/positions").status_code == 401
    assert log_in(c, old).status_code == 401
    assert log_in(c).status_code == 200


def test_forged_and_expired_cookies_are_refused():
    phone_on()
    c = phone(make_app())
    good = zr.make_session()
    body, sig = good.split(".")
    for bad in (body + "." + sig[:-2] + "AA", "x.y", "garbage", "", zr.make_session(time.time() - 181 * 86400)):
        assert zr.session_age(bad) is None
        c.cookies.clear()
        c.cookies.set(zr.COOKIE, bad, domain=TAILNET)
        assert c.get("/api/positions").status_code == 401
    assert zr.session_age(good) is not None


def test_a_week_old_session_is_renewed():
    phone_on()
    c = phone(make_app())
    c.cookies.set(zr.COOKIE, zr.make_session(time.time() - 8 * 86400), domain=TAILNET)
    r = c.get("/api/positions")
    assert r.status_code == 200 and "zeus_session=" in r.headers.get("set-cookie", "")
    c.cookies.set(zr.COOKIE, zr.make_session(), domain=TAILNET)
    assert "set-cookie" not in c.get("/api/positions").headers


def test_the_code_and_key_are_kept_private():
    code = zr.access_code()
    assert re.fullmatch(r"[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}", code)
    p = hc.home() / "remote.json"
    assert p.exists()
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o077 == 0


# ------------------------------------------------------------------ WebSockets
def test_websockets_follow_the_same_rules():
    # websocket_connect always sends Host: testserver, so each call names the host itself
    here = {"Host": "localhost:8765"}
    there = {"Host": TAILNET, "Origin": "https://" + TAILNET}
    phone_on()
    app = make_app()
    with local(app).websocket_connect("/ws", headers={**here, "Origin": "http://localhost:8765"}) as s:
        assert s.receive_text() == "hi"
    for origin in ("https://evil.example", "null"):             # another site's page, a sandboxed frame
        with pytest.raises(WebSocketDisconnect):
            with local(app).websocket_connect("/ws", headers={**here, "Origin": origin}) as s:
                s.receive_text()
    c = phone(app)
    with pytest.raises(WebSocketDisconnect):                     # not logged in
        with c.websocket_connect("/ws", headers=there) as s:
            s.receive_text()
    log_in(c)
    # the test client's cookie jar keeps a Secure cookie from wss://; a browser sends it
    cookie = {"Cookie": zr.COOKIE + "=" + c.cookies.get(zr.COOKIE)}
    with c.websocket_connect("/ws", headers={**there, **cookie}) as s:
        assert s.receive_text() == "hi"


# ------------------------------------------------------------------ controls on this computer
def test_status_shows_the_code_the_address_and_a_qr(monkeypatch):
    phone_on()
    monkeypatch.setattr(zr, "tailscale_status", lambda port: {
        "installed": True, "running": True, "state": "Running", "dns": TAILNET, "https_certs": True,
        "serve": True, "url": "https://" + TAILNET, "funnel": False, "busy": "", "download": zr.TS_DOWNLOAD})
    c = local(make_app())
    st = c.get("/api/remote/status").json()
    assert st["enabled"] and st["code"] == zr.access_code() and st["url"] == "https://" + TAILNET
    assert st["qr"].startswith("<svg") and st["port"] == 8765
    assert st["serve_cmd"] == "tailscale serve --bg --yes --https=443 http://127.0.0.1:8765"
    hc.update({"phone_access": "off"})
    assert c.get("/api/remote/status").json()["qr"] == ""


def test_control_writes_need_the_zeus_header():
    c = local(make_app())
    old = zr.access_code()
    assert c.post("/api/remote/code").status_code == 403
    assert c.post("/api/remote/code", headers={"X-Zeus-Client": "1", "Origin": "https://evil.example"}).status_code == 403
    r = c.post("/api/remote/code", headers={"X-Zeus-Client": "1", "Origin": "http://localhost:8765"})
    assert r.status_code == 200 and r.json()["code"] != old


def test_parse_tailscale_serve_status():
    cfg = {"TCP": {"443": {"HTTPS": True}},
           "Web": {TAILNET + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}},
           "AllowFunnel": {TAILNET + ":443": True}}
    assert zr.parse_serve(cfg, 8765) == {"serve": True, "url": "https://" + TAILNET, "funnel": True, "busy": ""}
    assert zr.parse_serve(cfg, 8790) == {"serve": False, "url": "", "funnel": False, "busy": "http://127.0.0.1:8765"}
    cfg = {"Web": {TAILNET + ":8443": {"Handlers": {"/": {"Proxy": "http://localhost:8765"}}}}}
    assert zr.parse_serve(cfg, 8765)["url"] == "https://" + TAILNET + ":8443"
    assert zr.parse_serve({}, 8765)["serve"] is False


def test_turning_on_tailscale_serve(monkeypatch):
    calls, serving = [], {"on": False}

    def status(port):
        return {"installed": True, "running": True, "serve": serving["on"], "busy": "",
                "url": "https://" + TAILNET if serving["on"] else ""}

    def run(args, timeout=8):
        calls.append(args)
        if "--bg" in args and not needs_https["v"]:
            serving["on"] = True
            return 0, "Available within your tailnet:\nhttps://" + TAILNET + "/\n", ""
        return -1, "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n  https://login.tailscale.com/f/serve?node=abc123\n", ""

    needs_https = {"v": True}
    monkeypatch.setattr(zr, "_ts_bin", lambda: "/usr/local/bin/tailscale")
    monkeypatch.setattr(zr, "tailscale_status", status)
    monkeypatch.setattr(zr, "_run", run)
    r = zr.tailscale_serve(8765, True)
    assert not r["ok"] and r["error"] == "enable_https" and r["link"] == "https://login.tailscale.com/f/serve?node=abc123"
    assert calls[-1] == ["/usr/local/bin/tailscale", "serve", "--bg", "--yes", "--https=443", "http://127.0.0.1:8765"]
    needs_https["v"] = False
    assert zr.tailscale_serve(8765, True)["ok"]
    n = len(calls)
    assert zr.tailscale_serve(8765, True)["ok"] and len(calls) == n      # already on: nothing to run
    monkeypatch.setattr(zr, "_ts_bin", lambda: "")
    assert zr.tailscale_serve(8765, True) == {"ok": False, "error": "not_installed"}


def test_keep_awake_follows_the_switches(monkeypatch):
    monkeypatch.setattr(zr, "keep_awake_command", lambda pid: [sys.executable, "-c", "import time; time.sleep(30)"])
    assert zr.sync_keep_awake()["active"] is False             # phone access off
    phone_on()
    assert zr.sync_keep_awake()["active"] is True
    proc = zr._awake
    hc.update({"keep_awake": "off"})
    assert zr.sync_keep_awake()["active"] is False
    proc.wait(timeout=5)


def test_write_guard_accepts_the_phones_own_address():
    import zeus_routes

    def req(origin, host, fwd=None):
        h = [(b"x-zeus-client", b"1"), (b"origin", origin.encode()), (b"host", host.encode())]
        if fwd:
            h.append((b"x-forwarded-host", fwd.encode()))
        return Request({"type": "http", "method": "POST", "path": "/api/settings", "headers": h,
                        "scheme": "https", "server": (host, 443), "query_string": b""})

    zeus_routes._guard(req("https://" + TAILNET, TAILNET))
    zeus_routes._guard(req("https://" + TAILNET, "127.0.0.1:8765", TAILNET))
    with pytest.raises(Exception):
        zeus_routes._guard(req("https://evil.example", "127.0.0.1:8765", TAILNET))


# ------------------------------------------------------------------ wiring, PWA files
def test_settings_schema_and_tab():
    assert hc.SCHEMA["phone_access"][0] == "off" and hc.SCHEMA["keep_awake"][0] == "on"
    assert ("phone", "Phone access", "手机访问") in hc.GROUPS
    html = (STATIC / "settings.html").read_text(encoding="utf-8")
    assert "/api/remote/status" in html and "phone_access: ['off', 'on']" in html


def test_the_server_puts_the_gate_in_front_and_the_worker_covers_every_page():
    src = SERVER.read_text(encoding="utf-8")
    assert "_zremote.install(app)" in src and "_zeus_host_guard" not in src
    assert '"Service-Worker-Allowed": "/"' in src
    assert "register('/static/sw.js', {scope: '/'})" in src
    assert "caches.match('/static/offline.html')" in src


def test_public_files_exist_and_load_nothing_from_elsewhere():
    for path in zr.PUBLIC_PATHS:
        if path.startswith("/static/"):
            assert (STATIC / path[len("/static/"):]).exists(), path
    for name in ("login.html", "offline.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r'(src|href)="https?://', html), name
        assert "viewport-fit=cover" in html


def test_manifest_installs_the_whole_site():
    import json
    m = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert m["id"] == "/" and m["scope"] == "/" and m["start_url"] == "/" and m["display"] == "standalone"

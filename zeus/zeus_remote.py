"""Phone access (手机访问): open ZEUS on your phone, at home or away, while it runs on your computer.

How it fits together
--------------------
* ZEUS keeps listening on 127.0.0.1. The phone reaches it through Tailscale: `tailscale serve`
  publishes https://<computer>.<tailnet>.ts.net inside your own tailnet and forwards it to
  127.0.0.1:<port>. On the same Wi-Fi, Listen address 0.0.0.0 works too (plain http).
* Every request is *local* (a browser on this computer) or *remote* (anything that came over the
  network or through a proxy or tunnel, even one running on this computer). Local requests never
  log in, as before.
* Phone access off (the default): remote requests get exactly the rule ZEUS always had.
* Phone access on: a remote device logs in once with the access code from ⚙ Settings → Phone
  access (or scans the QR code there) and keeps a signed cookie for 180 days, renewed while it is
  used. Settings, these controls, connection tests and MCP stay on this computer.

The access code and the key that signs the cookies live in ~/.zeus-terminal/remote.json (mode 600).
A new code, or "sign out every phone", replaces the key, so every cookie issued before stops working.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import zeus_config as hc

COOKIE = "zeus_session"
SESSION_DAYS = 180
RENEW_AFTER_DAYS = 7          # a phone in use gets a fresh 180 days once a week
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Headers that reverse proxies and tunnels add (Tailscale Serve, Cloudflare Tunnel, ngrok, nginx,
# Caddy). A request carrying any of them came through a proxy, even one running on this computer.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
                 "cf-connecting-ip", "true-client-ip", "tailscale-user-login", "tailscale-funnel-request")
# What a phone may load before logging in: the login page and what installing the app needs.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/static/manifest.json", "/static/icon-192.png",
                          "/static/icon-512.png", "/static/sw.js", "/static/offline.html", "/favicon.ico"})
# What stays on this computer even for a logged-in phone.
LOCAL_ONLY = ("/settings", "/api/settings", "/api/remote", "/api/zeus/test", "/api/zeus/mcp", "/mcp")
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no 0/O or 1/I to confuse
CODE_LEN = 8
PER_DEVICE_LIMIT = (5, 600)   # 5 wrong codes in 10 minutes: that device waits
GLOBAL_LIMIT = (20, 900)      # 20 wrong codes in 15 minutes from anywhere: every login waits
STATIC = Path(__file__).resolve().parent / "options_analyzer" / "static"

_lock = threading.Lock()
_state: dict | None = None


# --------------------------------------------------------------------------- settings
def enabled() -> bool:
    return hc.get_bool("phone_access")


def _path() -> Path:
    return hc.home() / "remote.json"


def _new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return raw[:4] + "-" + raw[4:]


def _norm(code) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(code or "").upper())


def _save(data: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, p)


def _load() -> dict:
    """The access code and signing key, created on first use."""
    global _state
    with _lock:
        if _state is None:
            try:
                data = json.loads(_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            changed = False
            if len(_norm(data.get("code"))) != CODE_LEN:
                data["code"], changed = _new_code(), True
            try:
                if len(bytes.fromhex(str(data.get("secret") or ""))) < 32:
                    raise ValueError
            except ValueError:
                data["secret"], changed = secrets.token_hex(32), True
            if changed:
                _save(data)
            _state = data
        return _state


def access_code() -> str:
    return _load()["code"]


def new_access_code() -> str:
    """A new code and a new signing key: every phone has to log in again with the new code."""
    global _state
    data = dict(_load())
    data.update(code=_new_code(), secret=secrets.token_hex(32))
    with _lock:
        _save(data)
        _state = data
        _fails.clear()
        _all_fails.clear()
    return data["code"]


def sign_out_all() -> None:
    """A new signing key with the same code: every phone has to log in again."""
    global _state
    data = dict(_load())
    data["secret"] = secrets.token_hex(32)
    with _lock:
        _save(data)
        _state = data


def check_code(code) -> bool:
    given = _norm(code)
    return len(given) == CODE_LEN and hmac.compare_digest(given, _norm(access_code()))


# --------------------------------------------------------------------------- sessions
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    key = bytes.fromhex(_load()["secret"])
    return _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def make_session(now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    body = _b64(json.dumps({"iat": now, "exp": now + SESSION_DAYS * 86400}, separators=(",", ":")).encode())
    return body + "." + _sign(body)


def session_age(value, now: float | None = None) -> float | None:
    """Seconds since the session cookie was issued; None when it is missing, forged or expired."""
    try:
        body, sig = str(value or "").split(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        data = json.loads(_unb64(body))
        now = time.time() if now is None else now
        if not (float(data["iat"]) - 300 <= now < float(data["exp"])):
            return None
        return max(0.0, now - float(data["iat"]))
    except Exception:
        return None


def _cookie(headers: dict, name: str) -> str:
    for part in (headers.get("cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v.strip().strip('"')
    return ""


def _https(scheme: str, headers: dict) -> bool:
    if scheme in ("https", "wss"):
        return True
    if (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() == "https":
        return True
    return "proto=https" in (headers.get("forwarded") or "").lower()


def _set_cookie_value(value: str, secure: bool) -> str:
    return (f"{COOKIE}={value}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Lax"
            + ("; Secure" if secure else ""))


# --------------------------------------------------------------------------- who is asking
def _host_only(value: str) -> str:
    v = (value or "").split(",")[0].strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v[1:]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback


@functools.lru_cache(maxsize=1)
def _gateway() -> str:
    return hc.container_gateway()


def is_local(client: str, headers: dict) -> bool:
    """A browser or program on this computer: loopback address, addressed to localhost, no proxy."""
    if any(h in headers for h in PROXY_HEADERS):
        return False
    host = _host_only(headers.get("host", ""))
    if host and host not in LOCAL_HOSTS:
        return False
    if _loopback(client):
        return True
    # In the Docker image, the computer reaches a port published on 127.0.0.1 from the bridge gateway.
    return hc.in_container() and bool(_gateway()) and client == _gateway()


def _legacy_ok(headers: dict) -> bool:
    """Phone access off: the rule ZEUS always had. Listening on 127.0.0.1 (the default), answer only
    requests addressed to localhost, a DNS-rebinding guard; listening on another address, answer all."""
    if str(hc.get("host") or "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        return True
    h = _host_only(headers.get("host", ""))
    return not h or h in LOCAL_HOSTS or h == "testserver"


def _origin_ok(headers: dict) -> bool:
    """A WebSocket opened by a page of another site (browsers do not apply CORS to WebSockets)."""
    origin = headers.get("origin")
    if origin is None:
        return True                       # not a browser
    oh = (urlparse(origin).hostname or "").lower()
    if not oh:
        return False                      # "null": a sandboxed frame or a file page
    return oh in LOCAL_HOSTS or oh in {_host_only(headers.get(h, "")) for h in ("host", "x-forwarded-host")} - {""}


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _client_key(client: str, headers: dict) -> str:
    """Which device is trying codes. A device on the network is its own address, whatever headers it
    sends. Through a proxy on this computer, the address the proxy saw is the last X-Forwarded-For
    entry (Tailscale Serve replaces the header; anything earlier could come from the device itself)."""
    if not _loopback(client):
        return client or "?"
    fwd = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    return fwd[-1] if fwd else (client or "?")


def _wants_page(method: str, headers: dict) -> bool:
    if method not in ("GET", "HEAD"):
        return False
    if headers.get("sec-fetch-mode") == "navigate" or headers.get("sec-fetch-dest") in ("document", "iframe"):
        return True
    return "text/html" in (headers.get("accept") or "")


# --------------------------------------------------------------------------- the gate
_ONLY_HERE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
              "<title>ZEUS</title><body style='background:#0d1117;color:#e6edf3;font:15px/1.6 -apple-system,"
              "BlinkMacSystemFont,sans-serif;padding:24px'><h3>🔒 设置只能在运行 ZEUS 的电脑上打开</h3>"
              "<p>Settings open only on the computer that runs ZEUS.</p><p><a style='color:#58a6ff' href='/'>"
              "← 返回终端 · Back to the terminal</a></p>")


async def _refuse_ws(receive, send) -> None:
    msg = await receive()
    if msg.get("type") == "websocket.connect":
        await send({"type": "websocket.close", "code": 1008})


class AccessMiddleware:
    """Decides who may use ZEUS, for pages, API calls and WebSockets alike."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        client = (scope.get("client") or ("",))[0] or ""
        path = scope.get("path") or "/"
        if kind == "websocket" and not _origin_ok(headers):
            return await _refuse_ws(receive, send)
        if is_local(client, headers):
            return await self.app(scope, receive, send)
        if not enabled():
            if _legacy_ok(headers):
                return await self.app(scope, receive, send)
            if kind == "websocket":
                return await _refuse_ws(receive, send)
            return await JSONResponse({"error": "host not allowed"}, status_code=403)(scope, receive, send)
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        age = session_age(_cookie(headers, COOKIE))
        if age is None:
            if kind == "websocket":
                return await _refuse_ws(receive, send)
            if _wants_page(scope.get("method", "GET"), headers):
                qs = scope.get("query_string", b"").decode("latin-1")
                nxt = path + ("?" + qs if qs else "")
                resp = RedirectResponse("/login" + ("" if nxt == "/" else "?next=" + quote(nxt, safe="")), 303)
            else:
                resp = JSONResponse({"error": "Log in first: open /login on this device", "login": "/login"},
                                    status_code=401, headers={"X-Zeus-Login": "/login"})
            return await resp(scope, receive, send)
        if _under(path, LOCAL_ONLY):
            if kind == "websocket":
                return await _refuse_ws(receive, send)
            if _wants_page(scope.get("method", "GET"), headers):
                return await HTMLResponse(_ONLY_HERE, status_code=403)(scope, receive, send)
            return await JSONResponse({"error": "Only on the computer that runs ZEUS"},
                                      status_code=403)(scope, receive, send)
        if kind == "http" and age > RENEW_AFTER_DAYS * 86400:
            fresh = _set_cookie_value(make_session(), _https(scope.get("scheme", ""), headers)).encode("latin-1")

            async def send_renewed(message):
                if message.get("type") == "http.response.start":
                    message = {**message, "headers": list(message.get("headers") or []) + [(b"set-cookie", fresh)]}
                await send(message)
            return await self.app(scope, receive, send_renewed)
        return await self.app(scope, receive, send)


# --------------------------------------------------------------------------- login
_fails: dict[str, list[float]] = {}
_all_fails: list[float] = []


def _retry_after(key: str, now: float) -> int:
    n, win = PER_DEVICE_LIMIT
    if len(_fails) > 1000:                # many devices: forget the ones whose wait is over
        for k in [k for k, ts in _fails.items() if not ts or now - ts[-1] >= win]:
            del _fails[k]
    mine = [t for t in _fails.get(key, []) if now - t < win]
    if mine:
        _fails[key] = mine
    else:
        _fails.pop(key, None)
    if len(mine) >= n:
        return int(mine[0] + win - now) + 1
    gn, gwin = GLOBAL_LIMIT
    _all_fails[:] = [t for t in _all_fails if now - t < gwin]
    if len(_all_fails) >= gn:
        return int(_all_fails[0] + gwin - now) + 1
    return 0


def _safe_next(value) -> str:
    """Where to go after logging in: a path on this site, never another site. Browsers drop tabs and
    line breaks from URLs and read a backslash as a slash, so "/<tab>/evil.example" would leave."""
    v = str(value or "/")
    if (not v.startswith("/") or v.startswith("/login") or "\\" in v
            or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in v)):
        return "/"
    u = urlparse(v)
    return "/" if (u.scheme or u.netloc or v.startswith("//")) else v


router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    headers = {k.lower(): v for k, v in request.headers.items()}
    client = request.client.host if request.client else ""
    if not enabled() or is_local(client, headers):
        return RedirectResponse("/", 303)
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/login")
async def login(request: Request):
    if not enabled():
        return JSONResponse({"error": "off"}, status_code=403)
    headers = {k.lower(): v for k, v in request.headers.items()}
    key = _client_key(request.client.host if request.client else "", headers)
    raw = await request.body()
    try:
        body = json.loads(raw) if len(raw) <= 4096 else {}
    except ValueError:
        body = {}
    # From here to the answer nothing awaits, so requests sent at the same moment are counted one
    # after another and cannot all slip past the limit together.
    now = time.time()
    wait = _retry_after(key, now)
    if wait:
        return JSONResponse({"error": "too_many", "retry_after": wait}, status_code=429)
    if not isinstance(body, dict) or not check_code(body.get("code")):
        _fails.setdefault(key, []).append(now)
        _all_fails.append(now)
        return JSONResponse({"error": "wrong_code"}, status_code=401)
    _fails.pop(key, None)
    resp = JSONResponse({"ok": True, "next": _safe_next(body.get("next"))})
    resp.headers.append("set-cookie", _set_cookie_value(make_session(), _https(request.url.scheme, headers)))
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- Tailscale
TS_DOWNLOAD = "https://tailscale.com/download"


def _ts_bin() -> str:
    for c in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if c and os.path.isfile(c):
            return c
    return ""


def _run(args: list[str], timeout: float = 8) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        dec = (lambda x: x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or ""))
        return -1, dec(e.stdout), dec(e.stderr)
    except OSError as e:
        return -2, "", str(e)


def _targets(proxy: str, port: int) -> bool:
    u = urlparse(proxy if "://" in proxy else "http://" + proxy)
    try:
        return (u.hostname or "") in LOCAL_HOSTS and u.port == port
    except ValueError:
        return False


def parse_serve(cfg: dict, port: int) -> dict:
    """From `tailscale serve status --json`: the https address that forwards to this ZEUS, and
    whether port 443 already forwards somewhere else."""
    out = {"serve": False, "url": "", "funnel": False, "busy": ""}
    for hostport, web in ((cfg or {}).get("Web") or {}).items():
        host, _, p = str(hostport).rpartition(":")
        root = ((web or {}).get("Handlers") or {}).get("/") or {}
        proxy = str(root.get("Proxy") or "")
        if proxy and _targets(proxy, port):
            out.update(serve=True, url="https://" + host + ("" if p == "443" else ":" + p),
                       funnel=bool(((cfg or {}).get("AllowFunnel") or {}).get(hostport)))
            return out
        if p == "443" and root:
            out["busy"] = proxy or "another handler"
    return out


def tailscale_status(port: int) -> dict:
    out = {"installed": False, "running": False, "state": "", "dns": "", "https_certs": False,
           "serve": False, "url": "", "funnel": False, "busy": "", "download": TS_DOWNLOAD}
    exe = _ts_bin()
    if not exe or hc.in_container():
        return out
    out["installed"] = True
    rc, so, _ = _run([exe, "status", "--json"])
    try:
        st = json.loads(so) if so.strip() else {}
    except ValueError:
        st = {}
    out["state"] = str(st.get("BackendState") or ("Stopped" if rc else ""))
    out["running"] = out["state"] == "Running"
    out["dns"] = str(((st.get("Self") or {}).get("DNSName") or "")).rstrip(".")
    out["https_certs"] = bool(st.get("CertDomains"))
    if out["running"]:
        rc, so, _ = _run([exe, "serve", "status", "--json"])
        try:
            out.update(parse_serve(json.loads(so) if so.strip() else {}, port))
        except ValueError:
            pass
    return out


def serve_command(port: int, on: bool = True, exe: str = "") -> list[str]:
    exe = exe or _ts_bin() or "tailscale"
    if on:
        return [exe, "serve", "--bg", "--yes", "--https=443", f"http://127.0.0.1:{port}"]
    return [exe, "serve", "--https=443", "off"]


def _login_link(text: str) -> str:
    m = re.search(r"https://login\.tailscale\.com/\S+", text or "")
    return m.group(0).rstrip(".,)") if m else ""


def tailscale_serve(port: int, on: bool) -> dict:
    """Start or stop forwarding https://<computer>.<tailnet>.ts.net to this ZEUS."""
    if not _ts_bin():
        return {"ok": False, "error": "not_installed"}
    if on:
        cur = tailscale_status(port)
        if not cur["running"]:
            return {"ok": False, "error": "not_running"}
        if cur["serve"]:
            return {"ok": True, "status": cur}
        if cur["busy"]:
            return {"ok": False, "error": "busy", "busy": cur["busy"]}
    # Until HTTPS is switched on for the tailnet, the command prints a link to do that and waits.
    rc, so, se = _run(serve_command(port, on), timeout=20)
    text = (so + "\n" + se).strip()
    status = tailscale_status(port)
    ok = status["serve"] if on else not status["serve"]
    res = {"ok": ok, "status": status, "output": text[-1500:]}
    if not ok and _login_link(text):
        res.update(error="enable_https", link=_login_link(text))
    return res


# --------------------------------------------------------------------------- same Wi-Fi, QR, keep awake
def _primary_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))     # no packet is sent: this only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def lan_url(port: int) -> str:
    if str(hc.get("host") or "127.0.0.1") in ("127.0.0.1", "localhost", "::1") or hc.in_container():
        return ""
    ip = _primary_ip()
    return f"http://{ip}:{port}" if ip and not ip.startswith("127.") else ""


def qr_svg(text: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(text, error="m").svg_inline(scale=4, border=2, dark="#000", light="#fff")


_awake: subprocess.Popen | None = None
_awake_lock = threading.Lock()


def keep_awake_command(pid: int) -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return ["caffeinate", "-i", "-s", "-w", str(pid)]   # idle sleep; system sleep on power
    if sys.platform.startswith("linux") and shutil.which("systemd-inhibit") and shutil.which("tail"):
        return ["systemd-inhibit", "--what=idle:sleep", "--who=ZEUS Terminal", "--why=Phone access is on",
                "--mode=block", "tail", f"--pid={pid}", "-f", "/dev/null"]
    return None


def sync_keep_awake() -> dict:
    """While phone access is on, keep the computer from sleeping (until ZEUS exits)."""
    global _awake
    want = enabled() and hc.get_bool("keep_awake") and not hc.in_container()
    with _awake_lock:
        alive = _awake is not None and _awake.poll() is None
        if want and not alive:
            cmd = keep_awake_command(os.getpid())
            if cmd:
                try:
                    _awake = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                except OSError:
                    _awake = None
        elif not want and alive:
            _awake.terminate()
            _awake = None
        return {"supported": keep_awake_command(os.getpid()) is not None and not hc.in_container(),
                "active": _awake is not None and _awake.poll() is None}


# --------------------------------------------------------------------------- controls (this computer only)
def _require_local(request: Request, write: bool = False) -> None:
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not is_local(request.client.host if request.client else "", headers):
        raise HTTPException(403, "Only on the computer that runs ZEUS")
    if write:
        if headers.get("x-zeus-client") != "1":
            raise HTTPException(403, "missing X-Zeus-Client header")
        origin = headers.get("origin")
        if origin and (urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            raise HTTPException(403, "cross-origin request refused")


def _port(request: Request) -> int:
    """The port this browser uses, which is also what `tailscale serve` must forward to
    (in Docker: the port published with -p, not the one inside the container)."""
    return request.url.port or int(os.environ.get("PORT") or hc.get("port") or 8765)


@router.get("/api/remote/status")
async def remote_status(request: Request):
    _require_local(request)
    port = _port(request)
    ts = await run_in_threadpool(tailscale_status, port)
    lan = lan_url(port)
    url = ts["url"] if ts["serve"] else lan
    code = access_code()
    on = enabled()
    return {"enabled": on, "code": code, "port": port, "container": hc.in_container(),
            "listen": str(hc.get("host") or "127.0.0.1"), "tailscale": ts, "lan": lan, "url": url,
            "qr": qr_svg(url + "/login#code=" + code) if (on and url) else "",
            "serve_cmd": " ".join(serve_command(port, exe="tailscale")),
            "keep_awake": await run_in_threadpool(sync_keep_awake)}


@router.post("/api/remote/code")
async def remote_new_code(request: Request):
    _require_local(request, write=True)
    return {"code": new_access_code()}


@router.post("/api/remote/signout")
async def remote_sign_out(request: Request):
    _require_local(request, write=True)
    sign_out_all()
    return {"ok": True}


@router.post("/api/remote/tailscale")
async def remote_tailscale(request: Request):
    _require_local(request, write=True)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await run_in_threadpool(tailscale_serve, _port(request), bool((body or {}).get("on", True)))


def install(app) -> None:
    """Put the gate in front of every route and add the login and control routes."""
    app.add_middleware(AccessMiddleware)
    app.include_router(router)
    app.on_event("startup")(sync_keep_awake)

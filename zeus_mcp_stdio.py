#!/usr/bin/env python3
"""stdio bridge to ZEUS's MCP server, for clients that launch a command (Claude Desktop and others).

    python3 zeus_mcp_stdio.py            # run by the client; talks MCP on stdin / stdout
    python3 zeus_mcp_stdio.py --check    # is ZEUS reachable? lists the tools

Every JSON-RPC message read from stdin is posted to the running terminal's endpoint
(http://127.0.0.1:<port>/mcp) and the reply is written to stdout, one message per
line. The bridge holds no state of its own beyond the protocol version a legacy client
negotiated, so modern (2026-07-28) and legacy (initialize-based) clients both work.
It uses only the Python standard library: any Python 3.8+ can run it, no install needed.

The address comes from --url, else ZEUS_MCP_URL, else the port in ZEUS_PORT or in
~/.zeus-terminal/config.json (ZEUS_HOME moves that folder), else 8765.
ZEUS itself must be running; when it is not, every request gets a clear error.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

K_VERSION = "io.modelcontextprotocol/protocolVersion"
K_CAPS = "io.modelcontextprotocol/clientCapabilities"
K_CLIENT = "io.modelcontextprotocol/clientInfo"
MODERN = "2026-07-28"
TIMEOUT = 180


def default_url() -> str:
    if os.environ.get("ZEUS_MCP_URL"):
        return os.environ["ZEUS_MCP_URL"]
    port = os.environ.get("ZEUS_PORT", "").strip()
    if not port:
        home = Path(os.environ.get("ZEUS_HOME") or (Path.home() / ".zeus-terminal")).expanduser()
        try:
            port = str(json.loads((home / "config.json").read_text(encoding="utf-8")).get("port") or "").strip()
        except (OSError, ValueError, AttributeError):
            port = ""
    return f"http://127.0.0.1:{port or 8765}/mcp"


def header_value(v: str) -> str:
    """Plain ASCII as is; anything else (or anything that looks like the sentinel) as =?base64?…?=."""
    looks_encoded = v.startswith("=?base64?") and v.endswith("?=")
    if all(32 <= ord(c) < 127 for c in v) and v == v.strip() and not looks_encoded:
        return v
    return "=?base64?" + base64.b64encode(v.encode("utf-8")).decode("ascii") + "?="


class Bridge:
    def __init__(self, url: str, out=None):
        self.url = url
        self.out = out if out is not None else sys.stdout.buffer
        self.lock = threading.Lock()
        self.legacy_version: str | None = None
        self.cancelled: set = set()

    # ------------------------------------------------------------ output
    def write(self, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def error(self, mid, message: str, code: int = -32603) -> None:
        self.write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    # ------------------------------------------------------------ HTTP
    def headers_for(self, msg: dict) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        method = msg.get("method")
        if meta.get(K_VERSION):
            h["MCP-Protocol-Version"] = str(meta[K_VERSION])
            if isinstance(method, str):
                h["Mcp-Method"] = method
            if method in ("tools/call", "prompts/get", "resources/read"):
                name = params.get("uri" if method == "resources/read" else "name")
                if name is not None:
                    h["Mcp-Name"] = header_value(str(name))
        elif self.legacy_version and method != "initialize":
            h["MCP-Protocol-Version"] = self.legacy_version
        return h

    def post(self, msg) -> tuple[int, bytes]:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers=self.headers_for(msg) if isinstance(msg, dict) else
                                     {"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @staticmethod
    def replies(body: bytes) -> list:
        text = body.decode("utf-8", "replace").strip()
        if not text:
            return []
        if text.startswith(("event:", "data:", ":")):  # an SSE stream: keep the data lines
            out = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        out.append(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
            return out
        try:
            obj = json.loads(text)
        except ValueError:
            return []
        return obj if isinstance(obj, list) else [obj]

    # ------------------------------------------------------------ one message
    def handle(self, msg) -> None:
        if not isinstance(msg, dict):
            self.error(None, "Invalid Request", -32600)
            return
        mid = msg.get("id")
        is_request = "id" in msg and isinstance(msg.get("method"), str)
        if msg.get("method") == "notifications/cancelled":
            self.cancelled.add((msg.get("params") or {}).get("requestId"))
            return
        try:
            status, body = self.post(msg)
        except (urllib.error.URLError, OSError) as e:
            if is_request:
                reason = getattr(e, "reason", e)
                self.error(mid, f"ZEUS Terminal is not running at {self.url} ({reason}). "
                                "Start ZEUS (start.command, start.sh or start.bat) and try again.")
            return
        if not is_request:
            return
        if mid in self.cancelled:
            self.cancelled.discard(mid)
            return
        replies = self.replies(body)
        if not replies:
            self.error(mid, f"ZEUS answered HTTP {status} without a JSON-RPC reply")
            return
        for reply in replies:
            res = reply.get("result") if isinstance(reply, dict) else None
            if msg.get("method") == "initialize" and isinstance(res, dict) and res.get("protocolVersion"):
                self.legacy_version = str(res["protocolVersion"])
            self.write(reply)


def check(url: str) -> int:
    """Human-readable connection test."""
    b = Bridge(url, out=open(os.devnull, "wb"))
    meta = {K_VERSION: MODERN, K_CAPS: {}, K_CLIENT: {"name": "zeus-mcp-check", "version": "1"}}
    try:
        _, body = b.post({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}})
        found = b.replies(body)
        _, body = b.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}})
        tools = b.replies(body)
    except (urllib.error.URLError, OSError) as e:
        print(f"✗ ZEUS Terminal is not reachable at {url}: {getattr(e, 'reason', e)}")
        print("  Start ZEUS first (start.command, start.sh or start.bat).")
        return 1
    if not found or "error" in found[0]:
        print(f"✗ {url} answered, but not as ZEUS's MCP server: {found[0].get('error') if found else 'no reply'}")
        return 1
    info = found[0]["result"].get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
    names = [t["name"] for t in (tools[0].get("result") or {}).get("tools", [])] if tools else []
    print(f"✓ {info.get('title', 'ZEUS')} {info.get('version', '')} at {url}")
    print(f"  protocols: {', '.join(found[0]['result'].get('supportedVersions', []))}")
    print(f"  {len(names)} tools: {', '.join(names)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="stdio bridge to ZEUS Terminal's MCP server")
    ap.add_argument("--url", help="MCP endpoint (default: http://127.0.0.1:<ZEUS port>/mcp)")
    ap.add_argument("--check", action="store_true", help="test the connection and list the tools")
    args = ap.parse_args(argv)
    url = args.url or default_url()
    if args.check:
        return check(url)
    bridge = Bridge(url)
    pending = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for raw in iter(sys.stdin.buffer.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                bridge.error(None, "Parse error", -32700)
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                pending.append(pool.submit(bridge.handle, m))
            pending = [f for f in pending if not f.done()]
        # stdin closed: give in-flight requests a moment, then leave promptly
        wait(pending, timeout=5)
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""E-mail alerts: every alert ZEUS raises on the desktop can also arrive by e-mail.

ZEUS modules raise alerts by running ``osascript -e 'display notification ...'``
or ``terminal-notifier -title ... -message ...``. When e-mail is set up in
Settings, zeus_config.apply_notification_shims() puts small relay scripts with
those two names first on PATH. A relay hands the title and text to
``python -m zeus_mail send`` in the background, then runs the real notifier if
desktop notifications are on. No module has to know about e-mail, and Linux
users, who have no desktop notifications here, get their alerts by mail.

Repeats of the same alert within 30 minutes are skipped, and at most 20 alerts
are mailed per hour.

    python -m zeus_mail test          send a test e-mail
    python -m zeus_mail send T BODY   send one alert (what the relays call)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import zeus_config as zc

REPEAT_WINDOW = 30 * 60      # seconds: the same alert is mailed once per window
HOURLY_CAP = 20              # alerts mailed per hour at most
TIMEOUT = 20                 # seconds for the SMTP conversation
REPO_ROOT = Path(__file__).resolve().parent


# ----------------------------------------------------------------- settings
def recipients() -> list:
    return [a for a in re.split(r"[\s,;]+", str(zc.get("email_to") or "")) if "@" in a]


def configured() -> bool:
    return zc.email_configured()


def _port() -> int:
    try:
        return int(str(zc.get("email_smtp_port") or "587").strip())
    except ValueError:
        return 587


# ----------------------------------------------------------------- sending
def send(subject: str, body: str) -> None:
    """Send one e-mail now. Raises on any SMTP or configuration error."""
    if not configured():
        raise RuntimeError("fill in sender, recipient, app password and SMTP server first")
    sender = str(zc.get("email_from")).strip()
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients())
    msg["Subject"] = f"ZEUS · {subject}".strip()
    # quoted-printable keeps Chinese alert text intact on servers without 8BITMIME
    msg.set_content(f"{body}\n\n—\nSent by ZEUS Terminal (Settings → E-mail alerts).", cte="quoted-printable")
    host, port = str(zc.get("email_smtp_server")).strip(), _port()
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    with server:
        server.ehlo()
        if port != 465 and server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        if server.has_extn("auth"):
            server.login(sender, str(zc.get("email_app_password")))
        server.send_message(msg)


def _state_path() -> Path:
    return zc.home() / "mail_state.json"


def _should_send(title: str, body: str, now: float) -> bool:
    """Skip repeats and enforce the hourly cap; remember what was sent."""
    path = _state_path()
    try:
        sent = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        sent = []
    sent = [(t, k) for t, k in sent if now - t < max(REPEAT_WINDOW, 3600)]
    key = hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()
    if any(k == key and now - t < REPEAT_WINDOW for t, k in sent):
        return False
    if sum(1 for t, _ in sent if now - t < 3600) >= HOURLY_CAP:
        return False
    sent.append((now, key))
    try:
        path.write_text(json.dumps(sent), encoding="utf-8")
    except Exception:
        pass
    return True


def relay(title: str, body: str, now: Optional[float] = None) -> bool:
    """Mail one alert unless it is a repeat or over the hourly cap. Never raises."""
    if not configured() or not (title or body):
        return False
    if not _should_send(title, body, time.time() if now is None else now):
        return False
    try:
        send(title or "Alert", body or title)
        return True
    except Exception as e:  # noqa: BLE001 — an alert must never crash its caller
        print(f"[zeus_mail] could not send: {e}", file=sys.stderr)
        return False


# ----------------------------------------------------------------- reading a notifier call
_AS_STRING = r'"((?:[^"\\]|\\.)*)"'


def _unquote(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def parse_notification(tool: str, argv: list) -> Optional[tuple]:
    """(title, body) of an osascript / terminal-notifier notification, else None."""
    if tool == "terminal-notifier":
        opts, i = {}, 0
        while i < len(argv):
            a = argv[i]
            if a.startswith("-") and i + 1 < len(argv):
                opts[a.lstrip("-").lower()] = argv[i + 1]
                i += 2
            else:
                i += 1
        if "message" not in opts and "title" not in opts:
            return None
        body = opts.get("message", "")
        if opts.get("subtitle"):
            body = f"{opts['subtitle']}\n{body}".strip()
        return opts.get("title", "") or "Alert", body
    if tool == "osascript":
        script = " ".join(argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-e")
        m = re.search(r"display notification\s+" + _AS_STRING, script)
        if not m:
            return None
        body = _unquote(m.group(1))
        t = re.search(r"with title\s+" + _AS_STRING, script)
        sub = re.search(r"subtitle\s+" + _AS_STRING, script)
        if sub:
            body = f"{_unquote(sub.group(1))}\n{body}".strip()
        return (_unquote(t.group(1)) if t else "Alert"), body
    return None


# ----------------------------------------------------------------- relay script entry point
def _real_binary(name: str) -> Optional[str]:
    skip = {str(zc.home() / "bin" / "relay"), str(zc.home() / "bin" / "quiet")}
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or d in skip:
            continue
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def shim(tool: str, argv: list) -> int:
    """Called by the relay scripts: mail the alert (in the background), then
    show it on the desktop when desktop notifications are on."""
    note = parse_notification(tool, argv)
    if note and configured():
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])))
        subprocess.Popen([sys.executable, "-m", "zeus_mail", "send", note[0], note[1]], cwd=str(REPO_ROOT), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    if zc.notifications_enabled():
        real = _real_binary(tool)
        if real:
            os.execv(real, [real] + list(argv))
    return 0


def main(argv: list) -> int:
    if argv[:1] == ["test"]:
        try:
            send("Test e-mail", "If you can read this, ZEUS e-mail alerts work.")
            print("sent to " + ", ".join(recipients()))
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"failed: {e}", file=sys.stderr)
            return 1
    if argv[:1] == ["send"] and len(argv) >= 3:
        return 0 if relay(argv[1], argv[2]) else 1
    if argv[:1] == ["shim"] and len(argv) >= 2:
        return shim(argv[1], argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""E-mail alerts: reading notifier calls, sending over SMTP, repeats and the
hourly cap, the PATH relays, and the settings test button."""
import os
import sys

import pytest

import zeus_config as zc
import zeus_mail as zm

W = {"X-Zeus-Client": "1"}
MAIL = {"email_from": "me@example.com", "email_to": "me@example.com, phone@example.org",
        "email_app_password": "app-pass", "email_smtp_server": "smtp.example.com", "email_smtp_port": "587"}


@pytest.fixture()
def mail_settings():
    zc.update(MAIL)
    yield
    zc.update({k: "" for k in MAIL} | {"email_smtp_server": "smtp.gmail.com", "email_smtp_port": "587"})


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port, type(self).__name__)]
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def has_extn(self, name):
        return name in ("starttls", "auth")

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture()
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(zm.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(zm.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


# ----------------------------------------------------------------- reading notifier calls
def test_parse_osascript_notification():
    script = 'display notification "AAPL reached 80% of max profit" with title "Take profit" subtitle "Bull call 200/210" sound name "Glass"'
    assert zm.parse_notification("osascript", ["-e", script]) == ("Take profit", "Bull call 200/210\nAAPL reached 80% of max profit")
    quoted = r'display notification "He said \"sell\"" with title "Note"'
    assert zm.parse_notification("osascript", ["-e", quoted]) == ("Note", 'He said "sell"')


def test_parse_terminal_notifier():
    argv = ["-title", "Risk radar", "-subtitle", "FOMC today", "-message", "3 events", "-open", "http://localhost:8765/"]
    assert zm.parse_notification("terminal-notifier", argv) == ("Risk radar", "FOMC today\n3 events")


def test_other_osascript_calls_are_not_alerts():
    assert zm.parse_notification("osascript", ["-e", 'tell application "Terminal" to activate']) is None
    assert zm.parse_notification("terminal-notifier", ["-remove", "ALL"]) is None


# ----------------------------------------------------------------- sending
def test_send_uses_starttls_and_login(mail_settings, smtp):
    zm.send("Take profit", "AAPL reached 80%")
    msg = smtp.sent[-1]
    assert msg["Subject"] == "ZEUS · Take profit" and msg["To"] == "me@example.com, phone@example.org"
    assert "AAPL reached 80%" in msg.get_content()
    assert [c[0] for c in smtp.last.calls] == ["connect", "ehlo", "starttls", "ehlo", "login"]
    assert smtp.last.calls[-1] == ("login", "me@example.com", "app-pass")


def test_port_465_uses_ssl(mail_settings, smtp):
    zc.update({"email_smtp_port": "465"})
    zm.send("T", "B")
    assert smtp.last.calls[0][3] == "FakeSMTPSSL" and ("starttls",) not in smtp.last.calls


def test_send_needs_settings(smtp):
    with pytest.raises(RuntimeError):
        zm.send("T", "B")


def test_repeats_and_hourly_cap(mail_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(zm, "_state_path", lambda: tmp_path / "mail_state.json")
    sent = []
    monkeypatch.setattr(zm, "send", lambda s, b: sent.append((s, b)))
    assert zm.relay("Take profit", "AAPL", now=1000)
    assert not zm.relay("Take profit", "AAPL", now=1000 + 60)                # repeat within 30 minutes
    assert zm.relay("Take profit", "AAPL", now=1000 + zm.REPEAT_WINDOW + 1)  # later: sent again
    for i in range(zm.HOURLY_CAP - 2):
        assert zm.relay("Alert", f"#{i}", now=2000 + i)
    assert not zm.relay("Alert", "one too many", now=2100)
    assert len(sent) == zm.HOURLY_CAP


def test_relay_never_raises(mail_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(zm, "_state_path", lambda: tmp_path / "mail_state.json")

    def boom(s, b):
        raise OSError("network down")
    monkeypatch.setattr(zm, "send", boom)
    assert zm.relay("T", "B", now=1) is False


# ----------------------------------------------------------------- PATH relays
@pytest.mark.skipif(os.name == "nt", reason="relays are shell scripts")
def test_relays_go_first_on_path_when_mail_is_set(mail_settings, monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    zc.apply_notification_shims()
    relay = zc.home() / "bin" / "relay"
    assert os.environ["PATH"].split(os.pathsep)[0] == str(relay)
    script = (relay / "osascript").read_text(encoding="utf-8")
    assert "-m zeus_mail shim osascript" in script and os.access(relay / "osascript", os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="relays are shell scripts")
def test_no_relays_without_mail(monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    zc.apply_notification_shims()
    assert str(zc.home() / "bin" / "relay") not in os.environ["PATH"].split(os.pathsep)


def test_shim_mails_in_the_background_and_quiet_without_desktop(mail_settings, monkeypatch):
    spawned = []
    monkeypatch.setattr(zm.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(zc, "notifications_enabled", lambda: False)
    assert zm.shim("osascript", ["-e", 'display notification "Body" with title "Title"']) == 0
    assert spawned == [[sys.executable, "-m", "zeus_mail", "send", "Title", "Body"]]
    spawned.clear()
    assert zm.shim("osascript", ["-e", "beep"]) == 0 and spawned == []


# ----------------------------------------------------------------- settings test button
@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import zeus_routes
    app = FastAPI()
    app.include_router(zeus_routes.router)
    return TestClient(app)


def test_test_button_reports_missing_settings(client):
    r = client.post("/api/zeus/test/email", headers=W).json()
    assert r["ok"] is False and "save" in r["error"]


def test_test_button_sends(client, mail_settings, smtp):
    r = client.post("/api/zeus/test/email", headers=W).json()
    assert r == {"ok": True, "sent_to": "2 address(es)"}
    assert smtp.sent[-1]["Subject"] == "ZEUS · Test e-mail"

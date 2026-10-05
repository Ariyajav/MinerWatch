"""Email alerts: which conditions fire, that each is sent once, and the settings API."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from minerwatch import alerts
from minerwatch.alerts import AlertMonitor, AlertSettings, evaluate, merge_update
from minerwatch.models import Miner, Range, Schedule, SleepBackend, SleepConfig, Window
from minerwatch.store import init_db
from minerwatch.web import make_server, open_readonly, snapshot

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def miner(mid, sleep=False):
    window = Window(days=frozenset(range(7)), ranges=[Range(start=0, end=1440)])
    cfg = SleepConfig(enabled=True, backend=SleepBackend.CGMINER) if sleep else SleepConfig()
    return Miner(id=mid, host="10.0.0.1", port=4028,
                 schedule=Schedule(timezone=timezone.utc, windows=[window]), sleep=cfg)


MINERS = {"m1": miner("m1"), "m2": miner("m2", sleep=True)}


def add(conn, mid, minutes_ago, state, action, reason=None, ghs=None):
    conn.execute(
        "INSERT INTO events (ts, miner, state, action, reason, ghs) VALUES (?, ?, ?, ?, ?, ?)",
        ((NOW - timedelta(minutes=minutes_ago)).isoformat(), mid, state, action, reason, ghs),
    )
    conn.commit()


def polls(conn, mid, minutes, state="stopped", action="alert"):
    for i in range(minutes * 4, -1, -1):  # every 15s, oldest first, ending now
        add(conn, mid, i / 4, state, action, "zero hashrate" if action == "alert" else None,
            0 if action == "alert" else 140000)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "events.db")
    conn = init_db(path)
    yield path, conn
    conn.close()


def active(conn, settings=None):
    settings = settings or AlertSettings()
    return evaluate(snapshot(conn, MINERS, 15, now=NOW), settings, conn, NOW)


class TestConditions:
    def test_a_healthy_fleet_raises_nothing(self, db):
        _, conn = db
        polls(conn, "m1", 60, "mining", "none")
        polls(conn, "m2", 60, "mining", "none")
        assert active(conn) == {}

    def test_not_mining_fires_only_after_the_configured_minutes(self, db):
        _, conn = db
        polls(conn, "m2", 60, "mining", "none")
        polls(conn, "m1", 30)  # 30 minutes of failure, threshold 45
        assert "down:m1" not in active(conn)
        polls(conn, "m1", 50)
        assert "down:m1" in active(conn)
        assert "down:m1" not in active(conn, AlertSettings(not_mining_minutes=0))

    def test_one_good_poll_in_the_span_means_not_down(self, db):
        _, conn = db
        polls(conn, "m2", 60, "mining", "none")
        polls(conn, "m1", 60)
        add(conn, "m1", 20, "mining", "none", ghs=140000)
        assert "down:m1" not in active(conn)

    def test_latched_and_unsure(self, db):
        _, conn = db
        polls(conn, "m2", 5, "mining", "none")
        add(conn, "m1", 30, "stopped", "needs_attention", "restart limit reached")
        add(conn, "m2", 10, "mining", "sleep_failed", "HTTP 500")
        polls(conn, "m1", 5)
        found = active(conn)
        assert "latched:m1" in found and "unsure:m2" in found

    def test_a_stale_supervisor_reports_only_that(self, db):
        _, conn = db
        add(conn, "m1", 30, "stopped", "needs_attention")
        add(conn, "m1", 20, "stopped", "alert", ghs=0)
        found = active(conn)
        assert list(found) == ["stale"]


class Outbox:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, settings, subject, body):
        if self.fail:
            raise OSError("connection refused")
        self.sent.append((subject, body))


def enabled_settings(path, **kw):
    s = AlertSettings(enabled=True, smtp_host="smtp.example.com", from_addr="mw@example.com",
                      to_addrs=["me@example.com"], **kw)
    alerts.save_settings(path, s)
    return s


class TestMonitor:
    def make(self, tmp_path, db_path, outbox):
        settings_path = str(tmp_path / "alerts.json")
        return settings_path, AlertMonitor(settings_path, db_path, MINERS, 15, snapshot,
                                           open_readonly, sender=outbox)

    def test_sends_once_then_resolves(self, tmp_path, db):
        path, conn = db
        outbox = Outbox()
        settings_path, mon = self.make(tmp_path, path, outbox)
        enabled_settings(settings_path)
        polls(conn, "m2", 5, "mining", "none")
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        mon.check_once(NOW)
        mon.check_once(NOW + timedelta(minutes=1))
        assert len(outbox.sent) == 1 and "m1 is latched" in outbox.sent[0][0]

        add(conn, "m1", -2, "unknown", "attention_cleared")
        add(conn, "m1", -2, "mining", "none", ghs=140000)
        add(conn, "m2", -2, "mining", "none", ghs=140000)
        mon.check_once(NOW + timedelta(minutes=3))
        assert len(outbox.sent) == 2 and outbox.sent[1][0].startswith("Resolved")

    def test_a_stale_supervisor_is_not_a_false_all_clear(self, tmp_path, db):
        path, conn = db
        outbox = Outbox()
        settings_path, mon = self.make(tmp_path, path, outbox)
        enabled_settings(settings_path)
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        polls(conn, "m2", 5, "mining", "none")
        mon.check_once(NOW)
        # No new polls for ten minutes: the supervisor has stopped.
        mon.check_once(NOW + timedelta(minutes=10))
        assert len(outbox.sent) == 2
        assert "Supervisor is not polling" in outbox.sent[1][0]
        assert "RESOLVED" not in outbox.sent[1][1]
        assert "latched:m1" in mon._sent

    def test_a_restarted_dashboard_does_not_resend(self, tmp_path, db):
        path, conn = db
        outbox = Outbox()
        settings_path, mon = self.make(tmp_path, path, outbox)
        enabled_settings(settings_path)
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        polls(conn, "m2", 5, "mining", "none")
        mon.check_once(NOW)
        _, again = self.make(tmp_path, path, outbox)
        again.check_once(NOW + timedelta(minutes=1))
        assert len(outbox.sent) == 1

    def test_reminder_after_repeat_hours(self, tmp_path, db):
        path, conn = db
        outbox = Outbox()
        settings_path, mon = self.make(tmp_path, path, outbox)
        enabled_settings(settings_path, repeat_hours=1)
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        polls(conn, "m2", 5, "mining", "none")
        mon.check_once(NOW)
        # Evaluate against the same rows an hour later: keep the supervisor
        # fresh by checking "now" while pretending the clock moved for the
        # bookkeeping only.
        mon._sent["latched:m1"]["last_sent"] = (NOW - timedelta(hours=2)).isoformat()
        mon.check_once(NOW)
        assert len(outbox.sent) == 2 and "STILL ACTIVE" in outbox.sent[1][1]

    def test_a_failed_send_is_retried_and_reported(self, tmp_path, db):
        path, conn = db
        outbox = Outbox(fail=True)
        settings_path, mon = self.make(tmp_path, path, outbox)
        enabled_settings(settings_path)
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        polls(conn, "m2", 5, "mining", "none")
        mon.check_once(NOW)
        assert "connection refused" in mon.status["last_error"]
        outbox.fail = False
        mon.check_once(NOW)
        assert len(outbox.sent) == 1 and mon.status["last_error"] is None

    def test_disabled_sends_nothing_but_still_reports_status(self, tmp_path, db):
        path, conn = db
        outbox = Outbox()
        settings_path, mon = self.make(tmp_path, path, outbox)
        add(conn, "m1", 30, "stopped", "needs_attention")
        polls(conn, "m1", 5)
        mon.check_once(NOW)
        assert outbox.sent == [] and mon.status["active"]


class TestSettings:
    def test_blank_password_keeps_the_stored_one_and_is_never_shown(self):
        current = AlertSettings(password="s3cret")
        updated = merge_update(current, {"password": "", "to_addrs": "a@x.com; b@x.com",
                                         "smtp_port": "465"})
        assert updated.password == "s3cret"
        assert updated.to_addrs == ["a@x.com", "b@x.com"] and updated.smtp_port == 465
        public = updated.public()
        assert "password" not in public and public["password_set"] is True

    def test_enabled_needs_a_server_and_recipients(self):
        problems = AlertSettings(enabled=True).validate()
        assert any("smtp_host" in p for p in problems)
        assert any("to_addrs" in p for p in problems)


@pytest.fixture
def server(tmp_path, db):
    path, conn = db
    add(conn, "m1", 0, "mining", "none", ghs=140000)
    settings_path = str(tmp_path / "alerts.json")
    srv = make_server("127.0.0.1", 0, path, MINERS, 15, settings_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", settings_path
    srv.shutdown()
    srv.server_close()


def post(url, body, headers=None):
    hdrs = {"Content-Type": "application/json", "X-MinerWatch": "1"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hdrs,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


FORM = {"enabled": True, "smtp_host": "smtp.example.com", "smtp_port": "587",
        "security": "starttls", "username": "u", "password": "pw",
        "from_addr": "mw@example.com", "to_addrs": "me@example.com"}


class TestSettingsApi:
    def test_save_then_read_back_without_the_password(self, server):
        base, settings_path = server
        assert post(base + "/api/alerts", FORM)["ok"]
        with urllib.request.urlopen(base + "/api/alerts", timeout=5) as r:
            got = json.loads(r.read())
        assert got["settings"]["smtp_host"] == "smtp.example.com"
        assert got["settings"]["password_set"] and "password" not in got["settings"]
        assert got["can_edit"] is True
        assert json.load(open(settings_path, encoding="utf-8"))["password"] == "pw"

    def test_a_cross_site_style_post_is_refused(self, server):
        base, settings_path = server
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base + "/api/alerts", FORM, headers={"X-MinerWatch": ""})
        assert err.value.code == 403

    def test_invalid_settings_are_not_saved(self, server):
        base, settings_path = server
        with pytest.raises(urllib.error.HTTPError) as err:
            post(base + "/api/alerts", {**FORM, "to_addrs": ""})
        assert err.value.code == 400
        assert not __import__("os").path.exists(settings_path)

    def test_test_email_uses_the_form_as_typed(self, server, monkeypatch):
        base, _ = server
        seen = []
        monkeypatch.setattr(alerts, "send_email", lambda s, subj, body: seen.append(s))
        assert post(base + "/api/alerts/test", FORM)["sent_to"] == ["me@example.com"]
        assert seen[0].smtp_host == "smtp.example.com"


def test_send_email_speaks_starttls_and_login(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    s = AlertSettings(smtp_host="smtp.example.com", username="u", password="p",
                      from_addr="mw@example.com", to_addrs=["a@x.com", "b@x.com"])
    alerts.send_email(s, "Hello", "body")
    assert calls == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "u"),
                     ("send", "a@x.com, b@x.com", "[MinerWatch] Hello")]

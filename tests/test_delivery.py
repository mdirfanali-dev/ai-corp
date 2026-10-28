import threading
from datetime import timedelta

import pytest

from aicorp import attention, notify, redact
from aicorp.delivery import MAX_ATTEMPTS, Notifier, compose
from helpers import FakeClock, Recorder, block_task, staff
from aicorp import tasks


@pytest.fixture
def rig(home, con):
    (a, b, c) = staff(con, home, 3)
    tg, mail = Recorder("telegram"), Recorder("email")
    clock = FakeClock()
    notifier = Notifier({"telegram": tg, "email": mail}, clock=clock)
    class R: pass
    r = R()
    r.con, r.home, r.ids, r.tg, r.mail, r.clock, r.notifier = con, home, (a, b, c), tg, mail, clock, notifier
    def critical(title="Aria is blocked", key=None):
        return notify.upsert(con, key=key or f"k:{title}", category="blocked", severity="critical", title=title,
                             detail="Open it.", link="/tasks/T-1")[0]
    r.critical = critical
    return r


def statuses(con, nid):
    return {(d["channel"], d["status"]) for d in notify.deliveries(con, nid)}


def test_critical_goes_to_telegram_and_email_exactly_once(rig):
    nid = rig.critical()
    for _ in range(10):
        rig.notifier.pump(rig.con)
    assert len(rig.tg.sent) == 1 and len(rig.mail.sent) == 1
    assert statuses(rig.con, nid) == {("dashboard", "sent"), ("telegram", "sent"), ("email", "sent")}
    subject, text = rig.tg.sent[0]
    assert subject == "[AICorp] CRITICAL: Aria is blocked" and "Open: http://127.0.0.1:8765/tasks/T-1" in text and "Since:" in text


def test_info_and_warn_stay_on_the_dashboard_by_default(rig):
    for sev in ("info", "warn"):
        notify.upsert(rig.con, key=sev, category="review", severity=sev, title=sev)
    rig.notifier.pump(rig.con)
    assert rig.tg.sent == [] and rig.mail.sent == []


def test_routing_is_configurable(rig):
    notify.upsert(rig.con, key="w", category="review", severity="warn", title="warn item")
    notify.set_setting(rig.con, "routing", {"info": ["dashboard"], "warn": ["dashboard", "telegram"], "critical": ["dashboard", "email"]})
    rig.critical()
    rig.notifier.pump(rig.con)
    assert [s for s, _ in rig.tg.sent] == ["[AICorp] WARN: warn item"] and [s for s, _ in rig.mail.sent] == ["[AICorp] CRITICAL: Aria is blocked"]


def test_resolved_notifications_are_not_sent(rig):
    rig.critical(key="gone")
    notify.resolve_key(rig.con, "gone")
    rig.notifier.pump(rig.con)
    assert rig.tg.sent == []


def test_secrets_are_scrubbed_from_outgoing_messages(rig):
    rig.notifier.scrub = redact.Redactor(["hunter2-secret"]).scrub
    notify.upsert(rig.con, key="s", category="system", severity="critical", title="Login with hunter2-secret failed",
                  detail="password=hunter2-secret")
    rig.notifier.pump(rig.con)
    sent = rig.tg.sent[0][0] + rig.tg.sent[0][1]
    assert "hunter2-secret" not in sent and "[REDACTED]" in sent


def test_failed_delivery_is_recorded_then_retried_with_backoff(rig):
    rig.tg.fail = 2
    nid = rig.critical()
    rig.notifier.pump(rig.con)
    assert ("telegram", "failed") in statuses(rig.con, nid) and len(rig.mail.sent) == 1 and rig.tg.sent == []
    failed = [d for d in notify.deliveries(rig.con, nid) if d["channel"] == "telegram"][0]
    assert failed["error"] == "simulated outage"
    rig.notifier.pump(rig.con)                       # too soon: backoff not over
    assert rig.tg.sent == [] and rig.tg.fail == 1
    rig.clock.advance(61)
    rig.notifier.pump(rig.con)                       # second failure
    assert rig.tg.fail == 0 and rig.tg.sent == []
    rig.clock.advance(60)
    rig.notifier.pump(rig.con)                       # backoff doubled to 120 s: still waiting
    assert rig.tg.sent == []
    rig.clock.advance(61)
    rig.notifier.pump(rig.con)
    assert len(rig.tg.sent) == 1 and ("telegram", "sent") in statuses(rig.con, nid)
    assert len(rig.mail.sent) == 1                   # the healthy channel was never repeated


def test_gives_up_after_the_attempt_limit(rig):
    rig.tg.fail = 99
    nid = rig.critical()
    for _ in range(12):
        rig.notifier.pump(rig.con)
        rig.clock.advance(600)
    failures = [d for d in notify.deliveries(rig.con, nid) if d["channel"] == "telegram" and d["status"] == "failed"]
    assert len(failures) == MAX_ATTEMPTS


def test_unconfigured_channel_is_reported_not_hidden(rig):
    notifier = Notifier({"email": rig.mail}, clock=rig.clock)
    nid = rig.critical()
    notifier.pump(rig.con)
    row = [d for d in notify.deliveries(rig.con, nid) if d["channel"] == "telegram"][0]
    assert row["status"] == "failed" and row["error"] == "channel not configured"
    assert len(rig.mail.sent) == 1


def test_rate_limit_suppresses_a_storm_and_recovers(rig):
    notify.set_setting(rig.con, "rate_per_hour", 3)
    ids = [rig.critical(f"alert {i}") for i in range(5)]
    rig.notifier.pump(rig.con)
    assert len(rig.tg.sent) == 3
    suppressed = [i for i in ids if ("telegram", "suppressed") in statuses(rig.con, i)]
    assert len(suppressed) == 2
    d = [d for d in notify.deliveries(rig.con, suppressed[0]) if d["channel"] == "telegram"][0]
    assert "rate limit" in d["error"]
    rig.clock.advance(3700)
    rig.critical("a later alert")
    rig.notifier.pump(rig.con)
    assert len(rig.tg.sent) == 4 and rig.tg.sent[-1][0].endswith("a later alert")


def test_a_claim_left_by_a_dead_process_is_released_and_retried(rig):
    nid = rig.critical()
    rig.con.execute("INSERT INTO notification_delivery (notification_id, channel, status, attempted_at) VALUES (?, 'telegram', 'sending', ?)",
                    (nid, (rig.clock() - timedelta(minutes=10)).isoformat()))
    rig.notifier.pump(rig.con)
    rows = [(d["status"], d["error"]) for d in notify.deliveries(rig.con, nid) if d["channel"] == "telegram"]
    assert rows == [("failed", "interrupted while sending"), ("sent", None)]       # released, then retried (back-off had passed)
    assert len(rig.tg.sent) == 1
    rig.notifier.pump(rig.con)
    assert len(rig.tg.sent) == 1


def test_a_fresh_claim_by_a_live_process_is_left_alone(rig):
    nid = rig.critical()
    rig.con.execute("INSERT INTO notification_delivery (notification_id, channel, status, attempted_at) VALUES (?, 'telegram', 'sending', ?)",
                    (nid, rig.clock().isoformat()))
    rig.notifier.pump(rig.con)
    assert rig.tg.sent == []


def test_two_processes_pumping_at_once_send_each_message_once(home, con):
    from aicorp.config import db_path
    from aicorp.db import connect
    slow_tg, slow_mail = Recorder("telegram", delay=0.3), Recorder("email", delay=0.3)
    for i in range(3):
        notify.upsert(con, key=f"n{i}", category="system", severity="critical", title=f"alert {i}")
    def run():
        c = connect(db_path(home))
        Notifier({"telegram": slow_tg, "email": slow_mail}).pump(c)
    threads = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(slow_tg.sent) == 3 and len(slow_mail.sent) == 3


def test_send_direct_without_a_database(rig):
    result = rig.notifier.send_direct("subject", "body")
    assert result == {"telegram": "sent", "email": "sent"} and rig.tg.sent == [("subject", "body")]
    assert Notifier({}).send_direct("s", "b") == {"telegram": "not configured", "email": "not configured"}
    rig.tg.fail = 1
    assert "failed" in rig.notifier.send_direct("s", "b", ("telegram",))["telegram"]


def test_compose_includes_link_age_and_repeat_count():
    subject, text = compose({"severity": "warn", "title": "Hi", "detail": "d", "link": "/tasks/T-9", "created_at": "2026-01-01T12:30:00+00:00",
                             "repeat_count": 4}, "https://example.test/")
    assert subject == "[AICorp] WARN: Hi" and "Open: https://example.test/tasks/T-9" in text and "Seen 4 times." in text
    assert "2026-01-01 12:30:00 UTC" in text


def test_end_to_end_block_to_alert(rig):
    (a, *_r) = rig.ids
    t = tasks.create_task(rig.con, title="Research market", owner=a)
    block_task(rig.con, t, "CAPTCHA", "recaptcha")
    attention.sync(rig.con, rig.home)
    rig.notifier.pump(rig.con)
    subject, text = rig.tg.sent[0]
    assert "W1 is blocked: CAPTCHA" in subject and "/tasks/" + t in text

from datetime import datetime, timedelta, timezone

import pytest

from aicorp import attention, events, killswitch, notify, tasks
from helpers import block_task, staff


@pytest.fixture
def world(home, con):
    ids = staff(con, home, 3)
    return con, home, ids


def open_items(con):
    return [dict(r) for r in con.execute("SELECT * FROM notification WHERE resolved_at IS NULL ORDER BY id")]


def test_each_block_reason_gets_the_right_severity_and_wording(world):
    con, home, (a, b, c) = world
    t1 = tasks.create_task(con, title="Research market", owner=a)
    t2 = tasks.create_task(con, title="Draft plan", owner=b)
    t3 = tasks.create_task(con, title="Estimate cost", owner=c)
    block_task(con, t1, "CAPTCHA", "recaptcha on example.com")
    block_task(con, t2, "USAGE_LIMIT", "limit reached")
    block_task(con, t3, "LOGIN_REQUIRED")
    created = attention.sync(con, home)
    assert len(created) == 3
    by_task = {n["task_id"]: n for n in open_items(con)}
    assert by_task[t1]["severity"] == "critical" and by_task[t1]["category"] == "blocked"
    assert "W1 is blocked: CAPTCHA" in by_task[t1]["title"] and "solve the CAPTCHA" in by_task[t1]["detail"]
    assert "recaptcha on example.com" in by_task[t1]["detail"] and by_task[t1]["link"] == f"/tasks/{t1}"
    assert by_task[t2]["severity"] == "warn" and "usage limit" in by_task[t2]["title"]
    assert by_task[t3]["severity"] == "critical" and "login" in by_task[t3]["title"]


def test_sync_is_idempotent_and_does_not_flood_the_log(world):
    con, home, (a, *_r) = world
    t = tasks.create_task(con, title="Stuck", owner=a)
    block_task(con, t, "CAPTCHA")
    before = None
    for i in range(20):
        attention.sync(con, home)
        if i == 0:
            before = con.execute("SELECT count(*) FROM event").fetchone()[0]
    items = open_items(con)
    assert len(items) == 1 and items[0]["repeat_count"] == 20
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] == before      # no new events after the first pass
    assert [d["channel"] for d in notify.deliveries(con, items[0]["id"])] == ["dashboard"]


def test_notifications_resolve_when_the_cause_goes_away(world):
    con, home, (a, b, _c) = world
    blocked = tasks.create_task(con, title="Blocked one", owner=a)
    failing = tasks.create_task(con, title="Failing one", owner=b)
    block_task(con, blocked, "CAPTCHA")
    tasks.transition(con, failing, "failed", actor=tasks.FOUNDER, summary="f", from_states={"assigned"}) if False else None
    con.execute("UPDATE task SET status = 'failed', attempts = 3 WHERE task_id = ?", (failing,))
    attention.sync(con, home)
    assert {n["category"] for n in open_items(con)} == {"blocked", "failed"}
    tasks.resolve_block(con, blocked)
    tasks.retry_failed(con, failing)
    attention.sync(con, home)
    assert open_items(con) == []
    assert con.execute("SELECT count(*) FROM notification WHERE resolved_at IS NOT NULL").fetchone()[0] == 2
    # a new block of the same task is a new cause, so a new notification
    block_task(con, blocked, "CAPTCHA")
    attention.sync(con, home)
    assert len(open_items(con)) == 1


def test_failed_approval_and_review_items(world):
    con, home, (a, b, c) = world
    failed = tasks.create_task(con, title="Failed job", owner=a)
    con.execute("UPDATE task SET status='failed', attempts=3 WHERE task_id=?", (failed,))
    approval = tasks.create_task(con, title="Publish post", owner=b, risk_tier=3)
    con.execute("UPDATE task SET status='awaiting_approval', result_summary='draft ready' WHERE task_id=?", (approval,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, 'now')", (approval,))
    review = tasks.create_task(con, title="Plain review", owner=c)
    con.execute("UPDATE task SET status='review' WHERE task_id=?", (review,))
    risky = tasks.create_task(con, title="Risky review", owner=c)
    con.execute("UPDATE task SET status='review' WHERE task_id=?", (risky,))
    for _ in range(2):
        events.append(con, type="guard.blocked", summary="blocked", task_id=risky, payload={})
    attention.sync(con, home)
    got = {n["task_id"]: n for n in open_items(con)}
    assert got[failed]["severity"] == "critical" and got[failed]["category"] == "failed"
    assert got[approval]["severity"] == "warn" and got[approval]["category"] == "approval" and "draft ready" in got[approval]["detail"]
    assert got[review]["severity"] == "info" and got[review]["category"] == "review"
    assert got[risky]["category"] == "guardrail" and "2 forbidden" in got[risky]["title"]


def test_dependency_blocks_are_quiet_info(world):
    con, home, (a, b, _c) = world
    first = tasks.create_task(con, title="First", owner=a)
    second = tasks.create_task(con, title="Second", owner=b, depends_on=[first])
    con.execute("UPDATE task SET status='failed' WHERE task_id=?", (first,))
    tasks.fail_dependents(con, first, "failed")
    attention.sync(con, home)
    by_task = {n["task_id"]: n for n in open_items(con)}
    assert by_task[second]["severity"] == "info" and "waiting for a failed task" in by_task[second]["title"]
    assert by_task[first]["severity"] == "critical"


def test_kill_switch_and_disk_conditions(world):
    con, home, _ = world
    attention.sync(con, home)
    assert open_items(con) == []
    killswitch.engage(con, home, reason="testing")
    attention.sync(con, home)
    assert [n["dedupe_key"] for n in open_items(con)] == ["killswitch"]
    killswitch.release(con, home)
    low = lambda p: (100_000_000_000, 4_000_000_000)             # 4 GB free of 100 GB
    critical = lambda p: (100_000_000_000, 500_000_000)
    attention.sync(con, home, disk_probe=low)
    item = open_items(con)[0]
    assert item["dedupe_key"] == "disk" and item["severity"] == "warn"
    attention.sync(con, home, disk_probe=critical)
    assert open_items(con)[0]["severity"] == "critical" and "New work is paused" in open_items(con)[0]["detail"]
    attention.sync(con, home, disk_probe=lambda p: (100_000_000_000, 50_000_000_000))
    assert open_items(con) == []
    assert attention.disk_level(home, critical)[0] == "critical" and attention.disk_level(home, low)[0] == "warn"


def test_worklist_is_ordered_most_urgent_then_oldest(world):
    con, home, (a, b, c) = world
    warn = tasks.create_task(con, title="Warn", owner=a)
    block_task(con, warn, "TIMEOUT")
    crit_new = tasks.create_task(con, title="Crit new", owner=b)
    block_task(con, crit_new, "CAPTCHA")
    crit_old = tasks.create_task(con, title="Crit old", owner=c)
    block_task(con, crit_old, "LOGIN_REQUIRED")
    con.execute("UPDATE block SET since = '2020-01-01T00:00:00' WHERE task_id = ?", (crit_old,))
    items = attention.worklist(con, home)
    assert [i["title"] for i in items] == ["Crit old", "Crit new", "Warn"]
    assert items[0]["actions"] == ["resolve", "cancel"] and items[0]["hint"]
    assert attention.worklist(con, home)[0]["who"] == "W3"


def test_usage_limit_resume_time_parsing():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert attention.parse_resume("Your limit resets in 2 hours", now) == now + timedelta(hours=2)
    assert attention.parse_resume("try again in 45 minutes", now) == now + timedelta(minutes=45)
    now.astimezone()
    at = attention.parse_resume("limit reached, resets at 5pm", now)
    assert at is not None and at.astimezone().hour == 17 and at > now
    at = attention.parse_resume("resets at 17:30", now)
    assert at.astimezone().hour == 17 and at.astimezone().minute == 30
    assert attention.parse_resume("resets at 25:99", now) is None
    for garbage in (None, "", "no idea when", "usage limit reached"):
        assert attention.parse_resume(garbage, now) is None


def test_usage_resume_backs_off_when_the_time_is_unknown(world):
    con, home, (a, *_r) = world
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    t = tasks.create_task(con, title="Limited", owner=a)
    waits = []
    for _ in range(6):
        block_task(con, t, "USAGE_LIMIT", "usage limit reached")
        waits.append((attention.usage_resume_time(con, t, "usage limit reached", now) - now).total_seconds())
        con.execute("UPDATE block SET resolved_at = 'x'")
        con.execute("UPDATE task SET status = 'assigned'")
    assert waits == [1800, 3600, 7200, 14400, 21600, 21600]
    parsed = attention.usage_resume_time(con, t, "resets in 3 hours", now)
    assert (parsed - now).total_seconds() == 3 * 3600 + 30


def test_auto_resume_puts_tasks_back_after_the_wait(world):
    con, home, (a, b, _c) = world
    due = tasks.create_task(con, title="Due", owner=a)
    later = tasks.create_task(con, title="Later", owner=b)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    block_task(con, due, "USAGE_LIMIT", resume_at=(now - timedelta(minutes=1)).isoformat())
    block_task(con, later, "USAGE_LIMIT", resume_at=(now + timedelta(hours=1)).isoformat())
    assert attention.auto_resume(con, now) == [due]
    assert tasks.get_task(con, due)["status"] == "assigned" and tasks.get_task(con, later)["status"] == "blocked"
    assert any(e["type"] == "task.block_resolved" for e in tasks.timeline(con, due))

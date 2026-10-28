from datetime import datetime, timedelta, timezone

import pytest

from aicorp import advisor, events, notify, scheduler
from aicorp.org import authorize
from helpers import staff

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)          # a Monday, 17:30 IST


def test_next_run_for_each_kind_of_spec():
    assert scheduler.next_run("every 15m", T0, IST) == T0 + timedelta(minutes=15)
    assert scheduler.next_run("every 2h", T0, IST) == T0 + timedelta(hours=2)
    assert scheduler.next_run("every 1d", T0, IST) == T0 + timedelta(days=1)
    assert scheduler.next_run("daily 18:00", T0, IST).astimezone(IST).strftime("%a %H:%M %d") == "Mon 18:00 05"
    assert scheduler.next_run("daily 08:00", T0, IST).astimezone(IST).strftime("%a %H:%M %d") == "Tue 08:00 06"
    assert scheduler.next_run("weekly mon 09:00", T0, IST).astimezone(IST).strftime("%a %H:%M %d") == "Mon 09:00 12"
    assert scheduler.next_run("weekly fri 09:00", T0, IST).astimezone(IST).strftime("%a %H:%M %d") == "Fri 09:00 09"
    exactly = datetime(2026, 1, 5, 3, 30, tzinfo=timezone.utc)      # 09:00 IST on the dot → strictly after
    assert scheduler.next_run("daily 09:00", exactly, IST).astimezone(IST).day == 6


@pytest.mark.parametrize("bad", ["", "hourly", "every 0m", "every m", "daily 25:00", "daily 8", "weekly xyz 09:00", "weekly mon 09:99", "every 99999h", "daily 08:00 now"])
def test_bad_specs_are_rejected(bad):
    with pytest.raises(ValueError):
        scheduler.parse_spec(bad)


def test_defaults_are_created_once_and_runnable_jobs_are_registered(con):
    from aicorp import advisor  # noqa
    scheduler.ensure_defaults(con, T0, IST)
    scheduler.ensure_defaults(con, T0 + timedelta(days=9), IST)
    rows = {r["job"]: r for r in con.execute("SELECT * FROM schedule")}
    assert set(rows) == {"scan_active", "nag_check", "daily_briefing", "weekly_report", "project_health", "meeting_prep", "inbox_digest", "maintenance", "backup"}
    assert rows["scan_active"]["next_run"] > T0.isoformat() and rows["daily_briefing"]["spec"] == "daily 08:00"


def test_jobs_run_when_due_and_only_once(home, con):
    staff(con, home, 1)
    notify.set_setting(con, "away_threshold_minutes", 120)
    first = scheduler.run_due(con, home, T0, tz=IST)
    assert first == []                                          # first look: jobs are scheduled into the future
    later = T0 + timedelta(minutes=16)
    ran = scheduler.run_due(con, home, later, tz=IST)
    assert {r["job"] for r in ran} == {"scan_active", "nag_check"}
    by = {r["job"]: r for r in ran}
    assert all(r["status"] == "ok" for r in ran)
    assert not by["scan_active"]["catch_up"] and by["nag_check"]["catch_up"]       # 1 min late vs. 11 min late (over the 5 min grace)
    assert scheduler.run_due(con, home, later + timedelta(seconds=5), tz=IST) == []          # claimed: not again
    assert con.execute("SELECT count(*) FROM job_run WHERE job = 'scan_active'").fetchone()[0] == 1
    nxt = con.execute("SELECT next_run FROM schedule WHERE job = 'scan_active'").fetchone()[0]
    assert datetime.fromisoformat(nxt) == later + timedelta(minutes=15)


def test_two_processes_cannot_run_the_same_job(home, con):
    from aicorp.config import db_path
    from aicorp.db import connect
    staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)
    other = connect(db_path(home))
    later = T0 + timedelta(minutes=16)
    a = scheduler.run_due(con, home, later, tz=IST)
    b = scheduler.run_due(other, home, later, tz=IST)
    assert {r["job"] for r in a} and b == []


def test_monitor_switch_stops_everything_and_is_logged(home, con):
    staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)
    scheduler.set_monitor(con, False)
    assert not scheduler.monitor_enabled(con)
    assert scheduler.run_due(con, home, T0 + timedelta(days=2), tz=IST) == []
    assert con.execute("SELECT count(*) FROM job_run").fetchone()[0] == 0
    scheduler.set_monitor(con, True)
    assert scheduler.run_due(con, home, T0 + timedelta(days=2), tz=IST)
    assert [e["type"] for e in con.execute("SELECT type FROM event WHERE type = 'monitor.switched'")] == ["monitor.switched"] * 2


def test_a_broken_job_is_recorded_and_does_not_stop_the_others(home, con, monkeypatch):
    from aicorp import redact
    redact.install(home)
    notify.set_setting(con, "away_threshold_minutes", 120)
    staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)

    def boom(ctx):
        raise RuntimeError("job exploded password=Hunter2Hunter2")
    name, (spec, desc, fn) = "scan_active", scheduler.REGISTRY["scan_active"]
    monkeypatch.setitem(scheduler.REGISTRY, name, (spec, desc, boom))
    ran = scheduler.run_due(con, home, T0 + timedelta(minutes=16), tz=IST)
    by = {r["job"]: r for r in ran}
    assert by["scan_active"]["status"] == "error" and by["nag_check"]["status"] == "ok"
    note = con.execute("SELECT * FROM notification WHERE dedupe_key = 'error:job:scan_active'").fetchone()
    assert note["severity"] == "warn" and "Hunter2Hunter2" not in note["detail"] + note["title"]
    run = con.execute("SELECT detail FROM job_run WHERE job = 'scan_active'").fetchone()[0]
    assert "RuntimeError" in run
    monkeypatch.setitem(scheduler.REGISTRY, name, (spec, desc, fn))                 # fixed: the warning clears itself
    scheduler.run_now(con, home, "scan_active", T0 + timedelta(minutes=20), tz=IST)
    assert con.execute("SELECT resolved_at FROM notification WHERE dedupe_key = 'error:job:scan_active'").fetchone()[0]


def test_run_now_and_edit_a_schedule(home, con):
    staff(con, home, 1)
    result = scheduler.run_now(con, home, "daily_briefing", T0, tz=IST)
    assert result["status"] == "ok" and con.execute("SELECT count(*) FROM report WHERE kind = 'daily'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        scheduler.run_now(con, home, "nonexistent")
    scheduler.ensure_defaults(con, T0, IST)
    scheduler.set_job(con, "daily_briefing", spec="daily 06:45", now=T0, tz=IST)
    row = con.execute("SELECT spec, next_run FROM schedule WHERE job = 'daily_briefing'").fetchone()
    assert row["spec"] == "daily 06:45" and datetime.fromisoformat(row["next_run"]).astimezone(IST).strftime("%H:%M") == "06:45"
    with pytest.raises(ValueError):
        scheduler.set_job(con, "daily_briefing", spec="whenever")
    scheduler.set_job(con, "backup", enabled=False)
    assert scheduler.run_due(con, home, T0 + timedelta(days=3), tz=IST) and not any(r["job"] == "backup" for r in scheduler.history(con))


def test_catch_up_after_the_computer_was_off(home, con):
    staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)
    scheduler.run_due(con, home, T0 + timedelta(minutes=10), tz=IST)               # normal operation
    scheduler.run_due(con, home, T0 + timedelta(minutes=20), tz=IST)
    gone_until = T0 + timedelta(hours=2, minutes=20)                                # laptop was closed for two hours
    ran = scheduler.run_due(con, home, gone_until, tz=IST)
    jobs = {r["job"]: r for r in ran}
    assert jobs["scan_active"]["catch_up"] and jobs["nag_check"]["catch_up"]       # missed runs happen once, flagged as catch-up
    assert sum(1 for r in ran if r["job"] == "scan_active") == 1                    # not once per missed period
    away = con.execute("SELECT * FROM report WHERE kind = 'away'").fetchone()
    assert away and "While you were away (2 h" in away["title"]
    text = (home / away["path"]).read_text()
    assert "## Caught up" in text and "scan_active: ok" in text and "## Needs you now" in text
    note = con.execute("SELECT * FROM notification WHERE entity_type = 'report' AND entity_id = ?", (away["report_id"],)).fetchone()
    assert note["category"] == "report" and note["link"].endswith(away["report_id"])
    assert scheduler.run_due(con, home, gone_until + timedelta(seconds=10), tz=IST) == []
    assert con.execute("SELECT count(*) FROM report WHERE kind = 'away'").fetchone()[0] == 1       # summarised once


def test_a_short_gap_does_not_trigger_the_away_report(home, con):
    staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)
    scheduler.run_due(con, home, T0 + timedelta(minutes=10), tz=IST)
    assert con.execute("SELECT count(*) FROM report WHERE kind = 'away'").fetchone()[0] == 0


def test_away_summary_lists_missed_meetings_and_what_happened(home, con):
    from aicorp import projects, tasks
    ids = staff(con, home, 1)
    scheduler.run_due(con, home, T0, tz=IST)
    scheduler.run_due(con, home, T0 + timedelta(minutes=10), tz=IST)
    scheduler.run_due(con, home, T0 + timedelta(minutes=20), tz=IST)
    now = T0 + timedelta(hours=3)
    when = (T0 + timedelta(hours=1)).astimezone(IST).strftime("%Y-%m-%dT%H:%M")
    meeting = projects.create_meeting(con, home, {"title": "Weekly sync", "scheduled_at": when})
    con.execute("UPDATE meeting SET scheduled_at = ? WHERE meeting_id = ?", ((T0 + timedelta(hours=1)).isoformat(), meeting))
    t = tasks.create_task(con, title="Overnight job", owner=ids[0])
    events.append(con, type="task.done", summary="done", task_id=t, ts=(T0 + timedelta(hours=1)).isoformat())
    scheduler.run_due(con, home, now, tz=IST)
    text = (home / con.execute("SELECT path FROM report WHERE kind = 'away'").fetchone()[0]).read_text()
    assert "Weekly sync" in text and "mark it held or cancelled" in text and "Tasks finished: 1" in text


def test_the_advisor_never_has_authority_to_run_things(home, con):
    staff(con, home, 1)
    adv = advisor.advisor_actor(con)
    assert adv["actor_name"] == "Advisor" and adv["actor_type"] == "employee"
    for capability in ("execute", "assign", "approve"):
        with pytest.raises(PermissionError):
            authorize(con, adv, capability=capability)
    with pytest.raises(PermissionError):
        authorize(con, adv, founder_only=True)
    authorize(con, {"actor_type": "founder"}, founder_only=True)
    authorize(con, {"actor_type": "system"}, capability="assign")
    with pytest.raises(PermissionError):
        authorize(con, {"actor_type": "stranger"}, capability="assign")


def test_two_programs_creating_the_default_jobs_at_the_same_moment_do_not_collide(tmp_path):
    from aicorp.config import db_path
    from aicorp.db import connect, migrate
    from aicorp import advisor  # noqa: F401
    (tmp_path / "database").mkdir()
    a, b = connect(db_path(tmp_path)), connect(db_path(tmp_path))
    migrate(a)
    scheduler.ensure_defaults(a, T0, IST)
    scheduler.ensure_defaults(b, T0, IST)                       # the second program arrives a moment later (or at once): no error
    assert a.execute("SELECT count(*) FROM schedule").fetchone()[0] == len(scheduler.REGISTRY)

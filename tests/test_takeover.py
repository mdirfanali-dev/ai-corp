"""Impersonation and interrupt: both identities on every event, permissions of the identity acted as,
guardrails that no override can lift, interrupt with checkpoint/resume."""
import json
import threading
import time

import pytest

from aicorp import events, impersonation, org, takeover, tasks
from aicorp.engine import Orchestrator
from aicorp.org import FOUNDER, ValidationError
from aicorp.runners import FakeRunner, RunResult
from helpers import staff


@pytest.fixture
def world(home, con):
    ids = staff(con, home, 3)                              # three workers (execute only)
    return con, home, ids


def last_event(con, type):
    return dict(con.execute("SELECT * FROM event WHERE type = ? ORDER BY id DESC LIMIT 1", (type,)).fetchone())


def ceo(con):
    return org.get_employee(con, con.execute("SELECT emp_id FROM employee WHERE system_key = 'ceo'").fetchone()[0])


def ceo_actor(con):
    row = con.execute("SELECT emp_id, name FROM employee WHERE system_key = 'ceo'").fetchone()
    return {"actor_type": "employee", "actor_id": row["emp_id"], "actor_name": row["name"], "actor_designation": "Chief Executive"}


# ------------------------------------------------------------------ impersonation

def test_events_carry_both_identities_and_the_real_one_is_never_overwritten(world):
    con, home, ids = world
    imp = impersonation.begin(con, FOUNDER, ids[0], reason="cover for the launch")
    started = last_event(con, "impersonation.started")
    assert started["mode"] == "impersonate" and started["actor_name"] == "Founder" and started["acting_as_emp_id"] == ids[0]
    with impersonation.acting(con, imp):
        tasks.create_task(con, title="Draft the announcement", owner=ids[0], actor=FOUNDER)
    e = last_event(con, "task.created")
    assert (e["actor_type"], e["actor_name"], e["actor_designation"]) == ("founder", "Founder", "Chairman")
    assert e["acting_as_emp_id"] == ids[0] and e["acting_as_name"] == "W1" and e["mode"] == "impersonate"
    assert json.loads(e["payload"])["imp_id"] == imp and "override" not in json.loads(e["payload"])
    tasks.create_task(con, title="Outside the session", owner=ids[0])                        # no session: plain Founder event
    after = last_event(con, "task.created")
    assert after["mode"] == "normal" and after["acting_as_name"] is None
    assert events.verify_chain(con).ok


def test_search_finds_events_by_either_identity(world):
    con, home, ids = world
    from aicorp import search
    imp = impersonation.begin(con, FOUNDER, ids[1])
    with impersonation.acting(con, imp):
        tasks.create_task(con, title="Pricing page", owner=ids[1])
    assert any(r["type"] == "task.created" for r in search.search_events(con, actor="Founder"))
    assert any(r["type"] == "task.created" for r in search.search_events(con, actor="W2"))


def test_the_permissions_of_the_identity_acted_as_apply(world):
    con, home, ids = world
    boss = tasks.create_task(con, title="Waiting", owner=ids[1], risk_tier=3)
    con.execute("UPDATE task SET status = 'awaiting_approval', result_summary = 'x' WHERE task_id = ?", (boss,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, 'now')", (boss,))
    imp = impersonation.begin(con, FOUNDER, ids[0])
    with impersonation.acting(con, imp):
        with pytest.raises(PermissionError, match="no 'assign' permission"):
            tasks.create_task(con, title="Work for someone else", owner=ids[1])
        tasks.create_task(con, title="Own work", owner=ids[0])                              # allowed: it is W1's own
        with pytest.raises(PermissionError):
            tasks.decide_approval(con, boss, "approved")                                    # approvals are never W1's
        with pytest.raises(PermissionError, match="stop impersonating"):
            org.create_role(con, {"name": "Hacker"}, FOUNDER)
    tasks.decide_approval(con, boss, "approved")                                            # the Founder, outside, can


def test_override_needs_explicit_confirmation_and_is_flagged_on_every_event(world):
    con, home, ids = world
    with pytest.raises(ValidationError, match="confirmed"):
        impersonation.begin(con, FOUNDER, ids[0], override=True)
    imp = impersonation.begin(con, FOUNDER, ids[0], override=True, confirm=True)
    with impersonation.acting(con, imp):
        tasks.create_task(con, title="Assign anything", owner=ids[2])                       # override lifts the role's limits
    e = last_event(con, "task.created")
    assert json.loads(e["payload"])["override"] is True and e["mode"] == "impersonate"
    assert "FOUNDER OVERRIDE" in last_event(con, "impersonation.started")["summary"]


def test_ceo_can_only_act_as_members_of_teams_it_heads_and_the_founder_is_told(world):
    con, home, ids = world
    boss = ceo_actor(con)
    with pytest.raises(PermissionError, match="teams it heads"):
        impersonation.begin(con, boss, ids[0])
    org.create_team(con, {"name": "Growth", "head_emp_id": boss["actor_id"], "member_ids": [ids[0]]})
    imp = impersonation.begin(con, boss, ids[0], reason="Aria is out today")
    note = con.execute("SELECT * FROM notification WHERE dedupe_key = ? AND resolved_at IS NULL", (f"impersonation:{imp}",)).fetchone()
    assert note and note["severity"] == "warn" and "acting as W1" in note["title"]
    with impersonation.acting(con, imp):
        tasks.create_task(con, title="Cover task", owner=ids[0], actor=boss)
    e = last_event(con, "task.created")
    assert e["actor_name"] == boss["actor_name"] and e["acting_as_name"] == "W1" and e["mode"] == "impersonate"
    with pytest.raises(PermissionError, match="override"):
        impersonation.begin(con, boss, ids[0], override=True, confirm=True)
    impersonation.end(con, imp, boss)
    assert con.execute("SELECT resolved_at FROM notification WHERE dedupe_key = ?", (f"impersonation:{imp}",)).fetchone()[0]
    with pytest.raises(PermissionError):
        impersonation.begin(con, ceo_actor(con) | {"actor_id": ids[0], "actor_name": "W1"}, ids[1])      # an ordinary employee


def test_sessions_expire_and_cannot_target_archived_or_missing_people(world):
    con, home, ids = world
    from datetime import datetime, timedelta, timezone
    imp = impersonation.begin(con, FOUNDER, ids[0])
    assert impersonation.get(con, imp) is not None
    assert impersonation.get(con, imp, datetime.now(timezone.utc) + timedelta(hours=3)) is None
    assert last_event(con, "impersonation.ended")["summary"].endswith("(expired)")
    with pytest.raises(ValidationError):
        impersonation.begin(con, FOUNDER, "E-nope")
    org.archive_employee(con, home, ids[2])
    with pytest.raises(ValidationError, match="archived"):
        impersonation.begin(con, FOUNDER, ids[2])


def test_a_new_session_replaces_the_old_one(world):
    con, home, ids = world
    a = impersonation.begin(con, FOUNDER, ids[0])
    b = impersonation.begin(con, FOUNDER, ids[1])
    assert impersonation.get(con, a) is None and impersonation.get(con, b)


# ------------------------------------------------------------------ interrupt / take over

class Slow(FakeRunner):
    """A worker that keeps going until told to stop, then reports a checkpoint."""

    def __init__(self):
        super().__init__()
        self.began = threading.Event()

    def run(self, ctx):
        self.began.set()
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if ctx.cancel.is_set():
                return RunResult(outcome="stopped", summary="stopped", checkpoint="step 3 of 5 done")
            time.sleep(0.02)
        return RunResult(outcome="done", summary="finished")


def engine_with(con, home, runner):
    return Orchestrator(home, runner, threaded=True, cap=2, lease_seconds=60, max_runtime=120, ram_probe=lambda: 0.1)


def test_interrupt_stops_the_agent_within_seconds_and_resume_continues_from_the_checkpoint(world):
    con, home, ids = world
    runner = Slow()
    engine = engine_with(con, home, runner)
    engine.start()
    try:
        t = tasks.create_task(con, title="Long research", description="Read and summarise", owner=ids[0])
        engine.tick()
        assert runner.began.wait(5) and tasks.get_task(con, t)["status"] == "running"
        started = time.monotonic()
        to = takeover.start(con, home, t)
        assert tasks.get_task(con, t)["interrupt_requested"] == 1
        while time.monotonic() - started < 10:
            engine.tick()
            if takeover.get(con, to)["status"] == "active":
                break
            time.sleep(0.02)
        stopped_after = time.monotonic() - started
        print(f"\nagent stopped and task held {stopped_after:.2f} s after the interrupt")
        assert stopped_after < 5
        task = tasks.get_task(con, t)
        assert task["status"] == "blocked" and task["lease_owner"] is None and task["interrupt_requested"] == 0
        assert task["checkpoint"] == "step 3 of 5 done" and task["block"]["detail"] == takeover.HELD
        row = takeover.get(con, to)
        assert row["snapshot"]["checkpoint"] == "step 3 of 5 done" and row["snapshot"]["title"] == "Long research"
        assert con.execute("SELECT status FROM employee WHERE emp_id = ?", (ids[0],)).fetchone()[0] != "working"
        e = last_event(con, "takeover.started")
        assert e["mode"] == "interrupt" and e["actor_name"] == "Founder" and e["acting_as_name"] == "W1"
        engine.tick()
        assert tasks.get_task(con, t)["status"] == "blocked"                            # the engine leaves a held task alone
        takeover.add_note(con, to, "Use the 2025 numbers, not 2024.")
        takeover.write_file(con, home, to, "notes/hints.md", "2025 figures: 12 percent")
        takeover.end(con, to, "resume")
        task = tasks.get_task(con, t)
        assert task["status"] == "assigned" and "Use the 2025 numbers" in task["checkpoint"] and "notes/hints.md" in task["checkpoint"]
        assert "step 3 of 5 done" in task["checkpoint"]
        assert takeover.get(con, to)["end_action"] == "resume" and takeover.open_for_task(con, t) is None
    finally:
        engine.shutdown()
    assert events.verify_chain(con).ok


def test_interrupt_on_a_waiting_task_holds_it_immediately_and_complete_finishes_it(world):
    con, home, ids = world
    t = tasks.create_task(con, title="Write the FAQ", owner=ids[0])
    to = takeover.start(con, home, t)
    assert tasks.get_task(con, t)["status"] == "blocked" and takeover.get(con, to)["status"] == "active"
    with pytest.raises(ValidationError, match="already taken over"):
        takeover.start(con, home, t)
    with pytest.raises(ValidationError, match="result"):
        takeover.end(con, to, "complete", result="  ")
    takeover.end(con, to, "complete", result="FAQ written by the Founder: see faq.md")
    task = tasks.get_task(con, t)
    assert task["status"] == "done" and "FAQ written" in task["result_summary"]
    assert last_event(con, "task.completed_by_founder")["actor_name"] == "Founder"


def test_cancel_ends_the_task_and_blocked_dependents_are_failed(world):
    con, home, ids = world
    a = tasks.create_task(con, title="Base", owner=ids[0])
    b = tasks.create_task(con, title="Follow-up", owner=ids[1], depends_on=[a])
    to = takeover.start(con, home, a)
    takeover.end(con, to, "cancel")
    assert tasks.get_task(con, a)["status"] == "rejected"
    assert tasks.get_task(con, b)["status"] in ("blocked", "failed", "rejected")


def test_only_the_founder_can_interrupt_and_finished_tasks_cannot_be_taken_over(world):
    con, home, ids = world
    t = tasks.create_task(con, title="Plain", owner=ids[0])
    with pytest.raises(PermissionError):
        takeover.start(con, home, t, ceo_actor(con))
    imp = impersonation.begin(con, FOUNDER, ids[1])
    with impersonation.acting(con, imp):
        with pytest.raises(PermissionError, match="stop impersonating"):
            takeover.start(con, home, t)
    con.execute("UPDATE task SET status = 'done' WHERE task_id = ?", (t,))
    with pytest.raises(ValidationError, match="cannot be taken over"):
        takeover.start(con, home, t)


def test_agent_that_finishes_first_ends_the_interrupt_quietly(world):
    con, home, ids = world
    runner = FakeRunner(duration=(0.0, 0.0))
    engine = engine_with(con, home, runner)
    engine.start()
    try:
        t = tasks.create_task(con, title="Quick job [quick]", owner=ids[0])
        engine.tick()
        con.execute("UPDATE task SET status = 'running', lease_owner = 'x' WHERE task_id = ?", (t,))    # pretend: still running
        to = takeover.start(con, home, t)
        con.execute("UPDATE task SET status = 'done', lease_owner = NULL WHERE task_id = ?", (t,))
        takeover.settle(con, home, t)
        assert takeover.get(con, to)["end_action"] == "abandoned"
        assert tasks.get_task(con, t)["interrupt_requested"] == 0
    finally:
        engine.shutdown()


# ------------------------------------------------------------------ guardrails hold under impersonation and override

@pytest.mark.parametrize("override", [None, True])        # None = the Founder without a session
@pytest.mark.parametrize("path", [".env", "secrets.env", "../../config/secrets.env", "/etc/passwd", "sub/.ssh/id_rsa", "x/../../../database/company.sqlite", ".git/config"])
def test_protected_paths_still_block_under_impersonation_and_override(world, path, override):
    con, home, ids = world
    t = tasks.create_task(con, title="Hands-on", owner=ids[0])
    to = takeover.start(con, home, t)
    session = impersonation.begin(con, FOUNDER, ids[0], override=True, confirm=True) if override else None
    with (impersonation.acting(con, session) if session else __import__("contextlib").nullcontext()):
        with pytest.raises(PermissionError, match="BLOCKED by AICorp policy"):
            takeover.write_file(con, home, to, path, "hacked")
    assert not (home / "config" / "secrets.env").exists() or "hacked" not in (home / "config" / "secrets.env").read_text()
    refused = last_event(con, "takeover.file_refused")
    assert refused["mode"] == "interrupt" and refused["actor_name"] == "Founder"


def test_symlink_out_of_the_workspace_is_refused(world, tmp_path):
    con, home, ids = world
    t = tasks.create_task(con, title="Hands-on", owner=ids[0])
    to = takeover.start(con, home, t)
    ws = takeover.workspace_of(con, home, ids[0])
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "out").symlink_to(tmp_path)
    with pytest.raises(PermissionError):
        takeover.write_file(con, home, to, "out/escape.txt", "x")
    assert not (tmp_path / "escape.txt").exists()


def test_secrets_in_notes_and_files_are_scrubbed(world):
    con, home, ids = world
    (home / "config" / "secrets.env").write_text("GMAIL_APP_PASSWORD=abcd-efgh-ijkl-mnop\n")
    from aicorp import redact
    redact.install(home)
    t = tasks.create_task(con, title="Hands-on", owner=ids[0])
    to = takeover.start(con, home, t)
    takeover.add_note(con, to, "password is abcd-efgh-ijkl-mnop")
    rel = takeover.write_file(con, home, to, "n.txt", "token abcd-efgh-ijkl-mnop")
    assert "abcd-efgh" not in json.dumps(takeover.get(con, to)["notes"])
    assert "abcd-efgh" not in (takeover.workspace_of(con, home, ids[0]) / rel).read_text()

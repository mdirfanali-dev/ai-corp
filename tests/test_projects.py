from datetime import datetime, timedelta, timezone

import pytest

from aicorp import org, projects, tasks
from aicorp.engine import Orchestrator
from aicorp.runners import FakeRunner
from helpers import run_until_idle, staff


@pytest.fixture
def world(home, con):
    return con, home, staff(con, home, 3)


def local(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).astimezone().strftime("%Y-%m-%dT%H:%M")


def test_project_crud_and_validation(world):
    con, home, (a, *_r) = world
    pid = projects.create_project(con, {"name": "  Micro SaaS  ", "goal": "Launch a form backend", "owner_emp_id": a, "next_review_at": local(48)})
    p = projects.get_project(con, pid)
    assert p["name"] == "Micro SaaS" and p["owner_name"] == "W1" and p["status"] == "active" and p["next_review_at"]
    projects.update_project(con, pid, {"name": "Micro SaaS v2", "status": "paused", "goal": "g"})
    assert projects.get_project(con, pid)["status"] == "paused"
    for bad in ({"name": ""}, {"name": "x", "owner_emp_id": "E-none"}, {"name": "x", "status": "weird"}, {"name": "x", "next_review_at": "soon"}):
        with pytest.raises(org.ValidationError):
            projects.create_project(con, bad)
    with pytest.raises(org.ValidationError):
        projects.update_project(con, "P-missing", {"name": "x"})
    projects.delete_project(con, pid)
    assert projects.get_project(con, pid) is None and projects.list_projects(con) == []
    assert [e["type"] for e in con.execute("SELECT type FROM event WHERE type LIKE 'project.%'")] == ["project.created", "project.updated", "project.deleted"]


def test_project_health_labels(world):
    con, home, (a, b, _c) = world
    now = datetime.now(timezone.utc)
    pid = projects.create_project(con, {"name": "P"})
    assert projects.project_health(con, pid, now)["label"] == "ok"                    # no work yet
    t1 = tasks.create_task(con, title="One", owner=a, project_id=pid)
    tasks.create_task(con, title="Two", owner=b, project_id=pid)
    assert projects.project_health(con, pid, now)["open"] == 2 and projects.project_health(con, pid, now)["label"] == "ok"
    con.execute("UPDATE task SET status = 'blocked' WHERE task_id = ?", (t1,))
    assert projects.project_health(con, pid, now)["label"] == "attention"
    con.execute("UPDATE task SET status = 'assigned', due_at = ? WHERE task_id = ?", ((now - timedelta(days=1)).isoformat(), t1))
    h = projects.project_health(con, pid, now)
    assert h["label"] == "attention" and h["overdue"] == 1
    assert projects.project_health(con, pid, now + timedelta(days=6))["label"] == "stalled" and projects.project_health(con, pid, now + timedelta(days=6))["idle_days"] >= 5
    con.execute("UPDATE task SET status = 'done'")
    assert projects.project_health(con, pid, now + timedelta(days=30))["label"] == "ok" and projects.project_health(con, pid, now)["done"] == 2


def test_meeting_lifecycle_attendees_and_minutes(world):
    con, home, (a, b, _c) = world
    pid = projects.create_project(con, {"name": "P"})
    mid = projects.create_meeting(con, home, {"title": "Kickoff", "project_id": pid, "scheduled_at": local(5), "agenda": "1. Scope", "attendees": ["founder", a, b]})
    m = projects.get_meeting(con, mid)
    assert m["project_name"] == "P" and {x["name"] for x in m["attendees"]} == {"Founder", "W1", "W2"} and m["status"] == "scheduled"
    for bad in ({"title": ""}, {"title": "x", "project_id": "P-none"}, {"title": "x", "attendees": ["E-nobody"]}, {"title": "x", "scheduled_at": "tomorrowish"}):
        with pytest.raises(org.ValidationError):
            projects.create_meeting(con, home, bad)
    projects.update_meeting(con, home, mid, {"title": "Kickoff v2", "project_id": pid, "attendees": [a], "agenda": "changed"})
    assert [x["name"] for x in projects.get_meeting(con, mid)["attendees"]] == ["W1"]
    path = projects.save_minutes(con, home, mid, "# Minutes\nWe decided to ship. password=Hunter2Hunter2")
    assert path == f"memory/meetings/{mid}.md" and (home / path).exists()
    assert projects.get_meeting(con, mid)["minutes_path"] == path
    projects.set_meeting_status(con, mid, "held")
    assert projects.get_meeting(con, mid)["status"] == "held"
    with pytest.raises(org.ValidationError):
        projects.set_meeting_status(con, mid, "exploded")
    assert [m["meeting_id"] for m in projects.list_meetings(con, pid)] == [mid] and projects.list_meetings(con, upcoming=True) == []


def test_minutes_are_scrubbed_of_known_secrets(world):
    from aicorp import redact
    con, home, _ = world
    (home / "config" / "secrets.env").write_text("GMAIL_APP_PASSWORD=zz yy xx ww vv\n")
    redact.install(home)
    mid = projects.create_meeting(con, home, {"title": "M"})
    path = projects.save_minutes(con, home, mid, "the password is zz yy xx ww vv ok")
    assert "zz yy" not in (home / path).read_text() and "[REDACTED]" in (home / path).read_text()


def test_action_items_become_tasks_in_the_meetings_project(world):
    con, home, (a, *_r) = world
    pid = projects.create_project(con, {"name": "P"})
    mid = projects.create_meeting(con, home, {"title": "Sync", "project_id": pid})
    with_owner = projects.add_action_item(con, mid, "Collect competitor prices", owner=a, due_at=local(24))
    loose = projects.add_action_item(con, mid, "Think about branding")
    items = {i["item_id"]: i for i in projects.get_meeting(con, mid)["actions"]}
    task = tasks.get_task(con, items[with_owner]["task_id"])
    assert task["project_id"] == pid and task["owner_name"] == "W1" and task["due_at"] and items[loose]["task_id"] is None
    with pytest.raises(org.ValidationError):
        projects.add_action_item(con, mid, "   ")
    with pytest.raises(org.ValidationError):
        projects.add_action_item(con, "M-none", "x")


def test_decisions_are_recorded_to_files_and_closed(world):
    con, home, _ = world
    mid = projects.create_meeting(con, home, {"title": "Sync"})
    did = projects.add_decision(con, home, {"title": "Use shared hosting", "text": "Cheaper to start.", "follow_up_by": local(72), "meeting_id": mid})
    d = projects.list_decisions(con, "open")[0]
    assert d["decision_id"] == did and (home / d["md_path"]).read_text().startswith("# Use shared hosting")
    with pytest.raises(org.ValidationError):
        projects.add_decision(con, home, {"title": "x", "meeting_id": "M-none"})
    projects.close_decision(con, did)
    assert projects.list_decisions(con, "open") == [] and projects.list_decisions(con, "followed_up")[0]["closed_at"]
    with pytest.raises(org.ValidationError):
        projects.close_decision(con, did)


def test_bot_meeting_is_an_orchestrated_round_not_free_chat(world):
    con, home, (a, b, c) = world
    org.archive_employee(con, home, c)
    mid = projects.create_meeting(con, home, {"title": "Pricing review", "agenda": "1. Prices", "attendees": ["founder", a, b, c]})
    made = projects.start_bot_meeting(con, mid)
    assert len(made["inputs"]) == 2                                                    # the archived employee and the Founder are skipped
    owners = {tasks.get_task(con, t)["owner_name"] for t in made["inputs"]}
    assert owners == {"W1", "W2"}
    final = tasks.get_task(con, made["consolidation"])
    assert final["owner_name"] == "CEO" and {d["task_id"] for d in final["depends_on"]} == set(made["inputs"])
    assert all(tasks.get_task(con, t)["risk_tier"] == 0 for t in made["inputs"]) and "1. Prices" in tasks.get_task(con, made["inputs"][0])["description"]
    with pytest.raises(org.ValidationError, match="already"):
        projects.start_bot_meeting(con, mid)
    with pytest.raises(org.ValidationError, match="finished"):
        projects.use_round_result_as_minutes(con, home, mid)
    # run it with the simulated runner: the inputs run first, then the CEO's minutes task
    engine = Orchestrator(home, FakeRunner(duration=(0.01, 0.02), seed=1), threaded=False)
    engine.start()
    try:
        run_until_idle(engine)
    finally:
        engine.shutdown()
    assert tasks.get_task(con, made["consolidation"])["status"] == "done"
    path = projects.use_round_result_as_minutes(con, home, mid)
    assert "simulated result" in (home / path).read_text()


def test_bot_meeting_needs_employee_attendees(world):
    con, home, _ = world
    mid = projects.create_meeting(con, home, {"title": "Solo", "attendees": ["founder"]})
    with pytest.raises(org.ValidationError, match="employee attendee"):
        projects.start_bot_meeting(con, mid)

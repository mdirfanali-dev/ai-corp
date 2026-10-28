import pytest

from aicorp import org, tasks
from aicorp.org import FOUNDER
from helpers import staff


@pytest.fixture
def world(home, con):
    ids = staff(con, home, 3)
    return con, home, ids


def advisor(con):
    return con.execute("SELECT emp_id FROM employee WHERE name = 'Advisor'").fetchone()[0]


def test_create_assigned_and_unassigned(world):
    con, _, (a, *_rest) = world
    t1 = tasks.create_task(con, title="Write report", owner=a)
    t2 = tasks.create_task(con, title="Unowned")
    assert tasks.get_task(con, t1)["status"] == "assigned"
    assert tasks.get_task(con, t2)["status"] == "created"
    tasks.assign_task(con, t2, a)
    assert tasks.get_task(con, t2)["status"] == "assigned"


def test_input_validation(world):
    con, _, (a, *_rest) = world
    for kwargs in ({"title": ""}, {"title": "x", "priority": 9}, {"title": "x", "risk_tier": 7},
                   {"title": "x", "owner": "E-none"}, {"title": "x", "parent_id": "T-none"},
                   {"title": "x", "depends_on": ["T-none"]}, {"title": "x", "due_at": "soon"}):
        with pytest.raises(org.ValidationError):
            tasks.create_task(con, **kwargs)


def test_delegation_depth_is_limited_to_three(world):
    con, _, (a, *_rest) = world
    parent = None
    for level in range(4):
        parent = tasks.create_task(con, title=f"level {level}", owner=a, parent_id=parent)
        assert tasks.get_task(con, parent)["depth"] == level
    with pytest.raises(org.ValidationError, match="depth"):
        tasks.create_task(con, title="too deep", owner=a, parent_id=parent)


def test_capability_rules_for_advisor_and_bench(world, home):
    con, _, (a, *_rest) = world
    adv = advisor(con)
    tasks.create_task(con, title="read-only briefing", owner=adv, risk_tier=0)
    with pytest.raises(org.ValidationError, match="execute"):
        tasks.create_task(con, title="do work", owner=adv, risk_tier=1)
    bench = org.create_employee(con, home, {"name": "Bench"})
    tasks.create_task(con, title="look something up", owner=bench, risk_tier=0)
    with pytest.raises(org.ValidationError, match="execute"):
        tasks.create_task(con, title="write code", owner=bench, risk_tier=2)
    ceo = con.execute("SELECT emp_id FROM employee WHERE name = 'CEO'").fetchone()[0]
    tasks.create_task(con, title="draft proposal", owner=ceo, risk_tier=1)


def test_archived_employee_cannot_take_tasks(world, home):
    con, _, (a, *_rest) = world
    org.archive_employee(con, home, a)
    with pytest.raises(org.ValidationError, match="archived"):
        tasks.create_task(con, title="x", owner=a)


def test_cross_team_work_must_be_a_logged_delegation(world, home):
    con, _, (a, b, c) = world
    org.create_team(con, {"name": "Marketing", "member_ids": [a]})
    org.create_team(con, {"name": "Development", "member_ids": [b, c]})
    mine = tasks.create_task(con, title="launch plan", owner=a)
    with pytest.raises(org.ValidationError, match="delegation"):
        tasks.create_task(con, title="estimate", owner=b, creator_emp_id=a)
    with pytest.raises(org.ValidationError, match="sub-task"):
        tasks.create_task(con, title="estimate", owner=b, creator_emp_id=a, delegation=True)
    sub = tasks.create_task(con, title="estimate", owner=b, creator_emp_id=a, delegation=True, parent_id=mine)
    assert tasks.get_task(con, sub)["kind"] == "delegation"
    kinds = [e["type"] for e in tasks.timeline(con, sub)]
    assert "task.delegated" in kinds
    # same team needs no delegation
    tasks.create_task(con, title="pair work", owner=c, creator_emp_id=b)
    # the CEO coordinates every team without delegation requests
    ceo = con.execute("SELECT emp_id FROM employee WHERE name = 'CEO'").fetchone()[0]
    tasks.create_task(con, title="ceo assigns", owner=b, creator_emp_id=ceo)
    # a manager may assign to their report
    worker_role = con.execute("SELECT role_id FROM role WHERE name = 'Worker'").fetchone()[0]
    org.update_employee(con, home, a, {"name": "W1", "reports_to": c, "role_ids": [worker_role]})
    tasks.create_task(con, title="manager to report", owner=a, creator_emp_id=c)


def test_dependencies_reject_cycles(world):
    con, _, (a, *_rest) = world
    t1 = tasks.create_task(con, title="one", owner=a)
    t2 = tasks.create_task(con, title="two", owner=a, depends_on=[t1])
    t3 = tasks.create_task(con, title="three", owner=a, depends_on=[t2])
    for src, dst in ((t1, t3), (t1, t2), (t1, t1)):
        with pytest.raises(org.ValidationError, match="cycle"):
            tasks.add_dependency(con, src, dst)
    with pytest.raises(org.ValidationError, match="does not exist"):
        tasks.add_dependency(con, t1, "T-none")


def test_state_machine_rejects_illegal_moves(world):
    con, _, (a, *_rest) = world
    t = tasks.create_task(con, title="x", owner=a)
    for target in ("done", "review", "failed", "awaiting_approval"):
        with pytest.raises(tasks.InvalidTransition):
            tasks.transition(con, t, target, actor=FOUNDER, summary="nope")
    tasks.cancel_task(con, t)
    assert tasks.get_task(con, t)["status"] == "rejected"
    for target in tasks.STATUSES:
        with pytest.raises(tasks.InvalidTransition):
            tasks.transition(con, t, target, actor=FOUNDER, summary="terminal")
    with pytest.raises(tasks.InvalidTransition):
        tasks.cancel_task(con, t)


def test_transition_table_is_consistent():
    assert set(tasks.TRANSITIONS) == set(tasks.STATUSES)
    for src, dsts in tasks.TRANSITIONS.items():
        assert dsts <= set(tasks.STATUSES) and src not in dsts
    assert not tasks.TRANSITIONS["done"] and not tasks.TRANSITIONS["rejected"]


def test_only_the_founder_decides_approvals(world):
    con, _, (a, *_rest) = world
    t = tasks.create_task(con, title="publish", owner=a, risk_tier=3)
    con.execute("UPDATE task SET status = 'awaiting_approval' WHERE task_id = ?", (t,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, 'now')", (t,))
    employee = {"actor_type": "employee", "actor_name": "W1", "actor_id": a, "actor_designation": None}
    with pytest.raises(PermissionError):
        tasks.decide_approval(con, t, "approved", actor=employee)
    tasks.decide_approval(con, t, "approved", "looks fine")
    assert tasks.get_task(con, t)["status"] == "done"


def test_failed_dependency_blocks_dependents_and_auto_unblocks(world):
    con, _, (a, b, _c) = world
    first = tasks.create_task(con, title="first", owner=a)
    second = tasks.create_task(con, title="second", owner=b, depends_on=[first])
    con.execute("UPDATE task SET status = 'failed' WHERE task_id = ?", (first,))
    assert tasks.fail_dependents(con, first, "failed") == [second]
    blocked = tasks.get_task(con, second)
    assert blocked["status"] == "blocked" and blocked["block"]["reason"] == "NEEDS_INPUT"
    assert tasks.auto_unblock(con) == []          # dependency still failed
    con.execute("UPDATE task SET status = 'done' WHERE task_id = ?", (first,))
    assert tasks.auto_unblock(con) == [second]
    assert tasks.get_task(con, second)["status"] == "assigned"


def test_resolve_block_and_retry_failed(world):
    con, _, (a, *_rest) = world
    t = tasks.create_task(con, title="x", owner=a)
    tasks.transition(con, t, "blocked", actor=FOUNDER, summary="b")
    tasks.open_block(con, t, "CAPTCHA", "solve it")
    tasks.resolve_block(con, t, note="solved")
    assert tasks.get_task(con, t)["status"] == "assigned" and tasks.get_task(con, t)["block"] is None
    tasks.transition(con, t, "blocked", actor=FOUNDER, summary="b")
    tasks.transition(con, t, "failed", actor=FOUNDER, summary="f")
    con.execute("UPDATE task SET attempts = 3 WHERE task_id = ?", (t,))
    tasks.retry_failed(con, t)
    task = tasks.get_task(con, t)
    assert task["status"] == "assigned" and task["attempts"] == 0
    with pytest.raises(tasks.InvalidTransition):
        tasks.resolve_block(con, t)

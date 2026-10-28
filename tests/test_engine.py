import json
import time

import pytest

from aicorp import events, killswitch, tasks
from aicorp.engine import EngineAlreadyRunning, Orchestrator
from aicorp.org import FOUNDER
from aicorp.runners import FakeRunner
from helpers import FakeClock, assert_transitions_are_logged, run_until_idle, staff, statuses


@pytest.fixture
def make_engine(home, con):
    engines = []

    def make(runner=None, **kw):
        engine = Orchestrator(home, runner or FakeRunner(duration=(0.02, 0.05), seed=1), **kw)
        engine.start()
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        if engine.con is not None:
            engine.shutdown()


def event_types(con, task_id):
    return [e["type"] for e in reversed(tasks.timeline(con, task_id))]


def test_ten_tasks_with_dependencies_respect_cap_and_order(home, con, make_engine):
    ids = staff(con, home, 10)
    # a diamond-ish DAG: t0 -> (t1, t2, t3) -> t4 ; t5..t9 independent
    t = [tasks.create_task(con, title=f"task {i}", owner=ids[i]) for i in range(5)]
    for dep in (1, 2, 3):
        tasks.add_dependency(con, t[dep], t[0])
        tasks.add_dependency(con, t[4], t[dep])
    t += [tasks.create_task(con, title=f"task {i}", owner=ids[i]) for i in range(5, 10)]
    runner = FakeRunner(duration=(0.08, 0.14), seed=3)
    engine = make_engine(runner, cap=3)
    run_until_idle(engine)
    assert set(statuses(con).values()) == {"done"} and len(t) == 10
    assert runner.max_concurrent <= 3, "parallel cap exceeded"
    assert runner.max_concurrent == 3, "cap never reached although work was available"
    start = dict(runner.started)
    end = dict(runner.finished)
    for dep_src, dep_dst in ((1, 0), (2, 0), (3, 0), (4, 1), (4, 2), (4, 3)):
        assert start[t[dep_src]] >= end[t[dep_dst]], f"task {dep_src} started before task {dep_dst} finished"
    assert_transitions_are_logged(con)
    assert events.verify_chain(con).ok


def test_one_employee_never_runs_two_tasks_at_once(home, con, make_engine):
    (a,) = staff(con, home, 1)
    for i in range(3):
        tasks.create_task(con, title=f"solo {i}", owner=a)
    runner = FakeRunner(duration=(0.03, 0.05), seed=1)
    engine = make_engine(runner, cap=4)
    run_until_idle(engine)
    assert runner.max_concurrent == 1 and set(statuses(con).values()) == {"done"}


def test_priority_then_due_date_decides_order(home, con, make_engine):
    ids = staff(con, home, 3)
    low = tasks.create_task(con, title="low", owner=ids[0], priority=5)
    high = tasks.create_task(con, title="high", owner=ids[1], priority=1)
    mid = tasks.create_task(con, title="mid", owner=ids[2], priority=3)
    runner = FakeRunner(duration=(0.01, 0.02), seed=1)
    engine = make_engine(runner, cap=1, threaded=False)
    run_until_idle(engine)
    assert [tid for tid, _ in runner.started] == [high, mid, low]


def test_failure_retries_with_backoff_then_succeeds(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="flaky [fail-once]", owner=a)
    clock = FakeClock()
    engine = make_engine(cap=2, threaded=False, clock=clock, base_backoff=10)
    engine.tick()
    task = tasks.get_task(con, t)
    assert task["status"] == "assigned" and task["attempts"] == 1 and task["not_before"]
    engine.tick()                                   # backoff not over yet
    assert tasks.get_task(con, t)["attempts"] == 1
    clock.advance(11)
    engine.tick()
    task = tasks.get_task(con, t)
    assert task["status"] == "done" and task["attempts"] == 2
    types = event_types(con, t)
    assert "task.retry_scheduled" in types and types[-1] == "task.done"
    assert_transitions_are_logged(con)


def test_gives_up_after_two_retries_and_blocks_dependents(home, con, make_engine):
    a, b = staff(con, home, 2)
    bad = tasks.create_task(con, title="always broken [fail]", owner=a)
    after = tasks.create_task(con, title="waits for bad", owner=b, depends_on=[bad])
    clock = FakeClock()
    engine = make_engine(cap=2, threaded=False, clock=clock, base_backoff=1)
    for _ in range(6):
        engine.tick()
        clock.advance(5)
    failed = tasks.get_task(con, bad)
    assert failed["status"] == "failed" and failed["attempts"] == tasks.MAX_RETRIES + 1
    blocked = tasks.get_task(con, after)
    assert blocked["status"] == "blocked" and blocked["block"]["detail"].startswith("dependency:")
    tasks.retry_failed(con, bad)
    assert tasks.get_task(con, bad)["attempts"] == 0
    assert_transitions_are_logged(con)


def test_blocked_review_and_approval_outcomes(home, con, make_engine):
    ids = staff(con, home, 3)
    blocked = tasks.create_task(con, title="needs human [block:CAPTCHA]", owner=ids[0])
    review = tasks.create_task(con, title="check me [review]", owner=ids[1])
    approval = tasks.create_task(con, title="ship it [approval]", owner=ids[2], risk_tier=3)
    engine = make_engine(threaded=False)
    run_until_idle(engine)
    b = tasks.get_task(con, blocked)
    assert b["status"] == "blocked" and b["block"]["reason"] == "CAPTCHA"
    assert con.execute("SELECT status FROM employee WHERE emp_id = ?", (ids[0],)).fetchone()[0] == "blocked"
    assert tasks.get_task(con, review)["status"] == "review"
    ap = tasks.get_task(con, approval)
    assert ap["status"] == "awaiting_approval" and ap["approval"]["tier"] == 3
    tasks.decide_approval(con, approval, "approved")
    assert tasks.get_task(con, approval)["status"] == "done"
    tasks.resolve_block(con, blocked)
    assert con.execute("SELECT status FROM employee WHERE emp_id = ?", (ids[0],)).fetchone()[0] == "idle"
    assert_transitions_are_logged(con)


def test_runner_exception_is_contained(home, con, make_engine):
    a, b = staff(con, home, 2)
    crash = tasks.create_task(con, title="explodes [crash]", owner=a)
    fine = tasks.create_task(con, title="unaffected", owner=b)
    clock = FakeClock()
    engine = make_engine(threaded=False, clock=clock, base_backoff=1)
    for _ in range(8):
        engine.tick()
        clock.advance(5)
    assert tasks.get_task(con, fine)["status"] == "done"
    assert tasks.get_task(con, crash)["status"] == "failed"
    failures = [json.loads(e["payload"]) for e in tasks.timeline(con, crash) if e["type"] == "task.failed"]
    assert "simulated crash" in failures[0]["error"]


def test_kill_switch_stops_workers_and_blocks_new_starts(home, con, make_engine):
    ids = staff(con, home, 4)
    created = [tasks.create_task(con, title=f"long {i}", owner=ids[i]) for i in range(4)]
    runner = FakeRunner(duration=(1.5, 1.5), seed=1)
    engine = make_engine(runner, cap=2)
    engine.tick()
    time.sleep(0.2)
    assert engine._active_count() == 2
    killswitch.engage(con, home, FOUNDER, "test")
    for _ in range(10):
        stats = engine.tick()
        time.sleep(0.05)
    engine.wait_for_workers(5)
    assert stats["killed"] and engine._active_count() == 0
    states = statuses(con)
    assert list(states.values()).count("assigned") == 4 and "running" not in states.values()
    assert all(tasks.get_task(con, t)["attempts"] == 0 for t in created), "a stop must not use up retries"
    assert any(e["type"] == "task.stopped" for t in created for e in tasks.timeline(con, t))
    killswitch.release(con, home)
    run_until_idle(engine, timeout=30)
    assert set(statuses(con).values()) == {"done"}
    assert_transitions_are_logged(con)


def test_stop_file_created_by_hand_also_works(home, con, make_engine):
    (a,) = staff(con, home, 1)
    tasks.create_task(con, title="never starts", owner=a)
    engine = make_engine(threaded=False)
    (home / "STOP").write_text("")
    assert engine.tick()["killed"] and statuses(con) == {t: "assigned" for t in statuses(con)}
    (home / "STOP").unlink()
    run_until_idle(engine)
    assert set(statuses(con).values()) == {"done"}


def test_unresponsive_worker_is_detached_after_grace(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="stuck [hang]", owner=a)
    runner = FakeRunner(duration=(0.01, 0.01), seed=1, hang_seconds=1.5)
    engine = make_engine(runner, kill_grace=0.2)
    engine.tick()
    time.sleep(0.1)
    killswitch.engage(con, home, FOUNDER)
    for _ in range(15):
        engine.tick()
        time.sleep(0.05)
    task = tasks.get_task(con, t)
    assert task["status"] == "assigned" and task["lease_owner"] is None
    assert any(json.loads(e["payload"]).get("forced") for e in tasks.timeline(con, t) if e["type"] == "task.stopped")
    engine.wait_for_workers(5)                      # late result from the detached worker is ignored
    assert tasks.get_task(con, t)["status"] == "assigned"
    assert "task.late_result_ignored" in event_types(con, t)


def test_ram_guard_pauses_starts_and_logs_once(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="needs memory", owner=a)
    level = {"used": 0.95}
    engine = make_engine(threaded=False, ram_probe=lambda: level["used"])
    for _ in range(3):
        assert engine.tick()["ram_blocked"]
    assert tasks.get_task(con, t)["status"] == "assigned"
    assert sum(1 for e in con.execute("SELECT type FROM event") if e[0] == "engine.ram_guard") == 1
    level["used"] = 0.30
    run_until_idle(engine)
    assert tasks.get_task(con, t)["status"] == "done"
    assert sum(1 for e in con.execute("SELECT type FROM event") if e[0] == "engine.ram_guard") == 2


def test_runtime_limit_blocks_a_stuck_task(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="forever [hang]", owner=a)
    runner = FakeRunner(duration=(0.01, 0.01), seed=1, hang_seconds=1.0)
    engine = make_engine(runner, max_runtime=0.2)
    engine.tick()
    for _ in range(10):
        time.sleep(0.05)
        engine.tick()
    task = tasks.get_task(con, t)
    assert task["status"] == "blocked" and task["block"]["reason"] == "TIMEOUT"
    engine.wait_for_workers(5)
    assert tasks.get_task(con, t)["status"] == "blocked"      # late result did not overwrite the block


def test_founder_can_cancel_a_running_task(home, con, make_engine):
    a, b = staff(con, home, 2)
    t = tasks.create_task(con, title="slow", owner=a)
    after = tasks.create_task(con, title="after", owner=b, depends_on=[t])
    engine = make_engine(FakeRunner(duration=(2, 2), seed=1), cap=2)
    engine.tick()
    time.sleep(0.2)
    tasks.cancel_task(con, t, reason="not needed")
    for _ in range(20):
        engine.tick()
        time.sleep(0.05)
    engine.wait_for_workers(5)
    assert tasks.get_task(con, t)["status"] == "rejected"
    assert tasks.get_task(con, after)["status"] == "blocked"
    assert_transitions_are_logged(con)


def test_orphaned_running_task_is_recovered_when_lease_expires(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="orphan", owner=a)
    clock = FakeClock()
    engine = make_engine(threaded=False, clock=clock)
    con.execute("UPDATE task SET status='running', lease_owner='dead:1', lease_expires_at=? WHERE task_id=?",
                ("2026-01-01T11:59:00+00:00", t))
    engine.tick()
    task = tasks.get_task(con, t)
    assert task["recoveries"] == 1 and task["status"] in ("assigned", "done")
    assert "task.recovered" in event_types(con, t)


def test_task_that_keeps_crashing_the_engine_is_blocked_for_a_human(home, con, make_engine):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="poison", owner=a)
    con.execute("UPDATE task SET status='running', lease_owner='dead:1', lease_expires_at='2000-01-01T00:00:00+00:00', "
                "recoveries = ? WHERE task_id = ?", (tasks.MAX_RECOVERIES, t))
    engine = make_engine(threaded=False)
    task = tasks.get_task(con, t)
    assert task["status"] == "blocked" and task["block"]["reason"] == "SYSTEM_ERROR"
    assert engine.nothing_to_do()


def test_start_recovers_tasks_left_running_and_resets_employee_status(home, con):
    (a,) = staff(con, home, 1)
    t = tasks.create_task(con, title="was running", owner=a)
    con.execute("UPDATE task SET status='running', lease_owner='old:1', checkpoint='half' WHERE task_id=?", (t,))
    con.execute("UPDATE employee SET status='working' WHERE emp_id=?", (a,))
    engine = Orchestrator(home, FakeRunner(), threaded=False)
    assert engine.start() == [t]
    task = tasks.get_task(con, t)
    assert task["status"] == "assigned" and task["checkpoint"] == "half"
    assert con.execute("SELECT status FROM employee WHERE emp_id=?", (a,)).fetchone()[0] == "idle"
    engine.shutdown()


def test_only_one_engine_per_folder_and_stale_lock_is_taken_over(home, con):
    first = Orchestrator(home, FakeRunner())
    first.start()
    second = Orchestrator(home, FakeRunner())
    with pytest.raises(EngineAlreadyRunning):
        second.start()
    first.shutdown()
    (home / ".engine.lock").write_text(json.dumps({"pid": 999999, "create_time": 1.0, "instance": "ghost"}))
    third = Orchestrator(home, FakeRunner())
    third.start()
    started = [json.loads(e["payload"]) for e in con.execute("SELECT payload FROM event WHERE type='engine.started'")]
    assert started[-1]["stale_lock_taken_over"] is True
    third.shutdown()
    assert not (home / ".engine.lock").exists()


def test_engine_status_row_is_kept_fresh(home, con, make_engine):
    engine = make_engine(cap=3, threaded=False)
    engine.tick()
    row = con.execute("SELECT * FROM engine_status").fetchone()
    assert row["cap"] == 3 and row["mode"] == "simulated"
    engine.shutdown()
    assert con.execute("SELECT count(*) FROM engine_status").fetchone()[0] == 0


def test_random_workload_every_transition_is_logged(home, con, make_engine):
    ids = staff(con, home, 8)
    import random

    rng = random.Random(11)
    markers = ["", "", "", " [fail-once]", " [fail]", " [review]", " [block:LOGIN_REQUIRED]"]
    created = []
    for i in range(24):
        deps = rng.sample(created, k=min(len(created), rng.randint(0, 2)))
        created.append(tasks.create_task(con, title=f"job {i}{rng.choice(markers)}", owner=rng.choice(ids),
                                         depends_on=deps, priority=rng.randint(1, 5)))
    engine = make_engine(FakeRunner(duration=(0.01, 0.03), seed=5), cap=4, base_backoff=0.02)
    end = time.monotonic() + 40
    while time.monotonic() < end:
        engine.tick()
        if engine.nothing_to_do():
            break
        time.sleep(0.01)
    assert engine.nothing_to_do()
    assert_transitions_are_logged(con)
    assert events.verify_chain(con).ok
    assert "running" not in statuses(con).values()

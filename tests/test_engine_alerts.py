import threading

import pytest

from aicorp import killswitch, notify, tasks
from aicorp.delivery import Notifier
from aicorp.engine import Orchestrator
from aicorp.runners import FakeRunner, RunResult
from helpers import FakeClock, Recorder, assert_transitions_are_logged, staff


@pytest.fixture
def rig(home, con):
    ids = staff(con, home, 4)
    tg, mail, clock = Recorder("telegram"), Recorder("email"), FakeClock()
    notifier = Notifier({"telegram": tg, "email": mail}, clock=clock)

    class R:
        pass
    r = R()
    r.home, r.con, r.ids, r.tg, r.mail, r.clock, r.notifier, r.engines = home, con, ids, tg, mail, clock, notifier, []

    def make(runner=None, **kw):
        kw.setdefault("housekeeping_every", 0)
        kw.setdefault("ram_probe", lambda: 0.1)
        engine = Orchestrator(home, runner or FakeRunner(duration=(0.01, 0.02), seed=1), threaded=False, clock=clock,
                              notifier=notifier, **kw)
        engine.start()
        r.engines.append(engine)
        return engine
    r.make = make
    yield r
    for e in r.engines:
        if e.con is not None:
            e.shutdown()


def open_keys(con):
    return sorted(r[0] for r in con.execute("SELECT dedupe_key FROM notification WHERE resolved_at IS NULL"))


def test_a_blocked_agent_becomes_one_alert_and_clears_when_fixed(rig):
    t = tasks.create_task(rig.con, title="Research market [block:CAPTCHA]", owner=rig.ids[0])
    engine = rig.make()
    for _ in range(5):
        engine.tick()
    assert tasks.get_task(rig.con, t)["status"] == "blocked"
    assert len(open_keys(rig.con)) == 1 and open_keys(rig.con)[0].startswith("block:")
    assert len(rig.tg.sent) == 1 and len(rig.mail.sent) == 1                      # CAPTCHA is critical
    assert "W1 is blocked: CAPTCHA" in rig.tg.sent[0][0]
    tasks.resolve_block(rig.con, t)
    engine.tick()
    assert open_keys(rig.con) == [] and len(rig.tg.sent) == 1                      # resolving does not send another message


def test_kill_switch_raises_a_critical_alert_and_clears_on_release(rig):
    engine = rig.make()
    killswitch.engage(rig.con, rig.home, reason="testing")
    engine.tick()
    assert open_keys(rig.con) == ["killswitch"] and len(rig.tg.sent) == 1 and "Kill switch is engaged" in rig.tg.sent[0][0]
    killswitch.release(rig.con, rig.home)
    engine.tick()
    assert open_keys(rig.con) == []


class LimitRunner(FakeRunner):
    """Behaves like Claude hitting its usage limit until `limited` is switched off."""
    def __init__(self):
        super().__init__(duration=(0.01, 0.02), seed=1)
        self.limited = True

    def run(self, ctx):
        if self.limited:
            return RunResult("blocked", "limit", block_reason="USAGE_LIMIT", detail="You've hit your usage limit. It resets in 2 hours.")
        return super().run(ctx)


def test_usage_limit_pauses_all_work_then_resumes_by_itself(rig):
    a = tasks.create_task(rig.con, title="First job", owner=rig.ids[0])
    b = tasks.create_task(rig.con, title="Second job", owner=rig.ids[1])
    runner = LimitRunner()
    engine = rig.make(runner, cap=1)                                               # one at a time, so the second waits
    engine.tick()                                                                   # the first job hits the limit
    assert tasks.get_task(rig.con, a)["status"] == "blocked"
    block = rig.con.execute("SELECT * FROM block WHERE task_id = ? AND resolved_at IS NULL", (a,)).fetchone()
    assert block["reason"] == "USAGE_LIMIT" and block["resume_at"] > rig.clock().isoformat()
    pause = notify.get_setting(rig.con, "usage_pause_until")
    assert pause and pause == block["resume_at"]
    for _ in range(5):
        stats = engine.tick()
        assert stats["usage_paused"] and stats["started"] == []
    assert tasks.get_task(rig.con, b)["status"] == "assigned"                      # nothing else was started
    assert any(e["type"] == "engine.usage_pause" for e in tasks.timeline(rig.con, a)) or rig.con.execute("SELECT 1 FROM event WHERE type='engine.usage_pause'").fetchone()
    warn = rig.con.execute("SELECT severity FROM notification WHERE task_id = ? AND resolved_at IS NULL", (a,)).fetchone()
    assert warn["severity"] == "warn" and rig.tg.sent == []                        # a warning, shown on the dashboard
    runner.limited = False
    rig.clock.advance(2 * 3600 + 120)
    for _ in range(6):
        engine.tick()
    assert tasks.get_task(rig.con, a)["status"] == "done" and tasks.get_task(rig.con, b)["status"] == "done"
    assert notify.get_setting(rig.con, "usage_pause_until") is None
    assert open_keys(rig.con) == []
    assert_transitions_are_logged(rig.con)


def test_disk_nearly_full_stops_new_work_and_alerts(rig):
    t = tasks.create_task(rig.con, title="Needs disk", owner=rig.ids[0])
    level = {"free": 400_000_000}
    engine = rig.make(disk_probe=lambda p: (100_000_000_000, level["free"]))
    for _ in range(3):
        stats = engine.tick()
        assert stats["disk_blocked"] and stats["started"] == []
    assert tasks.get_task(rig.con, t)["status"] == "assigned"
    assert open_keys(rig.con) == ["disk"] and len(rig.tg.sent) == 1
    level["free"] = 60_000_000_000
    for _ in range(3):
        engine.tick()
    assert tasks.get_task(rig.con, t)["status"] == "done" and open_keys(rig.con) == []


def test_sustained_memory_pressure_warns_after_a_minute(rig):
    used = {"v": 0.95}
    engine = rig.make(ram_probe=lambda: used["v"])
    engine.tick()
    assert open_keys(rig.con) == []                                                 # a brief spike is not worth a notification
    rig.clock.advance(90)
    engine.tick()
    assert open_keys(rig.con) == ["ram"]
    used["v"] = 0.2
    engine.tick()
    assert open_keys(rig.con) == []


def test_an_internal_error_in_the_scheduler_is_reported_and_the_engine_keeps_going(home, con):
    ids = staff(con, home, 1)
    t = tasks.create_task(con, title="Survivor", owner=ids[0])
    engine = Orchestrator(home, FakeRunner(duration=(0.01, 0.02), seed=1), threaded=False, housekeeping_every=0, ram_probe=lambda: 0.1)
    real = engine._runnable
    calls = {"n": 0}

    def flaky(now, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom: something unexpected")
        return real(now, limit)
    engine._runnable = flaky
    stop = threading.Event()
    thread = threading.Thread(target=lambda: engine.run_forever(poll=0.02, stop=stop, exit_when_idle=True), daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert tasks.get_task(con, t)["status"] == "done"
    err = con.execute("SELECT * FROM notification WHERE dedupe_key = 'error:scheduler:RuntimeError'").fetchone()
    assert err["severity"] == "critical" and "RuntimeError" in err["detail"]
    assert con.execute("SELECT count(*) FROM event WHERE type = 'system.error'").fetchone()[0] == 1


def test_a_worker_that_dies_unexpectedly_is_reported(rig):
    t = tasks.create_task(rig.con, title="Doomed", owner=rig.ids[0])
    engine = rig.make()
    def broken_finish(*a, **k):
        raise RuntimeError("database vanished")
    engine._finish = broken_finish
    engine.tick()
    assert [k for k in open_keys(rig.con)] == ["error:worker T-" + t[2:] + ":RuntimeError"] or any(k.startswith("error:worker") for k in open_keys(rig.con))
    assert tasks.get_task(rig.con, t)["status"] == "running"                        # left for lease recovery, not lost


def test_a_task_that_keeps_failing_ends_as_one_critical_alert_to_both_channels(rig):
    t = tasks.create_task(rig.con, title="Cost model [fail]", owner=rig.ids[0])
    engine = rig.make(base_backoff=0.5)
    for _ in range(12):
        engine.tick()
        rig.clock.advance(5)
    assert tasks.get_task(rig.con, t)["status"] == "failed"
    keys = open_keys(rig.con)
    assert keys == [f"failed:{t}"]
    assert len(rig.tg.sent) == 1 and len(rig.mail.sent) == 1 and "Task failed: Cost model" in rig.tg.sent[0][0]
    tasks.retry_failed(rig.con, t)
    engine.tick()
    assert f"failed:{t}" not in open_keys(rig.con)

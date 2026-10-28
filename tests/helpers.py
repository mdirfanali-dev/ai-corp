"""Shared helpers for task and engine tests."""
import time
from datetime import datetime, timedelta, timezone

from aicorp import org


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def staff(con, home, n, prefix="W"):
    """n employees who may execute work. Returns their ids."""
    org.seed_system(con, home)
    role = con.execute("SELECT role_id FROM role WHERE name = 'Worker'").fetchone()
    role_id = role[0] if role else org.create_role(con, {"name": "Worker", "execute": True})
    return [org.create_employee(con, home, {"name": f"{prefix}{i}", "role_ids": [role_id]}) for i in range(1, n + 1)]


def run_until_idle(engine, timeout=20.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        engine.tick()
        if engine.nothing_to_do():
            return
        time.sleep(step)
    raise AssertionError("engine did not become idle in time")


def statuses(con):
    return {r["task_id"]: r["status"] for r in con.execute("SELECT task_id, status FROM task")}


def assert_transitions_are_logged(con):
    """Every task's status is explained by an unbroken chain of logged transitions."""
    import json

    for row in con.execute("SELECT task_id, status FROM task"):
        steps = []
        for e in con.execute("SELECT type, payload FROM event WHERE task_id = ? ORDER BY id", (row["task_id"],)):
            payload = json.loads(e["payload"])
            if "from" in payload and "to" in payload:
                steps.append((e["type"], payload["from"], payload["to"]))
        previous = None
        for kind, src, dst in steps:
            if previous is not None:
                assert src == previous, f"{row['task_id']}: {kind} starts from {src} but task was {previous}"
            previous = dst
        if steps:
            assert previous == row["status"], f"{row['task_id']}: log ends at {previous}, table says {row['status']}"


class Recorder:
    """A stand-in channel: records messages, can be told to fail or be slow."""

    def __init__(self, name="telegram", fail=0, delay=0.0):
        import threading
        self.name, self.sent, self.fail, self.delay = name, [], fail, delay
        self._lock = threading.Lock()

    def send(self, subject, text):
        from aicorp.channels import DeliveryError
        import time as _t
        with self._lock:
            if self.fail > 0:
                self.fail -= 1
                raise DeliveryError("simulated outage")
        if self.delay:
            _t.sleep(self.delay)
        with self._lock:
            self.sent.append((subject, text))


def block_task(con, task_id, reason, detail="", resume_at=None):
    """Put a task into the blocked state the way the engine does."""
    from aicorp import tasks
    tasks.transition(con, task_id, "blocked", actor=tasks.FOUNDER, summary="blocked")
    return tasks.open_block(con, task_id, reason, detail, resume_at)

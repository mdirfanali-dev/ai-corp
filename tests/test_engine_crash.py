"""Real process tests: the engine is started as a separate program and killed with SIGKILL."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aicorp import events, tasks
from helpers import staff

SRC = str(Path(__file__).resolve().parents[1] / "src")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


def start_engine(home, *extra):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC}
    return subprocess.Popen([sys.executable, "-m", "aicorp.engine", "--runner", "fake", "--poll", "0.1", *extra],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_engine_refuses_to_start_without_a_runner(home):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC}
    result = subprocess.run([sys.executable, "-m", "aicorp.engine"], env=env, capture_output=True, text=True)
    assert result.returncode == 2 and "--runner claude" in result.stderr


def test_kill_minus_9_mid_run_then_restart_loses_nothing(home, con):
    ids = staff(con, home, 6)
    created = [tasks.create_task(con, title=f"job {i}", owner=ids[i]) for i in range(6)]
    tasks.add_dependency(con, created[5], created[0])           # one dependency in the mix
    first = start_engine(home, "--cap", "3", "--fake-duration", "3,3", "--lease", "30")
    try:
        running = lambda: con.execute("SELECT count(*) FROM task WHERE status='running'").fetchone()[0] >= 2
        assert wait_for(running), "engine never started two tasks:\n" + (first.stdout.read() if first.poll() else "")
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.kill()
    # state after the crash: tasks are still marked running, the lock file is left behind
    marked = [r[0] for r in con.execute("SELECT task_id FROM task WHERE status='running'")]
    assert len(marked) >= 2 and (home / ".engine.lock").exists()

    second = start_engine(home, "--cap", "3", "--fake-duration", "0.1,0.2", "--exit-when-idle")
    out, _ = second.communicate(timeout=60)
    assert second.returncode == 0, out

    final = {r["task_id"]: r["status"] for r in con.execute("SELECT task_id, status FROM task")}
    assert set(final) == set(created) and set(final.values()) == {"done"}, f"lost or stuck tasks: {final}"
    recovered = {e["task_id"] for e in con.execute("SELECT task_id FROM event WHERE type='task.recovered'")}
    assert set(marked) <= recovered
    started = con.execute("SELECT payload FROM event WHERE type='engine.started' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert '"stale_lock_taken_over":true' in started
    assert events.verify_chain(con).ok
    assert not (home / ".engine.lock").exists()
    # employees are not left marked as working
    assert con.execute("SELECT count(*) FROM employee WHERE status = 'working'").fetchone()[0] == 0


def test_sigterm_stops_cleanly_and_requeues_running_tasks(home, con):
    ids = staff(con, home, 2)
    created = [tasks.create_task(con, title=f"job {i}", owner=ids[i]) for i in range(2)]
    proc = start_engine(home, "--cap", "2", "--fake-duration", "5,5")
    try:
        assert wait_for(lambda: con.execute("SELECT count(*) FROM task WHERE status='running'").fetchone()[0] == 2)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert {tasks.get_task(con, t)["status"] for t in created} == {"assigned"}
    assert not (home / ".engine.lock").exists()

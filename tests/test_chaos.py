"""Chaos: processes frozen and killed, the database locked, the disk full. After each, nothing may be lost or run
twice, the database must pass its integrity check and the audit log must still verify."""
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from aicorp import events, tasks
from aicorp.config import db_path
from aicorp.db import connect
from helpers import assert_transitions_are_logged, staff

SRC = str(Path(__file__).resolve().parents[1] / "src")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


def start_engine(home, *extra):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC}
    return subprocess.Popen([sys.executable, "-m", "aicorp.engine", "--runner", "fake", "--poll", "0.1", *extra],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(predicate, timeout=40.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def healthy(home):
    con = connect(db_path(home))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert events.verify_chain(con).ok
        assert_transitions_are_logged(con)
    finally:
        con.close()


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_repeated_kill_minus_9_never_loses_or_duplicates_work(home, con):
    ids = staff(con, home, 8)
    made = [tasks.create_task(con, title=f"job {i}", owner=ids[i]) for i in range(8)]
    for round_ in range(3):                                       # killed three times in a row
        proc = start_engine(home, "--cap", "4", "--fake-duration", "1.5,1.5", "--lease", "5")
        assert wait_for(lambda: con.execute("SELECT count(*) FROM task WHERE status='running'").fetchone()[0] >= 1), "no task started"
        time.sleep(0.4)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    final = start_engine(home, "--cap", "4", "--fake-duration", "0.3,0.3", "--lease", "5")
    try:
        assert wait_for(lambda: con.execute("SELECT count(*) FROM task WHERE status = 'done'").fetchone()[0] == 8, 90), \
            dict(con.execute("SELECT status, count(*) FROM task GROUP BY status").fetchall())
    finally:
        stop(final)
    for t in made:
        assert con.execute("SELECT count(*) FROM event WHERE task_id = ? AND type = 'task.done'", (t,)).fetchone()[0] == 1, t      # finished exactly once
    healthy(home)


def test_a_frozen_engine_laptop_sleep_does_not_run_a_task_twice(home, con):
    ids = staff(con, home, 1)
    t = tasks.create_task(con, title="long job", owner=ids[0])
    proc = start_engine(home, "--cap", "1", "--fake-duration", "4,4", "--lease", "2")
    try:
        assert wait_for(lambda: con.execute("SELECT status FROM task WHERE task_id = ?", (t,)).fetchone()[0] == "running")
        os.kill(proc.pid, signal.SIGSTOP)                         # the lid is closed: every thread freezes, the lease runs out
        time.sleep(6)
        os.kill(proc.pid, signal.SIGCONT)                         # lid opened
        assert wait_for(lambda: con.execute("SELECT status FROM task WHERE task_id = ?", (t,)).fetchone()[0] == "done", 60), \
            con.execute("SELECT status FROM task WHERE task_id = ?", (t,)).fetchone()[0]
    finally:
        stop(proc)
    assert con.execute("SELECT count(*) FROM event WHERE task_id = ? AND type = 'task.done'", (t,)).fetchone()[0] == 1
    healthy(home)


def test_a_locked_database_makes_the_engine_wait_not_crash(home, con):
    ids = staff(con, home, 2)
    a = tasks.create_task(con, title="job a", owner=ids[0])
    blocker = sqlite3.connect(db_path(home), timeout=30, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")                            # someone holds the write lock
    proc = start_engine(home, "--cap", "2", "--fake-duration", "0.2,0.2")
    try:
        time.sleep(3)
        assert proc.poll() is None, "the engine died while the database was locked:\n" + proc.stdout.read()
        blocker.execute("ROLLBACK")
        blocker.close()
        assert wait_for(lambda: con.execute("SELECT status FROM task WHERE task_id = ?", (a,)).fetchone()[0] == "done", 40)
    finally:
        stop(proc)
    healthy(home)


def test_a_full_disk_fails_cleanly_and_leaves_a_consistent_database(home, con):
    ids = staff(con, home, 1)
    for i in range(30):
        tasks.create_task(con, title=f"seed {i}", owner=ids[0])
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    size = db_path(home).stat().st_size
    script = textwrap.dedent(f"""
        import resource, signal, sqlite3, sys
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        sys.path.insert(0, {SRC!r})
        from pathlib import Path
        from aicorp import events
        from aicorp.config import db_path
        from aicorp.db import connect
        home = Path({str(home)!r})
        con = connect(db_path(home))
        resource.setrlimit(resource.RLIMIT_FSIZE, ({size + 4096}, {size + 4096}))      # the disk is "full" from here on
        wrote = failed = 0
        for i in range(2000):
            try:
                events.append(con, type="test.fill", summary="x" * 400, payload={{"i": i}})
                wrote += 1
            except sqlite3.OperationalError as err:
                failed += 1
                last = str(err)
                break
        print("wrote", wrote, "failed", failed, last if failed else "")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert "failed 1" in out.stdout, out.stdout + out.stderr
    print("\n" + out.stdout.strip())
    healthy(home)                                                 # everything committed before the failure is intact, nothing half-written
    con2 = connect(db_path(home))
    before = con2.execute("SELECT count(*) FROM event").fetchone()[0]
    events.append(con2, type="test.after", summary="space is back")          # and the database works again at once
    assert con2.execute("SELECT count(*) FROM event").fetchone()[0] == before + 1 and events.verify_chain(con2).ok

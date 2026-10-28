"""The Advisor's jobs in real programs: a computer that was off for two hours catches up when the engine
(or only the watchdog) starts, and writes the "while you were away" report."""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aicorp import notify, projects, tasks
from helpers import staff

SRC = str(Path(__file__).resolve().parents[1] / "src")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


def prepare_two_hours_away(home, con):
    ids = staff(con, home, 2)
    now = datetime.now(timezone.utc)
    two_hours_ago = (now - timedelta(hours=2)).isoformat()
    t = tasks.create_task(con, title="Waiting approval", owner=ids[0], risk_tier=3)
    con.execute("UPDATE task SET status = 'awaiting_approval' WHERE task_id = ?", (t,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, ?)", (t, two_hours_ago))
    projects.create_meeting(con, home, {"title": "Missed sync", "scheduled_at": (now - timedelta(hours=1)).astimezone().strftime("%Y-%m-%dT%H:%M")})
    from aicorp import scheduler, advisor  # noqa: F401
    scheduler.ensure_defaults(con, now - timedelta(hours=2, minutes=1))
    con.execute("UPDATE schedule SET next_run = ?", (two_hours_ago,))
    notify.set_setting(con, "monitor_last_tick", two_hours_ago)


def run_program(module, home, *args, seconds=None):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC, "AICORP_TRANSPORT_DIR": str(home / "outbox")}
    proc = subprocess.Popen([sys.executable, "-m", module, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if seconds is None:
        out, _ = proc.communicate(timeout=60)
        return proc.returncode, out
    time.sleep(seconds)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    return proc.returncode, out


def check_caught_up(con, home):
    runs = [dict(r) for r in con.execute("SELECT * FROM job_run")]
    assert runs and all(r["status"] == "ok" for r in runs), runs
    assert {r["job"] for r in runs if r["catch_up"]} >= {"scan_active", "nag_check"}
    away = con.execute("SELECT * FROM report WHERE kind = 'away'").fetchone()
    assert away and "While you were away (2 h" in away["title"]
    text = (home / away["path"]).read_text()
    assert "Missed sync" in text and "Waiting approval" in text and "## Caught up" in text
    keys = {r[0] for r in con.execute("SELECT dedupe_key FROM notification WHERE resolved_at IS NULL")}
    assert any(k.startswith("adv:missed:") for k in keys) and any(k.startswith("report:") for k in keys)
    assert con.execute("SELECT count(*) FROM report WHERE kind = 'away'").fetchone()[0] == 1


def test_engine_catches_up_after_two_hours_off(home, con):
    prepare_two_hours_away(home, con)
    code, out = run_program("aicorp.engine", home, "--runner", "fake", "--exit-when-idle", "--poll", "0.2")
    assert code == 0, out
    check_caught_up(con, home)


def test_watchdog_alone_also_catches_up_and_a_second_program_does_not_repeat_it(home, con):
    prepare_two_hours_away(home, con)
    code, out = run_program("aicorp.watchdog", home, "--poll", "0.5", seconds=4)
    assert code == 0, out
    check_caught_up(con, home)
    before = con.execute("SELECT count(*) FROM job_run").fetchone()[0]
    code, out = run_program("aicorp.engine", home, "--runner", "fake", "--exit-when-idle", "--poll", "0.2")
    assert code == 0, out
    assert con.execute("SELECT count(*) FROM job_run").fetchone()[0] == before          # nothing ran twice
    assert con.execute("SELECT count(*) FROM report WHERE kind = 'away'").fetchone()[0] == 1

import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from aicorp import notify, tasks, watchdog
from aicorp.delivery import Notifier
from helpers import FakeClock, Recorder, staff

SRC = str(Path(__file__).resolve().parents[1] / "src")


def put_engine_status(con, pid, heartbeat):
    con.execute("DELETE FROM engine_status")
    con.execute("INSERT INTO engine_status VALUES (1, 'i', ?, ?, ?, 4, 0, 'live')", (pid, heartbeat.isoformat(), heartbeat.isoformat()))


@pytest.fixture
def dog(con):
    tg, mail = Recorder("telegram"), Recorder("email")
    class D: pass
    d = D()
    d.con, d.tg, d.mail, d.clock = con, tg, mail, FakeClock()
    d.notifier = Notifier({"telegram": tg, "email": mail}, clock=d.clock)
    return d


def test_dead_engine_is_reported_once_and_cleared_when_it_returns(dog):
    now = dog.clock()
    put_engine_status(dog.con, os.getpid(), now - timedelta(seconds=120))        # live pid, but silent for 2 minutes
    for _ in range(8):
        result = watchdog.tick(dog.con, dog.notifier, now)
    assert result["engine"] is False
    assert len(dog.tg.sent) == 1 and len(dog.mail.sent) == 1                     # not spammed
    assert "task engine stopped unexpectedly" in dog.tg.sent[0][0]
    row = dog.con.execute("SELECT * FROM notification WHERE dedupe_key = 'engine:down'").fetchone()
    assert row["severity"] == "critical" and row["resolved_at"] is None and row["repeat_count"] == 8
    put_engine_status(dog.con, os.getpid(), now)
    assert watchdog.tick(dog.con, dog.notifier, now)["engine"] is True
    assert dog.con.execute("SELECT resolved_at FROM notification WHERE dedupe_key = 'engine:down'").fetchone()[0]


def test_dead_process_is_detected_even_with_a_fresh_heartbeat(dog):
    now = dog.clock()
    put_engine_status(dog.con, 999999, now)
    assert watchdog.tick(dog.con, dog.notifier, now)["engine"] is False


def test_no_alert_when_no_engine_is_expected(dog):
    assert watchdog.tick(dog.con, dog.notifier)["engine"] is None
    assert dog.con.execute("SELECT count(*) FROM notification").fetchone()[0] == 0 and dog.tg.sent == []


def test_the_timeout_is_a_setting(dog):
    now = dog.clock()
    put_engine_status(dog.con, os.getpid(), now - timedelta(seconds=45))
    assert watchdog.tick(dog.con, dog.notifier, now)["engine"] is False         # default 30 s
    notify.set_setting(dog.con, "engine_down_after", 90)
    notify.resolve_key(dog.con, "engine:down")
    assert watchdog.tick(dog.con, dog.notifier, now)["engine"] is True


def test_watchdog_delivers_notifications_created_by_others_and_reports_itself(dog):
    notify.upsert(dog.con, key="x", category="blocked", severity="critical", title="Made by the dashboard")
    watchdog.tick(dog.con, dog.notifier)
    assert len(dog.tg.sent) == 1 and len(dog.mail.sent) == 1
    assert watchdog.watchdog_state(dog.con)["running"] is True
    dog.con.execute("UPDATE setting SET value = ? WHERE key = 'watchdog_heartbeat'", (json.dumps("2000-01-01T00:00:00+00:00"),))
    assert watchdog.watchdog_state(dog.con)["running"] is False


# ------------------------------------------------------------------ real processes

pytestmark_posix = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


def spawn(module, home, folder, *args):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC, "AICORP_TRANSPORT_DIR": str(folder)}
    return subprocess.Popen([sys.executable, "-m", module, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def lines(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


@pytestmark_posix
def test_killing_the_engine_makes_the_watchdog_alert_telegram_and_email(home, con, tmp_path):
    out = tmp_path / "outbox"
    ids = staff(con, home, 2)
    for i in range(2):
        tasks.create_task(con, title=f"long job {i}", owner=ids[i])
    notify.set_setting(con, "engine_down_after", 3)
    engine = spawn("aicorp.engine", home, out, "--runner", "fake", "--cap", "2", "--fake-duration", "30,30", "--poll", "0.1")
    dog = spawn("aicorp.watchdog", home, out, "--poll", "0.5")
    try:
        assert wait_for(lambda: con.execute("SELECT count(*) FROM task WHERE status='running'").fetchone()[0] == 2), "engine did not start work"
        time.sleep(1.5)
        assert lines(out / "telegram.jsonl") == [], "a healthy engine must not cause alerts"
        os.kill(engine.pid, signal.SIGKILL)
        engine.wait(timeout=10)
        assert wait_for(lambda: len(lines(out / "telegram.jsonl")) >= 1 and len(lines(out / "email.jsonl")) >= 1, 25), "no alert arrived"
        time.sleep(3.0)                                                         # several more watchdog cycles
        telegram, email = lines(out / "telegram.jsonl"), lines(out / "email.jsonl")
        assert len(telegram) == 1 and len(email) == 1, "the same alert was sent more than once"
        assert "task engine stopped unexpectedly" in telegram[0]["subject"] and "No heartbeat" in telegram[0]["text"]
        assert email[0]["subject"].startswith("[AICorp] CRITICAL")
        # restarting the engine clears the alert
        engine2 = spawn("aicorp.engine", home, out, "--runner", "fake", "--cap", "2", "--fake-duration", "30,30", "--poll", "0.1")
        try:
            assert wait_for(lambda: con.execute("SELECT resolved_at FROM notification WHERE dedupe_key='engine:down'").fetchone()[0] is not None, 20)
        finally:
            engine2.send_signal(signal.SIGTERM)
            engine2.wait(timeout=20)
    finally:
        for proc in (engine, dog):
            if proc.poll() is None:
                proc.kill()
        dog.wait(timeout=10)


@pytestmark_posix
def test_a_clean_engine_shutdown_is_not_an_alert(home, con, tmp_path):
    out = tmp_path / "outbox"
    ids = staff(con, home, 1)
    tasks.create_task(con, title="job", owner=ids[0])
    notify.set_setting(con, "engine_down_after", 3)
    dog = spawn("aicorp.watchdog", home, out, "--poll", "0.5")
    engine = spawn("aicorp.engine", home, out, "--runner", "fake", "--fake-duration", "0.1,0.2", "--poll", "0.1", "--exit-when-idle")
    try:
        engine.wait(timeout=30)
        time.sleep(5)
        assert lines(out / "telegram.jsonl") == [] and lines(out / "email.jsonl") == []
        assert con.execute("SELECT count(*) FROM notification WHERE dedupe_key='engine:down'").fetchone()[0] == 0
    finally:
        dog.send_signal(signal.SIGTERM)
        dog.wait(timeout=10)

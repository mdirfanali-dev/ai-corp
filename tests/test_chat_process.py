"""Chat with the Advisor through real programs: the watchdog listens, the engine answers questions."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aicorp import killswitch
from aicorp.config import db_path
from aicorp.db import connect
from helpers import staff

SRC = str(Path(__file__).resolve().parents[1] / "src")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


def start(module, home, *args):
    env = {**os.environ, "AICORP_HOME": str(home), "PYTHONPATH": SRC, "AICORP_TRANSPORT_DIR": str(home / "box")}
    return subprocess.Popen([sys.executable, "-m", module, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.communicate(timeout=20)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()[0]


def lines(home, name):
    path = home / "box" / f"{name}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def send(home, text, sender="4242"):
    (home / "box").mkdir(exist_ok=True)
    with open(home / "box" / "telegram_in.jsonl", "a") as fh:
        fh.write(json.dumps({"sender": sender, "text": text}) + "\n")


def wait_for(predicate, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def setup(home, con):
    (home / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=123456:ABCdefTOKENvalue\nTELEGRAM_CHAT_ID=4242\n")
    staff(con, home, 2)
    return home


def test_status_and_stop_over_chat_and_strangers_are_ignored(setup, con):
    home = setup
    dog = start("aicorp.watchdog", home)
    try:
        sent_at = time.monotonic()
        send(home, "/status")
        assert wait_for(lambda: any("Engine:" in m["text"] for m in lines(home, "telegram")))
        round_trip = time.monotonic() - sent_at
        print(f"\n/status round trip through the real watchdog: {round_trip:.1f} s")
        assert round_trip < 15
        send(home, "/stop", sender="31337")                            # a stranger
        time.sleep(4)
        assert not killswitch.engaged(home)
        send(home, "/stop")
        assert wait_for(lambda: killswitch.engaged(home))
        assert wait_for(lambda: any("ENGAGED" in m["text"] for m in lines(home, "telegram")))
    finally:
        out = stop(dog)
    assert "Traceback" not in out, out
    rows = connect(db_path(home)).execute("SELECT handled_as FROM chat_message WHERE direction = 'in' ORDER BY msg_id").fetchall()
    assert [r[0] for r in rows] == ["command", "ignored", "command"]


def test_question_is_answered_by_the_engine_and_returned_to_the_same_channel(setup, con):
    home = setup
    dog, engine = start("aicorp.watchdog", home), start("aicorp.engine", home, "--runner", "fake")
    try:
        sent_at = time.monotonic()
        send(home, "Where do we stand on the launch?")
        assert wait_for(lambda: any("On it" in m["text"] for m in lines(home, "telegram")), 20)
        answered = wait_for(lambda: len(lines(home, "telegram")) >= 2, 40)
        print(f"\nquestion round trip (simulated worker): {time.monotonic() - sent_at:.1f} s")
    finally:
        out = stop(engine) + stop(dog)
    assert "Traceback" not in out, out
    assert answered, lines(home, "telegram")
    c = connect(db_path(home))
    row = c.execute("SELECT * FROM chat_message WHERE direction = 'in'").fetchone()
    assert row["handled_as"] == "answered" and row["task_id"]
    task = c.execute("SELECT status, risk_tier FROM task WHERE task_id = ?", (row["task_id"],)).fetchone()
    assert task["status"] in ("done", "review") and task["risk_tier"] == 0

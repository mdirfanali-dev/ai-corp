"""The supervisor with tiny stand-in programs (exact behaviours) and with the real web, watchdog and engine."""
import os
import subprocess
import sys
import time
import urllib.request

import psutil
import pytest

from aicorp import config, supervisor
from aicorp.supervisor import Supervisor, SupervisorError


def script(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return [sys.executable, "-u", str(path)]


STEADY = "import time\nprint('hello', flush=True)\nwhile True:\n    time.sleep(0.2)\n"
STUBBORN = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ignoring TERM', flush=True)\nwhile True:\n    time.sleep(0.2)\n"
CRASH = "import sys\nprint('about to crash', flush=True)\nsys.exit(3)\n"
NOISY = "for i in range(2000):\n    print('line', i)\nimport time\ntime.sleep(30)\n"


@pytest.fixture
def home(tmp_path):
    return config.ensure_layout(tmp_path / "data")


def make(home, tmp_path, **argv):
    procs = {n: script(tmp_path, n, argv.get(n, STEADY)) for n in ("web", "watchdog", "engine")}
    return Supervisor(home, argv=procs, conflict_check=lambda: [], stop_timeout=1.5, restart_delay=0.05, ready_timeout=3, port=18765,
                      env={"PATH": os.environ["PATH"]})


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def states(sup):
    return {p["name"]: p["state"] for p in sup.state()["processes"]}


def running_pids(sup):
    return [p["pid"] for p in sup.state()["processes"] if p["pid"]]


def test_start_shows_running_and_captures_output(home, tmp_path):
    sup = make(home, tmp_path)
    sup.start()
    try:
        assert wait_for(lambda: set(states(sup).values()) == {"running"})
        assert wait_for(lambda: all("hello" in "\n".join(p["lines"]) for p in sup.state(tail=10)["processes"]))
        assert sup.state()["running"] and sup.state()["port"] == 18765
        with pytest.raises(SupervisorError, match="already running"):
            sup.start()
    finally:
        sup.stop()


def test_stop_is_ordered_reported_and_leaves_nothing_behind(home, tmp_path):
    sup = make(home, tmp_path)
    q = sup.subscribe()
    sup.start()
    assert wait_for(lambda: set(states(sup).values()) == {"running"})
    pids = running_pids(sup)
    steps = sup.stop()
    assert [(s["step"], s["state"]) for s in steps] == [("engine", "stopping"), ("engine", "off"), ("watchdog", "stopping"), ("watchdog", "off"),
                                                         ("web", "stopping"), ("web", "off"), ("browsers", "stopping"), ("browsers", "off")]
    events = []
    while not q.empty():
        events.append(q.get())
    shutdown = [e for e in events if e["kind"] == "shutdown"]
    assert [e["step"] for e in shutdown][-1] == "done" and len(shutdown) == len(steps) + 1
    assert set(states(sup).values()) == {"off"} and not sup.state()["running"]
    assert not any(psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE for p in pids)
    sup.start()                                   # can be started again
    assert wait_for(lambda: set(states(sup).values()) == {"running"})
    sup.stop()


def test_a_program_that_ignores_stop_is_forced_and_reported(home, tmp_path):
    sup = make(home, tmp_path, engine=STUBBORN)
    sup.start()
    assert wait_for(lambda: states(sup)["engine"] == "running")
    steps = sup.stop()
    engine = [s for s in steps if s["step"] == "engine" and s["state"] == "off"][0]
    assert engine["detail"] == "did not stop in time, forced"
    assert states(sup)["engine"] == "off"


def test_crash_is_shown_and_restarted_only_a_few_times(home, tmp_path):
    sup = make(home, tmp_path, engine=CRASH)
    sup.max_restarts = 2
    sup.start()
    try:
        assert wait_for(lambda: states(sup)["engine"] == "crashed")
        info = [p for p in sup.state()["processes"] if p["name"] == "engine"][0]
        assert info["restarts"] == 2 and info["exit_code"] == 3 and "not restarted again" in info["note"]
        assert wait_for(lambda: states(sup)["web"] == "running")       # the others are untouched
    finally:
        sup.stop()


def test_log_memory_is_bounded(home, tmp_path):
    sup = make(home, tmp_path, web=NOISY)
    sup.start()
    try:
        assert wait_for(lambda: any("line 1999" in l for p in sup.state(tail=600)["processes"] if p["name"] == "web" for l in p["lines"]))
        web = [p for p in sup.state(tail=10000)["processes"] if p["name"] == "web"][0]
        assert len(web["lines"]) == supervisor.LOG_LINES
    finally:
        sup.stop()


def test_secrets_never_reach_the_log_panes(home, tmp_path):
    (home / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=tok-12345-SECRET-value\n")
    leak = "import time\nprint('token is tok-12345-SECRET-value ok', flush=True)\ntime.sleep(30)\n"
    sup = make(home, tmp_path, watchdog=leak)
    q = sup.subscribe()
    sup.start()
    try:
        assert wait_for(lambda: any("token is" in l for p in sup.state(tail=10)["processes"] for l in p["lines"]))
        text = str(sup.state(tail=50))
        assert "SECRET-value" not in text
        seen = []
        while not q.empty():
            seen.append(str(q.get()))
        assert "SECRET-value" not in "".join(seen)
    finally:
        sup.stop()


def test_environment_has_no_api_key_and_carries_the_port(home, tmp_path):
    sup = Supervisor(home, port=18770, env={"ANTHROPIC_API_KEY": "sk-should-not-pass", "PATH": "/usr/bin"}, conflict_check=lambda: [])
    env = sup.environment()
    assert "ANTHROPIC_API_KEY" not in env and env["AICORP_PORT"] == "18770" and env["AICORP_HOME"] == str(home)
    assert supervisor.package_root() in env["PYTHONPATH"]


def test_one_control_panel_per_data_folder(home, tmp_path, monkeypatch):
    a = make(home, tmp_path)
    a.start()
    try:
        b = make(home, tmp_path)
        b.lock_path.write_text('{"pid": %d}' % os.getppid())      # another live process that is not ours
        monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: type("X", (), {"cmdline": lambda self: ["python", "-m", "aicorp.app"]})())
        with pytest.raises(SupervisorError, match="Another control panel"):
            b.start()
    finally:
        a.stop()


def test_programs_started_elsewhere_stop_the_start(home, tmp_path):
    sup = make(home, tmp_path)
    sup.conflict_check = lambda: [("engine", 4242)]
    with pytest.raises(SupervisorError, match="engine \\(process 4242\\)"):
        sup.start()
    assert set(states(sup).values()) == {"off"}


def test_busy_port_moves_to_the_next_free_one(home, tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 18780))
    s.listen(1)
    try:
        sup = Supervisor(home, port=18780, argv={n: script(tmp_path, n, STEADY) for n in ("web", "watchdog", "engine")},
                         conflict_check=lambda: [], env={"PATH": os.environ["PATH"]}, ready_timeout=2)
        sup.start()
        try:
            assert sup.state()["port"] == 18781 and sup.dashboard_url().endswith(":18781/")
            assert sup._env["AICORP_PORT"] == "18781"
        finally:
            sup.stop()
    finally:
        s.close()


def test_restart_one_program(home, tmp_path):
    sup = make(home, tmp_path)
    sup.start()
    try:
        assert wait_for(lambda: states(sup)["engine"] == "running")
        before = [p["pid"] for p in sup.state()["processes"] if p["name"] == "engine"][0]
        sup.restart("engine")
        assert wait_for(lambda: states(sup)["engine"] == "running" and [p["pid"] for p in sup.state()["processes"] if p["name"] == "engine"][0] != before)
        with pytest.raises(SupervisorError):
            sup.restart("nonsense")
    finally:
        sup.stop()


def test_real_claude_mode_needs_the_worker_user(home, tmp_path, monkeypatch):
    sup = make(home, tmp_path)
    monkeypatch.setattr(supervisor.doctor.Probe, "run", lambda self, cmd, timeout=15: (1, ""))
    with pytest.raises(SupervisorError, match="worker user"):
        sup.start("claude")
    with pytest.raises(SupervisorError, match="fake"):
        sup.start("nonsense")


# ------------------------------------------------------------------ the real programs

def http_status(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status
    except OSError:
        return None


def test_real_programs_start_answer_and_stop_cleanly(tmp_path):
    home = config.ensure_layout(tmp_path / "real")
    sup = Supervisor(home, mode="fake", port=18790, conflict_check=lambda: [], stop_timeout=10, ready_timeout=30)
    sup.start()
    try:
        assert wait_for(lambda: set(states(sup).values()) == {"running"}, 40), (states(sup), sup.state(tail=20))
        assert wait_for(lambda: http_status(sup.dashboard_url()) == 200, 20)
        logs = " ".join(" ".join(p["lines"]) for p in sup.state(tail=50)["processes"])
        assert "dashboard" in logs.lower() or "watchdog" in logs.lower() or "engine" in logs.lower()
        pids = running_pids(sup)
    finally:
        steps = sup.stop()
    assert {s["state"] for s in steps} <= {"stopping", "off"} and not [s for s in steps if s["detail"] and "forced" in s["detail"]], steps
    assert http_status(sup.dashboard_url()) is None
    assert wait_for(lambda: not [p for p in pids if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE], 15)


def test_clash_check_only_counts_programs_on_the_same_data_folder(home, tmp_path):
    other = config.ensure_layout(tmp_path / "other")
    env = {**os.environ, "PYTHONPATH": supervisor.package_root()}
    mine = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)", "-m", "aicorp.watchdog"], env={**env, "AICORP_HOME": str(home)})
    theirs = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)", "-m", "aicorp.engine"], env={**env, "AICORP_HOME": str(other)})
    try:
        time.sleep(0.5)
        found = supervisor.foreign_processes(home=home)
        assert ("watchdog", mine.pid) in found and not any(pid == theirs.pid for _, pid in found)
        assert any(pid == theirs.pid for _, pid in supervisor.foreign_processes(home=other))
    finally:
        mine.kill()
        theirs.kill()
        mine.wait()
        theirs.wait()

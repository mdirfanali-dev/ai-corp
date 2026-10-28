"""Supervisor: starts, watches and stops the three AICorp programs (web, watchdog, engine) as child processes.

It keeps a bounded tail of each program's output (secrets scrubbed) and publishes state changes and log lines to
subscribers. Stopping is an ordered sequence (engine, watchdog, web, then browsers we started) and every step is
reported, so a screen can show what is going off. The supervisor never uses sudo itself.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from . import doctor, redact

ORDER_STOP = ("engine", "watchdog", "web")        # shutdown order; start order is the reverse
ORDER_START = ("web", "watchdog", "engine")
LOG_LINES = 500
MAX_LINE = 2000


@dataclass
class Managed:
    name: str
    argv: list[str]
    state: str = "off"                 # off | starting | running | stopping | crashed
    popen: subprocess.Popen | None = None
    pid: int | None = None
    exit_code: int | None = None
    restarts: int = 0
    since: float = 0.0
    lines: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    seq: int = 0
    stopping: bool = False
    note: str = ""


class SupervisorError(RuntimeError):
    pass


def package_root() -> str:
    """Folder that contains the ``aicorp`` package, so children work without an editable install."""
    return str(Path(__file__).resolve().parents[1])


def foreign_processes(names: tuple[str, ...] = ORDER_START, own: set[int] | None = None, home: Path | None = None) -> list[tuple[str, int]]:
    """AICorp programs already running on the same data folder that this supervisor did not start."""
    from . import config
    own = own or set()
    here = (home or config.home()).resolve()
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if proc.info["pid"] in own or proc.info["pid"] == os.getpid() or not any(f"-m aicorp.{n}" in cmd for n in names):
                continue
            theirs = proc.environ().get("AICORP_HOME")
            if (Path(theirs).expanduser().resolve() if theirs else Path(config.__file__).resolve().parents[2]) != here:
                continue                                      # another company's data folder: no clash
        except (psutil.Error, OSError):
            continue
        found.extend((n, proc.info["pid"]) for n in names if f"-m aicorp.{n}" in cmd)
    return found


class Supervisor:
    def __init__(self, home: Path, mode: str = "fake", port: int | None = None, python: str | None = None,
                 argv: dict[str, list[str]] | None = None, max_restarts: int = 2, stop_timeout: float = 8.0,
                 conflict_check: Callable[[], list[tuple[str, int]]] | None = None, env: dict[str, str] | None = None,
                 restart_delay: float = 1.0, ready_timeout: float = 20.0) -> None:
        self.home = Path(home)
        self.mode = mode
        self.python = python or sys.executable
        self.base_env = dict(os.environ if env is None else env)
        self.port = port or doctor.find_free_port(doctor.Probe(self.home), int(self.base_env.get("AICORP_PORT", doctor.DEFAULT_PORT)))
        self.max_restarts = max_restarts
        self.stop_timeout = stop_timeout
        self.restart_delay = restart_delay
        self.ready_timeout = ready_timeout
        self._argv_override = argv
        self.conflict_check = conflict_check or (lambda: foreign_processes(own=self.own_pids(), home=self.home))
        self.procs: dict[str, Managed] = {name: Managed(name, []) for name in ORDER_START}
        self._lock = threading.RLock()
        self._subs: list[queue.Queue] = []
        self._event_id = 0
        self.scrub = redact.Redactor(redact.load_secret_values(self.home)).scrub
        self.lock_path = self.home / "runtime" / "supervisor.json"
        self.shutting_down = False

    # ------------------------------------------------------------ events
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, kind: str, **data) -> None:
        with self._lock:
            self._event_id += 1
            event = {"id": self._event_id, "kind": kind, **data}
            for q in list(self._subs):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass                      # a stuck viewer must never block the programs

    # ------------------------------------------------------------ description
    def own_pids(self) -> set[int]:
        return {m.pid for m in self.procs.values() if m.pid}

    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def state(self, tail: int = 0) -> dict:
        with self._lock:
            procs = []
            for name in ORDER_START:
                m = self.procs[name]
                item = {"name": name, "state": m.state, "pid": m.pid, "exit_code": m.exit_code, "restarts": m.restarts, "note": m.note,
                        "since": m.since}
                if tail:
                    item["lines"] = list(m.lines)[-tail:]
                procs.append(item)
            running = any(m.state in ("starting", "running", "stopping") for m in self.procs.values())
            return {"mode": self.mode, "port": self.port, "dashboard_url": self.dashboard_url(), "running": running,
                    "shutting_down": self.shutting_down, "processes": procs}

    # ------------------------------------------------------------ building the commands
    def environment(self) -> dict[str, str]:
        env = dict(self.base_env)
        env.pop("ANTHROPIC_API_KEY", None)                       # the app never uses API billing
        env.update({"AICORP_HOME": str(self.home), "AICORP_PORT": str(self.port), "PYTHONUNBUFFERED": "1",
                    "PYTHONPATH": os.pathsep.join(p for p in (package_root(), env.get("PYTHONPATH", "")) if p)})
        return env

    def _worker_setup(self, env: dict[str, str]) -> None:
        """Real Claude work needs the worker user. Use the configured one, or the standard one if it exists."""
        probe = doctor.Probe(self.home)
        user = env.get("AICORP_WORKER_USER")
        if not user:
            if probe.run(["id", "-u", doctor.DEFAULT_WORKER])[0] != 0:
                raise SupervisorError("Real Claude work needs the worker user. Run the doctor and follow its steps first.")
            user = env["AICORP_WORKER_USER"] = doctor.DEFAULT_WORKER
        if not env.get("AICORP_CLAUDE_BIN"):
            rc, out = probe.run(["getent", "passwd", user])
            parts = out.split(":")
            if rc == 0 and len(parts) > 5 and parts[5]:
                env["AICORP_CLAUDE_BIN"] = f"{parts[5]}/.local/bin/claude"

    def command(self, name: str) -> list[str]:
        if self._argv_override and name in self._argv_override:
            return list(self._argv_override[name])
        return {"web": [self.python, "-m", "aicorp.web", "--port", str(self.port)],
                "watchdog": [self.python, "-m", "aicorp.watchdog"],
                "engine": [self.python, "-m", "aicorp.engine", "--runner", self.mode]}[name]

    # ------------------------------------------------------------ lock file
    def _take_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            old = json.loads(self.lock_path.read_text())
            if old.get("pid") != os.getpid() and psutil.pid_exists(old["pid"]) and "aicorp" in " ".join(psutil.Process(old["pid"]).cmdline()):
                raise SupervisorError("Another control panel is already running for this data folder.")
        except (OSError, ValueError, KeyError, psutil.Error):
            pass
        self.lock_path.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))

    def release_lock(self) -> None:
        try:
            if json.loads(self.lock_path.read_text()).get("pid") == os.getpid():
                self.lock_path.unlink()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ start
    def start(self, mode: str | None = None) -> None:
        with self._lock:
            if any(m.state in ("starting", "running", "stopping") for m in self.procs.values()):
                raise SupervisorError("The system is already running.")
            if mode:
                if mode not in ("fake", "claude"):
                    raise SupervisorError("Mode must be 'fake' or 'claude'.")
                self.mode = mode
            clash = self.conflict_check()
            if clash:
                what = ", ".join(f"{n} (process {pid})" for n, pid in clash)
                raise SupervisorError(f"Already running outside the control panel: {what}. Stop those first, or just open the dashboard.")
            self._take_lock()
            env = self.environment()
            if self.mode == "claude":
                self._worker_setup(env)
            self._env = env
            self.port = doctor.find_free_port(doctor.Probe(self.home), self.port)
            env["AICORP_PORT"] = str(self.port)
            self.shutting_down = False
            for name in ORDER_START:
                self._spawn(self.procs[name], restarts=0)

    def _spawn(self, m: Managed, restarts: int) -> None:
        m.argv = self.command(m.name)
        m.state, m.exit_code, m.restarts, m.stopping, m.note = "starting", None, restarts, False, ""
        m.since = time.time()
        try:
            m.popen = subprocess.Popen(m.argv, env=self._env, cwd=str(self.home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True, text=True, bufsize=1, errors="replace")
        except OSError as exc:
            m.state, m.note, m.popen, m.pid = "crashed", f"could not start: {type(exc).__name__}", None, None
            self.emit("state", process=m.name, state=m.state, note=m.note)
            return
        m.pid = m.popen.pid
        self.emit("state", process=m.name, state="starting", pid=m.pid)
        threading.Thread(target=self._read, args=(m, m.popen), daemon=True, name=f"read-{m.name}").start()
        threading.Thread(target=self._watch, args=(m, m.popen), daemon=True, name=f"watch-{m.name}").start()

    def _read(self, m: Managed, popen: subprocess.Popen) -> None:
        for raw in popen.stdout:                       # ends when the process closes its output
            line = self.scrub(raw.rstrip("\n")[:MAX_LINE])
            with self._lock:
                m.seq += 1
                m.lines.append(line)
                seq = m.seq
            self.emit("log", process=m.name, seq=seq, line=line)

    def _ready(self, m: Managed, popen: subprocess.Popen) -> bool:
        end = time.monotonic() + self.ready_timeout
        while time.monotonic() < end and popen.poll() is None:
            if m.name == "web":
                with socket.socket() as s:
                    s.settimeout(0.3)
                    if s.connect_ex(("127.0.0.1", self.port)) == 0:
                        return True
            elif time.monotonic() > end - self.ready_timeout + 1.0:
                return True
            time.sleep(0.15)
        return popen.poll() is None

    def _watch(self, m: Managed, popen: subprocess.Popen) -> None:
        if self._ready(m, popen) and not m.stopping and m.popen is popen:
            with self._lock:
                if m.state == "starting":
                    m.state = "running"
            self.emit("state", process=m.name, state="running", pid=m.pid)
        code = popen.wait()
        with self._lock:
            if m.popen is not popen:
                return
            m.exit_code = code
            expected = m.stopping or self.shutting_down
            if expected:
                m.state, m.pid = "off", None
                self.emit("state", process=m.name, state="off", exit_code=code)
                return
            if m.restarts < self.max_restarts:
                m.note = f"stopped unexpectedly (exit {code}); restarting {m.restarts + 1}/{self.max_restarts}"
                restarts = m.restarts + 1
                self.emit("state", process=m.name, state="starting", note=m.note, exit_code=code)
            else:
                m.state, m.pid = "crashed", None
                m.note = f"stopped unexpectedly (exit {code}); not restarted again"
                self.emit("state", process=m.name, state="crashed", note=m.note, exit_code=code)
                return
        time.sleep(self.restart_delay)
        with self._lock:
            if not self.shutting_down and not m.stopping and m.popen is popen:
                self._spawn(m, restarts)

    # ------------------------------------------------------------ stop
    def _stop_one(self, m: Managed) -> str:
        """Returns 'off', 'forced' or 'idle' (it was not running)."""
        popen = m.popen
        if popen is None or popen.poll() is not None:
            with self._lock:
                m.state, m.pid = "off", None
            return "idle"
        with self._lock:
            m.stopping, m.state = True, "stopping"
        self.emit("state", process=m.name, state="stopping")
        outcome = "off"
        try:
            os.killpg(popen.pid, signal.SIGTERM)
            try:
                popen.wait(self.stop_timeout)
            except subprocess.TimeoutExpired:
                outcome = "forced"
                os.killpg(popen.pid, signal.SIGKILL)
                popen.wait(5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            outcome = "failed"
        return outcome

    def _stop_browsers(self) -> tuple[str, str]:
        from .browser_runner import SessionStore, kill_session
        try:
            store = SessionStore(self.home)
            sessions = store.all_alive()
        except OSError:
            return "off", "no browser windows"
        for session in sessions:
            kill_session(session)
            store.drop(session.get("task_id", ""))
        return "off", (f"{len(sessions)} browser window(s) closed" if sessions else "no browser windows open")

    def stop(self) -> list[dict]:
        """Ordered shutdown. Returns the steps; each is also emitted live as a ``shutdown`` event."""
        with self._lock:
            self.shutting_down = True
        steps = []

        def report(step: str, label: str, state: str, detail: str = "") -> None:
            item = {"step": step, "label": label, "state": state, "detail": detail}
            steps.append(item)
            self.emit("shutdown", **item)

        labels = {"engine": "Engine (runs the work)", "watchdog": "Watchdog (alerts and chat)", "web": "Dashboard"}
        for name in ORDER_STOP:
            report(name, labels[name], "stopping")
            outcome = self._stop_one(self.procs[name])
            state = {"idle": "off", "forced": "off", "off": "off", "failed": "failed"}[outcome]
            detail = {"forced": "did not stop in time, forced", "idle": "was not running", "failed": "could not be stopped"}.get(outcome, "")
            report(name, labels[name], state, detail)
        report("browsers", "Browser windows started by AICorp", "stopping")
        state, detail = self._stop_browsers()
        report("browsers", "Browser windows started by AICorp", state, detail)
        self.release_lock()
        self.emit("shutdown", step="done", label="Everything is off", state="off", detail="" if all(s["state"] != "failed" for s in steps) else "some steps failed")
        return steps

    def restart(self, name: str) -> None:
        with self._lock:
            if name not in self.procs:
                raise SupervisorError("Unknown program.")
            if self.shutting_down or not hasattr(self, "_env"):
                raise SupervisorError("The system is not running.")
            m = self.procs[name]
        self._stop_one(m)
        with self._lock:
            self._spawn(m, restarts=0)

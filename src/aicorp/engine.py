"""Orchestrator engine: schedules tasks onto runners, keeps leases, retries, recovers from crashes.

Run it with ``python -m aicorp.engine --runner fake`` (simulated work only until a real runner exists).
One engine runs per data folder (an instance lock enforces this). Everything it decides is plain
code; no AI is involved in scheduling.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import psutil

from . import attention, events, killswitch, limits, notify, org, redact, scheduler, skills, takeover, tasks
from .config import db_path, home as default_home
from .db import connect, migrate
from .org import SYSTEM, _tx
from .runners import FakeRunner, RunContext, RunResult, Runner

LOCK_NAME = ".engine.lock"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class EngineAlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """One engine per data folder. A lock left behind by a dead process is detected and taken over."""

    def __init__(self, home: Path, instance: str) -> None:
        self.path = home / LOCK_NAME
        self.instance = instance

    @staticmethod
    def _alive(info: dict) -> bool:
        try:
            proc = psutil.Process(int(info["pid"]))
            same = abs(proc.create_time() - float(info["create_time"])) < 2.0
            return same and proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, KeyError, ValueError, TypeError):
            return False

    def acquire(self) -> bool:
        """Take the lock. Returns True when a stale lock from a crashed engine was taken over."""
        took_over = False
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = {}
                if self._alive(info):
                    raise EngineAlreadyRunning(f"engine already running (pid {info.get('pid')})")
                self.path.unlink(missing_ok=True)
                took_over = True
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "create_time": psutil.Process().create_time(),
                           "instance": self.instance}, fh)
            return took_over
        raise EngineAlreadyRunning("could not acquire the engine lock")

    def release(self) -> None:
        try:
            if json.loads(self.path.read_text(encoding="utf-8")).get("instance") == self.instance:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


@dataclass
class Handle:
    task_id: str
    token: str
    emp_id: str
    started: datetime
    cancel: threading.Event
    thread: threading.Thread | None = None
    abandoned: bool = False
    timed_out: bool = False


class Orchestrator:
    def __init__(
        self, home: Path, runner: Runner, *, cap: int = 4, ram_limit: float = 0.80,
        lease_seconds: float = 60.0, max_runtime: float = 1800.0, base_backoff: float = 5.0,
        kill_grace: float = 5.0, ram_probe: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None, threaded: bool = True, instance: str | None = None,
        notifier=None, disk_probe=None, housekeeping_every: float = 2.0, advisor: bool = False, tz=None,
    ) -> None:
        self.home = Path(home).resolve()
        self.runner = runner
        self.cap = cap
        self.ram_limit = ram_limit
        self.lease_seconds = lease_seconds
        self.max_runtime = max_runtime
        self.base_backoff = base_backoff
        self.kill_grace = kill_grace
        self.ram_probe = ram_probe or (lambda: psutil.virtual_memory().percent / 100.0)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.threaded = threaded
        self.instance = instance or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lock = InstanceLock(self.home, self.instance)
        self.con = None
        self.active: dict[str, Handle] = {}
        self._mutex = threading.RLock()
        self._ram_blocked = False
        self._kill_since: datetime | None = None
        self.started_at: datetime | None = None
        self.notifier = notifier                 # None = create notifications but do not send them from here
        self.disk_probe = disk_probe
        self.advisor = advisor                   # run the Advisor's scheduled jobs from here (the CLI switches this on)
        self.tz = tz
        self.housekeeping_every = housekeeping_every
        self._last_housekeeping = 0.0
        self._ram_since: datetime | None = None
        self._last_error_at: datetime | None = None
        self._disk_critical = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> list[str]:
        """Open the database, take the instance lock and recover tasks left running by a crash."""
        self.con = connect(db_path(self.home))
        migrate(self.con)
        redact.install(self.home)                # secrets never reach the audit log
        took_over = self.lock.acquire()
        self.started_at = self.clock()
        recovered = self._recover_running("engine restarted while the task was running")
        self.con.execute(
            "UPDATE employee SET status = 'idle' WHERE status = 'working' AND emp_id NOT IN "
            "(SELECT owner_emp_id FROM task WHERE status = 'running' AND owner_emp_id IS NOT NULL)")
        events.append(self.con, type="engine.started",
                      summary=f"Engine started ({self.runner.name}{', simulated' if self.runner.simulated else ''}, "
                              f"cap {self.cap})" + (f"; recovered {len(recovered)} task(s)" if recovered else ""),
                      payload={"instance": self.instance, "cap": self.cap, "runner": self.runner.name,
                               "recovered": recovered, "stale_lock_taken_over": took_over}, **SYSTEM)
        self._write_status(self.clock())
        return recovered

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._mutex:
            handles = list(self.active.values())
        for handle in handles:
            handle.cancel.set()
        deadline = time.monotonic() + timeout
        for handle in handles:
            if handle.thread is not None:
                handle.thread.join(max(deadline - time.monotonic(), 0))
        if self.con is not None:
            events.append(self.con, type="engine.stopped", summary="Engine stopped",
                          payload={"instance": self.instance}, **SYSTEM)
            self.con.execute("DELETE FROM engine_status WHERE instance = ?", (self.instance,))
            self.con.close()
            self.con = None
        self.lock.release()

    def run_forever(self, poll: float = 0.5, exit_when_idle: bool = False,
                    stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.start()
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception as err:             # never die silently: tell the Founder and carry on
                    self.report_error("scheduler", err)
                    stop.wait(2.0)
                    continue
                if exit_when_idle and self.nothing_to_do():
                    break
                stop.wait(poll)
        finally:
            self.shutdown()

    # ------------------------------------------------------------ one scheduling pass

    def tick(self) -> dict:
        now = self.clock()
        stats = {"started": [], "killed": False, "ram_blocked": False, "usage_paused": False, "disk_blocked": False}
        self._write_status(now)
        self._housekeeping(now)
        if killswitch.engaged(self.home):
            self._apply_kill(now)
            stats["killed"] = True
            return stats
        self._kill_since = None
        self._extend_leases(now)
        self._deliver_cancellations()
        self._enforce_runtime(now)
        self._reap_expired(now)
        tasks.auto_unblock(self.con)
        free = self.cap - self._active_count()
        stats["ram_blocked"] = not self._ram_ok(now)
        stats["usage_paused"] = self._usage_paused(now)
        stats["disk_blocked"] = self._disk_critical
        if free > 0 and not (stats["ram_blocked"] or stats["usage_paused"] or stats["disk_blocked"]):
            for task_id in self._runnable(now, free):
                if self._start(task_id, now):
                    stats["started"].append(task_id)
        return stats

    def nothing_to_do(self) -> bool:
        if self._active_count():
            return False
        if self.con.execute("SELECT 1 FROM task WHERE status = 'running' LIMIT 1").fetchone():
            return False
        if self.con.execute("SELECT 1 FROM task WHERE status = 'assigned' AND not_before IS NOT NULL LIMIT 1").fetchone():
            return False
        return not self._runnable(self.clock(), 1)

    def wait_for_workers(self, timeout: float = 10.0) -> None:
        """Wait until every worker thread has ended, including ones that were detached."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._mutex:
                if not self.active:
                    return
            time.sleep(0.01)

    # ------------------------------------------------------------ scheduling internals

    def _active_count(self) -> int:
        with self._mutex:
            return sum(1 for h in self.active.values() if not h.abandoned)

    def _ram_ok(self, now: datetime | None = None) -> bool:
        now = now or self.clock()
        used = self.ram_probe()
        blocked = used >= self.ram_limit
        if blocked != self._ram_blocked:
            self._ram_blocked = blocked
            self._ram_since = now if blocked else None
            events.append(self.con, type="engine.ram_guard",
                          summary=("Memory high: not starting new work" if blocked else "Memory back to normal"),
                          payload={"used": round(used, 3), "limit": self.ram_limit}, **SYSTEM)
        long_enough = blocked and self._ram_since is not None and (now - self._ram_since).total_seconds() >= 60
        notify.condition(self.con, "ram", long_enough, category="system", severity="warn", title="Memory has been high for a while",
                         detail=f"{used:.0%} of memory is in use, so new work is not being started.", link="/attention")
        return not blocked

    def _usage_paused(self, now: datetime) -> bool:
        until = notify.get_setting(self.con, "usage_pause_until")
        return bool(until) and iso(now) < until

    def _housekeeping(self, now: datetime) -> None:
        """Periodic chores: notifications, deliveries, usage-limit resume, disk watch."""
        import time as _time
        mono = _time.monotonic()
        if mono - self._last_housekeeping < self.housekeeping_every:
            return
        self._last_housekeeping = mono
        level, _ = attention.disk_level(self.home, self.disk_probe)
        self._disk_critical = level == "critical"
        attention.auto_resume(self.con, now)
        reap = getattr(self.runner, "reap_browsers", None)
        if reap:
            reap(self.con)
        for row in self.con.execute("SELECT task_id FROM takeover WHERE status = 'requested'").fetchall():
            takeover.settle(self.con, self.home, row["task_id"])       # e.g. the engine restarted while an interrupt was pending
        if notify.get_setting(self.con, "usage_pause_until") and not self.con.execute(
                "SELECT 1 FROM block WHERE reason = 'USAGE_LIMIT' AND resolved_at IS NULL").fetchone():
            notify.set_setting(self.con, "usage_pause_until", None)
        attention.sync(self.con, self.home, self.disk_probe)
        if self.advisor:
            try:
                scheduler.run_due(self.con, self.home, now, self.notifier, self.tz)
            except Exception as err:
                self.report_error("advisor", err)
        if self._last_error_at and (now - self._last_error_at).total_seconds() > 600:
            notify.resolve_except(self.con, "error:", set())
            self._last_error_at = None
        if self.notifier is not None:
            self.notifier.pump(self.con)

    def report_error(self, where: str, err: BaseException) -> None:
        """Record an unexpected failure in the log and as a notification (deduplicated)."""
        self._last_error_at = self.clock()
        kind = type(err).__name__
        message = f"{where} failed: {kind}: {str(err)[:200]}"
        try:
            events.append(self.con, type="system.error", summary=message, payload={"where": where, "error": kind}, **SYSTEM)
            notify.upsert(self.con, key=f"error:{where}:{kind}", category="system", severity="critical",
                          title=f"Internal error in the engine ({where})", detail=f"{kind}: {str(err)[:200]}", link="/logs")
        except Exception:
            print(f"[engine] {message}", file=sys.stderr)

    def _runnable(self, now: datetime, limit: int) -> list[str]:
        rows = self.con.execute(
            "SELECT t.task_id FROM task t JOIN employee e ON e.emp_id = t.owner_emp_id "
            "WHERE t.status = 'assigned' AND t.deleted_at IS NULL AND t.lease_owner IS NULL AND t.interrupt_requested = 0 "
            "AND (t.not_before IS NULL OR t.not_before <= ?) "
            "AND e.status <> 'archived' AND e.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM task_dependency d JOIN task x ON x.task_id = d.depends_on "
            "                WHERE d.task_id = t.task_id AND x.status <> 'done') "
            "AND NOT EXISTS (SELECT 1 FROM task r WHERE r.owner_emp_id = t.owner_emp_id AND r.status = 'running') "
            "ORDER BY t.priority ASC, COALESCE(t.due_at, '9999') ASC, t.created_at ASC, t.task_id ASC",
            (iso(now),)).fetchall()
        # one task per employee at a time, even within a single pass
        chosen, owners = [], set()
        for row in rows:
            owner = self.con.execute("SELECT owner_emp_id FROM task WHERE task_id = ?", (row["task_id"],)).fetchone()[0]
            if owner in owners:
                continue
            owners.add(owner)
            chosen.append(row["task_id"])
            if len(chosen) >= limit:
                break
        return chosen

    def _start(self, task_id: str, now: datetime) -> bool:
        token = f"{self.instance}:{uuid.uuid4().hex[:8]}"
        with _tx(self.con):
            cur = self.con.execute(
                "UPDATE task SET status = 'running', lease_owner = ?, lease_expires_at = ?, "
                "attempts = attempts + 1, not_before = NULL, updated_at = ? "
                "WHERE task_id = ? AND status = 'assigned' AND lease_owner IS NULL AND interrupt_requested = 0 AND NOT EXISTS "
                "(SELECT 1 FROM task r WHERE r.owner_emp_id = task.owner_emp_id AND r.status = 'running')",
                (token, iso(now + timedelta(seconds=self.lease_seconds)), iso(now), task_id))
            if cur.rowcount != 1:
                return False
            row = self.con.execute("SELECT owner_emp_id, title, attempts FROM task WHERE task_id = ?",
                                   (task_id,)).fetchone()
            events.append_as(self.con, row["owner_emp_id"], type="task.running", task_id=task_id,
                             summary=f"Started {row['title']}",
                             payload={"from": "assigned", "to": "running", "attempt": row["attempts"]})
            tasks.set_employee_status(self.con, row["owner_emp_id"], "working")
        handle = Handle(task_id, token, row["owner_emp_id"], now, threading.Event())
        with self._mutex:
            self.active[task_id] = handle
        if self.threaded:
            handle.thread = threading.Thread(target=self._execute, args=(handle,), daemon=True,
                                             name=f"worker-{task_id}")
            handle.thread.start()
        else:
            self._execute(handle)
        return True

    def _extend_leases(self, now: datetime) -> None:
        expires = iso(now + timedelta(seconds=self.lease_seconds))
        with self._mutex:
            live = [(h.task_id, h.token) for h in self.active.values() if not h.abandoned]
        for task_id, token in live:
            self.con.execute("UPDATE task SET lease_expires_at = ? WHERE task_id = ? AND lease_owner = ?",
                             (expires, task_id, token))

    def _deliver_cancellations(self) -> None:
        """Pass a Founder's cancel request on to the worker that is running the task."""
        rows = self.con.execute("SELECT task_id FROM task WHERE status = 'running' AND (cancel_requested = 1 OR interrupt_requested = 1)").fetchall()
        with self._mutex:
            for row in rows:
                handle = self.active.get(row["task_id"])
                if handle is not None:
                    handle.cancel.set()

    def _enforce_runtime(self, now: datetime) -> None:
        with self._mutex:
            handles = [h for h in self.active.values() if not h.abandoned and not h.timed_out]
        for handle in handles:
            if (now - handle.started).total_seconds() <= self.max_runtime:
                continue
            handle.timed_out = True
            handle.cancel.set()
            with _tx(self.con):
                title = self.con.execute("SELECT title FROM task WHERE task_id = ?", (handle.task_id,)).fetchone()[0]
                moved = tasks.transition(
                    self.con, handle.task_id, "blocked", actor=SYSTEM, lease=handle.token,
                    summary=f"{title} exceeded its maximum run time",
                    payload={"reason": "TIMEOUT", "max_runtime": self.max_runtime},
                    sets={"lease_owner": None, "lease_expires_at": None})
                if moved:
                    tasks.open_block(self.con, handle.task_id, "TIMEOUT",
                                     f"ran longer than {self.max_runtime:.0f}s")
            if moved:
                handle.abandoned = True
                tasks.free_owner(self.con, handle.emp_id)

    def _reap_expired(self, now: datetime) -> None:
        """Running tasks whose lease expired and that no local worker owns: their engine died."""
        with self._mutex:
            mine = {h.token for h in self.active.values()}
        rows = self.con.execute(
            "SELECT task_id, lease_owner FROM task WHERE status = 'running' AND lease_expires_at < ?",
            (iso(now),)).fetchall()
        orphans = [r["task_id"] for r in rows if r["lease_owner"] not in mine]
        for task_id in orphans:
            self._recover_task(task_id, "lease expired (the worker or its engine stopped)")

    def _recover_running(self, reason: str) -> list[str]:
        rows = self.con.execute("SELECT task_id FROM task WHERE status = 'running'").fetchall()
        return [r["task_id"] for r in rows if self._recover_task(r["task_id"], reason)]

    def _recover_task(self, task_id: str, reason: str) -> bool:
        with _tx(self.con):
            row = self.con.execute("SELECT title, owner_emp_id, recoveries, checkpoint, status FROM task "
                                   "WHERE task_id = ?", (task_id,)).fetchone()
            if row is None or row["status"] != "running":
                return False
            count = row["recoveries"] + 1
            free = {"lease_owner": None, "lease_expires_at": None, "recoveries": count}
            if count > tasks.MAX_RECOVERIES:
                tasks.open_block(self.con, task_id, "SYSTEM_ERROR",
                                 f"the task was running during {count} engine interruptions")
                tasks.transition(self.con, task_id, "blocked", actor=SYSTEM, sets=free,
                                 summary=f"{row['title']} keeps being interrupted: needs a human",
                                 payload={"reason": reason, "recoveries": count})
            else:
                tasks.transition(self.con, task_id, "assigned", actor=SYSTEM, sets=free,
                                 event_type="task.recovered",
                                 summary=f"Recovered {row['title']}: {reason}",
                                 payload={"reason": reason, "recoveries": count, "checkpoint": row["checkpoint"]})
        tasks.free_owner(self.con, row["owner_emp_id"])
        return True

    def _apply_kill(self, now: datetime) -> None:
        with self._mutex:
            handles = [h for h in self.active.values() if not h.abandoned]
        for handle in handles:
            handle.cancel.set()
        if self._kill_since is None:
            self._kill_since = now
            return
        if (now - self._kill_since).total_seconds() < self.kill_grace:
            return
        for handle in handles:  # workers that ignore the stop request are forcibly detached
            if handle.thread is not None and handle.thread.is_alive():
                if self._requeue_stopped(self.con, handle, "kill switch: worker did not stop in time", forced=True):
                    handle.abandoned = True

    def _write_status(self, now: datetime) -> None:
        self.con.execute(
            "INSERT INTO engine_status (id, instance, pid, started_at, heartbeat_at, cap, running, mode) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET instance = excluded.instance, "
            "pid = excluded.pid, started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at, "
            "cap = excluded.cap, running = excluded.running, mode = excluded.mode",
            (self.instance, os.getpid(), iso(self.started_at or now), iso(now), self.cap, self._active_count(),
             "simulated" if self.runner.simulated else "live"))

    # ------------------------------------------------------------ worker side

    def _execute(self, handle: Handle) -> None:
        con = connect(db_path(self.home), cross_thread=True)     # a runner may report from its own helper threads
        guard = threading.Lock()

        def locked(fn):
            def call(*args, **kwargs):
                with guard:
                    return fn(*args, **kwargs)
            return call

        try:
            task = tasks.get_task(con, handle.task_id)
            emp = org.get_employee(con, handle.emp_id)
            ctx = RunContext(
                task=task, employee=emp, attempt=task["attempts"], checkpoint=task["checkpoint"],
                cancel=handle.cancel,
                progress=locked(lambda pct, msg: self._progress(con, handle, pct, msg)),
                save_checkpoint=locked(lambda text: self._save_checkpoint(con, handle, text)),
                inputs=tasks.dependency_results(con, handle.task_id),
                emit=locked(lambda kind, summary, payload=None: self._emit(con, handle, kind, summary, payload)),
                record_usage=locked(lambda model, tokens: self._usage(con, handle, model, tokens)),
                record_prompt=locked(lambda model, sent, baseline: self._prompt(con, handle, model, sent, baseline)),
                skill_filter=locked(lambda name: skills.usable(self.home, con, name, emp)[0]),
                limits=limits.resolve(con, task, emp, 1 if task["risk_tier"] == 3 else task["risk_tier"]),
                history_events=con.execute("SELECT count(*) FROM event WHERE task_id = ?", (handle.task_id,)).fetchone()[0])
            try:
                result = self.runner.run(ctx)
            except Exception as exc:  # a runner must never take the engine down
                result = RunResult("failed", f"runner crashed: {exc}", detail=f"{type(exc).__name__}: {exc}")
            self._finish(con, handle, task, result)
        except Exception as exc:
            print(f"[engine] worker for {handle.task_id} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.report_error(f"worker {handle.task_id}", exc)
        finally:
            con.close()
            with self._mutex:
                if self.active.get(handle.task_id) is handle:
                    del self.active[handle.task_id]

    def _progress(self, con, handle: Handle, percent: int, message: str) -> None:
        if not handle.abandoned:
            events.append_as(con, handle.emp_id, type="task.progress", task_id=handle.task_id,
                             summary=f"{message} ({percent}%)", payload={"percent": percent})

    def _emit(self, con, handle: Handle, kind: str, summary: str, payload: dict | None) -> None:
        if not handle.abandoned:
            events.append_as(con, handle.emp_id, type=kind, summary=summary, task_id=handle.task_id, payload=payload)

    def _prompt(self, con, handle: Handle, model: str, sent: int, baseline: int) -> None:
        con.execute("INSERT INTO usage (ts, emp_id, model, runs, est_tokens, task_id, prompt_chars, baseline_chars) VALUES (?, ?, ?, 1, 0, ?, ?, ?)",
                    (iso(self.clock()), handle.emp_id, model, handle.task_id, sent, baseline))

    def _usage(self, con, handle: Handle, model: str, tokens: int) -> None:
        """Tokens reported by the runner go on this run's row (created by ``_prompt``) so a run counts once."""
        row = con.execute("SELECT usage_id FROM usage WHERE task_id = ? AND est_tokens = 0 ORDER BY usage_id DESC LIMIT 1", (handle.task_id,)).fetchone()
        if row:
            con.execute("UPDATE usage SET est_tokens = ?, model = ? WHERE usage_id = ?", (tokens, model, row["usage_id"]))
            return
        con.execute("INSERT INTO usage (ts, emp_id, model, runs, est_tokens, task_id) VALUES (?, ?, ?, 1, ?, ?)",
                    (iso(self.clock()), handle.emp_id, model, tokens, handle.task_id))

    def _save_checkpoint(self, con, handle: Handle, text: str) -> None:
        con.execute("UPDATE task SET checkpoint = ? WHERE task_id = ? AND lease_owner = ?",
                    (events.scrub(text), handle.task_id, handle.token))

    def _actor(self, con, emp_id: str) -> dict:
        who = events.identity(con, emp_id)
        return {"actor_type": "employee", "actor_id": who["id"], "actor_name": who["name"],
                "actor_designation": who["designation"]}

    def _requeue_stopped(self, con, handle: Handle, reason: str, forced: bool = False,
                         checkpoint: str | None = None) -> bool:
        sets = {"lease_owner": None, "lease_expires_at": None}
        if checkpoint is not None:
            sets["checkpoint"] = checkpoint
        with _tx(con):
            title = con.execute("SELECT title FROM task WHERE task_id = ?", (handle.task_id,)).fetchone()[0]
            moved = tasks.transition(con, handle.task_id, "assigned", actor=SYSTEM, lease=handle.token,
                                     event_type="task.stopped", summary=f"Stopped {title}: {reason}",
                                     payload={"reason": reason, "forced": forced}, sets=sets)
            if moved:  # a stop is not a failure: give the attempt back
                con.execute("UPDATE task SET attempts = MAX(attempts - 1, 0) WHERE task_id = ?", (handle.task_id,))
        if moved:
            tasks.free_owner(con, handle.emp_id)
        return moved

    def _finish(self, con, handle: Handle, task: dict, result: RunResult) -> None:
        tid, token = handle.task_id, handle.token
        actor = self._actor(con, handle.emp_id)
        free = {"lease_owner": None, "lease_expires_at": None}
        title = task["title"]
        result.summary = events.scrub(result.summary)       # whatever a runner reports is scrubbed before it is stored
        result.detail = events.scrub(result.detail) if result.detail else result.detail
        result.checkpoint = events.scrub(result.checkpoint) if result.checkpoint else result.checkpoint
        flags = con.execute("SELECT cancel_requested, interrupt_requested FROM task WHERE task_id = ?", (tid,)).fetchone()
        cancel_requested, interrupted = bool(flags[0]), bool(flags[1])
        moved = False
        with _tx(con):
            if result.outcome == "stopped":
                if cancel_requested:
                    moved = tasks.transition(con, tid, "rejected", actor=actor, lease=token, sets=free,
                                             event_type="task.cancelled", summary=f"Cancelled {title}",
                                             payload={"by": "Founder request"})
                    if moved:
                        tasks.fail_dependents(con, tid, "rejected")
                else:
                    why = "interrupted by the Founder" if interrupted else "stopped by the engine"
                    moved = self._requeue_stopped(con, handle, why, checkpoint=result.checkpoint)
            elif result.outcome == "done":
                moved = tasks.transition(con, tid, "done", actor=actor, lease=token, summary=f"Finished {title}",
                                         payload={"result": result.summary},
                                         sets={**free, "result_summary": result.summary, "checkpoint": None})
                for art in result.artifacts if moved else []:
                    con.execute("INSERT INTO artifact (artifact_id, task_id, path, sha256, created_at) VALUES (?,?,?,?,?)",
                                (f"A-{uuid.uuid4().hex[:8]}", tid, art["path"], art.get("sha256", ""), iso(self.clock())))
            elif result.outcome == "review":
                moved = tasks.transition(con, tid, "review", actor=actor, lease=token,
                                         summary=f"{title} is ready for review", sets={**free, "result_summary": result.summary})
            elif result.outcome == "awaiting_approval":
                moved = tasks.transition(con, tid, "awaiting_approval", actor=actor, lease=token,
                                         summary=f"{title} needs the Founder's approval",
                                         sets={**free, "result_summary": result.summary})
                if moved:
                    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, ?, ?)",
                                (tid, task["risk_tier"], iso(self.clock())))
            elif result.outcome == "blocked":
                reason = result.block_reason if result.block_reason in tasks.BLOCK_REASONS else "RUNNER_ERROR"
                moved = tasks.transition(con, tid, "blocked", actor=actor, lease=token,
                                         summary=f"{title} is blocked: {reason}",
                                         payload={"reason": reason, "detail": result.detail},
                                         sets={**free, "checkpoint": result.checkpoint or task["checkpoint"]})
                if moved:
                    block_id = tasks.open_block(con, tid, reason, result.detail)
                    if reason == "USAGE_LIMIT":
                        resume = attention.usage_resume_time(con, tid, result.detail, self.clock())
                        con.execute("UPDATE block SET resume_at = ? WHERE block_id = ?", (iso(resume), block_id))
                        current = notify.get_setting(con, "usage_pause_until")
                        notify.set_setting(con, "usage_pause_until", max(filter(None, [current, iso(resume)])))
                        events.append(con, type="engine.usage_pause", summary=f"Usage limit: no new work until {iso(resume)[11:16]} UTC",
                                      payload={"resume_at": iso(resume)}, **SYSTEM)
            else:  # failed
                attempts = con.execute("SELECT attempts FROM task WHERE task_id = ?", (tid,)).fetchone()[0]
                if attempts <= tasks.MAX_RETRIES:
                    delay = self.base_backoff * (2 ** (attempts - 1))
                    retry_at = iso(self.clock() + timedelta(seconds=delay))
                    moved = tasks.transition(
                        con, tid, "assigned", actor=actor, lease=token, event_type="task.retry_scheduled",
                        summary=f"{title} failed (attempt {attempts}); retrying in {delay:g}s",
                        payload={"attempt": attempts, "retry_at": retry_at, "error": result.detail},
                        sets={**free, "not_before": retry_at})
                else:
                    moved = tasks.transition(
                        con, tid, "failed", actor=actor, lease=token,
                        summary=f"{title} failed after {attempts} attempts",
                        payload={"attempts": attempts, "error": result.detail}, sets=free)
                    if moved:
                        tasks.fail_dependents(con, tid, "failed")
            if not moved:
                events.append(con, type="task.late_result_ignored", task_id=tid,
                              summary=f"Ignored a late result for {title} (lease no longer held)",
                              payload={"outcome": result.outcome}, **SYSTEM)
        if moved:
            tasks.free_owner(con, handle.emp_id)
        if interrupted:                                      # the Founder asked for a take-over: hold the task (or note the agent won)
            takeover.settle(con, self.home, tid, result.checkpoint if result.outcome == "stopped" else None)


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicorp-engine", description="AICorp task engine")
    parser.add_argument("--runner", choices=["fake", "claude"],
                        help="worker backend: 'claude' = real Claude Code (uses your Claude plan), 'fake' = simulated")
    parser.add_argument("--browser", choices=["on", "off"], help="also run browser tasks (employees with the 'browser' tool) with Playwright; default: on for --runner claude")
    parser.add_argument("--cap", type=int, default=int(os.environ.get("AICORP_CAP", "4")))
    parser.add_argument("--ram-limit", type=float, default=0.80)
    parser.add_argument("--lease", type=float, default=60.0)
    parser.add_argument("--max-runtime", type=float, default=1800.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--fake-duration", default="0.5,2.0", help="min,max seconds for simulated work")
    parser.add_argument("--exit-when-idle", action="store_true")
    parser.add_argument("--alerts", choices=["on", "off"], help="send Telegram/e-mail alerts from the engine "
                        "(default: on for --runner claude, off for the simulated runner)")
    args = parser.parse_args(argv)
    if args.runner is None:
        print("Choose a worker backend:\n  --runner claude   real work with Claude Code (uses your Claude plan quota)\n"
              "  --runner fake     simulated work, for trying out the dashboard", file=sys.stderr)
        return 2
    if args.runner == "claude":
        from .claude_runner import ClaudeCodeRunner, RunnerConfig, RunnerConfigError

        try:
            runner = ClaudeCodeRunner(RunnerConfig.from_env(default_home()))
        except RunnerConfigError as err:
            print(err, file=sys.stderr)
            return 2
        who = runner.cfg.worker_user or "the same user as the engine (weaker isolation)"
        print(f"LIVE MODE: tasks run real Claude Code as {who}. This uses your Claude plan quota.")
    else:
        low, high = (float(x) for x in args.fake_duration.split(","))
        runner = FakeRunner(duration=(low, high), seed=None)
        print("DEMO MODE: tasks are SIMULATED. Nothing real is being done. Do not use for real work.")
    if (args.browser or ("on" if args.runner == "claude" else "off")) == "on":
        from .browser_runner import BrowserConfig, BrowserRunner, RoutingRunner

        runner = RoutingRunner(runner, BrowserRunner(BrowserConfig(home=default_home())))
        print("Browser worker on: employees with the 'browser' tool use a dedicated Chromium profile (at most 2 windows).")
    notifier = None
    if (args.alerts or ("on" if args.runner == "claude" else "off")) == "on":
        from .channels import load_transports
        from .delivery import Notifier

        notifier = Notifier(load_transports(default_home()), scrub=redact.Redactor(redact.load_secret_values(default_home())).scrub)
    from .config import ensure_layout
    ensure_layout(default_home())
    engine = Orchestrator(default_home(), runner, cap=args.cap, ram_limit=args.ram_limit, lease_seconds=args.lease,
                          max_runtime=args.max_runtime, base_backoff=args.backoff, notifier=notifier, advisor=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        engine.run_forever(poll=args.poll, exit_when_idle=args.exit_when_idle, stop=stop)
    except EngineAlreadyRunning as err:
        print(err, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

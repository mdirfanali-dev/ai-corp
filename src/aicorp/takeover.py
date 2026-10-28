"""Interrupt and take over: the Founder stops an agent mid-task and works in the task himself.

Flow: ``start`` -> (a running agent is asked to stop; the engine settles it) -> the task is *held*
(blocked, "taken over") with a snapshot of its context -> the Founder adds notes and files -> ``end``:
``resume`` (the agent continues from its checkpoint plus the Founder's notes), ``complete`` (the Founder
writes the result and the task is done) or ``cancel``.

Every step is recorded with ``mode = interrupt`` under the Founder's real identity and the employee's
name and role (``acting_as``). Files the Founder writes into the employee's workspace go through the same
path rules as the agents (``policy.check_path``): protected files and anything outside the workspace are
refused, whatever permissions or override the Founder has.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

from . import events, policy, tasks, wsfile
from .config import utc_now
from .org import FOUNDER, SYSTEM, ValidationError, authorize

HELD = "Taken over by the Founder"
MAX_NOTE = 4000
MAX_FILE = policy.MAX_WRITE_BYTES


def _identity(con, emp_id: str) -> dict:
    who = events.identity(con, emp_id)
    return {"acting_as_emp_id": who["id"], "acting_as_name": who["name"], "acting_as_role": who["designation"]}


def _log(con, emp_id: str, actor: dict, type: str, summary: str, task_id: str, payload: dict | None = None) -> None:
    events.append(con, type=type, summary=summary, task_id=task_id, mode="interrupt", payload=payload or {},
                  **_identity(con, emp_id), **{k: actor.get(k) for k in ("actor_type", "actor_id", "actor_name", "actor_designation")})


def get(con, takeover_id: str) -> dict | None:
    row = con.execute("SELECT * FROM takeover WHERE takeover_id = ?", (takeover_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["snapshot"] = json.loads(row["snapshot"]) if row["snapshot"] else None
    out["notes"] = [dict(r) for r in con.execute("SELECT * FROM takeover_note WHERE takeover_id = ? ORDER BY note_id", (takeover_id,))]
    return out


def open_for_task(con, task_id: str) -> dict | None:
    row = con.execute("SELECT takeover_id FROM takeover WHERE task_id = ? AND status <> 'ended'", (task_id,)).fetchone()
    return get(con, row["takeover_id"]) if row else None


def workspace_of(con, home: Path, emp_id: str) -> Path:
    row = con.execute("SELECT workspace FROM employee WHERE emp_id = ?", (emp_id,)).fetchone()
    return home / row["workspace"]


def _listing(root: Path, limit: int = 60) -> list[str]:
    out = []
    try:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                out.append(path.relative_to(root).as_posix())
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return out


def _snapshot(con, home: Path, task_id: str, emp_id: str, checkpoint: str | None) -> dict:
    task = tasks.get_task(con, task_id)
    last = [dict(r) for r in con.execute("SELECT ts, type, summary FROM event WHERE task_id = ? ORDER BY id DESC LIMIT 15", (task_id,))]
    return events.scrub({"taken_at": utc_now(), "title": task["title"], "description": task["description"], "status": task["status"],
                         "attempts": task["attempts"], "checkpoint": checkpoint if checkpoint is not None else task["checkpoint"],
                         "recent_events": last, "workspace_files": _listing(workspace_of(con, home, emp_id))})


# ---------------------------------------------------------------- starting

def start(con: sqlite3.Connection, home: Path, task_id: str, actor: dict = FOUNDER) -> str:
    """Interrupt the agent working on this task. Returns the take-over id."""
    authorize(con, actor, founder_only=True)
    task = tasks.get_task(con, task_id)
    if task is None or task["deleted_at"]:
        raise ValidationError("Task not found")
    if open_for_task(con, task_id):
        raise ValidationError("This task is already taken over")
    if task["status"] not in ("running", "assigned", "blocked"):
        raise ValidationError(f"A {task['status'].replace('_', ' ')} task cannot be taken over (only running, waiting or blocked ones)")
    takeover_id = f"TO-{uuid.uuid4().hex[:8]}"
    emp_id = task["owner_emp_id"]
    now = utc_now()
    with tasks._tx(con):
        con.execute("INSERT INTO takeover (takeover_id, task_id, emp_id, status, requested_at) VALUES (?,?,?, 'requested', ?)",
                    (takeover_id, task_id, emp_id, now))
        if task["status"] == "running":
            con.execute("UPDATE task SET interrupt_requested = 1 WHERE task_id = ?", (task_id,))
            _log(con, emp_id, actor, "takeover.requested", f"Interrupt requested: stopping the agent on {task['title']}", task_id,
                 {"takeover_id": takeover_id})
        else:
            _hold(con, home, takeover_id, task_id, emp_id, actor)
    return takeover_id


def _hold(con, home: Path, takeover_id: str, task_id: str, emp_id: str, actor: dict, checkpoint: str | None = None) -> None:
    """The agent is no longer working on the task: park it and record the context snapshot."""
    task = tasks.get_task(con, task_id)
    snapshot = _snapshot(con, home, task_id, emp_id, checkpoint)
    if task["status"] != "blocked":
        tasks.open_block(con, task_id, "NEEDS_INPUT", HELD)
        tasks.transition(con, task_id, "blocked", actor=SYSTEM, event_type="task.held", summary=f"{task['title']} is held: {HELD}",
                         payload={"takeover_id": takeover_id},
                         sets={"lease_owner": None, "lease_expires_at": None, "interrupt_requested": 0,
                               **({"checkpoint": checkpoint} if checkpoint is not None else {})})
    else:
        con.execute("UPDATE task SET interrupt_requested = 0 WHERE task_id = ?", (task_id,))
    con.execute("UPDATE takeover SET status = 'active', started_at = ?, snapshot = ? WHERE takeover_id = ?",
                (utc_now(), json.dumps(snapshot), takeover_id))
    tasks.free_owner(con, emp_id)
    _log(con, emp_id, actor, "takeover.started", f"The Founder took over {task['title']} (agent stopped, context saved)", task_id,
         {"takeover_id": takeover_id, "files": len(snapshot["workspace_files"])})


def settle(con: sqlite3.Connection, home: Path, task_id: str, checkpoint: str | None = None) -> bool:
    """Called by the engine once the agent has stopped (or finished first). Returns True if a hold began."""
    row = con.execute("SELECT * FROM takeover WHERE task_id = ? AND status = 'requested'", (task_id,)).fetchone()
    if row is None:
        return False
    task = tasks.get_task(con, task_id)
    if task["status"] in ("assigned", "blocked"):
        _hold(con, home, row["takeover_id"], task_id, row["emp_id"], FOUNDER, checkpoint)
        return True
    if task["status"] == "running":
        return False                                   # still stopping
    con.execute("UPDATE task SET interrupt_requested = 0 WHERE task_id = ?", (task_id,))
    con.execute("UPDATE takeover SET status = 'ended', ended_at = ?, end_action = 'abandoned' WHERE takeover_id = ?", (utc_now(), row["takeover_id"]))
    _log(con, row["emp_id"], SYSTEM, "takeover.abandoned", f"The agent finished {task['title']} before it could be interrupted ({task['status']})", task_id)
    return False


# ---------------------------------------------------------------- working in the task

def _active(con, takeover_id: str) -> dict:
    row = get(con, takeover_id)
    if row is None or row["status"] != "active":
        raise ValidationError("This take-over is not active (the agent may still be stopping, or it has ended)")
    return row


def add_note(con, takeover_id: str, text: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    row = _active(con, takeover_id)
    text = events.scrub((text or "").strip())[:MAX_NOTE]
    if not text:
        raise ValidationError("Write something first")
    con.execute("INSERT INTO takeover_note (takeover_id, ts, kind, text) VALUES (?,?, 'note', ?)", (takeover_id, utc_now(), text))
    _log(con, row["emp_id"], actor, "takeover.note", f"Founder note while working as {row['snapshot']['title'] if row['snapshot'] else 'the agent'}: {text[:120]}",
         row["task_id"], {"takeover_id": takeover_id})


def write_file(con, home: Path, takeover_id: str, relpath: str, content: str, actor: dict = FOUNDER) -> str:
    """Write a file into the employee's workspace, under the agents' path rules. Returns the relative path."""
    authorize(con, actor, founder_only=True)
    row = _active(con, takeover_id)
    workspace = workspace_of(con, home, row["emp_id"])
    pol = policy.Policy(workspace=str(workspace), deny_roots=[str(home / d) for d in ("config", "database", "backups", "company", ".trash", "runtime", "memory", "workspaces")])
    relpath = (relpath or "").strip()
    decision = policy.check_path(pol, relpath, write=True)
    if not decision.allow:
        events.append(con, type="takeover.file_refused", summary=f"File write refused: {decision.reason}", task_id=row["task_id"], mode="interrupt",
                      payload={"path": relpath[:200], "rule": decision.rule}, **_identity(con, row["emp_id"]),
                      **{k: actor.get(k) for k in ("actor_type", "actor_id", "actor_name", "actor_designation")})
        raise PermissionError(decision.message())
    data = events.scrub(content).encode("utf-8")
    if len(data) > MAX_FILE:
        raise ValidationError("That file is too large")
    target = Path(os.path.realpath(workspace / relpath))
    try:
        wsfile.write(target, data)
    except wsfile.WorkspaceWriteError as err:
        raise ValidationError(str(err)) from None
    digest = hashlib.sha256(data).hexdigest()
    con.execute("INSERT INTO artifact (artifact_id, task_id, path, sha256, created_at) VALUES (?,?,?,?,?)",
                (f"A-{uuid.uuid4().hex[:8]}", row["task_id"], target.relative_to(os.path.realpath(home)).as_posix(), digest, utc_now()))
    con.execute("INSERT INTO takeover_note (takeover_id, ts, kind, text) VALUES (?,?, 'file', ?)", (takeover_id, utc_now(), relpath))
    _log(con, row["emp_id"], actor, "takeover.file_written", f"Founder wrote {relpath} ({len(data)} bytes) into the workspace", row["task_id"],
         {"takeover_id": takeover_id, "path": relpath, "sha256": digest})
    return relpath


# ---------------------------------------------------------------- ending

def _handover_text(row: dict) -> str:
    notes = [n["text"] for n in row["notes"] if n["kind"] == "note"]
    files = [n["text"] for n in row["notes"] if n["kind"] == "file"]
    parts = ["The Founder took this task over and worked on it himself."]
    if notes:
        parts.append("His notes: " + " | ".join(notes))
    if files:
        parts.append("Files he wrote into your workspace: " + ", ".join(files))
    previous = (row["snapshot"] or {}).get("checkpoint")
    if previous:
        parts.append("Your earlier checkpoint: " + str(previous))
    return events.scrub("\n".join(parts))[:6000]


def end(con: sqlite3.Connection, takeover_id: str, action: str, actor: dict = FOUNDER, result: str = "") -> None:
    """Finish a take-over: ``resume`` (agent continues), ``complete`` (Founder's result, task done) or ``cancel``."""
    authorize(con, actor, founder_only=True)
    if action not in ("resume", "complete", "cancel"):
        raise ValidationError("Choose resume, complete or cancel")
    row = get(con, takeover_id)
    if row is None or row["status"] == "ended":
        raise ValidationError("This take-over has already ended")
    if row["status"] != "active":
        raise ValidationError("The agent is still stopping - try again in a moment")
    task = tasks.get_task(con, row["task_id"])
    emp_id, task_id = row["emp_id"], row["task_id"]
    with tasks._tx(con):
        if action == "resume":
            con.execute("UPDATE block SET resolved_at = ?, resolved_by = ? WHERE task_id = ? AND resolved_at IS NULL", (utc_now(), actor["actor_name"], task_id))
            tasks.transition(con, task_id, "assigned", actor=actor, event_type="task.block_resolved", summary=f"Agent resumes {task['title']} after the take-over",
                             sets={"lease_owner": None, "lease_expires_at": None, "not_before": None, "checkpoint": _handover_text(row)})
        elif action == "complete":
            result = events.scrub((result or "").strip())[:MAX_NOTE]
            if not result:
                raise ValidationError("Write the result first")
            con.execute("UPDATE block SET resolved_at = ?, resolved_by = ? WHERE task_id = ? AND resolved_at IS NULL", (utc_now(), actor["actor_name"], task_id))
            tasks.transition(con, task_id, "done", actor=actor, event_type="task.completed_by_founder", summary=f"The Founder completed {task['title']} himself",
                             payload={"result": result}, sets={"lease_owner": None, "lease_expires_at": None, "result_summary": result, "checkpoint": None})
        else:
            con.execute("UPDATE block SET resolved_at = ?, resolved_by = ? WHERE task_id = ? AND resolved_at IS NULL", (utc_now(), actor["actor_name"], task_id))
            tasks.transition(con, task_id, "rejected", actor=actor, event_type="task.cancelled", summary=f"Cancelled {task['title']} after the take-over",
                             sets={"lease_owner": None, "lease_expires_at": None})
            tasks.fail_dependents(con, task_id, "rejected")
        con.execute("UPDATE takeover SET status = 'ended', ended_at = ?, end_action = ? WHERE takeover_id = ?", (utc_now(), action, takeover_id))
        _log(con, emp_id, actor, "takeover.ended", f"Take-over of {task['title']} ended: {action}", task_id, {"takeover_id": takeover_id, "action": action})
    tasks.free_owner(con, emp_id)

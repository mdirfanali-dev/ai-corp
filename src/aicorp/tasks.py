"""Task lifecycle: creation, delegation rules, dependencies, state machine, blocks, approvals.

Every state change is validated here and written to the audit log in the same transaction,
so the task table and the event log can never disagree.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from . import browserplan, events, inputguard, org
from .config import utc_now
from .org import FOUNDER, SYSTEM, ValidationError, _tx

MAX_DEPTH = 3
MAX_RETRIES = 2          # retries after the first attempt, so at most 3 runs
MAX_RECOVERIES = 3       # engine crashes a task may survive before it needs a human
TERMINAL = ("done", "rejected")
STATUSES = ("created", "assigned", "running", "blocked", "review", "awaiting_approval",
            "done", "rejected", "failed")
TRANSITIONS: dict[str, set[str]] = {
    "created": {"assigned", "rejected"},
    "assigned": {"running", "blocked", "rejected"},
    "running": {"assigned", "blocked", "review", "awaiting_approval", "done", "failed", "rejected"},
    "blocked": {"assigned", "failed", "rejected", "done"},       # done: the Founder finished it after taking over
    "review": {"assigned", "awaiting_approval", "done", "rejected"},
    "awaiting_approval": {"done", "rejected", "assigned"},
    "failed": {"assigned", "rejected"},
    "done": set(),
    "rejected": set(),
}
BLOCK_REASONS = (
    "USAGE_LIMIT", "CAPTCHA", "LOGIN_REQUIRED", "UNKNOWN_UI", "RUNNER_ERROR", "PERMISSION_DENIED",
    "NEEDS_APPROVAL", "NEEDS_INPUT", "TIMEOUT", "RAM_PRESSURE", "DISK_LOW", "SYSTEM_ERROR",
)
DEPENDENCY_PREFIX = "dependency:"


class InvalidTransition(ValidationError):
    pass


def new_task_id() -> str:
    return f"T-{uuid.uuid4().hex[:8]}"


def parse_when(value: Any) -> str | None:
    return org.parse_expiry(value)


# ---------------------------------------------------------------- reading

def get_task(con: sqlite3.Connection, task_id: str) -> dict | None:
    row = con.execute(
        "SELECT t.*, e.name AS owner_name FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
        "WHERE t.task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    task["depends_on"] = [dict(r) for r in con.execute(
        "SELECT t.task_id, t.title, t.status FROM task_dependency d JOIN task t ON t.task_id = d.depends_on "
        "WHERE d.task_id = ? ORDER BY t.title", (task_id,))]
    task["dependents"] = [dict(r) for r in con.execute(
        "SELECT t.task_id, t.title, t.status FROM task_dependency d JOIN task t ON t.task_id = d.task_id "
        "WHERE d.depends_on = ? ORDER BY t.title", (task_id,))]
    task["children"] = [dict(r) for r in con.execute(
        "SELECT task_id, title, status FROM task WHERE parent_id = ? ORDER BY created_at", (task_id,))]
    block = con.execute("SELECT * FROM block WHERE task_id = ? AND resolved_at IS NULL ORDER BY block_id DESC",
                        (task_id,)).fetchone()
    task["block"] = dict(block) if block else None
    approval = con.execute("SELECT * FROM approval WHERE task_id = ? AND decided_at IS NULL "
                           "ORDER BY approval_id DESC", (task_id,)).fetchone()
    task["approval"] = dict(approval) if approval else None
    return task


def list_tasks(con: sqlite3.Connection, *, status: str | None = None, owner: str | None = None,
               q: str | None = None, limit: int = 200) -> list[dict]:
    sql = ("SELECT t.*, e.name AS owner_name, "
           "(SELECT count(*) FROM task_dependency d JOIN task x ON x.task_id = d.depends_on "
           " WHERE d.task_id = t.task_id AND x.status <> 'done') AS open_deps "
           "FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.deleted_at IS NULL")
    args: list[Any] = []
    if status:
        sql += " AND t.status = ?"
        args.append(status)
    if owner:
        sql += " AND t.owner_emp_id = ?"
        args.append(owner)
    if q:
        sql += " AND t.title LIKE ? ESCAPE '\\'"
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql += " ORDER BY t.updated_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args)]


def dependency_results(con: sqlite3.Connection, task_id: str) -> list[dict]:
    """Short results of the finished tasks this task waited for (input for the agent)."""
    out = []
    for r in con.execute(
            "SELECT t.task_id, t.title, t.result_summary AS summary FROM task_dependency d JOIN task t ON t.task_id = d.depends_on "
            "WHERE d.task_id = ? AND t.status = 'done' AND t.result_summary IS NOT NULL ORDER BY t.title", (task_id,)):
        item = dict(r)
        item["artifacts"] = [dict(a) for a in con.execute("SELECT path FROM artifact WHERE task_id = ? ORDER BY created_at LIMIT 12", (item.pop("task_id"),))]
        out.append(item)
    return out


def timeline(con: sqlite3.Connection, task_id: str, limit: int = 100) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM event WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit))]


# ---------------------------------------------------------------- creation and rules

def _shares_team(con, a: str, b: str) -> bool:
    return con.execute(
        "SELECT 1 FROM team_member x JOIN team_member y ON x.team_id = y.team_id "
        "JOIN team t ON t.team_id = x.team_id AND t.deleted_at IS NULL "
        "WHERE x.emp_id = ? AND y.emp_id = ? LIMIT 1", (a, b)).fetchone() is not None


def _needs_delegation(con, creator: str, owner: str) -> bool:
    """Cross-team work must go through a delegation task. The CEO coordinates every team."""
    if creator == owner:
        return False
    row = con.execute("SELECT system_key FROM employee WHERE emp_id = ?", (creator,)).fetchone()
    if row and row["system_key"] == "ceo":
        return False
    if _shares_team(con, creator, owner):
        return False
    links = con.execute(
        "SELECT 1 FROM employee WHERE (emp_id = ? AND reports_to = ?) OR (emp_id = ? AND reports_to = ?)",
        (owner, creator, creator, owner)).fetchone()
    return links is None


def _check_can_take(con, owner_id: str, risk_tier: int) -> dict:
    emp = org.get_employee(con, owner_id)
    if emp is None or emp["deleted_at"]:
        raise ValidationError("Owner does not exist")
    if emp["status"] == "archived":
        raise ValidationError(f"{emp['name']} is archived and cannot take tasks")
    if risk_tier >= 1 and not emp["capabilities"]["execute"]:
        raise ValidationError(
            f"{emp['name']} cannot take tier-{risk_tier} work (no 'execute' permission); only read-only tier-0 tasks")
    return emp


def create_task(
    con: sqlite3.Connection, *, title: str, description: str = "", owner: str | None = None,
    parent_id: str | None = None, project_id: str | None = None, depends_on: list[str] | None = None,
    priority: int = 3, risk_tier: int = 1, due_at: str | None = None, actor: dict = FOUNDER,
    creator_emp_id: str | None = None, delegation: bool = False,
) -> str:
    """Create a task. ``priority`` 1 is most urgent. ``creator_emp_id`` is set when an employee
    (not the Founder) creates the task, which activates the cross-team delegation rule."""
    if (who := org.acting_employee(actor)) is not None and owner != who:
        org.authorize(con, actor, capability="assign")      # creating work for someone else is assigning
    title = org.clean_name(events.scrub(title), "Title")
    description = events.scrub(description)
    if (owner_row := con.execute("SELECT profile FROM employee WHERE emp_id = ?", (owner,)).fetchone()) and \
            "browser" in [t.lower() for t in json.loads(owner_row["profile"] or "{}").get("tools", [])]:
        try:
            browserplan.validate_for_task(description, risk_tier)
        except browserplan.PlanError as err:
            raise ValidationError(f"Browser task: {err}") from None
    verdict = inputguard.check(f"{title}\n{description}")
    if verdict.blocked:
        events.append(con, type="guard.input_blocked", summary=f"Refused a task: input guard ({verdict.describe()})",
                      payload={"rules": verdict.rules, "title": title[:80]}, **actor)
        raise ValidationError(
            f"Blocked by the input guard ({verdict.describe()}). Agents never receive text that asks them to break "
            f"the safety rules, reveal secrets or run destructive commands. Please rephrase the task.")
    if not 1 <= int(priority) <= 5:
        raise ValidationError("Priority must be 1 (urgent) to 5 (low)")
    if not 0 <= int(risk_tier) <= 4:
        raise ValidationError("Risk tier must be 0 to 4")
    due = parse_when(due_at)
    depends = org.as_list(depends_on)
    errors: list[str] = []
    depth, kind = 0, "work"
    if parent_id:
        parent = con.execute("SELECT depth, owner_emp_id FROM task WHERE task_id = ?", (parent_id,)).fetchone()
        if parent is None:
            errors.append("Parent task does not exist")
        else:
            depth = parent["depth"] + 1
            if depth > MAX_DEPTH:
                errors.append(f"Delegation depth is limited to {MAX_DEPTH} levels")
    for dep in depends:
        if con.execute("SELECT 1 FROM task WHERE task_id = ?", (dep,)).fetchone() is None:
            errors.append(f"Dependency {dep} does not exist")
    if project_id and con.execute("SELECT 1 FROM project WHERE project_id = ?", (project_id,)).fetchone() is None:
        errors.append("Project does not exist")
    if owner:
        try:
            _check_can_take(con, owner, int(risk_tier))
        except ValidationError as err:
            errors += err.errors
        if creator_emp_id and not errors and _needs_delegation(con, creator_emp_id, owner):
            if not delegation:
                errors.append("Cross-team assignment must be sent as a delegation request")
            elif not parent_id or parent["owner_emp_id"] != creator_emp_id:
                errors.append("A delegation must be a sub-task of a task owned by the requester")
            else:
                kind = "delegation"
    if errors:
        raise ValidationError(*errors)
    task_id = new_task_id()
    now = utc_now()
    with _tx(con):
        con.execute(
            "INSERT INTO task (task_id, parent_id, project_id, title, description, owner_emp_id, status, priority, "
            "risk_tier, due_at, created_by, created_at, updated_at, kind, depth) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, parent_id, project_id, title, description, owner, "assigned" if owner else "created",
             int(priority), int(risk_tier), due, actor["actor_name"], now, now, kind, depth))
        for dep in depends:
            con.execute("INSERT INTO task_dependency VALUES (?, ?)", (task_id, dep))
        events.append(con, type="task.created", summary=f"Created task {title}", task_id=task_id,
                      payload={"owner": owner, "parent": parent_id, "depends_on": depends, "kind": kind,
                               "priority": int(priority), "risk_tier": int(risk_tier), "depth": depth}, **actor)
        if kind == "delegation":
            events.append(con, type="task.delegated", summary=f"Delegated {title} across teams", task_id=task_id,
                          payload={"from": creator_emp_id, "to": owner, "parent": parent_id}, **actor)
        if owner:
            events.append(con, type="task.assigned", summary=f"Assigned {title}", task_id=task_id,
                          payload={"owner": owner}, **actor)
    return task_id


def assign_task(con, task_id: str, owner_id: str, actor: dict = FOUNDER) -> None:
    org.authorize(con, actor, capability="assign")
    task = _require(con, task_id)
    if task["status"] not in ("created", "assigned"):
        raise InvalidTransition(f"Cannot reassign a task that is {task['status']}")
    _check_can_take(con, owner_id, task["risk_tier"])
    with _tx(con):
        con.execute("UPDATE task SET owner_emp_id = ?, status = 'assigned', updated_at = ? WHERE task_id = ?",
                    (owner_id, utc_now(), task_id))
        events.append(con, type="task.assigned", summary=f"Assigned {task['title']}", task_id=task_id,
                      payload={"owner": owner_id, "was": task["owner_emp_id"]}, **actor)


def add_dependency(con, task_id: str, depends_on: str, actor: dict = FOUNDER) -> None:
    org.authorize(con, actor, capability="assign")
    task = _require(con, task_id)
    if task["status"] in TERMINAL or task["status"] == "running":
        raise ValidationError(f"Cannot add a dependency to a task that is {task['status']}")
    if con.execute("SELECT 1 FROM task WHERE task_id = ?", (depends_on,)).fetchone() is None:
        raise ValidationError("Dependency does not exist")
    if task_id == depends_on or _reaches(con, depends_on, task_id):
        raise ValidationError("That dependency would create a cycle")
    with _tx(con):
        con.execute("INSERT OR IGNORE INTO task_dependency VALUES (?, ?)", (task_id, depends_on))
        events.append(con, type="task.dependency_added", summary=f"{task['title']} now waits for {depends_on}",
                      task_id=task_id, payload={"depends_on": depends_on}, **actor)


def _reaches(con, start: str, target: str) -> bool:
    """Is ``target`` reachable by following 'depends on' edges from ``start``?"""
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack += [r[0] for r in con.execute("SELECT depends_on FROM task_dependency WHERE task_id = ?", (node,))]
    return False


# ---------------------------------------------------------------- state machine

def _require(con, task_id: str) -> dict:
    task = get_task(con, task_id)
    if task is None or task["deleted_at"]:
        raise ValidationError("Task not found")
    return task


def transition(
    con: sqlite3.Connection, task_id: str, to: str, *, actor: dict, summary: str,
    event_type: str | None = None, payload: dict | None = None, sets: dict[str, Any] | None = None,
    lease: str | None = None, from_states: set[str] | None = None,
) -> bool:
    """Move a task to ``to`` if the state machine allows it. Returns False (and changes nothing)
    when ``lease`` is given and no longer matches, i.e. a stale worker tried to report."""
    with _tx(con):
        row = con.execute("SELECT status, title, lease_owner FROM task WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValidationError("Task not found")
        if lease is not None and row["lease_owner"] != lease:
            return False
        current = row["status"]
        if from_states is not None and current not in from_states:
            return False
        if to not in TRANSITIONS[current]:
            raise InvalidTransition(f"A {current} task cannot become {to}")
        cols = {"status": to, "updated_at": utc_now(), **(sets or {})}
        con.execute(f"UPDATE task SET {', '.join(f'{k} = ?' for k in cols)} WHERE task_id = ?",
                    [*cols.values(), task_id])
        events.append(con, type=event_type or f"task.{to}", summary=summary, task_id=task_id,
                      payload={"from": current, "to": to, **(payload or {})}, **actor)
    return True


def open_block(con, task_id: str, reason: str, detail: str | None, resume_at: str | None = None) -> int:
    if reason not in BLOCK_REASONS:
        raise ValueError(f"unknown block reason {reason}")
    cur = con.execute("INSERT INTO block (task_id, reason, detail, since, resume_at) VALUES (?, ?, ?, ?, ?)",
                      (task_id, reason, events.scrub(detail), utc_now(), resume_at))
    return int(cur.lastrowid)


def resolve_block(con, task_id: str, actor: dict = FOUNDER, note: str = "") -> None:
    org.authorize(con, actor, capability="assign")
    task = _require(con, task_id)
    if task["status"] != "blocked" or task["block"] is None:
        raise InvalidTransition("This task is not blocked")
    with _tx(con):
        con.execute("UPDATE block SET resolved_at = ?, resolved_by = ? WHERE task_id = ? AND resolved_at IS NULL",
                    (utc_now(), actor["actor_name"], task_id))
        transition(con, task_id, "assigned", actor=actor, event_type="task.block_resolved",
                   summary=f"Unblocked {task['title']}" + (f": {note}" if note else ""),
                   payload={"reason": task["block"]["reason"], "note": note},
                   sets={"lease_owner": None, "lease_expires_at": None, "not_before": None})
    free_owner(con, task["owner_emp_id"])


def retry_failed(con, task_id: str, actor: dict = FOUNDER) -> None:
    org.authorize(con, actor, capability="assign")
    task = _require(con, task_id)
    transition(con, task_id, "assigned", actor=actor, event_type="task.retry_requested",
               summary=f"Retry requested for {task['title']}", payload={"attempts_reset": True},
               sets={"attempts": 0, "recoveries": 0, "not_before": None, "lease_owner": None,
                     "lease_expires_at": None})


def cancel_task(con, task_id: str, actor: dict = FOUNDER, reason: str = "") -> None:
    """Cancel a task. A running task is asked to stop; the engine finishes the cancellation."""
    org.authorize(con, actor, capability="assign")
    task = _require(con, task_id)
    if task["status"] in TERMINAL:
        raise InvalidTransition(f"Task is already {task['status']}")
    if task["status"] == "running":
        with _tx(con):
            con.execute("UPDATE task SET cancel_requested = 1 WHERE task_id = ?", (task_id,))
            events.append(con, type="task.cancel_requested", summary=f"Cancel requested for {task['title']}",
                          task_id=task_id, payload={"reason": reason}, **actor)
        return
    with _tx(con):
        con.execute("UPDATE block SET resolved_at = ?, resolved_by = ? WHERE task_id = ? AND resolved_at IS NULL",
                    (utc_now(), actor["actor_name"], task_id))
        transition(con, task_id, "rejected", actor=actor, event_type="task.cancelled",
                   summary=f"Cancelled {task['title']}", payload={"reason": reason},
                   sets={"lease_owner": None, "lease_expires_at": None})
        fail_dependents(con, task_id, "rejected")
    free_owner(con, task["owner_emp_id"])


def decide_approval(con, task_id: str, decision: str, note: str = "", actor: dict = FOUNDER) -> None:
    """Only the Founder decides approvals - never while acting as someone else (unless overridden)."""
    if actor.get("actor_type") != "founder":
        raise PermissionError("Only the Founder can decide approvals")
    org.authorize(con, actor, founder_only=True)
    if decision not in ("approved", "rejected", "revise"):
        raise ValidationError("Decision must be approved, rejected or revise")
    task = _require(con, task_id)
    if task["status"] != "awaiting_approval" or task["approval"] is None:
        raise InvalidTransition("This task is not waiting for approval")
    target = {"approved": "done", "rejected": "rejected", "revise": "assigned"}[decision]
    with _tx(con):
        con.execute("UPDATE approval SET decided_at = ?, decision = ?, note = ? WHERE approval_id = ?",
                    (utc_now(), decision, note, task["approval"]["approval_id"]))
        transition(con, task_id, target, actor=actor, event_type="task.approval_decided",
                   summary=f"Founder {decision} {task['title']}", payload={"decision": decision, "note": note},
                   sets={"lease_owner": None, "lease_expires_at": None, "not_before": None})
        if target == "rejected":
            fail_dependents(con, task_id, "rejected")


# ---------------------------------------------------------------- dependencies and employee status

def fail_dependents(con, task_id: str, why: str) -> list[str]:
    """Tasks that wait for a failed or rejected task cannot proceed: block them for a human."""
    blocked = []
    rows = con.execute(
        "SELECT t.task_id, t.title, t.owner_emp_id FROM task_dependency d JOIN task t ON t.task_id = d.task_id "
        "WHERE d.depends_on = ? AND t.status = 'assigned'", (task_id,)).fetchall()
    for row in rows:
        with _tx(con):
            open_block(con, row["task_id"], "NEEDS_INPUT", f"{DEPENDENCY_PREFIX}{task_id} {why}")
            transition(con, row["task_id"], "blocked", actor=SYSTEM, event_type="task.dependency_failed",
                       summary=f"{row['title']} is blocked: dependency {task_id} {why}",
                       payload={"dependency": task_id, "why": why})
        blocked.append(row["task_id"])
    return blocked


def auto_unblock(con) -> list[str]:
    """Release tasks that were blocked only because a dependency failed, once all dependencies are done."""
    ready = con.execute(
        "SELECT t.task_id, t.title FROM task t JOIN block b ON b.task_id = t.task_id AND b.resolved_at IS NULL "
        "WHERE t.status = 'blocked' AND b.reason = 'NEEDS_INPUT' AND b.detail LIKE ? "
        "AND NOT EXISTS (SELECT 1 FROM task_dependency d JOIN task x ON x.task_id = d.depends_on "
        "WHERE d.task_id = t.task_id AND x.status <> 'done')", (DEPENDENCY_PREFIX + "%",)).fetchall()
    out = []
    for row in ready:
        with _tx(con):
            con.execute("UPDATE block SET resolved_at = ?, resolved_by = 'system' "
                        "WHERE task_id = ? AND resolved_at IS NULL", (utc_now(), row["task_id"]))
            transition(con, row["task_id"], "assigned", actor=SYSTEM, event_type="task.block_resolved",
                       summary=f"Dependencies of {row['title']} are done", payload={"auto": True},
                       sets={"lease_owner": None, "lease_expires_at": None})
        out.append(row["task_id"])
    return out


def set_employee_status(con, emp_id: str | None, status: str) -> None:
    """Keep the employee's working/idle/blocked marker in step with the task. Never touches
    archived or paused employees."""
    if emp_id:
        con.execute("UPDATE employee SET status = ? WHERE emp_id = ? AND status IN ('idle', 'working', 'blocked')",
                    (status, emp_id))


def free_owner(con, emp_id: str | None) -> None:
    if emp_id and con.execute("SELECT 1 FROM task WHERE owner_emp_id = ? AND status = 'running'",
                              (emp_id,)).fetchone() is None:
        blocked = con.execute("SELECT 1 FROM task WHERE owner_emp_id = ? AND status = 'blocked'", (emp_id,)).fetchone()
        set_employee_status(con, emp_id, "blocked" if blocked else "idle")

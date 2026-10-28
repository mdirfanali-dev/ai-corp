"""Projects, meetings, decisions and action items.

A meeting is a record (agenda, attendees, minutes in Markdown, decisions, action items that become
tasks). A "bot meeting" is an orchestrated round, never free chat between bots: every employee
attendee gets one short task for their input, then the CEO consolidates the inputs into minutes.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import events, org, tasks
from .config import utc_now
from .org import FOUNDER, ValidationError, _tx, authorize, clean_name

PROJECT_STATUSES = ("active", "paused", "done")
STALE_PROJECT_DAYS = 5


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _text(value: Any, limit: int = 20000) -> str:
    return events.scrub(str(value or "").replace("\x00", ""))[:limit]


# ---------------------------------------------------------------- projects

def create_project(con: sqlite3.Connection, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    name = clean_name(data.get("name"), "Project name")
    owner = (data.get("owner_emp_id") or "").strip() or None
    if owner and not con.execute("SELECT 1 FROM employee WHERE emp_id = ? AND deleted_at IS NULL", (owner,)).fetchone():
        raise ValidationError("Owner does not exist")
    status = data.get("status") or "active"
    if status not in PROJECT_STATUSES:
        raise ValidationError("Status must be active, paused or done")
    project_id = _id("P")
    with _tx(con):
        con.execute("INSERT INTO project (project_id, name, goal, status, owner_emp_id, next_review_at, created_at, notes) "
                    "VALUES (?,?,?,?,?,?,?,?)", (project_id, name, _text(data.get("goal"), 2000), status, owner,
                                                 tasks.parse_when(data.get("next_review_at")), utc_now(), _text(data.get("notes"), 4000)))
        events.append(con, type="project.created", summary=f"Created project {name}", payload={"project_id": project_id}, **actor)
    return project_id


def update_project(con, project_id: str, data: dict, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    current = get_project(con, project_id)
    if current is None:
        raise ValidationError("Project not found")
    name = clean_name(data.get("name"), "Project name")
    status = data.get("status") or current["status"]
    if status not in PROJECT_STATUSES:
        raise ValidationError("Status must be active, paused or done")
    owner = (data.get("owner_emp_id") or "").strip() or None
    if owner and not con.execute("SELECT 1 FROM employee WHERE emp_id = ? AND deleted_at IS NULL", (owner,)).fetchone():
        raise ValidationError("Owner does not exist")
    with _tx(con):
        con.execute("UPDATE project SET name=?, goal=?, status=?, owner_emp_id=?, next_review_at=?, notes=? WHERE project_id=?",
                    (name, _text(data.get("goal"), 2000), status, owner, tasks.parse_when(data.get("next_review_at")),
                     _text(data.get("notes"), 4000), project_id))
        events.append(con, type="project.updated", summary=f"Updated project {name}" + (f" ({current['status']} → {status})" if status != current["status"] else ""),
                      payload={"project_id": project_id}, **actor)


def delete_project(con, project_id: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    project = get_project(con, project_id)
    if project is None:
        raise ValidationError("Project not found")
    with _tx(con):
        con.execute("UPDATE project SET deleted_at = ? WHERE project_id = ?", (utc_now(), project_id))
        events.append(con, type="project.deleted", summary=f"Deleted project {project['name']}", payload={"project_id": project_id}, **actor)


def get_project(con, project_id: str) -> dict | None:
    row = con.execute("SELECT p.*, e.name AS owner_name FROM project p LEFT JOIN employee e ON e.emp_id = p.owner_emp_id "
                      "WHERE p.project_id = ? AND p.deleted_at IS NULL", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects(con, include_done: bool = True) -> list[dict]:
    sql = ("SELECT p.*, e.name AS owner_name FROM project p LEFT JOIN employee e ON e.emp_id = p.owner_emp_id "
           "WHERE p.deleted_at IS NULL" + ("" if include_done else " AND p.status <> 'done'") +
           " ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, p.name COLLATE NOCASE")
    return [dict(r) for r in con.execute(sql)]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def project_health(con: sqlite3.Connection, project_id: str, now: datetime | None = None, stale_days: int = STALE_PROJECT_DAYS) -> dict:
    """Open/blocked/overdue counts, last activity and a one-word label (ok / attention / stalled)."""
    now = now or datetime.now(timezone.utc)
    t = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, count(*) AS n FROM task WHERE project_id = ? AND deleted_at IS NULL GROUP BY status", (project_id,))}
    open_n = sum(n for s, n in t.items() if s not in tasks.TERMINAL)
    overdue = con.execute("SELECT count(*) FROM task WHERE project_id = ? AND deleted_at IS NULL AND due_at IS NOT NULL AND due_at < ? "
                          "AND status NOT IN ('done', 'rejected')", (project_id, _iso(now))).fetchone()[0]
    last = con.execute("SELECT max(updated_at) FROM task WHERE project_id = ? AND deleted_at IS NULL", (project_id,)).fetchone()[0]
    created = con.execute("SELECT created_at FROM project WHERE project_id = ?", (project_id,)).fetchone()
    anchor = last or (created[0] if created else None)
    idle_days = (now - datetime.fromisoformat(anchor)).total_seconds() / 86400 if anchor else 0.0
    blocked = t.get("blocked", 0) + t.get("failed", 0)
    label = "ok"
    if open_n and idle_days >= stale_days:
        label = "stalled"
    elif blocked or overdue:
        label = "attention"
    upcoming = con.execute("SELECT count(*) FROM meeting WHERE project_id = ? AND deleted_at IS NULL AND status = 'scheduled' "
                           "AND scheduled_at >= ?", (project_id, _iso(now))).fetchone()[0]
    return {"open": open_n, "done": t.get("done", 0), "blocked": blocked, "overdue": overdue, "last_activity": anchor,
            "idle_days": round(idle_days, 1), "label": label, "upcoming_meetings": upcoming}


# ---------------------------------------------------------------- meetings

def _attendees(con, raw: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in dict.fromkeys(str(x).strip() for x in raw if str(x).strip()):
        if item == "founder":
            out.append(("founder", "founder"))
        elif con.execute("SELECT 1 FROM employee WHERE emp_id = ? AND deleted_at IS NULL", (item,)).fetchone():
            out.append((item, "employee"))
        else:
            raise ValidationError(f"Unknown attendee {item}")
    return out


def _meeting_fields(con, data: dict) -> dict:
    project = (data.get("project_id") or "").strip() or None
    if project and get_project(con, project) is None:
        raise ValidationError("Project does not exist")
    return {"title": clean_name(data.get("title"), "Title"), "project": project, "when": tasks.parse_when(data.get("scheduled_at")),
            "agenda": _text(data.get("agenda"), 6000), "attendees": _attendees(con, org.as_list(data.get("attendees")))}


def create_meeting(con, home: Path, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    f = _meeting_fields(con, data)
    meeting_id = _id("M")
    with _tx(con):
        con.execute("INSERT INTO meeting (meeting_id, project_id, title, scheduled_at, agenda, created_at) VALUES (?,?,?,?,?,?)",
                    (meeting_id, f["project"], f["title"], f["when"], f["agenda"], utc_now()))
        for attendee, kind in f["attendees"]:
            con.execute("INSERT INTO meeting_attendee VALUES (?, ?, ?)", (meeting_id, attendee, kind))
        events.append(con, type="meeting.created", summary=f"Scheduled meeting {f['title']}",
                      payload={"meeting_id": meeting_id, "at": f["when"]}, **actor)
    return meeting_id


def update_meeting(con, home: Path, meeting_id: str, data: dict, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    if get_meeting(con, meeting_id) is None:
        raise ValidationError("Meeting not found")
    f = _meeting_fields(con, data)
    with _tx(con):
        con.execute("UPDATE meeting SET project_id=?, title=?, scheduled_at=?, agenda=? WHERE meeting_id=?",
                    (f["project"], f["title"], f["when"], f["agenda"], meeting_id))
        con.execute("DELETE FROM meeting_attendee WHERE meeting_id = ?", (meeting_id,))
        for attendee, kind in f["attendees"]:
            con.execute("INSERT INTO meeting_attendee VALUES (?, ?, ?)", (meeting_id, attendee, kind))
        events.append(con, type="meeting.updated", summary=f"Updated meeting {f['title']}", payload={"meeting_id": meeting_id}, **actor)


def set_meeting_status(con, meeting_id: str, status: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    if status not in ("scheduled", "held", "cancelled"):
        raise ValidationError("Status must be scheduled, held or cancelled")
    meeting = get_meeting(con, meeting_id)
    if meeting is None:
        raise ValidationError("Meeting not found")
    with _tx(con):
        con.execute("UPDATE meeting SET status = ? WHERE meeting_id = ?", (status, meeting_id))
        events.append(con, type=f"meeting.{status}", summary=f"Meeting {meeting['title']} marked {status}", payload={"meeting_id": meeting_id}, **actor)


def save_minutes(con, home: Path, meeting_id: str, text: str, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    meeting = get_meeting(con, meeting_id)
    if meeting is None:
        raise ValidationError("Meeting not found")
    relative = f"memory/meetings/{meeting_id}.md"
    _write(home / relative, _text(text, 60000).rstrip() + "\n")
    with _tx(con):
        con.execute("UPDATE meeting SET minutes_path = ? WHERE meeting_id = ?", (relative, meeting_id))
        events.append(con, type="meeting.minutes_saved", summary=f"Saved minutes for {meeting['title']}", payload={"meeting_id": meeting_id}, **actor)
    return relative


def get_meeting(con, meeting_id: str) -> dict | None:
    row = con.execute("SELECT m.*, p.name AS project_name FROM meeting m LEFT JOIN project p ON p.project_id = m.project_id "
                      "WHERE m.meeting_id = ? AND m.deleted_at IS NULL", (meeting_id,)).fetchone()
    if row is None:
        return None
    meeting = dict(row)
    meeting["attendees"] = [{"id": r["attendee_id"], "kind": r["attendee_kind"],
                             "name": "Founder" if r["attendee_kind"] == "founder" else (con.execute("SELECT name FROM employee WHERE emp_id = ?", (r["attendee_id"],)).fetchone() or ["?"])[0]}
                            for r in con.execute("SELECT * FROM meeting_attendee WHERE meeting_id = ?", (meeting_id,))]
    meeting["actions"] = [dict(r) for r in con.execute(
        "SELECT a.*, t.status AS task_status, t.title AS task_title FROM action_item a LEFT JOIN task t ON t.task_id = a.task_id "
        "WHERE a.meeting_id = ? ORDER BY a.created_at", (meeting_id,))]
    meeting["decisions"] = [dict(r) for r in con.execute("SELECT * FROM decision WHERE meeting_id = ? ORDER BY created_at", (meeting_id,))]
    return meeting


def list_meetings(con, project_id: str | None = None, upcoming: bool = False, now: datetime | None = None) -> list[dict]:
    sql, args = "SELECT m.*, p.name AS project_name FROM meeting m LEFT JOIN project p ON p.project_id = m.project_id WHERE m.deleted_at IS NULL", []
    if project_id:
        sql += " AND m.project_id = ?"
        args.append(project_id)
    if upcoming:
        sql += " AND m.status = 'scheduled' AND m.scheduled_at >= ?"
        args.append(_iso(now or datetime.now(timezone.utc)))
    sql += " ORDER BY COALESCE(m.scheduled_at, m.created_at) DESC" if not upcoming else " ORDER BY m.scheduled_at"
    return [dict(r) for r in con.execute(sql, args)]


# ---------------------------------------------------------------- action items and decisions

def add_action_item(con, meeting_id: str, description: str, owner: str | None = None, due_at: str | None = None,
                    risk_tier: int = 1, actor: dict = FOUNDER) -> str:
    """Record an action item. With an owner it becomes a task in the meeting's project."""
    authorize(con, actor, founder_only=True)
    meeting = get_meeting(con, meeting_id)
    if meeting is None:
        raise ValidationError("Meeting not found")
    description = _text(description, 500).strip()
    if not description:
        raise ValidationError("Describe the action item")
    task_id = None
    if owner:
        task_id = tasks.create_task(con, title=description[:60], description=description, owner=owner, project_id=meeting["project_id"],
                                    due_at=due_at, risk_tier=risk_tier, actor=actor)
    item_id = _id("A")
    with _tx(con):
        con.execute("INSERT INTO action_item (item_id, meeting_id, task_id, description, created_at) VALUES (?,?,?,?,?)",
                    (item_id, meeting_id, task_id, description, utc_now()))
        events.append(con, type="meeting.action_item", summary=f"Action item from {meeting['title']}: {description[:80]}",
                      task_id=task_id, payload={"meeting_id": meeting_id, "item_id": item_id}, **actor)
    return item_id


def add_decision(con, home: Path, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    title = clean_name(data.get("title"), "Decision")
    for key, table, col in (("meeting_id", "meeting", "meeting_id"), ("project_id", "project", "project_id")):
        if data.get(key) and not con.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (data[key],)).fetchone():
            raise ValidationError(f"Unknown {key.split('_')[0]}")
    meeting = get_meeting(con, data["meeting_id"]) if data.get("meeting_id") else None
    project = data.get("project_id") or (meeting["project_id"] if meeting else None)
    decision_id = _id("D")
    relative = f"memory/decisions/{decision_id}.md"
    text = _text(data.get("text"), 4000)
    follow = tasks.parse_when(data.get("follow_up_by"))
    _write(home / relative, f"# {title}\n\nDecided: {utc_now()[:10]}\nFollow up by: {follow[:10] if follow else '—'}\n\n{text}\n")
    with _tx(con):
        con.execute("INSERT INTO decision (decision_id, project_id, meeting_id, task_id, title, text, follow_up_by, created_at, md_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (decision_id, project, data.get("meeting_id") or None, data.get("task_id") or None, title, text,
                                                   follow, utc_now(), relative))
        events.append(con, type="decision.recorded", summary=f"Decision: {title}", payload={"decision_id": decision_id}, **actor)
    return decision_id


def close_decision(con, decision_id: str, status: str = "followed_up", actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    if status not in ("followed_up", "dropped"):
        raise ValidationError("Status must be followed_up or dropped")
    row = con.execute("SELECT title, status FROM decision WHERE decision_id = ?", (decision_id,)).fetchone()
    if row is None or row["status"] != "open":
        raise ValidationError("Decision not found or already closed")
    with _tx(con):
        con.execute("UPDATE decision SET status = ?, closed_at = ? WHERE decision_id = ?", (status, utc_now(), decision_id))
        events.append(con, type=f"decision.{status}", summary=f"Decision {row['title']}: {status.replace('_', ' ')}", payload={"decision_id": decision_id}, **actor)


def list_decisions(con, status: str | None = None, project_id: str | None = None) -> list[dict]:
    sql, args = "SELECT d.*, p.name AS project_name FROM decision d LEFT JOIN project p ON p.project_id = d.project_id WHERE 1=1", []
    if status:
        sql += " AND d.status = ?"
        args.append(status)
    if project_id:
        sql += " AND d.project_id = ?"
        args.append(project_id)
    return [dict(r) for r in con.execute(sql + " ORDER BY d.created_at DESC", args)]


# ---------------------------------------------------------------- bot meeting

def start_bot_meeting(con, meeting_id: str, actor: dict = FOUNDER) -> dict:
    """One input task per employee attendee, then one consolidation task for the CEO that waits for all of them."""
    authorize(con, actor, founder_only=True)
    meeting = get_meeting(con, meeting_id)
    if meeting is None:
        raise ValidationError("Meeting not found")
    if meeting["round_task_id"]:
        raise ValidationError("A bot round was already started for this meeting")
    ceo = con.execute("SELECT emp_id FROM employee WHERE system_key = 'ceo'").fetchone()
    if ceo is None:
        raise ValidationError("The CEO is missing")
    people = [a for a in meeting["attendees"] if a["kind"] == "employee"
              and con.execute("SELECT 1 FROM employee WHERE emp_id = ? AND status <> 'archived' AND deleted_at IS NULL", (a["id"],)).fetchone()]
    if not people:
        raise ValidationError("Add at least one employee attendee first")
    brief = f"Meeting: {meeting['title']}.\nAgenda:\n{meeting['agenda'] or '(none)'}\n"
    inputs = []
    for person in people:
        inputs.append(tasks.create_task(con, title=f"Meeting input from {person['name']}"[:60], owner=person["id"], risk_tier=0, project_id=meeting["project_id"],
                                        description=brief + "Give your short, evidence-based input on each agenda point.", actor=actor))
    final = tasks.create_task(con, title=f"Minutes: {meeting['title']}"[:60], owner=ceo["emp_id"], risk_tier=1, project_id=meeting["project_id"],
                              depends_on=inputs, description=brief + "Consolidate the inputs from the attendees into minutes: decisions, open questions, action items.",
                              actor=actor)
    with _tx(con):
        con.execute("UPDATE meeting SET round_task_id = ? WHERE meeting_id = ?", (final, meeting_id))
        events.append(con, type="meeting.bot_round_started", summary=f"Bot meeting started for {meeting['title']} ({len(inputs)} inputs)",
                      payload={"meeting_id": meeting_id, "inputs": inputs, "consolidation": final}, **actor)
    return {"inputs": inputs, "consolidation": final}


def use_round_result_as_minutes(con, home: Path, meeting_id: str, actor: dict = FOUNDER) -> str:
    meeting = get_meeting(con, meeting_id)
    if meeting is None or not meeting["round_task_id"]:
        raise ValidationError("This meeting has no bot round")
    row = con.execute("SELECT status, result_summary FROM task WHERE task_id = ?", (meeting["round_task_id"],)).fetchone()
    if row is None or row["status"] != "done" or not row["result_summary"]:
        raise ValidationError("The consolidation task has not finished yet")
    return save_minutes(con, home, meeting_id, f"# Minutes: {meeting['title']}\n\n{row['result_summary']}", actor)

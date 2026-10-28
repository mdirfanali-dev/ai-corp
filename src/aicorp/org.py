"""Organisation model: employees, roles and teams.

All rules live here (not in the web layer) so every caller gets the same checks:
unique names, role/team validity, temporary vs permanent lifecycle, and the fixed
limits of the built-in Advisor. Every change is written to the audit log.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import events, trash
from .config import utc_now

FOUNDER = {"actor_type": "founder", "actor_name": "Founder", "actor_designation": "Chairman"}
SYSTEM = {"actor_type": "system", "actor_name": "system", "actor_designation": None}
CREATABLE_KINDS = ("permanent", "temporary")
GRANTABLE = ("execute", "assign")  # 'approve' is never grantable: approvals belong to the Founder
NAME_MAX = 60
SYSTEM_ROLES = ("Chief Executive", "Chief of Staff")


class ValidationError(ValueError):
    def __init__(self, *errors: str):
        self.errors = [e for e in errors if e]
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------- helpers

@contextmanager
def _tx(con: sqlite3.Connection):
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _log(con, actor: dict, type: str, summary: str, payload: dict | None = None) -> None:
    events.append(con, type=type, summary=summary, payload=payload, **actor)


def clean_name(value: Any, label: str = "Name") -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not name:
        raise ValidationError(f"{label} is required")
    if len(name) > NAME_MAX:
        raise ValidationError(f"{label} must be at most {NAME_MAX} characters")
    if any(ord(c) < 32 for c in name):
        raise ValidationError(f"{label} contains control characters")
    return name


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = re.split(r"[,\n]", value) if isinstance(value, str) else list(value)
    seen: dict[str, None] = {}
    for item in items:
        item = str(item).strip()
        if item:
            seen.setdefault(item, None)
    return list(seen)


def parse_expiry(value: Any) -> str | None:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (local time) and store UTC ISO."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("Expiry must look like 2026-12-31 or 2026-12-31T18:00") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # interpret as the machine's local time
    return parsed.astimezone(timezone.utc).isoformat(timespec="minutes")


def to_local_input(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%dT%H:%M")


def _exists(con, table: str, key: str, value: str, extra: str = "") -> bool:
    return con.execute(f"SELECT 1 FROM {table} WHERE {key} = ? {extra}", (value,)).fetchone() is not None


# ---------------------------------------------------------------- authorisation

def authorize(con: sqlite3.Connection, actor: dict, *, capability: str | None = None, founder_only: bool = False) -> None:
    """Server-side permission check for anything that changes company state.

    The Founder and the system may do everything. An employee acting through the app may only do what
    its effective capabilities allow; the built-in Advisor never has execute/assign/approve.

    While the Founder (or the CEO) is *impersonating* someone, the permissions of the identity acted as
    apply instead - unless the Founder confirmed an explicit override. Guardrails never depend on this."""
    kind = actor.get("actor_type")
    session = events.current_acting()
    if session and not session["override"] and kind == session["by_type"] and (kind == "founder" or actor.get("actor_id") == session["by_id"]):
        target = session["target"]
        who = f"{target['name']} (acting as)"
        if founder_only or capability is None:
            raise PermissionError(f"{who} is not allowed to do this - stop impersonating, or use the Founder override")
        if not effective_capabilities(con, target["id"]).get(capability):
            raise PermissionError(f"{target['name']} has no '{capability}' permission - stop impersonating, or use the Founder override")
        return
    if kind in ("founder", "system"):
        return
    if kind == "employee":
        if founder_only or capability is None:
            raise PermissionError(f"{actor.get('actor_name', 'This employee')} is not allowed to do this")
        if not effective_capabilities(con, actor["actor_id"]).get(capability):
            raise PermissionError(f"{actor.get('actor_name', 'This employee')} has no '{capability}' permission")
        return
    raise PermissionError("unknown actor")


def acting_employee(actor: dict) -> str | None:
    """The employee whose permissions apply to this actor: itself, or the identity it is impersonating."""
    session = events.current_acting()
    if session and not session["override"] and actor.get("actor_type") == session["by_type"] \
            and (session["by_type"] == "founder" or actor.get("actor_id") == session["by_id"]):
        return session["target"]["id"]
    return actor.get("actor_id") if actor.get("actor_type") == "employee" else None


# ---------------------------------------------------------------- capabilities

def _role_caps(profile_json: str) -> dict:
    return json.loads(profile_json or "{}").get("capabilities", {})


def effective_capabilities(con: sqlite3.Connection, emp_id: str) -> dict[str, bool]:
    """What an employee may do: union of its roles and explicit grants; Advisor is always read-only."""
    row = con.execute("SELECT system_key, profile FROM employee WHERE emp_id = ?", (emp_id,)).fetchone()
    if row is None:
        raise KeyError(emp_id)
    caps = {"execute": False, "assign": False, "approve": False}
    explicit = json.loads(row["profile"] or "{}").get("capabilities", {})
    for r in con.execute(
        "SELECT r.permission_profile FROM employee_role er JOIN role r USING (role_id) WHERE er.emp_id = ?",
        (emp_id,),
    ):
        for key in GRANTABLE:
            caps[key] = caps[key] or bool(_role_caps(r["permission_profile"]).get(key))
    for key in GRANTABLE:
        caps[key] = caps[key] or bool(explicit.get(key))
    if row["system_key"] == "advisor":
        caps["execute"] = caps["assign"] = False
    return caps


def _check_advisor_limits(system_key: str | None, explicit: dict, role_ids: Iterable[str], con) -> list[str]:
    if system_key != "advisor":
        return []
    errors = []
    for key in GRANTABLE:
        if explicit.get(key):
            errors.append(f"The Advisor cannot be given '{key}' (it only reads, reminds and reports)")
    for rid in role_ids:
        row = con.execute("SELECT name, permission_profile FROM role WHERE role_id = ?", (rid,)).fetchone()
        if row and any(_role_caps(row["permission_profile"]).get(k) for k in GRANTABLE):
            errors.append(f"Role '{row['name']}' grants execute/assign and cannot be given to the Advisor")
    return errors


# ---------------------------------------------------------------- files (mirror of DB state)

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sync_employee_files(con: sqlite3.Connection, home: Path, emp_id: str) -> None:
    """Write company/employees/<id>/employee.json and instructions.md from the database."""
    emp = get_employee(con, emp_id)
    if emp is None:
        return
    folder = home / "company" / "employees" / emp_id
    doc = {
        "emp_id": emp["emp_id"], "name": emp["name"], "kind": emp["kind"], "status": emp["status"],
        "roles": [r["name"] for r in emp["roles"]], "teams": [t["name"] for t in emp["teams"]],
        "reports_to": {"emp_id": emp["reports_to"], "name": emp["manager"]} if emp["reports_to"] else None,
        "skills": emp["skills"], "tools": emp["profile"].get("tools", []),
        "capabilities": emp["capabilities"], "workspace": emp["workspace"],
        "expires_at": emp["expires_at"], "created_at": emp["created_at"],
    }
    _atomic_write(folder / "employee.json", json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    _atomic_write(folder / "instructions.md", (emp["instructions"] or "").rstrip() + "\n")


# ---------------------------------------------------------------- employees

def get_employee(con: sqlite3.Connection, emp_id: str) -> dict | None:
    row = con.execute(
        "SELECT e.*, m.name AS manager FROM employee e LEFT JOIN employee m ON m.emp_id = e.reports_to "
        "WHERE e.emp_id = ?", (emp_id,)).fetchone()
    if row is None:
        return None
    emp = dict(row)
    emp["skills"] = json.loads(emp["skills"] or "[]")
    emp["profile"] = json.loads(emp["profile"] or "{}")
    emp["roles"] = [dict(r) for r in con.execute(
        "SELECT r.role_id, r.name FROM employee_role er JOIN role r USING (role_id) "
        "WHERE er.emp_id = ? ORDER BY r.name", (emp_id,))]
    emp["teams"] = [dict(t) for t in con.execute(
        "SELECT t.team_id, t.name FROM team_member tm JOIN team t USING (team_id) "
        "WHERE tm.emp_id = ? AND t.deleted_at IS NULL ORDER BY t.name", (emp_id,))]
    emp["capabilities"] = effective_capabilities(con, emp_id)
    return emp


def list_employees(
    con: sqlite3.Connection, *, q: str | None = None, kind: str | None = None,
    status: str | None = None, team_id: str | None = None, include_deleted: bool = False,
) -> list[dict]:
    sql = ("SELECT e.emp_id FROM employee e WHERE 1=1")
    args: list[Any] = []
    if not include_deleted:
        sql += " AND e.deleted_at IS NULL"
    if q:
        sql += " AND e.name LIKE ? ESCAPE '\\'"
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if kind:
        sql += " AND e.kind = ?"
        args.append(kind)
    if status:
        sql += " AND e.status = ?"
        args.append(status)
    if team_id:
        sql += " AND e.emp_id IN (SELECT emp_id FROM team_member WHERE team_id = ?)"
        args.append(team_id)
    sql += " ORDER BY CASE e.kind WHEN 'system' THEN 0 ELSE 1 END, e.name COLLATE NOCASE"
    return [get_employee(con, r["emp_id"]) for r in con.execute(sql, args)]


def _validate_employee_input(con, data: dict, *, emp_id: str | None, system_key: str | None) -> dict:
    errors: list[str] = []
    out: dict[str, Any] = {}
    try:
        out["name"] = clean_name(data.get("name"))
    except ValidationError as e:
        errors += e.errors
    out["role_ids"] = as_list(data.get("role_ids"))
    for rid in out["role_ids"]:
        if not _exists(con, "role", "role_id", rid):
            errors.append(f"Unknown role {rid}")
    out["team_ids"] = as_list(data.get("team_ids"))
    for tid in out["team_ids"]:
        if not _exists(con, "team", "team_id", tid, "AND deleted_at IS NULL"):
            errors.append(f"Unknown team {tid}")
    manager = (data.get("reports_to") or "").strip() or None
    if manager:
        if not _exists(con, "employee", "emp_id", manager, "AND deleted_at IS NULL"):
            errors.append("Manager does not exist")
        else:
            cursor, hops = manager, 0
            while cursor and hops < 100:
                if cursor == emp_id:
                    errors.append("An employee cannot report to themselves or to their own subordinate")
                    break
                nxt = con.execute("SELECT reports_to FROM employee WHERE emp_id = ?", (cursor,)).fetchone()
                cursor, hops = (nxt["reports_to"] if nxt else None), hops + 1
    out["reports_to"] = manager
    out["skills"] = as_list(data.get("skills"))
    explicit = {k: bool(data.get(k)) for k in GRANTABLE}
    out["profile"] = {"tools": as_list(data.get("tools")), "capabilities": explicit}
    from .limits import ALLOWED_MODELS, TIMEOUT, TURNS
    if (data.get("model") or "").strip():
        if data["model"].strip() not in ALLOWED_MODELS:
            errors.append("Model must be one of: " + ", ".join(ALLOWED_MODELS))
        else:
            out["profile"]["model"] = data["model"].strip()
    for key, bounds in (("max_turns", TURNS), ("timeout", TIMEOUT)):
        raw = str(data.get(key) or "").strip()
        if raw:
            if not raw.isdigit() or not bounds[0] <= int(raw) <= bounds[1]:
                errors.append(f"{key.replace('_', ' ').capitalize()} must be a whole number from {bounds[0]} to {bounds[1]}")
            else:
                out["profile"][key] = int(raw)
    out["instructions"] = str(data.get("instructions") or "")
    out["notes"] = str(data.get("notes") or "").strip() or None
    try:
        out["expires_at"] = parse_expiry(data.get("expires_at"))
    except ValidationError as e:
        errors += e.errors
    errors += _check_advisor_limits(system_key, explicit, out["role_ids"], con)
    if errors:
        raise ValidationError(*errors)
    return out


def _write_relations(con, emp_id: str, role_ids: list[str], team_ids: list[str]) -> None:
    con.execute("DELETE FROM employee_role WHERE emp_id = ?", (emp_id,))
    for rid in role_ids:
        con.execute("INSERT INTO employee_role VALUES (?, ?, ?)", (emp_id, rid, utc_now()))
    con.execute("DELETE FROM team_member WHERE emp_id = ?", (emp_id,))
    for tid in team_ids:
        con.execute("INSERT INTO team_member VALUES (?, ?)", (tid, emp_id))


def _name_conflict(err: sqlite3.IntegrityError, name: str) -> ValidationError | None:
    if "employee.name" in str(err):
        return ValidationError(f"The name '{name}' is already used by another employee")
    return None


def create_employee(con, home: Path, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    kind = data.get("kind") or "permanent"
    if kind not in CREATABLE_KINDS:
        raise ValidationError("Kind must be permanent or temporary")
    clean = _validate_employee_input(con, data, emp_id=None, system_key=None)
    if kind == "permanent":
        clean["expires_at"] = None
    emp_id = _new_id("E")
    workspace = f"workspaces/employees/{emp_id}"
    try:
        with _tx(con):
            con.execute(
                "INSERT INTO employee (emp_id, name, kind, reports_to, workspace, notes, expires_at, "
                "created_at, skills, profile, instructions) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (emp_id, clean["name"], kind, clean["reports_to"], workspace, clean["notes"],
                 clean["expires_at"], utc_now(), json.dumps(clean["skills"], ensure_ascii=False),
                 json.dumps(clean["profile"], ensure_ascii=False), clean["instructions"]))
            _write_relations(con, emp_id, clean["role_ids"], clean["team_ids"])
            _log(con, actor, "employee.created", f"Created {kind} employee {clean['name']}",
                 {"emp_id": emp_id, "name": clean["name"], "kind": kind, "roles": clean["role_ids"],
                  "teams": clean["team_ids"], "expires_at": clean["expires_at"]})
    except sqlite3.IntegrityError as err:
        raise (_name_conflict(err, clean["name"]) or err) from None
    sync_employee_files(con, home, emp_id)
    return emp_id


def update_employee(con, home: Path, emp_id: str, data: dict, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    current = get_employee(con, emp_id)
    if current is None or current["deleted_at"]:
        raise ValidationError("Employee not found")
    data = {**{k: current["profile"][k] for k in ("model", "max_turns", "timeout") if k in current["profile"] and k not in data}, **data}
    clean = _validate_employee_input(con, data, emp_id=emp_id, system_key=current["system_key"])
    if current["kind"] != "temporary":
        clean["expires_at"] = None
    before = {"name": current["name"], "roles": [r["role_id"] for r in current["roles"]],
              "teams": [t["team_id"] for t in current["teams"]], "reports_to": current["reports_to"],
              "skills": current["skills"], "tools": current["profile"].get("tools", []),
              "expires_at": current["expires_at"]}
    after = {"name": clean["name"], "roles": clean["role_ids"], "teams": clean["team_ids"],
             "reports_to": clean["reports_to"], "skills": clean["skills"],
             "tools": clean["profile"]["tools"], "expires_at": clean["expires_at"]}
    changed = sorted(k for k in after if after[k] != before[k])
    if clean["instructions"] != current["instructions"]:
        changed.append("instructions")
    try:
        with _tx(con):
            con.execute(
                "UPDATE employee SET name=?, reports_to=?, notes=?, expires_at=?, skills=?, profile=?, "
                "instructions=? WHERE emp_id=?",
                (clean["name"], clean["reports_to"], clean["notes"], clean["expires_at"],
                 json.dumps(clean["skills"], ensure_ascii=False),
                 json.dumps(clean["profile"], ensure_ascii=False), clean["instructions"], emp_id))
            _write_relations(con, emp_id, clean["role_ids"], clean["team_ids"])
            _log(con, actor, "employee.updated",
                 f"Updated {clean['name']}" + (f" ({', '.join(changed)})" if changed else " (no changes)"),
                 {"emp_id": emp_id, "changed": changed,
                  "before": {k: before[k] for k in changed if k in before},
                  "after": {k: after[k] for k in changed if k in after}})
    except sqlite3.IntegrityError as err:
        raise (_name_conflict(err, clean["name"]) or err) from None
    sync_employee_files(con, home, emp_id)


def _require_regular(con, emp_id: str) -> dict:
    emp = get_employee(con, emp_id)
    if emp is None or emp["deleted_at"]:
        raise ValidationError("Employee not found")
    if emp["kind"] == "system":
        raise ValidationError(f"{emp['name']} is a built-in employee and cannot be archived or deleted")
    return emp


def archive_employee(con, home: Path, emp_id: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    emp = _require_regular(con, emp_id)
    with _tx(con):
        con.execute("UPDATE employee SET status = 'archived' WHERE emp_id = ?", (emp_id,))
        _log(con, actor, "employee.archived", f"Archived {emp['name']}", {"emp_id": emp_id})
    sync_employee_files(con, home, emp_id)


def restore_employee(con, home: Path, emp_id: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    emp = _require_regular(con, emp_id)
    if emp["status"] != "archived":
        raise ValidationError(f"{emp['name']} is not archived")
    with _tx(con):
        con.execute("UPDATE employee SET status = 'idle' WHERE emp_id = ?", (emp_id,))
        _log(con, actor, "employee.restored", f"Restored {emp['name']}", {"emp_id": emp_id})
    sync_employee_files(con, home, emp_id)


def delete_employee(con, home: Path, emp_id: str, actor: dict = FOUNDER) -> None:
    """Delete a temporary employee. The record is kept (soft delete) so logs stay meaningful;
    the name is released and the config folder moves to the trash."""
    authorize(con, actor, founder_only=True)
    emp = _require_regular(con, emp_id)
    if emp["kind"] != "temporary":
        raise ValidationError("Permanent employees can only be archived, not deleted")
    released = f"{emp['name']} [deleted {emp_id}]"
    with _tx(con):
        con.execute("UPDATE team SET head_emp_id = NULL WHERE head_emp_id = ?", (emp_id,))
        con.execute("UPDATE employee SET name=?, status='archived', deleted_at=?, deleted_by=? WHERE emp_id=?",
                    (released, utc_now(), actor["actor_name"], emp_id))
        _log(con, actor, "employee.deleted", f"Deleted temporary employee {emp['name']}",
             {"emp_id": emp_id, "name": emp["name"]})
    folder = home / "company" / "employees" / emp_id
    if folder.exists():
        trash.move_to_trash(home, folder, reason="temporary employee deleted", by=actor["actor_name"])


def expire_temporary(con, home: Path, now: datetime | None = None) -> list[str]:
    """Archive temporary employees whose expiry has passed. Deleting stays a Founder decision."""
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="minutes")
    due = [r["emp_id"] for r in con.execute(
        "SELECT emp_id FROM employee WHERE kind='temporary' AND deleted_at IS NULL AND status <> 'archived' "
        "AND expires_at IS NOT NULL AND expires_at <= ?", (now_iso,))]
    for emp_id in due:
        name = con.execute("SELECT name FROM employee WHERE emp_id = ?", (emp_id,)).fetchone()["name"]
        with _tx(con):
            con.execute("UPDATE employee SET status = 'archived' WHERE emp_id = ?", (emp_id,))
            _log(con, SYSTEM, "employee.expired", f"Temporary employee {name} reached its end date",
                 {"emp_id": emp_id})
        sync_employee_files(con, home, emp_id)
    return due


# ---------------------------------------------------------------- roles

def get_role(con, role_id: str) -> dict | None:
    row = con.execute("SELECT * FROM role WHERE role_id = ?", (role_id,)).fetchone()
    if row is None:
        return None
    role = dict(row)
    role["permission_profile"] = json.loads(role["permission_profile"] or "{}")
    role["skills"] = json.loads(role["skills"] or "[]")
    role["capabilities"] = role["permission_profile"].get("capabilities", {})
    role["tools"] = role["permission_profile"].get("tools", [])
    role["members"] = [dict(r) for r in con.execute(
        "SELECT e.emp_id, e.name FROM employee_role er JOIN employee e USING (emp_id) "
        "WHERE er.role_id = ? AND e.deleted_at IS NULL ORDER BY e.name", (role_id,))]
    return role


def list_roles(con) -> list[dict]:
    return [get_role(con, r["role_id"]) for r in con.execute("SELECT role_id FROM role ORDER BY name COLLATE NOCASE")]


def _role_payload(con, data: dict, role_id: str | None) -> tuple[str, str, str, str]:
    name = clean_name(data.get("name"), "Role name")
    caps = {k: bool(data.get(k)) for k in GRANTABLE}
    profile = json.dumps({"capabilities": caps, "tools": as_list(data.get("tools"))}, ensure_ascii=False)
    skills = json.dumps(as_list(data.get("skills")), ensure_ascii=False)
    if role_id and any(caps.values()):
        holders = con.execute(
            "SELECT e.name FROM employee_role er JOIN employee e USING (emp_id) "
            "WHERE er.role_id = ? AND e.system_key = 'advisor'", (role_id,)).fetchall()
        if holders:
            raise ValidationError("This role is held by the Advisor and cannot grant execute/assign")
    return name, str(data.get("description") or "").strip(), profile, skills


def create_role(con, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    name, description, profile, skills = _role_payload(con, data, None)
    role_id = _new_id("R")
    try:
        with _tx(con):
            con.execute("INSERT INTO role (role_id, name, description, permission_profile, skills, created_at) "
                        "VALUES (?,?,?,?,?,?)", (role_id, name, description, profile, skills, utc_now()))
            _log(con, actor, "role.created", f"Created role {name}", {"role_id": role_id})
    except sqlite3.IntegrityError as err:
        if "role.name" in str(err):
            raise ValidationError(f"A role named '{name}' already exists") from None
        raise
    return role_id


def update_role(con, role_id: str, data: dict, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    current = get_role(con, role_id)
    if current is None:
        raise ValidationError("Role not found")
    name, description, profile, skills = _role_payload(con, data, role_id)
    if current["name"] in SYSTEM_ROLES and name != current["name"]:
        raise ValidationError("Built-in roles cannot be renamed")
    try:
        with _tx(con):
            con.execute("UPDATE role SET name=?, description=?, permission_profile=?, skills=? WHERE role_id=?",
                        (name, description, profile, skills, role_id))
            _log(con, actor, "role.updated", f"Updated role {name}", {"role_id": role_id})
    except sqlite3.IntegrityError as err:
        if "role.name" in str(err):
            raise ValidationError(f"A role named '{name}' already exists") from None
        raise


def delete_role(con, role_id: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    role = get_role(con, role_id)
    if role is None:
        raise ValidationError("Role not found")
    if role["name"] in SYSTEM_ROLES:
        raise ValidationError("Built-in roles cannot be deleted")
    if role["members"]:
        raise ValidationError(f"Role is still assigned to {len(role['members'])} employee(s); remove it from them first")
    with _tx(con):
        con.execute("DELETE FROM employee_role WHERE role_id = ?", (role_id,))
        con.execute("DELETE FROM role WHERE role_id = ?", (role_id,))
        _log(con, actor, "role.deleted", f"Deleted role {role['name']}", {"role_id": role_id})


# ---------------------------------------------------------------- teams

def get_team(con, team_id: str) -> dict | None:
    row = con.execute(
        "SELECT t.*, h.name AS head_name FROM team t LEFT JOIN employee h ON h.emp_id = t.head_emp_id "
        "WHERE t.team_id = ?", (team_id,)).fetchone()
    if row is None:
        return None
    team = dict(row)
    team["members"] = [dict(r) for r in con.execute(
        "SELECT e.emp_id, e.name, e.status FROM team_member tm JOIN employee e USING (emp_id) "
        "WHERE tm.team_id = ? AND e.deleted_at IS NULL ORDER BY e.name COLLATE NOCASE", (team_id,))]
    return team


def list_teams(con) -> list[dict]:
    return [get_team(con, r["team_id"]) for r in con.execute(
        "SELECT team_id FROM team WHERE deleted_at IS NULL ORDER BY name COLLATE NOCASE")]


def _team_payload(con, data: dict) -> dict:
    out = {"name": clean_name(data.get("name"), "Team name"),
           "description": str(data.get("description") or "").strip(),
           "head": (data.get("head_emp_id") or "").strip() or None,
           "members": as_list(data.get("member_ids"))}
    errors = [f"Unknown employee {m}" for m in out["members"]
              if not _exists(con, "employee", "emp_id", m, "AND deleted_at IS NULL")]
    if out["head"] and not _exists(con, "employee", "emp_id", out["head"], "AND deleted_at IS NULL"):
        errors.append("Head does not exist")
    if errors:
        raise ValidationError(*errors)
    if out["head"] and out["head"] not in out["members"]:
        out["members"].append(out["head"])
    return out


def _save_team(con, team_id: str, p: dict, creating: bool, actor: dict) -> None:
    try:
        with _tx(con):
            if creating:
                con.execute("INSERT INTO team (team_id, name, description, head_emp_id, created_at) VALUES (?,?,?,?,?)",
                            (team_id, p["name"], p["description"], p["head"], utc_now()))
            else:
                con.execute("UPDATE team SET name=?, description=?, head_emp_id=? WHERE team_id=?",
                            (p["name"], p["description"], p["head"], team_id))
                con.execute("DELETE FROM team_member WHERE team_id = ?", (team_id,))
            for m in p["members"]:
                con.execute("INSERT INTO team_member VALUES (?, ?)", (team_id, m))
            _log(con, actor, "team.created" if creating else "team.updated",
                 f"{'Created' if creating else 'Updated'} team {p['name']}",
                 {"team_id": team_id, "head": p["head"], "members": p["members"]})
    except sqlite3.IntegrityError as err:
        if "team.name" in str(err):
            raise ValidationError(f"A team named '{p['name']}' already exists") from None
        raise


def create_team(con, data: dict, actor: dict = FOUNDER) -> str:
    authorize(con, actor, founder_only=True)
    team_id = _new_id("T")
    _save_team(con, team_id, _team_payload(con, data), True, actor)
    return team_id


def update_team(con, team_id: str, data: dict, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    if get_team(con, team_id) is None or con.execute(
            "SELECT deleted_at FROM team WHERE team_id = ?", (team_id,)).fetchone()[0]:
        raise ValidationError("Team not found")
    _save_team(con, team_id, _team_payload(con, data), False, actor)


def delete_team(con, team_id: str, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    team = get_team(con, team_id)
    if team is None or team["deleted_at"]:
        raise ValidationError("Team not found")
    with _tx(con):
        con.execute("UPDATE team SET deleted_at = ?, name = ? WHERE team_id = ?",
                    (utc_now(), f"{team['name']} [deleted {team_id}]", team_id))
        _log(con, actor, "team.deleted", f"Deleted team {team['name']}", {"team_id": team_id})


# ---------------------------------------------------------------- built-in employees

_CEO_INSTRUCTIONS = """# CEO

You coordinate the company for the Founder.
- Break the Founder's goals into tasks and delegate them to the right teams.
- Consolidate team reports, flag conflicts and open questions, and prepare proposals.
- You never approve external or critical actions: only the Founder does.
"""

_ADVISOR_INSTRUCTIONS = """# Advisor (Chief of Staff)

You work for the Founder, not for the CEO. You see everything and change nothing.
- Keep track of every active project, meeting, approval and deadline.
- Remind the Founder of anything pending, forgotten or overdue, and keep chasing until it is handled.
- Prepare reports and answer the Founder's questions.
- Challenge CEO proposals: assumptions, risks, missing evidence.
- You cannot execute work, assign tasks, approve actions or change permissions.
"""


def seed_system(con, home: Path) -> list[str]:
    """Create the built-in roles and the CEO and Advisor. Safe to run repeatedly."""
    created: list[str] = []
    role_ids: dict[str, str] = {}
    for name, desc, caps in (
        ("Chief Executive", "Coordinates the company, drafts proposals and delegates work.", {"assign": True, "execute": True}),
        ("Chief of Staff", "Tracks everything, reminds and reports. Read-only.", {}),
    ):
        row = con.execute("SELECT role_id FROM role WHERE name = ?", (name,)).fetchone()
        if row:
            role_ids[name] = row["role_id"]
            continue
        role_ids[name] = create_role(con, {"name": name, "description": desc, **caps}, SYSTEM)
        created.append(f"role:{name}")
    for key, name, role, instructions in (
        ("ceo", "CEO", "Chief Executive", _CEO_INSTRUCTIONS),
        ("advisor", "Advisor", "Chief of Staff", _ADVISOR_INSTRUCTIONS),
    ):
        if con.execute("SELECT 1 FROM employee WHERE system_key = ?", (key,)).fetchone():
            continue
        emp_id = _new_id("E")
        workspace = f"workspaces/employees/{emp_id}"
        with _tx(con):
            con.execute(
                "INSERT INTO employee (emp_id, name, kind, system_key, workspace, created_at, instructions) "
                "VALUES (?,?,?,?,?,?,?)", (emp_id, name, "system", key, workspace, utc_now(), instructions))
            con.execute("INSERT INTO employee_role VALUES (?, ?, ?)", (emp_id, role_ids[role], utc_now()))
            _log(con, SYSTEM, "employee.created", f"Created built-in employee {name}",
                 {"emp_id": emp_id, "system_key": key})
            sync_employee_files(con, home, emp_id)
        created.append(f"employee:{name}")
    return created

"""Acting as someone else, always on the record.

The Founder may act as any employee; the CEO only as members of teams it heads. While a session is open,
every event the real person writes carries *both* identities (``actor_*`` = who really did it,
``acting_as_*`` = whose name and role it was done under) and ``mode = impersonate``; the real identity is
never overwritten (see ``events.append``). By default the permissions of the identity acted as apply
(``org.authorize``); the Founder may confirm an explicit **override**, which is flagged on every event.
The guardrails (protected paths, secrets, kill switch, input guard) apply to everybody, always.
A CEO session is announced to the Founder as a notification.
"""
from __future__ import annotations

import contextlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import events, notify
from .org import FOUNDER, ValidationError

SESSION_HOURS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _real(actor: dict) -> dict:
    return {k: actor.get(k) for k in ("actor_type", "actor_id", "actor_name", "actor_designation")}


def can_impersonate(con: sqlite3.Connection, actor: dict, target_emp_id: str) -> None:
    """Raise PermissionError / ValidationError unless this actor may act as that employee."""
    target = con.execute("SELECT emp_id, status, deleted_at FROM employee WHERE emp_id = ?", (target_emp_id,)).fetchone()
    if target is None or target["deleted_at"]:
        raise ValidationError("Employee not found")
    if target["status"] == "archived":
        raise ValidationError("An archived employee cannot be impersonated")
    kind = actor.get("actor_type")
    if kind == "founder":
        return
    if kind == "employee":
        me = con.execute("SELECT system_key FROM employee WHERE emp_id = ?", (actor.get("actor_id"),)).fetchone()
        if me is None or me["system_key"] != "ceo":
            raise PermissionError("Only the Founder and the CEO can impersonate")
        if target_emp_id == actor["actor_id"]:
            raise ValidationError("The CEO cannot impersonate itself")
        managed = con.execute(
            "SELECT 1 FROM team t JOIN team_member m ON m.team_id = t.team_id WHERE t.head_emp_id = ? AND t.deleted_at IS NULL "
            "AND m.emp_id = ?", (actor["actor_id"], target_emp_id)).fetchone()
        if not managed:
            raise PermissionError("The CEO can only act as members of teams it heads")
        return
    raise PermissionError("Only the Founder and the CEO can impersonate")


def begin(con: sqlite3.Connection, actor: dict, target_emp_id: str, *, override: bool = False, confirm: bool = False,
          reason: str = "", now: datetime | None = None) -> str:
    """Open a session. ``override`` (Founder only) needs ``confirm=True``. Replaces any session this actor had open."""
    now = now or _now()
    can_impersonate(con, actor, target_emp_id)
    if override:
        if actor.get("actor_type") != "founder":
            raise PermissionError("Only the Founder can use the override")
        if not confirm:
            raise ValidationError("The Founder override must be confirmed explicitly")
    reason = events.scrub(reason or "")[:300]
    imp_id = f"IMP-{uuid.uuid4().hex[:8]}"
    target = events.identity(con, target_emp_id)
    for old in con.execute("SELECT imp_id FROM impersonation WHERE by_type = ? AND by_id IS ? AND ended_at IS NULL",
                           (actor["actor_type"], actor.get("actor_id"))).fetchall():
        end(con, old["imp_id"], actor, "replaced by a new session", now)
    con.execute("INSERT INTO impersonation (imp_id, by_type, by_id, by_name, target_emp_id, target_name, target_role, override, "
                "reason, started_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (imp_id, actor["actor_type"], actor.get("actor_id"), actor["actor_name"], target_emp_id, target["name"],
                 target["designation"], 1 if override else 0, reason, _iso(now), _iso(now + timedelta(hours=SESSION_HOURS))))
    events.append(con, type="impersonation.started", mode="impersonate", acting_as_emp_id=target["id"], acting_as_name=target["name"],
                  acting_as_role=target["designation"], summary=f"{actor['actor_name']} started acting as {target['name']}"
                  + (" with the FOUNDER OVERRIDE" if override else " (that role's permissions apply)"),
                  payload={"imp_id": imp_id, "override": override, "reason": reason}, **_real(actor))
    if actor["actor_type"] == "employee":                      # the CEO acting as someone is always shown to the Founder
        notify.upsert(con, key=f"impersonation:{imp_id}", category="impersonation", severity="warn",
                      title=f"{actor['actor_name']} is acting as {target['name']}",
                      detail=reason or "No reason given.", entity_type="impersonation", entity_id=imp_id, link="/logs?mode=impersonate")
    return imp_id


def end(con: sqlite3.Connection, imp_id: str, actor: dict, why: str = "ended", now: datetime | None = None) -> bool:
    row = con.execute("SELECT * FROM impersonation WHERE imp_id = ? AND ended_at IS NULL", (imp_id,)).fetchone()
    if row is None:
        return False
    con.execute("UPDATE impersonation SET ended_at = ?, ended_reason = ? WHERE imp_id = ?", ((_iso(now or _now())), why[:200], imp_id))
    notify.resolve_key(con, f"impersonation:{imp_id}")
    events.append(con, type="impersonation.ended", mode="impersonate", acting_as_emp_id=row["target_emp_id"], acting_as_name=row["target_name"],
                  acting_as_role=row["target_role"], summary=f"{row['by_name']} stopped acting as {row['target_name']} ({why})",
                  payload={"imp_id": imp_id, "override": bool(row["override"])}, **_real(actor if actor else FOUNDER))
    return True


def get(con: sqlite3.Connection, imp_id: str | None, now: datetime | None = None) -> dict | None:
    """An open, unexpired session (expired ones are closed on the spot)."""
    if not imp_id:
        return None
    row = con.execute("SELECT * FROM impersonation WHERE imp_id = ? AND ended_at IS NULL", (imp_id,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _iso(now or _now()):
        end(con, imp_id, {"actor_type": row["by_type"], "actor_id": row["by_id"], "actor_name": row["by_name"], "actor_designation": None},
            "expired", now)
        return None
    return dict(row)


def session_for(row: dict | None) -> dict | None:
    """The in-memory form used by ``events.set_acting``."""
    if row is None:
        return None
    return {"imp_id": row["imp_id"], "by_type": row["by_type"], "by_id": row["by_id"], "override": bool(row["override"]),
            "target": {"id": row["target_emp_id"], "name": row["target_name"], "designation": row["target_role"]}}


@contextlib.contextmanager
def acting(con: sqlite3.Connection, imp_id: str):
    """``with acting(con, imp_id): ...`` - events by the real actor inside carry both identities."""
    row = get(con, imp_id)
    if row is None:
        raise ValidationError("That impersonation session is not open")
    token = events.set_acting(session_for(row))
    try:
        yield row
    finally:
        events.reset_acting(token)


def history(con: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM impersonation ORDER BY started_at DESC LIMIT ?", (limit,))]

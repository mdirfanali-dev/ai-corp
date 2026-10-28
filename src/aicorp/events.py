"""Append-only, hash-chained audit log.

Each event stores the hash of the previous event, so changing or removing a past
row is detectable with :func:`verify_chain`. The log is tamper-evident, not
tamper-proof: someone with write access to the database file can rewrite the whole
chain. Record :func:`head` somewhere else (backups do this) to catch that.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .config import utc_now

GENESIS = "0" * 64
_acting: contextvars.ContextVar[dict | None] = contextvars.ContextVar("aicorp_acting", default=None)
_redactor: Callable[[str], str] | None = None


def set_acting(session: dict | None):
    """Mark the current request/thread as an impersonation session: ``by_type``, ``by_id`` (the real doer),
    ``target`` (identity acted as), ``override``, ``imp_id``. Every event the real doer writes meanwhile
    carries both identities. Returns a token for :func:`reset_acting`."""
    return _acting.set(session)


def reset_acting(token) -> None:
    _acting.reset(token)


def current_acting() -> dict | None:
    return _acting.get()


def set_redactor(fn: Callable[[str], str] | None) -> None:
    """Scrub secrets from every event before it is hashed and stored (see redact.install)."""
    global _redactor
    _redactor = fn


def scrub(value: Any) -> Any:
    """Apply the installed redactor to text (or nested data). Use before storing user-typed text."""
    return _scrub(value)


def _scrub(value: Any) -> Any:
    if _redactor is None:
        return value
    if isinstance(value, str):
        return _redactor(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value
HASHED_FIELDS = (
    "ts", "task_id", "actor_type", "actor_id", "actor_name", "actor_designation",
    "acting_as_emp_id", "acting_as_name", "acting_as_role", "mode", "type", "summary", "payload",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(prev_hash: str, record: dict[str, Any]) -> str:
    body = _canonical({k: record.get(k) for k in HASHED_FIELDS})
    return hashlib.sha256(f"{prev_hash}\n{body}".encode("utf-8")).hexdigest()


def head(con: sqlite3.Connection) -> tuple[int, str]:
    """Return (id, hash) of the newest event, or (0, GENESIS) when the log is empty."""
    row = con.execute("SELECT id, hash FROM event ORDER BY id DESC LIMIT 1").fetchone()
    return (row["id"], row["hash"]) if row else (0, GENESIS)


def identity(con: sqlite3.Connection, emp_id: str) -> dict[str, str | None]:
    """Snapshot of an employee's current name and roles, stored on the event."""
    emp = con.execute("SELECT emp_id, name FROM employee WHERE emp_id = ?", (emp_id,)).fetchone()
    if emp is None:
        raise KeyError(f"unknown employee {emp_id!r}")
    roles = [
        r["name"]
        for r in con.execute(
            "SELECT r.name FROM employee_role er JOIN role r USING (role_id) "
            "WHERE er.emp_id = ? ORDER BY r.name",
            (emp_id,),
        )
    ]
    return {"id": emp["emp_id"], "name": emp["name"], "designation": ", ".join(roles) or None}


def append(
    con: sqlite3.Connection,
    *,
    type: str,
    summary: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    actor_name: str = "system",
    actor_designation: str | None = None,
    acting_as_emp_id: str | None = None,
    acting_as_name: str | None = None,
    acting_as_role: str | None = None,
    mode: str = "normal",
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
    ts: str | None = None,
) -> int:
    """Append one event and return its id.

    Impersonation keeps the real actor in ``actor_*`` and the impersonated identity
    in ``acting_as_*``; the real identity is never overwritten.
    """
    session = _acting.get()
    if (session and mode == "normal" and not acting_as_name and actor_type == session["by_type"]
            and (actor_type == "founder" or actor_id == session["by_id"])):
        target = session["target"]
        acting_as_emp_id, acting_as_name, acting_as_role = target["id"], target["name"], target["designation"]
        mode = "impersonate"
        payload = {**(payload or {}), "imp_id": session["imp_id"], **({"override": True} if session["override"] else {})}
    if mode == "impersonate" and not acting_as_name:
        raise ValueError("impersonate events must name the identity being acted as")
    own_tx = not con.in_transaction
    if own_tx:
        con.execute("BEGIN IMMEDIATE")
    try:
        _, prev = head(con)
        record = {
            "ts": ts or utc_now(), "task_id": task_id, "actor_type": actor_type,
            "actor_id": actor_id, "actor_name": actor_name, "actor_designation": actor_designation,
            "acting_as_emp_id": acting_as_emp_id, "acting_as_name": acting_as_name,
            "acting_as_role": acting_as_role, "mode": mode, "type": type, "summary": _scrub(summary),
            "payload": _canonical(_scrub(payload or {})),
        }
        digest = compute_hash(prev, record)
        cur = con.execute(
            "INSERT INTO event (ts, task_id, actor_type, actor_id, actor_name, actor_designation, "
            "acting_as_emp_id, acting_as_name, acting_as_role, mode, type, summary, payload, "
            "prev_hash, hash) VALUES (:ts, :task_id, :actor_type, :actor_id, :actor_name, "
            ":actor_designation, :acting_as_emp_id, :acting_as_name, :acting_as_role, :mode, "
            ":type, :summary, :payload, :prev, :hash)",
            {**record, "prev": prev, "hash": digest},
        )
        if own_tx:
            con.execute("COMMIT")
        return int(cur.lastrowid)
    except Exception:
        if own_tx and con.in_transaction:
            con.execute("ROLLBACK")
        raise


def append_as(
    con: sqlite3.Connection,
    emp_id: str,
    *,
    acting_as: str | None = None,
    **kwargs: Any,
) -> int:
    """Append an event for an employee, snapshotting the current name and roles.

    With ``acting_as`` (an employee id) the event is recorded as an impersonation.
    """
    who = identity(con, emp_id)
    extra: dict[str, Any] = {}
    if acting_as:
        target = identity(con, acting_as)
        extra = {
            "acting_as_emp_id": target["id"], "acting_as_name": target["name"],
            "acting_as_role": target["designation"], "mode": kwargs.pop("mode", "impersonate"),
        }
    return append(
        con, actor_type="employee", actor_id=who["id"], actor_name=who["name"],
        actor_designation=who["designation"], **extra, **kwargs,
    )


@dataclass
class ChainResult:
    ok: bool
    count: int
    bad_id: int | None = None
    reason: str | None = None


def verify_chain(con: sqlite3.Connection) -> ChainResult:
    """Walk the whole log and check every link and hash."""
    prev = GENESIS
    count = 0
    for row in con.execute("SELECT * FROM event ORDER BY id"):
        count += 1
        if row["prev_hash"] != prev:
            return ChainResult(False, count, row["id"], "broken link (row missing or reordered)")
        expected = compute_hash(prev, dict(row))
        if row["hash"] != expected:
            return ChainResult(False, count, row["id"], "content does not match hash")
        prev = row["hash"]
    return ChainResult(True, count)


def verify_incremental(con: sqlite3.Connection) -> ChainResult:
    """Fast check for pages that show chain health: only events added since the last successful check are walked
    (the checkpoint row itself is re-checked too). A full walk (:func:`verify_chain`) still runs in the weekly
    maintenance and in the ``verify-chain`` command, so an edit to an old row is caught there."""
    row = con.execute("SELECT value FROM setting WHERE key = 'chain_checkpoint'").fetchone()
    start_id, prev = 0, GENESIS
    if row:
        import json as _json
        cp = _json.loads(row["value"])
        at = con.execute("SELECT hash FROM event WHERE id = ?", (cp["id"],)).fetchone()
        if at is not None and at["hash"] == cp["hash"]:
            start_id, prev = cp["id"], cp["hash"]
    count, last_id = 0, start_id
    for r in con.execute("SELECT * FROM event WHERE id > ? ORDER BY id", (start_id,)):
        count += 1
        if r["prev_hash"] != prev:
            return ChainResult(False, count, r["id"], "broken link (row missing or reordered)")
        if r["hash"] != compute_hash(prev, dict(r)):
            return ChainResult(False, count, r["id"], "content does not match hash")
        prev, last_id = r["hash"], r["id"]
    if last_id > start_id:
        import json as _json
        con.execute("INSERT INTO setting (key, value, updated_at) VALUES ('chain_checkpoint', ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (_json.dumps({"id": last_id, "hash": prev}), utc_now()))
    return ChainResult(True, last_id)               # ids are consecutive, so the last id is the number of events

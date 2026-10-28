"""The notification inbox: store, queries and the Founder's actions on it.

A notification is an *actionable attention item* (the audit log stays the record of truth). Each
one has a ``dedupe_key`` naming its cause; while the cause is open there is exactly one
notification for it, however often the cause is detected.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import events
from .config import utc_now
from .org import FOUNDER, SYSTEM, _tx, authorize

SEVERITIES = ("info", "warn", "critical")
CATEGORIES = ("blocked", "failed", "approval", "review", "guardrail", "system", "reminder", "report", "chat")
STATES = ("unread", "read", "snoozed", "resolved", "all")
DEFAULT_SETTINGS = {
    "routing": {"info": ["dashboard"], "warn": ["dashboard"], "critical": ["dashboard", "telegram", "email"]},
    "rate_per_hour": 20,
    "dashboard_url": "http://127.0.0.1:8765",
    "engine_down_after": 30,
    "monitor_enabled": True,
    "stale_days": 2,
    "project_stale_days": 5,
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "nag_daily_cap": 6,
    "away_threshold_minutes": 15,
    "ai_summary": False,
}


# ---------------------------------------------------------------- settings

def get_setting(con: sqlite3.Connection, key: str) -> Any:
    row = con.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_SETTINGS.get(key)))
    return json.loads(row["value"])


def set_setting(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute("INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at", (key, json.dumps(value), utc_now()))


# ---------------------------------------------------------------- creating and resolving

def upsert(con: sqlite3.Connection, *, key: str, category: str, severity: str, title: str, detail: str = "",
           entity_type: str | None = None, entity_id: str | None = None, task_id: str | None = None,
           link: str | None = None, channels: list[str] | None = None, actor: dict | None = None) -> tuple[int, bool]:
    """Create the notification for ``key`` or note that its cause is still there. Returns (id, created)."""
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    title, detail = events.scrub(title), events.scrub(detail)       # never store a credential in an alert
    now = utc_now()
    with _tx(con):
        row = con.execute("SELECT id FROM notification WHERE dedupe_key = ? AND resolved_at IS NULL", (key,)).fetchone()
        if row:
            con.execute("UPDATE notification SET repeat_count = repeat_count + 1, last_seen_at = ?, title = ?, "
                        "detail = ?, severity = ? WHERE id = ?", (now, title, detail, severity, row["id"]))
            return row["id"], False
        cur = con.execute(
            "INSERT INTO notification (category, severity, title, detail, entity_type, entity_id, created_at, "
            "dedupe_key, last_seen_at, task_id, link, channels) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (category, severity, title, detail, entity_type, entity_id, now, key, now, task_id, link,
             json.dumps(channels) if channels is not None else None))
        nid = cur.lastrowid
        con.execute("INSERT INTO notification_delivery (notification_id, channel, sent_at, status, attempted_at) "
                    "VALUES (?, 'dashboard', ?, 'sent', ?)", (nid, now, now))
        events.append(con, type="notification.created", summary=f"[{severity}] {title}", task_id=task_id,
                      payload={"notification_id": nid, "category": category, "key": key}, **(actor or SYSTEM))
    return nid, True


def resolve_key(con: sqlite3.Connection, key: str) -> bool:
    cur = con.execute("UPDATE notification SET resolved_at = ? WHERE dedupe_key = ? AND resolved_at IS NULL",
                      (utc_now(), key))
    return cur.rowcount > 0


def resolve_except(con: sqlite3.Connection, prefix: str, keep: set[str]) -> int:
    """Resolve every open notification whose key starts with ``prefix`` and is not in ``keep``."""
    rows = con.execute("SELECT id, dedupe_key FROM notification WHERE resolved_at IS NULL AND dedupe_key LIKE ?",
                       (prefix + "%",)).fetchall()
    gone = [r["id"] for r in rows if r["dedupe_key"] not in keep]
    now = utc_now()
    for nid in gone:
        con.execute("UPDATE notification SET resolved_at = ? WHERE id = ?", (now, nid))
    return len(gone)


def condition(con: sqlite3.Connection, key: str, active: bool, **fields: Any) -> bool:
    """Raise a notification while a condition holds and resolve it when it stops. Returns True if created."""
    if active:
        return upsert(con, key=key, **fields)[1]
    resolve_key(con, key)
    return False


# ---------------------------------------------------------------- reading

def _snoozed(now: str) -> str:
    return f"(n.snooze_until IS NOT NULL AND n.snooze_until > '{now}')"


def unread_count(con: sqlite3.Connection, now: str | None = None) -> int:
    """Unread, still-open, not snoozed: what the bell shows."""
    now = now or utc_now()
    return con.execute(
        f"SELECT count(*) FROM notification n WHERE n.read_at IS NULL AND n.resolved_at IS NULL AND NOT {_snoozed(now)}"
    ).fetchone()[0]


def _fts(text: str) -> str:
    return " ".join(f'"{w}"*' for w in re.findall(r"\w+", text, flags=re.UNICODE))


def _filters(*, category: str | None, severity: str | None, state: str, q: str, since: str | None,
             until: str | None, now: str) -> tuple[str, list]:
    where, args = [], []
    if category:
        where.append("n.category = ?")
        args.append(category)
    if severity:
        where.append("n.severity = ?")
        args.append(severity)
    if state == "unread":
        where.append(f"n.read_at IS NULL AND n.resolved_at IS NULL AND NOT {_snoozed(now)}")
    elif state == "read":
        where.append("n.read_at IS NOT NULL")
    elif state == "snoozed":
        where.append(_snoozed(now))
    elif state == "resolved":
        where.append("n.resolved_at IS NOT NULL")
    if since:
        where.append("n.created_at >= ?")
        args.append(since)
    if until:
        where.append("n.created_at <= ?")
        args.append(until)
    match = _fts(q)
    if match:
        where.append("n.id IN (SELECT rowid FROM notification_fts WHERE notification_fts MATCH ?)")
        args.append(match)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def list_notifications(con: sqlite3.Connection, *, category: str | None = None, severity: str | None = None,
                       state: str = "all", q: str = "", since: str | None = None, until: str | None = None,
                       limit: int = 50, offset: int = 0, now: str | None = None) -> list[dict]:
    now = now or utc_now()
    clause, args = _filters(category=category, severity=severity, state=state, q=q, since=since, until=until, now=now)
    rows = con.execute(
        f"SELECT n.* FROM notification n{clause} ORDER BY n.pinned DESC, "
        f"(n.resolved_at IS NULL) DESC, n.created_at DESC, n.id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["deliveries"] = deliveries(con, row["id"])
        item["snoozed"] = bool(row["snooze_until"] and row["snooze_until"] > now)
        out.append(item)
    return out


def ids_matching(con: sqlite3.Connection, **filters: Any) -> list[int]:
    now = filters.pop("now", None) or utc_now()
    clause, args = _filters(category=filters.get("category"), severity=filters.get("severity"),
                            state=filters.get("state", "all"), q=filters.get("q", ""),
                            since=filters.get("since"), until=filters.get("until"), now=now)
    return [r[0] for r in con.execute(f"SELECT n.id FROM notification n{clause}", args)]


def deliveries(con: sqlite3.Connection, notification_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT channel, status, sent_at, attempted_at, error FROM notification_delivery "
        "WHERE notification_id = ? ORDER BY id", (notification_id,))]


def get(con: sqlite3.Connection, notification_id: int, now: str | None = None) -> dict | None:
    row = con.execute("SELECT * FROM notification WHERE id = ?", (notification_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["deliveries"] = deliveries(con, notification_id)
    item["snoozed"] = bool(row["snooze_until"] and row["snooze_until"] > (now or utc_now()))
    return item


# ---------------------------------------------------------------- the Founder's actions

def _act(con, ids: list[int], sql: str, args: list, label: str, actor: dict, extra: dict | None = None) -> int:
    authorize(con, actor, founder_only=True)            # only the Founder acts on his own inbox
    ids = sorted({int(i) for i in ids})
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with _tx(con):
        changed = con.execute(f"{sql} WHERE id IN ({marks})", [*args, *ids]).rowcount
        events.append(con, type=f"notification.{label}", summary=f"{label.replace('_', ' ').capitalize()}: {changed} notification(s)",
                      payload={"ids": ids[:50], "count": changed, **(extra or {})}, **actor)
    return changed


def mark_read(con, ids: list[int], read: bool = True, actor: dict = FOUNDER) -> int:
    if read:
        return _act(con, ids, "UPDATE notification SET read_at = COALESCE(read_at, ?)", [utc_now()], "marked_read", actor)
    return _act(con, ids, "UPDATE notification SET read_at = NULL", [], "marked_unread", actor)


def _with_sources(con, ids: list[int]) -> list[int]:
    """A reminder about a notification carries that notification's id: acting on it acts on the original too."""
    ids = [int(i) for i in ids]
    if not ids:
        return ids
    marks = ",".join("?" * len(ids))
    sources = [int(r[0]) for r in con.execute(
        f"SELECT entity_id FROM notification WHERE id IN ({marks}) AND entity_type = 'notification' AND entity_id IS NOT NULL", ids)]
    return sorted(set(ids) | set(sources))


def snooze(con, ids: list[int], seconds: int, actor: dict = FOUNDER) -> int:
    ids = _with_sources(con, ids)
    until = (datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 60))).isoformat(timespec="microseconds")
    return _act(con, ids, "UPDATE notification SET snooze_until = ?", [until], "snoozed", actor, {"until": until})


def unsnooze(con, ids: list[int], actor: dict = FOUNDER) -> int:
    return _act(con, ids, "UPDATE notification SET snooze_until = NULL", [], "unsnoozed", actor)


def pin(con, ids: list[int], pinned: bool = True, actor: dict = FOUNDER) -> int:
    return _act(con, ids, "UPDATE notification SET pinned = ?", [1 if pinned else 0],
                "pinned" if pinned else "unpinned", actor)


def acknowledge(con, ids: list[int], actor: dict = FOUNDER) -> int:
    """Stop chasing me about this (the nag engine honours it); the item stays until resolved."""
    ids = _with_sources(con, ids)
    return _act(con, ids, "UPDATE notification SET ack_at = COALESCE(ack_at, ?), read_at = COALESCE(read_at, ?)",
                [utc_now(), utc_now()], "acknowledged", actor)

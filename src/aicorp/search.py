"""Full-text search over the audit log (SQLite FTS5)."""
from __future__ import annotations

import re
import sqlite3
from typing import Any


def _match_expression(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word is a prefix match, all must match."""
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{w}"*' for w in words)


def search_events(
    con: sqlite3.Connection,
    text: str = "",
    *,
    actor: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    mode: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Search events, newest first. ``actor`` matches the real or the impersonated identity.

    Matches in ``snippet`` are wrapped in \\x02 ... \\x03; callers turn them into markup after escaping."""
    where: list[str] = []
    args: list[Any] = []
    match = _match_expression(text)
    if match:
        sql = (
            "SELECT e.*, snippet(event_fts, 0, char(2), char(3), '…', 12) AS snippet "
            "FROM event_fts JOIN event e ON e.id = event_fts.rowid WHERE event_fts MATCH ?"
        )
        args.append(match)
    else:
        sql = "SELECT e.*, substr(e.summary, 1, 120) AS snippet FROM event e WHERE 1=1"
    if actor:
        where.append("(e.actor_name = ? COLLATE NOCASE OR e.acting_as_name = ? COLLATE NOCASE)")
        args += [actor, actor]
    if task_id:
        where.append("e.task_id = ?")
        args.append(task_id)
    if event_type:
        where.append("e.type = ?")
        args.append(event_type)
    if mode:
        where.append("e.mode = ?")
        args.append(mode)
    if since:
        where.append("e.ts >= ?")
        args.append(since)
    if until:
        where.append("e.ts <= ?")
        args.append(until)
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY e.id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args)]

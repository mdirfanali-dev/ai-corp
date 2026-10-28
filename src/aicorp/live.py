"""Read-only snapshot of the whole company for the live dashboard.

The browser never computes state: it asks for this snapshot (triggered by server-sent events and a
slow poll as safety net) and draws it. Everything comes from SQLite, so it is correct even when the
engine runs in another process or has just been killed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import events, killswitch, org


def engine_state(con: sqlite3.Connection) -> dict:
    """Is an engine running for this data folder? Based on its heartbeat and process."""
    row = con.execute("SELECT * FROM engine_status WHERE id = 1").fetchone()
    if row is None:
        return {"running": False}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    alive = age < 10 and psutil.pid_exists(row["pid"])
    return {"running": alive, "cap": row["cap"], "active": row["running"], "mode": row["mode"], "age": round(age)}


def public_event(row) -> dict:
    keys = ("id", "ts", "task_id", "actor_type", "actor_name", "actor_designation", "acting_as_name",
            "acting_as_role", "mode", "type", "summary")
    out = {k: row[k] for k in keys}
    out["payload"] = json.loads(row["payload"])
    return out


def _progress(con, task_id: str) -> int | None:
    row = con.execute("SELECT payload FROM event WHERE task_id = ? AND type = 'task.progress' "
                      "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["payload"]).get("percent") if row else None


def _card_task(task: dict | None) -> dict | None:
    if task is None:
        return None
    return {key: task[key] for key in ("task_id", "title", "status", "attempts", "progress", "block", "since")
            if key in task}


def snapshot(con: sqlite3.Connection, home: Path, feed_size: int = 30) -> dict:
    count = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
    engine = engine_state(con)
    kill = killswitch.info(home)

    running = {r["owner_emp_id"]: dict(r) for r in con.execute(
        "SELECT task_id, title, owner_emp_id, attempts, updated_at AS since, 'running' AS status "
        "FROM task WHERE status = 'running' AND deleted_at IS NULL")}
    for task in running.values():
        task["progress"] = _progress(con, task["task_id"])
    blocked: dict[str, dict] = {}
    for r in con.execute(
            "SELECT t.task_id, t.title, t.owner_emp_id, t.attempts, b.reason, b.detail, b.since, 'blocked' AS status "
            "FROM task t JOIN block b ON b.task_id = t.task_id AND b.resolved_at IS NULL "
            "WHERE t.status = 'blocked' AND t.deleted_at IS NULL ORDER BY b.since"):
        blocked.setdefault(r["owner_emp_id"], {**dict(r), "block": {"reason": r["reason"], "detail": r["detail"]}})
    waiting = {r["owner_emp_id"]: r["n"] for r in con.execute(
        "SELECT owner_emp_id, count(*) AS n FROM task WHERE status = 'assigned' AND deleted_at IS NULL "
        "AND owner_emp_id IS NOT NULL GROUP BY owner_emp_id")}
    needs_you: dict[str, list[dict]] = {}
    for r in con.execute("SELECT task_id, title, owner_emp_id, status FROM task WHERE deleted_at IS NULL "
                         "AND status IN ('awaiting_approval', 'review', 'failed') AND owner_emp_id IS NOT NULL "
                         "ORDER BY updated_at DESC"):
        needs_you.setdefault(r["owner_emp_id"], []).append(dict(r))

    cards: dict[str, dict] = {}
    for emp in org.list_employees(con):
        if emp["status"] == "archived":
            continue
        emp_id = emp["emp_id"]
        if emp_id in running:
            state = "working"
        elif emp_id in blocked:
            state = "blocked"
        elif emp["status"] == "paused":
            state = "paused"
        else:
            state = "idle"
        cards[emp_id] = {
            "emp_id": emp_id, "name": emp["name"], "kind": emp["kind"], "system_key": emp["system_key"],
            "state": state, "roles": [r["name"] for r in emp["roles"]], "reports_to": emp["manager"],
            "task": _card_task(running.get(emp_id) or blocked.get(emp_id)),
            "waiting": waiting.get(emp_id, 0), "needs_you": needs_you.get(emp_id, [])[:3],
            "expires_at": emp["expires_at"],
        }

    teams, in_team = [], set()
    for team in org.list_teams(con):
        members = [m["emp_id"] for m in team["members"] if m["emp_id"] in cards]
        in_team.update(members)
        teams.append({"team_id": team["team_id"], "name": team["name"], "head": team["head_name"],
                      "head_emp_id": team["head_emp_id"], "members": members,
                      "working": sum(1 for m in members if cards[m]["state"] == "working"),
                      "blocked": sum(1 for m in members if cards[m]["state"] == "blocked")})
    leaders = [e for e, c in cards.items() if c["system_key"]]
    bench = [e for e, c in cards.items() if e not in in_team and not c["system_key"]]

    open_tasks = [dict(r) for r in con.execute(
        "SELECT t.task_id, t.title, t.status, e.name AS owner FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
        "WHERE t.deleted_at IS NULL AND t.status NOT IN ('done', 'rejected') ORDER BY t.updated_at DESC LIMIT 80")]
    nodes = {t["task_id"]: t for t in open_tasks}
    edges = []
    if nodes:
        marks = ",".join("?" * len(nodes))
        for r in con.execute(f"SELECT task_id, depends_on FROM task_dependency WHERE task_id IN ({marks}) "
                             f"OR depends_on IN ({marks})", [*nodes, *nodes]):
            for tid in (r["task_id"], r["depends_on"]):
                if tid not in nodes:
                    extra = con.execute("SELECT t.task_id, t.title, t.status, e.name AS owner FROM task t "
                                        "LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.task_id = ?",
                                        (tid,)).fetchone()
                    if extra:
                        nodes[tid] = dict(extra)
            if r["task_id"] in nodes and r["depends_on"] in nodes:
                edges.append({"from": r["depends_on"], "to": r["task_id"]})
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    graph_nodes = [n for n in nodes.values() if n["task_id"] in connected or n["status"] != "assigned"][:80]
    ids = {n["task_id"] for n in graph_nodes}
    graph_edges = [e for e in edges if e["from"] in ids and e["to"] in ids]

    feed = [public_event(r) for r in con.execute("SELECT * FROM event ORDER BY id DESC LIMIT ?", (feed_size,))]
    n_running = count("SELECT count(*) FROM task WHERE status = 'running' AND deleted_at IS NULL")
    stats = {
        "running": n_running,
        "cap": engine.get("cap") if engine["running"] else None,
        "ram": round(psutil.virtual_memory().percent),
        "queue": count("SELECT count(*) FROM task WHERE status = 'assigned' AND deleted_at IS NULL"),
        "blocked": count("SELECT count(*) FROM task WHERE status = 'blocked' AND deleted_at IS NULL"),
        "needs_you": count("SELECT count(*) FROM task WHERE status IN ('blocked', 'failed', 'awaiting_approval') "
                           "AND deleted_at IS NULL"),
        "employees": len(cards),
    }
    return {"stats": stats, "engine": engine, "kill": kill, "cards": cards, "teams": teams, "leaders": leaders,
            "bench": bench, "graph": {"nodes": graph_nodes, "edges": graph_edges}, "feed": feed,
            "last_event": events.head(con)[0]}

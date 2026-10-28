"""Housekeeping that keeps a long-running company healthy, without ever deleting audit data.

* The audit log (``event``) is append-only and is **kept forever** (its triggers refuse update and delete); the size
  report projects how big it gets.
* Bulky operational tables are pruned (old job runs, delivery attempts of resolved notifications); old run logs are
  compressed, not deleted; files are never removed here - the trash is purged only by the Founder.
* The database is checked (integrity + audit hash chain) and a failure is raised as a critical notification.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

from . import events, notify, trash
from .config import db_path

JOB_RUN_KEEP_DAYS = 90
JOB_RUN_FAIL_KEEP_DAYS = 180
DELIVERY_KEEP_DAYS = 90
RUNLOG_GZIP_AFTER_DAYS = 30
VACUUM_MIN_BYTES = 20 * 1024 * 1024
VACUUM_MIN_FREE_RATIO = 0.30


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def sizes(con: sqlite3.Connection, home: Path) -> dict:
    path = db_path(home)
    db_bytes = path.stat().st_size if path.exists() else 0
    wal = Path(str(path) + "-wal")
    n = con.execute("SELECT count(*) FROM event").fetchone()[0]
    first = con.execute("SELECT min(ts) FROM event").fetchone()[0]
    event_bytes = con.execute("SELECT COALESCE(sum(length(summary) + length(payload) + 200), 0) FROM event").fetchone()[0]
    per_event = round(event_bytes / n) if n else 0
    days = max((datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds() / 86400, 1) if first else 1
    per_day = n / days
    trash_items = trash.list_trash(home)
    trash_bytes = sum(_tree_size(home / e["trashed"]) for e in trash_items)
    now = datetime.now(timezone.utc)
    expired = [e for e in trash_items if datetime.fromisoformat(e["expires_at"]) <= now]
    return {"db_bytes": db_bytes, "wal_bytes": wal.stat().st_size if wal.exists() else 0, "events": n, "bytes_per_event": per_event,
            "events_per_day": round(per_day, 1), "events_year_mb": round(per_day * 365 * per_event / 1e6, 1),
            "trash_items": len(trash_items), "trash_bytes": trash_bytes, "trash_expired": len(expired),
            "disk_free_bytes": shutil.disk_usage(home).free}


def _tree_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def _engine_running(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT pid, heartbeat_at FROM engine_status WHERE id = 1").fetchone()
    return bool(row and psutil.pid_exists(row["pid"]))


def run(con: sqlite3.Connection, home: Path, now: datetime | None = None) -> dict:
    """One maintenance pass. Returns what it did and found (also kept in the setting ``maintenance_last``)."""
    now = now or datetime.now(timezone.utc)
    report: dict = {"at": _iso(now), "problems": [], "did": []}
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    report["integrity"] = ok
    notify.condition(con, "maintenance:integrity", ok != "ok", category="system", severity="critical",
                     title="The database failed its integrity check", detail=f"SQLite says: {ok[:200]}. Stop the engine and restore the latest backup (Settings > Maintenance).",
                     link="/maintenance")
    if ok != "ok":
        report["problems"].append("integrity check failed")
    chain = events.verify_chain(con)
    report["chain_ok"] = chain.ok
    notify.condition(con, "maintenance:chain", not chain.ok, category="guardrail", severity="critical",
                     title="The audit log has been tampered with or damaged", detail=f"Hash chain broken at event {chain.bad_id}. Compare with the latest backup.",
                     link="/maintenance")
    if not chain.ok:
        report["problems"].append("audit chain broken")
    cut = _iso(now - timedelta(days=JOB_RUN_KEEP_DAYS))
    cut_fail = _iso(now - timedelta(days=JOB_RUN_FAIL_KEEP_DAYS))
    removed = con.execute("DELETE FROM job_run WHERE (status = 'ok' AND started_at < ?) OR started_at < ?", (cut, cut_fail)).rowcount
    removed_d = con.execute("DELETE FROM notification_delivery WHERE notification_id IN (SELECT id FROM notification WHERE resolved_at IS NOT NULL AND resolved_at < ?)",
                            (_iso(now - timedelta(days=DELIVERY_KEEP_DAYS)),)).rowcount
    if removed or removed_d:
        report["did"].append(f"pruned {removed} old job run(s) and {removed_d} old delivery record(s)")
    zipped = _gzip_old_run_logs(home, now)
    if zipped:
        report["did"].append(f"compressed {zipped} old run log(s)")
    if con.in_transaction:
        con.commit()
    size = sizes(con, home)
    page_count = con.execute("PRAGMA page_count").fetchone()[0]
    free = con.execute("PRAGMA freelist_count").fetchone()[0]
    if size["db_bytes"] >= VACUUM_MIN_BYTES and page_count and free / page_count >= VACUUM_MIN_FREE_RATIO and not _engine_running(con):
        con.execute("VACUUM")
        report["did"].append("vacuumed the database")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA optimize")
    if size["trash_expired"]:
        notify.upsert(con, key="maintenance:trash", category="system", severity="info", title=f"{size['trash_expired']} item(s) in the trash are past their retention date",
                      detail="Nothing is deleted automatically. Review and purge them at Maintenance when you are sure.", link="/maintenance")
    else:
        notify.resolve_key(con, "maintenance:trash")
    report["sizes"] = size
    notify.set_setting(con, "maintenance_last", report)
    events.append(con, type="maintenance.ran", summary="Maintenance: " + ("; ".join(report["did"] + report["problems"]) or "all fine"), payload={"problems": report["problems"]},
                  actor_type="system", actor_name="system")
    return report


def _gzip_old_run_logs(home: Path, now: datetime) -> int:
    folder = home / "memory" / "run-logs"
    if not folder.is_dir():
        return 0
    n = 0
    limit = now.timestamp() - RUNLOG_GZIP_AFTER_DAYS * 86400
    for path in folder.iterdir():
        if path.is_file() and path.suffix != ".gz" and path.stat().st_mtime < limit:
            target = path.with_name(path.name + ".gz")
            with open(path, "rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()                               # the content lives on in the .gz; nothing is lost
            n += 1
    return n

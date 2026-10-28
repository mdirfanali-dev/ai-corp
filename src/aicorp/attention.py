"""What needs the Founder, worked out from the current state of the database.

``sync`` is idempotent: run it as often as you like and every cause has exactly one open
notification, which is resolved automatically when the cause goes away. Because it reads
state (not events), nothing is lost if a process dies between "it happened" and "we told
the Founder".
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import killswitch, notify, tasks
from .config import utc_now
from .org import SYSTEM, _tx

# reason -> (severity, short label, what the Founder should do)
REASONS = {
    "CAPTCHA": ("critical", "CAPTCHA", "Open the browser window, solve the CAPTCHA, then press Resume."),
    "LOGIN_REQUIRED": ("critical", "login needed", "Sign in to the service in the agent's browser, then press Resume."),
    "UNKNOWN_UI": ("critical", "page looks different", "The page did not look as expected. Check it, then press Resume or take over."),
    "PERMISSION_DENIED": ("critical", "blocked by a safety rule", "The task was stopped by a guardrail. Review it before continuing."),
    "SYSTEM_ERROR": ("critical", "system error", "Something went wrong inside AICorp. See the log, then retry."),
    "DISK_LOW": ("critical", "disk almost full", "Free some disk space, then press Resume."),
    "NEEDS_INPUT": ("critical", "needs your input", "The task needs a decision or information from you."),
    "NEEDS_APPROVAL": ("warn", "needs approval", "Approve or reject it on the task page."),
    "USAGE_LIMIT": ("warn", "usage limit reached", "Claude's usage limit was hit. Work resumes automatically after the limit resets; you can also resume it yourself."),
    "TIMEOUT": ("warn", "took too long", "The task ran out of time or turns. Resume it, break it into smaller tasks, or cancel."),
    "RUNNER_ERROR": ("warn", "worker error", "The worker program failed. Retry, or check the log."),
    "RAM_PRESSURE": ("warn", "memory low", "The computer is short of memory. Close other programs, then press Resume."),
}
DISK_WARN_GB, DISK_CRITICAL_GB = 5.0, 1.0
DISK_WARN_RATIO, DISK_CRITICAL_RATIO = 0.05, 0.02
USAGE_BASE_WAIT = 1800          # seconds; doubles per repeated usage-limit block, capped
USAGE_MAX_WAIT = 6 * 3600


def disk_level(home: Path, probe: Callable[[Path], tuple[int, int]] | None = None) -> tuple[str, float]:
    """('ok'|'warn'|'critical', free GB) for the disk that holds the data folder."""
    total, free = (probe or (lambda p: shutil.disk_usage(p)[::2]))(home)
    free_gb = free / 1e9
    ratio = free / total if total else 1.0
    if free_gb < DISK_CRITICAL_GB or ratio < DISK_CRITICAL_RATIO:
        return "critical", free_gb
    if free_gb < DISK_WARN_GB or ratio < DISK_WARN_RATIO:
        return "warn", free_gb
    return "ok", free_gb


def parse_resume(detail: str | None, now: datetime) -> datetime | None:
    """Best effort: read 'resets in 2 hours' / 'try again at 5pm' out of a usage-limit message.
    The real message format is not documented, so this may return None; callers then back off."""
    text = (detail or "").lower()
    m = re.search(r"\bin\s+(\d+)\s*(minute|min|hour|hr)s?\b", text)
    if m:
        amount = int(m.group(1))
        return now + (timedelta(hours=amount) if m.group(2).startswith(("hour", "hr")) else timedelta(minutes=amount))
    m = re.search(r"\b(?:at|until|after)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        local = now.astimezone()
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)
    return None


def usage_resume_time(con: sqlite3.Connection, task_id: str, detail: str | None, now: datetime) -> datetime:
    parsed = parse_resume(detail, now)
    if parsed and parsed > now:
        return min(parsed + timedelta(seconds=30), now + timedelta(seconds=USAGE_MAX_WAIT))
    earlier = con.execute("SELECT count(*) FROM block WHERE task_id = ? AND reason = 'USAGE_LIMIT'", (task_id,)).fetchone()[0]
    wait = min(USAGE_BASE_WAIT * 2 ** max(earlier - 1, 0), USAGE_MAX_WAIT)
    return now + timedelta(seconds=wait)


def auto_resume(con: sqlite3.Connection, now: datetime) -> list[str]:
    """Put tasks that were parked by a usage limit back in the queue once their wait is over."""
    due = con.execute(
        "SELECT t.task_id, t.title, b.block_id FROM task t JOIN block b ON b.task_id = t.task_id AND b.resolved_at IS NULL "
        "WHERE t.status = 'blocked' AND b.reason = 'USAGE_LIMIT' AND b.resume_at IS NOT NULL AND b.resume_at <= ?",
        (now.astimezone(timezone.utc).isoformat(timespec="microseconds"),)).fetchall()
    out = []
    for row in due:
        with _tx(con):
            con.execute("UPDATE block SET resolved_at = ?, resolved_by = 'system' WHERE block_id = ?", (utc_now(), row["block_id"]))
            tasks.transition(con, row["task_id"], "assigned", actor=SYSTEM, event_type="task.block_resolved",
                             summary=f"Trying {row['title']} again after the usage-limit wait", payload={"auto": True, "reason": "USAGE_LIMIT"},
                             sets={"lease_owner": None, "lease_expires_at": None, "not_before": None})
        tasks.free_owner(con, con.execute("SELECT owner_emp_id FROM task WHERE task_id = ?", (row["task_id"],)).fetchone()[0])
        out.append(row["task_id"])
    return out


def sync(con: sqlite3.Connection, home: Path, disk_probe=None) -> list[int]:
    """Create/refresh/resolve notifications from the current state. Returns ids of new ones."""
    created: list[int] = []

    def raise_(key, **fields):
        nid, new = notify.upsert(con, key=key, **fields)
        if new:
            created.append(nid)

    keep: set[str] = set()
    for r in con.execute(
            "SELECT b.block_id, b.reason, b.detail, b.since, t.task_id, t.title, e.name AS owner "
            "FROM block b JOIN task t ON t.task_id = b.task_id LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
            "WHERE b.resolved_at IS NULL AND t.status = 'blocked' AND t.deleted_at IS NULL"):
        severity, label, hint = REASONS.get(r["reason"], ("warn", r["reason"].lower(), ""))
        detail = (r["detail"] or "").strip()
        if r["reason"] == "NEEDS_INPUT" and detail.startswith(tasks.DEPENDENCY_PREFIX):
            severity, label, hint = "info", "waiting for a failed task", "It continues automatically once that task is fixed."
            detail = detail[len(tasks.DEPENDENCY_PREFIX):]
        key = f"block:{r['block_id']}"
        keep.add(key)
        raise_(key, category="blocked", severity=severity, title=f"{r['owner'] or 'A task'} is blocked: {label}",
               detail=f"Task '{r['title']}'. {hint} {detail}".strip(), entity_type="task", entity_id=r["task_id"],
               task_id=r["task_id"], link=f"/tasks/{r['task_id']}")
    notify.resolve_except(con, "block:", keep)

    keep = set()
    for r in con.execute("SELECT t.task_id, t.title, t.attempts, e.name AS owner FROM task t "
                         "LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.status = 'failed' AND t.deleted_at IS NULL"):
        key = f"failed:{r['task_id']}"
        keep.add(key)
        raise_(key, category="failed", severity="critical", title=f"Task failed: {r['title']}",
               detail=f"{r['owner'] or 'The owner'} could not finish it after {r['attempts']} attempt(s). Retry it or cancel it.",
               entity_type="task", entity_id=r["task_id"], task_id=r["task_id"], link=f"/tasks/{r['task_id']}")
    notify.resolve_except(con, "failed:", keep)

    keep = set()
    for r in con.execute("SELECT a.approval_id, a.tier, t.task_id, t.title, t.result_summary FROM approval a "
                         "JOIN task t ON t.task_id = a.task_id WHERE a.decided_at IS NULL AND t.status = 'awaiting_approval'"):
        key = f"approval:{r['approval_id']}"
        keep.add(key)
        raise_(key, category="approval", severity="warn", title=f"Approval needed: {r['title']}",
               detail=f"Tier {r['tier']} work is ready for your decision. {r['result_summary'] or ''}".strip(),
               entity_type="task", entity_id=r["task_id"], task_id=r["task_id"], link=f"/tasks/{r['task_id']}")
    notify.resolve_except(con, "approval:", keep)

    keep = set()
    for r in con.execute("SELECT t.task_id, t.title, e.name AS owner, "
                         "(SELECT count(*) FROM event ev WHERE ev.task_id = t.task_id AND ev.type = 'guard.blocked') AS attempts "
                         "FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.status = 'review' AND t.deleted_at IS NULL"):
        key = f"review:{r['task_id']}"
        keep.add(key)
        if r["attempts"]:
            raise_(key, category="guardrail", severity="warn", title=f"{r['owner'] or 'An employee'} tried {r['attempts']} forbidden action(s)",
                   detail=f"All were blocked. Task '{r['title']}' needs your review before it counts as done.",
                   entity_type="task", entity_id=r["task_id"], task_id=r["task_id"], link=f"/tasks/{r['task_id']}")
        else:
            raise_(key, category="review", severity="info", title=f"Ready for review: {r['title']}",
                   detail=f"{r['owner'] or 'The owner'} finished and asks for a review.", entity_type="task",
                   entity_id=r["task_id"], task_id=r["task_id"], link=f"/tasks/{r['task_id']}")
    notify.resolve_except(con, "review:", keep)

    info = killswitch.info(home)
    if notify.condition(con, "killswitch", info is not None, category="system", severity="critical",
                        title="Kill switch is engaged", detail="All workers are stopped and nothing will start. "
                        f"{(info or {}).get('reason', '')} Release it on the Tasks page when you are ready.".strip(),
                        link="/tasks"):
        created.append(con.execute("SELECT id FROM notification WHERE dedupe_key = 'killswitch' AND resolved_at IS NULL").fetchone()[0])

    level, free_gb = disk_level(home, disk_probe)
    if notify.condition(con, "disk", level != "ok", category="system",
                        severity="critical" if level == "critical" else "warn", title="Disk space is low",
                        detail=f"Only {free_gb:.1f} GB free." + (" New work is paused." if level == "critical" else ""),
                        link="/attention"):
        created.append(con.execute("SELECT id FROM notification WHERE dedupe_key = 'disk' AND resolved_at IS NULL").fetchone()[0])
    return created


SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


def worklist(con: sqlite3.Connection, home: Path) -> list[dict]:
    """Everything that currently needs the Founder, most urgent and oldest first (Attention Center)."""
    items: list[dict] = []
    for r in con.execute(
            "SELECT b.block_id, b.reason, b.detail, b.since, b.resume_at, t.task_id, t.title, e.name AS owner "
            "FROM block b JOIN task t ON t.task_id = b.task_id LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
            "WHERE b.resolved_at IS NULL AND t.status = 'blocked' AND t.deleted_at IS NULL"):
        severity, label, hint = REASONS.get(r["reason"], ("warn", r["reason"].lower(), ""))
        detail = (r["detail"] or "").strip()
        if r["reason"] == "NEEDS_INPUT" and detail.startswith(tasks.DEPENDENCY_PREFIX):
            severity, label, hint = "info", "waiting for a failed task", "It continues automatically once that task is fixed."
            detail = detail[len(tasks.DEPENDENCY_PREFIX):]
        items.append({"kind": "blocked", "severity": severity, "who": r["owner"] or "—", "title": r["title"], "label": label,
                      "hint": hint, "detail": detail, "since": r["since"], "task_id": r["task_id"], "reason": r["reason"],
                      "resume_at": r["resume_at"], "actions": ["resolve", "cancel"]})
    for r in con.execute("SELECT t.task_id, t.title, t.attempts, t.updated_at, e.name AS owner FROM task t "
                         "LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.status = 'failed' AND t.deleted_at IS NULL"):
        items.append({"kind": "failed", "severity": "critical", "who": r["owner"] or "—", "title": r["title"], "label": "failed",
                      "hint": "It failed after every retry. Retry it or cancel it.", "detail": f"{r['attempts']} attempt(s)",
                      "since": r["updated_at"], "task_id": r["task_id"], "reason": "FAILED", "actions": ["retry", "cancel"]})
    for r in con.execute("SELECT a.tier, a.requested_at, t.task_id, t.title, t.result_summary, e.name AS owner FROM approval a "
                         "JOIN task t ON t.task_id = a.task_id LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
                         "WHERE a.decided_at IS NULL AND t.status = 'awaiting_approval'"):
        items.append({"kind": "approval", "severity": "warn", "who": r["owner"] or "—", "title": r["title"], "label": f"approval (tier {r['tier']})",
                      "hint": "Open the task to approve, ask for changes or reject.", "detail": r["result_summary"] or "",
                      "since": r["requested_at"], "task_id": r["task_id"], "reason": "APPROVAL", "actions": ["open"]})
    for r in con.execute("SELECT t.task_id, t.title, t.updated_at, e.name AS owner, "
                         "(SELECT count(*) FROM event ev WHERE ev.task_id = t.task_id AND ev.type = 'guard.blocked') AS attempts "
                         "FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id WHERE t.status = 'review' AND t.deleted_at IS NULL"):
        items.append({"kind": "review", "severity": "warn" if r["attempts"] else "info", "who": r["owner"] or "—", "title": r["title"],
                      "label": "guardrail attempts" if r["attempts"] else "ready for review",
                      "hint": (f"{r['attempts']} forbidden action(s) were attempted and blocked. " if r["attempts"] else "") + "Open the task to review it.",
                      "detail": "", "since": r["updated_at"], "task_id": r["task_id"], "reason": "REVIEW", "actions": ["open"]})
    info = killswitch.info(home)
    if info is not None:
        items.append({"kind": "system", "severity": "critical", "who": "System", "title": "Kill switch engaged", "label": "all work stopped",
                      "hint": "Release it when you are ready for work to continue.", "detail": info.get("reason", ""),
                      "since": info.get("at", ""), "task_id": None, "reason": "KILLSWITCH", "actions": ["release"]})
    for r in con.execute("SELECT * FROM notification WHERE resolved_at IS NULL AND category = 'system' AND dedupe_key <> 'killswitch'"):
        items.append({"kind": "system", "severity": r["severity"], "who": "System", "title": r["title"], "label": "system",
                      "hint": "", "detail": r["detail"] or "", "since": r["created_at"], "task_id": None, "reason": "SYSTEM", "actions": []})
    until = notify.get_setting(con, "usage_pause_until")
    if until and until > utc_now():
        items.append({"kind": "system", "severity": "warn", "who": "System", "title": "Waiting for the Claude usage limit to reset",
                      "label": "paused", "hint": "No new work starts until then.", "detail": f"Next attempt around {until[11:16]} UTC.",
                      "since": "", "task_id": None, "reason": "USAGE_PAUSE", "actions": []})
    items.sort(key=lambda i: (SEVERITY_ORDER.get(i["severity"], 3), i["since"] or "~"))
    return items

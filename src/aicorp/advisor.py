"""The Advisor: the Founder's chief of staff.

It looks at everything, reminds, chases and reports, and it changes nothing about the work itself.
All of this is deterministic code (no AI); an AI-written summary is an optional extra that runs as an
ordinary read-only task. Everything it writes goes to its own outputs only: notifications, reports
and schedules. What it does is attributed to the Advisor employee in the audit log.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path

from . import attention, backup, events, notify, projects, scheduler, tasks, trash
from .org import SYSTEM
from .scheduler import JobContext, register

NAGGED_CATEGORIES = ("approval", "blocked", "failed", "review", "guardrail", "reminder")
FIRST_NUDGE_HOURS = {"approval": 4, "blocked": 4, "failed": 4}
LADDER = [  # level, hours after creation (None = category specific), channels, extras
    {"level": 1, "hours": None, "channels": ["telegram"]},
    {"level": 2, "hours": 48, "channels": ["telegram"], "severity_up": True},
    {"level": 3, "hours": 72, "channels": ["telegram", "email"], "overdue": True},
    {"level": 4, "hours": 120, "channels": ["telegram"], "repeat_hours": 24},
]
FORGOTTEN_AFTER_HOURS = 72
OPEN_TASK = ("created", "assigned", "running", "blocked", "review", "awaiting_approval", "failed")


# ---------------------------------------------------------------- small helpers

def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dt(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def advisor_actor(con) -> dict:
    row = con.execute("SELECT emp_id, name FROM employee WHERE system_key = 'advisor'").fetchone()
    if row is None:
        return SYSTEM
    return {"actor_type": "employee", "actor_id": row["emp_id"], "actor_name": row["name"], "actor_designation": "Chief of Staff"}


def local_text(iso_text: str | None, tz: tzinfo | None = None, fmt: str = "%a %d %b %H:%M") -> str:
    return _dt(iso_text).astimezone(tz).strftime(fmt) if iso_text else "—"


def human_age(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def in_quiet_hours(now: datetime, quiet: dict, tz: tzinfo | None = None) -> bool:
    try:
        start = dtime.fromisoformat(quiet["start"])
        end = dtime.fromisoformat(quiet["end"])
    except (KeyError, ValueError, TypeError):
        return False
    t = now.astimezone(tz).time()
    return (start <= t < end) if start <= end else (t >= start or t < end)


def _upsert(con, adv, **fields):
    return notify.upsert(con, actor=adv, **fields)


# ---------------------------------------------------------------- scan rules (reminders)

def scan(con: sqlite3.Connection, now: datetime, tz: tzinfo | None = None) -> dict:
    """Find what is overdue, stale, forgotten, upcoming or stalled; one reminder per cause, cleared when the cause is gone."""
    adv = advisor_actor(con)
    stale_days = float(notify.get_setting(con, "stale_days"))
    project_days = float(notify.get_setting(con, "project_stale_days"))
    keep: set[str] = set()
    counts: dict[str, int] = {}

    def raise_(kind, key, **fields):
        keep.add(key)
        counts[kind] = counts.get(kind, 0) + 1
        _upsert(con, adv, key=key, category="reminder", **fields)

    for t in con.execute("SELECT t.*, e.name AS owner FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
                         "WHERE t.deleted_at IS NULL AND t.status NOT IN ('done', 'rejected')"):
        link, base = f"/tasks/{t['task_id']}", dict(entity_type="task", entity_id=t["task_id"], task_id=t["task_id"])
        due = _dt(t["due_at"])
        age = now - _dt(t["updated_at"])
        who = t["owner"] or "unassigned"
        if due and due < now:
            days = (now - due).total_seconds() / 86400
            raise_("overdue", f"adv:overdue:{t['task_id']}", severity="critical" if days >= 3 else "warn",
                   title=f"Overdue: {t['title']}", detail=f"Was due {local_text(t['due_at'], tz)} ({human_age(now - due)} ago). Owner: {who}. Status: {t['status'].replace('_', ' ')}.", link=link, **base)
        elif due and due - now <= timedelta(hours=24):
            raise_("due_soon", f"adv:duesoon:{t['task_id']}", severity="info", title=f"Due soon: {t['title']}",
                   detail=f"Due {local_text(t['due_at'], tz)}. Owner: {who}.", link=link, **base)
        if age.total_seconds() >= stale_days * 86400 and t["status"] != "created":
            raise_("stale", f"adv:stale:{t['task_id']}", severity="warn", title=f"No progress for {human_age(age)}: {t['title']}",
                   detail=f"{who} — status {t['status'].replace('_', ' ')}, last change {local_text(t['updated_at'], tz)}.", link=link, **base)

    for m in con.execute("SELECT * FROM meeting WHERE deleted_at IS NULL AND status = 'scheduled' AND scheduled_at IS NOT NULL"):
        when = _dt(m["scheduled_at"])
        link, base = f"/meetings/{m['meeting_id']}", dict(entity_type="meeting", entity_id=m["meeting_id"])
        if now <= when <= now + timedelta(hours=24):
            raise_("meeting", f"adv:meeting:{m['meeting_id']}", severity="info", title=f"Meeting {local_text(m['scheduled_at'], tz)}: {m['title']}",
                   detail=("Agenda is empty." if not (m["agenda"] or "").strip() else "Agenda is ready.") + " A preparation note is written an hour or so before.", link=link, **base)
        elif when < now - timedelta(hours=1):
            raise_("missed", f"adv:missed:{m['meeting_id']}", severity="warn", title=f"Did this meeting happen? {m['title']}",
                   detail=f"It was scheduled for {local_text(m['scheduled_at'], tz)}. Mark it held (and add minutes) or cancelled.", link=link, **base)

    for d in con.execute("SELECT * FROM decision WHERE status = 'open' AND follow_up_by IS NOT NULL AND follow_up_by < ?", (_iso(now),)):
        raise_("decision", f"adv:decision:{d['decision_id']}", severity="warn", title=f"No follow-up yet: {d['title']}",
               detail=f"The follow-up was due {local_text(d['follow_up_by'], tz)}. Mark it followed up or dropped.", link="/projects",
               entity_type="decision", entity_id=d["decision_id"])

    for e in con.execute("SELECT * FROM employee WHERE kind = 'temporary' AND deleted_at IS NULL AND status <> 'archived' AND expires_at IS NOT NULL"):
        end = _dt(e["expires_at"])
        if now <= end <= now + timedelta(hours=24):
            raise_("expiring", f"adv:expiring:{e['emp_id']}", severity="info", title=f"{e['name']} (temporary) ends {local_text(e['expires_at'], tz)}",
                   detail="Their open tasks need a new owner, or extend the end time.", link=f"/employees/{e['emp_id']}", entity_type="employee", entity_id=e["emp_id"])

    for p in projects.list_projects(con, include_done=False):
        if p["status"] != "active":
            continue
        health = projects.project_health(con, p["project_id"], now, int(project_days))
        base = dict(entity_type="project", entity_id=p["project_id"], link=f"/projects/{p['project_id']}")
        if health["label"] == "stalled":
            raise_("project", f"adv:project:{p['project_id']}", severity="warn", title=f"Project stalled: {p['name']}",
                   detail=f"{health['open']} open task(s), nothing has moved for {health['idle_days']:.0f} days.", **base)
        if p["next_review_at"] and _dt(p["next_review_at"]) < now:
            raise_("review", f"adv:review:{p['project_id']}", severity="info", title=f"Review due: {p['name']}",
                   detail=f"Planned for {local_text(p['next_review_at'], tz)}. Set a new review date when done.", **base)
    notify.resolve_except(con, "adv:", keep)
    return counts


# ---------------------------------------------------------------- the nag engine

def nag(con: sqlite3.Connection, now: datetime, tz: tzinfo | None = None) -> list[int]:
    """Chase unhandled items up a ladder. Quiet hours, snooze, acknowledgement and a daily cap keep it from nagging for nagging's sake.

    Level 1 after 4 h (approvals, blocked, failed) or 24 h (others) → Telegram; level 2 after 2 days (marks it more urgent);
    level 3 after 3 days → Telegram and e-mail, flagged overdue; from 5 days one reminder per day. Several items due at
    once become one grouped message. Acknowledge stops the chasing, resolving the cause ends it."""
    quiet = notify.get_setting(con, "quiet_hours")
    if in_quiet_hours(now, quiet, tz):
        return []
    adv = advisor_actor(con)
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    used = con.execute("SELECT count(*) FROM notification WHERE dedupe_key LIKE 'nag:%' AND created_at >= ?", (_iso(midnight),)).fetchone()[0]
    left = int(notify.get_setting(con, "nag_daily_cap")) - used
    _cleanup_nags(con, now)
    if left <= 0:
        return []
    marks = ",".join("?" * len(NAGGED_CATEGORIES))
    due: list[tuple[dict, dict]] = []
    for row in con.execute(f"SELECT * FROM notification WHERE resolved_at IS NULL AND ack_at IS NULL AND dedupe_key NOT LIKE 'nag:%' "
                           f"AND severity <> 'info' AND category IN ({marks}) AND (snooze_until IS NULL OR snooze_until <= ?)",
                           [*NAGGED_CATEGORIES, _iso(now)]):
        src = dict(row)
        age_h = (now - _dt(src["created_at"])).total_seconds() / 3600
        level = src["escalation_level"]
        step = None
        for rung in LADDER:
            hours = rung["hours"] if rung["hours"] is not None else FIRST_NUDGE_HOURS.get(src["category"], 24)
            if rung["level"] > level and age_h >= hours:
                step = rung
        if level >= 4:
            repeat = _dt(src["last_nag_at"])
            if repeat is None or (now - repeat).total_seconds() >= 24 * 3600:
                step = LADDER[-1]
        if step:
            due.append((src, step))
    if not due:
        return []
    due.sort(key=lambda d: (0 if d[0]["severity"] == "critical" else 1, d[0]["created_at"]))
    channels = list(dict.fromkeys(c for _, step in due for c in step["channels"]))
    created: list[int] = []
    if len(due) == 1:
        src, step = due[0]
        age = human_age(now - _dt(src["created_at"]))
        stamp = now.astimezone(tz).strftime("%Y%m%d") if step["level"] >= 4 else ""
        nid, _ = _upsert(con, adv, key=f"nag:{src['id']}:{step['level']}{':' + stamp if stamp else ''}", category="reminder",
                         severity="critical" if step["level"] >= 3 else "warn",
                         title=("OVERDUE reminder: " if step.get("overdue") else "Reminder: ") + src["title"],
                         detail=f"Waiting {age} (since {local_text(src['created_at'], tz)}). {(src['detail'] or '')[:200]}".strip(),
                         link=src["link"] or f"/inbox/{src['id']}", entity_type="notification", entity_id=str(src["id"]), channels=channels)
        created.append(nid)
    else:
        lines = [f"- {s['title']} ({human_age(now - _dt(s['created_at']))})" for s, _ in due[:8]]
        if len(due) > 8:
            lines.append(f"- … and {len(due) - 8} more")
        nid, _ = _upsert(con, adv, key=f"nag:digest:{_iso(now)}", category="reminder", severity="warn",
                         title=f"{len(due)} items are waiting for you", detail="\n".join(lines), link="/advisor", channels=channels)
        created.append(nid)
    for src, step in due:
        con.execute("UPDATE notification SET escalation_level = ?, last_nag_at = ? WHERE id = ?", (step["level"], _iso(now), src["id"]))
        if step.get("severity_up") and src["severity"] == "warn":
            con.execute("UPDATE notification SET severity = 'critical' WHERE id = ?", (src["id"],))
        con.execute("UPDATE notification SET resolved_at = ? WHERE resolved_at IS NULL AND dedupe_key LIKE 'nag:%' AND entity_type = 'notification' "
                    "AND entity_id = ? AND id NOT IN (?)", (_iso(now), str(src["id"]), created[0]))
    return created


def _cleanup_nags(con, now: datetime) -> None:
    """A reminder disappears with its cause; grouped and old reminders expire by themselves."""
    for row in con.execute("SELECT n.id, n.dedupe_key, n.created_at, n.entity_id FROM notification n WHERE n.resolved_at IS NULL AND n.dedupe_key LIKE 'nag:%'").fetchall():
        done = False
        if row["entity_id"]:
            src = con.execute("SELECT resolved_at, ack_at FROM notification WHERE id = ?", (int(row["entity_id"]),)).fetchone()
            done = src is None or src["resolved_at"] is not None or src["ack_at"] is not None
        age_h = (now - _dt(row["created_at"])).total_seconds() / 3600
        if done or (row["dedupe_key"].startswith("nag:digest:") and age_h >= 24) or age_h >= 72:
            con.execute("UPDATE notification SET resolved_at = ? WHERE id = ?", (_iso(now), row["id"]))


# ---------------------------------------------------------------- the Desk

def desk_items(con: sqlite3.Connection, now: datetime, tz: tzinfo | None = None, include_snoozed: bool = False) -> list[dict]:
    """Everything active, one row per open notification, with project, due time and the flags the tabs use."""
    items = []
    today = now.astimezone(tz).date()
    for n in con.execute("SELECT * FROM notification WHERE resolved_at IS NULL AND dedupe_key NOT LIKE 'nag:%'"):
        item = dict(n)
        project_id, due = None, None
        kind, ref = n["entity_type"], n["entity_id"]
        if n["task_id"] and not kind:
            kind, ref = "task", n["task_id"]
        if kind == "task":
            t = con.execute("SELECT project_id, due_at FROM task WHERE task_id = ?", (ref,)).fetchone()
            project_id, due = (t["project_id"], t["due_at"]) if t else (None, None)
        elif kind == "meeting":
            m = con.execute("SELECT project_id, scheduled_at FROM meeting WHERE meeting_id = ?", (ref,)).fetchone()
            project_id, due = (m["project_id"], m["scheduled_at"]) if m else (None, None)
        elif kind == "decision":
            d = con.execute("SELECT project_id, follow_up_by FROM decision WHERE decision_id = ?", (ref,)).fetchone()
            project_id, due = (d["project_id"], d["follow_up_by"]) if d else (None, None)
        elif kind == "project":
            project_id = ref
        created, due_dt = _dt(n["created_at"]), _dt(due)
        snoozed = bool(n["snooze_until"] and n["snooze_until"] > _iso(now))
        if snoozed and not include_snoozed:
            continue
        age = now - created
        key = n["dedupe_key"] or ""
        overdue_by = key.startswith("adv:overdue") and due_dt and due_dt < now
        item.update(
            project_id=project_id, due=due, snoozed=snoozed,
            age_text=human_age(now - due_dt) if overdue_by else human_age(age), age_label="overdue by" if overdue_by else "waiting",
            overdue=key.startswith("adv:overdue") or bool(due_dt and due_dt < now and kind in ("task", "decision")),
            forgotten=age.total_seconds() >= FORGOTTEN_AFTER_HOURS * 3600 and not n["ack_at"],
            waiting_on_me=(n["category"] in ("approval", "failed", "guardrail", "review") or key.startswith(("adv:decision", "adv:missed"))
                           or (n["category"] == "blocked" and n["severity"] == "critical")),
            today=created.astimezone(tz).date() == today or bool(due_dt and due_dt.astimezone(tz).date() == today),
            this_week=bool(due_dt and now <= due_dt <= now + timedelta(days=7)),
        )
        items.append(item)
    names = {r["project_id"]: r["name"] for r in con.execute("SELECT project_id, name FROM project")}
    for item in items:
        item["project_name"] = names.get(item["project_id"]) if item["project_id"] else None
    order = {"critical": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: (order.get(i["severity"], 3), 0 if i["waiting_on_me"] else 1, i["created_at"]))
    return items


TABS = ("all", "today", "week", "overdue", "forgotten", "waiting")


def filter_tab(items: list[dict], tab: str) -> list[dict]:
    pick = {"today": "today", "week": "this_week", "overdue": "overdue", "forgotten": "forgotten", "waiting": "waiting_on_me"}.get(tab)
    return [i for i in items if i[pick]] if pick else list(items)


def group_by_project(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["project_name"] or "No project", []).append(item)
    return sorted(groups.items(), key=lambda kv: (kv[0] == "No project", kv[0].lower()))


def review_order(items: list[dict]) -> list[dict]:
    """The order the Founder walks through them: needs me first, then overdue, forgotten, most urgent, oldest."""
    rank = {"critical": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: (0 if i["waiting_on_me"] else 1, 0 if i["overdue"] else 1, 0 if i["forgotten"] else 1,
                                        rank.get(i["severity"], 3), i["created_at"]))


# ---------------------------------------------------------------- reports

def save_report(con, home: Path, kind: str, title: str, text: str, period: str = "", now: datetime | None = None,
                link_kind: str = "report") -> str:
    import uuid
    adv = advisor_actor(con)
    now = now or datetime.now(timezone.utc)
    report_id = f"R-{uuid.uuid4().hex[:8]}"
    relative = f"memory/reports/{kind}/{now.strftime('%Y%m%dT%H%M%S')}-{report_id}.md"
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(events.scrub(text), encoding="utf-8")
    os.replace(tmp, target)
    con.execute("INSERT INTO report (report_id, kind, title, path, period, created_at) VALUES (?,?,?,?,?,?)", (report_id, kind, title, relative, period, _iso(now)))
    first_line = next((l for l in text.splitlines() if l.strip() and not l.startswith("#")), "")
    notify.upsert(con, key=f"report:{report_id}", category="report", severity="info", title=title, detail=first_line[:200],
                  entity_type="report", entity_id=report_id, link=f"/advisor/reports/{report_id}", actor=adv)
    events.append(con, type="report.created", summary=f"Report ready: {title}", payload={"report_id": report_id, "kind": kind}, **adv)
    return report_id


def _count_events(con, since: datetime, until: datetime) -> dict[str, int]:
    return {r["type"]: r["n"] for r in con.execute("SELECT type, count(*) AS n FROM event WHERE ts >= ? AND ts < ? GROUP BY type", (_iso(since), _iso(until)))}


def _needs_you_lines(con, now: datetime, tz, limit: int = 12) -> list[str]:
    items = [i for i in desk_items(con, now, tz) if i["waiting_on_me"]]
    return [f"- [{i['severity']}] {i['title']} — waiting {i['age_text']}" for i in items[:limit]] or ["- Nothing."]


def project_lines(con, now: datetime) -> list[str]:
    lines = []
    for p in projects.list_projects(con, include_done=False):
        h = projects.project_health(con, p["project_id"], now, int(notify.get_setting(con, "project_stale_days")))
        lines.append(f"- **{p['name']}** ({p['status']}): {h['label']} — {h['open']} open, {h['blocked']} blocked, {h['overdue']} overdue, {h['done']} done, idle {h['idle_days']:.0f} d")
    return lines or ["- No projects."]


def build_daily(con, now: datetime, tz=None) -> tuple[str, str]:
    day = now.astimezone(tz).strftime("%A %d %B %Y")
    ev = _count_events(con, now - timedelta(hours=24), now)
    usage = con.execute("SELECT coalesce(sum(est_tokens), 0), count(*) FROM usage WHERE ts >= ?", (_iso(now - timedelta(hours=24)),)).fetchone()
    today = now.astimezone(tz).date()
    due_today = [t for t in con.execute("SELECT t.title, t.due_at, e.name AS owner FROM task t LEFT JOIN employee e ON e.emp_id = t.owner_emp_id "
                                        "WHERE t.deleted_at IS NULL AND t.status NOT IN ('done','rejected') AND t.due_at IS NOT NULL ORDER BY t.due_at")
                 if _dt(t["due_at"]).astimezone(tz).date() == today]
    meetings = [m for m in projects.list_meetings(con, upcoming=True, now=now) if _dt(m["scheduled_at"]).astimezone(tz).date() == today]
    desk = desk_items(con, now, tz)
    forgotten = [i for i in desk if i["forgotten"]]
    overdue = [i for i in desk if i["overdue"]]
    engine = con.execute("SELECT heartbeat_at, mode FROM engine_status WHERE id = 1").fetchone()
    lines = [f"# Daily briefing — {day}", "", "## Yesterday (last 24 hours)",
             f"- Tasks finished: {ev.get('task.done', 0)} · failed: {ev.get('task.failed', 0)} · blocked: {ev.get('task.blocked', 0)}",
             f"- Approvals decided: {ev.get('task.approval_decided', 0)} · new notifications: {ev.get('notification.created', 0)}",
             f"- Agent runs recorded: {usage[1]} (about {usage[0]:,} tokens)", "", "## Today",
             *([f"- Due: {t['title']} ({t['owner'] or 'unassigned'}) {local_text(t['due_at'], tz, '%H:%M')}" for t in due_today] or ["- No task is due today."]),
             *([f"- Meeting {local_text(m['scheduled_at'], tz, '%H:%M')}: {m['title']}" for m in meetings] or ["- No meetings today."]),
             "", "## Overdue", *([f"- {i['title']} — {i['age_text']}" for i in overdue[:10]] or ["- Nothing is overdue."]),
             "", "## Needs your decision", *_needs_you_lines(con, now, tz), "", "## Forgotten (3+ days)",
             *([f"- {i['title']} — {i['age_text']}" for i in forgotten[:10]] or ["- Nothing."]), "", "## Projects", *project_lines(con, now), "",
             "## System", f"- Task engine: {'running (last heartbeat ' + local_text(engine['heartbeat_at'], tz, '%H:%M') + ')' if engine else 'not running'}",
             f"- Advisor monitor: {'on' if scheduler.monitor_enabled(con) else 'off'}"]
    return f"Daily briefing — {today.isoformat()}", "\n".join(lines) + "\n"


def build_weekly(con, now: datetime, tz=None) -> tuple[str, str]:
    since = now - timedelta(days=7)
    ev = _count_events(con, since, now)
    done = con.execute("SELECT t.title, p.name AS project FROM task t LEFT JOIN project p ON p.project_id = t.project_id WHERE t.status = 'done' AND t.updated_at >= ? ORDER BY t.updated_at DESC LIMIT 25", (_iso(since),)).fetchall()
    active = con.execute("SELECT actor_name, count(*) AS n FROM event WHERE ts >= ? AND actor_type = 'employee' GROUP BY actor_name ORDER BY n DESC LIMIT 10", (_iso(since),)).fetchall()
    lines = [f"# Weekly report — week ending {now.astimezone(tz).strftime('%d %B %Y')}", "", "## The week in numbers",
             f"- Finished: {ev.get('task.done', 0)} · failed: {ev.get('task.failed', 0)} · blocked: {ev.get('task.blocked', 0)} · cancelled: {ev.get('task.cancelled', 0)}",
             f"- Meetings held: {ev.get('meeting.held', 0)} · decisions recorded: {ev.get('decision.recorded', 0)}", "", "## Finished tasks",
             *([f"- {d['title']}" + (f" ({d['project']})" if d["project"] else "") for d in done] or ["- None."]), "", "## Most active employees",
             *([f"- {a['actor_name']}: {a['n']} actions" for a in active] or ["- None."]), "", "## Projects", *project_lines(con, now), "",
             "## Still waiting for you", *_needs_you_lines(con, now, tz, 20)]
    return f"Weekly report — {now.astimezone(tz).date().isoformat()}", "\n".join(lines) + "\n"


def build_meeting_prep(con, meeting: dict, now: datetime, tz=None) -> tuple[str, str]:
    lines = [f"# Preparation: {meeting['title']}", "", f"When: {local_text(meeting['scheduled_at'], tz)}", f"Project: {meeting['project_name'] or '—'}",
             "Attendees: " + (", ".join(a["name"] for a in meeting["attendees"]) or "—"), "", "## Agenda", meeting["agenda"] or "(no agenda written yet)", ""]
    if meeting["project_id"]:
        h = projects.project_health(con, meeting["project_id"], now)
        lines += ["## Project status", f"- {h['label']}: {h['open']} open, {h['blocked']} blocked, {h['overdue']} overdue, idle {h['idle_days']:.0f} d", "",
                  "## Open action items from earlier meetings"]
        open_actions = con.execute("SELECT a.description, t.status FROM action_item a JOIN meeting m ON m.meeting_id = a.meeting_id LEFT JOIN task t ON t.task_id = a.task_id "
                                   "WHERE m.project_id = ? AND a.meeting_id <> ? AND (t.status IS NULL OR t.status NOT IN ('done','rejected'))", (meeting["project_id"], meeting["meeting_id"])).fetchall()
        lines += [f"- {a['description']} ({(a['status'] or 'no task').replace('_', ' ')})" for a in open_actions] or ["- None."]
        lines += ["", "## Decisions still open"]
        lines += [f"- {d['title']}" for d in projects.list_decisions(con, "open", meeting["project_id"])] or ["- None."]
    return f"Meeting preparation — {meeting['title']}", "\n".join(lines) + "\n"


def away_summary(con, home: Path, since: datetime, now: datetime, ran: list[dict], tz=None) -> str:
    """Written when the monitor wakes up after a long gap: what happened and what was caught up."""
    ev = _count_events(con, since, now)
    missed = [m for m in projects.list_meetings(con) if m["status"] == "scheduled" and m["scheduled_at"] and since <= _dt(m["scheduled_at"]) < now]
    waiting = [i for i in desk_items(con, now, tz) if i["waiting_on_me"]]
    gap = human_age(now - since)
    lines = [f"# While you were away ({gap})", "", f"From {local_text(_iso(since), tz)} to {local_text(_iso(now), tz)}.", "",
             "## What happened", f"- Tasks finished: {ev.get('task.done', 0)} · failed: {ev.get('task.failed', 0)} · blocked: {ev.get('task.blocked', 0)}",
             f"- New notifications: {ev.get('notification.created', 0)}", "", "## Caught up",
             *([f"- {r['job']}: {r['status']}" for r in ran] or ["- No job was due."]), "", "## Meetings that passed",
             *([f"- {m['title']} ({local_text(m['scheduled_at'], tz)}) — mark it held or cancelled" for m in missed] or ["- None."]), "", "## Needs you now",
             *([f"- {i['title']} — {i['age_text']}" for i in waiting[:12]] or ["- Nothing."])]
    return save_report(con, home, "away", f"While you were away ({gap})", "\n".join(lines) + "\n", period=_iso(since), now=now)


def maybe_ai_summary(con, home: Path, report_id: str, text: str) -> str | None:
    """Optional: ask the Advisor (read-only, tier 0) for a written summary. Off unless switched on in settings."""
    if not notify.get_setting(con, "ai_summary"):
        return None
    adv = advisor_actor(con)
    if adv.get("actor_type") != "employee":
        return None
    try:
        task_id = tasks.create_task(con, title="Summarise today's briefing", owner=adv["actor_id"], risk_tier=0, actor=adv,
                                    creator_emp_id=adv["actor_id"],
                                    description="Write a five-line summary of this briefing for the Founder. Point out what is most urgent.\n\n" + text[:3000])
    except Exception as err:                       # e.g. the input guard objected to a task title in the report
        events.append(con, type="advisor.ai_summary_skipped", summary=f"AI summary skipped: {str(err)[:120]}", **adv)
        return None
    notify.set_setting(con, f"report_ai:{report_id}", task_id)
    return task_id


# ---------------------------------------------------------------- the challenge note

def challenge_note(con, task_id: str) -> list[dict]:
    """Automatic, shallow checks on a task's result before the Founder decides. They find gaps, not truth."""
    task = tasks.get_task(con, task_id)
    if task is None:
        return []
    text = f"{task['title']} {task['result_summary'] or ''}".lower()
    found: list[dict] = []

    def add(severity: str, message: str) -> None:
        found.append({"severity": severity, "text": message})

    if len(task["result_summary"] or "") < 80:
        add("warn", "The result is very short. There may be no real proposal text to judge.")
    for words, message in ((("cost", "budget", "price", "$", "₹", "usd"), "No cost or budget is mentioned."),
                           (("risk", "downside", "threat", "failure"), "No risks are mentioned."),
                           (("source", "http", "according", "evidence", "data shows", "study"), "No evidence or sources are cited.")):
        if not any(w in text for w in words):
            add("info", message)
    for dep in task["depends_on"]:
        if dep["status"] != "done":
            add("warn", f"It depends on '{dep['title']}', which is {dep['status'].replace('_', ' ')}.")
    for child in task["children"]:
        if child["status"] in ("failed", "blocked", "rejected"):
            add("warn", f"Sub-task '{child['title']}' is {child['status']}.")
    attempts = con.execute("SELECT count(*) FROM event WHERE task_id = ? AND type = 'guard.blocked'", (task_id,)).fetchone()[0]
    if attempts:
        add("warn", f"The agent attempted {attempts} forbidden action(s) while producing this.")
    if task["attempts"] > 1:
        add("info", f"It needed {task['attempts']} attempts.")
    return found or [{"severity": "info", "text": "No obvious gaps found by the automatic checks."}]


# ---------------------------------------------------------------- scheduled jobs

@register("scan_active", "every 15m", "Look for overdue, stale, forgotten and upcoming items")
def job_scan(ctx: JobContext) -> str:
    attention.sync(ctx.con, ctx.home)               # keep the desk right even when the engine is not running
    counts = scan(ctx.con, ctx.now, ctx.tz)
    return ", ".join(f"{k}: {v}" for k, v in counts.items()) or "nothing to flag"


@register("nag_check", "every 5m", "Chase items that have been waiting too long")
def job_nag(ctx: JobContext) -> str:
    made = nag(ctx.con, ctx.now, ctx.tz)
    if made and ctx.notifier is not None:
        ctx.notifier.pump(ctx.con)
    return f"{len(made)} reminder(s)"


@register("daily_briefing", "daily 08:00", "Prepare the morning briefing")
def job_daily(ctx: JobContext) -> str:
    title, text = build_daily(ctx.con, ctx.now, ctx.tz)
    report_id = save_report(ctx.con, ctx.home, "daily", title, text, period=ctx.now.astimezone(ctx.tz).date().isoformat(), now=ctx.now)
    maybe_ai_summary(ctx.con, ctx.home, report_id, text)
    return report_id


@register("weekly_report", "weekly mon 09:00", "Prepare the weekly report")
def job_weekly(ctx: JobContext) -> str:
    title, text = build_weekly(ctx.con, ctx.now, ctx.tz)
    return save_report(ctx.con, ctx.home, "weekly", title, text, now=ctx.now)


@register("project_health", "daily 09:30", "Project health check")
def job_projects(ctx: JobContext) -> str:
    lines = ["# Project health", "", *project_lines(ctx.con, ctx.now)]
    return save_report(ctx.con, ctx.home, "projects", f"Project health — {ctx.now.astimezone(ctx.tz).date().isoformat()}", "\n".join(lines) + "\n", now=ctx.now)


@register("meeting_prep", "every 30m", "Write a preparation note for meetings in the next 24 hours")
def job_meeting_prep(ctx: JobContext) -> str:
    made = 0
    for m in projects.list_meetings(ctx.con, upcoming=True, now=ctx.now):
        when = _dt(m["scheduled_at"])
        if when - ctx.now > timedelta(hours=24):
            continue
        if ctx.con.execute("SELECT 1 FROM report WHERE kind = 'meeting_prep' AND period = ?", (m["meeting_id"],)).fetchone():
            continue
        title, text = build_meeting_prep(ctx.con, projects.get_meeting(ctx.con, m["meeting_id"]), ctx.now, ctx.tz)
        save_report(ctx.con, ctx.home, "meeting_prep", title, text, period=m["meeting_id"], now=ctx.now)
        made += 1
    return f"{made} preparation note(s)"


@register("inbox_digest", "daily 18:00", "Evening summary of what is unread")
def job_digest(ctx: JobContext) -> str:
    rows = ctx.con.execute("SELECT category, count(*) AS n, min(created_at) AS oldest FROM notification WHERE read_at IS NULL AND resolved_at IS NULL "
                           "AND dedupe_key NOT LIKE 'nag:%' GROUP BY category ORDER BY n DESC").fetchall()
    if not rows:
        return "nothing unread"
    lines = [f"- {r['category']}: {r['n']} (oldest {human_age(ctx.now - _dt(r['oldest']))})" for r in rows]
    adv = advisor_actor(ctx.con)
    notify.upsert(ctx.con, key=f"digest:{ctx.now.astimezone(ctx.tz).date().isoformat()}", category="report", severity="info",
                  title=f"{sum(r['n'] for r in rows)} unread notification(s)", detail="\n".join(lines), link="/inbox?state=unread", actor=adv)
    return f"{len(rows)} categories"


@register("maintenance", "weekly sun 03:30", "Check the database and audit log, prune bulky records, report trash size")
def job_maintenance(ctx: JobContext) -> str:
    from . import maintenance
    report = maintenance.run(ctx.con, ctx.home, ctx.now)
    return "; ".join(report["problems"] + report["did"]) or "all fine"


@register("backup", "daily 02:30", "Back up the company data")
def job_backup(ctx: JobContext) -> str:
    path = backup.create_backup(ctx.home)
    old = sorted((ctx.home / "backups").glob("aicorp-backup-*.tar.gz"))[:-7]
    for stale in old:                              # nothing is deleted: old copies go to the trash
        trash.move_to_trash(ctx.home, stale, reason="backup rotation", by="Advisor")
    return f"{path.name}" + (f"; {len(old)} old copy(ies) moved to the trash" if old else "")

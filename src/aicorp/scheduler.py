"""A small cron-like scheduler for the Advisor's jobs.

Jobs are registered by name with a default schedule. ``run_due`` is safe to call from several
processes (the engine and the watchdog both do): a job is *claimed* by moving its ``next_run``
forward in a single conditional UPDATE, so each run happens once. It only runs while the monitor
is switched on. After the computer was off or asleep, jobs whose time passed run once ("catch-up")
and a "while you were away" report is written.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

from . import events, notify
from .config import utc_now
from .org import SYSTEM, authorize, FOUNDER

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
CATCH_UP_AFTER = 300.0           # seconds late before a run counts as catch-up
TICK_SAVE_EVERY = 30.0
REGISTRY: dict[str, tuple[str, str, Callable]] = {}      # name -> (default spec, description, function)


@dataclass
class JobContext:
    con: sqlite3.Connection
    home: Path
    now: datetime
    catch_up: bool
    notifier: object | None
    tz: tzinfo | None
    manual: bool = False


def register(name: str, spec: str, description: str):
    def wrap(fn):
        REGISTRY[name] = (spec, description, fn)
        return fn
    return wrap


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------- schedule specs

def parse_spec(spec: str) -> tuple[str, tuple]:
    text = " ".join(str(spec).lower().split())
    if m := re.fullmatch(r"every (\d{1,4})\s*(m|min|minutes?|h|hours?|d|days?)", text):
        unit = m.group(2)[0]
        minutes = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[unit]
        if minutes < 1:
            raise ValueError("interval must be at least 1 minute")
        return "every", (minutes,)
    if m := re.fullmatch(r"daily (\d{1,2}):(\d{2})", text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("invalid time")
        return "daily", (hour, minute)
    if m := re.fullmatch(r"weekly (mon|tue|wed|thu|fri|sat|sun) (\d{1,2}):(\d{2})", text):
        hour, minute = int(m.group(2)), int(m.group(3))
        if hour > 23 or minute > 59:
            raise ValueError("invalid time")
        return "weekly", (DAYS[m.group(1)], hour, minute)
    raise ValueError("use 'every 15m', 'every 2h', 'daily 08:00' or 'weekly mon 09:00'")


def next_run(spec: str, after: datetime, tz: tzinfo | None = None) -> datetime:
    """The first run time strictly after ``after`` (wall-clock times are in ``tz``, default the computer's zone)."""
    kind, params = parse_spec(spec)
    if kind == "every":
        return after + timedelta(minutes=params[0])
    local = after.astimezone(tz)
    if kind == "daily":
        hour, minute = params
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)
    dow, hour, minute = params
    target = (local + timedelta(days=(dow - local.weekday()) % 7)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=7)
    return target.astimezone(timezone.utc)


# ---------------------------------------------------------------- state

def monitor_enabled(con) -> bool:
    return bool(notify.get_setting(con, "monitor_enabled"))


def set_monitor(con, on: bool, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    notify.set_setting(con, "monitor_enabled", bool(on))
    events.append(con, type="monitor.switched", summary=f"Advisor monitor switched {'on' if on else 'off'}", payload={"on": bool(on)}, **actor)


def ensure_defaults(con, now: datetime, tz: tzinfo | None = None) -> None:
    for name, (spec, _desc, _fn) in REGISTRY.items():
        # OR IGNORE: the engine and the watchdog both call this and may start in the same instant
        con.execute("INSERT OR IGNORE INTO schedule (job, spec, enabled, next_run) VALUES (?, ?, 1, ?)", (name, spec, _iso(next_run(spec, now, tz))))


def set_job(con, job: str, *, spec: str | None = None, enabled: bool | None = None, now: datetime | None = None,
            tz: tzinfo | None = None, actor: dict = FOUNDER) -> None:
    authorize(con, actor, founder_only=True)
    row = con.execute("SELECT * FROM schedule WHERE job = ?", (job,)).fetchone()
    if row is None:
        raise ValueError("unknown job")
    now = now or datetime.now(timezone.utc)
    if spec is not None:
        parse_spec(spec)
        con.execute("UPDATE schedule SET spec = ?, next_run = ? WHERE job = ?", (spec, _iso(next_run(spec, now, tz)), job))
    if enabled is not None:
        con.execute("UPDATE schedule SET enabled = ? WHERE job = ?", (1 if enabled else 0, job))
        if enabled:
            con.execute("UPDATE schedule SET next_run = ? WHERE job = ? AND next_run < ?", (_iso(next_run(row["spec"], now, tz)), job, _iso(now)))
    events.append(con, type="schedule.updated", summary=f"Schedule changed for {job}", payload={"job": job, "spec": spec, "enabled": enabled}, **actor)


def status(con) -> list[dict]:
    out = []
    for row in con.execute("SELECT * FROM schedule ORDER BY job"):
        last = con.execute("SELECT status, started_at, finished_at, detail, catch_up FROM job_run WHERE job = ? ORDER BY run_id DESC LIMIT 1", (row["job"],)).fetchone()
        out.append({**dict(row), "description": REGISTRY.get(row["job"], ("", "", None))[1], "last": dict(last) if last else None})
    return out


def history(con, limit: int = 40) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM job_run ORDER BY run_id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- running

def _execute(ctx: JobContext, job: str) -> dict:
    from . import advisor  # noqa: F401  (registers the jobs)
    con = ctx.con
    run_id = con.execute("INSERT INTO job_run (job, started_at, status, catch_up) VALUES (?, ?, 'running', ?)",
                         (job, utc_now(), 1 if ctx.catch_up else 0)).lastrowid
    try:
        detail = REGISTRY[job][2](ctx) or ""
        status_text = "ok"
        notify.resolve_key(con, f"error:job:{job}")
    except Exception as err:                       # a broken job must not stop the others
        status_text, detail = "error", f"{type(err).__name__}: {str(err)[:300]}"
        events.append(con, type="job.error", summary=f"Job {job} failed: {detail}"[:300], payload={"job": job}, **SYSTEM)
        notify.upsert(con, key=f"error:job:{job}", category="system", severity="warn", title=f"Advisor job failed: {job}",
                      detail=detail, link="/advisor/schedule")
    con.execute("UPDATE job_run SET finished_at = ?, status = ?, detail = ? WHERE run_id = ?", (utc_now(), status_text, events.scrub(str(detail))[:500], run_id))
    return {"job": job, "status": status_text, "detail": detail, "catch_up": ctx.catch_up}


def run_now(con, home: Path, job: str, now: datetime | None = None, notifier=None, tz: tzinfo | None = None) -> dict:
    if job not in REGISTRY:
        from . import advisor  # noqa: F401
    if job not in REGISTRY:
        raise ValueError("unknown job")
    return _execute(JobContext(con, home, now or datetime.now(timezone.utc), False, notifier, tz, manual=True), job)


def run_due(con, home: Path, now: datetime | None = None, notifier=None, tz: tzinfo | None = None) -> list[dict]:
    """Run every job whose time has come. Does nothing while the monitor is off."""
    from . import advisor
    now = now or datetime.now(timezone.utc)
    if not monitor_enabled(con):
        return []
    ensure_defaults(con, now, tz)
    last_tick = notify.get_setting(con, "monitor_last_tick")
    gap = None
    if last_tick:
        away = (now - datetime.fromisoformat(last_tick)).total_seconds()
        if away > float(notify.get_setting(con, "away_threshold_minutes")) * 60:
            gap = (datetime.fromisoformat(last_tick), now)
    ran: list[dict] = []
    for row in con.execute("SELECT * FROM schedule WHERE enabled = 1 AND next_run <= ? ORDER BY next_run", (_iso(now),)).fetchall():
        upcoming = _iso(next_run(row["spec"], now, tz))
        claimed = con.execute("UPDATE schedule SET last_run = ?, next_run = ? WHERE job = ? AND next_run = ?",
                              (_iso(now), upcoming, row["job"], row["next_run"])).rowcount
        if claimed != 1:
            continue                                # another process took this run
        late = (now - datetime.fromisoformat(row["next_run"])).total_seconds()
        ran.append(_execute(JobContext(con, home, now, late > CATCH_UP_AFTER, notifier, tz), row["job"]))
    if gap is not None:
        claimed = con.execute("UPDATE setting SET value = ?, updated_at = ? WHERE key = 'monitor_last_tick' AND value = ?",
                              (f'"{_iso(now)}"', utc_now(), f'"{last_tick}"')).rowcount
        if claimed == 1:
            advisor.away_summary(con, home, gap[0], now, ran, tz)
    elif not last_tick or (now - datetime.fromisoformat(last_tick)).total_seconds() >= TICK_SAVE_EVERY:
        notify.set_setting(con, "monitor_last_tick", _iso(now))
    return ran

"""Independent watchdog: a tiny process that notices a dead task engine and sends the alerts.

It runs separately so that it still works when the engine has crashed. Every few seconds it
(1) checks the engine's heartbeat and raises/clears an "engine stopped" notification and
(2) delivers any notification that is due (the engine and the dashboard only *create* them).
If the database itself is unreachable it sends a direct message instead.
"""
from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import chat, notify, redact, scheduler
from .channels import load_inboxes, load_transports
from .config import db_path, home as default_home
from .db import connect, migrate
from .delivery import Notifier

DIRECT_ALERT_EVERY = 600.0     # seconds between "database unreachable" messages


def engine_alive(con: sqlite3.Connection, now: datetime, down_after: float) -> tuple[bool | None, str]:
    """(True|False|None, detail). None = no engine is expected (never started, or it stopped cleanly)."""
    row = con.execute("SELECT pid, heartbeat_at, instance FROM engine_status WHERE id = 1").fetchone()
    if row is None:
        return None, ""
    age = (now - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
    if age <= down_after and psutil.pid_exists(row["pid"]):
        return True, ""
    return False, f"No heartbeat for {int(age)} seconds (process {row['pid']})."


def tick(con: sqlite3.Connection, notifier: Notifier, now: datetime | None = None, advisor: bool = False, home: Path | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    notify.set_setting(con, "watchdog_heartbeat", now.isoformat(timespec="seconds"))
    down_after = float(notify.get_setting(con, "engine_down_after"))
    alive, detail = engine_alive(con, now, down_after)
    if alive is False:
        notify.upsert(con, key="engine:down", category="system", severity="critical",
                      title="The task engine stopped unexpectedly",
                      detail=f"{detail} Tasks that were running are recovered when you start it again "
                             f"(python -m aicorp.engine).", link="/attention")
    else:
        notify.resolve_key(con, "engine:down")
    ran = []
    if advisor and home is not None:
        try:
            ran = scheduler.run_due(con, home, now, notifier)       # same jobs as the engine; each run is claimed once
        except Exception as err:
            notify.upsert(con, key=f"error:watchdog-advisor:{type(err).__name__}", category="system", severity="warn",
                          title="The Advisor's scheduled jobs failed", detail=f"{type(err).__name__}: {str(err)[:200]}", link="/advisor/schedule")
    chat.collect_answers(con, now)                     # finished Advisor answers -> queued replies
    sent = chat.pump_outbound(con, notifier.transports, now, scrub=notifier.scrub)
    return {"engine": alive, "jobs": [r["job"] for r in ran], "chat_sent": sent["sent"], **notifier.pump(con)}


def chat_loop(home: Path, inboxes: dict, transports: dict, stop: threading.Event, scrub=None, poll_email_every: float = 60.0) -> None:
    """Listen for the Founder's Telegram/e-mail messages (own thread, own database connection)."""
    con = None
    last_mail = 0.0
    failed = False
    while not stop.is_set():
        try:
            if con is None:
                con = connect(db_path(home))
                migrate(con)
            failed = False
            if "telegram" in inboxes:
                failed = bool(chat.poll_once(con, home, inboxes, "telegram")["error"])      # long poll: waits up to ~20 s
            if "email" in inboxes and datetime.now(timezone.utc).timestamp() - last_mail >= poll_email_every:
                last_mail = datetime.now(timezone.utc).timestamp()
                chat.poll_once(con, home, inboxes, "email")
            chat.pump_outbound(con, transports, scrub=scrub)       # answer straight away; the tick also retries
        except Exception:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
            con = None
        stop.wait(15.0 if failed else 2.0 if "telegram" in inboxes else 5.0)


def watchdog_state(con: sqlite3.Connection, max_age: float = 20.0) -> dict:
    raw = notify.get_setting(con, "watchdog_heartbeat")
    if not raw:
        return {"running": False}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
    return {"running": age <= max_age, "age": round(age)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicorp-watchdog", description="AICorp watchdog and alert sender")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--no-chat", action="store_true", help="do not listen for Telegram/e-mail messages")
    args = parser.parse_args(argv)
    home = default_home()
    from .config import ensure_layout
    ensure_layout(home)
    redactor = redact.install(home)
    notifier = Notifier(load_transports(home), scrub=redactor.scrub)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"AICorp watchdog running (channels: {', '.join(sorted(notifier.transports)) or 'none configured'}). Ctrl+C to stop.")
    last_direct = 0.0
    con = None
    inboxes = {} if args.no_chat else load_inboxes(home)
    if inboxes:
        threading.Thread(target=chat_loop, args=(home, inboxes, notifier.transports, stop, notifier.scrub), daemon=True, name="chat").start()
        print(f"Chat with the Advisor on: {', '.join(sorted(inboxes))}")
    while not stop.is_set():
        try:
            if con is None:
                con = connect(db_path(home))
                migrate(con)
            tick(con, notifier, advisor=True, home=home)
        except Exception as err:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
            con = None
            now = datetime.now(timezone.utc).timestamp()
            if now - last_direct > DIRECT_ALERT_EVERY:
                last_direct = now
                notifier.send_direct("[AICorp] CRITICAL: the database is not reachable",
                                     f"The watchdog could not use the AICorp database ({type(err).__name__}). "
                                     f"The company may be stopped. Check the computer.")
        stop.wait(args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Milestone M9: Founder task -> CEO -> two teams in parallel -> review -> proposal -> approval, with Advisor reminders,
a Telegram status query, a nag escalation, impersonation and an interrupt - then every line of it is found in the audit log,
the chain verifies, and a backup restores the whole company in another folder."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from aicorp import advisor, chat, events, org, search, takeover, tasks
from aicorp.channels import FileTransport, Inbound
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.delivery import Notifier
from aicorp.engine import Orchestrator
from aicorp.runners import FakeRunner, RunResult
from aicorp.web.app import create_app
from helpers import assert_transitions_are_logged

SRC = str(Path(__file__).resolve().parents[1] / "src")


class CompanyRunner(FakeRunner):
    """Simulated agents. The CEO's plan is made through the same authorised API a real CEO would use; a '[slow]' task waits
    until it is interrupted, and on its second attempt (after a take-over) finishes at once."""

    def __init__(self, home):
        super().__init__(duration=(0.8, 0.8), seed=3)
        self.home = home
        self.planned = False

    def run(self, ctx):
        title = ctx.task["title"]
        if title.startswith("Plan") and not self.planned:
            self.planned = True
            con = connect(db_path(self.home))
            try:
                ceo = org.get_employee(con, ctx.employee["emp_id"])
                actor = {"actor_type": "employee", "actor_id": ceo["emp_id"], "actor_name": ceo["name"], "actor_designation": "Chief Executive"}
                names = {e["name"]: e["emp_id"] for e in org.list_employees(con)}
                kw = dict(actor=actor, creator_emp_id=ceo["emp_id"], parent_id=ctx.task["task_id"], delegation=True)
                m = tasks.create_task(con, title="Draft the launch messaging", owner=names["Aria"], **kw)
                r = tasks.create_task(con, title="Competitor scan [slow]", owner=names["Rae"], **kw)
                c = tasks.create_task(con, title="Consolidate the proposal [approval]", owner=ceo["emp_id"], risk_tier=3, depends_on=[m, r],
                                      actor=actor, creator_emp_id=ceo["emp_id"], parent_id=ctx.task["task_id"])
                ctx.emit("ceo.plan", f"Split the launch into 3 tasks: {m}, {r}, {c}", {"children": [m, r, c]})
            finally:
                con.close()
            return RunResult("done", "Plan made: messaging (Marketing), competitor scan (Research), then one consolidated proposal for approval")
        if "[slow]" in title and not ctx.checkpoint:
            ctx.save_checkpoint("scanned 3 of 6 competitors")
            end = time.monotonic() + 30
            while time.monotonic() < end:
                if ctx.cancel.is_set():
                    return RunResult("stopped", "stopped", checkpoint="scanned 3 of 6 competitors")
                time.sleep(0.05)
        if "[slow]" in title:
            return RunResult("done", "Competitor scan finished (6 of 6), resumed after the Founder's note")
        return super().run(ctx)


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def wait(predicate, timeout=60.0, tick=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if tick:
            tick()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_m9_end_to_end(tmp_path, capsys):
    home = tmp_path / "company-home"
    for name in ("company", "skills", "memory", "artifacts", "workspaces", "config", "database"):
        (home / name).mkdir(parents=True)
    (home / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=123456:ABCdefTOKENvalue\nTELEGRAM_CHAT_ID=4242\n")
    out = home / "outbox"
    os.environ["AICORP_TRANSPORT_DIR"] = str(out)
    try:
        run_scenario(home, out, tmp_path, capsys)
    finally:
        os.environ.pop("AICORP_TRANSPORT_DIR", None)


def run_scenario(home, out, tmp_path, capsys):
    steps = []
    with TestClient(create_app(home), raise_server_exceptions=False) as web:
        web.get("/")
        con = connect(db_path(home))
        role = org.create_role(con, {"name": "Worker", "execute": True})
        people = {n: org.create_employee(con, home, {"name": n, "role_ids": [role]}) for n in ("Aria", "Ben", "Rae", "Sam")}
        org.create_team(con, {"name": "Marketing", "head_emp_id": people["Aria"], "member_ids": [people["Ben"]]})
        org.create_team(con, {"name": "Research", "head_emp_id": people["Rae"], "member_ids": [people["Sam"]]})
        ceo_id = con.execute("SELECT emp_id FROM employee WHERE system_key = 'ceo'").fetchone()[0]
        runner = CompanyRunner(home)
        engine = Orchestrator(home, runner, threaded=True, cap=4, lease_seconds=60, max_runtime=120, ram_probe=lambda: 0.1, base_backoff=0.1)
        engine.start()
        pump = lambda: engine.tick()                                              # noqa: E731
        notifier = Notifier({"telegram": FileTransport(out, "telegram"), "email": FileTransport(out, "email")}, scrub=lambda s: s)
        try:
            # 1. The Founder gives the CEO a task through the dashboard
            r = post(web, "/tasks", {"title": "Plan the Q4 launch", "description": "Prepare a launch proposal for approval.", "owner": ceo_id, "priority": "2", "risk_tier": "1"})
            assert r.status_code == 303, r.text[:300]
            top = con.execute("SELECT task_id FROM task WHERE title = 'Plan the Q4 launch'").fetchone()[0]
            steps.append("Founder created the top task for the CEO")

            # 2. The CEO plans; two teams work in parallel; consolidation waits for both
            assert wait(lambda: con.execute("SELECT count(*) FROM task WHERE parent_id = ?", (top,)).fetchone()[0] == 3, 30, pump)
            kids = {r["title"]: r["task_id"] for r in con.execute("SELECT task_id, title FROM task WHERE parent_id = ?", (top,))}
            messaging, scan = kids["Draft the launch messaging"], kids["Competitor scan [slow]"]
            consolidate = kids["Consolidate the proposal [approval]"]
            assert wait(lambda: tasks.get_task(con, scan)["status"] == "running", 20, pump)
            steps.append("CEO split the work: Marketing and Research run in parallel")

            # 3. Impersonation: the Founder acts as Aria and adds a task in her name (both identities logged)
            post(web, "/impersonate/start", {"employee": people["Aria"], "reason": "Aria is out of office"})
            post(web, "/tasks", {"title": "Check the brand colours", "owner": people["Aria"], "priority": "3", "risk_tier": "1"})
            post(web, "/impersonate/stop")
            steps.append("Founder acted as Aria for one task")

            # 4. Interrupt: the Founder takes over the long research task, adds a note, lets the agent continue
            to = takeover.start(con, home, scan)
            assert wait(lambda: takeover.get(con, to)["status"] == "active", 20, pump)
            takeover.add_note(con, to, "Skip competitor 6, it is out of business.")
            takeover.end(con, to, "resume")
            steps.append("Founder took over the research task, left a note and resumed it")

            # 5. The proposal reaches the Founder for approval (tier 3 = draft only)
            assert wait(lambda: tasks.get_task(con, consolidate)["status"] == "awaiting_approval", 60, pump), tasks.get_task(con, consolidate)["status"]
            assert tasks.get_task(con, messaging)["status"] == "done" and tasks.get_task(con, scan)["status"] == "done"
            assert runner.max_concurrent >= 2, "the two teams did not work in parallel"
            note = " ".join(c["text"] for c in advisor.challenge_note(con, consolidate))
            assert note
            steps.append("Proposal waiting for approval; the Advisor attached a challenge note")

            # 6. Nag escalation: the approval has waited 5 hours -> Telegram reminder
            attention_sync(con, home)
            con.execute("UPDATE notification SET created_at = ? WHERE category = 'approval' AND resolved_at IS NULL", ((datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),))
            midday = datetime.now(timezone.utc).replace(hour=13, minute=0, second=0, microsecond=0)
            midday += timedelta(days=1) if midday < datetime.now(timezone.utc) + timedelta(hours=1) else timedelta(0)      # the next 13:00: outside quiet hours
            made = advisor.nag(con, midday, tz=timezone.utc)
            notifier.pump(con)
            tg = [json.loads(x) for x in (out / "telegram.jsonl").read_text().splitlines()]
            assert made and any("approval" in m["text"].lower() or "approve" in m["text"].lower() or "waiting" in m["text"].lower() for m in tg), tg
            steps.append("The Advisor nagged on Telegram about the unanswered approval")

            # 7. Telegram: the Founder asks for status and what is pending
            chat.ingest(con, home, Inbound("telegram", "4242", "/status", "e2e-1"))
            chat.ingest(con, home, Inbound("telegram", "4242", "/pending", "e2e-2"))
            chat.ingest(con, home, Inbound("telegram", "999", "/stop", "e2e-3"))                  # a stranger
            chat.pump_outbound(con, notifier.transports)
            replies = [json.loads(x)["text"] for x in (out / "telegram.jsonl").read_text().splitlines()]
            assert any("Engine:" in t and "Tasks:" in t for t in replies) and any("Consolidate the proposal" in t for t in replies)
            assert not (home / "STOP").exists()
            steps.append("Telegram /status and /pending answered; a stranger's /stop was ignored")

            # 8. The Founder approves in the dashboard
            page = web.get("/approvals").text
            assert "Consolidate the proposal" in page and "Approve the draft" in page
            post(web, f"/tasks/{consolidate}/decide", {"decision": "approved", "note": "Go ahead", "next": "/approvals"})
            assert wait(lambda: tasks.get_task(con, top)["status"] == "done" or tasks.get_task(con, consolidate)["status"] == "done", 20, pump)
            assert tasks.get_task(con, consolidate)["status"] == "done"
            steps.append("Founder approved the proposal")
        finally:
            engine.shutdown()

        # ---------------------------------------------------------------- the audit trail
        assert events.verify_chain(con).ok and events.verify_incremental(con).ok
        assert_transitions_are_logged(con)
        every = [r["task_id"] for r in con.execute("SELECT task_id FROM task")]
        for t in every:
            assert con.execute("SELECT count(*) FROM event WHERE task_id = ?", (t,)).fetchone()[0] >= 2
        trail = [r["type"] for r in con.execute("SELECT type FROM event WHERE task_id = ? ORDER BY id", (consolidate,))]
        for needed in ("task.created", "task.running", "task.awaiting_approval", "task.approval_decided"):
            assert needed in trail, (needed, trail)
        assert any(r["mode"] == "impersonate" and r["actor_name"] == "Founder" and r["acting_as_name"] == "Aria" for r in search.search_events(con, actor="Aria", limit=100))
        assert any(r["mode"] == "interrupt" and r["acting_as_name"] == "Rae" for r in search.search_events(con, mode="interrupt", limit=100))
        assert search.search_events(con, "launch", limit=50) and search.search_events(con, "Telegram", limit=50)
        assert {"Founder", "CEO", "Aria", "Rae", "Advisor"} <= {r[0] for r in con.execute("SELECT DISTINCT actor_name FROM event")}
        assert con.execute("SELECT count(*) FROM chat_message").fetchone()[0] == 5
        assert con.execute("SELECT count(*) FROM event WHERE type = 'chat.ignored'").fetchone()[0] == 1
        total_events = con.execute("SELECT count(*) FROM event").fetchone()[0]

        # ---------------------------------------------------------------- restore on "another laptop"
        archive = __import__("aicorp.backup", fromlist=["x"]).create_backup(home, tmp_path / "usb")
        elsewhere = tmp_path / "new-laptop"
        env = {**os.environ, "AICORP_HOME": str(elsewhere), "PYTHONPATH": SRC}
        env.pop("AICORP_TRANSPORT_DIR", None)
        done = subprocess.run([sys.executable, "-m", "aicorp.cli", "restore", str(archive), str(elsewhere)], env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert not (elsewhere / "config" / "secrets.env").exists()                              # secrets never travel; the Founder re-enters them
        for sub in ("config",):
            (elsewhere / sub).mkdir(exist_ok=True)
        check = subprocess.run([sys.executable, "-m", "aicorp.cli", "verify-chain"], env=env, capture_output=True, text=True)
        assert check.returncode == 0 and "ok=True" in check.stdout.replace(" ", ""), check.stdout + check.stderr
        con2 = connect(db_path(elsewhere))
        assert con2.execute("SELECT count(*) FROM event").fetchone()[0] == total_events
        assert {r["title"]: r["status"] for r in con2.execute("SELECT title, status FROM task")} == {r["title"]: r["status"] for r in con.execute("SELECT title, status FROM task")}
        assert events.head(con2) == events.head(con)
        assert [r["id"] for r in search.search_events(con2, "launch", limit=50)] == [r["id"] for r in search.search_events(con, "launch", limit=50)]
        # and the restored company works: a new task runs there
        new_id = con2.execute("SELECT emp_id FROM employee WHERE name = 'Ben'").fetchone()[0]
        t2 = tasks.create_task(con2, title="First task on the new laptop [quick]", owner=new_id)
        engine2 = Orchestrator(elsewhere, FakeRunner(duration=(0.1, 0.1)), threaded=False, cap=2, ram_probe=lambda: 0.1)
        engine2.start()
        try:
            engine2.tick()
        finally:
            engine2.shutdown()
        assert tasks.get_task(con2, t2)["status"] == "done" and events.verify_chain(con2).ok
        steps.append("Backup restored on a fresh folder: same events, same tasks, same search results, and it works")
    with capsys.disabled():
        print("\n[M9] scenario passed:\n" + "\n".join(f"[M9]  {i + 1}. {s}" for i, s in enumerate(steps)) + f"\n[M9] audit log: {total_events} events, hash chain intact")


def attention_sync(con, home):
    from aicorp import attention
    attention.sync(con, home)

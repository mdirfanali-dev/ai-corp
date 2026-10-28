"""Build a throw-away demo company (invented names, simulated work) and save screenshots for docs/USER-GUIDE.md.

    python scripts/make_screenshots.py            # needs: pip install playwright && playwright install chromium

Nothing from your real company folder is read or written.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from aicorp import org, projects, tasks  # noqa: E402
from aicorp.config import db_path, utc_now  # noqa: E402
from aicorp.db import connect, migrate  # noqa: E402
from aicorp.web.app import create_app  # noqa: E402


def build(home: Path) -> None:
    for name in ("company", "skills", "memory", "artifacts", "workspaces", "config", "database"):
        (home / name).mkdir(parents=True, exist_ok=True)
    con = connect(db_path(home))
    migrate(con)
    org.seed_system(con, home)
    role = org.create_role(con, {"name": "Specialist", "execute": True})
    people = {n: org.create_employee(con, home, {"name": n, "role_ids": [role]}) for n in ("Aria Khan", "Ben Ortiz", "Rae Lindqvist", "Sam Okafor", "Mia Tanaka")}
    org.create_team(con, {"name": "Marketing", "head_emp_id": people["Aria Khan"], "member_ids": [people["Ben Ortiz"]]})
    org.create_team(con, {"name": "Research", "head_emp_id": people["Rae Lindqvist"], "member_ids": [people["Sam Okafor"], people["Mia Tanaka"]]})
    pid = projects.create_project(con, {"name": "Q4 launch", "goal": "Ship the new pricing page"})
    now = datetime.now(timezone.utc)
    t1 = tasks.create_task(con, title="Draft the launch messaging", owner=people["Aria Khan"], project_id=pid)
    t2 = tasks.create_task(con, title="Competitor scan", owner=people["Rae Lindqvist"], project_id=pid)
    t3 = tasks.create_task(con, title="Collect pricing screenshots", owner=people["Sam Okafor"], project_id=pid)
    t4 = tasks.create_task(con, title="Consolidate the proposal", owner=people["Mia Tanaka"], project_id=pid, risk_tier=3)
    late = tasks.create_task(con, title="Update the FAQ", owner=people["Ben Ortiz"], project_id=pid)
    con.execute("UPDATE task SET status = 'running' WHERE task_id IN (?, ?)", (t1, t2))
    con.execute("UPDATE employee SET status = 'working' WHERE emp_id IN (?, ?)", (people["Aria Khan"], people["Rae Lindqvist"]))
    con.execute("UPDATE task SET status = 'blocked' WHERE task_id = ?", (t3,))
    tasks.open_block(con, t3, "CAPTCHA", "The page wants proof that you are human. A browser window is open: finish it there, then press Resume.")
    con.execute("UPDATE employee SET status = 'blocked' WHERE emp_id = ?", (people["Sam Okafor"],))
    con.execute("UPDATE task SET status = 'awaiting_approval', result_summary = ? WHERE task_id = ?", ("Draft launch proposal: positioning, pricing tiers, timeline and risks. Files: proposal.md", t4))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, ?)", (t4, (now - timedelta(hours=6)).isoformat()))
    con.execute("INSERT INTO artifact (artifact_id, task_id, path, sha256, created_at) VALUES ('A-demo', ?, 'workspaces/employees/demo/proposal.md', '', ?)", (t4, utc_now()))
    con.execute("UPDATE task SET due_at = ? WHERE task_id = ?", ((now - timedelta(days=2)).isoformat(), late))
    projects.create_meeting(con, home, {"title": "Weekly planning", "project_id": pid, "scheduled_at": (now + timedelta(hours=20)).astimezone().strftime("%Y-%m-%dT%H:%M")})
    from aicorp import attention
    attention.sync(con, home)
    con.close()


def main() -> int:
    out = ROOT / "docs" / "img"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aicorp-demo-") as tmp:
        home = Path(tmp)
        build(home)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(home), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 760})
            for url, name in (("/", "command-center"), ("/advisor", "advisor-desk"), ("/approvals", "approvals"), ("/usage", "usage"), ("/logs", "logs")):
                page.goto(f"http://127.0.0.1:{port}{url}")
                page.wait_for_timeout(1500)
                page.screenshot(path=str(out / f"{name}.png"))
                print("saved", name)
            browser.close()
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())

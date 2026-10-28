"""Acceptance in a real browser: three projects, two meetings and stale work all show on the Desk,
and review mode walks through them one by one."""
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn

from aicorp import projects, tasks
from aicorp.config import db_path
from aicorp.db import connect, migrate
from aicorp.web.app import create_app
from helpers import staff

sync_api = pytest.importorskip("playwright.sync_api")


def stamp(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).astimezone().strftime("%Y-%m-%dT%H:%M")


@pytest.fixture
def served(home):
    con = connect(db_path(home))
    migrate(con)
    ids = staff(con, home, 3)
    pids = [projects.create_project(con, {"name": n}) for n in ("Micro SaaS", "Study plan", "Blog")]
    now = datetime.now(timezone.utc)
    for i, pid in enumerate(pids):                                   # stale and overdue work in each project
        t = tasks.create_task(con, title=f"Stale task {i}", owner=ids[i], project_id=pid)
        con.execute("UPDATE task SET updated_at = ? WHERE task_id = ?", ((now - timedelta(days=4)).isoformat(), t))
    late = tasks.create_task(con, title="Overdue invoice", owner=ids[0], project_id=pids[0])
    con.execute("UPDATE task SET due_at = ? WHERE task_id = ?", ((now - timedelta(days=2)).isoformat(), late))
    projects.create_meeting(con, home, {"title": "Weekly planning", "project_id": pids[0], "scheduled_at": stamp(5)})
    projects.create_meeting(con, home, {"title": "Content review", "project_id": pids[2], "scheduled_at": stamp(-30)})     # missed
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(home), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield con, f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_desk_lists_everything_and_review_mode_walks_through_it(served):
    con, base = served
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1250, "height": 900})
        problems = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(base + "/advisor")
        text = page.inner_text("main")
        for expected in ("Micro SaaS", "Study plan", "Blog", "Overdue: Overdue invoice", "Weekly planning", "Did this meeting happen? Content review"):
            assert expected in text, expected
        for i in range(3):
            assert f"Stale task {i}" in text
        total = int(page.locator(".tab.on .count").inner_text())
        assert total >= 6
        page.click("text=Review one by one")
        page.wait_for_selector("text=Item 1 of")
        count_text = page.inner_text("main")
        review_total = int(count_text.split("Item 1 of ")[1].split()[0])
        assert review_total == total
        steps = 0
        titles = []
        while page.locator("text=I have seen it").count():
            titles.append(page.locator(".item .title").first.inner_text())
            page.click("text=I have seen it")
            page.wait_for_load_state()
            steps += 1
            assert steps <= review_total + 2
        assert steps == review_total and len(set(titles)) == review_total          # every item shown once, no repeats
        assert "All reviewed" in page.inner_text("main")
        page.goto(base + "/advisor")
        assert page.locator(".item").count() == total and page.locator("text=acknowledged").count() == total   # still on the desk, now acknowledged
        # a different view: "this week" / overdue tabs and snoozing
        page.click("text=Overdue")
        assert "Overdue invoice" in page.inner_text("main") and "Weekly planning" not in page.inner_text("main")
        assert problems == [], problems
        browser.close()

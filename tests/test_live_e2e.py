"""Browser test: real dashboard server + real engine (simulated runner) + headless Chromium."""
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
import uvicorn

from aicorp import org, tasks
from aicorp.config import db_path
from aicorp.db import connect, migrate
from aicorp.engine import Orchestrator
from aicorp.runners import FakeRunner
from aicorp.web.app import create_app
from helpers import staff

sync_api = pytest.importorskip("playwright.sync_api")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(home):
    con = connect(db_path(home))
    migrate(con)
    ids = staff(con, home, 6)                       # W1..W6
    org.create_team(con, {"name": "Marketing", "head_emp_id": ids[0], "member_ids": [ids[1], ids[2]]})
    org.create_team(con, {"name": "Development", "member_ids": [ids[3], ids[4]]})
    app = create_app(home)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    engine = Orchestrator(home, FakeRunner(duration=(4.0, 4.0), seed=1), cap=5)
    stop = threading.Event()
    thread = threading.Thread(target=lambda: engine.run_forever(poll=0.2, stop=stop), daemon=True)
    thread.start()
    try:
        yield {"con": con, "home": home, "ids": ids, "base": f"http://127.0.0.1:{port}"}
    finally:
        stop.set()
        thread.join(timeout=15)
        server.should_exit = True
        con.close()


def test_command_center_updates_live_and_matches_reality(live):
    con, ids, base = live["con"], live["ids"], live["base"]
    longs = [tasks.create_task(con, title=f"long job {i}", owner=ids[i]) for i in range(4)]
    waits = tasks.create_task(con, title="waits for a long job", owner=ids[5], depends_on=[longs[0]])
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        problems = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.goto(base + "/")

        # --- the page connects live and the running count equals the real number of workers
        page.wait_for_selector("#conn.on", timeout=8000)
        page.wait_for_function("document.getElementById('st-running').textContent.startsWith('4')", timeout=10000)
        for _ in range(60):                                                 # the engine writes its status a moment after the tasks start
            in_db = con.execute("SELECT count(*) FROM task WHERE status='running'").fetchone()[0]
            in_engine = con.execute("SELECT running FROM engine_status").fetchone()[0]
            if in_db == in_engine == 4:
                break
            time.sleep(0.1)
        shown = int(page.inner_text("#st-running").split("/")[0])
        assert shown == in_db == in_engine == 4
        assert page.inner_text("#st-running").strip() == "4 / 5"
        assert int(page.inner_text("#st-queue")) == 1                      # the dependent task waits
        assert page.locator(".card.s-working").count() >= 4                 # lanes + (none of the leaders work)

        # --- team lanes agree with the database
        lane_working = {}
        for lane in page.locator("section.lane").all():
            name = lane.locator(".lane-title").first.inner_text()
            lane_working[name] = lane.locator(".card.s-working").count()
        expected = {"Marketing": 0, "Development": 0}
        for row in con.execute("SELECT owner_emp_id FROM task WHERE status='running'"):
            for team in con.execute("SELECT t.name FROM team_member m JOIN team t USING (team_id) WHERE m.emp_id=?",
                                    (row[0],)):
                expected[team[0]] += 1
        assert {k: v for k, v in lane_working.items() if k in expected} == expected
        # "under whom" and progress are displayed
        assert "under" not in page.inner_text(".card[data-name='W1']")      # W1 heads Marketing, reports to nobody
        assert page.locator(".bar-fill").count() >= 4

        # --- dependency graph draws the edge
        page.wait_for_selector("svg.graph-svg path.edge", state="attached", timeout=5000)  # a flat edge has zero height

        # --- a blocked employee appears within 2 seconds of becoming blocked
        blocked = tasks.create_task(con, title="needs a human [quick] [block:CAPTCHA]", owner=ids[4])
        page.wait_for_selector(".card[data-name='W5'].s-blocked", timeout=6000)
        seen_at = datetime.now(timezone.utc)
        since = datetime.fromisoformat(con.execute("SELECT since FROM block WHERE task_id=?", (blocked,)).fetchone()[0])
        delay = (seen_at - since).total_seconds()
        print(f"\n[timing] blocked employee visible {delay:.2f}s after the task became blocked")
        assert delay <= 2.0, f"took {delay:.2f}s"
        assert "blocked: captcha" in page.inner_text(".card[data-name='W5']").lower()
        assert int(page.inner_text("#st-blocked")) == 1 and int(page.inner_text("#st-need")) == 1
        assert "blocked" in page.inner_text("#feed").lower()                # live feed got it without reload

        # --- offline / sleep: the page catches up by itself afterwards
        page.context.set_offline(True)
        org.create_employee(con, live["home"], {"name": "Added While Offline"})
        page.wait_for_selector("#conn.off", timeout=10000)
        page.context.set_offline(False)
        page.wait_for_selector(".card[data-name='Added While Offline']", timeout=15000)
        page.wait_for_selector("#conn.on", timeout=15000)

        # --- data is never interpreted as HTML
        org.create_employee(con, live["home"], {"name": "<img src=x onerror=window.__pwned=1>"})
        page.wait_for_selector(".card[data-name='<img src=x onerror=window.__pwned=1>']", timeout=8000)
        assert page.evaluate("window.__pwned") is None
        assert page.locator("img[src='x']").count() == 0

        # --- actions not built yet are visibly disabled, links work
        assert page.locator(".card[data-name='W1'] a:has-text('Impersonate')").get_attribute("href").startswith("/impersonate?emp=")
        page.click(".card[data-name='W1'] a:has-text('Log')")
        page.wait_for_url("**/logs?actor=W1")
        unexpected = [p for p in problems if "ERR_INTERNET_DISCONNECTED" not in p]   # expected while offline
        assert unexpected == [], unexpected
        browser.close()
    assert waits  # created above; its dependency edge was drawn

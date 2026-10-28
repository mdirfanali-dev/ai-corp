"""Acceptance in a real browser: impersonate from the Command Center, see both identities in the log,
take over a task, and watch a protected-path write being refused even under the Founder override."""
import socket
import threading
import time

import pytest
import uvicorn

from aicorp.config import db_path
from aicorp.db import connect, migrate
from aicorp.web.app import create_app
from helpers import staff

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def served(home):
    con = connect(db_path(home))
    migrate(con)
    ids = staff(con, home, 2)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(home), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield con, ids, f"http://127.0.0.1:{port}", home
    server.should_exit = True


def test_impersonate_log_both_identities_and_take_over(served):
    con, ids, base, home = served
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base + "/")
        # Impersonate W1 from the Command Center card
        page.locator(".card[data-name='W1'] a:has-text('Impersonate')").click()
        page.fill("#reason", "covering")
        page.click("text=Start acting")
        banner = page.inner_text(".banner")
        assert "acting as W1" in banner
        # create a task while acting
        page.goto(base + "/tasks/new")
        page.fill("input[name=title]", "Draft the FAQ")
        page.select_option("select[name=owner]", ids[0])
        page.click("main button[type=submit]")
        page.goto(base + "/logs?mode=impersonate")
        row = page.inner_text("table")
        assert "Founder" in row and "acting as W1" in row and "Draft the FAQ" in row
        # stop, then take the task over
        page.click("text=Stop acting as W1")
        assert page.locator(".banner").count() == 0
        task_id = con.execute("SELECT task_id FROM task WHERE title = 'Draft the FAQ'").fetchone()[0]
        page.goto(f"{base}/tasks/{task_id}/takeover")
        page.click("text=Interrupt and take over")
        assert "Where the agent was" in page.inner_text("main")
        page.fill("textarea[name=text]", "I will write this one myself")
        page.click("text=Save note")
        assert "I will write this one myself" in page.inner_text("main")
        # a protected write is refused, a normal write works
        page.fill("input[name=path]", "../../config/secrets.env")
        page.fill("textarea[name=content]", "pwned")
        page.click("text=Write file")
        assert "BLOCKED by AICorp policy" in page.inner_text(".flash")
        assert not (home / "config" / "secrets.env").exists()
        page.fill("input[name=path]", "faq.md")
        page.fill("textarea[name=content]", "# FAQ")
        page.click("text=Write file")
        page.fill("#result", "FAQ done - see faq.md")
        page.click("text=Mark done with this result")
        assert "done" in page.inner_text("h1").lower()
        # override: red banner, protected path still refused
        page.goto(base + "/impersonate")
        page.select_option("#employee", ids[1])
        page.click("summary")
        page.check("input[name=override]")
        page.fill("#confirm", "OVERRIDE")
        page.click("text=Start acting")
        assert page.locator(".banner.override").count() == 1 and "no permission limits" in page.inner_text(".banner")
        page.goto(base + "/logs?mode=impersonate")
        assert page.locator(".badge.override").count() >= 1
        browser.close()
    assert not (home / "config" / "secrets.env").exists()

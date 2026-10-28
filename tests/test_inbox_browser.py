"""The inbox in a real browser: bell, dropdown, select-all, and that hidden things stay hidden."""
import socket
import threading
import time

import pytest
import uvicorn

from aicorp import notify
from aicorp.config import db_path
from aicorp.db import connect, migrate
from aicorp.web.app import create_app

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def served(home):
    con = connect(db_path(home))
    migrate(con)
    for i in range(4):
        notify.upsert(con, key=f"k{i}", category="blocked", severity="critical" if i == 0 else "warn",
                      title=f"Alert number {i}", detail="something needs you", link="/attention")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(home), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield con, f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_bell_dropdown_select_all_and_hiding(served):
    con, base = served
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        problems = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.goto(base + "/inbox")
        assert page.inner_text("#bell-count").strip() == "4"
        page.click("#bell-btn")
        page.wait_for_selector(".bellitem")
        assert page.locator(".bellitem").count() == 4 and "Alert number" in page.inner_text("#bell-panel")
        page.click("#bell-btn")
        assert page.locator("#bell-panel").is_hidden()
        # select-all selects every row, mark read empties the bell and hides the badge
        page.check("#select-all")
        assert page.locator("input[name=ids]:checked").count() == 4
        page.uncheck("#select-all")
        assert page.locator("input[name=ids]:checked").count() == 0
        page.check("input[name=ids] >> nth=0")
        page.click("button[value=read]")
        page.wait_for_url("**/inbox**")
        assert page.inner_text("#bell-count").strip() == "3" and notify.unread_count(con) == 3
        page.check("#select-all")
        page.click("button[value=read]")
        page.wait_for_selector("#bell-count[hidden]", state="attached")
        assert page.locator("#bell-count").is_hidden() and notify.unread_count(con) == 0
        # the bell notices a new notification by itself (poll)
        notify.upsert(con, key="new", category="system", severity="critical", title="Brand new")
        page.wait_for_function("document.getElementById('bell-count') && !document.getElementById('bell-count').hidden", timeout=15000)
        assert page.inner_text("#bell-count").strip() == "1" and "critical" in page.get_attribute("#bell-count", "class")
        # the "mark all read" button inside the dropdown works too
        page.click("#bell-btn")
        page.wait_for_selector(".bellitem")
        page.click("#bell-panel button")
        page.wait_for_function("document.getElementById('bell-count').hidden", timeout=15000)
        assert notify.unread_count(con) == 0
        # settings: credential fields are styled inputs and never prefilled
        page.goto(base + "/settings")
        for field in ("TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD"):
            box = page.locator(f"#{field}")
            assert box.input_value() == "" and box.bounding_box()["width"] > 300
        assert problems == [], problems
        browser.close()

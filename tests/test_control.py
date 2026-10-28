"""The control panel: token protection, the API, and the page in a real browser (layouts, views, animated exit)."""
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aicorp import config, control, doctor
from aicorp.supervisor import Supervisor

pw = pytest.importorskip("playwright.sync_api")


def script(tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text(body)
    return [sys.executable, "-u", str(path)]


STEADY = "import time\nprint('{n} says hello', flush=True)\nwhile True:\n    time.sleep(0.2)\n"


@pytest.fixture
def sup(tmp_path):
    home = config.ensure_layout(tmp_path / "data")
    argv = {n: script(tmp_path, n, STEADY.replace("{n}", n)) for n in ("web", "watchdog", "engine")}
    return Supervisor(home, argv=argv, conflict_check=lambda: [], stop_timeout=2, restart_delay=0.05, ready_timeout=2, port=18801,
                      env={"PATH": os.environ["PATH"]})


def token_of(client):
    html = client.get("/").text
    return html.split('const TOKEN="')[1].split('"')[0]


def test_changes_need_the_token_and_a_local_origin(sup):
    client = TestClient(control.create_control_app(sup), base_url="http://127.0.0.1")
    for path in ("/api/start", "/api/stop", "/api/exit", "/api/restart/web", "/api/doctor/fix/venv"):
        assert client.post(path).status_code == 403, path
    good = {"X-Control-Token": token_of(client)}
    assert client.post("/api/start", headers={**good, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/start", headers={"X-Control-Token": "wrong"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400          # other host names are refused
    assert client.post("/api/doctor/fix/rm-rf", headers=good).status_code == 404


def test_start_state_and_stop_through_the_api(sup):
    client = TestClient(control.create_control_app(sup), base_url="http://127.0.0.1")
    h = {"X-Control-Token": token_of(client)}
    assert client.get("/api/state").json()["running"] is False
    r = client.post("/api/start", headers=h, json={"mode": "fake"})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.post("/api/start", headers=h, json={}).status_code == 409                # already running
    time.sleep(1.5)
    state = client.get("/api/state").json()
    assert state["running"] and {p["name"] for p in state["processes"]} == {"web", "watchdog", "engine"}
    assert client.post("/api/stop", headers=h).json()["ok"]
    end = time.time() + 10
    while time.time() < end and client.get("/api/state").json()["running"]:
        time.sleep(0.1)
    assert not client.get("/api/state").json()["running"]


def test_doctor_endpoint_and_view_only_layout(sup):
    client = TestClient(control.create_control_app(sup), base_url="http://127.0.0.1")
    data = client.get("/api/doctor").json()
    assert {c["id"] for c in data["checks"]} >= {"python", "port", "worker"} and "summary" in data
    for check in data["checks"]:
        if check["status"] != "ok":
            assert "step by step" in data["prompts"][check["id"]]
    # there is no endpoint that a layout choice could call: the page keeps it in the browser only
    routes = {r.path for r in control.create_control_app(sup).routes}
    assert not any("layout" in r for r in routes)


# ------------------------------------------------------------------ real browser

@pytest.fixture
def live(sup):
    exited = threading.Event()
    app = control.create_control_app(sup, on_exit=exited.set)
    port = doctor.find_free_port(doctor.Probe(sup.home), 18850)
    server, _ = control.serve(app, port)
    end = time.time() + 10
    while not server.started and time.time() < end:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", sup, exited
    server.should_exit = True
    sup.stop()


@pytest.fixture
def page():
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"] if os.environ.get("AICORP_BROWSER_NO_SANDBOX") == "1" else [])
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        yield context.new_page()
        browser.close()


def visible_panes(page):
    return [n for n in ("web", "watchdog", "engine") if page.locator(f"#pane-{n}").is_visible()]


def test_panel_in_a_real_browser(live, page):
    url, sup, exited = live
    page.goto(url)
    page.wait_for_selector("#doctorsum")
    assert "System check" in page.inner_text("#doctorsum")

    # start: three live log panes
    page.click("#onoff")
    for name in ("web", "watchdog", "engine"):
        page.wait_for_function(f"document.querySelector('#log-{name}').innerText.includes('{name} says hello')", timeout=10000)
    page.wait_for_function("document.querySelector('#dot-engine').className.includes('running')", timeout=10000)
    assert visible_panes(page) == ["web", "watchdog", "engine"]
    assert page.inner_text("#onoff") == "Stop"

    # layouts and visibility change only the view
    before = {p["name"]: (p["state"], p["pid"]) for p in sup.state()["processes"]}
    page.click("#layouts button[data-l=single]")
    assert visible_panes(page) == ["web"]
    page.click("#layouts button[data-l=cols]")
    cols = page.evaluate("getComputedStyle(document.querySelector('#logs')).gridTemplateColumns.split(' ').length")
    page.click("#layouts button[data-l=rows]")
    rows_cols = page.evaluate("getComputedStyle(document.querySelector('#logs')).gridTemplateColumns.split(' ').length")
    assert cols == 3 and rows_cols == 1
    page.click("#layouts button[data-l=grid]")
    page.uncheck("#chips input[value=watchdog]")
    assert visible_panes(page) == ["web", "engine"]
    page.check("#chips input[value=watchdog]")
    assert visible_panes(page) == ["web", "watchdog", "engine"]
    assert {p["name"]: (p["state"], p["pid"]) for p in sup.state()["processes"]} == before          # nothing restarted or stopped

    # the choice is remembered
    page.click("#layouts button[data-l=rows]")
    page.reload()
    page.wait_for_selector("#layouts button.on[data-l=rows]")

    # views: control / dashboard / split (the iframe points at the dashboard address)
    page.click("#views button[data-v=split]")
    page.wait_for_selector("#dash.show")
    assert page.get_attribute("#dash", "src") == sup.dashboard_url()
    assert page.locator("#logs").is_visible()
    page.click("#views button[data-v=dashboard]")
    assert not page.locator("#logs").is_visible() and page.locator("#dash").is_visible()
    page.click("#views button[data-v=control]")
    assert not page.locator("#dash").is_visible() and page.locator("#logs").is_visible()

    # Exit: confirm, animated steps in order, everything off, then the app is told to close
    page.click("#exit")
    page.wait_for_selector("#confirm.show")
    page.click("#no")
    assert sup.state()["running"]
    page.click("#exit")
    page.click("#yes")
    page.wait_for_selector("#shutdown.show")
    page.wait_for_function("document.querySelector('#step-done').className.includes('off')", timeout=30000)
    for step in ("engine", "watchdog", "web", "browsers", "done"):
        assert "off" in page.get_attribute(f"#step-{step}", "class"), step
    assert page.inner_text("#sdtitle") == "All off"
    assert exited.wait(8)
    assert not sup.state()["running"]


def test_failures_are_shown_plainly(live, page):
    url, sup, _ = live
    sup.conflict_check = lambda: [("engine", 4242)]
    page.goto(url)
    page.click("#onoff")
    page.wait_for_selector("#toast", state="visible")
    assert "outside the control panel" in page.inner_text("#toast")
    assert not sup.state()["running"]

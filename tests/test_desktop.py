"""The Windows desktop app's logic against a stand-in for wsl.exe that runs the commands locally, plus its pages in a real browser."""
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

import shell  # noqa: E402
import wslbridge as wb  # noqa: E402
from aicorp import doctor  # noqa: E402

FAKE_WSL = '''#!{python}
import os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_WSL_MODE", "ok")
def out16(text):
    sys.stdout.buffer.write(("\\ufeff" + text).encode("utf-16-le"))
    sys.exit(0)
if args[:1] == ["--status"]:
    sys.exit(1 if mode == "nowsl" else 0)
if args[:2] == ["-l", "-v"]:
    if mode == "nodistro":
        out16("  NAME STATE VERSION\\r\\n")
    ver = "1" if mode == "wsl1" else "2"
    out16("  NAME            STATE           VERSION\\r\\n* Ubuntu-24.04    Running         " + ver + "\\r\\n  Other           Stopped         2\\r\\n")
if args[:1] == ["-d"]:
    distro, rest = args[1], args[2:]
    if rest[:1] == ["--cd"]:
        os.chdir(os.path.expanduser(rest[1]))
        rest = rest[2:]
    assert rest[0] == "-e"
    cmd = rest[1:]
    cmd = [os.environ["FAKE_WSL_PYTHON"] if c == "python3" else c for c in cmd]
    if cmd[0] == "bash":
        os.execvp("bash", cmd)
    os.execvp(cmd[0], cmd)
sys.exit(2)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "fake-wsl"
    exe.write_text(FAKE_WSL.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setenv("FAKE_WSL_PYTHON", sys.executable)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return wb.Bridge(str(exe))


def settings_for(tmp_path, **kw):
    return {"distro": None, "project": str(ROOT), "data_dir": str(tmp_path / "data"), **kw}


# ------------------------------------------------------------------ parsing and settings

def test_wsl_output_decoding_and_distro_list():
    raw = ("  NAME            STATE           VERSION\r\n* Ubuntu-24.04    Running         2\r\n  docker-desktop  Stopped         2\r\n").encode("utf-16-le")
    assert wb.decode(b"\xff\xfe" + raw).startswith("NAME") or "NAME" in wb.decode(raw)
    found = wb.parse_distros(wb.decode(raw))
    assert [(d.name, d.default, d.state, d.version) for d in found] == [("Ubuntu-24.04", True, "Running", "2"), ("docker-desktop", False, "Stopped", "2")]
    assert wb.decode("plain utf8 é".encode()) == "plain utf8 é"
    assert wb.URL_RE.search("AICorp control panel: http://127.0.0.1:8760/   dashboard: http://127.0.0.1:8765/").group(1) == "http://127.0.0.1:8760/"


def test_settings_roundtrip_and_defaults(tmp_path):
    p = tmp_path / "s" / "settings.json"
    assert wb.load_settings(p) == {"distro": None, "project": None, "data_dir": None}
    wb.save_settings({"distro": "Ubuntu", "project": " ~/ai-corp ", "data_dir": "", "junk": "x"}, p)
    assert wb.load_settings(p) == {"distro": "Ubuntu", "project": "~/ai-corp", "data_dir": None}
    p.write_text("not json")
    assert wb.load_settings(p)["distro"] is None


# ------------------------------------------------------------------ checks

def by_id(results):
    return {r.id: r for r in results}


def test_windows_side_checks(fake, tmp_path, monkeypatch):
    for mode, expect in (("nowsl", "not installed"), ("nodistro", "no Linux distribution"), ("wsl1", "WSL 1")):
        monkeypatch.setenv("FAKE_WSL_MODE", mode)
        results, _ = wb.windows_checks(fake, settings_for(tmp_path))
        wsl = by_id(results)["wsl"]
        assert wsl.status == doctor.FAIL and expect in wsl.detail and wsl.needs_admin
    monkeypatch.setenv("FAKE_WSL_MODE", "ok")
    assert by_id(wb.windows_checks(wb.Bridge("no-such-wsl-program"), settings_for(tmp_path))[0])["wsl"].status == doctor.FAIL
    results, settings = wb.windows_checks(fake, settings_for(tmp_path))
    assert [r.status for r in results] == ["ok", "ok"] and settings["distro"] == "Ubuntu-24.04" and settings["project"] == str(ROOT)
    missing, _ = wb.windows_checks(fake, settings_for(tmp_path, project=str(tmp_path / "nope")))
    assert by_id(missing)["project"].status == doctor.FAIL and "Settings" in by_id(missing)["project"].next_step


def test_the_aicorp_folder_is_found_in_the_usual_places(fake, tmp_path):
    (tmp_path / "home" / "ai-corp").mkdir()
    (tmp_path / "home" / "ai-corp" / "pyproject.toml").write_text("")
    assert wb.find_project(fake, "Ubuntu-24.04") == str(tmp_path / "home" / "ai-corp")


def test_the_checker_runs_inside_linux_and_fixes_are_whitelisted(fake, tmp_path):
    results, settings = wb.all_checks(fake, settings_for(tmp_path))
    ids = {r.id for r in results}
    assert {"wsl", "project", "python", "port", "worker"} <= ids and not wb.blocking([r for r in results if r.id in ("wsl", "project")])
    broken = wb.wsl_doctor(wb.Bridge("no-such-wsl-program"), {"distro": "x", "project": "y", "data_dir": None})
    assert broken[0].status == doctor.FAIL and "Python" in broken[0].title


def test_help_prompt_from_windows_side_problems_leaks_nothing(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_MODE", "nowsl")
    monkeypatch.setenv("USERNAME", "alicesmith")
    results, _ = wb.windows_checks(fake, settings_for(tmp_path))
    text = wb.help_prompt(results)
    assert "WSL" in text and "step by step" in text and "alicesmith" not in text and str(tmp_path) not in text and str(ROOT) not in text


# ------------------------------------------------------------------ the running app

def alive_pids(pids):
    return [p for p in pids if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]


def test_app_starts_through_the_bridge_and_stops_when_the_input_closes(fake, tmp_path):
    app = wb.AppProcess(fake, settings_for(tmp_path, distro="Ubuntu-24.04"), port=18960)
    app.start()
    try:
        url = app.wait_for_url(60)
        assert url and url.endswith(":18960/") and wb.reachable(url, 30)
        family = [p.pid for p in psutil.Process(app.proc.pid).children(recursive=True)]
        assert len(family) >= 3 and app.alive()
    finally:
        app.stop(30)                                    # closing standard input is the whole protocol
    assert not app.alive() and app.proc.returncode == 0
    time.sleep(0.5)
    assert not alive_pids(family)
    assert not wb.reachable(url, 1.5)


def test_if_the_window_program_dies_the_system_shuts_down(fake, tmp_path):
    app = wb.AppProcess(fake, settings_for(tmp_path, distro="Ubuntu-24.04"), port=18961)
    app.start()
    url = app.wait_for_url(60)
    assert url and wb.reachable(url, 30)
    family = [p.pid for p in psutil.Process(app.proc.pid).children(recursive=True)]
    app.proc.stdin.close()                              # what a crashed Windows program looks like from inside Linux
    app.proc.stdin = None
    assert app.proc.wait(40) == 0
    time.sleep(0.5)
    assert not alive_pids(family)


# ------------------------------------------------------------------ the window logic

class FakeWindow:
    def __init__(self):
        self.loaded, self.js, self.destroyed = [], [], False

    def load_url(self, url):
        self.loaded.append(url)

    def evaluate_js(self, code):
        self.js.append(code)

    def destroy(self):
        self.destroyed = True


def wait_for_js(window, n, timeout=5):
    end = time.time() + timeout
    while time.time() < end and len(window.js) < n:
        time.sleep(0.02)
    assert len(window.js) >= n


def make_shell(fake, tmp_path, port=18964, **kw):
    sh = shell.Shell(fake, settings_for(tmp_path, **kw), port=port)
    sh.window = FakeWindow()
    sh.first_run = False
    return sh


def test_shell_start_exit_and_close_button(fake, tmp_path, monkeypatch):
    sh = make_shell(fake, tmp_path, port=18962)
    checked = sh.check()
    assert not checked["blocked"] and checked["autostart"] and checked["settings"]["distro"] == "Ubuntu-24.04"
    started = sh.start()
    assert started["ok"] and sh.state == "running" and sh.window.loaded and sh.window.loaded[0].endswith(":18962/")
    # pages cannot use the setup methods once the system runs
    assert sh.fix("venv") == {"ok": False, "message": "Not available."} and sh.start()["ok"] is False
    # the X button opens the panel's own Exit dialog instead of closing
    assert sh.on_closing() is False and sh.state == "exiting"                    # returns at once; the dialog is opened from another thread
    wait_for_js(sh.window, 1)
    assert "getElementById('exit')" in sh.window.js[0]
    assert sh.on_closing() is False
    wait_for_js(sh.window, 2)                                                    # pressing it again (after Cancel) opens the dialog again
    sh._exit_began -= 100
    assert sh.on_closing() is True                               # a shutdown stuck for 90 s can be given up on
    sh._exit_began = time.time()
    sh.exit_done()                                               # the panel says: everything is off
    end = time.time() + 20
    while time.time() < end and not sh.window.destroyed:
        time.sleep(0.1)
    assert sh.window.destroyed and sh.state == "closed" and not sh.app.alive()


def test_closing_during_setup_just_closes(fake, tmp_path):
    sh = make_shell(fake, tmp_path)
    assert sh.on_closing() is True


def test_a_failed_start_returns_to_setup_with_a_scrubbed_log(fake, tmp_path, monkeypatch):
    bad = tmp_path / "bad-python"                                           # a Python that cannot run AICorp, whatever is installed here
    bad.write_text(f"#!/bin/sh\necho 'cannot start in {tmp_path}/somewhere'\nexit 1\n")
    bad.chmod(0o755)
    monkeypatch.setenv("FAKE_WSL_PYTHON", str(bad))
    sh = make_shell(fake, tmp_path, distro="Ubuntu-24.04")
    result = sh.start()
    assert not result["ok"] and sh.state == "setup" and "address" in result["error"]
    assert "cannot start" in result["log"] and str(tmp_path) not in result["log"]


def test_unreachable_localhost_gives_the_wsl_hint(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "reachable", lambda *a, **k: False)
    sh = make_shell(fake, tmp_path, port=18963, distro="Ubuntu-24.04")
    result = sh.start()
    assert not result["ok"] and "localhostForwarding" in result["error"] and sh.state == "setup"


def test_the_exposed_api_is_small(fake, tmp_path):
    api = shell.Api(make_shell(fake, tmp_path))
    public = {n for n in dir(api) if not n.startswith("_")}
    assert public == {"check", "save", "fix", "start", "exit_done"}


# ------------------------------------------------------------------ the pages in a real browser

pw = pytest.importorskip("playwright.sync_api")

FAKE_API = """
window.__calls = [];
window.pywebview = {api: {
  check: async () => {window.__calls.push('check'); return window.__result},
  save: async v => {window.__calls.push('save:' + JSON.stringify(v)); return v},
  fix: async id => {window.__calls.push('fix:' + id); return {ok: true, message: 'Fixed ' + id}},
  start: async () => {window.__calls.push('start'); return window.__start || {ok: true}},
  exit_done: async () => {window.__calls.push('exit_done')}
}};
"""


@pytest.fixture
def page():
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"] if os.environ.get("AICORP_BROWSER_NO_SANDBOX") == "1" else [])
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        yield context.new_page()
        browser.close()


def load_setup(page, result, start=None):
    import json
    from setup_page import PAGE
    page.route("http://localhost/setup", lambda route: route.fulfill(body=PAGE, content_type="text/html"))      # localhost is a secure origin (clipboard)
    page.goto("http://localhost/setup")
    page.evaluate(f"window.__result = {json.dumps(result)}; window.__start = {json.dumps(start)}")
    page.evaluate(FAKE_API.replace("window.__calls = [];", "window.__calls = window.__calls || [];"))
    page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")


def result(checks, **kw):
    base = {"checks": checks, "prompts": {c["id"]: "PROMPT for " + c["id"] for c in checks if c["status"] != "ok"}, "prompt": "PROMPT ALL",
            "blocked": any(c["status"] == "fail" for c in checks), "settings": {"distro": "Ubuntu", "project": "~/ai-corp", "data_dir": None},
            "distros": ["Ubuntu", "Debian"], "autostart": False}
    return {**base, **kw}


def chk(id, status, title="T", detail="d", step="", fix=None, admin=False):
    return {"id": id, "title": title + " " + id, "status": status, "detail": detail, "next_step": step, "fix": fix, "needs_admin": admin}


def test_setup_page_blocks_guides_fixes_and_copies_the_prompt(page):
    load_setup(page, result([chk("wsl", "ok"), chk("python", "fail", step="Install Python", admin=True), chk("venv", "warn", step="Make one", fix="venv")]))
    page.wait_for_selector(".check")
    assert page.is_disabled("#start") and "needs your attention" in page.inner_text("#status")
    assert page.locator(".check").count() == 3
    assert "You do this yourself: Install Python" in page.inner_text(".check:nth-of-type(2)")
    page.locator("button", has_text="Copy AI prompt").first.click()
    assert page.evaluate("navigator.clipboard.readText()") == "PROMPT for python"
    page.click("#copyall")
    assert page.evaluate("navigator.clipboard.readText()") == "PROMPT ALL"
    page.click("button:has-text('Fix it')")
    page.wait_for_function("window.__calls.includes('fix:venv')")
    page.wait_for_function("window.__calls.filter(c => c === 'check').length >= 2")           # a fix is followed by a recheck
    page.fill("#project", "~/elsewhere")
    page.click("#save")
    page.wait_for_function("window.__calls.some(c => c.startsWith('save:') && c.includes('~/elsewhere'))")
    assert page.is_visible("#settings[open]")


def test_setup_page_starts_when_everything_is_ok_and_shows_a_failed_start(page):
    load_setup(page, result([chk("wsl", "ok"), chk("python", "ok")], autostart=False))
    page.wait_for_selector(".check")
    assert not page.is_disabled("#start") and "in place" in page.inner_text("#status")
    page.evaluate("window.__start = {ok: false, error: 'No address.', log: 'last line of the log'}")
    page.click("#start")
    page.wait_for_selector("#progress pre")
    assert "No address." in page.inner_text("#progress") and "last line of the log" in page.inner_text("#progress pre")
    assert not page.is_disabled("#start")                        # it can be tried again


def test_setup_page_goes_straight_to_the_app_when_ready(page):
    load_setup(page, result([chk("wsl", "ok")], autostart=True))
    page.wait_for_function("window.__calls.includes('start')")

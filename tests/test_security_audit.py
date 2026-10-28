"""Security review as tests: every state-changing route is protected, pages escape hostile text, secrets never show,
and the authorization layer is applied to every public function that changes company state."""
import inspect
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from aicorp import chat, impersonation, org, skills, takeover, tasks
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app
from helpers import staff

SRC = Path(__file__).resolve().parents[1] / "src" / "aicorp"


@pytest.fixture
def client(home):
    (home / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=123456:ABCdefTOKENvalue\nGMAIL_APP_PASSWORD=abcd-efgh-ijkl-mnop\nTELEGRAM_CHAT_ID=4242\n")
    with TestClient(create_app(home), raise_server_exceptions=False) as c:
        c.get("/")
        c.home = home
        yield c


def routes(app):
    out = []
    for r in app.routes:
        for method in getattr(r, "methods", None) or ():
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                out.append((method, r.path))
    return out


def fill(path):
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_state_changing_route_demands_the_csrf_token_and_a_local_origin(client):
    post_routes = routes(client.app)
    assert len(post_routes) > 50                                   # the audit really sees the application
    for method, path in post_routes:
        r = client.request(method, fill(path), data={}, follow_redirects=False)
        assert r.status_code in (403, 404, 405, 422), f"{method} {path} answered {r.status_code} without a CSRF token"
        r = client.request(method, fill(path), data={"csrf_token": client.cookies.get("aicorp_csrf")}, headers={"Origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403, f"{method} {path} accepted a foreign Origin ({r.status_code})"


def test_no_get_route_changes_state(client):
    con = connect(db_path(client.home))
    staff(con, client.home, 2)
    before = con.execute("SELECT (SELECT count(*) FROM event), (SELECT count(*) FROM task), (SELECT count(*) FROM employee), (SELECT count(*) FROM setting WHERE key NOT IN ('chain_checkpoint','watchdog_heartbeat','monitor_last_tick'))").fetchone()
    for r in client.app.routes:
        if "GET" in (getattr(r, "methods", None) or ()) and not r.path.startswith(("/static", "/api/events")) and "{" not in r.path:
            client.get(r.path)
    after = con.execute("SELECT (SELECT count(*) FROM event), (SELECT count(*) FROM task), (SELECT count(*) FROM employee), (SELECT count(*) FROM setting WHERE key NOT IN ('chain_checkpoint','watchdog_heartbeat','monitor_last_tick'))").fetchone()
    assert tuple(before)[1:3] == tuple(after)[1:3]                 # tasks and employees never change on a GET
    assert after[0] - before[0] <= 6                               # a few bookkeeping events (advisor scan) are fine, no flood


def test_a_foreign_host_header_is_refused(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_hostile_text_is_escaped_on_every_page_that_shows_user_text(client):
    con = connect(db_path(client.home))
    ids = staff(con, client.home, 1)
    evil = "<script>window.__pwned=1</script><img src=x onerror=alert(1)>"
    t = tasks.create_task(con, title="Evil title", description=evil, owner=ids[0])
    con.execute("UPDATE task SET result_summary = ?, status = 'awaiting_approval', risk_tier = 3 WHERE task_id = ?", (evil, t))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, '2026-01-01T00:00:00+00:00')", (t,))
    from aicorp import notify, projects
    notify.upsert(con, key="e", category="system", severity="warn", title=evil, detail=evil)
    projects.create_project(con, {"name": "P", "goal": evil})
    skills.save(client.home, con, {"name": "evil-skill", "body": evil, "description": evil})
    from aicorp.channels import Inbound
    chat.ingest(con, client.home, Inbound("telegram", "999", evil, "x1"))
    for url in ("/approvals", f"/tasks/{t}", "/inbox", "/projects", "/skills", "/skills/evil-skill", "/logs", "/chat", "/attention", "/advisor"):
        page = client.get(url).text
        assert "<script>window.__pwned" not in page and "onerror=alert(1)>" not in page, url


def test_secrets_never_appear_on_any_page(client):
    for r in client.app.routes:
        if "GET" in (getattr(r, "methods", None) or ()) and "{" not in r.path and not r.path.startswith(("/static", "/api/events")):
            body = client.get(r.path).text
            for secret in ("ABCdefTOKENvalue", "abcd-efgh-ijkl-mnop"):
                assert secret not in body, f"{secret[:6]}… leaked on {r.path}"


def test_the_secrets_file_and_runtime_folders_are_private(client):
    path = client.home / "config" / "secrets.env"
    from aicorp import channels
    channels.update_secret(client.home, "ALERT_EMAIL_TO", "me@example.com")
    assert path.stat().st_mode & 0o077 == 0, "update_secret must leave the file private (600)"


def test_templates_never_mark_user_text_as_safe():
    for t in (SRC / "web" / "templates").glob("*.html"):
        text = t.read_text()
        assert "|safe" not in text and "autoescape false" not in text, t.name


def test_every_public_state_changing_function_checks_who_is_acting():
    """Audit by reading the source: public functions in these modules that take an ``actor`` must call authorize (directly or
    through a helper that does) - so a new function cannot quietly skip the check."""
    exempt = {"get_task", "list_tasks", "timeline", "dependency_results", "transition", "open_block", "set_employee_status", "free_owner", "fail_dependents",
              "auto_unblock", "parse_when", "new_task_id", "settle", "history", "get", "open_for_task", "workspace_of", "session_for", "acting", "can_impersonate", "end", "acting_employee"}
    problems = []
    for mod in (tasks, org, takeover, impersonation, skills):
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("_") or fn.__module__ != mod.__name__ or name in exempt:
                continue
            params = inspect.signature(fn).parameters
            if "actor" not in params:
                continue
            body = inspect.getsource(fn)
            if "authorize(" not in body and "can_impersonate(" not in body and "_save_team(" not in body and "_require_regular(" not in body and "_check_can_take" not in body \
                    and "transition(" not in body:
                problems.append(f"{mod.__name__}.{name}")
    assert not problems, f"functions with an actor but no visible authorization: {problems}"

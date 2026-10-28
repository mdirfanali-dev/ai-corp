from urllib.parse import unquote

import pytest
from starlette.testclient import TestClient

from aicorp import impersonation, takeover, tasks
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app
from helpers import staff


@pytest.fixture
def client(home):
    with TestClient(create_app(home), raise_server_exceptions=False) as c:
        c.get("/")
        c.home = home
        yield c


def db(c):
    return connect(db_path(c.home))


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def err(r):
    return unquote(r.headers["location"])


def test_banner_cookie_and_both_identities_on_web_actions(client):
    con = db(client)
    ids = staff(con, client.home, 2)
    r = post(client, "/impersonate/start", {"employee": ids[0], "reason": "cover"})
    assert r.status_code == 303 and "aicorp_imp" in r.headers["set-cookie"]
    page = client.get("/").text
    assert "acting as W1" in page and "only that person" in page
    post(client, "/tasks", {"title": "Web task by W1", "owner": ids[0], "priority": "3", "risk_tier": "1"})
    e = con.execute("SELECT * FROM event WHERE type = 'task.created' ORDER BY id DESC LIMIT 1").fetchone()
    assert e["actor_name"] == "Founder" and e["acting_as_name"] == "W1" and e["mode"] == "impersonate"
    logs = client.get("/logs?mode=impersonate").text
    assert "acting as" in logs and "W1" in logs
    # permissions of W1 apply: no assigning work to others, no approvals
    r = post(client, "/tasks", {"title": "Work for W2", "owner": ids[1], "priority": "3", "risk_tier": "1"})
    assert r.status_code == 403 and "no &#39;assign&#39; permission" in r.text
    assert con.execute("SELECT count(*) FROM task WHERE title = 'Work for W2'").fetchone()[0] == 0
    post(client, "/impersonate/stop")
    assert "acting as" not in client.get("/").text
    post(client, "/tasks", {"title": "Back to normal", "owner": ids[1], "priority": "3", "risk_tier": "1"})
    assert con.execute("SELECT mode FROM event WHERE type = 'task.created' ORDER BY id DESC LIMIT 1").fetchone()[0] == "normal"


def test_override_needs_typed_confirmation_and_shows_red(client):
    con = db(client)
    ids = staff(con, client.home, 1)
    r = post(client, "/impersonate/start", {"employee": ids[0], "override": "on", "confirm": "yes"})
    assert "confirmed" in err(r) and "set-cookie" not in r.headers
    r = post(client, "/impersonate/start", {"employee": ids[0], "override": "on", "confirm": "OVERRIDE"})
    assert "aicorp_imp" in r.headers["set-cookie"]
    page = client.get("/").text
    assert "FOUNDER OVERRIDE" in page and "banner override" in page
    post(client, "/tasks", {"title": "Override task", "owner": ids[0], "priority": "3", "risk_tier": "1"})
    assert '"override":true' in con.execute("SELECT payload FROM event WHERE type = 'task.created' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert 'class="badge override"' in client.get("/logs?mode=impersonate").text


def test_a_forged_or_foreign_cookie_does_nothing(client):
    con = db(client)
    ids = staff(con, client.home, 1)
    client.cookies.set("aicorp_imp", "IMP-deadbeef")
    assert "acting as" not in client.get("/").text
    post(client, "/tasks", {"title": "Plain", "owner": ids[0], "priority": "3", "risk_tier": "1"})
    assert con.execute("SELECT mode FROM event WHERE type = 'task.created' ORDER BY id DESC LIMIT 1").fetchone()[0] == "normal"
    ceo = con.execute("SELECT emp_id, name FROM employee WHERE system_key = 'ceo'").fetchone()
    from aicorp import org
    org.create_team(con, {"name": "T", "head_emp_id": ceo["emp_id"], "member_ids": [ids[0]]})
    imp = impersonation.begin(con, {"actor_type": "employee", "actor_id": ceo["emp_id"], "actor_name": ceo["name"], "actor_designation": None}, ids[0])
    client.cookies.set("aicorp_imp", imp)                                        # a CEO session is never taken from a browser cookie
    assert "acting as" not in client.get("/").text


def test_take_over_pages_end_to_end_and_protected_files(client):
    con = db(client)
    ids = staff(con, client.home, 1)
    t = tasks.create_task(con, title="Report draft", owner=ids[0])
    assert "Interrupt and take over" in client.get(f"/tasks/{t}/takeover").text
    r = post(client, f"/tasks/{t}/takeover/start")
    assert r.status_code == 303
    page = client.get(f"/tasks/{t}/takeover").text
    assert "Where the agent was" in page and "Report draft" in page
    to = takeover.open_for_task(con, t)["takeover_id"]
    assert post(client, f"/takeover/{to}/note", {"text": "Use the new template"}).status_code == 303
    assert "Use the new template" in client.get(f"/tasks/{t}/takeover").text
    r = post(client, f"/takeover/{to}/file", {"path": "../../config/secrets.env", "content": "x"})
    assert "BLOCKED by AICorp policy" in err(r)
    r = post(client, f"/takeover/{to}/file", {"path": "draft.md", "content": "# Draft"})
    assert "err" not in r.headers["location"]
    r = post(client, f"/takeover/{to}/end", {"action": "complete", "result": "Finished by the Founder"})
    assert "err" not in r.headers["location"] and tasks.get_task(con, t)["status"] == "done"


def test_command_center_buttons_are_real_links_now(client):
    js = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/aicorp/web/static/live.js").read_text()
    assert "Arrives in Sprint" not in js and "/takeover" in js and "/impersonate?emp=" in js

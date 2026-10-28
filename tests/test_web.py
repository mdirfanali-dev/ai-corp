import re
import time

import pytest
from starlette.testclient import TestClient

from aicorp import events, org
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app


@pytest.fixture
def client(home):
    app = create_app(home)
    with TestClient(app) as c:
        c.get("/")  # sets the CSRF cookie
        c.home = home
        yield c


def token(c):
    return c.cookies.get("aicorp_csrf")


def post(c, url, data, **kw):
    return c.post(url, data={"csrf_token": token(c), **data}, follow_redirects=False, **kw)


def db(c):
    return connect(db_path(c.home))


def test_all_pages_render(client):
    for path in ["/", "/employees", "/employees/new", "/roles", "/roles/new", "/teams", "/teams/new",
                 "/logs", "/tasks", "/attention", "/approvals", "/settings", "/healthz"]:
        assert client.get(path).status_code == 200, path
    assert client.get("/nope").status_code == 404
    assert client.get("/employees/E-missing").status_code == 404


def test_post_without_csrf_token_is_refused_and_changes_nothing(client):
    r = client.post("/employees", data={"name": "Mallory"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/employees", data={"csrf_token": "wrong", "name": "Mallory"}, follow_redirects=False)
    assert r.status_code == 403
    con = db(client)
    assert con.execute("SELECT count(*) FROM employee WHERE name='Mallory'").fetchone()[0] == 0


def test_cross_site_origin_and_foreign_host_refused(client):
    r = client.post("/employees", data={"csrf_token": token(client), "name": "Eve"},
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400  # DNS-rebinding guard


def test_create_employee_through_the_form_then_see_it_and_its_log(client):
    con = db(client)
    role = org.create_role(con, {"name": "Researcher"})
    team = org.create_team(con, {"name": "Marketing"})
    r = post(client, "/employees", {"name": "Aria Khan", "kind": "permanent", "role_ids": [role],
                                    "team_ids": [team], "skills": "seo, writing", "instructions": "Be precise."})
    assert r.status_code == 303 and r.headers["location"].startswith("/employees/E-")
    page = client.get("/employees")
    assert "Aria Khan" in page.text and "Researcher" in page.text and "Marketing" in page.text
    detail = client.get(r.headers["location"].split("?")[0])
    assert "Be precise." in detail.text and "Recent activity" in detail.text


def test_duplicate_name_shows_error_and_keeps_input(client):
    post(client, "/employees", {"name": "Aria", "kind": "permanent"})
    r = post(client, "/employees", {"name": "ARIA", "kind": "permanent", "skills": "kept-skill"})
    assert r.status_code == 400 and "already used" in r.text and "kept-skill" in r.text


def test_output_is_escaped(client):
    evil = '<script>alert(1)</script>'
    post(client, "/employees", {"name": evil, "kind": "permanent"})
    for path in ("/employees", "/logs", "/"):
        text = client.get(path).text
        assert "<script>alert(1)</script>" not in text, path
    assert "&lt;script&gt;" in client.get("/employees").text


def test_temporary_employee_flow_and_search(client):
    r = post(client, "/employees", {"name": "Ben", "kind": "temporary"})
    detail_url = r.headers["location"].split("?")[0]
    assert "Delete temporary employee" in client.get(detail_url).text
    r = post(client, detail_url + "/delete", {})
    assert r.status_code == 303
    assert "Ben" not in client.get("/employees").text.replace("Ben [deleted", "")
    assert "Ben" in client.get("/employees?deleted=1").text
    assert "Deleted temporary employee Ben" in client.get("/logs?q=Ben").text


def test_system_employees_have_no_delete_or_archive_button(client):
    con = db(client)
    advisor = con.execute("SELECT emp_id FROM employee WHERE name='Advisor'").fetchone()[0]
    page = client.get(f"/employees/{advisor}").text
    assert "Built-in employee" in page and "Delete temporary" not in page and ">Archive<" not in page
    r = post(client, f"/employees/{advisor}/delete", {})
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_advisor_cannot_be_given_execute_via_form(client):
    con = db(client)
    advisor = con.execute("SELECT emp_id FROM employee WHERE name='Advisor'").fetchone()[0]
    r = post(client, f"/employees/{advisor}", {"name": "Advisor", "kind": "system", "execute": "on"})
    assert r.status_code == 400 and "cannot be given" in r.text


def test_role_and_team_pages(client):
    r = post(client, "/roles", {"name": "Writer", "assign": "on"})
    assert r.status_code == 303 and "Writer" in client.get("/roles").text
    dup = post(client, "/roles", {"name": "writer"})
    assert dup.status_code == 400 and "already exists" in dup.text
    r = post(client, "/teams", {"name": "Dev"})
    assert r.status_code == 303 and "Dev" in client.get("/teams").text


def test_logs_search_filters_and_pagination(client):
    con = db(client)
    for i in range(120):
        events.append(con, type="note", summary=f"bulk entry {i}")
    events.append(con, type="note", summary="Founder works as Aria", actor_type="founder",
                  actor_name="Founder", actor_designation="Chairman", acting_as_emp_id="E-x",
                  acting_as_name="Aria Khan", acting_as_role="Marketing Head", mode="impersonate")
    first = client.get("/logs").text
    assert "audit chain verified" in first and "Older" in first
    assert "Founder" in first and "acting as" in first and "Aria Khan" in first and "Marketing Head" in first
    older = re.search(r'href="(/logs\?[^"]*page=2[^"]*)"', first).group(1).replace("&amp;", "&")
    assert client.get(older).status_code == 200
    only_imp = client.get("/logs?mode=impersonate").text
    assert "Aria Khan" in only_imp and "bulk entry" not in only_imp
    assert "<mark>" in client.get("/logs?q=bulk").text


def test_search_finds_an_action_made_moments_ago_quickly(client):
    post(client, "/teams", {"name": "Quickfind Squad"})
    t0 = time.perf_counter()
    text = client.get("/logs?q=quickfind").text
    elapsed = time.perf_counter() - t0
    print(f"\n[timing] /logs?q=... page rendered in {elapsed * 1000:.0f} ms")
    assert "Quickfind Squad" in text and elapsed < 1.0


def test_broken_chain_is_shown_on_logs_page(client):
    con = db(client)
    con.execute("DROP TRIGGER event_no_update")
    con.execute("UPDATE event SET summary = 'forged' WHERE id = 1")
    assert "BROKEN" in client.get("/logs").text

import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from aicorp import events, killswitch, tasks
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app
from helpers import staff


@pytest.fixture
def client(home):
    with TestClient(create_app(home)) as c:
        c.get("/")
        c.home = home
        yield c


def db(c):
    return connect(db_path(c.home))


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def test_task_pages_render(client):
    con = db(client)
    (a,) = staff(con, client.home, 1)
    t = tasks.create_task(con, title="Visible task", owner=a)
    for path in ("/tasks", "/tasks/new", f"/tasks/{t}"):
        assert client.get(path).status_code == 200, path
    assert "Visible task" in client.get("/tasks").text
    assert client.get("/tasks/T-missing").status_code == 404


def test_create_task_through_form_and_validation_errors(client):
    con = db(client)
    a, b = staff(con, client.home, 2)
    first = tasks.create_task(con, title="Prerequisite", owner=a)
    r = post(client, "/tasks", {"title": "Main job", "owner": b, "priority": "2", "risk_tier": "1",
                                "depends_on": [first], "description": "do it"})
    assert r.status_code == 303 and r.headers["location"].startswith("/tasks/T-")
    page = client.get(r.headers["location"].split("?")[0]).text
    assert "Prerequisite" in page and "Main job" in page
    bad = post(client, "/tasks", {"title": "x", "owner": b, "risk_tier": "9"})
    assert bad.status_code == 400 and "Risk tier" in bad.text
    adv = con.execute("SELECT emp_id FROM employee WHERE name='Advisor'").fetchone()[0]
    denied = post(client, "/tasks", {"title": "Advisor job", "owner": adv, "risk_tier": "2"})
    assert denied.status_code == 400 and "execute" in denied.text
    nan = post(client, "/tasks", {"title": "x", "priority": "high"})
    assert nan.status_code == 400


def test_task_output_is_escaped(client):
    con = db(client)
    (a,) = staff(con, client.home, 1)
    tasks.create_task(con, title="<img src=x onerror=alert(1)>", owner=a)
    for path in ("/tasks", "/", "/logs"):
        assert "<img src=x" not in client.get(path).text, path


def test_resolve_cancel_and_approval_flow_via_pages(client):
    con = db(client)
    a, b = staff(con, client.home, 2)
    blocked = tasks.create_task(con, title="stuck", owner=a)
    tasks.transition(con, blocked, "blocked", actor=tasks.FOUNDER, summary="b")
    tasks.open_block(con, blocked, "LOGIN_REQUIRED", "log in please")
    page = client.get(f"/tasks/{blocked}").text
    assert "Blocked: LOGIN REQUIRED" in page and "log in please" in page
    assert post(client, f"/tasks/{blocked}/resolve", {"note": "logged in"}).status_code == 303
    assert tasks.get_task(con, blocked)["status"] == "assigned"
    assert post(client, f"/tasks/{blocked}/cancel").status_code == 303
    assert tasks.get_task(con, blocked)["status"] == "rejected"
    ap = tasks.create_task(con, title="publish", owner=b, risk_tier=3)
    con.execute("UPDATE task SET status='awaiting_approval' WHERE task_id=?", (ap,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, 'now')", (ap,))
    assert "Waiting for your approval" in client.get(f"/tasks/{ap}").text
    assert post(client, f"/tasks/{ap}/decide", {"decision": "approved"}).status_code == 303
    assert tasks.get_task(con, ap)["status"] == "done"
    err = post(client, f"/tasks/{ap}/decide", {"decision": "approved"})
    assert err.status_code == 303 and "err=" in err.headers["location"]


def test_kill_switch_buttons_and_banner(client):
    assert "Kill switch engaged" not in client.get("/").text
    assert post(client, "/killswitch/engage", {"reason": "testing"}).status_code == 303
    assert killswitch.engaged(client.home)
    assert "Kill switch engaged" in client.get("/employees").text
    assert post(client, "/killswitch/release").status_code == 303
    assert not killswitch.engaged(client.home)
    types = [e["type"] for e in db(client).execute("SELECT type FROM event WHERE type LIKE 'killswitch%' ORDER BY id")]
    assert types == ["killswitch.engaged", "killswitch.released"]
    assert post(client, "/killswitch/explode").status_code == 404


def test_post_without_csrf_is_refused_on_task_routes(client):
    for url in ("/tasks", "/killswitch/engage", "/tasks/T-x/cancel"):
        assert client.post(url, data={"title": "x"}, follow_redirects=False).status_code == 403


def test_engine_badge_reflects_heartbeat(client):
    assert "Engine stopped" in client.get("/").text
    con = db(client)
    now = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO engine_status VALUES (1, 'i', ?, ?, ?, 4, 2, 'simulated')", (os.getpid(), now, now))
    page = client.get("/").text
    assert "Engine 2/4 running" in page and "simulated" in page
    con.execute("UPDATE engine_status SET heartbeat_at = '2000-01-01T00:00:00+00:00'")
    assert "Engine stopped" in client.get("/").text


def read_events(response):
    out = []
    for block in response.iter_text():
        out.append(block)
    text = "".join(out)
    items = []
    for chunk in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line and not line.startswith(":"))
        if fields.get("event") == "audit":
            items.append((int(fields["id"]), json.loads(fields["data"])))
    return items, text


def test_sse_replays_missed_events_with_valid_json(client):
    con = db(client)
    for i in range(3):
        events.append(con, type="note", summary=f"hello {i}", task_id="T-1")
    with client.stream("GET", "/api/events/stream?since=0&timeout=1") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        items, text = read_events(r)
    assert [d["summary"] for _, d in items if d["type"] == "note"] == ["hello 0", "hello 1", "hello 2"]
    assert all(set(d) >= {"id", "ts", "actor_name", "type", "summary", "payload"} for _, d in items)
    last_id = items[-1][0]
    with client.stream("GET", "/api/events/stream?timeout=1", headers={"Last-Event-ID": str(last_id - 1)}) as r:
        items2, _ = read_events(r)
    assert [i for i, _ in items2] == [last_id]


def test_sse_delivers_new_events_live(client):
    db(client)

    def later():
        time.sleep(0.8)
        events.append(connect(db_path(client.home)), type="note", summary="appeared while connected")

    threading.Thread(target=later, daemon=True).start()
    started = time.monotonic()
    with client.stream("GET", "/api/events/stream?timeout=3") as r:
        items, _ = read_events(r)
    assert [d["summary"] for _, d in items] == ["appeared while connected"]
    assert time.monotonic() - started < 5


def test_api_events_json(client):
    con = db(client)
    events.append(con, type="note", summary="one")
    rows = client.get("/api/events?since=0").json()
    assert rows[-1]["summary"] == "one" and isinstance(rows[-1]["payload"], dict)

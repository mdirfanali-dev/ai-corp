import os
import re
from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

from aicorp import events, org, tasks
from aicorp.live import engine_state, snapshot
from aicorp.web.app import create_app
from helpers import staff

STATIC = Path(__file__).resolve().parents[1] / "src" / "aicorp" / "web"


def make_world(con, home):
    ids = staff(con, home, 5)
    marketing = org.create_team(con, {"name": "Marketing", "head_emp_id": ids[0], "member_ids": [ids[1]]})
    dev = org.create_team(con, {"name": "Development", "member_ids": [ids[2], ids[3]]})
    return ids, marketing, dev


def test_empty_company_has_only_leadership(home, con):
    org.seed_system(con, home)
    snap = snapshot(con, home)
    assert sorted(snap["cards"][i]["name"] for i in snap["leaders"]) == ["Advisor", "CEO"]
    assert snap["teams"] == [] and snap["bench"] == []
    assert snap["stats"]["running"] == 0 and snap["stats"]["cap"] is None and snap["engine"] == {"running": False}


def test_cards_reflect_running_blocked_and_waiting_work(home, con):
    (a, b, c, d, e), marketing, dev = make_world(con, home)
    run = tasks.create_task(con, title="Analyse competitors", owner=a)
    con.execute("UPDATE task SET status='running', attempts=1 WHERE task_id=?", (run,))
    events.append(con, type="task.progress", summary="halfway (50%)", task_id=run, payload={"percent": 50})
    stuck = tasks.create_task(con, title="Needs login", owner=c)
    tasks.transition(con, stuck, "blocked", actor=tasks.FOUNDER, summary="b")
    tasks.open_block(con, stuck, "LOGIN_REQUIRED", "please sign in")
    tasks.create_task(con, title="queued one", owner=b)
    tasks.create_task(con, title="queued two", owner=b)
    failed = tasks.create_task(con, title="broke", owner=d)
    con.execute("UPDATE task SET status='failed' WHERE task_id=?", (failed,))
    snap = snapshot(con, home)
    cards = snap["cards"]
    assert cards[a]["state"] == "working" and cards[a]["task"]["progress"] == 50 and cards[a]["task"]["attempts"] == 1
    assert cards[c]["state"] == "blocked" and cards[c]["task"]["block"]["reason"] == "LOGIN_REQUIRED"
    assert cards[b]["state"] == "idle" and cards[b]["waiting"] == 2
    assert cards[d]["needs_you"][0]["status"] == "failed"
    assert cards[e]["state"] == "idle" and e in snap["bench"]
    lane = {t["name"]: t for t in snap["teams"]}
    assert lane["Marketing"]["working"] == 1 and lane["Marketing"]["head"] == "W1"
    assert lane["Development"]["blocked"] == 1 and set(lane["Development"]["members"]) == {c, d}
    stats = snap["stats"]
    assert (stats["running"], stats["blocked"], stats["queue"]) == (1, 1, 2)  # running, blocked, two queued (the failed task is not queued)
    assert stats["needs_you"] == 2     # blocked + failed
    assert cards[a]["reports_to"] is None


def test_manager_shown_under_whom_and_archived_hidden(home, con):
    (a, b, *_rest), *_ = make_world(con, home)
    role = con.execute("SELECT role_id FROM role WHERE name='Worker'").fetchone()[0]
    org.update_employee(con, home, b, {"name": "W2", "reports_to": a, "role_ids": [role]})
    org.archive_employee(con, home, _rest[0])
    snap = snapshot(con, home)
    assert snap["cards"][b]["reports_to"] == "W1"
    assert _rest[0] not in snap["cards"]


def test_graph_contains_edges_and_context_but_not_loose_queue_items(home, con):
    (a, b, c, *_), *_ = make_world(con, home)
    first = tasks.create_task(con, title="first", owner=a)
    second = tasks.create_task(con, title="second", owner=b, depends_on=[first])
    loose = tasks.create_task(con, title="loose", owner=c)
    con.execute("UPDATE task SET status='done' WHERE task_id=?", (first,))
    graph = snapshot(con, home)["graph"]
    ids = {n["task_id"] for n in graph["nodes"]}
    assert {first, second} <= ids and loose not in ids          # done dependency is kept as context
    assert {"from": first, "to": second} in graph["edges"]


def test_feed_is_newest_first_and_limited(home, con):
    org.seed_system(con, home)
    for i in range(50):
        events.append(con, type="note", summary=f"n{i}")
    feed = snapshot(con, home, feed_size=10)["feed"]
    assert len(feed) == 10 and feed[0]["summary"] == "n49" and feed[-1]["summary"] == "n40"


def test_engine_state_uses_heartbeat_and_process(home, con):
    assert engine_state(con) == {"running": False}
    now = datetime.now(timezone.utc).isoformat()
    con.execute("INSERT INTO engine_status VALUES (1, 'i', ?, ?, ?, 4, 2, 'live')", (os.getpid(), now, now))
    assert engine_state(con)["running"] and engine_state(con)["active"] == 2
    con.execute("UPDATE engine_status SET pid = 999999")
    assert not engine_state(con)["running"]


def test_live_endpoint_is_uncached_json(home):
    with TestClient(create_app(home)) as client:
        response = client.get("/api/live")
        assert response.headers["cache-control"] == "no-store"
        assert {"stats", "cards", "teams", "graph", "feed"} <= set(response.json())


def test_frontend_never_injects_html_and_has_no_external_resources():
    js = (STATIC / "static" / "live.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    external = [u for u in re.findall(r"https?://[^\s\"'`)]+", js) if "w3.org" not in u]
    assert external == []
    for template in (STATIC / "templates").glob("*.html"):
        text = template.read_text()
        assert not re.search(r"(src|href)=\"https?://", text), template.name

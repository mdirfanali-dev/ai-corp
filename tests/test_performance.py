"""Performance against the locked target: dashboard pages under 300 ms with a realistically busy company."""
import statistics
import time

import pytest
from starlette.testclient import TestClient

from aicorp import events, notify, org, projects, search, tasks
from aicorp.config import db_path, utc_now
from aicorp.db import connect, migrate
from aicorp.web.app import create_app
from helpers import staff

PAGES = ["/", "/employees", "/teams", "/roles", "/tasks", "/tasks?status=done", "/projects", "/meetings", "/advisor", "/advisor/review", "/attention", "/inbox",
         "/logs", "/logs?q=task", "/approvals", "/skills", "/usage", "/settings", "/maintenance", "/chat", "/impersonate"]
TARGET_MS = 300 * (3 if __import__("os").environ.get("CI") else 1)       # shared CI runners are slower


@pytest.fixture(scope="module")
def busy(tmp_path_factory):
    home = tmp_path_factory.mktemp("busy")
    for name in ("company", "skills", "memory", "artifacts", "workspaces", "config", "database"):
        (home / name).mkdir()
    con = connect(db_path(home))
    migrate(con)
    ids = staff(con, home, 40)
    org.create_team(con, {"name": "Marketing", "member_ids": ids[:20]})
    org.create_team(con, {"name": "Research", "member_ids": ids[20:]})
    pids = [projects.create_project(con, {"name": f"Project {i}"}) for i in range(12)]
    made = []
    with con:
        for i in range(600):
            made.append(tasks.create_task(con, title=f"Task number {i} about pricing and copy", description="Some description " * 20, owner=ids[i % 40], project_id=pids[i % 12]))
    for i, t in enumerate(made):                                  # a spread of states, as in a busy company
        status = ("done", "done", "done", "assigned", "running", "blocked", "review", "failed")[i % 8]
        con.execute("UPDATE task SET status = ?, result_summary = 'Finished with a short result' WHERE task_id = ?", (status, t))
    for t in made[:40]:
        con.execute("UPDATE task SET status = 'awaiting_approval', risk_tier = 3 WHERE task_id = ?", (t,))
        con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, ?)", (t, utc_now()))
    con.execute("BEGIN")
    for i in range(30000):                                        # 30,000 audit events
        events.append(con, type="task.progress", summary=f"step {i} of task number {i % 600} about pricing", task_id=made[i % 600], payload={"percent": i % 100})
    con.execute("COMMIT")
    for i in range(300):
        notify.upsert(con, key=f"k{i}", category=("approval", "blocked", "failed", "reminder")[i % 4], severity=("info", "warn", "critical")[i % 3], title=f"Notification {i}", detail="detail " * 10)
    for i in range(400):
        con.execute("INSERT INTO usage (ts, emp_id, model, runs, est_tokens, prompt_chars, baseline_chars) VALUES (?,?,?,?,?,?,?)", (utc_now(), ids[i % 40], "claude-haiku-5-5", 1, 20000, 3000, 30000))
    con.commit()
    with TestClient(create_app(home), raise_server_exceptions=False) as client:
        client.get("/")
        yield client, con, ids
    con.close()


def timed(client, url, n=5):
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        r = client.get(url)
        samples.append((time.perf_counter() - start) * 1000)
        assert r.status_code == 200, (url, r.status_code)
    return statistics.median(samples), max(samples)


def test_every_dashboard_page_is_under_the_target_with_a_busy_company(busy, capsys):
    client, con, _ = busy
    print()
    worst = 0.0
    rows = []
    for url in PAGES:
        median, high = timed(client, url)
        rows.append((url, median, high))
        worst = max(worst, median)
    with capsys.disabled():
        print(f"\n[perf] 40 employees, 600 tasks, 30,000 events, 300 notifications - median of 5 loads per page (target < {TARGET_MS} ms):")
        for url, median, high in sorted(rows, key=lambda r: -r[1]):
            print(f"[perf] {url:22s} median {median:6.1f} ms   worst {high:6.1f} ms")
    slow = [(u, round(m)) for u, m, _ in rows if m >= TARGET_MS]
    assert not slow, f"over the {TARGET_MS} ms target: {slow}"


def test_search_and_chain_speed_on_30000_events(busy, capsys):
    client, con, _ = busy
    start = time.perf_counter()
    hits = search.search_events(con, "pricing", limit=50)
    first = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    search.search_events(con, actor="Founder", limit=50)
    by_actor = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    ok = events.verify_chain(con).ok
    chain = (time.perf_counter() - start) * 1000
    with capsys.disabled():
        print(f"\n[perf] full-text search {first:.1f} ms · by actor {by_actor:.1f} ms · verify the whole chain of {con.execute('SELECT count(*) FROM event').fetchone()[0]} events {chain:.0f} ms")
    assert hits and ok and first < 250 and by_actor < 100 and chain < 5000

import time

from aicorp import events
from aicorp.search import search_events
from conftest import add_employee


def test_search_text_actor_and_filters(con):
    add_employee(con, "e1", "Aria")
    events.append_as(con, "e1", type="task.started", summary="Started competitor analysis", task_id="T-1")
    events.append(con, type="note", summary="Founder approved the budget", actor_type="founder",
                  actor_name="Founder")
    assert [r["task_id"] for r in search_events(con, "competitor")] == ["T-1"]
    assert len(search_events(con, "compet")) == 1  # prefix match
    assert len(search_events(con, actor="aria")) == 1
    assert len(search_events(con, "budget", actor="Founder")) == 1
    assert search_events(con, "nonexistent") == []


def test_search_finds_impersonated_identity_and_unicode(con):
    add_employee(con, "e1", "Aria")
    events.append(con, type="note", summary="লগ পরীক্ষা", actor_type="founder", actor_name="Founder",
                  acting_as_emp_id="e1", acting_as_name="Aria", acting_as_role="Head",
                  mode="impersonate")
    assert len(search_events(con, actor="Aria")) == 1
    assert len(search_events(con, "পরীক্ষা")) == 1
    assert len(search_events(con, mode="impersonate")) == 1


def test_search_input_is_safe(con):
    events.append(con, type="note", summary="hello world")
    for hostile in ('"', "AND OR NOT", "hello*)", "'; DROP TABLE event;--", "NEAR("):
        search_events(con, hostile)  # must not raise
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] == 1


def test_search_10k_events_is_fast(con, capsys):
    con.execute("BEGIN")
    for i in range(10_000):
        events.append(con, type="task.progress",
                      summary=f"step {i} of marketing run, token{i % 500}", task_id=f"T-{i % 50}")
    con.execute("COMMIT")
    t0 = time.perf_counter()
    hits = search_events(con, "token42 marketing", limit=50)
    by_task = search_events(con, task_id="T-7", limit=50)
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[timing] 10,000 events: 2 searches in {elapsed * 1000:.1f} ms ({len(hits)}+{len(by_task)} rows)")
    assert hits and by_task
    assert elapsed < 1.0
    t0 = time.perf_counter()
    assert events.verify_chain(con).ok
    with capsys.disabled():
        print(f"[timing] verify_chain over 10,000 events: {(time.perf_counter() - t0) * 1000:.0f} ms")

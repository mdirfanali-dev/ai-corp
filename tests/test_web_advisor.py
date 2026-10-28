import re
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from aicorp import advisor, notify, projects, scheduler, tasks
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


def local(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).astimezone().strftime("%Y-%m-%dT%H:%M")


def seed_desk(c):
    con = db(c)
    ids = staff(con, c.home, 3)
    pid = projects.create_project(con, {"name": "Launch"})
    late = tasks.create_task(con, title="Late thing", owner=ids[0], project_id=pid)
    con.execute("UPDATE task SET due_at = ? WHERE task_id = ?", ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), late))
    appr = tasks.create_task(con, title="Needs approval", owner=ids[1], risk_tier=3)
    con.execute("UPDATE task SET status = 'awaiting_approval', result_summary = 'Short.' WHERE task_id = ?", (appr,))
    con.execute("INSERT INTO approval (task_id, tier, requested_at) VALUES (?, 3, 'now')", (appr,))
    return con, ids, pid, late, appr


def test_new_pages_render(client):
    for path in ("/advisor", "/advisor?tab=overdue", "/advisor?tab=forgotten&snoozed=1", "/advisor/review", "/advisor/schedule", "/advisor/reports",
                 "/projects", "/projects/new", "/meetings", "/meetings/new"):
        assert client.get(path).status_code == 200, path
    assert client.get("/projects/P-none").status_code == 404 and client.get("/meetings/M-none").status_code == 404 and client.get("/advisor/reports/R-none").status_code == 404
    nav = client.get("/").text
    assert 'href="/advisor"' in nav and 'href="/projects"' in nav and 'href="/meetings"' in nav


def test_desk_lists_everything_grouped_with_tabs(client):
    con, ids, pid, late, appr = seed_desk(client)
    page = client.get("/advisor").text
    assert "Late thing" in page and "Needs approval" in page and "<h2>Launch</h2>" in page and "<h2>No project</h2>" in page
    assert "overdue" in page and "needs you" in page and "Review one by one" in page
    assert "Late thing" in client.get("/advisor?tab=overdue").text and "Needs approval" not in client.get("/advisor?tab=overdue").text
    assert "Needs approval" in client.get("/advisor?tab=waiting").text and "Late thing" not in client.get("/advisor?tab=waiting").text
    assert "Nothing here" in client.get("/advisor?tab=forgotten").text


def test_desk_bulk_acknowledge_selected_and_all_shown(client):
    con, ids, *_ = seed_desk(client)
    client.get("/advisor")
    open_ids = [r[0] for r in con.execute("SELECT id FROM notification WHERE resolved_at IS NULL AND dedupe_key NOT LIKE 'nag:%'")]
    assert len(open_ids) >= 2
    r = post(client, "/inbox/action", {"action": "ack", "ids": [str(open_ids[0])], "next": "/advisor?tab=all"})
    assert r.headers["location"].startswith("/advisor?tab=all") and con.execute("SELECT ack_at FROM notification WHERE id = ?", (open_ids[0],)).fetchone()[0]
    assert con.execute("SELECT ack_at FROM notification WHERE id = ?", (open_ids[1],)).fetchone()[0] is None
    post(client, "/inbox/action", {"action": "ack", "ids": [str(i) for i in open_ids], "next": "/advisor"})
    assert con.execute("SELECT count(*) FROM notification WHERE resolved_at IS NULL AND ack_at IS NULL AND dedupe_key NOT LIKE 'nag:%'").fetchone()[0] == 0
    assert "acknowledged" in client.get("/advisor").text                                  # still listed until its cause is gone
    post(client, "/inbox/action", {"action": "snooze", "snooze_for": "1d", "ids": [str(open_ids[0])], "next": "/advisor"})
    assert "Late thing" not in client.get("/advisor").text or "Needs approval" not in client.get("/advisor").text
    assert client.get("/advisor?snoozed=1").status_code == 200


def test_review_mode_walks_through_items_one_by_one(client):
    con, *_ = seed_desk(client)
    first = client.get("/advisor/review").text
    total = int(re.search(r"Item 1 of (\d+)", first).group(1))
    assert total >= 2 and "Skip" in first
    seen = [re.search(r'<span class="title">([^<]+)</span>', first).group(1)]
    skip = client.get("/advisor/review?i=1").text
    assert "Item 2 of" in skip and re.search(r'<span class="title">([^<]+)</span>', skip).group(1) != seen[0]
    for _ in range(total):                                                                # acknowledge each item in turn
        page = client.get("/advisor/review?i=0").text
        if "All reviewed" in page:
            break
        item_id = int(re.search(r'name="ids" value="(\d+)"', page).group(1))
        r = post(client, "/inbox/action", {"action": "ack", "ids": str(item_id), "next": "/advisor/review?i=0"})
        assert r.headers["location"].startswith("/advisor/review?i=0")
    assert "All reviewed" in client.get("/advisor/review").text
    assert client.get("/advisor/review?i=99").status_code == 200 and client.get("/advisor/review?i=-3").status_code == 200


def test_schedule_page_monitor_run_now_and_settings(client):
    con = db(client)
    staff(con, client.home, 1)
    page = client.get("/advisor/schedule").text
    for job in ("scan_active", "nag_check", "daily_briefing", "weekly_report", "meeting_prep", "backup"):
        assert job in page
    assert post(client, "/advisor/monitor", {"state": "off", "next": "/advisor/schedule"}).status_code == 303
    assert not scheduler.monitor_enabled(con) and "Monitor is <b>OFF</b>" in client.get("/advisor/schedule").text
    post(client, "/advisor/monitor", {"state": "on"})
    r = post(client, "/advisor/run/daily_briefing")
    assert "daily_briefing" in r.headers["location"] and con.execute("SELECT count(*) FROM report WHERE kind = 'daily'").fetchone()[0] == 1
    assert post(client, "/advisor/run/nope").status_code == 404
    assert post(client, "/advisor/job/backup", {"spec": "daily 03:15", "enabled": "on"}).headers["location"].startswith("/advisor/schedule?ok=")
    assert con.execute("SELECT spec FROM schedule WHERE job = 'backup'").fetchone()[0] == "daily 03:15"
    assert post(client, "/advisor/job/backup", {"spec": "whenever", "enabled": "on"}).headers["location"].startswith("/advisor/schedule?err=")
    ok = post(client, "/advisor/settings", {"quiet_start": "21:30", "quiet_end": "06:45", "nag_daily_cap": "4", "stale_days": "3", "project_stale_days": "7", "away_threshold_minutes": "30", "ai_summary": "on"})
    assert ok.headers["location"].startswith("/advisor/schedule?ok=")
    assert notify.get_setting(con, "quiet_hours") == {"start": "21:30", "end": "06:45"} and notify.get_setting(con, "nag_daily_cap") == 4 and notify.get_setting(con, "ai_summary") is True
    for bad in ({"quiet_start": "25:00"}, {"nag_daily_cap": "0"}, {"stale_days": "x"}, {"away_threshold_minutes": "1"}):
        data = {"quiet_start": "21:30", "quiet_end": "06:45", "nag_daily_cap": "4", "stale_days": "3", "project_stale_days": "7", "away_threshold_minutes": "30", **bad}
        assert post(client, "/advisor/settings", data).headers["location"].startswith("/advisor/schedule?err="), bad
    assert client.post("/advisor/monitor", data={"state": "off"}, follow_redirects=False).status_code == 403


def test_reports_listing_view_and_path_safety(client):
    con = db(client)
    staff(con, client.home, 1)
    rid = advisor.save_report(con, client.home, "daily", "Daily briefing — test", "# Daily\n- a <b>bold</b> line\n")
    assert "Daily briefing — test" in client.get("/advisor/reports").text and "Daily briefing — test" not in client.get("/advisor/reports?kind=weekly").text
    page = client.get(f"/advisor/reports/{rid}").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page
    assert con.execute("SELECT read_at FROM notification WHERE entity_id = ?", (rid,)).fetchone()[0]          # opening marks its notification read
    con.execute("INSERT INTO report (report_id, kind, title, path, created_at) VALUES ('R-evil', 'x', 'evil', '../../../etc/passwd', 'now')")
    assert "report file is missing" in client.get("/advisor/reports/R-evil").text and "root:" not in client.get("/advisor/reports/R-evil").text


def test_projects_and_meetings_through_the_forms(client):
    con = db(client)
    ids = staff(con, client.home, 2)
    r = post(client, "/projects", {"name": "Micro SaaS", "goal": "Ship it", "status": "active", "owner_emp_id": ids[0]})
    pid = r.headers["location"].split("?")[0].split("/")[-1]
    assert "Micro SaaS" in client.get("/projects").text and "Ship it" in client.get(f"/projects/{pid}").text
    assert post(client, "/projects", {"name": ""}).status_code == 400
    r = post(client, "/meetings", {"title": "Kickoff", "project_id": pid, "scheduled_at": local(5), "agenda": "1. Scope", "attendees": ["founder", ids[0], ids[1]]})
    mid = r.headers["location"].split("?")[0].split("/")[-1]
    page = client.get(f"/meetings/{mid}").text
    assert "Kickoff" in page and "1. Scope" in page and "Run as bot meeting" in page and "Mark held" in page
    assert post(client, f"/meetings/{mid}/minutes", {"minutes": "# Notes\nAll good"}).headers["location"].startswith(f"/meetings/{mid}?ok=")
    assert "All good" in client.get(f"/meetings/{mid}").text
    post(client, f"/meetings/{mid}/action", {"description": "Collect prices", "owner": ids[0], "risk_tier": "1"})
    assert "Collect prices" in client.get(f"/meetings/{mid}").text and tasks.list_tasks(con, q="Collect prices")
    post(client, f"/meetings/{mid}/decision", {"title": "Use shared hosting", "text": "cheap", "follow_up_by": local(48)})
    assert "Use shared hosting" in client.get(f"/projects/{pid}").text
    did = con.execute("SELECT decision_id FROM decision").fetchone()[0]
    assert post(client, f"/decisions/{did}/close", {"status": "followed_up", "next": f"/projects/{pid}"}).headers["location"].startswith(f"/projects/{pid}")
    assert post(client, f"/meetings/{mid}/status", {"status": "held"}).status_code == 303 and "held" in client.get("/meetings").text
    assert post(client, f"/meetings/{mid}/bot-round").headers["location"].startswith(f"/meetings/{mid}?ok=")
    assert "Bot round" in client.get(f"/meetings/{mid}").text and post(client, f"/meetings/{mid}/bot-round").headers["location"].startswith(f"/meetings/{mid}?err=")
    assert post(client, f"/meetings/{mid}/status", {"status": "bogus"}).headers["location"].startswith(f"/meetings/{mid}?err=")
    assert client.post(f"/meetings/{mid}/minutes", data={"minutes": "x"}, follow_redirects=False).status_code == 403
    assert post(client, f"/projects/{pid}/delete").headers["location"].startswith("/projects?ok=")


def test_challenge_panel_appears_beside_an_approval(client):
    con, ids, pid, late, appr = seed_desk(client)
    page = client.get(f"/tasks/{appr}").text
    assert "The Advisor's challenge" in page and "very short" in page and "No cost or budget" in page
    assert "The Advisor's challenge" not in client.get(f"/tasks/{late}").text


def test_titles_and_notes_are_escaped_everywhere(client):
    con = db(client)
    ids = staff(con, client.home, 1)
    evil = "<script>alert(1)</script>"
    pid = projects.create_project(con, {"name": evil, "goal": evil})
    projects.create_meeting(con, client.home, {"title": evil, "project_id": pid})
    tasks.create_task(con, title=evil, owner=ids[0], project_id=pid)
    advisor.scan(con, datetime.now(timezone.utc) + timedelta(days=9))
    for path in ("/projects", f"/projects/{pid}", "/meetings", "/advisor", "/advisor/review", "/inbox"):
        assert "<script>alert(1)</script>" not in client.get(path).text, path

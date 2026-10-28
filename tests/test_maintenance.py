import gzip
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from aicorp import events, maintenance, notify, trash
from aicorp.web.app import create_app
from helpers import staff


def old(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_maintenance_prunes_only_bulk_records_and_never_the_audit_log(home, con):
    staff(con, home, 1)
    for i, (started, status) in enumerate([(old(100), "ok"), (old(100), "error"), (old(200), "error"), (old(1), "ok")]):
        con.execute("INSERT INTO job_run (job, started_at, status) VALUES (?, ?, ?)", (f"j{i}", started, status))
    nid, _ = notify.upsert(con, key="k", category="system", severity="info", title="old")
    con.execute("UPDATE notification SET resolved_at = ? WHERE id = ?", (old(120), nid))
    events_before = con.execute("SELECT count(*) FROM event").fetchone()[0]
    logs = home / "memory" / "run-logs"
    logs.mkdir(parents=True)
    (logs / "old.log").write_text("line\n" * 100)
    (logs / "new.log").write_text("fresh\n")
    os.utime(logs / "old.log", (time.time() - 40 * 86400,) * 2)
    report = maintenance.run(con, home)
    assert [r[0] for r in con.execute("SELECT job FROM job_run ORDER BY job")] == ["j1", "j3"]       # old ok run and 200-day run pruned; recent and 100-day failure kept
    assert con.execute("SELECT count(*) FROM notification_delivery WHERE notification_id = ?", (nid,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM notification WHERE id = ?", (nid,)).fetchone()[0] == 1
    assert (logs / "old.log.gz").exists() and not (logs / "old.log").exists() and (logs / "new.log").exists()
    assert gzip.open(logs / "old.log.gz").read().count(b"line") == 100                               # compressed, nothing lost
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] >= events_before and events.verify_chain(con).ok
    assert report["integrity"] == "ok" and report["chain_ok"] and not report["problems"]
    assert report["sizes"]["events"] > 0 and report["sizes"]["events_year_mb"] >= 0


def test_a_tampered_audit_log_raises_a_critical_alert(home, con):
    staff(con, home, 1)
    con.execute("DROP TRIGGER event_no_update")                               # what an attacker with file access could do
    con.execute("UPDATE event SET summary = 'rewritten history' WHERE id = 2")
    report = maintenance.run(con, home)
    assert report["chain_ok"] is False and "audit chain broken" in report["problems"]
    n = con.execute("SELECT severity, title FROM notification WHERE dedupe_key = 'maintenance:chain' AND resolved_at IS NULL").fetchone()
    assert n and n["severity"] == "critical"


def test_expired_trash_is_reported_but_never_purged_by_maintenance(home, con):
    f = home / "workspaces" / "x.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("keep me")
    dest = trash.move_to_trash(home, f, reason="test")
    entries = trash.list_trash(home)
    entries[0]["expires_at"] = old(1)
    (home / ".trash" / "manifest.jsonl").write_text("\n".join(__import__("json").dumps(e) for e in entries) + "\n")
    maintenance.run(con, home)
    assert dest.exists() and con.execute("SELECT count(*) FROM notification WHERE dedupe_key = 'maintenance:trash' AND resolved_at IS NULL").fetchone()[0] == 1
    with pytest.raises(PermissionError):
        trash.purge_expired(home)                                             # needs a human's confirm=True


def test_the_scheduler_has_a_weekly_maintenance_job(home, con):
    from aicorp import advisor, scheduler  # noqa: F401
    assert scheduler.REGISTRY["maintenance"][0] == "weekly sun 03:30"
    result = scheduler.run_now(con, home, "maintenance")
    assert result["status"] == "ok"


@pytest.fixture
def client(home):
    with TestClient(create_app(home), raise_server_exceptions=False) as c:
        c.get("/")
        c.home = home
        yield c


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def test_maintenance_page_run_restore_and_confirmed_purge(client):
    home = client.home
    f = home / "workspaces" / "a.txt"
    f.parent.mkdir(exist_ok=True)
    f.write_text("a")
    trash.move_to_trash(home, f)
    page = client.get("/maintenance").text
    assert "Maintenance" in page and "a.txt" in page and "per year" in page
    assert "Maintenance finished" in post(client, "/maintenance/run").headers["location"].replace("%20", " ")
    item = trash.list_trash(home)[0]["trashed"]
    assert post(client, "/maintenance/trash/restore", {"item": item}).status_code == 303 and f.exists()
    dest2 = trash.move_to_trash(home, f)
    entries = trash.list_trash(home)
    entries[-1]["expires_at"] = old(1)
    (home / ".trash" / "manifest.jsonl").write_text("\n".join(__import__("json").dumps(e) for e in entries) + "\n")
    assert "err=" in post(client, "/maintenance/trash/purge").headers["location"] and dest2.exists()      # not confirmed: nothing happens
    post(client, "/maintenance/trash/purge", {"confirm": "on"})
    assert not dest2.exists()

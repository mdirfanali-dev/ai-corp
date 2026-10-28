import re
import stat

import pytest
from starlette.testclient import TestClient

from aicorp import notify, tasks
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app
from helpers import Recorder, block_task, staff


@pytest.fixture
def client(home):
    app = create_app(home)
    app.state.transports = {"telegram": Recorder("telegram"), "email": Recorder("email")}
    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/")
        c.home, c.app_ = home, app
        yield c


def db(c):
    return connect(db_path(c.home))


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def seed(c, n=6):
    con = db(c)
    return [notify.upsert(con, key=f"k{i}", category="blocked" if i % 2 else "approval", severity="critical" if i % 2 else "warn",
                          title=f"Item number {i}", detail=f"details for {i}", link="/tasks")[0] for i in range(n)]


def test_pages_render_and_bell_shows_the_unread_count(client):
    for path in ("/attention", "/inbox", "/settings", "/api/notifications", "/api/notifications/summary"):
        assert client.get(path).status_code == 200, path
    seed(client, 4)
    page = client.get("/employees").text
    assert re.search(r'id="bell-count"[^>]*>\s*4\s*<', page)
    assert client.get("/api/notifications/summary").json() == {"unread": 4, "critical": 2}
    listing = client.get("/api/notifications?limit=2").json()
    assert len(listing) == 2 and set(listing[0]) == {"id", "severity", "category", "title", "created_at", "link", "read"}
    assert "/static/bell.js" in page and client.get("/static/bell.js").status_code == 200


def test_inbox_selective_select_all_and_matching_actions(client):
    ids = seed(client, 6)
    con = db(client)
    r = post(client, "/inbox/action", {"action": "read", "ids": [str(ids[0]), str(ids[1])]})          # selective
    assert r.status_code == 303 and notify.unread_count(con) == 4
    r = post(client, "/inbox/action", {"action": "read", "ids": [str(i) for i in ids]})                # "select all on this page"
    assert notify.unread_count(con) == 0
    post(client, "/inbox/action", {"action": "unread", "ids": [str(i) for i in ids]})
    assert notify.unread_count(con) == 6
    post(client, "/inbox/action", {"action": "read_all", "f_severity": "critical", "f_state": "all"})    # all matching a filter
    assert notify.unread_count(con) == 3
    assert {n["severity"] for n in notify.list_notifications(con, state="unread")} == {"warn"}
    post(client, "/inbox/action", {"action": "read_all"})                                              # everything left
    assert notify.unread_count(con) == 0
    assert "err=" in post(client, "/inbox/action", {"action": "read"}).headers["location"]             # nothing selected
    assert client.post("/inbox/action", data={"action": "read", "ids": "1"}, follow_redirects=False).status_code == 403
    assert post(client, "/inbox/action", {"action": "explode", "ids": "1"}).status_code == 400


def test_inbox_filters_search_snooze_pin_and_paging(client):
    ids = seed(client, 4)
    page = client.get("/inbox?severity=critical").text
    assert "Item number 1" in page and "Item number 0" not in page
    assert "Item number 3" in client.get("/inbox?q=details+for+3").text and "Item number 2" not in client.get("/inbox?q=details+for+3").text
    assert "Item number 0" in client.get("/inbox?category=approval").text
    post(client, "/inbox/action", {"action": "snooze", "snooze_for": "1d", "ids": [str(ids[0])]})
    assert notify.unread_count(db(client)) == 3
    assert "snoozed" in client.get("/inbox?state=snoozed").text and "Item number 0" not in client.get("/inbox?state=unread").text
    post(client, "/inbox/action", {"action": "pin", "ids": [str(ids[2])]})
    first_title = re.search(r'class="title" href="/inbox/\d+">([^<]+)<', client.get("/inbox").text).group(1)
    assert first_title == "Item number 2"
    con = db(client)
    for i in range(40):
        notify.upsert(con, key=f"bulk{i}", category="system", severity="info", title=f"Bulk {i}")
    assert "Older" in client.get("/inbox").text and client.get(re.search(r'href="(/inbox\?[^"]*page=2[^"]*)"', client.get("/inbox").text).group(1).replace("&amp;", "&")).status_code == 200


def test_opening_an_item_marks_it_read_and_shows_where_it_was_sent(client):
    con = db(client)
    nid = notify.upsert(con, key="x", category="blocked", severity="critical", title="Aria is blocked: CAPTCHA", detail="solve it",
                        link="/tasks/T-1", task_id=None)[0]
    from aicorp.delivery import Notifier
    Notifier(client.app_.state.transports).pump(con)
    page = client.get(f"/inbox/{nid}")
    assert page.status_code == 200 and "Aria is blocked: CAPTCHA" in page.text
    assert "telegram" in page.text and "email" in page.text and "sent" in page.text and "dashboard" in page.text
    assert notify.get(con, nid)["read_at"] and notify.unread_count(con) == 0
    assert client.get("/inbox/999999").status_code == 404
    assert post(client, "/inbox/action", {"action": "unread", "ids": str(nid), "next": f"/inbox/{nid}"}).headers["location"].startswith(f"/inbox/{nid}")


def test_redirect_targets_are_whitelisted(client):
    ids = seed(client, 1)
    for evil in ("https://evil.example/", "//evil.example", "/logs", "/inbox/../x", "javascript:alert(1)"):
        loc = post(client, "/inbox/action", {"action": "read", "ids": str(ids[0]), "next": evil}).headers["location"]
        assert loc.startswith("/inbox") and "evil" not in loc, evil


def test_output_is_escaped(client):
    con = db(client)
    nid = notify.upsert(con, key="xss", category="system", severity="critical", title="<img src=x onerror=alert(1)>",
                        detail="<script>alert(2)</script>")[0]
    for path in ("/inbox", f"/inbox/{nid}", "/attention", "/"):
        text = client.get(path).text
        assert "<img src=x" not in text and "<script>alert" not in text, path
    assert "&lt;img src=x" in client.get("/inbox").text


def test_attention_center_lists_blocked_work_and_the_resume_button_works(client):
    con = db(client)
    (a, b, c) = staff(con, client.home, 3)
    t = tasks.create_task(con, title="Research market", owner=a)
    block_task(con, t, "CAPTCHA", "recaptcha on example.com")
    failed = tasks.create_task(con, title="Cost model", owner=b)
    con.execute("UPDATE task SET status='failed', attempts=3 WHERE task_id=?", (failed,))
    page = client.get("/attention").text
    assert "W1" in page and "CAPTCHA" in page and "Research market" in page and "solve the CAPTCHA" in page
    assert "I fixed it" in page and "Cost model" in page and "Retry" in page and "Take over" in page
    assert db(client).execute("SELECT count(*) FROM notification WHERE resolved_at IS NULL").fetchone()[0] == 2
    r = post(client, f"/tasks/{t}/resolve", {"next": "/attention", "note": "solved"})
    assert r.headers["location"].startswith("/attention?ok=")
    assert tasks.get_task(con, t)["status"] == "assigned"
    assert db(client).execute("SELECT count(*) FROM notification WHERE resolved_at IS NULL").fetchone()[0] == 1       # resolved with the cause
    assert "All clear" not in client.get("/attention").text
    post(client, f"/tasks/{failed}/retry", {"next": "/attention"})
    assert "All clear" in client.get("/attention").text
    assert post(client, f"/tasks/{failed}/resolve", {"next": "/attention"}).headers["location"].startswith("/attention?err=")


def test_settings_page_masks_secrets_and_saves_changes(client):
    secrets_file = client.home / "config" / "secrets.env"
    secrets_file.write_text("TELEGRAM_BOT_TOKEN=123456789:AAH-sentinel-token-ABCDEFGHIJKLMNOPQRSTUV\nTELEGRAM_CHAT_ID=987654321\n"
                            "GMAIL_ADDRESS=founder@example.com\nGMAIL_APP_PASSWORD=sentinel app password 1234\nALERT_EMAIL_TO=alerts@example.com\n")
    page = client.get("/settings").text
    for secret in ("AAH-sentinel-token", "123456789:AAH", "987654321", "sentinel app password", "founder@example.com", "alerts@example.com"):
        assert secret not in page, secret
    assert "set up" in page and "f•••@example.com" in page
    r = post(client, "/settings/secret", {"TELEGRAM_BOT_TOKEN": "", "GMAIL_ADDRESS": "new@example.com", "ALERT_EMAIL_TO": ""})
    assert "ok=Saved+1" in r.headers["location"] or "Saved%201" in r.headers["location"]
    text = secrets_file.read_text()
    assert "GMAIL_ADDRESS=new@example.com" in text and "AAH-sentinel-token" in text           # blank fields keep the old value
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert "new@example.com" not in client.get("/settings").text
    changed = [e for e in db(client).execute("SELECT payload FROM event WHERE type='settings.updated'")]
    assert changed and all("new@example.com" not in row[0] for row in changed)
    assert post(client, "/settings/secret", {"GMAIL_ADDRESS": "bad\nINJECTED=1"}).headers["location"].startswith("/settings?err=")
    assert "INJECTED" not in secrets_file.read_text()


def test_settings_routing_limits_and_validation(client):
    r = post(client, "/settings/routing", {"r_warn_telegram": "on", "r_critical_email": "on"})
    assert r.status_code == 303
    assert notify.get_setting(db(client), "routing") == {"info": ["dashboard"], "warn": ["dashboard", "telegram"], "critical": ["dashboard", "email"]}
    assert post(client, "/settings/general", {"rate_per_hour": "5", "engine_down_after": "45", "dashboard_url": "http://localhost:9000/"}).headers["location"].startswith("/settings?ok=")
    assert notify.get_setting(db(client), "rate_per_hour") == 5 and notify.get_setting(db(client), "dashboard_url") == "http://localhost:9000"
    for bad in ({"rate_per_hour": "0"}, {"rate_per_hour": "x"}, {"engine_down_after": "1"}, {"dashboard_url": "javascript:alert(1)"}):
        data = {"rate_per_hour": "5", "engine_down_after": "45", "dashboard_url": "http://localhost:9000", **bad}
        assert post(client, "/settings/general", data).headers["location"].startswith("/settings?err="), bad


def test_test_buttons_send_through_the_channels_and_report_failures(client):
    tg, mail = client.app_.state.transports["telegram"], client.app_.state.transports["email"]
    assert "ok=" in post(client, "/settings/test/telegram").headers["location"] and len(tg.sent) == 1
    assert tg.sent[0][0] == "[AICorp] Test message"
    mail.fail = 1
    r = post(client, "/settings/test/email")
    assert "err=" in r.headers["location"] and "simulated" in r.headers["location"]
    assert post(client, "/settings/test/sms").status_code == 404
    assert client.post("/settings/test/telegram", follow_redirects=False).status_code == 403
    assert len(tg.sent) == 1


def test_unexpected_errors_are_recorded_notified_and_not_leaked(client):
    @client.app_.get("/boom")
    def boom():
        raise RuntimeError("secret-looking detail password=Hunter2Hunter2")
    response = client.get("/boom")
    assert response.status_code == 500 and "reference" in response.text and "Hunter2" not in response.text
    ref = re.search(r"<b>([0-9a-f]{8})</b>", response.text).group(1)
    con = db(client)
    ev = con.execute("SELECT summary FROM event WHERE type='system.error'").fetchone()[0]
    assert ref in ev and "Hunter2Hunter2" not in ev                                     # scrubbed even in the log
    n = con.execute("SELECT * FROM notification WHERE dedupe_key = 'error:web:RuntimeError'").fetchone()
    assert n["severity"] == "critical" and n["resolved_at"] is None
    client.get("/boom")
    assert con.execute("SELECT repeat_count FROM notification WHERE dedupe_key = 'error:web:RuntimeError'").fetchone()[0] == 2
    assert client.get("/employees").status_code == 200                                  # the dashboard keeps working


def test_watchdog_warning_is_shown_when_it_is_not_running(client):
    assert "Watchdog" in client.get("/settings").text and "not running" in client.get("/settings").text
    notify.set_setting(db(client), "watchdog_heartbeat", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
    assert "Watchdog: <b>running" in client.get("/settings").text

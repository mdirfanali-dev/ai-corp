import io
import json

import pytest
from starlette.testclient import TestClient

from aicorp import channels, chat
from aicorp.channels import Inbound
from aicorp.config import db_path
from aicorp.db import connect
from aicorp.web.app import create_app
from helpers import staff


@pytest.fixture
def client(home):
    (home / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=123456:ABCdefTOKENvalue\nTELEGRAM_CHAT_ID=4242\n")
    with TestClient(create_app(home), raise_server_exceptions=False) as c:
        c.get("/")
        c.home = home
        yield c


def post(c, url, data=None):
    return c.post(url, data={"csrf_token": c.cookies.get("aicorp_csrf"), **(data or {})}, follow_redirects=False)


def test_chat_log_page_lists_search_and_never_shows_secrets(client):
    con = connect(db_path(client.home))
    staff(con, client.home, 1)
    chat.ingest(con, client.home, Inbound("telegram", "4242", "/status", "a"))
    chat.ingest(con, client.home, Inbound("telegram", "999", "<script>alert(1)</script>", "b"))
    page = client.get("/chat").text
    assert "Engine:" in page and "ignored" in page and "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "123456:ABCdef" not in page
    hit = client.get("/chat?q=status").text
    assert "/status" in hit and "ignored" not in hit
    assert client.get("/chat?channel=email").text.count("<tr>") <= 2
    assert client.get("/chat?q=%25_%5C").status_code == 200            # wildcard characters are escaped, not interpreted


def test_settings_shows_the_chat_panel_and_status(client):
    con = connect(db_path(client.home))
    notify = __import__("aicorp.notify", fromlist=["x"])
    notify.set_setting(con, "chat_status:telegram", {"last_poll": "2026-10-28T10:00:00+00:00", "last_error": "Telegram not reachable: URLError"})
    page = client.get("/settings").text
    assert "Chat with the Advisor" in page and "Telegram not reachable" in page and "[AICorp]" in page


def test_mailbox_login_test_button(client):
    class Inbox:
        def __init__(self, fail): self.fail = fail
        def check_login(self):
            if self.fail:
                raise channels.DeliveryError("Gmail rejected the login (check the app password and that IMAP is enabled)")
    client.app.state.inboxes = {"email": Inbox(True)}
    r = post(client, "/settings/chat/test-email")
    assert "rejected" in r.headers["location"] or "rejected" in __import__("urllib.parse", fromlist=["x"]).unquote(r.headers["location"])
    client.app.state.inboxes = {"email": Inbox(False)}
    assert "ok=" in post(client, "/settings/chat/test-email").headers["location"]
    client.app.state.inboxes = {}
    assert "err=" in post(client, "/settings/chat/test-email").headers["location"]


def test_no_chat_message_can_change_things_through_the_web_routes(client):
    assert client.post("/settings/chat/test-email", data={}).status_code in (400, 403)      # needs the CSRF token


# ------------------------------------------------------------------ provider formats (mocked network)

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_telegram_updates_are_parsed_and_the_cursor_advances(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["body"] = json.loads(request.data)
        seen["url"] = request.full_url
        return Resp(json.dumps({"ok": True, "result": [
            {"update_id": 10, "message": {"chat": {"id": 4242}, "text": "/status"}},
            {"update_id": 11, "message": {"chat": {"id": 4242}, "photo": []}},
            {"update_id": 12, "edited_message": {"chat": {"id": 4242}, "text": "x"}}]}).encode())
    monkeypatch.setattr(channels.urllib.request, "urlopen", fake_urlopen)
    items, cursor = channels.TelegramInbox("T").poll("5", wait=0)
    assert [(i.sender, i.text, i.external_id) for i in items] == [("4242", "/status", "tg:10")]
    assert cursor == "13" and seen["body"]["offset"] == 5 and seen["url"].endswith("/botT/getUpdates")


def test_telegram_conflict_is_reported_in_plain_words(monkeypatch):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(channels.urllib.request, "urlopen", boom)
    with pytest.raises(channels.DeliveryError, match="already polling"):
        channels.TelegramInbox("T").poll(None, wait=0)


def test_imap_fetches_only_tagged_unread_mail_and_marks_it_seen(monkeypatch):
    raw = (b"From: boss@example.com\r\nSubject: [AICorp] hi\r\nMessage-ID: <9@x>\r\n"
           b"Authentication-Results: mx.google.com; dkim=pass\r\n\r\nWhat is pending?\r\n")
    calls = []

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def login(self, *a): calls.append("login")
        def select(self, *a, **k): calls.append("select")
        def uid(self, cmd, *args):
            calls.append((cmd, args))
            if cmd == "SEARCH":
                return "OK", [b"7 8"]
            if cmd == "FETCH":
                return "OK", [(b"1 (BODY[] {10}", raw if args[0] == b"7" else b"X-AICorp-Out: 1\r\nSubject: [AICorp] a\r\n\r\nb"), b")"]
            return "OK", []
        def logout(self): calls.append("logout")
    monkeypatch.setattr(channels.imaplib, "IMAP4_SSL", FakeIMAP)
    inbox = channels.EmailInbox("bot@example.com", "pw", "boss@example.com")
    items, _ = inbox.poll()
    assert [(i.sender, i.text, i.trusted) for i in items] == [("boss@example.com", "What is pending?", True)]
    assert ("SEARCH", (None, "UNSEEN", "SUBJECT", '"[AICorp]"')) in calls
    assert ("STORE", (b"8", "+FLAGS", "\\Seen")) in calls                # our own mail: marked seen, not processed
    inbox.done(items)
    assert ("STORE", ("7", "+FLAGS", "\\Seen")) in calls and calls[-1] == "logout"


def test_settings_shows_the_browser_worker_panel(client):
    page = client.get("/settings").text
    assert "Browser worker" in page and "Playwright" in page and "browser-sites.md" in page

"""Acceptance: no secret value appears in any log, page, notification, outgoing message or backup,
even when the secret is pasted into a task or ends up inside an error message."""
import tarfile

from starlette.testclient import TestClient

from aicorp import attention, backup, channels, events, notify, redact, tasks
from aicorp.channels import DeliveryError
from aicorp.db import connect
from aicorp.delivery import Notifier
from aicorp.engine import Orchestrator
from aicorp.runners import FakeRunner
from aicorp.web.app import create_app
from helpers import Recorder, block_task, staff

TOKEN = "1234567890:AAHsentinelTokenABCDEFGHIJKLMNOPQRSTUV"
APP_PASSWORD = "zqxj wvut srqp onml"
CHAT = "555000111"
ADDRESS = "sentinel.founder@example.org"
SECRET_VALUES = (TOKEN, APP_PASSWORD, APP_PASSWORD.replace(" ", ""), CHAT, "AAHsentinelToken")


class Leaky:
    """A channel whose error messages contain the credentials (as a careless library could)."""
    name = "telegram"

    def send(self, subject, text):
        raise DeliveryError(f"HTTP 401 for bot{TOKEN} using password {APP_PASSWORD}")


def all_database_text(con):
    return "\n".join(con.iterdump())


def assert_clean(label, text):
    for value in SECRET_VALUES:
        assert value not in text, f"{label} contains a secret ({value[:8]}…)"


def test_no_secret_reaches_any_log_page_message_or_backup(home, con, tmp_path, monkeypatch):
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "secrets.env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\nGMAIL_ADDRESS={ADDRESS}\nGMAIL_APP_PASSWORD={APP_PASSWORD}\nALERT_EMAIL_TO={ADDRESS}\n")
    ids = staff(con, home, 2)
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("AICORP_TRANSPORT_DIR", str(outbox))
    app = create_app(home)                                                  # installs the redactor, like the real program
    scrub = redact.Redactor(redact.load_secret_values(home)).scrub
    recorder = Recorder("email")

    # 1. the Founder pastes a credential into a task by mistake
    t = tasks.create_task(con, title=f"Log in with {APP_PASSWORD}", description=f"token is {TOKEN}", owner=ids[0], risk_tier=1)
    # 2. a task blocks with a detail that contains a credential; the alert is built from it
    t2 = tasks.create_task(con, title="Second job", owner=ids[1])
    block_task(con, t2, "LOGIN_REQUIRED", f"the site rejected {APP_PASSWORD} / {TOKEN}")
    attention.sync(con, home)
    # 3. delivery: one channel is healthy, one fails with a message containing the credentials
    notifier = Notifier({"email": recorder, "telegram": Leaky()}, scrub=scrub)
    notifier.pump(con)
    # 4. an internal error whose text contains the credentials (engine and dashboard)
    engine = Orchestrator(home, FakeRunner(), threaded=False, notifier=notifier, ram_probe=lambda: 0.1)
    engine.start()
    engine.report_error("scheduler", RuntimeError(f"auth failed for {APP_PASSWORD} with {TOKEN}"))
    events.append(con, type="note", summary=f"manual note mentioning {TOKEN}", payload={"nested": [f"{APP_PASSWORD}"]})
    engine.shutdown()
    # 5. a dashboard error and every dashboard page
    @app.get("/boom")
    def boom():
        raise RuntimeError(f"db password {APP_PASSWORD}")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/")
        client.get("/boom")
        for path in ("/", "/employees", "/tasks", f"/tasks/{t}", f"/tasks/{t2}", "/attention", "/inbox", "/inbox?q=password", "/settings",
                     "/logs", "/logs?q=token", "/api/live", "/api/notifications", "/api/events?since=0", "/employees/new", "/roles", "/teams"):
            response = client.get(path)
            assert response.status_code in (200, 404), (path, response.status_code)
            assert_clean(f"page {path}", response.text)
        first = client.get("/inbox").text
        import re
        for href in set(re.findall(r'href="(/inbox/\d+)"', first)):
            assert_clean(f"page {href}", client.get(href).text)

    # --- everything stored in the database
    assert_clean("database", all_database_text(con))
    assert con.execute("SELECT title, description FROM task WHERE task_id = ?", (t,)).fetchone()[0].count("[REDACTED]") == 1
    # --- everything that was sent out
    sent_text = "\n".join(f"{s}\n{b}" for s, b in recorder.sent)
    assert recorder.sent, "the healthy channel should have received the alert"
    assert_clean("outgoing e-mail", sent_text)
    errors = [d for n in con.execute("SELECT id FROM notification") for d in notify.deliveries(con, n[0]) if d["status"] == "failed"]
    assert errors and all("401" in d["error"] for d in errors)                       # the failure is reported, without credentials
    # --- a backup
    archive = backup.create_backup(home)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert not any(n.startswith("config") or "secrets" in n for n in names)
        for member in tar.getmembers():
            if member.isfile():
                data = tar.extractfile(member).read()
                text = data.decode("utf-8", errors="ignore")
                assert_clean(f"backup member {member.name}", text)
                if member.name.endswith(".sqlite"):
                    restored = tmp_path / "restored.sqlite"
                    restored.write_bytes(data)
                    assert_clean("backup database", all_database_text(connect(restored)))
    # --- run logs (written by the agent runner)
    for path in (home / "memory").rglob("*"):
        if path.is_file():
            assert_clean(f"file {path.name}", path.read_text(errors="ignore"))


def test_file_transport_messages_are_clean_too(home, con, tmp_path, monkeypatch):
    (home / "config" / "secrets.env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n")
    monkeypatch.setenv("AICORP_TRANSPORT_DIR", str(tmp_path / "out"))
    redactor = redact.install(home)
    notify.upsert(con, key="x", category="system", severity="critical", title=f"Token {TOKEN} was rejected")
    Notifier(channels.load_transports(home), scrub=redactor.scrub).pump(con)
    for file in (tmp_path / "out").iterdir():
        assert_clean(file.name, file.read_text())
    assert "[REDACTED]" in (tmp_path / "out" / "telegram.jsonl").read_text()

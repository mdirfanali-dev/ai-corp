"""Outgoing channels: Telegram (free Bot API) and e-mail (Gmail with an app password).

Secrets are read from ``config/secrets.env`` and never leave this module except to the provider.
Error messages are scrubbed before they are stored or shown. For tests and demos the environment
variable ``AICORP_TRANSPORT_DIR`` replaces both channels with files (one JSON line per message).
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from .config import utc_now


class DeliveryError(Exception):
    pass


class Transport(Protocol):
    name: str

    def send(self, subject: str, text: str) -> None: ...


def read_secrets(home: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in (home / "config" / "secrets.env").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def mask(value: str, keep: int = 2) -> str:
    return "" if not value else value[:keep] + "…" + "•" * 4


def mask_email(address: str) -> str:
    if "@" not in address:
        return mask(address)
    name, _, domain = address.partition("@")
    return f"{name[:1]}•••@{domain}"


class TelegramTransport:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: float = 20.0) -> None:
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    def send(self, subject: str, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": (f"{subject}\n\n{text}" if subject else text)[:3900],
                           "disable_web_page_preview": True}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", body,
                                         {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not json.load(response).get("ok"):
                    raise DeliveryError("Telegram refused the message")
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = json.load(err).get("description", "")
            except (ValueError, OSError):
                pass
            raise DeliveryError(f"Telegram error {err.code}: {detail}".replace(self.token, "***")) from None
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            raise DeliveryError(f"Telegram not reachable: {type(err).__name__}") from None


class EmailTransport:
    name = "email"

    def __init__(self, address: str, app_password: str, to: str, timeout: float = 30.0) -> None:
        self.address, self.password, self.to, self.timeout = address, re.sub(r"\s", "", app_password), to, timeout

    def send(self, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.address, self.to, subject
        message["X-AICorp-Out"] = "1"          # lets the inbound side skip our own mail
        message.set_content(text)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=self.timeout) as smtp:
                smtp.login(self.address, self.password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError("Gmail rejected the login (check the app password)") from None
        except (smtplib.SMTPException, OSError) as err:
            raise DeliveryError(f"Email failed: {type(err).__name__}") from None


class FileTransport:
    """Writes messages to a file instead of sending them (tests, demos, dry runs)."""

    def __init__(self, folder: Path, name: str) -> None:
        self.name, self.path = name, Path(folder) / f"{name}.jsonl"

    def send(self, subject: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": utc_now(), "subject": subject, "text": text}, ensure_ascii=False) + "\n")


def load_transports(home: Path) -> dict[str, Transport]:
    """The channels that are set up. Missing ones are simply absent (deliveries then show 'not configured')."""
    folder = os.environ.get("AICORP_TRANSPORT_DIR")
    if folder:
        return {n: FileTransport(Path(folder), n) for n in ("telegram", "email")}
    secrets = read_secrets(home)
    out: dict[str, Transport] = {}
    if secrets.get("TELEGRAM_BOT_TOKEN") and secrets.get("TELEGRAM_CHAT_ID"):
        out["telegram"] = TelegramTransport(secrets["TELEGRAM_BOT_TOKEN"], secrets["TELEGRAM_CHAT_ID"])
    if secrets.get("GMAIL_ADDRESS") and secrets.get("GMAIL_APP_PASSWORD"):
        out["email"] = EmailTransport(secrets["GMAIL_ADDRESS"], secrets["GMAIL_APP_PASSWORD"],
                                      secrets.get("ALERT_EMAIL_TO") or secrets["GMAIL_ADDRESS"])
    return out


def status(home: Path) -> dict[str, dict]:
    """What is configured, with every value masked (safe to show on a page)."""
    s = read_secrets(home)
    return {
        "telegram": {"configured": bool(s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID")),
                     "token": mask(s.get("TELEGRAM_BOT_TOKEN", "")), "chat_id": mask(s.get("TELEGRAM_CHAT_ID", ""), 3)},
        "email": {"configured": bool(s.get("GMAIL_ADDRESS") and s.get("GMAIL_APP_PASSWORD")),
                  "address": mask_email(s.get("GMAIL_ADDRESS", "")), "app_password": "•" * 8 if s.get("GMAIL_APP_PASSWORD") else "",
                  "to": mask_email(s.get("ALERT_EMAIL_TO", ""))},
    }


SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ALERT_EMAIL_TO")


def update_secret(home: Path, key: str, value: str) -> None:
    """Change one known key in secrets.env, keeping the file private (mode 600)."""
    if key not in SECRET_KEYS or re.search(r"[\r\n]", value):
        raise ValueError("unknown setting or invalid value")
    path = home / "config" / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.partition("=")[0].strip() == key and not line.startswith("#"):
            out.append(f"{key}={value.strip()}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value.strip()}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- inbound (two-way chat)

SUBJECT_TAG = "[AICorp]"


@dataclass
class Inbound:
    channel: str
    sender: str            # Telegram chat id, or the e-mail address
    text: str
    external_id: str
    trusted: bool = True   # e-mail: the provider vouched for the sender (DKIM/SPF pass)
    handle: object = None  # provider reference used to mark the message as processed


class TelegramInbox:
    """Long polling of the Bot API (no public server needed). The cursor is the next update id."""
    name = "telegram"

    def __init__(self, token: str, wait: int = 20) -> None:
        self.token, self.wait = token, wait

    def poll(self, cursor: str | None, wait: int | None = None) -> tuple[list[Inbound], str | None]:
        wait = self.wait if wait is None else wait
        query = {"timeout": wait, "allowed_updates": ["message"]}
        if cursor:
            query["offset"] = int(cursor)
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                         json.dumps(query).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=wait + 15) as response:
                data = json.load(response)
        except urllib.error.HTTPError as err:
            raise DeliveryError(f"Telegram error {err.code}" + (" (another program is already polling this bot)" if err.code == 409 else "")) from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as err:
            raise DeliveryError(f"Telegram not reachable: {type(err).__name__}") from None
        if not data.get("ok"):
            raise DeliveryError("Telegram refused the request")
        items, last = [], None
        for update in data.get("result", []):
            last = update["update_id"]
            message = update.get("message") or {}
            if isinstance(message.get("text"), str):
                items.append(Inbound("telegram", str((message.get("chat") or {}).get("id", "")), message["text"], f"tg:{update['update_id']}"))
        return items, (str(last + 1) if last is not None else cursor)

    def done(self, items: list[Inbound]) -> None:
        pass


def _plain_body(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", text))
    return text


def strip_quoted(text: str) -> str:
    """Keep only what the sender wrote: drop quoted lines, 'On ... wrote:' and signatures."""
    kept = []
    for line in text.replace("\r", "").split("\n"):
        if re.match(r"\s*(On .{5,120} wrote:|-----Original Message-----|From: .+@)", line) or line.strip() == "--":
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def parse_mail(raw: bytes, uid: object = None) -> Inbound | None:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if message.get("X-AICorp-Out"):
        return None                                      # our own alert, not a message to us
    subject = str(message.get("Subject") or "")
    sender = email.utils.parseaddr(str(message.get("From") or ""))[1].lower()
    auth = " ".join(str(h) for h in message.get_all("Authentication-Results", []))
    trusted = "google.com" in auth and bool(re.search(r"\b(dkim|spf)=pass\b", auth))
    return Inbound("email", sender, strip_quoted(_plain_body(message)), "mail:" + str(message.get("Message-ID") or f"uid{uid}"),
                   trusted, uid) if SUBJECT_TAG.lower() in subject.lower() else None


class EmailInbox:
    """IMAP polling of the Gmail inbox: only unread mail from the Founder's address whose subject carries the tag."""
    name = "email"

    def __init__(self, address: str, app_password: str, founder: str, timeout: float = 30.0) -> None:
        self.address, self.password, self.founder, self.timeout = address, re.sub(r"\s", "", app_password), founder, timeout

    def _login(self):
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=self.timeout)
        try:
            imap.login(self.address, self.password)
        except imaplib.IMAP4.error:
            raise DeliveryError("Gmail rejected the login (check the app password and that IMAP is enabled)") from None
        return imap

    def check_login(self) -> None:
        try:
            imap = self._login()
            imap.select("INBOX", readonly=True)
            imap.logout()
        except (imaplib.IMAP4.error, OSError) as err:
            raise DeliveryError(f"IMAP failed: {type(err).__name__}") from None

    def poll(self, cursor: str | None = None, wait: int | None = None) -> tuple[list[Inbound], str | None]:
        items: list[Inbound] = []
        try:
            imap = self._login()
            imap.select("INBOX")
            _, found = imap.uid("SEARCH", None, "UNSEEN", "SUBJECT", f'"{SUBJECT_TAG}"')
            for uid in (found[0] or b"").split()[:25]:
                _, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                parsed = parse_mail(raw, uid.decode()) if raw else None
                if parsed:
                    items.append(parsed)
                else:
                    imap.uid("STORE", uid, "+FLAGS", "\\Seen")      # tagged but ours / unparsable: stop seeing it
            self._imap = imap
        except (imaplib.IMAP4.error, OSError) as err:
            raise DeliveryError(f"IMAP failed: {type(err).__name__}") from None
        return items, cursor

    def done(self, items: list[Inbound]) -> None:
        imap = getattr(self, "_imap", None)
        if imap is None:
            return
        try:
            for item in items:
                imap.uid("STORE", item.handle, "+FLAGS", "\\Seen")
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        self._imap = None


class FileInbox:
    """Stand-in for tests and demos: reads one JSON line per message from ``<name>_in.jsonl``. Cursor = lines consumed."""

    def __init__(self, folder: Path, name: str) -> None:
        self.name, self.path = name, Path(folder) / f"{name}_in.jsonl"

    def poll(self, cursor: str | None, wait: int | None = None) -> tuple[list[Inbound], str | None]:
        start = int(cursor or 0)
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return [], cursor
        items = []
        for number, line in enumerate(lines[start:], start=start):
            try:
                d = json.loads(line)
                items.append(Inbound(self.name, str(d["sender"]), str(d["text"]), str(d.get("id") or f"file:{self.name}:{number}"), bool(d.get("trusted", True))))
            except (ValueError, KeyError, TypeError):
                continue
        return items, str(len(lines))

    def done(self, items: list[Inbound]) -> None:
        pass


def load_inboxes(home: Path) -> dict[str, object]:
    folder = os.environ.get("AICORP_TRANSPORT_DIR")
    if folder:
        return {n: FileInbox(Path(folder), n) for n in ("telegram", "email")}
    s = read_secrets(home)
    out: dict[str, object] = {}
    if s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"):
        out["telegram"] = TelegramInbox(s["TELEGRAM_BOT_TOKEN"])
    if s.get("GMAIL_ADDRESS") and s.get("GMAIL_APP_PASSWORD"):
        out["email"] = EmailInbox(s["GMAIL_ADDRESS"], s["GMAIL_APP_PASSWORD"], s.get("ALERT_EMAIL_TO") or s["GMAIL_ADDRESS"])
    return out


def allowed_senders(home: Path) -> dict[str, str]:
    """Who may talk to the Advisor: the configured Telegram chat and the Founder's e-mail address. Nobody else."""
    s = read_secrets(home)
    founder = (s.get("ALERT_EMAIL_TO") or s.get("GMAIL_ADDRESS") or "").strip().lower()
    return {"telegram": (s.get("TELEGRAM_CHAT_ID") or "").strip(), "email": founder}

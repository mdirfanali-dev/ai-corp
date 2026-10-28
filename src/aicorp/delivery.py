"""Sends notifications to Telegram / e-mail according to severity routing.

Safe to run from several processes at once (engine and watchdog): a channel is claimed with a
database row before sending, so a notification goes out at most once per channel. Failures are
retried with back-off; a per-hour limit stops an alert storm from flooding the Founder.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import notify
from .channels import DeliveryError, Transport

MAX_ATTEMPTS = 4
STALE_SENDING = 120            # seconds before a claim from a dead process is treated as failed
LOOKBACK = timedelta(hours=24)


def compose(item: dict, base_url: str) -> tuple[str, str]:
    subject = f"[AICorp] {item['severity'].upper()}: {item['title']}"
    lines = [item["title"], "", item.get("detail") or ""]
    if item.get("link"):
        lines += ["", f"Open: {base_url.rstrip('/')}{item['link']}"]
    lines += ["", f"Since: {item['created_at'][:19].replace('T', ' ')} UTC"]
    if item.get("repeat_count", 1) > 1:
        lines.append(f"Seen {item['repeat_count']} times.")
    return subject, "\n".join(lines).strip()


class Notifier:
    def __init__(self, transports: dict[str, Transport], clock: Callable[[], datetime] | None = None,
                 scrub: Callable[[str], str] | None = None) -> None:
        self.transports = transports
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.scrub = scrub or (lambda text: text)

    def _iso(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def pump(self, con: sqlite3.Connection) -> dict[str, int]:
        """Deliver whatever is due. Returns counts: sent, failed, suppressed."""
        now = self.clock()
        stats = {"sent": 0, "failed": 0, "suppressed": 0}
        self._release_stale(con, now)
        routing = notify.get_setting(con, "routing")
        per_hour = int(notify.get_setting(con, "rate_per_hour"))
        base = notify.get_setting(con, "dashboard_url")
        since = self._iso(now - LOOKBACK)
        rows = con.execute("SELECT * FROM notification WHERE resolved_at IS NULL AND created_at >= ? ORDER BY id", (since,)).fetchall()
        for row in rows:
            item = dict(row)
            chosen = json.loads(item["channels"]) if item.get("channels") else routing.get(item["severity"], [])
            for channel in [c for c in chosen if c != "dashboard"]:
                outcome = self._deliver(con, item, channel, now, per_hour, base)
                if outcome:
                    stats[outcome] += 1
        return stats

    def _release_stale(self, con, now: datetime) -> None:
        limit = self._iso(now - timedelta(seconds=STALE_SENDING))
        con.execute("UPDATE notification_delivery SET status = 'failed', error = 'interrupted while sending' "
                    "WHERE status = 'sending' AND attempted_at < ?", (limit,))

    def _deliver(self, con, item: dict, channel: str, now: datetime, per_hour: int, base: str) -> str | None:
        history = con.execute("SELECT status, attempted_at FROM notification_delivery WHERE notification_id = ? AND channel = ?",
                              (item["id"], channel)).fetchall()
        if any(h["status"] in ("sent", "suppressed", "sending") for h in history):
            return None
        failures = [h for h in history if h["status"] == "failed"]
        if len(failures) >= MAX_ATTEMPTS:
            return None
        if failures:
            last = max(datetime.fromisoformat(f["attempted_at"]) for f in failures)
            if now < last + timedelta(seconds=60 * 2 ** (len(failures) - 1)):
                return None
        try:
            cur = con.execute("INSERT INTO notification_delivery (notification_id, channel, status, attempted_at) "
                              "VALUES (?, ?, 'sending', ?)", (item["id"], channel, self._iso(now)))
        except sqlite3.IntegrityError:
            return None                                   # another process claimed it
        row_id = cur.lastrowid
        recent = con.execute("SELECT count(*) FROM notification_delivery WHERE channel = ? AND status = 'sent' "
                             "AND sent_at >= ?", (channel, self._iso(now - timedelta(hours=1)))).fetchone()[0]
        if recent >= per_hour:
            con.execute("UPDATE notification_delivery SET status = 'suppressed', error = ? WHERE id = ?",
                        (f"rate limit ({per_hour} per hour)", row_id))
            return "suppressed"
        transport = self.transports.get(channel)
        if transport is None:
            con.execute("UPDATE notification_delivery SET status = 'failed', error = 'channel not configured' WHERE id = ?", (row_id,))
            return "failed"
        subject, text = compose(item, base)
        try:
            transport.send(self.scrub(subject), self.scrub(text))
        except DeliveryError as err:
            con.execute("UPDATE notification_delivery SET status = 'failed', error = ? WHERE id = ?",
                        (self.scrub(str(err))[:300], row_id))
            return "failed"
        except Exception as err:                          # never let one channel break the pump
            con.execute("UPDATE notification_delivery SET status = 'failed', error = ? WHERE id = ?",
                        (f"unexpected {type(err).__name__}", row_id))
            return "failed"
        con.execute("UPDATE notification_delivery SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?",
                    (self._iso(self.clock()), row_id))
        return "sent"

    def send_direct(self, subject: str, text: str, channels: tuple[str, ...] = ("telegram", "email")) -> dict[str, str]:
        """Send without the database (used when the database itself is the problem). Returns per-channel result."""
        result = {}
        for channel in channels:
            transport = self.transports.get(channel)
            if transport is None:
                result[channel] = "not configured"
                continue
            try:
                transport.send(self.scrub(subject), self.scrub(text))
                result[channel] = "sent"
            except Exception as err:
                result[channel] = self.scrub(f"failed: {err}")[:200]
        return result

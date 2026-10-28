"""Sends ONE real Telegram message and ONE real e-mail through the configured channels.
Opt-in:  AICORP_LIVE_NOTIFY=1 pytest tests/test_notify_live.py -s"""
import os

import pytest

from aicorp import channels, notify
from aicorp.config import home
from aicorp.delivery import Notifier

pytestmark = pytest.mark.skipif(os.environ.get("AICORP_LIVE_NOTIFY") != "1", reason="sends real messages; opt-in")


def test_a_critical_alert_reaches_telegram_and_email(con):
    transports = channels.load_transports(home())
    assert set(transports) == {"telegram", "email"}, "both channels must be set up in config/secrets.env"
    nid, _ = notify.upsert(con, key="live-test", category="system", severity="critical",
                           title="Test alert (safe to ignore)",
                           detail="Sent by the AICorp live notification test. If you can read this on Telegram and in your inbox, "
                                  "critical alerts work.", link="/attention")
    stats = Notifier(transports).pump(con)
    print("\n[live] delivery result:", stats, [(d["channel"], d["status"], d["error"]) for d in notify.deliveries(con, nid)])
    assert {(d["channel"], d["status"]) for d in notify.deliveries(con, nid)} == {("dashboard", "sent"), ("telegram", "sent"), ("email", "sent")}

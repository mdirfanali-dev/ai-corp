from datetime import datetime, timedelta, timezone

import pytest

from aicorp import notify
from aicorp.search import search_events


def add(con, key, sev="warn", cat="blocked", title="A title", detail="", **kw):
    return notify.upsert(con, key=key, category=cat, severity=sev, title=title, detail=detail, **kw)[0]


def test_one_open_notification_per_cause_and_a_new_one_after_resolution(con):
    first, created = notify.upsert(con, key="k", category="system", severity="info", title="t")
    again, created2 = notify.upsert(con, key="k", category="system", severity="info", title="t")
    assert created and not created2 and first == again
    assert con.execute("SELECT repeat_count FROM notification WHERE id = ?", (first,)).fetchone()[0] == 2
    assert notify.resolve_key(con, "k") and not notify.resolve_key(con, "k")
    third, created3 = notify.upsert(con, key="k", category="system", severity="info", title="t")
    assert created3 and third != first
    with pytest.raises(ValueError):
        notify.upsert(con, key="x", category="system", severity="loud", title="t")


def test_unread_count_rules(con):
    a, b, c, d = (add(con, f"k{i}") for i in range(4))
    assert notify.unread_count(con) == 4
    notify.mark_read(con, [a])
    notify.snooze(con, [b], 3600)
    notify.resolve_key(con, "k2")
    assert notify.unread_count(con) == 1                 # only d
    notify.unsnooze(con, [b])
    assert notify.unread_count(con) == 2
    notify.mark_read(con, [a], read=False)
    assert notify.unread_count(con) == 3
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    notify.snooze(con, [a], 3600)
    assert notify.unread_count(con) == 2 and notify.unread_count(con, now=later) == 3     # snooze expires


def test_filters_search_and_paging(con):
    add(con, "a", "critical", "blocked", "Aria is blocked: CAPTCHA", "recaptcha on a shop site")
    add(con, "b", "warn", "approval", "Approval needed: publish blog post", "tier 3 draft")
    add(con, "c", "info", "review", "Ready for review: market report", "")
    add(con, "d", "critical", "failed", "Task failed: cost model", "after 3 attempts")
    titles = lambda **f: [n["title"] for n in notify.list_notifications(con, **f)]
    assert len(titles()) == 4
    assert set(titles(severity="critical")) == {"Aria is blocked: CAPTCHA", "Task failed: cost model"}
    assert titles(category="approval") == ["Approval needed: publish blog post"]
    assert titles(q="captcha") == ["Aria is blocked: CAPTCHA"] and titles(q="recapt") == ["Aria is blocked: CAPTCHA"]
    assert titles(q="blog") == ["Approval needed: publish blog post"] and titles(q="nonexistent") == []
    assert titles(q="critical drop table;") == []                       # hostile input is harmless
    assert len(titles(limit=2)) == 2 and len(titles(limit=2, offset=2)) == 2 and len(set(titles(limit=2) + titles(limit=2, offset=2))) == 4
    far = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert titles(since=far) == [] and len(titles(until=far)) == 4


def test_search_follows_edits_and_ignores_stale_text(con):
    nid = add(con, "k", title="Old wording about invoices")
    assert [n["id"] for n in notify.list_notifications(con, q="invoices")] == [nid]
    notify.upsert(con, key="k", category="blocked", severity="warn", title="New wording about contracts")
    assert notify.list_notifications(con, q="invoices") == [] and [n["id"] for n in notify.list_notifications(con, q="contracts")] == [nid]


def test_bulk_actions_selective_all_and_matching(con):
    ids = [add(con, f"k{i}", "critical" if i % 2 else "warn") for i in range(6)]
    assert notify.mark_read(con, ids[:2]) == 2 and notify.unread_count(con) == 4
    crit_unread = notify.ids_matching(con, severity="critical", state="unread")
    assert len(crit_unread) == 3 - len([i for i in ids[:2] if ids.index(i) % 2])
    notify.mark_read(con, notify.ids_matching(con, state="unread"))
    assert notify.unread_count(con) == 0
    assert notify.mark_read(con, []) == 0
    notify.pin(con, [ids[3]])
    assert notify.list_notifications(con)[0]["id"] == ids[3]          # pinned first
    notify.pin(con, [ids[3]], False)
    notify.acknowledge(con, [ids[0]])
    row = notify.get(con, ids[0])
    assert row["ack_at"] and row["read_at"]
    assert notify.get(con, 99999) is None


def test_actions_are_logged_once_per_request_not_per_item(con):
    ids = [add(con, f"k{i}") for i in range(5)]
    notify.mark_read(con, ids)
    notify.snooze(con, ids[:2], 3600)
    logged = search_events(con, event_type="notification.marked_read")
    assert len(logged) == 1 and '"count":5' in logged[0]["payload"] and logged[0]["actor_name"] == "Founder"
    assert len(search_events(con, event_type="notification.snoozed")) == 1


def test_settings_have_defaults_and_persist(con):
    assert notify.get_setting(con, "routing")["critical"] == ["dashboard", "telegram", "email"]
    notify.get_setting(con, "routing")["critical"].append("x")            # callers cannot corrupt the defaults
    assert notify.get_setting(con, "routing")["critical"] == ["dashboard", "telegram", "email"]
    notify.set_setting(con, "rate_per_hour", 7)
    assert notify.get_setting(con, "rate_per_hour") == 7 and notify.get_setting(con, "nope") is None


def test_creation_is_logged_for_the_live_feed(con):
    add(con, "k", "critical", title="Something broke", task_id="T-1")
    row = search_events(con, event_type="notification.created")[0]
    assert row["summary"] == "[critical] Something broke" and row["task_id"] == "T-1"

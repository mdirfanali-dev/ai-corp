import sqlite3

import pytest

from aicorp import events
from conftest import add_employee, add_role


def _log(con, n=5):
    for i in range(n):
        events.append(con, type="note", summary=f"event {i}")


def test_chain_verifies_and_links(con):
    _log(con)
    result = events.verify_chain(con)
    assert result.ok and result.count == 5
    rows = con.execute("SELECT prev_hash, hash FROM event ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] == events.GENESIS
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, 5))


def test_update_and_delete_are_blocked(con):
    _log(con, 2)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE event SET summary = 'x' WHERE id = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM event WHERE id = 1")


def test_tampering_is_detected_even_if_triggers_are_removed(con):
    _log(con)
    con.execute("DROP TRIGGER event_no_update")
    con.execute("UPDATE event SET summary = 'forged' WHERE id = 3")
    result = events.verify_chain(con)
    assert not result.ok and result.bad_id == 3


def test_deleting_a_middle_row_is_detected(con):
    _log(con)
    con.execute("DROP TRIGGER event_no_delete")
    con.execute("DELETE FROM event WHERE id = 3")
    result = events.verify_chain(con)
    assert not result.ok and result.bad_id == 4


def test_name_snapshot_survives_rename(con):
    add_employee(con, "e1", "Aria")
    add_role(con, "r1", "Marketing Head")
    con.execute("INSERT INTO employee_role VALUES ('e1', 'r1', 'now')")
    events.append_as(con, "e1", type="note", summary="before rename")
    con.execute("UPDATE employee SET name = 'Aria Khan' WHERE emp_id = 'e1'")
    events.append_as(con, "e1", type="note", summary="after rename")
    names = [r["actor_name"] for r in con.execute("SELECT actor_name FROM event ORDER BY id")]
    assert names == ["Aria", "Aria Khan"]
    assert con.execute("SELECT actor_designation FROM event WHERE id = 1").fetchone()[0] == "Marketing Head"
    assert events.verify_chain(con).ok


def test_impersonation_keeps_real_identity(con):
    add_employee(con, "e1", "Aria")
    add_role(con, "r1", "Marketing Head")
    con.execute("INSERT INTO employee_role VALUES ('e1', 'r1', 'now')")
    events.append(con, type="note", summary="Founder works as Aria", actor_type="founder",
                  actor_name="Founder", actor_designation="Chairman",
                  acting_as_emp_id="e1", acting_as_name="Aria", acting_as_role="Marketing Head",
                  mode="impersonate")
    row = con.execute("SELECT * FROM event").fetchone()
    assert (row["actor_name"], row["actor_designation"]) == ("Founder", "Chairman")
    assert (row["acting_as_name"], row["acting_as_role"], row["mode"]) == (
        "Aria", "Marketing Head", "impersonate")


def test_impersonation_requires_target_name(con):
    with pytest.raises(ValueError):
        events.append(con, type="note", summary="bad", actor_type="founder",
                      actor_name="Founder", mode="impersonate")
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] == 0


def test_failed_append_leaves_no_partial_state(con):
    _log(con, 2)
    with pytest.raises(sqlite3.IntegrityError):
        events.append(con, type="note", summary="x", actor_type="alien")
    assert events.verify_chain(con).ok and events.head(con)[0] == 2


def test_incremental_verification_walks_only_new_events_and_still_catches_tampering(con):
    for i in range(20):
        events.append(con, type="t", summary=f"e{i}")
    first = events.verify_incremental(con)
    assert first.ok and first.count == 20
    for i in range(5):
        events.append(con, type="t", summary=f"more{i}")
    second = events.verify_incremental(con)
    assert second.ok and second.count == 25
    con.execute("DROP TRIGGER event_no_update")
    con.execute("UPDATE event SET summary = 'forged' WHERE id = 22")           # a row after the checkpoint is edited
    assert events.verify_chain(con).ok is False                                 # an edit inside the checked part: the full walk always catches it
    events.append(con, type="t", summary="after")
    con.execute("UPDATE event SET hash = 'x' WHERE id = 25")                    # the checkpoint row itself changed
    assert events.verify_incremental(con).ok is False

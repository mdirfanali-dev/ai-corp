import sqlite3

import pytest

from conftest import add_employee, add_role


def test_employee_name_unique_case_insensitive(con):
    add_employee(con, "e1", "Aria Khan")
    for variant in ("aria khan", "ARIA KHAN"):
        with pytest.raises(sqlite3.IntegrityError):
            add_employee(con, f"x-{variant}", variant)


def test_emp_id_is_immutable(con):
    add_employee(con, "e1", "Aria")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        con.execute("UPDATE employee SET emp_id = 'e2' WHERE emp_id = 'e1'")


def test_employee_can_have_zero_one_or_many_roles(con):
    add_employee(con, "e0", "Bench")
    add_employee(con, "e1", "One")
    add_employee(con, "e2", "Many")
    add_role(con, "r1", "Researcher")
    add_role(con, "r2", "Writer")
    con.execute("INSERT INTO employee_role VALUES ('e1', 'r1', 'now')")
    con.execute("INSERT INTO employee_role VALUES ('e2', 'r1', 'now')")
    con.execute("INSERT INTO employee_role VALUES ('e2', 'r2', 'now')")
    counts = dict(con.execute("SELECT emp_id, count(r.role_id) FROM employee e "
                              "LEFT JOIN employee_role r USING (emp_id) GROUP BY emp_id").fetchall())
    assert counts == {"e0": 0, "e1": 1, "e2": 2}


def test_invalid_kind_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        add_employee(con, "e1", "X", kind="contractor")


def test_migrate_is_idempotent(con):
    from aicorp.db import migrate

    assert migrate(con) == []

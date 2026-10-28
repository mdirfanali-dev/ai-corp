import pytest

from aicorp import events

from aicorp.config import db_path, utc_now
from aicorp.db import connect, migrate


@pytest.fixture
def home(tmp_path):
    for name in ("company", "skills", "memory", "artifacts", "workspaces", "config", "database"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def con(home):
    connection = connect(db_path(home))
    migrate(connection)
    yield connection
    connection.close()


def add_employee(con, emp_id, name, kind="permanent"):
    con.execute(
        "INSERT INTO employee (emp_id, name, kind, created_at) VALUES (?, ?, ?, ?)",
        (emp_id, name, kind, utc_now()),
    )


def add_role(con, role_id, name):
    con.execute(
        "INSERT INTO role (role_id, name, created_at) VALUES (?, ?, ?)", (role_id, name, utc_now())
    )


@pytest.fixture(autouse=True)
def _reset_redactor():
    yield
    events.set_redactor(None)

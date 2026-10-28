import json
from datetime import datetime, timedelta, timezone

import pytest

from aicorp import org
from aicorp.search import search_events


@pytest.fixture
def seeded(home, con):
    org.seed_system(con, home)
    return con


def emp(con, name):
    return con.execute("SELECT emp_id FROM employee WHERE name = ?", (name,)).fetchone()["emp_id"]


def make(con, home, name, **extra):
    return org.create_employee(con, home, {"name": name, **extra})


def test_seed_creates_ceo_and_advisor_once(home, con):
    created = org.seed_system(con, home)
    assert {"employee:CEO", "employee:Advisor"} <= set(created)
    assert org.seed_system(con, home) == []
    ceo, advisor = org.get_employee(con, emp(con, "CEO")), org.get_employee(con, emp(con, "Advisor"))
    assert (ceo["kind"], advisor["kind"]) == ("system", "system")
    assert advisor["capabilities"] == {"execute": False, "assign": False, "approve": False}
    assert ceo["capabilities"]["assign"] and not ceo["capabilities"]["approve"]
    assert (home / "company" / "employees" / advisor["emp_id"] / "instructions.md").read_text().startswith("# Advisor")


def test_zero_one_many_roles_and_bench_defaults(seeded, home):
    r1 = org.create_role(seeded, {"name": "Researcher", "execute": True})
    r2 = org.create_role(seeded, {"name": "Writer"})
    bench = org.get_employee(seeded, make(seeded, home, "Bench"))
    one = org.get_employee(seeded, make(seeded, home, "One", role_ids=[r1]))
    many = org.get_employee(seeded, make(seeded, home, "Many", role_ids=[r1, r2]))
    assert (len(bench["roles"]), len(one["roles"]), len(many["roles"])) == (0, 1, 2)
    assert bench["capabilities"]["execute"] is False  # bench = read-only baseline
    assert one["capabilities"]["execute"] is True


def test_duplicate_names_blocked_with_friendly_error(seeded, home):
    make(seeded, home, "Aria Khan")
    with pytest.raises(org.ValidationError, match="already used"):
        make(seeded, home, "aria khan")
    with pytest.raises(org.ValidationError, match="already used"):
        make(seeded, home, "CEO")


def test_name_validation(seeded, home):
    for bad in ("", "   ", "x" * 61, "bad\x00name"):
        with pytest.raises(org.ValidationError):
            make(seeded, home, bad)
    assert org.get_employee(seeded, make(seeded, home, "  Spaced   Out  "))["name"] == "Spaced Out"


def test_every_change_is_logged_as_founder(seeded, home):
    emp_id = make(seeded, home, "Aria")
    org.update_employee(seeded, home, emp_id, {"name": "Aria Khan", "skills": "seo, writing"})
    org.archive_employee(seeded, home, emp_id)
    types = [r["type"] for r in search_events(seeded, actor="Founder", limit=10)]
    assert types[:3] == ["employee.archived", "employee.updated", "employee.created"]
    updated = search_events(seeded, event_type="employee.updated")[0]
    assert updated["actor_name"] == "Founder" and updated["actor_designation"] == "Chairman"
    assert "name" in json.loads(updated["payload"])["changed"]


def test_files_mirror_database(seeded, home):
    role = org.create_role(seeded, {"name": "Researcher"})
    emp_id = make(seeded, home, "Aria", role_ids=[role], skills="seo", instructions="# Aria\nBe precise.")
    folder = home / "company" / "employees" / emp_id
    doc = json.loads((folder / "employee.json").read_text())
    assert doc["name"] == "Aria" and doc["roles"] == ["Researcher"] and doc["skills"] == ["seo"]
    assert (folder / "instructions.md").read_text() == "# Aria\nBe precise.\n"
    assert not (home / "workspaces" / "employees" / emp_id).exists()      # the runner creates it, as the worker user
    org.update_employee(seeded, home, emp_id, {"name": "Aria 2", "instructions": "changed"})
    assert json.loads((folder / "employee.json").read_text())["name"] == "Aria 2"
    assert (folder / "instructions.md").read_text() == "changed\n"


def test_temporary_employee_expires_then_can_be_deleted_and_logs_remain(seeded, home):
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone().strftime("%Y-%m-%dT%H:%M")
    emp_id = make(seeded, home, "Ben", kind="temporary", expires_at=soon)
    assert org.expire_temporary(seeded, home) == []
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert org.expire_temporary(seeded, home, now=later) == [emp_id]
    assert org.get_employee(seeded, emp_id)["status"] == "archived"
    org.delete_employee(seeded, home, emp_id)
    gone = org.get_employee(seeded, emp_id)
    assert gone["deleted_at"] and "Ben" in gone["name"] and gone["name"] != "Ben"
    assert not (home / "company" / "employees" / emp_id).exists()
    assert any(emp_id in e["trashed"] for e in __import__("aicorp.trash", fromlist=["x"]).list_trash(home))
    assert [e for e in org.list_employees(seeded) if e["emp_id"] == emp_id] == []
    assert len(search_events(seeded, "Ben")) >= 2  # created + deleted entries remain searchable
    make(seeded, home, "Ben")  # name is free again


def test_permanent_and_system_employees_cannot_be_deleted(seeded, home):
    perm = make(seeded, home, "Cato")
    with pytest.raises(org.ValidationError, match="only be archived"):
        org.delete_employee(seeded, home, perm)
    org.archive_employee(seeded, home, perm)
    org.restore_employee(seeded, home, perm)
    assert org.get_employee(seeded, perm)["status"] == "idle"
    for name in ("CEO", "Advisor"):
        for action in (org.archive_employee, org.delete_employee):
            with pytest.raises(org.ValidationError, match="built-in"):
                action(seeded, home, emp(seeded, name))


def test_advisor_can_never_execute_assign_or_approve(seeded, home):
    advisor = emp(seeded, "Advisor")
    for flag in ("execute", "assign"):
        with pytest.raises(org.ValidationError, match="cannot be given"):
            org.update_employee(seeded, home, advisor, {"name": "Advisor", flag: True})
    power = org.create_role(seeded, {"name": "Operator", "execute": True})
    with pytest.raises(org.ValidationError, match="cannot be given to the Advisor"):
        org.update_employee(seeded, home, advisor, {"name": "Advisor", "role_ids": [power]})
    # a role already held by the Advisor cannot be upgraded either
    chief = seeded.execute("SELECT role_id FROM role WHERE name = 'Chief of Staff'").fetchone()[0]
    with pytest.raises(org.ValidationError, match="held by the Advisor"):
        org.update_role(seeded, chief, {"name": "Chief of Staff", "assign": True})
    caps = org.get_employee(seeded, advisor)["capabilities"]
    assert caps == {"execute": False, "assign": False, "approve": False}


def test_advisor_cap_holds_even_if_database_is_tampered(seeded, home):
    advisor = emp(seeded, "Advisor")
    seeded.execute("UPDATE employee SET profile = ? WHERE emp_id = ?",
                   (json.dumps({"capabilities": {"execute": True, "assign": True}}), advisor))
    assert org.effective_capabilities(seeded, advisor) == {"execute": False, "assign": False, "approve": False}


def test_reporting_cycles_and_unknown_references_rejected(seeded, home):
    a, b = make(seeded, home, "A"), make(seeded, home, "B")
    org.update_employee(seeded, home, b, {"name": "B", "reports_to": a})
    with pytest.raises(org.ValidationError, match="subordinate"):
        org.update_employee(seeded, home, a, {"name": "A", "reports_to": b})
    with pytest.raises(org.ValidationError, match="themselves"):
        org.update_employee(seeded, home, a, {"name": "A", "reports_to": a})
    with pytest.raises(org.ValidationError, match="Unknown role"):
        make(seeded, home, "C", role_ids=["R-nope"])
    with pytest.raises(org.ValidationError, match="Unknown team"):
        make(seeded, home, "C", team_ids=["T-nope"])


def test_roles_and_teams(seeded, home):
    role = org.create_role(seeded, {"name": "Researcher"})
    with pytest.raises(org.ValidationError, match="already exists"):
        org.create_role(seeded, {"name": "researcher"})
    a, b = make(seeded, home, "A", role_ids=[role]), make(seeded, home, "B")
    with pytest.raises(org.ValidationError, match="still assigned"):
        org.delete_role(seeded, role)
    for name in org.SYSTEM_ROLES:
        rid = seeded.execute("SELECT role_id FROM role WHERE name = ?", (name,)).fetchone()[0]
        with pytest.raises(org.ValidationError, match="Built-in"):
            org.delete_role(seeded, rid)
    team = org.create_team(seeded, {"name": "Marketing", "head_emp_id": a, "member_ids": [b]})
    t = org.get_team(seeded, team)
    assert t["head_name"] == "A" and {m["name"] for m in t["members"]} == {"A", "B"}
    with pytest.raises(org.ValidationError, match="already exists"):
        org.create_team(seeded, {"name": "MARKETING"})
    assert org.get_employee(seeded, a)["teams"][0]["name"] == "Marketing"
    assert [e["name"] for e in org.list_employees(seeded, team_id=team)] == ["A", "B"]
    org.delete_team(seeded, team)
    assert org.list_teams(seeded) == [] and org.get_employee(seeded, a)["teams"] == []


def test_failed_create_leaves_nothing_behind(seeded, home):
    before = seeded.execute("SELECT count(*) FROM event").fetchone()[0]
    with pytest.raises(org.ValidationError):
        make(seeded, home, "Dup", role_ids=["R-missing"])
    assert seeded.execute("SELECT count(*) FROM event").fetchone()[0] == before
    assert seeded.execute("SELECT count(*) FROM employee WHERE name = 'Dup'").fetchone()[0] == 0

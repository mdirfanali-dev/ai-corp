import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from aicorp import backup, events, trash
from aicorp.config import db_path
from aicorp.db import connect
from conftest import add_employee


def tree_hashes(root, names):
    out = {}
    for name in names:
        for p in sorted((root / name).rglob("*")):
            if p.is_file():
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_trash_moves_restores_and_refuses_outside(home, tmp_path_factory):
    f = home / "workspaces" / "note.txt"
    f.write_text("hello")
    dest = trash.move_to_trash(home, f, reason="test", by="Founder")
    assert not f.exists() and dest.read_text() == "hello"
    entry = trash.list_trash(home)[0]
    assert entry["original"] == "workspaces/note.txt"
    trash.restore(home, entry["trashed"])
    assert f.read_text() == "hello"
    outside = tmp_path_factory.mktemp("other") / "x.txt"
    outside.write_text("x")
    with pytest.raises(ValueError):
        trash.move_to_trash(home, outside)
    with pytest.raises(ValueError):
        trash.move_to_trash(home, home)


def test_purge_needs_confirmation_and_respects_retention(home):
    f = home / "workspaces" / "old.txt"
    f.write_text("old")
    dest = trash.move_to_trash(home, f)
    with pytest.raises(PermissionError):
        trash.purge_expired(home)
    assert trash.purge_expired(home, confirm=True) == []  # not expired yet
    assert dest.exists()
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert len(trash.purge_expired(home, confirm=True, now=later)) == 1
    assert not dest.exists()


def test_backup_restore_roundtrip_excludes_secrets(home, con, tmp_path):
    (home / "company" / "employees").mkdir(parents=True)
    (home / "company" / "employees" / "e1.json").write_text('{"name": "Aria"}')
    (home / "memory" / "decision.md").write_text("# decision")
    (home / "workspaces" / "aria").mkdir()
    (home / "workspaces" / "aria" / "out.txt").write_text("result")
    (home / "config" / "secrets.env").write_text("TOKEN=super-secret-value")
    (home / ".trash").mkdir()
    (home / ".trash" / "junk.txt").write_text("junk")
    add_employee(con, "e1", "Aria")
    for i in range(20):
        events.append(con, type="note", summary=f"event {i}")
    archive = backup.create_backup(home)

    target = tmp_path / "restored"
    info = backup.restore_backup(archive, target)
    assert info["events"] == 20
    names = ("company", "memory", "workspaces")
    assert tree_hashes(target, names) == tree_hashes(home, names)
    assert not (target / "config" / "secrets.env").exists()
    assert not (target / ".trash").exists()
    import tarfile
    with tarfile.open(archive) as tar:
        member_names = tar.getnames()
    assert not any(n.startswith(("config/", ".trash/")) for n in member_names)
    restored = connect(db_path(target))
    original = [tuple(r) for r in con.execute("SELECT * FROM event ORDER BY id")]
    copy = [tuple(r) for r in restored.execute("SELECT * FROM event ORDER BY id")]
    assert original == copy
    assert restored.execute("SELECT name FROM employee").fetchone()[0] == "Aria"
    restored.close()


def test_restore_refuses_non_empty_target(home, con, tmp_path):
    archive = backup.create_backup(home)
    target = tmp_path / "busy"
    target.mkdir()
    (target / "file").write_text("x")
    with pytest.raises(FileExistsError):
        backup.restore_backup(archive, target)


def _rewrite_archive(src_path, dst_path, member, change):
    import io
    import tarfile

    with tarfile.open(src_path) as src, tarfile.open(dst_path, "w:gz") as dst:
        for m in src.getmembers():
            if not m.isfile():
                dst.addfile(m)
                continue
            data = src.extractfile(m).read()
            if m.name == member:
                data = change(data)
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))


def test_restore_detects_corrupted_database(home, con, tmp_path):
    events.append(con, type="note", summary="one")
    archive = backup.create_backup(home)
    bad = tmp_path / "bad.tar.gz"
    _rewrite_archive(archive, bad, "database/company.sqlite", lambda d: d[:-10] + b"CORRUPTED!")
    with pytest.raises(ValueError, match="checksum mismatch"):
        backup.restore_backup(bad, tmp_path / "r2")


def test_restore_detects_modified_file(home, con, tmp_path):
    (home / "memory" / "a.md").write_text("original")
    archive = backup.create_backup(home)
    bad = tmp_path / "bad2.tar.gz"
    _rewrite_archive(archive, bad, "memory/a.md", lambda d: b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch: memory/a.md"):
        backup.restore_backup(bad, tmp_path / "r3")

"""Backup and restore of the company state.

Included: database snapshot, company/, skills/, memory/, artifacts/, workspaces/.
Never included: secrets, browser profiles, virtual environments, trash, other backups.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import events
from .config import db_path, utc_now
from .db import connect

INCLUDE_DIRS = ("company", "skills", "memory", "artifacts", "workspaces")
DB_ARCHIVE_NAME = "database/company.sqlite"
FORMAT_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(home: Path):
    for name in INCLUDE_DIRS:
        base = home / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path


def create_backup(home: Path, dest_dir: Path | None = None) -> Path:
    home = home.resolve()
    dest_dir = (dest_dir or home / "backups").resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = dest_dir / f"aicorp-backup-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "company.sqlite"
        src = connect(db_path(home))
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # consistent online snapshot
        finally:
            dst.close()
        head_id, head_hash = events.head(src)
        src.close()
        manifest = {
            "format": FORMAT_VERSION, "created_at": utc_now(),
            "event_head": {"id": head_id, "hash": head_hash},
            "files": {DB_ARCHIVE_NAME: _sha256(snapshot)},
        }
        members: list[tuple[Path, str]] = [(snapshot, DB_ARCHIVE_NAME)]
        for path in _files(home):
            rel = path.relative_to(home).as_posix()
            manifest["files"][rel] = _sha256(path)
            members.append((path, rel))
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            for path, arcname in members:
                tar.add(path, arcname=arcname)
    return archive


def restore_backup(archive: Path, target: Path) -> dict:
    """Restore into an empty or new directory, then verify checksums and the event chain."""
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} is not empty; restore into a fresh directory")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(target, filter="data")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    for rel, expected in manifest["files"].items():
        if _sha256(target / rel) != expected:
            raise ValueError(f"checksum mismatch: {rel}")
    (target / "database").mkdir(exist_ok=True)
    con = connect(db_path(target))
    try:
        chain = events.verify_chain(con)
        head_id, head_hash = events.head(con)
    finally:
        con.close()
    if not chain.ok:
        raise ValueError(f"event chain invalid after restore at id {chain.bad_id}: {chain.reason}")
    expected_head = manifest["event_head"]
    if (head_id, head_hash) != (expected_head["id"], expected_head["hash"]):
        raise ValueError("event log head differs from the backup manifest")
    return {"files": len(manifest["files"]), "events": chain.count, "head": head_hash}

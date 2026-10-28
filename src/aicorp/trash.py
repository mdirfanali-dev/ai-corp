"""Soft delete for files: nothing is removed, it is moved into ``.trash`` with a retention date.

Purging is a separate, explicit step meant for the human owner (dashboard, with a
confirmation). Automated workers must never call :func:`purge_expired`.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 30
MANIFEST = "manifest.jsonl"


def _trash(home: Path) -> Path:
    return home / ".trash"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def move_to_trash(home: Path, path: Path | str, *, reason: str = "", by: str = "system") -> Path:
    """Move ``path`` (which must live inside ``home``) into the trash and return the new location."""
    home = home.resolve()
    src = Path(path).resolve()
    if not _inside(src, home):
        raise ValueError("refusing to trash a path outside the project")
    if _inside(src, _trash(home)) or src == home:
        raise ValueError("refusing to trash the trash or the project root")
    if not src.exists():
        raise FileNotFoundError(src)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    dest = _trash(home) / stamp / src.relative_to(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    entry = {
        "original": str(src.relative_to(home)), "trashed": str(dest.relative_to(home)),
        "ts": now.isoformat(), "reason": reason, "by": by,
        "expires_at": (now + timedelta(days=RETENTION_DAYS)).isoformat(),
    }
    with open(_trash(home) / MANIFEST, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return dest


def list_trash(home: Path) -> list[dict]:
    manifest = _trash(home) / MANIFEST
    if not manifest.exists():
        return []
    return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]


def restore(home: Path, trashed: str) -> Path:
    """Move an item back to where it came from. Fails if something is already there."""
    home = home.resolve()
    for entry in list_trash(home):
        if entry["trashed"] == trashed:
            src, dest = home / entry["trashed"], home / entry["original"]
            if dest.exists():
                raise FileExistsError(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            return dest
    raise KeyError(trashed)


def purge_expired(home: Path, *, confirm: bool = False, now: datetime | None = None) -> list[str]:
    """Permanently delete trash items past their retention date. Requires ``confirm=True``."""
    if not confirm:
        raise PermissionError("purge_expired needs confirm=True (human-initiated only)")
    home = home.resolve()
    now = now or datetime.now(timezone.utc)
    kept: list[dict] = []
    purged: list[str] = []
    for entry in list_trash(home):
        item = home / entry["trashed"]
        if datetime.fromisoformat(entry["expires_at"]) <= now and _inside(item.resolve(), _trash(home)):
            if item.is_dir():
                shutil.rmtree(item)
            elif item.exists():
                item.unlink()
            purged.append(entry["trashed"])
        else:
            kept.append(entry)
    with open(_trash(home) / MANIFEST, "w", encoding="utf-8") as fh:
        for entry in kept:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return purged

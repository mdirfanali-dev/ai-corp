"""Emergency stop. A ``STOP`` file in the data folder halts every worker; deleting it resumes.

A plain file is used on purpose: it works even when the database or the dashboard is unusable
(``touch STOP`` from any terminal).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import events
from .config import utc_now
from .org import FOUNDER, authorize

STOP_FILE = "STOP"


def stop_path(home: Path) -> Path:
    return home / STOP_FILE


def engaged(home: Path) -> bool:
    return stop_path(home).exists()


def info(home: Path) -> dict | None:
    path = stop_path(home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}


def engage(con, home: Path, actor: dict = FOUNDER, reason: str = "") -> bool:
    """Create the STOP file. Returns False if it was already engaged."""
    authorize(con, actor, founder_only=True)
    path = stop_path(home)
    if path.exists():
        return False
    path.write_text(json.dumps({"by": actor["actor_name"], "reason": reason, "at": utc_now()}), encoding="utf-8")
    events.append(con, type="killswitch.engaged", summary=f"Kill switch engaged{': ' + reason if reason else ''}",
                  payload={"reason": reason}, **actor)
    return True


def release(con, home: Path, actor: dict = FOUNDER) -> bool:
    authorize(con, actor, founder_only=True)
    path = stop_path(home)
    if not path.exists():
        return False
    path.unlink()
    events.append(con, type="killswitch.released", summary="Kill switch released", **actor)
    return True

"""Locations used by AICorp. Nothing here is machine specific."""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Project data root. Override with the AICORP_HOME environment variable."""
    env = os.environ.get("AICORP_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


LAYOUT = ("company", "skills", "memory", "artifacts", "workspaces", "config", "database")


def ensure_layout(base: Path | None = None) -> Path:
    """Create the standard folders of a data directory (safe to repeat). ``config`` is private to the owner."""
    root = base or home()
    for name in LAYOUT:
        (root / name).mkdir(parents=True, exist_ok=True)
    try:
        (root / "config").chmod(0o700)
    except OSError:
        pass                       # e.g. a file system without Unix permissions
    return root


def db_path(base: Path | None = None) -> Path:
    return (base or home()) / "database" / "company.sqlite"


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

"""Writing into an employee workspace on behalf of a program that is not the agent (dashboard, browser runner).

In the real set-up a workspace belongs to the low-privilege worker user, so the dashboard user may not be
able to write there; the write then goes through ``sudo -n -u <worker>`` exactly like the agent's own tools.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class WorkspaceWriteError(Exception):
    pass


def write(target: Path, data: bytes) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return
    except PermissionError:
        pass
    user = os.environ.get("AICORP_WORKER_USER")
    if not user:
        raise WorkspaceWriteError("The workspace cannot be written by this user and no worker user is configured")
    try:
        subprocess.run(["sudo", "-n", "-u", user, "mkdir", "-p", str(target.parent)], check=True, capture_output=True, timeout=20)
        subprocess.run(["sudo", "-n", "-u", user, "tee", str(target)], input=data, check=True, capture_output=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        raise WorkspaceWriteError("The workspace belongs to the worker user and writing there failed (check the sudo set-up)") from None


def move_in(source: Path, target: Path) -> None:
    """Copy a file we own (e.g. a checked download) into the workspace, then remove the source."""
    write(target, source.read_bytes())
    source.unlink()

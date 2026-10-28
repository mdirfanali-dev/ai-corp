"""SQLite connection and migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import utc_now

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(path: Path | str, *, cross_thread: bool = False) -> sqlite3.Connection:
    """Open the database in WAL mode with foreign keys on.

    The connection is in autocommit mode; callers open transactions explicitly.
    ``cross_thread`` lets the web layer hand a per-request connection between the event loop
    and its worker threads; a connection must still only be used by one request at a time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=not cross_thread)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def migrate(con: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in filename order. Returns the versions applied."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r["version"] for r in con.execute("SELECT version FROM schema_migrations")}
    applied: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version in done:
            continue
        script = (
            "BEGIN;\n"
            + sql_file.read_text(encoding="utf-8")
            + f"\nINSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ('{version}', '{utc_now()}');\nCOMMIT;"
        )
        try:
            con.executescript(script)
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied

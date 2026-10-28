"""Command line helpers: ``python -m aicorp.cli <command>``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import backup, events, search
from .config import db_path, home
from .db import connect, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicorp", description="AICorp maintenance commands")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create or upgrade the database")
    sub.add_parser("verify-chain", help="check the audit log for tampering")
    find = sub.add_parser("search", help="search the audit log")
    find.add_argument("text", nargs="?", default="")
    find.add_argument("--actor")
    find.add_argument("--task")
    find.add_argument("--limit", type=int, default=20)
    sub.add_parser("backup", help="write a backup archive")
    restore = sub.add_parser("restore", help="restore a backup into a new, empty directory")
    restore.add_argument("archive", type=Path)
    restore.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    base = home()
    if args.cmd == "restore":
        print(json.dumps(backup.restore_backup(args.archive, args.target), indent=2))
        return 0
    if args.cmd == "backup":
        print(backup.create_backup(base))
        return 0
    con = connect(db_path(base))
    try:
        if args.cmd == "migrate":
            print("applied:", ", ".join(migrate(con)) or "nothing to do")
        elif args.cmd == "verify-chain":
            result = events.verify_chain(con)
            print(result)
            return 0 if result.ok else 1
        elif args.cmd == "search":
            for row in search.search_events(con, args.text, actor=args.actor,
                                            task_id=args.task, limit=args.limit):
                who = row["actor_name"]
                if row["acting_as_name"]:
                    who += f" (acting as {row['acting_as_name']})"
                snippet = row["snippet"].replace("\x02", "[").replace("\x03", "]")
                print(f"{row['ts']}  {who}  {row['type']}  {snippet}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

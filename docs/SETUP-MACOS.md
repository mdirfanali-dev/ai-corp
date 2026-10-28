# macOS notes (untested)

AICorp has **not been run on macOS**. The Python code is portable, but the worker-user script is Linux-specific
(`useradd`, `/etc/sudoers.d`, `getent`). If you try it, this is the equivalent you would do by hand; please report what you find.

1. Install Python 3.12+ and follow the install steps in [SETUP-LINUX.md](SETUP-LINUX.md) (sections 1-2).
2. Create a standard (non-admin) user, e.g. with *System Settings → Users & Groups*, with no access to your home folder.
3. Give your own account permission to run commands as that user without a password (a `sudoers` entry added with `visudo`,
   limited to `(that-user) NOPASSWD: ALL`).
4. Make the data folder traversable but not listable by others (`chmod 711`) and the `workspaces/` folder writable by the worker user
   and your group.
5. Install and log in to Claude Code as that user, then set `AICORP_WORKER_USER` and `AICORP_CLAUDE_BIN` as on Linux.

Until someone verifies these steps, treat macOS as a development platform only, not as an isolated one.

#!/usr/bin/env bash
# AICorp - create a low-privilege worker user and lock down the project tree (Linux / WSL2).
# Run:  sudo bash scripts/setup_worker_user.sh [--owner USER] [--root PROJECT_DIR] [--worker NAME]
# Safe to re-run. Does NOT delete anything and does NOT touch your home directory.
set -euo pipefail

usage() { echo "Usage: sudo bash $0 [--owner USER] [--root PROJECT_DIR] [--worker NAME]"; exit "${1:-0}"; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="${SUDO_USER:-}"
W="aicorp-worker"
while [ $# -gt 0 ]; do
  case "$1" in
    --owner) OWNER="${2:?}"; shift 2 ;;
    --root) ROOT="$(cd "${2:?}" && pwd)"; shift 2 ;;
    --worker) W="${2:?}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) usage 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -n "$OWNER" ] || { echo "Cannot tell who owns the project; pass --owner USER."; exit 1; }
id "$OWNER" >/dev/null 2>&1 || { echo "No such user: $OWNER"; exit 1; }
[ -d "$ROOT/src/aicorp" ] || { echo "$ROOT does not look like an AICorp project directory (pass --root)."; exit 1; }
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"

echo "== 1. worker user"
if id "$W" >/dev/null 2>&1; then echo "exists"; else useradd -m -s /bin/bash "$W"; fi
passwd -l "$W" >/dev/null   # no password login

echo "== 2. permissions"
chmod -R o-rwx "$ROOT"            # others: nothing
chmod 711 "$ROOT"                 # worker may traverse, not list
mkdir -p "$ROOT/workspaces"
chown -R "$W:$OWNER" "$ROOT/workspaces"
chmod -R u+rwX,g+rwX,o-rwx "$ROOT/workspaces"
chmod 2770 "$ROOT/workspaces"     # setgid: new files keep group $OWNER, so the dashboard can read them

echo "== 3. sudoers (only $OWNER may run commands AS $W, without a password)"
TMP="$(mktemp)"
echo "$OWNER ALL=($W) NOPASSWD: ALL" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" "/etc/sudoers.d/aicorp-worker-$W"
rm -f "$TMP"

echo "== 4. self-test (expected result in brackets)"
t() { # name, expect(ok|deny), command...
  local name="$1" exp="$2"; shift 2
  if sudo -u "$W" -H bash -c "$*" >/dev/null 2>&1; then got=ok; else got=deny; fi
  [ "$got" = "$exp" ] && echo "PASS  $name" || echo "FAIL  $name (got $got, expected $exp)"
}
t "worker cannot read secrets.env      [deny]" deny "cat $ROOT/config/secrets.env"
t "worker cannot list project root     [deny]" deny "ls $ROOT"
t "worker cannot read database dir     [deny]" deny "ls $ROOT/database"
t "worker cannot read your home        [deny]" deny "ls $OWNER_HOME"
t "worker CAN write its workspace      [ok]  " ok   "touch $ROOT/workspaces/.probe && rm $ROOT/workspaces/.probe"
t "worker cannot write outside         [deny]" deny "touch $ROOT/config/.probe"
echo "done."

#!/usr/bin/env python3
"""Checks that must pass before this project is published (and that CI repeats on every pull request).

    python scripts/public_gates.py --check-only        # CI: everything that needs no private input
    python scripts/public_gates.py --full              # before a release: also secret values, local deny-list, the test suite

Gates
  1. secrets      token patterns in any file (a line marked `gate: allow` is a deliberate fake used by a test); with --full also every value found in config/secrets.env
  2. identity     absolute paths with a user name, e-mail addresses, and (with --full) the words in config/gate-denylist.txt
                  plus the local user name, host name and git identity
  3. tree         no private/internal folders or runtime data in the tree (docs folders of the private repo, databases, profiles, ...)
  4. licence      LICENSE is the exact Apache-2.0 text, NOTICE and the community files exist
  5. platforms    README has the platform status table and it matches docs/platforms.json (nothing is called "tested" unless it is)
  6. links        relative links in the Markdown files point to files that exist
  7. tests        (--full) the test suite passes

The script prints every finding with file and line (never the secret itself) and exits 1 if there is any.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APACHE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
REQUIRED_FILES = ("LICENSE", "NOTICE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "docs/THIRD_PARTY.md", "docs/platforms.json")
FORBIDDEN_TOP = {"docFactory", "Prompts", "research", "reasearch", ".claude", ".venv", "runtime", "backups", ".trash", "database", ".git"}
FORBIDDEN_NAMES = {"CLAUDE.md", "secrets.env", ".env", "gate-denylist.txt"}
FORBIDDEN_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".pem", ".key", ".p12", ".pfx")
SELF_TEST_FILES = ("scripts/public_gates.py", "tests/test_public_gates.py")      # hold the patterns and deliberate fake plants
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".trash"}
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".gz", ".zip", ".pdf", ".sqlite", ".woff", ".woff2")
PLATFORM_ROWS = ("Linux", "Windows + WSL2", "macOS", "Windows (native)")
PLATFORM_WORDS = ("tested", "untested", "unsupported")

TOKEN_PATTERNS = [
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("Anthropic-style key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password assignment", re.compile(r"(?i)\b(app[_ -]?password|passwd|password)\s*[:=]\s*[\"']?([^\s\"'<>{}$()\[\]]{10,})(?=[\s\"',;]|$)")),
]
PLACEHOLDER_HINTS = ("dummy", "fake", "example", "test", "invalid", "placeholder", "xxxx", "your", "changeme", "redacted", "abcd-efgh", "hunter2", "sample", "secret-value", "secrets.env", "not-a-real")
EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
EMAIL_OK_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com", "anthropic.com", "github.com", "localhost")
EMAIL_OK_LOCALPARTS = ("noreply", "no-reply", "git")
ABS_PATH = [
    re.compile(r"(?<![\w.~-])/(?:home|Users)/([A-Za-z0-9._-]+)"),
    re.compile(r"(?<![\w.])/(?:var/www|mnt/[a-z])/[A-Za-z0-9._/-]+"),
    re.compile(r"\b[A-Za-z]:\\+Users\\+([A-Za-z0-9._-]+)"),
    re.compile(r"(?<![\w.])/root/[A-Za-z0-9._-]+"),
]
PATH_USER_OK = {"aicorp-worker", "user", "username", "you", "yourname", "youruser", "me", "alice", "bob", "example", "runner", "<user>", "$user", "$owner", "$home", "worker", "owner", "x", "name", "someone", "your-user"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass
class Finding:
    gate: str
    where: str
    what: str

    def __str__(self) -> str:
        return f"[{self.gate}] {self.where}: {self.what}"


# ---------------------------------------------------------------- files

def project_files(root: Path) -> list[Path]:
    """Files that would be published: ``git ls-files`` inside a repository, otherwise a walk that skips caches and virtual environments."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True, timeout=60).stdout
        rels = [p for p in out.decode("utf-8", "replace").split("\0") if p]
        if rels:
            return [root / p for p in rels if (root / p).is_file()]
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found += [Path(dirpath) / f for f in filenames]
    return found


def text_of(path: Path) -> str | None:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def is_placeholder(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in PLACEHOLDER_HINTS)


# ---------------------------------------------------------------- gates

def gate_secrets(root: Path, files: list[Path], secrets_file: Path | None) -> list[Finding]:
    out: list[Finding] = []
    values: list[str] = []
    if secrets_file and secrets_file.is_file():
        for line in secrets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                v = line.partition("=")[2].strip().strip("\"'")
                if len(v) >= 6:
                    values.append(v)
                    values.append(re.sub(r"\s", "", v))
    values = sorted({v for v in values if len(v) >= 6})
    for path in files:
        text = text_of(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SELF_TEST_FILES:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "gate: allow" in line:                              # a deliberately fake credential used by a test
                continue
            for value in values:
                if value in line:
                    out.append(Finding("secrets", f"{rel}:{lineno}", "contains a value from config/secrets.env"))
            for name, pattern in TOKEN_PATTERNS:
                for m in pattern.finditer(line):
                    hit = m.group(0)
                    if is_placeholder(hit) or is_placeholder(line) and name == "password assignment":
                        continue
                    out.append(Finding("secrets", f"{rel}:{lineno}", f"looks like a {name}"))
    return out


def gate_identity(root: Path, files: list[Path], deny_words: list[str]) -> list[Finding]:
    out: list[Finding] = []
    deny = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", re.I) for w in deny_words if len(w) >= 3]
    for path in files:
        text = text_of(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SELF_TEST_FILES:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in ABS_PATH:
                for m in pattern.finditer(line):
                    user = (m.group(1) if m.groups() else "").lower()
                    if user in PATH_USER_OK:
                        continue
                    out.append(Finding("identity", f"{rel}:{lineno}", "absolute path that names a user or a machine location"))
            for m in EMAIL.finditer(line):
                local, _, domain = m.group(0).lower().partition("@")
                if domain.endswith(EMAIL_OK_DOMAINS) or local in EMAIL_OK_LOCALPARTS:
                    continue
                out.append(Finding("identity", f"{rel}:{lineno}", "e-mail address"))
            for pattern in deny:
                if "github.com/" in line or line.lstrip().startswith(("Copyright", "authors =")):
                    break                                          # a repository URL and the copyright/author line name the (public) owner on purpose
                if pattern.search(line):
                    out.append(Finding("identity", f"{rel}:{lineno}", "contains a word from the local deny-list (user, host or private name)"))
    return out


def gate_tree(root: Path, files: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    for path in files:
        rel = path.relative_to(root)
        parts = rel.parts
        if parts[0] in FORBIDDEN_TOP:
            out.append(Finding("tree", rel.as_posix(), "internal or runtime folder must not be published"))
        elif rel.name in FORBIDDEN_NAMES or rel.name.lower().endswith(FORBIDDEN_SUFFIXES):
            out.append(Finding("tree", rel.as_posix(), "private or runtime file must not be published"))
        elif parts[0] == "workspaces" and rel.name != ".gitkeep":
            out.append(Finding("tree", rel.as_posix(), "workspace content must not be published"))
        elif parts[:2] == ("memory", "run-logs"):
            out.append(Finding("tree", rel.as_posix(), "run logs must not be published"))
    return out


def gate_licence(root: Path) -> list[Finding]:
    out: list[Finding] = []
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            out.append(Finding("licence", name, "required file is missing"))
    lic = root / "LICENSE"
    if lic.is_file() and hashlib.sha256(lic.read_bytes()).hexdigest() != APACHE_SHA256:
        out.append(Finding("licence", "LICENSE", "is not the unmodified Apache-2.0 text"))
    return out


def gate_platforms(root: Path) -> list[Finding]:
    out: list[Finding] = []
    readme, data = root / "README.md", root / "docs" / "platforms.json"
    if not readme.is_file() or not data.is_file():
        return [Finding("platforms", "README.md", "README or docs/platforms.json is missing")]
    try:
        status = json.loads(data.read_text(encoding="utf-8"))
    except ValueError:
        return [Finding("platforms", "docs/platforms.json", "is not valid JSON")]
    text = readme.read_text(encoding="utf-8")
    if not re.search(r"(?im)^#+\s*platform status", text):
        out.append(Finding("platforms", "README.md", "has no 'Platform status' section"))
    for row in PLATFORM_ROWS:
        value = status.get(row)
        if value not in PLATFORM_WORDS:
            out.append(Finding("platforms", "docs/platforms.json", f"'{row}' must be one of {', '.join(PLATFORM_WORDS)}"))
            continue
        line = next((l for l in text.splitlines() if l.lstrip().startswith("|") and row in l), None)
        if line is None:
            out.append(Finding("platforms", "README.md", f"the table has no row for '{row}'"))
        else:
            words = [w for w in PLATFORM_WORDS if re.search(rf"(?<![A-Za-z]){w}(?![A-Za-z])", line.lower())]
            if value not in words or ("tested" in words and value != "tested" and "untested" not in words):
                out.append(Finding("platforms", "README.md", f"the row for '{row}' does not say '{value}' (docs/platforms.json is the source of truth)"))
    return out


def gate_links(root: Path, files: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for m in LINK.finditer(text):
            target = m.group(1).split("#")[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("/"):
                continue
            if not (path.parent / target).resolve().exists():
                out.append(Finding("links", path.relative_to(root).as_posix(), f"link to '{target}' does not resolve"))
    return out


def gate_tests(root: Path) -> list[Finding]:
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"], cwd=root, capture_output=True, text=True)
    if done.returncode != 0:
        tail = "\n".join(done.stdout.strip().splitlines()[-6:])
        return [Finding("tests", "pytest", f"the test suite failed:\n{tail}")]
    return []


# ---------------------------------------------------------------- driver

def local_deny_words(root: Path, denylist: Path | None) -> list[str]:
    words: list[str] = []
    if denylist and denylist.is_file():
        words += [l.strip() for l in denylist.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    for getter in (getpass.getuser, socket.gethostname):
        try:
            words.append(getter())
        except Exception:
            pass
    for key in ("user.name", "user.email"):
        try:
            value = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=10).stdout.strip()
            if value:
                words += [value, value.split("@")[0]] if key == "user.email" else [value]
        except (OSError, subprocess.SubprocessError):
            pass
    generic = {"root", "user", "admin", "runner", "localhost", "ubuntu", "pi", "me"}
    return sorted({w for w in words if len(w) >= 4 and w.lower() not in generic})


def run(root: Path, *, secrets_file: Path | None = None, denylist: Path | None = None, full: bool = False, run_tests: bool = False) -> list[Finding]:
    root = root.resolve()
    files = project_files(root)
    findings: list[Finding] = []
    findings += gate_secrets(root, files, secrets_file if full else None)
    findings += gate_identity(root, files, local_deny_words(root, denylist) if full else [])
    findings += gate_tree(root, files)
    findings += gate_licence(root)
    findings += gate_platforms(root)
    findings += gate_links(root, files)
    if run_tests:
        findings += gate_tests(root)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public-release gates")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="CI mode: no private inputs, no tests")
    mode.add_argument("--full", action="store_true", help="also check secret values and the local deny-list, and run the tests")
    parser.add_argument("--secrets", type=Path, help="secrets.env whose values must not appear anywhere (default: <root>/config/secrets.env)")
    parser.add_argument("--denylist", type=Path, help="file with private words, one per line (default: <root>/config/gate-denylist.txt)")
    parser.add_argument("--skip-tests", action="store_true", help="with --full: do not run pytest (never for a real release)")
    args = parser.parse_args(argv)
    full = args.full
    root = args.root.resolve()
    findings = run(root, secrets_file=args.secrets or root / "config" / "secrets.env", denylist=args.denylist or root / "config" / "gate-denylist.txt",
                   full=full, run_tests=full and not args.skip_tests)
    for f in findings:
        print(f)
    gates = sorted({f.gate for f in findings})
    print(f"\n{'FAILED' if findings else 'PASSED'}: {len(findings)} finding(s)" + (f" in: {', '.join(gates)}" if gates else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

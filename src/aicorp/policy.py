"""Tool-call policy for agents. Standard library only.

This single file is used in two places: imported by the engine (for tests and for building the
policy) and copied next to the Claude Code hook script, which runs as the low-privilege worker
user and must not depend on the rest of the package.

Default is DENY. A call is allowed only if a rule below allows it. The policy is enforced by the
hook before a tool runs; it does not depend on the model behaving.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import shlex
import socket
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

SENTINEL = "BLOCKED by AICorp policy"

READ_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# Files and folders an agent may never touch, wherever they are.
PROTECTED_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".git", ".claude", ".config", ".trash",
                  "browser-profiles"}
PROTECTED_NAMES = {"secrets.env", ".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json",
                   "claude.md", "known_hosts", "authorized_keys", "company.sqlite"}
PROTECTED_PATTERNS = (".env.*", "secrets.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem", "*.key",
                      "*.p12", "*.pfx", "*.kdbx", "*.sqlite", "*.sqlite-*")

DEFAULT_COMMANDS = ("ls", "cat", "head", "tail", "wc", "grep", "sort", "cut", "tr", "echo", "printf", "mkdir",
                    "touch", "cp", "mv", "pwd", "diff", "basename", "dirname", "stat", "tree", "sha256sum",
                    "md5sum", "date", "true", "find", "git")
PRINT_ONLY = ("echo", "printf", "true", "date", "pwd")           # arguments are text, not paths
WRITERS = ("mkdir", "touch", "cp", "mv")                         # every path argument must be writable
FIND_FORBIDDEN = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls")
GIT_ALLOWED = ("status", "log", "diff", "show", "ls-files", "rev-parse")
GIT_FORBIDDEN_FLAGS = ("-c", "-C", "--git-dir", "--work-tree", "--exec-path", "--output", "-o", "--ext-diff",
                       "--textconv", "--upload-pack", "--receive-pack")
CONTROL_OPS = (";", "&&", "||", "|")
REDIRECT_OPS = (">", ">>", "<")
MAX_WRITE_BYTES = 2_000_000
MAX_QUERY = 500


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str
    reason: str = ""

    def message(self) -> str:
        return f"{SENTINEL} [{self.rule}]: {self.reason}"


@dataclass
class Policy:
    workspace: str
    tier: int = 1
    read_roots: list[str] = field(default_factory=list)       # extra folders readable, never writable
    deny_roots: list[str] = field(default_factory=list)       # only used to give a clearer message
    web: bool = False
    web_domains: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=lambda: list(DEFAULT_COMMANDS))

    def __post_init__(self) -> None:
        self.workspace = os.path.realpath(self.workspace)
        self.read_roots = [os.path.realpath(p) for p in self.read_roots]
        self.deny_roots = [os.path.realpath(p) for p in self.deny_roots]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Policy":
        return cls(**json.loads(text))


def _deny(rule: str, reason: str) -> Decision:
    return Decision(False, rule, reason)


def _ok(rule: str = "ok") -> Decision:
    return Decision(True, rule)


# ---------------------------------------------------------------------------- paths

def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _protected_part(parts: list[str]) -> str | None:
    """First path component that is a protected folder or file name (checked at every level)."""
    for part in parts:
        low = part.lower()
        if low in PROTECTED_DIRS or low in PROTECTED_NAMES or any(fnmatch.fnmatch(low, pat) for pat in PROTECTED_PATTERNS):
            return part
    return None


def check_path(policy: Policy, raw: Any, *, write: bool, base: str | None = None) -> Decision:
    """May the agent read (or write) this path? Symlinks are resolved before the decision."""
    if not isinstance(raw, str) or not raw.strip():
        return _deny("path.invalid", "empty or invalid path")
    if "\x00" in raw:
        return _deny("path.invalid", "path contains a NUL byte")
    if raw.startswith("~"):
        return _deny("path.home", "home-directory paths are not allowed")
    candidate = raw if os.path.isabs(raw) else os.path.join(base or policy.workspace, raw)
    real = os.path.realpath(candidate)
    roots = [policy.workspace] if write else [policy.workspace, *policy.read_roots]
    for root in roots:
        if _within(real, root):
            relative = os.path.relpath(real, root)
            hit = _protected_part([] if relative == "." else relative.split(os.sep))
            if hit:
                return _deny("path.protected", f"'{hit}' is a protected file or folder")
            return _ok("path.ok")
    if any(_within(real, d) for d in policy.deny_roots):
        return _deny("path.protected_area", "that area holds company data or another worker's files")
    where = "writes are limited to your own workspace" if write else "access is limited to your own workspace"
    return _deny("path.outside_workspace", where)


# ---------------------------------------------------------------------------- shell commands

def _tokenize(command: str) -> list[str] | Decision:
    if any(ch in command for ch in ("\n", "\r", "\x00")):
        return _deny("bash.multiline", "multi-line commands are not allowed")
    for bad, why in (("$", "variable or command substitution"), ("`", "command substitution"),
                     ("\\", "backslash escapes")):
        if bad in command:
            return _deny("bash.expansion", f"{why} is not allowed")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as err:
        return _deny("bash.parse", f"could not parse the command ({err})")


def check_bash(policy: Policy, command: Any) -> Decision:
    if not isinstance(command, str) or not command.strip():
        return _deny("bash.empty", "empty command")
    tokens = _tokenize(command)
    if isinstance(tokens, Decision):
        return tokens
    segment: list[str] = []
    segments: list[list[str]] = []
    redirects: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in CONTROL_OPS:
            if not segment:
                return _deny("bash.syntax", "empty command segment")
            segments.append(segment)
            segment = []
        elif tok in REDIRECT_OPS:
            if i + 1 >= len(tokens) or tokens[i + 1] in CONTROL_OPS + REDIRECT_OPS:
                return _deny("bash.syntax", "redirection without a target")
            redirects.append((tok, tokens[i + 1]))
            i += 1
        elif set(tok) <= set("();<>|&") and tok:
            return _deny("bash.operator", f"operator '{tok}' is not allowed")
        else:
            segment.append(tok)
        i += 1
    if not segment:
        return _deny("bash.syntax", "command ends with an operator")
    segments.append(segment)

    for op, target in redirects:
        verdict = check_path(policy, target, write=(op != "<"))
        if not verdict.allow:
            return _deny("bash.redirect", f"redirection target: {verdict.reason}")

    allowed = set(policy.commands)
    for words in segments:
        name = words[0]
        if "/" in name or "=" in name:
            return _deny("bash.command_form", "call commands by plain name (no paths, no VAR=value prefixes)")
        if name not in allowed:
            return _deny("bash.command_not_allowed", f"'{name}' is not an allowed command")
        verdict = _check_command(policy, name, words[1:])
        if not verdict.allow:
            return verdict
    return _ok("bash.ok")


def _check_command(policy: Policy, name: str, args: list[str]) -> Decision:
    if name == "find" and any(a in FIND_FORBIDDEN or a.startswith("-fprint") for a in args):
        return _deny("bash.find_action", "find may only search, not run or delete anything")
    if name == "sort" and any(a in ("-o", "--output") or a.startswith("--output=") for a in args):
        return _deny("bash.flag", "sort -o is not allowed")
    if name == "date" and any(a in ("-s", "--set") or a.startswith("--set=") for a in args):
        return _deny("bash.flag", "setting the clock is not allowed")
    if name == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        if any(a in GIT_FORBIDDEN_FLAGS or a.startswith(("--git-dir=", "--work-tree=", "--output=")) for a in args):
            return _deny("bash.git_flag", "that git option is not allowed")
        if sub not in GIT_ALLOWED:
            return _deny("bash.git_write", f"git {sub or ''} is not allowed; pushing, resetting and cleaning "
                                           f"need the Founder's approval (tier 3)")
        if args and args[0] != sub:
            return _deny("bash.git_flag", "put the git subcommand first")
    if name in PRINT_ONLY:
        return _ok()
    writer = name in WRITERS
    for arg in args:
        value = arg
        if arg.startswith("-"):
            if "=" not in arg:
                continue
            value = arg.split("=", 1)[1]
            if not value:
                continue
        verdict = check_path(policy, value, write=writer)
        if not verdict.allow:
            return _deny("bash.path", f"argument '{arg[:60]}': {verdict.reason}")
    return _ok()


# ---------------------------------------------------------------------------- web

def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or getattr(ip, "is_site_local", False))


def check_url(policy: Policy, url: Any, resolver: Callable | None = None) -> Decision:
    if not policy.web:
        return _deny("web.not_allowed", "this employee has no web access")
    if not isinstance(url, str) or len(url) > 2000:
        return _deny("web.invalid", "invalid or oversized URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return _deny("web.invalid", "malformed URL")
    if parts.scheme not in ("http", "https"):
        return _deny("web.scheme", "only http and https are allowed")
    if "@" in parts.netloc:
        return _deny("web.userinfo", "URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return _deny("web.invalid", "no host in URL")
    if port not in (None, 80, 443):
        return _deny("web.port", "only the standard web ports are allowed")
    if len(parts.query) + len(parts.fragment) > MAX_QUERY:
        return _deny("web.long_query", "the URL carries too much data in its query (possible data leak)")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".corp")) \
            or "." not in host and ":" not in host:
        return _deny("web.local_host", "local and internal addresses are not allowed")
    literal = host
    try:                                     # forms like 2130706433, 0x7f.1, 127.1
        literal = socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError):
        pass
    try:
        ipaddress.ip_address(literal.strip("[]"))
        addresses = [literal.strip("[]")]
    except ValueError:
        try:
            infos = (resolver or socket.getaddrinfo)(host, None)
            addresses = sorted({info[4][0] for info in infos})
        except OSError:
            return _deny("web.unresolved", "could not resolve the host")
    if not addresses or not all(_is_public(a) for a in addresses):
        return _deny("web.private_address", "that address is not on the public internet")
    if policy.web_domains and not any(host == d or host.endswith("." + d) for d in policy.web_domains):
        return _deny("web.domain", "that site is not on this employee's allowed list")
    return _ok("web.ok")


# ---------------------------------------------------------------------------- entry point

def decide(policy: Policy, tool_name: str, tool_input: Any, resolver: Callable | None = None) -> Decision:
    """Decide one tool call. Anything not explicitly allowed is denied."""
    if not isinstance(tool_input, dict):
        return _deny("input.invalid", "tool input is not an object")
    if tool_name == "TodoWrite":
        return _ok("todo")
    if tool_name == "Read":
        return check_path(policy, tool_input.get("file_path"), write=False)
    if tool_name == "Grep":
        return check_path(policy, tool_input.get("path") or policy.workspace, write=False)
    if tool_name == "Glob":
        pattern = str(tool_input.get("pattern", ""))
        if pattern.startswith(("/", "~")) or ".." in pattern.replace("\\", "/").split("/"):
            return _deny("glob.escape", "glob patterns must stay inside the workspace")
        return check_path(policy, tool_input.get("path") or policy.workspace, write=False)
    if tool_name in WRITE_TOOLS:
        if policy.tier < 1:
            return _deny("tier.read_only", "this task is read-only (tier 0)")
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if len(str(tool_input.get("content", ""))) > MAX_WRITE_BYTES:
            return _deny("write.too_large", "refusing to write a very large file")
        return check_path(policy, target, write=True)
    if tool_name == "Bash":
        if policy.tier < 2:
            return _deny("tier.no_shell", "running commands needs a tier 2 task")
        return check_bash(policy, tool_input.get("command"))
    if tool_name == "WebFetch":
        return check_url(policy, tool_input.get("url"), resolver)
    if tool_name == "WebSearch":
        if not policy.web:
            return _deny("web.not_allowed", "this employee has no web access")
        if len(str(tool_input.get("query", ""))) > 300:
            return _deny("web.long_query", "search query too long")
        return _ok("web.search")
    return _deny("tool.not_allowed", f"the tool '{tool_name}' is not available to agents")

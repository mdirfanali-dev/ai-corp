"""The tool policy is the real guardrail, so it gets the most thorough tests."""
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from aicorp import policy as pol

SRC = Path(pol.__file__).resolve().parent


@pytest.fixture
def tree(tmp_path):
    ws = tmp_path / "workspaces" / "employees" / "E-1"
    other = tmp_path / "workspaces" / "employees" / "E-2"
    for d in (ws / "sub", other, tmp_path / "config", tmp_path / "skills" / "seo", tmp_path / "outside",
              tmp_path / "database", ws / ".git", ws / ".claude"):
        d.mkdir(parents=True)
    (ws / "notes.txt").write_text("hello")
    (ws / "sub" / "a.txt").write_text("a")
    (other / "private.txt").write_text("other worker's file")
    (tmp_path / "config" / "secrets.env").write_text("TOKEN=abc123456")
    (tmp_path / "outside" / "x.txt").write_text("x")
    (tmp_path / "skills" / "seo" / "SKILL.md").write_text("# seo")
    os.symlink(tmp_path / "outside", ws / "link_out")
    os.symlink(tmp_path / "config" / "secrets.env", ws / "link_secret")
    return tmp_path


def make(tree, tier=1, **kw):
    return pol.Policy(workspace=str(tree / "workspaces/employees/E-1"), tier=tier,
                      read_roots=[str(tree / "skills")],
                      deny_roots=[str(tree / "config"), str(tree / "database"), str(tree / "workspaces")], **kw)


def allowed(tree, tool, tool_input, tier=1, **kw):
    return pol.decide(make(tree, tier, **kw), tool, tool_input, resolver=kw.get("resolver"))


# ------------------------------------------------------------------ reading

def test_reading_own_files_and_skills_is_allowed(tree):
    ws = tree / "workspaces/employees/E-1"
    for path in (str(ws / "notes.txt"), "notes.txt", "sub/a.txt", str(tree / "skills/seo/SKILL.md")):
        assert allowed(tree, "Read", {"file_path": path}).allow, path


@pytest.mark.parametrize("path, rule", [
    ("{t}/config/secrets.env", "path.protected_area"),
    ("../../../config/secrets.env", "path.protected_area"),
    ("link_secret", "path.protected_area"),                    # symlink into company data
    ("link_out/x.txt", "path.outside_workspace"),               # symlink out of the workspace
    ("{t}/workspaces/employees/E-2/private.txt", "path.protected_area"),   # another worker
    ("~/.ssh/id_rsa", "path.home"),
    ("/etc/passwd", "path.outside_workspace"),
    ("/proc/self/environ", "path.outside_workspace"),
    ("{t}/database/company.sqlite", "path.protected_area"),
    (".git/config", "path.protected"),
    (".claude/settings.json", "path.protected"),
    ("secrets.env", "path.protected"),
    (".env", "path.protected"),
    (".env.local", "path.protected"),
    ("server.pem", "path.protected"),
    ("id_rsa", "path.protected"),
    ("data.sqlite", "path.protected"),
    ("", "path.invalid"), ("a\x00b", "path.invalid"),
])
def test_forbidden_reads(tree, path, rule):
    decision = allowed(tree, "Read", {"file_path": path.replace("{t}", str(tree))})
    assert not decision.allow and decision.rule == rule, decision


def test_read_roots_are_never_writable(tree):
    decision = allowed(tree, "Write", {"file_path": str(tree / "skills/seo/SKILL.md"), "content": "x"})
    assert not decision.allow and decision.rule in ("path.outside_workspace", "path.protected_area")


def test_malformed_tool_input_is_denied(tree):
    for bad in (None, "string", 5, [], {"file_path": 5}, {"file_path": None}, {}):
        assert not pol.decide(make(tree), "Read", bad).allow


def test_grep_and_glob(tree):
    assert allowed(tree, "Grep", {"pattern": "x"}).allow
    assert allowed(tree, "Grep", {"pattern": "x", "path": "sub"}).allow
    assert not allowed(tree, "Grep", {"pattern": "x", "path": str(tree / "config")}).allow
    assert not allowed(tree, "Grep", {"pattern": "x", "path": "/"}).allow
    assert allowed(tree, "Glob", {"pattern": "**/*.txt"}).allow
    for pattern in ("/etc/*", "~/*", "../*", "sub/../../*"):
        assert not allowed(tree, "Glob", {"pattern": pattern}).allow, pattern
    assert not allowed(tree, "Glob", {"pattern": "*", "path": str(tree / "outside")}).allow


# ------------------------------------------------------------------ writing

def test_writing_needs_tier_one_and_the_workspace(tree):
    ws = tree / "workspaces/employees/E-1"
    assert not allowed(tree, "Write", {"file_path": "out.md", "content": "x"}, tier=0).allow
    for tool in ("Write", "Edit", "MultiEdit"):
        assert allowed(tree, tool, {"file_path": str(ws / "out.md"), "content": "x"}).allow
    assert allowed(tree, "NotebookEdit", {"notebook_path": "nb.ipynb"}).allow
    for path in ("../x.md", str(tree / "outside/x.md"), str(tree / "config/x"), "link_out/new.txt",
                 "link_secret", "/tmp/x", ".claude/settings.json", ".git/hooks/pre-commit", "CLAUDE.md",
                 ".env", "~/x"):
        assert not allowed(tree, "Write", {"file_path": path, "content": "x"}).allow, path
    assert not allowed(tree, "Write", {"file_path": "big.txt", "content": "x" * 2_100_000}).allow


# ------------------------------------------------------------------ shell

OK_COMMANDS = [
    "ls", "ls -la", "ls sub", "cat notes.txt", "head -n 5 notes.txt", "grep -rn hello .", "wc -l notes.txt",
    "cat notes.txt | head -5 | wc -l", "mkdir -p out && touch out/x.txt", "cp notes.txt out.txt",
    "mv notes.txt renamed.txt", "echo done > result.txt", "echo more >> result.txt", "find . -name '*.txt'",
    "git status", "git log --oneline", "git diff", "diff notes.txt sub/a.txt", "pwd", "date +%s",
    "sort notes.txt | cut -d, -f1 | tr a-z A-Z", "ls ; pwd", "stat notes.txt", "sha256sum notes.txt",
    "echo 'a;b' > semi.txt",
]


@pytest.mark.parametrize("command", OK_COMMANDS)
def test_allowed_shell_commands(tree, command):
    decision = allowed(tree, "Bash", {"command": command}, tier=2)
    assert decision.allow, (command, decision)


BAD_COMMANDS = [
    "rm notes.txt", "rm -rf /", "rm -rf .", "rmdir sub", "unlink notes.txt", "shred notes.txt", "ls; rm x",
    "ls && rm x", "ls || rm x", "ls | rm x", "echo $(rm x)", "echo `rm x`", "echo ${IFS}", "ls $HOME",
    "cat /etc/passwd", "cat ../config/secrets.env", "cat ~/.ssh/id_rsa", "cat link_secret",
    "echo x > /etc/x", "echo x > ../outside/x", "cat < /etc/passwd", "cat notes.txt > ../config/x",
    "echo x > .env", "echo x > .claude/settings.json",
    "python3 -c 'import os; os.remove(\"x\")'", "python3 x.py", "bash -c 'rm x'", "sh x.sh", "node x.js",
    "curl http://example.com", "wget http://example.com", "ssh host", "nc -e /bin/sh h 1", "scp a b",
    "sudo ls", "su -", "chmod 777 notes.txt", "chown root notes.txt", "dd if=/dev/zero of=x", "kill -9 1",
    "mv notes.txt /tmp/x", "cp notes.txt ../x", "cp /etc/passwd .", "ln -s /etc/passwd x", "ln x y",
    "find . -delete", "find . -exec rm {} ;", "find . -execdir rm {} ;", "find / -name x", "find . -fprint out",
    "git push", "git push --force", "git reset --hard", "git clean -fd", "git checkout .", "git commit -m x",
    "git config --global user.name x", "git -c core.pager=x status", "git -C / status", "git remote add x y",
    "git diff --output=out.txt", "git", "sort -o out.txt notes.txt", "env", "printenv", "cat a & rm b",
    "ls\nrm x", "FOO=1 ls", "/bin/ls", "./script.sh", "tee out.txt", "awk 'BEGIN{system(\"x\")}'", "sed -i s/a/b/ x",
    "xargs rm", "(ls)", "ls > ", "ls >", "| ls", "ls |", "&& ls", "ls &&", "echo hi >& /dev/tcp/x/1",
    "cat <<EOF", "cat <(ls)", "ls ~", "mkdir /tmp/x", "touch ../x", "date -s '2020-01-01'", "cat \\x", "",
    "   ", "alias x=y", "export A=1", "source x", ". x", "exec ls", "eval ls", "nohup ls",
]


@pytest.mark.parametrize("command", BAD_COMMANDS)
def test_forbidden_shell_commands(tree, command):
    decision = allowed(tree, "Bash", {"command": command}, tier=2)
    assert not decision.allow, command


def test_shell_needs_tier_two(tree):
    for tier in (0, 1):
        d = allowed(tree, "Bash", {"command": "ls"}, tier=tier)
        assert not d.allow and d.rule == "tier.no_shell"
    assert not allowed(tree, "Bash", {"command": 5}, tier=2).allow


def test_command_allowlist_can_be_narrowed(tree):
    narrow = pol.Policy(workspace=str(tree / "workspaces/employees/E-1"), tier=2, commands=["ls"])
    assert pol.decide(narrow, "Bash", {"command": "ls"}).allow
    assert not pol.decide(narrow, "Bash", {"command": "cat notes.txt"}).allow


# ------------------------------------------------------------------ web

def public(host, *a, **k):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def private(host, *a, **k):
    return [(2, 1, 6, "", ("10.0.0.7", 0))]


def web_policy(tree, **kw):
    return make(tree, tier=1, web=True, **kw)


def test_web_fetch_rules(tree):
    pw = web_policy(tree)
    assert pol.decide(pw, "WebFetch", {"url": "https://example.com/page?a=1"}, resolver=public).allow
    assert pol.decide(pw, "WebSearch", {"query": "market size of micro saas"}).allow
    no_web = make(tree)
    assert not pol.decide(no_web, "WebFetch", {"url": "https://example.com"}, resolver=public).allow
    assert not pol.decide(no_web, "WebSearch", {"query": "x"}).allow
    for url in ("http://localhost:8765/", "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
                "http://0x7f000001/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/", "http://0.0.0.0/",
                "https://user:pw@example.com/", "ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)",
                "https://example.com:8765/", "http://intranet/", "http://printer.local/", "http://db.internal/",
                "https://example.com/?" + "d" * 600, "not a url", "", "http://"):
        assert not pol.decide(pw, "WebFetch", {"url": url}, resolver=public).allow, url
    assert not pol.decide(pw, "WebFetch", {"url": "https://rebind.example.com/"}, resolver=private).allow

    def broken(host, *a, **k):
        raise OSError("no dns")
    assert not pol.decide(pw, "WebFetch", {"url": "https://example.com"}, resolver=broken).allow
    assert not pol.decide(pw, "WebSearch", {"query": "q" * 400}).allow


def test_web_domain_allowlist(tree):
    pw = web_policy(tree, web_domains=["example.com"])
    assert pol.decide(pw, "WebFetch", {"url": "https://docs.example.com/a"}, resolver=public).allow
    assert not pol.decide(pw, "WebFetch", {"url": "https://evil-example.com/"}, resolver=public).allow
    assert not pol.decide(pw, "WebFetch", {"url": "https://example.org/"}, resolver=public).allow


# ------------------------------------------------------------------ default deny

def test_unknown_tools_are_denied_by_default(tree):
    for tool in ("Task", "Agent", "KillShell", "BashOutput", "mcp__github__push", "SlashCommand", "ExitPlanMode", ""):
        assert not allowed(tree, tool, {}, tier=2).allow, tool
    assert allowed(tree, "TodoWrite", {"todos": []}).allow


# ------------------------------------------------------------------ invariants

def test_random_paths_never_escape_the_workspace(tree):
    """Property check: whatever the decision says 'allow' for must really lie inside the allowed roots."""
    policy = make(tree)
    ws = policy.workspace
    parts = ["..", ".", "config", "secrets.env", "sub", "link_out", "link_secret", "~", "", "x.txt", ".git", "E-2",
             "workspaces", "outside", "skills", "notes.txt", "..", "..", str(tree)]
    rng = random.Random(1234)
    allowed_count = 0
    for _ in range(4000):
        raw = rng.choice(["", "/"]) + "/".join(rng.choice(parts) for _ in range(rng.randint(1, 6)))
        for write in (False, True):
            decision = pol.check_path(policy, raw, write=write)
            if not decision.allow:
                continue
            allowed_count += 1
            real = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(ws, raw))
            roots = [ws] if write else [ws, *policy.read_roots]
            assert any(real == r or real.startswith(r + os.sep) for r in roots), (raw, real)
            assert "secrets.env" not in real and ".git" not in real.split(os.sep)
    assert allowed_count > 50           # the generator does hit allowed paths, so the check is meaningful


# ------------------------------------------------------------------ the hook program

def run_hook(tree, call, policy_text=None, stdin=None):
    work = tree / "hook"
    work.mkdir(exist_ok=True)
    (work / "aicorp_policy.py").write_text((SRC / "policy.py").read_text())
    (work / "guard_hook.py").write_text((SRC / "guard_hook.py").read_text())
    policy_file = work / "policy.json"
    policy_file.write_text(make(tree, tier=2).to_json() if policy_text is None else policy_text)
    payload = json.dumps(call) if stdin is None else stdin
    return subprocess.run([sys.executable, str(work / "guard_hook.py"), str(policy_file)], input=payload,
                          capture_output=True, text=True, cwd="/")


def test_hook_allows_and_blocks_with_exit_codes(tree):
    ok = run_hook(tree, {"tool_name": "Read", "tool_input": {"file_path": "notes.txt"}})
    assert ok.returncode == 0 and ok.stderr == ""
    bad = run_hook(tree, {"tool_name": "Read", "tool_input": {"file_path": str(tree / "config/secrets.env")}})
    assert bad.returncode == 2 and bad.stderr.startswith(pol.SENTINEL) and "[path.protected_area]" in bad.stderr
    rm = run_hook(tree, {"tool_name": "Bash", "tool_input": {"command": "rm -rf ."}})
    assert rm.returncode == 2 and "bash.command_not_allowed" in rm.stderr


def test_hook_fails_closed(tree):
    cases = [run_hook(tree, {}, stdin="not json"), run_hook(tree, {}, policy_text="{broken"),
             run_hook(tree, {}, stdin=""), run_hook(tree, {"tool_name": "Read"}),
             run_hook(tree, {"tool_input": {"file_path": "notes.txt"}})]
    for result in cases:
        assert result.returncode == 2 and pol.SENTINEL in result.stderr, result.stderr
    missing = subprocess.run([sys.executable, str(tree / "hook" / "guard_hook.py"), str(tree / "nope.json")],
                             input="{}", capture_output=True, text=True)
    assert missing.returncode == 2


def test_policy_json_round_trip(tree):
    original = make(tree, tier=2, web=True, web_domains=["example.com"])
    clone = pol.Policy.from_json(original.to_json())
    assert clone == original

"""The release gates must block a planted secret, path, e-mail, internal folder, a changed licence and a dishonest platform table."""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_gates", ROOT / "scripts" / "public_gates.py")
gates = importlib.util.module_from_spec(spec)
sys.modules["public_gates"] = gates
spec.loader.exec_module(gates)


@pytest.fixture
def tree(tmp_path):
    """A minimal valid public tree (a sub-folder, so private files can sit beside it): the real licence, docs and community files, plus a little code."""
    tmp_path = tmp_path / "public"
    tmp_path.mkdir()
    for name in ("LICENSE", "NOTICE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "docs", tmp_path / "docs")
    (tmp_path / "desktop").mkdir()
    shutil.copy(ROOT / "desktop" / "README.md", tmp_path / "desktop" / "README.md")      # docs link to it
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n")
    return tmp_path


def run(tree, **kw):
    return gates.run(tree, **kw)


def kinds(findings):
    return {f.gate for f in findings}


def test_the_real_project_tree_passes_the_gates_without_private_inputs():
    findings = gates.run(ROOT)
    assert not findings, "\n".join(str(f) for f in findings)


def test_a_clean_tree_passes(tree):
    assert run(tree) == []


@pytest.mark.parametrize("secret", ["123456789:AAH9xk3jH2bV7s1LmQpW8dRtYz0aBcDeFgH", "sk-ant-api03-Zk3Jh2bV7s1LmQpW8dRtYz0aBcDeFgHiJ",
                                    "ghp_" + "a1B2" * 10, "AKIA" + "ABCDEFGHIJKLMNOP", "-----BEGIN OPENSSH PRIVATE KEY-----", "password = Tr0ub4dour&3xyz"])
def test_a_planted_secret_pattern_is_blocked(tree, secret):
    (tree / "src" / "config.py").write_text(f'KEY = "{secret}"\n')
    found = run(tree)
    assert "secrets" in kinds(found) and any("config.py:1" in f.where for f in found)
    assert all(secret not in str(f) for f in found)                       # the finding never repeats the secret


def test_a_real_secret_value_from_secrets_env_is_blocked_even_without_a_known_pattern(tree, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GMAIL_APP_PASSWORD=qzxwvutsrqponmlk\nTELEGRAM_CHAT_ID=987654321\n")
    (tree / "docs" / "notes.md").write_text("my code is qzxwvutsrqponmlk\n")
    found = run(tree, secrets_file=secrets, full=True)
    assert any(f.gate == "secrets" and "notes.md:1" in f.where for f in found)
    (tree / "docs" / "notes.md").write_text("my code is qzxwvutsrqponml\n")        # a different value is fine
    assert not any(f.gate == "secrets" for f in run(tree, secrets_file=secrets, full=True))
    assert not any(f.gate == "secrets" for f in run(tree, secrets_file=secrets, full=False))   # the value check is a --full feature


@pytest.mark.parametrize("line", ["path = '/home/dave42/projects/app'", "p = '/Users/maria/Documents'", r"p = 'C:\Users\sam\Desktop'", "p = '/var/www/html/site'", "cd /mnt/c/Stuff/x"])
def test_a_planted_absolute_path_is_blocked(tree, line):
    (tree / "src" / "paths.py").write_text(line + "\n")
    assert "identity" in kinds(run(tree))


@pytest.mark.parametrize("line", ["contact bob@corp.io", "From: Jane Doe <jane.doe@gmail.com>"])
def test_a_planted_email_is_blocked(tree, line):
    (tree / "docs" / "contact.md").write_text(line + "\n")
    assert "identity" in kinds(run(tree))


@pytest.mark.parametrize("line", ["write to boss@example.com", "Co-Authored-By: X <noreply@anthropic.com>", "git@github.com:me/repo.git", "/home/aicorp-worker/.local/bin/claude", "ls /home/you/data"])
def test_documentation_style_values_are_not_false_alarms(tree, line):
    (tree / "docs" / "ok.md").write_text(line + "\n")
    assert run(tree) == []


def test_the_local_deny_list_catches_names_that_must_never_appear(tree, tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("# private words\nzanzibar-lab\nQuentinPrivate\n")
    (tree / "docs" / "oops.md").write_text("Built at the Zanzibar-Lab by QuentinPrivate.\n")
    found = run(tree, denylist=deny, full=True)
    assert len([f for f in found if f.gate == "identity" and f.where == "docs/oops.md:1"]) == 2
    assert not any(f.gate == "identity" for f in run(tree, denylist=deny, full=False))


@pytest.mark.parametrize("rel", ["docFactory/Prompts/P001.md", "CLAUDE.md", ".claude/settings.json", "config/secrets.env", "database/company.sqlite", "runtime/guard/x.py",
                                 "workspaces/employees/E-1/notes.md", "backups/a.tar.gz", "memory/run-logs/t.log", "id_rsa.pem"])
def test_internal_and_runtime_files_are_blocked(tree, rel):
    path = tree / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    assert "tree" in kinds(run(tree))


def test_workspaces_keep_file_is_allowed(tree):
    (tree / "workspaces").mkdir()
    (tree / "workspaces" / ".gitkeep").write_text("")
    assert run(tree) == []


def test_licence_must_be_the_exact_apache_text_and_the_community_files_must_exist(tree):
    (tree / "LICENSE").write_text((tree / "LICENSE").read_text() + "\nExtra clause.\n")
    assert any(f.gate == "licence" and "not the unmodified Apache-2.0" in f.what for f in run(tree))
    shutil.copy(ROOT / "LICENSE", tree / "LICENSE")
    (tree / "SECURITY.md").unlink()
    (tree / "NOTICE").unlink()
    missing = {f.where for f in run(tree) if f.gate == "licence"}
    assert missing == {"SECURITY.md", "NOTICE"}


def test_the_platform_table_cannot_claim_more_than_the_status_file(tree):
    readme = (tree / "README.md").read_text()
    assert "| macOS | untested" in readme
    (tree / "README.md").write_text(readme.replace("| macOS | untested", "| macOS | tested"))
    assert any(f.gate == "platforms" and "macOS" in f.what for f in run(tree))
    (tree / "README.md").write_text(readme)
    status = json.loads((tree / "docs" / "platforms.json").read_text())
    status["Linux"] = "maybe"
    (tree / "docs" / "platforms.json").write_text(json.dumps(status))
    assert any(f.gate == "platforms" and "Linux" in f.what for f in run(tree))
    (tree / "README.md").write_text(readme.replace("## Platform status", "## Where it runs"))
    assert any("no 'Platform status' section" in f.what for f in run(tree))


def test_broken_relative_links_are_blocked(tree):
    (tree / "docs" / "guide.md").write_text("See [the thing](missing-page.md) and [web](https://example.org) and [anchor](#x).\n")
    found = [f for f in run(tree) if f.gate == "links"]
    assert len(found) == 1 and "missing-page.md" in found[0].what


def test_the_command_line_exits_non_zero_on_findings_and_prints_where(tree, capsys):
    (tree / "src" / "leak.py").write_text("k = 'AKIA" + "ABCDEFGHIJKLMNOP'\n")
    code = gates.main(["--root", str(tree), "--check-only"])
    out = capsys.readouterr().out
    assert code == 1 and "leak.py:1" in out and "FAILED" in out
    (tree / "src" / "leak.py").unlink()
    assert gates.main(["--root", str(tree), "--check-only"]) == 0


def test_the_ci_workflow_cannot_leak_secrets_to_fork_pull_requests():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pull_request_target" not in text and "secrets." not in text
    assert "contents: read" in text and "pull_request:" in text


def test_the_public_owner_name_is_allowed_on_copyright_and_url_lines_only(tree, tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("ownerhandle\n")
    (tree / "NOTICE").write_text("Copyright 2026 ownerhandle and the AICorp contributors\n")
    (tree / "docs" / "url.md").write_text("clone https://github.com/ownerhandle/ai-corp.git\n")
    assert not [f for f in run(tree, denylist=deny, full=True) if f.gate == "identity"]
    (tree / "docs" / "oops.md").write_text("written by ownerhandle at home\n")
    assert any(f.gate == "identity" and f.where == "docs/oops.md:1" for f in run(tree, denylist=deny, full=True))

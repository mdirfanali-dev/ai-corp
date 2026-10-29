"""The Windows release workflow: who can trigger it, what it may write, and what it never contains."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github" / "workflows" / "release-windows.yml").read_text(encoding="utf-8")
WF = yaml.safe_load(TEXT)
TRIGGERS = WF.get(True, WF.get("on"))                    # YAML reads a bare "on" as true


def test_it_runs_only_for_version_tags():
    assert set(TRIGGERS) == {"push"} and TRIGGERS["push"] == {"tags": ["v*"]}      # no pull requests, no branches, no manual start


def test_write_access_exists_only_in_the_release_job():
    assert WF["permissions"] == {"contents": "read"}
    assert "permissions" not in WF["jobs"]["build"]
    assert WF["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert WF["jobs"]["release"]["needs"] == "build"


def test_no_secrets_and_no_untrusted_text_inside_scripts():
    assert "secrets." not in TEXT                                               # until signing is set up
    for job in WF["jobs"].values():
        for step in job["steps"]:
            assert "github.ref_name" not in step.get("run", "") and "github.head_ref" not in step.get("run", "")   # via env only


def test_prerelease_rule_and_notes_exist():
    assert "--prerelease" in TEXT and "-beta" in TEXT and "-rc" in TEXT and "--verify-tag" in TEXT
    for name in ("release-notes-beta.md", "release-notes.md"):
        assert (ROOT / ".github" / name).read_text().strip()
    beta = (ROOT / ".github" / "release-notes-beta.md").read_text()
    assert "Not code-signed" in beta and "not tested" in beta.lower() and "SHA256" in beta


def test_builds_on_windows_with_pinned_actions():
    assert WF["jobs"]["build"]["runs-on"] == "windows-latest"
    uses = [s["uses"] for j in WF["jobs"].values() for s in j["steps"] if "uses" in s]
    assert uses and all("@v" in u for u in uses)


def test_the_workflow_builds_and_attaches_the_installer_and_checksums_for_both_files():
    assert "build-installer.ps1" in TEXT and "innosetup" in TEXT
    assert "dist/AICorp-Setup-*.exe" in TEXT and "dist/AICorp.exe" in TEXT and "Get-ChildItem dist -Filter *.exe" in TEXT


def test_the_installer_script_installs_per_user_and_never_touches_user_data():
    iss = (ROOT / "desktop" / "installer" / "aicorp.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss                              # no administrator rights
    assert "UninstallDisplayIcon" in iss and "{group}" in iss              # appears in Windows Apps and the Start Menu
    assert "MB_DEFBUTTON2" in iss and "never touched" in iss               # settings are removed only on an explicit Yes, data never
    assert "DelTree(SettingsDir" in iss and iss.count("DelTree(") == 1      # the only deletion is the small settings folder
    assert "runasadmin" not in iss.lower() and "PrivilegesRequired=admin" not in iss and "wsl --install" not in iss      # it never asks for admin rights or installs WSL
    assert "{userappdata}\\AICorp" in iss

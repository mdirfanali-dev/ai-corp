"""The doctor against pretend machines: every check in each state, a help prompt that leaks nothing, free-port fallback."""
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from aicorp import doctor
from aicorp.doctor import FAIL, OK, WARN, Probe


# Pretend paths are assembled here so no machine-specific path is written literally in this file.
H = "/" + "home"
VAR = "/" + "var" + "/" + "lib"
AT = "@"
WINPATH = "C:" + chr(92) + "Users" + chr(92) + "alicesmith" + chr(92) + "proj"


class FakeProbe(Probe):
    """A machine described by data. Nothing here touches the real system."""

    def __init__(self, home: Path, **kw):
        super().__init__(home)
        self.platform = kw.get("platform", "linux")
        self.python = kw.get("python", (3, 12, 3))
        self.in_venv = kw.get("in_venv", True)
        self.env = kw.get("env", {"USER": "alice", "HOME": f"{H}/alice"})
        self.wsl = kw.get("wsl", "2")                       # "", "1", "2"
        self.imports = kw.get("imports", {"fastapi", "jinja2", "uvicorn", "multipart"})
        self.chromium_ok = kw.get("chromium", True)
        self.busy = set(kw.get("busy", ()))
        self.commands = kw.get("commands", {})               # tuple(cmd) -> (rc, out)
        self.files = kw.get("files", {})                     # path -> mode (None = missing)
        self.disk = kw.get("disk", 100.0)
        self.ram = kw.get("ram", 16.0)
        self.which_map = kw.get("which", {})
        self.ran = []

    def run(self, cmd, timeout=15):
        self.ran.append(list(cmd))
        return self.commands.get(tuple(cmd), (127, "not found"))

    def which(self, name):
        return self.which_map.get(name)

    def exists(self, path):
        return str(path) in self.files

    def nonempty(self, path):
        return str(path) in self.files

    def mode(self, path):
        return self.files.get(str(path))

    def is_wsl(self):
        return bool(self.wsl)

    def wsl_version(self):
        return self.wsl

    def can_import(self, module):
        return module in self.imports

    def chromium(self):
        return "/x/chrome" if self.chromium_ok else None

    def port_free(self, port):
        return port not in self.busy

    def disk_free_gb(self, path):
        return self.disk

    def ram_total_gb(self):
        return self.ram


def probe(tmp_path, **kw):
    return FakeProbe(tmp_path, **kw)


def by_id(results):
    return {r.id: r for r in results}


# ------------------------------------------------------------------ system / WSL / python

def test_healthy_machine_has_no_problems(tmp_path):
    secrets = tmp_path / "config" / "secrets.env"
    res = doctor.run_checks(probe(tmp_path, files={str(secrets): 0o600},
                                  env={"USER": "alice", "HOME": f"{H}/alice", "AICORP_WORKER_USER": "wk"},
                                  which={"claude": "/usr/bin/claude"},
                                  commands={("id", "-u", "wk"): (0, "1001"), ("sudo", "-n", "-u", "wk", "true"): (0, ""),
                                            ("sudo", "-n", "-u", "wk", "-H", f"{H}/wk/.local/bin/claude", "--version"): (0, "2.1.3 (Claude Code)"),
                                            ("getent", "passwd", "wk"): (0, f"wk:x:1001:1001::{H}/wk:/bin/bash"),
                                            ("sudo", "-n", "-u", "wk", "test", "-s", f"{H}/wk/.claude/.credentials.json"): (0, "")}))
    assert doctor.summary(res)["ok"] and all(r.status == OK for r in res), [(r.id, r.status, r.detail) for r in res if r.status != OK]


def test_wsl_states_on_windows(tmp_path):
    win = dict(platform="win32")
    none = by_id(doctor.run_checks(probe(tmp_path, commands={}, **win)))["wsl"]
    assert none.status == FAIL and none.needs_admin and "wsl --install" in none.next_step
    no_distro = by_id(doctor.run_checks(probe(tmp_path, commands={("wsl.exe", "--status"): (0, "ok"), ("wsl.exe", "-l", "-q"): (0, "")}, **win)))["wsl"]
    assert no_distro.status == FAIL and "distribution" in no_distro.detail
    good = by_id(doctor.run_checks(probe(tmp_path, commands={("wsl.exe", "--status"): (0, "ok"),
                                                             ("wsl.exe", "-l", "-q"): (0, "U\x00b\x00u\x00n\x00t\x00u\x00\nDebian")}, **win)))["wsl"]
    assert good.status == OK and "2 distribution" in good.detail


def test_wsl1_and_plain_linux(tmp_path):
    assert by_id(doctor.run_checks(probe(tmp_path, wsl="1")))["wsl"].status == FAIL
    assert by_id(doctor.run_checks(probe(tmp_path, wsl="")))["wsl"].detail == "not needed on this system"
    assert by_id(doctor.run_checks(probe(tmp_path, platform="darwin", wsl="")))["system"].status == WARN


def test_old_python_is_a_problem(tmp_path):
    r = by_id(doctor.run_checks(probe(tmp_path, python=(3, 10, 4))))["python"]
    assert r.status == FAIL and "3.12" in r.detail and r.needs_admin


# ------------------------------------------------------------------ environment

def test_missing_venv_package_and_chromium_offer_safe_fixes(tmp_path):
    res = by_id(doctor.run_checks(probe(tmp_path, in_venv=False, imports={"fastapi"}, chromium=False)))
    assert (res["venv"].fix, res["package"].fix, res["chromium"].fix) == ("venv", "package", "chromium")
    assert res["package"].status == FAIL and "jinja2" in res["package"].detail
    assert not any(r.needs_admin for r in (res["venv"], res["package"], res["chromium"]))


def test_worker_user_states(tmp_path):
    none = by_id(doctor.run_checks(probe(tmp_path)))["worker"]
    assert none.status == WARN and none.needs_admin
    cmds = {("id", "-u", "aicorp-worker"): (0, "1"), ("sudo", "-n", "-u", "aicorp-worker", "true"): (0, "")}
    exists = by_id(doctor.run_checks(probe(tmp_path, commands=cmds)))["worker"]            # the standard worker is used without being configured
    assert exists.status == OK and "found automatically" in exists.detail
    nosudo_default = by_id(doctor.run_checks(probe(tmp_path, commands={("id", "-u", "aicorp-worker"): (0, "1")})))["worker"]
    assert nosudo_default.status == FAIL and "sudo" in nosudo_default.detail
    env = {"AICORP_WORKER_USER": "wk"}
    gone = by_id(doctor.run_checks(probe(tmp_path, env=env)))["worker"]
    assert gone.status == FAIL and "does not exist" in gone.detail
    nosudo = by_id(doctor.run_checks(probe(tmp_path, env=env, commands={("id", "-u", "wk"): (0, "1")})))["worker"]
    assert nosudo.status == FAIL and "sudo" in nosudo.detail
    same = by_id(doctor.run_checks(probe(tmp_path, env={"AICORP_ALLOW_SAME_USER": "1"})))["worker"]
    assert same.status == WARN and "weaker" in same.next_step


def test_claude_states(tmp_path):
    missing = by_id(doctor.run_checks(probe(tmp_path)))["claude"]
    assert missing.status == WARN and missing.needs_admin
    base = {"AICORP_WORKER_USER": "wk"}
    cmds = {("id", "-u", "wk"): (0, "1"), ("sudo", "-n", "-u", "wk", "true"): (0, ""),
            ("sudo", "-n", "-u", "wk", "-H", f"{H}/wk/.local/bin/claude", "--version"): (0, "2.1.3 (Claude Code)"),
            ("getent", "passwd", "wk"): (0, f"wk:x:1:1::{H}/wk:/bin/sh")}
    nologin = by_id(doctor.run_checks(probe(tmp_path, env=base, commands=cmds)))["claude"]
    assert nologin.status == WARN and "no login" in nologin.detail and "2.1.3" in nologin.detail
    broken = by_id(doctor.run_checks(probe(tmp_path, env=base, commands={k: v for k, v in cmds.items() if "--version" not in k})))["claude"]
    assert broken.status == WARN and "could not be started" in broken.detail            # only real Claude work needs it, so it does not block the start


def test_secrets_file_states(tmp_path):
    path = str(tmp_path / "config" / "secrets.env")
    assert by_id(doctor.run_checks(probe(tmp_path)))["secrets"].status == WARN
    open_ = by_id(doctor.run_checks(probe(tmp_path, files={path: 0o644})))["secrets"]
    assert open_.status == FAIL and "chmod 600" in open_.next_step
    assert by_id(doctor.run_checks(probe(tmp_path, files={path: 0o600})))["secrets"].status == OK


def test_resources(tmp_path):
    assert by_id(doctor.run_checks(probe(tmp_path, disk=0.5)))["resources"].status == FAIL
    assert by_id(doctor.run_checks(probe(tmp_path, disk=3)))["resources"].status == WARN
    assert by_id(doctor.run_checks(probe(tmp_path, ram=2)))["resources"].status == WARN


def test_a_crashing_check_is_reported_not_fatal(tmp_path, monkeypatch):
    def boom(p):
        raise RuntimeError(f"secret path {H}/alice/x")
    monkeypatch.setattr(doctor, "CHECKS", (boom, doctor.check_python))
    res = doctor.run_checks(probe(tmp_path))
    assert res[0].status == WARN and "RuntimeError" in res[0].detail and "alice" not in res[0].detail and res[1].status == OK


# ------------------------------------------------------------------ port

def test_port_fallback_when_busy(tmp_path):
    p = probe(tmp_path, busy={8765, 8766})
    assert doctor.find_free_port(p) == 8767
    r = by_id(doctor.run_checks(p))["port"]
    assert r.status == OK and "8767" in r.detail and "busy" in r.detail
    assert by_id(doctor.run_checks(probe(tmp_path)))["port"].detail == "port 8765 is free"
    with pytest.raises(OSError):
        doctor.find_free_port(probe(tmp_path, busy=set(range(8765, 8815))))
    assert by_id(doctor.run_checks(probe(tmp_path, busy=set(range(8765, 8815)))))["port"].status == FAIL


def test_port_probe_sees_a_really_busy_port(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        real = Probe(tmp_path)
        assert not real.port_free(busy)
        assert doctor.find_free_port(real, busy) != busy
    finally:
        s.close()


# ------------------------------------------------------------------ help prompt

def test_help_prompt_names_the_problem_and_leaks_nothing(tmp_path):
    env = {"USER": "alicesmith", "HOME": f"{H}/alicesmith", "AICORP_WORKER_USER": "wkr-secret-user",
           "AICORP_HOME": f"{H}/alicesmith/aicorp-data"}
    p = probe(tmp_path, env=env, in_venv=False, chromium=False)
    results = doctor.run_checks(p)
    results.append(doctor.Result("custom", "Custom", FAIL, f"failed at {H}/alicesmith/aicorp-data/x for alicesmith{AT}example.com "
                                 f"token ghp_ABCDEFGHIJKLMNOPQRSTUVWX12 in {WINPATH}", f"see {VAR}/thing/a"))
    text = doctor.help_prompt(results, p)
    assert "Virtual environment" in text and "step by step" in text and "Chromium" in text
    for leaked in ("alicesmith", "wkr-secret-user", "example.com", "ghp_ABCDEF", f"{H}/", "Users", VAR, str(tmp_path)):
        assert leaked not in text, leaked
    assert doctor.help_prompt([doctor.Result("a", "A", OK, "fine")], p) == ""
    only = doctor.help_prompt(results, p, only="venv")
    assert "Virtual environment" in only and "Chromium" not in only


def test_scrub_hides_paths_users_emails_tokens(tmp_path):
    p = probe(tmp_path, env={"USER": "bobby", "HOME": f"{H}/bobby"})
    out = doctor.scrub(f"bobby at {H}/bobby/a/b mail bobby{AT}x.org key abcdefghijklmnopqrstuvwxyz123456 ok", p)
    assert "bobby" not in out and "<email>" in out and "<path>" in out and "<token>" in out
    assert "abcdefghij" not in out and "/a/b" not in out and "x.org" not in out


# ------------------------------------------------------------------ fixes and recheck

def test_only_listed_fixes_run_and_never_with_sudo(tmp_path):
    p = probe(tmp_path)
    with pytest.raises(KeyError):
        doctor.apply_fix("rm-everything", p)
    for fix in doctor.FIXES:
        for cmd in doctor.fix_commands(fix, p):
            assert "sudo" not in cmd and "apt" not in " ".join(cmd) and "wsl" not in " ".join(cmd).lower()


def test_recheck_after_a_fix_shows_ok(tmp_path):
    p = probe(tmp_path, in_venv=False, imports=set())
    before = by_id(doctor.run_checks(p))
    assert before["package"].status == FAIL and before["package"].fix == "package"

    class Fixed(FakeProbe):
        def run(self, cmd, timeout=15):
            p.imports.update({"fastapi", "jinja2", "uvicorn", "multipart"})
            return 0, "installed"
    ok, msg = doctor.apply_fix("package", Fixed(tmp_path, in_venv=False, imports=set()))
    assert ok and "check again" in msg
    after = by_id(doctor.run_checks(p))
    assert after["package"].status == OK


def test_failed_fix_reports_scrubbed_tail(tmp_path):
    class Bad(FakeProbe):
        def run(self, cmd, timeout=15):
            return 1, f"error while writing {H}/alice/secret/file"
    ok, msg = doctor.apply_fix("venv", Bad(tmp_path))
    assert not ok and f"{H}/alice" not in msg


# ------------------------------------------------------------------ output / no side effects

def test_json_output_and_exit_code(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(doctor, "Probe", lambda: probe(tmp_path))
    assert doctor.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"summary", "port", "checks"} and data["port"] == 8765
    assert {"id", "title", "status", "detail", "next_step", "fix", "needs_admin"} <= set(data["checks"][0])
    monkeypatch.setattr(doctor, "Probe", lambda: probe(tmp_path, python=(3, 9, 0)))
    assert doctor.main([]) == 1
    assert "[FAIL]" in capsys.readouterr().out
    assert doctor.main(["--prompt"]) == 1
    assert "step by step" in capsys.readouterr().out


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        h.update(str(path.relative_to(root)).encode())
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


def test_real_doctor_changes_nothing(tmp_path, monkeypatch):
    for name in ("config", "database"):
        (tmp_path / name).mkdir()
    (tmp_path / "config" / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    os.chmod(tmp_path / "config" / "secrets.env", 0o600)
    monkeypatch.setenv("AICORP_HOME", str(tmp_path))
    before = tree_hash(tmp_path)
    results = doctor.run_checks(Probe(tmp_path))
    doctor.help_prompt(results, Probe(tmp_path))
    assert tree_hash(tmp_path) == before
    assert {r.id for r in results} == {"system", "wsl", "python", "venv", "package", "chromium", "worker", "claude", "secrets", "port", "resources"}

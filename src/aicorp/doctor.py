"""Doctor: checks the prerequisites and says what to do next. ``python -m aicorp.doctor [--json] [--prompt]``.

Read-only: it looks, it never installs, changes files or runs anything with sudo/admin rights. Every check is a
small function over a ``Probe`` (the only place that touches the system), so tests can fake any machine.
The help prompt is meant to be pasted into an AI chat, so it is scrubbed of user names, home paths and secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PORT = 8765
DEFAULT_WORKER = "aicorp-worker"
MIN_PYTHON = (3, 12)
OK, WARN, FAIL = "ok", "warn", "fail"
# Thresholds are assumptions (not measured limits): they only decide when the doctor starts to complain.
MIN_DISK_GB_WARN, MIN_DISK_GB_FAIL = 5.0, 1.0
MIN_RAM_GB_WARN = 4.0


@dataclass
class Result:
    id: str
    title: str
    status: str
    detail: str
    next_step: str = ""
    fix: str | None = None                      # id of a safe one-click fix (see FIXES), else None
    needs_admin: bool = False                   # the Founder must do it by hand (sudo / admin / reboot / login)


class Probe:
    """Everything the checks know about the machine. Tests replace the methods they care about."""

    def __init__(self, home: Path | None = None) -> None:
        from . import config
        self.home = home or config.home()
        self.env = dict(os.environ)
        self.platform = sys.platform
        self.python = sys.version_info[:3]
        self.in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    def run(self, cmd: list[str], timeout: float = 15) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, type(exc).__name__

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def nonempty(self, path: Path | str) -> bool:
        try:
            return Path(path).stat().st_size > 0
        except OSError:
            return False

    def mode(self, path: Path | str) -> int | None:
        try:
            return Path(path).stat().st_mode & 0o777
        except OSError:
            return None

    def is_wsl(self) -> bool:
        try:
            return "microsoft" in Path("/proc/version").read_text().lower()
        except OSError:
            return False

    def wsl_version(self) -> str:
        try:
            text = Path("/proc/version").read_text().lower()
        except OSError:
            return ""
        return "2" if "wsl2" in text else ("1" if "microsoft" in text else "")

    def can_import(self, module: str) -> bool:
        import importlib.util
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    def chromium(self) -> str | None:
        try:
            from .browser_runner import chromium_path
            path = chromium_path()
        except Exception:                       # playwright missing or broken
            return None
        return path if Path(path).exists() else None

    def port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def disk_free_gb(self, path: Path) -> float:
        probe_path = path if path.exists() else path.parent
        return shutil.disk_usage(probe_path).free / 1e9

    def ram_total_gb(self) -> float | None:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1e6
        except (OSError, ValueError):
            pass
        return None


# ------------------------------------------------------------------ free port

def find_free_port(probe: Probe | None = None, preferred: int = DEFAULT_PORT, tries: int = 50) -> int:
    """``preferred`` if it is free, else the next free one. Raises if none of ``tries`` ports is free."""
    probe = probe or Probe()
    for port in range(preferred, preferred + tries):
        if probe.port_free(port):
            return port
    raise OSError(f"no free port between {preferred} and {preferred + tries - 1}")


# ------------------------------------------------------------------ checks

def check_system(p: Probe) -> Result:
    if p.platform.startswith("win"):
        return Result("system", "Operating system", WARN, "Windows (native)",
                      "AICorp runs inside WSL2 on Windows. The Windows app starts it there; the next check looks at WSL.")
    if p.platform == "darwin":
        return Result("system", "Operating system", WARN, "macOS (not tested yet)",
                      "It may work, but only Windows with WSL2 has been tested. Report what you find.")
    if p.is_wsl():
        return Result("system", "Operating system", OK, "Linux inside WSL" + p.wsl_version())
    return Result("system", "Operating system", OK, "Linux (native install is not tested yet)")


def check_wsl(p: Probe) -> Result:
    title = "WSL2 (Windows Subsystem for Linux)"
    if p.platform.startswith("win"):
        rc, out = p.run(["wsl.exe", "--status"])
        if rc != 0:
            return Result("wsl", title, FAIL, "WSL is not installed or not enabled",
                          "Open PowerShell as administrator, run: wsl --install -d Ubuntu - then restart the computer. "
                          "This needs administrator rights and a restart, so you do it yourself.", needs_admin=True)
        rc, out = p.run(["wsl.exe", "-l", "-q"])
        distros = [d.strip().replace("\x00", "") for d in out.splitlines() if d.strip()]
        if not distros:
            return Result("wsl", title, FAIL, "WSL is installed but has no Linux distribution",
                          "Run in PowerShell: wsl --install -d Ubuntu, then finish the Ubuntu first-start setup.", needs_admin=True)
        return Result("wsl", title, OK, f"{len(distros)} distribution(s) installed")
    if p.is_wsl():
        if p.wsl_version() == "1":
            return Result("wsl", title, FAIL, "This is WSL 1", "Convert it: in PowerShell run wsl --set-version <distro> 2", needs_admin=True)
        return Result("wsl", title, OK, "running inside WSL2")
    return Result("wsl", title, OK, "not needed on this system")


def check_python(p: Probe) -> Result:
    have = ".".join(map(str, p.python))
    if p.python[:2] < MIN_PYTHON:
        return Result("python", "Python", FAIL, f"Python {have} is too old (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer)",
                      "Install Python 3.12+ (Ubuntu: sudo apt install python3.12 python3.12-venv). This needs sudo, so you run it.",
                      needs_admin=True)
    return Result("python", "Python", OK, f"Python {have}")


def check_venv(p: Probe) -> Result:
    if p.in_venv:
        return Result("venv", "Virtual environment", OK, "running inside a virtual environment")
    return Result("venv", "Virtual environment", WARN, "not running inside a virtual environment",
                  "Create one so AICorp does not touch system Python: python3 -m venv .venv (one-click fix available).", fix="venv")


def check_package(p: Probe) -> Result:
    missing = [m for m in ("fastapi", "jinja2", "uvicorn", "multipart") if not p.can_import(m)]
    if missing:
        return Result("package", "AICorp and its libraries", FAIL, "missing: " + ", ".join(missing),
                      "Install them: pip install -e . (one-click fix available).", fix="package")
    return Result("package", "AICorp and its libraries", OK, "all required libraries are installed")


def check_chromium(p: Probe) -> Result:
    path = p.chromium()
    if path:
        return Result("chromium", "Browser for the browser worker (Chromium)", OK, "Chromium for Playwright is installed")
    return Result("chromium", "Browser for the browser worker (Chromium)", WARN, "Chromium for Playwright is not installed",
                  "Only needed for browser tasks: python -m playwright install chromium (one-click fix available).", fix="chromium")


def worker_user(p: Probe) -> str | None:
    return p.env.get("AICORP_WORKER_USER") or None


def check_worker(p: Probe) -> Result:
    user = worker_user(p)
    title = "Worker user (runs the AI agents)"
    if not user:
        if p.env.get("AICORP_ALLOW_SAME_USER") == "1":
            return Result("worker", title, WARN, "agents would run as you (AICORP_ALLOW_SAME_USER=1)",
                          "This is weaker isolation. A separate worker user is recommended.")
        rc, _ = p.run(["id", "-u", DEFAULT_WORKER])
        if rc == 0:
            return Result("worker", title, WARN, f"a user named {DEFAULT_WORKER} exists but AICORP_WORKER_USER is not set",
                          f"Set AICORP_WORKER_USER={DEFAULT_WORKER} (the control panel does this for you when it starts the system).")
        return Result("worker", title, WARN, "no worker user configured (fine for the simulated engine, not for real Claude work)",
                      "Run scripts/setup_worker_user.sh with sudo, then set AICORP_WORKER_USER. This needs sudo, so you run it.",
                      needs_admin=True)
    rc, _ = p.run(["id", "-u", user])
    if rc != 0:
        return Result("worker", title, FAIL, "the configured worker user does not exist",
                      "Run scripts/setup_worker_user.sh with sudo to create it. This needs sudo, so you run it.", needs_admin=True)
    rc, _ = p.run(["sudo", "-n", "-u", user, "true"])
    if rc != 0:
        return Result("worker", title, FAIL, "the worker user exists but passwordless sudo to it is not set up",
                      "Re-run scripts/setup_worker_user.sh with sudo (it writes the sudoers rule). You run it.", needs_admin=True)
    return Result("worker", title, OK, "worker user exists and can be used")


def _worker_home(p: Probe, user: str) -> str | None:
    rc, out = p.run(["getent", "passwd", user])
    if rc != 0:
        return None
    fields = out.split(":")
    return fields[5] if len(fields) > 5 and fields[5] else None


def check_claude(p: Probe) -> Result:
    title = "Claude Code (installed and logged in)"
    user = worker_user(p)
    binary = p.env.get("AICORP_CLAUDE_BIN", "claude")
    cmd = ["sudo", "-n", "-u", user, "-H", binary, "--version"] if user else [binary, "--version"]
    if not user and not p.which(binary):
        return Result("claude", title, WARN, "Claude Code was not found",
                      "Install Claude Code for the worker user, then log in once with your Claude Pro account.", needs_admin=True)
    rc, out = p.run(cmd)
    if rc != 0:
        return Result("claude", title, FAIL if user else WARN, "Claude Code could not be started" + (" as the worker user" if user else ""),
                      "Check AICORP_CLAUDE_BIN, and install Claude Code for the worker user.", needs_admin=True)
    version = re.search(r"\d+\.\d+\.\d+", out)
    where = _worker_home(p, user) if user else p.env.get("HOME")
    cred = f"{where}/.claude/.credentials.json" if where else None
    if user and cred:
        rc2, _ = p.run(["sudo", "-n", "-u", user, "test", "-s", cred])
        logged_in = rc2 == 0
    else:
        logged_in = bool(cred) and p.nonempty(cred)
    ver = f"version {version.group()}" if version else "installed"
    if not logged_in:
        return Result("claude", title, WARN, f"{ver}; no login found",
                      "Log in once as the worker user: sudo -u <worker> -H claude , then /login. You do this yourself (browser login).",
                      needs_admin=True)
    return Result("claude", title, OK, f"{ver}; a login file is present (whether it is still valid is not checked)")


def check_secrets(p: Probe) -> Result:
    path = p.home / "config" / "secrets.env"
    title = "Secrets file (Telegram / Gmail)"
    if not p.exists(path):
        return Result("secrets", title, WARN, "config/secrets.env does not exist (alerts and chat are off)",
                      "Optional. Copy config/secrets.env.example to config/secrets.env and fill it in yourself.")
    mode = p.mode(path)
    if mode is not None and not p.platform.startswith("win") and mode & 0o077:
        return Result("secrets", title, FAIL, f"config/secrets.env is readable by others (mode {mode:o})",
                      "Run: chmod 600 config/secrets.env")
    return Result("secrets", title, OK, "config/secrets.env exists and is private")


def check_port(p: Probe) -> Result:
    wanted = int(p.env.get("AICORP_PORT", DEFAULT_PORT))
    try:
        port = find_free_port(p, wanted)
    except OSError as exc:
        return Result("port", "Dashboard port", FAIL, str(exc), "Close programs that use ports 8765 and up, or set AICORP_PORT.")
    if port == wanted:
        return Result("port", "Dashboard port", OK, f"port {port} is free")
    return Result("port", "Dashboard port", OK, f"port {wanted} is busy; port {port} will be used instead")


def check_resources(p: Probe) -> Result:
    free = p.disk_free_gb(p.home)
    ram = p.ram_total_gb()
    detail = f"{free:.1f} GB disk free" + (f", {ram:.1f} GB RAM" if ram is not None else "")
    if free < MIN_DISK_GB_FAIL:
        return Result("resources", "Disk and memory", FAIL, detail, "Free some disk space; AICorp stores the database, backups and workspaces here.")
    if free < MIN_DISK_GB_WARN or (ram is not None and ram < MIN_RAM_GB_WARN):
        return Result("resources", "Disk and memory", WARN, detail, "Low on disk or memory; several agents and a browser at once may be slow.")
    return Result("resources", "Disk and memory", OK, detail)


CHECKS = (check_system, check_wsl, check_python, check_venv, check_package, check_chromium,
          check_worker, check_claude, check_secrets, check_port, check_resources)


def run_checks(probe: Probe | None = None) -> list[Result]:
    probe = probe or Probe()
    results = []
    for check in CHECKS:
        try:
            results.append(check(probe))
        except Exception as exc:                # one broken check must not hide the others
            results.append(Result(check.__name__.removeprefix("check_"), check.__name__, WARN, f"check failed: {type(exc).__name__}"))
    return results


def summary(results: list[Result]) -> dict:
    counts = {s: sum(1 for r in results if r.status == s) for s in (OK, WARN, FAIL)}
    return {"ok": counts[FAIL] == 0, "counts": counts}


# ------------------------------------------------------------------ help prompt

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ABS_PATH = re.compile(r"(?<![\w:/])(?:/[\w.@+-]+){2,}|[A-Za-z]:\\[^\s]+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def scrub(text: str, probe: Probe | None = None) -> str:
    """Remove what must not be pasted into a chat: e-mails, absolute paths, token-like strings, then known user names."""
    probe = probe or Probe()
    text = _EMAIL.sub("<email>", text)
    text = _ABS_PATH.sub("<path>", text)
    text = _LONG_TOKEN.sub("<token>", text)
    names: set[str] = {str(probe.home)}
    for key in ("USER", "USERNAME", "LOGNAME", "AICORP_WORKER_USER", "HOME", "USERPROFILE", "AICORP_HOME"):
        value = probe.env.get(key)
        if value and len(value) >= 3:
            names.add(value)
    for value in sorted(names, key=len, reverse=True):
        text = text.replace(value, "<hidden>")
    return text


def help_prompt(results: list[Result], probe: Probe | None = None, only: str | None = None) -> str:
    """A ready-to-paste message for ChatGPT/Claude: what was checked, what is wrong, what is wanted."""
    probe = probe or Probe()
    problems = [r for r in results if r.status != OK and (only is None or r.id == only)]
    if not problems:
        return ""
    system = next((r.detail for r in results if r.id == "system"), probe.platform)
    lines = [
        "I am installing a local tool called AICorp (a Python 3.12 app that runs on Linux or Windows with WSL2).",
        f"My system: {system}. Python {'.'.join(map(str, probe.python))}.",
        "Its setup checker reports these problems:",
    ]
    for r in problems:
        lines.append(f"- {r.title}: {r.detail} [{r.status}]")
        if r.next_step:
            lines.append(f"  The checker suggests: {r.next_step}")
    lines += ["", "Please guide me step by step, one step at a time, in simple words. Tell me what each command does before I run it,",
              "and warn me about anything that needs administrator rights or could delete data. I am not an expert.",
              "After each step I will tell you what I see."]
    return scrub("\n".join(lines), probe)


# ------------------------------------------------------------------ safe one-click fixes

def project_dir(probe: Probe | None = None) -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


def fix_commands(fix_id: str, probe: Probe | None = None) -> list[list[str]]:
    """The exact commands of a one-click fix. Never sudo/admin, never outside the project folder."""
    root = project_dir(probe)
    venv = root / ".venv"
    py = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    if fix_id == "venv":
        return [[sys.executable, "-m", "venv", str(venv)]]
    if fix_id == "package":
        return [[str(py if py.exists() else sys.executable), "-m", "pip", "install", "-e", str(root)]]
    if fix_id == "chromium":
        return [[str(py if py.exists() else sys.executable), "-m", "playwright", "install", "chromium"]]
    raise KeyError(fix_id)


FIXES = ("venv", "package", "chromium")


def apply_fix(fix_id: str, probe: Probe | None = None, timeout: float = 900) -> tuple[bool, str]:
    """Run a safe fix and return (success, last lines of output). Only the ids in FIXES are accepted."""
    if fix_id not in FIXES:
        raise KeyError(fix_id)
    probe = probe or Probe()
    out = ""
    for cmd in fix_commands(fix_id, probe):
        rc, out = probe.run(cmd, timeout=timeout)
        if rc != 0:
            return False, scrub("\n".join(out.splitlines()[-8:]), probe)
    return True, "done - run the check again"


# ------------------------------------------------------------------ command line

ICON = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def render(results: list[Result]) -> str:
    lines = []
    for r in results:
        lines.append(f"{ICON[r.status]} {r.title}: {r.detail}")
        if r.status != OK and r.next_step:
            lines.append(f"        -> {r.next_step}")
            if r.fix:
                lines.append(f"        (one-click fix: {r.fix})")
    s = summary(results)["counts"]
    lines.append(f"\n{s[OK]} ok, {s[WARN]} warning(s), {s[FAIL]} problem(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicorp.doctor", description="Check the prerequisites (changes nothing).")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--prompt", action="store_true", help="print a help prompt to paste into ChatGPT/Claude")
    parser.add_argument("--only", help="limit --prompt to one check id")
    parser.add_argument("--fix", choices=FIXES, help="run one safe fix (never sudo), then say what happened")
    args = parser.parse_args(argv)
    probe = Probe()
    if args.fix:
        ok, message = apply_fix(args.fix, probe)
        print(json.dumps({"ok": ok, "message": message}) if args.json else message)
        return 0 if ok else 1
    results = run_checks(probe)
    if args.json:
        print(json.dumps({"summary": summary(results), "port": find_free_port(probe, int(probe.env.get("AICORP_PORT", DEFAULT_PORT))),
                          "checks": [asdict(r) for r in results]}, indent=2))
    elif args.prompt:
        print(help_prompt(results, probe, args.only) or "Nothing to ask for help with: no problems found.")
    else:
        print(render(results))
    return 0 if summary(results)["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

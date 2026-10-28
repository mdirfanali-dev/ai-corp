"""Windows desktop app, logic part: talks to WSL, checks it, starts the AICorp control panel inside it.

No GUI and no Windows-only imports here, so it can be tested on Linux with a stand-in for ``wsl.exe``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

try:                                              # the checks and the help prompt are shared with the command-line doctor
    from aicorp import doctor
except ImportError:                               # running from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aicorp import doctor

URL_RE = re.compile(r"control panel:\s*(http://127\.0\.0\.1:\d+/)")
CREATE_NO_WINDOW = 0x08000000


def decode(raw: bytes) -> str:
    """``wsl.exe`` writes some output as UTF-16; other output is UTF-8."""
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="replace").lstrip("﻿")
    return raw.decode("utf-8", errors="replace")


@dataclass
class Distro:
    name: str
    default: bool
    state: str
    version: str


def parse_distros(text: str) -> list[Distro]:
    """Parse ``wsl -l -v``."""
    out = []
    for line in text.replace("\r", "").splitlines()[1:]:
        if not line.strip():
            continue
        default = line.lstrip().startswith("*")
        parts = line.replace("*", " ").split()
        if len(parts) >= 3:
            out.append(Distro(" ".join(parts[:-2]), default, parts[-2], parts[-1]))
    return out


# ------------------------------------------------------------------ settings

def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "AICorp" / "settings.json"


DEFAULTS = {"distro": None, "project": None, "data_dir": None}


def load_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {k: (data.get(k) or None) for k in DEFAULTS}


def save_settings(values: dict, path: Path | None = None) -> dict:
    path = path or settings_path()
    clean = {k: (str(values.get(k)).strip() or None) if values.get(k) else None for k in DEFAULTS}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


# ------------------------------------------------------------------ talking to wsl.exe

class Bridge:
    """Runs ``wsl.exe``. Tests give it another program name."""

    def __init__(self, exe: str = "wsl.exe") -> None:
        self.exe = exe

    def flags(self) -> dict:
        return {"creationflags": CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}

    def run(self, args: list[str], timeout: float = 90) -> tuple[int, str]:
        try:
            p = subprocess.run([self.exe, *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **self.flags())
        except FileNotFoundError:
            return 127, "wsl.exe was not found"
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        return p.returncode, decode(p.stdout + p.stderr).strip()

    def in_distro(self, distro: str, cwd: str | None, *cmd: str, timeout: float = 90) -> tuple[int, str]:
        args = ["-d", distro] + (["--cd", cwd] if cwd else []) + ["-e", *cmd]
        return self.run(args, timeout=timeout)

    def popen(self, distro: str, cwd: str, cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen([self.exe, "-d", distro, "--cd", cwd, "-e", *cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **self.flags())


def distros(bridge: Bridge) -> list[Distro]:
    rc, out = bridge.run(["-l", "-v"], timeout=30)
    return parse_distros(out) if rc == 0 else []


def find_project(bridge: Bridge, distro: str) -> str | None:
    """Look for an AICorp checkout in the usual places inside the distribution."""
    script = 'for d in "$HOME/ai-corp" "$HOME/aicorp" "$HOME/ai-corp/aicorp"; do [ -f "$d/pyproject.toml" ] && echo "$d" && break; done'
    rc, out = bridge.in_distro(distro, None, "bash", "-c", script, timeout=60)
    return out.splitlines()[-1].strip() if rc == 0 and out.strip() else None


# ------------------------------------------------------------------ checks

def windows_checks(bridge: Bridge, settings: dict) -> tuple[list[doctor.Result], dict]:
    """Checks that only the Windows side can do. Returns the results and the (possibly completed) settings."""
    settings = dict(settings)
    results: list[doctor.Result] = []
    rc, _ = bridge.run(["--status"], timeout=30)
    if rc != 0:
        results.append(doctor.Result("wsl", "WSL2 (Windows Subsystem for Linux)", doctor.FAIL, "WSL is not installed or not enabled",
                                     "Open PowerShell as administrator, run: wsl --install -d Ubuntu - then restart the computer. "
                                     "This needs administrator rights and a restart, so you do it yourself.", needs_admin=True))
        return results, settings
    found = distros(bridge)
    if not found:
        results.append(doctor.Result("wsl", "WSL2 (Windows Subsystem for Linux)", doctor.FAIL, "WSL is installed but has no Linux distribution",
                                     "Run in PowerShell: wsl --install -d Ubuntu, then finish the Ubuntu first-start setup.", needs_admin=True))
        return results, settings
    names = [d.name for d in found]
    chosen = settings.get("distro") if settings.get("distro") in names else next((d.name for d in found if d.default), names[0])
    settings["distro"] = chosen
    info = next(d for d in found if d.name == chosen)
    if info.version == "1":
        results.append(doctor.Result("wsl", "WSL2 (Windows Subsystem for Linux)", doctor.FAIL, f"{chosen} runs as WSL 1",
                                     f"Convert it: in PowerShell run wsl --set-version {chosen} 2", needs_admin=True))
        return results, settings
    results.append(doctor.Result("wsl", "WSL2 (Windows Subsystem for Linux)", doctor.OK, f"{chosen} (WSL 2) is installed"))
    project = settings.get("project") or find_project(bridge, chosen)
    if project:
        rc, _ = bridge.in_distro(chosen, None, "test", "-f", f"{project}/pyproject.toml", timeout=60)
        if rc != 0:
            project = None
    if not project:
        results.append(doctor.Result("project", "AICorp folder inside Linux", doctor.FAIL, "the AICorp folder was not found",
                                     "Clone it inside Linux (git clone https://github.com/mdirfanali-dev/ai-corp.git ~/ai-corp), "
                                     "then type that folder in Settings below and press Recheck."))
        return results, settings
    settings["project"] = project
    results.append(doctor.Result("project", "AICorp folder inside Linux", doctor.OK, "found"))
    return results, settings


def python_in(bridge: Bridge, settings: dict) -> str:
    """The project's own virtual environment when it exists, otherwise the system Python."""
    rc, _ = bridge.in_distro(settings["distro"], settings["project"], "test", "-x", ".venv/bin/python", timeout=60)
    return ".venv/bin/python" if rc == 0 else "python3"


def wsl_doctor(bridge: Bridge, settings: dict, fix: str | None = None) -> list[doctor.Result] | tuple[bool, str]:
    """Run the command-line doctor inside the distribution (system Python, no virtual environment needed)."""
    base = ["env", "PYTHONPATH=src", *([f"AICORP_HOME={settings['data_dir']}"] if settings.get("data_dir") else []),
            python_in(bridge, settings), "-m", "aicorp.doctor"]
    if fix:
        rc, out = bridge.in_distro(settings["distro"], settings["project"], *base, "--fix", fix, "--json", timeout=1200)
        try:
            data = json.loads(out.splitlines()[-1])
            return bool(data["ok"]), str(data["message"])
        except (ValueError, KeyError, IndexError):
            return False, "The fix could not run: " + doctor.scrub(out[-300:], _probe())
    rc, out = bridge.in_distro(settings["distro"], settings["project"], *base, "--json", timeout=120)
    try:
        data = json.loads(out[out.index("{"):])
        return [doctor.Result(**c) for c in data["checks"]]
    except (ValueError, KeyError, TypeError):
        return [doctor.Result("python", "Python inside Linux", doctor.FAIL, "the AICorp checker could not run inside Linux",
                              "Make sure Python 3.12 or newer is installed in Ubuntu: sudo apt install python3 python3-venv (you run this).",
                              needs_admin=True)]


def _probe():
    return SimpleNamespace(env=dict(os.environ), home=Path.home(), python=sys.version_info[:3], platform=sys.platform)


def all_checks(bridge: Bridge, settings: dict) -> tuple[list[doctor.Result], dict]:
    results, settings = windows_checks(bridge, settings)
    if all(r.status == doctor.OK for r in results) and settings.get("project"):
        results += [r for r in wsl_doctor(bridge, settings) if r.id != "wsl"]       # the Windows side already checked WSL
    return results, settings


def help_prompt(results: list[doctor.Result], only: str | None = None) -> str:
    return doctor.help_prompt(results, _probe(), only=only)


def blocking(results: list[doctor.Result]) -> bool:
    return any(r.status == doctor.FAIL for r in results)


# ------------------------------------------------------------------ the running app

class AppProcess:
    """The AICorp control panel running inside WSL. Standard input stays open: if this program dies, the panel shuts down."""

    def __init__(self, bridge: Bridge, settings: dict, port: int = 8760) -> None:
        self.bridge, self.settings, self.port = bridge, settings, port
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.lines: deque[str] = deque(maxlen=200)
        self._url_ready = threading.Event()

    def command(self) -> list[str]:
        env = ["env", "PYTHONPATH=src", *([f"AICORP_HOME={self.settings['data_dir']}"] if self.settings.get("data_dir") else [])]
        return [*env, "python3", "-u", "-m", "aicorp.app", "--control-port", str(self.port), "--stdin-guard"]

    def start(self) -> None:
        cmd = self.command()
        cmd[cmd.index("python3")] = python_in(self.bridge, self.settings)
        self.proc = self.bridge.popen(self.settings["distro"], self.settings["project"], cmd)
        threading.Thread(target=self._read, daemon=True, name="app-output").start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in iter(self.proc.stdout.readline, b""):
            line = decode(raw).rstrip()
            self.lines.append(line)
            m = URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(1)
                self._url_ready.set()
        self._url_ready.set()

    def wait_for_url(self, timeout: float = 120) -> str | None:
        self._url_ready.wait(timeout)
        return self.url

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def tail(self, n: int = 12) -> str:
        return doctor.scrub("\n".join(list(self.lines)[-n:]), _probe())

    def stop(self, wait: float = 20) -> None:
        """Ask nicely (close standard input = shut down in order), then insist."""
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def reachable(url: str, timeout: float = 30, fetch: Callable[[str], int] | None = None) -> bool:
    def default(u: str) -> int:
        with urllib.request.urlopen(u, timeout=3) as r:
            return r.status
    fetch = fetch or default
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fetch(url) == 200:
                return True
        except OSError:
            pass
        time.sleep(0.4)
    return False

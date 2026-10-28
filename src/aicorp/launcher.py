"""Open the dashboard in a standalone app-style browser window (best effort, cross-platform)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

CHROMIUM_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "chrome", "msedge")
MAC_APPS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium")


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def open_window(url: str) -> str:
    """Try an app-mode window, then the default browser. Returns what was used."""
    candidates: list[str] = [p for p in map(shutil.which, CHROMIUM_NAMES) if p]
    candidates += [p for p in MAC_APPS if Path(p).exists()] if sys.platform == "darwin" else []
    for exe in candidates:
        try:
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"app window via {Path(exe).name}"
        except OSError:
            continue
    if _is_wsl() and shutil.which("cmd.exe"):
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", "chrome", f"--app={url}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "app window via Windows Chrome (WSL)"
        except OSError:
            pass
    return "default browser" if webbrowser.open(url) else "none (open the URL manually)"

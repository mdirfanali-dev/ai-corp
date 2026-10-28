"""AICorp for Windows: one window that checks the computer, starts Linux (WSL) and AICorp, and shows the control panel.

``AICorp.exe [--project DIR] [--distro NAME] [--data-dir DIR]`` (the folders are Linux paths; they are remembered).
``python shell.py --check`` prints the checks as text without opening a window.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time

import wslbridge as wb


class Shell:
    """The logic of the window (not exposed to pages)."""

    def __init__(self, bridge: wb.Bridge, settings: dict, port: int = 8760) -> None:
        self.port = port
        self.bridge = bridge
        self.settings = settings
        self.state = "setup"                       # setup | starting | running | exiting
        self.app: wb.AppProcess | None = None
        self.window = None
        self.first_run = not wb.settings_path().exists()
        self._exit_began = 0.0

    # --- setup screen
    def check(self) -> dict:
        if self.state != "setup":
            return {"checks": [], "prompts": {}, "prompt": "", "blocked": True, "settings": self.settings, "distros": []}
        results, self.settings = wb.all_checks(self.bridge, self.settings)
        blocked = wb.blocking(results)
        if not blocked:
            wb.save_settings(self.settings)
        prompts = {r.id: wb.help_prompt(results, only=r.id) for r in results if r.status != "ok"}
        return {"checks": [dataclasses.asdict(r) for r in results], "prompts": prompts, "prompt": wb.help_prompt(results),
                "blocked": blocked, "settings": self.settings, "distros": [d.name for d in wb.distros(self.bridge)],
                "autostart": not self.first_run}

    def save(self, values: dict) -> dict:
        if self.state == "setup":
            self.settings = wb.save_settings({**self.settings, **values})
            self.first_run = False
        return self.settings

    def fix(self, fix_id: str) -> dict:
        if self.state != "setup" or fix_id not in ("venv", "package", "chromium"):
            return {"ok": False, "message": "Not available."}
        ok, message = wb.wsl_doctor(self.bridge, self.settings, fix=fix_id)
        return {"ok": ok, "message": message}

    def start(self) -> dict:
        if self.state != "setup":
            return {"ok": False, "error": "Already started."}
        if not self.settings.get("distro") or not self.settings.get("project"):
            return {"ok": False, "error": "Run the check first so AICorp knows where Linux and the AICorp folder are."}
        self.state = "starting"
        self.app = wb.AppProcess(self.bridge, self.settings, self.port)
        self.app.start()
        url = self.app.wait_for_url(150)
        if not url or not self.app.alive():
            log = self.app.tail()
            self.app.stop(3)
            self.state = "setup"
            return {"ok": False, "error": "The control panel did not report an address.", "log": log}
        if not wb.reachable(url, 40):
            log = self.app.tail()
            self.app.stop(10)
            self.state = "setup"
            return {"ok": False, "log": log, "error": "Windows cannot reach Linux on localhost. In %UserProfile%\\.wslconfig make sure "
                    "localhostForwarding is not set to false, run wsl --shutdown, and try again."}
        self.state = "running"
        threading.Thread(target=self._watch_child, daemon=True, name="watch-child").start()
        self.window.load_url(url)
        return {"ok": True}

    # --- control panel
    def exit_done(self) -> None:
        if self.state in ("running", "exiting"):
            threading.Timer(0.3, self.finish).start()

    def finish(self) -> None:
        if self.app:
            self.app.stop(15)
        self.state = "closed"
        if self.window:
            self.window.destroy()

    def _watch_child(self) -> None:
        while self.app and self.app.alive():
            time.sleep(0.5)
        if self.state in ("running", "exiting"):
            self.finish()

    OPEN_EXIT_DIALOG = ("(function(){var s=document.getElementById('shutdown');"
                        "if(s&&!s.classList.contains('show'))document.getElementById('exit').click();})()")

    def _open_exit_dialog(self) -> None:
        try:
            self.window.evaluate_js(self.OPEN_EXIT_DIALOG)
        except Exception:
            self.finish()                                      # the page is gone: nothing to ask, close

    def on_closing(self) -> bool:
        """The window's X button. While the system runs it opens the same Exit dialog as the Exit button (again, if it was cancelled)."""
        if self.state in ("running", "exiting"):
            if self.state == "exiting" and time.time() - self._exit_began > 90:
                return True                                    # a stuck shutdown can be forced after a minute and a half
            if self.state == "running":
                self.state, self._exit_began = "exiting", time.time()
            # evaluate_js must not run on the window's own thread (this handler): it would wait for itself and freeze the window
            threading.Thread(target=self._open_exit_dialog, daemon=True, name="open-exit-dialog").start()
            return False
        if self.state == "starting":
            return False                                       # wait until the start has finished or failed
        if self.app:
            self.app.stop(3)
        return True


class Api:
    """What the pages may call, nothing more. Everything except ``exit_done`` only works before the control panel is running."""

    def __init__(self, shell: Shell) -> None:
        self._shell = shell

    def check(self) -> dict:
        return self._shell.check()

    def save(self, values: dict) -> dict:
        return self._shell.save(values)

    def fix(self, fix_id: str) -> dict:
        return self._shell.fix(fix_id)

    def start(self) -> dict:
        return self._shell.start()

    def exit_done(self) -> None:
        self._shell.exit_done()


def check_only(bridge: wb.Bridge, settings: dict) -> int:
    results, _ = wb.all_checks(bridge, settings)
    for r in results:
        print(f"[{r.status.upper():4}] {r.title}: {r.detail}")
        if r.status != "ok" and r.next_step:
            print("       ->", r.next_step)
    print(json.dumps({"blocked": wb.blocking(results)}))
    return 1 if wb.blocking(results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="AICorp")
    parser.add_argument("--project")
    parser.add_argument("--distro")
    parser.add_argument("--data-dir")
    parser.add_argument("--check", action="store_true", help="print the checks and exit (no window)")
    args = parser.parse_args(argv)
    settings = wb.load_settings()
    updates = {k: v for k, v in (("project", args.project), ("distro", args.distro), ("data_dir", args.data_dir)) if v}
    if updates:
        settings = wb.save_settings({**settings, **updates})
    bridge = wb.Bridge()
    if args.check:
        return check_only(bridge, settings)
    import webview
    from setup_page import PAGE
    shell = Shell(bridge, settings)
    width, height = 1360, 860
    try:                                                       # never larger than the screen
        screen = webview.screens[0]
        width, height = min(width, int(screen.width * 0.92)), min(height, int(screen.height * 0.88))
    except (AttributeError, IndexError):
        pass
    window = webview.create_window("AICorp", html=PAGE, js_api=Api(shell), width=width, height=height, min_size=(800, 520))
    shell.window = window
    window.events.closing += shell.on_closing
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

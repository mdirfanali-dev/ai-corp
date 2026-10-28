# AICorp for Windows (desktop app)

One window that checks your computer, starts Linux (WSL2) and AICorp for you, and shows the control panel and the dashboard.
Windows 11 with WSL2 is the tested setup; Linux and macOS use `python -m aicorp.app` (see the user guide).

## Install

1. Install [Python for Windows](https://www.python.org/downloads/) (tick *Add python.exe to PATH*). It is only used to build the app.
2. In PowerShell, from the `desktop` folder of this repository:
   `powershell -ExecutionPolicy Bypass -File build.ps1 -Shortcut`
   (If the repository is inside WSL, open `\\wsl.localhost\<distribution>\...\desktop` or copy the folder first.)
3. Start **AICorp** from the desktop shortcut.

The first start shows a system check. Anything that needs administrator rights, a restart or a login is explained, and you do it
yourself; press *Recheck* afterwards. *Copy AI prompt* gives you text to paste into ChatGPT or Claude for step-by-step help.
Once everything is in place the app starts straight into the control panel.

## Settings

Stored in `%APPDATA%\AICorp\settings.json`: the Linux distribution, the AICorp folder inside Linux, and an optional data folder.
Command line: `AICorp.exe --project ~/ai-corp --distro Ubuntu --data-dir ~/aicorp-data`.
`python shell.py --check` prints the checks as text without opening a window.

## Good to know

* The `.exe` is not code-signed, so Windows SmartScreen may warn once: *More info* -> *Run anyway*.
* It needs the Microsoft Edge WebView2 runtime (included with Windows 11).
* Closing the window with the X button opens the same Exit dialog as the Exit button; everything is shut down in order.
  If the app is killed, the system inside Linux shuts itself down.
* If Windows cannot reach Linux on `localhost`, check `localhostForwarding` in `%UserProfile%\.wslconfig`, run `wsl --shutdown`, retry.
* Build tools (pywebview, PyInstaller and their dependencies) are used only to build the `.exe`; see `docs/THIRD_PARTY.md`.

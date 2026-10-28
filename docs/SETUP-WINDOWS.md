# Setup on Windows (desktop app)

Status: **tested** on Windows 11 with WSL2 (Ubuntu). Native Windows without WSL is not supported: the engine needs Linux processes.

AICorp runs inside WSL2. The Windows app is a small window that checks your computer, starts Linux and AICorp for you, and
shows the control panel and the dashboard, so you never open a terminal by hand.

## 1. Get Linux (WSL2) and AICorp

* Windows 11: open PowerShell **as administrator**, run `wsl --install -d Ubuntu`, restart, and finish the Ubuntu first-start questions.
* In Ubuntu: `git clone https://github.com/mdirfanali-dev/ai-corp.git ~/ai-corp` (Python 3.12 or newer is needed; see [DOCTOR.md](DOCTOR.md)).

## 2. Build and install the app

Follow [`desktop/README.md`](../desktop/README.md): install Python for Windows, then run `build.ps1 -Shortcut` in PowerShell.
It builds one `AICorp.exe` (about 13 MB), copies it to `%LOCALAPPDATA%\AICorp` and puts a shortcut on the desktop.
The app is not code-signed, so SmartScreen may warn once: *More info* -> *Run anyway*.

## 3. First start

The app checks WSL, finds the AICorp folder, and runs the [doctor](DOCTOR.md) inside Linux. Problems are listed with the exact step;
anything that needs administrator rights, `sudo`, a restart or a login is yours to do (the app never runs those itself).
Press *Recheck* after each step. *Copy AI prompt* gives you text for ChatGPT or Claude that contains no names, paths or secrets.
When everything is in place, *Start AICorp* starts Linux if it was off, then the programs, then shows the control panel.

## 4. Using it

Control panel and dashboard are in the same window (Control / Dashboard / Split). **Exit** (or the window's X button) asks once, then
stops the engine, the watchdog and the dashboard in order and closes the window. If the app is killed, the system inside Linux shuts
itself down.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Windows cannot reach Linux on localhost" | in `%UserProfile%\.wslconfig` do not set `localhostForwarding=false`; run `wsl --shutdown`; start again |
| "Already running outside the control panel" | you started the programs by hand on the same data folder; stop them or use another data folder in Settings |
| Window stays on the check screen | read the red items; fix; *Recheck* |
| WSL needs a restart | restart Windows; start the app again |

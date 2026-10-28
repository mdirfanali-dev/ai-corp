# Doctor: check your setup

The doctor looks at your computer and tells you what is missing and what to do next. It **only looks**: it never installs
anything, never changes a file and never runs a command with administrator rights.

```bash
python -m aicorp.doctor            # readable report
python -m aicorp.doctor --json     # for programs; also reports the dashboard port that would be used
python -m aicorp.doctor --prompt   # a ready-to-paste message for ChatGPT or Claude (see below)
```

The exit code is `0` when nothing is a problem (warnings are allowed) and `1` otherwise.

## What it checks

| Check | OK means | If not |
|---|---|---|
| Operating system | Linux, or Linux inside WSL | macOS and native Linux are marked untested; native Windows is only a starting point (the system runs in WSL2) |
| WSL2 | On Windows: WSL and a Linux distribution are installed. Inside WSL: it is version 2 | install with `wsl --install -d Ubuntu` (administrator rights and a restart) |
| Python | 3.12 or newer | install Python 3.12+ (needs sudo on Ubuntu) |
| Virtual environment | you are inside one | one-click fix: create `.venv` |
| AICorp and libraries | FastAPI, Jinja2, Uvicorn, python-multipart are importable | one-click fix: `pip install -e .` |
| Chromium | Playwright's Chromium exists (only the browser worker needs it) | one-click fix: `python -m playwright install chromium` |
| Worker user | the low-privilege user that runs the AI agents exists and passwordless `sudo -u` to it works | run `scripts/setup_worker_user.sh` with sudo |
| Claude Code | starts as the worker user and a login file exists | install it for the worker user, log in once yourself |
| Secrets file | `config/secrets.env` exists and only you can read it | copy the example, fill it in yourself, `chmod 600` |
| Dashboard port | port 8765 is free; if not, the next free port is chosen and reported | close the program that uses the ports |
| Disk and memory | at least 5 GB free disk and 4 GB RAM | free space; fewer parallel agents |

The disk and memory limits are rough guesses (assumptions), not measured requirements.
"Claude Code logged in" means a login file is present; the doctor cannot tell whether the login is still valid.

## Steps that need you

Anything that needs administrator rights, `sudo`, a restart or a browser login is **explained, never done for you**:
installing WSL, creating the worker user, installing Python, logging in to Claude. Do the step, then run the doctor again
(the *Recheck* button in the control panel does the same).

## Help prompt

`--prompt` prints a message you can paste into ChatGPT or Claude so they guide you step by step. It contains the failed
checks and your system type, and nothing personal: e-mail addresses, absolute paths, user names and token-like strings are
removed before it is shown. Read it before you paste it anyway.

## One-click fixes

Only three exist, all inside the project folder: create the virtual environment, install the libraries, install
Chromium. They never use `sudo`, never touch WSL and never change system settings.

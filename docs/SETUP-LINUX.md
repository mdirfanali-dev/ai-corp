# Setup on Linux and Windows + WSL2

Status: **Windows + WSL2 tested**, native Linux untested (see the [README](../README.md#platform-status)). The steps are the same;
the notes for WSL2 are marked.

## 1. Install

```bash
sudo apt install python3 python3-venv git        # Debian/Ubuntu; use your distribution's package manager
git clone https://github.com/mdirfanali-dev/ai-corp.git && cd ai-corp
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"                          # [dev] adds pytest; drop it if you only want to run AICorp
playwright install chromium                      # only for the browser worker
pytest -q                                        # optional: the suite uses a temporary folder, never your data
```

Pick a data folder (it will hold your company; keep it out of the repository) and export it in every terminal you use:

```bash
export AICORP_HOME="$HOME/aicorp-data"
```

The first start creates `company/ skills/ memory/ artifacts/ workspaces/ config/ database/` inside it.

## 2. Try it without AI

```bash
python -m aicorp.web                      # terminal 1: http://127.0.0.1:8765
python -m aicorp.engine --runner fake     # terminal 2: simulated work
```

## 3. Real work with Claude Code (low-privilege worker user)

Agents must not run as you. AICorp expects a separate account that cannot read your files:

```bash
sudo bash scripts/setup_worker_user.sh --owner "$USER" --root "$AICORP_HOME"
```

The script creates the user `aicorp-worker` (no password login), locks the data folder down, lets only *you* run commands as that
user (a file in `/etc/sudoers.d`), and runs a self-test. It never deletes anything. Read it before you run it; it asks for `sudo`.

Then install Claude Code **for that user** and log in with your own subscription:

```bash
sudo -u aicorp-worker -H bash        # follow Anthropic's install instructions, then run: claude   and log in
```

Start the engine pointing at it:

```bash
export AICORP_WORKER_USER=aicorp-worker
export AICORP_CLAUDE_BIN=/home/aicorp-worker/.local/bin/claude      # wherever `which claude` says for that user
python -m aicorp.engine --runner claude
```

Without a worker user the engine refuses to start unless you set `AICORP_ALLOW_SAME_USER=1` (agents then run as you, with weaker isolation).

## 4. Alerts, chat and the watchdog (optional)

```bash
cp config/secrets.env.example "$AICORP_HOME/config/secrets.env"   # then edit it yourself
chmod 600 "$AICORP_HOME/config/secrets.env"
python -m aicorp.watchdog
```

You need a Telegram bot token (from @BotFather) and, for e-mail, a Gmail address with 2-step verification, an app password and IMAP
enabled. Never paste these anywhere except that file (or the dashboard's Settings page). Without the watchdog nothing is sent.

## WSL2 notes

* Windows can limit WSL's memory in `%UserProfile%\.wslconfig` (`[wsl2]` → `memory=...`); the engine's memory guard (default 80 %) keeps
  workers from using all of it.
* The browser worker needs a window for CAPTCHA/login hand-offs; WSLg (Windows 11) provides it. Without a display it blocks the task
  with a message instead.
* Nothing runs while Windows sleeps or WSL is shut down; on wake the Advisor catches up and writes a "while you were away" report.

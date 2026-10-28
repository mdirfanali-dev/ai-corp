# Configuration

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AICORP_HOME` | the project folder | the data folder (database, company files, workspaces, `config/`) |
| `AICORP_PORT` | `8765` | dashboard port (always bound to `127.0.0.1`) |
| `AICORP_ALLOWED_HOSTS` | `localhost, 127.0.0.1` | extra `Host` header values the dashboard accepts |
| `AICORP_WORKER_USER` | none | low-privilege OS user that runs Claude Code |
| `AICORP_CLAUDE_BIN` | `claude` | path to that user's Claude Code executable |
| `AICORP_ALLOW_SAME_USER` | off | `1` = run agents as yourself (weaker isolation) |
| `AICORP_CAP` | `4` | engine: maximum parallel tasks |
| `AICORP_MAX_TURNS` / `AICORP_RUN_TIMEOUT` | `12` / `600` | fall-back turn and time limits per run (see *Run limits* below) |
| `AICORP_TRANSPORT_DIR` | none | testing: write alerts/chat to files instead of Telegram/e-mail |

## Engine options

`python -m aicorp.engine --runner claude|fake [--cap N] [--ram-limit 0.8] [--lease SECONDS] [--browser on|off] [--alerts on|off]`.
The browser worker is on by default with `--runner claude`.

## Secrets (`config/secrets.env`, mode 600)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ALERT_EMAIL_TO`. See `config/secrets.env.example`.
Secrets are never written to logs, pages, notifications or backups; known secret values are scrubbed from everything that is stored.

## In the dashboard

* **Settings** — alert routing per severity, rate limit, channel status and test buttons, chat status, browser worker.
* **Advisor → Schedule** — jobs (`every 15m`, `daily 08:00`, `weekly mon 09:00`, local time), monitor on/off, quiet hours, reminder limits.
* **Usage → Run limits** — model, maximum turns and time limit per risk tier; an employee can override them on its own page.
* **Skills** — add, vet and scope skills (`skills/<name>/SKILL.md`).
* **Maintenance** — health check, trash.

## Data folder layout

```
company/      records mirrored as files (employees, roles, policies)
skills/       SKILL.md folders
memory/       reports, decisions, meeting minutes, run logs
artifacts/    outputs kept by tasks
workspaces/   one folder per employee (the only place an agent may write)
config/       secrets.env (private)
database/     company.sqlite (+ WAL files)
backups/      nightly backups (the last 7)
runtime/      generated: guard hook, browser profiles and sessions (never backed up)
.trash/       nothing is deleted outright; files wait here 30 days
```

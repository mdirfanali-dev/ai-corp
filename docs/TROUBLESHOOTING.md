# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No worker user configured` when starting the engine | set `AICORP_WORKER_USER` (see [setup](SETUP-LINUX.md)) or, knowingly, `AICORP_ALLOW_SAME_USER=1`; or use `--runner fake` to try it |
| Dashboard says "Engine stopped" | start `python -m aicorp.engine ...`; unfinished tasks are recovered automatically |
| No Telegram/e-mail alerts | the watchdog must be running; check Settings → channel status and the test buttons; Gmail needs IMAP enabled and an app password |
| Telegram error 409 / "already polling" | two watchdogs are using the same bot; stop one |
| A task is blocked "CAPTCHA" or "login" | a browser window was opened for you: finish it, then press Resume (without a display the task says so) |
| `playwright` errors about a missing browser | `playwright install chromium` |
| Port already in use | `python -m aicorp.web --port 8800` |
| Dashboard unreachable from another machine | by design: it listens on `127.0.0.1` only |
| "database is locked" | another program holds a write lock for long; the engine waits up to 30 s and retries |
| Disk almost full | the engine pauses new work and alerts you; free space and it continues |
| Doubt about the logs | Maintenance → Run maintenance now (integrity check and the full audit-chain check) |
| Everything stopped at once | the kill switch is engaged (`STOP` file in the data folder); release it from the dashboard |

Still stuck? Open an issue with your operating system, Python version and the steps (remove personal data; never paste secrets).

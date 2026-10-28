# Security model

AICorp lets AI agents act on your computer, so protection is layered and fail-closed. This page says what each layer does and,
just as important, what it does **not** do.

## Layers

| # | Layer | What it does |
|---|---|---|
| 1 | Risk tiers | **0** read only · **1** draft in the employee's own folder · **2** local execution inside the workspace · **3** anything external: the agent prepares a *draft*, you approve and send it · **4** money, legal, accounts, credentials: never started by an agent. Each tier gets a fixed tool list |
| 2 | Tool hook | Every tool call of Claude Code passes a hook that decides *deny* unless the call is explicitly allowed: paths must stay in the employee's workspace and avoid protected names (`secrets.env`, `.env`, `.ssh`, `.git`, keys, ...), shell commands come from a short allow-list without redirects or pipes to unknown programs, URLs must be public http(s) |
| 3 | OS separation | Agents run as a separate low-privilege user that cannot read your home folder, the database or `config/` |
| 4 | Audit | Tool calls are read back from Claude's output stream and compared with what the hook allowed; denials are logged and shown |
| 5 | Input guard | Task text and any text that comes from files, pages, messages or other workers is data; text that tries to override rules, reveal secrets or run destructive commands is blocked or replaced |
| 6 | You | Tier 3 needs your approval; the kill switch (`STOP` file / red button) stops everything; nothing is hard-deleted |

Other protections: secrets are scrubbed from stored text; the dashboard checks the `Host` header, the `Origin` and a CSRF token on every
state-changing request and never changes state on a GET; the audit log is append-only and hash-chained; Telegram and e-mail accept
only the owner (and e-mail only when the provider vouches for the sender); the Advisor can read everything but cannot execute, assign or approve.

## What it does not protect against

* Someone who already controls your OS account, or has physical access to the computer.
* Running agents without a worker user (`AICORP_ALLOW_SAME_USER=1`): layers 2, 4, 5 still apply, but a bug in them is no longer contained by the OS.
* The browser worker runs as **your** user (it needs your desktop for CAPTCHA/login hand-offs). Its profile and evidence are protected from
  agents, but it is less isolated than the Claude worker.
* Mistakes in what an agent *writes* (a wrong draft); that is what approvals are for.
* Weaknesses in Claude Code, Chromium, SQLite or the other dependencies.
* The input guard and the Advisor's challenge notes are rules, not understanding: they will miss things.

## Reporting

Please report vulnerabilities privately, see [SECURITY.md](../SECURITY.md).

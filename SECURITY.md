# Security policy

AICorp runs on your own computer, binds to `127.0.0.1` only, and starts programs (Claude Code, a browser) on your behalf, so
security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Include what you found, how to reproduce it, the version or commit, and your operating system.

This is a small, best-effort project: there is no service-level agreement. You can expect an acknowledgement as soon as a
maintainer sees the report, a fix or a clear answer afterwards, and credit in the release notes if you want it.

## Supported versions

Only the latest commit on `main` and the most recent tagged release (if any) receive fixes.

## What is in scope

* Bypasses of the guardrails: reading or writing protected files, getting a secret out, escaping the agent's workspace,
  running tools or commands that the risk tier forbids, skipping the approval step for tier 3, starting tier 4 work.
* Dashboard problems: missing CSRF or origin checks, cross-site scripting, anything that changes state on a GET request.
* Leaks of secrets (`config/secrets.env` values) into logs, pages, backups, notifications or the browser worker's evidence.
* Tampering with the audit log that the hash chain fails to detect.
* Unsafe handling of inbound Telegram or e-mail messages (sender checks, injection).

## Out of scope

* Attacks that need an already-compromised user account or physical access to the computer.
* Findings that require disabling the documented protections (for example running agents as the same OS user without
  `AICORP_ALLOW_SAME_USER`, or editing the database by hand).
* Weaknesses of third-party services (Claude Code, Telegram, Gmail, websites the browser worker visits).
* Denial of service by someone who can already run commands as you.

See [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) for how the protections work and what they do not cover.

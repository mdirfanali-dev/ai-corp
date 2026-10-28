# Browser sites: rules, risks and terms

The browser worker (an employee with the `browser` tool) follows a JSON plan written in the task description.
There is no AI inside AICorp: it opens pages, copies text, takes screenshots and saves downloads as evidence.

## Rules that never change
- It never tries to get past a CAPTCHA, a login, a paywall or a rate limit. It stops, shows a browser window and waits for you.
- It never types passwords, codes or card details. You log in yourself, in the window it leaves open.
- It only visits public http/https sites on the standard ports. Local addresses, internal hosts, `file:` and addresses
  with embedded credentials are refused (also after redirects).
- It uses its own browser profile under `runtime/browser/profiles/<employee>`, never your personal profile. Logins you make
  there stay there (and are not part of backups).
- Everything it copies from a page is **untrusted data**: files start with an "UNTRUSTED WEB CONTENT" banner and are never
  treated as instructions by other workers.
- Downloads go to a quarantine folder first. Only documents, text and images whose content matches their type are
  moved to the employee's workspace; programs and scripts never are.
- At most 2 browsers are open at the same time (hand-off windows count).

## Terms of service and risk (your responsibility)
Websites differ in what they allow automated tools to do, and some forbid it. **Check the terms of each site yourself
before you point a task at it.** This file is the place to write down what you found:

| Site | Terms checked on | Automation allowed? | Notes |
|------|------------------|---------------------|-------|
| _(none yet)_ | | | |

## AI websites (optional)
`browser-sites.json` can describe a chat-style AI website so a plan can use the step `{"ask": {"site": "name", "prompt": "..."}}`.
Nothing is switched on by default (`"enabled": false`). Selectors differ per site and change when the site changes, so an adapter
can stop working at any time; the task then ends as "unexpected page" and waits for you. Claude work itself should go through
Claude Code, not through a website.

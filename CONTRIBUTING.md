# Contributing to AICorp

Thank you for helping. This is a small project maintained on a best-effort basis; a pull request may wait a while for review.

## Ground rules

* **Pull requests only.** Nothing is committed straight to `main`. Fork the repository, create a branch, open a pull request.
* **Sign off every commit** (Developer Certificate of Origin, https://developercertificate.org): `git commit -s` adds the
  `Signed-off-by:` line. By signing off you confirm you have the right to submit the work under Apache-2.0.
* **Keep the scope.** AICorp deliberately has *no built-in AI, no API keys, no cloud inference*, and it never solves CAPTCHAs,
  takes autonomous money/legal actions or lets agents talk to each other freely. Proposals that change this will be declined;
  open an issue first if you are unsure.
* **Tests and honesty.** New behaviour needs tests. Say plainly what you tested and on which operating system.
* **No personal data, secrets, absolute paths or e-mail addresses** in code, tests, docs or commit messages. A CI check enforces this.
* Be kind: see the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone <your fork> && cd ai-corp
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium            # only needed for the browser tests
pytest -q                              # opt-in live tests are skipped automatically
ruff check src scripts tests
```

Tests use a temporary data folder and never touch your real company data. Tests that need a real Claude login, a Telegram bot
or a desktop window are opt-in (see the docstring of each file).

## Pull request checklist

* Small and focused; explain *why*, not only *what*.
* `pytest` and `ruff check` pass.
* Docs updated when behaviour changes (`docs/`, `README.md`).
* Changes to guardrails, permissions, the sudo/worker-user script, dependencies or CI get extra scrutiny: say so in the description.

## How pull requests are handled

Pull request content is treated as **untrusted**. CI runs without access to any secret; a maintainer reads the diff before
anything is run locally, and never runs a contributed change with real credentials in reach.

# Code signing policy

**Status: not active yet.** The Windows app (`AICorp.exe`) is currently published **unsigned** as a beta. This page describes how
signing will work once it is set up.

Planned: free code signing provided by [SignPath.io](https://signpath.io), certificate by [SignPath Foundation](https://signpath.org).
The publisher shown by Windows will then be "SignPath Foundation".

## What is signed
Only `AICorp.exe` built by the GitHub Actions workflow `release-windows.yml` from a tagged commit of this repository. Nothing built on a
developer's computer is signed or published.

## Roles
| Role | Who |
|---|---|
| Maintainer, committer, reviewer and release approver | the repository owner, [mdirfanali-dev](https://github.com/mdirfanali-dev) |

Outside contributions are merged by the maintainer; a release is created only when the maintainer pushes a release tag.

## Privacy
AICorp runs on your computer. The Windows app starts the Linux side through `wsl.exe` and shows a local address (`127.0.0.1`);
it does not send data to any server of the project. Anything you connect yourself (Telegram, e-mail, Claude) is described in
the [security model](SECURITY-MODEL.md).

## Reporting
Security problems: see [SECURITY.md](../SECURITY.md).

# Security

This server holds a full-account Discord session. Treat any change to it as a change
to code that can act as your account.

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button on the Security
tab, not in a public issue.

## How installations stay safe from this public repo

- `main` only changes through reviewed pull requests with passing tests.
- Installations never track a branch. `scripts/deploy-pinned.ps1` checks out one exact
  commit, which must already be on `main`, and refuses a tag that points anywhere else.
- Dependencies are installed with `pip --require-hashes` from `requirements-local.lock`,
  so a hijacked package release cannot be pulled in.
- CI runs only on GitHub-hosted runners, with read-only permissions and actions pinned
  by commit SHA.
- Credentials live only in the local DPAPI vault (`credentials.dpapi`), which is
  git-ignored. Never commit tokens, logs, or `.env` files.

Before deploying a new commit, read its diff, especially `local_safety.py`, the tool
registry, and anything that touches credentials or outbound requests.

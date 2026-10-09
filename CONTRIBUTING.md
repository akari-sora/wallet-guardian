# Contributing

Wallet Guardian is licensed AGPL-3.0-only. See `wallet-guardian/LICENSE`. Contributions are inbound = outbound: a change you submit is licensed under AGPL-3.0-only, the same license as the project. By submitting a change you agree to those terms. Do not contribute code you cannot license that way.

The monitor is read-only. Do not add signing, transaction construction, broadcast, approve, revoke, or claim. Do not add a seed phrase, private key, or keystore path. Failed reads stay gaps (`unknown`, `unsupported`, `not_observable`, `blocked`, `credential_missing`). They are not zero and not complete.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/). From a checkout:

```bash
cd wallet-guardian
uv sync --frozen --group dev
uv run pytest -q
```

`uv run pytest -q` is the local gate for the monitor. A green local run is not a CI pass on a preview commit. The release-evidence collector stays `NOT_RUN` until a real gate directory is recorded. See `wallet-guardian/docs/ci.md`. Do not weaken or delete an existing test to make a change fit.

The Action Client under `wallet-guardian/action-client/` is a separate tree. Do not add monitor dependencies there, and do not change it as part of a monitor-only fix unless the issue says so.

## Fixtures and secrets

Fixtures use synthetic addresses such as the one in `wallet-guardian/examples/answers.example.json`, documented as synthetic. Do not put a real watched address, an API key, a keyed RPC URL, a notification recipient, or a private state file in the repo. Do not read or commit `private/`, `data/`, or `evidence/`. Config samples store environment-variable names only.

Tests stay offline. Use fixtures or a fake transport. An unmocked network call must fail the test.

## Branches and merge requests

Work on a branch. Open a GitLab merge request into the project's default branch. Describe what changed and how you ran `uv run pytest -q`. Fill a bug or feature issue from `.gitlab/issue_templates/` when that matches the work. Do not force-push the default branch. Do not commit secrets.

Issue templates ask reporters not to paste keys or real addresses. Keep the same rule in review comments and in logs you attach.

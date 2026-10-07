# Stack integration evidence

Suites below ran on MR !4 at `49df9e13bd1afdfbe7f6f6cc345b06907e39523d` (`cursor/p2-lista-reread-1a88`). This file is the record of that run. It does not change the client, the gate, or the approval registry.

MR: https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/4

## What this checkout contains

- !1 `223fbe391f3c809aec67a8affc3ea4657e73480e` is an ancestor.
- !3 `02e0382404e8b0d4994069dcd19b4ab52b63fae2` is an ancestor. The P2 commits sit on that tip.
- !2’s open tip `c5771481af7f549c21da058dd8283a7c6582032d` is not an ancestor. The merge-base with this checkout is `9ceac95cc847c941bac1662a91dfd02b18c39b44`. The missing commit is `c5771481` (`fix: stop stale private reads and unsigned SIWE decoys`). This run did not merge it.

## Ran

Local toolchain: uv 0.12.23, CPython 3.12.3, Node v22.14.0, npm 10.9.7. No `VITE_GUARDIAN_MAINNET_SEND`.

From `wallet-guardian/`:

- `uv sync --frozen --group dev`
- `uv run --frozen pytest -q` — 221 passed, 8.57s

From `wallet-guardian/action-client/` after `npm ci --ignore-scripts --no-audit --no-fund` (17 packages):

- `npm run test:api` — 97 passed, 0 failed, 0 skipped
- `npm run test:validation` — 16 passed, 0 failed, 0 skipped
- `npm run test:canonical` — exit 0, printed `canonical ok` (2 assertions)
- `npm run test:gate` — exit 0, printed `gate ok` (37 assertions)
- `npm run build` — `tsc --noEmit` and vite build succeeded

## Blocked

GitLab pipeline [2922129366](https://gitlab.com/aa22396584/wallet-guardian/-/pipelines/2922129366) for this SHA failed before either job ran:

- `python-tests` job 16998765389 — `ci_quota_exceeded`
- `client-tests` job 16998765390 — `ci_quota_exceeded`

Those jobs did not execute the suites. The counts above are from the local run.

## Skipped

Real wallets, mainnet send, a live BSC or EIP-1898 probe, Mode B/C, merging !1–!4, and the Pages job. Pages runs only on `main` and was not deployed.

# Reconcile MR !1 onto main

Date: 2026-10-07.
Source before merge: `ab3702f7` on `fix/review-state-and-isolation`.
Merged target: `a7a79e08` (`origin/main`).
This note records conflict choices. It does not mark the branch mainnet-ready.

MR !1 stays Draft. `VITE_GUARDIAN_MAINNET_SEND` is not set. No deploy was run. No wallet send was opened.

## Conflict files

Git reported two content conflicts. `src/guardian/cli.py` and `src/guardian/service.py` auto-merged; the result was checked rather than taken on faith.

### `.gitlab-ci.yml` (add/add)

Kept MR !1's locked jobs: `uv sync --frozen --group dev` and `npm ci --ignore-scripts` plus canonical, validation, and build. Added `npm run test:gate`. Kept main's `pages` job, still limited to `CI_COMMIT_BRANCH == main`, and made it fail if `VITE_GUARDIAN_MAINNET_SEND=true`. Did not replace the frozen test jobs with main's `uv sync --dev`.

### `action-client/src/main.ts`

Kept MR !1's preview checks: `validatePlan`, honest amount labels, fixed preview heading, bounded JSON, redirect refusal, 1 MiB cap, stale-load generation, duplicate `ref` rejection, and secret screening.

Kept main's confirm-gate module, EIP-5792 capability read (`wallet_getCapabilities` only), Mode C presented as unsupported, `.env.local` / Etherscan loading (those landed in Python, not this file), and `VITE_GUARDIAN_MAINNET_SEND` defaulting off.

Did not keep main's send path. `eth_sendTransaction` and `wallet_sendCalls` are not called. Task `preflight_ok` is not passed into the send decision. `clientConfirmDecision` forces `preflightOk: false` because this page does not reread chain state.

## Send gate (R9)

`confirm_gate` / `confirmDecision` still separate the button from the mainnet flag, and `agent_may_sign` stays false. Defaults now fail closed:

- `wallet_request_allowed` must be exactly true, otherwise reason `wallet_request_denied`
- simulation must be explicitly complete, otherwise `simulation_incomplete`
- block reasons deny send (`plan_blocked`)
- Mode C still returns `mode_c_unsupported`

`simulationAllowsSend` requires `effects_checked`, status `PASS`, and `usable_as_live_evidence`, and rejects state overrides. `INCOMPLETE` does not pass. The preview validator still rejects a plan that claims a successful simulation or `wallet_request_allowed: true`, so a rendered plan cannot satisfy the send predicate.

A pure call can still return `send_allowed: true` only when the caller passes every allow explicitly. The action client does not do that, and it does not broadcast. Turning on the Vite flag is not this MR.

## Auto-merged files that were kept from both sides

- `cli.py`: tenant-scoped reads from MR !1, plus `load_local_env()` at startup from main.
- `service.py`: snapshot/alert/tenant work from MR !1, plus Etherscan V2 key probe, `.env.local` loading, `agent_may_sign: false`, and the mainnet-send env report from main.
- `workbench.py` came from main (MR !1 had not edited it): Mode C ignores a caller-supplied enforcer because `ON_CHAIN_PERMISSION_ENFORCER` is none. `preflight_lista` can report `preflight_ok` after a state reread and still sets `continue_to_wallet: false`.
- `security/ssrf.py`: kept the `api.etherscan.io` exception for `198.18.0.0/15`. Other hosts stay blocked.
- Deploy docs, `vercel.json`, and `config/env.example` (`GUARDIAN_MAINNET_SEND=false`) came from main unchanged.

## Validation on the merged tree

Python 3.12.3, uv 0.12.23, Node v22.14.0. No wallet, API key, or mainnet send flag was used.

```sh
cd wallet-guardian
uv sync --frozen --group dev
uv run --frozen python -c "import eth_abi, eth_utils"
uv run --frozen pytest -q
```

`pytest -q`: 221 passed, exit 0. No skips in that summary.

```sh
cd wallet-guardian/action-client
npm ci --ignore-scripts --no-audit --no-fund
npm run test:canonical
npm run test:validation
npm run test:gate
npm run build
```

`test:canonical`: `canonical ok`. `test:validation`: 16 passed, 0 failed. `test:gate`: `gate ok`. `npm run build` is `tsc --noEmit && vite build`, exit 0. The production bundle does not contain `eth_sendTransaction` or `wallet_sendCalls`.

A local Chrome pass loaded the preview, including a task whose JSON set `preflight_ok` true. The confirm button stayed disabled. Forcing the click refused with the plan denial and did not call the wallet. A narrow viewport capability query called only `wallet_getCapabilities`.

The already published Cloudflare Pages origin returned HTTP 200. Its bundle is the pre-merge client: a forged `preflight_ok` task enabled the confirm button, and the click stopped on the compiled-off mainnet flag without calling `ethereum.request`. That bundle still contains `eth_sendTransaction` and `wallet_sendCalls`. This reconcile does not replace it.

## Not in this MR

Authenticated private task retrieval, a trusted client-owned execution reread, exact calldata decoding beyond the existing selector helper, awaiting a wallet submission, and receipt/replacement persistence remain later work. Mode C stays unsupported. Do not treat a static site or a green test run as mainnet authorization.

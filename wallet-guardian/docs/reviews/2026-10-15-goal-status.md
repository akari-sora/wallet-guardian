# Goal status for the 2026-10-14 review (2026-10-15)

Handoff for the packages in [2026-10-14-review-summary.md](2026-10-14-review-summary.md). `main` at the time of writing is `a84f7caa258989b27f0892273dd50cfbadc0e223` (`Merge !87 into main`). SHAs below are the merge commits on `main` unless noted.

This file records what landed and what did not. It does not say the monitor covers all chains, that it is production ready, or that a 72-hour shadow was verified.

The 2026-10-14 review itself is !60, merge `c25892563a5a9149e23f33290790028a8caf6e81`.

## Packages

| Package | What landed | MR | Merge SHA |
| --- | --- | --- | --- |
| 0-1 | Unalerted leaf candidates can be replayed, and `run-once` takes a lock. This is not a measured 72-hour run. | [!61](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/61) | `e9139c4ccc16b0265a9574f73a777c631f7b05d6` |
| 0-2 | A chain failure, or an unreadable news or price file, stays a gap for that input. It does not zero the other chains or mark the round complete. | [!62](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/62) | `8aae0ddceff0fd6398b5b2a4c4b562de001af8a6` |
| 0-3 | A log source that refuses an address-less `eth_getLogs` fails over to the next configured source. Any other log failure stays a gap. There is no family-RPC fallback for `eth_getLogs`. | [!63](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/63) | `b75fce12e82f21d93094d4c54cdf4d54dcfdcfd2` |
| 0-4 | `init` writes native-unit floors and does not advance a baseline it could not judge. !68 (`7b0776312bf5054acb44d178a2a8bce9e0bde93a`) stops writing a Solana floor that cannot alert. | [!65](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/65) | `ffb7615cf2a917d175521756ec9cd0c923f079e0` |
| 0-5 | CoinGecko contract prices, and an unvalued large token move when a price is missing. Offline tests only. No live price-source check is recorded here. | [!70](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/70) | `94fe67fd6fe26965f77b03c43e9e516e3d8bc445` |
| 0-6 | Local dead-man switch, non-zero exit when degraded, and outbox ack. No external ping, webhook, or paid plan. | [!67](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/67) | `4b65330f2e76b2097176cf114642e5e97aabb073` |
| 1-0 | Operator step, not a code change. The on-chain schedule has not been moved off Grok, and it has not been changed to all day. !74 only prints LaunchAgents. | — | — |
| 1-1 | Read-only `guardian balance-probe`. Log routing on `main` is !81. !80 merged into `cursor/log-sources-mask-881f` (`2582309d4110`) and did not reach `main`. A quiet Multicall3 round stays at 5 HTTP. A policy refusal of the first log source is 6 HTTP and is not capped. | [!79](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/79), [!81](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/81) | `44669fdae3ecbc4cb851bb9ea8708f487dc5846d`, `8d4a6a564466d144798933f07935cfccdedb3a5b` |
| 1-2 | NodeReal log URLs are masked. BSC, OP, and Base log sources were added, with the !63 failover. No live log-source check is recorded here. | [!75](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/75) | `c20bc8afc400535fd3b55b944c5039fa90eab7ed` |
| 1-3 | Optimism internal transfers can be read through Blockscout v2. This handoff has no live OP measurement. Base native external transactions and internal transfers are still not claimed. | [!78](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/78) | `a4da6c48424dc8769144ac287e5202350746d9e0` |
| 1-4 | News fetch is isolated from the on-chain commit. A news failure does not become a successful chain round. | [!77](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/77) | `4247c0b9c75fa1d0952bf8c6ee4556d140fc0472` |
| 1-5 | The first successful read sets the baseline. Later recovery does not replay that history as new alerts. `compare-legacy` was not run. | [!76](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/76) | `2a57f0aad9f2fcc65cbe6fec1f769fbf260e47e2` |
| 1-6 | Three LaunchAgent plists can be printed. They were not loaded. | [!74](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/74) | `9d83d0ffb6a9fcd7e0bdbc05fd4b49ac22b91d1d` |
| 1-7 | Shadow detection latency and an outbox-versus-threshold check are recorded in code. A 72-hour shadow was not run. | [!73](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/73) | `05735c43838a31fcf9047365ad59a13d808f55c6` |
| Phase-2 docs | Honest limits for merged behaviour, and wording that a key does not unlock DeBank or Zerion. This does not close every item 2-1 through 2-6 in the review summary. | [!71](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/71), [!72](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/72) | `827835133616c55ffe0c28364f325500c4cc5c27`, `0e0a952def656b000f990129082e45aa98e2a7d7` |
| WP-1 | Artifact inventory without a deploy. `action-client/deploy/releases.json` `releases` is still `[]`. Old hash deployments were not deleted. `vercel.json` and the GitLab `pages` job were not deleted. | [!64](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/64) | `32d7eac89117e80f7aa3ae6683840737b7f1c9d9` |
| WP-2 | Private-task routes come from one origin policy. `issue_task_link` does not issue a caller origin. `fixed_https_origin` stays false. `data_path` stays in the policy. | [!82](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/82) | `47197c4c1264727a6b2631c85510cc51a719c98b` |
| WP-3 | Send construction stays out of the default send-off bundle. !69 makes the send-off artifact test build `dist` itself. CI does not build a send variant beside the default build. | [!66](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/66), [!69](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/69) | `44c3e3512f205d051da23a2aac9d9776b08184c6`, `ef3c19c3fb76b76ac25f64ba77182674d71e34e7` |
| WP-4 | Fail-closed session and membership revocation. The 12h / 30min / 15min constants are suggestions, not a D-9 decision. On `a84f7ca` a switch still revokes, and the sender tab also applies its own logout broadcast. That banner bug is open in !88 and is not on `main`. | [!85](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/85) | `76ad84299c7ea6fb510cac73a689ad0d76423197` |
| WP-5 | Client-owned evidence checks. Empty Lista `deployment_approvals` stay unapproved. `effectsCheck` is a dry-run `eth_call`, not a trace. ADR 0001 was not revised. | [!83](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/83) | `af7d0b43878316e7da9f7ed298dba7076e0b81e7` |
| WP-6 | One local outcome per task. A transaction hash is not completion. Logout does not clear a local pending record. Nothing is auto-resent. `requestUserSend` on the default page still throws. The page does not call `confirmModeAOutcome`. | [!86](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/86) | `5c75a9ca63a0e8fb0ea91bb4f7aa07ec751c6375` |
| WP-7 | Fail-closed Mode A send variant. `vite.config.ts` calls the gate itself when `WG_SEND_VARIANT=mode_a` and throws if it fails. Output is `dist-mode-a`. No approved inventory entry was added. No successful send-variant build was run, and nothing was deployed. WalletConnect methods stay `["personal_sign"]`. | [!84](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/84) | `95a94f5a40206f2e2cd1887b021ecccdc47d2531` |

WP-8 (real wallet) was not started.

CI follow-up, not one of the review packages: !87, merge `a84f7caa258989b27f0892273dd50cfbadc0e223`. `python-tests` timeout stays 20 minutes. The job prints durations, uses a 60-second pytest timeout, writes JUnit as it goes, and refuses live DNS and non-loopback connects. `pages` on `main` is `when: manual` and `allow_failure: true`. The job was not deleted. `test_client_gate_matches_python` does not skip and does not spawn Node. It checks that `client-tests` runs `npm run test:gate` and that `action-client/src/gate.test.mjs` exists.

## What was verified

- Local pytest during the package work. In this environment the only product-unrelated failure is `tests/install/test_installable_monitor.py::test_mcm_share_s4_03_pinned_install_uv_pip` because `python -m ensurepip` is missing. A local run is not a CI pass.
- The operator reports that self-hosted CI on runner `wg-9700x`, for `main` `a84f7ca` after !84 and !87, is **800 passed, 0 skipped**. That report is the CI result from !87 onward. This handoff did not re-run that runner. Older GitLab.com shared-runner jobs that ended in `ci_quota_exceeded` are not a pass.
- `client-tests` on that CI runs the Node unit scripts and `npm run build`. It does not run `test:wallets:artifact`.

## What was not verified

- No live keys (Blockscout PRO, Alchemy, NodeReal, or any other).
- No real wallet, and no WalletConnect send.
- No 24-hour balance-probe measurement.
- No `compare-legacy` run.
- No live log-source check.
- No 72-hour shadow.

## Decisions still with the operator

The 2026-10-14 Mode A table defines D-1, D-2, D-3, D-4, D-5, D-9, D-10, D-11, and D-12. It does not define D-6, D-7, or D-8. None of the defined items below were decided by these merges.

| ID | Still open |
| --- | --- |
| D-1 | Approve a Lista implementation pin. The approval list is empty, so a re-read cannot pass. |
| D-2 | Whether a client `eth_call` dry-run counts as simulation. ADR 0001 was not revised. |
| D-3 | Whether Mode A v1 may send through WalletConnect. It does not. Methods stay `personal_sign`. |
| D-4 | Whether to add a second RPC. None was added. |
| D-5 | Phase B rehearsal choice (local testnet variant versus not). WP-8 has not started. |
| D-9 | Session absolute TTL, idle timeout, and Mode A `auth_time` window. The 12h / 30min / 15min numbers are suggestions only. |
| D-10 | Whether logout clears a local pending outcome. The code does not clear it. |
| D-11 | Whether to delete `action-client/vercel.json` and the GitLab `pages` job. Both remain. `pages` on `main` is manual and does not publish on merge. |
| D-12 | Scope after a first successful claim. There has been no mainnet send. |
| H-4 | Cloudflare dashboard work: delete old deployments, confirm 2FA and token scope. Not done by an agent. |
| H-6 | Whether synthetic-key negative tests may write the production D1. Not done. |
| H-7 | Separate written authorization for a small mainnet test. Not granted. |
| H-8 | Separate written authorization to deploy a send variant to the production origin. Not granted. |
| Telegram | Delivery stays `outbox_only`. No live Telegram cutover. |
| PyPI | Nothing was uploaded to PyPI. |
| Keys | Blockscout PRO, Alchemy, and NodeReal keys were not used or verified. |
| Address disclosure | Sending monitored addresses to 48club, Tenderly, or dRPC was not approved and was not done as a live check. |
| Mac sleep / schedule | Whether the Mac stays awake, and whether the on-chain schedule moves off Grok to launchd or cron, is still an operator step (1-0). Printed plists were not loaded. |

## Known residuals

- On `a84f7ca`, `npm run test:wallets:artifact` fails `WC-ARTIFACT-01/02 on` and `WA artifact`. A switch revokes, then this tab applies its own logout broadcast and the banner becomes 「其他分頁已登出…」 instead of the `revoked=true` switch text. Other tabs should still log out. [!88](https://gitlab.com/aa22396584/wallet-guardian/-/merge_requests/88) fixes the sender so it ignores its own message. It is not merged.
- `test:wallets:artifact` needs Chrome through `playwright-core`. `client-tests` uses `node:22.16.0-bookworm-slim`, which has no browser, and `playwright-core` does not download one. That is why the job does not run the script. `docs/ci.md` says the same.
- `deploy/releases.json` has no `approved_by_user` mode_a entry. A send-variant build stays refused. The default page does not send.
- Lista deployment approvals are empty.
- Solana has no transaction-delta alerts. !68 does not invent a floor for a path that cannot alert.
- Free endpoints still have the limits in the 2026-10-14 latency review: address-less `eth_getLogs` is not generally available, Base native external and internal history is not a free indexed feed, and public endpoints have no service guarantee.
- `requestUserSend` remains hard-denied. `GUARDIAN_MAINNET_SEND` and `fixed_https_origin` were not flipped. There is no `/rpc` and no client COMPLETE.
- Local pytest here still fails only the `ensurepip` install test. The slim CI image has `ensurepip`. That local failure was not patched out.

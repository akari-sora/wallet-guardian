# Concurrent main update discovered at review close-out

Reconciliation of this addendum is recorded in `2026-10-07-reconcile-main.md`. The findings below are the pre-merge review, not a claim that the branch is mainnet-ready.

This addendum is part of MR !1. It supersedes statements about the current target branch that were made from the original reviewed base.

- Original review base: `61885b98f8fb3b162e667a72849dd3fbd9bd4e93`.
- Target observed at close-out: `a7a79e08c647e784f7616b940558164831e568ac`.
- This review wrote only its own source branch. Another workflow advanced main during the review. No force push, rebase, merge, or deployment of that concurrent work was performed by this reviewer.
- GitLab reports `has_conflicts=true`. Do not merge or resolve with a blanket ours/theirs replacement.

## Reconciliation required

The concurrent changes include an Etherscan-specific synthetic-DNS exception, .env.local loading, a wallet confirmation/send gate, Pages/Vercel configuration, and a Cloudflare Pages deployment record. The review branch and target overlap in at least `.gitlab-ci.yml`, `action-client/src/main.ts`, `src/guardian/service.py`, and `src/guardian/cli.py`.

Preserve independently reviewed environment-loading and target deployment metadata when reconciling. Keep all state/storage/alert regressions from MR !1. Combine CI test gates with deployment policy deliberately rather than silently deleting another workflow's Pages job. Rerun the full merged tree, including the newly added target `test_confirm_flow.py` and gate tests. Original or target-only test counts are not evidence that the merged tree passes.

`main` has already changed mode_c_decision to reject a caller's claimed enforcer when no permission contract exists. The original review report's concern about enabling Mode C from a caller dict applies to the original base, not that updated target implementation.

The new `docs/deploy.md` records a fixed Cloudflare HTTPS origin. That record was read, not independently deployed or verified by this reviewer. It does not establish authenticated task retrieval, wallet execution, or transaction safety. Older README/coverage claims about no fixed origin need reconciliation with that deployment record.

## R9 — P1 release blocker: do not enable the newly added send gate

Inspected target: `a7a79e08c647e784f7616b940558164831e568ac`.
Paths: `wallet-guardian/action-client/src/main.ts` and `gate.mjs`.

The target derives `preflightOk` directly from task JSON (`preflight_ok=true`, `revision_required=false`, `changes=[]`) and combines it with `requires_user_wallet_confirmation` and the build-time `VITE_GUARDIAN_MAINNET_SEND` flag. There is no independent live client RPC revalidation behind those task booleans.

The preceding format checks require `wallet_request_allowed=false`, but the new send decision does not consume that denial, the plan's incomplete simulation, or its block reasons. Therefore a self-described clean task can lead to a wallet request if that build flag is enabled, despite the embedded plan saying wallet requests are disallowed. This is not a claim that the presently deployed default-off build has sent transactions.

Additional checks missing from that send path include current wallet chain/account agreement, current implementation/hash/request identity, a real expiry guard, and verified transaction effects. `listaCallsOnly` checks a selector prefix rather than exactly decoding the supported calldata. The `void ethereum.request(...)` calls also discard asynchronous failures and submission results; synchronous try/catch cannot provide reject/unknown/submitted recovery.

Keep the build flag off. Before enabling it, obtain preflight from a trusted client-owned execution context, respect explicit deny flags and incomplete evidence, decode exactly one allowed action, reread the actual wallet/account/chain and deployment state, await the wallet promise, and persist submission/replacement/receipt states. Add negative tests where task booleans are forged, the plan denies execution, the current chain is wrong, the request index changed, the quote is stale, or wallet submission rejects/returns an ambiguous result. The preview semantic validator in MR !1 is useful, but is not by itself this live execution gate.

## Current review gate

MR !1 remains Draft with two independent blockers: concurrent-target conflicts and missing full-test/build evidence. Its own head pipeline was also quota-blocked before any runner started. Resolve and test the combined code before approving a merge; do not call either branch production-ready because a static website returns HTTP 200.

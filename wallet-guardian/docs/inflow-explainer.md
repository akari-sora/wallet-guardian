# Inflow explainer

A native balance increase is still a `balance_delta`. This page describes a separate revision that tries to attribute one BSC increase to a Lista StakeManager withdrawal. The revision does not change the alert, its severity, its attribution, or any safety label. `funds_marked_safe` is false. Labels stay `unverified`. A match is not a statement that the funds are safe.

Experimental read-only attribution with bounded discovery. A verified request match requires an explicitly approved historical deployment and coherent transaction, receipt, request and checkpoint evidence. Incomplete evidence remains partial or unverified. Explanations do not certify fund safety, establish operator control, guarantee complete attribution, or change the underlying movement alert. No live historical-path qualification is claimed.

There is no live probe. `config/explainers/lista-stakemanager.json` keeps the allowlisted calls and an empty `implementations` list. `implementation_at_execution` stays unverified until a reviewed deployment interval is added. `live_bytecode_verified` is false. The candidate specification is lista-dao/synclub-contracts commit `2a60d77d9d9c7059736385a4425ca38492425c8e`. Synthetic calculation pins live in the test fixture and are not loaded by the packaged manifest.

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `MATCHES_OWN_REQUEST` | One request belongs to the monitored account, the implementation that executed is an explicitly approved deployment, the receipt, request, and checkpoints agree, and the amount matches that deployment's rule. |
| `PARTIAL` | Some of that link is present, and the request, the execution, or the amount is missing, ambiguous, or inconsistent. |
| `NO_MATCH` | The explanation is rejected. The inflow itself is not rejected. |
| `UNVERIFIED` | A required read, the implementation, or the evidence is not available. |
| `NOT_APPLICABLE` | No explainer selected this event. It is not a verdict about the inflow. |

An empty current request array, a truncated log window, pruned logs, or a failed historical read does not produce `NO_MATCH`. `implementation_at_execution_unverified` does not produce `MATCHES_OWN_REQUEST`. A numeric block height is not a substitute for the block hash. The word `latest` is not sent.

## What is recorded

The transaction submitter, the effective caller, the request account, and the payout recipient are separate fields. A third-party submitter can send `claimWithdrawFor`. A BOT role is recorded when the read succeeds. The role is not required for a match, and it is not proof of who operates the wallet.

Amounts are integer base units, stored as decimal strings:

- `requested_slis_amount` is the slisBNB in the request log.
- `burned_slis_amount` stays unset. `requestWithdraw` in the candidate specification transfers slisBNB to the manager. A later aggregate burn is not allocated to this request.
- `request_time_bnb_quote` is the BNB amount stored at request time, when the pinned rule is the stored-BNB rule.
- `contract_expected_claim_amount` is that stored amount, or the legacy integer share `(total_bnb * requested_slis) / total_slis` using floor division.
- `actual_claim_payment` is the amount in `ClaimWithdrawal`.
- `effective_settlement_ratio` is the string `payment/requested`. It is not a float and it is not a historical conversion rate.

Optional conversion quotes may stay `not_queried`. That does not erase a match. A non-zero residual does not erase a match either. The residual is the observed signed delta minus verified native credits to the monitored account. An unrelated inflow stays in the residual. An amount that disagrees with the pinned rule is `PARTIAL`, not `NO_MATCH`. Payment to someone else, or a receipt that is not successful, is `NO_MATCH`.

## Swap-and-pop

The request identity is `eip155:{chain}:{manager}:{account}:{request tx}:{log index}`. The array index and the uuid are not the identity. A claim index is only the position in the queue at that moment.

Replay starts from `getUserWithdrawalRequests` at the old checkpoint block hash, then applies request and claim logs inside `(old, new]`. It does not start from the empty array left after the claim. A request that is already pending at the old checkpoint is sought in one bounded window at or before that checkpoint, and a found request log is kept for a later observation. A wider balance interval keeps the logs it has already read and resumes; an unfinished window stays retryable. The BNB quote is read at the parent of the claim, before in-block claims are applied, so a swap-and-pop keeps the amount. A request created in the same block as its claim has no stored quote at that parent hash, so the result is not `MATCHES_OWN_REQUEST`.

Two requests for the same slisBNB amount stay distinct because the identity is the transaction and log, not the amount and not index 0.

## Implementation at execution

The proxy's ERC-1967 implementation slot is read at the parent block hash and at the claim block hash. `Upgraded` logs in the claim block are kept only when the emitter, block, and removal flag agree with that block, and they are applied in transaction and log order. An upgrade in the same transaction as the claim is unverified. The implementation after every upgrade in the block has to equal the slot at the claim block hash. A storage read that fails is unverified. A code hash that is not in an approved manifest interval is not a match. The packaged interval list is empty, so this check does not verify a deployment. A historical read that fails is not replaced with `latest`.

`claimWithdrawFor` is decoded and never submitted. The manifest cannot add a call, a view, or an event outside the plugin allowlist. A stale ABI hash loads nothing.

For an approved interval that sets `receipt_implies_payment`, a `ClaimWithdrawal` log in a successful receipt is the payment evidence when its emitter, beneficiary, data, index, and transaction identity agree with the discovered claim. A missing receipt status is unavailable evidence. Only an established failed execution is `failed_payment`. A successful outer transaction without that log is not payment. This implication is not inherited by any other code hash, and the packaged manifest does not enable it.

## Limits

Automatic explanation prints the run report before this cycle's explanation reads. Work left from an earlier cycle runs at the start of the next `run-once`. There is no new scheduler and no new sender. One event has 30 seconds and 32 provider calls. The remaining event time and the remaining round budget are checked again after each response and when the verdict is stored, and that remaining time is the transport timeout. A late result is not a verified match and does not remove the alert. The same evidence is not written twice. A reorg supersedes the current revision and keeps the old row. A rate limit or an archive failure is not retried on every later round unless the local evidence changes or the operator passes `--refresh`. An abandoned running job can be reclaimed. A stale attempt cannot write over a newer generation.

`guardian explain --event-id` reads the stored revision and does not call a provider. `--refresh` requires `--mcm-config`, uses the monitor's read-only transport, and does not send.

## Aave and Lido

These notes are documentation only. This explainer does not implement them.

Aave v3 aToken and debt balances are position reads. A native increase is not attributed to supply, withdraw, or an aToken transfer by this page's explainer. See the Aave position reader for collateral and debt legs. That reader does not decide that an inflow is safe.

Lido stETH, wstETH, and the withdrawal queue are position reads on Ethereum. A request id, a finalized status, and a claimable balance are not an inflow explanation, and this package does not encode a Lido claim. A native increase on Ethereum is `NOT_APPLICABLE` for the Lista explainer.

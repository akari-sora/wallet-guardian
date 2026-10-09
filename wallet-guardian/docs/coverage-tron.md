# TRON read-only coverage

Status date: 2026-10-09. This package reads a bounded slice of TRON. It is not "TRON fully supported".

Shipped example monitors do not enable `tron-mainnet`. The chain file stays `enabled: false`, `status: planned_not_live_verified`, and `identity.status: to_verify_by_probe` (`expected_value` is null). A block header from a probe is not a verified genesis identity.

Environment names are `TRONGRID_API_URL` and optional `TRON_PRO_API_KEY`. The key is sent only as the `TRON-PRO-API-KEY` header. If the key is absent the read is keyless and still sends the watch address. No private key, seed, or keystore is read or stored.

Local request policy for this adapter: one in-flight request and at most one request per second, with bounded retries that honor `Retry-After`. Six successful keyless responses do not establish quota or sustained availability.

`/wallet/getnowblock` is latest-head evidence, not solidified-state evidence. Latest-only balance and constant-call reads keep that label. Economically significant history is accepted only after solidified inclusion and successful execution from `/walletsolidity/gettransactionbyid` and `/walletsolidity/gettransactioninfobyid`. History uses `only_confirmed=true`. Pagination freezes the filter set, keeps it with the fingerprint, caps pages (documented maximum 200 rows), and does not mark a capped or repeated-fingerprint window complete.

| Capability | Status | Notes |
|---|---|---|
| Native balance (liquid TRX, integer sun) | partial | `walletsolidity/getaccount`. Staked TRX, energy, and bandwidth are not included. A failed or malformed body is not stored as zero. An empty account object is an unactivated account, which is not a malformed body. |
| Native history (direct TRX transfers) | partial | `only_confirmed` account transactions, then solidified success checks. Fees stay separate from transfer amounts. Failed execution is not a successful transfer. Indexer rows missing from the solidity node stay lag, not success. Activation transactions are refused. |
| TRC-20 history | partial | `/transactions/trc20` is typed by the record type. An approval is not a token movement. |
| Configured-token balance | partial | `balanceOf` for explicitly configured contracts only, via an allowlisted constant selector. Not a discovery of every token. |
| Known-pair allowance | partial | `allowance(owner,spender)` for configured pairs only (`configured_pairs_only`). This is not an approval-history scan. Non-read selectors are refused even though `triggerconstantcontract` could simulate them. ABI address words are the 20-byte payload, not the 21-byte TRON address. |
| Account permissions | partial | Owner and active threshold and key counts only. Account keys are not handled and are not control. |
| Staking, resource accounting, internal transfers, TRC-10, general approval discovery, activation, account-key updates | not_verified | Out of scope. Reported as unknown, unsupported, or refused. Not safe, not complete, and not zero. |

Evidence note: keyless TronGrid probes on 2026-10-09 returned HTTP 200 for accounts, transactions, transactions/trc20, triggerconstantcontract, and getnowblock. Those six responses are not a live pass, not a quota measurement, and not proof the indexer stays available.

Nothing here signs, builds, or broadcasts a transaction.

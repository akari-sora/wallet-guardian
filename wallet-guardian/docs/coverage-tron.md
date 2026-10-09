# TRON read-only coverage

Status date: 2026-10-09. This package reads a bounded slice of TRON. It is not "TRON fully supported".

Shipped example monitors do not enable `tron-mainnet`. The chain file stays `enabled: false`, `status: planned_not_live_verified`, and `identity.status: to_verify_by_probe` (`expected_value` is null). A block header from a probe is not a verified genesis identity.

Environment names are `TRONGRID_API_URL` and optional `TRON_PRO_API_KEY`. The key is sent only as the `TRON-PRO-API-KEY` header. If the key is absent the read is keyless and still sends the watch address. No private key, seed, or keystore is read or stored.

Local request policy for this adapter: one in-flight request and at most one request per second, with bounded retries that honor the full `Retry-After`. A wait that would pass the round deadline is deferred instead of shortened, and an expired deadline does not send the attempt. Six successful keyless responses do not establish quota or sustained availability.

`/wallet/getnowblock` is latest-head evidence, not solidified-state evidence. Latest-only balance and constant-call reads keep that label. Economically significant history is accepted only after solidified inclusion and successful execution from `/walletsolidity/gettransactionbyid` and `/walletsolidity/gettransactioninfobyid`. History uses `only_confirmed=true`. Each walk freezes `min_timestamp` and `max_timestamp` with the other filters and sends that same set on every page. An omitted upper timestamp would default to the current time and is not used. A fingerprint is kept only for a page whose rows were verified or explicitly classified. A missing solidified receipt or a request cap leaves the cursor on that unfinished page and keeps the same window. Rows already classified on that page are stored and are not read again; the next scan continues at the first unverified row. A finished window stores the solidified height with those bounds; the next scan starts after the stored upper timestamp instead of dropping it. Pages are capped (documented maximum 200 rows). A capped or repeated-fingerprint window is not complete.

`_read_tron()` creates alert candidates only for finalized native TRX `asset_in` and `asset_out`. Stored TRC-20 transfers and approval events are history rows, not alert candidates. Storing that history does not verify a token alert or an approval alert.

| Capability | Status | Notes |
|---|---|---|
| Native balance (liquid TRX, integer sun) | partial | `walletsolidity/getaccount`. Staked TRX, energy, and bandwidth are not included. The account or error envelope is checked first. A body for another account is `account_mismatch` and its balance is not stored. A failed or malformed body is not stored as zero. An empty account object is an unactivated account. After a valid envelope, an omitted balance is the protobuf default of zero. |
| Native history (direct TRX transfers) | partial | `only_confirmed` account transactions, then solidified success checks. Fees stay separate from transfer amounts and are attributed only when the solidified transaction shows this wallet as the fee payer. Failed execution is not a successful transfer. Indexer rows missing from the solidity node stay lag, not success, and do not advance the cursor. Activation transactions are refused. |
| TRC-20 history | partial | `/transactions/trc20` is typed by the record type. An approval is not a token movement. A successful transaction is not a TRC-20 transfer unless the solidified receipt log matches the contract, the parties, and the amount. The stored identity is that transaction plus the receipt log index. Two logs with the same contract, parties, and amount keep those distinct indexes. A repeated indexer row for one log stays one identity. |
| Configured-token balance | partial | `balanceOf` for explicitly configured contracts only, via an allowlisted constant selector. Not a discovery of every token. |
| Known-pair allowance | partial | `allowance(owner,spender)` for configured pairs only (`configured_pairs_only`). This is not an approval-history scan. At most eight pairs are read; any further pair is named `omitted_pair` with `allowance_scope_omitted` and is not stored as zero. Non-read selectors are refused even though `triggerconstantcontract` could simulate them. ABI address words are the 20-byte payload, not the 21-byte TRON address. |
| Account permissions | partial | Owner threshold and owner-key count, plus the number of active permissions; active thresholds/keys are not analysed. |
| Staking, resource accounting, internal transfers, TRC-10, general approval discovery, activation, account-key updates | not_verified | Out of scope. Reported as unknown, unsupported, or refused. Not safe, not complete, and not zero. |

Evidence note: keyless TronGrid probes on 2026-10-09 returned HTTP 200 for accounts, transactions, transactions/trc20, triggerconstantcontract, and getnowblock. Those six responses are not a live pass, not a quota measurement, and not proof the indexer stays available.

Nothing here signs, builds, or broadcasts a transaction.

# Relations

`guardian relations` is a manual read of public transfer history. This page matches the behavior in `src/guardian/relations/`.

## What it is

Two commands exist: `guardian relations inbound` and `guardian relations expand`. Both are read-only. The package is imported only from the command line: `guardian.cli` calls `execute` when the command is `relations`. `run-once`, `watchdog`, `balance-probe`, and `install-service` do not call it. The built-in schedule and the schedule `install-service` produces do not call relations. Nothing in the relations package signs a transaction or broadcasts one.

JSON and Markdown are written only for a run whose status is `dry_run`, `consent_required`, `complete`, or `partial`. Markdown is written only when `--markdown` is also set. A finished command exits 0, including when its status is `partial`. That exit code is not an empty result. A missing `--yes` is `consent_required` and exits 2. No request is sent on exit 2. `ConfigError` exits 2 and writes no result file. A `SecurityError` whose reason is not `redirect_host` exits 4 and writes no result file. A request may already have been sent. Any other error exits 1 and writes no result file.

## Layers

`inbound` is layer 1. Each seed is read with direction `to`. Senders are grouped into `counterparties`. A transfer whose sender and recipient are the same address is not a row.

`expand` is layer 2 for one payer named on the command line. The payer is read with direction `from` on each named network. Other recipients are `co_recipients`. The payer is not one of those recipients.

Nothing expands a result into another query. There is no third command and no third layer. Addresses that appear only in a response are not added to the job list. The plan carries this note: "Responses include other addresses. Those addresses are not queried."

## Disclosure and consent

Before any request, the command builds a disclosure plan. `plan.queries` lists each host that would be contacted, with the network, `source_id`, direction, and the addresses that host would receive. `plan.unqueried` lists skipped addresses and the reason. Queries are sorted by network, `source_id`, direction, then host. Unqueried rows are sorted by network, address, then reason. The plan stores the host name, not a full URL.

The private label file is loaded before this branch. A bad label file exits 2 with no request, including on `--dry-run`.

With `--dry-run`, the status is `dry_run`, the exit code is 0, `reads` is `[]`, and no transport is built. `--dry-run` wins when `--yes` is also set. Without `--yes` and without `--dry-run`, the status is `consent_required`, the exit code is 2, `reads` is `[]`, and no transport is built. Both of those paths still write the result file under the state directory.

With `--yes` and without `--dry-run`, only the planned queries are sent. A skip choice sends nothing for that address.

With `--yes` (and without `--dry-run`), the same plan is written to stderr as one JSON line `{"disclosure_plan": …}` before the first request; stdout still carries only the final result.

## Flags

`build_parser()` accepts the flags below on `inbound`, `expand`, or both. A non-integer value for `--since-days`, `--max-pages`, `--max-nodes`, or `--hub-threshold` is rejected by argparse: the process exits 2, stderr prints usage, and stdout has no JSON. An integer outside a bound is `ConfigError` and exit 2 before any request. Bounds are inclusive.

| Flag | Commands | Default | Bound or rule |
| --- | --- | --- | --- |
| `--help` | both | | Prints help and does not send. |
| `--db` | both | | Accepted. `execute` does not read it. The profile comes from `--mcm-config`. |
| `--tenant` | both | | Accepted. `execute` does not read it. |
| `--json` | both | | Accepted. The process prints one JSON object whether or not this flag is set. An argparse error and `--help` do not print that JSON. |
| `--mcm-config` | both | required | Path of the monitor profile. |
| `--seed` | both, repeatable | profile accounts | Inbound: address to read. Expand: address excluded from `co_recipients` and used as the seed-receipt set. Duplicates are dropped. An empty string is `bad_address`. Inbound with no seed and no profile account is `seed_required`. |
| `--network` | both, repeatable | see below | Exact duplicates are dropped. The comparison is the exact string, so a different case is an unknown network. |
| `--since-days` | both | 90 | 1 through 3650. |
| `--max-pages` | both | 10 | 1 through 50. |
| `--max-nodes` | both | 500 | 1 through 5000. Caps queried addresses and output rows. |
| `--dry-run` | both | off | Disclosure plan only. No request. |
| `--yes` | both | off | Send the disclosed reads. |
| `--markdown` | both | off | Also write a Markdown file next to the JSON result. |
| `--payer` | expand, required | | The address whose outgoing transfers are read. |
| `--hub-threshold` | expand | 200 | 0 through 1000000. A hub from recipient count needs a count strictly greater than this value. |
| `--expand-hub` | expand | off | List `co_recipients` for a hub payer and set `noise` to `high`. |

`--network` on inbound is optional. When it is omitted, an EVM seed uses every enabled network whose family is `evm`. Any other known family uses its home network even when that network is not enabled: `solana-mainnet`, `bitcoin-mainnet`, `tron-mainnet`, `sui-mainnet`, `aptos-mainnet`, `ton-mainnet`, or `cosmos-hub`. An unknown family is `ConfigError` `family`.

When `--network` is set on inbound, a seed is read only on named networks of its own family. A seed that matches none of them is unqueried with `read_status` `unsupported` and reason `network_family_mismatch`. If no seed matches any named network, the command exits 2 with `network_family` and sends nothing. An unknown network name is `unknown_network`. A named network other than Solana or Bitcoin must be enabled, or the command exits 2 with `network_disabled`. Solana and Bitcoin may be named when they are disabled; the read is still `unsupported` and sends nothing.

`--network` on expand is required. Repeat it to add another network in the payer's family. A different family is `network_family` and sends nothing. The same enabled-network rule applies, including the Solana and Bitcoin exception.

`--since-days` sets the oldest timestamp kept: `max(0, now - since_days * 86400)`, in whole seconds. An older row is not an edge. Stopping the page walk at a row outside the window applies to Blockscout, and that walk ends as a finished window, not as a gap. TRON sends the time window for the server to filter. Locally, an older row is dropped and that page walk does not stop there. BSC uses block timestamps. A chunk that has not reached that cutoff is not a finished window.

`--max-nodes` is checked twice. Before any request, the number of distinct `(network, address)` pairs that would actually be queried must be at most the cap. Skip-only jobs do not count. Above the cap, the command exits 2 with `max_nodes` and sends nothing. After a read, `counterparties` or `co_recipients` are cut to that many rows, in the order already sorted. `truncated` is `{"reason": "node_cap", "total": <full row count>}`. If the command status would have been `complete`, it becomes `partial`. The source `read_status` can stay `complete`. `recipient_count` still uses the full recipient set.

`--max-pages` is the page cap passed to each Blockscout tail and to the TRON walk. On keyless BSC it is the number of block chunks read for the token part. On NodeReal it is the total number of `nr_getAssetTransfers` pages for each part, not a per-chunk cap and not that number squared.

Address text is classified, then parsed. A `0x` string of length 42 is EVM. A `T` string of length 34 is tried as TRON and, if that parse fails, as Solana. A `bc1`, `tb1`, or `bcrt1` prefix is Bitcoin. A leading `1` or `3` is tried as Bitcoin and, if that parse fails, as Solana. Anything else is tried as Solana. An unparsable address exits 2 and sends nothing. Solana and Bitcoin are parsed and then returned as `unsupported`. They are not queried.

## Sources by network

Relations does not edit the saved profile and does not add a source to `run-once`. Blockscout is used only when the profile already routes it. The route scan walks `history.native_external`, `history.native_internal`, `history.fungible`, then `history.token`, and keeps the first usable source.

| Networks | What is read | Status when not read | Requests |
| --- | --- | --- | --- |
| `ethereum-mainnet`, `optimism-mainnet`, `base-mainnet`, `arbitrum-one`, `polygon-pos` | The first enabled, routed Blockscout source that has a host. A source with `credential_env` is used only when that credential is present. Three parts: `token-transfers`, `transactions`, `internal-transactions`. | See the rows below. | Only that host. |
| Those five, when no Blockscout source is usable and at least one enabled routed Blockscout source is missing its credential | | `read_status` `gap`, reason `credential_missing`, `gap` `credential_missing` | None. This is chosen before the Etherscan row. |
| Those five, when no Blockscout source is usable and an enabled routed `etherscan_v2` source exists | | `read_status` `gap`, reason `source_gated`, `gap` `etherscan_deferred` | None. A key in the environment does not start an Etherscan call. |
| Those five, when nothing above matches | | `read_status` `gap`, reason `endpoint_missing` | None. |
| `bsc-mainnet` | NodeReal `nr_getAssetTransfers` when an enabled routed `nodereal_transfers` source has both a credential and a host. Otherwise keyless ERC-20 `eth_getLogs` on `bsc-logs-48club`. | A missing NodeReal key does not skip the network. See below. | That one host. |
| `tron-mainnet` | Enabled `tron_grid` source with a host. A source whose `endpoint_env` is `TRONGRID_API_URL` is preferred. Three parts, in order `trc20`, `trx`, `trc10`. `trc20` is read from `/v1/accounts/{address}/transactions/trc20`. `trx` and `trc10` are `gap` with reason `not_in_trc20_endpoint`, so the read is `gap`. | No host: `gap`, reason `endpoint_missing` | That host only. No native TRX request. |
| Solana, Bitcoin, Sui, Aptos, TON, Cosmos | Address parse only. Homes: `solana-mainnet`, `bitcoin-mainnet`, `sui-mainnet`, `aptos-mainnet`, `ton-mainnet`, `cosmos-hub`. | `read_status` `unsupported`, reason `unsupported` | None. |
| Any other network, including any other EVM network | Not read. | `read_status` `unsupported`, reason `unsupported` | None. |

A Blockscout credential is taken from the secret profile first, then from the process environment. The Blockscout client sends it as a bearer header on that source, not in the query string. Inbound's first request uses `filter=to`. Expand's first request uses `filter=from`. Later Blockscout pages follow `next_page_params`. Rows are still dropped unless the queried address is the `to` side (inbound) or the `from` side (expand).

On `optimism-mainnet`, a keyless Blockscout source whose endpoint is empty, and any routed Blockscout source whose host is `optimism.blockscout.com`, is read at `https://explorer.optimism.io` with no credential. That rewrite still happens when the source names a credential and the value is unset. A routed host that is already `explorer.optimism.io` also sends no credential, including when a key is present and when a key is named but unset. A routed Blockscout host that is already a different host, including a PRO host whose key is present, is left as configured. The saved profile is not rewritten. `run-once` still uses the endpoint the profile resolved.

On `bsc-mainnet`, NodeReal is selected only when the credential and the URL are both present. The read has three parts, in order `token-transfers` (category `20`), `transactions` (`external`), and `internal-transactions` (`internal`). Each part may spend at most `--max-pages` `nr_getAssetTransfers` pages in total, walking back from the chain head in spans of 100000 blocks. The same part stops at 5000 kept items (`item_cap`). A block timestamp is looked up at most 32 times per part (`time_lookup_cap`). A successful stamp is cached for the rest of this command, and a cache hit does not count. A part is `complete` only when a chunk reaches block 0 or the chunk-start timestamp is at or before the `--since-days` cutoff. Stopping at `--max-pages` or at 5000 items keeps the rows already read and is `partial`, with reason `page_cap` or `item_cap`. A block payload with no timestamp is `window_unchecked`. A transport failure keeps its own reason (`rate_limited`, `daily_cap`, `redirect_host`, and the rest) and is not rewritten as `window_unchecked`. A page cap or a timestamp cap is `partial` even when no row is kept. A failure of the first request, when no row is kept, is `gap`. After one successful request that returned no row, a later failure is `partial`, and the reason is that failure. When rows are kept, a later failure is `partial`. A row is read from `blockNum` or `blockNumber`, a top-level `value` or `rawContract.value`, and `contractAddress`, `tokenAddress`, or `rawContract.address`. `isError` other than `0` drops that row. `receiptsStatus` or `receipts_status` of `0` or `0x0` drops that row. A row with no timestamp uses the capped block lookup. If that lookup drops a row, the part keeps `timestamp_reason` even when the part was already `partial`. A `rate_limited`, `daily_cap`, or `redirect_host` stamp stops further timestamp lookups in that part, and rows that still have no timestamp are dropped for that same reason. When that stop drops every row of a part that already had a successful `nr_getAssetTransfers`, the part is `partial` with that stop reason, including a part that was `complete` before the stop. It is not a `gap`. When the part already ended for `rate_limited`, `daily_cap`, or `redirect_host`, rows that still have no timestamp are dropped for that same reason without another block lookup, and the reason is not rewritten as `window_unchecked`. `expand` also sends one `eth_getCode` for the payer on this same host, unless this read or a part `timestamp_reason` is `rate_limited`, `daily_cap`, or `redirect_host`. A retry waits for `Retry-After` and stops at 30 seconds. That wait does not use the 10 second token-bucket cap.

Without that NodeReal key, relations reads ERC-20 `Transfer` logs from `bsc-logs-48club`. A set `RPC_BSC_LOGS_URL` is the host that receives the address. When that variable is empty, or the profile has no enabled source for it, this command uses `https://rpc-bsc.48.club`. That URL is not written into the profile. `run-once` still skips `bsc-logs-48club` until `RPC_BSC_LOGS_URL` is set, so a monitor round does not start sending watched addresses there. A profile entry `bsc-logs-48club` with `enabled` false is an opt-out: the read is `gap` with reason `source_disabled` and no request is sent. Chunks are at most 1000 blocks, or the source `range_limit` when that value is from 1 to 1000. `--max-pages` is the number of those chunks. At BSC block time, 1000 blocks times `--max-pages` is a few hours, so a 90-day window is usually `partial` with reason `page_cap`. The token part is `complete` when a chunk reaches block 0 or the chunk-start timestamp is at or before the `--since-days` cutoff. `transactions` and `internal-transactions` are `gap` with reason `not_in_keyless_logs`, because these logs do not contain native or internal BNB. The read is `gap` even when the token part is `complete`, and the command is not `complete`. When rows were kept, `counterparties` and `co_recipients` are not `[]` for that read. 48club publishes no quota and no SLA. The operator is an MEV builder. Block timestamps use the same cap of 32 lookups per part and the same command cache. A timestamp failure keeps the underlying reason. A page cap or a timestamp cap on the token part is `partial` even when no row is kept. A failure of the first request, when no row is kept, is `gap`. After one successful request that returned no row, a later failure is `partial`, and the reason is that failure. When rows are kept, a later failure is `partial`. That includes `rate_limited`, `daily_cap`, and `redirect_host`. A chunk that returns rows is `gap` when its first block-timestamp lookup fails before any row is kept, and no earlier chunk succeeded. A cut or failed read is not an empty list and is not `complete`. `expand` sends one `eth_getCode` for the payer on this same host. The payer address is already in the disclosure plan.

TRON sends `only_to` or `only_from` for the same direction. The window is the same `--since-days` cutoff, in milliseconds. A TRON row whose type is set and is not `transfer` is dropped. A fungible row needs a token contract. The read also records `trx` and `trc10`. Both are `gap` with reason `not_in_trc20_endpoint`, and neither is requested. Because those parts are `gap`, the read is `gap`. On expand, `seed_receipts` is null and `evidence_note` is `seed_receipts_unknown`, the same as keyless BSC, even when kept `trc20` rows still produce `co_recipients` evidence such as `same_asset`, and `hub` stays null unless a hub reason such as `recipient_count` or `label` applies.

These rows are not edges: value zero (counted on the read as `ignored_zero_value`), a failed or reverted transaction, an NFT (`ERC-721`, `ERC-1155`, `ERC721`, `ERC1155`), a native contract-creation row with a null or blank `to`, and a Blockscout row with no block number whose `result` is `pending`. Any other Blockscout row with no block number fails that part with `missing_block`.

Each Blockscout tail also stops after 5000 kept items (`item_cap`). That part is `partial` with reason `item_cap`. The TRON client stops at 48 requests. That stop is stored on the `trc20` part as `partial` with reason `request_cap`. The read stays `gap` with reason `not_in_trc20_endpoint`.

## Read status

Each read has `read_status`, `reason`, `parts`, `ignored_zero_value`, and `gap` when the choice set one. A skip has an empty `parts` list and a null `source_id`.

`parts` is the per-call record. Blockscout writes three objects, in tail order `token-transfers`, `transactions`, `internal-transactions`. BSC writes the same three part names. TRON writes three objects, in order `trc20`, `trx`, `trc10`. Each object has `part`, `read_status`, and `reason`.

A Blockscout part is `complete` when the client category is `ok` or `empty`, pagination is complete, and the read is not blocked. Category `partial` stays `partial` (reasons include `page_cap` and `item_cap`). Anything else is `gap`, and the reason is kept (`bot_challenge`, `redirect_host`, `connection_error`, `daily_cap`, `repeated_cursor`, and the rest). A row whose amount cannot be parsed is not kept. Blockscout token transfers, Blockscout native transactions, TRON, NodeReal, and keyless BSC all do this. That part becomes `partial` with reason `unreadable_row` only when it would otherwise have been `complete`. A part that is already `partial` keeps its original reason. A part that is `gap` stays `gap`. A missing decimals value stays null and does not make the row unreadable.

Blockscout remembers each `next_page_params` query it has already sent. NodeReal remembers each `pageKey` it has already sent inside one block span. When the next cursor is one of those, that page is dropped, earlier pages are kept, and that part is `gap` with reason `repeated_cursor`. TRON already drops the page that repeats a fingerprint, with reason `repeated_fingerprint`. Rows from the kept pages are still counted. The read still rolls up from the parts, so the first `gap` part sets the read reason.

Each part counts a row once. A Blockscout internal transaction uses the transaction hash plus the first present field among `uniqueId`, `traceId`, `traceAddress`, and `index`. A 48club log uses the transaction hash plus `logIndex` or `log_index`. The same key is one event, and dropping the later copy does not change a `complete` part. Two logs in one transaction stay two rows when those indexes differ. Blockscout token transfers, Blockscout native transactions, NodeReal transfers, and TRON TRC-20 rows do not have that key in the fields this command parses. Two equal rows on the same page both count, and a `complete` part stays `complete`. The same transaction hash, sender, recipient, contract, and raw amount on a later page keeps the first copy only. A `complete` part then becomes `partial` with reason `ambiguous_duplicate`. A part that is already `partial` or `gap` keeps its status and its reason.

The read rolls up in that same tail order. The first part whose `read_status` is `gap` makes the read a `gap`, and that part's reason is the read reason. Rows from other parts are still kept. If there is no gap and some part is `partial`, the read is `partial`. Otherwise the read is `complete`.

A TRON `trc20` part is `complete` when the walk is complete and has no error. Reason `page_cap` or `request_cap` makes that part `partial`. Any other incomplete walk makes that part `gap`. An unreadable row on a walk that would be `complete` makes the `trc20` part `partial` with reason `unreadable_row`. `trx` and `trc10` are `gap` with reason `not_in_trc20_endpoint` on every result, including a budget stop, `redirect_host`, a walk gap, and a finished walk. The read rolls up in part order `trc20`, `trx`, `trc10`. The first gap sets the read status and reason. When `trc20` itself is `gap`, the read reason is that part's reason. Otherwise the read is `gap` with reason `not_in_trc20_endpoint`. Rows kept from `trc20` stay in the result, including rows already read when the `trc20` walk fails on a later page and that part is `gap`. On `repeated_fingerprint`, the page that triggered the repeat is not counted, and rows from earlier pages are kept. The read is never `complete`.

`unsupported` is the skip for a family or network this command does not query. It is not a `gap` and it is not an empty history.

The command `status` is `complete` only when every read's `read_status` is `complete`. Any other mix, including `gap` and `unsupported`, is `partial`. `dry_run` and `consent_required` are the statuses used before a read. If `truncated` is set and the status was `complete`, the status becomes `partial`.

`counterparties` (inbound) and `co_recipients` (expand, when they are published) follow one list rule. Rows that were read are returned as a list even when the read is `partial` or a sibling part is `gap`. The list is `[]` only when there are no rows and every contributing read is complete (`source_complete` for expand). Otherwise the list is null. Null is not stored as `0` and not stored as `[]`. `merged_addresses` is null when that list is null, and `[]` when the list is empty. A hub that is not expanded omits `co_recipients` and `merged_addresses` instead of setting them null.

## Hub rule

Hub fields exist on `expand` only. `hub_reasons` is filled in this order, and more than one reason can apply:

1. `contract`, when any outgoing edge reports the payer as a contract. One such edge on any selected network is enough. The flag is not set back to false.
2. `label`, when `lookup` hits the payer on a selected network, or the private label file matches the payer with kind `exchange`, `protocol`, or `bridge`.
3. `recipient_count`, when the recipient count is known and strictly greater than `--hub-threshold`. The count is the full set, including seed addresses and excluding the payer. The same EVM address on two networks counts once. The output cap does not lower this count.

`hub` is true when `hub_reasons` is not empty, including when the read is incomplete. A static label hit still sets `hub` true. BSC is not a gated network: with no NodeReal key the command still reads keyless logs. A skipped network sends nothing. `hub` is false only when `hub_reasons` is empty, every adopted read is `complete`, and the payer contract flag was read as false. A missing flag does not become false. TRON rows, and Blockscout rows with no `is_contract` flag, do not output `hub: false`. `hub` is null when `hub_reasons` is empty and that flag was not read as false. An incomplete read does not claim `hub: false`.

On BSC `expand`, the same host receives one `eth_getCode` for the payer. That address is already in the disclosure plan, and the call uses that plan host. Only an empty `0x` code means the flag is false. Any non-empty code, including `0x00`, means a contract, so the payer is a hub and `co_recipients` stay hidden unless `--expand-hub` is set. A failed code read sets `contract_check` to that status and reason, leaves the flag unknown, leaves `hub` null when no other hub reason applies, and does not list `co_recipients` unless `--expand-hub` is set. The same host is not asked for code when this read, or a part `timestamp_reason`, is `rate_limited`, `daily_cap`, or `redirect_host`. Inbound does not send `eth_getCode`. Addresses that appear only in a response are not queried.

`recipient_count` is null when there are no edges and the overall read is not `complete`. A `partial` read with no edges does not store `0`. A `complete` read with no edges beside a gap does not store `0`. A read that has edges stores the count, including when the read is `partial`. A `complete` read with no edges stores `0` only when every adopted read is `complete`. `recipient_count_complete` is true only when every adopted read is `complete`.

By default a hub does not list `co_recipients`. `--expand-hub` lists them and sets `noise` to `high`. Without that flag, `noise` is absent. The Markdown for a hidden hub says: "Co-recipients are not listed for a hub payer; pass --expand-hub to list them."

Kinds `self` and `other` in the private file do not add the `label` reason.

## Evidence and confidence

Evidence is computed for expand `co_recipients` only. Inbound rows do not carry `score`, `confidence`, or `evidence`. Seed addresses are excluded from `co_recipients` after the count. Their transfers are the seed receipts those kinds are compared with.

Four kinds exist. A kind that is not present is omitted. No kind is stored as false.

| Kind | Weight | When it is present |
| --- | --- | --- |
| `same_tx` | 3 | The recipient has a transfer whose `(network, tx_hash)` is also a seed receipt. The public object keeps at most five hashes. The reason text uses the full shared-transaction count: "paid in the same transaction as a seed address (N transactions)". |
| `same_asset` | 1 | The recipient received an asset key `(network, kind, contract)` that also appears in the seed receipts. |
| `amount_band` | 1 | Shared asset keys are tried in sorted order; the first key with at least one in-band amount is published, and the band uses that key's seed amounts. The inclusive band is `min(seed) * 80 // 100` through `(max(seed) * 120 + 99) // 100`. Null raw amounts are skipped. At most five matched amounts are published. No source in this command publishes a null raw amount. |
| `cadence` | 1 | Both sides have at least two distinct timestamps. The median gap on each side is at least 3600 seconds, and the two medians differ by at most 20 percent of the larger one. A gap of exactly 20 percent is kept. A wider gap is omitted. |

`score` is the sum of the weights of the kinds that are present. The maximum is 6. `confidence` is `high` when the score is at least 4, `medium` when it is at least 2, and `low` otherwise, including 0. Kinds in the `evidence` list stay in the order `same_tx`, `same_asset`, `amount_band`, `cadence`.

`co_recipients` are sorted by score descending, then by presence of `same_tx` (a tie at the same score lists `same_tx` first), then by `count` descending, then by last timestamp descending, then by address ascending. Inbound `counterparties` are sorted by `count` descending, then last timestamp descending, then address ascending. `tx_hashes` on a row keeps at most 20 hashes.

Absence and a failed check are not the same, and neither is stored as a negative flag.

A kind is absent when it is not evaluated. That happens when the seed-receipt count is 0, when the recipient has no edges, when cadence has fewer than two distinct timestamps, and when a cadence median is under 3600 seconds. There is no comparison to fail.

A kind does not match when the inputs exist and the predicate fails: no shared transaction, no shared asset, no raw amount inside the band, or cadence medians more than 20 percent apart. The kind is omitted in that case too. A missing kind does not by itself say which of these two happened. An empty `evidence` list with score 0 is `confidence` `low`. It is not a statement that a check failed, and it is not stored as false.

`seed_receipts` and `evidence_note` have three cases. The rolled-up `read_status` is what decides them, not each part.

1. Receipts are published. `seed_receipts` is an object with `assets` and `count`, and `evidence_note` is omitted. This is the path when `count` is greater than zero and not every read is `gap` or `unsupported`. A `partial` read can be in this case. `count` is the number of parsed seed edges, not a claim that the window held nothing else.
2. A zero is published when the seed edge count is 0 and either every read is `complete`, or other edges were kept. `seed_receipts` is `{"assets": [], "count": 0}`. Every evidence kind is omitted because the count is 0. If every read is `complete`, `evidence_note` is `no_seed_receipts`. If other edges were kept and not every read is `complete`, `evidence_note` is `no_seed_receipts_in_partial_read`.
3. The summary is withheld. If every read is `gap` or `unsupported`, `seed_receipts` is null and `evidence_note` is `seed_receipts_unknown`. That stays true even when a part returned rows and the edge count is not zero. Those edges can still be grouped, and evidence kinds can still be computed from them. The null object is not stored as `0`. No edges and an overall read that is not `complete` also withholds `seed_receipts` and `recipient_count`. That includes one `complete` read beside a gap. In that case `evidence_note` is omitted, and the null is not stored as `0`.

Token reason text names the contract. Native reason text says `native`. A token symbol is not copied into the reason sentence.

## EVM cross-chain merge

`merged_addresses` groups shown rows whose address starts with `0x`. The key is the lowercased address. A group is emitted only when that address appears on at least two networks in the shown rows. Rows cut off by `--max-nodes` are not merged. TRON addresses and other non-`0x` addresses are not merged. The same address text on two EVM networks is the whole rule. Transfers are not paired across a bridge, and two different addresses are not merged.

Each merged row has `address`, `networks` (sorted), `count` and `tx_hash_count` (sums), `first_timestamp` (minimum), `last_timestamp` (maximum), and `is_contract` (`true` if any row is true, `false` if every row is false, otherwise `unknown`). Expand copies `score` and `confidence` from the first shown row of that address. Inbound does not add those two fields. The merged list is sorted by address.

When one network was requested, expand sets both `network` and `networks`. When more than one network was requested, it sets `networks` only.

## Private labels file

The file is `<state_dir>/relations-labels.json`. It is not inside the `relations` result directory. It is read locally. A missing file means there are no private labels. The label text is not sent to any host and is not placed on a request URL.

On POSIX, a mode that includes group or other bits is `labels_file_mode` and exit 2 before any request. Mode `0600` has none of those bits. A symlink is `labels_file`. So is a path that is not a regular file, a file larger than 1048576 bytes, a body that is not the schema, or a `labels` array longer than 10000 rows. The same errors fire on `--dry-run`.

The body is JSON. `schema_version` must be `guardian.relations.labels.v1`. Each row has `network`, `address`, `label`, and `kind`. `kind` is one of `exchange`, `protocol`, `bridge`, `self`, or `other`. `network` is a registry id, or the wildcard `evm` or `tron`. The address is parsed for that family and stored in canonical form. The EVM match key is lowercase. `label` is 1 to 64 characters, printable, with no CR or LF. An exact network row beats a wildcard row.

On a result row, a private-file match sets `label`, `label_kind`, and `label_source` `user_private_file`. A static `lookup` hit on the same address is kept beside it as `static_label`. With no private-file match, `label` and `label_source` come from `lookup`, or both are null. Kinds `exchange`, `protocol`, and `bridge` make the payer a hub. `self` and `other` do not. Any `lookup` hit makes the payer a hub, with reason `label`, whatever the private-file kind would have been.

## Output files

JSON and Markdown are written only for a run whose status is `dry_run`, `consent_required`, `complete`, or `partial`. Markdown is written only with `--markdown`. An error exit writes neither file: `ConfigError` exits 2, a `SecurityError` whose reason is not `redirect_host` exits 4 and a request may already have been sent, and any other error exits 1. Both live under `<state_dir>/relations/`. The JSON name is `{stamp}-{command}.json`, where `stamp` is UTC `YYYYMMDDTHHMMSS` plus microseconds and `Z`. If that name exists, the next file is `{stamp}-{command}-2.json`, then `-3`, and so on. The Markdown path is the same stem with a `.md` suffix. Without `--markdown` there is no Markdown file and no `markdown_path`.

`result_path` is set on the printed object. On POSIX, a newly created `relations` directory is mode `0700`, and an existing directory that is group- or world-accessible is changed to `0700`. Each result file is written at mode `0600`.

The JSON is redacted before it is written or printed. `redact` masks its usual key and bearer patterns, and it also replaces profile secret values of length 6 or more, including resolved credential values. Markdown is rendered from that redacted object. A `scheme://` URL in the Markdown is replaced with `[url removed]`.

The Markdown ends with this paragraph:

> This report lists addresses and heuristic evidence only. Shared transactions, assets, amounts or timing do not show that two addresses are controlled by the same party.

A null `counterparties`, `co_recipients`, `merged_addresses`, or `seed_receipts` is rendered as `unknown`. An empty `merged_addresses` list is rendered as "No merged addresses." A plan with no queries says "No host receives an address."

The Gaps section lists every part on its own line: network, source id, part name, and `read_status`, plus the reason when the reason is not empty. A read with no parts that is not `complete` keeps one line for that read. Unqueried rows stay on their own lines. `truncated` is one more line, `truncated`, its reason, `total`, and the total, so `node_cap` is shown. The section is `None` only when there are no such lines.

## Limits

- BSC keyless relations reads use 48club (`https://rpc-bsc.48.club`, or the host in `RPC_BSC_LOGS_URL`). NodeReal is used when its key and URL are both set. Keyless logs do not include native or internal BNB, so those parts are `not_in_keyless_logs` and the read is `gap`. 1000 blocks times `--max-pages` is a few hours at BSC block time, so a 90-day window is usually `partial` with reason `page_cap`. There is no published quota. A rate limit or a failed chunk is `gap` or `partial`, never an empty list and never `complete`. A retry waits for `Retry-After` and stops at 30 seconds. That wait does not use the 10 second token-bucket cap. `run-once` does not gain a default 48club URL. A disabled `bsc-logs-48club` entry sends nothing.
- Solana and Bitcoin are parsed as addresses and then returned as `unsupported`. No request is sent. Sui, Aptos, TON, and Cosmos are the same.
- Etherscan is not registered for relations. A network that only has an enabled routed `etherscan_v2` source is `gap` / `source_gated` / `etherscan_deferred` and sends nothing, even when a key is present. Base, Arbitrum, and Polygon stay on that rule when they have no usable Blockscout host.
- The keyless Optimism source is `https://explorer.optimism.io`. An endpoint whose host is `optimism.blockscout.com`, and a keyless Optimism Blockscout source with an empty endpoint, are read there and no credential is sent. The old host is still rewritten when a credential is named and unset. A host that is already `explorer.optimism.io` sends no credential, including when a key is present. A different configured host is kept. A redirect whose host differs from the registered host is refused, including a redirect from `explorer.optimism.io` itself. Relations stores that as a `redirect_host` gap and does not follow it.
- Keyless public Blockscout for Base (`base.blockscout.com`), Arbitrum (`arbitrum.blockscout.com`), and Polygon (`polygon.blockscout.com`) has returned HTTP 403 with a Cloudflare page (`Just a moment`) to the recorded Linux host. That result is host-specific. Relations stores the reason `bot_challenge` as a `gap`. The Blockscout call does not set a User-Agent, and this command does not invent one to pass the challenge.
- TRON reads TRC-20 transfers only. Native TRX and TRC-10 are not read. Those parts are `gap` with reason `not_in_trc20_endpoint`, so the read is `gap`. It is not `complete`, and it is not an empty counterparty or co-recipient list.
- `--max-pages` stops a walk and the read is `partial` with reason `page_cap`. On TRON and on keyless BSC, the token part is `partial` and the read stays `gap` when that cap stops the walk: the token part reason is `page_cap`, and the read reason is `not_in_trc20_endpoint` on TRON and `not_in_keyless_logs` on keyless BSC. The TRON client has a fixed cap of 48 requests (`request_cap`); that cap is not `--max-pages`. Each Blockscout tail also stops at 5000 items (`item_cap`) and that part is `partial`. On NodeReal, `--max-pages` is the total page count for one part, and that part also stops at 5000 items. Each BSC part looks up at most 32 block timestamps (`time_lookup_cap`). A cut keeps the rows and is `partial`. A TRON cut keeps the `trc20` rows and the read stays `gap`. A `trc20` walk that fails after an earlier page also keeps the rows already read, and that part stays `gap`. On `repeated_fingerprint`, the page that triggered the repeat is not counted, and rows from earlier pages are kept. A Blockscout or NodeReal cursor that repeats a cursor already sent is `repeated_cursor`: that page is dropped, earlier pages are kept, and the part is `gap`. A later page that repeats a row with no separate row key makes a `complete` part `partial` with reason `ambiguous_duplicate` and counts the first copy only. A query count above `--max-nodes` exits 2 and sends nothing. An output row count above `--max-nodes` keeps the first `--max-nodes` rows and the status becomes `partial`. The cut sets `truncated.reason` to `node_cap`. If the status would have been `complete`, it becomes `partial`.
- A hub's extra recipients are high noise. The field is `noise` `high`, and only when `--expand-hub` is set. The default hub report does not list them.
- Shared transactions, assets, amounts, or timing do not show that two addresses are controlled by the same party. Matching by amount does not show that either. An equal amount is not control of both addresses.
- The command reads public chain data for the addresses it was given. It does not query ENS. It does not query off-chain records.

## Not built

These are not implemented in `src/guardian/relations/`:

- Cross-chain bridge pairing. `merged_addresses` does not do it.
- Exchange deposit-address clustering.
- Bitcoin common-input clustering. Bitcoin is `unsupported`.
- Off-chain records, including ENS.

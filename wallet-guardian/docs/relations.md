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

`--since-days` sets the oldest timestamp kept: `max(0, now - since_days * 86400)`, in whole seconds. An older row is not an edge. Stopping the page walk at a row outside the window applies to Blockscout, and that walk ends as a finished window, not as a gap. TRON sends the time window for the server to filter. Locally, an older row is dropped and that page walk does not stop there.

`--max-nodes` is checked twice. Before any request, the number of distinct `(network, address)` pairs that would actually be queried must be at most the cap. Skip-only jobs do not count. Above the cap, the command exits 2 with `max_nodes` and sends nothing. After a read, `counterparties` or `co_recipients` are cut to that many rows, in the order already sorted. `truncated` is `{"reason": "node_cap", "total": <full row count>}`. If the command status would have been `complete`, it becomes `partial`. The source `read_status` can stay `complete`. `recipient_count` still uses the full recipient set.

`--max-pages` is the page cap passed to each Blockscout tail and to the TRON walk.

Address text is classified, then parsed. A `0x` string of length 42 is EVM. A `T` string of length 34 is tried as TRON and, if that parse fails, as Solana. A `bc1`, `tb1`, or `bcrt1` prefix is Bitcoin. A leading `1` or `3` is tried as Bitcoin and, if that parse fails, as Solana. Anything else is tried as Solana. An unparsable address exits 2 and sends nothing. Solana and Bitcoin are parsed and then returned as `unsupported`. They are not queried.

## Sources by network

No new source is registered. Blockscout is used only when the profile already routes it. The route scan walks `history.native_external`, `history.native_internal`, `history.fungible`, then `history.token`, and keeps the first usable source.

| Networks | What is read | Status when not read | Requests |
| --- | --- | --- | --- |
| `ethereum-mainnet`, `optimism-mainnet`, `base-mainnet`, `arbitrum-one`, `polygon-pos` | The first enabled, routed Blockscout source that has a host. A source with `credential_env` is used only when that credential is present. Three parts: `token-transfers`, `transactions`, `internal-transactions`. | See the rows below. | Only that host. |
| Those five, when no Blockscout source is usable and at least one enabled routed Blockscout source is missing its credential | | `read_status` `gap`, reason `credential_missing`, `gap` `credential_missing` | None. This is chosen before the Etherscan row. |
| Those five, when no Blockscout source is usable and an enabled routed `etherscan_v2` source exists | | `read_status` `gap`, reason `source_gated`, `gap` `etherscan_deferred` | None. A key in the environment does not start an Etherscan call. |
| Those five, when nothing above matches | | `read_status` `gap`, reason `endpoint_missing` | None. |
| `bsc-mainnet` | Not read. The configured sources are ignored. | `read_status` `gap`, reason `bsc_source_gated`, `gap` `credential_missing` | None. |
| `tron-mainnet` | Enabled `tron_grid` source with a host. A source whose `endpoint_env` is `TRONGRID_API_URL` is preferred. One part: `trc20`, from `/v1/accounts/{address}/transactions/trc20`. | No host: `gap`, reason `endpoint_missing` | That host only. No native TRX request. |
| Solana, Bitcoin, Sui, Aptos, TON, Cosmos | Address parse only. Homes: `solana-mainnet`, `bitcoin-mainnet`, `sui-mainnet`, `aptos-mainnet`, `ton-mainnet`, `cosmos-hub`. | `read_status` `unsupported`, reason `unsupported` | None. |
| Any other network, including any other EVM network | Not read. | `read_status` `unsupported`, reason `unsupported` | None. |

A Blockscout credential is taken from the secret profile first, then from the process environment. The Blockscout client sends it as a bearer header on that source, not in the query string. Inbound's first request uses `filter=to`. Expand's first request uses `filter=from`. Later Blockscout pages follow `next_page_params`. Rows are still dropped unless the queried address is the `to` side (inbound) or the `from` side (expand).

TRON sends `only_to` or `only_from` for the same direction. The window is the same `--since-days` cutoff, in milliseconds. A TRON row whose type is set and is not `transfer` is dropped. A fungible row needs a token contract. There is no native TRX part.

These rows are not edges: value zero (counted on the read as `ignored_zero_value`), a failed or reverted transaction, an NFT (`ERC-721`, `ERC-1155`, `ERC721`, `ERC1155`), a native contract-creation row with a null or blank `to`, and a Blockscout row with no block number whose `result` is `pending`. Any other Blockscout row with no block number fails that part with `missing_block`.

Each Blockscout tail also stops after 5000 kept items (`item_cap`). That part is `partial` with reason `item_cap`. The TRON client stops at 48 requests and relations stores that as `partial` with reason `request_cap`.

## Read status

Each read has `read_status`, `reason`, `parts`, `ignored_zero_value`, and `gap` when the choice set one. A skip has an empty `parts` list and a null `source_id`.

`parts` is the per-call record. Blockscout writes three objects, in tail order `token-transfers`, `transactions`, `internal-transactions`. TRON writes one object, `trc20`. Each object has `part`, `read_status`, and `reason`.

A Blockscout part is `complete` when the client category is `ok` or `empty`, pagination is complete, and the read is not blocked. Category `partial` stays `partial` (reasons include `page_cap` and `item_cap`). Anything else is `gap`, and the reason is kept (`bot_challenge`, `redirect_host`, `connection_error`, `daily_cap`, and the rest). A part that would be `complete` but contains a row that cannot be parsed becomes `partial` with reason `unreadable_row`.

The read rolls up in that same tail order. The first part whose `read_status` is `gap` makes the read a `gap`, and that part's reason is the read reason. Rows from other parts are still kept. If there is no gap and some part is `partial`, the read is `partial`. Otherwise the read is `complete`.

A TRON walk that is complete and has no error is `complete`. Reason `page_cap` or `request_cap` is `partial`. Any other incomplete walk is `gap`. An unreadable TRON row on a `complete` walk makes the read `partial` with reason `unreadable_row`.

`unsupported` is the skip for a family or network this command does not query. It is not a `gap` and it is not an empty history.

The command `status` is `complete` only when every read's `read_status` is `complete`. Any other mix, including `gap` and `unsupported`, is `partial`. `dry_run` and `consent_required` are the statuses used before a read. If `truncated` is set and the status was `complete`, the status becomes `partial`.

`counterparties` (inbound) and `co_recipients` (expand, when they are published) follow one list rule. Rows that were read are returned as a list even when the read is `partial` or a sibling part is `gap`. The list is `[]` only when there are no rows and every contributing read is complete (`source_complete` for expand). Otherwise the list is null. Null is not stored as `0` and not stored as `[]`. `merged_addresses` is null when that list is null, and `[]` when the list is empty. A hub that is not expanded omits `co_recipients` and `merged_addresses` instead of setting them null.

## Hub rule

Hub fields exist on `expand` only. `hub_reasons` is filled in this order, and more than one reason can apply:

1. `contract`, when any outgoing edge reports the payer as a contract. One such edge on any selected network is enough. The flag is not set back to false.
2. `label`, when `lookup` hits the payer on a selected network, or the private label file matches the payer with kind `exchange`, `protocol`, or `bridge`.
3. `recipient_count`, when the recipient count is known and strictly greater than `--hub-threshold`. The count is the full set, including seed addresses and excluding the payer. The same EVM address on two networks counts once. The output cap does not lower this count.

`hub` is true when `hub_reasons` is not empty, including when the read is incomplete. A static label hit on a gated network still sets `hub` true and sends nothing. `hub` is false when `hub_reasons` is empty and every adopted read is `complete`. `hub` is null when `hub_reasons` is empty and the read is not complete. An incomplete read does not claim `hub: false`.

`recipient_count` is null when no read is `complete` or `partial` and there are no rows. `recipient_count_complete` is true only when every adopted read is `complete`.

By default a hub does not list `co_recipients`. `--expand-hub` lists them and sets `noise` to `high`. Without that flag, `noise` is absent. The Markdown for a hidden hub says: "Co-recipients are not listed for a hub payer; pass --expand-hub to list them."

Kinds `self` and `other` in the private file do not add the `label` reason.

## Evidence and confidence

Evidence is computed for expand `co_recipients` only. Inbound rows do not carry `score`, `confidence`, or `evidence`. Seed addresses are excluded from `co_recipients` after the count. Their transfers are the seed receipts those kinds are compared with.

Four kinds exist. A kind that is not present is omitted. No kind is stored as false.

| Kind | Weight | When it is present |
| --- | --- | --- |
| `same_tx` | 3 | The recipient has a transfer whose `(network, tx_hash)` is also a seed receipt. The public object keeps at most five hashes. The reason text uses the full shared-transaction count: "paid in the same transaction as a seed address (N transactions)". |
| `same_asset` | 1 | The recipient received an asset key `(network, kind, contract)` that also appears in the seed receipts. |
| `amount_band` | 1 | Shared asset keys are tried in sorted order; the first key with at least one in-band amount is published, and the band uses that key's seed amounts. The inclusive band is `min(seed) * 80 // 100` through `(max(seed) * 120 + 99) // 100`. Null raw amounts are skipped. At most five matched amounts are published. |
| `cadence` | 1 | Both sides have at least two distinct timestamps. The median gap on each side is at least 3600 seconds, and the two medians differ by at most 20 percent of the larger one. A gap of exactly 20 percent is kept. A wider gap is omitted. |

`score` is the sum of the weights of the kinds that are present. The maximum is 6. `confidence` is `high` when the score is at least 4, `medium` when it is at least 2, and `low` otherwise, including 0. Kinds in the `evidence` list stay in the order `same_tx`, `same_asset`, `amount_band`, `cadence`.

`co_recipients` are sorted by score descending, then by presence of `same_tx` (a tie at the same score lists `same_tx` first), then by `count` descending, then by last timestamp descending, then by address ascending. Inbound `counterparties` are sorted by `count` descending, then last timestamp descending, then address ascending. `tx_hashes` on a row keeps at most 20 hashes.

Absence and a failed check are not the same, and neither is stored as a negative flag.

A kind is absent when it is not evaluated. That happens when the seed-receipt count is 0, when the recipient has no edges, when cadence has fewer than two distinct timestamps, and when a cadence median is under 3600 seconds. There is no comparison to fail.

A kind does not match when the inputs exist and the predicate fails: no shared transaction, no shared asset, no raw amount inside the band, or cadence medians more than 20 percent apart. The kind is omitted in that case too. A missing kind does not by itself say which of these two happened. An empty `evidence` list with score 0 is `confidence` `low`. It is not a statement that a check failed, and it is not stored as false.

`seed_receipts` and `evidence_note` have three cases. The rolled-up `read_status` is what decides them, not each part.

1. Receipts are published. `seed_receipts` is an object with `assets` and `count`, and `evidence_note` is omitted. This is the path when `count` is greater than zero and not every read is `gap` or `unsupported`. A `partial` read can be in this case. `count` is the number of parsed seed edges, not a claim that the window held nothing else.
2. A zero is published. `seed_receipts` is `{"assets": [], "count": 0}`. Every evidence kind is omitted because the count is 0. If every read is `complete`, `evidence_note` is `no_seed_receipts`. If at least one read is `complete` or `partial` and not every read is `complete`, `evidence_note` is `no_seed_receipts_in_partial_read`.
3. The summary is withheld. If every read is `gap` or `unsupported`, `seed_receipts` is null and `evidence_note` is `seed_receipts_unknown`. That stays true even when a part returned rows and the edge count is not zero. Those edges can still be grouped, and evidence kinds can still be computed from them. The null object is not stored as `0`.

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

## Limits

- Which source reads BSC is the user's choice (a NodeReal key or 48club). Until that choice is made, `bsc-mainnet` is a `credential_missing` gap, reason `bsc_source_gated`, and it sends nothing. Relations ignores configured sources for that network.
- Solana and Bitcoin are parsed as addresses and then returned as `unsupported`. No request is sent. Sui, Aptos, TON, and Cosmos are the same.
- Etherscan is added only after the user has registered a key and live verification is finished. Until both are done, relations does not call Etherscan. A network that only has an enabled routed `etherscan_v2` source is `gap` / `source_gated` / `etherscan_deferred` and sends nothing, even when a key is present.
- `optimism.blockscout.com` answers HTTP 301 to another host (`explorer.optimism.io`, recorded since at least 2026-10-09). This recorded `redirect_host` gap appears only when the endpoint is set to `optimism.blockscout.com`. The default and the recommended origin are in `docs/api-keys.md`. A redirect whose host differs from the registered host is refused. Relations stores that as a `redirect_host` gap and does not follow it.
- Keyless public Blockscout for Base (`base.blockscout.com`), Arbitrum (`arbitrum.blockscout.com`), and Polygon (`polygon.blockscout.com`) has returned HTTP 403 with a Cloudflare page (`Just a moment`) to the recorded Linux host. That result is host-specific. Relations stores the reason `bot_challenge` as a `gap`. The Blockscout call does not set a User-Agent, and this command does not invent one to pass the challenge.
- TRON reads TRC-20 transfers only. There is no native TRX read.
- `--max-pages` stops a walk and the read is `partial` with reason `page_cap`. The TRON client has a fixed cap of 48 requests (`request_cap`); that cap is not `--max-pages`. Each Blockscout tail also stops at 5000 items (`item_cap`) and that part is `partial`. A query count above `--max-nodes` exits 2 and sends nothing. An output row count above `--max-nodes` keeps the first `--max-nodes` rows and the status becomes `partial`. The cut sets `truncated.reason` to `node_cap`. If the status would have been `complete`, it becomes `partial`.
- A hub's extra recipients are high noise. The field is `noise` `high`, and only when `--expand-hub` is set. The default hub report does not list them.
- Shared transactions, assets, amounts, or timing do not show that two addresses are controlled by the same party. Matching by amount does not show that either. An equal amount is not control of both addresses.
- The command reads public chain data for the addresses it was given. It does not query ENS. It does not query off-chain records.

## Not built

These are not implemented in `src/guardian/relations/`:

- Cross-chain bridge pairing. `merged_addresses` does not do it.
- Exchange deposit-address clustering.
- Bitcoin common-input clustering. Bitcoin is `unsupported`.
- Off-chain records, including ENS.

# FAQ and troubleshooting

Package version in this tree: `0.4.0rc4`. The monitor is a read-only preview. The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. The 72h shadow validation is not complete. A failed read is a gap. It is not a zero balance, not a complete history, and not an all-clear.

Commands and flags: [cli-reference.md](cli-reference.md). How to install and how to read a coverage row: [guide-cli.md](guide-cli.md).

## `rate_limited` and HTTP 429 on a free key

Free-tier hosts share a small budget. NodeReal's limit is on the key, shared by every host that key opens, not a separate allowance per chain. The example profiles put `nodereal-bsc` on one budget group, `nodereal-key`, with `max_rps` 1. When the provider returns HTTP 429, the monitor waits (the token bucket, and `Retry-After` on `eth_chainId`) only inside the round deadline, then retries. A row that still cannot be read stays partial, or `identity_unreadable` with cause `rate_limited`. A wait that would pass the deadline is not taken. That row is not a zero balance and not `complete_in_declared_scope`.

Etherscan's published free tier is about 5 calls/second. Blockscout PRO's published free tier is about 5 requests/second. Check the provider's current terms. This project does not buy a plan.

## `identity_unreadable`

The chain identity read did not return a usable answer. On EVM that read is `eth_chainId`. A 429 on that call is retried inside the round deadline; a successful identity is reused for the rest of that run. If it still fails, the reason stays `identity_unreadable` and the cause (often `rate_limited`) stays next to it. Native balance, fungible history, and permission rows for that network are blocked. They are not stored as zero. A chain-id mismatch is a different reason (`chain_id_mismatch`), not a successful read of the wrong network.

## `credential_missing`

The source needs a key, the variable is unset, and the call is not made. `credential_hint` names the variable and an anchor in [api-keys.md](api-keys.md). Put the value in the secret profile or the environment. Init writes empty strings and does not copy a value it finds already set. A missing key is not an empty wallet. BSC external history has no keyless source: without `NODEREAL_API_KEY` that cell stays `credential_missing`.

No-key is a supported mode. Public RPC still runs when its endpoint variable is set. Keyed indexers do not.

## Partial history, lookback, and the request cap

A fresh database does not walk from genesis. With no cursor and no `polling.bootstrap_origin`, the origin is `max(0, head - ceil(bootstrap_lookback_days * 86400 / seconds_per_block))`. Init sets `bootstrap_lookback_days` to 30 and `bootstrap_request_cap` to 2000. The same origin is the start of the approval log scan (`Approval(address,address,uint256)` and `ApprovalForAll`). Logs older than that origin, including approvals that already existed, are not inventoried. `historical_backfill_pending` continues from the saved cursor toward head. It does not walk back to genesis. To inventory older approvals, set `polling.bootstrap_origin`. Built-in block times are ethereum 12s, bsc 0.75s, arbitrum 0.25s, base 2s, optimism 2s, polygon 2.1s, avalanche 2s, and 12s otherwise. When `round_deadline_seconds` is omitted, the EVM log and history scan and the TRON reader each stop after 180 seconds and can report `partial`. Native balance pre-read treats a missing deadline as no limit. Solana and Bitcoin readers do not take that deadline. The sanctions feed read is not passed it. News fetches use a separate 180-second budget measured from the start of the fetch. A whole `run-once` can take longer.

Hitting the cap or the deadline records `partial` with `bootstrap_request_cap` or `deadline`, plus `historical_backfill_pending` while older blocks are still unread. The next round resumes at that checkpoint plus one, minus an overlap of 2. At the default cap, an Arbitrum 30-day log backfill is many rounds if the deadline does not stop a round first. `pagination_incomplete` means a page walk stopped at its cap. That history is not complete. Bitcoin history stops at a page cap the same way: partial, never a finished chain.

## `plan_not_supported` on the free Etherscan plan

The free Etherscan V2 plan returns history for Ethereum, Arbitrum, and Polygon. Base, Optimism, BNB Chain, and Avalanche on that free plan return `plan_not_supported` ("Free API access is not supported for this chain"). The refusal is cached for that source and network. Later wallets and the next round skip straight to the fallback. A cached refusal is not an empty history. Avalanche deep native history is `expansion_pending` in the coverage matrix. Base on Blockscout PRO's free plan is HTTP 402, also `plan_not_supported`, then the keyless Base instance. Optimism on that Blockscout plan returned HTTP 200 in the recorded probe. That is not the Base refusal.

## Network names

Registry ids are the state key. These spellings are the same chain. Typing either one does not create a second baseline:

| Typed name | Canonical id |
| --- | --- |
| arbitrum-mainnet | arbitrum-one |
| arbitrum-one | arbitrum-one |
| polygon-mainnet | polygon-pos |
| polygon-pos | polygon-pos |

`arbitrum-mainnet` and `polygon-mainnet` are the `*-mainnet` names people type because most other chains use that pattern. `arbitrum-one` and `polygon-pos` are the ids in `config/chains`. Accounts, routes, and `enabled_networks` all normalize to the canonical id. Two routes that become the same network and capability with the same sources in the same order collapse to one route. Different source lists, or the same sources in a different order, are a config error: nothing is merged and the stored baseline is not rewritten. Any other name fails. The error lists the valid ids and suggestions, and it quotes the typed name only when that name is an identifier. A URL or an address is shown as `<invalid name>`. A close name is not selected for you.

## Where a key is read

A nonempty value in the private secret profile wins. A blank or missing profile value falls through to the process environment, including a key the process inherited from a parent shell or a service manager. Doctor, coverage, and the adapters use that same rule. After a 401 the monitor does not try the other source. An unattended service should start from a deliberately scoped environment, not from a login shell that already holds keys.

`guardian doctor` and `guardian coverage --mcm-config` print `variable`, `selected_from` (`profile`, `env`, or `absent`), `present`, and `shadowed`. `shadowed` means the source that was not selected also had a nonempty value. The value itself is not printed.

## Doctor, resolver mode, and egress

`guardian doctor` stays offline. It does not open provider connections. `--live` is an explicit probe. Each probe stops at a 5 second whole-call deadline (not only an idle timeout on one socket read). That bound is remaining monotonic time: a request does not start after it expires, and the client for an in-flight request is closed. Unusable resolver settings, including `doh` with endpoints or bootstrap pins that do not load, set `resolver.error`, leave `effective_mode` null, and do not open a provider connection. When the settings load, the probe connects to the validated address and keeps the original hostname for the Host header, SNI, and the certificate check. The same credential selection is used for the report and for those requests. A 401 does not switch source. With `--mcm-config`, `--live` still calls the legacy provider endpoints and is labelled as such: it does not prove the selected profile's connectivity. `providers verify` is that same legacy probe, labelled the same way.

The report keeps four facts apart:

| Field | Meaning |
| --- | --- |
| `resolver.requested_mode` | `system`, `doh`, or `invalid`. An unusable `GUARDIAN_RESOLVER_MODE` (or the env name in the monitor's `resolver` block) is `invalid` and the setting text is not printed |
| `resolver.effective_mode` | The mode the loader actually selected. Null when the settings are not usable |
| `resolver.error` | Null when the settings loaded. Otherwise a short reason, and the raw setting is not printed |
| `resolver.endpoints` | DoH hostnames only. Not URLs, not keys |
| `connectivity.probed` | True only when `--live` ran the probe. Unusable resolver settings leave it false |
| `egress_qualification.qualified` | False here. DoH enabled is not a qualified path |

`GUARDIAN_DOH_ENDPOINTS` is a comma-separated list of HTTPS resolver URLs. `GUARDIAN_DOH_BOOTSTRAP` pins those hosts. See [host-network.md](host-network.md). Formal qualification is `guardian egress-doctor`, not this doctor report.

## DNS and egress blocked

If the system resolver returns an address in `198.18.0.0/15`, the lookup is refused. That range is not allowlisted. Set `GUARDIAN_RESOLVER_MODE=doh` and supply `GUARDIAN_DOH_ENDPOINTS` and `GUARDIAN_DOH_BOOTSTRAP` as [host-network.md](host-network.md) describes. The public config only names those variables.

`guardian egress-doctor` prints `verdict` `OK` or `BLOCKED_EGRESS`. Formal `BLOCKED_EGRESS` means destination enforcement was not shown (on the reviewed host, an SNI proxy). A preview verdict of `OK` on that host is not proof. A failed lookup is not a complete read and not a zero balance. A Cloudflare HTTP 403 page that says `Just a moment...` on keyless Blockscout is `blocked` with `bot_challenge`, and it is host-specific. It is not an empty wallet.

## Sleeping Mac

A laptop that is asleep at the slot does not run that slot. The shipped timers set `Persistent=false`, so systemd does not fire the missed slot on wake as an on-time run. `persistent_counts_as_on_time` is false. The slot recorder stores `reason` `missed`, or `late_start` if the process starts more than 30 minutes late. A later catch-up may fill data and does not count as the missed slot having run on time. `install-service --schedule-mode launchd_agent` prints three LaunchAgents and does not load them. A run coalesced on wake is not an on-time slot. See [scheduling.md](scheduling.md). Host smoke of missed-slot recording is `NOT_RUN`.

## An empty alert outbox is not all clear

`outbox_written` of 0 means no new deduped alert was inserted. It does not mean every capability was read, that history is complete, or that nothing moved. Movements under `native_eth_threshold` or `asset_usd_threshold` are `below_threshold` and are not written. A missing USD quote does not meet the USD rule. Coverage rows can still say `credential_missing`, `partial`, `identity_unreadable`, `rate_limited`, `plan_not_supported`, `feed_not_supplied`, or `unknown`. `--no-send` leaves `sent` at 0 even when rows were written. Default delivery is `outbox_only`. Nothing was sent to Telegram unless you opted in, and the Telegram canary is `NOT_RUN`.

## `run-once` stops with `endpoint_missing`

You enabled a chain but did not set its endpoint variable (for Ethereum, `RPC_ETHEREUM_MAINNET_URL`). Nothing was read. Export a JSON-RPC URL for each enabled chain, then run again. `init` prints the variable names under `data_sharing`.

## `run-once` refuses with a resolver message (benchmark or private address)

Your system resolver answered a public host with a reserved address. Set `GUARDIAN_RESOLVER_MODE=doh` and see `docs/host-network.md`. The monitor refuses rather than connect to an address it cannot trust.

## A first no-key EVM scan can stop around three minutes

When `round_deadline_seconds` is omitted, the EVM log and history scan uses a 180-second deadline. On a free public RPC with no indexer key, history and permission reads can end as `partial` (`pagination_incomplete`, `gaps_present`) and `history.native_external` can be an error with `unsupported_source`. That is the real no-key coverage for that chain. It is not a zero balance and it is not a clean wallet. Other steps are outside that default. `docs/coverage-matrix.md` records a no-key EVM `run-once` that did not finish inside 15 minutes.

## Hard caps in this tree

These are local limits in the readers. A cap leaves a gap. It does not mean the unread range was empty.

| Limit | Where it applies |
| --- | --- |
| Esplora history: 25 transactions per page, at most 4 pages | Bitcoin `txs/chain` walk. A capped walk is partial |
| TronGrid: 200 rows per page, 2 pages, at most 48 requests, 1 request per second in this process | TRON reader. Six HTTP 200s are not a quota |
| Solana signatures: at most 1000 per page, default page cap 20 | `run-once` still walks signatures. `fetch_transactions` is false, so those signatures are not parsed into transfers |
| NodeReal asset transfers: ranges wider than 100000 blocks are split | `nr_getAssetTransfers` |
| Init source budget: 1 request per second, 2000 requests per day, 2 retries | Each budget group `guardian init` writes |
| Quote age: 900 seconds | A CoinGecko token quote older than that is not a fresh USD value |
| Alchemy `LOG_BLOCK_CAP` is 10 | That adapter does not call `eth_getLogs`. The constant is not an approval-scan width |

## A `read_error` can be a program exception

Around each account and network, `run-once` turns an unexpected exception into `read_error` and does not keep a traceback in the report. The rows filled in for that isolated failure are `balances.native`, `history.fungible`, `history.native_external`, `history.token`, `permissions.erc20`, and `permissions.standard`, and only when that row was not already stored. A Solana `balances.fungible` or `permissions.spl_*` row, or a TRON `permissions.tron_account` row, can be absent after that exception. An absent row is not a zero. A Permit2 note read that throws is skipped. A checkpoint supersede that throws is ignored.


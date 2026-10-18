# API keys

Wallet Guardian reads public chain data. Keys are optional. Put them in the private secret profile (the file named by `MONITOR_SECRET_PROFILE`) or in the process environment. Never commit a key, a keyed URL, or a secret profile to this repo. The public config only names the variables.

`.env.local` supplies only `ETHERSCAN_API_KEY`, `GOPLUS_API_KEY`, `DEBANK_ACCESS_KEY`, `ZERION_API_KEY`, `ALCHEMY_API_KEY`, `TENDERLY_ACCESS_KEY`, and `GUARDIAN_MAINNET_SEND`; any other key in that file is ignored and belongs in the process environment or the secret profile.

A nonempty value in the secret profile is used. If that value is missing or blank, the process environment is used. `guardian doctor`, `guardian coverage`, and the read adapters share that resolver. A 401 does not switch to the other source. Doctor and coverage show provenance only: the variable name, `selected_from` (`profile`, `env`, or `absent`), whether a value is present, and whether the other source was shadowed. They do not print the value, a fragment of it, or a URL that contains a key.

Resolver mode is not a key. `GUARDIAN_RESOLVER_MODE`, `GUARDIAN_DOH_ENDPOINTS`, and `GUARDIAN_DOH_BOOTSTRAP` choose system DNS or DNS-over-HTTPS. DoH enabled is not a qualified path. See [host-network.md](host-network.md).

A missing key is never printed. `credential_missing` rows, `guardian config validate`, and `guardian sources probe` name the variable and point here, for example `ETHERSCAN_API_KEY see docs/api-keys.md#etherscan`.

The facts below combine provider documentation with the supplied 2026-10-18 request observations. Observed behavior is labeled as such and can change independently of published limits. Check current terms before relying on a number or endpoint. This project does not buy a plan or turn on automatic upgrades.

For a short end-user setup guide, including Grok Bot secret-card handling, see [free-keys.md](free-keys.md).

`examples/monitor.json` is the keyed profile: each provider is configured, and a missing key falls through to the keyless path when one exists. `examples/monitor.nokeys.json` is the same routes with Helius enabled (so a missing key is visible) and CoinGecko on the public tier (no `credential_env`).

Lido stETH, wstETH, and the withdrawal queue (`lido`, ethereum-mainnet) need no API key: public Ethereum RPC `eth_call` only, and the plugin is opt-in (`plugins`: `lido`). offline fixtures; live probe: see docs/coverage-matrix.md.

<a id="etherscan"></a>

## Etherscan

Signup: <https://etherscan.io/apis>. One key covers the V2 multichain API.

What it unlocks: `history.native_external` on Ethereum, Arbitrum, and Polygon (transaction list). Token history on those chains still prefers public RPC logs.

Free tier: about 3 calls/second (a free key received `Max calls per sec rate limit reached (3/sec)` on 2026-10-18) and 100,000 calls/day; check current terms.

Free tier, checked 2026-10-18; check current terms: Ethereum, Arbitrum, Polygon, Linea, Sonic, Unichain, HyperEVM, Mantle, Celo, Berachain, Monad, Sei, and Plasma are included. BSC, Base, Optimism, Avalanche, and Gnosis are paid-only except for source-code and ABI calls. Since 2026-07-01 the free tier returns at most 1,000 records per request. Arbitrum moves to a shared free pool on 2026-11-01. A chain the plan refuses is `plan_not_supported`. That result is cached, with a TTL, for the source and network. Later wallets, `tokentx`, and the next round skip straight to the approved fallback. An indexer source with `enabled: false` is not called. An `ofac_sls` source is the exception in the OFAC section: `run-once` still fetches it when the endpoint is set.

A fresh database does not walk from genesis. With no cursor and no `polling.bootstrap_origin`, the origin is `max(0, head - ceil(bootstrap_lookback_days * 86400 / seconds_per_block))`. Built-in block times are ethereum 12s, bsc 0.75s, arbitrum 0.25s, base 2s, optimism 2s, polygon 2.1s, avalanche 2s, and 12s otherwise. `polling.seconds_per_block` can override one network. Coverage is `complete_in_declared_scope` for that window only. `polling.bootstrap_request_cap` (2000 in the example) is the per-source, per-round budget for bootstrap and catch-up, counting every indexer page, every bisect, and every `eth_getLogs` (a batch counts as its log methods). When `round_deadline_seconds` is omitted, the EVM log and history scan stops after 180 seconds and can record `partial`. That default does not bound every step of `run-once`. Hitting the cap or the deadline records partial with `bootstrap_request_cap` or `deadline`, plus `historical_backfill_pending` while older blocks are still unread. The next round resumes at that checkpoint plus one, minus the overlap of 2. At the default cap, an Arbitrum 30-day log backfill (10,368,000 blocks, about 20,728 log reads) is 12 rounds if the deadline does not stop a round first. Successful DNS answers are cached in-process for min(record TTL, 300 seconds). A private address is rejected and not cached.

Environment:

- `ETHERSCAN_API_KEY`
- `ETHERSCAN_API_BASE` (the API origin; the client calls `/v2/api`)

Without the key: in the example profiles, Ethereum and Avalanche native history try keyless Routescan after Etherscan. Other configured fallbacks then run in their documented order. The per-chain Arbitrum and Polygon Blockscout hosts received a Cloudflare challenge in the 2026-10-18 observations, so they are not reliable keyless history paths; a challenge is `blocked`, never an empty history. A profile written by `guardian init` does not include Routescan, Blockscout PRO, or keyless Blockscout. It adds Etherscan (except BSC) ahead of a per-chain Blockscout source that needs its own key. Avalanche is included (`blockscout-avalanchec`). `plan_not_supported` comes from the Etherscan response for that chain, not from a fixed list of chain names. When no approved fallback succeeds, `history.native_external` stays `credential_missing`, `plan_not_supported`, or another explicit gap. Token history uses public RPC `eth_getLogs` with the configured chunk size. BSC does not use Etherscan.

<a id="routescan"></a>

## Routescan

Routescan exposes an Etherscan-compatible API at `https://api.routescan.io/v2/network/mainnet/evm/<chainId>/etherscan/api`. The example profiles use that API as a fallback after Etherscan for `history.native_external` on Ethereum (chain id 1) and Avalanche C-Chain (chain id 43114). It works without a key. An optional key uses `ROUTESCAN_API_KEY`. Profiles written by `guardian init` do not currently add this source.

Limits checked 2026-10-18; check current terms: keyless access is about 2 requests/second and 10,000 calls/day. An optional free key raises that to 5 requests/second and 100,000 calls/day. The examples use a conservative `max_rps` of 1. Chains 10, 56, 8453, 42161, and 137 answered `chain not supported` in the supplied research. That response is `plan_not_supported`, never an empty history.

Environment:

- `ROUTESCAN_API_KEY` (optional; omitted for keyless access)
- `ROUTESCAN_API_BASE` (required endpoint setting; suggested value `https://api.routescan.io`; there is no built-in default, and the client builds the chain-specific path)

Without the key: Ethereum and Avalanche history still use the configured Routescan fallback. A failed, limited, or unsupported read remains a coverage gap.

<a id="nodereal"></a>

## NodeReal

Signup: <https://nodereal.io>. `nr_getAssetTransfers` is documented for Ethereum and BSC only. Take the URL from the dashboard you actually enabled. Do not invent a host.

What it unlocks: BSC `history.native_external` (external transfers, both directions, full `pageKey`). Token categories are a separate capability. A refused method is `method_not_found` or `unauthorized` (`blocked`), not an empty wallet.

`examples/monitor.json` also names `bsc-logs-nodereal` for BSC `history.token` and `permissions.standard`. It reuses `NODEREAL_BSC_URL` and the `nodereal-key` budget group, with `range_limit` 2000. The key stays in that URL's `/v1/<key>` path. Logs and `budget.providers` mask the key segment and keep the host prefix. Do not commit the URL. The public host `bsc.nodereal.io` is not this source: the 2026-10-14 review recorded `-32005` there. The keyed getLogs width is the configured example width, not a live probe. This package did not re-run that review.

Free tier: the limit is on the API key, shared by every host that key opens (BSC, Ethereum, Optimism, and any other chain on that same key). It is not a separate allowance per chain. NodeReal's free tier is a compute-unit-per-second limit. Check the dashboard for the current number. This repo does not assume a paid CU pack.

`examples/monitor.json` and `examples/monitor.nokeys.json` put the NodeReal BSC source `nodereal-bsc` on one budget group, `nodereal-key`, with `max_rps` 1 and `max_concurrency` 1. That is a conservative default so one free key is not burst by this process. Several sources may name that same `budget_group`; they then share one token bucket.

Ethereum and Optimism RPC sources stay on `rpc-ethereum-mainnet` and `rpc-optimism-mainnet` in those examples. Those endpoints are whatever you put in `RPC_ETHEREUM_MAINNET_URL` and `RPC_OPTIMISM_MAINNET_URL`. The monitor does not read the host and does not guess that a URL is NodeReal. If the URL you copied from the NodeReal dashboard is a NodeReal host, set that source's `budget_group` to `nodereal-key` (the group must exist under `budgets` with the same limits). Do this for every NodeReal host that uses the same key. Sources that share `NODEREAL_API_KEY` already have to name one budget group.

When that shared limit is exceeded the provider returns HTTP 429. The monitor waits (the token bucket, and Retry-After on `eth_chainId`) inside the round deadline and retries. A row that still cannot be read is partial, or `identity_unreadable` with `rate_limited`. It is not a zero balance and it is not complete. A wait that would pass the round deadline is not taken.

Environment:

- `NODEREAL_API_KEY`
- `NODEREAL_BSC_URL`

Without the key: BSC native history in `run-once` is `credential_missing`. There is no keyless BSC native-history source. BSC token logs use `bsc-logs-48club` when `RPC_BSC_LOGS_URL` is set. If that variable is also unset, the monitor log row is a gap, not an empty history. `guardian relations` is separate: with no NodeReal key it reads ERC-20 logs from `https://rpc-bsc.48.club` unless `RPC_BSC_LOGS_URL` names another host. Native and internal BNB stay a gap on that path (`not_in_keyless_logs`). A failed or cut relations read is `gap` or `partial`, not an empty list and not `complete`. The monitor profile still has no default 48club URL. dataseed and publicnode are not topic-only log sources.

<a id="blockscout"></a>

## Blockscout PRO

Signup: <https://dev.blockscout.com>. Since 2026-07-01 all Blockscout API calls require a PRO key. The free PRO plan is a key, not a paid subscription. Checked 2026-10-18; check current terms.

What it unlocks: Etherscan-compatible account history (`txlist`, `tokentx`, and logs when the plan allows them) for chains the free plan includes. In the 2026-10-18 checks, the free key answered HTTP 402 for featured chains Base (chain id 8453) and Polygon (chain id 137), and HTTP 404 for BSC. Those results are gaps such as `plan_not_supported`, never empty history. The refusal is cached.

Free tier, checked 2026-10-18; check current terms: 100,000 credits/day, 5 requests/second, about 20 credits for a basic call. 100,000 credits is not 100,000 requests.

The key is sent only as `Authorization: Bearer`. It is never placed in the URL or the query string, and it is never written into logs or evidence.

Environment:

- `BLOCKSCOUT_API_KEY`
- `BLOCKSCOUT_PRO_API_BASE` (required endpoint setting; suggested value `https://api.blockscout.com`; there is no built-in default, and the client calls `/v2/api?chain_id=<id>`)

Without the key, when `BLOCKSCOUT_PRO_API_BASE` is set, the PRO route in `examples/monitor.json` still calls that host once and can return HTTP 402 (`Proceed with API key or make a X402 payment`). The watch address is on that request. That 402 is `credential_missing`, and that example profile continues to the next configured source. If the endpoint variable is unset, the source is skipped. A body that says the chain is outside the plan is `plan_not_supported` instead. A profile written by `guardian init` has no PRO source and no keyless Blockscout source. On an init profile, when Etherscan has no key, or the plan refuses that chain, and the per-chain Blockscout key is also missing, the history cell stays `credential_missing` or `plan_not_supported`. An Etherscan key whose plan accepts the chain can fill the cell without the Blockscout key. That outcome is not limited to a fixed list of chain names.

Legacy per-chain hosts were checked 2026-10-18 and may stop working without a key at any time. Set the origin; the client calls `/api`:

- Ethereum: suggested `BLOCKSCOUT_ETH_URL=https://eth.blockscout.com`; it answered keyless.
- Base: suggested `BLOCKSCOUT_BASE_PUBLIC_URL=https://base.blockscout.com`; scripts received a Cloudflare HTTP 403 challenge.
- Optimism: suggested `BLOCKSCOUT_OPTIMISM_PUBLIC_URL=https://explorer.optimism.io`; it answered keyless. (`https://optimism.blockscout.com` redirects to it; set the final origin because a cross-host redirect is refused as `redirect_host`.) `guardian relations` reads `https://explorer.optimism.io` when that variable is unset or still names `optimism.blockscout.com`. It does not follow the redirect. `run-once` uses only the endpoint resolved from its profile.
- Arbitrum: suggested `BLOCKSCOUT_ARBITRUM_URL=https://arbitrum.blockscout.com`; scripts received a Cloudflare HTTP 403 challenge.
- Polygon: suggested `BLOCKSCOUT_POLYGON_URL=https://polygon.blockscout.com`; scripts received a Cloudflare HTTP 403 challenge.
- Gnosis: its per-chain host answered keyless in the supplied research, but the example profiles do not currently name a Gnosis Blockscout URL.

These suggested values are not built-in `run-once` defaults. If an endpoint variable is unset, that source is skipped. The `guardian relations` Optimism behavior described above is a separate exception.

A Cloudflare HTTP 403 HTML page that says `Just a moment...` is `blocked` with reason `bot_challenge`. It is not parsed as an empty wallet.

Older per-chain variables still work for a keyed instance you configure yourself: `BLOCKSCOUT_BASE_URL` / `BLOCKSCOUT_BASE_API_KEY` and `BLOCKSCOUT_OPTIMISM_URL` / `BLOCKSCOUT_OPTIMISM_API_KEY`. Those keys also travel as a bearer header, not in the URL.

In `examples/monitor.json`, the order for Base and Optimism is PRO when the key is set and the plan includes the chain, then the keyless instance, then an honest gap. `guardian init` does not write the PRO source or the keyless Blockscout source.

<a id="alchemy"></a>

## Alchemy

Alchemy Transfers is the preferred Base source for external and token transfers in `examples/monitor.json`, when you set `ALCHEMY_API_KEY`. Only that key is needed for Base; `ALCHEMY_TRANSFERS_URL` is an optional override. `guardian init` does not add Alchemy, Blockscout PRO, or a keyless Blockscout source. On an init profile, when Etherscan has no key, or the plan refuses that chain, and the per-chain Blockscout key is also missing, the history cell stays `credential_missing` or `plan_not_supported`. An Etherscan key whose plan accepts the chain can fill the cell without the Blockscout key. Alchemy does not replace Approval or `ApprovalForAll` logs. Those stay on the Base log route (`tenderly_base`, then `base-1rpc` only when that source is enabled and the remaining budget can catch up).

A missing key does not crash the run. In `examples/monitor.json` the monitor records `credential_missing` with a pointer to this section, then continues with Blockscout PRO and the keyless Base instance. Those Blockscout gaps stay `plan_not_supported` or `blocked` / `bot_challenge`. They are not an empty wallet. Base stays a required network. An init profile has neither of those fallback sources.

Signup:

1. Create an account at <https://www.alchemy.com>.
2. Open the dashboard and create an app.
3. Select Base Mainnet.
4. Put the app key in `ALCHEMY_API_KEY` in the private secret profile or process environment.
5. Optionally set `ALCHEMY_TRANSFERS_URL` to the keyed HTTPS URL from the dashboard to override the endpoint.

When `ALCHEMY_TRANSFERS_URL` is unset or blank, this Base-only adapter derives `https://base-mainnet.g.alchemy.com/v2/<key>` from `ALCHEMY_API_KEY`. An explicit endpoint wins. Doctor, coverage, and provider provenance identify the derived endpoint as `derived_from_ALCHEMY_API_KEY` without its value.

Do not commit the URL or the key. The public config only names `ALCHEMY_TRANSFERS_URL` and `ALCHEMY_API_KEY`. Logs and reports redact an Alchemy `/v2/` key.

Free tier, checked against Alchemy's published tables on 2026-10-18 (an estimate, not a live quota read); check current terms:

- About 30,000,000 compute units per month and 300 compute units per second.
- `alchemy_getAssetTransfers` is 120 compute units per call. The run report records that under `budget.providers` with `credits_basis` `estimate`.
- Categories requested: `external`, `erc20`, `erc721`, `erc1155`. `fromAddress` and `toAddress` are separate calls. `pageKey` is followed until it is absent. `fromBlock` and `toBlock` stay a fixed window.
- Free `eth_getLogs` is 10 blocks per query. That method is not the approval scan, and this adapter does not call it.

Base log endpoints, if you set them yourself:

- `TENDERLY_BASE_URL`, for example `https://base.gateway.tenderly.co`, `range_limit` 1000. No key.
- `BASE_1RPC_URL`, for example `https://1rpc.io/base`, `range_limit` 50. The example leaves `base-1rpc` disabled. It runs only after you enable it, and only when the remaining daily budget can cover the catch-up.
- `RPC_BASE_DRPC_URL`, `range_limit` 100. The example leaves `base-logs-drpc` disabled. The 2026-10-14 review recorded Base dRPC accepting 100 blocks and rejecting 1000 with "ranges over". No default URL is set. See [Public log endpoints](#logs).

<a id="logs"></a>

## Public log endpoints

These sources do not take an API key. The public config names the environment variable. It does not contain a URL, and it does not contain a key. A missing variable skips that source. A read that fails stays a gap. It is not an empty wallet and it is not complete. Wallet addresses travel in `eth_getLogs` topics, so the first call records an `indexer_disclosures` row. `privacy.third_party_wallet_queries` stays `deny_until_local_consent` until you set `operator_approved_sources_only`.

The observations below were checked 2026-10-18 in the supplied research. Check current terms and endpoint behavior before relying on them. This package did not repeat those network calls.

- `RPC_BSC_LOGS_URL` is `bsc-logs-48club`, `range_limit` 1000. The monitor has no default endpoint. There are no docs and no SLA. The operator is an MEV builder. Earlier review evidence recorded HTTP 200 at 100 and 1000 blocks; that is not a finished history. `guardian relations` uses `https://rpc-bsc.48.club` for its own keyless ERC-20 read when this variable is unset, and does not copy that URL into the profile. A rate limit or a failed chunk stays `gap` or `partial`. On 2026-10-18, BSC dataseed returned `limit exceeded` at 10 blocks, and PublicNode refused topic-only `eth_getLogs` because it requires an address. Neither is a topic-only log source. The 48club privacy page cited by the earlier review is <https://docs.48.club/privacy-rpc.md>.
- `RPC_OPTIMISM_LOGS_URL` is `op-logs-public`, `range_limit` 2000. `https://mainnet.optimism.io` accepted topic-only 10,000-block scans. Optimism documents the public endpoint as not suitable for production: <https://docs.optimism.io/app-developers/reference/rpc-providers>.
- Arbitrum's `https://arb1.arbitrum.io` accepted topic-only 10,000-block scans. This observation does not add an endpoint to the public config.
- dRPC free endpoints accepted 100 blocks and rejected 150 even though the returned error text said 10,000. `RPC_BASE_DRPC_URL` remains `range_limit` 100 and disabled in the examples.
- `https://mainnet.base.org` returned HTTP 429 even at 10 blocks.
- `https://api.avax.network` accepted at most 100 blocks per `eth_getLogs` request.

<a id="service-changes"></a>

## Services that changed in 2025-2026

Checked 2026-10-18; check current terms. Items marked third-party research were supplied by the research summary and were not verified by this repository or integrated into its runtime.

- Sim by Dune shut down on 2026-08-01. Moralis ended its free Data API on 2026-09-01. Alchemy deprecated `alchemy_simulate*` on 2026-09-30. Tenderly Free has no API. These affect simulation or data-source choices; they are third-party research facts.
- Pyth Hermes has required a key since 2026-08-26. This is a third-party research fact; Wallet Guardian does not currently use Hermes.
- Chainalysis deprecated its on-chain sanctions oracle, last updated 2026-03-18. Do not use it as the only sanctions check. This is a third-party research fact; this repo uses a supplied OFAC list instead.
- Tronscan requires a key. This is a third-party research fact; the current Tron reader uses optional `TRON_PRO_API_KEY` for TronGrid, not Tronscan.
- Sui Foundation full nodes removed JSON-RPC; use GraphQL for current Sui integrations. This is a third-party research fact; the current family reader is not implemented.
- LINE Notify ended on 2025-03-31. This is a third-party research fact; Wallet Guardian does not provide a LINE sender.
- `api.binance.com` may answer HTTP 451. `data-api.binance.vision` is Binance's public market-data host. This is a third-party research fact; the current price adapters do not use Binance.

<a id="goplus"></a>

## GoPlus

Signup: <https://gopluslabs.io>.

What it unlocks: nothing in this preview. `run-once` does not call GoPlus. With or without a key and consent, the `address_risk` row is a blocked gap (`credential_missing`, `third_party_denied`, or `not_run`). No risk screening is performed. The privacy policy is unchanged: `third_party_wallet_queries` stays `deny_until_local_consent` unless you set `operator_approved_sources_only`. A deny still means the wallet address is not sent.

Environment:

- `GOPLUS_API_KEY` (sent as a bearer header)
- `GOPLUS_API_BASE`

Without the key: `address_risk` and `asset_risk` are `credential_missing`. That is not a clean bill of health.

Free tier: check current terms on the GoPlus dashboard.

<a id="coingecko"></a>

## CoinGecko

Signup: <https://www.coingecko.com/en/api>.

What it unlocks: native-coin USD quotes for native transfers and balance deltas (`simple/price`), and, when the source is enabled, free contract quotes for token transfers (`simple/token_price`, `contract_addresses`). A fresh token quote can meet `asset_usd_threshold`. `init` does not add a CoinGecko source. A missing, stale, or failed quote is `valuation_unknown`. It is never stored as 0 and never stored as 1. Decimals come from Etherscan or one cached `decimals()` call per contract per round, and an unreadable value is not 0. Without a quote, a pinned stablecoin whose raw size meets that USD floor, or a hit on `alerts.token_raw_thresholds` (`network:0xcontract` to a positive raw integer), writes an `unvalued_large_move` alert. That alert is not a USD valuation.

The adapter already calls the public keyless tier when `COINGECKO_API_KEY` is absent. That tier has a lower rate limit than a demo key. Check current terms; the public tier is commonly on the order of tens of calls per minute, and a demo key raises the monthly cap.

Environment:

- `COINGECKO_API_KEY` (optional; query parameter, omitted when absent)
- `COINGECKO_API_BASE`

`examples/monitor.nokeys.json` leaves `credential_env` off so the public tier is the configured source. `examples/monitor.json` names the key; if the value is absent the source is `keyless`, not blocked.

<a id="helius"></a>

## Helius

Signup: <https://www.helius.dev>.

What it unlocks: nothing in this preview. `run-once` does not call Helius. The adapter exists and is tested offline, but the pipeline does not import it. Setting `HELIUS_API_KEY` changes nothing in a run. Solana transactions are not parsed, so there are no SOL/SPL transfer rows and no transfer alerts. Only the signature list is read. Those events would not mark plain RPC coverage complete and would not replace on-chain amounts.

Environment:

- `HELIUS_API_KEY`
- `HELIUS_PARSED_EVENTS_URL`

Without the key: parsed events are `credential_missing`. The configured Solana RPC is still used for balances and account reads. In `examples/monitor.json` the Helius source is disabled, so it is not called. `examples/monitor.nokeys.json` enables it so the missing key is reported. `guardian init` writes `helius-parser` with `enabled` true and lists `HELIUS_API_KEY` when a Solana account is included. `run-once` does not call that adapter. The variable can be left empty.

Free tier: check current terms. This repo does not upgrade the plan.

<a id="ofac"></a>

## OFAC SLS

The sanctions list is a public feed. It does not use an API key. The monitor matches addresses locally and does not upload a watch address.

Environment:

- `OFAC_SLS_FEED` (a `file://` path of a local SDN XML file you downloaded, or the HTTPS URL of the list you choose to supply)

A `file://` value is read from disk, for example `file:///absolute/path/to/sdn.xml`. An HTTPS value is fetched with GET. The path has to be one this adapter already allows (`/api/download/`, `/api/PublicationPreview/`, or `/exports/`). No API key is added to the request.

If the endpoint is empty, or the monitor file has no source with `"adapter": "ofac_sls"`, sanctions coverage is `feed_not_supplied`. `run-once` takes the first `ofac_sls` source and does not read `enabled`. `enabled: false` still fetches the list when the endpoint is set. Remove the source or clear the endpoint to avoid the fetch. The variable name is whatever `endpoint_env` says; `examples/monitor.json` uses `OFAC_SLS_FEED`. `guardian init` stores the list name `us-ofac-sdn` and does not add that source, so `OFAC_SLS_FEED` alone does nothing on an init profile. `examples/monitor.json` includes the source. Setting the variable names the feed for that source. It does not by itself mark a wallet as screened. A list whose publish date is older than 7 days is `partial` with reason `feed_stale`, not a clear screen. A malformed list is an error, not a miss and not zero. The report records the source id, the fetch time, the publish date, the validity, and how many addresses were checked. It does not record the list body. Check the publisher's current file location and terms. An empty balance is not "not sanctioned."

<a id="esplora"></a>

## Esplora (Bitcoin)

Esplora is a public, keyless HTTP API. There is no API key. Shared public instances have shared rate limits; check the operator's current terms before you depend on one.

The monitor stores only the environment variable name. You supply the base URL yourself. A base is an origin, or an origin whose path is `/api`. These two public bases are keyless:

- `https://blockstream.info/api`
- `https://mempool.space/api`

Environment:

- `ESPLORA_API_URL` (the base you choose; no key and no query secret)

Do not put that URL in `examples/` or in a commit. A failed read is not a zero balance. Mempool amounts are not added to the confirmed balance. One address is an address set, not a whole HD wallet.

offline fixtures; live probe: see docs/coverage-matrix.md

<a id="keys"></a>

## Where the values live

1. Create a secret profile outside the git work tree. Mode `0600` where the OS enforces it.
2. Point `MONITOR_SECRET_PROFILE` at that file. Point `MONITOR_ACCOUNTS_FILE` and `MONITOR_STATE_DIR` at private paths too.
3. Keys and URLs are strings in that JSON object. Names look like `ETHERSCAN_API_KEY`. Values never go in `examples/`, `docs/`, or a commit.
4. `guardian config validate` and `guardian sources probe` print the variable name and a link on this page when a credential is missing. They do not print the value.

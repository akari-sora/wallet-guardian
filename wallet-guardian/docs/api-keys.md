# API keys

Wallet Guardian reads public chain data. Keys are optional. Put them in the private secret profile (the file named by `MONITOR_SECRET_PROFILE`) or in the process environment. Never commit a key, a keyed URL, or a secret profile to this repo. The public config only names the variables.

A nonempty value in the secret profile is used. If that value is missing or blank, the process environment is used. `guardian doctor`, `guardian coverage`, and the read adapters share that resolver. A 401 does not switch to the other source. Doctor and coverage show provenance only: the variable name, `selected_from` (`profile`, `env`, or `absent`), whether a value is present, and whether the other source was shadowed. They do not print the value, a fragment of it, or a URL that contains a key.

Resolver mode is not a key. `GUARDIAN_RESOLVER_MODE`, `GUARDIAN_DOH_ENDPOINTS`, and `GUARDIAN_DOH_BOOTSTRAP` choose system DNS or DNS-over-HTTPS. DoH enabled is not a qualified path. See [host-network.md](host-network.md).

A missing key is never printed. `credential_missing` rows, `guardian config validate`, and `guardian sources probe` name the variable and point here, for example `ETHERSCAN_API_KEY see docs/api-keys.md#etherscan`.

Limits below are the provider's published free tier at the time this note was written. Check current terms before you rely on a number. This project does not buy a plan or turn on automatic upgrades.

`examples/monitor.json` is the keyed profile: each provider is configured, and a missing key falls through to the keyless path when one exists. `examples/monitor.nokeys.json` is the same routes with Helius enabled (so a missing key is visible) and CoinGecko on the public tier (no `credential_env`).

Lido stETH, wstETH, and the withdrawal queue (`lido`, ethereum-mainnet) need no API key: public Ethereum RPC `eth_call` only, and the plugin is opt-in (`plugins`: `lido`). offline fixtures; live probe: see docs/coverage-matrix.md.

<a id="etherscan"></a>

## Etherscan

Signup: <https://etherscan.io/apis>. One key covers the V2 multichain API.

What it unlocks: `history.native_external` on Ethereum, Arbitrum, and Polygon (transaction list). Token history on those chains still prefers public RPC logs.

Free tier, check current terms: about 5 calls/second and 100,000 calls/day. The free plan does not include every chain. A chain the plan refuses is `plan_not_supported`. That result is cached, with a TTL, for the source and network. Later wallets, `tokentx`, and the next round skip straight to the approved fallback. A source with `enabled: false` is not called.

A fresh database does not walk from genesis. With no cursor and no `polling.bootstrap_origin`, the origin is `max(0, head - ceil(bootstrap_lookback_days * 86400 / seconds_per_block))`. Built-in block times are ethereum 12s, bsc 0.75s, arbitrum 0.25s, base 2s, optimism 2s, polygon 2.1s, avalanche 2s, and 12s otherwise. `polling.seconds_per_block` can override one network. Coverage is `complete_in_declared_scope` for that window only. `polling.bootstrap_request_cap` (2000 in the example) is the per-source, per-round budget for bootstrap and catch-up, counting every indexer page, every bisect, and every `eth_getLogs` (a batch counts as its log methods). When `round_deadline_seconds` is omitted the round stops after 180 seconds. Hitting the cap or the deadline records partial with `bootstrap_request_cap` or `deadline`, plus `historical_backfill_pending` while older blocks are still unread. The next round resumes at that checkpoint plus one, minus the overlap of 2. At the default cap, an Arbitrum 30-day log backfill (10,368,000 blocks, about 20,728 log reads) is 12 rounds if the deadline does not stop a round first. Successful DNS answers are cached in-process for min(record TTL, 300 seconds). A private address is rejected and not cached.

Environment:

- `ETHERSCAN_API_KEY`
- `ETHERSCAN_API_BASE` (the API origin; the client calls `/v2/api`)

Without the key: native history uses a Blockscout keyless instance when that chain has one (Ethereum, Arbitrum, Polygon, Base, Optimism). Otherwise the row is `credential_missing`. Token history uses public RPC `eth_getLogs` with the configured chunk size. BSC does not use Etherscan.

<a id="nodereal"></a>

## NodeReal

Signup: <https://nodereal.io>. `nr_getAssetTransfers` is documented for Ethereum and BSC only. Take the URL from the dashboard you actually enabled. Do not invent a host.

What it unlocks: BSC `history.native_external` (external transfers, both directions, full `pageKey`). Token categories are a separate capability. A refused method is `method_not_found` or `unauthorized` (`blocked`), not an empty wallet.

Free tier: the limit is on the API key, shared by every host that key opens (BSC, Ethereum, Optimism, and any other chain on that same key). It is not a separate allowance per chain. NodeReal's free tier is a compute-unit-per-second limit. Check the dashboard for the current number. This repo does not assume a paid CU pack.

`examples/monitor.json` and `examples/monitor.nokeys.json` put the NodeReal BSC source `nodereal-bsc` on one budget group, `nodereal-key`, with `max_rps` 1 and `max_concurrency` 1. That is a conservative default so one free key is not burst by this process. Several sources may name that same `budget_group`; they then share one token bucket.

Ethereum and Optimism RPC sources stay on `rpc-ethereum-mainnet` and `rpc-optimism-mainnet` in those examples. Those endpoints are whatever you put in `RPC_ETHEREUM_MAINNET_URL` and `RPC_OPTIMISM_MAINNET_URL`. The monitor does not read the host and does not guess that a URL is NodeReal. If the URL you copied from the NodeReal dashboard is a NodeReal host, set that source's `budget_group` to `nodereal-key` (the group must exist under `budgets` with the same limits). Do this for every NodeReal host that uses the same key. Sources that share `NODEREAL_API_KEY` already have to name one budget group.

When that shared limit is exceeded the provider returns HTTP 429. The monitor waits (the token bucket, and Retry-After on `eth_chainId`) inside the round deadline and retries. A row that still cannot be read is partial, or `identity_unreadable` with `rate_limited`. It is not a zero balance and it is not complete. A wait that would pass the round deadline is not taken.

Environment:

- `NODEREAL_API_KEY`
- `NODEREAL_BSC_URL`

Without the key: BSC native history is `credential_missing`. There is no keyless BSC native-history source. BSC token history still uses public RPC logs when that endpoint accepts a topic-only filter; if it rejects even one block, the row stays a gap.

<a id="blockscout"></a>

## Blockscout PRO

Signup: <https://dev.blockscout.com>. The free PRO plan is a key, not a paid subscription.

What it unlocks: Etherscan-compatible account history (`txlist`, `tokentx`, and logs when the plan allows them) for chains the free plan includes. Optimism (chain id 10) is on that plan. Base (chain id 8453) is a featured chain and the free PRO plan answers HTTP 402; that is `plan_not_supported`, then the keyless Base instance. The refusal is cached. It is not an empty history.

Free tier, check current terms: 100,000 credits/day, 5 requests/second, about 20 credits for a basic call. 100,000 credits is not 100,000 requests.

The key is sent only as `Authorization: Bearer`. It is never placed in the URL or the query string, and it is never written into logs or evidence.

Environment:

- `BLOCKSCOUT_API_KEY`
- `BLOCKSCOUT_PRO_API_BASE` (default `https://api.blockscout.com`; the client calls `/v2/api?chain_id=<id>`)

Without the key the PRO route returns HTTP 402 (`Proceed with API key or make a X402 payment`). That is `credential_missing`, and the run continues to the keyless instance.

Keyless instances (no key, shared rate limit about 5–10 requests/second, check current terms). Set the origin; the client calls `/api`:

- Ethereum: `BLOCKSCOUT_ETH_URL` default `https://eth.blockscout.com`
- Base: `BLOCKSCOUT_BASE_PUBLIC_URL` default `https://base.blockscout.com`
- Optimism: `BLOCKSCOUT_OPTIMISM_PUBLIC_URL` default `https://explorer.optimism.io` (`https://optimism.blockscout.com` answers HTTP 301 to it since at least 2026-10-09; set the final origin, a cross-host redirect is refused as redirect_host)
- Arbitrum: `BLOCKSCOUT_ARBITRUM_URL` default `https://arbitrum.blockscout.com`
- Polygon: `BLOCKSCOUT_POLYGON_URL` default `https://polygon.blockscout.com`

A Cloudflare HTTP 403 HTML page that says `Just a moment...` is `blocked` with reason `bot_challenge`. It is not parsed as an empty wallet.

Older per-chain variables still work for a keyed instance you configure yourself: `BLOCKSCOUT_BASE_URL` / `BLOCKSCOUT_BASE_API_KEY` and `BLOCKSCOUT_OPTIMISM_URL` / `BLOCKSCOUT_OPTIMISM_API_KEY`. Those keys also travel as a bearer header, not in the URL.

Order for Base and Optimism: PRO when the key is set and the plan includes the chain, then the keyless instance, then an honest gap.

<a id="alchemy"></a>

## Alchemy

Alchemy Transfers is the preferred Base source for external and token transfers when you set both env names below. It does not replace Approval or `ApprovalForAll` logs. Those stay on the Base log route (`tenderly_base`, then `base-1rpc` only when that source is enabled and the remaining budget can catch up).

A missing key does not crash the run. The monitor records `credential_missing` with a pointer to this section, then continues with Blockscout PRO and the keyless Base instance. Those Blockscout gaps stay `plan_not_supported` or `blocked` / `bot_challenge`. They are not an empty wallet. Base stays a required network.

Signup:

1. Create an account at <https://www.alchemy.com>.
2. Open the dashboard and create an app.
3. Select Base Mainnet.
4. Copy the HTTPS URL the dashboard shows for that app. It already contains the key in the path (`/v2/<key>`).
5. Put that URL in the private secret profile as `ALCHEMY_TRANSFERS_URL`.
6. Put the same key in `ALCHEMY_API_KEY`. The monitor uses the name as the gate. It does not append the key again.

Do not commit the URL or the key. The public config only names `ALCHEMY_TRANSFERS_URL` and `ALCHEMY_API_KEY`. Logs and reports redact an Alchemy `/v2/` key.

Free tier, checked against Alchemy's published tables on 2026-10-09 (an estimate, not a live quota read):

- About 30,000,000 compute units per month and 300 compute units per second.
- `alchemy_getAssetTransfers` is 120 compute units per call. The run report records that under `budget.providers` with `credits_basis` `estimate`.
- Categories requested: `external`, `erc20`, `erc721`, `erc1155`. `fromAddress` and `toAddress` are separate calls. `pageKey` is followed until it is absent. `fromBlock` and `toBlock` stay a fixed window.
- Free `eth_getLogs` is 10 blocks per query. That method is not the approval scan, and this adapter does not call it.

Base log endpoints, if you set them yourself:

- `TENDERLY_BASE_URL`, for example `https://base.gateway.tenderly.co`, `range_limit` 1000. No key.
- `BASE_1RPC_URL`, for example `https://1rpc.io/base`, `range_limit` 50. The example leaves `base-1rpc` disabled. It runs only after you enable it, and only when the remaining daily budget can cover the catch-up.

<a id="goplus"></a>

## GoPlus

Signup: <https://gopluslabs.io>.

What it unlocks: address and asset risk lookups. The privacy policy is unchanged: `third_party_wallet_queries` stays `deny_until_local_consent` unless you set `operator_approved_sources_only`. A deny still means the wallet address is not sent.

Environment:

- `GOPLUS_API_KEY` (sent as a bearer header)
- `GOPLUS_API_BASE`

Without the key: `address_risk` and `asset_risk` are `credential_missing`. That is not a clean bill of health.

Free tier: check current terms on the GoPlus dashboard.

<a id="coingecko"></a>

## CoinGecko

Signup: <https://www.coingecko.com/en/api>.

What it unlocks: native-coin USD quotes for native transfers and balance deltas (`simple/price`), and, when the source is enabled, free contract quotes for token transfers (`simple/token_price`, `contract_addresses`). A fresh token quote can meet `asset_usd_threshold`. `init` does not add a CoinGecko source. A missing, stale, or failed quote is `valuation_unknown`. It is never stored as 0 and never stored as 1. Without a quote, a pinned stablecoin whose raw size meets that USD floor, or a hit on `alerts.token_raw_thresholds` (`network:0xcontract` to a positive raw integer), writes an `unvalued_large_move` alert. That alert is not a USD valuation.

The adapter already calls the public keyless tier when `COINGECKO_API_KEY` is absent. That tier has a lower rate limit than a demo key. Check current terms; the public tier is commonly on the order of tens of calls per minute, and a demo key raises the monthly cap.

Environment:

- `COINGECKO_API_KEY` (optional; query parameter, omitted when absent)
- `COINGECKO_API_BASE`

`examples/monitor.nokeys.json` leaves `credential_env` off so the public tier is the configured source. `examples/monitor.json` names the key; if the value is absent the source is `keyless`, not blocked.

<a id="helius"></a>

## Helius

Signup: <https://www.helius.dev>.

What it unlocks: Solana parsed transaction events. Those events do not mark plain RPC coverage complete and do not replace on-chain amounts.

Environment:

- `HELIUS_API_KEY`
- `HELIUS_PARSED_EVENTS_URL`

Without the key: parsed events are `credential_missing`. The configured Solana RPC is still used for balances and account reads. In `examples/monitor.json` the Helius source is disabled, so it is not called. `examples/monitor.nokeys.json` enables it so the missing key is reported.

Free tier: check current terms. This repo does not upgrade the plan.

<a id="ofac"></a>

## OFAC SLS

The sanctions list is a public feed. It does not use an API key. The monitor matches addresses locally and does not upload a watch address.

Environment:

- `OFAC_SLS_FEED` (a `file://` path of a local SDN XML file you downloaded, or the HTTPS URL of the list you choose to supply)

A `file://` value is read from disk, for example `file:///absolute/path/to/sdn.xml`. An HTTPS value is fetched with GET. The path has to be one this adapter already allows (`/api/download/`, `/api/PublicationPreview/`, or `/exports/`). No API key is added to the request.

If that variable is unset, sanctions coverage is `feed_not_supplied`. Setting the variable names the feed; it does not by itself mark a wallet as screened. A list whose publish date is older than 7 days is `partial` with reason `feed_stale`, not a clear screen. A malformed list is an error, not a miss and not zero. The report records the source id, the fetch time, the publish date, the validity, and how many addresses were checked. It does not record the list body. Check the publisher's current file location and terms. An empty balance is not "not sanctioned."

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

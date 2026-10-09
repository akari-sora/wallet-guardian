# API keys

Wallet Guardian reads public chain data. Keys are optional. Put them in the private secret profile (the file named by `MONITOR_SECRET_PROFILE`) or in the process environment. Never commit a key, a keyed URL, or a secret profile to this repo. The public config only names the variables.

A missing key is never printed. `credential_missing` rows, `guardian config validate`, and `guardian sources probe` name the variable and point here, for example `ETHERSCAN_API_KEY see docs/api-keys.md#etherscan`.

Limits below are the provider's published free tier at the time this note was written. Check current terms before you rely on a number. This project does not buy a plan or turn on automatic upgrades.

`examples/monitor.json` is the keyed profile: each provider is configured, and a missing key falls through to the keyless path when one exists. `examples/monitor.nokeys.json` is the same routes with Helius enabled (so a missing key is visible) and CoinGecko on the public tier (no `credential_env`).

<a id="etherscan"></a>

## Etherscan

Signup: <https://etherscan.io/apis>. One key covers the V2 multichain API.

What it unlocks: `history.native_external` on Ethereum, Arbitrum, and Polygon (transaction list). Token history on those chains still prefers public RPC logs.

Free tier, check current terms: about 5 calls/second and 100,000 calls/day. The free plan does not include every chain. A chain the plan refuses is `plan_not_supported`. That result is cached and the next round uses the approved fallback instead of repeating the same refusal.

Environment:

- `ETHERSCAN_API_KEY`
- `ETHERSCAN_API_BASE` (the API origin; the client calls `/v2/api`)

Without the key: native history uses a Blockscout keyless instance when that chain has one (Ethereum, Arbitrum, Polygon, Base, Optimism). Otherwise the row is `credential_missing`. Token history uses public RPC `eth_getLogs` with the configured chunk size. BSC does not use Etherscan.

<a id="nodereal"></a>

## NodeReal

Signup: <https://nodereal.io>. `nr_getAssetTransfers` is documented for Ethereum and BSC only. Take the URL from the dashboard you actually enabled. Do not invent a host.

What it unlocks: BSC `history.native_external` (external transfers, both directions, full `pageKey`). Token categories are a separate capability. A refused method is `method_not_found` or `unauthorized` (`blocked`), not an empty wallet.

Free tier: check current terms on the NodeReal dashboard. This repo does not assume a paid CU pack.

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
- Optimism: `BLOCKSCOUT_OPTIMISM_PUBLIC_URL` default `https://optimism.blockscout.com`
- Arbitrum: `BLOCKSCOUT_ARBITRUM_URL` default `https://arbitrum.blockscout.com`
- Polygon: `BLOCKSCOUT_POLYGON_URL` default `https://polygon.blockscout.com`

A Cloudflare HTTP 403 HTML page that says `Just a moment...` is `blocked` with reason `bot_challenge`. It is not parsed as an empty wallet.

Older per-chain variables still work for a keyed instance you configure yourself: `BLOCKSCOUT_BASE_URL` / `BLOCKSCOUT_BASE_API_KEY` and `BLOCKSCOUT_OPTIMISM_URL` / `BLOCKSCOUT_OPTIMISM_API_KEY`. Those keys also travel as a bearer header, not in the URL.

Order for Base and Optimism: PRO when the key is set and the plan includes the chain, then the keyless instance, then an honest gap.

<a id="alchemy"></a>

## Alchemy

Alchemy is the free keyed candidate for Base and Optimism asset transfers after Blockscout. It is not routed by default. `guardian.sources.alchemy` is an interface only (`ROUTED_BY_DEFAULT` is false). Opt in only by adding your own route. A missing key means Alchemy is not routed.

Signup:

1. Create an account at <https://www.alchemy.com>.
2. Create an app.
3. Select Base Mainnet or OP Mainnet.
4. Copy the API key.
5. Set `ALCHEMY_API_KEY`.

The transfers URL comes from that app. The env name, if you opt in, is `ALCHEMY_TRANSFERS_URL`. Do not invent a hostname.

Free tier, check current terms: the Transfers API returns asset transfers. It does not replace approval or `ApprovalForAll` logs. Free `eth_getLogs` is 10 blocks per query. It is not a way to scan a wide log range.

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

What it unlocks: USD quotes. A missing quote is `valuation_unknown`. It is never stored as 0 and never stored as 1.

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

The sanctions list is a public feed. It does not use an API key.

Environment:

- `OFAC_SLS_FEED` (the HTTPS URL of the list you choose to supply)

If that variable is unset, sanctions coverage is `feed_not_supplied`. Setting the variable names the feed; it does not by itself mark a wallet as screened. Check the publisher's current file location and terms. An empty balance is not "not sanctioned."

<a id="keys"></a>

## Where the values live

1. Create a secret profile outside the git work tree. Mode `0600` where the OS enforces it.
2. Point `MONITOR_SECRET_PROFILE` at that file. Point `MONITOR_ACCOUNTS_FILE` and `MONITOR_STATE_DIR` at private paths too.
3. Keys and URLs are strings in that JSON object. Names look like `ETHERSCAN_API_KEY`. Values never go in `examples/`, `docs/`, or a commit.
4. `guardian config validate` and `guardian sources probe` print the variable name and a link on this page when a credential is missing. They do not print the value.

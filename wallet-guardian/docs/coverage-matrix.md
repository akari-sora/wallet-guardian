# Coverage matrix

Date of this preview: 2026-10-09. License: AGPL-3.0-only (see `LICENSE`).

The installable distribution is `wallet-guardian-monitor` (wheel `wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl`). Whether git tag `v0.4.0-rc.3` is published, see GitLab releases. The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`). `--no-send` forbids sends. The canary is NOT_RUN. How the monitor differs from the optional Action Client is stated once in `docs/threat-model.md`.

This matrix is the preview claim for chain × capability. It is derived from `config/source-capabilities/*.json`, `config/chains`, `examples/monitor.json`, `src/guardian/sources/`, `src/guardian/protocols/`, `docs/api-keys.md`, `docs/mcm-01/network-tiers.plan.json`, and `docs/coverage.md`. The live probes below are recorded facts. This document does not re-run them. No monitored address, API key, or keyed URL appears here.

## How to read

Allowed statuses, and only these:

| Token | Meaning |
| --- | --- |
| ok | This path has both an offline fixture and a recorded live probe. The machine-readable evidence block flags both. A path marked ok is still not a promise that every later read succeeds, and a failed read is not a zero balance. |
| partial | A recorded probe or a fixture covers part of the path: one host, one block window, one category, or one of the two paths. |
| blocked | A recorded refusal: HTTP 403, HTTP 429, JSON-RPC -32701, or a free-plan `plan_not_supported` cached as a capability failure. A refusal is not an empty wallet. |
| credential_missing | The path needs a key or there is no keyless source, and the product records that gap. |
| expansion_pending | The monitor keeps the cell for a later source. Avalanche deep native history is the case the code names. |
| not_verified | Code or an offline fixture may exist, and this preview does not claim a live probe. |

The table columns are the no-key path, the keyed path, and one row status. Path statuses sit in their own columns so a keyed probe cannot hide a no-key gap. The row status matches the two path statuses when they agree. When they differ, the row status is partial. A row is ok only when both paths are ok. This preview has no such row: every ok path sits next to a gap, so the row stays partial. Evidence for each ok path is in the fenced JSON block `mcm-share-s8-coverage-evidence`.

`docs/mcm-01/network-tiers.plan.json` marks every network `planned_not_live_verified`. That plan status is not a live pass. Tier 0 networks below are the ones `examples/monitor.json` enables. Higher tiers stay not_verified.

## Recorded probes used here

These are the facts this matrix is allowed to treat as live probes. They were not re-run for this file.

- NodeReal `nr_getAssetTransfers` on BSC with the operator free key: external and erc20 succeeded with pageKey (2026-10-09). Without a key the external-history cell is credential_missing. There is no keyless BSC external-history source.
- Etherscan V2 free plan: history works for Ethereum, Arbitrum, and Polygon. Base, Optimism, BSC, and Avalanche on that free plan return plan_not_supported, cached as a capability failure.
- Blockscout PRO free key: Optimism HTTP 200 (about 5 requests/second, about 20 credits a call). Base HTTP 402 plan_not_supported. Keyless optimism.blockscout.com HTTP 200 from the Linux host. Keyless base.blockscout.com HTTP 403 Cloudflare challenge from the Linux host, HTTP 200 from the operator Windows PC. Those Base results are host-specific.
- Public RPC `eth_getLogs` without an address: publicnode Ethereum, Avalanche, and Polygon return -32701 for any range; publicnode Arbitrum returns 403; arb1.arbitrum.io, api.avax.network, polygon.gateway.tenderly.co, and NodeReal Ethereum and Optimism accept 2000 blocks; Base Tenderly caps at 1000, 1rpc at 50, and mainnet.base.org returns 429.
- Alchemy Transfers for Base: `src/guardian/sources/alchemy.py` implements the client. Offline fixtures cover it. No live probe is recorded, so the keyed cell is not_verified. A missing key is credential_missing, then Blockscout.
- Kinza on BSC runs from offline CLI fixtures. No live verification is claimed. ERC-4626 with no configured vaults is unknown / vaults_not_configured and is not required. Solana is offline fixtures only.
- Avalanche deep native history is expansion_pending.
- Hyperliquid info and the earlier Lista read are described in `docs/coverage.md`. This preview did not re-run them.
- A formal shadow of 24 successive windows has not been run. The DNS-over-HTTPS resolver is implemented. On the reviewed SNI-proxy host, formal qualification is BLOCKED_EGRESS and the preview verdict may be OK; that preview verdict is not a coverage status. See `docs/host-network.md`. The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`). The canary is NOT_RUN. `--no-send` forbids sends.

## Verified probes, 2026-10-09 (MCM-SHARE-02)

Each row below was run on 2026-10-09 (Taipei) from the Linux box with public test addresses only: a well-known public EVM address for balance and history, a public Solana staker address and a public Solana exchange address. No monitored address, key, or keyed URL is recorded. "RPC" probes are raw JSON-RPC calls (`eth_getBalance`, `getBalance`) to the named public endpoint. "CLI" means `guardian run-once --mcm-config … --no-send` on a throwaway state directory with DNS-over-HTTPS resolver mode (this host's system resolver returns 198.18.0.1, which the monitor refuses by design). These rows add to, and where they differ they supersede, the older tables on this page, whose rows were not re-probed. They are not a `live_pass`, and a read that works once is not a promise.

| network / source | capability | path | status | evidence (2026-10-09) |
| --- | --- | --- | --- | --- |
| ethereum, bsc, arbitrum-one, base, optimism, polygon-pos, avalanche-c | balances.native | keyless public RPC | partial | RPC `eth_getBalance` returned a value on publicnode (eth, bsc, op), arb1.arbitrum.io, base.gateway.tenderly.co, polygon.gateway.tenderly.co, api.avax.network. Not run through the EVM CLI for every chain (see CLI row). |
| ethereum, arbitrum-one, polygon-pos | history.native_external | Etherscan V2 free key | ok | `txlist` returned rows for chain ids 1, 42161, 137. Fixtures exist. |
| bsc, base, optimism, avalanche-c | history.native_external | Etherscan V2 free key | blocked | "Free API access is not supported for this chain" for chain ids 56, 8453, 10, 43114. |
| ethereum, arbitrum-one, optimism | history.native_external | Blockscout PRO free key | partial | `txlist` HTTP 200 for chain ids 1, 42161, 10. The example monitor routes PRO only for Base and Optimism, so Ethereum and Arbitrum PRO are an unused spare path. |
| base, polygon-pos | history.native_external | Blockscout PRO free key | blocked | HTTP 402 "requires one of the following plans". |
| bsc, avalanche-c | history.native_external | Blockscout PRO | blocked | HTTP 404 "Network not supported". |
| ethereum | history.native_external | keyless eth.blockscout.com | partial | HTTP 200 with rows from the Linux host. |
| arbitrum-one, polygon-pos, base | history.native_external | keyless Blockscout | blocked | HTTP 403 Cloudflare "Just a moment" from the Linux host (host-specific; Base was 200 from the operator PC earlier). |
| optimism | history.native_external | keyless optimism.blockscout.com | blocked | HTTP 301 to explorer.optimism.io. The redirect target answers HTTP 200 with the same API, so the documented default is now `https://explorer.optimism.io`. The old "HTTP 200 from the Linux host" note above is stale. |
| bsc | history.native_external | NodeReal | credential_missing | No NodeReal key exists on this box, so the keyed path was not re-probed today. The bare endpoint without key answers 404. The earlier recorded NodeReal probe is the only evidence. |
| solana-mainnet | balances.native, balances.fungible, history | CLI, public RPC | partial | CLI run on a public staker address: `getBalance`, `getTokenAccountsByOwner`, `getSignaturesForAddress` all answered; native balance complete in declared scope; fungible/history rows `partial` (`gap_detection_incomplete`, `token2022_unknown_extension`). On a very active exchange address the token read returned `response_too_large` (error, not zero). |
| solana-mainnet | positions.stake | CLI, public RPC | partial | Before this change the CLI always returned `stake_accounts_not_provided`. Now the wallet address is queried as staker and withdrawer (`getProgramAccounts`, offsets 12 and 44): the public staker address showed 4 stake accounts, observed. Offline CLI tests cover both offsets, a no-stake wallet, and a failed read (unknown, never an empty stake). `partial` because only one public address was probed and `getProgramAccounts` may be refused by other RPC providers. |
| solana-mainnet | parsed events | Helius | credential_missing | No key; not probed. |
| address risk | address_risk | GoPlus | credential_missing | The CLI reports `credential_missing` and `third_party_denied`. A direct keyless GET of the GoPlus address-security endpoint returned HTTP 200 for a public address, but the adapter requires a key and consent, so nothing is claimed. |
| sanctions | sanctions | OFAC SDN | not_verified | `OFAC_SLS_FEED` may be a local `file://` SDN XML or an HTTPS URL. run-once exact-matches monitored addresses and counterparties locally. Unset stays `feed_not_supplied`. A list older than 7 days is `partial` with `feed_stale`. A malformed list is an error, not zero. The row records source id, fetch time, publish date, validity, and the count checked, never the list body. Setting the variable does not mark a wallet screened. A live publisher download is not claimed here. |
| prices | prices.usd | CoinGecko public | partial | Direct keyless GET HTTP 200. Not verified through the CLI valuation path. |

### News feeds fetched and parsed (keyless GET, then `parse_feed`)

| feed | HTTP | size | parse | newest item |
| --- | --- | --- | --- | --- |
| governance.aave.com latest.rss | 200 | 296 KB | ok, 30 items | 2026-10-08 |
| medium.com/feed/@kinzafinance | 200 | 61 KB | ok, 10 items | 2025-08-08 (stale feed) |
| status.solana.com history.rss | 200 | 26 KB | ok, 25 items | 2024-02-06 (status history is sparse) |
| api.paragraph.com/blogs/rss/@blog.lista.org (Lista blog RSS, added) | 200 | 252 KiB | ok, 20 items | 2026-10-01 |
| UXLINK | blog.uxlink.io/feed redirects to a Medium identity stub; docs notice page is HTML | n/a | not a feed | no feed: honest gap, manual only |

Not added: Bitcoin Core `rss.xml` (593 KB) is refused by the parser's item cap. Every allowlisted URL is https RSS and is listed in `ALLOWLISTED_FEED_URLS`; tests assert the catalog and the allowlist match and that an HTML redirect stub is not a feed. No other protocol (Kinza apart from Medium, UXLINK, Lista beyond the blog) has automatic news.

### Added in this change set, probed 2026-10-09

| network / source | capability | path | status | evidence |
| --- | --- | --- | --- | --- |
| bitcoin-mainnet | balances.native | Esplora, keyless `https://blockstream.info/api`, CLI | partial | A public address: confirmed balance in sats matched `funded_txo_sum - spent_txo_sum` from the API; row `complete_in_declared_scope`, support partial because one address is an address set, not an HD wallet. mempool.space/api answers HTTP 200 on the address endpoint (not run through the CLI). Offline CLI tests cover pagination, dedup after restart, mempool as pending, 429/500 as errors, GET only. |
| bitcoin-mainnet | history.native_external | Esplora, CLI | partial | A mid-size public address: 3 pages followed, then `page_cap` (`pagination_incomplete`), never complete. A very large address returned `response_too_large` (error, not zero). Not claimed: tokens, Ordinals, Runes, Lightning, xpub. |
| ethereum-mainnet | positions.lido | pinned eth_call views | partial | Raw `eth_call` on publicnode against the pinned stETH/wstETH/WithdrawalQueue addresses (names and symbols matched): `balanceOf`, `sharesOf`, `getPooledEthByShares` agreed to 0 wei for a public contract holder, `getWithdrawalRequests` answered an empty array, `getStETHByWstETH` answered. The monitor path is covered by offline CLI tests (holdings, pending and finalized withdrawal, failed call is unknown, empty is none_observed). A full CLI run on a busy public holder did not finish inside the time box. Opt-in plugin `lido`; not listed in the example monitors. |
| news | lido blog and research forum RSS | allowlisted GET | ok | `blog.lido.fi/rss/` HTTP 200 (about 202 KiB, 15 items) and `research.lido.fi/latest.rss` HTTP 200 (30 items) parse; offline fixtures in the news pipeline test. Same caveat as other feeds: a feed that parses is not a claim about incident coverage. |
| ethereum, bsc, optimism, base, polygon, avalanche | no-key `run-once` (EVM) | CLI, public RPC, DoH | partial | A no-key `run-once` on a throwaway state with a one-day bootstrap window did not finish inside 15 minutes (timed out) for a public quiet address across six EVM networks, and not inside 20 minutes with Arbitrum included, because log scans are chunked to the public RPC range limits under the free request budget. Per-source results above are the evidence; no end-to-end EVM no-key CLI pass is claimed. |

### What is still not verified
- Alchemy Base, Helius, NodeReal (keyed), GoPlus (keyed), Kinza and UXLINK plugins, Aave on a live position, and any chain not named above have no probe today.
- TRON: an experimental, opt-in, read-only reader exists (see the TRON row and `docs/coverage-tron.md`). It is disabled by default, not live-qualified, and not claimed as TRON support. Only finalized native TRX transfers produce alert candidates; stored TRC-20 or approval history does not establish token or approval alerting. Hyperliquid pipeline support is not built in the MCM path and is not claimed.
- Preview wording (TRON is separate: an experimental, opt-in TRON reader is also included, disabled by default and not live-qualified; it provides bounded reads, not complete TRON monitoring, and must not be the sole safety monitor): Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path; capability varies by network and source (see the tables).

## Chain × capability

| network | capability | no-key path | no-key status | keyed path | keyed status | status |
| --- | --- | --- | --- | --- | --- | --- |
| ethereum-mainnet | balances.native | operator RPC named by RPC_ETHEREUM_MAINNET_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| ethereum-mainnet | history.token | public RPC eth_getLogs; publicnode rejects any range with -32701; NodeReal eth accepts 2000 blocks | partial | no keyed log source on this route | not_verified | partial |
| ethereum-mainnet | permissions.standard | Approval and ApprovalForAll logs on the same RPC route, from an explicit origin | partial | no keyed approval source on this route | not_verified | partial |
| ethereum-mainnet | history.native_external | keyless Blockscout at BLOCKSCOUT_ETH_URL when that endpoint is set | not_verified | Etherscan V2 txlist with ETHERSCAN_API_KEY | ok | partial |
| bsc-mainnet | balances.native | operator RPC named by RPC_BSC_MAINNET_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| bsc-mainnet | history.token | public RPC eth_getLogs when the endpoint accepts a topic filter | not_verified | no keyed token-log source on this route | not_verified | not_verified |
| bsc-mainnet | permissions.standard | public RPC approval logs | not_verified | no keyed approval source on this route | not_verified | not_verified |
| bsc-mainnet | history.native_external | no keyless BSC external-history source | credential_missing | NodeReal nr_getAssetTransfers category external with NODEREAL_API_KEY | ok | partial |
| bsc-mainnet | history.fungible | public RPC token logs | not_verified | NodeReal nr_getAssetTransfers category erc20 | partial | partial |
| bsc-mainnet | history.native_external | Etherscan is not the BSC route | credential_missing | Etherscan V2 free plan | blocked | partial |
| arbitrum-one | balances.native | operator RPC named by RPC_ARBITRUM_ONE_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| arbitrum-one | history.token | public RPC eth_getLogs; publicnode returns 403; arb1.arbitrum.io accepts 2000 blocks | partial | no keyed log source on this route | not_verified | partial |
| arbitrum-one | permissions.standard | approval logs on the same RPC route | partial | no keyed approval source on this route | not_verified | partial |
| arbitrum-one | history.native_external | keyless Blockscout at BLOCKSCOUT_ARBITRUM_URL | not_verified | Etherscan V2 txlist | partial | partial |
| base-mainnet | balances.native | operator RPC named by RPC_BASE_MAINNET_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| base-mainnet | history.token | tenderly_base range_limit 1000, then base-1rpc range_limit 50 only when that source is enabled and the remaining budget can catch up | partial | Alchemy free eth_getLogs is not this route | not_verified | partial |
| base-mainnet | permissions.standard | the Base log route above | partial | Alchemy Transfers does not scan Approval or ApprovalForAll | not_verified | partial |
| base-mainnet | history.native_external | keyless base.blockscout.com | partial | Alchemy Transfers when ALCHEMY_API_KEY and ALCHEMY_TRANSFERS_URL are both set | not_verified | partial |
| base-mainnet | history.native_external | Blockscout PRO without BLOCKSCOUT_API_KEY returns HTTP 402 | credential_missing | Blockscout PRO free plan for chain id 8453 | blocked | partial |
| base-mainnet | history.native_external | Etherscan is not the configured Base transfer route | credential_missing | Etherscan V2 free plan | blocked | partial |
| optimism-mainnet | balances.native | operator RPC named by RPC_OPTIMISM_MAINNET_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| optimism-mainnet | history.token | public RPC eth_getLogs; NodeReal opt accepts 2000 blocks | partial | no keyed log source on this route | not_verified | partial |
| optimism-mainnet | permissions.standard | approval logs on the Optimism RPC route | partial | no keyed approval source on this route | not_verified | partial |
| optimism-mainnet | history.native_external | keyless optimism.blockscout.com | partial | Blockscout PRO free key for chain id 10 | partial | partial |
| optimism-mainnet | history.native_external | Etherscan is not the configured Optimism history route | credential_missing | Etherscan V2 free plan | blocked | partial |
| polygon-pos | balances.native | operator RPC named by RPC_POLYGON_POS_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| polygon-pos | history.token | public RPC eth_getLogs; publicnode returns -32701 for any range; polygon.gateway.tenderly.co accepts 2000 blocks | partial | no keyed log source on this route | not_verified | partial |
| polygon-pos | permissions.standard | approval logs on the Polygon RPC route | partial | no keyed approval source on this route | not_verified | partial |
| polygon-pos | history.native_external | keyless Blockscout at BLOCKSCOUT_POLYGON_URL | not_verified | Etherscan V2 txlist | partial | partial |
| avalanche-c | balances.native | operator RPC named by RPC_AVALANCHE_C_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| avalanche-c | history.token | public RPC eth_getLogs; publicnode returns -32701 for any range; api.avax.network accepts 2000 blocks | partial | no keyed log source on this route | not_verified | partial |
| avalanche-c | permissions.standard | approval logs on the Avalanche RPC route | partial | no keyed approval source on this route | not_verified | partial |
| avalanche-c | history.native_external | no keyless deep-history source on this route | expansion_pending | Etherscan V2 free plan | expansion_pending | expansion_pending |
| solana-mainnet | balances.native | operator Solana RPC named by RPC_SOLANA_MAINNET_URL | not_verified | no keyed balance source on this route | not_verified | not_verified |
| solana-mainnet | balances.token_accounts | operator Solana RPC | not_verified | no keyed token-account source on this route | not_verified | not_verified |
| solana-mainnet | history.account_set | operator Solana RPC | not_verified | no keyed history source on this route | not_verified | not_verified |
| solana-mainnet | permissions.spl | operator Solana RPC | not_verified | no keyed SPL source on this route | not_verified | not_verified |
| solana-mainnet | history.native_external | parsed events need HELIUS_API_KEY; examples/monitor.json leaves the source disabled | credential_missing | Helius parsed events when the key and URL are set and the source is enabled | not_verified | partial |
| bsc-mainnet | positions.kinza | Kinza plugin over the BSC RPC, offline CLI fixtures | not_verified | no separate Kinza API key | not_verified | not_verified |
| ethereum-mainnet | positions.kinza | Kinza plugin over the Ethereum RPC, offline fixtures | not_verified | no separate Kinza API key | not_verified | not_verified |
| configured-evm | positions.erc4626 | opt-in pins for sDAI and Steakhouse USDC (vault exposure only) over evm_rpc, or caller-supplied vaults; off unless configured (`docs/positions-more.md`) | not_verified | no keyed vault source | not_verified | not_verified |
| bsc-mainnet | positions.lista | Lista slisBNB plugin over BSC RPC | partial | no keyed Lista source | not_verified | partial |
| hyperliquid-info | positions | read-only info endpoint used by the legacy doctor path | partial | no keyed Hyperliquid source in the MCM routes | not_verified | partial |
| configured-evm | positions.aave-v3 | Aave V3 plugin over evm_rpc | not_verified | no keyed Aave source | not_verified | not_verified |
| configured-evm | positions.uxlink | UXLINK balanceOf over the public asset file and evm_rpc | not_verified | no keyed UXLINK source | not_verified | not_verified |
| solana-mainnet | positions.stake | solana_stake plugin over Solana RPC | not_verified | no keyed stake source | not_verified | not_verified |
| * | prices.usd | CoinGecko public tier when COINGECKO_API_KEY is absent | not_verified | optional COINGECKO_API_KEY demo tier | not_verified | not_verified |
| * | address_risk | GoPlus without GOPLUS_API_KEY | credential_missing | GoPlus with GOPLUS_API_KEY | not_verified | partial |
| * | sanctions | public OFAC feed named by OFAC_SLS_FEED; no API key | not_verified | no keyed OFAC source | not_verified | not_verified |
| * | address_risk | not enabled in examples/monitor.json | not_verified | Chainabuse would need an operator key this package does not configure | not_verified | not_verified |
| * | positions | not enabled in examples/monitor.json | not_verified | DeBank is not a configured keyed source | not_verified | not_verified |
| cosmos-hub | comet.rpc | comet_rpc adapter file only | not_verified | no keyed Comet source in the example monitor | not_verified | not_verified |
| linea-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| zksync-era | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| scroll-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| mantle-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| sonic-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| unichain-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| hyperevm-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| bitcoin-mainnet | enabled_route | listed in examples/monitor.json enabled_networks; address-set-only Esplora path (see the dated Bitcoin probe rows above) | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| tron-mainnet | experimental_reader | experimental, opt-in, disabled by default; bounded native TRX + TRC-20 history + configured balances; not live-qualified; only finalized native TRX produces alert candidates; do not use as the only safety monitor (`docs/coverage-tron.md`) | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| ethereum-mainnet | positions.compound-v3 (USDC Comet) | opt-in; stored-index present value, not accrued to now; block height is not block-hash proof; a positive oracle price is not freshness (`docs/positions-more.md`) | not_verified | no keyed source | not_verified | not_verified |
| yield / promotions | parsers | experimental offline fixture and parser, disabled; no live fetch, scheduler or delivery; extracted terms are partial interpretations (`docs/yield-promotions.md`) | not_verified | no keyed source | not_verified | not_verified |
| blast-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| opbnb-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| ton-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| sui-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| aptos-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| cosmos-hub | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| osmosis-mainnet | enabled_route | not in examples/monitor.json enabled_networks | not_verified | no keyed route in the example monitor | not_verified | not_verified |
| ton-mainnet | enabled_route | toncenter_v3.json is not the example route | not_verified | no keyed route | not_verified | not_verified |
| sui-mainnet | enabled_route | sui_grpc.json is not the example route | not_verified | no keyed route | not_verified | not_verified |
| aptos-mainnet | enabled_route | aptos_indexer.json is not the example route | not_verified | no keyed route | not_verified | not_verified |

## Public eth_getLogs probes

These rows are endpoint facts from the list above. They use the same six statuses. None of them is ok: a range that was accepted is partial, and a refusal is blocked.

| probe | status | detail |
| --- | --- | --- |
| publicnode-ethereum-getLogs | blocked | publicnode Ethereum eth_getLogs without an address returns -32701 for any range. |
| publicnode-avalanche-getLogs | blocked | publicnode Avalanche eth_getLogs without an address returns -32701 for any range. |
| publicnode-polygon-getLogs | blocked | publicnode Polygon eth_getLogs without an address returns -32701 for any range. |
| publicnode-arbitrum-getLogs | blocked | publicnode Arbitrum eth_getLogs returns HTTP 403. |
| arb1.arbitrum.io-getLogs | partial | arb1.arbitrum.io accepts eth_getLogs at 2000 blocks. That window is not a finished history. |
| api.avax.network-getLogs | partial | api.avax.network accepts eth_getLogs at 2000 blocks. Deep native history stays expansion_pending. |
| polygon.gateway.tenderly.co-getLogs | partial | polygon.gateway.tenderly.co accepts eth_getLogs at 2000 blocks. |
| nodereal-eth-getLogs | partial | NodeReal Ethereum eth_getLogs accepts 2000 blocks. This is not the BSC nr_getAssetTransfers row. |
| nodereal-opt-getLogs | partial | NodeReal Optimism eth_getLogs accepts 2000 blocks. |
| tenderly-base-getLogs | partial | Base Tenderly log cap is 1000 blocks, matching tenderly_base range_limit. |
| base-1rpc-getLogs | partial | 1rpc Base cap is 50 blocks. examples/monitor.json leaves base-1rpc disabled. |
| mainnet.base.org-getLogs | blocked | mainnet.base.org returned HTTP 429. That endpoint is not the configured Base log route. |

## Keyed sources and the no-key column

Each keyed source in `examples/monitor.json` has a no-key entry and an anchor in `docs/api-keys.md`. The anchor is the element id on that page (`<a id="etherscan">` and the same pattern for the other providers).

| source | credential env | no-key path | anchor |
| --- | --- | --- | --- |
| etherscan | ETHERSCAN_API_KEY | Keyless Blockscout on Ethereum, Arbitrum, Polygon, Base, and Optimism when that endpoint is set. BSC does not use Etherscan. With no key and no keyless instance the row is credential_missing. | docs/api-keys.md#etherscan |
| nodereal-bsc | NODEREAL_API_KEY | credential_missing. There is no keyless BSC external-history source. BSC token logs stay on public RPC when that endpoint accepts them. | docs/api-keys.md#nodereal |
| blockscout-pro | BLOCKSCOUT_API_KEY | HTTP 402 without the key is credential_missing, then the keyless instance for that chain. Optimism is on the free plan. Base on the free plan is plan_not_supported. | docs/api-keys.md#blockscout |
| blockscout-base | BLOCKSCOUT_BASE_API_KEY | A missing BLOCKSCOUT_BASE_API_KEY does not invent an empty history. The key travels as a bearer header. | docs/api-keys.md#blockscout |
| blockscout-optimism | BLOCKSCOUT_OPTIMISM_API_KEY | Keyless fallback is BLOCKSCOUT_OPTIMISM_PUBLIC_URL. The Linux host recorded HTTP 200 for optimism.blockscout.com. | docs/api-keys.md#blockscout |
| alchemy-base | ALCHEMY_API_KEY | credential_missing, then Blockscout PRO and the keyless Base instance. The adapter is implemented. No live probe, so a keyed run stays not_verified. | docs/api-keys.md#alchemy |
| goplus | GOPLUS_API_KEY | credential_missing. That is not a clean bill of health. Wallet-address queries stay off until local consent. | docs/api-keys.md#goplus |
| coingecko | COINGECKO_API_KEY | Public keyless tier when the key is absent. examples/monitor.nokeys.json leaves credential_env off. A missing quote is valuation_unknown. | docs/api-keys.md#coingecko |
| helius-parser | HELIUS_API_KEY | Parsed events are credential_missing. The configured Solana RPC still serves account reads. examples/monitor.json leaves this source disabled; examples/monitor.nokeys.json enables it so the missing key is visible. | docs/api-keys.md#helius |

Keyless sources that the routes still name:

| source | no-key path | anchor |
| --- | --- | --- |
| ofac | No API key. OFAC_SLS_FEED names the public list. An unset variable is feed_not_supplied, recorded in the matrix as not_verified. | docs/api-keys.md#ofac |
| blockscout-eth-public | Keyless Ethereum instance. BLOCKSCOUT_ETH_URL defaults to the public eth.blockscout.com origin. No live probe of that host is recorded in this preview. | docs/api-keys.md#blockscout |
| blockscout-base-public | Keyless Base instance. Linux host: HTTP 403 bot_challenge. Operator Windows PC: HTTP 200. Host-specific, so the matrix cell is partial. | docs/api-keys.md#blockscout |
| blockscout-optimism-public | Keyless Optimism instance. The Linux host recorded HTTP 200. The matrix cell stays partial because that 200 is not a finished history. | docs/api-keys.md#blockscout |
| tenderly-base | No key. TENDERLY_BASE_URL, range_limit 1000. See the Base log row. The anchor documents the Base log endpoints next to Alchemy. | docs/api-keys.md#alchemy |

## What is not claimed

- The formal 24-window shadow has not been run. `tools/mcm_shadow_round.py` does not start that count. This preview does not claim fitness for production use.
- Telegram canary status is NOT_RUN. The sender tests use a mock. See `docs/notifications.md`.
- On the reviewed host that routes by Server Name Indication, formal egress qualification is BLOCKED_EGRESS. A preview verdict of OK on that host is not proof. See `docs/host-network.md`.
- Solana: a public-address CLI check was done on 2026-10-09 (see the verified section); it is partial, not a live pass.
- Kinza live verification has not been done.
- ERC-4626 without configured vaults stays unknown / vaults_not_configured and is not required.
- Alchemy Transfers on Base has offline fixtures and no recorded live probe.
- The Base keyless Blockscout result depends on the host.
- BSC external history has no keyless source.
- Avalanche deep native history is expansion_pending.
- `guardian install-service` is a dry-run installer. No unit has been installed on a host, and the host smoke is NOT_RUN. See `docs/box-unattended.md`.
- Tagged previews are published only as GitLab releases. Nothing is uploaded to PyPI or any package index.
- Deferred and not claimed: sUSDe, Morpho Blue markets, TRC-20 and approval alerting on TRON, TRON genesis identity verification, yield live fetch, scheduler and delivery.
- Text-handling residuals (see `docs/injection-privacy-tests.md`): a double HTML escape is cosmetic; the incident matcher can miss digit-plus-letter tokens such as "Aave v3 incident"; `privacy_boundary` is a policy table, not proof of isolation; there is no claim of general prompt-injection immunity.
- Failed reads, missing keys, plan refusals, and unrun probes are gaps. They are not zero balances and they are not a finished scan.

## Machine-readable cells

The fenced blocks are what the offline guard checks. `status`, `no_key_status`, `keyed_status`, and each endpoint probe `status` are the only status fields. An ok path has `offline_fixtures` and `live_probe` both true in the evidence block.

```json
{
  "kind": "mcm-share-s8-coverage-cells",
  "allowed_statuses": [
    "ok",
    "partial",
    "blocked",
    "credential_missing",
    "expansion_pending",
    "not_verified"
  ],
  "cells": [
    {
      "id": "ethereum-mainnet/balances.native",
      "network": "ethereum-mainnet",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_ETHEREUM_MAINNET_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "examples/monitor.json routes this to evm_rpc. This preview has no recorded live balance probe."
    },
    {
      "id": "ethereum-mainnet/history.token",
      "network": "ethereum-mainnet",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs; publicnode rejects any range with -32701; NodeReal eth accepts 2000 blocks",
      "no_key_status": "partial",
      "keyed_path": "no keyed log source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "The endpoint result splits. A 2000-block window is not a finished token history. Token history still prefers logs over Etherscan tokentx."
    },
    {
      "id": "ethereum-mainnet/permissions.standard",
      "network": "ethereum-mainnet",
      "capability": "permissions.standard",
      "no_key_path": "Approval and ApprovalForAll logs on the same RPC route, from an explicit origin",
      "no_key_status": "partial",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Same endpoint split as history.token. Older history stays unknown. A transfer read does not finish an approval scan."
    },
    {
      "id": "ethereum-mainnet/history.native_external",
      "network": "ethereum-mainnet",
      "capability": "history.native_external",
      "no_key_path": "keyless Blockscout at BLOCKSCOUT_ETH_URL when that endpoint is set",
      "no_key_status": "not_verified",
      "keyed_path": "Etherscan V2 txlist with ETHERSCAN_API_KEY",
      "keyed_status": "ok",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json",
        "config/source-capabilities/blockscout.json"
      ],
      "note": "Keyed free-plan history for Ethereum has a recorded live probe and an offline txlist fixture. Keyless eth.blockscout.com has no recorded live probe in this preview."
    },
    {
      "id": "bsc-mainnet/balances.native",
      "network": "bsc-mainnet",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_BSC_MAINNET_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "docs/coverage.md records an older official RPC read. This preview did not re-run it."
    },
    {
      "id": "bsc-mainnet/history.token",
      "network": "bsc-mainnet",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs when the endpoint accepts a topic filter",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed token-log source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "The 2026-10-09 log probes do not include a BSC eth_getLogs result. A rejected range stays a gap."
    },
    {
      "id": "bsc-mainnet/permissions.standard",
      "network": "bsc-mainnet",
      "capability": "permissions.standard",
      "no_key_path": "public RPC approval logs",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "No recorded BSC approval-log probe. NodeReal asset transfers are a different row and do not finish approvals."
    },
    {
      "id": "bsc-mainnet/history.native_external",
      "network": "bsc-mainnet",
      "capability": "history.native_external",
      "no_key_path": "no keyless BSC external-history source",
      "no_key_status": "credential_missing",
      "keyed_path": "NodeReal nr_getAssetTransfers category external with NODEREAL_API_KEY",
      "keyed_status": "ok",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/nodereal_transfers.json"
      ],
      "note": "Keyed path: recorded 2026-10-09 live probe of external transfers with pageKey, plus offline fixtures. No key leaves credential_missing. That gap is not an empty wallet."
    },
    {
      "id": "bsc-mainnet/history.fungible",
      "network": "bsc-mainnet",
      "capability": "history.fungible",
      "no_key_path": "public RPC token logs",
      "no_key_status": "not_verified",
      "keyed_path": "NodeReal nr_getAssetTransfers category erc20",
      "keyed_status": "partial",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/nodereal_transfers.json"
      ],
      "note": "Recorded live probe: BSC erc20 with pageKey on 2026-10-09. Offline fixtures map category 20 to history.fungible. The pageKey walk in that fixture is the external category, so this row stays partial."
    },
    {
      "id": "bsc-mainnet/history.native_external#etherscan",
      "network": "bsc-mainnet",
      "capability": "history.native_external",
      "no_key_path": "Etherscan is not the BSC route",
      "no_key_status": "credential_missing",
      "keyed_path": "Etherscan V2 free plan",
      "keyed_status": "blocked",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json"
      ],
      "note": "The free plan returns plan_not_supported for BSC. That refusal is cached as a capability failure. The configured external-history route is NodeReal."
    },
    {
      "id": "arbitrum-one/balances.native",
      "network": "arbitrum-one",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_ARBITRUM_ONE_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "No recorded live balance probe in this preview."
    },
    {
      "id": "arbitrum-one/history.token",
      "network": "arbitrum-one",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs; publicnode returns 403; arb1.arbitrum.io accepts 2000 blocks",
      "no_key_status": "partial",
      "keyed_path": "no keyed log source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Host and endpoint split the result. A 2000-block window is not a finished token history."
    },
    {
      "id": "arbitrum-one/permissions.standard",
      "network": "arbitrum-one",
      "capability": "permissions.standard",
      "no_key_path": "approval logs on the same RPC route",
      "no_key_status": "partial",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Same endpoint split as history.token. Older approval history stays unknown."
    },
    {
      "id": "arbitrum-one/history.native_external",
      "network": "arbitrum-one",
      "capability": "history.native_external",
      "no_key_path": "keyless Blockscout at BLOCKSCOUT_ARBITRUM_URL",
      "no_key_status": "not_verified",
      "keyed_path": "Etherscan V2 txlist",
      "keyed_status": "partial",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json",
        "config/source-capabilities/blockscout.json"
      ],
      "note": "The free plan works for Arbitrum history. The committed offline txlist fixture uses chain id 1, so this chain stays partial rather than ok. Keyless arbitrum.blockscout.com has no recorded live probe here."
    },
    {
      "id": "base-mainnet/balances.native",
      "network": "base-mainnet",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_BASE_MAINNET_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "No recorded live balance probe. Base stays a required network. A failed read is a gap."
    },
    {
      "id": "base-mainnet/history.token",
      "network": "base-mainnet",
      "capability": "history.token",
      "no_key_path": "tenderly_base range_limit 1000, then base-1rpc range_limit 50 only when that source is enabled and the remaining budget can catch up",
      "no_key_status": "partial",
      "keyed_path": "Alchemy free eth_getLogs is not this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Recorded probes: Tenderly 1000-block cap, 1rpc 50, mainnet.base.org 429. base-1rpc is disabled in examples/monitor.json until an operator enables it. The 429 endpoint is blocked and is not the configured route."
    },
    {
      "id": "base-mainnet/permissions.standard",
      "network": "base-mainnet",
      "capability": "permissions.standard",
      "no_key_path": "the Base log route above",
      "no_key_status": "partial",
      "keyed_path": "Alchemy Transfers does not scan Approval or ApprovalForAll",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json",
        "config/source-capabilities/alchemy_transfers.json"
      ],
      "note": "Transfer reads stay off this row. Offline fixtures cover the 1000-block chunker. No live approval probe is claimed."
    },
    {
      "id": "base-mainnet/history.native_external",
      "network": "base-mainnet",
      "capability": "history.native_external",
      "no_key_path": "keyless base.blockscout.com",
      "no_key_status": "partial",
      "keyed_path": "Alchemy Transfers when ALCHEMY_API_KEY and ALCHEMY_TRANSFERS_URL are both set",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/alchemy_transfers.json",
        "config/source-capabilities/blockscout.json"
      ],
      "note": "AlchemyClient is implemented and offline fixtures walk both directions and pageKey. No live Alchemy probe is recorded, so the keyed cell is not_verified. A missing key is credential_missing on that source, then the run continues. Keyless Base is host-specific: HTTP 403 bot_challenge from the Linux host, HTTP 200 from the operator Windows PC."
    },
    {
      "id": "base-mainnet/history.native_external#blockscout-pro",
      "network": "base-mainnet",
      "capability": "history.native_external",
      "no_key_path": "Blockscout PRO without BLOCKSCOUT_API_KEY returns HTTP 402",
      "no_key_status": "credential_missing",
      "keyed_path": "Blockscout PRO free plan for chain id 8453",
      "keyed_status": "blocked",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/blockscout.json"
      ],
      "note": "With a free key the featured-chain response is HTTP 402 plan_not_supported, cached as a capability failure. That is not an empty history."
    },
    {
      "id": "base-mainnet/history.native_external#etherscan",
      "network": "base-mainnet",
      "capability": "history.native_external",
      "no_key_path": "Etherscan is not the configured Base transfer route",
      "no_key_status": "credential_missing",
      "keyed_path": "Etherscan V2 free plan",
      "keyed_status": "blocked",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json"
      ],
      "note": "The free plan returns plan_not_supported for Base. The refusal is cached. The configured transfer route is Alchemy, then Blockscout."
    },
    {
      "id": "optimism-mainnet/balances.native",
      "network": "optimism-mainnet",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_OPTIMISM_MAINNET_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "No recorded live balance probe in this preview."
    },
    {
      "id": "optimism-mainnet/history.token",
      "network": "optimism-mainnet",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs; NodeReal opt accepts 2000 blocks",
      "no_key_status": "partial",
      "keyed_path": "no keyed log source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "A 2000-block acceptance is the recorded probe. It is not a finished token history."
    },
    {
      "id": "optimism-mainnet/permissions.standard",
      "network": "optimism-mainnet",
      "capability": "permissions.standard",
      "no_key_path": "approval logs on the Optimism RPC route",
      "no_key_status": "partial",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Same log-width limit as history.token. Older approval history stays unknown."
    },
    {
      "id": "optimism-mainnet/history.native_external",
      "network": "optimism-mainnet",
      "capability": "history.native_external",
      "no_key_path": "keyless optimism.blockscout.com",
      "no_key_status": "partial",
      "keyed_path": "Blockscout PRO free key for chain id 10",
      "keyed_status": "partial",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/blockscout.json"
      ],
      "note": "Recorded probes: PRO HTTP 200 at about 5 requests/second and about 20 credits/call; keyless HTTP 200 from the Linux host. Offline Blockscout fixtures exist. A 200 is not a finished public-address history, so neither path is ok."
    },
    {
      "id": "optimism-mainnet/history.native_external#etherscan",
      "network": "optimism-mainnet",
      "capability": "history.native_external",
      "no_key_path": "Etherscan is not the configured Optimism history route",
      "no_key_status": "credential_missing",
      "keyed_path": "Etherscan V2 free plan",
      "keyed_status": "blocked",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json"
      ],
      "note": "The free plan returns plan_not_supported for Optimism. The refusal is cached. The configured route is Blockscout PRO, then the keyless instance."
    },
    {
      "id": "polygon-pos/balances.native",
      "network": "polygon-pos",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_POLYGON_POS_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "No recorded live balance probe in this preview."
    },
    {
      "id": "polygon-pos/history.token",
      "network": "polygon-pos",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs; publicnode returns -32701 for any range; polygon.gateway.tenderly.co accepts 2000 blocks",
      "no_key_status": "partial",
      "keyed_path": "no keyed log source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "The endpoint result splits. A 2000-block window is not a finished token history."
    },
    {
      "id": "polygon-pos/permissions.standard",
      "network": "polygon-pos",
      "capability": "permissions.standard",
      "no_key_path": "approval logs on the Polygon RPC route",
      "no_key_status": "partial",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Same endpoint split as history.token. Older approval history stays unknown."
    },
    {
      "id": "polygon-pos/history.native_external",
      "network": "polygon-pos",
      "capability": "history.native_external",
      "no_key_path": "keyless Blockscout at BLOCKSCOUT_POLYGON_URL",
      "no_key_status": "not_verified",
      "keyed_path": "Etherscan V2 txlist",
      "keyed_status": "partial",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json",
        "config/source-capabilities/blockscout.json"
      ],
      "note": "The free plan works for Polygon history. The committed offline txlist fixture uses chain id 1, so this chain stays partial rather than ok. Keyless polygon.blockscout.com has no recorded live probe here."
    },
    {
      "id": "avalanche-c/balances.native",
      "network": "avalanche-c",
      "capability": "balances.native",
      "no_key_path": "operator RPC named by RPC_AVALANCHE_C_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Native balance is still a configured read. This preview has no recorded live balance probe, so the cell stays not_verified."
    },
    {
      "id": "avalanche-c/history.token",
      "network": "avalanche-c",
      "capability": "history.token",
      "no_key_path": "public RPC eth_getLogs; publicnode returns -32701 for any range; api.avax.network accepts 2000 blocks",
      "no_key_status": "partial",
      "keyed_path": "no keyed log source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Log width and deep native history are different cells. A 2000-block log window is partial."
    },
    {
      "id": "avalanche-c/permissions.standard",
      "network": "avalanche-c",
      "capability": "permissions.standard",
      "no_key_path": "approval logs on the Avalanche RPC route",
      "no_key_status": "partial",
      "keyed_path": "no keyed approval source on this route",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "Same endpoint split as history.token."
    },
    {
      "id": "avalanche-c/history.native_external",
      "network": "avalanche-c",
      "capability": "history.native_external",
      "no_key_path": "no keyless deep-history source on this route",
      "no_key_status": "expansion_pending",
      "keyed_path": "Etherscan V2 free plan",
      "keyed_status": "expansion_pending",
      "status": "expansion_pending",
      "capability_files": [
        "config/source-capabilities/etherscan_v2.json",
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "The free plan returns plan_not_supported. The monitor records expansion_pending for that Avalanche case. Deep history is not an empty wallet. It does not block the other six configured EVM networks."
    },
    {
      "id": "solana-mainnet/balances.native",
      "network": "solana-mainnet",
      "capability": "balances.native",
      "no_key_path": "operator Solana RPC named by RPC_SOLANA_MAINNET_URL",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed balance source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/solana_rpc.json"
      ],
      "note": "Offline fixtures only. A real public-address verification has not been done."
    },
    {
      "id": "solana-mainnet/balances.token_accounts",
      "network": "solana-mainnet",
      "capability": "balances.token_accounts",
      "no_key_path": "operator Solana RPC",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed token-account source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/solana_rpc.json"
      ],
      "note": "Offline fixtures only. Closed token accounts that were not discovered stay a history gap."
    },
    {
      "id": "solana-mainnet/history.account_set",
      "network": "solana-mainnet",
      "capability": "history.account_set",
      "no_key_path": "operator Solana RPC",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed history source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/solana_rpc.json"
      ],
      "note": "Scanning the owner public key does not cover every inbound credit. No live public-address check is claimed."
    },
    {
      "id": "solana-mainnet/permissions.spl",
      "network": "solana-mainnet",
      "capability": "permissions.spl",
      "no_key_path": "operator Solana RPC",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed SPL source on this route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/solana_rpc.json"
      ],
      "note": "Offline fixtures only. No live public-address check is claimed."
    },
    {
      "id": "solana-mainnet/history.native_external#helius",
      "network": "solana-mainnet",
      "capability": "history.native_external",
      "no_key_path": "parsed events need HELIUS_API_KEY; examples/monitor.json leaves the source disabled",
      "no_key_status": "credential_missing",
      "keyed_path": "Helius parsed events when the key and URL are set and the source is enabled",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/helius_parsed_events.json"
      ],
      "note": "A missing key is credential_missing. Parsed events do not finish plain RPC coverage. No live Helius probe is recorded."
    },
    {
      "id": "bsc-mainnet/positions.kinza",
      "network": "bsc-mainnet",
      "capability": "positions.kinza",
      "no_key_path": "Kinza plugin over the BSC RPC, offline CLI fixtures",
      "no_key_status": "not_verified",
      "keyed_path": "no separate Kinza API key",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/kinza.json"
      ],
      "note": "The plugin runs from offline fixtures. No live verification is claimed. A missing reserve stays unknown."
    },
    {
      "id": "ethereum-mainnet/positions.kinza",
      "network": "ethereum-mainnet",
      "capability": "positions.kinza",
      "no_key_path": "Kinza plugin over the Ethereum RPC, offline fixtures",
      "no_key_status": "not_verified",
      "keyed_path": "no separate Kinza API key",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/kinza.json"
      ],
      "note": "Same plugin, Ethereum deployment. No live verification is claimed."
    },
    {
      "id": "configured-evm/positions.erc4626",
      "network": "configured-evm",
      "capability": "positions.erc4626",
      "no_key_path": "opt-in pins for sDAI and Steakhouse USDC (vault exposure only) over evm_rpc, or caller-supplied vaults; off unless configured (`docs/positions-more.md`)",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed vault source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/erc4626.json",
        "config/protocols/erc4626-pins.json"
      ],
      "note": "With no configured vaults and no prior position the plugin returns unknown / vaults_not_configured and the cell is not required. That is not an empty position."
    },
    {
      "id": "bsc-mainnet/positions.lista",
      "network": "bsc-mainnet",
      "capability": "positions.lista",
      "no_key_path": "Lista slisBNB plugin over BSC RPC",
      "no_key_status": "partial",
      "keyed_path": "no keyed Lista source",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/protocols/lista-slisbnb.json"
      ],
      "note": "docs/coverage.md records an earlier read-only run-once. This preview did not re-run it. An unsigned plan stays unsent."
    },
    {
      "id": "hyperliquid-info/positions",
      "network": "hyperliquid-info",
      "capability": "positions",
      "no_key_path": "read-only info endpoint used by the legacy doctor path",
      "no_key_status": "partial",
      "keyed_path": "no keyed Hyperliquid source in the MCM routes",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [],
      "note": "docs/coverage.md records an earlier info read. An empty master response is not every subaccount at zero. This preview did not re-run it. hyperevm-mainnet is a separate planned network."
    },
    {
      "id": "configured-evm/positions.aave-v3",
      "network": "configured-evm",
      "capability": "positions.aave-v3",
      "no_key_path": "Aave V3 plugin over evm_rpc",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed Aave source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/aave-v3.json"
      ],
      "note": "Offline plugin code. No live position probe is claimed in this preview."
    },
    {
      "id": "configured-evm/positions.uxlink",
      "network": "configured-evm",
      "capability": "positions.uxlink",
      "no_key_path": "UXLINK balanceOf over the public asset file and evm_rpc",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed UXLINK source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/uxlink-holdings.json"
      ],
      "note": "Offline fixtures. No live holding probe is claimed."
    },
    {
      "id": "solana-mainnet/positions.stake",
      "network": "solana-mainnet",
      "capability": "positions.stake",
      "no_key_path": "solana_stake plugin over Solana RPC",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed stake source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/solana-stake.json"
      ],
      "note": "Offline fixtures only. No live public-address check is claimed."
    },
    {
      "id": "prices/usd-quote",
      "network": "*",
      "capability": "prices.usd",
      "no_key_path": "CoinGecko public tier when COINGECKO_API_KEY is absent",
      "no_key_status": "not_verified",
      "keyed_path": "optional COINGECKO_API_KEY demo tier",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/coingecko.json"
      ],
      "note": "A missing quote is valuation_unknown. The keyed example can name the key; the no-key example leaves credential_env off. No live quote probe is claimed."
    },
    {
      "id": "risk/address-asset",
      "network": "*",
      "capability": "address_risk",
      "no_key_path": "GoPlus without GOPLUS_API_KEY",
      "no_key_status": "credential_missing",
      "keyed_path": "GoPlus with GOPLUS_API_KEY",
      "keyed_status": "not_verified",
      "status": "partial",
      "capability_files": [
        "config/source-capabilities/goplus.json"
      ],
      "note": "credential_missing is not a clean bill of health. third_party_wallet_queries stays deny_until_local_consent. No live risk probe is claimed."
    },
    {
      "id": "sanctions/ofac",
      "network": "*",
      "capability": "sanctions",
      "no_key_path": "public OFAC feed named by OFAC_SLS_FEED; no API key",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed OFAC source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/ofac_sls.json"
      ],
      "note": "An unset feed is feed_not_supplied in the product. This matrix records that as not_verified. An empty balance is not a sanctions result."
    },
    {
      "id": "risk/chainabuse",
      "network": "*",
      "capability": "address_risk",
      "no_key_path": "not enabled in examples/monitor.json",
      "no_key_status": "not_verified",
      "keyed_path": "Chainabuse would need an operator key this package does not configure",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/chainabuse.json"
      ],
      "note": "The capability file is present. The example monitor does not route it. No live probe is claimed."
    },
    {
      "id": "portfolio/debank",
      "network": "*",
      "capability": "positions",
      "no_key_path": "not enabled in examples/monitor.json",
      "no_key_status": "not_verified",
      "keyed_path": "DeBank is not a configured keyed source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/debank.json"
      ],
      "note": "docs/coverage.md already called this family blocked for lack of a key. This matrix does not promote it. The cell stays not_verified."
    },
    {
      "id": "cosmos/comet",
      "network": "cosmos-hub",
      "capability": "comet.rpc",
      "no_key_path": "comet_rpc adapter file only",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed Comet source in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/comet_rpc.json"
      ],
      "note": "The adapter file is a contract. The example monitor does not enable Cosmos. network-tiers.plan.json marks the network planned_not_live_verified."
    },
    {
      "id": "linea-mainnet/enabled-route",
      "network": "linea-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1 in network-tiers.plan.json. Not in examples/monitor.json enabled_networks. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "zksync-era/enabled-route",
      "network": "zksync-era",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "scroll-mainnet/enabled-route",
      "network": "scroll-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "mantle-mainnet/enabled-route",
      "network": "mantle-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "sonic-mainnet/enabled-route",
      "network": "sonic-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "unichain-mainnet/enabled-route",
      "network": "unichain-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "hyperevm-mainnet/enabled-route",
      "network": "hyperevm-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 1. Distinct from the Hyperliquid info row. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "bitcoin-mainnet/enabled-route",
      "network": "bitcoin-mainnet",
      "capability": "enabled_route",
      "no_key_path": "listed in examples/monitor.json enabled_networks; address-set-only Esplora path (see the dated Bitcoin probe rows above)",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/esplora.json"
      ],
      "note": "Bitcoin tier 1. esplora.json is the capability file. Listed in the example monitor; address-set-only. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "tron-mainnet/experimental-reader",
      "network": "tron-mainnet",
      "capability": "experimental_reader",
      "no_key_path": "experimental, opt-in, disabled by default; bounded native TRX + TRC-20 history + configured balances; not live-qualified; only finalized native TRX produces alert candidates; do not use as the only safety monitor (`docs/coverage-tron.md`)",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/tron_grid.json"
      ],
      "note": "Chain file stays enabled:false and planned_not_live_verified; genesis identity is not verified; no TRC-20 or approval alerting."
    },
    {
      "id": "ethereum-mainnet/positions.compound-v3",
      "network": "ethereum-mainnet",
      "capability": "positions.compound-v3 (USDC Comet)",
      "no_key_path": "opt-in; stored-index present value, not accrued to now; block height is not block-hash proof; a positive oracle price is not freshness (`docs/positions-more.md`)",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/protocols/compound-v3.json"
      ],
      "note": "Opt-in only. Offline fixtures; no live verification is claimed."
    },
    {
      "id": "yield-promotions/parsers",
      "network": "yield / promotions",
      "capability": "parsers",
      "no_key_path": "experimental offline fixture and parser, disabled; no live fetch, scheduler or delivery; extracted terms are partial interpretations (`docs/yield-promotions.md`)",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed source",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/yields/pool-mapping.v1.json"
      ],
      "note": "Inert package: parsers and fixtures only."
    },
    {
      "id": "blast-mainnet/enabled-route",
      "network": "blast-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 2. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "opbnb-mainnet/enabled-route",
      "network": "opbnb-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/evm_rpc.json"
      ],
      "note": "EVM tier 2. etherscan_v2.json says the opBNB API is not this adapter. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "ton-mainnet/enabled-route",
      "network": "ton-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/toncenter_v2.json"
      ],
      "note": "TON tier 2. toncenter_v3.json is a second unrouted file. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "sui-mainnet/enabled-route",
      "network": "sui-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/sui_graphql.json"
      ],
      "note": "Sui tier 2. sui_grpc.json is a second unrouted file. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "aptos-mainnet/enabled-route",
      "network": "aptos-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/aptos_rest.json"
      ],
      "note": "Aptos tier 3. aptos_indexer.json is a second unrouted file. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "cosmos-hub/enabled-route",
      "network": "cosmos-hub",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/cosmos_rest.json"
      ],
      "note": "Cosmos tier 3. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "osmosis-mainnet/enabled-route",
      "network": "osmosis-mainnet",
      "capability": "enabled_route",
      "no_key_path": "not in examples/monitor.json enabled_networks",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route in the example monitor",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/cosmos_rest.json"
      ],
      "note": "Osmosis tier 3. Not enabled in the example monitor. network-tiers.plan.json status is planned_not_live_verified."
    },
    {
      "id": "unrouted/toncenter_v3",
      "network": "ton-mainnet",
      "capability": "enabled_route",
      "no_key_path": "toncenter_v3.json is not the example route",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/toncenter_v3.json"
      ],
      "note": "Second TON capability file. Not enabled. Not a live probe."
    },
    {
      "id": "unrouted/sui_grpc",
      "network": "sui-mainnet",
      "capability": "enabled_route",
      "no_key_path": "sui_grpc.json is not the example route",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/sui_grpc.json"
      ],
      "note": "Second Sui capability file. Not enabled. Not a live probe."
    },
    {
      "id": "unrouted/aptos_indexer",
      "network": "aptos-mainnet",
      "capability": "enabled_route",
      "no_key_path": "aptos_indexer.json is not the example route",
      "no_key_status": "not_verified",
      "keyed_path": "no keyed route",
      "keyed_status": "not_verified",
      "status": "not_verified",
      "capability_files": [
        "config/source-capabilities/aptos_indexer.json"
      ],
      "note": "Second Aptos capability file. Not enabled. Not a live probe."
    }
  ],
  "endpoint_probes": [
    {
      "id": "publicnode-ethereum-getLogs",
      "status": "blocked",
      "detail": "publicnode Ethereum eth_getLogs without an address returns -32701 for any range."
    },
    {
      "id": "publicnode-avalanche-getLogs",
      "status": "blocked",
      "detail": "publicnode Avalanche eth_getLogs without an address returns -32701 for any range."
    },
    {
      "id": "publicnode-polygon-getLogs",
      "status": "blocked",
      "detail": "publicnode Polygon eth_getLogs without an address returns -32701 for any range."
    },
    {
      "id": "publicnode-arbitrum-getLogs",
      "status": "blocked",
      "detail": "publicnode Arbitrum eth_getLogs returns HTTP 403."
    },
    {
      "id": "arb1.arbitrum.io-getLogs",
      "status": "partial",
      "detail": "arb1.arbitrum.io accepts eth_getLogs at 2000 blocks. That window is not a finished history."
    },
    {
      "id": "api.avax.network-getLogs",
      "status": "partial",
      "detail": "api.avax.network accepts eth_getLogs at 2000 blocks. Deep native history stays expansion_pending."
    },
    {
      "id": "polygon.gateway.tenderly.co-getLogs",
      "status": "partial",
      "detail": "polygon.gateway.tenderly.co accepts eth_getLogs at 2000 blocks."
    },
    {
      "id": "nodereal-eth-getLogs",
      "status": "partial",
      "detail": "NodeReal Ethereum eth_getLogs accepts 2000 blocks. This is not the BSC nr_getAssetTransfers row."
    },
    {
      "id": "nodereal-opt-getLogs",
      "status": "partial",
      "detail": "NodeReal Optimism eth_getLogs accepts 2000 blocks."
    },
    {
      "id": "tenderly-base-getLogs",
      "status": "partial",
      "detail": "Base Tenderly log cap is 1000 blocks, matching tenderly_base range_limit."
    },
    {
      "id": "base-1rpc-getLogs",
      "status": "partial",
      "detail": "1rpc Base cap is 50 blocks. examples/monitor.json leaves base-1rpc disabled."
    },
    {
      "id": "mainnet.base.org-getLogs",
      "status": "blocked",
      "detail": "mainnet.base.org returned HTTP 429. That endpoint is not the configured Base log route."
    }
  ]
}
```

```json
{
  "kind": "mcm-share-s8-coverage-evidence",
  "cells": {
    "bsc-mainnet/history.native_external": {
      "keyed": {
        "offline_fixtures": true,
        "live_probe": true,
        "fixture_tests": [
          "test_mcm_w4_03_nodereal_splits_span_and_walks_page_key"
        ],
        "live_probe_record": "2026-10-09 NodeReal nr_getAssetTransfers on BSC, external category, pageKey followed. No monitored address is written here."
      }
    },
    "ethereum-mainnet/history.native_external": {
      "keyed": {
        "offline_fixtures": true,
        "live_probe": true,
        "fixture_tests": [
          "test_mcm_w4_02_etherscan_histories_are_separate"
        ],
        "live_probe_record": "Etherscan V2 free plan history works for Ethereum. Recorded fact used by this preview; not re-run here. No monitored address is written here."
      }
    }
  }
}
```

```json
{
  "kind": "mcm-share-s8-keyed-sources",
  "keyed_sources": [
    {
      "id": "etherscan",
      "credential_env": "ETHERSCAN_API_KEY",
      "anchor": "etherscan",
      "no_key": "Keyless Blockscout on Ethereum, Arbitrum, Polygon, Base, and Optimism when that endpoint is set. BSC does not use Etherscan. With no key and no keyless instance the row is credential_missing.",
      "capability_file": "config/source-capabilities/etherscan_v2.json"
    },
    {
      "id": "nodereal-bsc",
      "credential_env": "NODEREAL_API_KEY",
      "anchor": "nodereal",
      "no_key": "credential_missing. There is no keyless BSC external-history source. BSC token logs stay on public RPC when that endpoint accepts them.",
      "capability_file": "config/source-capabilities/nodereal_transfers.json"
    },
    {
      "id": "blockscout-pro",
      "credential_env": "BLOCKSCOUT_API_KEY",
      "anchor": "blockscout",
      "no_key": "HTTP 402 without the key is credential_missing, then the keyless instance for that chain. Optimism is on the free plan. Base on the free plan is plan_not_supported.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "blockscout-base",
      "credential_env": "BLOCKSCOUT_BASE_API_KEY",
      "anchor": "blockscout",
      "no_key": "A missing BLOCKSCOUT_BASE_API_KEY does not invent an empty history. The key travels as a bearer header.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "blockscout-optimism",
      "credential_env": "BLOCKSCOUT_OPTIMISM_API_KEY",
      "anchor": "blockscout",
      "no_key": "Keyless fallback is BLOCKSCOUT_OPTIMISM_PUBLIC_URL. The Linux host recorded HTTP 200 for optimism.blockscout.com.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "alchemy-base",
      "credential_env": "ALCHEMY_API_KEY",
      "anchor": "alchemy",
      "no_key": "credential_missing, then Blockscout PRO and the keyless Base instance. The adapter is implemented. No live probe, so a keyed run stays not_verified.",
      "capability_file": "config/source-capabilities/alchemy_transfers.json"
    },
    {
      "id": "goplus",
      "credential_env": "GOPLUS_API_KEY",
      "anchor": "goplus",
      "no_key": "credential_missing. That is not a clean bill of health. Wallet-address queries stay off until local consent.",
      "capability_file": "config/source-capabilities/goplus.json"
    },
    {
      "id": "coingecko",
      "credential_env": "COINGECKO_API_KEY",
      "anchor": "coingecko",
      "no_key": "Public keyless tier when the key is absent. examples/monitor.nokeys.json leaves credential_env off. A missing quote is valuation_unknown.",
      "capability_file": "config/source-capabilities/coingecko.json"
    },
    {
      "id": "helius-parser",
      "credential_env": "HELIUS_API_KEY",
      "anchor": "helius",
      "no_key": "Parsed events are credential_missing. The configured Solana RPC still serves account reads. examples/monitor.json leaves this source disabled; examples/monitor.nokeys.json enables it so the missing key is visible.",
      "capability_file": "config/source-capabilities/helius_parsed_events.json"
    }
  ],
  "keyless_sources": [
    {
      "id": "ofac",
      "credential_env": null,
      "anchor": "ofac",
      "no_key": "No API key. OFAC_SLS_FEED names the public list. An unset variable is feed_not_supplied, recorded in the matrix as not_verified.",
      "capability_file": "config/source-capabilities/ofac_sls.json"
    },
    {
      "id": "blockscout-eth-public",
      "credential_env": null,
      "anchor": "blockscout",
      "no_key": "Keyless Ethereum instance. BLOCKSCOUT_ETH_URL defaults to the public eth.blockscout.com origin. No live probe of that host is recorded in this preview.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "blockscout-base-public",
      "credential_env": null,
      "anchor": "blockscout",
      "no_key": "Keyless Base instance. Linux host: HTTP 403 bot_challenge. Operator Windows PC: HTTP 200. Host-specific, so the matrix cell is partial.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "blockscout-optimism-public",
      "credential_env": null,
      "anchor": "blockscout",
      "no_key": "Keyless Optimism instance. The Linux host recorded HTTP 200. The matrix cell stays partial because that 200 is not a finished history.",
      "capability_file": "config/source-capabilities/blockscout.json"
    },
    {
      "id": "tenderly-base",
      "credential_env": null,
      "anchor": "alchemy",
      "no_key": "No key. TENDERLY_BASE_URL, range_limit 1000. See the Base log row. The anchor documents the Base log endpoints next to Alchemy.",
      "capability_file": "config/source-capabilities/evm_rpc.json"
    }
  ]
}
```

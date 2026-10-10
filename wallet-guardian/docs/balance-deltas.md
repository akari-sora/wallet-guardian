# Native balance deltas

A `balance_delta` alert means the native coin balance of one watched wallet changed between two recorded checkpoints by at least one usable threshold. It does not name a transaction, a counterparty, or a cause unless a separate read supports that detail.

The comparison does not wait for a nonce change. An internal transfer can move the native balance while the sender nonce stays the same. The first valid observation is a quiet baseline. It is not a historical alert.

## What is compared

Each eligible observation is a balance at one block. The preferred read is EIP-1898: `eth_getBalance` with that block's hash and `requireCanonical: true`. If the endpoint rejects that parameter, the read uses the block number and says so (`fixed_height` is a weaker binding). `eth_getBalance("latest")` is not a checkpoint. A block tag the chain does not serve falls through to the numeric head. BSC uses its own confirmation policy (`bsc_bep126_fast_finality`). It does not inherit an Ethereum confirmation depth, and a successful `finalized` tag on BSC is recorded as confirmed rather than Ethereum-final.

The signed change is new balance minus previous balance, in the chain's smallest unit. Decreases use the magnitude. `direction` is `increase` or `decrease`. `cause` stays `unknown`.

## Thresholds

A candidate alerts when either floor is usable and met:

- On `ethereum-mainnet`, `arbitrum-one`, `base-mainnet`, and `optimism-mainnet`, the existing `native_eth_threshold` (default 0.5) applies in wei.
- `alerts.native_unit_thresholds` may set an explicit decimal amount in that chain's native unit, keyed by the canonical network id. A BSC profile can set `bsc-mainnet` to `1.0`. That value is not applied to any other chain, and 0.5 BNB is not treated as 0.5 ETH.
- A fresh USD quote from the existing CoinGecko simple-price path can also meet `asset_usd_threshold` (default 1000). Coin ids come from the pinned catalog. The quote must include `last_updated_at` and be at most 15 minutes old. A missing or stale quote is not zero and is not a hit.

If a native floor is usable and not met, a missing USD quote does not turn the decision into `threshold_unavailable`. That decision is used only when neither a native floor nor a fresh USD quote can be applied. A `threshold_unavailable` observation still advances the baseline and is not an outbox alert. A below-threshold observation also advances the baseline, so later rounds do not add the small moves together.

## What a failed read does

A failed, malformed, wrong-chain, or inconsistent read does not replace the baseline with zero. The previous checkpoint stays. The same block height with a different hash does not alert and does not overwrite the baseline. A backwards answer inside the chain's retention window does the same. A backwards jump deeper than that retention window starts a new baseline and records a continuity gap. It does not invent a movement. A later successful read compares against the last usable observation and says when the interval is longer than one quiet round.

## History and labels

Known native transfers and fees can explain a delta after the fact. The explanation enriches the existing alert. It does not insert a second payment row. Token logs do not explain a native balance. Two large opposing transfers stay visible when their net is zero; they are not deleted just because the net change is zero.

BSC can read NodeReal `nr_getAssetTransfers` category `internal` as `history.native_internal`, with its own cursor. The statuses are `not_queried`, `credential_missing`, `plan_not_supported`, `read_error`, and `rate_limited`. A missing key does not block the native-unit balance alert. A failed child call is not a transfer. A missing trace id stays `indexer_reported` limited attribution. It is not an invented trace index.

A static label such as Lista StakeManager attaches only to a counterparty already present on a transfer or trace, on `bsc-mainnet`, at the full address. The label source is `static_local_registry`. `counterparty_identity` is `unverified` and `safety_assessment` is `not_established`. A label does not lower severity and does not attach to a delta-only observation. A withdrawal that increases liquid BNB is not automatically profit.

## Limits

- The alert is a net change between checkpoints, not the gross flow. Cancelled movements that net to zero are invisible to this alert.
- It does not claim a cause. Transaction hash, counterparty, and withdrawal text appear only when a separate read supports them.
- No-key BSC still alerts when the configured native-unit threshold is met. Internal history stays `credential_missing` in that case.
- This document does not claim a live balance probe or a live internal-transfer probe.

# Native balance deltas

A `balance_delta` alert means the native coin balance of one watched wallet changed between two recorded checkpoints by at least one usable threshold. It does not name a transaction, a counterparty, or a cause unless a separate read supports that detail.

The comparison does not wait for a nonce change. An internal transfer can move the native balance while the sender nonce stays the same. The first valid observation is a quiet baseline. It is not a historical alert.

## What is compared

Each eligible observation is a balance at one block. The preferred read is EIP-1898: `eth_getBalance` with that block's hash and `requireCanonical: true`. A numeric height (`fixed_height`, a weaker binding) is used only when that parameter or tag is an established unsupported error, such as invalid params or an unsupported tag. "Block not canonical", block-not-found, unavailable historical state, HTTP 429, and other transient failures do not fall back and are not eligible observations. A body that carries both `result` and `error`, and a transport result that is not ok, are rejected the same way. A failed or rate-limited `finalized` read does not continue into a head read unless that tag is established as unsupported. `eth_getBalance("latest")` is not a checkpoint. BSC uses its own confirmation policy (`bsc_bep126_fast_finality`). It does not inherit an Ethereum confirmation depth, and a successful `finalized` tag on BSC is recorded as confirmed rather than Ethereum-final.

The signed change is new balance minus previous balance, in the chain's smallest unit. Decreases use the magnitude. `direction` is `increase` or `decrease`. `cause` stays `unknown`.

## Thresholds

A candidate alerts when either floor is usable and met:

- On `ethereum-mainnet`, `arbitrum-one`, `base-mainnet`, and `optimism-mainnet`, the existing `native_eth_threshold` (default 0.5) applies in wei.
- `alerts.native_unit_thresholds` may set an explicit decimal amount in that chain's native unit, keyed by the canonical network id. An operator can set `bsc-mainnet` to `1.0` in a private profile. That `1.0` is an example, not a value shipped in the example monitor. It is not applied to any other chain, and 0.5 BNB is not treated as 0.5 ETH. A key that is not a canonical network name is a config error, and the error lists the valid names. The same native-unit floor applies to native-transfer candidates and to balance deltas. Own-wallet pairs are still judged on their net.
- A fresh USD quote from the existing CoinGecko simple-price path can also meet `asset_usd_threshold` (default 1000). Coin ids come from the pinned catalog. The quote must include `last_updated_at` and be at most 15 minutes old. A missing or stale quote is not zero and is not a hit.

If a native floor is usable and not met, a missing USD quote does not turn the decision into `threshold_unavailable`. That decision is used only when neither a native floor nor a fresh USD quote can be applied. A `threshold_unavailable` observation still advances the baseline and is not an outbox alert. A below-threshold observation also advances the baseline, so later rounds do not add the small moves together.

## What a failed read does

A failed, malformed, wrong-chain, or inconsistent read does not replace the baseline with zero. The previous checkpoint stays. Each observation is bound to the baseline version read before the fetch. It is stored only when that version is still current; a newer baseline is used to recompute, and an older observation is discarded. The same block height with a different hash is kept as a conflict. It does not alert and does not overwrite the baseline. A single lower block does not start a new baseline, including when the distance is larger than retention. The baseline is replaced only by an explicit rebaseline after the stored block is read again and its hash is not canonical. That replacement is not a movement alert. `retention_blocks` deletes checkpoints older than that distance from the new block. It is not proof that a polling round was missed. A longer interval is recorded only after a failed round, and a later successful read then compares against the last usable observation.

## History and labels

Known native transfers and fees can explain a delta after the fact when the chain, asset, and checkpoints are compatible, the native effects succeeded, and any fee is attributable. An equal amount is not enough. `indexer_reported` and limited execution evidence stay limited. The explanation enriches the stored alert. It does not insert a second payment row. If a native transfer alert is already queued or sent and a later balance delta covers the same movement, or the delta is stored first, the two records stay linked and only one new-movement notification is pending. A sent delivery row stays sent. The combined record keeps material gross movements and fees. Token logs do not explain a native balance. Two large opposing transfers stay visible when their net is zero; they are not deleted just because the net change is zero. A native-threshold hit is committed, with its decision, before optional pricing. A pricing failure does not drop that observation.

BSC can read NodeReal `nr_getAssetTransfers` category `internal` as `history.native_internal`, with its own cursor. The statuses are `not_queried`, `credential_missing`, `plan_not_supported`, `read_error`, and `rate_limited`. A missing key does not block the native-unit balance alert. A failed child call is not a transfer. A missing trace id stays `indexer_reported` limited attribution. It is not an invented trace index.

A static label such as Lista StakeManager attaches only to a counterparty already present on a transfer or trace, on `bsc-mainnet`, at the full address. The label source is `static_local_registry`. `counterparty_identity` is `unverified` and `safety_assessment` is `not_established`. A label does not lower severity and does not attach to a delta-only observation. A withdrawal that increases liquid BNB is not automatically profit.

## Limits

- The alert is a net change between checkpoints, not the gross flow. Cancelled movements that net to zero are invisible to this alert.
- It does not claim a cause. Transaction hash, counterparty, and withdrawal text appear only when a separate read supports them.
- No-key BSC alerts on a native movement when a configured native-unit floor is met, or when a usable USD quote meets the USD floor. With neither, the decision is `threshold_unavailable` and there is no outbox alert. Internal history stays `credential_missing` when the key is absent. A configured `1.0` BNB floor is part of the operator's private profile, not a global default.
- This document does not claim a live balance probe or a live internal-transfer probe.

## What this closes, and what the older patrol already did

The older private legacy patrol is outside this package. On 2026-10-10 its 11:20 run did report the +8.421285 BNB claim (ListaStakeManager `claimWithdrawFor`, tx `0xc79b5c45440267284140fcf8ff59a7b7c4c59e6839ea83fc972232b7faa99218`, block 126733521, nonce unchanged) through the NodeReal `internal` transfer category. It reported the claim about three hours after it landed. So the BSC patrol path did list internal transfers.

The gaps this package addresses are narrower:

- Latency: the patrol had no balance-delta check and no cheap probe, so a large move waited for the next scheduled run. `docs/balance-probe-design.md` is a design only, with no scheduler.
- Other chains: the patrol made no large-inflow judgment on non-BSC chains. A balance delta does not depend on a history source, so it covers every network with a readable native balance.
- This package's history layer: before it, `history.native_internal` was `not_queried` in rc.3. Now only BSC via NodeReal `internal` is queried, with offline fixtures. Other networks stay `not_queried` or `unsupported`.

A known protocol sender such as the Lista StakeManager is a static label. It is labelled, and the counterparty stays UNVERIFIED unless a source proves it.

# FAQ and troubleshooting

Package version in this tree: `0.4.0rc1`. The monitor is a read-only preview. The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. The 72h shadow validation is not complete. A failed read is a gap. It is not a zero balance, not a complete history, and not an all-clear.

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

A fresh database does not walk from genesis. With no cursor and no `polling.bootstrap_origin`, the origin is `max(0, head - ceil(bootstrap_lookback_days * 86400 / seconds_per_block))`. Init sets `bootstrap_lookback_days` to 30 and `bootstrap_request_cap` to 2000. Built-in block times are ethereum 12s, bsc 0.75s, arbitrum 0.25s, base 2s, optimism 2s, polygon 2.1s, avalanche 2s, and 12s otherwise. When `round_deadline_seconds` is omitted, the round stops after 180 seconds.

Hitting the cap or the deadline records `partial` with `bootstrap_request_cap` or `deadline`, plus `historical_backfill_pending` while older blocks are still unread. The next round resumes at that checkpoint plus one, minus an overlap of 2. At the default cap, an Arbitrum 30-day log backfill is many rounds if the deadline does not stop a round first. `pagination_incomplete` means a page walk stopped at its cap. That history is not complete. Bitcoin history stops at a page cap the same way: partial, never a finished chain.

## `plan_not_supported` on the free Etherscan plan

The free Etherscan V2 plan returns history for Ethereum, Arbitrum, and Polygon. Base, Optimism, BNB Chain, and Avalanche on that free plan return `plan_not_supported` ("Free API access is not supported for this chain"). The refusal is cached for that source and network. Later wallets and the next round skip straight to the fallback. A cached refusal is not an empty history. Avalanche deep native history is `expansion_pending` in the coverage matrix. Base on Blockscout PRO's free plan is HTTP 402, also `plan_not_supported`, then the keyless Base instance. Optimism on that Blockscout plan returned HTTP 200 in the recorded probe. That is not the Base refusal.

## DNS and egress blocked

If the system resolver returns an address in `198.18.0.0/15`, the lookup is refused. That range is not allowlisted. Set `GUARDIAN_RESOLVER_MODE=doh` and supply `GUARDIAN_DOH_ENDPOINTS` and `GUARDIAN_DOH_BOOTSTRAP` as [host-network.md](host-network.md) describes. The public config only names those variables.

`guardian egress-doctor` prints `verdict` `OK` or `BLOCKED_EGRESS`. Formal `BLOCKED_EGRESS` means destination enforcement was not shown (on the reviewed host, an SNI proxy). A preview verdict of `OK` on that host is not proof. A failed lookup is not a complete read and not a zero balance. A Cloudflare HTTP 403 page that says `Just a moment...` on keyless Blockscout is `blocked` with `bot_challenge`, and it is host-specific. It is not an empty wallet.

## Sleeping Mac

A laptop that is asleep at the slot does not run that slot. The shipped timers set `Persistent=false`, so systemd does not fire the missed slot on wake as an on-time run. `persistent_counts_as_on_time` is false. The slot recorder stores `reason` `missed`, or `late_start` if the process starts more than 30 minutes late. A later catch-up may fill data and does not count as the missed slot having run on time. `install-service` does not write a macOS LaunchAgent. See [scheduling.md](scheduling.md). Host smoke of missed-slot recording is `NOT_RUN`.

## An empty alert outbox is not all clear

`outbox_written` of 0 means no new deduped alert was inserted. It does not mean every capability was read, that history is complete, or that nothing moved. Movements under `native_eth_threshold` or `asset_usd_threshold` are `below_threshold` and are not written. A missing USD quote does not meet the USD rule. Coverage rows can still say `credential_missing`, `partial`, `identity_unreadable`, `rate_limited`, `plan_not_supported`, `feed_not_supplied`, or `unknown`. `--no-send` leaves `sent` at 0 even when rows were written. Default delivery is `outbox_only`. Nothing was sent to Telegram unless you opted in, and the Telegram canary is `NOT_RUN`.

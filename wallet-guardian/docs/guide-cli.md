# CLI guide

Wallet Guardian's monitor is a preview you run on a machine you control. The package version in this tree is `0.4.0rc4`. It is read-only: it does not sign, build a transaction, or broadcast, and it does not ask for a seed phrase or a private key. The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. What each chain can actually read is [coverage-matrix.md](coverage-matrix.md). The 72h shadow validation is not complete. Checks on these preview commits are a local gate, not a CI pass. There is no PyPI release.

You do not need Grok Bot, Cloudflare, D1, a wallet login, or Node to follow this page. Flags for every command are in [cli-reference.md](cli-reference.md).

## Install

Download one release's wheel, `SHA256SUMS`, and `constraints.txt` from the project's GitLab Releases page: `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.4`. Check the hash before you install. `uv tool install` does not apply `constraints.txt` unless you pass it. Do not install a package named `guardian` from PyPI. The steps and the Windows interpreter path are in [install.md](install.md).

```bash
uv tool install --constraints constraints.txt ./wallet_guardian_monitor-0.4.0rc4-py3-none-any.whl
guardian --help
```

Before the first `run-once`, export an endpoint for each chain you enabled, for example `export RPC_ETHEREUM_MAINNET_URL="https://ethereum-rpc.publicnode.com"` (any public JSON-RPC URL you choose). Without it `run-once` stops with `endpoint_missing`. `ethereum-rpc.publicnode.com` answers balance reads but refuses address-less `eth_getLogs` (-32701, recorded in [coverage-matrix.md](coverage-matrix.md)). With only that endpoint, `permissions.standard` and `history.token` stay gaps. For approval monitoring, use an endpoint that accepts topic-only `eth_getLogs` over a 2000-block range; the matrix lists the ones that did on the recorded probe. See [faq-troubleshooting.md](faq-troubleshooting.md).

The wheel name in this tree is `wallet_guardian_monitor-0.4.0rc4-py3-none-any.whl`. A later release uses that release's own wheel and constraints file.

## Quickstart

Create `answers.json` outside any git checkout. This minimal file is complete; its address is synthetic, so replace it with an address you want to watch:

```bash
cat > answers.json <<'JSON'
{
  "tenant_id": "local",
  "key_mode": "none",
  "plugins": [],
  "accounts": [
    {
      "label": "demo-wallet",
      "family": "evm",
      "address": "0x0000000000000000000000000000000000000001",
      "networks": ["ethereum-mainnet"],
      "relationship": "watch_only"
    }
  ]
}
JSON
```

(The repository also ships `examples/answers.example.json` with the same content.) Each account needs `label`, `family`, `address`, `networks`. `key_mode` is `none` or `byo`. `relationship` is `watch_only` or `owned_user_declared`. A watch address is not proof of control. Init accepts `family` values `evm`, `solana`, `bitcoin`, `tron`, `aptos`, `cosmos`, `sui`, and `ton`. `run-once` reads `evm`, `solana`, `bitcoin`, and experimental `tron` when that account is configured. `aptos`, `cosmos`, `sui`, and `ton` are accepted and a source is written (`cosmos_rest`, `sui_graphql`, `aptos_rest`, or `toncenter_v3`). The round then reports `unsupported_family` and does not query those hosts.

```bash
guardian init --from answers.json --yes
guardian config validate --mcm-config "$HOME/.config/guardian/monitor.json"
guardian run-once --mcm-config "$HOME/.config/guardian/monitor.json" --no-send
guardian coverage --mcm-config "$HOME/.config/guardian/monitor.json"
```

`init` without `--dir` writes the XDG config directory: `$XDG_CONFIG_HOME/guardian`, or `~/.config/guardian` when that variable is unset. `GUARDIAN_CONFIG_DIR` overrides it. The command prints `private_dir` and `state_dir`. Use the `monitor.json` inside `private_dir` if you passed `--dir`. Init refuses a directory inside the git work tree. Nothing is written until `--yes` or until you type `yes`. A second run refuses to overwrite. `--force` backs up the config files and does not touch `monitor.sqlite`.

`run-once --no-send` still reads the endpoints you configured. It writes the local outbox and does not hand those rows to a sender. `sent` stays 0 and `delivery_status` is `no_send`. `live_verified` stays false. `canary` is `NOT_RUN` (neither a pass nor a failure).

The older Lista path uses `--config` and is unchanged. This guide is the `--mcm-config` profile from `init`.

## Configuration and keys

Public config stores names, not values. Init writes:

| File | Role |
| --- | --- |
| `monitor.json` | Networks, sources, budgets, plugins, privacy, delivery, consent |
| `accounts.json` | The addresses you entered. Init does not derive more addresses |
| `secret-profile.json` | Env var names mapped to empty strings. Fill values here or export them |
| `delivery-profile.json` | `outbox_only`. No sender is started |
| `env-paths.json` | Paths only (`MONITOR_ACCOUNTS_FILE`, `MONITOR_DELIVERY_PROFILE`, `MONITOR_SECRET_PROFILE`, `MONITOR_STATE_DIR`) |

A missing key is `credential_missing`. The row's `credential_hint` names the variable and a section of [api-keys.md](api-keys.md), for example `ETHERSCAN_API_KEY see docs/api-keys.md#etherscan`. The value is not printed. Init does not copy an environment value into the secret profile.

No-key (`key_mode` `none`) leaves credential variables unset. Public RPC still runs when its endpoint variable is set. In an init profile no keyless history path is configured: every keyed indexer without its key is `credential_missing` (or `unsupported_source` if its URL variable is unset). Bring-your-own-key (`byo`) means you put your own values under the names already in the secret profile. The two modes are the same files. Details and free-tier limits are in [api-keys.md](api-keys.md).

Init's data-sharing summary lists each source before the write: whether a wallet address would be sent, that the machine hostname is not added, and which host would receive the call. GoPlus and Chainabuse are listed as off. `privacy.third_party_wallet_queries` stays `deny_until_local_consent`.

If you include Solana, init lists a Helius source, but rc.4 `run-once` never calls it. Solana coverage is native balance, token accounts, the signature list and SPL authorities. Transactions are not parsed, so there are no Solana transfer rows or transfer alerts. The checked-in `examples/monitor.json` leaves Helius disabled; a profile you create with `init` lists the source and still does not call it.

Endpoint variables are URLs you choose. Do not put a key inside the URL. Some providers put the key in the query string themselves (Etherscan `apikey`, Helius `api-key`). Blockscout sends its key as a bearer header.

## Reading coverage

`guardian coverage --mcm-config <monitor.json>` prints stored rows. It does not start a new network read. `live_verified` is false. A row that was never written is absent. Absence is unknown, not a finished scan and not a zero balance.

`quality` is one of `complete_in_declared_scope`, `partial`, `stale`, `error`, `unknown`. Read `reason_codes` next to it.

| Word | What it means |
| --- | --- |
| `complete_in_declared_scope` | The declared window was read, and pagination and gap detection finished. The window comes from the cursor or from `polling.bootstrap_lookback_days` (30 in the init profile). It is not a promise about older history. An empty answer inside that window is an empty window, not "this wallet has always been empty". |
| `partial` | The read stopped early. Look for `pagination_incomplete`, `gap_detection_incomplete`, `gaps_present`, `bootstrap_request_cap`, `deadline`, or `historical_backfill_pending`. The next round can resume. Partial is not complete. |
| `credential_missing` | That source was not called because the named variable is unset. The hint points at [api-keys.md](api-keys.md). Not a zero balance and not `complete_in_declared_scope`. |
| `identity_unreadable` | The chain identity read (for EVM, `eth_chainId`) did not return a usable answer. Causes stay beside it, including `rate_limited` if retries inside the round deadline still fail. Native balance, fungible history, and permission rows for that network stay blocked. Not zero. |
| `rate_limited` | HTTP 429 or the local request budget. The monitor waits, including `Retry-After` on `eth_chainId`, only inside the round deadline. A row that still does not finish stays partial, or `identity_unreadable` with this cause. Not zero and not complete. |
| `feed_not_supplied` | Sanctions coverage when the monitor file has no `ofac_sls` source, or when that source's endpoint is empty. Init stores the list name `us-ofac-sdn` and does not add the source, so the variable alone stays here. That is not "not sanctioned". `examples/monitor.json` includes `"adapter": "ofac_sls"`. `run-once` uses the first such source and does not read `enabled`: `enabled: false` still fetches the list when the endpoint is set. Remove the source or clear the endpoint to avoid the fetch. The endpoint variable is whatever `endpoint_env` names, for example `OFAC_SLS_FEED`. When that endpoint names a readable SDN XML list, `run-once` exact-matches the monitored address and counterparties locally. A list older than 7 days is `partial` with `feed_stale`. An unreadable or undated list is an error. Treat a no-hit result as only as good as the list date shown in coverage. The matrix cell stays `not_verified` (no live probe). |

Other reason codes you will meet, and that are also gaps: `plan_not_supported`, `blocked`, `bot_challenge`, `unsupported`, `not_observable`, `not_applicable`, `not_queried`, `expansion_pending`, `valuation_unknown`. Unknown prices stay unknown. They are not stored as 0 and not stored as 1.

`support` is separate from `quality`. `unsupported` and `not_observable` stay unknown. ERC-4626 with no configured vaults is unknown / `vaults_not_configured`, not an empty position.

## Alert types

Init writes `native_eth_threshold` `0.5` and `asset_usd_threshold` `1000`. The alert policy reads those two fields. A native ETH amount is compared in wei, and only on `ethereum-mainnet`, `arbitrum-one`, `base-mainnet`, and `optimism-mainnet`. Native coins can use a USD quote when a CoinGecko source is configured (init does not add one). Token transfers use that same source's free contract price when it is enabled. A fresh quote can reach `asset_usd`. A missing quote stays `valuation_unknown` and is not stored as 0. A pinned stablecoin at least as large as `asset_usd_threshold` units, or a hit on `alerts.token_raw_thresholds`, alerts as `unvalued_large_move` without claiming a USD price. The pinned list is 13 `0x` contracts on `ethereum-mainnet`, `arbitrum-one`, `optimism-mainnet`, `base-mainnet`, `polygon-pos`, `bsc-mainnet`, and `avalanche-c` (`src/guardian/pricing/stables.py`). Token valuation only considers an asset id that starts with `0x`. Solana SPL and TRON TRC-20 amounts are not valued and do not take the `unvalued_large_move` path. Approval alerts come from `Approval(address,address,uint256)` and `ApprovalForAll(address,address,bool)` logs since the bootstrap origin. Init sets `bootstrap_lookback_days` to 30 and does not set `polling.bootstrap_origin`. Logs older than that origin are not inventoried. Set `polling.bootstrap_origin` before the first scan, or point the run at a new state directory that has no cursor and set it before that scan. The value is one block height and applies to every EVM chain. After a `permissions.erc20` cursor is stored, editing the setting does not walk backward and does not reset the cursor. A later round that records `historical_backfill_pending` continues from the saved cursor toward head. It does not walk back to genesis. Permit2 internal allowances are not tracked. A Permit2 `Permit` log is stored only as a `relay_permit` note with `not_current_allowance`, and a failed note read is skipped. An existing approval is inventoried only when its log is inside the bootstrap window on a state database that does not yet have a `permissions.erc20` cursor. Set `polling.bootstrap_origin` before that first scan, or use a new state directory. Editing it later does not reset the cursor. The value is one block height and applies to every EVM chain. The next scan with a stored cursor starts at that cursor plus one, minus an overlap of 2. Init writes `native_unit_thresholds` for the non-ETH networks it enables that have an amount-alert path: `bsc-mainnet` `1.0`, `polygon-pos` `1000`, `avalanche-c` `10`, and `bitcoin-mainnet` `0.01`. Those are operator units, not a USD equivalent. Init does not write a Solana floor. Solana transactions are not parsed and native balance deltas are EVM-only, so a Solana amount does not alert. An operator-set `solana-mainnet` value is not an amount-alert path. A key that is not a canonical network name is rejected and the error lists the valid names. The same floor applies to native transfers and to `balance_delta`. If a movement has neither a native floor nor a fresh USD quote, the decision is `threshold_unavailable`: one visible unjudged outbox row is written and the recorded baseline stays put so a later round can judge the same movement. Solana transaction deltas are not part of this floor. A net native balance change can be written as `balance_delta`; see [balance-deltas.md](balance-deltas.md). The monitor schema also requires the rest of the `alerts` object to stay at fixed values, including `permission_changes` true, `generic_news` false, and `unknown_price_policy` `show_unknown_not_zero`. Those constants are not switches you can flip in a valid file.

Outbox kinds the policy marks as alerts:

| Kind | When it is written |
| --- | --- |
| `baseline_summary` | First time a wallet, network, and capability are seen. Counts of events and explicit risks. Not a loss alert |
| `native_eth` | Native amount on the four ETH networks is at least `native_eth_threshold` |
| `native_unit` | Native amount on a network with `native_unit_thresholds` is at least that floor. Own-wallet pairs still use the net |
| `balance_delta` | Net native balance changed by at least a configured native-unit floor or a fresh USD floor. See [balance-deltas.md](balance-deltas.md) |
| `asset_usd` | A valued USD quote is at least `asset_usd_threshold`. Native coins use `simple/price`. Token contracts use `simple/token_price` when a CoinGecko source is enabled |
| `unvalued_large_move` | No fresh token quote, and either the contract is a pinned stablecoin at least as large as the USD floor or `token_raw_thresholds` is met. Also a priced token whose `decimals()` read is missing: `reason` is `decimals_unknown` or `decimals_unreadable`, and the amount is not stored as zero. Not a USD value |
| `internal_transfer` | A paired transfer between your own watched addresses, when the net meets a threshold |
| `position_change` | A position candidate |
| `first_observed` | A permission (or the same kind of change) with no prior observation |
| `permission_new` | New approval, operator, delegate, or authority |
| `permission_increased` | Allowance increased |
| `permission_extended` | Expiry extended |
| `revocation` | Decreased allowance or a revocation. Informational |
| `exposure`, `exposure_unknown`, `exposure_update` | A news incident linked to a watched position or approval. `loss_alert` is false |
| `monitor_blind`, `monitor_recovered` | From `guardian watchdog`, one row per blind episode |
| `monitor_new_gap` | A required key newly `error` or `blocked` during an episode that already knew the older gaps |

`below_threshold` is a decision that is not written. The older Lista path can also write `claimable` and `claim_missing`. Those are claim observations, not a broadcast.

A repeated `dedup_key` does not insert a second outbox row.

## Outbox

New alerts are inserted into the `outbox` table of `monitor.sqlite` with status `pending`. `guardian outbox pending --mcm-config <monitor.json>` lists those rows (`id`, `kind`, `dedup_key`, `network`, `identity`) and does not print a wallet address. `guardian outbox ack --id <id>` marks one pending row `acknowledged`. That is a relay receipt, not a send. A sent row is left sent. `run-once` reports `outbox_written` (rows newly inserted after dedup) and `sent`. With `--no-send`, `sent` is 0 even when rows were written. A `degraded` run exits 3. `failed` stays 1 and a misconfigured delivery exits 2. `guardian watchdog` calls the same delivery path as `run-once`. With the default `outbox_only` that leaves rows pending. A configured sender can send them, including when `run-once` itself is not running. `watchdog --no-send` does not call the sender. `install-service` prints `watchdog --no-send` whenever the paired `run-once` line uses `--no-send`, and the shadow plan does the same. Default delivery mode is `outbox_only`. An opt-in Telegram sender exists in the package and stays off until you configure it; its canary is `NOT_RUN`. See [notifications.md](notifications.md).

A required capability that is already `error` or `blocked` stays a known gap. Watchdog emits `monitor_blind` when that set first gets worse, and `monitor_new_gap` if a further required key gets worse during the episode. The same known gap does not start a new episode. `monitor_recovered` is emitted when the episode's gaps are no longer worse.

An empty outbox is not all clear. It means no new alert row was committed. Coverage can still be `partial`, `credential_missing`, `unknown`, or `error`. A movement under the threshold does not become a row. A repeated event is deduped. Unread history is still unread.

## State files

| Path | Contents |
| --- | --- |
| Config directory | The five init files above, plus `.env.local` if you put one there |
| State directory `monitor.sqlite` | Coverage, cursors, alerts, outbox, runs. Created by `run-once`, not by `init`. Sidecars `-wal` and `-shm` appear while the database is open |
| State directory | `guardian doctor` also writes `environment_report.json`, `source_registry.json`, and `provider_benchmark.json` here. The default doctor is offline; `--live` is the bounded probe |
| `shadow-slots.json` | Slot ledger (`guardian.shadow-slots.v1`) when the slot recorder runs. See [scheduling.md](scheduling.md) |

Defaults: config `~/.config/guardian`, state `~/.local/state/guardian`. Overrides: `GUARDIAN_CONFIG_DIR`, `GUARDIAN_STATE_DIR`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`. On POSIX, private directories are mode `0700` and new private files are mode `0600`. Windows does not enforce those bits.

The legacy commands (`doctor`, `run-once --config`, `snapshot`) use the state directory as well. `--db` on those commands is an explicit sqlite path and is not rewritten.

## Upgrade

Stop the monitor so it is not holding the database. Copy the state directory, including `-wal` and `-shm` if they exist. There is no automatic backup. Install the new wheel with that release's `constraints.txt`. Database migrations move forward only. If you reinstall an older wheel after a newer migration, the old build refuses the file with `database schema is newer than this build` and does not delete it. Restore the backup you took. Full steps are in [install.md](install.md).

## Uninstall

```bash
uv tool uninstall wallet-guardian-monitor
```

If you used a virtualenv, delete that environment. Uninstall does not delete the XDG config or state directories. Delete `~/.config/guardian` and `~/.local/state/guardian` (or the directories you overrode) only when you mean to remove the addresses, the secret profile, and the outbox.

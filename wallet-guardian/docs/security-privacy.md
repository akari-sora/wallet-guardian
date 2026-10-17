# Security and privacy

The monitor is a read-only program for one operator on a machine they control. Package version in this tree: `0.4.0rc4`. It does not sign, build a transaction, or broadcast. It does not ask for a seed phrase, a private key, or a keystore, and it has no path that would send one to a wallet. A tenant id is a label, not a boundary between customers. This page is not an audit. The 72h shadow validation is not complete.

The longer model is [security-model.md](security-model.md). Assets, actors, and trust boundaries are in [threat-model.md](threat-model.md). Tests that lock the read-only rules live under `tests/`, including `tests/contracts/sources/test_mcm_w2_01_no_chain_writes.py` and the offline suites named in the README. Those tests do not call the network. A green local run is not a CI pass on the preview commits.

## Keys and seeds

Public config names environment variables (`endpoint_env`, `credential_env`). It does not contain key values, keyed RPC URLs, or watch addresses. `guardian init` writes `secret-profile.json` with those names and empty strings. It does not copy an environment value into the file. You put values in that file later, or you export them. A blank profile value can use a key inherited from the process environment. An unattended service should start from a deliberately scoped environment, not from a shell that already holds keys. Doctor reports `configured` or `not_configured` and does not print the value. Logs go through redaction. Do not point an endpoint variable at a URL that already contains a key. Diagnostics do not echo an invalid resolver mode or a network name that is a URL or an address.

Do not commit `private/`, `data/`, `evidence/`, a secret profile, or `.env.local`.

## What leaves the machine

Init prints a data-sharing summary before it writes. The machine hostname is not added to requests. The provider still sees the connection's IP address. That summary is not a complete match for this table: a keyless Esplora source is summarized as not queried, with `sends_addresses` false, while the Bitcoin read still sends the address when the endpoint is set. Rows marked **init** are sources `guardian init` writes. Rows marked **example** are in `examples/monitor.json` and are not added by init.

| Source class | What can leave | When it does not |
| --- | --- | --- |
| EVM or Solana RPC (**init**) | The watch address, inside balance, log, and call reads, to the host named by the endpoint variable | The endpoint variable is unset, so there is no host to call |
| Keyed indexers (Etherscan, Blockscout, NodeReal) (**init**) | The watch address, only when that source is called. Etherscan sends `apikey` in the query when the key is set. A missing key skips the call, and that skipped call does not send a key. Blockscout sends the key as a bearer header, not in the URL | The key is missing. The call is skipped and coverage stays `credential_missing` |
| Helius (**init** source, not called) | Not called by `run-once` in this preview. Init can still list `HELIUS_API_KEY` and enable `helius-parser` for a Solana account. The watch address is not sent to Helius | The adapter is not called, with or without the key |
| Esplora (**init**) | The watch address on `/address/{address}`, `/address/{address}/txs/chain`, and `/address/{address}/txs/mempool`. Init names the variable `BITCOIN_MAINNET_URL`. The init summary currently says this address is not sent | The endpoint variable is unset, or no Bitcoin account was selected |
| Keyless Blockscout and `ESPLORA_API_URL` (**example**) | The watch address, on the keyless Blockscout instance or the example Esplora base in `examples/monitor.json`. Init does not write a keyless Blockscout source and does not use the name `ESPLORA_API_URL` | That example source is absent, or its endpoint variable is unset |
| Blockscout PRO (**example**) | The watch address, to the host in `BLOCKSCOUT_PRO_API_BASE`, even when `BLOCKSCOUT_API_KEY` is unset. `examples/monitor.json` enables `blockscout-pro`. On `history.native_external` the address is a query parameter on `/v2/api`. If an internal-history route reaches this source, the address is in `/api/v2/addresses/{address}/…`. The call still happens with no key. HTTP 402 is `credential_missing`, unless the body says the chain is outside the plan, which is `plan_not_supported`. On `history.native_external`, the example then continues to the next source. A key, when set, is a bearer header, not a query value. Init does not add this source | `BLOCKSCOUT_PRO_API_BASE` is unset, or the profile has no `blockscout-pro` source |
| TronGrid (**init**, when a TRON account was selected) | The watch address, to the host in `TRONGRID_API_URL`. `TRON_PRO_API_KEY` is optional. A missing key does not skip the read | `TRONGRID_API_URL` is unset, or no TRON account is configured |
| Tenderly Base gateway (**example**) | The watch address inside Base `eth_getLogs` for `history.token` and `permissions.standard`, to the host in `TENDERLY_BASE_URL`. `examples/monitor.json` enables `tenderly_base`. `TENDERLY_ACCESS_KEY` is not this path and is not called. Init does not add `tenderly_base` | The URL is unset, or the profile is not that example route |
| Alchemy Transfers (**example**) | The watch address as `fromAddress` or `toAddress` on `alchemy_getAssetTransfers`, to the host in `ALCHEMY_TRANSFERS_URL`. `examples/monitor.json` enables `alchemy-base`. Init does not add it | The key or URL is unset |
| Prices (CoinGecko) | Not added by `init`. When a CoinGecko source is enabled, `run-once` requests native-coin ids (`/simple/price`) and token contracts (`/simple/token_price/{platform}`, `contract_addresses`) for transfers in that round. If `COINGECKO_API_KEY` is set, the value goes in the query parameter `x_cg_demo_api_key`. The watch address is not in that request. A missing quote is `valuation_unknown` and is not stored as 0 | The price source is disabled, which is the init default. Pinned stablecoins and `token_raw_thresholds` can still emit `unvalued_large_move` without a price request |
| News feeds | Not fetched unless the monitor file sets `news.fetch` to true. An allowlisted GET uses the feed URL only. It does not put the watch address in the URL and it does not take an API key. `--news-input` reads a local file | The default init file does not set `news.fetch` |
| OFAC (**example** source; init does not add it) | Screening uses the first source whose `adapter` is `ofac_sls`. `enabled: false` does not stop the fetch while the endpoint is set. The endpoint variable is whatever `endpoint_env` names, for example `OFAC_SLS_FEED` in `examples/monitor.json`. That read matches locally. No address is uploaded. A stale or unreadable list is reported as such, and is not a "no hit" | No address is uploaded. Without an `ofac_sls` source, or with the endpoint empty, the list host is not called. Setting `OFAC_SLS_FEED` alone on an init profile stays `feed_not_supplied` |
| GoPlus and Chainabuse | Not called by `run-once` in this preview, with or without a key, a base URL, or consent. Init lists both as off. The watch address is not sent. The `address_risk` row stays a blocked gap | No address-risk screening is performed |
| Notifications | Nothing, while delivery mode is `outbox_only`. Rows stay in the local outbox. `install-service` does not start a sender | `--no-send` also refuses to hand rows to a sender |
| `guardian relations` | Manual. The disclosure plan is printed first, and `--yes` is required before a request is sent. Without a NodeReal key the plan may name `rpc-bsc.48.club`. A keyless Optimism read may name `explorer.optimism.io`. See [relations.md](relations.md) | Without `--yes`, or with `--dry-run`, nothing is sent |

## Opt-in third parties

GoPlus and Chainabuse are off by default. `guardian init` puts both in `not_auto_enabled`. `privacy.third_party_wallet_queries` is `deny_until_local_consent`. `operator_consent.third_party_user_address_lookups` is `off`. `run-once` still does not call either one. While privacy is not `operator_approved_sources_only`, the GoPlus row includes `third_party_denied`. When that privacy value is `operator_approved_sources_only`, the row includes `not_run`. A missing `GOPLUS_API_KEY` or base still adds `credential_missing`. The address is not sent. That row is not a clean bill of health.

`examples/monitor.json` includes a GoPlus source with `enabled` true and the same privacy deny. The example does not turn the lookup on. Chainabuse is not a source in that file. The capability file exists. The example monitor does not route it.

A key, a base URL, consent, or an edit that enables the source does not make `run-once` call GoPlus or Chainabuse in this preview. Init will not enable them.

## Local files and permissions

Private directories are mode `0700` and new private files are mode `0600` on POSIX. Windows does not enforce those bits. Defaults are `~/.config/guardian` and `~/.local/state/guardian`, overridable with `GUARDIAN_CONFIG_DIR`, `GUARDIAN_STATE_DIR`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME`. See [install.md](install.md) and [guide-cli.md](guide-cli.md).

Anyone who can read the state directory can read cursors, coverage, and unsent alerts. Anyone who can write it can change what you think the chain said. `--force` on init backs up config files and leaves `monitor.sqlite` untouched. Config backups can contain watch addresses. Treat them as private.

`guardian doctor` writes `environment_report.json`, `source_registry.json`, and `provider_benchmark.json` under the state directory. The default report is offline. `--live` is a provider probe with a 5 second whole-call deadline per probe. That bound is remaining time, and an in-flight client is closed when it expires. Unusable resolver settings are reported and not probed. Loaded settings pin the connection to the validated address and keep the original hostname for Host, SNI, and the certificate check. With `--mcm-config` that probe is the legacy provider check and does not prove the selected profile's connectivity. `providers verify` is the same labelled legacy probe. Neither report prints key values, and DoH enabled is not a qualified path.

## How to verify

- [security-model.md](security-model.md) for what init writes and what it refuses.
- [threat-model.md](threat-model.md) for assets, actors, SSRF limits, and the supply chain.
- [coverage-matrix.md](coverage-matrix.md) for which reads are partial, blocked, or not verified. Do not promote a gap.
- `tests/contracts/sources/test_mcm_w2_01_no_chain_writes.py` refuses chain writes.
- `tests/unit/test_cli_security.py` checks the CLI error path stays off send.
- The offline pytest suite is the local gate. It is not a CI pass, and it is not a substitute for the 72h shadow, which is not complete.

Install a wheel whose digest you checked, with that release's constraints. The check is yours to perform. Init cannot undo a wheel that was already replaced.

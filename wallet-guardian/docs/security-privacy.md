# Security and privacy

The monitor is a read-only program for one operator on a machine they control. Package version in this tree: `0.4.0rc2`. It does not sign, build a transaction, or broadcast. It does not ask for a seed phrase, a private key, or a keystore, and it has no path that would send one to a wallet. A tenant id is a label, not a boundary between customers. This page is not an audit. The 72h shadow validation is not complete.

The longer model is [security-model.md](security-model.md). Assets, actors, and trust boundaries are in [threat-model.md](threat-model.md). Tests that lock the read-only rules live under `tests/`, including `tests/contracts/sources/test_mcm_w2_01_no_chain_writes.py` and the offline suites named in the README. Those tests do not call the network. A green local run is not a CI pass on the preview commits.

## Keys and seeds

Public config names environment variables (`endpoint_env`, `credential_env`). It does not contain key values, keyed RPC URLs, or watch addresses. `guardian init` writes `secret-profile.json` with those names and empty strings. It does not copy an environment value into the file. You put values in that file later, or you export them. Doctor reports `configured` or `not_configured` and does not print the value. Logs go through redaction. Do not point an endpoint variable at a URL that already contains a key.

Do not commit `private/`, `data/`, `evidence/`, a secret profile, or `.env.local`.

## What leaves the machine

Init prints a data-sharing summary before it writes. The machine hostname is not added to requests. The provider still sees the connection's IP address. With the profile `init` writes:

| Source class | What can leave | When it does not |
| --- | --- | --- |
| EVM or Solana RPC | The watch address, inside balance, log, and call reads, to the host named by the endpoint variable | The endpoint variable is unset, so there is no host to call |
| Keyed indexers (Etherscan, Blockscout, NodeReal, Helius) | The watch address, only when that source is called. Etherscan sends `apikey` in the query. Helius sends `api-key` in the query. Blockscout sends the key as a bearer header, not in the URL | The key is missing. The call is skipped and coverage stays `credential_missing` |
| Keyless HTTP (Esplora, keyless Blockscout) | The watch address on the request to the base URL you set | The endpoint variable is unset, or that family was not selected |
| Prices (CoinGecko) | Not added by `init`. When a price source is enabled, the request quotes a catalog asset id (`ids`) or a token contract (`contract_addresses`). If `COINGECKO_API_KEY` is set, the value goes in the query parameter `x_cg_demo_api_key`. The watch address is not in that request. A missing quote is `valuation_unknown` | The price source is disabled, which is the init default |
| News feeds | Not fetched unless the monitor file sets `news.fetch` to true. An allowlisted GET uses the feed URL only. It does not put the watch address in the URL and it does not take an API key. `--news-input` reads a local file | The default init file does not set `news.fetch` |
| OFAC | Init stores the list name `us-ofac-sdn` and does not add an OFAC source. If you set `OFAC_SLS_FEED` (a local file or an HTTPS URL), `run-once` downloads or reads that list and matches addresses locally; no address is uploaded. An unset endpoint is `feed_not_supplied`; a stale or unreadable list is reported as such, not as "no hit" | No address is uploaded by init or by screening |
| GoPlus and Chainabuse | Not enabled by init. The summary lists both as off. The watch address is not sent | Stays off until you change the config yourself |
| Notifications | Nothing, while delivery mode is `outbox_only`. Rows stay in the local outbox. `install-service` does not start a sender | `--no-send` also refuses to hand rows to a sender |

## Opt-in third parties

GoPlus and Chainabuse are off by default. `guardian init` puts both in `not_auto_enabled`. `privacy.third_party_wallet_queries` is `deny_until_local_consent`. `operator_consent.third_party_user_address_lookups` is `off`. While privacy is not `operator_approved_sources_only`, a GoPlus row is `third_party_denied` and the address is not sent. A missing `GOPLUS_API_KEY` is still `credential_missing`. That is not a clean bill of health.

`examples/monitor.json` includes a GoPlus source with `enabled` true and the same privacy deny. The example does not turn the lookup on. Chainabuse is not a source in that file. The capability file exists. The example monitor does not route it.

Turning either lookup on is a separate edit you make yourself. Init will not do it.

## Local files and permissions

Private directories are mode `0700` and new private files are mode `0600` on POSIX. Windows does not enforce those bits. Defaults are `~/.config/guardian` and `~/.local/state/guardian`, overridable with `GUARDIAN_CONFIG_DIR`, `GUARDIAN_STATE_DIR`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME`. See [install.md](install.md) and [guide-cli.md](guide-cli.md).

Anyone who can read the state directory can read cursors, coverage, and unsent alerts. Anyone who can write it can change what you think the chain said. `--force` on init backs up config files and leaves `monitor.sqlite` untouched. Config backups can contain watch addresses. Treat them as private.

`guardian doctor` writes `environment_report.json`, `source_registry.json`, and `provider_benchmark.json` under the state directory. It can contact configured read endpoints. It does not print key values.

## How to verify

- [security-model.md](security-model.md) for what init writes and what it refuses.
- [threat-model.md](threat-model.md) for assets, actors, SSRF limits, and the supply chain.
- [coverage-matrix.md](coverage-matrix.md) for which reads are partial, blocked, or not verified. Do not promote a gap.
- `tests/contracts/sources/test_mcm_w2_01_no_chain_writes.py` refuses chain writes.
- `tests/unit/test_cli_security.py` checks the CLI error path stays off send.
- The offline pytest suite is the local gate. It is not a CI pass, and it is not a substitute for the 72h shadow, which is not complete.

Install a wheel whose digest you checked, with that release's constraints. The check is yours to perform. Init cannot undo a wheel that was already replaced.

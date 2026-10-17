# Security model

Wallet Guardian's monitor is a read-only program for one operator on a machine they control. The installable distribution is `wallet-guardian-monitor` (wheel `wallet_guardian_monitor-0.4.0rc4-py3-none-any.whl`). It is not a hosted service and it does not isolate mutually untrusted customers. A `tenant_id` in the accounts file is a label for that operator's data. It is not a security boundary between customers. Whether git tag `v0.4.0-rc.4` is published, see GitLab releases.

授權：AGPL-3.0-only（見 `LICENSE`）。

## What the monitor is

The monitor reads public chain data for addresses the operator typed, stores the result locally, and can leave alerts in an outbox. It does not sign, does not build a transaction, and does not broadcast. It does not ask for a seed phrase, a private key, or a keystore, and it has no path that would send one to a wallet.

The Action Client (wallet login, WalletConnect, Pages Functions, D1) is a separate optional subsystem. See [docs/threat-model.md](threat-model.md#monitor-does-not-sign). A new user does not need the author's Grok session, Cloudflare, D1, a wallet login, or Node to run the monitor.

## Keys stay local

Public config names environment variables (`endpoint_env`, `credential_env`, and the `private_inputs` names). It does not contain key values, keyed RPC URLs, or watch addresses.

`guardian init` writes a secret profile whose keys are those env var names and whose values are empty strings. The process reads key values from the environment or from that profile later, when the operator has put them there. Init itself never copies an environment value into a file. A no-key setup leaves the credential variables unset. Coverage then shows `credential_missing` and a hint that names the variable and `docs/api-keys.md`. A missing key is not a zero balance and not a complete history.

Private files are written mode `0600` and private directories mode `0700` on POSIX. Windows does not enforce those bits. The default locations are the XDG config and state directories (`GUARDIAN_CONFIG_DIR` / `GUARDIAN_STATE_DIR` override them). See `docs/install.md`.

## What leaves the machine

Init prints a data-sharing summary before it writes. Each selected source says whether a wallet address is sent, whether the machine hostname is sent (it is not), and which provider receives the call. That summary is not a complete match for the reads below. A keyless Esplora source (no `credential_env`, which is how init writes Bitcoin) is summarized as not queried, with `sends_addresses` false. `run-once` still sends the Bitcoin address when the endpoint is set. The operator confirms with `--yes` or by typing `yes`. Without that confirmation, no config is written.

With the defaults from init:

- The RPC host named by the operator receives the watch address in balance, log, and call reads once that endpoint variable is set.
- A keyed indexer (Etherscan, Blockscout, NodeReal) receives the address only when that source is called. A missing key skips the call (`credential_missing`); that skipped call does not send the key. When the key is set and the source is called, the key goes to that provider the way its API requires. Etherscan sends `apikey` in the query. Helius is not called by `run-once` in this preview. Init can still write `helius-parser` and list `HELIUS_API_KEY`.
- TronGrid receives the watch address when a TRON account is configured and `TRONGRID_API_URL` is set. `TRON_PRO_API_KEY` is optional. A missing key does not skip the read. The keyless call still sends the address. A set key goes out only as the `TRON-PRO-API-KEY` header.
- GoPlus and Chainabuse are not called by `run-once` in this preview, with or without a key, a base URL, or consent. The `address_risk` row is always a blocked gap (`credential_missing`, `third_party_denied`, or `not_run`). No address-risk screening is performed. `privacy.third_party_wallet_queries` stays `deny_until_local_consent`.
- Screening uses the first source whose `adapter` is `ofac_sls`. `run-once` does not read `enabled`. A source left at `enabled: false` is still fetched when its endpoint is set. The endpoint variable is whatever that source's `endpoint_env` names; `examples/monitor.json` uses `OFAC_SLS_FEED`. To avoid the fetch, remove the source or leave the endpoint empty. Init stores the list name `us-ofac-sdn` and does not add that source, so setting `OFAC_SLS_FEED` alone on an init profile stays `feed_not_supplied`. When the endpoint is set, it may name a local `file://` SDN XML file or an HTTPS URL. The monitor matches locally and does not upload a watch address. A list older than 7 days is partial (`feed_stale`). A malformed list is an error, and is not a miss. Init does not download a list by itself.
- Delivery mode is `outbox_only`. The Telegram sender exists and is opt-in; `--no-send` forbids sends. `single_sender_handoff` in the delivery profile is false. Slack, email, and webhook clients are not in this package.
- Init does not derive further addresses from a key, an xpub, or another chain's address. The accounts file contains only the addresses the operator entered.
- `install-service --dry-run`, and a run without `--apply`, prints planned unit names and exits non-zero. `install-service --apply` without `--output` writes files and then enables timers or installs a crontab. It runs `systemctl enable` (or `systemctl --user enable`) and does not pass `--now`, so this command does not start a timer. systemd system mode writes the six unit files under `/etc/systemd/system`. systemd user mode writes them under `/home/<user>/.config/systemd/user` (`--user` defaults to `guardian-monitor`). The three timers enabled are `guardian-mcm-shadow.timer`, `guardian-legacy-collector.timer`, and `guardian-watchdog.timer`. cron mode writes `guardian-unattended.cron` to `/etc/cron.d`, writes the current user crontab plus the managed block to `/etc/cron.d/guardian-unattended.cron.merged`, then runs `crontab -u <user>` on that merged file. Those writes do not set mode `0600`. The cron lines have no user field, so a cron that reads `/etc/cron.d` can parse them as a system crontab. The managed block in the user crontab is replaced; other lines of that crontab are kept. With `--output`, the rendered files go under that directory instead of the system paths; cron `--apply` still runs `crontab -u`. The generated `run-once` and `watchdog` lines include `--no-send`. `guardian-legacy-collector.service` and the second cron line run `--legacy-executable` (default `/usr/local/sbin/guardian-legacy-collector`) with no `--no-send`. That program is the operator's. `--schedule-mode launchd_agent` does not call `launchctl`. Launchd checks that each plist command includes `--no-send`. Init does not run `install-service`. Host smoke of an applied unit is `NOT_RUN`.

Some provider APIs put the credential in the query string (Etherscan's `apikey`, Helius's `api-key`). Blockscout sends its key as a bearer header, not in the URL. Those values still never belong in the public config. Logs and errors go through redaction. Do not point an endpoint variable at a URL that already contains a key.

The provider sees the connection's IP address. The monitor does not add the operator's hostname to the request.

## Consent record

A written monitor file includes `operator_consent`: acknowledgement, an ISO-8601 timestamp, `third_party_user_address_lookups` set to `off`, and `key_mode` of `none` or `byo`. That record is how this operator agreed to the summary. It is not a contract with a provider and it does not turn third-party address lookups on.

Running init again refuses to overwrite. `--force` copies the existing config files to a sibling backup and then writes new files. It does not open, copy, or replace `monitor.sqlite`.

## What this model does not claim

A watch address is not proof of control. Unknown prices stay unknown, not zero. A failed or blocked read stays blocked or unknown, not a complete empty wallet. Matching an OFAC entry, or failing to match one, is not a compliance determination. The in-process plugin registry is not a sandbox for third-party code. This build does not accept remote plugins.

See `docs/threat-model.md` for assets, actors, and the trust boundaries around those flows.

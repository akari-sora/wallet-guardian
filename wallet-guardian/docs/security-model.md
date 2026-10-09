# Security model

Wallet Guardian's monitor is a read-only program for one operator on a machine they control. The installable distribution is `wallet-guardian-monitor` (wheel `wallet_guardian_monitor-0.4.0rc1-py3-none-any.whl`). It is not a hosted service and it does not isolate mutually untrusted customers. A `tenant_id` in the accounts file is a label for that operator's data. It is not a security boundary between customers. Whether git tag `v0.4.0-rc.1` is published, see GitLab releases.

授權：AGPL-3.0-only（見 `LICENSE`）。

## What the monitor is

The monitor reads public chain data for addresses the operator typed, stores the result locally, and can leave alerts in an outbox. It does not sign, does not build a transaction, and does not broadcast. It does not ask for a seed phrase, a private key, or a keystore, and it has no path that would send one to a wallet.

The Action Client (wallet login, WalletConnect, Pages Functions, D1) is a separate optional subsystem. See [docs/threat-model.md](threat-model.md#monitor-does-not-sign). A new user does not need the author's Grok session, Cloudflare, D1, a wallet login, or Node to run the monitor.

## Keys stay local

Public config names environment variables (`endpoint_env`, `credential_env`, and the `private_inputs` names). It does not contain key values, keyed RPC URLs, or watch addresses.

`guardian init` writes a secret profile whose keys are those env var names and whose values are empty strings. The process reads key values from the environment or from that profile later, when the operator has put them there. Init itself never copies an environment value into a file. A no-key setup leaves the credential variables unset. Coverage then shows `credential_missing` and a hint that names the variable and `docs/api-keys.md`. A missing key is not a zero balance and not a complete history.

Private files are written mode `0600` and private directories mode `0700` on POSIX. Windows does not enforce those bits. The default locations are the XDG config and state directories (`GUARDIAN_CONFIG_DIR` / `GUARDIAN_STATE_DIR` override them). See `docs/install.md`.

## What leaves the machine

Init prints a data-sharing summary before it writes. Each selected source says whether a wallet address is sent, whether the machine hostname is sent (it is not), and which provider receives the call. The operator confirms with `--yes` or by typing `yes`. Without that confirmation, no config is written.

With the defaults from init:

- The RPC host named by the operator receives the watch address in balance, log, and call reads once that endpoint variable is set.
- A keyed indexer (Etherscan, Blockscout, NodeReal, Helius, TronGrid) receives the address only when that source is actually called. A missing key is not sent.
- GoPlus and Chainabuse user-address lookups are not called unless the operator sets the key and the base. Without `GOPLUS_API_KEY` or `GOPLUS_API_BASE`, the row is `credential_missing` and no request is made. `privacy.third_party_wallet_queries` stays `deny_until_local_consent`.
- `OFAC_SLS_FEED` may name a local `file://` SDN XML file or an HTTPS URL. The monitor matches locally and does not upload a watch address. Unset coverage is `feed_not_supplied`. A list older than 7 days is partial (`feed_stale`). A malformed list is an error, not a miss. Init does not download a list by itself.
- Delivery mode is `outbox_only`. The Telegram sender exists and is opt-in; `--no-send` forbids sends. `single_sender_handoff` in the delivery profile is false. Slack, email, and webhook clients are not in this package.
- Init does not derive further addresses from a key, an xpub, or another chain's address. The accounts file contains only the addresses the operator entered.
- `install-service` prints planned unit names and exits non-zero. It does not install a service. The operator enables scheduling later, on purpose.

Some provider APIs put the credential in the query string (Etherscan's `apikey`, Helius's `api-key`). Blockscout sends its key as a bearer header, not in the URL. Those values still never belong in the public config. Logs and errors go through redaction. Do not point an endpoint variable at a URL that already contains a key.

The provider sees the connection's IP address. The monitor does not add the operator's hostname to the request.

## Consent record

A written monitor file includes `operator_consent`: acknowledgement, an ISO-8601 timestamp, `third_party_user_address_lookups` set to `off`, and `key_mode` of `none` or `byo`. That record is how this operator agreed to the summary. It is not a contract with a provider and it does not turn third-party address lookups on.

Running init again refuses to overwrite. `--force` copies the existing config files to a sibling backup and then writes new files. It does not open, copy, or replace `monitor.sqlite`.

## What this model does not claim

A watch address is not proof of control. Unknown prices stay unknown, not zero. A failed or blocked read stays blocked or unknown, not a complete empty wallet. Matching an OFAC entry, or failing to match one, is not a compliance determination. The in-process plugin registry is not a sandbox for third-party code. This build does not accept remote plugins.

See `docs/threat-model.md` for assets, actors, and the trust boundaries around those flows.

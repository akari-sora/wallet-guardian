# Threat model

This note is the threat model for the read-only monitor as a single-operator, self-hosted program. It is not a pentest report and it is not a promise that every provider is honest. 授權：AGPL-3.0-only（見 `LICENSE`）。

The installable distribution is `wallet-guardian-monitor`. The import package and the CLI stay `guardian`. The wheel file name is `wallet_guardian_monitor-0.4.0rc4-py3-none-any.whl`. Whether git tag `v0.4.0-rc.4` is published, see GitLab releases.

<a id="monitor-does-not-sign"></a>

The monitor never signs, never builds a transaction, and never broadcasts. The optional Action Client can make a SIWE login signature. That signature is a login, not a transaction signature, and it is not part of the monitor. This is the only place that difference is stated. WalletConnect, Pages Functions, and D1 are the same optional subsystem and are not required to run the monitor.

## Assets

- Watch addresses and labels in the private accounts file. These identify what the operator cares about. They are not keys.
- Endpoint URLs and API keys, which live in the environment or in the secret profile after the operator puts them there. Init writes only the variable names.
- Local state: `monitor.sqlite` (snapshots, cursors, outbox, coverage), plus any evidence or doctor output under the state directory.
- The data-sharing consent record and the choice of chains, plugins, and sources.
- The operator's ability to keep the monitor read-only: no signing key and no broadcast path. The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`). `--no-send` forbids sends.

## Actors

- The operator, who chooses addresses and env var names and confirms the summary.
- Other local users and backup tools on the same machine, who can read files the mode bits allow.
- Chain RPC operators and indexers `run-once` actually calls: public or self-hosted EVM RPC, Solana RPC, Etherscan, Blockscout, NodeReal, Esplora, and TronGrid. With `examples/monitor.json`, Alchemy Transfers (`alchemy-base`, `alchemy_getAssetTransfers`) and the Tenderly Base gateway (`tenderly_base`, `TENDERLY_BASE_URL`) also receive the watch address. Helius is not called in this preview, even when init writes an enabled `helius-parser`. Init can also write TON Center, Sui, Aptos, and Cosmos REST sources. `run-once` does not query those four. It records `unsupported_family`.
- Risk and list publishers that stay uncalled in this preview: GoPlus and Chainabuse. The OFAC list host is queried only when the monitor file contains an `ofac_sls` source. Init does not add that source.
- A notification recipient. The Telegram sender exists and is opt-in. Delivery defaults to off (`outbox_only`), and `--no-send` forbids sends. Slack, email, and webhook clients are not in this package. The outbox stays local while delivery mode is `outbox_only`.
- The supply chain: the Python package, its pinned dependencies, and whatever host serves the wheel. Tests do not call the network.
- The author of a plugin that already ships in this package. Plugins are local code selected by id. Init does not download or execute a plugin from the network.

## Trust boundaries

1. The operator's account versus other accounts on the machine. POSIX `0700` directories and `0600` files are the control. They are not a control on Windows.
2. The git checkout versus the private directory. Public config must not contain addresses, keys, or keyed URLs. Private paths must sit outside the work tree when the program is running from a checkout.
3. The monitor process versus the network. Only configured read endpoints are in scope. Signing and broadcast methods are refused before a socket is opened.
4. The monitor versus the Action Client. A task file, a page, or a wallet button is not authorization for the monitor to send.
5. Fixture tests versus a live run. Tests inject a fake transport. An unmatched fixture fails. It does not fall through to the public internet.

## Data flows

Init prints a data-sharing summary before it writes. That summary is not a complete match for the table below. A keyless Esplora source has no `credential_env` (that is how init writes Bitcoin). The summary then says the family is not queried and sets `sends_addresses` false. `run-once` still sends the Bitcoin address when the endpoint is set. The table is the read behavior.

| Source | What can leave | When it does not leave |
|---|---|---|
| EVM or Solana RPC named by the operator | Watch address inside balance, log, and call reads. Not the machine hostname. | The endpoint variable is unset, so there is no host to call. |
| Etherscan | Watch address and, by that API's design, the key as the `apikey` query parameter. | The key is missing. The call is skipped and coverage is `credential_missing`. |
| Blockscout | Watch address. The key, when present, is a bearer header, not a query secret. | The key is missing. Init does not use the Blockscout PRO key name that would probe once without a key. |
| NodeReal | Watch address, when that source is called. | The key is missing, so the call is not made. |
| Helius | Nothing from `run-once` in this preview. Init can write an enabled `helius-parser` source and list `HELIUS_API_KEY`. The adapter is not called, with or without the key. | The call is not made. |
| Esplora (Bitcoin) | The watch address, on `/address/{address}`, `/address/{address}/txs/chain`, and `/address/{address}/txs/mempool`, to the base URL in that source's endpoint variable. Init writes `BITCOIN_MAINNET_URL` for `bitcoin-mainnet`. `examples/monitor.json` uses `ESPLORA_API_URL`. The init summary currently says this address is not sent. | The endpoint variable is unset, or no Bitcoin account is configured. |
| Tenderly Base gateway (example file) | The watch address inside Base `eth_getLogs` for `history.token` and `permissions.standard`, to the host in `TENDERLY_BASE_URL`. `examples/monitor.json` enables `tenderly_base` (`adapter` `evm_rpc`) on those routes. Init does not add it. `TENDERLY_ACCESS_KEY` is not used by this path and is not called. | The URL is unset, the source is disabled, or the profile is not that example route. |
| Alchemy Transfers (example file) | The watch address as `fromAddress` or `toAddress` on `alchemy_getAssetTransfers`, to the host in `ALCHEMY_TRANSFERS_URL`. `examples/monitor.json` enables `alchemy-base`. Init does not add it. | The key or URL is unset, so the call is not made. |
| TON Center, Sui, Aptos, Cosmos REST | Nothing in this build. Init can write a source for these families. `run-once` marks them `unsupported_family` and does not query them. | Coverage stays unsupported. A missing row is not a zero balance. |
| TronGrid (opt-in, experimental) | The watch address, to the host in `TRONGRID_API_URL`, when a TRON account is configured. `TRON_PRO_API_KEY` is optional. Without it the read is keyless and still sends the address. A set key goes out only as the `TRON-PRO-API-KEY` header. | `TRONGRID_API_URL` is unset, or no TRON account is configured. |
| GoPlus, Chainabuse | Nothing in this preview. `run-once` does not call them, with or without a key, a base URL, or consent. The `address_risk` row stays a blocked gap (`credential_missing`, `third_party_denied`, or `not_run`). | No address-risk screening is performed. |
| OFAC list host | A local match when the monitor file has a source with `"adapter": "ofac_sls"` and that source's endpoint is set. `run-once` takes the first such source and does not read `enabled`. `enabled: false` still fetches the list. The endpoint variable is whatever that source's `endpoint_env` names; `examples/monitor.json` uses `OFAC_SLS_FEED`, which may be a local `file://` SDN XML or an HTTPS URL. Init stores the list name `us-ofac-sdn` and does not add that source, so the variable alone on an init profile stays `feed_not_supplied`. | No address is uploaded. To avoid the fetch, remove the source or leave the endpoint empty. A missing source, or an empty endpoint, stays `feed_not_supplied`. A stale or malformed list is not a clear screen. |
| Notification channel | The Telegram sender exists and is opt-in. `--no-send` forbids sends. | Mode defaults to `outbox_only`. `install-service` does not start a sender. |

The provider on the other end of an enabled call learns the watch address, the query shape, the client IP, and the time. That is the disclosure the summary asks the operator to accept.

## SSRF and egress

Endpoints come from env var values, not from the public file. The read path refuses loopback, link-local, private, multicast, and cloud metadata names. A redirect must stay on the same host. Credentials are not taken from the public document. A URL with userinfo is refused.

This stops the monitor from being pointed at the cloud metadata service or an internal admin port by a poisoned config value. It does not make a public RPC honest, and it does not hide the client IP from a provider the operator chose. Egress is "the hosts the operator named," not an open proxy.

Write, sign, and broadcast operations are denied in the transport policy before a request is built. A read-only JSON-RPC POST is still a read. The HTTP method is not the control; the operation is.

## Supply chain

Install a wheel whose digest you checked, with the pinned constraints from that same release (`docs/install.md`). `uv tool install` does not apply `uv.lock` unless you pass those constraints. Do not `pip install` an unrelated project named `guardian` from PyPI.

This package does not add a dependency for onboarding. Schema checks use the small local validator already in the tree. Plugins are the ids compiled into the package. There is no entry point that loads a third-party plugin from disk or from a URL.

A compromised dependency or a tampered wheel can ignore every control above. Verifying the artifact is the operator's check, not something init can do after the process is already the attacker's.

## Local state

State lives under the XDG state directory or the `state` folder inside an explicit `--dir`. The database is created by a later `run-once`, not by init. Init's `--force` backs up config files and leaves `monitor.sqlite` byte-for-byte as it was. Backups of the config are still private files; they can contain watch addresses. Protect them the same way as the accounts file.

Anyone who can read the state directory can read cursors, coverage, and unsent alerts. Anyone who can write it can change what the operator thinks the chain said. File modes reduce that to the operator's user on POSIX. Disk encryption and backups are the operator's problem. The repo must not contain `private/`, `data/`, or `evidence/`.

## Notification channel risks

`outbox_only` means a row can sit on disk and is not handed to a network sender. The Telegram sender exists and is opt-in; delivery defaults to off, and `--no-send` forbids sends. Slack, email, and webhook clients are not in this package. A turned-on sender would disclose alert contents (and possibly addresses) to whatever channel the operator configures. That channel can be logged, forwarded, or phished. `install-service` will not enable a sender. Flipping `delivery.mode` to `configured_sender` without opting in is a misconfiguration, not an install step.

A webhook URL, if one is ever configured, is an egress decision of its own and has to survive the same SSRF checks as any other URL. Init does not write a webhook.

## Out of scope

- Signing, approving, revoking, claiming, or broadcasting.
- Hosted multi-tenant isolation. One operator, one machine.
- Deriving addresses from a seed, an xpub, or another chain.
- Turning on GoPlus or Chainabuse by default.
- Treating a printed plan as an installed service. `install-service --dry-run`, and a run without `--apply`, prints planned unit names and exits non-zero. On a systemd or cron plan, `install-service --apply` without `--dry-run` installs whether or not `--output` is set, and a successful install reports `installed: true`. `--output` only changes where the rendered files are written. It does not turn `--apply` into a preview. systemd still runs `daemon-reload` and `systemctl enable` (or `systemctl --user enable`) on `guardian-mcm-shadow.timer`, `guardian-legacy-collector.timer`, and `guardian-watchdog.timer`, and does not pass `--now`, so this command does not start a timer. Those `enable` commands name the timers; they do not point at the `--output` directory. cron still runs `crontab -u <user>` on the merged file. Without `--output`, systemd system mode writes the six unit files under `/etc/systemd/system`. systemd user mode writes them under `/home/<user>/.config/systemd/user` (`--user` defaults to `guardian-monitor`). cron mode writes `guardian-unattended.cron` to `/etc/cron.d` and the current user crontab plus the managed block to `/etc/cron.d/guardian-unattended.cron.merged`, then runs `crontab -u`. With `--output`, those files are written under that directory instead, and `crontab -u` installs the merged file from there. The program does not force mode `0600` on those writes. The cron lines have no user field, so a cron that reads `/etc/cron.d` can parse them as a system crontab. The managed block in the user crontab is replaced; other lines of that crontab are kept. The generated `run-once` and `watchdog` lines include `--no-send`. `guardian-legacy-collector.service` and the second cron line run `--legacy-executable` (default `/usr/local/sbin/guardian-legacy-collector`) with no `--no-send`. That program is the operator's. `--schedule-mode launchd_agent` prints LaunchAgents, checks that those plist commands include `--no-send`, and does not call `launchctl`, including when `--apply` is set. Init does not run `install-service`. Host smoke of an applied unit is `NOT_RUN`.
- The Action Client's wallet session, D1, or Pages deployment.
- Claiming that a clear OFAC check, a missing price, or an empty log scan means the wallet is safe or complete.
- Live network tests. Acceptance tests use a fake transport. A real socket in those tests is a failure.

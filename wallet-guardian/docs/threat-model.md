# Threat model

This note is the threat model for the read-only monitor as a single-operator, self-hosted program. It is not a pentest report and it is not a promise that every provider is honest. 授權：AGPL-3.0-only（見 `LICENSE`）。

The installable distribution is `wallet-guardian-monitor`. The import package and the CLI stay `guardian`. The wheel file name is `wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl`. Whether git tag `v0.4.0-rc.3` is published, see GitLab releases.

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
- Chain RPC operators and indexers the operator enables: public or self-hosted EVM RPC, Solana RPC, Etherscan, Blockscout, NodeReal, Helius, Esplora, TronGrid, TON Center, Sui, Aptos, and Cosmos REST hosts.
- Risk and list publishers that are off by default: GoPlus, Chainabuse, and the OFAC list host. A later opt-in is a separate decision.
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

Init's summary is the operator-facing list. The flows below match that summary.

| Source | What can leave | When it does not leave |
|---|---|---|
| EVM or Solana RPC named by the operator | Watch address inside balance, log, and call reads. Not the machine hostname. | The endpoint variable is unset, so there is no host to call. |
| Etherscan | Watch address and, by that API's design, the key as the `apikey` query parameter. | The key is missing. The call is skipped and coverage is `credential_missing`. |
| Blockscout | Watch address. The key, when present, is a bearer header, not a query secret. | The key is missing. Init does not use the Blockscout PRO key name that would probe once without a key. |
| NodeReal, Helius | Watch address. Helius puts `api-key` in the query when the call is made. | The key is missing, so the call is not made. |
| Esplora, TON Center, Sui, Aptos, Cosmos REST | Nothing in this build. Those families are not queried yet. | Coverage stays unsupported, not zero. |
| TronGrid | Nothing until a Tron adapter runs. A missing `TRON_PRO_API_KEY` would still block a later call. | Init does not send the address today. |
| GoPlus, Chainabuse | Without `GOPLUS_API_KEY` or `GOPLUS_API_BASE`, the row is `credential_missing` and no request is made. | Stays uncalled unless the operator sets the key, the base, and consent. |
| OFAC list host | `OFAC_SLS_FEED` may be a local `file://` SDN XML or an HTTPS URL. The match is local. | No address is uploaded. Unset stays `feed_not_supplied`. A stale or malformed list is not a clear screen. |
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
- Installing systemd units, cron, or a login item. `install-service` only prints a plan and exits non-zero.
- The Action Client's wallet session, D1, or Pages deployment.
- Claiming that a clear OFAC check, a missing price, or an empty log scan means the wallet is safe or complete.
- Live network tests. Acceptance tests use a fake transport. A real socket in those tests is a failure.

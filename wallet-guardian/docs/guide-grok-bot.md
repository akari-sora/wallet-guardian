# Using Wallet Guardian with Grok Bot

Two ways to use this preview: the `guardian` command by itself ([guide-cli.md](guide-cli.md)), or an assistant that runs that same command for you. This page is the second way. The package version in this tree is `0.4.0rc5`. The monitor is read-only. The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. The 72h shadow validation is not complete. There is no PyPI release.

The shareable Wallet Guardian bot template is coming. There is no template link.

Until that template exists, point Grok Bot at the `guardian` executable you installed and at this repository's docs, especially [cli-reference.md](cli-reference.md), [coverage-matrix.md](coverage-matrix.md), and [security-privacy.md](security-privacy.md). The bot should call the installed command. It should not reimplement the monitor, and it should not invent flags that `--help` does not list.

The agent skill is `skills/wallet-guardian/`. The single file for a Grok Bot upload is `skills/dist/grok-bot/SKILL.md` (the reference pages are inlined, so that file has no relative links). How each host is installed is in [guide-agents.md](guide-agents.md). Those host installs were not exercised (未實測). Prefer that skill file when the host accepts one. The paste block below is the older instruction text. 見 Classes. 見 Hard limits.

The rc.5 wheel on the release page includes `capabilities`. `v0.4.0-rc.4` and earlier wheels do not. On rc.4 or earlier the bot stops at the first step.

## Copy into the assistant

Paste the block below as the bot's instructions. Replace each [OWNER: ...] note with your own choice before you rely on a run. Do not paste a seed phrase, a private key, a keystore, or an API key into the chat.

```text
Run `guardian capabilities` first. The JSON it prints is the catalog. Classes A, B, C, and D follow that catalog.

If the `guardian` command does not exist, stop. Tell the owner: 沒有安裝. Do not guess a command. Do not install or upgrade. The owner installs the release.

If `guardian capabilities` returns `invalid choice`, stop. Do not run any further `guardian` command, including `--help` at any level. Do not look for a renamed command. Tell the owner: 已安裝的版本沒有能力目錄，請安裝含 `capabilities` 的 release. Do not guess a command. Do not install or upgrade. The owner installs the release.

If `guardian capabilities` does not exit 0, its output is not JSON, it names a class the catalog does not define, or its JSON `version` differs from `0.4.0rc5`, stop. Tell the owner. Do not guess.

`guardian --version` does not show whether `capabilities` is present. Use the result of `guardian capabilities`.

見 Classes.
見 Hard limits.

You operate the installed Wallet Guardian CLI for the owner. The package is a read-only preview (0.4.0rc5 in the tree these notes were written for).

Setup you still need from the owner, in words, not as secrets:
- Which addresses to watch. They must type the addresses. Do not derive addresses from a key or an xpub.
- Which chains, chosen from Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, Solana, limited Avalanche C-Chain, and address-set Bitcoin.
- Whether they have keys (key_mode byo) or not (key_mode none). If byo, they export the values themselves. You only ever see env var names.

List these commands, then wait for one agreement to the whole list:
- Start from examples/answers.example.json (synthetic address). Write answers only where the owner told you to.
- `guardian init` prints the data-sharing summary. Do not pass --yes until that summary is on the list and the owner has agreed to the list.
- `guardian config validate --mcm-config [OWNER: path to the monitor.json that init wrote]`
- `guardian run-once --mcm-config [OWNER: that file] --no-send`. A missing or unreadable `--news-input` file is an `exposure_news` gap (`news_unreadable`). The chain reads still run. Do not treat that gap as "no news" or as a finished round.
- `guardian coverage --mcm-config [OWNER: that file]`.
- Summarize coverage gaps and any outbox rows in plain language.

Treat credential_missing, partial, unknown, error, identity_unreadable, rate_limited, plan_not_supported, feed_not_supplied, blocked, expansion_pending, not_observable, unsupported, degraded, and stale as gaps. Never describe them as safe, complete, or zero. An empty outbox is not all clear. live_verified false and canary NOT_RUN stay as printed.

Do not install a service unless the owner asks, and even then start with `guardian install-service --dry-run` and stop. Do not pass apply. Do not invent a template URL or a release URL.
```

## First-run conversation

Tell the bot three things, and nothing secret:

1. The addresses, as public watch addresses you are willing to send to the RPC and indexer hosts you enable.
2. The chains, from the named list above. The example answers file watches only `ethereum-mainnet`.
3. Whether you already have your own API keys. If you do not, say `key_mode none`. If you do, say `byo` and export the variables yourself in a shell the bot does not record. The names are in [api-keys.md](api-keys.md).

Then ask it to show one list of the commands for this session and wait for one agreement to that list. 見 Classes. The list includes the init data-sharing summary, `--yes` on that init, `config validate`, `run-once --no-send`, and `coverage`. `run-once --no-send` still performs the reads.

## What the bot does for the owner

It runs `guardian run-once --no-send` with the monitor file from the agreed list, reads `guardian coverage`, and summarizes the JSON: which rows are `complete_in_declared_scope`, which are gaps, and which alert kinds were written. 見 Hard limits. It does not turn on a notification sender. Delivery stays `outbox_only` unless you later change that yourself.

## What it must not do

- 見 Hard limits.
- Treat a gap, a refused plan, a 429, or an empty outbox as a clean bill of health.
- Do not add chains the coverage matrix does not name. 見 Hard limits.
- Apply `install-service`, enable linger, or start a sender unless you explicitly take that step yourself.
- Invent a link for the bot template. The template is coming, and this page has no link.

## Privacy

Wallet addresses in a read are sent to the RPC or indexer hosts you enable. Your machine hostname is not added to those requests. The provider still sees the connection's IP address. With no key, a keyed indexer is not called and the row stays `credential_missing`. GoPlus and Chainabuse stay off unless you later opt in. The OFAC list is not downloaded by `init`. Details are in [security-privacy.md](security-privacy.md).

If an assistant runs the CLI, the command output goes through that assistant session. Coverage JSON, alert summaries, and the addresses in them are visible to that session and to whoever operates that assistant. A local `guardian` run without an assistant does not take that extra hop. Do not paste secret-profile values or `.env.local` into the chat either way.

# Using Wallet Guardian with Grok Bot

Two ways to use this preview: the `guardian` command by itself ([guide-cli.md](guide-cli.md)), or an assistant that runs that same command for you. This page is the second way. The package version in this tree is `0.4.0rc1`. The monitor is read-only. The named chains are Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, and Solana, with limited Avalanche C-Chain coverage and an address-set-only Bitcoin path. The 72h shadow validation is not complete. There is no PyPI release.

The shareable Wallet Guardian bot template is coming. There is no template link.

Until that template exists, point Grok Bot at the `guardian` executable you installed and at this repository's docs, especially [cli-reference.md](cli-reference.md), [coverage-matrix.md](coverage-matrix.md), and [security-privacy.md](security-privacy.md). The bot should call the installed command. It should not reimplement the monitor, and it should not invent flags that `--help` does not list.

## Copy into the assistant

Paste the block below as the bot's instructions. Replace the angle-bracket notes with your own choices before you rely on a run. Do not paste a seed phrase, a private key, a keystore, or an API key into the chat.

```text
You operate the installed Wallet Guardian CLI for the owner. The package is a read-only preview (0.4.0rc1 in the tree these notes were written for). You do not sign, build a transaction, broadcast, approve, revoke, or claim. You never ask for a seed phrase, a private key, or a keystore. If the owner pastes one, stop and tell them to rotate it outside this chat.

Setup you still need from the owner, in words, not as secrets:
- Which addresses to watch. They must type the addresses. Do not derive addresses from a key or an xpub.
- Which chains, chosen from Ethereum, BNB Chain, Arbitrum One, Base, Optimism, Polygon PoS, Solana, limited Avalanche C-Chain, and address-set Bitcoin. Do not say "all chains".
- Whether they have keys (key_mode byo) or not (key_mode none). If byo, they export the values themselves. You only ever see env var names.

Then:
- Start from examples/answers.example.json (synthetic address). Write answers only where the owner told you to.
- Run `guardian init` so it prints the data-sharing summary. Do not pass --yes until the owner has read that summary and agreed.
- Run `guardian config validate --mcm-config <the monitor.json init wrote>`.
- Run `guardian run-once --mcm-config <that file> --no-send`.
- Run `guardian coverage --mcm-config <that file>`.
- Summarize coverage gaps and any outbox rows in plain language.

Treat credential_missing, partial, unknown, error, identity_unreadable, rate_limited, plan_not_supported, feed_not_supplied, blocked, and expansion_pending as gaps. Never describe them as safe, complete, or zero. An empty outbox is not all clear. live_verified false and canary NOT_RUN stay as printed.

Do not enable GoPlus or Chainabuse. Do not install a service unless the owner asks, and even then start with `guardian install-service --dry-run` and stop. Do not claim the 72h shadow validation is complete, a CI pass, or a PyPI release. Do not invent a template URL or a release URL.
```

## First-run conversation

Tell the bot three things, and nothing secret:

1. The addresses, as public watch addresses you are willing to send to the RPC and indexer hosts you enable.
2. The chains, from the named list above. The example answers file watches only `ethereum-mainnet`.
3. Whether you already have your own API keys. If you do not, say `key_mode none`. If you do, say `byo` and export the variables yourself in a shell the bot does not record. The names are in [api-keys.md](api-keys.md).

Then ask it to show the init data-sharing summary and wait. After you agree, it can pass `--yes`, run `run-once --no-send`, read coverage, and summarize alerts. `run-once --no-send` still performs the reads.

## What the bot does for the owner

It runs `guardian run-once --mcm-config <file> --no-send`, reads `guardian coverage`, and summarizes the JSON: which rows are `complete_in_declared_scope`, which are gaps, and which alert kinds were written. It does not sign or send. It does not ask for seeds or keys. It does not turn on a notification sender. Delivery stays `outbox_only` unless you later change that yourself.

## What it must not do

- Ask for, store, or repeat a seed phrase, private key, keystore, or API key.
- Sign, approve, revoke, claim, build a transaction, or broadcast.
- Treat a gap, a refused plan, a 429, or an empty outbox as a clean bill of health.
- Add chains the coverage matrix does not name and then call the result complete.
- Enable GoPlus or Chainabuse user-address lookups. Init leaves both off.
- Apply `install-service`, enable linger, or start a sender unless you explicitly take that step yourself.
- Do not claim the 72h shadow validation is complete, that these commits passed CI, or that a package was published to PyPI.
- Invent a link for the bot template. The template is coming, and this page has no link.

## Privacy

Wallet addresses in a read are sent to the RPC or indexer hosts you enable. Your machine hostname is not added to those requests. The provider still sees the connection's IP address. With no key, a keyed indexer is not called and the row stays `credential_missing`. GoPlus and Chainabuse stay off unless you later opt in. The OFAC list is not downloaded by `init`. Details are in [security-privacy.md](security-privacy.md).

If an assistant runs the CLI, the command output goes through that assistant session. Coverage JSON, alert summaries, and the addresses in them are visible to that session and to whoever operates that assistant. A local `guardian` run without an assistant does not take that extra hop. Do not paste secret-profile values or `.env.local` into the chat either way.

# Changelog

All notable changes to the Wallet Guardian monitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

PEP 440 version `0.4.0rc5` maps to the git tag name `v0.4.0-rc.5`.
Whether that tag is published, see GitLab releases. This changelog does not publish. The distribution name is `wallet-guardian-monitor`.

## [Unreleased]

### Fixed

- `guardian relations` reads count each row once (!108). Blockscout and NodeReal stop on a repeated pagination cursor and keep the pages already read, marking the part `partial`. Rows are deduplicated by identity (transaction hash, log index, trace position); a row seen in both keyed and unkeyed copies counts once; rows that look duplicated but cannot be proven the same mark the part `partial` with reason `ambiguous_duplicate` instead of counting twice. A page-cap stop keeps what was read (`partial` / `page_cap`). A log index, block number or amount given as a non-ASCII digit string is treated as unreadable instead of stopping the run. Markdown output lists gaps per part. Known limits: duplicate rows without a key on the same page still count twice and the part stays `complete`; the same trace across pages under different field names can count twice; NodeReal and Blockscout handle the page cap differently.

## [0.4.0rc5] - 2026-10-17

Preview channel. No package-index upload is performed. The monitor is read-only: it does not sign, build a transaction, or broadcast. Wheel: `wallet_guardian_monitor-0.4.0rc5-py3-none-any.whl`.

### Added

- `guardian balance-probe --mcm-config --probe-state --no-send` reads Multicall3 native balances and topic-OR logs, and starts `run-once` only when a balance or a Transfer or approval log changed. When logs cannot be read, a native balance change already read in that round still records `balance_change` for that chain: the row stays `failure: logs`, the probe cursor does not advance, a same-balance or first observation is not committed, and nothing is written as zero. Token transfers and approvals still need a log source that accepts block ranges. It writes no alerts and does not advance `history.*` cursors. `--no-send` is required. Probe logs follow the `history.token` route and fail over only on a policy refusal. Migration `0011_balance_probe` adds `probe_cursors` and `probe_trigger_records`; `PRAGMA user_version` stays 1. On 2026-10-15, one round against public contract addresses, publicnode (Ethereum, BSC, Base, Optimism) accepted a topic-only `eth_getLogs` for a single block and returned JSON-RPC `-32701` for two or more blocks; one round, not a 24-hour measurement. Live latency measurement was not run.
- Dead-man switch for degraded coverage: `guardian watchdog` (already present in rc.4) now hands off through delivery without enabling a sender; a known gap stays quiet and a new gap emits `monitor_new_gap`. The watchdog accepts `--no-send` and does not call the sender. `guardian outbox pending --mcm-config` lists pending rows without wallet addresses; `guardian outbox ack --id` records a relay acknowledgement and does not send.
- Token transfer valuation: when a CoinGecko source is already enabled, free `simple/token_price` quotes can stamp `asset_usd`. Decimals come from Etherscan or one cached `decimals()` call per contract per round. A missing or failed quote is not stored as zero. A pinned stablecoin at or above `asset_usd_threshold` units, or a `token_raw_thresholds` hit, alerts as `unvalued_large_move`, which is not a USD value. `init` does not add a CoinGecko source.
- `guardian init` writes `native_unit_thresholds` for the non-ETH networks it enables (BNB 1, POL 1000, AVAX 10, BTC 0.01). These are operator units, not USD. No Solana floor is written, because Solana transactions are not parsed and native balance deltas are EVM-only.
- Optimism internal transfers via keyless Blockscout REST v2 (`history.native_internal`), routed in `examples/monitor.json`. A `txlistinternal` status 2 stays partial with `indexer_lag` and does not advance the cursor. Offline fixtures; not live-qualified.
- Log sources for BSC, Optimism and Base in the example monitors (`bsc-logs-48club`, `bsc-logs-nodereal`, `op-logs-public`, `base-logs-drpc`), with NodeReal `/v1/<key>` URL masking and redaction of keys nested in JSON-RPC `error.data`. `guardian init` does not add these sources.
- Shadow rounds record trigger count and detect-latency P50/P95 (null when not measured) and whether outbox rows match chain events above the threshold; `compare-legacy` adds a latency-delta column. The 24-window qualification rules are unchanged.
- `guardian install-service --schedule-mode launchd_agent` prints three LaunchAgent plists (5-minute probe, 3-hour `run-once`, 15-minute watchdog). It never calls `launchctl`.
- `guardian relations inbound` and `guardian relations expand` (manual, read-only): addresses that paid a watched address in a time window, and where one chosen payer also sent funds, with hub rules, heuristic evidence, a bounded score and confidence, and EVM cross-chain address merge. A disclosure plan is printed first; nothing is sent without `--yes`, and with `--yes` the plan is written to stderr and flushed before the first read. Every read reports `complete`, `partial`, `gap` or `unsupported`; a truncated or failed read is never an empty list. Keyless Optimism uses `explorer.optimism.io`; keyless BSC uses 48club, NodeReal when its key and URL are set. Solana and Bitcoin are unsupported. TRON reads only the TRC-20 endpoint and records `trx` and `trc10` as gaps, so a TRON read is never complete. Shared evidence does not show common ownership. See `docs/relations.md`. An agent must not run it.
- `guardian --version` and `guardian capabilities` (a static JSON map of every subcommand and variant with class A/B/C/D, network and local-write flags, consent flags, exit codes and gap rules; it opens no socket and writes no file). `skills/wallet-guardian/` is an Agent Skills package; `skills/dist/grok-bot/SKILL.md` is the flattened single file; `tools/build_agent_skills.py` and `tools/install_agent_skills.py` (dry run unless `--apply`, never overwrites). Installing the skill into Claude Code, Codex, Cursor, Grok Build, Hermes or Grok Bot was not exercised.
- Action Client (optional subsystem): release inventory and a GET-only deployed-artifact verifier; send construction isolated from the default `send_off` bundle (the default build fails if it is reachable); a fail-closed Mode A send variant whose build exits non-zero without an approved inventory entry; Mode A client-owned evidence checks with Lista deployment approvals kept empty; fail-closed session and membership revocation with idle timeout; private-task routes derived from one origin policy; one persisted Mode A outcome per task. Mainnet send stays off; no real-wallet, mobile in-app browser or deployment test was run.

### Fixed

- Candidates committed with a leaf cursor before the round's alert decision are kept in `pending_alert_candidates` (migration `0010`) and judged by the next round; a corrupt pending row is stored as a gap and skipped. A second `run-once` on the same state file is refused instead of racing the cursor.
- A `threshold_unavailable` movement is one visible unjudged outbox row and does not advance the baseline; dust against the same frozen baseline does not write another row.
- Address-less log refusals are classified: `-32701` "specify an address" is `filter_requires_address`, a one-block `-32005` "limit exceeded" is `method_disabled`; those sources are skipped next round. A real range cap still bisects.
- A missing, unreadable, or oversized news or price file is an exposure gap. A `SecurityError` on one network is recorded on that network and does not abort the other networks. News GETs run after the chain cycle with their own deadline, and news alerts commit in a later transaction.
- A blocked capability no longer counts as already seen: the baseline flag is set only after a successful read, so a later recovery does not replay the lookback window as one alert per old transfer.
- Priced transfers from `eth_getLogs` read decimals instead of falling through as `below_threshold`; a missing read emits `unvalued_large_move` with a reason.
- Every `run-once` and watchdog line that `install-service` generates carries `--no-send`. The legacy collector line (systemd `guardian-legacy-collector.service` and the second schedule line of the cron template, `guardian-unattended.cron:10`) runs the operator's own executable and does not carry `--no-send`.
- The launchd probe agent passes an absolute `--probe-state`.
- The Action Client ignores a tab's own logout broadcast; send-marker exemptions match content hashes, not file names; the release inventory stays out of the Python wheel.
- Long flags must be spelled in full (!104). Every `guardian` parser, nested subparsers included, now sets `allow_abbrev=False` (`src/guardian/cli.py:159-167`, used at `:172` and `:176`). Before, `install-service --output X --app` parsed as `--apply`; now an abbreviated flag exits 2 while arguments are parsed. `tests/unit/test_agent_skills.py:1515` checks `install-service ... --app` and `init --y`; `:1528` walks the parser tree and checks that each long flag with its last character dropped is refused. The skill copies (`skills/wallet-guardian/references/safety.md:64`, `skills/dist/grok-bot/SKILL.md:128`, `docs/guide-agents.md:13`, `docs/guide-grok-bot.md:34`) say a flag must match the `--help` spelling and an abbreviated flag is class D (`tests/unit/test_agent_skills.py:1562`). The skill also stops when `guardian capabilities` exits non-zero, prints non-JSON, names an unknown class, or reports another version (`skills/wallet-guardian/SKILL.md:34`). The skill generator rejects HTML comments in reference files (`tools/build_agent_skills.py:321-324`) and closes an outer fence only on a matching closer line.

### Changed

- A degraded `guardian run-once --mcm-config` exits 3 (rc.4 exited 0 on this path). `failed` stays 1. A misconfigured delivery still exits 2.
- CI: D-11 deleted the GitLab `pages` job and `action-client/vercel.json`; `.gitlab-ci.yml` does not publish or deploy. `python-tests` has a 20-minute job timeout, `resource_group: wg-python-tests`, `-rf --durations=25`, and a 60-second per-test cap (`pytest-timeout`); tests refuse live DNS and non-loopback sockets.
- README and docs: status-and-limits block, the init profile versus `examples/monitor.json` distinction, and corrected wording on Helius, GoPlus, DeBank, Zerion and Tenderly keys (not called by `run-once`).
- `AGENTS.md:24` (!105) now reads: "Do not read, print, or hand over API keys. The installed CLI reads them only from environment variables, the gitignored `.env.local`, or the secret profile JSON file whose path an environment variable gives (`MONITOR_SECRET_PROFILE` by default)." At 5a2c33f it read "Do not print API keys. Read them only from environment variables." The same line is quoted in `skills/wallet-guardian/references/safety.md:19` and `skills/dist/grok-bot/SKILL.md:83`. `docs/api-keys.md:5` lists the names `.env.local` supplies, and `docs/box-unattended.md:117` names the three sources for scheduled jobs. Tests pin the rule text (`tests/unit/test_agent_skills.py:519`, `:528`). Docs and tests only; no CLI code changed. `AGENTS.md:21` still says "Load API keys from the environment or gitignored `.env.local`."
- Docs only, no code (!106): security, privacy, coverage, scheduling and README text rewritten to match the code. `install-service --apply` is described as an install whether or not `--output` is set (`docs/scheduling.md:56`, `:62`, `:76`; `docs/security-model.md:34`; `docs/threat-model.md:93`). Blockscout PRO without a key is listed as still receiving the watch address in `examples/monitor.json` (`docs/security-privacy.md:24`, `docs/api-keys.md:80`). An existing `permissions.erc20` cursor keeps scanning and recording approvals; only adopting `polling.bootstrap_origin` as the start depends on having no cursor (`docs/guide-cli.md:101`). The audit items it closes are under Known limitations.

### Known limitations / not claimed

- None of the new paths (balance-probe, watchdog, token valuation, Optimism internal history, new log sources, relations) is live-qualified. Live latency was not measured.
- The 72-hour / 24-window formal shadow is not complete. No live Telegram canary (delivery defaults to `outbox_only`; the generated `run-once` and watchdog lines carry `--no-send`, but the legacy collector line runs the operator's own executable without it). No production-egress qualification. No PyPI release.
- CI: GitLab pipeline [#2957467957](https://gitlab.com/aa22396584/wallet-guardian/-/pipelines/2957467957) on `5a2c33f5b55568dda24eec856bbc4e621a73a717` (push to `main`, 2026-10-17) finished with status `success`. It ran `python-tests` (1062 tests, 1062 passed, 0 skipped per the pipeline test report) and `client-tests`. The `box-*` jobs did not run, and no job runs `post-install-functional`, `read-only-negative` or `public-sample-probe`. That pipeline ran on `5a2c33f`, not on the release commit; no CI result is claimed for the release commit. That pipeline is not release evidence; release evidence for rc.5 is `NOT_RUN`.
- A 2026-10-17 doc-vs-code audit of `5a2c33f` found 16 doc overclaims (5 high) and 10 undocumented limits. !106 rewrote the doc text for each of the 16 and now states each of the 10; the item-by-item list will be in the release notes for `v0.4.0-rc.5`. !106 changed docs only, so the code behaviour behind the high ones is unchanged and is now stated: Bitcoin reads send the watch address to Esplora, and keyless TronGrid reads send it too (`docs/threat-model.md:48`, `:52`); setting `OFAC_SLS_FEED` alone does no screening in an init profile (`docs/security-model.md:31`); GoPlus and Chainabuse are never called even with key, base and consent (`docs/threat-model.md:53`); approval alerting does not cover Permit2 allowances and does not inventory approvals older than the 30-day bootstrap origin (`README.md:11`). Two older phrases remain: `docs/guide-grok-bot.md:81` says GoPlus and Chainabuse "stay off unless you later opt in", and `docs/coverage-matrix.md:213` says GoPlus wallet-address queries "stay off until local consent".
- `install-service --apply` installs even with `--output`: cron mode still runs `crontab -u <user>`, and systemd mode still runs `systemctl daemon-reload` and `systemctl enable` on the three timers. The docs were corrected in !106: `docs/scheduling.md:56` now says `--output` only changes where the rendered files are written and that `--apply` without `--dry-run` installs whether or not `--output` is set (also `docs/scheduling.md:76`, `docs/security-model.md:34`, `docs/threat-model.md:93`). The code is unchanged in this range (`src/guardian/service_install.py:821-832`).
- Inherited: free-tier and provider limits; Solana transactions are not parsed (no SOL/SPL amount alerts); TRON is experimental, opt-in and not live-qualified; yield has no live path; positions are opt-in; inflow explanations have no approved Lista deployment intervals; `constraints.txt` pins versions but does not hash-verify dependencies.

### Release blockers for stable

- The formal 24-window shadow; live gaps in `docs/coverage-matrix.md`; Telegram canary; release evidence with status `PASS` on the tagged SHA (including the steps no CI job runs); fixes for the code gaps behind the high findings of the 2026-10-17 audit (!106 corrected the docs, not the code); approved Lista deployment intervals before any operational `MATCHES_OWN_REQUEST`; a human decision to publish.

## [0.4.0rc4] - 2026-10-11

Preview channel. The manifest is a pre-publication evidence snapshot; the GitLab release is recorded separately. No package-index upload is performed. The monitor is read-only: it does not sign, build a transaction, or broadcast. Wheel: `wallet_guardian_monitor-0.4.0rc4-py3-none-any.whl`.

### Added

- Explicit network aliases: `arbitrum-mainnet` and `polygon-mainnet` normalize to the registry ids `arbitrum-one` and `polygon-pos` on accounts, routes, and enabled networks. Typing the registry id keeps that id, so an existing baseline stays the state key. Unknown names fail with the valid ids and suggestions.
- One credential resolver for doctor, coverage, and the read adapters. A nonempty secret-profile value wins over the process environment. Diagnostics show the variable name, `selected_from` (`profile`, `env`, or `absent`), presence, and whether the other source was shadowed. Values are not printed. A 401 does not switch source.
- `guardian doctor` is offline unless `--live` is set. The report separates requested resolver mode, effective resolver mode, connectivity, and egress qualification. DoH enabled is not a qualified path. `providers verify` still runs the bounded provider probe.
- A net native-balance change on a configured EVM network can be a `balance_delta` alert without a nonce change and without a history source, when an eligible definite-block observation and a usable threshold are available. The first valid observation is a quiet baseline. A failed read is not stored as zero. See `docs/balance-deltas.md`.
- A BSC native increase can be checked against a versioned Lista StakeManager explainer. The packaged manifest ships with no approved deployment interval. The shipped manifest has no approved deployment intervals and cannot currently produce the strongest operational verdict. The result is a separate revision (`MATCHES_OWN_REQUEST`, `PARTIAL`, `NO_MATCH`, or `UNVERIFIED`). It does not change the alert severity, the attribution, or a safety label. `guardian explain --event-id` stays offline unless `--refresh` is passed with `--mcm-config`. That refresh is read-only. See `docs/inflow-explainer.md`.
- `alerts.native_unit_thresholds` is an optional per-network native-unit floor. The existing 0.5 native-ETH floor stays on ethereum-mainnet, arbitrum-one, base-mainnet, and optimism-mainnet.
- BSC NodeReal category `internal` uses its own `history.native_internal` cursor. Offline fixtures only. No live internal probe is claimed.
- `docs/balance-probe-design.md` describes a future read-only probe command. The command is not implemented. No scheduler was added.

### Fixed

- Explanation work, including jobs left from an earlier round, runs after the native-balance pass and the alert handoff, and only on the time still left in the round.
- A running explanation job keeps a SQLite lease. Another process cannot take it before the lease expires, and a reorg fences the job before the first revision.
- An internal native credit is explained from its transaction and amount. The residual keeps that amount when the alert has no balance checkpoint.
- A Lista request is not linked by a matching slisBNB amount or by a removed log. The cached row keeps the block hash and the removal flag, and a conflicting copy is not verified. The transaction index has to agree on the discovered claim, the transaction, and the receipt.
- Experimental read-only attribution with bounded discovery. A verified request match requires an explicitly approved historical deployment and coherent transaction, receipt, request and checkpoint evidence. Incomplete evidence remains partial or unverified. Explanations do not certify fund safety, establish operator control, guarantee complete attribution, or change the underlying movement alert. No live historical-path qualification is claimed.
- Diagnostics do not echo an unusable resolver mode or a network name that is a URL or an address. `resolver.requested_mode` is only `system`, `doh`, or `invalid`. Network errors quote an identifier-shaped name, otherwise `<invalid name>`.
- Alias normalization collapses identical routes and rejects conflicting source lists or source order for the same canonical network and capability. The conflict is a config error. State keys are not rewritten and sources are not merged.
- `guardian doctor --live` resolves credentials once and uses that selection for the report and the probe requests. A profile value is what the probe sends when it wins, including when the environment is empty, and a 401 does not switch source. Unusable resolver settings, including `doh` with endpoints or bootstrap pins that do not load, set `resolver.error`, leave `effective_mode` null, and do not open a provider connection. When the settings load, each probe connects to the validated address and keeps the original hostname for the Host header, SNI, and certificate check. The 5 second bound is remaining monotonic time: connect and TLS use the time still left, an expired deadline closes the stream before request bytes are written, the open client is closed, and that probe does not continue to its next call. With `--mcm-config`, the live check is labelled as a legacy provider probe and does not prove the selected profile's connectivity. `providers verify` is that same labelled legacy probe.
- The test suite points home and the XDG config, state, and data directories at a per-test directory and clears inherited monitor path, endpoint, and resolver settings so a developer secret profile or `.env.local` is not reloaded.
- A numeric block-height balance is used only when the endpoint rejects the EIP-1898 parameter or tag. Canonical, historical, rate-limit, and mixed result/error responses are not eligible checkpoints. A successful envelope with `result: null` is an absent checkpoint, not an unsupported tag, and does not fall back to head.
- A forward balance comparison re-reads the stored checkpoint before it can be `continuous`, including when no conflict is open. The proof counts only when the returned block number is the requested height and the hash is well formed. The round budget is checked again after that read returns. A missing, mismatched, or late proof defers the comparison and leaves the baseline in place.
- When several native transfers together explain one balance delta, the notification transaction reserves the whole set. A component notice that is already pending is marked superseded and linked to the delta. It is not marked sent. Evidence rows, gross components, and sent receipts stay.
- Reconciliation requires positive execution evidence. An orphaned fee is excluded. A malformed native amount on a relevant internal row is a parsing gap.
- An observation commits against the baseline version read before the fetch. One lower block does not reset that baseline. A same-height hash conflict is retained. Rebaseline runs only after the stored hash is re-read and is not canonical. A forward comparison does that re-read even when no conflict row is open. Retention prunes old checkpoints and is not treated as a missed round. Non-alert decisions are stored with the observation.
- A native-threshold hit is committed before optional pricing, so a pricing failure keeps the observation. Quote requests use the selected credential, clock, and sleeper, and keep the quote source and timestamps.
- Internal history does not treat a failed receipt, a trace error, or `DELEGATECALL` / `STATICCALL` as a successful native transfer. An ambiguous row is a coverage gap. Reconciliation does not mark limited or indexer-reported history as explained only because the amount matches.
- A native transfer and a balance delta for the same movement share one pending notification, in either arrival order. The relationship is re-checked inside the transaction that creates the notification. Both evidence rows stay, and a sent delivery row stays sent.
- `native_unit_thresholds` applies to native-transfer candidates as well as balance deltas. An unknown network key is a config error. `1.0` BNB is not a shipped default.

### Changed

- `guardian doctor` no longer contacts providers unless `--live` is passed. `--live` uses a 5 second whole-call deadline per probe.
- A blank secret-profile value can use a key inherited from the process environment. Unattended services should start from a deliberately scoped environment.

### Known limitations / not claimed

- Adds native-movement detection, first-run diagnostics and experimental inflow attribution. These are preview features with fixture evidence; none has been live-qualified.
- Native-balance alerts report net changes between eligible checkpoints, not complete gross flows. Detection remains polling-bound: rc.4 installs no new scheduler and does not itself reduce polling latency. The first successful observation is a baseline. Failed or inconsistent reads can defer a comparison, opposing movements can cancel between observations, and a missing usable threshold can prevent an alert. 1.0 BNB is an operator-profile setting, not a shipped universal default, and unconditional no-key BSC alerts are not claimed.
- BSC NodeReal internal history is a configured, keyed path with offline fixture evidence, not newly live-qualified coverage. Other networks' internal-history gaps remain explicit. Static counterparty labels do not establish safety, and a withdrawal payout is not automatically profit.
- Inflow explanations: automatic, bounded Lista attribution attempts are included. The shipped manifest has no approved deployment intervals, so `MATCHES_OWN_REQUEST` is not currently available operationally. Results may remain partial or unverified. An explanation is a separate revision from the original movement alert; it does not certify safety and does not automatically resend a notification. The live historical path is not qualified.
- Operator-reported context: the legacy BSC patrol reported an approximately 8.42 BNB internal withdrawal payment about three hours later. The rc.4 fixtures reproduce its shape; they are not a live replay of that transaction.
- Doctor stays offline unless `--live`; DoH enabled is not a qualified connectivity or egress path; the legacy-probe limitation remains.
- Inherited from rc.3: evidence is the full local gate at the release SHA, not a GitLab CI pass; the 72-hour formal shadow is not complete; no live Telegram canary (delivery defaults to off); no production-egress qualification; free-tier and provider limits; TRON is not live-qualified; yield has no live path; positions are opt-in; no PyPI release; `constraints.txt` pins versions but does not hash-verify dependencies.
- The aggregate release evidence stays `NOT_RUN` while the optional public-sample-probe is not executed; that is waived for this labelled preview only.

### Release blockers for stable

- The formal 24-window shadow; live gaps in `docs/coverage-matrix.md`; Telegram canary; a CI pass; approved Lista deployment intervals before any operational `MATCHES_OWN_REQUEST`; a human decision to publish.

## [0.4.0rc3] - 2026-10-10

Preview channel. The manifest is a pre-publication evidence snapshot: its `published: false` records that generation stage, and the GitLab release is recorded separately. No package-index upload is performed. The monitor is read-only: it does not sign, build a transaction, or broadcast. Wheel: `wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl`.

### Added and hardened

TRON and the new position readers require explicit opt-in and are off in the shipped example monitor. The yield component remains inert. Applicable display and feed hardening operates in the normal runtime paths.

- Injection and privacy tests: untrusted feed text is quarantined per item, display text is neutralised, and a policy table (`privacy_boundary`) lists what may leave the machine. See `docs/injection-privacy-tests.md` for the residuals: a double HTML escape is cosmetic, the incident matcher can miss digit-plus-letter tokens such as "Aave v3 incident", and there is no claim of general prompt-injection immunity.
- Opt-in positions: sDAI and Steakhouse USDC ERC-4626 pins (vault exposure only) and Compound III USDC (stored-index present value, not accrued to now; block height is not block-hash proof; a positive oracle price is not freshness). sUSDe and Morpho Blue markets are deferred. See `docs/positions-more.md`.
- Experimental TRON reader: opt-in, the chain stays `enabled: false`, bounded native TRX and TRC-20 history and configured balances, not live-qualified, genesis identity not verified, only finalized native TRX produces alert candidates, no TRC-20 or approval alerting. Do not use it as the only safety monitor. See `docs/coverage-tron.md`.
- Yield and promotion parsers: offline fixture and parser only, disabled. No live fetch, scheduler or delivery; extracted terms are partial interpretations. See `docs/yield-promotions.md`.

### Changed

- Coverage matrix, README status and install docs describe the four items above with these limits. Named networks are unchanged; there is no "all chains" claim.

### Known limitations / not claimed

- The 72-hour formal shadow (24 successive windows) is not complete. Evidence is the full local gate at the release SHA, not a GitLab CI pass.
- No live Telegram canary; delivery defaults to off.
- TRON is not live-qualified. Yield has no live path. sUSDe and Morpho Blue are not supported.
- The aggregate release evidence stays `NOT_RUN` while the optional public-sample-probe is not executed; that is waived for the labelled preview only.
- No PyPI release. `constraints.txt` pins versions; it does not hash-verify dependencies.

### Release blockers for stable

- The formal 24-window shadow; live gaps in `docs/coverage-matrix.md`; Telegram canary; a CI pass; a human decision to publish.

## [0.4.0rc2] - 2026-10-09

Preview channel. The manifest is a pre-publication evidence snapshot: its `published: false` records that generation stage, and the GitLab release is recorded separately. No package-index upload is performed. The monitor is read-only: it does not sign, build a transaction, or broadcast. Wheel: `wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl`.

### Fixed and changed

- User-facing preview copy: a CLI guide, command reference, scheduling, FAQ, Grok Bot notes, security and privacy, a contributing guide, and GitLab issue templates.
- A 429 on `eth_chainId` is retried inside the round deadline and a successful identity is reused for that run; the example NodeReal source shares the `nodereal-key` budget so one key is not burst across hosts.
- Indexer catch-up starts at the lookback origin, uses one span per explorer, and stops at `bootstrap_request_cap` so a later round can resume. Plan refusals and successful DNS answers are cached.
- Transfer-log directions split on the same ranges, 429 waits honor Retry-After inside the round deadline, and eth_getLogs counts toward `bootstrap_request_cap` so a no-key bootstrap stays bounded.
- Docs: the quickstart now states that an RPC endpoint variable must be set (without it `run-once` stops with `endpoint_missing`), the resolver-mode hint, and what a bounded no-key first run looks like. Found by the clean-room smoke of this release.
- Docs: the install page names the rc.2 wheel and tag; stale statements flagged by the external review (wheel name, tag text, signing boundary) are corrected. News-feed health, local OFAC screening and legacy compare classification are wired into `run-once`; a stale or unreadable feed is reported, never "no risk".

### Known limitations / not claimed

- The 72-hour formal shadow (24 successive windows) is not complete. Evidence for rc.2 is the full local gate at the release SHA, not a GitLab CI pass.
- The monitor is a preview for the named chains in `docs/coverage-matrix.md`; it does not claim to cover all chains and is not claimed to be production ready.
- Free-tier keys limit coverage. A rate-limited or key-less read is partial, `credential_missing`, or `identity_unreadable` with its cause, never a zero balance.
- Telegram canary stays `NOT_RUN`; delivery defaults to off.
- Whether git tag `v0.4.0-rc.2` is published, see GitLab releases. rc.1 is `v0.4.0-rc.1` and is unchanged.

### Release blockers for stable

- The formal 24-window shadow, which has not been run; live gaps recorded in `docs/coverage-matrix.md`; Telegram canary; a CI pass; a human decision to publish.

## [0.4.0rc1] - 2026-10-09

Preview channel. `published` stays false. The monitor is read-only: it does not sign, build a transaction, or broadcast.

### Changed

- The distribution name is `wallet-guardian-monitor` (wheel `wallet_guardian_monitor-0.4.0rc1-py3-none-any.whl`). The import package and the CLI stay `guardian`. A read-only PyPI lookup on 2026-10-09 returned 404 for that name, so it looked free at that time. It is not reserved and nothing was uploaded.

### Added

- MCM-LIVE-01R: free-tier history catch-up to a fixed checkpoint, and no-key fallbacks. A missing key is `credential_missing` with a pointer in `docs/api-keys.md`. A plan refusal is cached and is not an empty wallet. See `docs/mcm-live-01r.md`.
- S4: installable wheel and sdist, `constraints.txt`, and `SHA256SUMS` from `scripts/build-release.sh`. License is AGPL-3.0-only. See `docs/install.md`.
- S2: same-host legacy evidence sidecar and compare path. See `docs/legacy-evidence.md`.
- S6: on-demand runner model and `tools/release_evidence.py`. A missing gate step stays `NOT_RUN`. See `docs/ci.md`.
- S0: egress policy, DNS-over-HTTPS, and `guardian egress-doctor`. See `docs/host-network.md`.
- S5: `guardian init` for one operator, plus `docs/security-model.md` and `docs/threat-model.md`.
- S7: opt-in Telegram sender. Tests use a mock. The canary is `NOT_RUN`. See `docs/notifications.md`.
- S3: `guardian install-service` is a dry-run installer for separate shadow, watchdog, and legacy-collector systemd units (cron only as a fallback). It prints the plan and exits non-zero until an apply really installs; `--apply` is explicit. No unit was installed on any host and the host smoke is `NOT_RUN`. See `docs/box-unattended.md`.
- S1: Base source profile. Alchemy Transfers and the Base log route are covered by offline fixtures. No live Alchemy probe is recorded. See `docs/coverage.md`.
- S8: honest coverage matrix (`docs/coverage-matrix.md`), an unpublished release-manifest draft, and a version consistency check. See `docs/release-preview.md`.

### Known limitations / not claimed

- The formal shadow of 24 successive windows has not been run. The shadow tool does not start that count.
- This preview does not claim the monitor is fit for production use.
- Telegram canary status is `NOT_RUN`.
- On the reviewed SNI-proxy host, formal egress qualification is `BLOCKED_EGRESS`. A preview verdict of OK on that host is not proof.
- Solana, Kinza, and Alchemy Transfers on Base have offline fixtures and no recorded live probe for those claims. The coverage matrix states the cell.
- BSC external history has no keyless source. Avalanche deep native history is `expansion_pending`.
- The unattended installer has never been applied on a real host. Host smoke and restart smoke are `NOT_RUN`.
- Whether git tag `v0.4.0-rc.1` is published, see GitLab releases. The manifest draft stays unpublished.

### Release blockers for stable

- A real gate directory whose `tools/release_evidence.py` status is PASS, including pause and cleanup readback when the label is a GitLab runner.
- The formal 24-window shadow, which has not been run.
- Telegram canary, which is `NOT_RUN`.
- Formal egress qualification on a host that actually delivers the pinned address. The reviewed SNI-proxy host stays `BLOCKED_EGRESS` for that qualification.
- Live gaps recorded in `docs/coverage-matrix.md`, including Base host-specific keyless Blockscout, BSC external history without a key, Avalanche deep history, Solana public-address checks, and Kinza.
- A human decision to publish. Whether git tag `v0.4.0-rc.1` exists, see GitLab releases. How the monitor differs from the optional Action Client is stated in `docs/threat-model.md`. The Telegram sender exists and is opt-in; delivery defaults to off (`outbox_only`), and `--no-send` forbids sends.
- Host smoke of the unattended units (install, reboot, missed-slot recording) on the actual host.

## [0.3.0] - 2026-10-07

Earlier read-only monitor and task-workbench tree. `docs/coverage.md` describes that tree. Those notes are not restated as new live probes for 0.4.0rc1.

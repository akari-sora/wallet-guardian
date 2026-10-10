# Changelog

All notable changes to the Wallet Guardian monitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

PEP 440 version `0.4.0rc3` maps to the git tag name `v0.4.0-rc.3`.
Whether that tag is published, see GitLab releases. This changelog does not publish. The distribution name is `wallet-guardian-monitor`.

## [Unreleased]

### Added

- Explicit network aliases: `arbitrum-mainnet` and `polygon-mainnet` normalize to the registry ids `arbitrum-one` and `polygon-pos` on accounts, routes, and enabled networks. Typing the registry id keeps that id, so an existing baseline stays the state key. Unknown names fail with the valid ids and suggestions.
- One credential resolver for doctor, coverage, and the read adapters. A nonempty secret-profile value wins over the process environment. Diagnostics show the variable name, `selected_from` (`profile`, `env`, or `absent`), presence, and whether the other source was shadowed. Values are not printed. A 401 does not switch source.
- `guardian doctor` is offline unless `--live` is set. The report separates requested resolver mode, effective resolver mode, connectivity, and egress qualification. DoH enabled is not a qualified path. `providers verify` still runs the bounded provider probe.
- A net native-balance change on a configured EVM network can be a `balance_delta` alert without a nonce change and without a history source, when an eligible definite-block observation and a usable threshold are available. The first valid observation is a quiet baseline. A failed read is not stored as zero. See `docs/balance-deltas.md`.
- A BSC native increase can be checked against a versioned Lista StakeManager explainer. The packaged manifest ships with no approved deployment interval. The result is a separate revision (`MATCHES_OWN_REQUEST`, `PARTIAL`, `NO_MATCH`, or `UNVERIFIED`). It does not change the alert severity, the attribution, or a safety label. `guardian explain --event-id` stays offline unless `--refresh` is passed with `--mcm-config`. That refresh is read-only. See `docs/inflow-explainer.md`.
- `alerts.native_unit_thresholds` is an optional per-network native-unit floor. The existing 0.5 native-ETH floor stays on ethereum-mainnet, arbitrum-one, base-mainnet, and optimism-mainnet.
- BSC NodeReal category `internal` uses its own `history.native_internal` cursor. Offline fixtures only. No live internal probe is claimed.
- `docs/balance-probe-design.md` describes a future read-only probe command. The command is not implemented. No scheduler was added.

### Fixed

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

### Not claimed

- No live balance probe, no live NodeReal internal attribution, and no production or 72-hour shadow claim. The published preview `0.4.0rc3` (`wallet-guardian-monitor`) is unchanged.

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

# Changelog

All notable changes to the Wallet Guardian monitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

PEP 440 version `0.4.0rc2` maps to the git tag name `v0.4.0-rc.2`.
Whether that tag is published, see GitLab releases. This changelog does not publish. The distribution name is `wallet-guardian-monitor`.

## [Unreleased]

## [0.4.0rc2] - 2026-10-09

Preview channel. `published` stays false for PyPI: nothing is uploaded to a package index. The monitor is read-only: it does not sign, build a transaction, or broadcast. Wheel: `wallet_guardian_monitor-0.4.0rc2-py3-none-any.whl`.

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

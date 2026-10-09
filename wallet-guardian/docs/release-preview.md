# Preview release 0.4.0rc3

License: AGPL-3.0-only (see `LICENSE`).

This note is the preview-versus-stable gate list for the shareable preview. The distribution name is `wallet-guardian-monitor` (wheel `wallet_guardian_monitor-0.4.0rc3-py3-none-any.whl`). It records which items this branch already has evidence for, and which stay `NOT_RUN`. It does not upload or publish. How the monitor differs from the optional Action Client is stated once in `docs/threat-model.md`.

PEP 440 `0.4.0rc3` is the version in `pyproject.toml`, `guardian.__version__`, `uv.lock`, `CHANGELOG.md`, and `release/manifest.draft.json`. The git tag name that matches that PEP 440 string is `v0.4.0-rc.3`. Whether that tag is published, see GitLab releases.

`tools/release_manifest.py` drafts a manifest and refuses `published: true`. The committed draft keeps `channel` at `preview`, `published` at false, and `tag` at null.

## Preview gates and stable gates

| Gate | Preview on this branch | Evidence | Stable |
| --- | --- | --- | --- |
| Installable wheel, constraints, AGPL-3.0-only | Met by the S4 tree | `scripts/build-release.sh`, `docs/install.md`, `LICENSE`, install tests | Rebuild the dist for the commit you tag, and check `SHA256SUMS` |
| Free-tier catch-up and no-key gaps | Met offline | `docs/mcm-live-01r.md`, `docs/api-keys.md`, R1 tests | Operator keys stay outside the repo. A missing key stays `credential_missing` |
| Same-host legacy evidence | Met offline | `docs/legacy-evidence.md`, S2 tests | A real shadow still has to point the tool at the operator's sidecar |
| On-demand CI model and release evidence | Model is in the tree. A gate run is `NOT_RUN` | `docs/ci.md`, `tools/release_evidence.py`, `schemas/release-evidence.schema.json` | PASS evidence from a real gate directory. A missing step stays `NOT_RUN` |
| Egress policy and DNS-over-HTTPS | Implemented | `docs/host-network.md`, S0 tests, `guardian egress-doctor` | Formal qualification needs proof the network delivered the pinned address |
| SNI-proxy host | Formal verdict is `BLOCKED_EGRESS`. Preview verdict may be OK and is not proof | `docs/host-network.md` | Formal qualification stays `BLOCKED_EGRESS` until destination enforcement is shown |
| `guardian init`, security model, threat model | Met | `docs/security-model.md`, `docs/threat-model.md`, S5 tests | Still one operator on a machine they control. Not a hosted multi-tenant service |
| Telegram sender | The sender exists and is opt-in. Delivery defaults to off (`outbox_only`). `--no-send` forbids sends. Canary is `NOT_RUN` | `docs/notifications.md`, S7 tests | Canary is `NOT_RUN` |
| Base source profile | Offline fixtures met. Live Alchemy probe is not recorded | `src/guardian/sources/alchemy.py`, S1 tests, `docs/coverage-matrix.md` | Keyed cell stays `not_verified` until a live probe is recorded |
| Coverage matrix | This preview | `docs/coverage-matrix.md` | Update the matrix when a probe changes. Do not promote a gap |
| Release manifest | Draft only | `tools/release_manifest.py`, `schemas/release-manifest.schema.json`, `release/manifest.draft.json` | The tool still refuses `published: true` |
| Formal 24-window shadow | `NOT_RUN` | `docs/mcm-live-01.md` describes the counter. The tool does not start it | Blocker. An unrun shadow is not a finished qualification |
| Service installer | Dry-run installer met. Host smoke `NOT_RUN` | `guardian install-service` (S3), `docs/box-unattended.md`, S3 tests | A printed plan is not an installed service; no unit was applied on any host |
| Tag and upload | see GitLab releases | Whether tag `v0.4.0-rc.3` is published, see GitLab releases. Nothing is pushed by this preview | Operator steps below, done later by a person |

## What stays NOT_RUN

- Telegram canary.
- The formal 24-window shadow.
- A release-evidence PASS collected from a real gate directory.
- A live probe of Alchemy Transfers on Base.
- A live public-address check for Solana.
- A live Kinza check.
- Formal egress qualification on the reviewed SNI-proxy host.

The coverage matrix is the cell-level list. A failed read stays a gap.

## How to cut the release later

These steps are for an operator after this preview. They are not done here. Do not treat this file as a tag.

1. Choose the commit. Confirm `tools/check_version_consistency.py` exits 0 so `pyproject.toml`, `guardian.__version__`, `uv.lock`, `CHANGELOG.md`, and `release/manifest.draft.json` still agree on `0.4.0rc3`.
2. From `wallet-guardian`, build with `sh scripts/build-release.sh --out-dir dist`. That writes the wheel, the sdist, `constraints.txt`, and `SHA256SUMS`. It does not upload.
3. On the on-demand host, run the gate described in `docs/ci.md` for that commit. Collect the gate directory. Then run `python tools/release_evidence.py --gate-dir GATE --output release-evidence.json`. If a required step is missing, the status stays `NOT_RUN` or worse, and that file is not a stable pass.
4. In a shell that does not have API keys or bot tokens set, draft the manifest: `python tools/release_manifest.py --dist dist --output release/manifest.draft.json --source-sha SHA --evidence release-evidence.json`. `SHA` is the 40-hex commit. The tool refuses to run when credential variables are set, refuses URL arguments, and refuses `published: true`.
5. Read the draft. `published` is false, `tag` is null, and `channel` is `preview`. Publishing is a separate human action this tool does not perform.
6. The matching git tag name is `v0.4.0-rc.3`. Whether it is published, see GitLab releases. Create or publish it only in that later step.
7. Upload the dist and any release page yourself, after the hashes match. This branch does not push.

## Related pages

- `docs/coverage-matrix.md` for cell statuses.
- `docs/api-keys.md` for env var names.
- `docs/host-network.md` for egress verdicts.
- `docs/install.md` for installing a wheel you already checked.
- `CHANGELOG.md` for the preview entry, the unclaimed list, and the stable blockers.

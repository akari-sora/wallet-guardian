# CI: on-demand runner and release evidence

GitLab.com shared-runner minutes are exhausted. The release gate runs on the operator's existing on-demand Linux host and does not consume GitLab.com compute minutes. This document is the model and the evidence contract. It does not register a runner, call the GitLab API, or start a container.

Runner profile: `ci/profile/wg-box-ondemand.md`. Example config (placeholders only): `ci/box-runner.config.example.toml`. Collector: `tools/release_evidence.py`. Schema: `schemas/release-evidence.schema.json`.

## On-demand runner model

- Dedicated tag: `wg-box-ondemand`.
- Dedicated CI UID, rootless Podman, cgroup limits.
- One gate at a time: runner `concurrent = 1`, runner `limit = 1`, and job `resource_group: wg-box-ondemand`.
- The runner stays paused except while an approved SHA is running. After the job, pause it again and record cleanup.
- A persistent host runs only an approved explicit SHA. Arbitrary fork merge-request code does not run next to private monitoring data.
- Shared-runner jobs `python-tests` and `client-tests` keep their current rules. `pages` stays in the file (D-11). On `main` it is `when: manual` and `allow_failure: true`, so a merge does not publish and does not block the pipeline.

Own runners do not spend GitLab.com minutes. A failed GitLab pipeline is never treated as green.

## Isolation boundaries

Jobs and the example config must not:

- set privileged mode
- use host networking
- mount the container engine socket into the job
- mount monitor state, the bot home directory, the whole workspace, a runner token, `private/`, `data/`, or `evidence/`
- mount any volume other than the executor cache (`/cache`)

The YAML holds no secrets, real monitored addresses, keyed RPC URLs, or notification recipients. Box jobs use the same gate commands as the shared jobs: Python tests with JUnit, client tests, and build checks (`uv build --wheel --sdist`).

## How to trigger

`box-python-tests`, `box-client-tests`, and `box-build-checks` are added only by the first matching rule:

1. A fork merge request (`CI_MERGE_REQUEST_SOURCE_PROJECT_ID` different from `CI_MERGE_REQUEST_PROJECT_ID`) is `when: never`.
2. Any `merge_request_event` is `when: never`.
3. `WG_BOX_CI` equals `1` and `CI_PIPELINE_SOURCE` is `web`, `api`, or `trigger`.
4. `WG_BOX_CI` equals `1` and `CI_COMMIT_REF_PROTECTED` is `true`.
5. Otherwise `when: never`.

Manual here means the operator starts the pipeline from the GitLab web UI (source `web`) or by API or trigger, with variable `WG_BOX_CI=1`. That is not an unreviewed fork MR.

Each job requires a non-empty `CI_COMMIT_SHA` and writes it to `gate-run/CI_COMMIT_SHA`. The evidence `source_sha` has to be that same commit. Before unpausing, the operator checks the host lock and that the SHA is the approved one.

## Evidence format

The collector reads a gate directory. It does not open the network and it does not read `private/`, `data/`, or `evidence/`.

```text
identity.json
versions.json
steps/<id>.log
junit/python.xml
junit/client.xml
artifacts/<wheel-or-sdist>
artifacts/hashes.json
probe/provenance.json
runner/pause.json       # required for gitlab_runner
runner/cleanup.json     # required for gitlab_runner
```

`identity.json`:

```json
{
  "source_sha": "<40 hex CI_COMMIT_SHA>",
  "content_hash": "<64 hex>",
  "environment": "local_external"
}
```

`environment` is `local_external` or `gitlab_runner:<tag>` (for this host, `gitlab_runner:wg-box-ondemand`). `execution_label` copies that value. `external_execution` is true only for `local_external`.

`content_hash` uses the same runtime definition as `content_hash()` in `tools/mcm_shadow_round.py`: file bytes under `src/guardian` and `config/`, LF-normalized, skipping `__pycache__`. The collector records the value stored in the gate directory. It does not walk the repository, so a private tree is never hashed by this tool.

`versions.json` must include string values for `python`, `uv`, `pytest`, `node`, and `npm`.

A step log is a header, then `---`. Lines after `---` are not trusted and are not copied into the evidence.

```text
command: uv run --frozen pytest -q --junitxml=test-results/python.xml
exit_code: 0
allow_failure: false
junit: junit/python.xml
```

Required step ids:

- `python-tests` (JUnit required, skipped tests are not PASS)
- `client-tests` (JUnit required, skipped tests are not PASS)
- `build-checks` (wheel and sdist sha256 required when this step exits 0)
- `post-install-functional`
- `read-only-negative`
- `public-sample-probe` (also requires `probe/provenance.json`)

The public-sample probe uses a committed public fixture and a synthetic sample id. It is not a live monitored address. A failed read is `FAIL`, `BLOCKED`, or `NOT_RUN`, never a zero balance and never safe or complete.

Collect:

```sh
cd wallet-guardian
uv run --frozen python tools/release_evidence.py --gate-dir <gate-dir> --output release-evidence.json
```

Exit 0 means the written document `status` is `PASS`. Any other status still writes the JSON and exits 1. A missing gate directory exits 2 and does not invent PASS.

Statuses are only `PASS`, `FAIL`, `SKIP`, `BLOCKED`, and `NOT_RUN`.

- Missing step: `NOT_RUN` (`step_missing:<id>`).
- Missing or empty JUnit for a test step: `NOT_RUN` (`junit_missing:<id>` or `junit_empty:<id>`). Counts are null when the file is absent, not a successful zero.
- Claimed `SKIP`, or any skipped JUnit test (including a skipped element hidden behind `skipped="0"`): `SKIP`.
- `allow_failure: true` with exit 0: `BLOCKED` (`allow_failure:<id>`). With a non-zero exit: `FAIL`.
- Non-zero exit or JUnit failures/errors: `FAIL`.
- `artifacts/hashes.json` disagreeing with the sha256 of the files: `FAIL` (`artifact_hash_mismatch`).
- Build exited 0 but no artifact file: `BLOCKED` (`artifact_hashes_missing`).

`allow_failure` and a hidden skip cannot pass a required step.

## Runner pause and cleanup readback

The schema fields are:

- `runner_pause_readback.status`
- `runner_pause_readback.paused_after_use`
- `runner_pause_readback.readback`
- `runner_pause_readback.recorded_at`
- `runner_cleanup_readback.status`
- `runner_cleanup_readback.cleanup_confirmed`
- `runner_cleanup_readback.readback`
- `runner_cleanup_readback.recorded_at`

`runner/pause.json`:

```json
{
  "paused_after_use": true,
  "readback": "runner paused after the gate; active jobs 0",
  "recorded_at": "2026-10-09T06:00:00Z"
}
```

`runner/cleanup.json`:

```json
{
  "cleanup_confirmed": true,
  "readback": "job containers removed; cache only; no state mounts left",
  "recorded_at": "2026-10-09T06:01:00Z"
}
```

When `environment` is `gitlab_runner:<tag>`, a missing file, a false flag, or an empty readback refuses PASS. Missing files are `BLOCKED` with `runner_pause_readback_missing` or `runner_cleanup_readback_missing`. A false flag is `FAIL` (`runner_pause_readback_rejected` or `runner_cleanup_readback_rejected`).

`local_external` does not require those files. The output still includes both objects with status `NOT_RUN` and reason `not_required_for_local_external`.

## GitHub mirror fallback

GitHub Actions on a public mirror is a build-only fallback. It is not release evidence. This repository does not add a workflow that runs by default.

If a mirror workflow is added later, all of these are required:

- `workflow_dispatch` only. `push` and `pull_request` do not start it.
- Build only. It does not run the private monitor and it does not receive user keys.
- Every action is pinned to a full commit SHA.
- `permissions: contents: read`.
- No API keys, runner tokens, or notification recipients.
- Short artifact retention.

A green GitHub build does not turn a failed GitLab pipeline into a pass.

## What counts as release evidence

Acceptable release evidence is one controlled Linux environment running the full gate on the same source SHA. The document has to include:

- source SHA (`source_sha`, the job's `CI_COMMIT_SHA`)
- runtime content hash (`content_hash`)
- tool versions (`tool_versions`)
- exact commands (`commands[].command`) and exit codes
- JUnit for the Python and client test steps
- wheel/sdist sha256 (`artifact_hashes`) when the build step ran
- post-install functional and read-only negative checks
- public-sample probe provenance
- an explicit status of `PASS`, `FAIL`, `SKIP`, `BLOCKED`, or `NOT_RUN`

`execution_label` is `gitlab_runner:<tag>` or `local_external`. If the runner is unavailable, an equally isolated local gate may be used only when it is labelled `local_external` (`external_execution` true). That label means external execution, not a GitLab result.

Stable-scope required tests do not pass through a hidden skip or `allow_failure`. Missing evidence stays `NOT_RUN` or `BLOCKED`. It is not stored as zero, safe, or complete.

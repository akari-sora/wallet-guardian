# wg-box-ondemand runner profile

Runner-side profile for the operator's existing on-demand Linux host. This file does not register a runner, call the GitLab API, or start a container. The example config next to it is `ci/box-runner.config.example.toml`. Every secret there is a placeholder.

The evidence contract, trigger rules, and GitHub mirror fallback are in `wallet-guardian/docs/ci.md`.

## Dedicated account

Use one CI UID that is not the operator's daily account and not the monitor bot account. Placeholder: `PLACEHOLDER_UID`.

That user runs rootless Podman. GitLab Runner uses the Docker-compatible executor and connects to `PLACEHOLDER_ROOTLESS_PODMAN_API`. That address is how the runner daemon reaches Podman. It is not a volume mounted into the job. Do not mount the container engine socket, a runner token, monitor state, the bot home directory, or the whole workspace into the job.

## Tag and concurrency

Jobs reach this host only with the tag `wg-box-ondemand`.

The example sets `concurrent = 1`, runner `limit = 1`, and `request_concurrency = 1`. The CI jobs share `resource_group: wg-box-ondemand`, so the gate jobs do not overlap. Before unpausing, check the host lock and confirm no other gate is queued or running. Arbitrary external merge-request code does not run on this host next to private monitoring data.

## Fixed SHA

Leave the runner paused except for an approved commit.

Start a pipeline only when `WG_BOX_CI` is `1` and the source is web, api, or trigger, or when `WG_BOX_CI` is `1` on a protected branch. Fork merge-request pipelines are `when: never` in `.gitlab-ci.yml`. Each job checks that `CI_COMMIT_SHA` is non-empty and writes it to `gate-run/CI_COMMIT_SHA`. Compare that value with the approved commit before accepting evidence.

## Isolation

The example config is the boundary for every job on this tag:

- `privileged = false`
- `network_mode = "bridge"` so the job does not use host networking
- `volumes = ["/cache"]` and no other mount
- cgroup limits via `cpus`, `memory`, and `memory_swap`, with `oom_kill_disable = false`
- `cap_drop = ["ALL"]` and `security_opt = ["no-new-privileges"]`
- token text is `PLACEHOLDER_RUNNER_TOKEN`

`tls_verify = false` applies only to the local rootless API placeholder, not to a remote daemon.

## Pause and cleanup readback

After the gate finishes:

1. Pause the runner. Write `runner/pause.json` with `paused_after_use`, `readback`, and `recorded_at`.
2. Confirm job containers and scratch are gone. Write `runner/cleanup.json` with `cleanup_confirmed`, `readback`, and `recorded_at`.
3. Run `wallet-guardian/tools/release_evidence.py` on the gate directory.

For `execution_label` `gitlab_runner:wg-box-ondemand`, missing `runner_pause_readback` or `runner_cleanup_readback` is not PASS. `local_external` is the label when the same isolated gate runs off the GitLab runner; it must be labelled as external execution.

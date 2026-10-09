# Unattended box schedule (draft)

授權：AGPL-3.0-only（見 `LICENSE`）。

This file is an installer draft. It does not install a unit, enable a timer, turn on linger, or disable a Windows task. Nothing in this package starts a round by itself. The operator does that later, on a machine they control, after reading the plan from `guardian install-service --dry-run`.

The monitor stays read-only. Units call `run-once --no-send` or `watchdog`. They do not sign, approve, revoke, claim, or broadcast. Config names environment variables only. This document has no monitored addresses and no API keys.

The installed program is one fixed version. The unit records that version on `GUARDIAN_INSTALLED_VERSION` and calls an absolute executable. A round must not download `main` and must not run `uv sync` or otherwise refresh dependencies. A moving branch is not an installed version.

## What runs, separately

Three schedules stay in separate units. None of them waits for another, and the collector does not wait for an LLM chat to be idle.

| Unit | Role |
| --- | --- |
| `guardian-mcm-shadow.service` / `.timer` | MCM shadow round every 3 hours. `RandomizedDelaySec=0`. `Persistent=false`. |
| `guardian-legacy-collector.service` / `.timer` | Placeholder `ExecStart` for the operator's legacy command. |
| `guardian-watchdog.service` / `.timer` | Health check on its own timer. |

`Persistent=false` means systemd does not start a missed slot when the machine comes back. Turning persistence on does not mean every missed slot ran on time. One later process can fill data. It is not one on-time round per missed slot, and it is never a second on-time round for a slot that already counted.

## Slot rules

The timer's slot is the scheduled instant.

- The round starts on time only inside the slot plus or minus 30 minutes.
- Required scans and the legacy comparison must be sealed within 60 minutes after the slot.
- A missed slot, a timeout, a late start, a late seal, or missing evidence does not count.
- Missing evidence is `evidence_missing`. It is not safe, not complete, and not zero. A failed read is not a zero result.
- A later catch-up may fill data. It does not count as an on-time round. If the slot already counted once, the catch-up does not make a second on-time round.
- One writer holds the shadow lock. A second concurrent round is refused.

`TimeoutStartSec` kills a hung process. That timeout is a failure, not an on-time seal.

## Accounts and limits

Use three different accounts.

- The monitor account is the dedicated non-root user `guardian-monitor`. System units set `User=` to that account. It is not root.
- The CI account runs tests and releases. It does not own the monitor state directory.
- The coding workspace is where source is edited. It is not the config directory and not the state directory.

Draft resource limits on each service: `MemoryMax=512M`, `CPUQuota=50%`, `TimeoutStartSec=3600`, plus `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths` for the state directory only, and `PrivateTmp=yes`.

## Priority

Install the first option that is actually true on the host.

1. A systemd **user** timer, only after linger is yes **and** the user manager is up. Unit files alone do not survive logout.
2. Otherwise a systemd **system** service under the dedicated non-root monitor account.
3. Otherwise cron, and only when `cron` or `crond` is present. An untested or missing cron is not a fallback. The cron file does not chain the collector behind an LLM session.

`guardian install-service --dry-run` prints this choice. It does not call `systemctl enable`, `systemctl start`, or `crontab`. `--apply` is the install path. This draft does not run `--apply` for you. Without `--apply`, the command refuses to install.

## Linger check

Linger and the user manager are separate facts. Check both for the monitor account:

```bash
loginctl show-user guardian-monitor --property=Linger --property=State
```

- `Linger=yes` and `State=active` or `State=lingering` means the user timer can keep running after logout.
- `Linger=no`, or a state that is not a running user manager, refuses the user-timer plan. The plan warns and moves to the next option above.
- `loginctl enable-linger` is an operator command. This package does not run it.

A dry-run probes `loginctl` only. It does not call `systemctl` unless you pass `--apply`.

## Dry-run

From the fixed install, not from a fresh checkout of `main`:

```bash
guardian install-service --dry-run \
  --mcm-config "$GUARDIAN_CONFIG_DIR/monitor.json" \
  --output /tmp/guardian-units-preview
```

Read the printed plan. Rendered units under `--output` are a preview. Paths in the units are absolute. The exit status stays non-zero until an apply actually installs, and a dry-run apply is still a dry-run.

Confirm the preview before any enable:

- `ExecStart` is the absolute installed executable plus `--no-send` for the MCM unit.
- `GUARDIAN_INSTALLED_VERSION` matches the installed package, not `main`.
- No unit downloads code or syncs dependencies.
- Hardening keys listed above are present.
- `Persistent=false` and `RandomizedDelaySec=0` are set on the MCM timer.
- The legacy `ExecStart` is still the operator's own command.

## Common-cause failure

A watchdog on the same host as the collector cannot alert when the whole host is down. Power, disk, and kernel failures stop the collector and the watchdog together. That shared fate is a common-cause failure. Silence from the watchdog is not proof the host was healthy, and it is not proof a missed slot ran. This draft does not add an off-host notifier. Do not treat a same-host watchdog as coverage for a dead machine.

## Windows retirement checklist

The Windows scheduled task stays in place until the Linux host has replaced it. Disabling that task is an operator step. This package does not disable it.

Disable the Windows scheduled task only after all three of these are true:

1. Host smoke: the Linux units have run on the host and the dry-run plan matches the enabled timers.
2. State watermark: the shadow slot ledger has advanced for an on-time window, not merely a catch-up file.
3. Backup check: a backup of the state directory (including sqlite sidecars, if present) has been taken and a restore of that backup has been checked.

If any one of those is missing, leave the Windows task enabled. A catch-up row is not the watermark. A missed slot is not the watermark.

## What this draft does not do

It does not enable linger, write `/etc/systemd`, install a crontab, open a port, or send a notification. `sender_started` stays false. The same-host watchdog still cannot see a dead host. Qualification of 24 shadow windows is a separate record and is not granted by installing a timer.

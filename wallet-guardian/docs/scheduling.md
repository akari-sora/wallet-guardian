# Scheduling

`guardian install-service` prints a plan for three separate schedules. It does not sign, broadcast, or fetch code. The package version in this tree is `0.4.0rc2`. No unit has been installed on a host by this preview. Host smoke, including reboot and missed-slot recording on a real machine, is `NOT_RUN`.

The 72h shadow validation is not complete. A timer does not start that count. The formal series of 24 successive 3-hour windows has not been run.

## What the command does

```bash
guardian install-service --dry-run --mcm-config "$HOME/.config/guardian/monitor.json"
```

`--dry-run` prints JSON and does not enable units. The process exit code is non-zero until an apply really installs (`installed` is false). `--dry-run` wins if you also pass `--apply`: the run stays a dry run. Without `--apply`, `systemctl` is not called.

The JSON below is abbreviated to the fields that say whether anything was installed. A real payload also includes `commands`, `cron_detected`, `executable`, `linger`, `rendered`, `selected_files`, `version`, and `wrote`, and `planned_units` has six entries. Paths and the probed choice depend on the machine. `version` is the pinned package version, not a branch name. This example is the user-timer choice (`status` `ok`).

```json
{
  "applied": false,
  "dry_run": true,
  "installed": false,
  "sender_started": false,
  "systemctl_called": false,
  "fetches_code": false,
  "persistent_counts_as_on_time": false,
  "status": "ok",
  "schedule_mode": "systemd_user_timer",
  "cron_fallback": false,
  "offset_minutes": 5,
  "on_calendar": "*-*-* 0/3:05:00",
  "common_cause": "same-host watchdog cannot alert when the host is down",
  "planned_units": [
    {"name": "guardian-mcm-shadow.service", "action": "print_only"}
  ],
  "warnings": []
}
```

`schedule_mode` is chosen in this order:

1. `systemd_user_timer` when linger is yes, the user manager is up, and systemd is present. Status `ok`.
2. Otherwise `systemd_system_service` under the dedicated non-root user (`--user`, default `guardian-monitor`). Status `warn`.
3. Otherwise `cron`, only when `crontab` and `cron` or `crond` are present. Status `warn`.
4. Otherwise `none`. `--apply` then refuses with `no_scheduler_to_install`.

`planned_units` always names these six files, with `action` `print_only` on a dry run:

- `guardian-mcm-shadow.service` and `.timer`
- `guardian-legacy-collector.service` and `.timer`
- `guardian-watchdog.service` and `.timer`

The rendered MCM `ExecStart` is the absolute `guardian` plus `run-once --mcm-config <path> --no-send`. The watchdog `ExecStart` is `watchdog --mcm-config <path>`. The legacy unit's `ExecStart` is the operator command (`--legacy-executable`, default `/usr/local/sbin/guardian-legacy-collector`). `Persistent=false` and `RandomizedDelaySec=0` are set on the timers. The MCM and legacy calendars are `*-*-* 0/3:<offset>:00` (`--offset-minutes`, default 5, range 0 through 59). The watchdog calendar is `*:0/15`. Each service includes `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp=yes`, `MemoryMax=512M`, `CPUQuota=50%`, and `TimeoutStartSec=3600`.

`--output <directory>` writes the rendered text only under that directory. That is still not an install. `--apply` without `--dry-run` is the install path. This preview does not tell you to apply it. A same-host watchdog cannot see a dead host. `common_cause` in the JSON says that. Silence is not proof the machine was healthy.

A cron or systemd job has no shell profile. The CLI fills unset path variables from `env-paths.json` next to `monitor.json`. API keys still have to be in the environment or the secret profile the job can read. A missing key is `credential_missing`, not an empty wallet.

## systemd user timer

This is the first choice, and only when `loginctl show-user <user> --property=Linger --property=State` shows `Linger=yes` and `State=active` or `State=lingering`. The package does not run `loginctl enable-linger`. User-timer files go under that user's `~/.config/systemd/user` on apply. The user directive is omitted and `WantedBy` is `default.target`.

If linger or the user manager is missing, the plan warns (`linger_missing`, `user_manager_missing`, `systemd_user_timer_refused`) and moves to a system service. System units set `User=` and `Group=` to the monitor account, which cannot be root, and `WantedBy` is `multi-user.target`.

## cron

The fallback file is `guardian-unattended.cron`. The shipped lines are:

```text
<offset> */3 * * * <guardian> run-once --mcm-config <monitor.json> --no-send
<offset> */3 * * * <legacy-executable>
*/15 * * * * <guardian> watchdog --mcm-config <monitor.json>
```

`--apply` in cron mode reads `crontab -l` and replaces only the block between `# BEGIN wallet-guardian-monitor (managed block, do not edit)` and `# END wallet-guardian-monitor`. Other lines stay. A dry run does not call `crontab`.

## launchd (macOS)

`install-service` does not write a LaunchAgent or a LaunchDaemon. It probes `loginctl` and looks for `systemctl` plus `/run/systemd/system`, and for `crontab` together with `cron` or `crond`. A Mac does not have `/run/systemd/system`, so the plan is `cron` only when those cron binaries are present, and `none` otherwise. The read commands are the same ones the Linux units run. A manual LaunchAgent that you load yourself can call them. This plist is not produced by the CLI:

```xml
<key>ProgramArguments</key>
<array>
  <string>/absolute/path/to/guardian</string>
  <string>run-once</string>
  <string>--mcm-config</string>
  <string>/absolute/path/to/monitor.json</string>
  <string>--no-send</string>
</array>
<key>StartCalendarInterval</key>
<array>
  <dict>
    <key>Hour</key><integer>0</integer>
    <key>Minute</key><integer>5</integer>
  </dict>
</array>
```

Repeat the hour dict for 3, 6, 9, 12, 15, 18, and 21 if you want the same 3-hour grid as `OnCalendar=*-*-* 0/3:05:00`. A second agent, every 15 minutes, runs `watchdog --mcm-config` with the same monitor file. Keep the absolute paths. Do not point the agent at `uv sync` or a moving branch.

## Sleeping laptops and missed slots

A slot counts as on time only when the round starts within 30 minutes of the scheduled instant and the required scans plus the comparison are sealed within 60 minutes after the slot. `Persistent=false` means a systemd timer does not start a slot that was missed while the machine was asleep. `persistent_counts_as_on_time` in the dry-run JSON is false: a later start is not an on-time round for the slot that was skipped.

The slot ledger is `shadow-slots.json` (`guardian.shadow-slots.v1`) in the directory the recorder is given. A missed slot is stored with `reason` `missed` when the evaluator is called with the slot marked missed. A start more than 30 minutes late is `late_start`. A later catch-up may set `data_filled` and does not add a second on-time count. Those reasons are not rewritten into `on_time`. A sleeping laptop therefore leaves a missed slot on the record instead of a silent backfill. Host smoke of that recording on a real machine is `NOT_RUN`. The same rule is what a macOS schedule has to live with: a run after the lid opens does not turn the skipped slot into an on-time one.

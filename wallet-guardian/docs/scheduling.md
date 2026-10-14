# Scheduling

`guardian install-service` prints a plan for three separate schedules. It does not sign, broadcast, or fetch code. The package version in this tree is `0.4.0rc4`. No unit has been installed on a host by this preview. Host smoke, including reboot and missed-slot recording on a real machine, is `NOT_RUN`.

The 72h shadow validation is not complete. A timer does not start that count. The formal series of 24 successive 3-hour windows has not been run.

A news file written by another job is optional. `run-once --news-input <path>` reads it when the file is there. If the path is missing, unreadable, or larger than 2 MiB, the round records `news_unreadable` or `news_too_large` on `exposure_news` and continues the chain reads. That gap is not a zero and not a completed news check. This page does not switch the sender on.

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

On a systemd or cron plan, `planned_units` names these six files, with `action` `print_only` on a dry run:

- `guardian-mcm-shadow.service` and `.timer`
- `guardian-legacy-collector.service` and `.timer`
- `guardian-watchdog.service` and `.timer`

The rendered MCM `ExecStart` is the absolute `guardian` plus `run-once --mcm-config <path> --no-send`. The paired watchdog `ExecStart` is `watchdog --no-send --mcm-config <path>`. The legacy unit's `ExecStart` is the operator command (`--legacy-executable`, default `/usr/local/sbin/guardian-legacy-collector`). `Persistent=false` and `RandomizedDelaySec=0` are set on the timers. The MCM and legacy calendars are `*-*-* 0/3:<offset>:00` (`--offset-minutes`, default 5, range 0 through 59). The watchdog calendar is `*:0/15`. Each service includes `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp=yes`, `MemoryMax=512M`, `CPUQuota=50%`, and `TimeoutStartSec=3600`.

`--output <directory>` writes the rendered text only under that directory. That is still not an install. `--apply` without `--dry-run` is the install path. This preview does not tell you to apply it. A same-host watchdog cannot see a dead host. `common_cause` in the JSON says that. Silence is not proof the machine was healthy. `watchdog` does call the delivery path itself, so a configured sender can send pending outbox rows when `run-once` is not the process that notices. Default `outbox_only` still does not send. A `degraded` `run-once` exits 3, which is not the `failed` exit 1. A required gap that was already `error` or `blocked` is a known gap; a newly worse required key emits `monitor_new_gap` instead of being folded into that old episode.

A cron or systemd job has no shell profile. The CLI fills unset path variables from `env-paths.json` next to `monitor.json`. API keys still have to be in the environment or the secret profile the job can read. A missing key is `credential_missing`, not an empty wallet.

## systemd user timer

This is the first choice, and only when `loginctl show-user <user> --property=Linger --property=State` shows `Linger=yes` and `State=active` or `State=lingering`. The package does not run `loginctl enable-linger`. User-timer files go under that user's `~/.config/systemd/user` on apply. The user directive is omitted and `WantedBy` is `default.target`.

If linger or the user manager is missing, the plan warns (`linger_missing`, `user_manager_missing`, `systemd_user_timer_refused`) and moves to a system service. System units set `User=` and `Group=` to the monitor account, which cannot be root, and `WantedBy` is `multi-user.target`.

## cron

The fallback file is `guardian-unattended.cron`. The shipped lines are:

```text
<offset> */3 * * * <guardian> run-once --mcm-config <monitor.json> --no-send
<offset> */3 * * * <legacy-executable>
*/15 * * * * <guardian> watchdog --no-send --mcm-config <monitor.json>
```

`--apply` in cron mode reads `crontab -l` and replaces only the block between `# BEGIN wallet-guardian-monitor (managed block, do not edit)` and `# END wallet-guardian-monitor`. Other lines stay. A dry run does not call `crontab`.

## launchd (macOS)

`guardian install-service --dry-run --schedule-mode launchd_agent` prints three LaunchAgents. The plist text is generated. Nothing in this command loads it. The process exit code is non-zero, the same rule as a systemd dry-run, because `installed` stays false. `--apply` does not change that. This command never calls `launchctl`, never runs `launchctl load` or `launchctl bootstrap`, never edits `pmset`, and never asks for root. `--user` is not written into the agent. macOS may later show a background-item permission prompt if you load an agent yourself. This package does not answer that prompt.

`--schedule-mode auto` (the default) is the Linux choice above. It still probes `loginctl` and looks for `systemctl` plus `/run/systemd/system`, and for `crontab` together with `cron` or `crond`. `launchd_agent` does not run those probes. In that payload, `cron_detected` and `linger` are not measurements.

`--output <directory>` writes the three plist files only under that directory. It does not copy them into `~/Library/LaunchAgents` and it does not write the systemd units.

The files are:

| File | Command | `StartCalendarInterval` |
|---|---|---|
| `com.walletguardian.balance-probe.plist` | `balance-probe --mcm-config <monitor.json> --probe-state <state>/balance-probe.sqlite --no-send` | `Minute` `0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55`, no `Hour` |
| `com.walletguardian.run-once.plist` | `run-once --mcm-config <monitor.json> --no-send` | `Hour` `0, 3, 6, 9, 12, 15, 18, 21` and `Minute` `--offset-minutes` (default 5) |
| `com.walletguardian.watchdog.plist` | `watchdog --no-send --mcm-config <monitor.json>` | `Minute` `0, 15, 30, 45`, no `Hour` |

`balance-probe` is a read-only subcommand. The probe plist passes `--probe-state` as an absolute path, `<state directory>/balance-probe.sqlite`, because that flag is required. `--no-send` is required too. This command still does not load the agent.

Each `ProgramArguments` array starts with the absolute `guardian` path. The monitor path and the probe-state path are absolute. `run-once` and `watchdog` use the same `--no-send` forms as the systemd and cron units. No argument is `uv sync`. The plists also set `GUARDIAN_CONFIG_DIR`, `GUARDIAN_STATE_DIR`, and `GUARDIAN_INSTALLED_VERSION`. Those are the same facts the systemd units put in `Environment=`.

Schedules use `StartCalendarInterval`, not `StartInterval`. The man page is [launchd.plist(5)](https://keith.github.io/xcode-man-pages/launchd.plist.5.html). A `Minute` list with no `Hour` repeats every hour at those minutes. The probe list is the 5-minute grid. The watchdog list is the 15-minute grid. The run-once dicts are the same 3-hour grid as `OnCalendar=*-*-* 0/3:05:00` when the offset is 5.

Sleep, from that man page: if several `StartCalendarInterval` times pass while the computer is asleep, launchd coalesces them into one event on wake. That wake run is not an on-time slot. `persistent_counts_as_on_time` stays false. A missed slot stays missed. The same page says a `StartInterval` firing that occurs during sleep is missed and is not coalesced on wake. If a generated or supplied plist uses `StartInterval`, the plan warns that sleep will skip runs.

A path with a space or brackets is one XML string. `&`, `<`, and `>` are escaped, and `[` `]` are numeric character references, so the path is not split and the file stays well-formed. Do not point an agent at a moving branch.

## Sleeping laptops and missed slots

A slot counts as on time only when the round starts within 30 minutes of the scheduled instant and the required scans plus the comparison are sealed within 60 minutes after the slot. `Persistent=false` means a systemd timer does not start a slot that was missed while the machine was asleep. `persistent_counts_as_on_time` in the dry-run JSON is false: a later start is not an on-time round for the slot that was skipped.

The slot ledger is `shadow-slots.json` (`guardian.shadow-slots.v1`) in the directory the recorder is given. A missed slot is stored with `reason` `missed` when the evaluator is called with the slot marked missed. A start more than 30 minutes late is `late_start`. A later catch-up may set `data_filled` and does not add a second on-time count. Those reasons are not rewritten into `on_time`. A sleeping laptop therefore leaves a missed slot on the record instead of a silent backfill. Host smoke of that recording on a real machine is `NOT_RUN`. The same rule is what a macOS schedule has to live with: [launchd.plist(5)](https://keith.github.io/xcode-man-pages/launchd.plist.5.html) may coalesce missed `StartCalendarInterval` slots into one run on wake, and that run does not turn the skipped slots into on-time ones.

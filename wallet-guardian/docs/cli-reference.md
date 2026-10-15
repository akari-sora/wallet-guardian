# Command reference

Package version in this tree: `0.4.0rc4`. Each fenced block is the output of that command's `--help`. JSON is printed for every command. `--json` is accepted and does not turn that off.

The monitor is read-only. `run-once --no-send` still contacts the read endpoints you configured. It does not sign, build a transaction, or broadcast, and it does not hand the outbox to a sender. `balance-probe` is the same kind of read: `--no-send` is required, and it does not start a sender. Setup steps are in [guide-cli.md](guide-cli.md).

A nested command (`config validate`, `alerts test`, `action plan`) has its own section. `{formal,preview}` and `{system,doh}` on `egress-doctor` are flag choices, not commands.

## `guardian`

```text
usage: guardian [-h]
                {doctor,run-once,balance-probe,snapshot,reconcile,claims,providers,wallets,approvals,explain,coverage,replay,alerts,self-check,diagnose,actions,action,rescue,operation-coverage,workbench,config,sources,export,compare-legacy,news,legacy-capture,outbox,watchdog,monitor,egress-doctor,init,install-service}
                ...

positional arguments:
  {doctor,run-once,balance-probe,snapshot,reconcile,claims,providers,wallets,approvals,explain,coverage,replay,alerts,self-check,diagnose,actions,action,rescue,operation-coverage,workbench,config,sources,export,compare-legacy,news,legacy-capture,outbox,watchdog,monitor,egress-doctor,init,install-service}
    init                write a read-only monitor profile
    install-service     print a dry-run install plan; units stay disabled without --apply

options:
  -h, --help            show this help message and exit
```

## `guardian doctor`

```text
usage: guardian doctor [-h] [--db DB] [--tenant TENANT] [--json] [--mcm-config MCM_CONFIG] [--live]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --live                live probe with a 5 second whole-call deadline per probe;
                        omitted doctor stays offline
```

Offline unless `--live` is passed. `--live` runs a provider probe and stops each probe at a 5 second whole-call deadline. That bound is remaining time: a later request in that probe does not start, and the open client is closed. Unusable resolver settings are reported and not probed. When they load, connections go to the validated address and the original hostname stays on the Host header, SNI, and the certificate check. It does not qualify egress. DoH enabled is not a qualified path. With `--mcm-config`, credential lines name the variable, `selected_from` (`profile`, `env`, or `absent`), `present`, and `shadowed`. They do not print values. `--live` with `--mcm-config` is a legacy provider probe: it does not prove the selected profile's connectivity.

## `guardian run-once`

```text
usage: guardian run-once [-h] [--db DB] [--tenant TENANT] [--json] (--config CONFIG | --mcm-config MCM_CONFIG) [--no-send] [--news-input NEWS_INPUT] [--price-input PRICE_INPUT]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --config CONFIG
  --mcm-config MCM_CONFIG
  --no-send
  --news-input NEWS_INPUT
                        caller-supplied RSS, Atom, or structured news; not fetched
  --price-input PRICE_INPUT
                        caller-supplied price quotes; not fetched
```

## `guardian balance-probe`

```text
usage: guardian balance-probe [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG --probe-state PROBE_STATE [--no-send] [--trigger {run-once,none}]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --probe-state PROBE_STATE
  --no-send
  --trigger {run-once,none}
```

`--no-send` is the only delivery mode. Omitting it exits 2. The probe reads one head, checks Multicall3 code, then reads native balances and two topic-OR `eth_getLogs` filters. It writes no alerts and does not advance `history.*` cursors. When the log route is a gap, a native balance change already read in that round still records a `balance_change` trigger for that chain; the row stays `failure: logs` and the probe cursor does not advance. `--trigger none` records a change and does not start `run-once`. The default `run-once` starts at most one `run-once --no-send` when a new change is still outstanding. A second probe, or a `run-once` that already holds its own lock, is skipped and recorded. This command does not change the three-hour `run-once` interval or the shadow lock.

## `guardian snapshot`

```text
usage: guardian snapshot [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian reconcile`

```text
usage: guardian reconcile [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian claims`

```text
usage: guardian claims [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian providers`

```text
usage: guardian providers [-h] [--db DB] [--tenant TENANT] [--json] {verify} ...

positional arguments:
  {verify}
    verify              legacy provider probe; does not prove a selected
                        profile's connectivity

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian providers verify`

```text
usage: guardian providers verify [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

`providers verify` is a legacy provider probe. It does not prove that a selected monitor profile can reach its configured sources.

## `guardian wallets`

```text
usage: guardian wallets [-h] [--db DB] [--tenant TENANT] [--json] {discover} ...

positional arguments:
  {discover}

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian wallets discover`

```text
usage: guardian wallets discover [-h] [--db DB] [--tenant TENANT] [--json] --config CONFIG

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
  --config CONFIG
```

## `guardian approvals`

```text
usage: guardian approvals [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian explain`

```text
usage: guardian explain [-h] [--db DB] [--tenant TENANT] [--json] --event-id
                        EVENT_ID [--mcm-config MCM_CONFIG] [--refresh]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --event-id EVENT_ID
  --mcm-config MCM_CONFIG
  --refresh             re-read historical state through --mcm-config; read-only, no send
```

## `guardian coverage`

```text
usage: guardian coverage [-h] [--db DB] [--tenant TENANT] [--json] [--mcm-config MCM_CONFIG]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
```

## `guardian replay`

```text
usage: guardian replay [-h] [--db DB] [--tenant TENANT] [--json] --fixture FIXTURE [--no-send]

options:
  -h, --help         show this help message and exit
  --db DB            sqlite path
  --tenant TENANT    local tenant scope (required when ambiguous)
  --json             print JSON (this is always on)
  --fixture FIXTURE
  --no-send
```

## `guardian alerts`

```text
usage: guardian alerts [-h] [--db DB] [--tenant TENANT] [--json] {test} ...

positional arguments:
  {test}

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian alerts test`

```text
usage: guardian alerts test [-h] [--db DB] [--tenant TENANT] [--json] --dry-run

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
  --dry-run
```

## `guardian self-check`

```text
usage: guardian self-check [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian diagnose`

```text
usage: guardian diagnose [-h] [--db DB] [--tenant TENANT] [--json] --task TASK

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
  --task TASK
```

## `guardian actions`

```text
usage: guardian actions [-h] [--db DB] [--tenant TENANT] [--json] {list} ...

positional arguments:
  {list}

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian actions list`

```text
usage: guardian actions list [-h] [--db DB] [--tenant TENANT] [--json] --wallet WALLET

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
  --wallet WALLET
```

## `guardian action`

```text
usage: guardian action [-h] [--db DB] [--tenant TENANT] [--json] {plan,simulate,export,status,reconcile} ...

positional arguments:
  {plan,simulate,export,status,reconcile}

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
```

## `guardian action plan`

```text
usage: guardian action plan [-h] [--db DB] [--tenant TENANT] [--json] --intent-file INTENT_FILE

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --intent-file INTENT_FILE
```

## `guardian action simulate`

```text
usage: guardian action simulate [-h] [--db DB] [--tenant TENANT] [--json] --plan-id PLAN_ID

options:
  -h, --help         show this help message and exit
  --db DB            sqlite path
  --tenant TENANT    local tenant scope (required when ambiguous)
  --json             print JSON (this is always on)
  --plan-id PLAN_ID
```

## `guardian action export`

```text
usage: guardian action export [-h] [--db DB] [--tenant TENANT] [--json] --plan-id PLAN_ID --unsigned-only

options:
  -h, --help         show this help message and exit
  --db DB            sqlite path
  --tenant TENANT    local tenant scope (required when ambiguous)
  --json             print JSON (this is always on)
  --plan-id PLAN_ID
  --unsigned-only
```

## `guardian action status`

```text
usage: guardian action status [-h] [--db DB] [--tenant TENANT] [--json] --task-id TASK_ID

options:
  -h, --help         show this help message and exit
  --db DB            sqlite path
  --tenant TENANT    local tenant scope (required when ambiguous)
  --json             print JSON (this is always on)
  --task-id TASK_ID
```

## `guardian action reconcile`

```text
usage: guardian action reconcile [-h] [--db DB] [--tenant TENANT] [--json] --task-id TASK_ID

options:
  -h, --help         show this help message and exit
  --db DB            sqlite path
  --tenant TENANT    local tenant scope (required when ambiguous)
  --json             print JSON (this is always on)
  --task-id TASK_ID
```

## `guardian rescue`

```text
usage: guardian rescue [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian operation-coverage`

```text
usage: guardian operation-coverage [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian workbench`

```text
usage: guardian workbench [-h] [--db DB] [--tenant TENANT] [--json]

options:
  -h, --help       show this help message and exit
  --db DB          sqlite path
  --tenant TENANT  local tenant scope (required when ambiguous)
  --json           print JSON (this is always on)
```

## `guardian config`

```text
usage: guardian config [-h] {validate} ...

positional arguments:
  {validate}

options:
  -h, --help  show this help message and exit
```

## `guardian config validate`

```text
usage: guardian config validate [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
```

## `guardian sources`

```text
usage: guardian sources [-h] {probe} ...

positional arguments:
  {probe}

options:
  -h, --help  show this help message and exit
```

## `guardian sources probe`

```text
usage: guardian sources probe [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
```

## `guardian export`

```text
usage: guardian export [-h] [--db DB] [--tenant TENANT] [--json] [--public] --mcm-config MCM_CONFIG

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --public
  --mcm-config MCM_CONFIG
```

## `guardian compare-legacy`

```text
usage: guardian compare-legacy [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG --legacy-state LEGACY_STATE [--since SINCE] [--until UNTIL] [--legacy-runs-dir LEGACY_RUNS_DIR]
                               [--slot-start SLOT_START] [--slot-end SLOT_END] [--legacy-max-age-seconds LEGACY_MAX_AGE_SECONDS]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --legacy-state LEGACY_STATE
  --since SINCE         ISO window start
  --until UNTIL         ISO window end
  --legacy-runs-dir LEGACY_RUNS_DIR
  --slot-start SLOT_START
  --slot-end SLOT_END
  --legacy-max-age-seconds LEGACY_MAX_AGE_SECONDS
```

## `guardian news`

```text
usage: guardian news [-h] {health} ...

positional arguments:
  {health}

options:
  -h, --help  show this help message and exit
```

## `guardian news health`

```text
usage: guardian news health [-h] [--db DB] [--tenant TENANT] [--json]
                            --mcm-config MCM_CONFIG [--offline]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --offline             read stored feed health and do not fetch
```

## `guardian legacy-capture`

```text
usage: guardian legacy-capture [-h] {begin,record,finish} ...

positional arguments:
  {begin,record,finish}

options:
  -h, --help            show this help message and exit
```

## `guardian legacy-capture begin`

```text
usage: guardian legacy-capture begin [-h] --runs-dir RUNS_DIR --run-id RUN_ID --legacy-state LEGACY_STATE --tool-name TOOL_NAME --tool-version TOOL_VERSION --tool-hash TOOL_HASH --host-id HOST_ID
                                     --scope-hash SCOPE_HASH --account ACCOUNT --started-at STARTED_AT

options:
  -h, --help            show this help message and exit
  --runs-dir RUNS_DIR
  --run-id RUN_ID
  --legacy-state LEGACY_STATE
  --tool-name TOOL_NAME
  --tool-version TOOL_VERSION
  --tool-hash TOOL_HASH
  --host-id HOST_ID
  --scope-hash SCOPE_HASH
  --account ACCOUNT     label=<sha256 of the identity key>
  --started-at STARTED_AT
```

## `guardian legacy-capture record`

```text
usage: guardian legacy-capture record [-h] --runs-dir RUNS_DIR --run-id RUN_ID [--window-json WINDOW_JSON] [--event-json EVENT_JSON]

options:
  -h, --help            show this help message and exit
  --runs-dir RUNS_DIR
  --run-id RUN_ID
  --window-json WINDOW_JSON
  --event-json EVENT_JSON
```

## `guardian legacy-capture finish`

```text
usage: guardian legacy-capture finish [-h] --runs-dir RUNS_DIR --run-id RUN_ID --legacy-state LEGACY_STATE --exit-code EXIT_CODE --finished-at FINISHED_AT

options:
  -h, --help            show this help message and exit
  --runs-dir RUNS_DIR
  --run-id RUN_ID
  --legacy-state LEGACY_STATE
  --exit-code EXIT_CODE
  --finished-at FINISHED_AT
```

## `guardian outbox`

```text
usage: guardian outbox [-h] {pending,ack} ...

positional arguments:
  {pending,ack}

options:
  -h, --help     show this help message and exit
```

## `guardian outbox pending`

```text
usage: guardian outbox pending [-h] [--db DB] [--tenant TENANT] [--json]
                               --mcm-config MCM_CONFIG

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
```

## `guardian outbox ack`

```text
usage: guardian outbox ack [-h] [--db DB] [--tenant TENANT] [--json]
                           --mcm-config MCM_CONFIG --id ID

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --id ID
```

## `guardian watchdog`

```text
usage: guardian watchdog [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG [--no-send]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --no-send
```

## `guardian monitor`

```text
usage: guardian monitor [-h] {plan} ...

positional arguments:
  {plan}

options:
  -h, --help  show this help message and exit
```

## `guardian monitor plan`

```text
usage: guardian monitor plan [-h] [--db DB] [--tenant TENANT] [--json] --mcm-config MCM_CONFIG

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
```

## `guardian egress-doctor`

```text
usage: guardian egress-doctor [-h] [--db DB] [--tenant TENANT] [--json] [--mcm-config MCM_CONFIG] [--egress-config EGRESS_CONFIG] [--qualification {formal,preview}] [--mode {system,doh}]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --mcm-config MCM_CONFIG
  --egress-config EGRESS_CONFIG
  --qualification {formal,preview}
  --mode {system,doh}
```

## `guardian init`

```text
usage: guardian init [-h] [--db DB] [--tenant TENANT] [--json] [--dir DIRECTORY] [--from ANSWERS_FROM] [--yes] [--force]

options:
  -h, --help           show this help message and exit
  --db DB              sqlite path
  --tenant TENANT      local tenant scope (required when ambiguous)
  --json               print JSON (this is always on)
  --dir DIRECTORY      private directory (default: XDG config dir)
  --from ANSWERS_FROM  non-interactive answers JSON
  --yes                record data-sharing consent and write files
  --force              back up existing files first; does not touch the state database
```

## `guardian install-service`

```text
usage: guardian install-service [-h] [--db DB] [--tenant TENANT] [--json] [--dry-run] [--apply] [--schedule-mode {auto,launchd_agent}] [--mcm-config MCM_CONFIG] [--output OUTPUT]
                                [--executable EXECUTABLE] [--config-dir CONFIG_DIR] [--state-dir STATE_DIR] [--user USER] [--offset-minutes OFFSET_MINUTES] [--legacy-executable LEGACY_EXECUTABLE]

options:
  -h, --help            show this help message and exit
  --db DB               sqlite path
  --tenant TENANT       local tenant scope (required when ambiguous)
  --json                print JSON (this is always on)
  --dry-run             print the plan and do not enable units
  --apply               install rendered units; refused when omitted
  --schedule-mode {auto,launchd_agent}
                        auto (default) selects systemd or cron; launchd_agent prints three LaunchAgents and does not load them
  --mcm-config MCM_CONFIG
  --output OUTPUT       write rendered units only under this directory
  --executable EXECUTABLE
                        absolute path of the installed guardian executable
  --config-dir CONFIG_DIR
  --state-dir STATE_DIR
  --user USER
  --offset-minutes OFFSET_MINUTES
  --legacy-executable LEGACY_EXECUTABLE
```

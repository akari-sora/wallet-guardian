## Version

- Package version (wheel filename, or `environment.guardian_version` from `guardian doctor --json`):
- `guardian --version`: this preview's `guardian --help` does not list a `--version` flag. If you ran it, paste the output here. Use `environment.guardian_version` as the version when the flag is absent.

## What happened

## What you expected

## Command

The `guardian` command you ran, with secrets removed. `run-once` for a profile from `init` is `guardian run-once --mcm-config <path> --no-send`.

## Coverage

Paste redacted output of `guardian coverage --mcm-config <your monitor file>`.

Do not paste API keys, seed phrases, private keys, keystores, keyed RPC URLs, notification recipients, or real watched addresses. If you need to show the shape of an address, use the synthetic address in `wallet-guardian/examples/answers.example.json`.

## Notes

A gap (`credential_missing`, `partial`, `unknown`, `identity_unreadable`, `rate_limited`, `plan_not_supported`, `feed_not_supplied`) is not a zero balance. Say which `reason_codes` you saw.

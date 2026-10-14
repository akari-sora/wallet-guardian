# Balance probe

`guardian balance-probe` is implemented. It is read-only. It does not sign, build a transaction, or broadcast. It does not open a websocket and it does not start a sender. `--no-send` is the only delivery mode: omitting it exits 2. No other delivery flag exists.

```text
guardian balance-probe --mcm-config PATH --probe-state PATH --no-send [--trigger run-once|none]
```

`--trigger` defaults to `run-once`. `none` records a change and does not start `run-once`. The command does not add a scheduler, and it does not change the three-hour `run-once` interval or shadow-slot rules. The probe lock is `<probe-state>.balance-probe.lock`. It is not `.shadow-round.lock` and it is not the run-once lock. A second probe misses that lock and records `balance_probe_locked`. If `run-once` already holds its own lock, the probe does not start another `run-once` and records `run_once_locked`.

The probe writes no alerts. It does not advance `history.native_external` or `history.native_internal`. Those cursors stay on their own rows. Probe cursors and trigger records are the `probe_cursors` and `probe_trigger_records` tables from migration `0011_balance_probe`. `PRAGMA user_version` stays 1.

## What one round does

For each enabled EVM network that has a watched wallet and an `evm_rpc` source:

1. `eth_getBlockByNumber("latest")` for the number and hash.
2. `eth_getCode` on Multicall3 `0xcA11bde05977b3631167028862bE2a173976CA11`, bound to that block hash. Empty code (`0x`, `0x0`, or all-zero bytes) falls back to one `eth_getBalance` per wallet. A failed code read does not fall back. No code hash is stored or compared.
3. When code is present, one `eth_call` of `aggregate3(getEthBalance × N)` at that block hash. Selectors are computed from `aggregate3((address,bool,bytes)[])` and `getEthBalance(address)`. Each item sets `allowFailure` so a failed item comes back as that item. A failed item is a failed read for that wallet, not a zero balance. A successful zero word is a real zero.
4. `eth_getLogs` for incoming `Transfer` logs: topic0 is Transfer, topic2 is the watched addresses OR-ed. The wallet is not placed in the `address` filter.
5. `eth_getLogs` for outgoing transfers and approvals: topic0 is Transfer, Approval, ApprovalForAll, and the Permit2 events, OR-ed, and topic1 is the watched addresses OR-ed.

A quiet native balance is committed with the same versioned baseline as `run-once`. The first valid observation is version 1 and does not alert. A changed raw balance is not written by the probe, so the triggered `run-once` still sees the delta. A failed read does not write zero and does not bump the version.

A Transfer log, an approval-family log, or a changed raw balance records a trigger for that chain only. The same fingerprint is not recorded again when the head hash changes; the next round still reads. `--trigger run-once` then starts at most one `run-once --no-send` for outstanding triggers. `--trigger none` does not start it.

Head, code, multicall, per-wallet balance, or log failure: no trigger, no probe-cursor advance, no zero written.

The optional nonce batch and a second header for `latest − k` are not issued. Either one would push a quiet Multicall3 round past the five-HTTP ceiling below. The bound block is the single `latest` head this round read.

## Shared reducer

One observation is fetched outside the database transaction. The commit then takes a short immediate transaction and requires the expected baseline version. The first valid observation writes version 1 and does not alert. The probe uses `plan_transition` for that decision. A raw balance that differs from the stored baseline is not committed here, so the triggered `run-once` still computes the delta. A same-balance observation at a later block commits with no alert. A lower block is discarded. A failed read does not write zero and does not bump the version. History cursors stay on their own rows.

## Request budget

This is an engineering estimate, not a provider quota and not a promise. The nonce batch from the earlier design is not part of this command.

A quiet Multicall3 round is 5 HTTP calls and 5 methods per chain: head, `eth_getCode`, one `aggregate3`, and two `eth_getLogs`. That is within 5 HTTP and within `4 + N` methods for one or more wallets. The method count does not grow with N on this path. An empty-code fallback adds one `eth_getBalance` per wallet instead of the single `eth_call`.

Count the existing three-hour `run-once` monitor as well. Every endpoint that shares the provider account shares the budget. This command does not add a scheduler and does not change the monitor interval.

## Not run

These acceptance items are operator work. This change does not call a live RPC and does not claim them.

- **NOT_RUN.** Live 24 hour measurement on an independent state directory, with `--trigger none`, against public high-activity contracts (Base and OP `0x4200000000000000000000000000000000000006`, BSC WBNB). Detection latency is probe submit time minus the event block timestamp. The bar is P95 ≤ interval + 60 seconds, HTTP 429 under 1%, and total requests within ±20% of the budget table in `docs/reviews/2026-10-14-latency-coverage.md` section 6.
- **NOT_RUN.** After cutover, or during shadow, `compare-legacy` pairing of the same event's legacy alert time and MCM outbox time. The bar is a P95 at least 2 hours faster than the legacy patrol.

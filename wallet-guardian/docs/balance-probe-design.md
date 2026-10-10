# Balance probe design

This note describes a future read-only command. It does not add the command, a scheduler, or a sender. `guardian balance-probe` is not in the CLI.

The shape, when it is built, is:

```text
guardian balance-probe --mcm-config PATH --no-send
```

`--no-send` is the only delivery mode this design allows. The command would call the same native-balance reducer as `run-once` and the same database. It would not keep a second baseline.

## Shared reducer

One observation is fetched outside the database transaction. The commit then takes a short immediate transaction and requires the expected baseline version. The first valid observation writes version 1 and does not alert. If the stored version moved:

- a stored checkpoint at a higher block wins, and the older observation is discarded
- a stored checkpoint at a lower block is the new baseline, and the delta is computed again

A failed read does not write zero and does not bump the version. History cursors stay on their own rows. A balance probe must not advance `history.native_external` or `history.native_internal`.

Cheap means one native balance read per watched wallet, plus the shared chain-id and header reads for that round. It does not mean one RPC for the whole monitor.

## Request budget

This is an engineering estimate, not a provider quota and not a promise.

For one wallet, one round is 2 balance reads plus 2 to 4 shared validation reads (chain id, headers, the before/after hash check, and an optional nonce). That is 4 to 6 requests per wallet per round before retries. At 288 rounds in a day (one every five minutes) that is 1,152 to 1,728 requests per wallet per day before retries. A quote lookup is cached for the round, not repeated per wallet.

Count the existing three-hour `run-once` monitor as well. Every endpoint that shares the provider account shares the budget. This design does not add a scheduler and does not change the monitor interval.

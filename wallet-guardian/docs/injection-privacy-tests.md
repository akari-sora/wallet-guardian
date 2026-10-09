# Injection and privacy tests

Package `test/injection-and-privacy`. These tests call `guardian.cli.main` with a fixture transport. They do not open a socket.

## Privacy boundary

"Never send or store a monitored address anywhere" does not fit an address monitor. The rule in `guardian.security.privacy_boundary` is:

| Destination | Wallet address | API credential |
| --- | --- | --- |
| Approved chain RPC or indexer | Necessary configured queries | Only on that indexer's own request |
| News, promotions, and non-consented risk providers | No address and no wallet-derived query | No |
| Private accounts and state | Necessary address data, owner-only permissions | No |
| Public export, routine diagnostics, package artifacts | No | No |
| Outbox, event, and error logs | Only on an authorized private output | No |

A public protocol pin is not a monitored wallet. Tests keep a fake secret and a unique synthetic address, and they look for plain and encoded forms.

## What untrusted text may change

News titles, token names, and other retrieved text are data. They may appear only in bounded display fields such as `detail`. They must not change policy hashes, trusted sources, thresholds, the notification recipient, endpoints, filesystem paths, execution flags, or safety verdicts. Removing the words "ignore previous instructions" is not the control. Each sink escapes for its own format (`guardian.security.display_text`).

A malformed feed item is quarantined. A well-formed sibling in the same document is kept. An oversized body, a compressed body, a DOCTYPE or entity declaration, and a truncated document are explicit failures. They are not an empty wallet and not a safe result. Failed reads stay failed. `sent` stays 0 unless the operator opted into a sender.

## Tests

| ID | Function |
| --- | --- |
| SAFE-01 | `test_mcm_safe_01_policy_immutability` |
| SAFE-02 | `test_mcm_safe_02_sink_second_order_injection` |
| SAFE-03 | `test_mcm_safe_03_destination_privacy` |
| SAFE-04 | `test_mcm_safe_04_clean_installed_cli` |
| SAFE-05 | `test_mcm_safe_05_parser_bounds_and_zero_sends` |

## Residual limits (stated, not hidden)

- The HTML sink escapes text that ingest already escaped. This is safe and cosmetic.
- The protocol matcher drops a title that has a token mixing digits and letters (for example "Aave v3 incident" can be missed). This is a false-negative limit, not only an attacker lever. It is recorded separately and not fixed here.
- `privacy_boundary.py` is a policy table, not universal enforcement. Test destinations should come from registered sources, not hostname substrings.
- Escaping and canary tests do not establish general prompt-injection immunity for any downstream language model.
- Quarantined feed items are counted in the news state (`quarantined_items`) and the fetch result reason.
- The clean-install test runs with a minimal environment and `UV_OFFLINE=1`. If dependencies are not pre-staged in the uv cache, the gate is unmet and the test fails. It never retries online.

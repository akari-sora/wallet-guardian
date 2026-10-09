# Yield promotions (fixture package)

Status: parser and local comparison only. `ENABLED` is false. There is no scheduler, no live fetch, and no send. `run-once` does not import this package, and the CLI is unchanged.

This document is the package note. The earlier plan stays with its owner.

## What a run does

`guardian.yields.state.evaluate` reads two fixture documents through `ReadOnlyTransport` and a caller-supplied backend. The hosts are `yields.invalid` (DefiLlama-shaped pool list) and `promotions.invalid` (catalog 93 title list). An optional promotion body is caller-supplied bytes. It is not a third fetch, and links inside it are counted and not requested.

The clock is injected. The same UTC day does not fetch again. A stored validator is sent as `If-None-Match` or `If-Modified-Since`. A 304 with no stored body is `cache_missing` / `304_without_cache` and does not become a zero sample. A failed read does not replace or zero a previous record.

Decompressed JSON is capped at 16 MiB, with a nesting cap and a record cap. NaN, non-finite numbers, and truncated JSON fail the document. A gzip member must finish, including its trailer. A second gzip member is trailing data and fails the document. A nesting or decoder failure is a rejection reason, not an uncaught exception. A failure reason is not an empty success, and it does not replace a previous valid observation.

## Matching

Pool ids are UUIDs in `config/yields/pool-mapping.v1.json`. They are not addresses. A missing mapping means no matched candidate. Publisher `chain` and `project` strings are not copied onto the stored identity.

A held position is a positive raw amount on the mapped protocol, chain, and at least one mapped contract. The same chain and one overlapping contract from a different protocol is not a hold. `yield_change` is produced only for an observed pool sample in that case, after the noise rule below, and it is a candidate on the report. It is not written to the alert outbox.

An `explicit_body` announcement does not enter that pool comparison. When it matches a held campaign, the first time its semantic is seen the report gains one `promotion_event`. The semantic is a hash of the extracted terms and the stated figures. The same semantic does not emit again. A `promotion_event` is not a `yield_change`. It is not scheduled and it is not sent.

A standing approval is permission exposure, not proof of a deposit or promotion eligibility. Eligibility stays unknown.

A trusted feed that names a held protocol is not sufficient without a supported campaign or product mapping. A campaign contract that is not the mapped contract is spoofed. "Verified" here means the address is listed in this mapping. It is not an audit and not a safety badge.

## Yield language

Stored labels are "reported base yield", "reported incentive/reward yield", and "reported total yield". Null breakdowns stay null. A missing total is not the sum of the parts. APR is not converted to APY. `apr_converted_to_apy` is false and `compounding_model` is null.

Incentive-heavy numbers are not described as temporary unless the accepted body states an end or a lock duration. A negated or conflicting clause is not stored as an affirmative minimum, lock, or deadline. KYC, new-user, and the other eligibility limits this parser does not structure stay `residual_conditions` `unknown`. Each extracted field keeps its own quoted provenance. A title is not a body. Catalog 93 is undocumented here, article detail is `not_observable`, and a title date is not a deadline. Publication time is not a deadline. Ambiguous phrases stay unknown.

Hold-to-Earn HTML is `blocked` with reason `waf_challenge_not_bypassed`. It is not fetched.

## Noise policy

The default product rule, not a risk guarantee: a component moves by at least 2 percentage points and 25 percent relative, on two distinct fresh UTC days, in the same direction versus the current baseline. The compared samples must share one known unit, APY or APR. An unknown unit, or a unit that differs from the stored sample, resets the comparison to a new baseline. That reset does not confirm a pending move and it does not emit `yield_change`. Zero to nonzero uses the absolute test only, still at 2 points. Decreases count. The first fetch is a quiet baseline. At most one yield candidate is emitted per wallet, pool, and UTC day. The same semantic revision does not emit again. Null to a number, and a number to null, are not numeric changes.

Thresholds are stored on the state document and can be replaced by the caller. They stay a product filter. They do not turn a reading into income, and they do not change a null into a number.

## Offline entry

```python
from guardian.yields import evaluate

evaluate(database, tenant, clock=clock, backend=backend, resolve=resolve, holdings=holdings)
```

`backend` must be a fixture backend. A live HTTP backend and any `sender` are refused before a read. `tests/integration/test_mcm_yld_promotions.py` is a parser and state integration suite, not a CLI acceptance suite. The `test_mcm_yld_01` through `test_mcm_yld_05` groups cover mapping, the noise series, terms, the caps, and the absence of live fetches and sends.

Experimental offline fixture/parser component, disabled by default, with no scheduler, live ingestion or delivery. Extracted dates and terms are partial machine interpretations, not confirmation of eligibility, safety, completeness or current offer availability. Verify original issuer terms before acting.

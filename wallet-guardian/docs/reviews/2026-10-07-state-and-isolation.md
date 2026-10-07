# Review: state integrity, local tenant isolation, and unsigned preview

Reviewed base: `61885b98f8fb3b162e667a72849dd3fbd9bd4e93`.
Branch: `fix/review-state-and-isolation`.
Date: 2026-10-07.

## Verdict

This is a read-only prototype and an unsigned preview workbench, not an approved wallet-execution service. This change fixes concrete defects without turning on wallet requests, background signing, public deployment, or notification delivery. Keep the MR in Draft until the complete locked-dependency tests and build pass. Passing helper tests does not establish live chain coverage, a functioning scheduler, or wallet compatibility.

## Findings addressed

| ID | Priority | Original defect | Change |
| --- | --- | --- | --- |
| R1 | P1 | A failed wallet contributes no items while another successful wallet causes an incomplete replacement snapshot. A fully failed run also computes removals against an empty list. | Reconcile each successfully observed wallet/chain scope independently. Retain failed or unobserved scopes with original quantities and timestamps, marked STALE and not fresh. |
| R2 | P1 | Claim alerts are emitted only on the first baseline. A later WAITING-to-CLAIMABLE transition has no alert. Health failures reset each run; an unrelated failure hides valid claim candidates. | Evaluate actual claimability every cycle, persist deduplication and source-health episodes, and commit snapshot, alert, outbox, health, and task updates atomically. Pending outbox entries are not delivered notifications. |
| R3 | P1 | Wallet, task, run, and snapshot primary keys are global despite accepting tenant IDs. Same-key writes can overwrite another tenant or fail with a generation collision. | Composite tenant keys, a transactional legacy migration, task revision checks, and parent-generation compare-and-commit under BEGIN IMMEDIATE. SQL scoping is not server authentication. |
| R4 | P1 | CLI snapshot and claims read every historical generation and every tenant; actions list ignores its wallet argument. Task and explanation reads are not scoped. | Latest-generation views, exact wallet filtering, and explicit --tenant when the local DB contains multiple tenants. Nested subcommands retain --db and --tenant. |
| R5 | P2 | A live plan combines one read's deployment/pause/block fields with another read's request array. The default TTL is near the Unix epoch and a state-only draft is called SIMULATED. | Build from a coherent fresh state, reject stale block timestamps, use the actual clock, increase stored plan revisions, and mark service-generated plans DRAFT with simulation source not_run. No claim transaction simulation is claimed. |
| R6 | P1 before any signing release | Human summary fields outside the digest can misstate the input quantity, units, chain label, fees, and simulation status. A recomputable digest is not origin authentication. | Validate fixed Lista semantics, original account and recipient, exact calldata/value/target, quantities, units, and all displayed financial summary fields. Display a fixed preview heading and explicitly disclose the absence of live revalidation or origin authentication. |
| R7 | P2 | Same-origin task fetches follow redirects, input size is unbounded, and an old async result can reappear after clearing the view. Standalone plans bypass task-only secret screening. | Reject redirects, bind the requested reference, stream-limit task JSON to 1 MiB, bound local files and nesting, screen both input paths, and invalidate stale asynchronous generations. These are not authenticated task routing. |
| R8 | P2 | DOM typings omit DOM.Iterable and the dev server binds all interfaces. | Add DOM.Iterable, bind localhost in both Vite and its npm command, and add Python/client CI gates. |

Additional changes: Hyperliquid malformed responses fail closed, its unqueried account modes/subaccount positions remain PARTIAL, actual raw response data is retained, HTTPX failures are handled without pretending success, and configured-but-unimplemented provider adapters are labelled accurately. A disappeared withdrawal remains MISSING_NO_CLAIM_EVIDENCE until a matching claim receipt is available; its displayed quantity is explicitly last known, not newly verified received funds.

## Validation actually performed

GitLab file exports were read at the pinned base, copied locally, and compared using Git blob hashes. The working environment could not clone or install the locked dependency set because external DNS resolution failed. No private wallet or API credentials were used in these tests.

| Check | Result | Limit |
| --- | --- | --- |
| Five new storage regression cases against the original exported storage | 5 expected failures, exit 1 | Reproduces same-wallet/task/run identity collisions, generation collision, and missing persistent alert deduplication. |
| Current review storage, monitoring-cycle, and local-view tests | 33 passed, exit 0 | Python 3.13.5; real SQLite and production cycle code, synthetic provider data. Not the complete repository suite. |
| Service/CLI integration module | 1 module skipped at collection | Contains 8 integration functions. Required eth_abi/eth_utils dependencies could not be installed here. None of these 8 functions is counted as passed. |
| Client validation tests | 16 passed, 0 failed, exit 0 | Node 22.16.0; helper/stream tests, not browser or real-wallet end-to-end tests. |
| Python syntax compilation of local changed sources/tests | Exit 0 | Does not validate unavailable third-party imports. |
| TypeScript check before DOM.Iterable | TS2339 on URLSearchParams.keys | Local TypeScript 5.8.3. |
| TypeScript check after DOM.Iterable | Exit 0 | Global TypeScript 5.8.3, not an npm-ci locked Vite build. |
| GitLab CI attempt | BLOCKED: ci_quota_exceeded | Pipeline 2920844312; jobs 16989492210 and 16989492211 failed before runner assignment/start. YAML validation was not the failure. |
| Full uv.lock suite, canonical parity suite, npm ci/Vite build, fork and real-wallet tests | NOT PASSED IN THIS REVIEW ENVIRONMENT | Must be run in a provisioned environment before the corresponding gate can be approved. |

The initial CI failure is visible at [pipeline 2920844312](https://gitlab.com/aa22396584/wallet-guardian/-/pipelines/2920844312). Later pushes can have their own quota-blocked pipelines; consult the MR head, not a historical green status. Do not change billing or runner configuration without operator approval.

Reproduce the new regression subset after installing the repository's normal dependencies:

```sh
cd wallet-guardian
uv sync --frozen --group dev
uv run --frozen python -c "import eth_abi, eth_utils"
uv run --frozen pytest -q tests/unit/test_review_storage.py tests/unit/test_review_monitoring.py tests/unit/test_review_views.py tests/unit/test_review_service.py
uv run --frozen pytest -q
cd action-client
npm ci --ignore-scripts --no-audit --no-fund
npm run test:canonical
npm run test:validation
npm run build
```

The service integration module must execute, not skip, in the fully provisioned acceptance run. A skip must not be counted as an integration pass.

## Migration and behavior notes

Stop the local collector and back up its private SQLite database before upgrading. The migration raises user_version to 1, preserves existing payloads and historical duplicate alert rows, and seeds the new dedup table without replaying old notifications. It cannot recover information already overwritten by the original globally keyed schema. Older binaries should not be used with the migrated database; keep the pre-upgrade backup for rollback.

Wallet labels are still legacy position identifiers. Rebinding a stored label to a different address, or renaming an existing address without an identity migration, now fails closed. This avoids merging unrelated history or duplicating a portfolio. A future migration should replace label-based identities with stable wallet IDs.

The CLI is a local operator tool. --tenant selects a scope; it does not authenticate a remote caller. Do not publish it as a multi-user API. Evidence is kept in a tenant-derived directory, which is organizational separation, not protection against another process with the same OS identity.

A normal expected claim is suppressed for an external watcher only when the operator supplies an exact Lista request_uuid, the appropriate chain/protocol, and a VERIFIED_HEALTHY ownership assertion. The original amount/date hint alone is insufficient. No external watcher is canceled by this MR; its heartbeat integration and ownership handoff still require validation.

Health alerts report failed reads, and recovery only means the configured reads recovered. Unsupported discovery/positions do not become covered after a successful HTTP response. A healthy run still has the explicitly listed limitations.

## Outstanding release blockers and follow-up work

These are not fixed or certified by this MR:

1. **Actual monitoring coverage and delivery.** Live collection remains limited to specific BSC/Lista reads and Hyperliquid info. DeBank/Zerion/Alchemy discovery adapters, a real exposure-aware news collector, complete approvals/bridge/lending/LP history, scheduler heartbeat, and a notification sender are not complete. The outbox introduced here is durable storage only. Do not advertise all-chain monitoring or notifications_sent=true.
2. **Task-server authentication and routing.** No authenticated server for private task retrieval is implemented. TaskDirectory still uses a global in-memory reference key and mutable values; issue_task_link's /t/ref path is not the client's ?ref route. Its HTTPS-string test is not a verified deployment registry. Do not expose this prototype directory/link issuer as a public multi-tenant service.
3. **Real execution authorization.** Mode selection and task-state helpers contain caller-provided booleans and placeholders, not a verified on-chain enforcer or receipt verifier. A SHA digest authenticates no publisher. Wallet connection, chain/account change handling, live client-side reconstruction, independently trusted implementation manifests, transaction effects, permission revocation, and phone-wallet end-to-end tests are required before a separate signing release. Mode C remains unsupported.
4. **Outbound transport hardening.** security/ssrf.py checks DNS before HTTPX resolves again. This is not connection-level DNS pinning and does not establish protection against DNS rebinding. Do not accept arbitrary RPC, metadata, proof, or certificate URLs until the actual connecting transport enforces approved destinations, redirects, response bounds, and peer identity. Hardcoded current endpoints narrow exposure but do not implement a generic safe fetcher.
5. **RPC and finality evidence.** The current timestamp guard is not full block-hash/reorg verification, an independent-provider quorum, or a deployment/code-hash allowlist. Add endpoint/schema checks, fixed-block evidence, implementation upgrade handling, fork fixtures, and actual claim-event/postcondition verification.
6. **Helper coverage versus production coverage.** Original acceptance catalogs include pure policy helpers and synthetic assertions. For example, sanitize_share does not recurse into lists, and some completion/permission helpers trust booleans. They cannot be used as production privacy or signing gates. Wire requirements to actual entry-point and adversarial tests rather than treating a catalog count as certification.

No mainnet transaction was submitted, no wallet permissions were requested, no executor key was created, no public port was opened, and no existing external monitor was modified by this review.

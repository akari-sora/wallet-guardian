# P1 review: browser lifetime, login-message scope, and bounded responses

Reviewed P1: `9ceac95cc847c941bac1662a91dfd02b18c39b44` (MR !2).
P0 dependency: `223fbe391f3c809aec67a8affc3ea4657e73480e` (MR !1).
Fix branch: `fix/p1-client-auth-lifecycle`, targeting the P1 branch, not main.

## Scope and verdict

P0 conflicts are resolved: both !1 and !2 reported `has_conflicts=false` during this review. Neither is merged. The P1 server implements real session/ACL/database routes rather than the former public-static task sketch. Inspection found credential-derived tenant/wallet scoping, stored-message verification, conditional one-time nonce consumption, server-side logout revocation, and explicit negative execution flags.

Those improvements do not eliminate client lifetime bugs. This change fixes four bounded findings without adding transaction execution, deploying Pages, modifying D1, changing the fixed-origin status, or enabling Mode C. The server authentication and database schema are unchanged.

## Findings and fixes

| ID | Priority | Reproduced/inspected issue | Fix |
| --- | --- | --- | --- |
| P1-R1 | P1: private-view boundary | `logout()`/`cancelPrivateTask()` neither invalidate load generations nor clear lastPlan/lastTask. An earlier GET or capability lookup can repopulate a private/cancelled task after the UI reports logout/cancellation. | Invalidate pending view generations and saved plan/capability state at lifecycle boundaries. Ignore stale session restoration, serialize auth mutations, and prevent a late cancellation response replacing a new view. |
| P1-R2 | P2: session usability | `login()` stores the challenge CSRF in the same variable used for an existing session. Rejecting the login signature leaves that wrong token in place, so later logout/cancel is sent with the wrong CSRF. | Keep challenge CSRF local to the verify request, allow an explicit request CSRF header, retain the existing session on refused/failed reauthentication, and display the actual read-session address. |
| P1-R3 | P1 before a user-facing login release | Before `personal_sign`, two substring matches are treated as the login-message policy. A changed domain/URI or extra SIWE resources can still reach the wallet. This is a failure to enforce the client's login-only signing scope, not a demonstrated bypass of server signature/ACL verification. | Validate the exact fixed P1 SIWE profile against browser origin and independently read wallet address/chain; bind response/message fields, nonce shape, issue/expiration times and the fixed statement. Reject extra lines/resources. Recheck the wallet immediately before offering the message. |
| P1-R4 | P2: response lifetime | `readBoundedJson()` limits accumulated bytes but leaves its stream reader locked, including oversize/error paths. | Cancel/release in finally and test success, malformed JSON, oversize streaming, and read failure. |

The strict profile intentionally accepts the current server's 11-line, 10-minute EOA login message, not arbitrary valid SIWE extensions. A different profile requires a coordinated versioned change. Server verification remains required. The origin check does not defend against an attacker replacing the entire same-origin client. No real wallet signature was produced by this review.

While a login mutation is in progress, login/logout controls are serialized. The user can finish or reject the wallet prompt before logging out. This avoids a same-page verify response installing a cookie after a completed logout. This is not a claim of global cross-tab session revocation: tab synchronization and server-wide revoke-all policy are separate work.

On an unconfirmed logout or cancellation response, the private view is cleared and the result is described as unconfirmed, not as an established server/chain state. Logout retries retain the correct session CSRF. No operation is automatically broadcast or retried on chain.

## Actual verification

The reviewed client files were exported with the GitLab connector and checked against Git blob hashes. The changed production files, test files and package scripts were also compared against GitLab blobs after committing.

Environment: Node 22.16.0, locally installed TypeScript 5.8.3. The review container cannot resolve the external package hosts; Vite and @noble/curves are not installed here. No claim is made that `npm ci`, the complete P1 API suite, the Python suite or the Vite build ran in this environment.

- Seven selected tests against the exact original client: **six expected failures and one successful control**, exit 1. The failures reproduce three late-private-view cases, challenge/session CSRF corruption, wrong-domain signing, and extra-resource signing.
- New regression suite after the patch: **35 passed, 0 failed, 0 skipped**, exit 0. Repeated three times successfully; this is 35 distinct cases, not 105 distinct tests.
- Breakdown: 14 entry-point lifecycle cases, 17 fixed-login-profile cases, 4 stream-lifetime cases.
- `tsc --noEmit`: exit 0 with local TypeScript 5.8.3. This is not an npm-ci/Vite artifact test.
- The entry-point harness transpiles the actual `src/main.ts` and runs it with the real local validation/digest/gate modules, a synthetic DOM, synthetic provider and mocked network. Its test-only evaluation substitutes the mainnet flag with `true` without changing the checked-in gate. **It is not a browser, mobile-wallet, Cloudflare/D1-runtime, or production-bundle E2E test.** No private keys or user wallet data are used.
- MR !2 pipeline `2921532804`, jobs `16994125295` and `16994125296`: `ci_quota_exceeded`, no runner and no start time. This is not a failed test assertion or a passing gate.

`test:api` includes the new tests, and `test:review` runs this subset separately. No dependency versions or lockfile contents are changed.

Required complete acceptance on a provisioned machine:

```sh
cd wallet-guardian
uv sync --frozen --group dev
uv run --frozen pytest -q
cd action-client
npm ci --ignore-scripts --no-audit --no-fund
npm run test:canonical
npm run test:validation
npm run test:gate
npm run test:api
npm run build
```

Repeat the late-response/logout/cancel/refused-login cases in an actual browser and mock-wallet setup. These tests should also run with the compiled artifact's send configuration off; changing an environment variable in a source test is not deployment provenance.

## Follow-ups, not hidden P2 completion claims

1. **Public API availability protection:** the original challenge bucket is keyed by IP plus caller-supplied address. Rotating addresses obtains new buckets; verify has no independent rate limit before recovery. Before exposing this API, add source-level budgets, bounded storage cleanup and verification/publisher budgets with targeted tests. The current same-address limiter is not a global source-IP limit. This MR does not change server rate policy.
2. **Authorization lifecycle:** removing tenant_members alone is not a complete publisher revocation strategy because an active scoped publisher can create membership for its allowed wallet. Define and test revocation of membership, publisher credentials and browser sessions together where intended; do not call this a demonstrated arbitrary cross-tenant bypass.
3. **D1 evidence:** the existing SQLite test adapter is valuable but is not Cloudflare read-replication or edge-routing evidence. `withSession('first-primary')` forces the first query to primary and provides sequential consistency for later queries; it does not literally force every statement to primary. No already-completed logout bypass was demonstrated from this detail.
4. **P2 stays separate:** this patch does not implement independent contract reads, implementation trust, account/chain event invalidation for execution, nonce/pending transaction tracking, receipt verification, or EIP-5792 compatibility. The existing send function continues to throw unconditionally; C stays unsupported.

For P2, implement a read-only client-owned preflight slice first. Treat task JSON as requested intent, not RPC/deployment authority or a source of `preflight_ok`. Resolve trusted endpoints/ABI/deployments locally, reread the original account and Lista request identity/index at a coherent block, decode the exact allowed call, validate freshness/TTL and invalidate the result on auth/task/account/chain changes. A successful read is not a transaction-effects simulation or permission to remove the deny flags. Keep real sending and deployment out of that MR.

## Official specifications consulted

- SIWE message and origin requirements: https://eips.ethereum.org/EIPS/eip-4361
- D1 session/read-replication guarantees: https://developers.cloudflare.com/d1/best-practices/read-replication/
- D1 database API: https://developers.cloudflare.com/d1/worker-api/d1-database/

No merge, deployment, public port, billing change, schedule update, D1 provisioning, real login signature, transaction, permission grant or executor credential was performed by this review.

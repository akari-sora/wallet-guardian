/** Mode A guided path for one Lista original-address claimWithdraw.

 * A re-read pass is not send authorization. Production callers keep
 * requestUserSend fail-closed. The only wallet request in this module runs
 * when a test passes testOnlyAllowWalletRequest === true and a provider that
 * marks itself noBroadcast. That call is recorded by the test double. This
 * module does not broadcast.
 */

import { claimCalldata, sameAddress, TRUSTED_LISTA } from "./trusted-lista.mjs";
import { reverifyListaClaim } from "./lista-reread.mjs";

export const POISON_NOTE =
  "確認這次領到自己的原地址，只代表這一筆。不是永遠信任這個地址，也不是通訊錄。";

export const OUTCOME_TEXT = Object.freeze({
  idle: "錢包結果：尚未提出（not started）。交易雜湊不是完成。",
  pending: "錢包結果：待確認（pending）。有雜湊也不算完成。",
  unknown: "錢包結果：不明（unknown）。不會自動再送一次。",
  confirmed: "錢包結果：已核對經濟結果（confirmed）。雜湊本身不是完成。",
  replaced: "錢包結果：已替代（replaced）。舊雜湊與新雜湊都不是完成。",
  failed: "錢包結果：失敗（failed）。不會自動再送。",
});

const DRIFT_REASONS = new Set([
  "index_drift",
  "amount_drift",
  "deployment_changed",
  "recipient_changed",
  "paused",
  "not_claimable",
  "claimable_missing",
  "calldata_mismatch",
  "uuid_not_unique",
  "account_changed",
  "wrong_chain",
]);

const CLAIM_FIELDS = [
  "idx",
  "uuid",
  "amountIn",
  "amountOut",
  "implementation",
  "proxyCodeSha256",
  "implementationCodeSha256",
  "calldata",
  "to",
  "value",
  "recipient",
  "chainId",
];

const ADDRESS_FIELDS = new Set(["implementation", "to", "recipient"]);

/** A passing re-read never flips send. A forged allowSend is rejected. */
export function sendAuthorizedByReread(view) {
  if (view && view.allowSend !== "closed") {
    throw new Error("allow send stays closed");
  }
  if (view && Object.prototype.hasOwnProperty.call(view, "testOnlyAllowWalletRequest")) {
    throw new Error("re-read cannot carry send authorization");
  }
  return false;
}

export function emptyWalletTrack() {
  return {
    status: "idle",
    done: false,
    hashIsDone: false,
    autoRetry: false,
    newTransaction: false,
    txHash: null,
    replacementHash: null,
    economicVerified: false,
  };
}

function hashText(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value);
}

function userRejected(error) {
  if (!error || typeof error !== "object") return false;
  if (error.code === 4001) return true;
  return /user rejected|user denied|rejected the request/i.test(String(error.message || ""));
}

export function interpretWalletReturn(value, error) {
  if (error) {
    if (userRejected(error)) return { type: "rejected" };
    return { type: "ambiguous" };
  }
  if (!hashText(value)) return { type: "ambiguous" };
  return { type: "submitted", txHash: value.toLowerCase() };
}

export function applyWalletEvent(track, event) {
  const base = {
    ...(track && typeof track === "object" ? track : emptyWalletTrack()),
    done: false,
    hashIsDone: false,
    autoRetry: false,
    newTransaction: false,
    economicVerified: false,
  };
  if (!event || event.type === "rejected") {
    return { ...base, status: "failed", txHash: null, replacementHash: null };
  }
  if (event.type === "ambiguous") {
    return { ...base, status: "unknown", txHash: hashText(event.txHash) ? event.txHash.toLowerCase() : null };
  }
  if (event.type === "submitted") {
    if (!hashText(event.txHash)) return { ...base, status: "unknown", txHash: null };
    return { ...base, status: "pending", txHash: event.txHash.toLowerCase() };
  }
  if (event.type === "replaced") {
    return {
      ...base,
      status: "replaced",
      txHash: hashText(event.previousHash) ? event.previousHash.toLowerCase() : base.txHash,
      replacementHash: hashText(event.replacementHash) ? event.replacementHash.toLowerCase() : null,
    };
  }
  return { ...base, status: "unknown", txHash: null };
}

/** List disappearance is not enough. A hash without this check stays pending. */
export function verifyClaimEconomics({
  requestGone,
  claimLog,
  nativeDelta,
  gasUsed,
  concurrentOtherTxs,
}) {
  if (!claimLog || claimLog.bnbRaw == null) {
    return { verified: false, reason: "missing_claim_event", listDisappearanceIsEnough: false };
  }
  if (concurrentOtherTxs === true || nativeDelta == null || gasUsed == null) {
    return { verified: false, reason: "native_delta_ambiguous", listDisappearanceIsEnough: false };
  }
  let expected;
  try {
    expected = BigInt(claimLog.bnbRaw) - BigInt(gasUsed);
  } catch {
    return { verified: false, reason: "effect_mismatch", listDisappearanceIsEnough: false };
  }
  if (BigInt(nativeDelta) !== expected || requestGone !== true) {
    return { verified: false, reason: "effect_mismatch", listDisappearanceIsEnough: false };
  }
  return { verified: true, reason: null, listDisappearanceIsEnough: false };
}

export function recordEconomicOutcome(track, economics) {
  const current = track && typeof track === "object" ? { ...track } : emptyWalletTrack();
  const verdict = verifyClaimEconomics(economics || {});
  const receiptFailed = economics && (economics.receiptStatus === "failed" || economics.receiptStatus === "0x0");
  if (verdict.verified === true && !receiptFailed) {
    return {
      ...current,
      status: "confirmed",
      done: true,
      hashIsDone: false,
      autoRetry: false,
      newTransaction: false,
      economicVerified: true,
      economicReason: null,
    };
  }
  return {
    ...current,
    status: receiptFailed ? "failed" : "pending",
    done: false,
    hashIsDone: false,
    autoRetry: false,
    newTransaction: false,
    economicVerified: false,
    economicReason: verdict.reason,
  };
}

export function recordReplacement(track, replacement) {
  return applyWalletEvent(track, {
    type: "replaced",
    previousHash: replacement && (replacement.previousHash || track?.txHash),
    replacementHash: replacement && replacement.replacementHash,
  });
}

/** Ambiguous, pending, replaced, failed, and confirmed claims do not start another Mode A send. */
export function retryModeA(track) {
  const status = track && track.status ? track.status : "idle";
  if (status === "idle") {
    return { status, newTransaction: false, autoRetry: false, done: false, hashIsDone: false, blocked: false };
  }
  return {
    status,
    newTransaction: false,
    autoRetry: false,
    done: false,
    hashIsDone: false,
    blocked: true,
  };
}

function fieldValue(claim, field) {
  const value = claim[field];
  if (ADDRESS_FIELDS.has(field) && typeof value === "string") return value.toLowerCase();
  if (field === "calldata" && typeof value === "string") return value.toLowerCase();
  return value;
}

export function materialClaimChanges(first, second) {
  const left = first && first.claim;
  const right = second && second.claim;
  if (!left || !right) return [];
  const changes = [];
  for (const field of CLAIM_FIELDS) {
    if (fieldValue(left, field) !== fieldValue(right, field)) changes.push(field);
  }
  const leftAccount = first.binding && first.binding.account;
  const rightAccount = second.binding && second.binding.account;
  if (!sameAddress(leftAccount, rightAccount)) changes.push("account");
  return changes;
}

export function nextClaimRevision(plan, changes) {
  const revision = plan && Number.isSafeInteger(plan.revision) ? plan.revision : 0;
  if (!changes.length) {
    return { revision, previousValid: true, replacedSilently: false, changes: [] };
  }
  return { revision: revision + 1, previousValid: false, replacedSilently: false, changes: [...changes] };
}

function driftReasons(second) {
  const reasons = second && Array.isArray(second.reasons) ? second.reasons : [];
  return reasons.filter((reason) => DRIFT_REASONS.has(reason));
}

/** Rebuild claimWithdraw(idx) from the re-read observation. The plan idx is not an input. */
export function exactClaimRequest(observation) {
  sendAuthorizedByReread(observation);
  if (!observation || observation.reread !== "pass") {
    throw new Error("only a passing re-read can describe the claim");
  }
  const claim = observation.claim;
  if (!claim || !Number.isSafeInteger(claim.idx) || claim.idx < 0) {
    throw new Error("missing claim observation");
  }
  const data = claimCalldata(claim.idx);
  if (String(claim.calldata).toLowerCase() !== data) throw new Error("calldata mismatch");
  if (!data.startsWith("0xb13acedd") || data.length !== 74) throw new Error("not a single uint256");
  if (claim.value !== "0") throw new Error("value");
  if (!sameAddress(claim.to, TRUSTED_LISTA.proxy)) throw new Error("target");
  if (!sameAddress(claim.recipient, observation.binding && observation.binding.account)) {
    throw new Error("recipient");
  }
  if (!sameAddress(claim.recipient, claim.recipient)) throw new Error("recipient");
  return {
    method: "eth_sendTransaction",
    params: [{
      from: observation.binding.account,
      to: TRUSTED_LISTA.proxy,
      data,
      value: "0x0",
    }],
  };
}

const DENIED_BATCH_METHOD = "wallet_sendCalls";

export function assertSingleStep(request) {
  if (!request || request.method === DENIED_BATCH_METHOD || request.method !== "eth_sendTransaction") {
    throw new Error("mode A does not batch");
  }
  if (!Array.isArray(request.params) || request.params.length !== 1 || Array.isArray(request.params[0].calls)) {
    throw new Error("mode A does not batch");
  }
  return request.method;
}

function isNoBroadcastRecorder(provider) {
  return Boolean(provider)
    && provider.noBroadcast === true
    && provider.broadcasts !== true
    && typeof provider.request === "function";
}

function closedGuide(fields) {
  return {
    allowSend: "closed",
    walletRequested: false,
    broadcast: false,
    testProbe: null,
    autoRetry: false,
    outcome: emptyWalletTrack(),
    prepared: null,
    revision: null,
    poison: { once: true, forever: false, addressBook: false, text: POISON_NOTE },
    ...fields,
    allowSend: "closed",
    broadcast: false,
  };
}

async function readOnce(input) {
  const body = {
    call: input.call,
    plan: input.plan,
    task: input.task ?? null,
    account: input.account,
    connectedAccount: input.connectedAccount,
    walletChainId: input.walletChainId,
    walletIdentityVerified: input.walletIdentityVerified,
    planDigest: input.planDigest ?? input.plan.digest,
    revision: input.revision,
    startedAt: input.startedAt,
    nowUnix: input.nowUnix,
    clock: input.clock,
  };
  if (Object.prototype.hasOwnProperty.call(input, "testApprovals")) body.testApprovals = input.testApprovals;
  return reverifyListaClaim(body);
}

/**
 * Open the guided claim: two independent re-reads, then a wallet request only
 * for a test recorder. Re-read pass does not set testOnlyAllowWalletRequest.
 */
export async function guideListaModeA(input) {
  if (!input || !input.plan || typeof input.call !== "function") {
    return closedGuide({ phase: "missing_fields", reads: 0 });
  }
  const prior = retryModeA(input.prior);
  if (prior.blocked) {
    const confirmed = input.prior.status === "confirmed" && input.prior.economicVerified === true;
    return closedGuide({
      phase: "no_auto_retry",
      outcome: {
        ...emptyWalletTrack(),
        ...input.prior,
        autoRetry: false,
        newTransaction: false,
        hashIsDone: false,
        done: confirmed,
      },
      reads: 0,
    });
  }
  if (input.batchIndexes != null || input.prefetchedIndex != null || input.mode === "B" || input.mode === "C") {
    return closedGuide({ phase: "batch_preread_rejected", reads: 0 });
  }
  let rpcCalls = 0;
  const counting = async (method, params) => {
    rpcCalls += 1;
    return input.call(method, params);
  };
  const scoped = { ...input, call: counting };
  const first = await readOnce(scoped);
  sendAuthorizedByReread(first);
  if (first.reread !== "pass") {
    return closedGuide({ phase: "first_reread", first, second: null, reads: rpcCalls });
  }
  const second = await readOnce(scoped);
  sendAuthorizedByReread(second);
  const fieldChanges = materialClaimChanges(first, second);
  const reasonChanges = second.reread === "pass" ? [] : driftReasons(second);
  const changes = [...new Set([...fieldChanges, ...reasonChanges])];
  const revision = nextClaimRevision(input.plan, changes);
  if (second.reread !== "pass" || changes.length) {
    return closedGuide({
      phase: changes.length ? "revision_required" : "second_reread",
      first,
      second,
      revision,
      reads: rpcCalls,
    });
  }
  let prepared;
  try {
    prepared = exactClaimRequest(second);
    assertSingleStep(prepared);
  } catch {
    return closedGuide({ phase: "not_single_step", first, second, revision, reads: rpcCalls });
  }
  const testOnly = input.testOnlyAllowWalletRequest === true;
  if (!testOnly || sendAuthorizedByReread(second) !== false) {
    return closedGuide({
      phase: "send_closed",
      first,
      second,
      revision,
      prepared,
      reads: rpcCalls,
      reason: "reread_is_not_send_authorization",
    });
  }
  if (!isNoBroadcastRecorder(input.provider)) {
    return closedGuide({
      phase: "send_closed",
      first,
      second,
      revision,
      prepared,
      reads: rpcCalls,
      reason: "no_broadcast_provider_required",
    });
  }
  let value;
  let error = null;
  try {
    value = await input.provider.request(prepared);
  } catch (caught) {
    error = caught;
  }
  const outcome = applyWalletEvent(emptyWalletTrack(), interpretWalletReturn(value, error));
  return closedGuide({
    phase: "test_probe",
    first,
    second,
    revision,
    prepared,
    reads: rpcCalls,
    outcome,
    walletRequested: true,
    testProbe: "recorded_not_broadcast",
    reason: "test_only_no_broadcast",
  });
}

/** Mode A guided path for one Lista original-address claimWithdraw.

 * A re-read pass is not send authorization. This module returns claim fields
 * and does not construct or submit a wallet request.
 */

import { CLAIM_WITHDRAWAL_TOPIC, claimCalldata, isAddress, sameAddress, TRUSTED_LISTA } from "./trusted-lista.mjs";
import { reverifyListaClaim, settleReread } from "./lista-reread.mjs";

export const POISON_NOTE =
  "確認這次領到自己的原地址，只代表這一筆。不是永遠信任這個地址，也不是通訊錄。";

export const OUTCOME_TEXT = Object.freeze({
  idle: "錢包結果：尚未提出（not started）。交易雜湊不是完成。",
  pending: "錢包結果：待確認（pending）。有雜湊也不算完成。",
  unknown: "錢包結果：不明（unknown）。不會自動再送一次。",
  confirmed: "錢包結果：已核對經濟結果（confirmed）。雜湊本身不是完成。",
  replaced: "錢包結果：已替代（replaced）。舊雜湊與新雜湊都不是完成。",
  failed: "錢包結果：失敗（failed）。鏈上還原，不會自動再送。",
  rejected: "錢包結果：已拒簽（rejected）。這不是鏈上失敗。新的點擊與兩次重讀之後才能再試，不會自動再送。",
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

/** Versioned wallet-error table. Further rows wait for a real-wallet sample. */
export const WALLET_RETURN_TABLE_VERSION = 1;

export const WALLET_RETURN_TABLE = Object.freeze({
  version: WALLET_RETURN_TABLE_VERSION,
  rejected: Object.freeze([
    Object.freeze({ code: 4001 }),
    Object.freeze({ code: 5000, messageIncludes: "User rejected", source: "walletconnect" }),
  ]),
});

function errorCode(error) {
  if (!error || typeof error !== "object") return null;
  return typeof error.code === "number" && Number.isInteger(error.code) ? error.code : null;
}

function errorMessage(error) {
  if (!error || typeof error !== "object" || typeof error.message !== "string") return "";
  return error.message;
}

/** 4001, or WalletConnect 5000 whose message contains "User rejected". Everything else is ambiguous. */
export function classifyWalletError(error) {
  const code = errorCode(error);
  const message = errorMessage(error);
  for (const row of WALLET_RETURN_TABLE.rejected) {
    if (code !== row.code) continue;
    if (row.messageIncludes && !message.includes(row.messageIncludes)) continue;
    return "rejected";
  }
  return "ambiguous";
}

export function interpretWalletReturn(value, error) {
  if (error) {
    const classification = classifyWalletError(error);
    if (classification === "rejected") {
      return { type: "rejected", classification, tableVersion: WALLET_RETURN_TABLE_VERSION };
    }
    return { type: "ambiguous", classification: "ambiguous", tableVersion: WALLET_RETURN_TABLE_VERSION };
  }
  if (!hashText(value)) {
    return { type: "ambiguous", classification: "ambiguous", tableVersion: WALLET_RETURN_TABLE_VERSION };
  }
  return {
    type: "submitted",
    classification: "submitted",
    tableVersion: WALLET_RETURN_TABLE_VERSION,
    txHash: value.toLowerCase(),
  };
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
  if (!event) {
    return { ...base, status: "failed", txHash: null, replacementHash: null };
  }
  if (event.type === "rejected") {
    return { ...base, status: "rejected", txHash: null, replacementHash: null };
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

function uintString(value) {
  return typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value) && BigInt(value) < (1n << 256n);
}

function wei(value) {
  if (typeof value === "bigint") {
    if (value < 0n || value >= (1n << 256n)) return null;
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) return null;
    return BigInt(value);
  }
  if (typeof value !== "string" || !/^(0|[1-9][0-9]*|0x[0-9a-fA-F]+)$/.test(value)) return null;
  try {
    const parsed = BigInt(value);
    if (parsed < 0n || parsed >= (1n << 256n)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function word(value) {
  const parsed = wei(value);
  if (parsed == null) return null;
  return parsed.toString(16).padStart(64, "0");
}

function addressTopic(account) {
  return "0x" + account.slice(2).toLowerCase().padStart(64, "0");
}

function sameAccount(left, right) {
  if (left == null && right == null) return true;
  return sameAddress(left, right);
}

function sameUint(left, right) {
  const a = wei(left);
  const b = wei(right);
  return a != null && b != null && a === b;
}

/** Expected values come from the submission stored at send time, not from the caller's story. */
function boundSubmission(track) {
  const submission = track && track.submission;
  if (!submission || typeof submission !== "object") return null;
  if (!hashText(track.txHash) || !hashText(submission.txHash)) return null;
  if (submission.txHash.toLowerCase() !== track.txHash.toLowerCase()) return null;
  if (!isAddress(submission.account)) return null;
  if (!sameAddress(submission.to, TRUSTED_LISTA.proxy)) return null;
  if (!Number.isSafeInteger(submission.idx) || submission.idx < 0) return null;
  if (submission.chainId !== TRUSTED_LISTA.chainId) return null;
  if (!uintString(String(submission.uuid)) || !uintString(String(submission.amountOut))) return null;
  let data;
  try {
    data = claimCalldata(submission.idx);
  } catch {
    return null;
  }
  if (String(submission.calldata).toLowerCase() !== data) return null;
  return {
    ...submission,
    txHash: submission.txHash.toLowerCase(),
    account: submission.account.toLowerCase(),
    uuid: String(submission.uuid),
    amountOut: String(submission.amountOut),
    calldata: data,
    to: TRUSTED_LISTA.proxy,
  };
}

function unverified(reason, failed) {
  return { verified: false, failed: failed === true, reason, listDisappearanceIsEnough: false };
}

function blockHashOf(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value) ? value.toLowerCase() : null;
}

function blockNumberOf(value) {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? value : null;
  if (typeof value === "bigint") {
    if (value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) return null;
    return Number(value);
  }
  if (typeof value !== "string") return null;
  if (/^0x[0-9a-fA-F]+$/.test(value) || /^(0|[1-9][0-9]*)$/.test(value)) {
    try {
      const parsed = Number(BigInt(value));
      return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
    } catch {
      return null;
    }
  }
  return null;
}

/** Receipt, logs, post-state, and the balance window share one chain and compatible blocks. */
function receiptBackground(receipt, submission) {
  const chainId = blockNumberOf(receipt.chainId);
  const blockHash = blockHashOf(receipt.blockHash);
  const blockNumber = blockNumberOf(receipt.blockNumber);
  if (chainId == null || blockHash == null || blockNumber == null) return unverified("block_unbound", false);
  if (chainId !== submission.chainId) return unverified("provenance_mismatch", false);
  return { chainId, blockHash, blockNumber };
}

function observationBackground(part, receiptBlock) {
  if (!part || typeof part !== "object") return "block_unbound";
  const chainId = blockNumberOf(part.chainId);
  const blockHash = blockHashOf(part.blockHash);
  const blockNumber = blockNumberOf(part.blockNumber);
  if (chainId == null || blockHash == null || blockNumber == null) return "block_unbound";
  if (chainId !== receiptBlock.chainId) return "provenance_mismatch";
  if (blockNumber < receiptBlock.blockNumber) return "provenance_mismatch";
  if (blockNumber === receiptBlock.blockNumber && blockHash !== receiptBlock.blockHash) return "provenance_mismatch";
  return null;
}

/**
 * A confirmed claim is this task's successful receipt, matching claim event,
 * original account, request id, and received amount. Caller totals are ignored.
 * The fee is gasUsed × effectiveGasPrice, never gas units subtracted from wei.
 */
export function verifyClaimEconomics(track, evidence) {
  const current = track && typeof track === "object" ? track : emptyWalletTrack();
  if (current.status === "replaced") return unverified("replaced", false);
  if (!hashText(current.txHash)) return unverified("missing_tx", false);
  const submission = boundSubmission(current);
  if (!submission) return unverified("missing_submission", false);
  const receipt = evidence && evidence.receipt;
  if (!receipt || typeof receipt !== "object") return unverified("missing_receipt", false);
  if (!hashText(receipt.transactionHash) || receipt.transactionHash.toLowerCase() !== submission.txHash) {
    return unverified("provenance_mismatch", false);
  }
  if (!sameAddress(receipt.from, submission.account) || !sameAddress(receipt.to, submission.to)) {
    return unverified("provenance_mismatch", false);
  }
  const receiptBlock = receiptBackground(receipt, submission);
  if (receiptBlock.reason) return receiptBlock;
  if (receipt.status === "0x0") return unverified("receipt_failed", true);
  if (receipt.status !== "0x1") return unverified("receipt_pending", false);
  if (!Array.isArray(receipt.logs)) return unverified("missing_claim_event", false);
  const claimLogs = receipt.logs.filter((log) => {
    return log && Array.isArray(log.topics) && String(log.topics[0]).toLowerCase() === CLAIM_WITHDRAWAL_TOPIC;
  });
  if (claimLogs.length === 0) return unverified("missing_claim_event", false);
  const expectedData = "0x" + word(submission.idx) + word(submission.amountOut);
  const expectedAccount = addressTopic(submission.account);
  for (const log of claimLogs) {
    if (log.removed === true) return unverified("provenance_mismatch", false);
    if (!sameAddress(log.address, submission.to)) return unverified("provenance_mismatch", false);
    if (!hashText(log.transactionHash) || log.transactionHash.toLowerCase() !== submission.txHash) {
      return unverified("provenance_mismatch", false);
    }
    const logHash = blockHashOf(log.blockHash);
    const logNumber = blockNumberOf(log.blockNumber);
    if (logHash == null || logNumber == null) return unverified("block_unbound", false);
    if (logHash !== receiptBlock.blockHash || logNumber !== receiptBlock.blockNumber) {
      return unverified("provenance_mismatch", false);
    }
    if (String(log.topics[1] || "").toLowerCase() !== expectedAccount) return unverified("provenance_mismatch", false);
    if (String(log.data || "").toLowerCase() !== expectedData) return unverified("provenance_mismatch", false);
  }
  const gasUsed = wei(receipt.gasUsed);
  const price = wei(receipt.effectiveGasPrice);
  if (gasUsed == null || price == null) return unverified("gas_fee_unknown", false);
  const fee = gasUsed * price;
  if (evidence.gasFeeWei != null && wei(evidence.gasFeeWei) !== fee) return unverified("gas_fee_unknown", false);
  const window = evidence.balanceWindow;
  if (!window || typeof window !== "object" || !Array.isArray(window.otherTxHashes)) {
    return unverified("native_delta_ambiguous", false);
  }
  if (window.otherTxHashes.length !== 0) return unverified("native_delta_ambiguous", false);
  if (!sameAddress(window.account, submission.account)) return unverified("provenance_mismatch", false);
  if (!hashText(window.txHash) || window.txHash.toLowerCase() !== submission.txHash) {
    return unverified("provenance_mismatch", false);
  }
  const before = wei(window.beforeWei);
  const after = wei(window.afterWei);
  const amount = wei(submission.amountOut);
  if (before == null || after == null || amount == null) return unverified("native_delta_ambiguous", false);
  if (after - before !== amount - fee) return unverified("effect_mismatch", false);
  const request = evidence.request;
  if (!request || typeof request !== "object" || request.gone !== true) return unverified("request_not_gone", false);
  if (!sameAddress(request.account, submission.account)) return unverified("provenance_mismatch", false);
  if (!sameUint(request.uuid, submission.uuid) || !sameUint(request.idx, submission.idx)) {
    return unverified("provenance_mismatch", false);
  }
  const windowBlock = observationBackground(window, receiptBlock);
  if (windowBlock) return unverified(windowBlock, false);
  const requestBlock = observationBackground(request, receiptBlock);
  if (requestBlock) return unverified(requestBlock, false);
  if (evidence.context) {
    const contextBlock = observationBackground(evidence.context, receiptBlock);
    if (contextBlock) return unverified(contextBlock, false);
  }
  return { verified: true, failed: false, reason: null, listDisappearanceIsEnough: false };
}

export function recordEconomicOutcome(track, evidence) {
  const current = track && typeof track === "object" ? { ...track } : emptyWalletTrack();
  const verdict = verifyClaimEconomics(current, evidence || {});
  if (verdict.verified === true) {
    return {
      ...current,
      status: "confirmed",
      done: true,
      hashIsDone: false,
      autoRetry: false,
      newTransaction: false,
      txHash: current.txHash.toLowerCase(),
      economicVerified: true,
      economicReason: null,
    };
  }
  const sticky = current.status === "unknown"
    || current.status === "failed"
    || current.status === "replaced"
    || current.status === "rejected";
  const status = verdict.failed ? "failed" : (sticky ? current.status : "pending");
  return {
    ...current,
    status,
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

/** Ambiguous, pending, replaced, failed, rejected, and confirmed claims do not start another Mode A send. */
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

/**
 * A rejected signature may be tried again only after a new user gesture and two new re-reads.
 * Pending, unknown, failed, and every other status stay closed. This never schedules a retry.
 */
export function retryAllowed({ status, userClick, rereads } = {}) {
  if (status !== "rejected") return false;
  if (userClick !== true) return false;
  if (!Number.isSafeInteger(rereads) || rereads < 2) return false;
  return true;
}

/** Durable record that must be verified instead of sent again. Rejected is handled by retryAllowed. */
export function confirmBlock(record) {
  const status = record && record.status;
  if (status === "pending" || status === "unknown" || status === "requested" || status === "replaced") {
    return "verify_outcome";
  }
  if (status === "confirmed" || status === "failed") return "no_auto_retry";
  if (status === "rejected") return "rejected";
  return null;
}

/** One task, one in-flight guide. The outcome lives here, not only in a caller-supplied prior. */
export function createModeAFlow() {
  return {
    taskKey: null,
    inFlight: false,
    flightToken: 0,
    outcome: emptyWalletTrack(),
  };
}

export function outcomeAfterReread(flow) {
  const outcome = flow && flow.outcome && typeof flow.outcome === "object" ? flow.outcome : emptyWalletTrack();
  const status = typeof outcome.status === "string" && Object.prototype.hasOwnProperty.call(OUTCOME_TEXT, outcome.status)
    ? outcome.status
    : "idle";
  const confirmed = status === "confirmed" && outcome.economicVerified === true && hashText(outcome.txHash);
  return {
    ...emptyWalletTrack(),
    ...outcome,
    status,
    done: confirmed,
    hashIsDone: false,
    autoRetry: false,
    newTransaction: false,
    economicVerified: confirmed ? true : false,
  };
}

/** Check a stored submission against a receipt. This does not call a provider. */
export function verifyRecordedClaim(flow, evidence) {
  const next = recordEconomicOutcome(flow && flow.outcome, evidence || {});
  if (flow && typeof flow === "object") flow.outcome = next;
  return next;
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

/** Claim fields from the re-read. No method name is attached. The plan idx is not an input. */
export function describePreparedClaim(observation) {
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
  if (!isAddress(claim.recipient)) throw new Error("recipient");
  return {
    from: observation.binding.account,
    to: TRUSTED_LISTA.proxy,
    data,
    value: "0x0",
  };
}

function closedGuide(fields) {
  return {
    allowSend: "closed",
    walletRequested: false,
    broadcast: false,
    testProbe: null,
    autoRetry: false,
    outcome: emptyWalletTrack(),
    preparedClaim: null,
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

function taskKeyOf(input, identity) {
  if (identity && typeof identity.taskRef === "string" && identity.taskRef) return identity.taskRef;
  if (input && input.task && typeof input.task.ref === "string" && input.task.ref) return input.task.ref;
  if (input && input.plan && input.plan.task_id != null) return String(input.plan.task_id);
  return null;
}

function sampleIdentity(input) {
  if (typeof input.identity !== "function") {
    return {
      connectedAccount: input.connectedAccount,
      walletChainId: input.walletChainId,
      walletIdentityVerified: input.walletIdentityVerified,
      revision: input.revision,
      planDigest: input.planDigest ?? input.plan.digest,
      taskRef: input.task && input.task.ref ? input.task.ref : null,
    };
  }
  const live = input.identity();
  if (!live || typeof live !== "object") return null;
  return {
    connectedAccount: live.connectedAccount ?? null,
    walletChainId: live.walletChainId ?? null,
    walletIdentityVerified: live.walletIdentityVerified === true,
    revision: live.revision ?? null,
    planDigest: typeof live.planDigest === "string" ? live.planDigest : null,
    taskRef: typeof live.taskRef === "string" && live.taskRef ? live.taskRef : null,
  };
}

function currentTask(identity) {
  return Boolean(identity)
    && identity.taskRef != null
    && identity.revision != null
    && identity.planDigest != null;
}

function stillCurrent(input) {
  return typeof input.isCurrent !== "function" || input.isCurrent() === true;
}

function sampleNow(input) {
  if (typeof input.clock === "function") {
    const now = input.clock();
    return Number.isSafeInteger(now) ? now : null;
  }
  return Number.isSafeInteger(input.nowUnix) ? input.nowUnix : null;
}

function identityDrift(previous, next) {
  if (!previous || !next) return "identity_changed";
  if (previous.revision !== next.revision || previous.planDigest !== next.planDigest) return "revision_changed";
  if (previous.taskRef !== next.taskRef) return "identity_changed";
  if (!sameAccount(previous.connectedAccount, next.connectedAccount)) return "identity_changed";
  if (previous.walletChainId !== next.walletChainId) return "identity_changed";
  if (previous.walletIdentityVerified !== next.walletIdentityVerified) return "identity_changed";
  return null;
}

function heldPhase(status) {
  if (status === "pending" || status === "unknown") return "verify_outcome";
  return "no_auto_retry";
}

function publish(outcome) {
  return outcomeAfterReread({ outcome });
}

function readBody(input, call, identity, nowUnix) {
  return {
    ...input,
    call,
    connectedAccount: identity.connectedAccount,
    walletChainId: identity.walletChainId,
    walletIdentityVerified: identity.walletIdentityVerified,
    planDigest: identity.planDigest,
    revision: identity.revision,
    nowUnix,
    startedAt: nowUnix,
    clock: typeof input.clock === "function" ? input.clock : () => nowUnix,
  };
}

/**
 * Open the guided claim: two independent re-reads, then claim fields.
 * This function does not call a provider. The same task takes the in-flight
 * lock before the first wait. Identity, task version, and the clock are
 * sampled again before the guide returns.
 */
export async function guideListaModeA(input) {
  if (!input || !input.plan || typeof input.call !== "function") {
    return closedGuide({ phase: "missing_fields", reads: 0 });
  }
  const flow = input.flow && typeof input.flow === "object" ? input.flow : createModeAFlow();
  const taskKey = taskKeyOf(input, null);
  if (flow.inFlight) {
    const other = flow.taskKey && taskKey && flow.taskKey !== taskKey;
    return closedGuide({
      phase: other ? "task_held" : "in_flight",
      outcome: publish(flow.outcome),
      reads: 0,
    });
  }
  const stored = flow.outcome && flow.outcome.status ? flow.outcome.status : "idle";
  if (stored !== "idle") {
    const other = flow.taskKey && taskKey && flow.taskKey !== taskKey;
    return closedGuide({
      phase: other ? "task_held" : heldPhase(stored),
      outcome: publish(flow.outcome),
      reads: 0,
    });
  }
  const prior = retryModeA(input.prior);
  if (prior.blocked) {
    flow.outcome = publish(input.prior);
    if (!flow.taskKey) flow.taskKey = taskKey;
    return closedGuide({
      phase: heldPhase(input.prior.status),
      outcome: flow.outcome,
      reads: 0,
    });
  }
  if (input.batchIndexes != null || input.prefetchedIndex != null || input.mode === "B" || input.mode === "C") {
    return closedGuide({ phase: "batch_preread_rejected", reads: 0 });
  }
  const token = ++flow.flightToken;
  flow.inFlight = true;
  let rpcCalls = 0;
  try {
    const counting = async (method, params) => {
      rpcCalls += 1;
      return input.call(method, params);
    };
    const firstIdentity = sampleIdentity(input);
    if (!stillCurrent(input) || (typeof input.identity === "function" && !currentTask(firstIdentity))) {
      return closedGuide({ phase: "not_current", reads: rpcCalls, outcome: publish(flow.outcome) });
    }
    flow.taskKey = taskKeyOf(input, firstIdentity) || flow.taskKey;
    const firstNow = sampleNow(input);
    const first = await readOnce(readBody(input, counting, firstIdentity, firstNow));
    sendAuthorizedByReread(first);
    if (first.reread !== "pass") {
      return closedGuide({ phase: "first_reread", first, second: null, reads: rpcCalls, outcome: publish(flow.outcome) });
    }
    const secondIdentity = sampleIdentity(input);
    const secondNow = sampleNow(input);
    if (!stillCurrent(input) || (typeof input.identity === "function" && !currentTask(secondIdentity))) {
      return closedGuide({ phase: "not_current", first, second: null, reads: rpcCalls, outcome: publish(flow.outcome) });
    }
    const drifted = identityDrift(firstIdentity, secondIdentity);
    if (drifted) {
      return closedGuide({ phase: drifted, first, second: null, reads: rpcCalls, outcome: publish(flow.outcome) });
    }
    const second = await readOnce(readBody(input, counting, secondIdentity, secondNow));
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
        outcome: publish(flow.outcome),
      });
    }
    let preparedClaim = null;
    try {
      preparedClaim = describePreparedClaim(second);
    } catch {
      return closedGuide({ phase: "not_single_step", first, second, revision, reads: rpcCalls, outcome: publish(flow.outcome) });
    }
    const liveIdentity = sampleIdentity(input);
    const liveNow = sampleNow(input);
    if (!stillCurrent(input) || !currentTask(liveIdentity)) {
      return closedGuide({
        phase: "not_current",
        first,
        second,
        revision,
        preparedClaim,
        reads: rpcCalls,
        outcome: publish(flow.outcome),
      });
    }
    const liveDrift = identityDrift(secondIdentity, liveIdentity);
    if (liveDrift) {
      return closedGuide({
        phase: liveDrift,
        first,
        second,
        revision,
        preparedClaim,
        reads: rpcCalls,
        outcome: publish(flow.outcome),
      });
    }
    const settled = settleReread(second, {
      simulation: input.plan.simulation,
      account: liveIdentity.connectedAccount,
      chainId: liveIdentity.walletChainId,
      planDigest: liveIdentity.planDigest,
      taskRef: liveIdentity.taskRef,
      revision: liveIdentity.revision,
      nowUnix: liveNow,
    });
    if (settled.reread !== "pass") {
      return closedGuide({
        phase: "expired_before_wallet",
        first,
        second,
        revision,
        preparedClaim,
        settled,
        reads: rpcCalls,
        outcome: publish(flow.outcome),
      });
    }
    if (!sameAddress(liveIdentity.connectedAccount, preparedClaim.from)) {
      return closedGuide({
        phase: "identity_changed",
        first,
        second,
        revision,
        preparedClaim,
        reads: rpcCalls,
        outcome: publish(flow.outcome),
      });
    }
    if (typeof input.recordClaim === "function") {
      const recorded = await input.recordClaim({
        preparedClaim,
        observation: second,
        identity: secondIdentity,
        flow,
      });
      if (recorded && typeof recorded === "object") {
        return closedGuide({
          phase: recorded.phase,
          first,
          second,
          revision,
          preparedClaim,
          reads: rpcCalls,
          outcome: recorded.outcome ?? publish(flow.outcome),
          walletRequested: recorded.walletRequested === true,
          testProbe: recorded.testProbe ?? null,
          reason: recorded.reason,
        });
      }
    }
    return closedGuide({
      phase: "send_closed",
      first,
      second,
      revision,
      preparedClaim,
      reads: rpcCalls,
      reason: "reread_is_not_send_authorization",
      outcome: publish(flow.outcome),
    });
  } finally {
    if (flow.flightToken === token) flow.inFlight = false;
  }
}

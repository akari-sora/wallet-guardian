/** Confirm gate for one stored outcome. The wallet caller is injected. This module does not broadcast. */

import {
  applyWalletEvent,
  confirmBlock,
  createModeAFlow,
  emptyWalletTrack,
  guideListaModeA,
  interpretWalletReturn,
  retryAllowed,
} from "./mode-a.mjs";
import {
  outcomeKey,
  withOutcomeLock,
  writePending,
  writeRejected,
  writeRequested,
  writeUnknown,
} from "./outcome-store.mjs";
import { sameAddress } from "./trusted-lista.mjs";

function blocked(reason, extra = {}) {
  return {
    blocked: reason,
    phase: "blocked",
    reason,
    walletRequests: 0,
    walletRequested: false,
    autoRetries: 0,
    ...extra,
  };
}

function newIntentId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `intent-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

function submissionFrom(ctx) {
  const observation = ctx && ctx.observation;
  const claim = observation && observation.claim;
  const account = observation && observation.binding && observation.binding.account;
  if (!claim || typeof account !== "string") return null;
  return {
    account,
    idx: claim.idx,
    uuid: String(claim.uuid),
    amountOut: String(claim.amountOut),
    calldata: ctx.preparedClaim && ctx.preparedClaim.data,
    to: ctx.preparedClaim && ctx.preparedClaim.to,
    chainId: claim.chainId,
    taskRef: ctx.identity && ctx.identity.taskRef ? ctx.identity.taskRef : null,
    revision: ctx.identity && ctx.identity.revision != null ? ctx.identity.revision : null,
  };
}

async function confirmLocked(input, key) {
  const store = input && input.store;
  if (!store || store.blocked === "storage_unavailable") return blocked("storage_unavailable");
  const identity = input.outcomeIdentity;
  let existing = null;
  try {
    existing = await store.get(key);
  } catch (error) {
    const reason = error && error.code === "storage_unreadable" ? "verify_outcome" : "storage_unavailable";
    return blocked(reason);
  }
  const held = confirmBlock(existing);
  if (held === "verify_outcome" || held === "no_auto_retry") {
    return blocked(held, { record: existing });
  }
  if (held === "rejected" && input.userClick !== true) {
    return blocked("no_auto_retry", { record: existing, retry: false });
  }
  if (input.userClick !== true || typeof input.requestWallet !== "function") {
    return blocked("send_closed", { record: existing });
  }
  const liveAccount = input.connectedAccount ?? input.account;
  if (!sameAddress(identity.account, liveAccount)) return blocked("account_mismatch", { record: existing });
  const taskRef = input.task && input.task.ref;
  if (typeof taskRef === "string" && taskRef && taskRef !== identity.ref) return blocked("ref_mismatch", { record: existing });

  let walletRequests = 0;
  let rereadRounds = 0;
  const flow = createModeAFlow();
  const call = async (method, params) => {
    if (method === "eth_chainId") rereadRounds += 1;
    return input.call(method, params);
  };
  const recordClaim = async (ctx) => {
    const passes = ctx && ctx.observation && ctx.observation.reread === "pass";
    if (!passes || rereadRounds < 2) {
      return { phase: "blocked", reason: "reread_required", walletRequested: false, outcome: flow.outcome };
    }
    if (existing && existing.status === "rejected" && !retryAllowed({ status: "rejected", userClick: true, rereads: rereadRounds })) {
      return { phase: "blocked", reason: "retry_not_allowed", walletRequested: false, outcome: flow.outcome };
    }
    let written;
    try {
      written = await writeRequested(store, identity, {
        intent_id: newIntentId(),
        requested_at: Number.isSafeInteger(input.requestedAt) ? input.requestedAt : Date.now(),
        submission: submissionFrom(ctx),
      });
    } catch {
      return { phase: "blocked", reason: "storage_unavailable", walletRequested: false, outcome: flow.outcome };
    }
    if (!written || written.blocked) {
      return { phase: "blocked", reason: "storage_unavailable", walletRequested: false, outcome: flow.outcome };
    }
    let value;
    let error = null;
    walletRequests += 1;
    try {
      value = await input.requestWallet({ record: written.value });
    } catch (caught) {
      error = caught;
    }
    const interpreted = interpretWalletReturn(value, error);
    try {
      if (interpreted.type === "rejected") await writeRejected(store, identity, {});
      else if (interpreted.type === "submitted") await writePending(store, identity, { txHash: interpreted.txHash });
      else await writeUnknown(store, identity, {});
    } catch {
      return {
        phase: "blocked",
        reason: "storage_unavailable",
        walletRequested: true,
        outcome: applyWalletEvent(emptyWalletTrack(), interpreted),
      };
    }
    const outcome = applyWalletEvent(emptyWalletTrack(), interpreted);
    const stored = await store.get(key);
    if (stored && stored.submission) outcome.submission = stored.submission;
    if (stored && stored.txHash) outcome.txHash = stored.txHash;
    flow.outcome = outcome;
    return {
      phase: "recorded",
      reason: interpreted.type,
      outcome,
      walletRequested: true,
    };
  };

  let guided;
  try {
    guided = await guideListaModeA({
      plan: input.plan,
      task: input.task,
      account: input.account,
      connectedAccount: input.connectedAccount,
      walletChainId: input.walletChainId,
      walletIdentityVerified: input.walletIdentityVerified,
      planDigest: input.planDigest,
      revision: input.revision,
      nowUnix: input.nowUnix,
      startedAt: input.startedAt,
      call,
      testApprovals: input.testApprovals,
      clock: input.clock,
      identity: input.identity,
      isCurrent: input.isCurrent,
      flow,
      recordClaim,
    });
  } catch {
    return blocked("reread_failed", { walletRequests, record: await store.get(key).catch(() => existing) });
  }
  const record = await store.get(key).catch(() => existing);
  return {
    blocked: null,
    phase: guided.phase,
    reason: guided.reason,
    walletRequests,
    walletRequested: walletRequests > 0,
    autoRetries: 0,
    record,
    outcome: guided.outcome,
    reads: guided.reads,
    first: guided.first,
    second: guided.second,
  };
}

/**
 * One confirm attempt. Pending, unknown, and requested records block before any wallet call.
 * Rejected records proceed only when this call is a new user gesture; the guide then performs two re-reads.
 */
export async function confirmModeAOutcome(input) {
  let key;
  try {
    key = outcomeKey(input && input.outcomeIdentity);
  } catch {
    return blocked("storage_unavailable");
  }
  return withOutcomeLock(key, () => confirmLocked(input, key));
}

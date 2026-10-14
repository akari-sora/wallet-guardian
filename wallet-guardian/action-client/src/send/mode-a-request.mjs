/**
 * Test-only claim request construction. The production send_off bundle must not
 * import this module. It does not broadcast: a recorder has to mark itself and
 * the caller has to pass the test-only flag. Real sending is not implemented here.
 */
import {
  applyWalletEvent,
  describePreparedClaim,
  emptyWalletTrack,
  guideListaModeA as guideReadOnly,
  interpretWalletReturn,
  sendAuthorizedByReread,
} from "../mode-a.mjs";
import { sameAddress } from "../trusted-lista.mjs";

const DENIED_BATCH_METHOD = "wallet_sendCalls";

function hashText(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value);
}

/** A re-read object cannot carry a send flag. */
export function assertClosedReread(view) {
  sendAuthorizedByReread(view);
  if (view && Object.prototype.hasOwnProperty.call(view, "testOnlyAllowWalletRequest")) {
    throw new Error("re-read cannot carry send authorization");
  }
  return false;
}

/** Rebuild claimWithdraw(idx) from the re-read observation. The plan idx is not an input. */
export function exactClaimRequest(observation) {
  assertClosedReread(observation);
  const body = describePreparedClaim(observation);
  return {
    method: "eth_sendTransaction",
    params: [body],
  };
}

export function assertSingleStep(request) {
  if (!request || request.method === DENIED_BATCH_METHOD || request.method !== "eth_sendTransaction") {
    throw new Error("mode A does not batch");
  }
  if (!Array.isArray(request.params) || request.params.length !== 1 || Array.isArray(request.params[0].calls)) {
    throw new Error("mode A does not batch");
  }
  return request.method;
}

export function isNoBroadcastRecorder(provider) {
  return Boolean(provider)
    && provider.noBroadcast === true
    && provider.broadcasts !== true
    && typeof provider.request === "function";
}

function rememberSubmission(outcome, observation, prepared, identity) {
  if (!outcome || outcome.status !== "pending" || !hashText(outcome.txHash)) return outcome;
  const claim = observation && observation.claim;
  const account = observation && observation.binding && observation.binding.account;
  if (!claim || !sameAddress(account, account)) return outcome;
  return {
    ...outcome,
    submission: {
      txHash: outcome.txHash.toLowerCase(),
      account,
      idx: claim.idx,
      uuid: String(claim.uuid),
      amountOut: String(claim.amountOut),
      calldata: prepared.params[0].data,
      to: prepared.params[0].to,
      taskRef: identity.taskRef ?? null,
      revision: identity.revision ?? null,
      chainId: claim.chainId,
    },
  };
}

export function recordNoBroadcastClaim(input) {
  return async ({ preparedClaim, observation, identity, flow }) => {
    if (!input || input.testOnlyAllowWalletRequest !== true) return null;
    if (!isNoBroadcastRecorder(input.provider)) {
      return {
        phase: "send_closed",
        reason: "no_broadcast_provider_required",
        walletRequested: false,
        testProbe: null,
      };
    }
    const prepared = exactClaimRequest(observation);
    assertSingleStep(prepared);
    const body = prepared.params[0];
    if (body.from !== preparedClaim.from || body.to !== preparedClaim.to || body.data !== preparedClaim.data || body.value !== preparedClaim.value) {
      throw new Error("prepared claim drifted");
    }
    let value;
    let error = null;
    try {
      value = await input.provider.request(prepared);
    } catch (caught) {
      error = caught;
    }
    const outcome = rememberSubmission(
      applyWalletEvent(emptyWalletTrack(), interpretWalletReturn(value, error)),
      observation,
      prepared,
      identity,
    );
    flow.outcome = outcome;
    return {
      phase: "test_probe",
      outcome,
      walletRequested: true,
      testProbe: "recorded_not_broadcast",
      reason: "test_only_no_broadcast",
    };
  };
}

/** Read-only guide plus the test recorder path. Production main does not call this. */
export async function guideListaModeA(input) {
  return guideReadOnly({
    ...input,
    recordClaim: recordNoBroadcastClaim(input),
  });
}

/**
 * Mode A send order. Dependencies are injected so this module does not import
 * the re-read lifecycle or an outcome store. The default bundle does not import it.
 * There is no retry loop, no chain switch, and no scheduler.
 */
import { originIsApproved } from "../approved-origins.mjs";
import { interpretWalletReturn } from "../mode-a.mjs";
import { sameAddress, TRUSTED_LISTA } from "../trusted-lista.mjs";
import {
  expectedModeAClaimTx,
  issueModeAClaimCapability,
  modeAClaimDecision,
} from "./request-policy-allow.mjs";
import { walletScreenChecklist } from "./wallet-screen.mjs";

export const MODE_A_AUTH_MAX_AGE_SECONDS = 15 * 60;

function blocked(reason) {
  return { ok: false, sent: 0, reason };
}

async function runStep(fn, args) {
  if (typeof fn !== "function") return { missing: true };
  try {
    return { value: await fn(...args) };
  } catch {
    return { threw: true };
  }
}

function authTimeOf(identity) {
  if (Number.isSafeInteger(identity.authTime)) return identity.authTime;
  if (Number.isSafeInteger(identity.auth_time)) return identity.auth_time;
  return null;
}

function deadline(observation, field) {
  if (!observation || typeof observation !== "object") return null;
  if (Number.isSafeInteger(observation[field])) return observation[field];
  const binding = observation.binding;
  if (binding && Number.isSafeInteger(binding[field])) return binding[field];
  return null;
}

function rereadFailed(value) {
  if (!value || typeof value !== "object") return true;
  if (value.ok === false) return true;
  if (value.reread === "fail" || value.reread === "insufficient") return true;
  return false;
}

function settleFailed(value) {
  if (!value || typeof value !== "object") return true;
  if (value.ok === false) return true;
  if (value.reread === "fail" || value.reread === "insufficient") return true;
  return false;
}

function storedStatus(event) {
  if (event && event.type === "submitted") return "pending";
  if (event && event.type === "rejected") return "rejected";
  return "unknown";
}

/**
 * Order: click, bindIdentity, two rereads, effectsCheck, settleReread(now),
 * store requested, then one guardedRequest. Any failed step skips the request.
 */
export async function runModeASendOrder(input) {
  const source = input && typeof input === "object" ? input : {};
  const clickStep = await runStep(source.click, []);
  if (clickStep.missing || clickStep.threw) return blocked("no_click");
  const click = clickStep.value;
  if (!click || click.userGesture !== true) return blocked("no_click");
  if (Number(click.priorCode) === 4001 && click.newClick !== true) return blocked("needs_new_click");

  const identityStep = await runStep(source.bindIdentity, []);
  if (identityStep.missing || identityStep.threw || !identityStep.value || identityStep.value.ok === false) {
    return blocked("identity");
  }
  const identity = identityStep.value;
  if (identity.taskSource === "local-file" || identity.source === "local-file") return blocked("local_file_task");
  if (typeof identity.origin === "string" && identity.origin.startsWith("file:")) return blocked("local_file_task");
  if (!originIsApproved(identity.origin, { production: true })) return blocked("unapproved_origin");

  const nowUnix = typeof source.now === "function" ? source.now() : source.now;
  if (!Number.isSafeInteger(nowUnix)) return blocked("clock");
  const authTime = authTimeOf(identity);
  if (authTime == null || authTime > nowUnix || nowUnix - authTime > MODE_A_AUTH_MAX_AGE_SECONDS) {
    return blocked("stale_auth_time");
  }
  if (identity.chainId !== TRUSTED_LISTA.chainId) return blocked("wrong_chain");

  const firstStep = await runStep(source.reread, [{ round: 1 }]);
  if (firstStep.missing || firstStep.threw || rereadFailed(firstStep.value)) return blocked("reread");
  const secondStep = await runStep(source.reread, [{ round: 2 }]);
  if (secondStep.missing || secondStep.threw || rereadFailed(secondStep.value)) return blocked("reread");
  const first = firstStep.value;
  const second = secondStep.value;

  if (!sameAddress(first.account, second.account) || !sameAddress(second.account, identity.account)) {
    return blocked("account_swap");
  }
  if (first.chainId !== TRUSTED_LISTA.chainId || second.chainId !== TRUSTED_LISTA.chainId) {
    return blocked("wrong_chain");
  }
  const sender = second.sender ?? second.from;
  if (!sameAddress(sender, identity.account)) return blocked("other_sender");

  const expected = expectedModeAClaimTx({ from: identity.account, idx: second.idx });
  if (!expected) return blocked("tampered_calldata");
  const firstData = typeof first.calldata === "string" ? first.calldata.toLowerCase() : "";
  const secondData = typeof second.calldata === "string" ? second.calldata.toLowerCase() : "";
  if (first.idx !== second.idx || firstData !== expected.data || secondData !== expected.data) {
    return blocked("tampered_calldata");
  }
  if (first.to !== TRUSTED_LISTA.proxy || second.to !== TRUSTED_LISTA.proxy) return blocked("target");
  if (second.value !== "0x0" && second.value !== "0" && second.value !== 0) return blocked("value");

  const effectsStep = await runStep(source.effectsCheck, [{ identity, first, second, nowUnix }]);
  if (effectsStep.missing || effectsStep.threw || !effectsStep.value || effectsStep.value.ok !== true) {
    return blocked(effectsStep.value && effectsStep.value.reason ? effectsStep.value.reason : "effects");
  }

  const settleStep = await runStep(source.settleReread, [second, { nowUnix, now: source.now }]);
  if (settleStep.missing || settleStep.threw || settleFailed(settleStep.value)) {
    return blocked(settleStep.value && settleStep.value.reason ? settleStep.value.reason : "settle");
  }
  const approvalValidUntil = deadline(second, "approvalValidUntil");
  if (approvalValidUntil != null && nowUnix >= approvalValidUntil) return blocked("approval_expired");
  const planExpiresAt = deadline(second, "planExpiresAt");
  if (planExpiresAt != null && nowUnix >= planExpiresAt) return blocked("plan_ttl");

  const screen = walletScreenChecklist(source.walletScreen);
  if (!screen.ok) return blocked(screen.refusal || "wallet_screen");

  const readStep = await runStep(source.store, [{ op: "read", account: identity.account, idx: second.idx }]);
  if (readStep.missing || readStep.threw) return blocked("storage_unavailable");
  const existing = readStep.value;
  if (existing && existing.ok === false) return blocked(existing.reason || "storage_unavailable");
  if (existing && (existing.status === "pending" || existing.status === "requested" || existing.status === "unknown")) {
    return blocked("already_pending");
  }
  if (identity.pending === true || second.pending === true) return blocked("already_pending");

  const writeStep = await runStep(source.store, [{
    op: "write",
    status: "requested",
    account: identity.account,
    idx: second.idx,
    at: nowUnix,
  }]);
  if (writeStep.missing || writeStep.threw) return blocked("storage_unavailable");
  if (!writeStep.value || writeStep.value.ok === false) {
    const reason = writeStep.value && writeStep.value.reason === "already_pending"
      ? "already_pending"
      : "storage_unavailable";
    return blocked(reason);
  }

  const params = [expected];
  const capability = issueModeAClaimCapability({
    from: identity.account,
    idx: second.idx,
    chainId: TRUSTED_LISTA.chainId,
  });
  const decision = modeAClaimDecision("eth_sendTransaction", params, { modeAClaim: capability });
  if (!decision.allowed) return blocked(decision.reason || "params");
  if (typeof source.guardedRequest !== "function") return blocked("no_request");

  let value;
  let error = null;
  try {
    value = await source.guardedRequest({
      method: "eth_sendTransaction",
      params,
      capability,
    });
  } catch (caught) {
    error = caught;
  }
  const event = interpretWalletReturn(value, error);
  const status = storedStatus(event);
  await runStep(source.store, [{
    op: "write",
    status,
    txHash: event.txHash ?? null,
    account: identity.account,
    idx: second.idx,
  }]);
  return {
    ok: event.type === "submitted",
    sent: 1,
    reason: status,
    event,
  };
}

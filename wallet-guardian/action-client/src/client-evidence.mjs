/** Client-owned Mode A evidence. Not send authority.

 * effectsCheck is a dry-run eth_call. It is not a trace and does not
 * replace the simulation ADR 0001 requires. D-2 is the operator's decision.
 * The gate does not read server plan flags.
 */

import { blockTagFailure, canonicalBlockTag, classifyDeploymentApproval } from "./lista-reread.mjs";
import {
  TRUSTED_LISTA,
  claimCalldata,
  hexQuantity,
  isAddress,
  sameAddress,
} from "./trusted-lista.mjs";

/**
 * Suggested default from the Mode A package spec: require auth_time within
 * 15 minutes before a Mode A attempt. Operator-owned. Not a decision of D-9.
 */
export const SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS = 15 * 60;

const PRIVATE_REF = /^ref_[0-9a-f]{32}$/;
const BLOCK_HASH = /^0x[0-9a-fA-F]{64}$/;

function effectsResult(status, reason) {
  return { status, reason, dryRun: true };
}

function thrown(error) {
  if (error instanceof Error) return error;
  const wrapped = new Error(typeof error === "string" ? error : "rpc error");
  if (error && typeof error === "object") wrapped.rpcError = error.rpcError || error.error || error;
  return wrapped;
}

function isRevert(error) {
  const rpcError = error && error.rpcError;
  const code = rpcError && rpcError.code;
  if (code === 3) return true;
  const message = `${error && error.message ? error.message : ""} ${rpcError && rpcError.message ? rpcError.message : ""}`;
  return /execution reverted|\brevert\b/i.test(message);
}

function classifyEffectsError(error) {
  const wrapped = thrown(error);
  const rpcError = wrapped.rpcError;
  if (rpcError && rpcError.code === -32602) {
    return effectsResult("insufficient", "block_identity_unsupported");
  }
  const tagReason = blockTagFailure(wrapped);
  if (tagReason) return effectsResult("insufficient", tagReason);
  if (isRevert(wrapped)) return effectsResult("fail", "revert");
  return effectsResult("insufficient", "rpc_failed");
}

function pinnedBlockTag(blockTag) {
  return Boolean(blockTag) && typeof blockTag === "object" &&
    blockTag.requireCanonical === true &&
    typeof blockTag.blockHash === "string" &&
    BLOCK_HASH.test(blockTag.blockHash);
}

/** Dry-run claimWithdraw at the reread block. Does not write a server flag. */
export async function effectsCheck({ call, account, idx, blockTag } = {}) {
  if (typeof call !== "function" || !isAddress(account) || !pinnedBlockTag(blockTag)) {
    return effectsResult("insufficient", "rpc_failed");
  }
  let data;
  try {
    data = claimCalldata(idx);
  } catch {
    return effectsResult("insufficient", "rpc_failed");
  }
  try {
    const result = await call("eth_call", [{
      from: account,
      to: TRUSTED_LISTA.proxy,
      data,
    }, blockTag]);
    if (result && typeof result === "object") return classifyEffectsError(result);
    if (typeof result !== "string" || !/^0x[0-9a-fA-F]*$/.test(result)) {
      return effectsResult("insufficient", "rpc_failed");
    }
    return effectsResult("dry_run_pass", null);
  } catch (error) {
    return classifyEffectsError(error);
  }
}

function chainIdOf(value) {
  if (typeof value === "number" && Number.isSafeInteger(value)) return value;
  try {
    return hexQuantity(value);
  } catch {
    return null;
  }
}

function sameGeneration(left, right) {
  if (typeof left === "number" && Number.isSafeInteger(left)) return left === right;
  return typeof left === "string" && left.length > 0 && left === right;
}

/** Local files and synthetic examples are not a private task load. */
export function classifyTaskSource(source) {
  if (!source || typeof source !== "object") {
    return { ok: false, reason: "source_not_private_api" };
  }
  const ref = source.privateRef;
  if (source.kind !== "private_api" || typeof ref !== "string" || !PRIVATE_REF.test(ref)) {
    return { ok: false, reason: "source_not_private_api" };
  }
  if (source.path !== `/api/tasks/${ref}`) {
    return { ok: false, reason: "source_not_private_api" };
  }
  if (!sameGeneration(source.authGeneration, source.loadedAuthGeneration)) {
    return { ok: false, reason: "source_not_private_api" };
  }
  return { ok: true, reason: null, privateRef: ref };
}

/** Live eth_accounts and eth_chainId. Every listed address must be the same, on chain 56. */
export async function bindIdentity({ request, loginAddress, session, plan } = {}) {
  const mismatch = { ok: false, reason: "identity_mismatch", account: null, chainId: null };
  if (typeof request !== "function") return mismatch;
  let accounts;
  let chainValue;
  try {
    accounts = await request("eth_accounts", []);
    chainValue = await request("eth_chainId", []);
  } catch {
    return mismatch;
  }
  const live = Array.isArray(accounts) ? accounts[0] : null;
  const chainId = chainIdOf(chainValue);
  const sessionAddress = session && session.address;
  const wanted = [
    loginAddress,
    sessionAddress,
    live,
    plan && plan.sender,
    plan && plan.recipient,
    plan && plan.execution_account,
  ];
  const aligned = wanted.every((value) => sameAddress(value, loginAddress));
  if (!aligned || chainId !== TRUSTED_LISTA.chainId) {
    return { ok: false, reason: "identity_mismatch", account: isAddress(live) ? live.toLowerCase() : null, chainId };
  }
  return { ok: true, reason: null, account: loginAddress.toLowerCase(), chainId };
}

function decision(blocked, reason, reasons) {
  return {
    blocked,
    outcome: blocked ? "blocked" : "evidence_ok",
    reason,
    reasons,
    sendAllowed: false,
  };
}

function authFresh(session) {
  if (!session || typeof session !== "object" || !Number.isSafeInteger(session.auth_time)) {
    return false;
  }
  const age = Math.floor(Date.now() / 1000) - session.auth_time;
  return age >= 0 && age <= SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS;
}

function approvalStatus(approval) {
  const entries = approval && Array.isArray(approval.entries) ? approval.entries : [];
  const tuple = approval && approval.tuple;
  return classifyDeploymentApproval(entries, tuple, Math.floor(Date.now() / 1000));
}

function effectsGateReason(effects) {
  if (effects && effects.status === "dry_run_pass") return null;
  if (effects && effects.reason === "block_identity_unsupported") return "block_identity_unsupported";
  if (effects && effects.status === "fail") return "effects_fail";
  return "effects_insufficient";
}

/**
 * Send gate. Arguments are client evidence plus the caller session
 * (auth_time, address). Server plan flags are not parameters and are not read.
 * A passing result still leaves sendAllowed false.
 */
export function modeAClientGate(input) {
  const reread = input && input.reread;
  const effects = input && input.effects;
  const identity = input && input.identity;
  const approval = input && input.approval;
  const session = input && input.session;
  const reasons = [];

  if (!authFresh(session)) reasons.push("relogin_required");

  const source = classifyTaskSource(identity && identity.taskSource);
  if (!source.ok) {
    reasons.push("source_not_private_api");
  } else if (!identity || identity.ok !== true || identity.reason || identity.chainId !== TRUSTED_LISTA.chainId ||
      !sameAddress(identity.account, session && session.address)) {
    reasons.push("identity_mismatch");
  }

  if (!reread || reread.reread !== "pass") {
    const listed = reread && Array.isArray(reread.reasons) ? reread.reasons.filter((reason) => typeof reason === "string" && reason) : [];
    reasons.push(listed[0] || "reread_fail");
  }

  const pins = approvalStatus(approval);
  if (pins.status !== "active") reasons.push(pins.status);

  const effectReason = effectsGateReason(effects);
  if (effectReason) reasons.push(effectReason);

  if (reasons.length) return decision(true, reasons[0], reasons);
  return decision(false, null, []);
}

/** Read identity and, when the reread passed, the dry-run. Never sends.
 * Pin entries always come from the client registry, not from the caller or the task.
 */
export async function runModeAClientGate({
  request,
  plan,
  loginAddress,
  session,
  taskSource,
  reread,
  approval,
} = {}) {
  const bound = await bindIdentity({ request, loginAddress, session, plan });
  const identity = {
    ok: bound.ok,
    reason: bound.reason,
    account: bound.account,
    chainId: bound.chainId,
    taskSource,
  };
  let effects = effectsResult("insufficient", "reread_not_pass");
  if (reread && reread.reread === "pass" && reread.claim && reread.binding) {
    let blockTag = null;
    try {
      blockTag = canonicalBlockTag(reread.binding.blockHash);
    } catch {
      blockTag = null;
    }
    if (blockTag) {
      effects = await effectsCheck({
        call: request,
        account: bound.account || loginAddress,
        idx: reread.claim.idx,
        blockTag,
      });
    }
  }
  return modeAClientGate({
    reread,
    effects,
    identity,
    approval: {
      entries: TRUSTED_LISTA.approvals,
      tuple: approval && approval.tuple,
    },
    session,
  });
}

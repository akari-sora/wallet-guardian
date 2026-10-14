/**
 * Mode A allow branch. Only the send variant imports this module.
 * src/wallets/request-policy.mjs stays default-deny and does not import it.
 */
import { claimCalldata, TRUSTED_LISTA } from "../trusted-lista.mjs";
import { walletRequestDecision } from "../wallets/request-policy.mjs";

const BRANCH = "request-policy-allow";
const TX_KEYS = Object.freeze(["from", "to", "data", "value"]);
const capabilities = new WeakSet();

function withBranch(decision) {
  return { ...decision, branch: BRANCH };
}

/**
 * Register `record` in this module's WeakSet and return the same object.
 * A plain object the caller constructs, or an object from another realm, is not in the set.
 */
export function issueModeAClaimCapability(record) {
  if (!record || typeof record !== "object") {
    throw new TypeError("mode A claim capability requires an object");
  }
  capabilities.add(record);
  return record;
}

export function modeAClaimCapabilityIssued(record) {
  return Boolean(record) && (typeof record === "object" || typeof record === "function") && capabilities.has(record);
}

export function expectedModeAClaimTx(record) {
  if (!record || typeof record.from !== "string") return null;
  let data;
  try {
    data = claimCalldata(record.idx);
  } catch {
    return null;
  }
  return {
    from: record.from,
    to: TRUSTED_LISTA.proxy,
    data,
    value: "0x0",
  };
}

function exactTx(tx, expected) {
  if (!tx || typeof tx !== "object" || Array.isArray(tx) || !expected) return false;
  const keys = Object.keys(tx);
  if (keys.length !== TX_KEYS.length) return false;
  for (const key of TX_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(tx, key)) return false;
    if (tx[key] !== expected[key]) return false;
  }
  return true;
}

function exactClaimParams(params, capability) {
  if (!Array.isArray(params) || params.length !== 1) return false;
  return exactTx(params[0], expectedModeAClaimTx(capability));
}

/**
 * Default policy, plus one eth_sendTransaction shape.
 * wallet_sendCalls and wallet_switchEthereumChain stay denied.
 */
export function modeAClaimDecision(method, params, context = {}) {
  if (method === "wallet_sendCalls" || method === "wallet_switchEthereumChain") {
    return withBranch({ allowed: false, reason: "method_not_allowed" });
  }
  if (method === "eth_sendTransaction") {
    if (!modeAClaimCapabilityIssued(context.modeAClaim)) {
      return withBranch({ allowed: false, reason: "no_capability" });
    }
    if (!exactClaimParams(params, context.modeAClaim)) {
      return withBranch({ allowed: false, reason: "params" });
    }
    return withBranch({ allowed: true, reason: "mode_a_claim" });
  }
  return withBranch(walletRequestDecision(method, params, context));
}

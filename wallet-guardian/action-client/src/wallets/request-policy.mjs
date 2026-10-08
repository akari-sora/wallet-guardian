/**
 * WALLET-CONNECT-01 W-A4 / W-B3: one request policy for every wallet connection.
 *
 * Default deny. The page may connect, read the account and chain, look up EIP-5792 capability
 * after a click, and sign the fixed SIWE read-only login message. Nothing else: no transaction
 * send/sign, no typed data, no eth_sign, no permission grant, delegation or account upgrade.
 * A rejected login signature is final; there is no fallback to another signing method.
 */
import { acceptLoginMessage } from "../siwe-gate.mjs";

export const CONNECT_METHOD = "eth_requestAccounts";
export const LOGIN_SIGN_METHOD = "personal_sign";
export const CAPABILITY_METHOD = "wallet_getCapabilities";
export const READ_METHODS = Object.freeze(["eth_accounts", "eth_chainId"]);

/** Listed so tests and docs can show they are refused. The policy itself is default-deny. */
export const REFUSED_METHODS = Object.freeze([
  "eth_sendTransaction",
  "eth_sendRawTransaction",
  "eth_signTransaction",
  "eth_sign",
  "eth_signTypedData",
  "eth_signTypedData_v3",
  "eth_signTypedData_v4",
  "wallet_sendCalls",
  "wallet_grantPermissions",
  "wallet_requestPermissions",
  "wallet_switchEthereumChain",
  "wallet_addEthereumChain",
  "wallet_upgradeAccount",
  "eth_signAuthorization",
  "wallet_signAuthorization",
]);

export class WalletPolicyError extends Error {
  constructor(method, reason) {
    super(`wallet request refused by policy: ${reason}`);
    this.name = "WalletPolicyError";
    this.method = method;
    this.reason = reason;
  }
}

const ADDRESS = /^0x[a-fA-F0-9]{40}$/;
const HEX_PAYLOAD = /^0x(?:[0-9a-fA-F]{2})*$/;
const encoder = new TextEncoder();

function hexToBytes(hex) {
  const body = hex.slice(2);
  const out = new Uint8Array(body.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = Number.parseInt(body.slice(i * 2, i * 2 + 2), 16);
  return out;
}

/** The exact bytes a wallet will sign for a personal_sign payload (before the EIP-191 prefix). */
export function personalSignBytes(payload) {
  if (typeof payload !== "string") return null;
  if (HEX_PAYLOAD.test(payload)) return hexToBytes(payload);
  return encoder.encode(payload);
}

function sameBytes(left, right) {
  if (!left || !right || left.length !== right.length) return false;
  for (let i = 0; i < left.length; i += 1) if (left[i] !== right[i]) return false;
  return true;
}

function emptyParams(params) {
  return params === undefined || (Array.isArray(params) && params.length === 0);
}

/**
 * @param {string} method
 * @param {unknown} params
 * @param {{ userAction?: boolean, selected?: boolean, approvedMessage?: string, address?: string, nowMs?: number }} [context]
 * @returns {{ allowed: boolean, reason: string }}
 */
export function walletRequestDecision(method, params, context = {}) {
  if (typeof method !== "string") return { allowed: false, reason: "method" };
  if (READ_METHODS.includes(method)) {
    return emptyParams(params) ? { allowed: true, reason: "read" } : { allowed: false, reason: "params" };
  }
  if (method === CONNECT_METHOD) {
    if (!emptyParams(params)) return { allowed: false, reason: "params" };
    if (context.selected !== true) return { allowed: false, reason: "not_selected" };
    if (context.userAction !== true) return { allowed: false, reason: "needs_user_action" };
    return { allowed: true, reason: "connect" };
  }
  if (method === CAPABILITY_METHOD) {
    if (context.userAction !== true) return { allowed: false, reason: "needs_user_action" };
    if (params !== undefined && (!Array.isArray(params) || params.length > 2)) return { allowed: false, reason: "params" };
    return { allowed: true, reason: "capability" };
  }
  if (method === LOGIN_SIGN_METHOD) {
    if (context.userAction !== true) return { allowed: false, reason: "needs_user_action" };
    if (!Array.isArray(params) || params.length !== 2) return { allowed: false, reason: "params" };
    const [payload, address] = params;
    const approved = context.approvedMessage;
    if (typeof approved !== "string" || typeof context.address !== "string") return { allowed: false, reason: "no_fixed_login" };
    if (typeof address !== "string" || !ADDRESS.test(address) || address.toLowerCase() !== context.address.toLowerCase()) {
      return { allowed: false, reason: "address" };
    }
    const nowMs = typeof context.nowMs === "number" ? context.nowMs : Date.now();
    if (!acceptLoginMessage(approved, address, nowMs)) return { allowed: false, reason: "not_fixed_login" };
    if (!sameBytes(personalSignBytes(payload), encoder.encode(approved))) return { allowed: false, reason: "bytes_differ" };
    return { allowed: true, reason: "fixed_login" };
  }
  return { allowed: false, reason: "method_not_allowed" };
}

export function assertWalletRequest(method, params, context) {
  const decision = walletRequestDecision(method, params, context);
  if (!decision.allowed) throw new WalletPolicyError(method, decision.reason);
  return decision;
}

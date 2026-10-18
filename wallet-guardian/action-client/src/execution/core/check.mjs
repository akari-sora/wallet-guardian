/**
 * §3.5 (1) general checks. Run before any wallet request; a refusal means zero requests.
 * These checks catch honest mistakes and injections that cannot change the release.
 * They do not stop an agent that can rewrite the deployed client (PLAN §3.1).
 */
import { REGISTRY_ROLES, SELF_ROLES } from "../model.mjs";
import { decodeCall, encodeCall, leaves, parseSignature } from "./abi.mjs";
import { loadAllowlist, REVOKED_ROLE } from "./allowlist.mjs";
import { deniedSelectorCategory } from "./deny.mjs";
import { refuse } from "./errors.mjs";
import { bytesToHex, hexToBytes, isAddressHex, sameAddressHex } from "./hex.mjs";
import { isAllowedSendMethod } from "./methods.mjs";
import { ESCALATION_RULES } from "./rules.mjs";

const TX_KEYS = new Set(["from", "to", "data", "value", "chainId"]);
const EIP7702_KEYS = new Set(["authorizationList", "type"]);

function deploymentKey(chainId, address) {
  return `${chainId}:${String(address).toLowerCase()}`;
}

/**
 * Build the frozen context the checks run against: registered deployments and the allowlist.
 * Deployment: { chain_id, address, permission_type }.
 */
export function createCoreContext({ deployments = [], allowlist = [] } = {}) {
  const map = new Map();
  for (const [index, item] of deployments.entries()) {
    if (!item || !Number.isSafeInteger(item.chain_id) || item.chain_id <= 0 || !isAddressHex(item.address) || typeof item.permission_type !== "string") {
      refuse("bad_registry", `deployments[${index}]`);
    }
    const key = deploymentKey(item.chain_id, item.address);
    if (map.has(key)) refuse("bad_registry", `duplicate ${key}`);
    map.set(key, Object.freeze({ ...item }));
  }
  return Object.freeze({ deployments: map, allowlist: loadAllowlist(allowlist) });
}

function quantity(value, label) {
  if (typeof value === "bigint") return value;
  if (typeof value === "number" && Number.isSafeInteger(value)) return BigInt(value);
  if (typeof value === "string" && /^0x[0-9a-fA-F]+$/.test(value)) return BigInt(value);
  refuse(`bad_${label}`);
}

function checkBytes(leaf) {
  if (leaf.type.kind === "bytes" && leaf.value !== "0x") refuse("nonempty_bytes", leaf.path);
  if (leaf.type.kind === "array" && leaf.arrayLength > 0) {
    let element = leaf.type.element;
    while (element.kind === "array") element = element.element;
    if (element.kind === "bytes") refuse("nonempty_bytes", leaf.path);
  }
}

function checkRole(leaf, entry, account, chainId, context) {
  if (leaf.type.kind !== "address") return;
  const role = entry.roles[leaf.path.replace(/\[\d+\]/g, "")];
  if (role === undefined) refuse("address_role_missing", leaf.path);
  if (SELF_ROLES.includes(role)) {
    if (!sameAddressHex(leaf.value, account)) refuse(`${role}_not_self`, leaf.path);
    return;
  }
  if (REGISTRY_ROLES.includes(role)) {
    if (!context.deployments.has(deploymentKey(chainId, leaf.value))) refuse(`${role}_not_registered`, leaf.path);
    return;
  }
  if (role !== REVOKED_ROLE) refuse("address_role_missing", leaf.path);
}

/**
 * Check one transaction object against the context. `intent.signature` is the full
 * signature (with parameter names) the builder meant to encode.
 */
export function checkTransaction(tx, { account, signature } = {}, context) {
  if (!context || !(context.deployments instanceof Map) || !(context.allowlist instanceof Map)) refuse("bad_context");
  if (!tx || typeof tx !== "object" || Array.isArray(tx)) refuse("bad_transaction");
  for (const key of Object.keys(tx)) {
    if (EIP7702_KEYS.has(key)) refuse("eip7702_refused", key);
    if (!TX_KEYS.has(key)) refuse("unknown_field", key);
  }
  if (!isAddressHex(account)) refuse("bad_account");
  if (!sameAddressHex(tx.from, account)) refuse("from_not_self");
  const chainId = quantity(tx.chainId, "chain_id");
  if (chainId <= 0n || chainId > BigInt(Number.MAX_SAFE_INTEGER)) refuse("bad_chain_id");
  if (!Object.prototype.hasOwnProperty.call(tx, "value")) refuse("value_missing");
  if (quantity(tx.value, "value") !== 0n) refuse("nonzero_value");
  if (!isAddressHex(tx.to)) refuse("bad_to");
  if (sameAddressHex(tx.to, account)) refuse("to_is_self");
  const deployment = context.deployments.get(deploymentKey(Number(chainId), tx.to));
  if (!deployment) refuse("to_not_registered");
  const bytes = hexToBytes(tx.data, "data");
  if (bytes.length < 4) refuse("bad_calldata", "no selector");
  const selector = bytesToHex(bytes.slice(0, 4));
  const denied = deniedSelectorCategory(selector);
  if (denied) refuse(`denied_${denied}`, selector);
  const entry = context.allowlist.get(`${deployment.permission_type}:${selector}`);
  if (!entry) {
    const other = [...context.allowlist.values()].some((item) => item.parsed.selector === selector);
    refuse(other ? "signature_mismatch" : "function_not_allowlisted", selector);
  }
  const intended = parseSignature(signature);
  if (intended.full !== entry.parsed.full) refuse("signature_mismatch", `${intended.full} vs ${entry.parsed.full}`);
  const values = decodeCall(entry.parsed, tx.data);
  for (const leaf of leaves(entry.parsed.params, values)) {
    checkBytes(leaf);
    checkRole(leaf, entry, account, Number(chainId), context);
  }
  const rule = ESCALATION_RULES[entry.parsed.canonical];
  if (rule) rule(values);
  return Object.freeze({
    chainId: Number(chainId),
    to: tx.to,
    selector,
    signature: entry.parsed.full,
    permission_type: deployment.permission_type,
  });
}

/** Full request check: the one allowed send method, one transaction, then checkTransaction. */
export function assertSendable(request, intent, context) {
  if (!request || typeof request !== "object") refuse("bad_request");
  if (!isAllowedSendMethod(request.method)) refuse("method_not_allowed");
  if (!Array.isArray(request.params) || request.params.length !== 1) refuse("bad_request", "params");
  return checkTransaction(request.params[0], intent, context);
}

/** Build a transaction from a full signature and arguments, then run the checks. value is always 0. */
export function buildTransaction({ account, chainId, to, signature, args }, context) {
  const parsed = parseSignature(signature);
  if (!Number.isSafeInteger(chainId) || chainId <= 0) refuse("bad_chain_id");
  const tx = Object.freeze({
    from: account,
    to,
    data: encodeCall(parsed, args),
    value: "0x0",
    chainId: `0x${chainId.toString(16)}`,
  });
  checkTransaction(tx, { account, signature: parsed.full }, context);
  return tx;
}

/** Shared constants. Keep them aligned with guardian.execution.model. */

import { toChecksumAddress } from "./checksum.mjs";
import { ExecutionRegistryError } from "./errors.mjs";

export const REGISTRY_VERSION = "guardian.execution_registry.v1";
export const CHAINS_VERSION = "guardian.execution_chains.v1";
export const CANDIDATES_VERSION = "guardian.execution_candidates.v1";
export const TRUST_VERSION = "guardian.trust_critical_paths.v1";
export const RULES_VERSION = "guardian.execution_candidate_rules.v1";

export const UINT256_MAX = (1n << 256n) - 1n;
export const EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc";

export const PROXY_KINDS = ["none", "eip1967_impl"];
export const PERMISSION_TYPES = [
  "erc20_allowance",
  "erc721_operator",
  "erc721_token",
  "permit2",
  "credit_delegation",
  "aave_debt_token",
  "morpho_like",
  "erc4626_vault",
  "queue_vault",
];
export const ADDRESS_ROLES = ["receiver", "onBehalf", "owner", "spender", "target"];
export const SELF_ROLES = ["receiver", "onBehalf", "owner"];
export const REGISTRY_ROLES = ["spender", "target"];
export const SOLIDITY_TYPES = ["address", "address payable", "uint256", "bool", "bytes"];
export const ADDRESS_TYPES = ["address", "address payable"];
export const ACTIONS = [
  "revoke_erc20",
  "revoke_erc721_operator",
  "revoke_erc721_token",
  "revoke_credit_delegation",
  "revoke_morpho",
  "erc4626_redeem",
  "aave_withdraw",
  "moolah_withdraw_collateral",
  "queue_request",
  "queue_claim",
  "aave_repay",
  "moolah_repay",
];
export const IDENTITY_FIELDS = [
  "from",
  "onBehalf",
  "receiver",
  "transfer_to",
  "eth_accounts_0",
  "plan_sender",
  "plan_recipient",
  "execution_account",
];
export const TRUST_NOTE = "Changing this list is a trust-critical review change. The list is not a boundary.";
export const TRUST_PATH = "wallet-guardian/config/execution/trust-critical-paths.json";
export const REQUIRED_TRUST_PATHS = [
  "wallet-guardian/config/execution/",
  TRUST_PATH,
  "wallet-guardian/action-client/src/execution/",
  "wallet-guardian/action-client/src/send/",
  "wallet-guardian/action-client/src/wallets/",
  "wallet-guardian/action-client/src/mode-a.mjs",
  "wallet-guardian/action-client/vite.config.ts",
  "wallet-guardian/action-client/package.json",
  "wallet-guardian/action-client/package-lock.json",
  "wallet-guardian/action-client/tools/",
  "wallet-guardian/action-client/deploy/release.schema.json",
];
export const REQUIRED_TRUST_CATEGORIES = [
  "registry_and_policy",
  "client_core",
  "send_path",
  "guided_flow",
  "build",
  "release_schema",
];
export const RELATIONSHIPS = [
  { id: "monitor_chains_json", path: "config/chains.json", purpose: "monitor_enabled_declaration", execution_rpc: false },
  { id: "planned_network_registry", path: "config/chains/", purpose: "mcm01_planned_registry", execution_rpc: false },
  { id: "wheel_resource_copy", path: "src/guardian/_resources/config/", purpose: "installed_monitor_copy", execution_rpc: false },
  { id: "execution_rpc_catalog", path: "config/execution/chains.json", purpose: "execution_rpc_catalog", execution_rpc: true },
];

const DATE = /^20[0-9]{2}-[01][0-9]-[0-3][0-9]$/;
const TIME = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;
const UINT = /^(0|[1-9][0-9]*)$/;
const HASH64 = /^[0-9a-f]{64}$/;
const BLOCK_HASH = /^0x[0-9a-f]{64}$/;
const RELATIVE_PATH = /^[A-Za-z0-9_./-]+$/;

export function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function expectObject(value, allowed, required, label) {
  if (!isObject(value)) throw new ExecutionRegistryError("bad_type", label);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new ExecutionRegistryError("unknown_field", `${label}.${key}`);
  }
  for (const key of required) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw new ExecutionRegistryError("missing_field", `${label}.${key}`);
    }
  }
  return value;
}

export function requireChecksum(value, label) {
  let checksum;
  try {
    checksum = toChecksumAddress(value);
  } catch (error) {
    if (error instanceof ExecutionRegistryError) throw new ExecutionRegistryError("bad_address", label);
    throw error;
  }
  if (checksum !== value) throw new ExecutionRegistryError("address_not_checksum", label);
  return checksum;
}

export function requireUintString(value, label) {
  if (typeof value !== "string" || !UINT.test(value)) throw new ExecutionRegistryError("bad_integer", label);
  if (BigInt(value) > UINT256_MAX) throw new ExecutionRegistryError("out_of_range", label);
  return value;
}

export function requireHttpsUrl(value, label) {
  if (typeof value !== "string" || !value.startsWith("https://") || value.length > 400) {
    throw new ExecutionRegistryError("bad_url", label);
  }
  if ([...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) {
    throw new ExecutionRegistryError("control_character", label);
  }
  const host = value.slice(8).split("/", 1)[0];
  if (host === "" || host.includes("@") || value.includes(" ") || value.includes("\\")) {
    throw new ExecutionRegistryError("bad_url", label);
  }
  const lowered = value.toLowerCase();
  if (lowered.includes("apikey=") || lowered.includes("glpat-") || lowered.includes("nodereal.io/v1/")) {
    throw new ExecutionRegistryError("bad_url", label);
  }
  return value;
}

function validDateParts(value) {
  const [year, month, day] = value.split("-").map((part) => Number(part));
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

export function requireDate(value, label) {
  if (typeof value !== "string" || !DATE.test(value) || !validDateParts(value)) {
    throw new ExecutionRegistryError("bad_date", label);
  }
  return value;
}

export function requireTime(value, label) {
  if (typeof value !== "string" || !TIME.test(value)) throw new ExecutionRegistryError("bad_time", label);
  const [day, clock] = value.slice(0, -1).split("T");
  if (!validDateParts(day)) throw new ExecutionRegistryError("bad_time", label);
  const [hour, minute, second] = clock.split(":").map((part) => Number(part));
  if (hour > 23 || minute > 59 || second > 59) throw new ExecutionRegistryError("bad_time", label);
  return value;
}

export function requireText(value, label, maxLen = 400) {
  if (typeof value !== "string" || value === "" || value.length > maxLen) {
    throw new ExecutionRegistryError("bad_type", label);
  }
  if ([...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) {
    throw new ExecutionRegistryError("control_character", label);
  }
  return value;
}

export function requireRelativePath(value, label) {
  const text = requireText(value, label, 240);
  if (!RELATIVE_PATH.test(text) || text.split("/").includes("..")) {
    throw new ExecutionRegistryError("bad_path", label);
  }
  return text;
}

export function requireHash64(value, label) {
  if (typeof value !== "string" || !HASH64.test(value)) throw new ExecutionRegistryError("bad_hash", label);
  return value;
}

export function requireBlockHash(value, label) {
  if (typeof value !== "string" || !BLOCK_HASH.test(value)) throw new ExecutionRegistryError("bad_hash", label);
  return value;
}

export function requireChainId(value, label) {
  if (typeof value !== "number" || !Number.isInteger(value)) throw new ExecutionRegistryError("bad_integer", label);
  if (value < 1) throw new ExecutionRegistryError("out_of_range", label);
  return value;
}

export function rejectInertApproval(value) {
  if (isObject(value)) {
    for (const [key, item] of Object.entries(value)) {
      if (key === "approved_by_user") throw new ExecutionRegistryError("approved_by_user_has_no_effect", key);
      if (key === "user_approved" && item === true) throw new ExecutionRegistryError("user_approved_has_no_effect", key);
      if (key === "own_addresses" || (key === "own_address_list" && item !== false)) {
        throw new ExecutionRegistryError("own_address_list_rejected", key);
      }
      rejectInertApproval(item);
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) rejectInertApproval(item);
  }
}

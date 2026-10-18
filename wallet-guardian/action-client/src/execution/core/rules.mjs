/**
 * Zero rules (escalation rules) by canonical signature. A function whose counterparty
 * may be any address (`revoked` role) is only safe when its amount is forced to 0 or false.
 * ERC-721 `approve` uses ERC-20 semantics until S-4 (§3.5 L263).
 */
import { UINT256_MAX } from "../model.mjs";
import { refuse } from "./errors.mjs";

export const ESCALATION_RULES = Object.freeze({
  "approve(address,uint256)": (values) => {
    if (values[1] === UINT256_MAX) refuse("unlimited_approve");
    if (values[1] !== 0n) refuse("nonzero_approve");
  },
  "setApprovalForAll(address,bool)": (values) => {
    if (values[1] !== false) refuse("approval_for_all_true");
  },
  "approveDelegation(address,uint256)": (values) => {
    if (values[1] !== 0n) refuse("nonzero_delegation");
  },
  "setAuthorization(address,bool)": (values) => {
    if (values[1] !== false) refuse("authorization_true");
  },
});

/** Names that grant or change a permission. Without a zero rule they cannot be allowlisted (review 21 F1). */
export const PERMISSION_NAME_PREFIXES = Object.freeze([
  "approve",
  "setapproval",
  "setoperator",
  "setauthoriz",
  "approvedelegation",
  "setdelegate",
]);

export function hasZeroRule(canonical) {
  return Object.prototype.hasOwnProperty.call(ESCALATION_RULES, canonical);
}

export function isPermissionName(name) {
  const lower = String(name).toLowerCase();
  return PERMISSION_NAME_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

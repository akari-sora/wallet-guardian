/**
 * Zero rules (escalation rules) by permission_type:canonical signature (§3.5 L259, L263; §6.2 L795–799).
 * A function whose counterparty may be any address (`revoked` role) is only safe when its
 * parameter rule forces a revocation (amount 0, approved false, or ERC-721 recipient address(0)).
 */
import { UINT256_MAX } from "../model.mjs";
import { refuse } from "./errors.mjs";
import { sameAddressHex, selectorOfCanonical } from "./hex.mjs";

export const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";

function checkErc20Approve(values) {
  if (values[1] === UINT256_MAX) refuse("unlimited_approve");
  if (values[1] !== 0n) refuse("nonzero_approve");
}

function checkErc721Approve(values) {
  if (!sameAddressHex(values[0], ZERO_ADDRESS)) refuse("nonzero_approve");
}

function checkSetApprovalForAll(values) {
  if (values[1] !== false) refuse("approval_for_all_true");
}

function checkApproveDelegation(values) {
  if (values[1] !== 0n) refuse("nonzero_delegation");
}

function checkSetAuthorization(values) {
  if (values[1] !== false) refuse("authorization_true");
}

export const ESCALATION_RULES = Object.freeze({
  "erc20_allowance:approve(address,uint256)": checkErc20Approve,
  "erc721_token:approve(address,uint256)": checkErc721Approve,
  "erc721_operator:setApprovalForAll(address,bool)": checkSetApprovalForAll,
  "erc721_token:setApprovalForAll(address,bool)": checkSetApprovalForAll,
  "aave_debt_token:approveDelegation(address,uint256)": checkApproveDelegation,
  "credit_delegation:approveDelegation(address,uint256)": checkApproveDelegation,
  "morpho_like:setAuthorization(address,bool)": checkSetAuthorization,
});

/** Names that grant or change a permission. Without a zero rule they cannot be allowlisted (review 21 F1). */
export const PERMISSION_NAME_PREFIXES = Object.freeze([
  "approve",
  "setapproval",
  "setoperator",
  "setauthoriz",
  "approvedelegation",
  "setdelegate",
  "increaseapproval",
  "authorizeoperator",
]);

/** Selectors of permission-granting functions. A non-permission name with one of these selectors is refused at load. */
export const PERMISSION_SELECTORS = Object.freeze(
  [
    "approve(address,uint256)",
    "setApprovalForAll(address,bool)",
    "approve(address,uint256,uint256)",
    "setOperator(address,bool)",
    "approveDelegation(address,uint256)",
    "setAuthorization(address,bool)",
  ].map(selectorOfCanonical),
);

const PERMISSION_SELECTOR_SET = new Set(PERMISSION_SELECTORS);

export function escalationRule(permissionType, canonical) {
  const key = `${permissionType}:${canonical}`;
  return Object.prototype.hasOwnProperty.call(ESCALATION_RULES, key) ? ESCALATION_RULES[key] : null;
}

export function hasZeroRule(permissionType, canonical) {
  return escalationRule(permissionType, canonical) !== null;
}

export function isPermissionName(name) {
  const lower = String(name).toLowerCase();
  return PERMISSION_NAME_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

export function isPermissionSelector(selector) {
  return PERMISSION_SELECTOR_SET.has(String(selector).toLowerCase());
}

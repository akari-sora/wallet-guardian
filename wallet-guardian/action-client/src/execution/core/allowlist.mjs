/**
 * Default-deny function allowlist for the client core.
 * An entry names a permission type, a full signature with parameter names,
 * the declared selector, and a role for every address parameter.
 */
import { PERMISSION_TYPES, REGISTRY_ROLES, SELF_ROLES } from "../model.mjs";
import { parseSignature } from "./abi.mjs";
import { deniedName, deniedSelectorCategory } from "./deny.mjs";
import { refuse } from "./errors.mjs";
import { hasZeroRule, isPermissionName } from "./rules.mjs";

/**
 * `revoked` is the counterparty of a revocation (§6.2): any address, but the amount must be 0 or false.
 * Only functions with a zero rule (rules.mjs) may use it; loadEntry refuses the rest (review 21 F1).
 */
export const REVOKED_ROLE = "revoked";
export const CORE_ROLES = Object.freeze([...SELF_ROLES, ...REGISTRY_ROLES, REVOKED_ROLE]);

const ENTRY_KEYS = new Set(["permission_type", "signature", "selector", "roles"]);

function addressPaths(params, prefix = "") {
  const paths = [];
  for (const param of params) {
    const path = prefix ? `${prefix}.${param.name}` : param.name;
    let type = param.type;
    while (type.kind === "array") type = type.element;
    if (type.kind === "tuple") paths.push(...addressPaths(type.components, path));
    else if (type.kind === "address") paths.push(path);
  }
  return paths;
}

function namedEverywhere(params) {
  return params.every((param) => {
    if (!param.name) return false;
    let type = param.type;
    while (type.kind === "array") type = type.element;
    return type.kind !== "tuple" || namedEverywhere(type.components);
  });
}

function loadEntry(entry, index) {
  const label = `allowlist[${index}]`;
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) refuse("bad_allowlist", label);
  for (const key of Object.keys(entry)) if (!ENTRY_KEYS.has(key)) refuse("bad_allowlist", `${label}: unknown field ${key}`);
  for (const key of ENTRY_KEYS) if (!Object.prototype.hasOwnProperty.call(entry, key)) refuse("bad_allowlist", `${label}: missing ${key}`);
  if (!PERMISSION_TYPES.includes(entry.permission_type)) refuse("bad_allowlist", `${label}: permission_type`);
  if (entry.permission_type === "permit2") refuse("denied_function", `${label}: permit2`);
  const parsed = parseSignature(entry.signature);
  if (!namedEverywhere(parsed.params)) refuse("bad_allowlist", `${label}: every parameter needs a name`);
  if (deniedName(parsed.name)) refuse("denied_function", `${label}: ${parsed.name}`);
  if (deniedSelectorCategory(parsed.selector)) refuse("denied_function", `${label}: ${parsed.canonical}`);
  if (typeof entry.selector !== "string" || entry.selector.toLowerCase() !== parsed.selector) {
    refuse("selector_mismatch", `${label}: ${parsed.canonical} is ${parsed.selector}`);
  }
  const roles = entry.roles;
  if (!roles || typeof roles !== "object" || Array.isArray(roles)) refuse("bad_allowlist", `${label}: roles`);
  const paths = addressPaths(parsed.params);
  for (const path of paths) {
    if (!Object.prototype.hasOwnProperty.call(roles, path)) refuse("address_role_missing", `${label}: ${path}`);
  }
  for (const [path, role] of Object.entries(roles)) {
    if (!paths.includes(path)) refuse("bad_allowlist", `${label}: role for unknown address ${path}`);
    if (!CORE_ROLES.includes(role)) refuse("bad_allowlist", `${label}: role ${role}`);
  }
  // F1: a revoked counterparty is only safe when the amount is forced to 0 or false.
  if (Object.values(roles).includes(REVOKED_ROLE) && !hasZeroRule(parsed.canonical)) {
    refuse("revoked_role_without_zero_rule", `${label}: ${parsed.canonical}`);
  }
  // F1: approve/setApproval/setOperator/setAuthoriz/approveDelegation/setDelegate need a zero rule.
  if (isPermissionName(parsed.name) && !hasZeroRule(parsed.canonical)) {
    refuse("permission_function_without_zero_rule", `${label}: ${parsed.canonical}`);
  }
  return Object.freeze({
    permission_type: entry.permission_type,
    parsed,
    roles: Object.freeze({ ...roles }),
  });
}

/**
 * Load an allowlist. Refuses denied functions, wrong selectors, missing roles,
 * and selector collisions (same selector, different canonical signature).
 */
export function loadAllowlist(entries) {
  if (!Array.isArray(entries)) refuse("bad_allowlist", "not an array");
  const byKey = new Map();
  const canonicalBySelector = new Map();
  entries.forEach((raw, index) => {
    const entry = loadEntry(raw, index);
    const known = canonicalBySelector.get(entry.parsed.selector);
    if (known !== undefined && known !== entry.parsed.canonical) {
      refuse("selector_collision", `${entry.parsed.selector}: ${known} vs ${entry.parsed.canonical}`);
    }
    canonicalBySelector.set(entry.parsed.selector, entry.parsed.canonical);
    const key = `${entry.permission_type}:${entry.parsed.selector}`;
    if (byKey.has(key)) refuse("duplicate_entry", key);
    byKey.set(key, entry);
  });
  return byKey;
}

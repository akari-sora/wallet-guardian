/** Identity binding. One account per call. No list of own addresses. */

import { ExecutionRegistryError } from "./errors.mjs";
import {
  ADDRESS_TYPES,
  IDENTITY_FIELDS,
  REGISTRY_ROLES,
  SELF_ROLES,
  requireChecksum,
  requireUintString,
} from "./model.mjs";

export function checkIdentity(account, identity) {
  if (identity === null || typeof identity !== "object" || Array.isArray(identity)) {
    throw new ExecutionRegistryError("bad_type", "identity");
  }
  if (
    Object.prototype.hasOwnProperty.call(identity, "own_addresses")
    || (Object.prototype.hasOwnProperty.call(identity, "own_address_list") && identity.own_address_list !== false)
  ) {
    throw new ExecutionRegistryError("own_address_list_rejected", "identity");
  }
  const accountCs = requireChecksum(account, "account");
  const allowed = new Set([...IDENTITY_FIELDS, "siwe_address"]);
  for (const key of Object.keys(identity)) {
    if (!allowed.has(key)) throw new ExecutionRegistryError("unknown_field", `identity.${key}`);
  }
  for (const key of IDENTITY_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(identity, key)) {
      throw new ExecutionRegistryError("missing_field", `identity.${key}`);
    }
    if (requireChecksum(identity[key], `identity.${key}`) !== accountCs) {
      const code = key === "receiver" || key === "transfer_to" ? "recipient_not_self" : "identity_mismatch";
      throw new ExecutionRegistryError(code, key);
    }
  }
  if (Object.prototype.hasOwnProperty.call(identity, "siwe_address") && identity.siwe_address !== null) {
    if (requireChecksum(identity.siwe_address, "identity.siwe_address") !== accountCs) {
      throw new ExecutionRegistryError("identity_mismatch", "siwe_address");
    }
  }
}

export function retainAfterAccountSwitch(state) {
  if (state === null || typeof state !== "object" || Array.isArray(state)) {
    throw new ExecutionRegistryError("bad_type", "account_switch");
  }
  const allowed = ["evidence", "pending", "unknown"];
  for (const key of Object.keys(state)) {
    if (!allowed.includes(key)) throw new ExecutionRegistryError("unknown_field", `account_switch.${key}`);
  }
  for (const key of allowed) {
    if (!Object.prototype.hasOwnProperty.call(state, key)) {
      throw new ExecutionRegistryError("missing_field", `account_switch.${key}`);
    }
  }
  return { pending: state.pending, unknown: state.unknown };
}

export function checkCall(registry, { policy_id: policyId, account, identity, arguments: args }) {
  checkIdentity(account, identity);
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    throw new ExecutionRegistryError("bad_type", "arguments");
  }
  const policies = new Map(registry.policies.map((item) => [item.id, item]));
  const policy = policies.get(policyId);
  if (policy === undefined) throw new ExecutionRegistryError("unknown_policy", policyId);
  if (policy.recipient_rule !== "self") throw new ExecutionRegistryError("recipient_not_self", policyId);
  if (policy.record_status !== "active") throw new ExecutionRegistryError("not_registered", policyId);
  const names = new Set(policy.parameters.map((item) => item.name));
  const given = new Set(Object.keys(args));
  if (given.size !== names.size || [...names].some((name) => !given.has(name))) {
    throw new ExecutionRegistryError("argument_mismatch", policyId);
  }
  const accountCs = requireChecksum(account, "account");
  const active = registry.deployments.filter((item) => item.chain_id === policy.chain_id && item.record_status === "active");
  const byAddress = new Map(active.map((item) => [item.address.toLowerCase(), item]));
  const allowed = new Set(policy.allowed_spender_ids);
  for (const param of policy.parameters) {
    const value = args[param.name];
    const solidityType = param.solidity_type;
    if (ADDRESS_TYPES.includes(solidityType)) {
      const address = requireChecksum(value, param.name);
      const role = param.role;
      if (SELF_ROLES.includes(role)) {
        if (address !== accountCs) {
          const code = role === "receiver" ? "recipient_not_self" : "identity_mismatch";
          throw new ExecutionRegistryError(code, param.name);
        }
      } else if (REGISTRY_ROLES.includes(role)) {
        const found = byAddress.get(address.toLowerCase());
        if (found === undefined || (role === "spender" && !allowed.has(found.id))) {
          throw new ExecutionRegistryError("not_registered", param.name);
        }
      } else {
        throw new ExecutionRegistryError("bad_enum", role);
      }
    } else if (solidityType === "uint256") {
      requireUintString(value, param.name);
    } else if (solidityType === "bool") {
      if (typeof value !== "boolean") throw new ExecutionRegistryError("bad_type", param.name);
    } else if (solidityType === "bytes") {
      if (typeof value !== "string") throw new ExecutionRegistryError("bad_type", param.name);
    } else {
      throw new ExecutionRegistryError("bad_enum", solidityType);
    }
  }
}

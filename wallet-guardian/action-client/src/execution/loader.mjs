/** Strict loader. It does not build or send transactions, and it never grants approval. */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ExecutionRegistryError } from "./errors.mjs";
import {
  ACTIONS,
  ADDRESS_ROLES,
  ADDRESS_TYPES,
  CANDIDATES_VERSION,
  CHAINS_VERSION,
  PERMISSION_TYPES,
  PROXY_KINDS,
  REGISTRY_VERSION,
  RELATIONSHIPS,
  REQUIRED_TRUST_CATEGORIES,
  REQUIRED_TRUST_PATHS,
  SOLIDITY_TYPES,
  TRUST_NOTE,
  TRUST_PATH,
  TRUST_VERSION,
  expectObject,
  rejectInertApproval,
  requireBlockHash,
  requireChainId,
  requireChecksum,
  requireDate,
  requireHash64,
  requireHttpsUrl,
  requireRelativePath,
  requireText,
  requireTime,
  requireUintString,
} from "./model.mjs";
import { parseStrictJson } from "./strict-json.mjs";

const CHAIN_KEYS = new Set(["chain_id", "network_id", "status", "revoked_at", "rpc"]);
const RPC_KEYS = new Set(["url", "operator", "source"]);
const SOURCE_KEYS = new Set(["url", "retrieved"]);
const IMPL_KEYS = new Set(["address", "runtime_hash"]);
const DEPLOYMENT_KEYS = new Set([
  "id", "chain_id", "network_id", "address", "protocol_id", "protocol_version", "permission_type",
  "proxy_kind", "proxy_kind_basis", "implementation", "runtime_hash", "source_block_number",
  "source_block_hash", "pin_status", "assertions", "valid_until", "official_sources", "record_status",
  "revoked_at", "approval", "user_approved", "source_files",
]);
const POLICY_KEYS = new Set([
  "id", "chain_id", "deployment_id", "market_id", "position_id", "asset_deployment_id",
  "allowed_spender_ids", "action", "recipient_rule", "spend_cap", "allowance_cap", "loss_cap",
  "penalty_cap", "amount_rule", "parameters", "record_status", "revoked_at", "approval", "user_approved",
]);
const PARAM_KEYS = new Set(["name", "solidity_type", "role"]);
const IDENTITY_KEYS = new Set(["rule", "siwe_address", "own_address_list", "on_account_switch"]);
const SWITCH_KEYS = new Set(["clear", "keep"]);
const UNCLASSIFIED_KEYS = new Set([
  "approval", "user_approved", "protocol_id", "protocol_version", "network_id", "chain_id",
  "address_family", "role", "address", "reason", "official_sources", "source_file",
]);
const SKIPPED_KEYS = new Set(["address", "network_id", "protocol_id", "reason", "role", "source_file"]);
const CANDIDATE_REASONS = new Set([
  "proxy_kind_not_documented", "not_evm", "proxy_basis_missing", "duplicate_address_conflict",
  "official_source_missing", "bad_address", "unknown_network", "unrecognized_protocol_file",
]);

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function loadChainsText(text) {
  return chains(parsedObject(text));
}

export function loadRegistryText(text, chainDocument) {
  return registry(parsedObject(text), chainDocument);
}

export function loadTrustText(text) {
  return trust(parsedObject(text));
}

export function loadCandidatesText(text) {
  return candidates(parsedObject(text));
}

export function loadExecutionDirectory(directory) {
  const chainsDocument = loadChainsText(readFileSync(join(directory, "chains.json"), "utf8"));
  const registryDocument = loadRegistryText(readFileSync(join(directory, "registry.json"), "utf8"), chainsDocument);
  const trustDocument = loadTrustText(readFileSync(join(directory, "trust-critical-paths.json"), "utf8"));
  return { chains: chainsDocument, registry: registryDocument, trust_critical_paths: trustDocument };
}

export function userApprovalGranted(text, chainsText = null) {
  try {
    const parsed = parseStrictJson(text);
    if (!isPlain(parsed)) return false;
    const version = parsed.schema_version;
    let loaded;
    if (version === REGISTRY_VERSION) {
      if (chainsText == null) return false;
      loaded = loadRegistryText(text, loadChainsText(chainsText));
    } else if (version === CANDIDATES_VERSION) {
      loaded = loadCandidatesText(text);
    } else {
      return false;
    }
    if (loaded.user_approved === true) return true;
    for (const key of ["deployments", "policies", "unclassified"]) {
      for (const item of loaded[key] ?? []) {
        if (item && item.user_approved === true) return true;
      }
    }
    return false;
  } catch (error) {
    if (error instanceof ExecutionRegistryError) return false;
    throw error;
  }
}

function isPlain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parsedObject(text) {
  const parsed = parseStrictJson(text);
  rejectInertApproval(parsed);
  if (!isPlain(parsed)) throw new ExecutionRegistryError("bad_type", "document");
  return parsed;
}

function chains(document) {
  const allowed = new Set(["schema_version", "purpose", "enables_send", "relationships", "chains"]);
  expectObject(document, allowed, allowed, "chains");
  if (document.schema_version !== CHAINS_VERSION) throw new ExecutionRegistryError("bad_enum", "chains.schema_version");
  if (document.purpose !== "execution_rpc_catalog") throw new ExecutionRegistryError("bad_enum", "chains.purpose");
  if (document.enables_send !== false) throw new ExecutionRegistryError("enables_send_rejected", "chains.enables_send");
  if (!sameJson(document.relationships, RELATIONSHIPS)) {
    throw new ExecutionRegistryError("catalog_relationship", "chains.relationships");
  }
  if (!Array.isArray(document.chains)) throw new ExecutionRegistryError("bad_type", "chains.chains");
  const rows = document.chains.map((item, index) => chain(item, index));
  rows.sort((left, right) => left.chain_id - right.chain_id);
  const seen = new Set();
  for (const item of rows) {
    if (seen.has(item.chain_id)) throw new ExecutionRegistryError("duplicate_key", `chain_id:${item.chain_id}`);
    seen.add(item.chain_id);
  }
  return {
    chains: rows,
    enables_send: false,
    purpose: "execution_rpc_catalog",
    relationships: RELATIONSHIPS.map((item) => ({ ...item })),
    schema_version: CHAINS_VERSION,
  };
}

function chain(value, index) {
  const label = `chains[${index}]`;
  const item = expectObject(value, CHAIN_KEYS, CHAIN_KEYS, label);
  const chainId = requireChainId(item.chain_id, `${label}.chain_id`);
  const networkId = requireText(item.network_id, `${label}.network_id`, 80);
  const stripped = networkId.replaceAll("-", "");
  if (!/^[0-9a-z]+$/.test(stripped) || networkId !== networkId.toLowerCase()) {
    throw new ExecutionRegistryError("bad_type", `${label}.network_id`);
  }
  if (item.status !== "active" && item.status !== "revoked") {
    throw new ExecutionRegistryError("bad_enum", `${label}.status`);
  }
  if (!Array.isArray(item.rpc)) throw new ExecutionRegistryError("bad_type", `${label}.rpc`);
  const rpc = item.rpc.map((entry, rpcIndex) => rpcEntry(entry, `${label}.rpc[${rpcIndex}]`));
  let revokedAt = null;
  if (item.status === "active") {
    if (item.revoked_at !== null) throw new ExecutionRegistryError("bad_type", `${label}.revoked_at`);
    if (rpc.length === 0) throw new ExecutionRegistryError("missing_field", `${label}.rpc`);
  } else {
    revokedAt = requireTime(item.revoked_at, `${label}.revoked_at`);
  }
  return { chain_id: chainId, network_id: networkId, revoked_at: revokedAt, rpc, status: item.status };
}

function rpcEntry(value, label) {
  const item = expectObject(value, RPC_KEYS, RPC_KEYS, label);
  return {
    operator: requireText(item.operator, `${label}.operator`, 80),
    source: requireHttpsUrl(item.source, `${label}.source`),
    url: requireHttpsUrl(item.url, `${label}.url`),
  };
}

function registry(document, chainDocument) {
  const allowed = new Set(["schema_version", "send_enabled", "user_approved", "deployments", "policies", "identity"]);
  expectObject(document, allowed, allowed, "registry");
  if (document.schema_version !== REGISTRY_VERSION) throw new ExecutionRegistryError("bad_enum", "registry.schema_version");
  if (document.send_enabled !== false) throw new ExecutionRegistryError("send_enabled_rejected", "registry.send_enabled");
  if (document.user_approved !== false) throw new ExecutionRegistryError("user_approved_has_no_effect", "registry.user_approved");
  if (!Array.isArray(document.deployments) || !Array.isArray(document.policies)) {
    throw new ExecutionRegistryError("bad_type", "registry.lists");
  }
  const byChain = new Map(chainDocument.chains.map((item) => [item.chain_id, item]));
  const deployments = document.deployments.map((item, index) => deployment(item, index, "registry", byChain));
  deployments.sort((left, right) => compareText(left.id, right.id));
  unique(deployments, "id", "deployment");
  const seenAddress = new Set();
  for (const item of deployments) {
    const key = `${item.chain_id}:${item.address.toLowerCase()}`;
    if (seenAddress.has(key)) throw new ExecutionRegistryError("duplicate_key", item.address);
    seenAddress.add(key);
  }
  const byId = new Map(deployments.map((item) => [item.id, item]));
  const policies = document.policies.map((item, index) => policy(item, index, byId));
  policies.sort((left, right) => compareText(left.id, right.id));
  unique(policies, "id", "policy");
  return {
    deployments,
    identity: identityBlock(document.identity),
    policies,
    schema_version: REGISTRY_VERSION,
    send_enabled: false,
    user_approved: false,
  };
}

function deployment(value, index, mode, byChain) {
  const label = `deployments[${index}]`;
  const item = expectObject(value, DEPLOYMENT_KEYS, DEPLOYMENT_KEYS, label);
  if (item.user_approved !== false) throw new ExecutionRegistryError("user_approved_has_no_effect", `${label}.user_approved`);
  const recordStatus = item.record_status;
  if (mode === "registry" && recordStatus !== "active" && recordStatus !== "revoked") {
    throw new ExecutionRegistryError("bad_enum", `${label}.record_status`);
  }
  if (mode === "candidate" && recordStatus !== "candidate") {
    throw new ExecutionRegistryError("bad_enum", `${label}.record_status`);
  }
  const approval = mode === "candidate" ? "candidate_unapproved" : "not_approved";
  if (item.approval !== approval) throw new ExecutionRegistryError("bad_enum", `${label}.approval`);
  const chainId = requireChainId(item.chain_id, `${label}.chain_id`);
  const networkId = requireText(item.network_id, `${label}.network_id`, 80);
  if (byChain !== null) {
    const chainRow = byChain.get(chainId);
    if (chainRow === undefined || chainRow.network_id !== networkId) {
      throw new ExecutionRegistryError("chain_mismatch", label);
    }
    if (recordStatus === "active" && chainRow.status !== "active") {
      throw new ExecutionRegistryError("chain_mismatch", `${label}.status`);
    }
  }
  if (!PROXY_KINDS.includes(item.proxy_kind)) throw new ExecutionRegistryError("proxy_kind_rejected", String(item.proxy_kind));
  if (!PERMISSION_TYPES.includes(item.permission_type)) {
    throw new ExecutionRegistryError("bad_enum", `${label}.permission_type`);
  }
  if (item.pin_status !== "pinned" && item.pin_status !== "not_read_offline") {
    throw new ExecutionRegistryError("bad_enum", `${label}.pin_status`);
  }
  let runtimeHash;
  let sourceBlockNumber;
  let sourceBlockHash;
  let validUntil;
  let implementation;
  if (recordStatus === "candidate") {
    if (item.pin_status !== "not_read_offline") throw new ExecutionRegistryError("bad_enum", `${label}.pin_status`);
    runtimeHash = nullHash(item.runtime_hash, `${label}.runtime_hash`);
    sourceBlockNumber = nullUint(item.source_block_number, `${label}.source_block_number`);
    sourceBlockHash = nullBlock(item.source_block_hash, `${label}.source_block_hash`);
    validUntil = nullTime(item.valid_until, `${label}.valid_until`);
    implementation = implementationOf(item.implementation, `${label}.implementation`, item.proxy_kind, false);
  } else {
    if (item.pin_status !== "pinned") throw new ExecutionRegistryError("pin_required", label);
    runtimeHash = requireHash64(item.runtime_hash, `${label}.runtime_hash`);
    sourceBlockNumber = requireUintString(item.source_block_number, `${label}.source_block_number`);
    sourceBlockHash = requireBlockHash(item.source_block_hash, `${label}.source_block_hash`);
    validUntil = requireTime(item.valid_until, `${label}.valid_until`);
    implementation = implementationOf(item.implementation, `${label}.implementation`, item.proxy_kind, true);
  }
  let revokedAt = null;
  if (recordStatus === "revoked") revokedAt = requireTime(item.revoked_at, `${label}.revoked_at`);
  else if (item.revoked_at !== null) throw new ExecutionRegistryError("bad_type", `${label}.revoked_at`);
  const address = requireChecksum(item.address, `${label}.address`);
  if (implementation !== null && implementation.address === address) {
    throw new ExecutionRegistryError("implementation_is_proxy", label);
  }
  return {
    address,
    approval,
    assertions: stringList(item.assertions, `${label}.assertions`),
    chain_id: chainId,
    id: identifier(item.id, `${label}.id`),
    implementation,
    network_id: networkId,
    official_sources: sources(item.official_sources, `${label}.official_sources`),
    permission_type: item.permission_type,
    pin_status: item.pin_status,
    protocol_id: identifier(item.protocol_id, `${label}.protocol_id`),
    protocol_version: requireText(item.protocol_version, `${label}.protocol_version`, 40),
    proxy_kind: item.proxy_kind,
    proxy_kind_basis: requireText(item.proxy_kind_basis, `${label}.proxy_kind_basis`),
    record_status: recordStatus,
    revoked_at: revokedAt,
    runtime_hash: runtimeHash,
    source_block_hash: sourceBlockHash,
    source_block_number: sourceBlockNumber,
    source_files: pathList(item.source_files, `${label}.source_files`),
    user_approved: false,
    valid_until: validUntil,
  };
}

function implementationOf(value, label, proxyKind, pinned) {
  if (!pinned || proxyKind === "none") {
    if (value !== null) throw new ExecutionRegistryError("bad_type", label);
    return null;
  }
  if (proxyKind !== "eip1967_impl") throw new ExecutionRegistryError("proxy_kind_rejected", proxyKind);
  const item = expectObject(value, IMPL_KEYS, IMPL_KEYS, label);
  return {
    address: requireChecksum(item.address, `${label}.address`),
    runtime_hash: requireHash64(item.runtime_hash, `${label}.runtime_hash`),
  };
}

function policy(value, index, deployments) {
  const label = `policies[${index}]`;
  const item = expectObject(value, POLICY_KEYS, POLICY_KEYS, label);
  if (item.user_approved !== false) throw new ExecutionRegistryError("user_approved_has_no_effect", `${label}.user_approved`);
  if (item.approval !== "not_approved") throw new ExecutionRegistryError("bad_enum", `${label}.approval`);
  if (item.record_status !== "active" && item.record_status !== "revoked") {
    throw new ExecutionRegistryError("bad_enum", `${label}.record_status`);
  }
  if (item.recipient_rule !== "self") throw new ExecutionRegistryError("recipient_not_self", `${label}.recipient_rule`);
  if (item.amount_rule !== "all") throw new ExecutionRegistryError("bad_enum", `${label}.amount_rule`);
  if (item.penalty_cap !== "0") throw new ExecutionRegistryError("penalty_cap_not_zero", `${label}.penalty_cap`);
  if (!ACTIONS.includes(item.action)) throw new ExecutionRegistryError("bad_enum", `${label}.action`);
  const deploymentId = identifier(item.deployment_id, `${label}.deployment_id`);
  const deploymentRow = deployments.get(deploymentId);
  if (deploymentRow === undefined) throw new ExecutionRegistryError("not_registered", deploymentId);
  const chainId = requireChainId(item.chain_id, `${label}.chain_id`);
  if (chainId !== deploymentRow.chain_id) throw new ExecutionRegistryError("chain_mismatch", label);
  if (item.record_status === "active" && deploymentRow.record_status !== "active") {
    throw new ExecutionRegistryError("not_registered", deploymentId);
  }
  const parameters = parameterList(item.parameters, label);
  const allowed = idList(item.allowed_spender_ids, `${label}.allowed_spender_ids`);
  for (const spenderId of allowed) {
    if (!deployments.has(spenderId)) throw new ExecutionRegistryError("not_registered", spenderId);
  }
  if (parameters.some((param) => param.role === "spender") && allowed.length === 0) {
    throw new ExecutionRegistryError("not_registered", `${label}.allowed_spender_ids`);
  }
  let revokedAt = null;
  if (item.record_status === "revoked") revokedAt = requireTime(item.revoked_at, `${label}.revoked_at`);
  else if (item.revoked_at !== null) throw new ExecutionRegistryError("bad_type", `${label}.revoked_at`);
  let assetId = item.asset_deployment_id;
  if (assetId !== null) {
    assetId = identifier(assetId, `${label}.asset_deployment_id`);
    if (!deployments.has(assetId)) throw new ExecutionRegistryError("not_registered", assetId);
  }
  return {
    action: item.action,
    allowed_spender_ids: allowed,
    allowance_cap: requireUintString(item.allowance_cap, `${label}.allowance_cap`),
    amount_rule: "all",
    approval: "not_approved",
    asset_deployment_id: assetId,
    chain_id: chainId,
    deployment_id: deploymentId,
    id: identifier(item.id, `${label}.id`),
    loss_cap: requireUintString(item.loss_cap, `${label}.loss_cap`),
    market_id: optionalId(item.market_id, `${label}.market_id`),
    parameters,
    penalty_cap: "0",
    position_id: optionalId(item.position_id, `${label}.position_id`),
    recipient_rule: "self",
    record_status: item.record_status,
    revoked_at: revokedAt,
    spend_cap: requireUintString(item.spend_cap, `${label}.spend_cap`),
    user_approved: false,
  };
}

function parameterList(value, label) {
  if (!Array.isArray(value)) throw new ExecutionRegistryError("bad_type", `${label}.parameters`);
  const parsed = [];
  const names = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const item = value[index];
    const paramLabel = `${label}.parameters[${index}]`;
    if (!isPlain(item)) throw new ExecutionRegistryError("bad_type", paramLabel);
    const solidityType = item.solidity_type;
    if (!SOLIDITY_TYPES.includes(solidityType)) throw new ExecutionRegistryError("bad_enum", `${paramLabel}.solidity_type`);
    if (ADDRESS_TYPES.includes(solidityType) && !Object.prototype.hasOwnProperty.call(item, "role")) {
      throw new ExecutionRegistryError("address_role_required", paramLabel);
    }
    const allowed = ADDRESS_TYPES.includes(solidityType) ? PARAM_KEYS : new Set(["name", "solidity_type"]);
    expectObject(item, allowed, allowed, paramLabel);
    const name = requireText(item.name, `${paramLabel}.name`, 80);
    const stripped = name.replaceAll("_", "");
    if (!/^[0-9A-Za-z]+$/.test(stripped) || /^[0-9]/.test(name)) {
      throw new ExecutionRegistryError("bad_type", `${paramLabel}.name`);
    }
    if (names.has(name)) throw new ExecutionRegistryError("duplicate_key", name);
    names.add(name);
    const parsedItem = { name, solidity_type: solidityType };
    if (ADDRESS_TYPES.includes(solidityType)) {
      if (!ADDRESS_ROLES.includes(item.role)) throw new ExecutionRegistryError("bad_enum", `${paramLabel}.role`);
      parsedItem.role = item.role;
    }
    parsed.push(parsedItem);
  }
  return parsed;
}

function identityBlock(value) {
  const item = expectObject(value, IDENTITY_KEYS, IDENTITY_KEYS, "identity");
  if (item.rule !== "single_account_v1") throw new ExecutionRegistryError("identity_rule", "identity.rule");
  if (item.siwe_address !== "compare_when_session_present") {
    throw new ExecutionRegistryError("identity_rule", "identity.siwe_address");
  }
  if (item.own_address_list !== false) throw new ExecutionRegistryError("own_address_list_rejected", "identity.own_address_list");
  const switched = expectObject(item.on_account_switch, SWITCH_KEYS, SWITCH_KEYS, "identity.on_account_switch");
  if (!sameJson(switched.clear, ["evidence"]) || !sameJson(switched.keep, ["pending", "unknown"])) {
    throw new ExecutionRegistryError("identity_rule", "identity.on_account_switch");
  }
  return {
    on_account_switch: { clear: ["evidence"], keep: ["pending", "unknown"] },
    own_address_list: false,
    rule: "single_account_v1",
    siwe_address: "compare_when_session_present",
  };
}

function candidates(document) {
  const allowed = new Set(["schema_version", "approval", "user_approved", "deployments", "unclassified", "skipped"]);
  expectObject(document, allowed, allowed, "candidates");
  if (document.schema_version !== CANDIDATES_VERSION) throw new ExecutionRegistryError("bad_enum", "candidates.schema_version");
  if (document.approval !== "candidate_unapproved") throw new ExecutionRegistryError("bad_enum", "candidates.approval");
  if (document.user_approved !== false) throw new ExecutionRegistryError("user_approved_has_no_effect", "candidates.user_approved");
  for (const key of ["deployments", "unclassified", "skipped"]) {
    if (!Array.isArray(document[key])) throw new ExecutionRegistryError("bad_type", `candidates.${key}`);
  }
  const deployments = document.deployments.map((item, index) => deployment(item, index, "candidate", null));
  deployments.sort((left, right) => compareText(left.id, right.id));
  unique(deployments, "id", "deployment");
  const seenAddress = new Set();
  for (const item of deployments) {
    const key = `${item.chain_id}:${item.address.toLowerCase()}`;
    if (seenAddress.has(key)) throw new ExecutionRegistryError("duplicate_key", item.address);
    seenAddress.add(key);
  }
  const unclassified = document.unclassified.map((item, index) => unclassifiedRow(item, index));
  unclassified.sort(compareUnclassified);
  const skipped = document.skipped.map((item, index) => skippedRow(item, index));
  skipped.sort(compareSkipped);
  return {
    approval: "candidate_unapproved",
    deployments,
    schema_version: CANDIDATES_VERSION,
    skipped,
    unclassified,
    user_approved: false,
  };
}

function unclassifiedRow(value, index) {
  const label = `unclassified[${index}]`;
  const item = expectObject(value, UNCLASSIFIED_KEYS, UNCLASSIFIED_KEYS, label);
  if (item.approval !== "candidate_unapproved" || item.user_approved !== false) {
    throw new ExecutionRegistryError("user_approved_has_no_effect", label);
  }
  if (!CANDIDATE_REASONS.has(item.reason)) throw new ExecutionRegistryError("bad_enum", `${label}.reason`);
  let chainId = item.chain_id;
  if (chainId !== null) chainId = requireChainId(chainId, `${label}.chain_id`);
  let address = requireText(item.address, `${label}.address`, 120);
  const family = requireText(item.address_family, `${label}.address_family`, 40);
  if (family === "evm") address = requireChecksum(address, `${label}.address`);
  return {
    address,
    address_family: family,
    approval: "candidate_unapproved",
    chain_id: chainId,
    network_id: requireText(item.network_id, `${label}.network_id`, 80),
    official_sources: sources(item.official_sources, `${label}.official_sources`),
    protocol_id: identifier(item.protocol_id, `${label}.protocol_id`),
    protocol_version: requireText(item.protocol_version, `${label}.protocol_version`, 40),
    reason: item.reason,
    role: requireText(item.role, `${label}.role`, 80),
    source_file: requireRelativePath(item.source_file, `${label}.source_file`),
    user_approved: false,
  };
}

function skippedRow(value, index) {
  const label = `skipped[${index}]`;
  const item = expectObject(value, SKIPPED_KEYS, SKIPPED_KEYS, label);
  if (!CANDIDATE_REASONS.has(item.reason)) throw new ExecutionRegistryError("bad_enum", `${label}.reason`);
  return {
    address: optionalText(item.address, `${label}.address`),
    network_id: optionalText(item.network_id, `${label}.network_id`),
    protocol_id: optionalText(item.protocol_id, `${label}.protocol_id`),
    reason: item.reason,
    role: optionalText(item.role, `${label}.role`),
    source_file: requireRelativePath(item.source_file, `${label}.source_file`),
  };
}

function trust(document) {
  const allowed = new Set(["schema_version", "note", "categories", "paths"]);
  expectObject(document, allowed, allowed, "trust");
  if (document.schema_version !== TRUST_VERSION) throw new ExecutionRegistryError("bad_enum", "trust.schema_version");
  if (document.note !== TRUST_NOTE) throw new ExecutionRegistryError("bad_enum", "trust.note");
  if (!Array.isArray(document.categories) || !Array.isArray(document.paths)) {
    throw new ExecutionRegistryError("bad_type", "trust.lists");
  }
  const categories = [];
  const fromCategories = new Set();
  const names = new Set();
  for (let index = 0; index < document.categories.length; index += 1) {
    const label = `trust.categories[${index}]`;
    const category = expectObject(document.categories[index], new Set(["name", "paths"]), new Set(["name", "paths"]), label);
    const name = requireText(category.name, `${label}.name`, 80);
    if (names.has(name)) throw new ExecutionRegistryError("duplicate_key", name);
    names.add(name);
    if (!Array.isArray(category.paths) || category.paths.length === 0) {
      throw new ExecutionRegistryError("bad_type", `${label}.paths`);
    }
    const paths = category.paths.map((path) => requireRelativePath(path, `${label}.paths`));
    if (new Set(paths).size !== paths.length) throw new ExecutionRegistryError("duplicate_key", name);
    for (const path of paths) fromCategories.add(path);
    categories.push({ name, paths: [...paths].sort(compareText) });
  }
  for (const name of REQUIRED_TRUST_CATEGORIES) {
    if (!names.has(name)) throw new ExecutionRegistryError("missing_field", "trust.categories");
  }
  const declared = document.paths.map((path) => requireRelativePath(path, "trust.paths"));
  if (new Set(declared).size !== declared.length) throw new ExecutionRegistryError("duplicate_key", "trust.paths");
  const expected = new Set(fromCategories);
  expected.add(TRUST_PATH);
  if (expected.size !== new Set(declared).size || declared.some((path) => !expected.has(path))) {
    throw new ExecutionRegistryError("bad_path", "trust.paths");
  }
  for (const path of REQUIRED_TRUST_PATHS) {
    if (!declared.includes(path)) throw new ExecutionRegistryError("missing_field", "trust.paths");
  }
  return { categories, note: TRUST_NOTE, paths: [...declared].sort(compareText), schema_version: TRUST_VERSION };
}

function sources(value, label) {
  if (!Array.isArray(value) || value.length === 0) throw new ExecutionRegistryError("missing_field", label);
  const parsed = [];
  const urls = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const source = expectObject(value[index], SOURCE_KEYS, SOURCE_KEYS, `${label}[${index}]`);
    const url = requireHttpsUrl(source.url, `${label}[${index}].url`);
    if (urls.has(url)) throw new ExecutionRegistryError("duplicate_key", url);
    urls.add(url);
    parsed.push({ retrieved: requireDate(source.retrieved, `${label}[${index}].retrieved`), url });
  }
  parsed.sort((left, right) => compareText(left.url, right.url));
  return parsed;
}

function stringList(value, label) {
  if (!Array.isArray(value)) throw new ExecutionRegistryError("bad_type", label);
  return value.map((item, index) => requireText(item, `${label}[${index}]`, 200));
}

function pathList(value, label) {
  if (!Array.isArray(value) || value.length === 0) throw new ExecutionRegistryError("missing_field", label);
  const paths = value.map((item, index) => requireRelativePath(item, `${label}[${index}]`));
  if (new Set(paths).size !== paths.length) throw new ExecutionRegistryError("duplicate_key", label);
  return [...paths].sort(compareText);
}

function idList(value, label) {
  if (!Array.isArray(value)) throw new ExecutionRegistryError("bad_type", label);
  const parsed = value.map((item, index) => identifier(item, `${label}[${index}]`));
  if (new Set(parsed).size !== parsed.length) throw new ExecutionRegistryError("duplicate_key", label);
  return parsed;
}

function identifier(value, label) {
  const text = requireText(value, label, 120);
  if ([...text].some((char) => !"abcdefghijklmnopqrstuvwxyz0123456789:_-".includes(char))) {
    throw new ExecutionRegistryError("bad_type", label);
  }
  return text;
}

function optionalId(value, label) {
  if (value === null) return null;
  return identifier(value, label);
}

function optionalText(value, label) {
  if (value === null) return null;
  return requireText(value, label, 160);
}

function nullHash(value, label) {
  if (value !== null) throw new ExecutionRegistryError("pin_required", label);
  return null;
}

function nullUint(value, label) {
  if (value !== null) throw new ExecutionRegistryError("pin_required", label);
  return null;
}

function nullBlock(value, label) {
  if (value !== null) throw new ExecutionRegistryError("pin_required", label);
  return null;
}

function nullTime(value, label) {
  if (value !== null) throw new ExecutionRegistryError("bad_time", label);
  return null;
}

function unique(items, key, label) {
  const seen = new Set();
  for (const item of items) {
    if (seen.has(item[key])) throw new ExecutionRegistryError("duplicate_key", `${label}:${item[key]}`);
    seen.add(item[key]);
  }
}

function compareText(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function compareUnclassified(left, right) {
  const leftKey = [left.protocol_id, left.network_id, left.role, left.source_file, left.address, left.chain_id == null ? -1 : left.chain_id];
  const rightKey = [right.protocol_id, right.network_id, right.role, right.source_file, right.address, right.chain_id == null ? -1 : right.chain_id];
  for (let index = 0; index < leftKey.length; index += 1) {
    if (leftKey[index] < rightKey[index]) return -1;
    if (leftKey[index] > rightKey[index]) return 1;
  }
  return 0;
}

function compareSkipped(left, right) {
  const leftKey = [left.source_file, left.protocol_id ?? "", left.role ?? "", left.reason, left.address ?? ""];
  const rightKey = [right.source_file, right.protocol_id ?? "", right.role ?? "", right.reason, right.address ?? ""];
  for (let index = 0; index < leftKey.length; index += 1) {
    if (leftKey[index] < rightKey[index]) return -1;
    if (leftKey[index] > rightKey[index]) return 1;
  }
  return 0;
}

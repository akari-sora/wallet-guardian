/** Candidate entries from packaged deployments and protocols. Prints only; does not write the registry. */

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { toChecksumAddress } from "./checksum.mjs";
import { ExecutionRegistryError } from "./errors.mjs";
import { loadCandidatesText } from "./loader.mjs";
import {
  EIP1967_IMPLEMENTATION_SLOT,
  PERMISSION_TYPES,
  PROXY_KINDS,
  RULES_VERSION,
  compareCodePoints,
  expectObject,
  requireText,
} from "./model.mjs";
import { parseStrictJson } from "./strict-json.mjs";

const RULE_KEYS = new Set([
  "protocol_id", "also_protocol_ids", "role", "permission_type", "proxy_kind",
  "proxy_kind_basis", "requires_slot_file", "requires_note_includes",
]);
const PERMIT2_KEYS = new Set(["permission_type", "proxy_kind", "proxy_kind_basis"]);

export function proposeExecutionCandidates(root) {
  return loadCandidatesText(JSON.stringify(assemble(root)));
}

function assemble(root) {
  const { rules, permit2 } = loadRules(root);
  const { byNetwork, byChain } = chainIndex(root);
  const observations = [];
  const skipped = [];
  const protocols = join(root, "config", "protocols");
  if (existsDir(protocols)) {
    for (const name of readdirSync(protocols).filter((item) => item.endsWith(".json")).sort(compareCodePoints)) {
      readProtocol(join(protocols, name), root, byNetwork, byChain, observations, skipped);
    }
  }
  const permit2Dir = join(root, "config", "deployments", "permit2");
  if (existsDir(permit2Dir)) {
    for (const name of readdirSync(permit2Dir).filter((item) => item.endsWith(".json")).sort(compareCodePoints)) {
      readPermit2(join(permit2Dir, name), root, byNetwork, permit2, observations, skipped);
    }
  }
  return classify(observations, skipped, rules, root);
}

function existsDir(path) {
  try {
    return readdirSync(path) !== undefined;
  } catch {
    return false;
  }
}

function loadRules(root) {
  const document = parseStrictJson(readFileSync(join(root, "config", "execution", "candidate-rules.json"), "utf8"));
  const allowed = new Set(["schema_version", "eip1967_slot", "eip1967_slot_file", "rules", "permit2"]);
  expectObject(document, allowed, allowed, "rules");
  if (document.schema_version !== RULES_VERSION) throw new ExecutionRegistryError("bad_enum", "rules.schema_version");
  if (document.eip1967_slot !== EIP1967_IMPLEMENTATION_SLOT) {
    throw new ExecutionRegistryError("proxy_kind_rejected", "eip1967_slot");
  }
  if (document.eip1967_slot_file !== "action-client/src/trusted-lista.mjs") {
    throw new ExecutionRegistryError("bad_path", "eip1967_slot_file");
  }
  if (!Array.isArray(document.rules)) throw new ExecutionRegistryError("bad_type", "rules.rules");
  const rules = document.rules.map((item, index) => rule(item, index));
  const permit2 = expectObject(document.permit2, PERMIT2_KEYS, PERMIT2_KEYS, "rules.permit2");
  if (!PERMISSION_TYPES.includes(permit2.permission_type) || !PROXY_KINDS.includes(permit2.proxy_kind)) {
    throw new ExecutionRegistryError("bad_enum", "rules.permit2");
  }
  requireText(permit2.proxy_kind_basis, "rules.permit2.proxy_kind_basis");
  return { rules, permit2 };
}

function rule(value, index) {
  const label = `rules[${index}]`;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ExecutionRegistryError("bad_type", label);
  }
  for (const key of Object.keys(value)) {
    if (!RULE_KEYS.has(key)) throw new ExecutionRegistryError("unknown_field", `${label}.${key}`);
  }
  for (const key of ["protocol_id", "role", "permission_type", "proxy_kind", "proxy_kind_basis"]) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) throw new ExecutionRegistryError("missing_field", `${label}.${key}`);
  }
  if (!PERMISSION_TYPES.includes(value.permission_type) || !PROXY_KINDS.includes(value.proxy_kind)) {
    throw new ExecutionRegistryError("bad_enum", label);
  }
  const also = value.also_protocol_ids ?? [];
  if (!Array.isArray(also)) throw new ExecutionRegistryError("bad_type", `${label}.also_protocol_ids`);
  const slot = value.requires_slot_file ?? false;
  const note = Object.prototype.hasOwnProperty.call(value, "requires_note_includes") ? value.requires_note_includes : undefined;
  if (slot === true && note !== undefined) throw new ExecutionRegistryError("bad_type", `${label}.basis`);
  if (slot !== true && (typeof note !== "string" || note === "")) {
    throw new ExecutionRegistryError("missing_field", `${label}.basis`);
  }
  if (slot !== true && slot !== false) throw new ExecutionRegistryError("bad_type", `${label}.requires_slot_file`);
  return value;
}

function chainIndex(root) {
  const byNetwork = new Map();
  const byChain = new Map();
  const directory = join(root, "config", "chains");
  if (!existsDir(directory)) return { byNetwork, byChain };
  for (const name of readdirSync(directory).filter((item) => item.endsWith(".json")).sort(compareCodePoints)) {
    const document = parseStrictJson(readFileSync(join(directory, name), "utf8"));
    if (document === null || typeof document !== "object" || Array.isArray(document)) continue;
    const network = document.network;
    const family = document.family;
    let chainId = document.evm_chain_id;
    if (typeof network !== "string" || typeof family !== "string") continue;
    if (typeof chainId !== "number" || !Number.isInteger(chainId)) chainId = null;
    byNetwork.set(network, { family, chain_id: chainId, network_id: network });
    if (chainId !== null) {
      if (byChain.has(chainId)) throw new ExecutionRegistryError("duplicate_key", `chain_id:${chainId}`);
      byChain.set(chainId, network);
    }
  }
  return { byNetwork, byChain };
}

function readProtocol(path, root, byNetwork, byChain, observations, skipped) {
  const document = parseStrictJson(readFileSync(path, "utf8"));
  const relativePath = relative(root, path).split("\\").join("/");
  if (document === null || typeof document !== "object" || Array.isArray(document)) {
    skipped.push(skip(relativePath, "unrecognized_protocol_file", {}));
    return;
  }
  if (document.protocol_id === "lista-slisbnb" && Object.prototype.hasOwnProperty.call(document, "proxy") && !Object.prototype.hasOwnProperty.call(document, "contracts")) {
    readLegacy(document, relativePath, byChain, observations, skipped);
    return;
  }
  if (!Object.prototype.hasOwnProperty.call(document, "id") || !Object.prototype.hasOwnProperty.call(document, "contracts")) {
    skipped.push(skip(relativePath, "unrecognized_protocol_file", { protocol_id: textOrNull(document.id) }));
    return;
  }
  const protocolId = document.id;
  const version = document.version;
  const contracts = document.contracts;
  if (typeof protocolId !== "string" || typeof version !== "string" || contracts === null || typeof contracts !== "object" || Array.isArray(contracts)) {
    skipped.push(skip(relativePath, "unrecognized_protocol_file", { protocol_id: textOrNull(protocolId) }));
    return;
  }
  for (const [network, roles] of Object.entries(contracts)) {
    const info = byNetwork.get(network);
    if (roles === null || typeof roles !== "object" || Array.isArray(roles) || info === undefined) {
      skipped.push(skip(relativePath, "unknown_network", { protocol_id: protocolId, network_id: textOrNull(network) }));
      continue;
    }
    for (const [roleName, entry] of Object.entries(roles)) {
      if (typeof roleName !== "string" || entry === null || typeof entry !== "object" || Array.isArray(entry)) {
        skipped.push(skip(relativePath, "unrecognized_protocol_file", { protocol_id: protocolId, network_id: network, role: textOrNull(roleName) }));
        continue;
      }
      observations.push(observation({
        protocol_id: protocolId,
        protocol_version: version,
        network_id: network,
        info,
        role: roleName,
        address: entry.address,
        notes: typeof entry.notes === "string" ? entry.notes : "",
        sources: entrySources(entry),
        source_file: relativePath,
      }));
    }
  }
}

function readLegacy(document, relativePath, byChain, observations, skipped) {
  const chainId = document.chain_id;
  const network = typeof chainId === "number" && Number.isInteger(chainId) ? byChain.get(chainId) : undefined;
  if (network === undefined) {
    skipped.push(skip(relativePath, "unknown_network", { protocol_id: "lista-slisbnb" }));
    return;
  }
  const sources = [];
  const retrieved = document.docs_read_at;
  for (const url of document.sources ?? []) {
    if (typeof url === "string" && typeof retrieved === "string") sources.push({ retrieved, url });
  }
  const info = { family: "evm", chain_id: chainId, network_id: network };
  for (const roleName of ["proxy", "slisbnb"]) {
    observations.push(observation({
      protocol_id: "lista-slisbnb",
      protocol_version: "1",
      network_id: network,
      info,
      role: roleName,
      address: document[roleName],
      notes: "",
      sources: sources.map((item) => ({ ...item })),
      source_file: relativePath,
    }));
  }
}

function readPermit2(path, root, byNetwork, permit2, observations, skipped) {
  const document = parseStrictJson(readFileSync(path, "utf8"));
  const relativePath = relative(root, path).split("\\").join("/");
  if (document === null || typeof document !== "object" || Array.isArray(document)) {
    skipped.push(skip(relativePath, "unrecognized_protocol_file", { protocol_id: "permit2" }));
    return;
  }
  const network = document.network;
  const info = typeof network === "string" ? byNetwork.get(network) : undefined;
  if (info === undefined) {
    skipped.push(skip(relativePath, "unknown_network", { protocol_id: "permit2", network_id: textOrNull(network) }));
    return;
  }
  const sources = [];
  if (typeof document.source_url === "string" && typeof document.retrieved === "string") {
    sources.push({ retrieved: document.retrieved, url: document.source_url });
  }
  const row = observation({
    protocol_id: "permit2",
    protocol_version: "1",
    network_id: network,
    info,
    role: "permit2",
    address: document.address,
    notes: "",
    sources,
    source_file: relativePath,
  });
  row.rule = {
    protocol_id: "permit2",
    role: "permit2",
    permission_type: permit2.permission_type,
    proxy_kind: permit2.proxy_kind,
    proxy_kind_basis: permit2.proxy_kind_basis,
    requires_slot_file: false,
    requires_note_includes: null,
    basis: "deployment_file",
  };
  observations.push(row);
}

function observation(fields) {
  return {
    address: fields.address,
    chain_id: fields.info.chain_id,
    family: fields.info.family,
    network_id: fields.network_id,
    notes: fields.notes,
    official_sources: fields.sources,
    protocol_id: fields.protocol_id,
    protocol_version: fields.protocol_version,
    role: fields.role,
    rule: null,
    source_file: fields.source_file,
  };
}

function entrySources(entry) {
  if (typeof entry.source_url === "string" && typeof entry.retrieved === "string") {
    return [{ retrieved: entry.retrieved, url: entry.source_url }];
  }
  return [];
}

function classify(observations, skipped, rules, root) {
  const classified = [];
  const unclassified = [];
  for (const item of observations) {
    const prepared = prepareAddress(item, skipped);
    if (prepared === null) continue;
    if (prepared.family !== "evm") {
      const row = unclassifiedItem(prepared, "not_evm");
      if (row === null) {
        skipped.push(skip(prepared.source_file, "official_source_missing", {
          protocol_id: prepared.protocol_id,
          network_id: prepared.network_id,
          role: prepared.role,
          address: textOrNull(prepared.address),
        }));
      } else unclassified.push(row);
      continue;
    }
    let matched = matchRule(rules, prepared.protocol_id, prepared.role);
    if (matched === null && prepared.rule !== null) matched = prepared.rule;
    let row = null;
    if (matched === null) row = unclassifiedItem(prepared, "proxy_kind_not_documented");
    else if (!basisOk(matched, prepared.notes, root)) row = unclassifiedItem(prepared, "proxy_basis_missing");
    else {
      prepared.rule = matched;
      classified.push(prepared);
      continue;
    }
    if (row === null) {
      skipped.push(skip(prepared.source_file, "official_source_missing", {
        protocol_id: prepared.protocol_id,
        network_id: prepared.network_id,
        role: prepared.role,
        address: typeof prepared.address === "string" ? prepared.address : null,
      }));
    } else unclassified.push(row);
  }
  const { deployments, demoted } = mergeClassified(classified);
  for (const item of demoted) {
    const row = unclassifiedItem(item, "duplicate_address_conflict");
    if (row !== null) unclassified.push(row);
  }
  return {
    approval: "candidate_unapproved",
    deployments,
    schema_version: "guardian.execution_candidates.v1",
    skipped,
    unclassified,
    user_approved: false,
  };
}

function prepareAddress(item, skipped) {
  const address = item.address;
  if (item.family !== "evm") {
    if (typeof address !== "string" || address === "") {
      skipped.push(skip(item.source_file, "bad_address", { protocol_id: item.protocol_id, network_id: item.network_id, role: item.role }));
      return null;
    }
    return item;
  }
  if (typeof address !== "string") {
    skipped.push(skip(item.source_file, "bad_address", { protocol_id: item.protocol_id, network_id: item.network_id, role: item.role }));
    return null;
  }
  try {
    item.address = toChecksumAddress(address);
  } catch {
    skipped.push(skip(item.source_file, "bad_address", {
      protocol_id: item.protocol_id,
      network_id: item.network_id,
      role: item.role,
      address,
    }));
    return null;
  }
  return item;
}

function matchRule(rules, protocolId, roleName) {
  for (const item of rules) {
    const names = [item.protocol_id, ...(item.also_protocol_ids ?? [])];
    if (names.includes(protocolId) && item.role === roleName) return item;
  }
  return null;
}

function basisOk(ruleRow, notes, root) {
  if (ruleRow.requires_slot_file === true) {
    const path = join(root, "action-client", "src", "trusted-lista.mjs");
    let text;
    try {
      text = readFileSync(path, "utf8");
    } catch {
      return false;
    }
    return text.includes(EIP1967_IMPLEMENTATION_SLOT);
  }
  const needle = ruleRow.requires_note_includes;
  if (typeof needle === "string" && needle !== "") return notes.includes(needle);
  return ruleRow.basis === "deployment_file";
}

function mergeClassified(classified) {
  const groups = new Map();
  for (const item of classified) {
    const key = `${item.chain_id}:${item.address.toLowerCase()}`;
    const group = groups.get(key) ?? [];
    group.push(item);
    groups.set(key, group);
  }
  const deployments = [];
  const demoted = [];
  for (const group of groups.values()) {
    const kinds = new Set(group.map((item) => `${item.rule.permission_type}|${item.rule.proxy_kind}|${item.role}`));
    if (kinds.size !== 1) {
      demoted.push(...group);
      continue;
    }
    let preferred = group[0];
    for (const item of group.slice(1)) preferred = prefer(preferred, item);
    const sources = new Map();
    for (const item of group) {
      for (const source of item.official_sources) {
        const current = sources.get(source.url);
        if (current === undefined || source.retrieved > current) sources.set(source.url, source.retrieved);
      }
    }
    if (sources.size === 0) {
      demoted.push(preferred);
      continue;
    }
    const sourceFiles = [...new Set(group.map((item) => item.source_file))].sort(compareCodePoints);
    deployments.push(deploymentFrom(preferred, sources, sourceFiles));
  }
  return { deployments, demoted };
}

function prefer(left, right) {
  const primary = left.rule.protocol_id;
  if (left.protocol_id === primary) return left;
  if (right.protocol_id === primary) return right;
  if (left.protocol_id.includes("-") && !right.protocol_id.includes("-")) return right;
  if (right.protocol_id.includes("-") && !left.protocol_id.includes("-")) return left;
  if (compareCodePoints(left.protocol_id, right.protocol_id) <= 0) return left;
  return right;
}

function deploymentFrom(item, sources, sourceFiles) {
  const ruleRow = item.rule;
  return {
    address: item.address,
    approval: "candidate_unapproved",
    assertions: [],
    chain_id: item.chain_id,
    id: `candidate:${item.chain_id}:${item.protocol_id}:${item.role}`,
    implementation: null,
    network_id: item.network_id,
    official_sources: [...sources.keys()].sort(compareCodePoints).map((url) => ({ retrieved: sources.get(url), url })),
    permission_type: ruleRow.permission_type,
    pin_status: "not_read_offline",
    protocol_id: item.protocol_id,
    protocol_version: item.protocol_version,
    proxy_kind: ruleRow.proxy_kind,
    proxy_kind_basis: ruleRow.proxy_kind_basis,
    record_status: "candidate",
    revoked_at: null,
    runtime_hash: null,
    source_block_hash: null,
    source_block_number: null,
    source_files: sourceFiles,
    user_approved: false,
    valid_until: null,
  };
}

function unclassifiedItem(item, reason) {
  if (!item.official_sources.length) return null;
  return {
    address: item.address,
    address_family: item.family,
    approval: "candidate_unapproved",
    chain_id: item.family === "evm" ? item.chain_id : null,
    network_id: item.network_id,
    official_sources: item.official_sources.map((source) => ({ ...source })),
    protocol_id: item.protocol_id,
    protocol_version: item.protocol_version,
    reason,
    role: item.role,
    source_file: item.source_file,
    user_approved: false,
  };
}

function skip(sourceFile, reason, fields) {
  return {
    address: fields.address ?? null,
    network_id: fields.network_id ?? null,
    protocol_id: fields.protocol_id ?? null,
    reason,
    role: fields.role ?? null,
    source_file: sourceFile,
  };
}

function textOrNull(value) {
  return typeof value === "string" && value !== "" ? value : null;
}

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { proposeExecutionCandidates } from "../src/execution/candidates.mjs";
import { policyDigest, registryDigest } from "../src/execution/digest.mjs";
import { ExecutionRegistryError } from "../src/execution/errors.mjs";
import { checkCall, checkIdentity, retainAfterAccountSwitch } from "../src/execution/identity.mjs";
import {
  loadChainsText,
  loadExecutionDirectory,
  loadRegistryText,
  userApprovalGranted,
} from "../src/execution/loader.mjs";
import { ACTIONS, ADDRESS_ROLES, PERMISSION_TYPES, PROXY_KINDS } from "../src/execution/model.mjs";
import { parseStrictJson } from "../src/execution/strict-json.mjs";
import { validateRelease } from "../tools/release-record.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const fixtureDir = join(root, "tests", "fixtures", "execution", "valid");
const oracleDir = join(root, "tests", "fixtures", "execution");
const ACCOUNT = "0x0000000000000000000000000000000000000001";
const OTHER = "0x0000000000000000000000000000000000000002";
const VAULT = "0x0000000000000000000000000000000000000003";

function read(path) {
  return readFileSync(path, "utf8");
}

function registryText() {
  return read(join(fixtureDir, "registry.json"));
}

function chainsText() {
  return read(join(fixtureDir, "chains.json"));
}

function loadFixture(text = registryText()) {
  return loadRegistryText(text, loadChainsText(chainsText()));
}

function codeOf(text) {
  assert.equal(userApprovalGranted(text, chainsText()), false);
  try {
    loadFixture(text);
  } catch (error) {
    assert.equal(error instanceof ExecutionRegistryError, true);
    return error.code;
  }
  assert.fail("expected rejection");
}

function identity(account, siwe = "absent") {
  const body = {
    from: account,
    onBehalf: account,
    receiver: account,
    transfer_to: account,
    eth_accounts_0: account,
    plan_sender: account,
    plan_recipient: account,
    execution_account: account,
  };
  if (siwe !== "absent") body.siwe_address = siwe;
  return body;
}

test("float rejected", () => {
  const text = registryText().replace('"penalty_cap": "0"', '"penalty_cap": 1.0');
  assert.equal(codeOf(text), "float_rejected");
  assert.throws(() => parseStrictJson("1e2"), (error) => error.code === "float_rejected");
});

test("duplicate key rejected", () => {
  for (const raw of ['{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"a":1,"\\u0061":2}']) {
    assert.throws(() => parseStrictJson(raw), (error) => error.code === "duplicate_key");
  }
});

test("unknown field rejected", () => {
  const text = registryText().replace('"send_enabled": false', '"send_enabled": false, "extra": 1');
  assert.equal(codeOf(text), "unknown_field");
});

test("proxy kind rejected", () => {
  for (const kind of ["beacon", "uups"]) {
    const text = registryText().replace('"proxy_kind": "eip1967_impl"', `"proxy_kind": "${kind}"`);
    assert.equal(codeOf(text), "proxy_kind_rejected");
  }
});

test("recipient not self rejected", () => {
  const text = registryText().replace('"recipient_rule": "self"', '"recipient_rule": "other"');
  assert.equal(codeOf(text), "recipient_not_self");
});

test("address role required", () => {
  const missing = registryText().replace(
    '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
    '{"name": "receiver", "solidity_type": "address"}',
  );
  assert.equal(codeOf(missing), "address_role_required");
  const labeled = registryText().replace(
    '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
    '{"name": "receiver", "solidity_type": "address", "label": "to"}',
  );
  assert.equal(codeOf(labeled), "address_role_required");
  const extra = registryText().replace(
    '{"name": "receiver", "solidity_type": "address", "role": "receiver"}',
    '{"name": "receiver", "solidity_type": "address", "role": "receiver", "label": "to"}',
  );
  assert.equal(codeOf(extra), "unknown_field");
});

test("penalty cap not zero", () => {
  for (const raw of ['"penalty_cap": "1"', '"penalty_cap": 0', '"penalty_cap": "00"', '"penalty_cap": "0.0"']) {
    assert.equal(codeOf(registryText().replace('"penalty_cap": "0"', raw)), "penalty_cap_not_zero");
  }
});

test("approved_by_user has no effect", () => {
  const text = registryText().replace('"user_approved": false', '"user_approved": false, "approved_by_user": true');
  assert.equal(codeOf(text), "approved_by_user_has_no_effect");
  const granted = registryText().replace('"user_approved": false', '"user_approved": true');
  assert.equal(codeOf(granted), "user_approved_has_no_effect");
  const loaded = loadFixture();
  assert.equal(loaded.user_approved, false);
  assert.equal(userApprovalGranted(registryText(), chainsText()), false);
});

test("fixture and catalog match the python semantic oracle", () => {
  const loaded = { chains: loadChainsText(chainsText()), registry: loadFixture() };
  assert.deepEqual(loaded, JSON.parse(read(join(fixtureDir, "semantic.json"))));
  const digests = JSON.parse(read(join(fixtureDir, "digests.json")));
  assert.equal(registryDigest(loaded.registry), digests.registry);
  assert.equal(policyDigest(loaded.registry), digests.policy);
  const catalog = loadExecutionDirectory(join(root, "config/execution"));
  assert.deepEqual(catalog, JSON.parse(read(join(oracleDir, "catalog.semantic.json"))));
  assert.equal(catalog.chains.enables_send, false);
  assert.equal(catalog.registry.send_enabled, false);
});

test("schema enums match and release digests stay optional", () => {
  const schema = JSON.parse(read(join(root, "config/execution/schemas/registry.schema.json")));
  assert.deepEqual(schema.$defs.deployment.properties.proxy_kind.enum, PROXY_KINDS);
  assert.deepEqual(schema.$defs.deployment.properties.permission_type.enum, PERMISSION_TYPES);
  assert.deepEqual(schema.$defs.policy.properties.action.enum, ACTIONS);
  assert.deepEqual(schema.$defs.parameter.properties.role.enum, ADDRESS_ROLES);
  const release = JSON.parse(read(join(root, "action-client/deploy/release.schema.json")));
  assert.equal(release.$defs.release.properties.mainnet_send.const, false);
  assert.equal(release.$defs.release.required.includes("digests"), false);
  const origins = JSON.parse(read(join(root, "config/deployments/private-task-origins.json")));
  const sample = {
    release_id: "fixture-send-off",
    deployment_id: origins.deployments[0].deployment_id,
    origin: origins.deployments[0].origin,
    source_sha: "a".repeat(40),
    client_build: "action-client@0.3.0",
    variant: "send_off",
    walletconnect: false,
    mainnet_send: false,
    files: { "index.html": "b".repeat(64) },
    headers_sha256: "c".repeat(64),
    cf_deployment_url: "https://example.pages.dev",
    status: "retired",
  };
  assert.deepEqual(validateRelease(sample), []);
  const digests = {
    execution_registry: "d".repeat(64),
    policy: "e".repeat(64),
    core_bundle: "f".repeat(64),
    dist: "1".repeat(64),
  };
  assert.deepEqual(validateRelease({ ...sample, digests }), []);
  assert.equal(validateRelease({ ...sample, digests: { policy: "e".repeat(64) } }).length > 0, true);
  assert.equal(validateRelease({ ...sample, mainnet_send: true }).some((error) => error.includes("mainnet_send")), true);
});

test("identity binding and call rules", () => {
  const registry = loadFixture();
  checkIdentity(ACCOUNT, identity(ACCOUNT));
  checkIdentity(ACCOUNT, identity(ACCOUNT, null));
  assert.throws(() => checkIdentity(ACCOUNT, identity(ACCOUNT, OTHER)), (error) => error.code === "identity_mismatch");
  const mismatched = identity(ACCOUNT);
  mismatched.from = OTHER;
  assert.throws(() => checkIdentity(ACCOUNT, mismatched), (error) => error.code === "identity_mismatch");
  const recipient = identity(ACCOUNT);
  recipient.receiver = OTHER;
  assert.throws(() => checkIdentity(ACCOUNT, recipient), (error) => error.code === "recipient_not_self");
  assert.throws(
    () => checkIdentity(ACCOUNT, { ...identity(ACCOUNT), own_addresses: [ACCOUNT] }),
    (error) => error.code === "own_address_list_rejected",
  );
  assert.deepEqual(retainAfterAccountSwitch({ evidence: { gone: true }, pending: [1], unknown: { left: true } }), {
    pending: [1],
    unknown: { left: true },
  });
  checkCall(registry, {
    policy_id: "fixture-redeem",
    account: ACCOUNT,
    identity: identity(ACCOUNT),
    arguments: { owner: ACCOUNT, receiver: ACCOUNT, shares: "1" },
  });
  assert.throws(
    () => checkCall(registry, {
      policy_id: "fixture-redeem",
      account: ACCOUNT,
      identity: identity(ACCOUNT),
      arguments: { owner: ACCOUNT, receiver: OTHER, shares: "1" },
    }),
    (error) => error.code === "recipient_not_self",
  );
  assert.throws(
    () => checkCall(registry, {
      policy_id: "fixture-revoke",
      account: ACCOUNT,
      identity: identity(ACCOUNT),
      arguments: { spender: OTHER },
    }),
    (error) => error.code === "not_registered",
  );
  checkCall(registry, {
    policy_id: "fixture-revoke",
    account: ACCOUNT,
    identity: identity(ACCOUNT),
    arguments: { spender: VAULT },
  });
});

test("candidates match the python oracle and stay unapproved", async () => {
  const registryBefore = await readFile(join(root, "config/execution/registry.json"));
  const listaBefore = await readFile(join(root, "config/protocols/lista-bsc.json"));
  const document = proposeExecutionCandidates(root);
  assert.deepEqual(await readFile(join(root, "config/execution/registry.json")), registryBefore);
  assert.deepEqual(await readFile(join(root, "config/protocols/lista-bsc.json")), listaBefore);
  assert.deepEqual(document, JSON.parse(read(join(oracleDir, "candidates.semantic.json"))));
  assert.equal(document.user_approved, false);
  assert.equal(document.approval, "candidate_unapproved");
  assert.equal(document.deployments.length, 11);
});

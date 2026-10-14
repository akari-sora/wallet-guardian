import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { classifyDeploymentApproval } from "./lista-reread.mjs";
import {
  SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS,
  bindIdentity,
  classifyTaskSource,
  effectsCheck,
  modeAClientGate,
} from "./client-evidence.mjs";
import { proposeListaApproval } from "../tools/propose-lista-approval.mjs";
import {
  DEPLOYMENT_APPROVALS,
  TRUSTED_LISTA,
  claimCalldata,
  isAddress,
  sameAddress,
} from "./trusted-lista.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const OTHER = "0x" + "3".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const BLOCK = "0x" + "ab".repeat(32);
const PROXY_CODE = "0x6001";
const IMPL_CODE = "0x6002";
const REF = "ref_" + "ab".repeat(16);

function sha(hex) {
  return createHash("sha256").update(Buffer.from(hex.slice(2), "hex")).digest("hex");
}

function nowUnix() {
  return Math.floor(Date.now() / 1000);
}

function tuple() {
  return {
    implementation: IMPL,
    proxyCodeSha256: sha(PROXY_CODE),
    implementationCodeSha256: sha(IMPL_CODE),
  };
}

function pin(overrides = {}) {
  return {
    id: "test-only-lista",
    registryVersion: 1,
    implementation: IMPL,
    proxyCodeSha256: sha(PROXY_CODE),
    implementationCodeSha256: sha(IMPL_CODE),
    provenance: "test-only injection, not a production pin",
    validFromUnix: 0,
    validUntilUnix: nowUnix() + 3600,
    permanent: false,
    retired: false,
    ...overrides,
  };
}

function taskSource(overrides = {}) {
  return {
    kind: "private_api",
    privateRef: REF,
    path: `/api/tasks/${REF}`,
    authGeneration: 4,
    loadedAuthGeneration: 4,
    ...overrides,
  };
}

function identity(overrides = {}) {
  return {
    ok: true,
    reason: null,
    account: ACCOUNT.toLowerCase(),
    chainId: 56,
    taskSource: taskSource(),
    ...overrides,
  };
}

function session(overrides = {}) {
  return { address: ACCOUNT, auth_time: nowUnix(), ...overrides };
}

function passingReread() {
  return { reread: "pass", reasons: [], claim: { idx: 0 }, binding: { blockHash: BLOCK } };
}

function evidence(overrides = {}) {
  return {
    reread: passingReread(),
    effects: { status: "dry_run_pass", reason: null },
    identity: identity(),
    approval: { entries: [pin()], tuple: tuple() },
    session: session(),
    ...overrides,
  };
}

test("isAddress replaces a self-comparison and sameAddress still compares two addresses", () => {
  const mixed = "0x" + "ab".repeat(20);
  const upperHex = "0x" + "AB".repeat(20);
  assert.equal(isAddress(ACCOUNT), true);
  assert.equal(isAddress(ACCOUNT), sameAddress(ACCOUNT, ACCOUNT));
  assert.equal(isAddress("0x123"), false);
  assert.equal(sameAddress("0x123", "0x123"), false);
  assert.equal(sameAddress(ACCOUNT, OTHER), false);
  assert.equal(isAddress(upperHex), true);
  assert.equal(sameAddress(mixed, upperHex), true);
  assert.equal(sameAddress(upperHex, mixed), true);
});

test("effectsCheck dry-run passes only a non-reverting eth_call at the pinned block", async () => {
  let seen = null;
  const blockTag = { blockHash: BLOCK, requireCanonical: true };
  const result = await effectsCheck({
    call: async (method, params) => {
      seen = { method, params };
      return "0x";
    },
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(result.status, "dry_run_pass");
  assert.equal(result.dryRun, true);
  assert.equal(Object.hasOwn(result, "preflight_ok"), false);
  assert.equal(seen.method, "eth_call");
  assert.deepEqual(seen.params[0], {
    from: ACCOUNT,
    to: TRUSTED_LISTA.proxy,
    data: claimCalldata(0),
  });
  assert.deepEqual(seen.params[1], blockTag);
  const incomplete = await effectsCheck({
    call: async () => undefined,
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(incomplete.status, "insufficient");
  assert.notEqual(incomplete.status, "dry_run_pass");
  const loose = await effectsCheck({
    call: async () => "0x",
    account: ACCOUNT,
    idx: 0,
    blockTag: "latest",
  });
  assert.equal(loose.status, "insufficient");
});

test("effectsCheck maps revert to fail and -32602 to block_identity_unsupported", async () => {
  const blockTag = { blockHash: BLOCK, requireCanonical: true };
  const reverted = await effectsCheck({
    call: async () => {
      const error = new Error("execution reverted");
      error.rpcError = { code: 3, message: "execution reverted" };
      throw error;
    },
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(reverted.status, "fail");
  assert.equal(reverted.reason, "revert");
  const unsupported = await effectsCheck({
    call: async () => {
      const error = new Error("invalid params");
      error.rpcError = { code: -32602, message: "invalid params" };
      throw error;
    },
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(unsupported.status, "insufficient");
  assert.equal(unsupported.reason, "block_identity_unsupported");
});

test("bindIdentity requires the live account, session, and plan addresses on chain 56", async () => {
  const login = "0x" + "ab".repeat(20);
  const checksum = "0x" + "AB".repeat(20);
  const plan = { sender: login, recipient: checksum, execution_account: "0x" + "aB".repeat(20) };
  const matched = await bindIdentity({
    request: async (method) => (method === "eth_accounts" ? [checksum] : "0x38"),
    loginAddress: login,
    session: { address: checksum },
    plan,
  });
  assert.equal(matched.ok, true);
  assert.equal(matched.chainId, 56);
  assert.equal(matched.account, login.toLowerCase());
  const swapped = await bindIdentity({
    request: async (method) => (method === "eth_accounts" ? [OTHER] : "0x38"),
    loginAddress: ACCOUNT,
    session: { address: ACCOUNT },
    plan,
  });
  assert.equal(swapped.ok, false);
  assert.equal(swapped.reason, "identity_mismatch");
  const otherSender = await bindIdentity({
    request: async (method) => (method === "eth_accounts" ? [ACCOUNT] : "0x38"),
    loginAddress: ACCOUNT,
    session: { address: ACCOUNT },
    plan: { ...plan, sender: OTHER },
  });
  assert.equal(otherSender.reason, "identity_mismatch");
  const wrongChain = await bindIdentity({
    request: async (method) => (method === "eth_accounts" ? [ACCOUNT] : "0x1"),
    loginAddress: ACCOUNT,
    session: { address: ACCOUNT },
    plan,
  });
  assert.equal(wrongChain.reason, "identity_mismatch");
  assert.equal(wrongChain.chainId, 1);
});

test("task source accepts only the private task route for the same auth generation", () => {
  assert.equal(classifyTaskSource(taskSource()).ok, true);
  assert.equal(classifyTaskSource(taskSource({ kind: "local_file" })).reason, "source_not_private_api");
  assert.equal(classifyTaskSource(taskSource({ kind: "synthetic" })).reason, "source_not_private_api");
  assert.equal(classifyTaskSource(taskSource({ privateRef: "" })).reason, "source_not_private_api");
  assert.equal(classifyTaskSource(taskSource({ loadedAuthGeneration: 5 })).reason, "source_not_private_api");
  assert.equal(classifyTaskSource(null).reason, "source_not_private_api");
});

test("deployment approval mechanism stays fail-closed for empty, expired, and unbounded pins", () => {
  const observed = tuple();
  const now = nowUnix();
  assert.deepEqual(classifyDeploymentApproval([], observed, now), { status: "unapproved_deployment", entry: null });
  assert.equal(classifyDeploymentApproval([pin({ validUntilUnix: now - 1 })], observed, now).status, "approval_retired");
  assert.equal(classifyDeploymentApproval([pin({ validUntilUnix: null, permanent: false })], observed, now).status, "approval_unbounded");
  assert.equal(classifyDeploymentApproval([pin()], observed, now).status, "active");
  assert.equal(DEPLOYMENT_APPROVALS.entries.length, 0);
  assert.equal(TRUSTED_LISTA.approvals.length, 0);
  assert.equal(SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS, 15 * 60);
});

test("proposeListaApproval prints a blank candidate and does not write pins", async () => {
  const configUrl = new URL("../../config/protocols/lista-bsc.json", import.meta.url);
  const bundledUrl = new URL("../../src/guardian/_resources/config/protocols/lista-bsc.json", import.meta.url);
  const beforeConfig = readFileSync(configUrl, "utf8");
  const beforeBundled = readFileSync(bundledUrl, "utf8");
  const toolSource = readFileSync(new URL("../tools/propose-lista-approval.mjs", import.meta.url), "utf8");
  assert.equal(toolSource.includes("writeFile"), false);
  assert.equal(toolSource.includes("appendFile"), false);
  assert.equal(toolSource.includes("eth_sendTransaction"), false);
  const methods = [];
  const fetchImpl = async (url, init) => {
    assert.equal(url, TRUSTED_LISTA.rpcUrl);
    const body = JSON.parse(init.body);
    methods.push(body.method);
    assert.equal(body.method === "eth_sendTransaction", false);
    const result = {
      eth_chainId: "0x38",
      eth_blockNumber: "0x64",
      eth_getBlockByNumber: { number: "0x64", hash: BLOCK, timestamp: "0x10" },
      eth_getStorageAt: "0x" + IMPL.slice(2).padStart(64, "0"),
    }[body.method];
    if (body.method === "eth_getStorageAt") {
      assert.equal(String(body.params[0]).toLowerCase(), TRUSTED_LISTA.proxy.toLowerCase());
      assert.equal(body.params[1], TRUSTED_LISTA.implementationSlot);
      assert.equal(body.params[2].requireCanonical, true);
      assert.equal(body.params[2].blockHash, BLOCK);
    }
    if (body.method === "eth_getCode") {
      assert.equal(body.params[1].requireCanonical, true);
      const who = String(body.params[0]).toLowerCase();
      const code = who === TRUSTED_LISTA.proxy.toLowerCase() ? PROXY_CODE : IMPL_CODE;
      return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: code }), { status: 200 });
    }
    if (result === undefined) throw new Error("unexpected " + body.method);
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result }), { status: 200 });
  };
  const candidate = await proposeListaApproval({ fetchImpl });
  assert.deepEqual(methods, [
    "eth_chainId",
    "eth_blockNumber",
    "eth_getBlockByNumber",
    "eth_getStorageAt",
    "eth_getCode",
    "eth_getCode",
  ]);
  assert.equal(candidate.candidateOnly, true);
  assert.equal(candidate.implementation, IMPL);
  assert.equal(candidate.proxy, TRUSTED_LISTA.proxy);
  assert.equal(candidate.proxyCodeSha256, sha(PROXY_CODE));
  assert.equal(candidate.implementationCodeSha256, sha(IMPL_CODE));
  assert.equal(candidate.blockNumber, 100);
  assert.equal(candidate.blockHash, BLOCK);
  assert.equal(candidate.provenance, "");
  assert.equal(candidate.validUntil, null);
  assert.equal(candidate.validUntilUnix, null);
  assert.equal(readFileSync(configUrl, "utf8"), beforeConfig);
  assert.equal(readFileSync(bundledUrl, "utf8"), beforeBundled);
  assert.equal(TRUSTED_LISTA.approvals.length, 0);
  assert.deepEqual(JSON.parse(beforeConfig).deployment_approvals.entries, []);
});

test("the gate function does not mention server plan flags", () => {
  const source = readFileSync(new URL("./client-evidence.mjs", import.meta.url), "utf8");
  const start = source.indexOf("export function modeAClientGate");
  const end = source.indexOf("export async function runModeAClientGate");
  const body = source.slice(start, end);
  for (const word of ["simulation", "wallet_request_allowed", "preflight_ok", "walletRequestAllowed", "preflightOk"]) {
    assert.equal(body.includes(word), false, word);
  }
  assert.equal(source.includes("eth_sendTransaction"), false);
  assert.equal(source.includes("wallet_sendCalls"), false);
});

test("modeAClientGate blocks expired auth, bad source, and non-active pins without opening send", () => {
  const open = modeAClientGate(evidence());
  assert.equal(open.blocked, false);
  assert.equal(open.outcome, "evidence_ok");
  assert.equal(open.sendAllowed, false);
  const stale = modeAClientGate(evidence({
    session: session({ auth_time: nowUnix() - SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS - 5 }),
  }));
  assert.equal(stale.blocked, true);
  assert.equal(stale.reason, "relogin_required");
  assert.equal(stale.sendAllowed, false);
  const missing = modeAClientGate(evidence({ session: { address: ACCOUNT } }));
  assert.equal(missing.reason, "relogin_required");
  const local = modeAClientGate(evidence({
    identity: identity({ taskSource: taskSource({ kind: "local_file" }) }),
  }));
  assert.equal(local.reason, "source_not_private_api");
  const synthetic = modeAClientGate(evidence({
    identity: identity({ taskSource: taskSource({ kind: "synthetic" }) }),
  }));
  assert.equal(synthetic.reason, "source_not_private_api");
  const empty = modeAClientGate(evidence({ approval: { entries: [], tuple: tuple(), status: "active" } }));
  assert.equal(empty.reason, "unapproved_deployment");
  const retired = modeAClientGate(evidence({
    approval: { entries: [pin({ validUntilUnix: nowUnix() - 10 })], tuple: tuple(), status: "active" },
  }));
  assert.equal(retired.reason, "approval_retired");
  const unbounded = modeAClientGate(evidence({
    approval: { entries: [pin({ validUntilUnix: null })], tuple: tuple(), status: "active" },
  }));
  assert.equal(unbounded.reason, "approval_unbounded");
  const forged = modeAClientGate({
    ...evidence({ approval: { entries: [], tuple: tuple() } }),
    simulation: { status: "PASS", effects_checked: true, usable_as_live_evidence: true },
    wallet_request_allowed: true,
    preflight_ok: true,
    plan: { deployment_approvals: [pin()], wallet_request_allowed: true, preflight_ok: true },
  });
  assert.equal(forged.reason, "unapproved_deployment");
  assert.equal(forged.sendAllowed, false);
});

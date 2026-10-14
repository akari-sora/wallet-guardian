import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import {
  SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS,
  effectsCheck,
  modeAClientGate,
  runModeAClientGate,
} from "../src/client-evidence.mjs";
import { reverifyListaClaim } from "../src/lista-reread.mjs";
import { TRUSTED_LISTA, claimCalldata } from "../src/trusted-lista.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const OTHER = "0x" + "9".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const BLOCK = "0x" + "ab".repeat(32);
const REF = "ref_" + "cd".repeat(16);
const SENDS = new Set(["eth_sendTransaction", "wallet_sendCalls", "eth_sendRawTransaction"]);

function sha(hex) {
  return createHash("sha256").update(Buffer.from(hex.slice(2), "hex")).digest("hex");
}

function nowUnix() {
  return Math.floor(Date.now() / 1000);
}

function tuple() {
  return {
    implementation: IMPL,
    proxyCodeSha256: sha("0x6001"),
    implementationCodeSha256: sha("0x6002"),
  };
}

function pin(overrides = {}) {
  return {
    id: "test-only-lista",
    registryVersion: 1,
    implementation: IMPL,
    proxyCodeSha256: sha("0x6001"),
    implementationCodeSha256: sha("0x6002"),
    provenance: "test-only injection, not a production pin",
    validFromUnix: 0,
    validUntilUnix: nowUnix() + 3600,
    permanent: false,
    retired: false,
    ...overrides,
  };
}

function plan(overrides = {}) {
  const digest = "ab".repeat(32);
  return {
    protocol_id: "lista-slisbnb",
    chain_id: 56,
    sender: ACCOUNT,
    recipient: ACCOUNT,
    execution_account: ACCOUNT,
    revision: 1,
    digest,
    deployment: {
      proxy: TRUSTED_LISTA.proxy,
      implementation: IMPL,
      proxy_code_sha256: sha("0x6001"),
      implementation_code_sha256: sha("0x6002"),
      method: "claimWithdraw(uint256)",
    },
    intent: { type: "lista_claim_withdraw", uuid: "42", idx: 0 },
    calls: [{ to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "0" }],
    asset_in: { raw_amount: "5" },
    asset_out: { raw_amount: "6" },
    local_ttl_unix: 1_700_000_000 + 3600,
    ...overrides,
  };
}

function taskSource(overrides = {}) {
  return {
    kind: "private_api",
    privateRef: REF,
    path: `/api/tasks/${REF}`,
    authGeneration: 2,
    loadedAuthGeneration: 2,
    ...overrides,
  };
}

function freshSession(overrides = {}) {
  return { address: ACCOUNT, auth_time: nowUnix(), ...overrides };
}

function passingReread() {
  return { reread: "pass", reasons: [], claim: { idx: 0 }, binding: { blockHash: BLOCK } };
}

function wallet(behavior = {}) {
  const log = [];
  const events = [];
  let accounts = behavior.accounts ?? [ACCOUNT];
  const api = {
    log,
    events,
    sends() {
      return log.filter((entry) => SENDS.has(entry.method));
    },
    setAccounts(next) {
      accounts = next;
    },
    on(event) {
      events.push(event);
    },
    async request(method, params) {
      log.push({ method, params: params ? structuredClone(params) : params });
      if (SENDS.has(method)) throw new Error("send");
      if (method === "eth_accounts") return accounts.slice();
      if (method === "eth_chainId") return behavior.chain ?? "0x38";
      if (method === "eth_call") {
        if (behavior.callError) throw behavior.callError;
        return behavior.callResult === undefined ? "0x" : behavior.callResult;
      }
      throw new Error("unexpected " + method);
    },
  };
  return api;
}

function baseEvidence(overrides = {}) {
  return {
    reread: passingReread(),
    effects: { status: "dry_run_pass", reason: null, dryRun: true },
    identity: {
      ok: true,
      reason: null,
      account: ACCOUNT.toLowerCase(),
      chainId: 56,
      taskSource: taskSource(),
    },
    approval: { entries: [pin()], tuple: tuple() },
    session: freshSession(),
    ...overrides,
  };
}

test("reread mismatches block the gate and the wallet mock records no send", async () => {
  const cases = [
    {
      name: "idx",
      plan: plan({ calls: [{ to: TRUSTED_LISTA.proxy, data: claimCalldata(1), value: "0" }] }),
      reason: "calldata_mismatch",
    },
    {
      name: "to",
      plan: plan({ calls: [{ to: "0x" + "4".repeat(40), data: claimCalldata(0), value: "0" }] }),
      reason: "calldata_mismatch",
    },
    {
      name: "value",
      plan: plan({ calls: [{ to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "1" }] }),
      reason: "calldata_mismatch",
    },
    {
      name: "extra",
      plan: plan({
        calls: [
          { to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "0" },
          { to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "0" },
        ],
      }),
      reason: "not_single_step",
    },
  ];
  for (const item of cases) {
    const current = item.plan;
    const reread = await reverifyListaClaim({
      plan: current,
      account: ACCOUNT,
      planDigest: current.digest,
      revision: current.revision,
      nowUnix: 1_700_000_000,
      call: () => {
        throw new Error("reread fail must not read");
      },
    });
    assert.equal(reread.reread, "fail", item.name);
    assert.equal(reread.reasons[0], item.reason, item.name);
    const mock = wallet();
    const decision = await runModeAClientGate({
      request: mock.request,
      plan: current,
      loginAddress: ACCOUNT,
      session: freshSession(),
      taskSource: taskSource(),
      reread,
      approval: { entries: [pin()], tuple: tuple() },
    });
    assert.equal(decision.blocked, true, item.name);
    assert.equal(decision.outcome, "blocked", item.name);
    assert.equal(decision.reason, item.reason, item.name);
    assert.equal(decision.sendAllowed, false, item.name);
    assert.equal(mock.sends().length, 0, item.name);
    assert.equal(mock.events.length, 0, item.name);
    for (const entry of mock.log) {
      assert.equal(["eth_accounts", "eth_chainId", "eth_call"].includes(entry.method), true, entry.method);
    }
  }
});

test("plan.sender differing from loginAddress blocks with identity_mismatch and no send", async () => {
  const mock = wallet();
  const current = plan({ sender: OTHER });
  const decision = await runModeAClientGate({
    request: mock.request,
    plan: current,
    loginAddress: ACCOUNT,
    session: freshSession(),
    taskSource: taskSource(),
    reread: passingReread(),
  });
  assert.equal(decision.blocked, true);
  assert.equal(decision.reason, "identity_mismatch");
  assert.equal(decision.sendAllowed, false);
  assert.equal(mock.sends().length, 0);
  assert.equal(mock.log.some((entry) => entry.method === "eth_accounts"), true);
});

test("empty client pins stay unapproved_deployment even if the caller supplies an active entry", async () => {
  assert.equal(TRUSTED_LISTA.approvals.length, 0);
  const mock = wallet();
  const decision = await runModeAClientGate({
    request: mock.request,
    plan: {
      ...plan(),
      simulation: { status: "PASS", effects_checked: true, usable_as_live_evidence: true },
      wallet_request_allowed: true,
      preflight_ok: true,
      deployment_approvals: [pin()],
    },
    loginAddress: ACCOUNT,
    session: freshSession(),
    taskSource: taskSource(),
    reread: passingReread(),
    approval: { entries: [pin()], tuple: tuple(), status: "active" },
  });
  assert.equal(decision.reason, "unapproved_deployment");
  assert.equal(decision.blocked, true);
  assert.equal(decision.sendAllowed, false);
  assert.equal(mock.sends().length, 0);
  assert.equal(TRUSTED_LISTA.approvals.length, 0);
});

test("eth_call revert fails the dry-run and -32602 is insufficient", async () => {
  const blockTag = { blockHash: BLOCK, requireCanonical: true };
  const revertWallet = wallet({
    callError: Object.assign(new Error("execution reverted"), {
      rpcError: { code: 3, message: "execution reverted" },
    }),
  });
  const reverted = await effectsCheck({
    call: revertWallet.request,
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(reverted.status, "fail");
  const revertGate = modeAClientGate(baseEvidence({ effects: reverted }));
  assert.equal(revertGate.blocked, true);
  assert.equal(revertGate.reason, "effects_fail");
  assert.equal(revertGate.sendAllowed, false);
  assert.equal(revertWallet.sends().length, 0);

  const unsupportedWallet = wallet({
    callError: Object.assign(new Error("invalid params"), {
      rpcError: { code: -32602, message: "invalid params" },
    }),
  });
  const unsupported = await effectsCheck({
    call: unsupportedWallet.request,
    account: ACCOUNT,
    idx: 0,
    blockTag,
  });
  assert.equal(unsupported.status, "insufficient");
  assert.equal(unsupported.reason, "block_identity_unsupported");
  const unsupportedGate = modeAClientGate(baseEvidence({ effects: unsupported }));
  assert.equal(unsupportedGate.blocked, true);
  assert.equal(unsupportedGate.reason, "block_identity_unsupported");
  assert.equal(unsupportedWallet.sends().length, 0);
  assert.equal(unsupportedWallet.log.some((entry) => entry.method === "eth_call"), true);
});

test("swapping eth_accounts after a passing reread blocks without accountsChanged", async () => {
  const mock = wallet();
  const first = await runModeAClientGate({
    request: mock.request,
    plan: plan(),
    loginAddress: ACCOUNT,
    session: freshSession(),
    taskSource: taskSource(),
    reread: passingReread(),
  });
  assert.equal(first.reason, "unapproved_deployment");
  assert.equal(first.reasons.includes("identity_mismatch"), false);
  const readsBefore = mock.log.filter((entry) => entry.method === "eth_accounts").length;
  mock.setAccounts([OTHER]);
  const second = await runModeAClientGate({
    request: mock.request,
    plan: plan(),
    loginAddress: ACCOUNT,
    session: freshSession(),
    taskSource: taskSource(),
    reread: passingReread(),
  });
  assert.equal(second.blocked, true);
  assert.equal(second.reason, "identity_mismatch");
  assert.equal(second.sendAllowed, false);
  assert.equal(mock.events.length, 0);
  assert.equal(mock.sends().length, 0);
  assert.equal(mock.log.filter((entry) => entry.method === "eth_accounts").length, readsBefore + 1);
});

test("local and synthetic tasks are source_not_private_api with no send", async () => {
  for (const kind of ["local_file", "synthetic"]) {
    const mock = wallet();
    const decision = await runModeAClientGate({
      request: mock.request,
      plan: plan(),
      loginAddress: ACCOUNT,
      session: freshSession(),
      taskSource: taskSource({ kind }),
      reread: passingReread(),
    });
    assert.equal(decision.blocked, true, kind);
    assert.equal(decision.reason, "source_not_private_api", kind);
    assert.equal(decision.sendAllowed, false, kind);
    assert.equal(mock.sends().length, 0, kind);
  }
});

test("auth_time older than the suggested window requires re-login and no send", async () => {
  const mock = wallet();
  const decision = await runModeAClientGate({
    request: mock.request,
    plan: plan(),
    loginAddress: ACCOUNT,
    session: freshSession({ auth_time: nowUnix() - SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS - 5 }),
    taskSource: taskSource(),
    reread: passingReread(),
  });
  assert.equal(decision.blocked, true);
  assert.equal(decision.reason, "relogin_required");
  assert.equal(decision.sendAllowed, false);
  assert.equal(mock.sends().length, 0);
  const missing = await runModeAClientGate({
    request: wallet().request,
    plan: plan(),
    loginAddress: ACCOUNT,
    session: { address: ACCOUNT },
    taskSource: taskSource(),
    reread: passingReread(),
  });
  assert.equal(missing.reason, "relogin_required");
});

test("gate output changes only when client evidence changes", () => {
  const baseline = baseEvidence();
  const baselineOut = modeAClientGate(baseline);
  assert.equal(baselineOut.outcome, "evidence_ok");
  assert.equal(baselineOut.sendAllowed, false);

  function mix(seed) {
    let state = seed >>> 0;
    return () => {
      state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }
  const rng = mix(0x5eed);
  const statuses = ["PASS", "FAIL", "INCOMPLETE", "NOT_RUN"];
  const stamped = JSON.stringify(baselineOut);
  const explicit = {
    simulation: { status: "PASS", effects_checked: true, usable_as_live_evidence: true, source: "server" },
    wallet_request_allowed: true,
    preflight_ok: true,
  };
  function decorate(source, flags) {
    return {
      ...source,
      ...flags,
      plan: { ...flags, deployment_approvals: [pin()], block_reasons: [] },
      reread: { ...source.reread, ...flags },
      effects: { ...source.effects, ...flags },
      identity: { ...source.identity, ...flags },
      approval: { ...source.approval, ...flags, status: "active" },
      session: { ...source.session, ...flags },
    };
  }
  assert.equal(JSON.stringify(modeAClientGate(decorate(baseline, explicit))), stamped);
  for (let index = 0; index < 24; index += 1) {
    const flags = {
      simulation: {
        status: statuses[Math.floor(rng() * statuses.length)],
        effects_checked: rng() < 0.5,
        usable_as_live_evidence: rng() < 0.5,
      },
      wallet_request_allowed: rng() < 0.5,
      preflight_ok: rng() < 0.5,
    };
    assert.equal(JSON.stringify(modeAClientGate(decorate(baseline, flags))), stamped);
  }
  const blocked = baseEvidence({ approval: { entries: [], tuple: tuple() } });
  const blockedOut = JSON.stringify(modeAClientGate(blocked));
  assert.notEqual(blockedOut, stamped);
  assert.equal(JSON.stringify(modeAClientGate(decorate(blocked, explicit))), blockedOut);

  const changed = [
    baseEvidence({ reread: { reread: "fail", reasons: ["calldata_mismatch"] } }),
    baseEvidence({ effects: { status: "fail", reason: "revert", dryRun: true } }),
    baseEvidence({ identity: { ...baseline.identity, ok: false, reason: "identity_mismatch" } }),
    baseEvidence({ approval: { entries: [], tuple: tuple() } }),
    baseEvidence({
      session: freshSession({ auth_time: nowUnix() - SUGGESTED_MODE_A_AUTH_TIME_MAX_AGE_SECONDS - 5 }),
    }),
    baseEvidence({
      identity: { ...baseline.identity, taskSource: taskSource({ kind: "local_file" }) },
    }),
  ];
  for (const sample of changed) {
    assert.notEqual(JSON.stringify(modeAClientGate(sample)), stamped);
    assert.equal(JSON.stringify(modeAClientGate(decorate(sample, explicit))), JSON.stringify(modeAClientGate(sample)));
  }
});

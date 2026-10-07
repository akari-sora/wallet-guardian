import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { clientConfirmDecision } from "./gate.mjs";
import {
  emptyRereadView,
  locateWithdrawal,
  reverifyListaClaim,
  settleReread,
  statusLines,
} from "./lista-reread.mjs";
import {
  TRUSTED_LISTA,
  claimCalldata,
  encodePausedCall,
  encodeRequestStatusCall,
  encodeWithdrawalRequestsCall,
  fetchTrustedRpc,
  trustedQuerySources,
} from "./trusted-lista.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const OTHER = "0x" + "3".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const NOW = 1_700_000_000;
const PROXY_CODE = "0x6001";
const IMPL_CODE = "0x6002";
const BLOCK = "0x" + "ab".repeat(32);

function sha(hex) {
  return createHash("sha256").update(Buffer.from(hex.slice(2), "hex")).digest("hex");
}

function word(value) {
  return BigInt(value).toString(16).padStart(64, "0");
}

function requestsReturn(rows) {
  let body = word(32) + word(rows.length);
  for (const row of rows) body += word(row.uuid) + word(row.amount) + word(row.start);
  return "0x" + body;
}

const ONE_REQUEST = requestsReturn([{ uuid: 42, amount: 5, start: 7 }]);
const STATUS_OK = "0x" + word(1) + word(6);

function plan(overrides = {}) {
  return {
    protocol_id: "lista-slisbnb",
    chain_id: 56,
    sender: ACCOUNT,
    recipient: ACCOUNT,
    execution_account: ACCOUNT,
    revision: 1,
    digest: "ab".repeat(32),
    deployment: {
      proxy: TRUSTED_LISTA.proxy,
      implementation: IMPL,
      proxy_code_sha256: sha(PROXY_CODE),
      implementation_code_sha256: sha(IMPL_CODE),
      method: "claimWithdraw(uint256)",
    },
    intent: { type: "lista_claim_withdraw", uuid: "42", idx: 0 },
    calls: [{ to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "0" }],
    asset_in: { raw_amount: "5" },
    asset_out: { raw_amount: "6" },
    simulation: { status: "INCOMPLETE", effects_checked: false, source: "not_run" },
    wallet_request_allowed: false,
    ...overrides,
  };
}

function input(overrides = {}) {
  const current = overrides.plan ?? plan();
  return {
    plan: current,
    task: overrides.task ?? null,
    account: ACCOUNT,
    connectedAccount: null,
    walletChainId: null,
    planDigest: current.digest,
    revision: current.revision,
    nowUnix: NOW,
    call: overrides.call,
  };
}

function script(overrides = {}) {
  const log = [];
  const impl = overrides.impl ?? IMPL;
  async function call(method, params) {
    log.push({ method, params: structuredClone(params) });
    if (overrides.throwName) {
      const error = new Error("synthetic");
      error.name = overrides.throwName;
      throw error;
    }
    if (method === "eth_chainId") return overrides.chain ?? "0x38";
    if (method === "eth_blockNumber") return "0x64";
    if (method === "eth_getBlockByNumber") {
      return {
        number: overrides.blockNumber ?? "0x64",
        hash: overrides.blockHash ?? BLOCK,
        timestamp: "0x" + (overrides.timestamp ?? NOW - 10).toString(16),
      };
    }
    if (method === "eth_getStorageAt") return "0x" + word(impl);
    if (method === "eth_getCode") {
      const who = String(params[0]).toLowerCase();
      if (who === TRUSTED_LISTA.proxy.toLowerCase()) return overrides.proxyCode ?? PROXY_CODE;
      if (who === impl.toLowerCase()) return overrides.implCode ?? IMPL_CODE;
      throw new Error("unexpected code " + who);
    }
    if (method === "eth_call") {
      assert.equal(String(params[0].to).toLowerCase(), TRUSTED_LISTA.proxy.toLowerCase());
      assert.equal(params[1], "0x64");
      if (params[0].data === encodePausedCall()) return "0x" + word(overrides.paused ? 1 : 0);
      if (params[0].data === encodeWithdrawalRequestsCall(ACCOUNT)) return overrides.requests ?? ONE_REQUEST;
      return overrides.status ?? STATUS_OK;
    }
    throw new Error("unexpected method");
  }
  return { call, log };
}

test("rebuilt claimWithdraw calldata is exact for idx 0 and 1", () => {
  assert.equal(claimCalldata(0), "0xb13acedd" + "0".repeat(64));
  assert.equal(claimCalldata(1), "0xb13acedd" + "0".repeat(63) + "1");
});

test("trusted sources ignore rpc and abi named by task JSON", () => {
  const named = trustedQuerySources({
    rpc: "https://evil.example/rpc",
    abi: [{ name: "claimWithdrawFor" }],
    deployment: { proxy: "0x" + "9".repeat(40) },
  });
  const plain = trustedQuerySources(null);
  assert.equal(named.rpcUrl, TRUSTED_LISTA.rpcUrl);
  assert.equal(named.proxy, plain.proxy);
  assert.deepEqual(named.allowedOps, ["claimWithdraw(uint256)"]);
  const chains = JSON.parse(readFileSync(new URL("../../config/chains.json", import.meta.url), "utf8"));
  const lista = JSON.parse(readFileSync(new URL("../../config/protocols/lista-bsc.json", import.meta.url), "utf8"));
  assert.equal(chains.enabled[0].rpc[0].url, TRUSTED_LISTA.rpcUrl);
  assert.equal(chains.enabled[0].chain_id, 56);
  assert.equal(lista.proxy, TRUSTED_LISTA.proxy);
  for (const signature of TRUSTED_LISTA.reads) assert.ok(lista.methods.read.includes(signature));
  assert.deepEqual(lista.methods.planned_write, [TRUSTED_LISTA.method]);
});

test("forged preflight: pass re-read, simulation not done, allow send closed", async () => {
  const current = plan();
  const task = { mode: "A", ref: "ref_" + "ab".repeat(16), preflight: { preflight_ok: true, revision_required: false, changes: [] } };
  Object.freeze(task.preflight);
  Object.freeze(current);
  Object.freeze(current.simulation);
  const { call, log } = script();
  const result = await reverifyListaClaim({ ...input({ plan: current, task }), call });
  assert.equal(result.reread, "pass");
  assert.equal(result.simulation, "not_done");
  assert.equal(result.allowSend, "closed");
  assert.equal(task.preflight.preflight_ok, true);
  assert.equal(current.wallet_request_allowed, false);
  assert.equal(current.simulation.status, "INCOMPLETE");
  assert.equal(log.some((entry) => entry.method === "eth_sendTransaction"), false);
  assert.equal(log.filter((entry) => entry.method === "eth_call" && entry.params[1] === "0x64").length, 3);
  const decision = clientConfirmDecision({
    plan: current,
    task: { ...task, requires_user_wallet_confirmation: true },
    mainnetSend: true,
    mode: "A",
  });
  assert.equal(decision.sendAllowed, false);
  assert.equal(decision.buttonEnabled, false);
  assert.equal(decision.reason, "preflight_not_ok");
  const lines = statusLines(result);
  assert.match(lines.reread, /pass/);
  assert.match(lines.simulation, /not done/);
  assert.match(lines.send, /closed/);
});

test("wrong chain fails and does not continue the block read", async () => {
  const { call, log } = script({ chain: "0x1" });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "fail");
  assert.deepEqual(result.reasons, ["wrong_chain"]);
  assert.equal(result.allowSend, "closed");
  assert.equal(result.simulation, "not_done");
  assert.deepEqual(log.map((entry) => entry.method), ["eth_chainId"]);
});

test("account switch fails before any contract read", async () => {
  const { call, log } = script();
  const result = await reverifyListaClaim({ ...input(), call, connectedAccount: OTHER });
  assert.equal(result.reread, "fail");
  assert.deepEqual(result.reasons, ["account_changed"]);
  assert.equal(result.allowSend, "closed");
  assert.equal(log.length, 0);
});

test("stale block is insufficient, not a pass", async () => {
  const { call } = script({ timestamp: NOW - 1000 });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "insufficient");
  assert.deepEqual(result.reasons, ["stale_block"]);
  assert.equal(result.allowSend, "closed");
  assert.equal(result.simulation, "not_done");
});

test("index change fails and the status read uses the uuid index, not the cached idx", async () => {
  const drifted = requestsReturn([
    { uuid: 7, amount: 5, start: 1 },
    { uuid: 42, amount: 5, start: 7 },
  ]);
  const { call, log } = script({ requests: drifted, status: "0x" + word(1) + word(6) });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "fail");
  assert.equal(result.reasons.includes("index_drift"), true);
  assert.equal(result.allowSend, "closed");
  const status = log.find((entry) => entry.method === "eth_call" && entry.params[0].data === encodeRequestStatusCall(ACCOUNT, 1));
  assert.ok(status);
  assert.equal(log.some((entry) => entry.params?.[0]?.data === encodeRequestStatusCall(ACCOUNT, 0)), false);
});

test("missing claimable fails and does not pre-read a status index", async () => {
  const { call, log } = script({ requests: requestsReturn([{ uuid: 9, amount: 5, start: 1 }]) });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "fail");
  assert.deepEqual(result.reasons, ["claimable_missing"]);
  assert.equal(log.some((entry) => String(entry.params?.[0]?.data ?? "").startsWith(encodeRequestStatusCall(ACCOUNT, 0).slice(0, 10))), false);
});

test("deployment change fails", async () => {
  const { call, log } = script({ impl: "0x" + "4".repeat(40) });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "fail");
  assert.equal(result.reasons.includes("deployment_changed"), true);
  assert.equal(result.allowSend, "closed");
  assert.equal(log.some((entry) => String(entry.params?.[0]).toLowerCase() === ("0x" + "4".repeat(40))), true);
});

test("timeout is insufficient and does not authorize send", async () => {
  const { call } = script({ throwName: "TimeoutError" });
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "insufficient");
  assert.deepEqual(result.reasons, ["timeout"]);
  assert.equal(result.simulation, "not_done");
  assert.equal(result.allowSend, "closed");
});

test("missing deployment fields are insufficient", async () => {
  const current = plan();
  delete current.deployment.implementation;
  delete current.deployment.proxy_code_sha256;
  const { call, log } = script();
  const result = await reverifyListaClaim({ ...input({ plan: current }), call });
  assert.equal(result.reread, "insufficient");
  assert.deepEqual(result.reasons, ["missing_fields"]);
  assert.equal(log.length, 0);
  assert.equal(result.allowSend, "closed");
});

test("not claimable, pause, recipient change, and mode C all block", async () => {
  const paused = await reverifyListaClaim({ ...input(), call: script({ paused: true }).call });
  assert.equal(paused.reread, "fail");
  assert.equal(paused.reasons.includes("paused"), true);
  const waiting = await reverifyListaClaim({
    ...input(),
    call: script({ status: "0x" + word(0) + word(6) }).call,
  });
  assert.equal(waiting.reread, "fail");
  assert.equal(waiting.reasons.includes("not_claimable"), true);
  const moved = plan({ recipient: OTHER });
  const recipient = await reverifyListaClaim({ ...input({ plan: moved }), call: script().call });
  assert.equal(recipient.reread, "fail");
  assert.deepEqual(recipient.reasons, ["recipient_changed"]);
  const mode = await reverifyListaClaim({ ...input({ task: { mode: "C", ref: "ref_" + "ab".repeat(16) } }), call: script().call });
  assert.equal(mode.reread, "fail");
  assert.deepEqual(mode.reasons, ["mode_unsupported"]);
  assert.equal(mode.allowSend, "closed");
});

test("a passing observation expires, and a revision or account change invalidates it", async () => {
  const { call } = script();
  const result = await reverifyListaClaim({ ...input(), call });
  assert.equal(result.reread, "pass");
  const context = {
    simulation: { status: "INCOMPLETE", effects_checked: false },
    account: ACCOUNT,
    chainId: 56,
    planDigest: result.binding.planDigest,
    taskRef: null,
    revision: 1,
    nowUnix: NOW,
  };
  assert.equal(settleReread(result, context).reread, "pass");
  assert.equal(settleReread(result, context).simulation, "not_done");
  assert.equal(settleReread(result, { ...context, nowUnix: result.binding.freshUntil }).reread, "insufficient");
  assert.equal(settleReread(result, { ...context, revision: 2 }).reasons.includes("revision_changed"), true);
  assert.equal(settleReread(result, { ...context, account: OTHER }).reasons.includes("account_changed"), true);
  const forged = { reread: "pass", simulation: "pass", allowSend: "open", reasons: [], binding: result.binding };
  const settled = settleReread(forged, { ...context, simulation: { status: "INCOMPLETE", effects_checked: false } });
  assert.equal(settled.reread, "pass");
  assert.equal(settled.simulation, "not_done");
  assert.equal(settled.allowSend, "closed");
  assert.throws(() => statusLines({ ...settled, allowSend: "open" }));
});

test("simulation FAIL stays fail when the re-read passes, and incomplete does not become pass", async () => {
  const current = plan({
    simulation: { status: "FAIL", effects_checked: false, source: "not_run" },
  });
  const result = await reverifyListaClaim({ ...input({ plan: current }), call: script().call });
  assert.equal(result.reread, "pass");
  assert.equal(result.simulation, "fail");
  assert.equal(result.allowSend, "closed");
  assert.equal(emptyRereadView({ status: "TEST_ONLY_OVERRIDE", effects_checked: true, usable_as_live_evidence: true }).simulation, "not_done");
});

test("locateWithdrawal does not accept a cached or batch index", () => {
  const rows = [
    { idx: 0, uuid: "7", amountIn: "5" },
    { idx: 1, uuid: "42", amountIn: "5" },
  ];
  const found = locateWithdrawal(rows, "42");
  assert.deepEqual(found.statusIndexes, [1]);
  assert.equal(locateWithdrawal(rows, "42").idx, 1);
  assert.equal(locateWithdrawal([{ idx: 0, uuid: "42" }, { idx: 1, uuid: "42" }], "42").kind, "duplicate");
});

test("fetch uses only the trusted endpoint and refuses a send method before the network", async () => {
  let calls = 0;
  const fetchImpl = async (url) => {
    calls += 1;
    assert.equal(url, TRUSTED_LISTA.rpcUrl);
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: "0x38" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  assert.equal(await fetchTrustedRpc("eth_chainId", [], { fetchImpl }), "0x38");
  await assert.rejects(() => fetchTrustedRpc("eth_sendTransaction", [], { fetchImpl }));
  assert.equal(calls, 1);
});

test("ui copy keeps the three statuses and lock screen separate from lock chain", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const doc = readFileSync(new URL("../../docs/p2-lista-reread.md", import.meta.url), "utf8");
  const lines = statusLines(emptyRereadView(undefined));
  assert.ok(html.includes(lines.reread));
  assert.ok(html.includes(lines.simulation));
  assert.ok(html.includes(lines.send));
  assert.ok(html.includes('data-check="reread"'));
  assert.ok(html.includes('data-check="simulation"'));
  assert.ok(html.includes('data-check="send"'));
  for (const text of [html, doc]) assert.match(text, /lock screen ≠ lock chain/);
  const main = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  const reader = readFileSync(new URL("./lista-reread.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(main, /preflight_ok\s*=/);
  assert.doesNotMatch(reader, /preflight_ok\s*=/);
  assert.doesNotMatch(main, /eth_sendTransaction|wallet_sendCalls/);
  assert.doesNotMatch(reader, /eth_sendTransaction|wallet_sendCalls/);
});

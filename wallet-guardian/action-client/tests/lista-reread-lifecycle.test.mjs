import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { digestBody } from "../src/canonical.mjs";
import {
  TRUSTED_LISTA,
  claimCalldata,
  encodePausedCall,
  encodeRequestStatusCall,
  encodeWithdrawalRequestsCall,
} from "../src/trusted-lista.mjs";
import { deferred, harness, jsonResponse, tick } from "./client-review-harness.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const PROXY_CODE = "0x6001";
const IMPL_CODE = "0x6002";
const BLOCK = "0x" + "cd".repeat(32);

function sha(hex) {
  return createHash("sha256").update(Buffer.from(hex.slice(2), "hex")).digest("hex");
}

function word(value) {
  return BigInt(value).toString(16).padStart(64, "0");
}

async function richTask(revision = 1) {
  const plan = {
    protocol_version: "guardian.action_plan.v1",
    task_id: "synthetic-reread-task",
    revision,
    chain_namespace: "evm",
    chain_id: 56,
    sender: ACCOUNT,
    execution_account: ACCOUNT,
    execution_account_type: "eoa",
    recipient: ACCOUNT,
    protocol_id: "lista-slisbnb",
    deployment: {
      proxy: TRUSTED_LISTA.proxy,
      implementation: IMPL,
      proxy_code_sha256: sha(PROXY_CODE),
      implementation_code_sha256: sha(IMPL_CODE),
      method: "claimWithdraw(uint256)",
    },
    intent: { type: "lista_claim_withdraw", uuid: "42", idx: 0 },
    calls: [{ to: TRUSTED_LISTA.proxy, data: claimCalldata(0), value: "0" }],
    asset_in: { symbol: "slisBNB", raw_amount: "5", decimals: 18 },
    asset_out: { symbol: "BNB", raw_amount: "6", decimals: 18 },
    preconditions: { is_claimable: true, paused: false },
    bounds: { min_out_raw: "6", slippage_bps: null },
    max_fee_wei: null,
    simulation: { status: "INCOMPLETE", effects_checked: false, source: "not_run" },
    wallet_request_allowed: false,
    block_reasons: ["synthetic"],
    human_summary: {
      purpose: "合成範例",
      sender: ACCOUNT,
      recipient: ACCOUNT,
      chain_id: 56,
      chain_label: "BNB Smart Chain (chain ID 56)",
      amount_in_raw: "5",
      amount_in_unit: "slisBNB",
      amount_out_raw: "6",
      amount_out_unit: "BNB",
      fee_label: "費用未知",
      simulation_status: "INCOMPLETE",
      wallet_request_allowed: false,
    },
  };
  plan.digest = await digestBody(plan);
  const ref = "ref_" + "ab".repeat(16);
  const task = {
    protocol_version: "guardian.task.v1",
    client_build: "action-client@0.3.0",
    ref,
    mode: "A",
    execution_authorized: false,
    wallet_request_allowed: false,
    preflight: { preflight_ok: true },
    steps: [{ id: "execute", label: "Synthetic claim", required: true, separate: true }],
    link: { issued: false },
    plan,
  };
  return {
    ref,
    revision,
    cancel_allowed: true,
    execution_authorized: false,
    send_authorized: false,
    tx_hash_verified: false,
    on_chain_revoked: false,
    task,
  };
}

function chainResult(body, { chain = "0x38" } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const method = body.method;
  if (method === "eth_chainId") return chain;
  if (method === "eth_blockNumber") return "0x64";
  if (method === "eth_getBlockByNumber") {
    return { number: "0x64", hash: BLOCK, timestamp: "0x" + (now - 5).toString(16) };
  }
  if (method === "eth_getStorageAt") return "0x" + word(IMPL);
  if (method === "eth_getCode") {
    const who = String(body.params[0]).toLowerCase();
    if (who === TRUSTED_LISTA.proxy.toLowerCase()) return PROXY_CODE;
    return IMPL_CODE;
  }
  if (method === "eth_call") {
    const data = body.params[0].data;
    if (data === encodePausedCall()) return "0x" + word(0);
    if (data === encodeWithdrawalRequestsCall(ACCOUNT)) {
      return "0x" + word(32) + word(1) + word(42) + word(5) + word(7);
    }
    if (data === encodeRequestStatusCall(ACCOUNT, 0)) return "0x" + word(1) + word(6);
  }
  throw new Error("unexpected rpc " + method);
}

function rpcResponse(result) {
  return jsonResponse({ jsonrpc: "2.0", id: 1, result });
}

async function preview(taskBody, onRpc) {
  const h = await harness({
    onFetch: (path, init) => {
      const url = String(path);
      if (url === TRUSTED_LISTA.rpcUrl) return onRpc(init);
      if (url.startsWith("/api/tasks/")) return jsonResponse(taskBody);
      return undefined;
    },
  });
  for (let i = 0; i < 500 && !h.elements.get("view").textContent.includes("Lista"); i += 1) await tick();
  return h;
}

function heldPassRpc() {
  let opened = false;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  return {
    release,
    onRpc(init) {
      const body = JSON.parse(init.body);
      if (!opened) {
        opened = true;
        return gate.then(() => rpcResponse(chainResult(body)));
      }
      return rpcResponse(chainResult(body));
    },
  };
}

test("logout drops an in-flight re-read so a late pass cannot return", async () => {
  const taskBody = await richTask();
  const rpc = heldPassRpc();
  const h = await preview(taskBody, rpc.onRpc);
  const pending = h.app.reverifyCurrent();
  for (let i = 0; i < 50; i += 1) await tick();
  await h.app.logout();
  rpc.release();
  await pending;
  assert.equal(h.app.getState().reread.reread, "insufficient");
  assert.equal(h.app.getState().reread.allowSend, "closed");
  assert.equal(h.app.getState().lastPlan, null);
  assert.equal(taskBody.task.preflight.preflight_ok, true);
});

test("cancel drops an in-flight re-read", async () => {
  const taskBody = await richTask();
  const rpc = heldPassRpc();
  const h = await preview(taskBody, rpc.onRpc);
  const pending = h.app.reverifyCurrent();
  for (let i = 0; i < 50; i += 1) await tick();
  await h.app.cancelPrivateTask();
  rpc.release();
  await pending;
  assert.equal(h.elements.get("view").textContent.includes("已取消"), true);
  assert.equal(h.app.getState().reread.reread, "insufficient");
  assert.equal(h.app.getState().reread.allowSend, "closed");
});

test("double-click keeps the newer failure when an older pass finishes last", async () => {
  const taskBody = await richTask();
  const gates = [deferred(), deferred()];
  let chainCalls = 0;
  const h = await preview(taskBody, (init) => {
    const body = JSON.parse(init.body);
    if (body.method === "eth_chainId") {
      const gate = gates[chainCalls];
      chainCalls += 1;
      return gate.promise.then((chain) => rpcResponse(chain));
    }
    return rpcResponse(chainResult(body));
  });
  const older = h.app.reverifyCurrent();
  for (let i = 0; i < 50 && chainCalls < 1; i += 1) await tick();
  const newer = h.app.reverifyCurrent();
  for (let i = 0; i < 50 && chainCalls < 2; i += 1) await tick();
  assert.equal(chainCalls, 2);
  gates[1].resolve("0x1");
  await newer;
  assert.equal(h.app.getState().reread.reread, "fail");
  assert.equal(h.app.getState().reread.reasons.includes("wrong_chain"), true);
  gates[0].resolve("0x38");
  await older;
  assert.equal(h.app.getState().reread.reread, "fail");
  assert.equal(h.app.getState().reread.allowSend, "closed");
  assert.equal(h.app.getState().reread.simulation, "not_done");
  assert.equal(taskBody.task.preflight.preflight_ok, true);
});

test("account change and chain change invalidate a stored pass", async () => {
  const taskBody = await richTask();
  let selected = ACCOUNT;
  const h = await harness({
    onWallet: (args) => (args.method === "eth_accounts" || args.method === "eth_requestAccounts") ? [selected] : undefined,
    onFetch: (path, init) => {
      const url = String(path);
      if (url === TRUSTED_LISTA.rpcUrl) return rpcResponse(chainResult(JSON.parse(init.body)));
      if (url.startsWith("/api/tasks/")) return jsonResponse(taskBody);
      return undefined;
    },
  });
  for (let i = 0; i < 500 && !h.elements.get("view").textContent.includes("Lista"); i += 1) await tick();
  await h.app.reverifyCurrent();
  assert.equal(h.app.getState().reread.reread, "pass");
  assert.equal(h.elements.get("verify-send").textContent.includes("closed"), true);
  assert.equal(h.elements.get("verify-simulation").textContent.includes("not done"), true);
  selected = "0x" + "4".repeat(40);
  h.providerListeners.get("accountsChanged")([selected]);
  assert.equal(h.app.getState().reread.reread, "insufficient");
  await h.app.reverifyCurrent();
  assert.equal(h.app.getState().reread.reread, "fail");
  assert.equal(h.app.getState().reread.reasons.includes("account_changed"), true);
  h.providerListeners.get("chainChanged")("0x1");
  assert.equal(h.app.getState().reread.reread, "insufficient");
  assert.equal(h.app.getState().reread.reasons.includes("wrong_chain"), true);
  assert.equal(h.app.getState().reread.allowSend, "closed");
});

test("a passing re-read does not enable send when the harness mainnet flag is on", async () => {
  const taskBody = await richTask();
  const h = await preview(taskBody, (init) => rpcResponse(chainResult(JSON.parse(init.body))));
  await h.app.reverifyCurrent();
  assert.equal(h.app.getState().reread.reread, "pass");
  const button = h.elements.get("view").children.flatMap(function walk(node) {
    return [node, ...(node.children ? node.children.flatMap(walk) : [])];
  }).find((node) => node._text === "在我的錢包確認");
  assert.equal(button.disabled, true);
  button.listeners.get("click")();
  assert.equal(h.walletCalls.some((call) => /send/i.test(call.method)), false);
  assert.equal(taskBody.task.preflight.preflight_ok, true);
});

test("revision bump drops a stored pass", async () => {
  let current = await richTask(1);
  const h = await harness({
    onFetch: (path, init) => {
      const url = String(path);
      if (url === TRUSTED_LISTA.rpcUrl) return rpcResponse(chainResult(JSON.parse(init.body)));
      if (url.startsWith("/api/tasks/")) return jsonResponse(current);
      return undefined;
    },
  });
  for (let i = 0; i < 500 && !h.elements.get("view").textContent.includes("Lista"); i += 1) await tick();
  await h.app.reverifyCurrent();
  assert.equal(h.app.getState().reread.reread, "pass");
  current = await richTask(2);
  await h.app.loadPrivateTask(current.ref, h.app.getState().loadGeneration);
  assert.equal(h.app.getState().privateRevision, 2);
  assert.equal(h.app.getState().reread.reread, "insufficient");
  assert.equal(h.app.getState().reread.allowSend, "closed");
  assert.equal(current.task.preflight.preflight_ok, true);
});

import assert from "node:assert/strict";
import test from "node:test";
import { assertInertPayload, assertTaskRef, boundedJson, units, validatePlan, LISTA_PROXY } from "./plan-validation.mjs";

function plan() {
  const address = "0x" + "1".repeat(40);
  return {
    protocol_version: "guardian.action_plan.v1", protocol_id: "lista-slisbnb", chain_namespace: "evm", chain_id: 56,
    sender: address, execution_account: address, recipient: address, wallet_request_allowed: false,
    deployment: { proxy: LISTA_PROXY, method: "claimWithdraw(uint256)" },
    intent: { type: "lista_claim_withdraw", uuid: "1", idx: 0 },
    calls: [{ to: LISTA_PROXY, data: "0xb13acedd" + "0".repeat(64), value: "0" }],
    preconditions: { is_claimable: true, paused: false },
    asset_in: { raw_amount: "5", decimals: 18, symbol: "slisBNB" },
    asset_out: { raw_amount: "6", decimals: 18, symbol: "BNB" },
    max_fee_wei: null, simulation: { status: "INCOMPLETE", effects_checked: false },
    human_summary: { sender: address, recipient: address, chain_id: 56,
      chain_label: "BNB Smart Chain (chain ID 56)", amount_in_raw: "5", amount_in_unit: "slisBNB",
      amount_out_raw: "6", amount_out_unit: "BNB", fee_label: "費用未知", simulation_status: "INCOMPLETE",
      wallet_request_allowed: false },
  };
}

test("a draft may be inspected, but never authorizes a wallet request", () => {
  assert.doesNotThrow(() => validatePlan(plan()));
  const p = plan(); p.wallet_request_allowed = true;
  assert.throws(() => validatePlan(p));
});

for (const [field, bad] of [["amount_in_raw", "999"], ["amount_out_unit", "ETH"],
  ["amount_in_unit", "USDT"], ["chain_label", "Ethereum"], ["chain_id", 1],
  ["fee_label", "免費"], ["simulation_status", "PASS"]]) {
  test(`tampered unsigned display field ${field} is rejected`, () => {
    const p = plan(); p.human_summary[field] = bad;
    assert.throws(() => validatePlan(p));
  });
}

test("a recomputable digest cannot substitute for a pinned target", () => {
  const p = plan(); p.calls[0].to = "0x" + "2".repeat(40);
  assert.throws(() => validatePlan(p));
});
test("extra call and stale index are independently rejected", () => {
  const p = plan(); p.calls.push({ ...p.calls[0] });
  assert.throws(() => validatePlan(p));
  const q = plan(); q.intent.idx = 2;
  assert.throws(() => validatePlan(q));
});
test("precision and account identity are fixed, not supplied by labels", () => {
  const p = plan(); p.asset_out.decimals = 0;
  assert.throws(() => validatePlan(p));
  const q = plan(); q.execution_account = "0x" + "2".repeat(40);
  assert.throws(() => validatePlan(q));
});
test("zero decimals and uint256 formatting are exact", () => {
  assert.equal(units("123", 0), "123");
  assert.equal(units("1", 18), "0.000000000000000001");
  assert.equal(units(((1n << 256n) - 1n).toString(), 0), ((1n << 256n) - 1n).toString());
  for (const decimals of [-1, 1.5, 256, Infinity]) assert.throws(() => units("1", decimals));
  assert.throws(() => units((1n << 256n).toString(), 18));
});
test("standalone plans cannot bypass nested secret screening", () => {
  assert.throws(() => assertInertPayload({ note: [{ privateKey: "secret" }] }));
  assert.throws(() => assertInertPayload({ note: [" wc:secret"] }));
});
test("task response must match the requested reference", () => {
  assertTaskRef({ protocol_version: "guardian.task.v1", ref: "a" }, "a");
  assert.throws(() => assertTaskRef({ protocol_version: "guardian.task.v1", ref: "b" }, "a"));
});
test("streaming JSON reader bounds bytes, even without Content-Length", async () => {
  const response = new Response('{"ok":true}', { headers: { "content-type": "application/json" } });
  assert.deepEqual(await boundedJson(response), { ok: true });
  const huge = new Response('"' + "x".repeat(100) + '"', { headers: { "content-type": "application/json" } });
  await assert.rejects(boundedJson(huge, 10));
});
test("paused claim with calldata cannot be previewed as a valid action", () => {
  const p = plan(); p.preconditions.paused = true;
  assert.throws(() => validatePlan(p));
});

/**
 * S-1b decisions 6 and 7: fixed refusal vectors. One test per vector; each must be
 * refused with its listed reason. The accept list is the positive control: the same
 * context lets real revocations and self-only exits through.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { assertSendable, CoreRefusal, createCoreContext, encodeCall } from "../src/execution/core/index.mjs";

const VECTORS = JSON.parse(readFileSync(new URL("./fixtures/execution-core/negative-vectors.json", import.meta.url), "utf8"));
/** Tests may name the send method; the core bundle may not (marker scan). */
const SEND_METHOD = "eth_sendTransaction";
const A = VECTORS.addresses;

function resolve(value) {
  if (Array.isArray(value)) return value.map(resolve);
  if (typeof value === "string" && value.startsWith("$")) {
    const address = A[value.slice(1)];
    assert.ok(address, `unknown alias ${value}`);
    return address;
  }
  return value;
}

const context = createCoreContext({
  deployments: VECTORS.deployments.map((item) => ({ chain_id: VECTORS.chain_id, address: A[item.alias], permission_type: item.permission_type })),
  allowlist: VECTORS.allowlist,
});

function request(vector) {
  const tx = {
    from: A.self,
    to: A[vector.to],
    data: encodeCall(vector.signature, resolve(vector.args)),
    value: vector.value ?? "0x0",
    chainId: `0x${VECTORS.chain_id.toString(16)}`,
    ...(vector.tx_extra ?? {}),
  };
  return { method: vector.method ?? SEND_METHOD, params: [tx] };
}

const REQUIRED_CATEGORIES = [
  "multicall",
  "execute",
  "aggregate",
  "permit",
  "nonempty_bytes",
  "nonzero_approve",
  "unlimited_approve",
  "allowance_increase",
  "approval_for_all",
  "delegation",
  "authorization",
  "permit2",
  "transfer_from",
  "transfer_to_other",
  "admin",
  "upgrade",
  "signature_mismatch",
  "nonzero_value",
  "to_not_registered",
  "method_not_send",
  "receiver_not_self",
];

test("vector file covers every required category", () => {
  const present = new Set(VECTORS.vectors.map((item) => item.category));
  for (const category of REQUIRED_CATEGORIES) assert.ok(present.has(category), category);
  const ids = VECTORS.vectors.map((item) => item.id);
  assert.equal(new Set(ids).size, ids.length, "vector ids are unique");
});

for (const vector of VECTORS.accept) {
  test(`accept (positive control): ${vector.id}`, () => {
    const summary = assertSendable(request(vector), { account: A.self, signature: vector.signature }, context);
    assert.equal(summary.to, A[vector.to]);
  });
}

for (const vector of VECTORS.vectors) {
  test(`refuse ${vector.category}: ${vector.id}`, () => {
    assert.throws(
      () => assertSendable(request(vector), { account: A.self, signature: vector.signature }, context),
      (error) => {
        assert.ok(error instanceof CoreRefusal, `expected CoreRefusal, got ${error}`);
        assert.equal(error.reason, vector.expect);
        return true;
      },
    );
  });
}

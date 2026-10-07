import assert from "node:assert/strict";
import test from "node:test";
import { RPC_BODY_LIMIT, readRpcResult, takeRpcResult } from "../src/rpc-result.mjs";
import { fetchTrustedRpc, TRUSTED_LISTA } from "../src/trusted-lista.mjs";

const ID = 1;

function bytesOf(value) {
  return new TextEncoder().encode(typeof value === "string" ? value : JSON.stringify(value));
}

function responseFrom(bytes, { status = 200 } = {}) {
  let cancelled = false;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = new Response(stream, { status, headers: { "content-type": "application/json" } });
  return { response, released: () => cancelled || stream.locked === false };
}

test("MALFORMED_RPC_ENVELOPE bare result is rejected", async () => {
  const { response, released } = responseFrom(bytesOf({ result: "0x38" }));
  await assert.rejects(readRpcResult(response, ID), /rpc version/);
  assert.equal(released(), true);
  assert.equal(response.body.locked, false);
});

test("MALFORMED_RPC_ENVELOPE wrong version and id is rejected", async () => {
  const { response, released } = responseFrom(bytesOf({ jsonrpc: "1.0", id: 999, result: "0x38" }));
  await assert.rejects(readRpcResult(response, ID), /rpc version/);
  assert.equal(released(), true);
});

test("matching id is required and string ids do not match numbers", () => {
  assert.throws(() => takeRpcResult({ jsonrpc: "2.0", id: "1", result: "0x38" }, ID), /rpc id/);
  assert.throws(() => takeRpcResult({ jsonrpc: "2.0", id: 2, result: "0x38" }, ID), /rpc id/);
});

test("result and error are mutually exclusive", () => {
  assert.throws(() => takeRpcResult({ jsonrpc: "2.0", id: ID, result: "0x38", error: { code: 1, message: "no" } }, ID), /xor/);
  assert.throws(() => takeRpcResult({ jsonrpc: "2.0", id: ID }, ID), /xor/);
  const failure = takeRpcResult.bind(null, { jsonrpc: "2.0", id: ID, error: { code: -32602, message: "invalid params" } }, ID);
  assert.throws(failure, /invalid params/);
});

test("a valid envelope returns only the result", () => {
  assert.equal(takeRpcResult({ jsonrpc: "2.0", id: ID, result: "0x38" }, ID), "0x38");
});

test("UNBOUNDED_RPC_BODY above 1 MiB is rejected and the reader is released", async () => {
  const pad = "a".repeat(RPC_BODY_LIMIT + 8);
  const { response, released } = responseFrom(bytesOf(pad));
  await assert.rejects(readRpcResult(response, ID), /too large/);
  assert.equal(released(), true);
  assert.equal(response.body.locked, false);
});

test("fatal UTF-8 rejects the body and releases the reader", async () => {
  const { response, released } = responseFrom(new Uint8Array([0xff, 0xfe, 0xfd]));
  await assert.rejects(readRpcResult(response, ID), /utf-8/);
  assert.equal(released(), true);
});

test("malformed JSON releases the reader", async () => {
  const { response } = responseFrom(bytesOf("{"));
  await assert.rejects(readRpcResult(response, ID), /not json/);
  assert.equal(response.body.locked, false);
});

test("fetchTrustedRpc rejects a bare result and does not return it", async () => {
  const fetchImpl = async (url) => {
    assert.equal(url, TRUSTED_LISTA.rpcUrl);
    return new Response(JSON.stringify({ result: "0x38" }), { status: 200 });
  };
  await assert.rejects(fetchTrustedRpc("eth_chainId", [], { fetchImpl }), /rpc version/);
});

test("fetchTrustedRpc returns a matching 2.0 result", async () => {
  const fetchImpl = async () => new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: "0x38" }), { status: 200 });
  assert.equal(await fetchTrustedRpc("eth_chainId", [], { fetchImpl }), "0x38");
});

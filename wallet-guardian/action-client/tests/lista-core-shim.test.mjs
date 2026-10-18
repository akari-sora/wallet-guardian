/**
 * S-1b decision 10: trusted-lista.mjs is a re-export shim over the client core.
 * Every export keeps its name, value, and behavior. Outputs are compared byte for byte
 * against the 7fa97fe module, vendored in tests/fixtures/lista-baseline/.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

import * as baseline from "./fixtures/lista-baseline/trusted-lista.7fa97fe.mjs";
import * as shim from "../src/trusted-lista.mjs";
import * as core from "../src/execution/core/lista.mjs";
import { buildListaClaimTransaction, encodeCall, LISTA_CLAIM_FULL_SIGNATURE } from "../src/execution/core/index.mjs";

const BASELINE_FILE = fileURLToPath(new URL("./fixtures/lista-baseline/trusted-lista.7fa97fe.mjs", import.meta.url));
const SHIM_FILE = fileURLToPath(new URL("../src/trusted-lista.mjs", import.meta.url));
/** sha256 of `git show 7fa97fe:wallet-guardian/action-client/src/trusted-lista.mjs`. */
const BASELINE_SHA256 = "78750257b7d2f09785945f62126346855253de68646f1a94d057a8203047da65";
const SELF = "0x00000000000000000000000000000000000a11ce";

function stable(value) {
  return JSON.stringify(value, (_key, item) => (typeof item === "bigint" ? `bigint:${item}` : item));
}

function shape(value) {
  if (value === null || typeof value !== "object") return typeof value;
  const out = { frozen: Object.isFrozen(value), array: Array.isArray(value), keys: {} };
  for (const key of Object.keys(value)) out.keys[key] = shape(value[key]);
  return out;
}

function outcome(fn, args) {
  try {
    return stable({ ok: fn(...args) });
  } catch (error) {
    return stable({ name: error?.name, message: error?.message });
  }
}

async function outcomeAsync(fn, args) {
  try {
    return stable({ ok: await fn(...args) });
  } catch (error) {
    return stable({ name: error?.name, message: error?.message });
  }
}

/** Deterministic PRNG so the vectors are the same on every run. */
function prng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randomHex(rand, bytes) {
  let hex = "0x";
  for (let index = 0; index < bytes; index += 1) hex += Math.floor(rand() * 256).toString(16).padStart(2, "0");
  return hex;
}

function word(value) {
  return BigInt(value).toString(16).padStart(64, "0");
}

const rand = prng(20261017);
const ADDRESSES = [SELF, "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6", "0xABCDEFabcdef0123456789ABCDEFabcdef012345", "0x12", "", null, 7, "0xZZ00000000000000000000000000000000000000", "1adB950d8bB3dA4bE104211D5AB038628e477fE6"];
for (let index = 0; index < 20; index += 1) ADDRESSES.push(randomHex(rand, 20));
const IDX = [0, 1, 2, 31, 255, 256, 65535, 2 ** 32, Number.MAX_SAFE_INTEGER, -1, 1.5, Number.NaN, Infinity, "1", null, undefined, 2 ** 53, 1n];
for (let index = 0; index < 40; index += 1) IDX.push(Math.floor(rand() * Number.MAX_SAFE_INTEGER));
const HEX = ["0x", "0x0", "0x00", "", null, "zz", "0x" + "0".repeat(64), "0x" + "0".repeat(63) + "1", "0x" + "0".repeat(63) + "2", "0x" + "1".repeat(64)];
for (let index = 0; index < 40; index += 1) HEX.push(randomHex(rand, Math.floor(rand() * 200)));
for (let count = 0; count < 4; count += 1) {
  let body = word(32) + word(count);
  for (let row = 0; row < count; row += 1) body += word(1000 + row) + word(10n ** 18n + BigInt(row)) + word(1700000000 + row);
  HEX.push("0x" + body);
  HEX.push("0x" + body.slice(0, -2));
}
HEX.push("0x" + word(1) + word(123456789));
HEX.push("0x" + word(0) + word(0));
HEX.push("0x" + word(32) + word(129));
HEX.push("0x" + "0".repeat(24) + "1adb950d8bb3da4be104211d5ab038628e477fe6");
HEX.push("0x1adb950d8bb3da4be104211d5ab038628e477fe6");
const QUANTITIES = ["0x0", "0x1", "0xff", "0x1fffffffffffff", "0x20000000000000", "0x", "1", 1, null, "0xg"];
const SIGNATURES = ["claimWithdraw(uint256)", "paused()", "", "ClaimWithdrawal(address,uint256,uint256)", "transfer(address,uint256)", "ünïcode()", "a".repeat(300)];
const READ_CALLS = [
  ["eth_call", [{}, "latest"]],
  ["eth_getTransactionCount", [SELF, "latest"]],
  ["eth_getTransactionCount", [SELF, "pending"]],
  ["eth_getTransactionCount", [SELF, "earliest"]],
  ["eth_getTransactionCount", [SELF]],
  ["eth_getBlockByNumber", ["latest", true]],
  ["eth_getBlockByNumber", ["latest", "true"]],
  ["eth_getBlockByNumber", ["latest"]],
  ["eth_getLogs", [{}]],
  ["eth_sendRawTransaction", ["0x"]],
  [null, []],
  [42, []],
];

test("baseline fixture is the 7fa97fe module with only the import path changed", () => {
  const text = readFileSync(BASELINE_FILE, "utf8").replace('from "../../../src/rpc-result.mjs";', 'from "./rpc-result.mjs";');
  assert.equal(createHash("sha256").update(text, "utf8").digest("hex"), BASELINE_SHA256);
});

test("shim exports exactly the same names as 7fa97fe", () => {
  assert.deepEqual(Object.keys(shim).sort(), Object.keys(baseline).sort());
  for (const name of Object.keys(baseline)) assert.equal(typeof shim[name], typeof baseline[name], name);
});

test("shim re-exports the core bindings (no second copy)", () => {
  for (const name of Object.keys(core)) {
    if (name in shim) assert.equal(shim[name], core[name], name);
  }
});

test("shim keeps the EIP-1967 slot literal the S-1a candidate generator reads", () => {
  assert.ok(readFileSync(SHIM_FILE, "utf8").includes("0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"));
});

test("constants are identical, including frozen shape", () => {
  for (const name of ["CLAIM_SIGNATURE", "DEPLOYMENT_APPROVALS", "TRUSTED_LISTA", "CLAIM_WITHDRAWAL_EVENT", "CLAIM_WITHDRAWAL_TOPIC", "CLAIM_SELECTOR"]) {
    assert.equal(stable(shim[name]), stable(baseline[name]), name);
    assert.deepEqual(shape(shim[name]), shape(baseline[name]), name);
  }
});

test("claimCalldata is byte-identical for every vector", () => {
  for (const idx of IDX) assert.equal(outcome(shim.claimCalldata, [idx]), outcome(baseline.claimCalldata, [idx]), String(idx));
});

test("read encoders are byte-identical", () => {
  assert.equal(outcome(shim.encodePausedCall, []), outcome(baseline.encodePausedCall, []));
  for (const account of ADDRESSES) {
    assert.equal(outcome(shim.encodeWithdrawalRequestsCall, [account]), outcome(baseline.encodeWithdrawalRequestsCall, [account]));
    for (const idx of IDX.slice(0, 12)) {
      assert.equal(outcome(shim.encodeRequestStatusCall, [account, idx]), outcome(baseline.encodeRequestStatusCall, [account, idx]));
    }
  }
});

test("decoders and helpers give identical results and identical errors", () => {
  for (const hex of HEX) {
    for (const name of ["decodeBool", "decodeStatus", "decodeWithdrawalRequests", "addressFromStorage", "hexQuantity"]) {
      assert.equal(outcome(shim[name], [hex]), outcome(baseline[name], [hex]), `${name}(${String(hex).slice(0, 20)})`);
    }
  }
  for (const value of QUANTITIES) assert.equal(outcome(shim.hexQuantity, [value]), outcome(baseline.hexQuantity, [value]));
  for (const signature of SIGNATURES) {
    assert.equal(outcome(shim.selectorOf, [signature]), outcome(baseline.selectorOf, [signature]));
    assert.equal(outcome(shim.eventTopic, [signature]), outcome(baseline.eventTopic, [signature]));
  }
  for (const left of ADDRESSES) {
    assert.equal(outcome(shim.isAddress, [left]), outcome(baseline.isAddress, [left]));
    for (const right of ADDRESSES.slice(0, 6)) {
      assert.equal(outcome(shim.sameAddress, [left, right]), outcome(baseline.sameAddress, [left, right]));
    }
  }
  for (const task of [undefined, {}, { rpcUrl: "https://evil.example", proxy: SELF, abi: ["transfer(address,uint256)"] }]) {
    assert.equal(outcome(shim.trustedQuerySources, [task]), outcome(baseline.trustedQuerySources, [task]));
    assert.deepEqual(shape(shim.trustedQuerySources(task)), shape(baseline.trustedQuerySources(task)));
  }
  for (const [method, params] of READ_CALLS) {
    assert.equal(outcome(shim.assertReadMethod, [method]), outcome(baseline.assertReadMethod, [method]), String(method));
    assert.equal(outcome(shim.assertReadCall, [method, params]), outcome(baseline.assertReadCall, [method, params]), String(method));
  }
});

test("codeSha256 is identical", async () => {
  for (const hex of HEX) assert.equal(await outcomeAsync(shim.codeSha256, [hex]), await outcomeAsync(baseline.codeSha256, [hex]));
});

test("fetchTrustedRpc sends identical requests and returns identical results", async () => {
  const scenarios = [
    { status: 200, body: JSON.stringify({ jsonrpc: "2.0", id: 1, result: "0x" + word(1) }) },
    { status: 200, body: JSON.stringify({ jsonrpc: "2.0", id: 2, result: "0x1" }) },
    { status: 200, body: JSON.stringify({ jsonrpc: "2.0", id: 1, error: { code: -32000, message: "x" } }) },
    { status: 200, body: "not json" },
    { status: 500, body: "fail" },
  ];
  for (const [method, params] of [["eth_call", [{ to: shim.TRUSTED_LISTA.proxy, data: shim.claimCalldata(0) }, "latest"]], ["eth_getTransactionCount", [SELF, "pending"]], ["eth_getLogs", [{}]]]) {
    for (const scenario of scenarios) {
      const run = async (module) => {
        const calls = [];
        const fetchImpl = async (url, init) => {
          calls.push(stable({ url, ...init, signal: init.signal === undefined ? "none" : "set" }));
          return new Response(scenario.body, { status: scenario.status });
        };
        const result = await outcomeAsync(module.fetchTrustedRpc, [method, params, { fetchImpl }]);
        return { result, calls };
      };
      assert.deepEqual(await run(shim), await run(baseline), `${method} ${scenario.status} ${scenario.body.slice(0, 20)}`);
    }
  }
});

test("Lista through the core: same bytes as claimCalldata, value 0, passes the general checks", () => {
  for (const idx of [0, 1, 2, 77, Number.MAX_SAFE_INTEGER]) {
    const tx = buildListaClaimTransaction({ account: SELF, idx });
    assert.equal(tx.data, baseline.claimCalldata(idx));
    assert.equal(tx.to, baseline.TRUSTED_LISTA.proxy);
    assert.equal(tx.value, "0x0");
    assert.equal(tx.chainId, "0x38");
    assert.equal(encodeCall(LISTA_CLAIM_FULL_SIGNATURE, [idx]), baseline.claimCalldata(idx));
  }
  assert.throws(() => buildListaClaimTransaction({ account: SELF, idx: -1 }), /bad idx/);
});

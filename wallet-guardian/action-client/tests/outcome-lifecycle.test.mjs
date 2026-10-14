import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  assertReadCall,
  CLAIM_WITHDRAWAL_TOPIC,
  claimCalldata,
  encodePausedCall,
  encodeRequestStatusCall,
  encodeWithdrawalRequestsCall,
  fetchTrustedRpc,
  TRUSTED_LISTA,
} from "../src/trusted-lista.mjs";
import {
  classifyWalletError,
  guideListaModeA,
  retryAllowed,
  WALLET_RETURN_TABLE_VERSION,
} from "../src/mode-a.mjs";
import { confirmModeAOutcome } from "../src/outcome-confirm.mjs";
import {
  openOutcomeStore,
  outcomeKey,
  RETAIN_PENDING_ON_LOGOUT,
  writePending,
  writeRequested,
} from "../src/outcome-store.mjs";
import {
  LIST_DISAPPEARANCE_IS_ENOUGH,
  MAX_FULL_BLOCK_TRANSACTIONS,
  pollVisibleOutcome,
  VERIFY_ACTION,
  verifyStoredOutcome,
} from "../src/outcome-verify.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const OTHER = "0x" + "3".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const NOW = 1_700_000_000;
const PROXY_CODE = "0x6001";
const IMPL_CODE = "0x6002";
const BLOCK = "0x" + "ab".repeat(32);
const ORIGIN = "https://preview.example";
const REF = "ref_" + "ab".repeat(16);
const HASH = "0x" + "11".repeat(32);
const HASH_B = "0x" + "22".repeat(32);

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
const SHIFTED = requestsReturn([
  { uuid: 1, amount: 1, start: 1 },
  { uuid: 42, amount: 5, start: 7 },
]);
const STATUS_OK = "0x" + word(1) + word(6);
const EMPTY_REQUESTS = "0x" + word(32) + word(0);

function approvedEntry() {
  return {
    id: "test-only-lista",
    registryVersion: 1,
    implementation: IMPL,
    proxyCodeSha256: sha(PROXY_CODE),
    implementationCodeSha256: sha(IMPL_CODE),
    provenance: "test-only injection, not a production pin",
    validFromUnix: 0,
    validUntilUnix: NOW + 3600,
    permanent: false,
    retired: false,
  };
}

function plan(account = ACCOUNT) {
  return {
    protocol_id: "lista-slisbnb",
    chain_id: 56,
    sender: account,
    recipient: account,
    execution_account: account,
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
    local_ttl_unix: NOW + 3600,
  };
}

function script(account = ACCOUNT, mutate = {}) {
  const log = [];
  let round = 0;
  async function call(method, params) {
    log.push({ method, params: structuredClone(params) });
    if (method === "eth_chainId") round += 1;
    const second = round >= 2;
    if (method === "eth_chainId") return "0x38";
    if (method === "eth_blockNumber") return "0x64";
    if (method === "eth_getBlockByNumber") {
      return { number: "0x64", hash: BLOCK, timestamp: "0x" + (NOW - 10).toString(16) };
    }
    const tag = params[params.length - 1];
    if (!tag || tag.requireCanonical !== true || tag.blockHash !== BLOCK) {
      throw new Error("state read was not pinned to the header hash");
    }
    if (method === "eth_getStorageAt") return "0x" + word(IMPL);
    if (method === "eth_getCode") {
      const who = String(params[0]).toLowerCase();
      if (who === TRUSTED_LISTA.proxy.toLowerCase()) return PROXY_CODE;
      return IMPL_CODE;
    }
    if (method === "eth_call") {
      if (params[0].data === encodePausedCall()) return "0x" + word(0);
      if (params[0].data === encodeWithdrawalRequestsCall(account)) {
        return second && mutate.idxDrift ? SHIFTED : ONE_REQUEST;
      }
      const idx = second && mutate.idxDrift ? 1 : 0;
      if (params[0].data === encodeRequestStatusCall(account, idx)) return STATUS_OK;
    }
    throw new Error("unexpected method " + method);
  }
  return { call, log };
}

function identityFor(account = ACCOUNT, revision = 1) {
  return { origin: ORIGIN, account, ref: REF, revision };
}

function guideFor(account = ACCOUNT, mutate = {}) {
  const chain = script(account, mutate);
  const current = plan(account);
  return {
    chain,
    input: {
      plan: current,
      task: { mode: "A", ref: REF },
      account,
      connectedAccount: account,
      walletChainId: 56,
      walletIdentityVerified: true,
      planDigest: current.digest,
      revision: current.revision,
      nowUnix: NOW,
      startedAt: NOW,
      call: chain.call,
      testApprovals: [approvedEntry()],
      outcomeIdentity: identityFor(account),
    },
  };
}

function memoryIdb() {
  const data = new Map();
  return {
    data,
    open() {
      const request = {};
      const db = {
        objectStoreNames: { contains() { return true; } },
        createObjectStore() {},
        transaction() {
          return {
            objectStore() {
              return {
                get(key) {
                  const req = {};
                  queueMicrotask(() => {
                    req.result = data.has(key) ? data.get(key) : undefined;
                    req.onsuccess?.();
                  });
                  return req;
                },
                put(value, key) {
                  data.set(key, value);
                  const req = {};
                  queueMicrotask(() => {
                    req.onsuccess?.();
                  });
                  return req;
                },
              };
            },
          };
        },
      };
      queueMicrotask(() => {
        request.result = db;
        request.onsuccess?.();
      });
      return request;
    },
  };
}

function throwingIdb() {
  return {
    open() {
      throw new Error("InvalidStateError");
    },
  };
}

function memorySession() {
  const data = new Map();
  return {
    data,
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(String(key), String(value));
    },
    removeItem(key) {
      data.delete(key);
    },
  };
}

async function opened({ indexedDB = memoryIdb(), sessionStorage = null } = {}) {
  const backend = indexedDB;
  const store = await openOutcomeStore({ indexedDB, sessionStorage });
  return { store, backend };
}

function walletError(code, message, name) {
  const error = new Error(message);
  if (code != null) error.code = code;
  if (name) error.name = name;
  return error;
}

test("retryAllowed is a rejected signature plus a new click and two re-reads", () => {
  assert.equal(retryAllowed({ status: "rejected", userClick: true, rereads: 2 }), true);
  assert.equal(retryAllowed({ status: "rejected", userClick: true, rereads: 3 }), true);
  assert.equal(retryAllowed({ status: "rejected", userClick: false, rereads: 2 }), false);
  assert.equal(retryAllowed({ status: "rejected", userClick: true, rereads: 1 }), false);
  assert.equal(retryAllowed({ status: "rejected", userClick: true, rereads: 0 }), false);
  assert.equal(retryAllowed({ status: "failed", userClick: true, rereads: 2 }), false);
  assert.equal(retryAllowed({ status: "pending", userClick: true, rereads: 2 }), false);
  assert.equal(retryAllowed({ status: "unknown", userClick: true, rereads: 2 }), false);
  assert.equal(retryAllowed({ status: "confirmed", userClick: true, rereads: 2 }), false);
});

test("the wallet return table rejects only 4001 and WalletConnect 5000 User rejected", () => {
  assert.equal(WALLET_RETURN_TABLE_VERSION, 1);
  assert.equal(classifyWalletError(walletError(4001, "User rejected the request.")), "rejected");
  assert.equal(classifyWalletError(walletError(5000, "User rejected.")), "rejected");
  assert.equal(classifyWalletError(walletError(5000, "User rejected the request")), "rejected");
  assert.equal(classifyWalletError(walletError(5000, "Session disconnected")), "ambiguous");
  assert.equal(classifyWalletError(walletError(-32603, "User rejected")), "ambiguous");
  assert.equal(classifyWalletError(walletError(undefined, "timeout", "TimeoutError")), "ambiguous");
  assert.equal(classifyWalletError(walletError(4900, "Disconnected")), "ambiguous");
  assert.equal(classifyWalletError(walletError(undefined, "user rejected")), "ambiguous");
});

test("AC1 the store already says requested inside the wallet callback", async () => {
  const { store } = await opened();
  const guide = guideFor();
  const key = outcomeKey(guide.input.outcomeIdentity);
  let seen = null;
  const result = await confirmModeAOutcome({
    ...guide.input,
    store,
    userClick: true,
    async requestWallet() {
      seen = await store.get(key);
      return HASH;
    },
  });
  assert.equal(seen.status, "requested");
  assert.equal(Object.hasOwn(seen, "txHash"), false);
  assert.equal(typeof seen.intent_id, "string");
  assert.equal(Number.isSafeInteger(seen.requested_at), true);
  assert.equal(seen.submission.uuid, "42");
  assert.equal(seen.submission.account, ACCOUNT);
  assert.equal(result.walletRequests, 1);
  assert.equal(result.autoRetries, 0);
  assert.equal(result.reads, 18);
  assert.equal(result.record.status, "pending");
  assert.equal(result.record.txHash, HASH);
  assert.equal(result.record.submission.txHash, HASH);
  assert.equal(result.outcome.done, false);
  assert.equal(result.outcome.hashIsDone, false);
  assert.equal(result.outcome.status, "pending");
});

test("AC2 a pending record survives reload and blocks confirm with zero wallet requests", async () => {
  const idb = memoryIdb();
  const session = memorySession();
  const first = await openOutcomeStore({ indexedDB: idb, sessionStorage: session });
  const identity = identityFor();
  await writePending(first, identity, {
    intent_id: "intent-pending",
    requested_at: NOW,
    txHash: HASH,
    submission: { account: ACCOUNT, uuid: "42", idx: 0 },
  });
  const reloaded = await openOutcomeStore({ indexedDB: idb, sessionStorage: memorySession() });
  assert.equal((await reloaded.get(outcomeKey(identity))).status, "pending");
  assert.equal((await reloaded.get(outcomeKey(identity))).txHash, HASH);
  const passed = await guideListaModeA(guideFor().input);
  assert.equal(passed.first.reread, "pass");
  assert.equal(passed.second.reread, "pass");
  assert.equal(passed.walletRequested, false);
  const guide = guideFor();
  let walletCalls = 0;
  const blocked = await confirmModeAOutcome({
    ...guide.input,
    store: reloaded,
    userClick: true,
    async requestWallet() {
      walletCalls += 1;
      return HASH;
    },
  });
  assert.equal(blocked.blocked, "verify_outcome");
  assert.equal(blocked.walletRequests, 0);
  assert.equal(blocked.walletRequested, false);
  assert.equal(blocked.autoRetries, 0);
  assert.equal(walletCalls, 0);
  assert.equal(guide.chain.log.length, 0);
  assert.equal((await reloaded.get(outcomeKey(identity))).status, "pending");
  assert.equal(session.data.size, 0);
});

test("AC3 a 4001 rejection retries only after a new click and two passing re-reads", async () => {
  const { store } = await opened();
  const identity = identityFor();
  const calls = [];
  async function requestWallet() {
    calls.push("wallet");
    throw walletError(4001, "User rejected the request.");
  }
  const first = await confirmModeAOutcome({
    ...guideFor().input,
    store,
    userClick: true,
    requestWallet,
  });
  assert.equal(first.record.status, "rejected");
  assert.equal(first.outcome.status, "rejected");
  assert.notEqual(first.outcome.status, "failed");
  assert.equal(first.walletRequests, 1);
  assert.equal(first.autoRetries, 0);
  assert.equal(calls.length, 1);
  const quiet = guideFor();
  const noClick = await confirmModeAOutcome({
    ...quiet.input,
    store,
    userClick: false,
    requestWallet,
  });
  assert.equal(noClick.blocked, "no_auto_retry");
  assert.equal(noClick.walletRequests, 0);
  assert.equal(noClick.autoRetries, 0);
  assert.equal(quiet.chain.log.length, 0);
  assert.equal(calls.length, 1);
  const drifted = guideFor(ACCOUNT, { idxDrift: true });
  const onePairFailed = await confirmModeAOutcome({
    ...drifted.input,
    store,
    userClick: true,
    requestWallet,
  });
  assert.equal(onePairFailed.walletRequests, 0);
  assert.equal(onePairFailed.second.reread, "fail");
  assert.equal(calls.length, 1);
  assert.equal((await store.get(outcomeKey(identity))).status, "rejected");
  const again = await confirmModeAOutcome({
    ...guideFor().input,
    store,
    userClick: true,
    async requestWallet() {
      calls.push("wallet");
      return HASH;
    },
  });
  assert.equal(again.walletRequests, 1);
  assert.equal(again.autoRetries, 0);
  assert.equal(again.reads, 18);
  assert.equal(again.record.status, "pending");
  assert.equal(calls.length, 2);
});

test("WalletConnect 5000 User rejected is rejected, and any other 5000 is unknown", async () => {
  const rejected = await confirmModeAOutcome({
    ...guideFor().input,
    store: (await opened()).store,
    userClick: true,
    requestWallet() {
      throw walletError(5000, "User rejected.");
    },
  });
  assert.equal(rejected.record.status, "rejected");
  assert.equal(rejected.autoRetries, 0);
  const ambiguous = await confirmModeAOutcome({
    ...guideFor().input,
    store: (await opened()).store,
    userClick: true,
    requestWallet() {
      throw walletError(5000, "Socket stalled");
    },
  });
  assert.equal(ambiguous.record.status, "unknown");
  assert.notEqual(ambiguous.record.status, "rejected");
});

test("AC4 internal errors, timeouts, and disconnects stay unknown and cannot be resent", async () => {
  const cases = [
    walletError(-32603, "User rejected"),
    walletError(undefined, "timeout", "TimeoutError"),
    walletError(4900, "Disconnected"),
  ];
  for (const error of cases) {
    const { store } = await opened();
    const calls = [];
    const first = await confirmModeAOutcome({
      ...guideFor().input,
      store,
      userClick: true,
      requestWallet() {
        calls.push("wallet");
        throw error;
      },
    });
    assert.equal(first.record.status, "unknown");
    assert.notEqual(first.record.status, "rejected");
    assert.equal(first.autoRetries, 0);
    assert.equal(calls.length, 1);
    const guide = guideFor();
    const again = await confirmModeAOutcome({
      ...guide.input,
      store,
      userClick: true,
      requestWallet() {
        calls.push("wallet");
        return HASH;
      },
    });
    assert.equal(again.blocked, "verify_outcome");
    assert.equal(again.walletRequests, 0);
    assert.equal(guide.chain.log.length, 0);
    assert.equal(calls.length, 1);
    assert.equal((await store.get(outcomeKey(identityFor()))).status, "unknown");
  }
});

function addressTopic(account) {
  return "0x" + account.slice(2).toLowerCase().padStart(64, "0");
}

function receiptOf(mutate = {}) {
  const blockHash = "0x" + "44".repeat(32);
  return {
    transactionHash: HASH,
    status: mutate.status ?? "0x1",
    from: ACCOUNT,
    to: TRUSTED_LISTA.proxy,
    gasUsed: "0x2",
    effectiveGasPrice: "0x3",
    chainId: mutate.chainId === undefined ? 56 : mutate.chainId,
    blockHash: mutate.blockHash ?? blockHash,
    blockNumber: 100,
    logs: mutate.logs === undefined ? [{
      address: TRUSTED_LISTA.proxy,
      transactionHash: HASH,
      blockHash,
      blockNumber: 100,
      topics: [CLAIM_WITHDRAWAL_TOPIC, addressTopic(ACCOUNT)],
      data: "0x" + word(0) + word(6),
    }] : mutate.logs,
  };
}

function chainMock(mutate = {}) {
  const calls = [];
  const blockHash = "0x" + "44".repeat(32);
  const receipt = mutate.receipt === undefined ? receiptOf(mutate) : mutate.receipt;
  async function call(method, params) {
    calls.push({ method, params });
    if (method === "eth_getTransactionByHash") return mutate.tx === undefined ? { hash: HASH, from: ACCOUNT } : mutate.tx;
    if (method === "eth_getTransactionReceipt") return mutate.receiptMissing ? null : receipt;
    if (method === "eth_chainId") return mutate.chainHex ?? "0x38";
    if (method === "eth_getTransactionCount") return mutate.nonceCount ?? "0x1";
    if (method === "eth_getBlockByNumber") {
      assert.equal(params[1], true);
      return {
        hash: mutate.returnedBlockHash ?? receipt.blockHash,
        number: "0x64",
        transactions: mutate.transactions ?? [{ hash: HASH, from: ACCOUNT }],
      };
    }
    if (method === "eth_getBalance") {
      const block = params[1];
      return block === "0x63" ? "1000" : "1000";
    }
    if (method === "eth_call") return EMPTY_REQUESTS;
    throw new Error("unexpected " + method);
  }
  return { call, calls };
}

async function seedPending(store, identity, extra = {}) {
  await writePending(store, identity, {
    intent_id: "intent-seed",
    requested_at: NOW,
    txHash: HASH,
    submission: {
      account: ACCOUNT,
      idx: 0,
      uuid: "42",
      amountOut: "6",
      calldata: claimCalldata(0),
      to: TRUSTED_LISTA.proxy,
      chainId: 56,
      ...extra,
    },
  });
}

test("AC5 a hash the chain does not have is never confirmed", async () => {
  const { store } = await opened();
  const identity = identityFor();
  await seedPending(store, identity);
  const missing = chainMock({ tx: null, receiptMissing: true });
  const first = await verifyStoredOutcome(store, identity, {
    call: missing.call,
    invocation: { kind: "user", action: VERIFY_ACTION },
  });
  assert.notEqual(first.status, "confirmed");
  assert.equal(first.verified, false);
  assert.equal(first.hashIsDone, false);
  assert.equal(first.listDisappearanceIsEnough, false);
  assert.equal(LIST_DISAPPEARANCE_IS_ENOUGH, false);
  const second = await verifyStoredOutcome(store, identity, {
    call: missing.call,
    invocation: { kind: "user", action: VERIFY_ACTION },
  });
  assert.notEqual(second.status, "confirmed");
  assert.equal((await store.get(outcomeKey(identity))).status === "confirmed", false);
  const replacedStore = (await opened()).store;
  await seedPending(replacedStore, identity, { nonce: 4 });
  const spent = chainMock({ tx: null, receiptMissing: true, nonceCount: "0x5" });
  const replaced = await verifyStoredOutcome(replacedStore, identity, {
    call: spent.call,
    invocation: { kind: "user", action: VERIFY_ACTION },
  });
  assert.equal(replaced.status, "replaced");
  assert.equal(replaced.confirmed, false);
  assert.equal((await replacedStore.get(outcomeKey(identity))).status, "replaced");
});

test("AC6 a 0x0 receipt fails and unbound receipts stay unverified", async () => {
  const user = { kind: "user", action: VERIFY_ACTION };
  const { store } = await opened();
  const identity = identityFor();
  await seedPending(store, identity);
  const reverted = await verifyStoredOutcome(store, identity, {
    call: chainMock({ status: "0x0", logs: [] }).call,
    invocation: user,
  });
  assert.equal(reverted.status, "failed");
  assert.equal(reverted.reason, "receipt_failed");
  assert.equal(reverted.verified, false);
  assert.equal(reverted.listDisappearanceIsEnough, false);
  assert.equal((await store.get(outcomeKey(identity))).status, "failed");
  const afterFail = await confirmModeAOutcome({
    ...guideFor().input,
    store,
    userClick: true,
    requestWallet() {
      throw new Error("must not send");
    },
  });
  assert.equal(afterFail.blocked, "no_auto_retry");
  assert.equal(afterFail.walletRequests, 0);

  async function unverified(mutate, reason) {
    const fresh = (await opened()).store;
    await seedPending(fresh, identity);
    const verdict = await verifyStoredOutcome(fresh, identity, { call: chainMock(mutate).call, invocation: user });
    assert.notEqual(verdict.status, "confirmed");
    assert.equal(verdict.verified, false);
    assert.equal(verdict.complete, false);
    assert.equal(verdict.reason, reason);
    assert.equal(verdict.listDisappearanceIsEnough, false);
    assert.notEqual((await fresh.get(outcomeKey(identity))).status, "confirmed");
  }
  await unverified({ logs: [] }, "missing_claim_event");
  await unverified({ chainHex: "0x1" }, "wrong_chain");
  await unverified({ returnedBlockHash: "0x" + "55".repeat(32) }, "block_mismatch");
  const capped = Array.from({ length: MAX_FULL_BLOCK_TRANSACTIONS + 1 }, (_item, index) => ({
    hash: "0x" + index.toString(16).padStart(64, "0"),
    from: OTHER,
  }));
  await unverified({ transactions: capped }, "native_delta_ambiguous");
});

test("a matching receipt can be confirmed without treating the hash or a vanished list as enough", async () => {
  const { store } = await opened();
  const identity = identityFor();
  await seedPending(store, identity);
  const mock = chainMock();
  const verdict = await verifyStoredOutcome(store, identity, {
    call: mock.call,
    invocation: { kind: "user", action: VERIFY_ACTION },
  });
  assert.equal(verdict.status, "confirmed");
  assert.equal(verdict.verified, true);
  assert.equal(verdict.hashIsDone, false);
  assert.equal(verdict.listDisappearanceIsEnough, false);
  assert.equal(verdict.complete, false);
  assert.equal(mock.calls.some((entry) => entry.method === "eth_getLogs"), false);
  assert.equal((await store.get(outcomeKey(identity))).status, "confirmed");
});

test("receipt checks run only for an explicit action or a visible poll", async () => {
  const { store } = await opened();
  const identity = identityFor();
  await seedPending(store, identity);
  let reads = 0;
  const call = async () => {
    reads += 1;
    return null;
  };
  const hidden = await verifyStoredOutcome(store, identity, {
    call,
    invocation: { kind: "visible-poll", visible: false },
  });
  assert.equal(hidden.reason, "invocation_required");
  assert.equal(hidden.verified, false);
  assert.equal(reads, 0);
  const scheduled = await verifyStoredOutcome(store, identity, {
    call,
    invocation: { kind: "scheduler" },
  });
  assert.equal(scheduled.reason, "invocation_required");
  assert.equal(reads, 0);
  let polls = 0;
  let visible = false;
  const skipped = await pollVisibleOutcome({
    isVisible: () => visible,
    maxPolls: 3,
    verify: async () => {
      polls += 1;
    },
  });
  assert.equal(skipped.ran, 0);
  assert.equal(skipped.stopped, "hidden");
  assert.equal(skipped.usedInterval, false);
  assert.equal(polls, 0);
  visible = true;
  const limited = await pollVisibleOutcome({
    isVisible: () => visible,
    maxPolls: 2,
    verify: async () => {
      polls += 1;
    },
    schedule: async () => {
      visible = false;
    },
  });
  assert.equal(limited.ran, 1);
  assert.equal(limited.stopped, "hidden");
  assert.equal(polls, 1);
});

test("AC7 IndexedDB throwing with no sessionStorage blocks before any wallet request", async () => {
  const store = await openOutcomeStore({ indexedDB: throwingIdb(), sessionStorage: null });
  assert.equal(store.blocked, "storage_unavailable");
  assert.equal(store.kind, null);
  let calls = 0;
  const result = await confirmModeAOutcome({
    ...guideFor().input,
    store,
    userClick: true,
    async requestWallet() {
      calls += 1;
      return HASH;
    },
  });
  assert.equal(result.blocked, "storage_unavailable");
  assert.equal(result.walletRequests, 0);
  assert.equal(calls, 0);
  const neither = await openOutcomeStore({ indexedDB: null, sessionStorage: null });
  assert.equal(neither.blocked, "storage_unavailable");
  const fallback = memorySession();
  const sessionStore = await openOutcomeStore({ indexedDB: throwingIdb(), sessionStorage: fallback });
  assert.equal(sessionStore.kind, "sessionStorage");
  let fallbackSends = 0;
  const sent = await confirmModeAOutcome({
    ...guideFor().input,
    store: sessionStore,
    userClick: true,
    async requestWallet() {
      fallbackSends += 1;
      assert.equal((await sessionStore.get(outcomeKey(identityFor()))).status, "requested");
      return HASH;
    },
  });
  assert.equal(fallbackSends, 1);
  assert.equal(sent.record.status, "pending");
  assert.equal(sent.autoRetries, 0);
  const reloaded = await openOutcomeStore({ indexedDB: throwingIdb(), sessionStorage: fallback });
  assert.equal((await reloaded.get(outcomeKey(identityFor()))).status, "pending");
  assert.equal((await reloaded.get(outcomeKey(identityFor()))).txHash, HASH);
});

test("an unknown outcome survives reload and still cannot be resent", async () => {
  const idb = memoryIdb();
  const store = await openOutcomeStore({ indexedDB: idb, sessionStorage: null });
  await confirmModeAOutcome({
    ...guideFor().input,
    store,
    userClick: true,
    requestWallet() {
      throw walletError(undefined, "timeout", "TimeoutError");
    },
  });
  const reloaded = await openOutcomeStore({ indexedDB: idb, sessionStorage: null });
  assert.equal((await reloaded.get(outcomeKey(identityFor()))).status, "unknown");
  let calls = 0;
  const again = await confirmModeAOutcome({
    ...guideFor().input,
    store: reloaded,
    userClick: true,
    requestWallet() {
      calls += 1;
      return HASH;
    },
  });
  assert.equal(again.blocked, "verify_outcome");
  assert.equal(calls, 0);
});

test("AC8 the same ref under another account does not clobber a pending outcome", async () => {
  const idb = memoryIdb();
  const store = await openOutcomeStore({ indexedDB: idb, sessionStorage: null });
  const idA = identityFor(ACCOUNT);
  const idB = identityFor(OTHER);
  assert.equal(idA.ref, idB.ref);
  assert.notEqual(outcomeKey(idA), outcomeKey(idB));
  await writePending(store, idA, {
    intent_id: "intent-a",
    requested_at: NOW,
    txHash: HASH,
    submission: { account: ACCOUNT },
  });
  const guide = guideFor(OTHER);
  const result = await confirmModeAOutcome({
    ...guide.input,
    store,
    outcomeIdentity: idB,
    userClick: true,
    async requestWallet() {
      return HASH_B;
    },
  });
  assert.equal(result.walletRequests, 1);
  assert.equal(result.record.status, "pending");
  assert.equal(result.record.txHash, HASH_B);
  const still = await store.get(outcomeKey(idA));
  assert.equal(still.status, "pending");
  assert.equal(still.txHash, HASH);
  assert.equal(still.intent_id, "intent-a");
  assert.equal(idb.data.size, 2);
});

test("logout keeps the local pending record and the read allowlist stays read-only", async () => {
  assert.equal(RETAIN_PENDING_ON_LOGOUT, true);
  const storeModule = await import("../src/outcome-store.mjs");
  assert.equal("clearOnLogout" in storeModule, false);
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  const logout = main.slice(main.indexOf("async function logout"), main.indexOf("async function cancelPrivateTask"));
  assert.equal(logout.includes("indexedDB"), false);
  assert.equal(logout.includes("sessionStorage"), false);
  assert.equal(logout.includes("outcome"), false);
  assert.equal(main.includes("outcome-store"), false);
  assert.equal(main.includes("outcome-confirm"), false);
  assert.equal(main.includes("outcome-verify"), false);
  assert.match(main, /獨立重讀不是送出授權。允許實際送出保持關閉。不會呼叫錢包。/);
  for (const name of ["outcome-store.mjs", "outcome-verify.mjs", "outcome-confirm.mjs", "mode-a.mjs", "trusted-lista.mjs"]) {
    const text = readFileSync(new URL("../src/" + name, import.meta.url), "utf8");
    assert.equal(text.includes("setInterval"), false, name);
    assert.equal(text.includes("eth_sendTransaction"), false, name);
    assert.equal(text.includes("COMPLETE"), false, name);
    assert.equal(text.includes("\"/rpc\""), false, name);
    assert.equal(text.includes("\"/api/"), false, name);
  }
  assert.doesNotThrow(() => assertReadCall("eth_getBlockByNumber", ["0x10", true]));
  assert.doesNotThrow(() => assertReadCall("eth_getBlockByNumber", ["0x10", false]));
  assert.throws(() => assertReadCall("eth_getBlockByNumber", ["0x10", "true"]));
  assert.doesNotThrow(() => assertReadCall("eth_getTransactionCount", [ACCOUNT, "latest"]));
  assert.doesNotThrow(() => assertReadCall("eth_getTransactionCount", [ACCOUNT, "pending"]));
  assert.throws(() => assertReadCall("eth_getTransactionCount", [ACCOUNT, "0x10"]));
  assert.throws(() => assertReadCall("eth_getTransactionCount", [ACCOUNT, "earliest"]));
  assert.doesNotThrow(() => assertReadCall("eth_getBalance", [ACCOUNT, "0x10"]));
  assert.doesNotThrow(() => assertReadCall("eth_getTransactionReceipt", [HASH]));
  assert.doesNotThrow(() => assertReadCall("eth_getTransactionByHash", [HASH]));
  assert.throws(() => assertReadCall("eth_getLogs", [{}]));
  assert.throws(() => assertReadCall("eth_sendTransaction", [{}]));
  let network = 0;
  const fetchImpl = async () => {
    network += 1;
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: "0x1" }), { status: 200 });
  };
  await assert.rejects(() => fetchTrustedRpc("eth_getLogs", [{}], { fetchImpl }));
  await assert.rejects(() => fetchTrustedRpc("eth_getTransactionCount", [ACCOUNT, "0x5"], { fetchImpl }));
  await assert.rejects(() => fetchTrustedRpc("eth_sendTransaction", [{}], { fetchImpl }));
  assert.equal(network, 0);
  const bare = await openOutcomeStore();
  assert.equal(bare.blocked, "storage_unavailable");
});

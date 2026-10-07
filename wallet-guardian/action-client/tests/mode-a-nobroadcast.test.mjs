import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { claimCalldata, encodePausedCall, encodeRequestStatusCall, encodeWithdrawalRequestsCall, TRUSTED_LISTA } from "../src/trusted-lista.mjs";
import { settleReread } from "../src/lista-reread.mjs";
import {
  applyWalletEvent,
  assertSingleStep,
  exactClaimRequest,
  guideListaModeA,
  OUTCOME_TEXT,
  POISON_NOTE,
  recordEconomicOutcome,
  recordReplacement,
  retryModeA,
  sendAuthorizedByReread,
} from "../src/mode-a.mjs";

const ACCOUNT = "0x" + "1".repeat(40);
const OTHER = "0x" + "3".repeat(40);
const IMPL = "0x" + "2".repeat(40);
const NOW = 1_700_000_000;
const PROXY_CODE = "0x6001";
const IMPL_CODE = "0x6002";
const BLOCK = "0x" + "ab".repeat(32);
const BLOCK_TWO = "0x" + "cd".repeat(32);

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
    local_ttl_unix: NOW + 3600,
    ...overrides,
  };
}

function script(mutate = {}) {
  const log = [];
  let round = 0;
  async function call(method, params) {
    log.push({ method, params: structuredClone(params) });
    if (method === "eth_chainId") round += 1;
    const second = round >= 2;
    const block = second && mutate.secondBlock ? mutate.secondBlock : BLOCK;
    if (method === "eth_chainId") return "0x38";
    if (method === "eth_blockNumber") return "0x64";
    if (method === "eth_getBlockByNumber") {
      return { number: "0x64", hash: block, timestamp: "0x" + (NOW - 10).toString(16) };
    }
    const tag = params[params.length - 1];
    if (!tag || tag.requireCanonical !== true || tag.blockHash !== block) {
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
      if (params[0].data === encodeWithdrawalRequestsCall(ACCOUNT)) {
        return second && mutate.idxDrift ? SHIFTED : ONE_REQUEST;
      }
      const idx = second && mutate.idxDrift ? 1 : 0;
      if (params[0].data === encodeRequestStatusCall(ACCOUNT, idx)) return STATUS_OK;
    }
    throw new Error("unexpected method " + method);
  }
  return { call, log };
}

function guideInput(overrides = {}) {
  const current = overrides.plan ?? plan();
  const chain = script(overrides.mutate);
  return {
    chain,
    input: {
      plan: current,
      task: { mode: "A", ref: "ref_" + "ab".repeat(16) },
      account: ACCOUNT,
      connectedAccount: ACCOUNT,
      walletChainId: 56,
      walletIdentityVerified: true,
      planDigest: current.digest,
      revision: current.revision,
      nowUnix: NOW,
      startedAt: NOW,
      call: chain.call,
      testApprovals: [approvedEntry()],
      testOnlyAllowWalletRequest: false,
      mainnetSend: true,
      provider: null,
      ...overrides.fields,
    },
  };
}

function recorder(result = "0x" + "11".repeat(32)) {
  const recorded = [];
  return {
    noBroadcast: true,
    broadcasts: false,
    networkBroadcasts: 0,
    recorded,
    async request(args) {
      recorded.push(structuredClone(args));
      if (args.method === "eth_sendTransaction" || args.method === "wallet_sendCalls") {
        return typeof result === "function" ? result(args) : result;
      }
      throw new Error("unexpected method");
    },
  };
}

test("production and a passing re-read do not authorize a wallet request", async () => {
  const { input, chain } = guideInput();
  const result = await guideListaModeA(input);
  assert.equal(result.first.reread, "pass");
  assert.equal(result.second.reread, "pass");
  assert.equal(result.reads, 18);
  assert.equal(result.allowSend, "closed");
  assert.equal(result.first.allowSend, "closed");
  assert.equal(result.walletRequested, false);
  assert.equal(result.broadcast, false);
  assert.equal(result.testProbe, null);
  assert.equal(result.reason, "reread_is_not_send_authorization");
  assert.equal(input.testOnlyAllowWalletRequest, false);
  assert.equal(sendAuthorizedByReread(result.first), false);
  assert.equal(chain.log.some((entry) => entry.method === "eth_sendTransaction"), false);
  assert.equal(chain.log.some((entry) => entry.method === "wallet_sendCalls"), false);
  assert.equal(result.prepared.method, "eth_sendTransaction");
  assert.deepEqual(Object.keys(result.prepared.params[0]), ["from", "to", "data", "value"]);
  assert.equal(result.prepared.params[0].data, claimCalldata(result.second.claim.idx));
  assert.equal(result.prepared.params[0].value, "0x0");
  assert.equal(result.prepared.params[0].to, TRUSTED_LISTA.proxy);
  assert.equal("recipient" in result.prepared.params[0], false);
  assert.equal(result.poison.forever, false);
  assert.equal(result.poison.addressBook, false);
  assert.equal(result.poison.once, true);
});

test("a string flag, a live mainnet flag, and a real provider still do not call the wallet", async () => {
  const live = recorder();
  delete live.noBroadcast;
  let calls = 0;
  live.request = async (args) => {
    calls += 1;
    live.recorded.push(args);
    return "0x" + "22".repeat(32);
  };
  const flagged = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: "true",
    mainnetSend: true,
    provider: live,
  });
  assert.equal(flagged.walletRequested, false);
  assert.equal(flagged.allowSend, "closed");
  assert.equal(calls, 0);
  const omitted = await guideListaModeA({
    ...guideInput().input,
    mainnetSend: true,
  });
  assert.equal(omitted.walletRequested, false);
  assert.equal(omitted.reason, "reread_is_not_send_authorization");
  const unmarked = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider: live,
  });
  assert.equal(unmarked.walletRequested, false);
  assert.equal(unmarked.reason, "no_broadcast_provider_required");
  assert.equal(calls, 0);
  assert.equal(unmarked.prepared.params[0].data, claimCalldata(0));
});

test("test-only no-broadcast recorder sees one claimWithdraw and does not broadcast", async () => {
  const provider = recorder();
  const { input } = guideInput({
    fields: { testOnlyAllowWalletRequest: true, provider, mainnetSend: false },
  });
  const result = await guideListaModeA(input);
  assert.equal(result.phase, "test_probe");
  assert.equal(result.allowSend, "closed");
  assert.equal(result.broadcast, false);
  assert.equal(result.testProbe, "recorded_not_broadcast");
  assert.equal(result.walletRequested, true);
  assert.equal(provider.recorded.length, 1);
  assert.equal(provider.broadcasts, false);
  assert.equal(provider.networkBroadcasts, 0);
  const call = provider.recorded[0];
  assert.equal(call.method, "eth_sendTransaction");
  assert.equal(assertSingleStep(call), "eth_sendTransaction");
  assert.equal(call.params.length, 1);
  assert.equal(call.params[0].from, ACCOUNT);
  assert.equal(call.params[0].to, TRUSTED_LISTA.proxy);
  assert.equal(call.params[0].data, claimCalldata(0));
  assert.equal(call.params[0].value, "0x0");
  assert.equal(call.params[0].data.length, 74);
  assert.equal(provider.recorded.some((entry) => entry.method === "wallet_sendCalls"), false);
  assert.equal(result.outcome.status, "pending");
  assert.equal(result.outcome.done, false);
  assert.equal(result.outcome.hashIsDone, false);
  assert.equal(result.outcome.autoRetry, false);
  assert.equal(result.outcome.txHash, "0x" + "11".repeat(32));
  assert.equal(result.first.allowSend, "closed");
  assert.equal(input.testOnlyAllowWalletRequest, true);
  assert.equal(result.reads, 18);
});

test("batch index pre-read and mode B or C never reach the wallet", async () => {
  const provider = recorder();
  const base = guideInput({ fields: { testOnlyAllowWalletRequest: true, provider } }).input;
  const prefetched = await guideListaModeA({ ...base, prefetchedIndex: 0 });
  assert.equal(prefetched.phase, "batch_preread_rejected");
  assert.equal(prefetched.reads, 0);
  assert.equal(provider.recorded.length, 0);
  const batch = await guideListaModeA({ ...guideInput().input, batchIndexes: [0, 1], testOnlyAllowWalletRequest: true, provider });
  assert.equal(batch.reads, 0);
  assert.equal(provider.recorded.length, 0);
  for (const mode of ["B", "C"]) {
    const blocked = await guideListaModeA({
      ...guideInput().input,
      mode,
      testOnlyAllowWalletRequest: true,
      provider,
    });
    assert.equal(blocked.phase, "batch_preread_rejected");
    assert.equal(blocked.walletRequested, false);
  }
  assert.equal(provider.recorded.length, 0);
  assert.throws(() => assertSingleStep({ method: "wallet_sendCalls", params: [{}] }), /does not batch/);
});

test("a moved index is a new revision and does not send", async () => {
  const provider = recorder();
  const { input } = guideInput({
    mutate: { idxDrift: true },
    fields: { testOnlyAllowWalletRequest: true, provider },
  });
  const result = await guideListaModeA(input);
  assert.equal(result.first.reread, "pass");
  assert.equal(result.first.claim.idx, 0);
  assert.equal(result.second.reread, "fail");
  assert.equal(result.second.reasons.includes("index_drift"), true);
  assert.equal(result.second.claim.idx, 1);
  assert.equal(result.phase, "revision_required");
  assert.equal(result.revision.revision, 2);
  assert.equal(result.revision.previousValid, false);
  assert.equal(result.revision.replacedSilently, false);
  assert.equal(result.revision.changes.includes("index_drift"), true);
  assert.equal(result.walletRequested, false);
  assert.equal(provider.recorded.length, 0);
  assert.equal(result.allowSend, "closed");
  const rebuilt = claimCalldata(result.second.claim.idx);
  assert.equal(rebuilt, claimCalldata(1));
  assert.notEqual(rebuilt, input.plan.calls[0].data);
});

test("a newer block with the same claim is not a new revision", async () => {
  const provider = recorder();
  const { input } = guideInput({
    mutate: { secondBlock: BLOCK_TWO },
    fields: { testOnlyAllowWalletRequest: true, provider },
  });
  const result = await guideListaModeA(input);
  assert.equal(result.second.reread, "pass");
  assert.notEqual(result.first.binding.blockHash, result.second.binding.blockHash);
  assert.equal(result.revision.revision, 1);
  assert.equal(result.revision.previousValid, true);
  assert.equal(provider.recorded.length, 1);
  assert.equal(provider.recorded[0].params[0].data, claimCalldata(0));
});

test("calldata is rebuilt from the observed idx, not a tampered plan blob", async () => {
  const { input } = guideInput();
  const result = await guideListaModeA(input);
  input.plan.calls[0].data = "0x" + "ff".repeat(36);
  input.plan.intent.idx = 9;
  const request = exactClaimRequest(result.second);
  assert.equal(request.params[0].data, claimCalldata(0));
  assert.equal(request.params[0].data, result.second.claim.calldata);
  const settled = settleReread(result.second, {
    simulation: input.plan.simulation,
    account: ACCOUNT,
    chainId: 56,
    planDigest: input.plan.digest,
    taskRef: input.task.ref,
    revision: 1,
    nowUnix: NOW + 1,
  });
  assert.equal(settled.reread, "pass");
  assert.equal(settled.allowSend, "closed");
  assert.equal(settled.claim.calldata, claimCalldata(0));
  const stale = settleReread(result.second, {
    simulation: input.plan.simulation,
    account: OTHER,
    chainId: 56,
    planDigest: input.plan.digest,
    taskRef: input.task.ref,
    revision: 1,
    nowUnix: NOW + 1,
  });
  assert.equal(stale.claim, null);
  assert.equal(stale.allowSend, "closed");
});

test("wallet outcomes never treat a hash as done and do not auto-retry", () => {
  const submitted = applyWalletEvent(null, { type: "submitted", txHash: "0x" + "ab".repeat(32) });
  assert.equal(submitted.status, "pending");
  assert.equal(submitted.done, false);
  assert.equal(submitted.hashIsDone, false);
  const economics = recordEconomicOutcome(submitted, {
    requestGone: true,
    claimLog: { bnbRaw: "1000" },
    nativeDelta: "900",
    gasUsed: "100",
    concurrentOtherTxs: false,
  });
  assert.equal(economics.status, "confirmed");
  assert.equal(economics.done, true);
  assert.equal(economics.hashIsDone, false);
  assert.equal(economics.economicVerified, true);
  const disappeared = recordEconomicOutcome(submitted, { requestGone: true, claimLog: null });
  assert.notEqual(disappeared.status, "confirmed");
  assert.equal(disappeared.done, false);
  assert.equal(disappeared.hashIsDone, false);
  assert.equal(disappeared.economicReason, "missing_claim_event");
  const ambiguous = applyWalletEvent(submitted, { type: "ambiguous" });
  assert.equal(ambiguous.status, "unknown");
  assert.equal(ambiguous.autoRetry, false);
  assert.equal(ambiguous.newTransaction, false);
  const again = retryModeA(ambiguous);
  assert.equal(again.blocked, true);
  assert.equal(again.newTransaction, false);
  const rejected = applyWalletEvent(null, { type: "rejected" });
  assert.equal(rejected.status, "failed");
  assert.equal(retryModeA(rejected).blocked, true);
  const replaced = recordReplacement(submitted, {
    previousHash: submitted.txHash,
    replacementHash: "0x" + "cd".repeat(32),
  });
  assert.equal(replaced.status, "replaced");
  assert.equal(replaced.done, false);
  assert.equal(replaced.hashIsDone, false);
  assert.equal(replaced.txHash, submitted.txHash);
  assert.equal(replaced.replacementHash, "0x" + "cd".repeat(32));
  const empty = applyWalletEvent(null, { type: "submitted", txHash: "" });
  assert.equal(empty.status, "unknown");
});

test("an ambiguous probe is not sent again", async () => {
  const provider = recorder(async () => {
    const error = new Error("timeout");
    error.name = "TimeoutError";
    throw error;
  });
  const first = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider,
  });
  assert.equal(first.outcome.status, "unknown");
  assert.equal(first.outcome.autoRetry, false);
  assert.equal(provider.recorded.length, 1);
  const second = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider,
    prior: first.outcome,
  });
  assert.equal(second.phase, "no_auto_retry");
  assert.equal(second.walletRequested, false);
  assert.equal(second.outcome.autoRetry, false);
  assert.equal(second.outcome.newTransaction, false);
  assert.equal(provider.recorded.length, 1);
  const denied = recorder(() => {
    const error = new Error("user rejected");
    error.code = 4001;
    throw error;
  });
  const rejected = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider: denied,
  });
  assert.equal(rejected.outcome.status, "failed");
  const stopped = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider: denied,
    prior: rejected.outcome,
  });
  assert.equal(stopped.walletRequested, false);
  assert.equal(denied.recorded.length, 1);
});

test("recipient confirmation is one claim, and the page keeps send closed", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  const reader = readFileSync(new URL("../src/lista-reread.mjs", import.meta.url), "utf8");
  const guide = readFileSync(new URL("../src/mode-a.mjs", import.meta.url), "utf8");
  assert.ok(html.includes(POISON_NOTE));
  assert.ok(html.includes(OUTCOME_TEXT.idle));
  assert.match(html, /不是永遠信任這個地址/);
  assert.equal(html.includes("通訊錄"), true);
  assert.doesNotMatch(main, /eth_sendTransaction|wallet_sendCalls|eth_sendRawTransaction/);
  assert.doesNotMatch(reader, /eth_sendTransaction|wallet_sendCalls/);
  assert.match(main, /testOnlyAllowWalletRequest:\s*false/);
  assert.match(main, /provider:\s*null/);
  assert.doesNotMatch(main, /testOnlyAllowWalletRequest:\s*true/);
  assert.match(main, /guideListaModeA/);
  assert.match(guide, /wallet_sendCalls/);
  assert.match(guide, /eth_sendTransaction/);
  assert.match(guide, /noBroadcast === true/);
  assert.equal(sendAuthorizedByReread({ reread: "pass", allowSend: "closed" }), false);
  assert.throws(() => sendAuthorizedByReread({ reread: "pass", allowSend: "open" }), /closed/);
  assert.throws(
    () => sendAuthorizedByReread({ reread: "pass", allowSend: "closed", testOnlyAllowWalletRequest: true }),
    /send authorization/,
  );
});

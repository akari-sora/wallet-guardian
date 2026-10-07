import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { CLAIM_WITHDRAWAL_EVENT, CLAIM_WITHDRAWAL_TOPIC, claimCalldata, encodePausedCall, encodeRequestStatusCall, encodeWithdrawalRequestsCall, TRUSTED_LISTA } from "../src/trusted-lista.mjs";
import { settleReread } from "../src/lista-reread.mjs";
import {
  applyWalletEvent,
  assertSingleStep,
  createModeAFlow,
  exactClaimRequest,
  guideListaModeA,
  OUTCOME_TEXT,
  outcomeAfterReread,
  POISON_NOTE,
  recordEconomicOutcome,
  recordReplacement,
  retryModeA,
  sendAuthorizedByReread,
  verifyRecordedClaim,
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

function addressTopic(account) {
  return "0x" + account.slice(2).toLowerCase().padStart(64, "0");
}

function topicOf(signature) {
  const bytes = keccak_256(new TextEncoder().encode(signature));
  return "0x" + [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function boundSubmission(txHash = "0x" + "ab".repeat(32)) {
  return {
    txHash,
    account: ACCOUNT,
    idx: 0,
    uuid: "42",
    amountOut: "1000",
    calldata: claimCalldata(0),
    to: TRUSTED_LISTA.proxy,
    taskRef: "ref_" + "ab".repeat(16),
    revision: 1,
    chainId: 56,
  };
}

function boundEvidence(submission, mutate = {}) {
  const gasUsed = mutate.gasUsed ?? 21n;
  const price = mutate.price ?? 2n;
  const amount = BigInt(submission.amountOut);
  const fee = gasUsed * price;
  const before = 5_000n;
  const delta = mutate.nativeDelta ?? (amount - fee);
  const blockHash = mutate.blockHash ?? ("0x" + "11".repeat(32));
  const blockNumber = mutate.blockNumber ?? 100;
  const chainId = mutate.chainId ?? submission.chainId;
  const log = {
    address: mutate.logAddress ?? TRUSTED_LISTA.proxy,
    transactionHash: mutate.logHash ?? submission.txHash,
    removed: mutate.removed === true,
    blockHash: mutate.omitLogBlock ? undefined : (mutate.logBlockHash ?? blockHash),
    blockNumber: mutate.omitLogBlock ? undefined : (mutate.logBlockNumber ?? blockNumber),
    topics: [
      CLAIM_WITHDRAWAL_TOPIC,
      addressTopic(mutate.logAccount ?? submission.account),
    ],
    data: "0x" + word(mutate.logIdx ?? submission.idx) + word(mutate.logAmount ?? amount),
  };
  const evidence = {
    receipt: {
      transactionHash: mutate.receiptHash ?? submission.txHash,
      status: mutate.status ?? "0x1",
      from: mutate.from ?? submission.account,
      to: mutate.to ?? TRUSTED_LISTA.proxy,
      chainId: mutate.omitReceiptBlock ? undefined : (mutate.receiptChainId ?? chainId),
      blockHash: mutate.omitReceiptBlock ? undefined : (mutate.receiptBlockHash ?? blockHash),
      blockNumber: mutate.omitReceiptBlock ? undefined : blockNumber,
      gasUsed: "0x" + gasUsed.toString(16),
      effectiveGasPrice: mutate.omitPrice ? undefined : "0x" + price.toString(16),
      logs: mutate.logs ?? [log],
    },
    balanceWindow: mutate.omitWindow ? undefined : {
      account: mutate.windowAccount ?? submission.account,
      txHash: submission.txHash,
      chainId: mutate.windowChainId ?? chainId,
      blockHash: mutate.omitWindowBlock ? undefined : (mutate.windowBlockHash ?? blockHash),
      blockNumber: mutate.omitWindowBlock ? undefined : (mutate.windowBlockNumber ?? blockNumber),
      beforeWei: before.toString(),
      afterWei: (before + delta).toString(),
      otherTxHashes: mutate.otherTxHashes ?? [],
    },
    request: {
      gone: mutate.gone ?? true,
      account: mutate.requestAccount ?? submission.account,
      uuid: mutate.uuid ?? submission.uuid,
      idx: mutate.requestIdx ?? submission.idx,
      chainId: mutate.requestChainId ?? chainId,
      blockHash: mutate.omitRequestBlock ? undefined : (mutate.requestBlockHash ?? blockHash),
      blockNumber: mutate.omitRequestBlock ? undefined : (mutate.requestBlockNumber ?? blockNumber),
    },
  };
  if (mutate.contextChainId != null) {
    evidence.context = {
      chainId: mutate.contextChainId,
      blockHash: mutate.contextBlockHash ?? blockHash,
      blockNumber: mutate.contextBlockNumber ?? blockNumber,
    };
  }
  if (mutate.gasFeeWei != null) evidence.gasFeeWei = mutate.gasFeeWei;
  return evidence;
}

test("wallet outcomes never treat a hash as done and do not auto-retry", () => {
  const submitted = applyWalletEvent(null, { type: "submitted", txHash: "0x" + "ab".repeat(32) });
  assert.equal(submitted.status, "pending");
  assert.equal(submitted.done, false);
  assert.equal(submitted.hashIsDone, false);
  const loose = recordEconomicOutcome(submitted, {
    requestGone: true,
    claimLog: { bnbRaw: "1000" },
    nativeDelta: "900",
    gasUsed: "100",
    concurrentOtherTxs: false,
  });
  assert.equal(loose.status, "pending");
  assert.equal(loose.done, false);
  assert.equal(loose.hashIsDone, false);
  assert.equal(loose.economicVerified, false);
  assert.equal(loose.txHash, submitted.txHash);
  const noHash = recordEconomicOutcome(null, {
    requestGone: true,
    claimLog: { bnbRaw: "1000" },
    nativeDelta: "900",
    gasUsed: "100",
    receiptStatus: "0x1",
  });
  assert.equal(noHash.status, "pending");
  assert.equal(noHash.done, false);
  assert.equal(noHash.economicVerified, false);
  assert.equal(noHash.txHash, null);
  assert.equal(noHash.economicReason, "missing_tx");
  const disappeared = recordEconomicOutcome(submitted, { requestGone: true, claimLog: null });
  assert.notEqual(disappeared.status, "confirmed");
  assert.equal(disappeared.done, false);
  assert.equal(disappeared.hashIsDone, false);
  assert.equal(disappeared.economicReason, "missing_submission");
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
  const flow = createModeAFlow();
  const first = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider,
    flow,
  });
  assert.equal(first.outcome.status, "unknown");
  assert.equal(first.outcome.autoRetry, false);
  assert.equal(flow.outcome.status, "unknown");
  assert.equal(provider.recorded.length, 1);
  const remembered = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider,
    flow,
  });
  assert.equal(remembered.phase, "verify_outcome");
  assert.equal(remembered.walletRequested, false);
  assert.equal(provider.recorded.length, 1);
  assert.equal(outcomeAfterReread(flow).status, "unknown");
  const second = await guideListaModeA({
    ...guideInput().input,
    testOnlyAllowWalletRequest: true,
    provider,
    prior: first.outcome,
  });
  assert.equal(second.phase, "verify_outcome");
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
  assert.equal(stopped.phase, "no_auto_retry");
  assert.equal(stopped.walletRequested, false);
  assert.equal(denied.recorded.length, 1);
});

test("a confirmed outcome binds this task receipt and does not treat gas units as wei", () => {
  assert.equal(CLAIM_WITHDRAWAL_TOPIC, topicOf(CLAIM_WITHDRAWAL_EVENT));
  assert.equal(CLAIM_WITHDRAWAL_TOPIC.length, 66);
  const submission = boundSubmission();
  const submitted = applyWalletEvent(null, { type: "submitted", txHash: submission.txHash });
  submitted.submission = submission;
  const confirmed = recordEconomicOutcome(submitted, boundEvidence(submission));
  assert.equal(confirmed.status, "confirmed");
  assert.equal(confirmed.done, true);
  assert.equal(confirmed.economicVerified, true);
  assert.equal(confirmed.hashIsDone, false);
  assert.equal(confirmed.txHash, submission.txHash);
  assert.equal(confirmed.autoRetry, false);
  const gasUnitsAsWei = recordEconomicOutcome(submitted, boundEvidence(submission, { nativeDelta: 1000n - 21n }));
  assert.equal(gasUnitsAsWei.status, "pending");
  assert.equal(gasUnitsAsWei.done, false);
  assert.equal(gasUnitsAsWei.economicVerified, false);
  assert.equal(gasUnitsAsWei.economicReason, "effect_mismatch");
  const namedFee = recordEconomicOutcome(submitted, boundEvidence(submission, { gasFeeWei: "42" }));
  assert.equal(namedFee.status, "confirmed");
  assert.equal(namedFee.hashIsDone, false);
  const wrongFee = recordEconomicOutcome(submitted, boundEvidence(submission, { gasFeeWei: "21" }));
  assert.equal(wrongFee.economicVerified, false);
  assert.equal(wrongFee.economicReason, "gas_fee_unknown");
  const noPrice = recordEconomicOutcome(submitted, boundEvidence(submission, { omitPrice: true }));
  assert.equal(noPrice.economicReason, "gas_fee_unknown");
  const pendingReceipt = recordEconomicOutcome(submitted, boundEvidence(submission, { status: "pending" }));
  assert.equal(pendingReceipt.status, "pending");
  assert.equal(pendingReceipt.done, false);
  assert.equal(pendingReceipt.economicReason, "receipt_pending");
  const reverted = recordEconomicOutcome(submitted, boundEvidence(submission, { status: "0x0" }));
  assert.equal(reverted.status, "failed");
  assert.equal(reverted.done, false);
  assert.equal(reverted.economicVerified, false);
  assert.equal(reverted.economicReason, "receipt_failed");
  const otherTxs = recordEconomicOutcome(submitted, boundEvidence(submission, { otherTxHashes: ["0x" + "22".repeat(32)] }));
  assert.equal(otherTxs.economicReason, "native_delta_ambiguous");
  const hiddenWindow = recordEconomicOutcome(submitted, boundEvidence(submission, { omitWindow: true }));
  assert.equal(hiddenWindow.economicReason, "native_delta_ambiguous");
  const otherAccount = recordEconomicOutcome(submitted, boundEvidence(submission, { logAccount: OTHER }));
  assert.equal(otherAccount.economicReason, "provenance_mismatch");
  const otherHash = recordEconomicOutcome(submitted, boundEvidence(submission, { receiptHash: "0x" + "cd".repeat(32) }));
  assert.equal(otherHash.economicReason, "provenance_mismatch");
  const otherRequest = recordEconomicOutcome(submitted, boundEvidence(submission, { uuid: "99" }));
  assert.equal(otherRequest.economicReason, "provenance_mismatch");
  const stillThere = recordEconomicOutcome(submitted, boundEvidence(submission, { gone: false }));
  assert.equal(stillThere.economicReason, "request_not_gone");
  const otherIdx = recordEconomicOutcome(submitted, boundEvidence(submission, { logIdx: 1 }));
  assert.equal(otherIdx.economicReason, "provenance_mismatch");
  const callerStory = {
    ...boundEvidence(submission),
    requestGone: true,
    claimLog: { bnbRaw: "1" },
    nativeDelta: "1",
    gasUsed: "1",
  };
  const stored = recordEconomicOutcome(submitted, callerStory);
  assert.equal(stored.status, "confirmed");
  const unbound = recordEconomicOutcome(
    applyWalletEvent(null, { type: "submitted", txHash: submission.txHash }),
    callerStory,
  );
  assert.equal(unbound.economicVerified, false);
  assert.equal(unbound.economicReason, "missing_submission");
  const removed = recordEconomicOutcome(submitted, boundEvidence(submission, { removed: true }));
  assert.equal(removed.status, "pending");
  assert.equal(removed.done, false);
  assert.equal(removed.economicVerified, false);
  assert.equal(removed.economicReason, "provenance_mismatch");
  const otherBlock = recordEconomicOutcome(submitted, boundEvidence(submission, { logBlockHash: "0x" + "22".repeat(32) }));
  assert.equal(otherBlock.economicVerified, false);
  assert.equal(otherBlock.economicReason, "provenance_mismatch");
  const otherChain = recordEconomicOutcome(submitted, boundEvidence(submission, { contextChainId: 1 }));
  assert.equal(otherChain.economicVerified, false);
  assert.equal(otherChain.economicReason, "provenance_mismatch");
  const requestChain = recordEconomicOutcome(submitted, boundEvidence(submission, { requestChainId: 1 }));
  assert.equal(requestChain.economicVerified, false);
  assert.equal(requestChain.economicReason, "provenance_mismatch");
  const missingBlock = recordEconomicOutcome(submitted, boundEvidence(submission, { omitReceiptBlock: true }));
  assert.equal(missingBlock.economicVerified, false);
  assert.equal(missingBlock.economicReason, "block_unbound");
  const missingLogBlock = recordEconomicOutcome(submitted, boundEvidence(submission, { omitLogBlock: true }));
  assert.equal(missingLogBlock.economicReason, "block_unbound");
  const earlierState = recordEconomicOutcome(submitted, boundEvidence(submission, {
    requestBlockNumber: 99,
    requestBlockHash: "0x" + "33".repeat(32),
  }));
  assert.equal(earlierState.economicVerified, false);
  assert.equal(earlierState.economicReason, "provenance_mismatch");
  const laterState = recordEconomicOutcome(submitted, boundEvidence(submission, {
    windowBlockNumber: 101,
    windowBlockHash: "0x" + "44".repeat(32),
    requestBlockNumber: 101,
    requestBlockHash: "0x" + "44".repeat(32),
  }));
  assert.equal(laterState.status, "confirmed");
  assert.equal(laterState.hashIsDone, false);
});

test("the same task records one request and keeps an ambiguous outcome for verification", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  const { input, chain } = guideInput({
    fields: { testOnlyAllowWalletRequest: true, provider, flow },
  });
  const first = guideListaModeA(input);
  const second = guideListaModeA(input);
  const [left, right] = await Promise.all([first, second]);
  assert.deepEqual([left.phase, right.phase].sort(), ["in_flight", "test_probe"]);
  assert.equal(provider.recorded.length, 1);
  assert.equal(chain.log.length, 18);
  assert.equal(flow.inFlight, false);
  assert.equal(flow.outcome.status, "pending");
  assert.equal(flow.outcome.submission.uuid, "42");
  assert.equal(flow.outcome.submission.amountOut, "6");
  const again = await guideListaModeA({
    ...guideInput({ fields: { testOnlyAllowWalletRequest: true, provider, flow } }).input,
  });
  assert.equal(again.phase, "verify_outcome");
  assert.equal(again.walletRequested, false);
  assert.equal(provider.recorded.length, 1);
  const loose = verifyRecordedClaim(flow, {
    requestGone: true,
    claimLog: { bnbRaw: "6" },
    nativeDelta: "4",
    gasUsed: "2",
    concurrentOtherTxs: false,
  });
  assert.equal(loose.economicVerified, false);
  assert.equal(loose.done, false);
  assert.equal(provider.recorded.length, 1);
  const watched = recorder();
  const amount = 6n;
  const gasUsed = 2n;
  const price = 3n;
  const fee = gasUsed * price;
  const before = 1_000n;
  const verified = verifyRecordedClaim(flow, {
    provider: watched,
    receipt: {
      transactionHash: flow.outcome.txHash,
      status: "0x1",
      from: ACCOUNT,
      to: TRUSTED_LISTA.proxy,
      gasUsed: "0x" + gasUsed.toString(16),
      effectiveGasPrice: "0x" + price.toString(16),
      chainId: 56,
      blockHash: "0x" + "11".repeat(32),
      blockNumber: 100,
      logs: [{
        address: TRUSTED_LISTA.proxy,
        transactionHash: flow.outcome.txHash,
        blockHash: "0x" + "11".repeat(32),
        blockNumber: 100,
        topics: [CLAIM_WITHDRAWAL_TOPIC, addressTopic(ACCOUNT)],
        data: "0x" + word(0) + word(amount),
      }],
    },
    balanceWindow: {
      account: ACCOUNT,
      txHash: flow.outcome.txHash,
      chainId: 56,
      blockHash: "0x" + "11".repeat(32),
      blockNumber: 100,
      beforeWei: before.toString(),
      afterWei: (before + amount - fee).toString(),
      otherTxHashes: [],
    },
    request: {
      gone: true,
      account: ACCOUNT,
      uuid: "42",
      idx: 0,
      chainId: 56,
      blockHash: "0x" + "11".repeat(32),
      blockNumber: 100,
    },
    gasFeeWei: fee.toString(),
  });
  assert.equal(watched.recorded.length, 0);
  assert.equal(verified.status, "confirmed");
  assert.equal(verified.done, true);
  assert.equal(verified.hashIsDone, false);
  assert.equal(flow.outcome.status, "confirmed");
  const retry = await guideListaModeA({
    ...guideInput({ fields: { testOnlyAllowWalletRequest: true, provider, flow } }).input,
  });
  assert.equal(retry.phase, "no_auto_retry");
  assert.equal(provider.recorded.length, 1);
});

test("a changed account or chain is checked again and is not handed to the recorder", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  let account = ACCOUNT;
  let chainId = 56;
  const { input, chain } = guideInput({
    fields: {
      testOnlyAllowWalletRequest: true,
      provider,
      flow,
      identity: () => ({
        connectedAccount: account,
        walletChainId: chainId,
        walletIdentityVerified: account === ACCOUNT && chainId === 56,
        revision: 1,
        planDigest: "ab".repeat(32),
        taskRef: "ref_" + "ab".repeat(16),
      }),
    },
  });
  const baseCall = input.call;
  input.call = async (method, params) => {
    const result = await baseCall(method, params);
    if (chain.log.length >= 9) {
      account = OTHER;
      chainId = 1;
    }
    return result;
  };
  const result = await guideListaModeA(input);
  assert.equal(result.phase, "identity_changed");
  assert.equal(result.walletRequested, false);
  assert.equal(result.second, null);
  assert.equal(provider.recorded.length, 0);
  assert.equal(flow.outcome.status, "idle");
});

test("the clock is read again before the recorder, including once the plan TTL has passed", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  let now = NOW;
  const { input, chain } = guideInput({
    fields: {
      testOnlyAllowWalletRequest: true,
      provider,
      flow,
      clock: () => now,
    },
  });
  const baseCall = input.call;
  input.call = async (method, params) => {
    const result = await baseCall(method, params);
    if (chain.log.length >= 18) now = input.plan.local_ttl_unix;
    return result;
  };
  const expiredDuringSecond = await guideListaModeA(input);
  assert.equal(expiredDuringSecond.walletRequested, false);
  assert.equal(expiredDuringSecond.second.reasons.includes("plan_expired"), true);
  assert.equal(provider.recorded.length, 0);
  assert.notEqual(expiredDuringSecond.phase, "test_probe");

  const later = recorder();
  let samples = 0;
  const held = createModeAFlow();
  const preSend = await guideListaModeA({
    ...guideInput({
      fields: {
        testOnlyAllowWalletRequest: true,
        provider: later,
        flow: held,
        clock: () => {
          samples += 1;
          return samples >= 5 ? NOW + 10_000 : NOW;
        },
      },
    }).input,
  });
  assert.equal(samples, 5);
  assert.equal(preSend.phase, "expired_before_wallet");
  assert.equal(preSend.second.reread, "pass");
  assert.equal(preSend.walletRequested, false);
  assert.equal(later.recorded.length, 0);
  assert.equal(outcomeAfterReread(held).status, "idle");
});

test("identity sampled after both reads still blocks the recorder", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  let samples = 0;
  const result = await guideListaModeA({
    ...guideInput({
      fields: {
        testOnlyAllowWalletRequest: true,
        provider,
        flow,
        identity: () => {
          samples += 1;
          const drifted = samples >= 3;
          return {
            connectedAccount: drifted ? OTHER : ACCOUNT,
            walletChainId: drifted ? 97 : 56,
            walletIdentityVerified: !drifted,
            revision: drifted ? 2 : 1,
            planDigest: "ab".repeat(32),
            taskRef: "ref_" + "ab".repeat(16),
          };
        },
      },
    }).input,
  });
  assert.equal(samples, 3);
  assert.equal(result.phase, "revision_changed");
  assert.equal(result.second.reread, "pass");
  assert.equal(result.walletRequested, false);
  assert.equal(provider.recorded.length, 0);
});

test("approval expiry before handoff is settled against the live clock and is not recorded", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  const entry = approvedEntry();
  entry.validUntilUnix = NOW + 1;
  let ticks = 0;
  const result = await guideListaModeA({
    ...guideInput({
      fields: {
        testOnlyAllowWalletRequest: true,
        provider,
        flow,
        testApprovals: [entry],
        clock: () => {
          ticks += 1;
          return ticks >= 5 ? NOW + 2 : NOW;
        },
      },
    }).input,
  });
  assert.equal(ticks, 5);
  assert.equal(result.second.reread, "pass");
  assert.equal(result.second.binding.freshUntil, NOW + 1);
  assert.equal(result.phase, "expired_before_wallet");
  assert.equal(result.settled.reasons.includes("approval_retired"), true);
  assert.equal(result.walletRequested, false);
  assert.equal(provider.recorded.length, 0);
  assert.equal(flow.outcome.status, "idle");
});

test("clearing the task before handoff does not revive the closed-over plan", async () => {
  const provider = recorder();
  const flow = createModeAFlow();
  const opened = plan();
  const openedTask = { mode: "A", ref: "ref_" + "ab".repeat(16) };
  let currentPlan = opened;
  let currentTask = openedTask;
  let generation = 1;
  const viewed = 1;
  const { input, chain } = guideInput({
    plan: opened,
    fields: {
      testOnlyAllowWalletRequest: true,
      provider,
      flow,
      task: openedTask,
      identity: () => {
        if (viewed !== generation || !currentPlan || !currentTask) return null;
        return {
          connectedAccount: ACCOUNT,
          walletChainId: 56,
          walletIdentityVerified: true,
          revision: currentPlan.revision,
          planDigest: currentPlan.digest,
          taskRef: currentTask.ref,
        };
      },
      isCurrent: () => viewed === generation && currentPlan != null && currentTask != null,
    },
  });
  const baseCall = input.call;
  input.call = async (method, params) => {
    const result = await baseCall(method, params);
    if (chain.log.length >= 18) {
      currentPlan = null;
      currentTask = null;
      generation += 1;
    }
    return result;
  };
  const result = await guideListaModeA(input);
  assert.equal(result.phase, "not_current");
  assert.equal(result.second.reread, "pass");
  assert.equal(result.walletRequested, false);
  assert.equal(provider.recorded.length, 0);
  assert.equal(flow.outcome.status, "idle");
  flow.outcome = {
    ...flow.outcome,
    status: "unknown",
    txHash: "0x" + "11".repeat(32),
  };
  const kept = await guideListaModeA({
    ...guideInput({
      fields: { testOnlyAllowWalletRequest: true, provider, flow, isCurrent: () => false },
    }).input,
  });
  assert.equal(kept.phase, "verify_outcome");
  assert.equal(kept.outcome.status, "unknown");
  assert.equal(flow.outcome.status, "unknown");
  assert.equal(provider.recorded.length, 0);
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
  assert.match(main, /createModeAFlow/);
  assert.match(main, /flow:\s*modeAFlow/);
  assert.match(main, /identity:\s*\(\)/);
  assert.match(main, /clock:\s*\(\)/);
  assert.match(main, /outcomeAfterReread\(\s*modeAFlow\s*\)/);
  assert.match(main, /isCurrent:\s*\(\)\s*=>/);
  assert.doesNotMatch(main, /lastPlan\?\.revision \?\? plan\.revision/);
  assert.doesNotMatch(main, /lastPlan\?\.digest \?\? plan\.digest/);
  assert.doesNotMatch(main, /lastTask\?\.ref \?\? task\?\.ref/);
  assert.doesNotMatch(main, /paintWalletOutcome\(\s*["']idle["']\s*\)/);
  assert.match(guide, /settleReread/);
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

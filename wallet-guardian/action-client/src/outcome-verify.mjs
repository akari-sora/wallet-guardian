/** Receipt check for one stored outcome. Reads only. No background scheduler. */

import { verifyClaimEconomics } from "./mode-a.mjs";
import {
  outcomeKey,
  withOutcomeLock,
  writeConfirmed,
  writeFailed,
  writeReplaced,
  writeUnknown,
} from "./outcome-store.mjs";
import {
  assertReadCall,
  CLAIM_WITHDRAWAL_TOPIC,
  decodeWithdrawalRequests,
  encodeWithdrawalRequestsCall,
  hexQuantity,
  sameAddress,
  TRUSTED_LISTA,
} from "./trusted-lista.mjs";

export const MAX_FULL_BLOCK_TRANSACTIONS = 32;
export const LIST_DISAPPEARANCE_IS_ENOUGH = false;
export const VERIFY_ACTION = "核對結果";

function closed(fields) {
  return {
    verified: false,
    confirmed: false,
    complete: false,
    done: false,
    hashIsDone: false,
    failed: fields.failed === true,
    listDisappearanceIsEnough: LIST_DISAPPEARANCE_IS_ENOUGH,
    status: fields.status,
    reason: fields.reason,
  };
}

export function invocationAllowed(invocation) {
  if (!invocation || typeof invocation !== "object") return false;
  if (invocation.kind === "user" && invocation.action === VERIFY_ACTION) return true;
  if (invocation.kind === "visible-poll" && invocation.visible === true) return true;
  return false;
}

function keepStatus(record) {
  if (!record || typeof record.status !== "string") return "pending";
  if (record.status === "unknown" || record.status === "replaced" || record.status === "failed" || record.status === "rejected") {
    return record.status;
  }
  return "pending";
}

function hexQty(value) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return "0x" + value.toString(16);
  if (typeof value === "bigint" && value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER)) return "0x" + value.toString(16);
  if (typeof value === "string" && /^0x[0-9a-fA-F]+$/.test(value)) return value;
  if (typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value)) return "0x" + BigInt(value).toString(16);
  return null;
}

function asNumber(value) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  const hex = hexQty(value);
  if (!hex) return null;
  try {
    return hexQuantity(hex);
  } catch {
    return null;
  }
}

function statusCode(value) {
  if (value === 0 || value === 0n || value === 1 || value === 1n) return Number(value);
  if (typeof value === "string" && /^0x[0-9a-fA-F]+$/.test(value)) {
    try {
      const parsed = BigInt(value);
      if (parsed === 0n || parsed === 1n) return Number(parsed);
    } catch {
      return null;
    }
  }
  return null;
}

function bindCall(call) {
  if (typeof call !== "function") throw new Error("missing read call");
  return async (method, params) => {
    assertReadCall(method, params);
    return call(method, params);
  };
}

function hasClaimLog(receipt) {
  if (!receipt || !Array.isArray(receipt.logs)) return false;
  return receipt.logs.some((log) => {
    return log && Array.isArray(log.topics) && String(log.topics[0]).toLowerCase() === CLAIM_WITHDRAWAL_TOPIC;
  });
}

async function classifyMissingHash(record, call) {
  const submission = record && record.submission;
  const account = submission && submission.account;
  const nonce = submission && submission.nonce;
  if (typeof account !== "string" || nonce == null || !Number.isSafeInteger(Number(nonce))) return "unknown";
  const countHex = await call("eth_getTransactionCount", [account, "latest"]);
  const count = asNumber(countHex);
  if (count == null) return "unknown";
  if (count > Number(nonce)) return "replaced";
  return "unknown";
}

async function readRequestGone(call, submission, blockHash, blockNumber) {
  const raw = await call("eth_call", [
    { to: TRUSTED_LISTA.proxy, data: encodeWithdrawalRequestsCall(submission.account) },
    { blockHash, requireCanonical: true },
  ]);
  const rows = decodeWithdrawalRequests(raw);
  const present = rows.some((row) => String(row.uuid) === String(submission.uuid));
  return {
    gone: present !== true,
    account: submission.account,
    uuid: String(submission.uuid),
    idx: submission.idx,
    chainId: TRUSTED_LISTA.chainId,
    blockHash,
    blockNumber,
  };
}

/**
 * Check one stored hash. A missing hash, a reverted receipt, a missing claim log,
 * the wrong chain, or a block that does not match stays unverified or failed.
 * Disappearance from the request list is never enough.
 */
export async function verifyOutcome(record, context = {}) {
  const status = keepStatus(record);
  if (!invocationAllowed(context.invocation)) {
    return closed({ status, reason: "invocation_required" });
  }
  if (!record || typeof record.txHash !== "string") {
    return closed({ status: "unknown", reason: "missing_tx" });
  }
  let call;
  try {
    call = bindCall(context.call);
  } catch {
    return closed({ status, reason: "read_refused" });
  }
  try {
    const tx = await call("eth_getTransactionByHash", [record.txHash]);
    const receipt = await call("eth_getTransactionReceipt", [record.txHash]);
    if (tx == null && receipt == null) {
      const missing = await classifyMissingHash(record, call);
      return closed({ status: missing, reason: "missing_tx" });
    }
    if (receipt == null) return closed({ status: "pending", reason: "missing_receipt" });
    const reportedChain = asNumber(await call("eth_chainId", []));
    const receiptChain = receipt.chainId == null ? reportedChain : asNumber(receipt.chainId);
    if (reportedChain !== TRUSTED_LISTA.chainId || receiptChain !== TRUSTED_LISTA.chainId) {
      return closed({ status, reason: "wrong_chain" });
    }
    const blockHex = hexQty(receipt.blockNumber);
    const receiptHash = typeof receipt.blockHash === "string" ? receipt.blockHash.toLowerCase() : "";
    if (!blockHex || !/^0x[0-9a-fA-F]{64}$/.test(receiptHash)) {
      return closed({ status, reason: "block_unbound" });
    }
    const block = await call("eth_getBlockByNumber", [blockHex, true]);
    const blockHash = block && typeof block.hash === "string" ? block.hash.toLowerCase() : "";
    const blockNumber = asNumber(blockHex);
    const reportedNumber = block && block.number != null ? asNumber(block.number) : blockNumber;
    if (!block || blockHash !== receiptHash || reportedNumber !== blockNumber || blockNumber == null) {
      return closed({ status, reason: "block_mismatch" });
    }
    if (statusCode(receipt.status) === 0) {
      return closed({ status: "failed", failed: true, reason: "receipt_failed" });
    }
    if (statusCode(receipt.status) !== 1) return closed({ status, reason: "receipt_pending" });
    if (!hasClaimLog(receipt)) return closed({ status, reason: "missing_claim_event" });
    const transactions = block.transactions;
    if (!Array.isArray(transactions) || transactions.length > MAX_FULL_BLOCK_TRANSACTIONS) {
      return closed({ status, reason: "native_delta_ambiguous" });
    }
    const submission = record.submission;
    if (!submission || typeof submission !== "object" || !sameAddress(submission.account, submission.account)) {
      return closed({ status, reason: "missing_submission" });
    }
    if (blockNumber === 0) return closed({ status, reason: "native_delta_ambiguous" });
    const others = [];
    for (const txBody of transactions) {
      if (!txBody || typeof txBody !== "object" || typeof txBody.hash !== "string") {
        return closed({ status, reason: "native_delta_ambiguous" });
      }
      if (txBody.hash.toLowerCase() === record.txHash.toLowerCase()) continue;
      if (typeof txBody.from === "string" && sameAddress(txBody.from, submission.account)) {
        others.push(txBody.hash.toLowerCase());
      }
    }
    if (others.length > 0) return closed({ status, reason: "native_delta_ambiguous" });
    const beforeHex = "0x" + (blockNumber - 1).toString(16);
    const afterHex = "0x" + blockNumber.toString(16);
    const beforeWei = await call("eth_getBalance", [submission.account, beforeHex]);
    const afterWei = await call("eth_getBalance", [submission.account, afterHex]);
    const request = await readRequestGone(call, submission, receiptHash, blockNumber);
    const evidence = {
      receipt: {
        ...receipt,
        blockHash: receiptHash,
        blockNumber,
        chainId: TRUSTED_LISTA.chainId,
      },
      balanceWindow: {
        account: submission.account,
        txHash: record.txHash,
        chainId: TRUSTED_LISTA.chainId,
        blockHash: receiptHash,
        blockNumber,
        beforeWei,
        afterWei,
        otherTxHashes: others,
      },
      request,
    };
    const verdict = verifyClaimEconomics({
      status: "pending",
      txHash: record.txHash,
      submission: { ...submission, txHash: record.txHash },
    }, evidence);
    if (verdict.verified === true) {
      return {
        verified: true,
        confirmed: true,
        complete: false,
        done: true,
        hashIsDone: false,
        failed: false,
        listDisappearanceIsEnough: false,
        status: "confirmed",
        reason: null,
      };
    }
    return closed({
      status: verdict.failed === true ? "failed" : status,
      failed: verdict.failed === true,
      reason: verdict.reason || "unverified",
    });
  } catch {
    return closed({ status, reason: "read_failed" });
  }
}

/** User action or a visible poll. Hidden and scheduled calls do not read and do not confirm. */
export async function verifyStoredOutcome(store, identity, context = {}) {
  const key = outcomeKey(identity);
  return withOutcomeLock(key, async () => {
    const verdict = await verifyOutcome(await readable(store, key), context);
    if (!store || store.blocked || !invocationAllowed(context.invocation)) return verdict;
    if (verdict.status === "confirmed") await writeConfirmed(store, identity, {});
    else if (verdict.status === "failed") await writeFailed(store, identity, {});
    else if (verdict.status === "replaced") await writeReplaced(store, identity, {});
    else if (verdict.status === "unknown") await writeUnknown(store, identity, {});
    return verdict;
  });
}

async function readable(store, key) {
  if (!store || store.blocked || typeof store.get !== "function") return null;
  return store.get(key);
}

/**
 * Limited polls while the page is visible. The caller supplies the wait between polls.
 * This does not register a repeating timer.
 */
export async function pollVisibleOutcome({ isVisible, maxPolls, verify, schedule } = {}) {
  const limit = Number.isSafeInteger(maxPolls) && maxPolls > 0 ? Math.min(maxPolls, 8) : 3;
  let ran = 0;
  for (let index = 0; index < limit; index += 1) {
    if (typeof isVisible !== "function" || isVisible() !== true) {
      return { ran, stopped: "hidden", usedInterval: false };
    }
    if (typeof verify === "function") await verify();
    ran += 1;
    if (index + 1 >= limit) break;
    if (isVisible() !== true) return { ran, stopped: "hidden", usedInterval: false };
    if (typeof schedule === "function") await schedule();
  }
  return { ran, stopped: "limit", usedInterval: false };
}

/**
 * Client-trusted Lista read policy. Task JSON cannot name these sources.
 *
 * S-1b: the Lista encoding, pins, and decoders moved to the client core
 * (./execution/core/lista.mjs). This file re-exports them with the same names
 * and values. The read-only RPC transport stays here because the core has no
 * network or wallet access.
 */

import { readRpcResult } from "./rpc-result.mjs";
import { TRUSTED_LISTA } from "./execution/core/lista.mjs";

export {
  CLAIM_SIGNATURE,
  DEPLOYMENT_APPROVALS,
  TRUSTED_LISTA,
  selectorOf,
  eventTopic,
  CLAIM_WITHDRAWAL_EVENT,
  CLAIM_WITHDRAWAL_TOPIC,
  CLAIM_SELECTOR,
  trustedQuerySources,
  isAddress,
  sameAddress,
  claimCalldata,
  encodePausedCall,
  encodeWithdrawalRequestsCall,
  encodeRequestStatusCall,
  decodeBool,
  decodeStatus,
  decodeWithdrawalRequests,
  addressFromStorage,
  codeSha256,
  hexQuantity,
} from "./execution/core/lista.mjs";

/**
 * EIP-1967 implementation slot pin. The S-1a candidate generator
 * (config/execution/candidate-rules.json eip1967_slot_file) looks for this
 * constant in this file, so it stays written here and must match the core.
 */
const EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc";
if (TRUSTED_LISTA.implementationSlot !== EIP1967_IMPLEMENTATION_SLOT) {
  throw new Error("Lista implementation slot is not the pinned EIP-1967 slot");
}

/** Read-only Lista calls and receipt checks. Log queries stay off this list. */
const READ_METHODS = new Set([
  "eth_chainId",
  "eth_blockNumber",
  "eth_getBlockByNumber",
  "eth_call",
  "eth_getCode",
  "eth_getStorageAt",
  "eth_getTransactionReceipt",
  "eth_getTransactionByHash",
  "eth_getBalance",
  "eth_getTransactionCount",
]);

const TRANSACTION_COUNT_TAGS = new Set(["latest", "pending"]);

export function assertReadMethod(method) {
  if (typeof method !== "string" || !READ_METHODS.has(method)) {
    throw new Error("rpc method is not a trusted read");
  }
}

/** Method allowlist plus the parameter limits for receipt reads. `eth_getBlockByNumber` may set full=true. */
export function assertReadCall(method, params) {
  assertReadMethod(method);
  if (method === "eth_getTransactionCount") {
    if (!Array.isArray(params) || params.length !== 2 || !TRANSACTION_COUNT_TAGS.has(params[1])) {
      throw new Error("eth_getTransactionCount accepts only latest or pending");
    }
  }
  if (method === "eth_getBlockByNumber") {
    if (!Array.isArray(params) || params.length !== 2 || typeof params[1] !== "boolean") {
      throw new Error("eth_getBlockByNumber full flag must be boolean");
    }
  }
}

const RPC_REQUEST_ID = 1;

export async function fetchTrustedRpc(method, params, { fetchImpl, signal } = {}) {
  assertReadCall(method, params);
  const response = await (fetchImpl ?? globalThis.fetch)(TRUSTED_LISTA.rpcUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: RPC_REQUEST_ID, method, params }),
    redirect: "error",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    await response.body?.cancel?.().catch(() => {});
    throw new Error("rpc http");
  }
  const result = await readRpcResult(response, RPC_REQUEST_ID);
  if (result == null) throw new Error("rpc body");
  return result;
}

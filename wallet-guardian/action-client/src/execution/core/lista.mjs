/**
 * Lista encoding and pins for the client core (S-1b).
 * Moved byte-for-byte from src/trusted-lista.mjs at 7fa97fe. The RPC transport
 * (READ_METHODS, assertReadMethod, assertReadCall, fetchTrustedRpc) stays in
 * src/trusted-lista.mjs because the core has no network or wallet access.
 * Task JSON cannot name these sources.
 */

import { keccak_256 } from "@noble/hashes/sha3.js";

export const CLAIM_SIGNATURE = "claimWithdraw(uint256)";

/**
 * Client-owned deployment pins. D-1: entries stay empty until an operator
 * approves one. An empty list is valid and means unapproved_deployment.
 * A chain read must not fill this. Keep the same object in
 * config/protocols/lista-bsc.json and
 * src/guardian/_resources/config/protocols/lista-bsc.json.
 */
export const DEPLOYMENT_APPROVALS = Object.freeze({
  registry_version: 1,
  provenance: "client policy; entries are reviewed pins and are not learned from a chain read or from task JSON",
  entries: Object.freeze([]),
});

export const TRUSTED_LISTA = Object.freeze({
  chainId: 56,
  rpcUrl: "https://bsc-dataseed.bnbchain.org",
  proxy: "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6",
  implementationSlot: "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
  freshnessTtlSeconds: 120,
  maxClockSkewSeconds: 120,
  method: CLAIM_SIGNATURE,
  protocolId: "lista-slisbnb",
  reads: Object.freeze([
    "paused()",
    "getUserWithdrawalRequests(address)",
    "getUserRequestStatus(address,uint256)",
  ]),
  approvalRegistryVersion: DEPLOYMENT_APPROVALS.registry_version,
  approvals: DEPLOYMENT_APPROVALS.entries,
});

function bytesToHex(bytes) {
  let hex = "0x";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

export function selectorOf(signature) {
  return bytesToHex(keccak_256(new TextEncoder().encode(signature)).slice(0, 4));
}

export function eventTopic(signature) {
  return bytesToHex(keccak_256(new TextEncoder().encode(signature)));
}

/** Lista stake-manager claim. Indexed account, idx, and amount. The uuid is not in the log. */
export const CLAIM_WITHDRAWAL_EVENT = "ClaimWithdrawal(address,uint256,uint256)";
export const CLAIM_WITHDRAWAL_TOPIC = eventTopic(CLAIM_WITHDRAWAL_EVENT);

export const CLAIM_SELECTOR = selectorOf(CLAIM_SIGNATURE);
if (CLAIM_SELECTOR !== "0xb13acedd") {
  throw new Error("claimWithdraw selector is not the pinned Lista selector");
}

const SELECTORS = Object.freeze({
  claimWithdraw: CLAIM_SELECTOR,
  paused: selectorOf("paused()"),
  getUserWithdrawalRequests: selectorOf("getUserWithdrawalRequests(address)"),
  getUserRequestStatus: selectorOf("getUserRequestStatus(address,uint256)"),
});

/** Ignore any rpc, abi, or deployment the task document claims. */
export function trustedQuerySources(_taskJson) {
  return {
    rpcUrl: TRUSTED_LISTA.rpcUrl,
    chainId: TRUSTED_LISTA.chainId,
    proxy: TRUSTED_LISTA.proxy,
    implementationSlot: TRUSTED_LISTA.implementationSlot,
    abi: Object.freeze([...TRUSTED_LISTA.reads, TRUSTED_LISTA.method]),
    allowedOps: Object.freeze([TRUSTED_LISTA.method]),
    selectors: SELECTORS,
  };
}

/** True when value is a 20-byte hex address. */
export function isAddress(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value);
}

export function sameAddress(left, right) {
  return typeof right === "string" && isAddress(left) && left.toLowerCase() === right.toLowerCase();
}

function hexToBytes(hex) {
  const raw = typeof hex === "string" && hex.startsWith("0x") ? hex.slice(2) : null;
  if (raw === null || raw.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(raw)) {
    throw new Error("bad hex");
  }
  const bytes = new Uint8Array(raw.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(raw.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

function word(value) {
  const hex = BigInt(value).toString(16);
  if (hex.length > 64) throw new Error("word overflow");
  return hex.padStart(64, "0");
}

function addressWord(address) {
  if (!isAddress(address)) throw new Error("bad address");
  return address.slice(2).toLowerCase().padStart(64, "0");
}

export function claimCalldata(idx) {
  if (!Number.isSafeInteger(idx) || idx < 0) throw new Error("bad idx");
  return CLAIM_SELECTOR + word(idx);
}

export function encodePausedCall() {
  return SELECTORS.paused;
}

export function encodeWithdrawalRequestsCall(account) {
  return SELECTORS.getUserWithdrawalRequests + addressWord(account);
}

export function encodeRequestStatusCall(account, idx) {
  return SELECTORS.getUserRequestStatus + addressWord(account) + word(idx);
}

function wordAt(bytes, offset) {
  if (offset < 0 || offset + 32 > bytes.length) throw new Error("short word");
  let hex = "0x";
  for (let index = 0; index < 32; index += 1) hex += bytes[offset + index].toString(16).padStart(2, "0");
  return BigInt(hex);
}

function boolWord(value) {
  if (value !== 0n && value !== 1n) throw new Error("bad bool");
  return value === 1n;
}

export function decodeBool(hex) {
  const bytes = hexToBytes(hex);
  if (bytes.length < 32) throw new Error("short bool");
  for (let index = 0; index < 31; index += 1) if (bytes[index] !== 0) throw new Error("bad bool");
  return boolWord(wordAt(bytes, 0));
}

export function decodeStatus(hex) {
  const bytes = hexToBytes(hex);
  if (bytes.length < 64) throw new Error("short status");
  for (let index = 0; index < 31; index += 1) if (bytes[index] !== 0) throw new Error("bad bool");
  return { isClaimable: boolWord(wordAt(bytes, 0)), bnbAmount: wordAt(bytes, 32).toString(10) };
}

export function decodeWithdrawalRequests(hex) {
  const bytes = hexToBytes(hex);
  if (bytes.length < 64) throw new Error("short requests");
  const offset = wordAt(bytes, 0);
  if (offset !== 32n) throw new Error("unexpected requests offset");
  const length = wordAt(bytes, 32);
  if (length > 128n) throw new Error("requests too long");
  const count = Number(length);
  if (bytes.length < 64 + count * 96) throw new Error("truncated requests");
  const rows = [];
  for (let index = 0; index < count; index += 1) {
    const at = 64 + index * 96;
    rows.push({
      idx: index,
      uuid: wordAt(bytes, at).toString(10),
      amountIn: wordAt(bytes, at + 32).toString(10),
      startTime: wordAt(bytes, at + 64).toString(10),
    });
  }
  return rows;
}

export function addressFromStorage(hex) {
  const raw = typeof hex === "string" && hex.startsWith("0x") ? hex.slice(2).toLowerCase() : "";
  const padded = raw.padStart(64, "0");
  if (!/^0{24}[0-9a-f]{40}$/.test(padded)) throw new Error("bad implementation word");
  return "0x" + padded.slice(24);
}

export async function codeSha256(hex) {
  const bytes = hex === "0x" ? new Uint8Array() : hexToBytes(hex);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function hexQuantity(value) {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]+$/.test(value)) throw new Error("bad quantity");
  const parsed = Number.parseInt(value, 16);
  if (!Number.isSafeInteger(parsed)) throw new Error("bad quantity");
  return parsed;
}

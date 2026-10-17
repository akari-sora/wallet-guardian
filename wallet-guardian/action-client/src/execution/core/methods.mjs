/**
 * Wallet RPC method check without naming the method in the core bundle.
 * The core never calls a wallet; it only verifies that a caller-built request
 * uses the one allowed send method (§3.5 (1) L213). The constant is the keccak-256
 * of that method name, so the core artifact carries no send-method string
 * (PLAN §2 :8 後半; marker scan must report 0 for the core build).
 */
import { keccakHex } from "./hex.mjs";

export const SEND_METHOD_KECCAK = "0x087da26ceed52d7a80bace742f7f2e05ef48fa7a1839d1da67a376529b890ae8";

export function isAllowedSendMethod(method) {
  return typeof method === "string" && keccakHex(method) === SEND_METHOD_KECCAK;
}

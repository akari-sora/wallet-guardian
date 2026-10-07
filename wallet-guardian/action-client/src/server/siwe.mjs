import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";
import { LOGIN_STATEMENT } from "../login-statement.mjs";

export { LOGIN_STATEMENT };
export { buildSiweMessage, checksumAddress } from "../siwe-message.mjs";

function bytesToHex(bytes) {
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

export function readNonce(message) {
  if (typeof message !== "string" || message.length > 4096) return null;
  const match = /\nNonce: ([A-Za-z0-9]{16,128})\n/.exec(message);
  return match ? match[1] : null;
}

function personalHash(message) {
  const payload = new TextEncoder().encode(message);
  const prefix = new TextEncoder().encode(`\u0019Ethereum Signed Message:\n${payload.length}`);
  const bytes = new Uint8Array(prefix.length + payload.length);
  bytes.set(prefix, 0);
  bytes.set(payload, prefix.length);
  return keccak_256(bytes);
}

/**
 * ERC-1271 contract accounts are unsupported.
 * Recovery is EOA-only. An owner key does not authenticate a different address.
 */
export function recoverAddress(message, signature) {
  if (typeof signature !== "string" || signature.length > 200) return { error: "erc1271_unsupported" };
  const hex = signature.startsWith("0x") || signature.startsWith("0X") ? signature.slice(2) : signature;
  if (!/^[0-9a-fA-F]+$/.test(hex) || hex.length !== 130) return { error: "erc1271_unsupported" };
  const bytes = new Uint8Array(65);
  for (let i = 0; i < 65; i += 1) bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  let recovery = bytes[64];
  if (recovery >= 27) recovery -= 27;
  if (recovery !== 0 && recovery !== 1) return { error: "erc1271_unsupported" };
  try {
    const point = secp256k1.Signature.fromCompact(bytes.slice(0, 64))
      .addRecoveryBit(recovery)
      .recoverPublicKey(personalHash(message));
    const uncompressed = point.toRawBytes(false).slice(1);
    return { address: `0x${bytesToHex(keccak_256(uncompressed).slice(-20))}` };
  } catch {
    return { error: "signature_mismatch" };
  }
}

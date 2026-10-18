/** Hex and hash helpers for the client core. No network, no wallet. */
import { keccak_256 } from "@noble/hashes/sha3.js";
import { refuse } from "./errors.mjs";

export function bytesToHex(bytes) {
  let hex = "0x";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  return hex;
}

export function hexToBytes(hex, label = "hex") {
  const raw = typeof hex === "string" && hex.startsWith("0x") ? hex.slice(2) : null;
  if (raw === null || raw.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(raw)) refuse("bad_hex", label);
  const bytes = new Uint8Array(raw.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(raw.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

export function keccakHex(text) {
  return bytesToHex(keccak_256(new TextEncoder().encode(text)));
}

/** 4-byte selector of a canonical signature such as `approve(address,uint256)`. */
export function selectorOfCanonical(canonical) {
  return keccakHex(canonical).slice(0, 10);
}

export function isAddressHex(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value);
}

export function sameAddressHex(left, right) {
  return isAddressHex(left) && isAddressHex(right) && left.toLowerCase() === right.toLowerCase();
}

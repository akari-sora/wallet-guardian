/** EIP-55 checksum. Matches eth_utils.to_checksum_address for 20-byte addresses. */

import { keccak_256 } from "@noble/hashes/sha3.js";

import { ExecutionRegistryError } from "./errors.mjs";

export function toChecksumAddress(value) {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(value)) {
    throw new ExecutionRegistryError("bad_address", "address");
  }
  const hex = value.slice(2).toLowerCase();
  const hash = keccak_256(new TextEncoder().encode(hex));
  let out = "0x";
  for (let index = 0; index < 40; index += 1) {
    const nibble = (hash[index >> 1] >> (index % 2 === 0 ? 4 : 0)) & 0xf;
    const char = hex[index];
    out += nibble >= 8 ? char.toUpperCase() : char;
  }
  return out;
}

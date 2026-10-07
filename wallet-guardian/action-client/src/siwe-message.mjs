import { keccak_256 } from "@noble/hashes/sha3";
import { LOGIN_STATEMENT } from "./login-statement.mjs";

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function checksumAddress(address) {
  if (typeof address !== "string" || !ADDRESS.test(address)) return null;
  const hex = address.toLowerCase().slice(2);
  const hash = keccak_256(new TextEncoder().encode(hex));
  let out = "0x";
  for (let i = 0; i < 40; i += 1) {
    const nibble = (hash[i >> 1] >> (i % 2 === 0 ? 4 : 0)) & 0xf;
    out += nibble >= 8 ? hex[i].toUpperCase() : hex[i];
  }
  return out;
}

export function buildSiweMessage({ domain, address, uri, chainId, nonce, issuedAt, expirationTime }) {
  return [
    `${domain} wants you to sign in with your Ethereum account:`,
    address,
    "",
    LOGIN_STATEMENT,
    "",
    `URI: ${uri}`,
    "Version: 1",
    `Chain ID: ${chainId}`,
    `Nonce: ${nonce}`,
    `Issued At: ${issuedAt}`,
    `Expiration Time: ${expirationTime}`,
  ].join("\n");
}

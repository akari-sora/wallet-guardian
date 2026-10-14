import { LOGIN_STATEMENT } from "./login-statement.mjs";
import { buildSiweMessage, checksumAddress } from "./siwe-message.mjs";
import { productionViewEntries } from "./view-origin.mjs";

/** Audience the action client is willing to sign. Not an execution allowlist. */
const audience = productionViewEntries()[0];
if (!audience) throw new Error("private task view origin is not configured");
export const LOGIN_CHAIN_ID = 56;
export const LOGIN_DOMAIN = new URL(audience.origin).host;
export const LOGIN_URI = audience.origin;

const NONCE = /^[a-f0-9]{32}$/;
const ISO = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$/;
const CSRF = /^[a-f0-9]{64}$/;
const CHALLENGE_KEYS = [
  "message",
  "csrf_token",
  "expires_at",
  "domain",
  "uri",
  "chain_id",
  "address",
  "erc1271",
  "execution_authorized",
  "send_authorized",
];
const SKEW_MS = 60_000;
const TTL_MS = 600_000;

function parseIso(value) {
  if (typeof value !== "string" || !ISO.test(value)) return null;
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  if (new Date(ms).toISOString() !== value) return null;
  return ms;
}

/**
 * Parse a login message and rebuild it. Returns the parsed fields only when
 * the rebuilt bytes match and every field is the fixed read-only policy.
 */
export function acceptLoginMessage(message, address, nowMs) {
  if (typeof message !== "string" || message.length === 0 || message.length > 4096) return null;
  if (message.includes("\r") || message.includes("\0")) return null;
  const checksum = checksumAddress(address);
  if (!checksum || typeof nowMs !== "number" || !Number.isFinite(nowMs)) return null;
  const lines = message.split("\n");
  if (lines.length !== 11) return null;
  const [domainLine, addressLine, blank1, statement, blank2, uriLine, version, chainLine, nonceLine, issuedLine, expLine] = lines;
  if (domainLine !== `${LOGIN_DOMAIN} wants you to sign in with your Ethereum account:`) return null;
  if (addressLine !== checksum) return null;
  if (blank1 !== "" || blank2 !== "") return null;
  if (statement !== LOGIN_STATEMENT) return null;
  if (!uriLine.startsWith("URI: ") || uriLine.slice(5) !== LOGIN_URI) return null;
  if (version !== "Version: 1") return null;
  if (!chainLine.startsWith("Chain ID: ") || chainLine.slice("Chain ID: ".length) !== String(LOGIN_CHAIN_ID)) return null;
  if (!nonceLine.startsWith("Nonce: ")) return null;
  const nonce = nonceLine.slice("Nonce: ".length);
  if (!NONCE.test(nonce)) return null;
  if (!issuedLine.startsWith("Issued At: ") || !expLine.startsWith("Expiration Time: ")) return null;
  const issuedAt = issuedLine.slice("Issued At: ".length);
  const expirationTime = expLine.slice("Expiration Time: ".length);
  const issuedMs = parseIso(issuedAt);
  const expMs = parseIso(expirationTime);
  if (issuedMs == null || expMs == null) return null;
  if (issuedMs > nowMs + SKEW_MS) return null;
  if (expMs <= nowMs) return null;
  const span = expMs - issuedMs;
  if (span <= 0 || span > TTL_MS + SKEW_MS) return null;
  const rebuilt = buildSiweMessage({
    domain: LOGIN_DOMAIN,
    address: checksum,
    uri: LOGIN_URI,
    chainId: LOGIN_CHAIN_ID,
    nonce,
    issuedAt,
    expirationTime,
  });
  if (rebuilt !== message) return null;
  return {
    message,
    address: checksum,
    nonce,
    issuedAt,
    expirationTime,
    domain: LOGIN_DOMAIN,
    uri: LOGIN_URI,
    chainId: LOGIN_CHAIN_ID,
  };
}

/** Challenge JSON the client may hand to personal_sign. Null means do not sign. */
export function acceptLoginChallenge(body, address, nowMs) {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const keys = Object.keys(body);
  if (keys.length !== CHALLENGE_KEYS.length) return null;
  for (const key of CHALLENGE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(body, key)) return null;
  }
  if (body.execution_authorized !== false || body.send_authorized !== false || body.erc1271 !== "unsupported") return null;
  if (body.domain !== LOGIN_DOMAIN || body.uri !== LOGIN_URI || body.chain_id !== LOGIN_CHAIN_ID) return null;
  if (typeof body.csrf_token !== "string" || !CSRF.test(body.csrf_token)) return null;
  if (!Number.isSafeInteger(body.expires_at)) return null;
  const parsed = acceptLoginMessage(body.message, address, nowMs);
  if (!parsed) return null;
  if (body.address !== parsed.address) return null;
  if (new Date(body.expires_at * 1000).toISOString() !== parsed.expirationTime) return null;
  return parsed.message;
}

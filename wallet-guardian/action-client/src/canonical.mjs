/** Canonical JSON. Must match guardian.canonical.canonical_dumps. */

export function canonicalStringify(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalStringify(item)).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  return `{${keys
    .map((key) => `${JSON.stringify(key)}:${canonicalStringify(value[key])}`)
    .join(",")}}`;
}

export const CANONICAL_PLAN_KEYS = [
  "protocol_version",
  "task_id",
  "revision",
  "chain_namespace",
  "chain_id",
  "sender",
  "execution_account",
  "execution_account_type",
  "recipient",
  "protocol_id",
  "deployment",
  "intent",
  "calls",
  "asset_in",
  "asset_out",
  "preconditions",
  "bounds",
  "max_fee_wei",
  "simulation",
  "wallet_request_allowed",
];

export async function digestBody(body) {
  const payload = {};
  for (const key of CANONICAL_PLAN_KEYS) {
    payload[key] = body[key];
  }
  const bytes = new TextEncoder().encode(canonicalStringify(payload));
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Pages `_headers` do not apply to Function responses.
 * Every success and error response sets Cache-Control itself.
 */

const NO_STORE = "no-store";

export function randomHex(byteLength) {
  const bytes = crypto.getRandomValues(new Uint8Array(byteLength));
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out;
}

export async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function timingSafeEqual(left, right) {
  const a = typeof left === "string" ? left : "";
  const b = typeof right === "string" ? right : "";
  const key = await crypto.subtle.importKey(
    "raw",
    crypto.getRandomValues(new Uint8Array(32)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const encoder = new TextEncoder();
  const [macA, macB] = await Promise.all([
    crypto.subtle.sign("HMAC", key, encoder.encode(a)),
    crypto.subtle.sign("HMAC", key, encoder.encode(b)),
  ]);
  const viewA = new Uint8Array(macA);
  const viewB = new Uint8Array(macB);
  let diff = viewA.length ^ viewB.length;
  const length = Math.max(viewA.length, viewB.length);
  for (let i = 0; i < length; i += 1) diff |= (viewA[i] ?? 0) ^ (viewB[i] ?? 0);
  return diff === 0 && a.length === b.length;
}

export function cookies(request) {
  const header = request.headers.get("cookie") ?? "";
  const out = {};
  for (const part of header.split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    out[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return out;
}

export function hostCookie(name, value, maxAge) {
  return `${name}=${value}; Secure; HttpOnly; Path=/; SameSite=Lax; Max-Age=${maxAge}`;
}

export function json(status, body, extra = []) {
  const headers = new Headers();
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", NO_STORE);
  headers.set("pragma", "no-cache");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-content-type-options", "nosniff");
  headers.set("vary", "Cookie");
  for (const [name, value] of extra) headers.append(name, value);
  return new Response(JSON.stringify(body), { status, headers });
}

export function internalError() {
  return json(500, { error: "internal_error" });
}

export function stampNoStore(response) {
  const headers = new Headers(response.headers);
  headers.set("cache-control", NO_STORE);
  headers.set("pragma", "no-cache");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-content-type-options", "nosniff");
  return new Response(response.body, { status: response.status, headers });
}

export async function readJson(request, limit) {
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().includes("application/json")) return { error: "content_type" };
  if (!request.body) return { error: "invalid_json" };
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        return { error: "too_large" };
      }
      chunks.push(value);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // cancel() may already have released the reader
    }
  }
  if (size === 0) return { error: "invalid_json" };
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const value = JSON.parse(new TextDecoder().decode(bytes));
    if (!value || typeof value !== "object" || Array.isArray(value)) return { error: "invalid_json" };
    return { value };
  } catch {
    return { error: "invalid_json" };
  }
}

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

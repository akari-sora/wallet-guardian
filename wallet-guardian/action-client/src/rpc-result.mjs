/** JSON-RPC 2.0 response checks for one trusted read. Not chain truth. */

export const RPC_BODY_LIMIT = 1024 * 1024;

function concat(chunks, size) {
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/** Read at most `limit` bytes, then always cancel and unlock the reader. */
export async function readBoundedRpcBytes(response, limit = RPC_BODY_LIMIT) {
  if (!response || !response.body || typeof response.body.getReader !== "function") {
    throw new Error("empty rpc body");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
      size += bytes.byteLength;
      if (size > limit) throw new Error("rpc body too large");
      chunks.push(bytes);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  return concat(chunks, size);
}

export function decodeRpcJson(bytes) {
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new Error("rpc body is not utf-8");
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("rpc body is not json");
  }
}

function sameId(actual, expected) {
  return Object.is(actual, expected);
}

/** Require jsonrpc 2.0, the request id, and exactly one of result or error. */
export function takeRpcResult(body, expectedId) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("rpc envelope");
  if (body.jsonrpc !== "2.0") throw new Error("rpc version");
  if (!sameId(body.id, expectedId)) throw new Error("rpc id");
  const hasResult = Object.prototype.hasOwnProperty.call(body, "result");
  const hasError = Object.prototype.hasOwnProperty.call(body, "error");
  if (hasResult === hasError) throw new Error("rpc result xor error");
  if (hasError) {
    const message = body.error && typeof body.error.message === "string" ? body.error.message : "rpc error";
    const failure = new Error(message);
    failure.rpcError = body.error;
    throw failure;
  }
  return body.result;
}

export async function readRpcResult(response, expectedId, limit = RPC_BODY_LIMIT) {
  const bytes = await readBoundedRpcBytes(response, limit);
  return takeRpcResult(decodeRpcJson(bytes), expectedId);
}

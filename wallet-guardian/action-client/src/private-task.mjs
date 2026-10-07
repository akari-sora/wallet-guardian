import { clientConfirmDecision } from "./gate.mjs";

const REF = /^ref_[a-f0-9]{32}$/;

export function parsePrivateTaskRef(pathname) {
  if (typeof pathname !== "string") return null;
  const match = /^\/t\/(ref_[a-f0-9]{32})\/?$/.exec(pathname);
  if (!match || !REF.test(match[1])) return null;
  return match[1];
}

/** Task JSON, including preflight_ok, is not send authority. A chain re-read is not send authority either. */
export function privateTaskSendAllowed(task, mainnetSend = false) {
  const decision = clientConfirmDecision({
    plan: task && task.plan,
    task,
    mainnetSend: mainnetSend === true,
    mode: task && task.mode === "C" ? "C" : "A",
  });
  return decision.sendAllowed === true;
}

export async function readBoundedJson(response, limit = 256 * 1024) {
  const type = response.headers.get("content-type") ?? "";
  if (!type.includes("json")) throw new Error("not json");
  if (!response.body) throw new Error("empty");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error("too large");
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

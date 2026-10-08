/** Thin wrapper over `wrangler d1 execute DB` so tools use the same binding and config as the Pages project. */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const CLIENT_DIR = fileURLToPath(new URL("../../", import.meta.url));

export function d1Execute(target, sql, { persistTo, config } = {}) {
  const args = ["--no-install", "wrangler", "d1", "execute", "DB", "--json", "--command", sql];
  if (config) args.push("--config", config);
  if (target === "remote") args.push("--remote");
  else if (target === "local") {
    args.push("--local");
    if (persistTo) args.push("--persist-to", persistTo);
  } else throw new Error("target must be local or remote");
  const run = spawnSync("npx", args, {
    cwd: CLIENT_DIR,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "1" },
    maxBuffer: 16 * 1024 * 1024,
  });
  if (run.status !== 0) {
    const detail = `${run.stdout ?? ""}\n${run.stderr ?? ""}`.replace(/wgpub_[a-f0-9.]+/g, "wgpub_<redacted>").trim();
    throw new Error(`wrangler d1 execute failed (exit ${run.status}): ${detail.slice(-1200)}`);
  }
  const start = run.stdout.indexOf("[");
  const parsed = JSON.parse(run.stdout.slice(start));
  const results = [];
  for (const item of parsed) {
    if (item.success === false) throw new Error("d1 statement failed");
    results.push(...(item.results ?? []));
  }
  return results;
}

export function maskHex(value, keep = 4) {
  if (typeof value !== "string" || value.length <= keep + 2) return "<masked>";
  return `${value.slice(0, keep)}…${value.slice(-2)}`;
}

export function maskAddress(address) {
  if (typeof address !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(address)) return "<masked>";
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

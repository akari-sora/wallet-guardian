/**
 * Fail-closed gate for a mode_a send-variant build.
 * Exits non-zero unless WG_SEND_VARIANT=mode_a, WG_RELEASE_AUTH_ID is non-empty,
 * and deploy/releases.json has a mode_a entry with status approved_by_user.
 * This package does not add that entry and does not deploy.
 */
import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { loadReleaseDocument } from "./release-record.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));

export function sendVariantGate(env = process.env, document = loadReleaseDocument()) {
  if (!env || env.WG_SEND_VARIANT !== "mode_a") {
    return { ok: false, reason: "WG_SEND_VARIANT must be mode_a" };
  }
  if (typeof env.WG_RELEASE_AUTH_ID !== "string" || env.WG_RELEASE_AUTH_ID.trim() === "") {
    return { ok: false, reason: "WG_RELEASE_AUTH_ID is missing" };
  }
  const releases = document && Array.isArray(document.releases) ? document.releases : [];
  const approved = releases.some((entry) => entry && entry.variant === "mode_a" && entry.status === "approved_by_user");
  if (!approved) {
    return { ok: false, reason: "no approved_by_user mode_a inventory entry" };
  }
  return { ok: true, reason: "" };
}

function main() {
  const gate = sendVariantGate(process.env, loadReleaseDocument());
  if (!gate.ok) {
    console.error(`build:send-variant refused: ${gate.reason}`);
    process.exit(1);
  }
  const viteBin = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
  const child = spawnSync(process.execPath, [viteBin, "build"], {
    cwd: CLIENT,
    env: {
      ...process.env,
      WG_SEND_VARIANT: "mode_a",
      WG_SEND_VARIANT_BUILD: "mode_a",
    },
    stdio: "inherit",
  });
  process.exit(child.status === null ? 1 : child.status);
}

function isDirectRun() {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (isDirectRun()) main();

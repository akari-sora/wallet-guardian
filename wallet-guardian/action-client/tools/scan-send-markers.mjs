/**
 * Scan built JavaScript for send-construction markers.
 * A file is exempt only when its bytes match the content-hash allowlist.
 * The name walletconnect-sdk*.js is not an exemption.
 *
 * `--core <dir>` scans the client core build (S-1b) with the wider CORE_MARKER list.
 * The core has no allowlist exemption, must contain at least one .js file, and its
 * wg-core.digest.json must match the bundle bytes.
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const SEND_MARKER = /eth_sendTransaction|eth_sendRawTransaction|eth_signTransaction|wallet_sendCalls|testOnlyAllowWalletRequest|noBroadcast|request-policy-allow/g;

const ALLOWLIST_PATH = join(dirname(fileURLToPath(import.meta.url)), "send-marker-allowlist.json");

export function contentSha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

export function loadAllowlist(path = ALLOWLIST_PATH) {
  const document = JSON.parse(readFileSync(path, "utf8"));
  const hashes = Array.isArray(document.sha256) ? document.sha256 : [];
  return new Set(hashes.filter((item) => typeof item === "string" && item.length === 64));
}

export function isAllowlistedSdkContent(text, allowlist = loadAllowlist()) {
  if (typeof text !== "string") return false;
  return allowlist.has(contentSha256(text));
}

export function isWalletConnectSdkChunk(_rel, text) {
  return isAllowlistedSdkContent(text);
}

export function scanText(text) {
  return text.match(SEND_MARKER) ?? [];
}

/** Wider list for the client core: any wallet provider, signing, or send capability. */
export const CORE_MARKER = /eth_sendTransaction|eth_sendRawTransaction|eth_sign\w*|personal_sign|wallet_sendCalls|wallet_signAuthorization|eth_requestAccounts|window\.ethereum|\.request\(|testOnlyAllowWalletRequest|noBroadcast|request-policy-allow/g;

export const CORE_DIGEST_FILE = "wg-core.digest.json";

export function scanCoreText(text) {
  return text.match(CORE_MARKER) ?? [];
}

/** Scan a core build directory. Returns { hits, files, digest } where digest is "ok", "missing", or "mismatch". */
export function scanCoreMarkers(dir) {
  const hits = [];
  const files = [];
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const absolute = join(current, name);
      if (statSync(absolute).isDirectory()) {
        walk(absolute);
        continue;
      }
      if (!name.endsWith(".js") && !name.endsWith(".mjs")) continue;
      const rel = relative(dir, absolute).split("\\").join("/");
      files.push(rel);
      const found = scanCoreText(readFileSync(absolute, "utf8"));
      if (found.length > 0) hits.push({ file: rel, count: found.length, markers: [...new Set(found)] });
    }
  };
  walk(dir);
  let digest = "missing";
  try {
    const record = JSON.parse(readFileSync(join(dir, CORE_DIGEST_FILE), "utf8"));
    const bytes = readFileSync(join(dir, String(record.file)));
    digest = createHash("sha256").update(bytes).digest("hex") === record.sha256 ? "ok" : "mismatch";
  } catch {
    digest = "missing";
  }
  return { hits, files, digest };
}

function mainCore(dir) {
  const { hits, files, digest } = scanCoreMarkers(dir);
  if (files.length === 0) {
    console.error(`core markers: no .js files in ${dir}`);
    process.exit(2);
  }
  for (const hit of hits) console.error(`core ${hit.file}: ${hit.markers.join(",")} (${hit.count})`);
  if (digest !== "ok") console.error(`core digest: ${digest}`);
  if (hits.length > 0 || digest !== "ok") process.exit(1);
  process.stdout.write("core markers: 0\n");
  process.exit(0);
}

export function scanSendMarkers(dir, options = {}) {
  const allowlist = options.allowlist ?? loadAllowlist();
  const hits = [];
  const walk = (current) => {
    for (const name of readdirSync(current)) {
      const absolute = join(current, name);
      if (statSync(absolute).isDirectory()) {
        walk(absolute);
        continue;
      }
      if (!name.endsWith(".js")) continue;
      const rel = relative(dir, absolute).split("\\").join("/");
      const text = readFileSync(absolute, "utf8");
      if (isAllowlistedSdkContent(text, allowlist)) continue;
      const found = scanText(text);
      if (found.length > 0) hits.push({ file: rel, count: found.length, markers: [...new Set(found)] });
    }
  };
  walk(dir);
  return hits;
}

function main() {
  if (process.argv[2] === "--core") {
    if (!process.argv[3]) {
      console.error("usage: node tools/scan-send-markers.mjs --core <core-dist-dir>");
      process.exit(2);
    }
    mainCore(process.argv[3]);
    return;
  }
  const dir = process.argv[2];
  if (!dir) {
    console.error("usage: node tools/scan-send-markers.mjs <dist-dir> | --core <core-dist-dir>");
    process.exit(2);
  }
  const hits = scanSendMarkers(dir);
  if (hits.length === 0) {
    process.stdout.write("send markers: 0\n");
    process.exit(0);
  }
  for (const hit of hits) console.error(`${hit.file}: ${hit.markers.join(",")} (${hit.count})`);
  process.exit(1);
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) main();

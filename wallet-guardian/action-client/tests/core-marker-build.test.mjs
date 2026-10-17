/**
 * S-1b decisions 1–3: `npm run build` builds the core as a separate output with a digest,
 * then runs the marker scans. A marker in the core makes the build fail.
 * Builds run in temp copies; the real dist is not touched. Nothing is deployed.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { CORE_MARKER, scanCoreMarkers, scanCoreText, scanText } from "../tools/scan-send-markers.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const SCANNER = join(CLIENT, "tools/scan-send-markers.mjs");

function run(cwd, command, args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd, env: process.env });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (status) => resolvePromise({ status, output: `${stdout}\n${stderr}` }));
  });
}

function tempClient() {
  const root = mkdtempSync(join(tmpdir(), "wg-core-build-"));
  const project = join(root, "wallet-guardian");
  const client = join(project, "action-client");
  mkdirSync(join(project, "config/deployments"), { recursive: true });
  for (const name of ["src", "tools", "public"]) {
    if (existsSync(join(CLIENT, name))) cpSync(join(CLIENT, name), join(client, name), { recursive: true });
  }
  for (const name of ["index.html", "vite.config.ts", "vite.core.config.mjs", "tsconfig.json", "package.json"]) {
    cpSync(join(CLIENT, name), join(client, name));
  }
  cpSync(join(CLIENT, "../config/deployments/private-task-origins.json"), join(project, "config/deployments/private-task-origins.json"));
  symlinkSync(join(CLIENT, "node_modules"), join(client, "node_modules"));
  return { root, client };
}

function inject(client, source) {
  const core = join(client, "src/execution/core");
  writeFileSync(join(core, "injected-marker.mjs"), source);
  const index = join(core, "index.mjs");
  writeFileSync(index, `${readFileSync(index, "utf8")}export * from "./injected-marker.mjs";\n`);
}

test("core marker list covers the decided strings, wider than the page list", () => {
  for (const marker of ["eth_sendTransaction", "eth_sendRawTransaction", "eth_sign", "eth_signTransaction", "eth_signTypedData_v4", "personal_sign", "wallet_sendCalls", "window.ethereum", "provider.request(", "eth_requestAccounts"]) {
    assert.ok(scanCoreText(`x = "${marker}"`).length > 0, marker);
  }
  assert.equal(scanText('x = "personal_sign"').length, 0, "page list stays as it was");
  assert.ok(CORE_MARKER.global);
});

test("core scan: missing or tampered digest fails; empty dir has no files", () => {
  const dir = mkdtempSync(join(tmpdir(), "wg-core-scan-"));
  try {
    writeFileSync(join(dir, "wg-core.js"), "export const ok = 1;\n");
    assert.equal(scanCoreMarkers(dir).digest, "missing");
    const sha256 = createHash("sha256").update(readFileSync(join(dir, "wg-core.js"))).digest("hex");
    writeFileSync(join(dir, "wg-core.digest.json"), JSON.stringify({ file: "wg-core.js", sha256 }));
    assert.deepEqual(scanCoreMarkers(dir), { hits: [], files: ["wg-core.js"], digest: "ok" });
    writeFileSync(join(dir, "wg-core.js"), "export const ok = 2;\n");
    assert.equal(scanCoreMarkers(dir).digest, "mismatch");
    const empty = mkdtempSync(join(tmpdir(), "wg-core-empty-"));
    assert.deepEqual(scanCoreMarkers(empty).files, []);
    rmSync(empty, { recursive: true, force: true });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("core scan CLI exits 2 on an empty core dir and 1 on a hit", async () => {
  const empty = mkdtempSync(join(tmpdir(), "wg-core-empty-"));
  const dirty = mkdtempSync(join(tmpdir(), "wg-core-dirty-"));
  try {
    assert.equal((await run(CLIENT, process.execPath, [SCANNER, "--core", empty])).status, 2);
    writeFileSync(join(dirty, "wg-core.js"), 'const p = window.ethereum;\n');
    const sha256 = createHash("sha256").update(readFileSync(join(dirty, "wg-core.js"))).digest("hex");
    writeFileSync(join(dirty, "wg-core.digest.json"), JSON.stringify({ file: "wg-core.js", sha256 }));
    const result = await run(CLIENT, process.execPath, [SCANNER, "--core", dirty]);
    assert.equal(result.status, 1);
    assert.match(result.output, /window\.ethereum/);
  } finally {
    rmSync(empty, { recursive: true, force: true });
    rmSync(dirty, { recursive: true, force: true });
  }
});

test("control: npm run build in a clean temp copy exits 0 with a separate core output and digest", async () => {
  const { root, client } = tempClient();
  try {
    const result = await run(client, "npm", ["run", "build"]);
    assert.equal(result.status, 0, result.output);
    assert.match(result.output, /send markers: 0/);
    assert.match(result.output, /core markers: 0/);
    const record = JSON.parse(readFileSync(join(client, "dist/core/wg-core.digest.json"), "utf8"));
    const bytes = readFileSync(join(client, "dist/core", record.file));
    assert.equal(record.file, "wg-core.js");
    assert.equal(record.bytes, bytes.length);
    assert.equal(record.sha256, createHash("sha256").update(bytes).digest("hex"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}, { timeout: 300000 });

const INJECTIONS = [
  { marker: "eth_sendTransaction", source: 'export const INJECTED_MARKER = "eth_sendTransaction";\n' },
  { marker: "personal_sign", source: 'export const INJECTED_MARKER = "personal_sign";\n' },
  { marker: ".request(", source: "export function injectedCall(provider) { return provider.request({}); }\n" },
];

for (const { marker, source } of INJECTIONS) {
  test(`reverse: a core module containing ${marker} makes npm run build fail`, async () => {
    const { root, client } = tempClient();
    try {
      inject(client, source);
      const result = await run(client, "npm", ["run", "build"]);
      assert.notEqual(result.status, 0, result.output);
      assert.ok(result.output.includes(`core wg-core.js: `), result.output);
      assert.ok(result.output.includes(marker), result.output);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }, { timeout: 300000 });
}

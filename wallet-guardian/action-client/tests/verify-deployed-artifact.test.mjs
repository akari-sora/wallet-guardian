import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { manifestFromDirectory } from "../tools/artifact-manifest.mjs";
import { productionViewOrigin } from "../src/view-origin.mjs";

const CLIENT = new URL("..", import.meta.url);
const ORIGIN = productionViewOrigin();
const REF = `/t/ref_${"0".repeat(32)}`;

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "wg-artifact-"));
  mkdirSync(join(root, "assets"));
  const headers = [
    "/*",
    "  Content-Security-Policy: default-src 'none'; script-src 'self'",
    "",
  ].join("\n");
  const app = "export const ready = true;\nimport('./lazy-def.js');\n";
  const lazy = "export const lazy = 1;\n";
  const css = "body{color:#111}\n";
  const html = "<!doctype html><html><head><link rel=\"stylesheet\" href=\"/assets/app.css\"></head><body><script type=\"module\" src=\"/assets/app.js\"></script></body></html>\n";
  writeFileSync(join(root, "_headers"), headers);
  writeFileSync(join(root, "index.html"), html);
  writeFileSync(join(root, "assets/app.js"), app);
  writeFileSync(join(root, "assets/lazy-def.js"), lazy);
  writeFileSync(join(root, "assets/app.css"), css);
  return { root, headers, app, lazy, css, html };
}

function releaseFor(manifest) {
  return {
    schema_version: "guardian.action_client_releases.v1",
    releases: [
      {
        release_id: "fixture-send-off",
        deployment_id: "wg-private-task-v1",
        origin: ORIGIN,
        source_sha: "a".repeat(40),
        client_build: "action-client@0.3.0",
        variant: "send_off",
        walletconnect: false,
        mainnet_send: false,
        files: manifest.files,
        headers_sha256: manifest.headers_sha256,
        cf_deployment_url: "https://fixture.wallet-guardian-action-client.pages.dev",
        status: "retired",
      },
    ],
  };
}

function start(handler) {
  const server = createServer(handler);
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({ server, origin: `http://127.0.0.1:${address.port}` });
    });
  });
}

function runVerifier(args) {
  const script = new URL("./tools/verify-deployed-artifact.mjs", CLIENT).pathname;
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script, ...args]);
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

test("artifact manifest hashes dist files and _headers", () => {
  const { root, app } = fixture();
  const manifest = manifestFromDirectory(root);
  assert.equal(manifest.headers_sha256, manifest.files._headers);
  assert.equal(typeof manifest.files["assets/app.js"], "string");
  assert.equal(manifest.files["assets/app.js"].length, 64);
  assert.notEqual(manifest.files["assets/app.js"], manifest.files["assets/lazy-def.js"]);
  assert.equal(app.includes("import('./lazy-def.js')"), true);
});

test("verifier accepts a local mock and names a one-byte asset change", async () => {
  const built = fixture();
  const manifest = manifestFromDirectory(built.root);
  const recordPath = join(built.root, "releases.json");
  writeFileSync(recordPath, JSON.stringify(releaseFor(manifest)));
  const files = {
    "/": { status: 200, type: "text/html; charset=utf-8", body: built.html, csp: true },
    "/index.html": { status: 200, type: "text/html; charset=utf-8", body: built.html },
    "/assets/app.js": { status: 200, type: "text/javascript", body: built.app },
    "/assets/lazy-def.js": { status: 200, type: "text/javascript", body: built.lazy },
    "/assets/app.css": { status: 200, type: "text/css", body: built.css },
    "/_headers": { status: 200, type: "text/plain", body: built.headers },
    "/api/session": { status: 200, type: "application/json", body: "{\"authenticated\":false}", cache: "no-store" },
    "/api/rpc": { status: 404, type: "application/json", body: "{\"error\":\"not_found\"}" },
    [REF]: { status: 200, type: "text/html; charset=utf-8", body: "<!doctype html><html><body>task</body></html>" },
  };
  const { server, origin } = await start((request, response) => {
    const path = new URL(request.url, origin).pathname;
    const item = files[path];
    if (!item) {
      response.writeHead(404, { "content-type": "text/plain" });
      response.end("missing");
      return;
    }
    const headers = { "content-type": item.type };
    if (item.cache) headers["cache-control"] = item.cache;
    if (item.csp) headers["content-security-policy"] = "default-src 'none'; script-src 'self'";
    response.writeHead(item.status, headers);
    response.end(item.body);
  });
  try {
    const ok = await runVerifier(["--origin", origin, "--release", "fixture-send-off", "--releases", recordPath]);
    assert.equal(ok.status, 0, ok.stderr);

    files["/assets/app.js"] = { status: 200, type: "text/javascript", body: `${built.app} ` };
    const tampered = await runVerifier(["--origin", origin, "--release", "fixture-send-off", "--releases", recordPath]);
    assert.notEqual(tampered.status, 0);
    assert.match(`${tampered.stdout}\n${tampered.stderr}`, /assets\/app\.js/);

    files["/assets/app.js"] = { status: 200, type: "text/javascript", body: built.app };
    files["/assets/lazy-def.js"] = { status: 404, type: "text/plain", body: "gone" };
    const missing = await runVerifier(["--origin", origin, "--release", "fixture-send-off", "--releases", recordPath]);
    assert.notEqual(missing.status, 0);
    assert.match(`${missing.stdout}\n${missing.stderr}`, /assets\/lazy-def\.js/);

    files["/assets/lazy-def.js"] = { status: 200, type: "text/javascript", body: built.lazy };
    const cleanScan = await runVerifier(["--origin", origin, "--release", "fixture-send-off", "--releases", recordPath, "--scan-send-markers"]);
    assert.equal(cleanScan.status, 0, cleanScan.stderr);
    files["/assets/app.js"] = { status: 200, type: "text/javascript", body: `${built.app}\neth_sendTransaction\n` };
    const dirtyScan = await runVerifier(["--origin", origin, "--release", "fixture-send-off", "--releases", recordPath, "--scan-send-markers"]);
    assert.notEqual(dirtyScan.status, 0);
    assert.match(dirtyScan.stderr, /assets\/app\.js: send marker eth_sendTransaction/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("expect-gone fails while client HTML is served and passes on 404", async () => {
  const built = fixture();
  const recordPath = join(built.root, "releases.json");
  writeFileSync(recordPath, JSON.stringify(releaseFor(manifestFromDirectory(built.root))));
  const html = await start((_request, response) => {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end("<!doctype html><html><body>client</body></html>");
  });
  try {
    const still = await runVerifier(["--origin", html.origin, "--release", "fixture-send-off", "--releases", recordPath, "--expect-gone"]);
    assert.notEqual(still.status, 0);
    assert.match(still.stderr, /client HTML/);
  } finally {
    await new Promise((resolve) => html.server.close(resolve));
  }
  const gone = await start((_request, response) => {
    response.writeHead(404, { "content-type": "text/plain" });
    response.end("not found");
  });
  try {
    const result = await runVerifier(["--origin", gone.origin, "--release", "fixture-send-off", "--releases", recordPath, "--expect-gone"]);
    assert.equal(result.status, 0, result.stderr);
  } finally {
    await new Promise((resolve) => gone.server.close(resolve));
  }
});

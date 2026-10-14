/**
 * send_off artifact checks. Requires `npm run build` for the dist scan.
 * The negative build uses a temp copy of src and does not deploy.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { isWalletConnectSdkChunk, scanSendMarkers, scanText } from "../tools/scan-send-markers.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const DIST = join(CLIENT, "dist");

function isFile(path) {
  return existsSync(path) && statSync(path).isFile();
}

function specsIn(file) {
  const text = readFileSync(file, "utf8");
  const specs = [];
  for (const pattern of [
    /from\s+["'](\.[^"']+)["']/g,
    /import\s*\(\s*["'](\.[^"']+)["']\s*\)/g,
    /import\s+["'](\.[^"']+)["']/g,
  ]) {
    for (const match of text.matchAll(pattern)) specs.push(match[1]);
  }
  return specs;
}

function resolveSpec(fromFile, spec) {
  const raw = resolve(dirname(fromFile), spec);
  const candidates = [raw, `${raw}.mjs`, `${raw}.ts`, `${raw}.js`, `${raw}.json`, join(raw, "index.mjs"), join(raw, "index.js")];
  return candidates.find((candidate) => isFile(candidate)) ?? null;
}

function importGraph(entry) {
  const seen = new Set();
  const queue = [entry];
  while (queue.length > 0) {
    const file = queue.pop();
    if (!file || seen.has(file)) continue;
    seen.add(file);
    if (file.endsWith(".json")) continue;
    for (const spec of specsIn(file)) {
      const next = resolveSpec(file, spec);
      if (next) queue.push(next);
    }
  }
  return seen;
}

function run(cwd, command, args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (status) => resolvePromise({ status, stdout, stderr }));
  });
}

test("main.ts import graph does not include src/send", () => {
  const graph = importGraph(join(CLIENT, "src/main.ts"));
  const send = [...graph].filter((file) => file.split("\\").join("/").includes("/src/send/"));
  assert.deepEqual(send, []);
  assert.ok(graph.size > 10);
});

test("scanner reports an application marker and exempts the WalletConnect SDK chunk name", () => {
  const root = mkdtempSync(join(tmpdir(), "wg-scan-"));
  mkdirSync(join(root, "assets"));
  writeFileSync(join(root, "assets/index-app.js"), "const method = \"eth_sendTransaction\";\n");
  writeFileSync(join(root, "assets/walletconnect-sdk-abc123.js"), "eth_sendTransaction wallet_sendCalls\n");
  const hits = scanSendMarkers(root);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].file, "assets/index-app.js");
  assert.equal(isWalletConnectSdkChunk("assets/walletconnect-sdk-abc123.js"), true);
  assert.deepEqual(scanText("personal_sign"), []);
});

test("built dist application chunks have no send markers", () => {
  assert.equal(existsSync(join(DIST, "index.html")), true, "run npm run build first");
  assert.deepEqual(scanSendMarkers(DIST), []);
});

test("a temp main.ts that imports src/send fails the send_off build", async () => {
  const root = mkdtempSync(join(tmpdir(), "wg-sendoff-"));
  const project = join(root, "wallet-guardian");
  const client = join(project, "action-client");
  mkdirSync(join(project, "config/deployments"), { recursive: true });
  cpSync(join(CLIENT, "src"), join(client, "src"), { recursive: true });
  for (const name of ["index.html", "vite.config.ts", "tsconfig.json", "package.json"]) {
    cpSync(join(CLIENT, name), join(client, name));
  }
  cpSync(join(CLIENT, "../config/deployments/private-task-origins.json"), join(project, "config/deployments/private-task-origins.json"));
  symlinkSync(join(CLIENT, "node_modules"), join(client, "node_modules"));
  const mainPath = join(client, "src/main.ts");
  writeFileSync(mainPath, `import "./send/mode-a-request.mjs";\n${readFileSync(mainPath, "utf8")}`);
  const result = await run(client, "npm", ["run", "build"]);
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}\n${result.stderr}`, /src\/send/);
}, { timeout: 180000 });

function chromePath() {
  for (const candidate of [process.env.WG_CHROME, "/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/chromium"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

test("headless chrome on an unapproved origin calls no wallet methods", async () => {
  assert.equal(existsSync(join(DIST, "index.html")), true, "run npm run build first");
  const chrome = chromePath();
  assert.ok(chrome, "needs Chrome/Chromium");
  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
    const root = resolve(DIST);
    const file = resolve(DIST, rel);
    const inside = file === root || file.startsWith(`${root}/`);
    if (!inside || !isFile(file)) {
      response.writeHead(404, { "content-type": "text/plain" });
      response.end("missing");
      return;
    }
    const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
    response.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream" });
    response.end(readFileSync(file));
  });
  await new Promise((resolvePromise) => server.listen(0, "127.0.0.1", resolvePromise));
  const port = server.address().port;
  const { chromium } = await import("playwright-core");
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  try {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.__calls = [];
      window.ethereum = {
        request: async (args) => {
          window.__calls.push(args);
          if (args && args.method === "eth_requestAccounts") return ["0x" + "1".repeat(40)];
          return null;
        },
        on() {},
        removeListener() {},
      };
    });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: "networkidle" });
    await page.locator("#banner").waitFor({ state: "visible" });
    assert.match(await page.locator("#banner").innerText(), /此網址不是核准來源/);
    assert.equal(await page.locator("#login").isDisabled(), true);
    await page.locator("#login").click({ force: true });
    const calls = await page.evaluate(() => window.__calls);
    assert.deepEqual(calls, []);
    assert.equal(calls.some((call) => call && call.method === "eth_requestAccounts"), false);
  } finally {
    await browser.close();
    await new Promise((resolvePromise) => server.close(resolvePromise));
  }
}, { timeout: 60000 });

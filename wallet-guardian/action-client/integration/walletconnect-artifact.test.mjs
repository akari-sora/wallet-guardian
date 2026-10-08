/**
 * WALLET-CONNECT-01 W-B artifact checks (test:wallets:artifact): WC-ARTIFACT-01 no-send and
 * WC-ARTIFACT-02 network-policy.
 *
 * Builds the client twice with the Vite JS API into temp dirs (WalletConnect off: no build settings;
 * on: VITE_WALLETCONNECT_ENABLED=true + a synthetic project id), serves each build in headless Chrome
 * at the production origin with the build's own dist/_headers applied (so the real CSP is enforced),
 * answers /api/* with the real Pages Function handler over an in-memory D1, and routes
 * wss://relay.walletconnect.org to an in-process MockRelay. The "phone wallet" is a test wallet on the
 * real @walletconnect/sign-client 2.25.0 connected to the same MockRelay. Not the real relay, not
 * Reown's servers, not a real wallet app. Needs Chrome/Chromium (WG_CHROME or /opt/google/chrome/chrome).
 */
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { extname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { secp256k1 } from "@noble/curves/secp256k1";
import { digestBody } from "../src/canonical.mjs";
import { contentSecurityPolicy } from "../src/csp.mjs";
import { sha256Hex, randomHex } from "../src/server/http.mjs";
import { checksumAddress } from "../src/siwe-message.mjs";
import { LOGIN_STATEMENT } from "../src/login-statement.mjs";
import { addressOf, realBackend, walletSign } from "../tests/wallet-fixtures.mjs";
import { MockRelay, startRelayServer } from "../tests/wc-relay-mock.mjs";
import { TEST_PROJECT_ID, createTestWallet } from "../tests/wc-fixtures.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const ORIGIN = "https://wallet-guardian-action-client.pages.dev";
const SEND_METHODS = new Set([
  "eth_sendTransaction", "eth_sendRawTransaction", "wallet_sendCalls", "eth_sign", "eth_signTransaction",
  "eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4", "wallet_grantPermissions",
  "wallet_switchEthereumChain", "wallet_addEthereumChain",
]);
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png" };
// The production Reown id, split so this file itself never contains it as one token.
const PRODUCTION_PROJECT_ID = ["957639a7", "5a85c8ba", "85aa5b9a", "efe1c189"].join("");
const CONFIRM = "在我的錢包確認";

function chromePath() {
  for (const candidate of [process.env.WG_CHROME, "/opt/google/chrome/chrome", "/usr/bin/chromium", "/usr/bin/google-chrome"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

const KEYS = ["VITE_WALLETCONNECT_ENABLED", "VITE_REOWN_PROJECT_ID", "VITE_GUARDIAN_MAINNET_SEND"];
async function buildVariant(walletConnect) {
  const { build } = await import("vite");
  const outDir = mkdtempSync(join(tmpdir(), `wg-wc-${walletConnect ? "on" : "off"}-`));
  const saved = Object.fromEntries(KEYS.map((k) => [k, process.env[k]]));
  for (const k of KEYS) delete process.env[k];
  if (walletConnect) {
    process.env.VITE_WALLETCONNECT_ENABLED = "true";
    process.env.VITE_REOWN_PROJECT_ID = TEST_PROJECT_ID;
  }
  try {
    await build({ root: CLIENT, configFile: join(CLIENT, "vite.config.ts"), mode: "production", logLevel: "error",
      build: { outDir, emptyOutDir: true } });
  } finally {
    for (const k of KEYS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  }
  return outDir;
}

function files(dir, prefix = "") {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) out.push(...files(join(dir, entry.name), rel));
    else out.push(rel);
  }
  return out;
}

/** Headers for "/*" from the build's own Cloudflare Pages _headers file. */
function pagesHeadersFor(dir) {
  const out = {};
  let inBlock = false;
  for (const line of readFileSync(join(dir, "_headers"), "utf8").split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    if (!/^\s/.test(line)) { inBlock = line.trim() === "/*"; continue; }
    if (!inBlock) continue;
    const at = line.indexOf(":");
    out[line.slice(0, at).trim().toLowerCase()] = line.slice(at + 1).trim();
  }
  return out;
}

async function forgedReadyTask(address) {
  const sample = JSON.parse(readFileSync(join(CLIENT, "public/sample-task.json"), "utf8"));
  delete sample.ref;
  sample.preflight = { preflight_ok: true, revision_required: false, changes: [] };
  sample.requires_user_wallet_confirmation = true;
  sample.evidence.fixed_https_origin = true;
  sample.evidence.mainnet_execution_authorized = true;
  for (const field of ["sender", "execution_account", "recipient"]) sample.plan[field] = address;
  sample.plan.human_summary.sender = address;
  sample.plan.human_summary.recipient = address;
  sample.plan.block_reasons = [];
  sample.plan.digest = await digestBody(sample.plan);
  return sample;
}

async function publish(backend, address) {
  const secret = randomHex(32);
  const id = randomHex(8);
  await backend.db().prepare(
    `INSERT INTO publish_credentials (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, NULL, NULL)`,
  ).bind(id, await sha256Hex(secret), "tenant-wc", address.toLowerCase()).run();
  const created = await backend.fetchApi("/api/bot/tasks", {
    method: "POST",
    headers: { authorization: `Bearer wgpub_${id}.${secret}`, "content-type": "application/json" },
    body: JSON.stringify({ expires_at: Math.floor(Date.now() / 1000) + 3600, task: await forgedReadyTask(address) }),
  });
  const body = await created.json();
  assert.equal(created.status, 201, JSON.stringify(body));
  return body.ref;
}

/** Browser context serving `dir` with its _headers, the real API, an injected wallet, and recorders. */
async function openSite(t, dir, { backend, key, address, relay }) {
  const chrome = chromePath();
  assert.ok(chrome, "needs Chrome/Chromium (set WG_CHROME)");
  const { chromium } = await import("playwright-core");
  const headers = pagesHeadersFor(dir);
  const rec = { requests: [], offsite: [], sockets: [], apiBodies: [], injected: [], console: [] };
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  t.after(() => browser.close());
  const context = await browser.newContext({ serviceWorkers: "block" });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    rec.requests.push(url.href);
    if (url.origin !== ORIGIN) {
      rec.offsite.push(url.href);
      // The SDK's Verify attestation iframe: answered with an empty document (no attestation).
      if (url.origin === "https://verify.walletconnect.org") {
        return route.fulfill({ status: 200, headers: { "content-type": "text/html" }, body: "<!doctype html><title>verify stub</title>" });
      }
      return route.abort();
    }
    if (url.pathname.startsWith("/api/")) {
      const forwarded = { ...request.headers() };
      delete forwarded.cookie;
      rec.apiBodies.push(`${request.method()} ${url.pathname}${url.search} ${request.postData() ?? ""}`);
      const response = await backend.fetchApi(url.pathname + url.search, {
        method: request.method(), headers: forwarded, body: request.postData() ?? undefined,
      });
      return route.fulfill({
        status: response.status,
        headers: { "content-type": response.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
        body: await response.text(),
      });
    }
    const file = url.pathname === "/" || url.pathname.startsWith("/t/") ? "index.html" : url.pathname.slice(1);
    const path = join(dir, file);
    if (!path.startsWith(dir) || !existsSync(path)) return route.fulfill({ status: 404, headers, body: "not found" });
    return route.fulfill({ status: 200, headers: { ...headers, "content-type": TYPES[extname(file)] ?? "application/octet-stream" }, body: readFileSync(path) });
  });
  let socketSeq = 0;
  await context.routeWebSocket(/^wss?:\/\//, (ws) => {
    rec.sockets.push(ws.url());
    if (!relay || !ws.url().startsWith("wss://relay.walletconnect.org/")) { ws.close({ code: 1008, reason: "not allowed in test" }); return; }
    const conn = relay.attach((text) => ws.send(text), `page-${++socketSeq}`);
    ws.onMessage((message) => conn.receive(String(message)));
    ws.onClose(() => conn.close());
  });
  await context.exposeBinding("__wgWallet", (_source, method, params) => {
    rec.injected.push(method);
    if (SEND_METHODS.has(method)) return { error: "send-attempt-recorded" };
    if (method === "eth_requestAccounts" || method === "eth_accounts") return { result: [address] };
    if (method === "eth_chainId") return { result: "0x38" };
    if (method === "wallet_getCapabilities") return { result: {} };
    if (method === "personal_sign") {
      const payload = String(params?.[0] ?? "");
      if (!payload.includes(LOGIN_STATEMENT)) return { error: "non-login personal_sign refused" };
      return { result: walletSign(payload, key) };
    }
    return { error: `unsupported ${method}` };
  });
  await context.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    if (window.top !== window) return;
    const provider = {
      on() {}, removeListener() {},
      async request({ method, params }) {
        const reply = await window.__wgWallet(method, params ?? []);
        if (reply && "result" in reply) return reply.result;
        const error = new Error(reply?.error ?? "rejected");
        error.code = 4001;
        throw error;
      },
    };
    window.ethereum = provider;
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
    window.addEventListener("eip6963:requestProvider", () => {
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: Object.freeze({
        info: Object.freeze({ uuid: "00000000-0000-4000-8000-000000000002", name: "Injected Wallet", rdns: "io.example.wallet", icon: png }),
        provider,
      }) }));
    });
  });
  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.accept());
  page.on("console", (message) => rec.console.push(message.text()));
  return { browser, context, page, rec, headers };
}

async function forceConfirm(page) {
  await page.waitForFunction((label) => [...document.querySelectorAll("button")].some((b) => b.textContent === label), CONFIRM, { timeout: 20000 });
  await page.evaluate((label) => {
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent === label);
    button.disabled = false;
    button.removeAttribute("disabled");
    for (let i = 0; i < 3; i += 1) button.click();
  }, CONFIRM);
  await page.waitForTimeout(1500);
  assert.equal(await page.evaluate(() => document.querySelector("#verify-send")?.dataset.state), "closed");
}

const wcAssets = (dir) => files(dir).filter((f) => /walletconnect|appkit/i.test(f));
const jsText = (dir) => files(dir).filter((f) => f.endsWith(".js")).map((f) => readFileSync(join(dir, f), "utf8")).join("\n");

test("WC-ARTIFACT-01/02 off: no build settings → no WalletConnect code/option/endpoints, strict CSP enforced, injected login works, forced confirm = 0 tx", async (t) => {
  const dir = await buildVariant(false);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  assert.deepEqual(wcAssets(dir), [], "no WalletConnect chunk in the off build");
  const js = jsText(dir);
  for (const needle of ["relay.walletconnect", "verify.walletconnect", "pulse.walletconnect", "app.binance.com", "display_uri", TEST_PROJECT_ID, PRODUCTION_PROJECT_ID]) {
    assert.equal(js.includes(needle), false, `off bundle contains ${needle}`);
  }
  const backend = await realBackend();
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const ref = await publish(backend, address);
  const { page, rec, headers } = await openSite(t, dir, { backend, key, address, relay: null });
  const response = await page.goto(`${ORIGIN}/t/${ref}`, { waitUntil: "domcontentloaded" });
  const csp = response.headers()["content-security-policy"];
  assert.equal(csp, contentSecurityPolicy({ walletConnect: false }));
  assert.equal(csp, headers["content-security-policy"]);
  assert.match(csp, /frame-src 'none'/);
  assert.equal(/\*|unsafe-eval|unsafe-inline|walletconnect/.test(csp), false);
  await page.waitForFunction(() => document.querySelectorAll("#wallet-list button").length === 1, null, { timeout: 15000 });
  assert.equal(await page.locator("#wallet-list button[data-source=walletconnect]").count(), 0, "WalletConnect option hidden");
  await page.click("#login");
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 20000 });
  assert.equal(backend.seen.find((r) => r.path === "/api/auth/verify")?.status, 200, "fixed SIWE positive path");
  await forceConfirm(page);
  assert.deepEqual(rec.injected.filter((m) => SEND_METHODS.has(m)), [], "zero transaction requests");
  assert.deepEqual(rec.sockets, [], "no WebSocket at all");
  assert.deepEqual([...new Set(rec.offsite.map((u) => new URL(u).hostname))].filter((h) => h !== "bsc-dataseed.bnbchain.org"), []);
  assert.deepEqual(await page.evaluate(() => window.__csp), [], "no CSP violations");
  t.diagnostic(`off: assets=${files(dir).length} injected=${JSON.stringify(rec.injected)} offsite=${JSON.stringify([...new Set(rec.offsite)])}`);
});

test("WC-ARTIFACT-01/02 on: relay untouched until WalletConnect is chosen; QR/links → test wallet approves → fixed SIWE; 0 tx in both modes; CSP/endpoints/cleanup", async (t) => {
  const dir = await buildVariant(true);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const chunks = wcAssets(dir);
  assert.equal(chunks.filter((f) => /walletconnect-sdk/.test(f)).length, 1, `one lazily loaded SDK chunk: ${chunks}`);
  const js = jsText(dir);
  assert.equal(js.includes(TEST_PROJECT_ID), true, "project id is injected from the build env");
  assert.equal(js.includes(PRODUCTION_PROJECT_ID), false);
  for (const needle of ["api.web3modal.org", "pulse.walletconnect.org/e"]) {
    // Present-in-dead-code is tolerated only if never contacted; the network assertions below decide.
    t.diagnostic(`bundle mentions ${needle}: ${js.includes(needle)}`);
  }

  const backend = await realBackend();
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const ref = await publish(backend, address);
  const relay = new MockRelay();
  const relayServer = await startRelayServer(relay);
  t.after(() => relayServer.close());
  const wallet = await createTestWallet(relayServer.url, { key, address });
  t.after(() => wallet.close());
  const { page, rec, headers } = await openSite(t, dir, { backend, key, address, relay });

  const response = await page.goto(`${ORIGIN}/t/${ref}`, { waitUntil: "domcontentloaded" });
  const csp = response.headers()["content-security-policy"];
  assert.equal(csp, contentSecurityPolicy({ walletConnect: true }));
  assert.equal(csp, headers["content-security-policy"]);
  assert.match(csp, /connect-src 'self' https:\/\/bsc-dataseed\.bnbchain\.org wss:\/\/relay\.walletconnect\.org;/);
  assert.match(csp, /frame-src https:\/\/verify\.walletconnect\.org;/);
  assert.equal(/\*|unsafe-eval|unsafe-inline/.test(csp), false);
  await page.waitForFunction(() => document.querySelectorAll("#wallet-list button").length === 2, null, { timeout: 15000 });
  assert.match(await page.textContent("#wallet-list button[data-source=walletconnect]"), /WalletConnect/);
  await page.waitForTimeout(500);
  const sdkRequested = () => rec.requests.some((u) => /walletconnect-sdk|appkit/.test(u));
  assert.equal(sdkRequested(), false, "SDK chunk not fetched before WalletConnect is chosen");
  assert.deepEqual(rec.sockets, []);

  // Mode 1: injected wallet. WalletConnect offered but not chosen → never loaded, relay never contacted.
  await page.locator("#wallet-list button", { hasText: "Injected Wallet" }).click();
  await page.click("#login");
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 20000 });
  await forceConfirm(page);
  assert.deepEqual(rec.injected.filter((m) => SEND_METHODS.has(m)), [], "zero transaction requests (injected)");
  assert.equal(sdkRequested(), false);
  assert.deepEqual(rec.sockets, [], "未選 WC 不連 Relay");
  const injectedBefore = rec.injected.length;

  // Mode 2: WalletConnect. Switching revokes the injected D1 session first (W-A3).
  await page.locator("#wallet-list button[data-source=walletconnect]").click();
  await page.waitForFunction(() => document.querySelector("#logout")?.hidden === true, null, { timeout: 15000 });
  assert.match(await page.textContent("#banner"), /revoked=true/);
  // A switch is a logout (W-A3) and drops the private view; the user re-opens the task link.
  await page.goto(`${ORIGIN}/t/${ref}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll("#wallet-list button").length === 2, null, { timeout: 15000 });
  assert.equal(sdkRequested(), false, "still no SDK after the injected session");
  assert.deepEqual(rec.sockets, []);
  await page.locator("#wallet-list button[data-source=walletconnect]").click();
  await page.click("#login");
  try {
    await page.waitForSelector("#wc-panel a.wc-link-generic", { state: "attached", timeout: 30000 });
  } catch (error) {
    t.diagnostic(`banner=${await page.textContent("#banner")} panel=${await page.evaluate(() => document.querySelector("#wc-panel")?.outerHTML?.slice(0, 600))}`);
    t.diagnostic(`csp=${JSON.stringify(await page.evaluate(() => window.__csp))} sockets=${JSON.stringify(rec.sockets)} console=${JSON.stringify(rec.console.slice(-15))}`);
    t.diagnostic(`requests=${JSON.stringify(rec.requests.filter((u) => !u.includes("/api/")).slice(-10))} relay=${JSON.stringify(relay.log.slice(0, 10))}`);
    throw error;
  }
  assert.equal(sdkRequested(), true, "SDK chunk fetched only now");
  const links = await page.evaluate(() => [...document.querySelectorAll("#wc-panel a")].map((a) => ({ href: a.getAttribute("href"), target: a.getAttribute("target"), rel: a.getAttribute("rel") })));
  const uri = links.find((l) => l.href.startsWith("wc:")).href;
  const symKey = new URLSearchParams(uri.split("?")[1]).get("symKey");
  assert.match(symKey, /^[0-9a-f]{64}$/);
  assert.deepEqual(links.map((l) => l.href.split("?")[0]), ["https://app.binance.com/cedefi/wc", "bnc://app.binance.com/cedefi/wc", uri.split("?")[0]]);
  assert.equal(links[0].target, "_blank");
  assert.equal(await page.locator("#wc-panel canvas.wc-qr").count(), 1, "QR canvas drawn");
  assert.equal(rec.sockets.length >= 1 && rec.sockets.every((u) => u.startsWith("wss://relay.walletconnect.org/")), true, JSON.stringify(rec.sockets));
  assert.equal(new URL(rec.sockets[0]).searchParams.get("projectId"), TEST_PROJECT_ID);

  await wallet.pair(uri); // the "phone" scans the QR
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 30000 });
  assert.equal(backend.seen.filter((r) => r.path === "/api/auth/verify" && r.status === 200).length, 2, "fixed SIWE positive path over WalletConnect");
  const proposal = wallet.proposals[0];
  t.diagnostic(`actual proposal: required=${JSON.stringify(proposal.requiredNamespaces)} optional=${JSON.stringify(proposal.optionalNamespaces)}`);
  const scope = { ...proposal.requiredNamespaces, ...proposal.optionalNamespaces };
  assert.deepEqual(Object.keys(scope), ["eip155"]);
  assert.deepEqual(scope.eip155.chains, ["eip155:56"]);
  assert.deepEqual(scope.eip155.methods, ["personal_sign"]);
  assert.deepEqual(wallet.requests.map((r) => r.method), ["personal_sign"]);
  assert.equal(new TextDecoder().decode(wallet.signedBytes[0]).includes(LOGIN_STATEMENT), true);
  assert.deepEqual(await page.locator("#wc-panel a").count(), 0, "jump links (URI) removed once connected");

  await forceConfirm(page);
  assert.deepEqual(wallet.requests.map((r) => r.method), ["personal_sign"], "zero transaction requests (WalletConnect)");
  assert.equal(rec.injected.length, injectedBefore, "the injected wallet was not asked during the WalletConnect flow");

  // Sensitive-data cleanup: URI / pairing key only in the SDK's own storage (IndexedDB), never in the
  // DOM, page storage, API traffic, or console.
  const html = await page.content();
  assert.equal(html.includes(symKey) || html.includes("wc:"), false, "no URI in the DOM");
  const pageStorage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  assert.equal(pageStorage.includes(symKey) || pageStorage.includes(uri), false, `page storage: ${pageStorage.slice(0, 300)}`);
  assert.equal(rec.apiBodies.some((b) => b.includes(symKey) || b.includes("wc:")), false, "no URI/key sent to the backend");
  assert.equal(rec.console.some((c) => c.includes(symKey) || c.includes("wc:")), false, "no URI/key in console");
  const rows = JSON.stringify((await backend.db().prepare("SELECT * FROM sessions").all()).results);
  assert.equal(rows.includes(symKey), false);

  // Endpoints actually contacted.
  const hosts = [...new Set(rec.offsite.map((u) => new URL(u).hostname))];
  assert.deepEqual(hosts.filter((h) => !["bsc-dataseed.bnbchain.org", "verify.walletconnect.org"].includes(h)), [], JSON.stringify(hosts));
  assert.equal(rec.offsite.filter((u) => u.includes("verify.walletconnect.org")).every((u) => new URL(u).pathname === "/v3/attestation"), true);
  assert.equal([...rec.offsite, ...rec.sockets].some((u) => /pulse\.|rpc\.walletconnect|echo\.|web3modal|explorer-api/.test(u)), false);
  assert.deepEqual(await page.evaluate(() => window.__csp), [], "no CSP violations");

  // Reload: D1 session from the cookie; WalletConnect restore is not D1 login and nothing is re-signed or reconnected unasked.
  const socketsBefore = rec.sockets.length;
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 15000 });
  await page.waitForTimeout(1500);
  assert.equal(rec.sockets.length, socketsBefore, "reload does not reconnect the relay by itself");
  assert.deepEqual(wallet.requests.map((r) => r.method), ["personal_sign"], "reload never re-signs");
  assert.deepEqual(await page.evaluate(() => window.__csp), []);
  t.diagnostic(`on: offsite=${JSON.stringify([...new Set(rec.offsite)])} sockets=${JSON.stringify(rec.sockets.map((u) => u.split("?")[0]))} relayLog=${JSON.stringify([...new Set(relay.log.map((l) => l.method))])}`);
});

test("WC-ARTIFACT-02 source: the production project id is never hard-coded (only VITE_REOWN_PROJECT_ID)", () => {
  const roots = ["src", "public", "tests", "integration", "functions"].map((d) => join(CLIENT, d));
  const hits = [];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    for (const rel of files(root)) {
      const text = readFileSync(join(root, rel), "utf8");
      if (text.includes(PRODUCTION_PROJECT_ID)) hits.push(rel);
    }
  }
  for (const f of ["index.html", "vite.config.ts", "wrangler.jsonc", "package.json"]) {
    if (readFileSync(join(CLIENT, f), "utf8").includes(PRODUCTION_PROJECT_ID)) hits.push(f);
  }
  assert.deepEqual(hits, []);
});

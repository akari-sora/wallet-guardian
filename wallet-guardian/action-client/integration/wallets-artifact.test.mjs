/**
 * WALLET-CONNECT-01 W-A artifact check (test:wallets:artifact).
 *
 * Loads the BUILT dist/ in headless Chrome at the production origin (requests are intercepted, the
 * network is not used), with synthetic EIP-6963 wallets injected before page scripts. /api/* is
 * answered in-process by the real Pages Function handler (src/server/handle.mjs) over an in-memory
 * D1 stand-in. This is not `wrangler pages dev` (see test:runtime) and not a real wallet.
 *
 * Covers on the artifact: WA-01 (no account request during discovery), WA-02 (login only to the
 * picked provider), WA-04 (hostile name/icon inert), WA-05 (switch revokes the D1 session), WA-06
 * (backend verifies the signed bytes), and the no-send invariant with forced confirm clicks.
 * Needs: `npm run build` first and Chrome/Chromium (WG_CHROME or /opt/google/chrome/chrome).
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { secp256k1 } from "@noble/curves/secp256k1";
import { digestBody } from "../src/canonical.mjs";
import { sha256Hex, randomHex } from "../src/server/http.mjs";
import { checksumAddress } from "../src/siwe-message.mjs";
import { LOGIN_STATEMENT } from "../src/login-statement.mjs";
import { addressOf, realBackend, walletSign } from "../tests/wallet-fixtures.mjs";
import { productionViewOrigin } from "../src/view-origin.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const DIST = join(CLIENT, "dist");
const ORIGIN = productionViewOrigin();
const SEND_METHODS = new Set([
  "eth_sendTransaction", "eth_sendRawTransaction", "wallet_sendCalls", "eth_sign", "eth_signTransaction",
  "eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4", "wallet_grantPermissions",
]);
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };

function chromePath() {
  for (const candidate of [process.env.WG_CHROME, "/opt/google/chrome/chrome", "/usr/bin/chromium", "/usr/bin/google-chrome"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

function artifactHashes() {
  const out = {};
  const walk = (dir, prefix = "") => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(join(dir, entry.name), rel);
      else out[rel] = createHash("sha256").update(readFileSync(join(dir, entry.name))).digest("hex");
    }
  };
  walk(DIST);
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

test("WA artifact: built dist + two EIP-6963 wallets + hostile metadata → login only via the picked wallet, 0 send calls, switch revokes", async (t) => {
  assert.ok(existsSync(join(DIST, "index.html")), "run `npm run build` first");
  const chrome = chromePath();
  assert.ok(chrome, "needs Chrome/Chromium (set WG_CHROME)");
  const { chromium } = await import("playwright-core");
  const hashes = artifactHashes();
  t.diagnostic(`dist sha256: ${JSON.stringify(hashes)}`);

  const backend = await realBackend();
  const binanceKey = secp256k1.utils.randomPrivateKey();
  const otherKey = secp256k1.utils.randomPrivateKey();
  const binanceAddress = checksumAddress(addressOf(binanceKey));
  const otherAddress = checksumAddress(addressOf(otherKey));
  const secret = randomHex(32);
  const id = randomHex(8);
  await backend.db().prepare(
    `INSERT INTO publish_credentials (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, NULL, NULL)`,
  ).bind(id, await sha256Hex(secret), "tenant-wallets", binanceAddress.toLowerCase()).run();
  const created = await backend.fetchApi("/api/bot/tasks", {
    method: "POST",
    headers: { authorization: `Bearer wgpub_${id}.${secret}`, "content-type": "application/json" },
    body: JSON.stringify({ expires_at: Math.floor(Date.now() / 1000) + 3600, task: await forgedReadyTask(binanceAddress) }),
  });
  const createdBody = await created.json();
  assert.equal(created.status, 201, JSON.stringify(createdBody));
  const ref = createdBody.ref;

  const calls = [];
  const offsite = [];
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  t.after(() => browser.close());
  const context = await browser.newContext({ serviceWorkers: "block" });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) {
      offsite.push(url.hostname);
      return route.abort();
    }
    if (url.pathname.startsWith("/api/")) {
      const headers = { ...request.headers() };
      delete headers.cookie;
      const response = await backend.fetchApi(url.pathname + url.search, {
        method: request.method(), headers, body: request.postData() ?? undefined,
      });
      return route.fulfill({
        status: response.status,
        headers: { "content-type": response.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
        body: await response.text(),
      });
    }
    let file = url.pathname === "/" || url.pathname.startsWith("/t/") ? "index.html" : url.pathname.slice(1);
    const path = join(DIST, file);
    if (!path.startsWith(DIST) || !existsSync(path)) return route.fulfill({ status: 404, body: "not found" });
    return route.fulfill({ status: 200, headers: { "content-type": TYPES[extname(file)] ?? "application/octet-stream" }, body: readFileSync(path) });
  });
  await context.exposeBinding("__wgWallet", (_source, label, method, params) => {
    calls.push({ label, method });
    if (SEND_METHODS.has(method)) return { error: "send-attempt-recorded" };
    const address = label === "binance" ? binanceAddress : otherAddress;
    const key = label === "binance" ? binanceKey : otherKey;
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
    const make = (label) => ({
      on() {}, removeListener() {},
      async request({ method, params }) {
        const reply = await window.__wgWallet(label, method, params ?? []);
        if (reply && "result" in reply) return reply.result;
        const error = new Error(reply?.error ?? "rejected");
        error.code = 4001;
        throw error;
      },
    });
    const binance = make("binance");
    const other = make("other");
    const hostile = make("hostile");
    window.binancew3w = { ethereum: binance };
    window.ethereum = other; // another extension took window.ethereum
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
    const announce = () => {
      for (const [provider, info] of [
        [other, { uuid: "00000000-0000-4000-8000-000000000002", name: "Other Wallet", rdns: "io.example.wallet", icon: png }],
        [binance, { uuid: "00000000-0000-4000-8000-000000000001", name: "Binance Wallet", rdns: "com.binance.wallet", icon: png }],
        [hostile, { uuid: "00000000-0000-4000-8000-000000000003", name: "<img src=x onerror=\"window.__pwned=1\">Evil",
          rdns: "com.binance.wallet", icon: "data:image/svg+xml;base64," + btoa("<svg xmlns='http://www.w3.org/2000/svg' onload='window.__pwned=2'><script>window.__pwned=3</script></svg>") }],
      ]) {
        window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: Object.freeze({ info: Object.freeze(info), provider }) }));
      }
    };
    window.addEventListener("eip6963:requestProvider", announce);
  });

  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(`${ORIGIN}/t/${ref}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelectorAll("#wallet-list button").length === 3, null, { timeout: 15000 });
  const picker = await page.evaluate(() => [...document.querySelectorAll("#wallet-list button")].map((b) => ({
    name: b.querySelector(".wallet-name")?.textContent,
    nameElements: b.querySelector(".wallet-name")?.childElementCount,
    handlers: b.querySelectorAll("[onerror],[onload]").length,
    imgs: [...b.querySelectorAll("img")].map((img) => img.getAttribute("src")?.slice(0, 26)),
  })));
  assert.deepEqual(picker.map((p) => p.name), ["Other Wallet", "Binance Wallet", "<img src=x onerror=\"window.__pwned=1\">Evil"]);
  assert.equal(picker.every((p) => p.nameElements === 0 && p.handlers === 0), true, "hostile name is text, not markup");
  assert.equal(picker.every((p) => p.imgs.every((src) => src.startsWith("data:image/"))), true, "icons only as data:image <img>");
  t.diagnostic(`picker: ${JSON.stringify(picker)}`);
  await page.waitForTimeout(500);
  assert.deepEqual(calls, [], "discovery asked no wallet for anything");

  // Login without a choice asks nobody.
  await page.click("#login");
  await page.waitForTimeout(300);
  assert.deepEqual(calls, []);
  assert.match(await page.textContent("#banner"), /請先選擇/);

  // Pick Binance, then log in.
  await page.locator("#wallet-list button", { hasText: "Binance Wallet" }).click();
  assert.deepEqual(calls, [], "selection alone asks for nothing");
  await page.click("#login");
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 20000 });
  await page.waitForFunction(() => [...document.querySelectorAll("button")].some((b) => b.textContent === "在我的錢包確認"), null, { timeout: 20000 });
  assert.equal(calls.every((c) => c.label === "binance"), true, `calls went elsewhere: ${JSON.stringify(calls)}`);
  assert.equal(calls[0].method, "eth_requestAccounts");
  assert.equal(calls.filter((c) => c.method === "personal_sign").length, 1);
  assert.equal(backend.seen.find((r) => r.path === "/api/auth/verify")?.status, 200, "backend verified the signed bytes");

  // Forced confirm clicks: still zero transaction requests.
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent === "在我的錢包確認");
    button.disabled = false;
    button.removeAttribute("disabled");
    for (let i = 0; i < 3; i += 1) button.click();
  });
  await page.waitForTimeout(2000);
  assert.deepEqual(calls.filter((c) => SEND_METHODS.has(c.method)), []);
  assert.equal(await page.evaluate(() => document.querySelector("#verify-send")?.dataset.state), "closed");
  assert.equal(await page.evaluate(() => window.__pwned), undefined, "hostile metadata never executed");

  // Switch to the other wallet: the D1 session is revoked on the server.
  await page.locator("#wallet-list button", { hasText: "Other Wallet" }).click();
  await page.waitForFunction(() => document.querySelector("#logout")?.hidden === true, null, { timeout: 15000 });
  assert.match(await page.textContent("#banner"), /revoked=true/);
  const rows = await backend.db().prepare("SELECT address, revoked_at FROM sessions").all();
  assert.equal(rows.results.length, 1);
  assert.notEqual(rows.results[0].revoked_at, null, "old session row revoked");
  assert.equal((await page.textContent("#view")).includes("Lista"), false);
  assert.equal(calls.filter((c) => c.label !== "binance").length, 0, "switching asks the new wallet for nothing");
  assert.equal(calls.some((c) => c.label === "hostile"), false);
  t.diagnostic(`offsite requests aborted: ${JSON.stringify([...new Set(offsite)])}`);
  t.diagnostic(`wallet calls: ${JSON.stringify(calls)}`);
});

/**
 * D1-01..D1-09 against the real local runtime: `wrangler pages dev dist` (Pages Functions in workerd)
 * plus a fresh local D1 (--persist-to). Not the Node SQLite stand-in used by test:api.
 *
 * Requests keep the production AUTH audience: curl --connect-to (and Chrome host-resolver-rules) send
 * https://wallet-guardian-action-client.pages.dev to 127.0.0.1. --insecure / ignore-certificate-errors is
 * only used together with that explicit loopback redirect for the local dev certificate.
 *
 * Needs: `npm run build` first, curl, and a Chrome/Chromium for D1-09 (WG_CHROME or /opt/google/chrome/chrome).
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";
import { digestBody } from "../src/canonical.mjs";
import { LOGIN_STATEMENT } from "../src/login-statement.mjs";
import { acceptLoginChallenge } from "../src/siwe-gate.mjs";
import { checksumAddress } from "../src/siwe-message.mjs";
import { buildTestTask } from "../tools/lib/test-task.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const HOST = "wallet-guardian-action-client.pages.dev";
const ORIGIN = `https://${HOST}`;
const PORT = Number(process.env.WG_RUNTIME_PORT ?? 8788);
const TABLES = ["challenges", "sessions", "tenant_members", "publish_credentials", "tasks", "task_acl", "rate_buckets"];
const SEND_METHODS = new Set([
  "eth_sendTransaction",
  "eth_sendRawTransaction",
  "wallet_sendCalls",
  "eth_sign",
  "eth_signTransaction",
  "eth_signTypedData",
  "eth_signTypedData_v3",
  "eth_signTypedData_v4",
]);
const WRANGLER_ENV = { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "1" };

const state = { persistTo: null, storeDir: null, child: null, log: "", bodyFile: null, firstApply: "" };

function hex(bytes) {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function addressOf(key) {
  return `0x${hex(keccak_256(secp256k1.getPublicKey(key, false).slice(1)).slice(-20))}`;
}

function signMessage(message, key) {
  const payload = new TextEncoder().encode(message);
  const prefix = new TextEncoder().encode(`\u0019Ethereum Signed Message:\n${payload.length}`);
  const bytes = new Uint8Array(prefix.length + payload.length);
  bytes.set(prefix, 0);
  bytes.set(payload, prefix.length);
  const signature = secp256k1.sign(keccak_256(bytes), key);
  const out = new Uint8Array(65);
  out.set(signature.toCompactRawBytes(), 0);
  out[64] = signature.recovery + 27;
  return `0x${hex(out)}`;
}

function sha256Hex(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

function curl(method, path, options = {}) {
  const args = [
    "--silent", "--show-error", "--max-time", "20",
    "--insecure", "--connect-to", `${HOST}:443:127.0.0.1:${PORT}`,
    "-X", method, "-D", "-", "-o", state.bodyFile,
  ];
  const headers = { "cf-connecting-ip": options.ip ?? "203.0.113.50", ...(options.headers ?? {}) };
  if (options.cookie) headers.cookie = options.cookie;
  if (options.csrf) headers["x-csrf-token"] = options.csrf;
  if (options.auth) headers.authorization = options.auth;
  if (options.json !== undefined) {
    headers["content-type"] = "application/json";
    if (options.origin !== false) headers.origin = ORIGIN;
    args.push("--data-binary", JSON.stringify(options.json));
  }
  for (const [name, value] of Object.entries(headers)) args.push("-H", `${name}: ${value}`);
  args.push(`${ORIGIN}${path}`);
  const run = spawnSync("curl", args, { encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
  if (run.status !== 0) throw new Error(`curl failed: ${run.stderr}`);
  const lines = run.stdout.split(/\r?\n/);
  const status = Number(/^HTTP\/\S+\s+(\d+)/.exec(lines[0])?.[1]);
  const headerMap = new Map();
  const setCookies = [];
  for (const line of lines.slice(1)) {
    const index = line.indexOf(":");
    if (index === -1) continue;
    const name = line.slice(0, index).trim().toLowerCase();
    const value = line.slice(index + 1).trim();
    if (name === "set-cookie") setCookies.push(value);
    headerMap.set(name, value);
  }
  const text = readFileSync(state.bodyFile, "utf8");
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = null;
  }
  return { status, headers: headerMap, setCookies, text, body };
}

function cookieFrom(result, name) {
  const found = result.setCookies.find((item) => item.startsWith(`${name}=`));
  return found ? found.split(";")[0] : "";
}

function assertJsonNoStore(result) {
  assert.match(result.headers.get("cache-control") ?? "", /no-store/i, `cache-control for ${result.text.slice(0, 80)}`);
  assert.match(result.headers.get("content-type") ?? "", /application\/json/);
  assert.equal(result.text.trimStart().startsWith("<"), false);
  assert.equal(typeof result.body, "object");
}

function wrangler(args) {
  const run = spawnSync("npx", ["--no-install", "wrangler", ...args], {
    cwd: CLIENT, encoding: "utf8", env: WRANGLER_ENV, maxBuffer: 8 * 1024 * 1024,
  });
  if (run.status !== 0) throw new Error(`wrangler ${args.slice(0, 4).join(" ")} failed: ${(run.stderr || run.stdout).slice(-800)}`);
  return `${run.stdout}\n${run.stderr}`;
}

function d1(sql) {
  const run = spawnSync(
    "npx",
    ["--no-install", "wrangler", "d1", "execute", "DB", "--local", "--persist-to", state.persistTo, "--json", "--command", sql],
    { cwd: CLIENT, encoding: "utf8", env: WRANGLER_ENV, maxBuffer: 8 * 1024 * 1024 },
  );
  if (run.status !== 0) throw new Error(`d1 failed: ${(run.stderr || run.stdout).slice(-600)}`);
  const parsed = JSON.parse(run.stdout.slice(run.stdout.indexOf("[")));
  return parsed.flatMap((item) => item.results ?? []);
}

function node(script, args) {
  const run = spawnSync(process.execPath, [join(CLIENT, script), ...args], {
    cwd: CLIENT, encoding: "utf8", env: WRANGLER_ENV, maxBuffer: 8 * 1024 * 1024,
  });
  let body = null;
  try {
    body = JSON.parse(run.stdout);
  } catch {
    body = null;
  }
  return { status: run.status, body, stdout: run.stdout, stderr: run.stderr };
}

function insertCredential(wallet, tenant, { expiresAt = null, revokedAt = null } = {}) {
  const id = randomBytes(8).toString("hex");
  const secret = randomBytes(32).toString("hex");
  d1(
    `INSERT INTO publish_credentials (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at) VALUES ('${id}', '${sha256Hex(secret)}', '${tenant}', '${wallet.toLowerCase()}', ${expiresAt ?? "NULL"}, ${revokedAt ?? "NULL"})`,
  );
  return { id, token: `wgpub_${id}.${secret}` };
}

async function publishTask(token, key, extra = {}, task = null) {
  const body = task ?? (await buildTestTask(addressOf(key), { label: "D1-RUNTIME" }));
  return curl("POST", "/api/bot/tasks", {
    origin: false,
    auth: `Bearer ${token}`,
    json: { expires_at: Math.floor(Date.now() / 1000) + 3600, task: body, ...extra },
  });
}

function login(key, ip) {
  const address = checksumAddress(addressOf(key));
  const challenge = curl("POST", "/api/auth/challenge", { json: { address }, ip });
  assert.equal(challenge.status, 200, challenge.text);
  assert.equal(typeof acceptLoginChallenge(challenge.body, address, Date.now()), "string");
  const attempt = cookieFrom(challenge, "__Host-wg_attempt");
  assert.match(attempt, /^__Host-wg_attempt=[a-f0-9]{64}$/);
  const verified = curl("POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature: signMessage(challenge.body.message, key) },
    cookie: attempt, csrf: challenge.body.csrf_token, ip,
  });
  assert.equal(verified.status, 200, verified.text);
  assert.equal(verified.body.execution_authorized, false);
  assert.equal(verified.body.send_authorized, false);
  const session = cookieFrom(verified, "__Host-wg_session");
  assert.match(session, /^__Host-wg_session=[a-f0-9]{64}$/);
  return { address, cookie: session, csrf: verified.body.csrf_token };
}

async function waitReady(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    if (state.child.exitCode !== null) throw new Error(`pages dev exited: ${state.log.slice(-800)}`);
    try {
      const probe = curl("GET", "/api/session");
      if (probe.status === 200 && probe.body?.authenticated === false) return;
      last = `${probe.status} ${probe.text.slice(0, 120)}`;
    } catch (error) {
      last = String(error.message).slice(0, 200);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`pages dev not ready: ${last}\n${state.log.slice(-800)}`);
}

test.before(async () => {
  assert.equal(existsSync(join(CLIENT, "dist/index.html")), true, "run npm run build first");
  state.persistTo = mkdtempSync(join(tmpdir(), "wg-d1-rt-"));
  state.storeDir = mkdtempSync(join(tmpdir(), "wg-pub-"));
  chmodSync(state.storeDir, 0o700);
  state.bodyFile = join(state.persistTo, "body.json");
  state.firstApply = wrangler(["d1", "migrations", "apply", "DB", "--local", "--persist-to", state.persistTo]);
  state.child = spawn(
    "npx",
    [
      "--no-install", "wrangler", "pages", "dev", "dist",
      "--ip", "127.0.0.1", "--port", String(PORT), "--local-protocol", "https",
      "--persist-to", state.persistTo, "--show-interactive-dev-session=false",
    ],
    { cwd: CLIENT, env: WRANGLER_ENV, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );
  state.child.stdout.on("data", (chunk) => { state.log += chunk; });
  state.child.stderr.on("data", (chunk) => { state.log += chunk; });
  await waitReady();
});

test.after(async () => {
  if (state.child && state.child.exitCode === null) {
    // npx -> wrangler -> workerd: signal the whole process group, then force it.
    const exited = new Promise((resolve) => state.child.once("exit", resolve));
    try { process.kill(-state.child.pid, "SIGTERM"); } catch {}
    const timer = setTimeout(() => { try { process.kill(-state.child.pid, "SIGKILL"); } catch {} }, 5000);
    await exited;
    clearTimeout(timer);
  }
  state.child?.stdout?.destroy();
  state.child?.stderr?.destroy();
  if (process.env.WG_RUNTIME_LOG) writeFileSync(process.env.WG_RUNTIME_LOG, state.log.replace(/wgpub_[a-f0-9.]+/g, "wgpub_<redacted>"));
  rmSync(state.persistTo, { recursive: true, force: true });
  rmSync(state.storeDir, { recursive: true, force: true });
});

test("D1-01 migration: empty DB migrates; second apply has nothing pending and keeps data", async () => {
  assert.match(state.firstApply, /0001_p1_init\.sql/);
  const tables = d1("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").map((row) => row.name);
  for (const name of TABLES) assert.equal(tables.includes(name), true, `missing ${name}`);
  assert.equal(tables.includes("d1_migrations"), true);
  d1("INSERT INTO tenant_members (tenant_id, wallet_address) VALUES ('keep-me', '0x0000000000000000000000000000000000000001')");
  const second = wrangler(["d1", "migrations", "apply", "DB", "--local", "--persist-to", state.persistTo]);
  assert.match(second, /No migrations to apply/);
  const kept = d1("SELECT COUNT(*) AS n FROM tenant_members WHERE tenant_id = 'keep-me'")[0].n;
  assert.equal(Number(kept), 1);
  assert.equal(curl("GET", "/api/session").status, 200);
});

test("D1-02 login: synthetic SIWE creates a session; replay and concurrent verify succeed at most once", async () => {
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const ip = "203.0.113.10";
  const challenge = curl("POST", "/api/auth/challenge", { json: { address }, ip });
  assert.equal(challenge.status, 200, challenge.text);
  const attempt = cookieFrom(challenge, "__Host-wg_attempt");
  const signature = signMessage(challenge.body.message, key);
  const verifyArgs = (cookie) => {
    const out = join(state.persistTo, `v-${randomBytes(4).toString("hex")}.json`);
    return [
      "--silent", "--insecure", "--connect-to", `${HOST}:443:127.0.0.1:${PORT}`, "-o", out, "-w", "%{http_code}",
      "-X", "POST", "-H", "content-type: application/json", "-H", `origin: ${ORIGIN}`, "-H", `cf-connecting-ip: ${ip}`,
      "-H", `cookie: ${cookie}`, "-H", `x-csrf-token: ${challenge.body.csrf_token}`,
      "--data-binary", JSON.stringify({ message: challenge.body.message, signature }), `${ORIGIN}/api/auth/verify`,
    ];
  };
  // Truly concurrent: two curl processes started together.
  const run = (args) => new Promise((resolve) => {
    const child = spawn("curl", args);
    let out = "";
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.on("exit", () => resolve(Number(out)));
  });
  const statuses = (await Promise.all([run(verifyArgs(attempt)), run(verifyArgs(attempt)), run(verifyArgs(attempt))])).sort();
  assert.deepEqual(statuses, [200, 401, 401]);
  const sessions = d1(`SELECT COUNT(*) AS n FROM sessions WHERE address = '${address.toLowerCase()}'`)[0].n;
  assert.equal(Number(sessions), 1);
  const replay = curl("POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature }, cookie: attempt, csrf: challenge.body.csrf_token, ip,
  });
  assert.equal(replay.status, 401);
  assert.equal(replay.body.error, "nonce_replay");
  const fresh = login(key, ip);
  const me = curl("GET", "/api/session", { cookie: fresh.cookie });
  assert.equal(me.body.authenticated, true);
  assert.equal(me.body.address, address);
  assert.equal(me.body.execution_authorized, false);
});

test("D1-03 isolation: A cannot read B; unknown and unauthorized refs leak nothing", async () => {
  const keyA = secp256k1.utils.randomPrivateKey();
  const keyB = secp256k1.utils.randomPrivateKey();
  const credA = insertCredential(addressOf(keyA), "tenant-a");
  const credB = insertCredential(addressOf(keyB), "tenant-b");
  const createdA = await publishTask(credA.token, keyA);
  const createdB = await publishTask(credB.token, keyB);
  assert.equal(createdA.status, 201, createdA.text);
  assert.equal(createdB.status, 201, createdB.text);
  assert.equal(createdA.body.view_url, `${ORIGIN}/t/${createdA.body.ref}`);
  const sessionA = login(keyA, "203.0.113.11");
  const own = curl("GET", `/api/tasks/${createdA.body.ref}`, { cookie: sessionA.cookie });
  assert.equal(own.status, 200, own.text);
  assert.equal(own.body.task.test_task.executable, false);
  assert.equal(own.body.task.link.purpose, "private_task_view");
  assert.equal(own.body.task.evidence.fixed_https_origin, false);
  const cross = curl("GET", `/api/tasks/${createdB.body.ref}`, { cookie: sessionA.cookie });
  const unknown = curl("GET", `/api/tasks/ref_${"f".repeat(32)}`, { cookie: sessionA.cookie });
  for (const denied of [cross, unknown]) {
    assert.equal(denied.status, 404);
    assert.deepEqual(Object.keys(denied.body).sort(), ["error", "execution_authorized", "send_authorized"]);
    assert.equal(denied.text.includes("plan"), false);
    assertJsonNoStore(denied);
  }
  const forgedQuery = curl("GET", `/api/tasks/${createdB.body.ref}?tenant_id=tenant-b`, {
    cookie: sessionA.cookie, headers: { "x-tenant-id": "tenant-b" },
  });
  assert.equal(forgedQuery.status, 404);
});

test("D1-04 revoke: logout revokes server-side; replaying the old cookie gets 401", async () => {
  const key = secp256k1.utils.randomPrivateKey();
  const cred = insertCredential(addressOf(key), "tenant-r");
  const created = await publishTask(cred.token, key);
  const session = login(key, "203.0.113.12");
  assert.equal(curl("GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie }).status, 200);
  const logout = curl("POST", "/api/auth/logout", { json: {}, cookie: session.cookie, csrf: session.csrf, ip: "203.0.113.12" });
  assert.equal(logout.status, 200, logout.text);
  assert.equal(logout.body.revoked, true);
  const after = curl("GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(after.status, 401);
  assert.equal(after.body.error, "unauthenticated");
  assert.equal(curl("GET", "/api/session", { cookie: session.cookie }).body.authenticated, false);
});

test("D1-05 lifecycle: cancel then GET 410; expired 410; wrong revision 409", async () => {
  const key = secp256k1.utils.randomPrivateKey();
  const cred = insertCredential(addressOf(key), "tenant-l");
  const created = await publishTask(cred.token, key);
  const session = login(key, "203.0.113.13");
  const stale = curl("GET", `/api/tasks/${created.body.ref}?revision=7`, { cookie: session.cookie });
  assert.equal(stale.status, 409);
  const wrong = curl("POST", `/api/tasks/${created.body.ref}/cancel`, { json: { revision: 99 }, cookie: session.cookie, csrf: session.csrf });
  assert.equal(wrong.status, 409);
  assert.equal(wrong.body.error, "revision_mismatch");
  const noCsrf = curl("POST", `/api/tasks/${created.body.ref}/cancel`, { json: { revision: 1 }, cookie: session.cookie });
  assert.equal(noCsrf.status, 403);
  const cancel = curl("POST", `/api/tasks/${created.body.ref}/cancel`, { json: { revision: 1 }, cookie: session.cookie, csrf: session.csrf });
  assert.equal(cancel.status, 200, cancel.text);
  assert.equal(cancel.body.status, "cancelled");
  assert.equal(cancel.body.on_chain_revoked, false);
  const gone = curl("GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(gone.status, 410);
  assert.equal(gone.body.error, "cancelled");
  assert.equal(gone.body.task, undefined);
  const short = await publishTask(cred.token, key);
  assert.equal(short.status, 201);
  d1(`UPDATE tasks SET expires_at = ${Math.floor(Date.now() / 1000) - 5} WHERE ref = '${short.body.ref}'`);
  const expired = curl("GET", `/api/tasks/${short.body.ref}`, { cookie: session.cookie });
  assert.equal(expired.status, 410);
  assert.equal(expired.body.error, "expired");
});

test("D1-06 publisher: bootstrap + publish tools; tenant/wallet from credential; forged, expired, revoked refused", async () => {
  const key = secp256k1.utils.randomPrivateKey();
  const wallet = checksumAddress(addressOf(key));
  const common = ["--target", "local", "--persist-to", state.persistTo, "--store-dir", state.storeDir, "--wallet", wallet];
  const issued = node("tools/bootstrap-publisher.mjs", common);
  assert.equal(issued.status, 0, issued.stderr);
  assert.equal(issued.body.results[0].status, "issued");
  assert.equal(issued.body.results[0].readback_match, true);
  assert.equal(issued.stdout.includes("wgpub_"), false);
  const storeFile = issued.body.results[0].store_file;
  assert.equal((readFileSync(storeFile).length > 0), true);
  assert.equal(spawnSync("stat", ["-c", "%a", storeFile], { encoding: "utf8" }).stdout.trim(), "600");
  const again = node("tools/bootstrap-publisher.mjs", common);
  assert.equal(again.status, 0, again.stderr);
  assert.equal(again.body.results[0].status, "ok_existing");
  const rows = d1(`SELECT COUNT(*) AS n FROM publish_credentials WHERE wallet_address = '${wallet.toLowerCase()}'`)[0].n;
  assert.equal(Number(rows), 1, "re-run must not mint another token");
  const record = JSON.parse(readFileSync(storeFile, "utf8"));
  const dbRow = d1(`SELECT secret_hash, tenant_id, expires_at FROM publish_credentials WHERE credential_id = '${record.credential_id}'`)[0];
  assert.equal(dbRow.secret_hash, sha256Hex(record.token.split(".")[1]));
  assert.equal(JSON.stringify(dbRow).includes(record.token.split(".")[1]), false, "plaintext secret must not be in D1");
  assert.equal(Number(dbRow.expires_at) - record.created_at, 86400);

  const published = node("tools/publish-private-task.mjs", [
    "--credential-file", storeFile, "--api-base", `https://127.0.0.1:${PORT}`, "--insecure-local",
  ]);
  assert.equal(published.status, 0, published.stdout + published.stderr);
  assert.equal(published.body.status, 201);
  assert.equal(published.body.view_url, `${ORIGIN}/t/${published.body.ref}`);
  assert.equal(published.stdout.includes("wgpub_"), false);
  const taskRow = d1(`SELECT tenant_id, wallet_address FROM tasks WHERE ref = '${published.body.ref}'`)[0];
  assert.equal(taskRow.tenant_id, record.tenant_id);
  assert.equal(taskRow.wallet_address, wallet.toLowerCase());

  const forgedTenant = await publishTask(record.token, key, { tenant_id: "tenant-evil" });
  assert.equal(forgedTenant.status, 403);
  assert.equal(forgedTenant.body.error, "tenant_untrusted");
  const forgedWallet = await publishTask(record.token, secp256k1.utils.randomPrivateKey());
  assert.equal(forgedWallet.status, 403);
  assert.equal(forgedWallet.body.error, "wallet_untrusted");
  const badSecret = await publishTask(`wgpub_${record.credential_id}.${"0".repeat(64)}`, key);
  assert.equal(badSecret.status, 401);
  const now = Math.floor(Date.now() / 1000);
  const expired = insertCredential(wallet, record.tenant_id, { expiresAt: now - 10 });
  assert.equal((await publishTask(expired.token, key)).status, 401);

  const stray = secp256k1.utils.randomPrivateKey();
  insertCredential(addressOf(stray), "tenant-x");
  const unverifiable = node("tools/bootstrap-publisher.mjs", [
    "--target", "local", "--persist-to", state.persistTo, "--store-dir", state.storeDir, "--wallet", checksumAddress(addressOf(stray)),
  ]);
  assert.equal(unverifiable.status, 5);
  assert.equal(unverifiable.body.results[0].status, "unverifiable_credential");
  const strayRows = d1(`SELECT COUNT(*) AS n FROM publish_credentials WHERE wallet_address = '${addressOf(stray)}'`)[0].n;
  assert.equal(Number(strayRows), 1, "unverifiable credential must not be overwritten or duplicated");

  const revoked = node("tools/bootstrap-publisher.mjs", ["--target", "local", "--persist-to", state.persistTo, "--store-dir", state.storeDir, "--revoke", record.credential_id]);
  assert.equal(revoked.status, 0, revoked.stderr);
  assert.equal(revoked.body.results[0].revoked, true);
  const afterRevoke = await publishTask(record.token, key);
  assert.equal(afterRevoke.status, 401);
});

test("D1-07 routing/cache: /api/* success and error responses are JSON + no-store from the Function", () => {
  const checks = [
    ["/api/session", 200],
    [`/api/tasks/ref_${"a".repeat(32)}`, 401],
    ["/api/tasks/not-a-ref", 404],
    ["/api/rpc", 404],
    ["/api/tasks/ref_" + "a".repeat(32) + "/complete", 404],
    ["/api/nope", 404],
    ["/api", 404],
  ];
  for (const [path, status] of checks) {
    const probe = curl("GET", path);
    assert.equal(probe.status, status, `${path}: ${probe.text.slice(0, 120)}`);
    assertJsonNoStore(probe);
    assert.equal(probe.body.execution_authorized, false);
    assert.equal(probe.text.includes("guardian.task.v1"), false);
  }
  const post = curl("POST", "/api/session", { json: {} });
  assert.equal(post.status, 405);
  assertJsonNoStore(post);
  const cross = curl("GET", "/api/session", { headers: { "sec-fetch-site": "cross-site" } });
  assert.equal(cross.status, 403);
  assertJsonNoStore(cross);
  const page = curl("GET", `/t/ref_${"b".repeat(32)}`);
  assert.equal(page.status, 200);
  assert.match(page.headers.get("content-type") ?? "", /text\/html/);
  const legacy = curl("GET", `/tasks/ref_${"b".repeat(32)}.json`);
  assert.equal(legacy.text.includes("guardian.task.v1"), false);
});

test("D1-08 budget: per-source-IP limits across addresses for challenge and verify", () => {
  const ip = "198.51.100.88";
  const statuses = [];
  for (let i = 0; i < 31; i += 1) {
    const address = checksumAddress(addressOf(secp256k1.utils.randomPrivateKey()));
    statuses.push(curl("POST", "/api/auth/challenge", { json: { address }, ip }).status);
  }
  assert.deepEqual(statuses.slice(0, 30), Array(30).fill(200));
  assert.equal(statuses[30], 429);
  const otherIp = curl("POST", "/api/auth/challenge", {
    json: { address: checksumAddress(addressOf(secp256k1.utils.randomPrivateKey())) }, ip: "198.51.100.89",
  });
  assert.equal(otherIp.status, 200);
  const verify = [];
  for (let i = 0; i < 31; i += 1) {
    verify.push(curl("POST", "/api/auth/verify", { json: { message: "x", signature: `0x${"11".repeat(65)}` }, ip: "198.51.100.90" }).status);
  }
  assert.deepEqual(verify.slice(0, 30), Array(30).fill(401));
  assert.equal(verify[30], 429);
  const buckets = d1("SELECT bucket_key, count FROM rate_buckets WHERE bucket_key LIKE 'verify-ip:%' OR bucket_key LIKE 'challenge-ip:%'");
  assert.equal(buckets.some((row) => row.bucket_key === "verify-ip:198.51.100.90"), true);
});

function chromePath() {
  for (const candidate of [process.env.WG_CHROME, "/opt/google/chrome/chrome", "/usr/bin/chromium", "/usr/bin/google-chrome"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

async function forgedReadyTask(key) {
  const sample = JSON.parse(readFileSync(join(CLIENT, "public/sample-task.json"), "utf8"));
  const address = checksumAddress(addressOf(key));
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

test("D1-09 no-send: built artifact + forged ready-looking task + forced confirm click → 0 send calls", async (t) => {
  const chrome = chromePath();
  assert.ok(chrome, "D1-09 needs Chrome/Chromium (set WG_CHROME)");
  const { chromium } = await import("playwright-core");
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const cred = insertCredential(address, "tenant-ui");
  const forged = await forgedReadyTask(key);
  const created = await publishTask(cred.token, key, {}, forged);
  assert.equal(created.status, 201, created.text);
  const ref = created.body.ref;

  const calls = [];
  const browser = await chromium.launch({
    executablePath: chrome,
    headless: true,
    args: [`--host-resolver-rules=MAP ${HOST}:443 127.0.0.1:${PORT}`, "--ignore-certificate-errors"],
  });
  t.after(() => browser.close());
  const context = await browser.newContext({ ignoreHTTPSErrors: true, serviceWorkers: "block" });
  const offsite = [];
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === HOST) return route.continue();
    offsite.push(url.hostname);
    return route.abort();
  });
  await context.exposeBinding("__wgWallet", (_source, method, params) => {
    calls.push(method);
    if (SEND_METHODS.has(method)) return { error: "send-attempt-recorded" };
    if (method === "eth_requestAccounts" || method === "eth_accounts") return { result: [address] };
    if (method === "eth_chainId") return { result: "0x38" };
    if (method === "personal_sign") {
      const message = String(params?.[0] ?? "");
      if (!message.includes(LOGIN_STATEMENT)) return { error: "non-login personal_sign refused" };
      return { result: signMessage(message, key) };
    }
    return { error: `unsupported ${method}` };
  });
  await context.addInitScript(() => {
    window.ethereum = {
      isMetaMask: false,
      on() {},
      removeListener() {},
      async request({ method, params }) {
        const reply = await window.__wgWallet(method, params ?? []);
        if (reply && "result" in reply) return reply.result;
        const error = new Error(reply?.error ?? "rejected");
        error.code = 4001;
        throw error;
      },
    };
  });
  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(`${ORIGIN}/t/${ref}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#view")?.textContent?.includes("登入"), null, { timeout: 15000 });
  await page.click("#login");
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 20000 });
  await page.waitForFunction(() => [...document.querySelectorAll("button")].some((b) => b.textContent === "在我的錢包確認"), null, { timeout: 20000 });
  const before = await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent === "在我的錢包確認");
    return { disabled: button.disabled, send: document.querySelector("#verify-send")?.dataset.state };
  });
  assert.equal(before.disabled, true, "confirm must render disabled");
  assert.equal(before.send, "closed");
  // Force it: strip disabled and click repeatedly.
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find((b) => b.textContent === "在我的錢包確認");
    button.disabled = false;
    button.removeAttribute("disabled");
    for (let i = 0; i < 3; i += 1) button.click();
  });
  await page.waitForTimeout(2500);
  const sendCalls = calls.filter((method) => SEND_METHODS.has(method));
  assert.deepEqual(sendCalls, [], `send-like wallet calls: ${sendCalls.join(",")}`);
  assert.equal(calls.filter((m) => m === "personal_sign").length, 1, "only the login message is signed");
  assert.equal(await page.evaluate(() => document.querySelector("#verify-send")?.dataset.state), "closed");

  // Same flow the human check will do: cancel → reload refused → logout → old session refused.
  const sessionCookie = (await context.cookies(ORIGIN)).find((c) => c.name === "__Host-wg_session");
  assert.ok(sessionCookie);
  await page.waitForFunction(() => !document.querySelector("#cancel-task")?.hidden, null, { timeout: 10000 });
  await page.click("#cancel-task");
  await page.waitForFunction(() => document.querySelector("#view")?.textContent?.includes("已取消"), null, { timeout: 15000 });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#view")?.textContent?.includes("過期、取消或被取代"), null, { timeout: 20000 });
  await page.waitForFunction(() => !document.querySelector("#logout")?.hidden, null, { timeout: 10000 });
  await page.click("#logout");
  await page.waitForFunction(() => document.querySelector("#logout")?.hidden, null, { timeout: 10000 });
  const replay = curl("GET", `/api/tasks/${ref}`, { cookie: `__Host-wg_session=${sessionCookie.value}` });
  assert.equal(replay.status, 401);
  assert.deepEqual(calls.filter((method) => SEND_METHODS.has(method)), []);
});

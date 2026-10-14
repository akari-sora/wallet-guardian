import assert from "node:assert/strict";
import test from "node:test";
import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";
import { digestBody } from "../canonical.mjs";
import { readFileSync } from "node:fs";
import {
  SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS,
  getSession,
  loadSession,
} from "./auth.mjs";
import {
  SUGGESTED_MODE_A_AUTH_FRESHNESS_SECONDS,
  SUGGESTED_SESSION_ABSOLUTE_TTL_SECONDS,
  SUGGESTED_SESSION_IDLE_TIMEOUT_SECONDS,
} from "./db.mjs";
import { handleApi } from "./handle.mjs";
import { randomHex, sha256Hex } from "./http.mjs";
import { checksumAddress } from "./siwe.mjs";
import { getTask } from "./tasks.mjs";
import { createPrimaryDatabase, privateTaskSchema } from "./test-db.mjs";
import { productionViewOrigin } from "../view-origin.mjs";
import { revokeWalletAccess, sqlBatchDb, validateCredentialId } from "../../tools/lib/revoke-access.mjs";

const ORIGIN = productionViewOrigin();
const HOST = new URL(ORIGIN).host;
const schema = privateTaskSchema();
const sample = JSON.parse(readFileSync(new URL("../../public/sample-task.json", import.meta.url), "utf8"));

function hex(bytes) {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function addressOf(privateKey) {
  return `0x${hex(keccak_256(secp256k1.getPublicKey(privateKey, false).slice(1)).slice(-20))}`;
}

function signMessage(message, privateKey) {
  const payload = new TextEncoder().encode(message);
  const prefix = new TextEncoder().encode(`\u0019Ethereum Signed Message:\n${payload.length}`);
  const bytes = new Uint8Array(prefix.length + payload.length);
  bytes.set(prefix, 0);
  bytes.set(payload, prefix.length);
  const signature = secp256k1.sign(keccak_256(bytes), privateKey);
  const compact = signature.toCompactRawBytes();
  const out = new Uint8Array(65);
  out.set(compact, 0);
  out[64] = signature.recovery + 27;
  return `0x${hex(out)}`;
}

function openEnv() {
  const database = createPrimaryDatabase(schema);
  const env = {
    DB: {
      prepare() {
        throw new Error("direct d1 prepare");
      },
      withSession(mode) {
        return database.withSession(mode);
      },
    },
    AUTH_DOMAIN: HOST,
    AUTH_URI: ORIGIN,
    AUTH_CHAIN_ID: "56",
  };
  return { env, db: () => env.DB.withSession("first-primary") };
}

function cookiePair(response, name) {
  const found = response.headers.getSetCookie().find((item) => item.startsWith(`${name}=`));
  if (!found) return "";
  return found.split(";")[0];
}

async function take(env, method, path, options = {}) {
  const headers = new Headers(options.headers ?? {});
  headers.set("cf-connecting-ip", options.ip ?? "203.0.113.8");
  if (options.cookie) headers.set("cookie", options.cookie);
  if (options.csrf) headers.set("x-csrf-token", options.csrf);
  if (options.json !== undefined) headers.set("content-type", "application/json");
  if (options.json !== undefined && options.origin !== false && !headers.has("origin")) headers.set("origin", ORIGIN);
  const response = await handleApi(
    new Request(`${ORIGIN}${path}`, {
      method,
      headers,
      body: options.json === undefined ? undefined : JSON.stringify(options.json),
    }),
    env,
  );
  const text = await response.text();
  return { response, body: JSON.parse(text), text };
}

async function taskFor(address) {
  const checksum = checksumAddress(address);
  const task = structuredClone(sample);
  delete task.ref;
  task.plan.sender = checksum;
  task.plan.execution_account = checksum;
  task.plan.recipient = checksum;
  task.plan.human_summary.sender = checksum;
  task.plan.human_summary.recipient = checksum;
  task.plan.digest = await digestBody(task.plan);
  return task;
}

async function publish(box, privateKey, tenant) {
  const secret = randomHex(32);
  const id = randomHex(8);
  const wallet = addressOf(privateKey).toLowerCase();
  await box.db().prepare(
    `INSERT INTO publish_credentials
      (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, NULL, NULL)`,
  ).bind(id, await sha256Hex(secret), tenant, wallet).run();
  return { token: `wgpub_${id}.${secret}`, wallet, tenant, id };
}

async function createTask(box, token, privateKey) {
  const address = addressOf(privateKey);
  const created = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${token}` },
    json: { expires_at: Math.floor(Date.now() / 1000) + 3600, task: await taskFor(address) },
  });
  assert.equal(created.response.status, 201, created.text);
  return created;
}

async function login(box, privateKey, ip, cookie = "") {
  const address = addressOf(privateKey);
  const challenge = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip });
  assert.equal(challenge.response.status, 200, challenge.text);
  const attempt = cookiePair(challenge.response, "__Host-wg_attempt");
  const verified = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature: signMessage(challenge.body.message, privateKey) },
    cookie: cookie ? `${cookie}; ${attempt}` : attempt,
    csrf: challenge.body.csrf_token,
    ip,
  });
  assert.equal(verified.response.status, 200, verified.text);
  return {
    cookie: cookiePair(verified.response, "__Host-wg_session"),
    csrf: verified.body.csrf_token,
    address: verified.body.address,
    attempt,
    challenge,
  };
}

function sessionToken(cookie) {
  const token = cookie.slice("__Host-wg_session=".length);
  assert.match(token, /^[a-f0-9]{64}$/);
  return token;
}

test("suggested session lifetimes stay operator defaults and do not enable send", () => {
  assert.equal(SUGGESTED_SESSION_ABSOLUTE_TTL_SECONDS, 12 * 60 * 60);
  assert.equal(SUGGESTED_SESSION_IDLE_TIMEOUT_SECONDS, 30 * 60);
  assert.equal(SUGGESTED_MODE_A_AUTH_FRESHNESS_SECONDS, 15 * 60);
  assert.equal(SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS, 60);
  const send = readFileSync(new URL("../send/mode-a-request.mjs", import.meta.url), "utf8");
  const main = readFileSync(new URL("../main.ts", import.meta.url), "utf8");
  assert.equal(send.includes("SUGGESTED_MODE_A_AUTH_FRESHNESS_SECONDS"), false);
  assert.equal(main.includes("SUGGESTED_MODE_A_AUTH_FRESHNESS_SECONDS"), false);
});

test("AC1 unauthenticated GET of a valid-looking task ref is 401 JSON no-store without a task", async () => {
  const box = openEnv();
  const ref = `ref_${"ab".repeat(16)}`;
  const loaded = await take(box.env, "GET", `/api/tasks/${ref}`);
  assert.equal(loaded.response.status, 401);
  assert.equal((loaded.response.headers.get("cache-control") ?? "").includes("no-store"), true);
  assert.equal((loaded.response.headers.get("content-type") ?? "").includes("application/json"), true);
  assert.equal(Object.hasOwn(loaded.body, "task"), false);
  assert.equal(loaded.body.error, "unauthenticated");
  assert.equal(loaded.text.includes("payload"), false);
});

test("AC2 logged-in cross-tenant, missing, and bad refs are identical 404 bytes", async () => {
  const box = openEnv();
  const owner = secp256k1.utils.randomPrivateKey();
  const other = secp256k1.utils.randomPrivateKey();
  const ownerCredential = await publish(box, owner, "tenant-a");
  const otherCredential = await publish(box, other, "tenant-b");
  await createTask(box, ownerCredential.token, owner);
  const created = await createTask(box, otherCredential.token, other);
  const session = await login(box, owner, "203.0.113.31");
  const cross = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  const missing = await take(box.env, "GET", `/api/tasks/ref_${"0".repeat(32)}`, { cookie: session.cookie });
  const bad = await take(box.env, "GET", `/api/tasks/ref_${"g".repeat(32)}`, { cookie: session.cookie });
  assert.equal(cross.response.status, 404);
  assert.equal(missing.response.status, 404);
  assert.equal(bad.response.status, 404);
  assert.notEqual(cross.response.status, 403);
  assert.equal(cross.text, missing.text);
  assert.equal(cross.text, bad.text);
  assert.equal(Object.hasOwn(cross.body, "task"), false);
  assert.deepEqual(cross.body, { error: "not_found", execution_authorized: false, send_authorized: false });
});

test("AC3 revoke-wallet kills the old session and publish, and a new session still cannot read", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const created = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.32");
  const before = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(before.response.status, 200);
  await revokeWalletAccess(box.db(), credential.wallet, 1_700_000_000);
  const old = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(old.response.status, 401);
  assert.equal(Object.hasOwn(old.body, "task"), false);
  const published = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${credential.token}` },
    json: { expires_at: Math.floor(Date.now() / 1000) + 3600, task: await taskFor(addressOf(key)) },
  });
  assert.equal(published.response.status, 401);
  const fresh = await login(box, key, "203.0.113.33");
  const hidden = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: fresh.cookie });
  assert.equal(hidden.response.status, 404);
  assert.notEqual(hidden.response.status, 403);
  assert.equal(Object.hasOwn(hidden.body, "task"), false);
  const member = await box.db().prepare(
    "SELECT revoked_at FROM tenant_members WHERE tenant_id = ? AND wallet_address = ?",
  ).bind("tenant-a", credential.wallet).first();
  assert.notEqual(member.revoked_at, null);
});

test("revoked membership is not visible and postCreate does not revive it", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const created = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.34");
  const revokedAt = 1_700_000_111;
  await box.db().prepare(
    "UPDATE tenant_members SET revoked_at = ? WHERE tenant_id = ? AND wallet_address = ?",
  ).bind(revokedAt, "tenant-a", credential.wallet).run();
  const hidden = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  const missing = await take(box.env, "GET", `/api/tasks/ref_${"0".repeat(32)}`, { cookie: session.cookie });
  assert.equal(hidden.response.status, 404);
  assert.notEqual(hidden.response.status, 403);
  assert.equal(hidden.text, missing.text);
  const again = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${credential.token}` },
    json: { expires_at: Math.floor(Date.now() / 1000) + 3600, task: await taskFor(addressOf(key)) },
  });
  assert.equal(again.response.status, 403);
  assert.equal(again.body.error, "membership_revoked");
  const tasks = await box.db().prepare("SELECT COUNT(*) AS n FROM tasks").first();
  assert.equal(Number(tasks.n), 1);
  await box.db().prepare(
    "INSERT INTO tenant_members (tenant_id, wallet_address, revoked_at) VALUES (?, ?, NULL) ON CONFLICT DO NOTHING",
  ).bind("tenant-a", credential.wallet).run();
  const member = await box.db().prepare(
    "SELECT revoked_at FROM tenant_members WHERE tenant_id = ? AND wallet_address = ?",
  ).bind("tenant-a", credential.wallet).first();
  assert.equal(Number(member.revoked_at), revokedAt);
});

test("AC4 verify with an old session cookie revokes that token hash in the same batch", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  await createTask(box, credential.token, key);
  const first = await login(box, key, "203.0.113.35");
  const oldToken = sessionToken(first.cookie);
  const oldHash = await sha256Hex(oldToken);
  const second = await login(box, key, "203.0.113.36", first.cookie);
  assert.notEqual(sessionToken(second.cookie), oldToken);
  const oldRow = await box.db().prepare("SELECT revoked_at FROM sessions WHERE token_hash = ?").bind(oldHash).first();
  assert.notEqual(oldRow.revoked_at, null);
  const rows = await box.db().prepare("SELECT revoked_at FROM sessions").all();
  assert.equal(rows.results.length, 2);
  assert.equal(rows.results.filter((row) => row.revoked_at == null).length, 1);
  const denied = await take(box.env, "GET", "/api/session", { cookie: first.cookie });
  assert.equal(denied.body.authenticated, false);
  const kept = await take(box.env, "GET", "/api/session", { cookie: second.cookie });
  assert.equal(kept.body.authenticated, true);
  assert.equal(typeof kept.body.auth_time, "number");
});

test("AC5 idle timeout uses the now argument and fails closed", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const created = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.37");
  const row = await box.db().prepare(
    "SELECT created_at, auth_time, last_seen_at, expires_at FROM sessions WHERE csrf_token = ?",
  ).bind(session.csrf).first();
  assert.equal(Number(row.auth_time), Number(row.created_at));
  assert.equal(Number(row.last_seen_at), Number(row.auth_time));
  const anchor = Number(row.last_seen_at);
  const request = new Request(`${ORIGIN}/api/tasks/${created.body.ref}`, { headers: { cookie: session.cookie } });
  const fresh = await getTask(request, box.db(), anchor + SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS - 1, created.body.ref);
  assert.equal(fresh.status, 200);
  const untouched = await box.db().prepare("SELECT last_seen_at FROM sessions WHERE csrf_token = ?").bind(session.csrf).first();
  assert.equal(Number(untouched.last_seen_at), anchor);
  const stamped = anchor + SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS;
  const touched = await getTask(request, box.db(), stamped, created.body.ref);
  assert.equal(touched.status, 200);
  const written = await box.db().prepare("SELECT last_seen_at FROM sessions WHERE csrf_token = ?").bind(session.csrf).first();
  assert.equal(Number(written.last_seen_at), stamped);
  const idleAt = stamped + SUGGESTED_SESSION_IDLE_TIMEOUT_SECONDS;
  const idle = await getTask(request, box.db(), idleAt, created.body.ref);
  assert.equal(idle.status, 401);
  assert.equal(Object.hasOwn(await idle.clone().json(), "task"), false);
  const gone = await loadSession(box.db(), request, idleAt);
  assert.equal(gone, null);
  const still = await loadSession(box.db(), request, idleAt - 1);
  assert.ok(still);

  await box.db().prepare(
    "UPDATE sessions SET last_seen_at = NULL, auth_time = NULL, created_at = ? WHERE csrf_token = ?",
  ).bind(anchor, session.csrf).run();
  const legacyOk = await getTask(request, box.db(), anchor + 10, created.body.ref);
  assert.equal(legacyOk.status, 200);
  const legacyIdle = await getTask(request, box.db(), anchor + SUGGESTED_SESSION_IDLE_TIMEOUT_SECONDS, created.body.ref);
  assert.equal(legacyIdle.status, 401);
  const viewed = await getSession(request, box.db(), anchor + 10);
  assert.equal((await viewed.json()).auth_time, null);

  const absolute = Number(row.expires_at);
  await box.db().prepare(
    "UPDATE sessions SET last_seen_at = ?, auth_time = ?, created_at = ? WHERE csrf_token = ?",
  ).bind(absolute - 10, absolute - 10, absolute - 10, session.csrf).run();
  const expired = await getTask(request, box.db(), absolute, created.body.ref);
  assert.equal(expired.status, 401);
});

test("old session insert columns still load when auth_time is null", async () => {
  const box = openEnv();
  const wallet = `0x${"11".repeat(20)}`;
  const token = randomHex(32);
  const csrf = randomHex(32);
  const now = 1_700_000_000;
  await box.db().prepare(
    `INSERT INTO sessions (token_hash, address, csrf_token, created_at, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, ?, NULL)`,
  ).bind(await sha256Hex(token), wallet, csrf, now, now + SUGGESTED_SESSION_ABSOLUTE_TTL_SECONDS).run();
  const request = new Request(`${ORIGIN}/api/session`, { headers: { cookie: `__Host-wg_session=${token}` } });
  const session = await loadSession(box.db(), request, now + 5);
  assert.equal(session.address, wallet);
  assert.equal(session.auth_time, null);
  const body = await (await getSession(request, box.db(), now + 5)).json();
  assert.equal(body.authenticated, true);
  assert.equal(body.auth_time, null);
});

test("AC6 logout-all requires CSRF and origin, then every session for that address is 401", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const other = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const otherCredential = await publish(box, other, "tenant-b");
  const created = await createTask(box, credential.token, key);
  await createTask(box, otherCredential.token, other);
  const first = await login(box, key, "203.0.113.41");
  const second = await login(box, key, "203.0.113.42");
  const stranger = await login(box, other, "203.0.113.43");
  const missingCsrf = await take(box.env, "POST", "/api/auth/logout-all", { json: {}, cookie: first.cookie });
  assert.equal(missingCsrf.response.status, 403);
  assert.equal(missingCsrf.body.error, "csrf_required");
  const cross = await take(box.env, "POST", "/api/auth/logout-all", {
    json: {},
    cookie: first.cookie,
    csrf: first.csrf,
    headers: { origin: "https://attacker.example" },
  });
  assert.equal(cross.response.status, 403);
  assert.equal(cross.body.error, "origin_required");
  const still = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: first.cookie });
  assert.equal(still.response.status, 200);
  const done = await take(box.env, "POST", "/api/auth/logout-all", {
    json: {},
    cookie: second.cookie,
    csrf: second.csrf,
  });
  assert.equal(done.response.status, 200);
  assert.equal(done.body.revoked, true);
  assert.equal(done.body.scope, "address");
  assert.equal(done.body.execution_authorized, false);
  for (const cookie of [first.cookie, second.cookie]) {
    const dead = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie });
    assert.equal(dead.response.status, 401);
    assert.equal(Object.hasOwn(dead.body, "task"), false);
  }
  const otherStill = await take(box.env, "GET", "/api/session", { cookie: stranger.cookie });
  assert.equal(otherStill.body.authenticated, true);
  const restored = await login(box, key, "203.0.113.44");
  const readable = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: restored.cookie });
  assert.equal(readable.response.status, 200);
});

test("AC7 a revoke string with a quote is rejected by the format check and builds no SQL", async () => {
  const malicious = "x' OR 1=1 --";
  const result = validateCredentialId(malicious);
  assert.equal(result.ok, false);
  assert.equal(Object.hasOwn(result, "sql"), false);
  assert.equal(JSON.stringify(result).toLowerCase().includes("select"), false);
  assert.equal(JSON.stringify(result).toLowerCase().includes("update"), false);
  assert.doesNotMatch(validateCredentialId.toString(), /SELECT|UPDATE|INSERT|DELETE|d1Execute/i);
  let prepared = 0;
  const db = {
    prepare() {
      prepared += 1;
      throw new Error("SQL was built");
    },
    async batch() {
      throw new Error("SQL ran");
    },
  };
  await assert.rejects(revokeWalletAccess(db, malicious, 1), /0x plus 40/);
  assert.equal(prepared, 0);
  const calls = [];
  await revokeWalletAccess(sqlBatchDb((sql) => calls.push(sql)), `0x${"11".repeat(20)}`, 50);
  assert.equal(calls.length, 1);
  assert.equal((calls[0].match(/UPDATE /g) ?? []).length, 3);
  assert.equal(calls[0].includes(malicious), false);
});

test("AC8 nonce replay, challenge domain mismatch, and cross-site fetch still fail", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const address = addressOf(key);
  const mismatch = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address, domain: "evil.example" },
    ip: "203.0.113.45",
  });
  assert.equal(mismatch.response.status, 400);
  assert.equal(mismatch.body.error, "domain_mismatch");
  const challenge = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address },
    ip: "203.0.113.45",
  });
  assert.equal(challenge.response.status, 200, challenge.text);
  const attempt = cookiePair(challenge.response, "__Host-wg_attempt");
  const payload = {
    json: { message: challenge.body.message, signature: signMessage(challenge.body.message, key) },
    cookie: attempt,
    csrf: challenge.body.csrf_token,
    ip: "203.0.113.45",
  };
  const cross = await take(box.env, "POST", "/api/auth/verify", {
    ...payload,
    headers: { "sec-fetch-site": "cross-site" },
  });
  assert.equal(cross.response.status, 403);
  assert.equal(cross.body.error, "origin_required");
  const first = await take(box.env, "POST", "/api/auth/verify", payload);
  assert.equal(first.response.status, 200, first.text);
  const replay = await take(box.env, "POST", "/api/auth/verify", payload);
  assert.equal(replay.response.status, 401);
  assert.equal(replay.body.error, "nonce_replay");
});

test("AC9 remote GET checks are operator NOT_RUN", () => {
  // NOT_RUN. After deploy, the operator runs the three GET checks in
  // docs/d1-private-task-live.md lines 84-88:
  //   GET /api/session
  //   GET /api/tasks/ref_00000000000000000000000000000000
  //   GET /api/rpc
  // and one more: a forged cookie reading a task, which must be 401.
  // This suite does not call the live site.
  assert.equal(schema.includes("ALTER TABLE sessions ADD COLUMN auth_time"), true);
  assert.equal(schema.includes("ALTER TABLE tenant_members ADD COLUMN revoked_at"), true);
});

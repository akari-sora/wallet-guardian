import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import test from "node:test";
import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";
import { digestBody } from "../canonical.mjs";
import { onRequest } from "../../functions/api/[[path]].js";
import { onRequest as apiMiddleware } from "../../functions/api/_middleware.js";
import { handleApi } from "./handle.mjs";
import { randomHex, sha256Hex } from "./http.mjs";
import { acceptLoginChallenge } from "../siwe-gate.mjs";
import { checksumAddress } from "./siwe.mjs";
import { createPrimaryDatabase, privateTaskSchema } from "./test-db.mjs";

const ORIGIN = "https://wallet-guardian-action-client.pages.dev";
const schema = privateTaskSchema();
const sample = JSON.parse(readFileSync(new URL("../../public/sample-task.json", import.meta.url), "utf8"));
const staticSample = readFileSync(new URL("../../public/sample-task.json", import.meta.url), "utf8");

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

function openEnv(extra = {}) {
  const database = createPrimaryDatabase(schema);
  const seen = [];
  const env = {
    DB: {
      prepare() {
        throw new Error("direct d1 prepare");
      },
      withSession(mode) {
        seen.push(mode);
        return database.withSession(mode);
      },
    },
    AUTH_DOMAIN: "wallet-guardian-action-client.pages.dev",
    AUTH_URI: ORIGIN,
    AUTH_CHAIN_ID: "56",
    ...extra,
  };
  return { env, seen, db: () => env.DB.withSession("first-primary") };
}

function cookiePair(response, name) {
  const found = response.headers.getSetCookie().find((item) => item.startsWith(`${name}=`));
  if (!found) return "";
  return found.split(";")[0];
}

function assertNoStore(response) {
  assert.equal((response.headers.get("cache-control") ?? "").includes("no-store"), true);
  assert.equal((response.headers.get("content-type") ?? "").includes("application/json"), true);
  assert.equal((response.headers.get("content-type") ?? "").includes("text/html"), false);
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
  assertNoStore(response);
  const text = await response.text();
  assert.equal(text.startsWith("<!doctype"), false);
  assert.equal(text.startsWith("<html"), false);
  assert.equal(text.includes(staticSample.slice(0, 80)), false);
  return { response, body: JSON.parse(text), text };
}

async function taskFor(address) {
  const checksum = checksumAddress(address);
  const task = structuredClone(sample);
  delete task.ref;
  task.preflight = { preflight_ok: true, revision_required: false, changes: [] };
  task.requires_user_wallet_confirmation = true;
  task.reported_tx_hash = "0x" + "ab".repeat(32);
  task.evidence.fixed_https_origin = true;
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
  return { token: `wgpub_${id}.${secret}`, wallet, tenant };
}

async function createTask(box, token, privateKey, extra = {}) {
  const task = await taskFor(addressOf(privateKey));
  const created = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${token}` },
    json: { expires_at: Math.floor(Date.now() / 1000) + 3600, task, ...extra },
  });
  return { created, task };
}

async function login(box, privateKey, ip = "203.0.113.9") {
  const address = checksumAddress(addressOf(privateKey));
  const challenge = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip });
  assert.equal(challenge.response.status, 200, challenge.text);
  assert.equal(typeof acceptLoginChallenge(challenge.body, address, Date.now()), "string");
  assert.equal(challenge.body.erc1271, "unsupported");
  assert.equal(challenge.body.execution_authorized, false);
  assert.equal(challenge.body.send_authorized, false);
  const attempt = cookiePair(challenge.response, "__Host-wg_attempt");
  assert.equal(attempt.startsWith("__Host-wg_attempt="), true);
  assert.equal(cookiePair(challenge.response, "__Host-wg_session"), "");
  const verified = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature: signMessage(challenge.body.message, privateKey) },
    cookie: attempt,
    csrf: challenge.body.csrf_token,
    ip,
  });
  assert.equal(verified.response.status, 200, verified.text);
  const session = cookiePair(verified.response, "__Host-wg_session");
  const header = verified.response.headers.getSetCookie().find((item) => item.startsWith("__Host-wg_session="));
  assert.match(header, /Secure/);
  assert.match(header, /HttpOnly/);
  assert.match(header, /Path=\//);
  assert.match(header, /SameSite=Lax/);
  assert.equal(/Domain=/i.test(header), false);
  assert.equal(box.seen.every((mode) => mode === "first-primary"), true);
  return { cookie: session, csrf: verified.body.csrf_token, address: verified.body.address, challenge, verified };
}

test("happy login reads only the caller's task and preflight does not authorize send", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const { created } = await createTask(box, credential.token, key);
  assert.equal(created.response.status, 201, created.text);
  assert.equal(created.body.tenant_id, "tenant-a");
  assert.equal(created.body.send_authorized, false);
  assert.equal(created.body.execution_authorized, false);
  assert.equal(created.body.tx_hash_verified, false);
  const session = await login(box, key);
  const before = await box.db().prepare("SELECT updated_at, status, executed FROM tasks WHERE ref = ?").bind(created.body.ref).first();
  const loaded = await take(box.env, "GET", `/api/tasks/${created.body.ref}?tenant_id=victim&wallet_address=0x${"22".repeat(20)}`, {
    cookie: session.cookie,
  });
  assert.equal(loaded.response.status, 200, loaded.text);
  assert.equal(loaded.body.tenant_id, "tenant-a");
  assert.equal(loaded.body.send_authorized, false);
  assert.equal(loaded.body.execution_authorized, false);
  assert.equal(loaded.body.tx_hash_verified, false);
  assert.equal(loaded.body.on_chain_revoked, false);
  assert.equal(loaded.body.cancel_allowed, true);
  assert.equal(loaded.body.view.authorizes_execution, false);
  assert.equal(loaded.body.task.preflight.preflight_ok, true);
  assert.equal(loaded.body.task.wallet_request_allowed, false);
  assert.equal(loaded.body.task.execution_authorized, false);
  // The view link comes from config/deployments/private-task-origins.json, not from the task.
  assert.equal(loaded.body.task.link.issued, true);
  assert.equal(loaded.body.task.link.purpose, "private_task_view");
  assert.equal(loaded.body.task.link.url, `${ORIGIN}/t/${created.body.ref}`);
  assert.equal(loaded.body.task.link.execution_authorized, false);
  assert.equal(created.body.view_url, `${ORIGIN}/t/${created.body.ref}`);
  assert.equal(loaded.body.task.evidence.fixed_https_origin, false);
  assert.equal(loaded.body.task.ref, created.body.ref);
  const after = await box.db().prepare("SELECT updated_at, status, executed FROM tasks WHERE ref = ?").bind(created.body.ref).first();
  assert.deepEqual(after, before);
  const again = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(again.response.status, 200);
  assert.equal(again.body.revision, loaded.body.revision);
});

test("refresh keeps the session and relogin replaces it", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const { created } = await createTask(box, credential.token, key);
  const first = await login(box, key, "203.0.113.21");
  const refreshed = await take(box.env, "GET", "/api/session", { cookie: first.cookie });
  assert.equal(refreshed.body.authenticated, true);
  assert.equal(refreshed.body.address, first.address);
  assert.equal(refreshed.body.execution_authorized, false);
  const still = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: first.cookie });
  assert.equal(still.response.status, 200);
  const loggedOut = await take(box.env, "POST", "/api/auth/logout", { json: {}, cookie: first.cookie, csrf: first.csrf });
  assert.equal(loggedOut.response.status, 200);
  assert.equal(loggedOut.body.revoked, true);
  const revoked = await box.db().prepare("SELECT revoked_at FROM sessions WHERE csrf_token = ?").bind(first.csrf).first();
  assert.ok(revoked.revoked_at);
  const old = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: first.cookie });
  assert.equal(old.response.status, 401);
  assert.equal(old.body.task, undefined);
  const second = await login(box, key, "203.0.113.22");
  const restored = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: second.cookie });
  assert.equal(restored.response.status, 200);
  const oldAgain = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: first.cookie });
  assert.equal(oldAgain.response.status, 401);
});

test("cross-tenant and forged ref or tenant are denied", async () => {
  const box = openEnv();
  const owner = secp256k1.utils.randomPrivateKey();
  const other = secp256k1.utils.randomPrivateKey();
  const ownerCredential = await publish(box, owner, "tenant-a");
  const otherCredential = await publish(box, other, "tenant-b");
  const { created } = await createTask(box, otherCredential.token, other);
  const outsider = await login(box, owner, "203.0.113.30");
  const cross = await take(box.env, "GET", `/api/tasks/${created.body.ref}?tenant_id=tenant-a`, { cookie: outsider.cookie });
  const forgedRef = await take(box.env, "GET", `/api/tasks/ref_${"0".repeat(32)}?tenant_id=tenant-b`, { cookie: outsider.cookie });
  assert.equal(cross.response.status, 404);
  assert.equal(forgedRef.response.status, 404);
  assert.deepEqual(cross.body, forgedRef.body);
  assert.deepEqual(cross.body, { error: "not_found", execution_authorized: false, send_authorized: false });
  assert.equal(cross.text.includes("合成範例"), false);
  const forgedCreate = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${ownerCredential.token}`, "x-tenant-id": "tenant-b" },
    json: {
      tenant_id: "tenant-b",
      wallet_address: checksumAddress(addressOf(other)),
      expires_at: Math.floor(Date.now() / 1000) + 3600,
      task: await taskFor(addressOf(owner)),
    },
  });
  assert.equal(forgedCreate.response.status, 403);
  assert.equal(forgedCreate.body.error, "tenant_untrusted");
  const rows = await box.db().prepare("SELECT COUNT(*) AS n FROM tasks").bind().first();
  assert.equal(Number(rows.n), 1);
  const stored = await box.db().prepare("SELECT tenant_id, wallet_address FROM tasks").bind().first();
  assert.equal(stored.tenant_id, "tenant-b");
  assert.equal(stored.wallet_address, addressOf(other).toLowerCase());
});

test("nonce replay and parallel consume issue one session", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const challenge = await take(box.env, "POST", "/api/auth/challenge", { json: { address, domain: ORIGIN.replace("https://", ""), chainId: 56 }, ip: "203.0.113.40" });
  assert.equal(challenge.response.status, 200, challenge.text);
  const attempt = cookiePair(challenge.response, "__Host-wg_attempt");
  const signature = signMessage(challenge.body.message, key);
  const options = {
    json: { message: challenge.body.message, signature },
    cookie: attempt,
    csrf: challenge.body.csrf_token,
    ip: "203.0.113.40",
  };
  const [first, second] = await Promise.all([
    take(box.env, "POST", "/api/auth/verify", options),
    take(box.env, "POST", "/api/auth/verify", options),
  ]);
  const statuses = [first.response.status, second.response.status].sort();
  assert.deepEqual(statuses, [200, 401]);
  const replay = [first, second].find((item) => item.response.status === 401);
  assert.equal(replay.body.error, "nonce_replay");
  const again = await take(box.env, "POST", "/api/auth/verify", options);
  assert.equal(again.response.status, 401);
  assert.equal(again.body.error, "nonce_replay");
  const sessions = await box.db().prepare("SELECT COUNT(*) AS n FROM sessions").bind().first();
  assert.equal(Number(sessions.n), 1);
  const consumed = await box.db().prepare("SELECT consumed FROM challenges").bind().first();
  assert.equal(Number(consumed.consumed), 1);
});

test("logout revokes the server session and csrf is required", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const { created } = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.50");
  const missing = await take(box.env, "POST", "/api/auth/logout", { json: {}, cookie: session.cookie });
  assert.equal(missing.response.status, 403);
  assert.equal(missing.body.error, "csrf_required");
  const still = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(still.response.status, 200);
  const getLogout = await take(box.env, "GET", "/api/auth/logout", { cookie: session.cookie });
  assert.equal(getLogout.response.status, 405);
  const afterGet = await box.db().prepare("SELECT revoked_at FROM sessions").bind().first();
  assert.equal(afterGet.revoked_at, null);
  const loggedOut = await take(box.env, "POST", "/api/auth/logout", { json: {}, cookie: session.cookie, csrf: session.csrf });
  assert.equal(loggedOut.body.revoked, true);
  const cleared = loggedOut.response.headers.getSetCookie().find((item) => item.startsWith("__Host-wg_session="));
  assert.match(cleared, /Max-Age=0/);
  const denied = await take(box.env, "GET", `/api/tasks/${created.body.ref}`, { cookie: session.cookie });
  assert.equal(denied.response.status, 401);
  const anonymous = await take(box.env, "GET", "/api/session", { cookie: session.cookie });
  assert.equal(anonymous.body.authenticated, false);
  assert.equal(anonymous.body.address, undefined);
});

test("expired tasks, superseded rows, and old revisions are rejected", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const { created } = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.60");
  const ref = created.body.ref;
  await box.db().prepare("UPDATE tasks SET revision = 2 WHERE ref = ?").bind(ref).run();
  const old = await take(box.env, "GET", `/api/tasks/${ref}?revision=1`, { cookie: session.cookie });
  assert.equal(old.response.status, 409);
  assert.equal(old.body.error, "revision_mismatch");
  assert.equal(old.body.task, undefined);
  const current = await take(box.env, "GET", `/api/tasks/${ref}`, { cookie: session.cookie });
  assert.equal(current.response.status, 200);
  assert.equal(current.body.revision, 2);
  const staleCancel = await take(box.env, "POST", `/api/tasks/${ref}/cancel`, {
    json: { revision: 1, tx_hash: "0x" + "11".repeat(32), status: "COMPLETE" },
    cookie: session.cookie,
    csrf: session.csrf,
  });
  assert.equal(staleCancel.response.status, 409);
  assert.equal(staleCancel.body.on_chain_revoked, false);
  assert.equal(staleCancel.body.tx_hash_verified, false);
  const stillActive = await box.db().prepare("SELECT status, executed FROM tasks WHERE ref = ?").bind(ref).first();
  assert.equal(stillActive.status, "active");
  assert.equal(Number(stillActive.executed), 0);
  await box.db().prepare("UPDATE tasks SET expires_at = 1 WHERE ref = ?").bind(ref).run();
  const expired = await take(box.env, "GET", `/api/tasks/${ref}?revision=2`, { cookie: session.cookie });
  assert.equal(expired.response.status, 410);
  assert.equal(expired.body.error, "expired");
  assert.equal(expired.body.task, undefined);
  await box.db().prepare("UPDATE tasks SET expires_at = ?, status = 'superseded' WHERE ref = ?").bind(Math.floor(Date.now() / 1000) + 3600, ref).run();
  const superseded = await take(box.env, "GET", `/api/tasks/${ref}`, { cookie: session.cookie });
  assert.equal(superseded.response.status, 410);
  assert.equal(superseded.body.error, "superseded");
  assert.equal(superseded.body.task, undefined);
});

test("cancel does not claim on-chain revoke or completion", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const { created } = await createTask(box, credential.token, key);
  const session = await login(box, key, "203.0.113.70");
  const ref = created.body.ref;
  const cancelled = await take(box.env, "POST", `/api/tasks/${ref}/cancel`, {
    json: { revision: 1, tx_hash: "0x" + "22".repeat(32), status: "COMPLETE" },
    cookie: session.cookie,
    csrf: session.csrf,
  });
  assert.equal(cancelled.response.status, 200, cancelled.text);
  assert.equal(cancelled.body.status, "cancelled");
  assert.equal(cancelled.body.on_chain_revoked, false);
  assert.equal(cancelled.body.tx_hash_verified, false);
  assert.equal(cancelled.body.send_authorized, false);
  const row = await box.db().prepare("SELECT status, executed, payload_json FROM tasks WHERE ref = ?").bind(ref).first();
  assert.equal(row.status, "cancelled");
  assert.equal(Number(row.executed), 0);
  assert.equal(row.payload_json.includes(`0x${"22".repeat(32)}`), false);
  const follow = await take(box.env, "GET", `/api/tasks/${ref}`, { cookie: session.cookie });
  assert.equal(follow.response.status, 410);
  assert.equal(follow.body.error, "cancelled");
  assert.equal(follow.body.task, undefined);
  const complete = await take(box.env, "POST", `/api/tasks/${ref}/complete`, {
    json: { tx_hash: "0x" + "33".repeat(32), status: "COMPLETE" },
    cookie: session.cookie,
    csrf: session.csrf,
  });
  assert.equal(complete.response.status, 404);
  assert.equal(complete.body.error, "complete_not_supported");
  const rpc = await take(box.env, "POST", "/api/rpc", { json: { method: "eth_call" }, cookie: session.cookie });
  assert.equal(rpc.response.status, 404);
  assert.equal(rpc.body.error, "rpc_not_supported");
  const finalRow = await box.db().prepare("SELECT status, executed FROM tasks WHERE ref = ?").bind(ref).first();
  assert.equal(finalRow.status, "cancelled");
  assert.equal(Number(finalRow.executed), 0);
});

test("erc1271 is unsupported and an owner key cannot impersonate another address", async () => {
  const box = openEnv();
  const owner = secp256k1.utils.randomPrivateKey();
  const contract = addressOf(secp256k1.utils.randomPrivateKey());
  const challenge = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address: checksumAddress(contract) },
    ip: "203.0.113.80",
  });
  assert.equal(challenge.response.status, 200, challenge.text);
  const impersonated = await take(box.env, "POST", "/api/auth/verify", {
    json: {
      message: challenge.body.message,
      signature: signMessage(challenge.body.message, owner),
      account_type: "contract",
    },
    cookie: cookiePair(challenge.response, "__Host-wg_attempt"),
    csrf: challenge.body.csrf_token,
    ip: "203.0.113.80",
  });
  assert.equal(impersonated.response.status, 400);
  assert.equal(impersonated.body.error, "erc1271_unsupported");
  const mismatch = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature: signMessage(challenge.body.message, owner) },
    cookie: cookiePair(challenge.response, "__Host-wg_attempt"),
    csrf: challenge.body.csrf_token,
    ip: "203.0.113.80",
  });
  assert.equal(mismatch.response.status, 401);
  assert.equal(mismatch.body.error, "signature_mismatch");
  const longSig = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: challenge.body.message, signature: `0x${"11".repeat(80)}` },
    cookie: cookiePair(challenge.response, "__Host-wg_attempt"),
    csrf: challenge.body.csrf_token,
    ip: "203.0.113.80",
  });
  assert.equal(longSig.response.status, 400);
  assert.equal(longSig.body.error, "erc1271_unsupported");
  const sessions = await box.db().prepare("SELECT COUNT(*) AS n FROM sessions").bind().first();
  assert.equal(Number(sessions.n), 0);
  const consumed = await box.db().prepare("SELECT consumed FROM challenges").bind().first();
  assert.equal(Number(consumed.consumed), 0);
});

test("browser session cannot publish and omitted tenant comes from the credential", async () => {
  const box = openEnv();
  const key = secp256k1.utils.randomPrivateKey();
  const credential = await publish(box, key, "tenant-a");
  const session = await login(box, key, "203.0.113.90");
  const blocked = await take(box.env, "POST", "/api/bot/tasks", {
    json: { expires_at: Math.floor(Date.now() / 1000) + 1000, task: await taskFor(addressOf(key)) },
    cookie: session.cookie,
    csrf: session.csrf,
  });
  assert.equal(blocked.response.status, 401);
  const created = await createTask(box, credential.token, key);
  assert.equal(created.created.response.status, 201, created.created.text);
  assert.equal(created.created.body.tenant_id, "tenant-a");
  const wrongWallet = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${credential.token}` },
    json: {
      tenant_id: "tenant-a",
      wallet_address: checksumAddress(`0x${"33".repeat(20)}`),
      expires_at: Math.floor(Date.now() / 1000) + 1000,
      task: await taskFor(addressOf(key)),
    },
  });
  assert.equal(wrongWallet.response.status, 403);
  assert.equal(wrongWallet.body.error, "wallet_untrusted");
  const otherKey = secp256k1.utils.randomPrivateKey();
  const plantedSender = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${credential.token}`, "x-tenant-id": "tenant-b" },
    json: { expires_at: Math.floor(Date.now() / 1000) + 1000, task: await taskFor(addressOf(otherKey)) },
  });
  assert.equal(plantedSender.response.status, 403);
  assert.equal(plantedSender.body.error, "wallet_untrusted");
  const modeC = await take(box.env, "POST", "/api/bot/tasks", {
    origin: false,
    headers: { authorization: `Bearer ${credential.token}` },
    json: {
      expires_at: Math.floor(Date.now() / 1000) + 1000,
      task: { ...(await taskFor(addressOf(key))), mode: "C" },
    },
  });
  assert.equal(modeC.response.status, 400);
  assert.equal(modeC.body.error, "mode_c_unsupported");
  const rows = await box.db().prepare("SELECT COUNT(*) AS n FROM tasks").bind().first();
  assert.equal(Number(rows.n), 1);
});

test("function errors and unknown api paths are no-store json, never static tasks", async () => {
  const broken = await onRequest({
    request: new Request(`${ORIGIN}/api/session`),
    env: {
      DB: { withSession() { throw new Error("primary down"); } },
      AUTH_DOMAIN: "wallet-guardian-action-client.pages.dev",
      AUTH_URI: ORIGIN,
      AUTH_CHAIN_ID: "56",
    },
  });
  assert.equal(broken.status, 500);
  assertNoStore(broken);
  const brokenText = await broken.text();
  assert.equal(brokenText.includes("primary down"), false);
  assert.equal(JSON.parse(brokenText).error, "internal_error");
  const wrapped = await apiMiddleware({
    async next() {
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
    },
  });
  assertNoStore(wrapped);
  const failed = await apiMiddleware({
    async next() {
      throw new Error("boom");
    },
  });
  assert.equal(failed.status, 500);
  assertNoStore(failed);
  assert.equal((await failed.text()).includes("boom"), false);

  const box = openEnv({ AUTH_URI: "http://evil.example/steal", AUTH_DOMAIN: "evil.example" });
  const badConfig = await take(box.env, "POST", "/api/auth/challenge", { json: { address: checksumAddress(addressOf(secp256k1.utils.randomPrivateKey())) } });
  assert.equal(badConfig.response.status, 500);
  assert.equal(badConfig.body.error, "internal_error");

  const empty = openEnv();
  const paths = ["/api", "/api/sample-task.json", `/api/tasks/ref_${"ab".repeat(16)}.json`, "/api/does-not-exist"];
  for (const path of paths) {
    const response = await take(empty.env, "GET", path);
    assert.equal(response.response.status, 404);
    assert.equal(response.body.error, "not_found");
    assert.equal(response.text.includes("合成範例"), false);
    assert.notEqual(response.text, staticSample);
  }
  const redirects = readFileSync(new URL("../../public/_redirects", import.meta.url), "utf8");
  assert.match(redirects, /\/t\/\*/);
  assert.equal(/\/api\/\*\s+\/index\.html/.test(redirects), false);
  const routes = JSON.parse(readFileSync(new URL("../../public/_routes.json", import.meta.url), "utf8"));
  assert.equal(routes.include.includes("/api/*"), true);
  assert.equal(routes.exclude.includes("/api/*"), false);
  assert.equal(existsSync(new URL("../../public/tasks", import.meta.url)), false);
  assert.equal(existsSync(new URL("../../public/api", import.meta.url)), false);
  // _headers is generated at build time from src/csp.mjs (WALLET-CONNECT-01 W-B5); it never targets /api.
  const { pagesHeaders } = await import("../csp.mjs");
  for (const walletConnect of [false, true]) {
    const headers = pagesHeaders({ walletConnect });
    assert.equal(headers.includes("/api"), false);
    assert.match(headers, /Content-Security-Policy: default-src 'none'/);
  }
});

test("challenge is bound to domain, chain, and attempt, and is rate limited", async () => {
  const box = openEnv({ AUTH_CHALLENGE_LIMIT: "2" });
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const wrongDomain = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address, domain: "evil.example" },
    ip: "203.0.113.100",
  });
  assert.equal(wrongDomain.response.status, 400);
  assert.equal(wrongDomain.body.error, "domain_mismatch");
  const wrongChain = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address, chainId: 1 },
    ip: "203.0.113.100",
  });
  assert.equal(wrongChain.response.status, 400);
  assert.equal(wrongChain.body.error, "chain_mismatch");
  const first = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip: "203.0.113.100" });
  const second = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip: "203.0.113.100" });
  assert.equal(first.response.status, 200);
  assert.equal(second.response.status, 200);
  const wrongAttempt = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: first.body.message, signature: signMessage(first.body.message, key) },
    cookie: `__Host-wg_attempt=${"0".repeat(64)}`,
    csrf: first.body.csrf_token,
    ip: "203.0.113.100",
  });
  assert.equal(wrongAttempt.response.status, 401);
  assert.equal(wrongAttempt.body.error, "attempt_mismatch");
  const untouched = await box.db().prepare("SELECT consumed FROM challenges WHERE message = ?").bind(first.body.message).first();
  assert.equal(Number(untouched.consumed), 0);
  const limited = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip: "203.0.113.100" });
  assert.equal(limited.response.status, 429);
  const cross = await take(box.env, "POST", "/api/auth/verify", {
    json: { message: first.body.message, signature: signMessage(first.body.message, key) },
    cookie: cookiePair(first.response, "__Host-wg_attempt"),
    csrf: first.body.csrf_token,
    headers: { "sec-fetch-site": "cross-site" },
    ip: "203.0.113.100",
  });
  assert.equal(cross.response.status, 403);
  const noOrigin = await take(box.env, "POST", "/api/auth/verify", {
    origin: false,
    json: { message: second.body.message, signature: signMessage(second.body.message, key) },
    cookie: cookiePair(second.response, "__Host-wg_attempt"),
    csrf: second.body.csrf_token,
    ip: "203.0.113.101",
  });
  assert.equal(noOrigin.response.status, 403);
  assert.equal(noOrigin.body.error, "origin_required");
});

test("expired challenges are removed before the global quota is applied", async () => {
  const box = openEnv({ AUTH_CHALLENGE_GLOBAL_LIMIT: "1", AUTH_CHALLENGE_LIMIT: "10" });
  const now = Math.floor(Date.now() / 1000);
  await box.db().prepare(
    `INSERT INTO challenges
      (nonce, attempt_id, csrf_token, address, domain, uri, chain_id, message, expires_at, consumed)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`,
  ).bind(
    randomHex(16),
    randomHex(32),
    randomHex(32),
    `0x${"11".repeat(20)}`,
    "wallet-guardian-action-client.pages.dev",
    ORIGIN,
    56,
    "expired",
    now - 5,
  ).run();
  await box.db().prepare(
    "INSERT INTO rate_buckets (bucket_key, window_start, count) VALUES (?, ?, ?)",
  ).bind("challenge:old", now - 10_000, 99).run();
  const address = checksumAddress(addressOf(secp256k1.utils.randomPrivateKey()));
  const issued = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip: "203.0.113.77" });
  assert.equal(issued.response.status, 200, issued.text);
  const expired = await box.db().prepare("SELECT COUNT(*) AS n FROM challenges WHERE message = ?").bind("expired").first();
  assert.equal(Number(expired.n), 0);
  const staleBucket = await box.db().prepare("SELECT bucket_key FROM rate_buckets WHERE bucket_key = ?").bind("challenge:old").first();
  assert.equal(staleBucket, null);
  const other = checksumAddress(addressOf(secp256k1.utils.randomPrivateKey()));
  const blocked = await take(box.env, "POST", "/api/auth/challenge", { json: { address: other }, ip: "203.0.113.78" });
  assert.equal(blocked.response.status, 429);
  assert.equal(blocked.body.error, "rate_limited");
  const live = await box.db().prepare("SELECT COUNT(*) AS n FROM challenges WHERE consumed = 0").bind().first();
  assert.equal(Number(live.n), 1);
});

test("preview without D1 or AUTH vars answers private_api_unavailable as no-store json", async () => {
  for (const env of [{}, { AUTH_DOMAIN: "wallet-guardian-action-client.pages.dev", AUTH_URI: ORIGIN, AUTH_CHAIN_ID: "56" }]) {
    for (const path of ["/api/session", "/api/tasks/ref_" + "0".repeat(32)]) {
      const response = await onRequest({ request: new Request(`${ORIGIN}${path}`), env });
      assert.equal(response.status, 503);
      assertNoStore(response);
      const body = JSON.parse(await response.text());
      assert.equal(body.error, "private_api_unavailable");
      assert.equal(body.execution_authorized, false);
    }
  }
  const rpc = await onRequest({ request: new Request(`${ORIGIN}/api/rpc`), env: {} });
  assert.equal(rpc.status, 404);
});

test("view link is issued only for an origin listed in the versioned policy", async () => {
  const key = secp256k1.utils.randomPrivateKey();
  const other = openEnv({ AUTH_DOMAIN: "other.example", AUTH_URI: "https://other.example" });
  const credential = await publish(other, key, "tenant-o");
  const { created } = await createTask(other, credential.token, key);
  assert.equal(created.response.status, 201, created.text);
  assert.equal(created.body.view_url, null);
  const row = await other.db().prepare("SELECT payload_json FROM tasks WHERE ref = ?").bind(created.body.ref).first();
  const stored = JSON.parse(row.payload_json);
  assert.equal(stored.link.issued, false);
  assert.equal(stored.link.reason, "view_origin_not_approved");
  assert.equal(stored.link.url, null);
  assert.equal(stored.evidence.fixed_https_origin, false);
});

test("verify and challenge have a cross-address budget per source ip", async () => {
  const box = openEnv({ AUTH_CHALLENGE_IP_LIMIT: "3", AUTH_VERIFY_IP_LIMIT: "2", AUTH_CHALLENGE_LIMIT: "10" });
  const ip = "198.51.100.23";
  const statuses = [];
  for (let i = 0; i < 4; i += 1) {
    const address = checksumAddress(addressOf(secp256k1.utils.randomPrivateKey()));
    const issued = await take(box.env, "POST", "/api/auth/challenge", { json: { address }, ip });
    statuses.push(issued.response.status);
  }
  assert.deepEqual(statuses, [200, 200, 200, 429]);
  const elsewhere = await take(box.env, "POST", "/api/auth/challenge", {
    json: { address: checksumAddress(addressOf(secp256k1.utils.randomPrivateKey())) },
    ip: "198.51.100.24",
  });
  assert.equal(elsewhere.response.status, 200);
  const verifyStatuses = [];
  for (let i = 0; i < 3; i += 1) {
    const attempt = await take(box.env, "POST", "/api/auth/verify", {
      json: { message: "x", signature: "0x" + "11".repeat(65) },
      ip,
    });
    verifyStatuses.push(attempt.response.status);
  }
  assert.deepEqual(verifyStatuses, [401, 401, 429]);
});

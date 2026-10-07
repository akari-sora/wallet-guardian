import { digestBody } from "../canonical.mjs";
import { CLIENT_BUILD } from "../gate.mjs";
import { assertInertPayload, validatePlan } from "../plan-validation.mjs";
import { loadSession } from "./auth.mjs";
import { readConfig } from "./db.mjs";
import { json, randomHex, readJson, sha256Hex, timingSafeEqual } from "./http.mjs";
import { checksumAddress } from "./siwe.mjs";

const REF_PATTERN = /^ref_[a-f0-9]{32}$/;
const TASK_VERSION = "guardian.task.v1";
const MAX_TTL = 7 * 24 * 60 * 60;
const DUMMY_SECRET = "wallet-guardian-publish-dummy";

let dummyHashPromise;

function dummyHash() {
  dummyHashPromise ??= sha256Hex(DUMMY_SECRET);
  return dummyHashPromise;
}

function denied() {
  return json(404, { error: "not_found", execution_authorized: false, send_authorized: false });
}

function closed(extra = {}) {
  return {
    execution_authorized: false,
    send_authorized: false,
    tx_hash_verified: false,
    on_chain_revoked: false,
    erc1271: "unsupported",
    ...extra,
  };
}

async function visibleTask(db, ref, address) {
  if (!REF_PATTERN.test(ref)) return null;
  return db
    .prepare(
      `SELECT t.ref, t.tenant_id, t.wallet_address, t.revision, t.status, t.expires_at,
              t.payload_json, t.executed, t.updated_at
       FROM tasks t
       JOIN task_acl a ON a.ref = t.ref AND a.tenant_id = t.tenant_id AND a.wallet_address = ?
       JOIN tenant_members m ON m.tenant_id = t.tenant_id AND m.wallet_address = ?
       WHERE t.ref = ?`,
    )
    .bind(address, address, ref)
    .first();
}

function availability(row, now) {
  if (row.status === "superseded") return "superseded";
  if (row.status === "cancelled") return "cancelled";
  if (row.status !== "active") return "expired";
  if (Number(row.expires_at) <= now) return "expired";
  if (Number(row.executed) !== 0) return "not_cancellable";
  return null;
}

export async function getTask(request, db, now, ref) {
  const url = new URL(request.url);
  const session = await loadSession(db, request, now);
  if (!session) return json(401, { error: "unauthenticated", execution_authorized: false });
  const row = await visibleTask(db, ref, session.address);
  if (!row) return denied();
  const requested = url.searchParams.get("revision");
  if (requested !== null) {
    if (!/^[0-9]+$/.test(requested) || Number(requested) !== Number(row.revision)) {
      return json(409, closed({ error: "revision_mismatch", ref: row.ref }));
    }
  }
  const problem = availability(row, now);
  if (problem === "expired" || problem === "cancelled" || problem === "superseded") {
    return json(410, closed({ error: problem, ref: row.ref, revision: row.revision }));
  }
  if (problem) return json(409, closed({ error: problem, ref: row.ref, revision: row.revision }));
  let task;
  try {
    task = JSON.parse(row.payload_json);
  } catch {
    return json(500, { error: "internal_error" });
  }
  return json(200, closed({
    ref: row.ref,
    revision: row.revision,
    status: row.status,
    expires_at: row.expires_at,
    cancel_allowed: true,
    tenant_id: row.tenant_id,
    view: { address: checksumAddress(session.address), authorizes_execution: false },
    task,
  }));
}

export async function postCancel(request, db, now, ref) {
  const parsed = await readJson(request, 8192);
  if (parsed.error) return json(400, { error: parsed.error, execution_authorized: false });
  const session = await loadSession(db, request, now);
  if (!session) return json(401, { error: "unauthenticated", execution_authorized: false });
  const csrf = request.headers.get("x-csrf-token") ?? "";
  if (!(await timingSafeEqual(session.csrf_token, csrf))) {
    return json(403, { error: "csrf_required", execution_authorized: false });
  }
  const row = await visibleTask(db, ref, session.address);
  if (!row) return denied();
  const body = parsed.value;
  if (body.tenant_id !== undefined && body.tenant_id !== row.tenant_id) {
    return json(403, closed({ error: "tenant_untrusted" }));
  }
  if (!Number.isSafeInteger(body.revision)) return json(400, closed({ error: "revision_required" }));
  if (Number(body.revision) !== Number(row.revision)) {
    return json(409, closed({ error: "revision_mismatch", ref: row.ref, revision: row.revision }));
  }
  const problem = availability(row, now);
  if (problem === "expired" || problem === "cancelled" || problem === "superseded") {
    return json(410, closed({ error: problem, ref: row.ref, revision: row.revision }));
  }
  if (problem) return json(409, closed({ error: problem, ref: row.ref }));
  const updated = await db
    .prepare(
      `UPDATE tasks
       SET status = 'cancelled', updated_at = ?
       WHERE ref = ? AND revision = ? AND status = 'active' AND executed = 0 AND expires_at > ?`,
    )
    .bind(now, ref, row.revision, now)
    .run();
  if (Number(updated.meta.changes) !== 1) return json(409, closed({ error: "not_cancellable", ref }));
  return json(200, closed({ ref, revision: row.revision, status: "cancelled", cancel_allowed: false }));
}

function rejectTask(task) {
  if (!task || typeof task !== "object" || Array.isArray(task)) return "invalid_task";
  try {
    assertInertPayload(task);
  } catch {
    return "invalid_task";
  }
  if (task.protocol_version !== TASK_VERSION || task.client_build !== CLIENT_BUILD) return "invalid_task";
  if (task.execution_authorized !== false || task.wallet_request_allowed !== false) return "invalid_task";
  if (task.mode === "C") return "mode_c_unsupported";
  if (task.mode !== "A" && task.mode !== "B") return "invalid_task";
  if (task.ref !== undefined) return "ref_untrusted";
  if (task.requests_7702 === true || task.unlimited_approval === true) return "invalid_task";
  if (task.link && task.link.execution_authorized) return "invalid_task";
  if (!Array.isArray(task.steps) || task.steps.length === 0) return "invalid_task";
  for (const step of task.steps) {
    if (!step || step.separate !== true) return "invalid_task";
    if (step.requests_7702 || step.unlimited_approval || step.adds_admin || step.migrates_assets || step.upgrades_account) {
      return "invalid_task";
    }
  }
  return null;
}

async function publishCredential(db, request, now) {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer wgpub_([a-f0-9]{16})\.([a-f0-9]{64})$/.exec(header);
  const presentedHash = match ? await sha256Hex(match[2]) : await dummyHash();
  const row = match
    ? await db
        .prepare(
          `SELECT credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at
           FROM publish_credentials WHERE credential_id = ?`,
        )
        .bind(match[1])
        .first()
    : null;
  const expected = row?.secret_hash ?? (await dummyHash());
  const secretOk = await timingSafeEqual(expected, presentedHash);
  if (!row || !secretOk || row.revoked_at != null || (row.expires_at != null && row.expires_at <= now)) return null;
  return { ...row, wallet_address: String(row.wallet_address).toLowerCase() };
}

export async function postCreate(request, env, db, now) {
  readConfig(env);
  const parsed = await readJson(request, 128 * 1024);
  if (parsed.error) return json(parsed.error === "too_large" ? 413 : 400, { error: parsed.error, execution_authorized: false });
  const credential = await publishCredential(db, request, now);
  if (!credential) return json(401, { error: "unauthenticated", execution_authorized: false });
  const body = parsed.value;
  if (body.tenant_id !== undefined && body.tenant_id !== credential.tenant_id) {
    return json(403, { error: "tenant_untrusted", execution_authorized: false });
  }
  if (body.wallet_address !== undefined) {
    const supplied = checksumAddress(body.wallet_address);
    if (!supplied || supplied.toLowerCase() !== credential.wallet_address) {
      return json(403, { error: "wallet_untrusted", execution_authorized: false });
    }
  }
  if (body.task && body.task.tenant_id !== undefined && body.task.tenant_id !== credential.tenant_id) {
    return json(403, { error: "tenant_untrusted", execution_authorized: false });
  }
  if (!Number.isSafeInteger(body.expires_at) || body.expires_at <= now || body.expires_at > now + MAX_TTL) {
    return json(400, { error: "invalid_expiry", execution_authorized: false });
  }
  const problem = rejectTask(body.task);
  if (problem) return json(400, { error: problem, execution_authorized: false, send_authorized: false });
  const task = JSON.parse(JSON.stringify(body.task));
  delete task.tenant_id;
  const ref = `ref_${randomHex(16)}`;
  task.ref = ref;
  task.execution_authorized = false;
  task.wallet_request_allowed = false;
  task.link = {
    issued: false,
    reason: "no_fixed_https_origin",
    url: null,
    ref,
    execution_authorized: false,
    contains_private_state: false,
  };
  if (task.evidence && typeof task.evidence === "object") {
    task.evidence.fixed_https_origin = false;
    task.evidence.mainnet_execution_authorized = false;
    task.evidence.mode_c_real_wallet = "UNSUPPORTED";
  }
  try {
    validatePlan(task.plan);
  } catch {
    return json(400, { error: "invalid_task", execution_authorized: false });
  }
  if (typeof task.plan.sender !== "string" || task.plan.sender.toLowerCase() !== credential.wallet_address) {
    return json(403, { error: "wallet_untrusted", execution_authorized: false });
  }
  if ((await digestBody(task.plan)) !== task.plan.digest) {
    return json(400, { error: "digest_mismatch", execution_authorized: false });
  }
  const payload = JSON.stringify(task);
  if (payload.length > 64 * 1024) return json(413, { error: "too_large", execution_authorized: false });
  const wallet = credential.wallet_address;
  const tenant = credential.tenant_id;
  await db.batch([
    db.prepare(
      "INSERT INTO tenant_members (tenant_id, wallet_address) VALUES (?, ?) ON CONFLICT DO NOTHING",
    ).bind(tenant, wallet),
    db.prepare(
      `INSERT INTO tasks
        (ref, tenant_id, wallet_address, revision, status, expires_at, payload_json, created_at, updated_at, executed)
       VALUES (?, ?, ?, 1, 'active', ?, ?, ?, ?, 0)`,
    ).bind(ref, tenant, wallet, body.expires_at, payload, now, now),
    db.prepare("INSERT INTO task_acl (ref, tenant_id, wallet_address) VALUES (?, ?, ?)").bind(ref, tenant, wallet),
  ]);
  return json(201, closed({
    ref,
    revision: 1,
    tenant_id: tenant,
    wallet_address: checksumAddress(wallet),
    status: "active",
  }));
}

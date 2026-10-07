import { readConfig } from "./db.mjs";
import {
  cookies,
  hostCookie,
  json,
  randomHex,
  readJson,
  sha256Hex,
  timingSafeEqual,
} from "./http.mjs";
import { buildSiweMessage, checksumAddress, readNonce, recoverAddress } from "./siwe.mjs";

const ATTEMPT = "__Host-wg_attempt";
const SESSION = "__Host-wg_session";

function clearCookie(name) {
  return hostCookie(name, "", 0);
}

export async function loadSession(db, request, now) {
  const token = cookies(request)[SESSION];
  if (!token || !/^[a-f0-9]{64}$/.test(token)) return null;
  const tokenHash = await sha256Hex(token);
  const row = await db
    .prepare(
      "SELECT token_hash, address, csrf_token, expires_at, revoked_at FROM sessions WHERE token_hash = ?",
    )
    .bind(tokenHash)
    .first();
  if (!row || row.revoked_at != null || row.expires_at <= now) return null;
  return row;
}

async function rateLimited(db, key, now, windowSeconds, limit) {
  const windowStart = now - (now % windowSeconds);
  const row = await db
    .prepare(
      `INSERT INTO rate_buckets (bucket_key, window_start, count)
       VALUES (?, ?, 1)
       ON CONFLICT(bucket_key) DO UPDATE SET
         window_start = excluded.window_start,
         count = CASE
           WHEN rate_buckets.window_start = excluded.window_start THEN rate_buckets.count + 1
           ELSE 1
         END
       RETURNING count`,
    )
    .bind(key, windowStart)
    .first();
  return !row || Number(row.count) > limit;
}

function publicIdentity(address) {
  return {
    authenticated: true,
    address: checksumAddress(address),
    erc1271: "unsupported",
    execution_authorized: false,
    send_authorized: false,
  };
}

export async function postChallenge(request, env, db, now) {
  const cfg = readConfig(env);
  const parsed = await readJson(request, 4096);
  if (parsed.error) return json(400, { error: parsed.error, execution_authorized: false });
  const body = parsed.value;
  if (body.domain !== undefined && body.domain !== cfg.domain) return json(400, { error: "domain_mismatch" });
  if (body.uri !== undefined && body.uri !== cfg.uri) return json(400, { error: "uri_mismatch" });
  if (body.chainId !== undefined && Number(body.chainId) !== cfg.chainId) return json(400, { error: "chain_mismatch" });
  if (body.account_type === "contract" || body.erc1271 === true) return json(400, { error: "erc1271_unsupported" });
  const checksum = checksumAddress(body.address);
  if (!checksum) return json(400, { error: "invalid_address" });
  const address = checksum.toLowerCase();
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  const windowStart = now - (now % cfg.challengeWindow);
  await db.prepare("DELETE FROM challenges WHERE expires_at <= ?").bind(now).run();
  await db.prepare("DELETE FROM rate_buckets WHERE window_start < ?").bind(windowStart).run();
  if (await rateLimited(db, `challenge:${ip}:${address}`, now, cfg.challengeWindow, cfg.challengeLimit)) {
    return json(429, { error: "rate_limited", execution_authorized: false });
  }
  const live = await db
    .prepare("SELECT COUNT(*) AS n FROM challenges WHERE consumed = 0 AND expires_at > ?")
    .bind(now)
    .first();
  if (!live || Number(live.n) >= cfg.challengeGlobalLimit) {
    return json(429, { error: "rate_limited", execution_authorized: false });
  }
  const nonce = randomHex(16);
  const attemptId = randomHex(32);
  const csrf = randomHex(32);
  const issuedAt = new Date(now * 1000).toISOString();
  const expirationTime = new Date((now + cfg.challengeTtl) * 1000).toISOString();
  const message = buildSiweMessage({
    domain: cfg.domain,
    address: checksum,
    uri: cfg.uri,
    chainId: cfg.chainId,
    nonce,
    issuedAt,
    expirationTime,
  });
  await db
    .prepare(
      `INSERT INTO challenges
        (nonce, attempt_id, csrf_token, address, domain, uri, chain_id, message, expires_at, consumed)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`,
    )
    .bind(nonce, attemptId, csrf, address, cfg.domain, cfg.uri, cfg.chainId, message, now + cfg.challengeTtl)
    .run();
  return json(
    200,
    {
      message,
      csrf_token: csrf,
      expires_at: now + cfg.challengeTtl,
      domain: cfg.domain,
      uri: cfg.uri,
      chain_id: cfg.chainId,
      address: checksum,
      erc1271: "unsupported",
      execution_authorized: false,
      send_authorized: false,
    },
    [["set-cookie", hostCookie(ATTEMPT, attemptId, cfg.challengeTtl)]],
  );
}

export async function postVerify(request, env, db, now) {
  const cfg = readConfig(env);
  const parsed = await readJson(request, 16 * 1024);
  if (parsed.error) return json(400, { error: parsed.error, execution_authorized: false });
  const body = parsed.value;
  if (body.account_type === "contract" || body.erc1271 === true || body.signature_type === "erc1271") {
    return json(400, { error: "erc1271_unsupported", execution_authorized: false });
  }
  if (typeof body.message !== "string" || typeof body.signature !== "string") {
    return json(400, { error: "invalid_request" });
  }
  const attempt = cookies(request)[ATTEMPT] ?? "";
  const csrf = request.headers.get("x-csrf-token") ?? "";
  const nonce = readNonce(body.message);
  if (!attempt || !nonce) return json(401, { error: "attempt_mismatch", execution_authorized: false });
  const row = await db
    .prepare(
      `SELECT nonce, attempt_id, csrf_token, address, domain, uri, chain_id, message, expires_at
       FROM challenges WHERE nonce = ?`,
    )
    .bind(nonce)
    .first();
  if (!row || row.expires_at <= now) return json(401, { error: "challenge_expired", execution_authorized: false });
  if (!(await timingSafeEqual(row.attempt_id, attempt))) {
    return json(401, { error: "attempt_mismatch", execution_authorized: false });
  }
  if (!(await timingSafeEqual(row.csrf_token, csrf))) {
    return json(403, { error: "csrf_required", execution_authorized: false });
  }
  if (!(await timingSafeEqual(row.message, body.message))) {
    return json(401, { error: "message_mismatch", execution_authorized: false });
  }
  if (row.domain !== cfg.domain || row.uri !== cfg.uri || Number(row.chain_id) !== cfg.chainId) {
    return json(401, { error: "message_mismatch", execution_authorized: false });
  }
  const recovered = recoverAddress(row.message, body.signature);
  if (recovered.error) {
    const status = recovered.error === "erc1271_unsupported" ? 400 : 401;
    return json(status, { error: recovered.error, execution_authorized: false });
  }
  if (recovered.address.toLowerCase() !== row.address) {
    return json(401, { error: "signature_mismatch", execution_authorized: false });
  }
  const consumed = await db
    .prepare("UPDATE challenges SET consumed = 1 WHERE nonce = ? AND consumed = 0 AND expires_at > ?")
    .bind(nonce, now)
    .run();
  if (Number(consumed.meta.changes) !== 1) {
    return json(401, { error: "nonce_replay", execution_authorized: false });
  }
  const token = randomHex(32);
  const sessionCsrf = randomHex(32);
  const expires = now + cfg.sessionTtl;
  await db
    .prepare(
      `INSERT INTO sessions (token_hash, address, csrf_token, created_at, expires_at, revoked_at)
       VALUES (?, ?, ?, ?, ?, NULL)`,
    )
    .bind(await sha256Hex(token), row.address, sessionCsrf, now, expires)
    .run();
  return json(
    200,
    { ...publicIdentity(row.address), expires_at: expires, csrf_token: sessionCsrf },
    [
      ["set-cookie", hostCookie(SESSION, token, cfg.sessionTtl)],
      ["set-cookie", clearCookie(ATTEMPT)],
    ],
  );
}

export async function postLogout(request, env, db, now) {
  readConfig(env);
  const parsed = await readJson(request, 1024);
  if (parsed.error) return json(400, { error: parsed.error, execution_authorized: false });
  const session = await loadSession(db, request, now);
  if (!session) {
    return json(401, { authenticated: false, error: "unauthenticated", execution_authorized: false }, [
      ["set-cookie", clearCookie(SESSION)],
    ]);
  }
  const csrf = request.headers.get("x-csrf-token") ?? "";
  if (!(await timingSafeEqual(session.csrf_token, csrf))) {
    return json(403, { error: "csrf_required", execution_authorized: false });
  }
  await db
    .prepare("UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL")
    .bind(now, session.token_hash)
    .run();
  return json(200, { authenticated: false, revoked: true, erc1271: "unsupported", execution_authorized: false }, [
    ["set-cookie", clearCookie(SESSION)],
  ]);
}

export async function getSession(request, db, now) {
  const session = await loadSession(db, request, now);
  if (!session) return json(200, { authenticated: false, erc1271: "unsupported", execution_authorized: false });
  return json(200, {
    ...publicIdentity(session.address),
    expires_at: session.expires_at,
    csrf_token: session.csrf_token,
  });
}

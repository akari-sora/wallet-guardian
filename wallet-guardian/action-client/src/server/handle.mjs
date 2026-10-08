import { getSession, postChallenge, postLogout, postVerify } from "./auth.mjs";
import { primary, privateApiConfigured, readConfig } from "./db.mjs";
import { internalError, json, nowSeconds } from "./http.mjs";
import { getTask, postCancel, postCreate } from "./tasks.mjs";

const ROUTES = [
  { method: "POST", pattern: /^\/api\/auth\/challenge$/, origin: true, handler: (request, env, db, now) => postChallenge(request, env, db, now) },
  { method: "POST", pattern: /^\/api\/auth\/verify$/, origin: true, handler: (request, env, db, now) => postVerify(request, env, db, now) },
  { method: "POST", pattern: /^\/api\/auth\/logout$/, origin: true, handler: (request, env, db, now) => postLogout(request, env, db, now) },
  { method: "GET", pattern: /^\/api\/session$/, origin: false, handler: (request, _env, db, now) => getSession(request, db, now) },
  { method: "GET", pattern: /^\/api\/tasks\/(ref_[a-f0-9]{32})$/, origin: false, handler: (request, _env, db, now, match) => getTask(request, db, now, match[1]) },
  { method: "POST", pattern: /^\/api\/tasks\/(ref_[a-f0-9]{32})\/cancel$/, origin: true, handler: (request, _env, db, now, match) => postCancel(request, db, now, match[1]) },
  { method: "POST", pattern: /^\/api\/bot\/tasks$/, origin: false, handler: (request, env, db, now) => postCreate(request, env, db, now) },
];

async function route(request, env) {
  if (request.headers.get("sec-fetch-site") === "cross-site") {
    return json(403, { error: "origin_required", execution_authorized: false });
  }
  const path = new URL(request.url).pathname;
  if (path === "/api/rpc" || path.startsWith("/api/rpc/")) {
    return json(404, { error: "rpc_not_supported", execution_authorized: false });
  }
  if (/^\/api\/tasks\/[^/]+\/complete$/.test(path)) {
    return json(404, { error: "complete_not_supported", execution_authorized: false, tx_hash_verified: false });
  }
  if (!path.startsWith("/api")) return json(404, { error: "not_found", execution_authorized: false });
  // Preview deployments carry no D1 binding or AUTH audience: the private API stays unavailable there.
  if (!privateApiConfigured(env)) {
    return json(503, { error: "private_api_unavailable", execution_authorized: false });
  }

  let matched = false;
  for (const entry of ROUTES) {
    const match = entry.pattern.exec(path);
    if (!match) continue;
    matched = true;
    if (request.method !== entry.method) continue;
    if (entry.origin) {
      const cfg = readConfig(env);
      if (request.headers.get("origin") !== cfg.origin) {
        return json(403, { error: "origin_required", execution_authorized: false });
      }
    }
    return entry.handler(request, env, primary(env), nowSeconds(), match);
  }
  if (matched) return json(405, { error: "method_not_allowed", execution_authorized: false });
  return json(404, { error: "not_found", execution_authorized: false });
}

export async function handleApi(request, env) {
  try {
    const response = await route(request, env);
    if (!(response instanceof Response)) return internalError();
    return response;
  } catch {
    return internalError();
  }
}

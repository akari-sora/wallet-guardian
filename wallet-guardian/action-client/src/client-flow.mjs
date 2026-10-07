/** Shown when cancel timed out or the connection dropped. Not a success or failure claim. */
export const CANCEL_PENDING_CONFIRMATION =
  "取消結果待確認。正在重新讀取這筆任務。這次沒有證據可以說已取消或沒有取消。";

const CSRF = /^[a-f0-9]{64}$/;

export function createClientFlow() {
  return {
    loadGeneration: 0,
    sessionEpoch: 0,
    observedCapability: /** @type {string} */ ("unknown"),
    lastPlan: /** @type {object | null} */ (null),
    lastTask: /** @type {object | null} */ (null),
    sessionCsrf: /** @type {string | null} */ (null),
    attemptCsrf: /** @type {string | null} */ (null),
    loginInFlight: false,
    loginAttempt: 0,
    privateRef: /** @type {string | null} */ (null),
    privateRevision: /** @type {number | null} */ (null),
    privateCancelAllowed: false,
  };
}

export function viewStamp(flow) {
  return {
    generation: flow.loadGeneration,
    epoch: flow.sessionEpoch,
    ref: flow.privateRef,
    revision: flow.privateRevision,
  };
}

/** True only when this completion still belongs to the same view, session, and known task revision. */
export function stampMatches(flow, stamp) {
  if (!stamp || typeof stamp !== "object") return false;
  if (stamp.generation !== flow.loadGeneration) return false;
  if (stamp.epoch !== flow.sessionEpoch) return false;
  if (stamp.ref !== flow.privateRef) return false;
  if (stamp.revision != null && stamp.revision !== flow.privateRevision) return false;
  return true;
}

export function invalidatePrivateView(flow) {
  flow.loadGeneration += 1;
  flow.lastPlan = null;
  flow.lastTask = null;
  flow.observedCapability = "unknown";
  flow.privateCancelAllowed = false;
  return flow.loadGeneration;
}

export function beginPresent(flow) {
  flow.privateRevision = null;
  flow.privateCancelAllowed = false;
  invalidatePrivateView(flow);
  return viewStamp(flow);
}

export function beginTaskLoad(flow, ref) {
  flow.privateRef = ref;
  flow.privateRevision = null;
  flow.privateCancelAllowed = false;
  invalidatePrivateView(flow);
  return viewStamp(flow);
}

export function clearPresented(flow) {
  flow.privateRef = null;
  flow.privateRevision = null;
  invalidatePrivateView(flow);
  return viewStamp(flow);
}

export function rememberPresented(flow, stamp, plan, task) {
  if (!stampMatches(flow, stamp)) return false;
  flow.lastPlan = plan;
  flow.lastTask = task;
  return true;
}

export function canRedrawCapability(flow, stamp) {
  return stampMatches(flow, stamp) && flow.lastPlan != null;
}

/** Drop in-flight work immediately. Keeps the session CSRF so logout can still be authorized. */
export function beginLogout(flow) {
  const csrf = flow.sessionCsrf;
  flow.loginInFlight = false;
  flow.attemptCsrf = null;
  invalidatePrivateView(flow);
  return csrf;
}

export function finishLogout(flow) {
  flow.sessionCsrf = null;
  flow.attemptCsrf = null;
  flow.loginInFlight = false;
  flow.privateRef = null;
  flow.privateRevision = null;
  flow.sessionEpoch += 1;
  invalidatePrivateView(flow);
}

export function beginLogin(flow) {
  if (flow.loginInFlight) return null;
  flow.loginInFlight = true;
  flow.loginAttempt += 1;
  flow.attemptCsrf = null;
  return flow.loginAttempt;
}

export function loginStillCurrent(flow, attempt) {
  return flow.loginInFlight === true && flow.loginAttempt === attempt;
}

/** Challenge CSRF is only for verify. It must not replace a live session token. */
export function noteAttemptCsrf(flow, token) {
  if (!flow.loginInFlight) return false;
  if (typeof token !== "string" || !CSRF.test(token)) return false;
  flow.attemptCsrf = token;
  return true;
}

export function endLogin(flow) {
  flow.loginInFlight = false;
  flow.attemptCsrf = null;
}

export function adoptSession(flow, csrf) {
  if (typeof csrf !== "string" || !CSRF.test(csrf)) return false;
  flow.sessionCsrf = csrf;
  flow.attemptCsrf = null;
  flow.loginInFlight = false;
  flow.sessionEpoch += 1;
  return true;
}

export function mutationCsrf(flow, purpose) {
  if (purpose === "verify") return flow.attemptCsrf;
  return flow.sessionCsrf;
}

export function applyUnauthenticated(flow, stamp) {
  if (!stampMatches(flow, stamp)) return false;
  flow.sessionCsrf = null;
  return true;
}

export function acceptCancelResult(flow, stamp, body) {
  if (!stampMatches(flow, stamp)) return false;
  if (typeof stamp.ref !== "string" || stamp.revision == null) return false;
  if (!body || typeof body !== "object") return false;
  if (body.ref !== stamp.ref) return false;
  if (!Number.isSafeInteger(body.revision) || body.revision !== stamp.revision) return false;
  if (body.status !== "cancelled") return false;
  if (body.cancel_allowed !== false) return false;
  if (body.execution_authorized !== false) return false;
  if (body.send_authorized !== false) return false;
  if (body.tx_hash_verified !== false) return false;
  if (body.on_chain_revoked !== false) return false;
  return true;
}

export function resolveCancel(flow, stamp, outcome) {
  if (!stampMatches(flow, stamp)) return "stale";
  if (acceptCancelResult(flow, stamp, outcome && outcome.body)) {
    flow.privateCancelAllowed = false;
    flow.lastPlan = null;
    flow.lastTask = null;
    flow.observedCapability = "unknown";
    flow.loadGeneration += 1;
    return "cancelled";
  }
  if (outcome && outcome.ok === true) return "rejected";
  return "failed";
}

export function isCancelUnconfirmed(error) {
  if (!error || typeof error !== "object") return false;
  const name = error.name;
  if (name === "TimeoutError" || name === "AbortError" || name === "NetworkError") return true;
  if (name === "TypeError") return true;
  return false;
}

export function resolveCancelError(error) {
  return isCancelUnconfirmed(error) ? "pending" : "failed";
}

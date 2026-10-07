import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  CANCEL_PENDING_CONFIRMATION,
  adoptSession,
  applyUnauthenticated,
  beginLogin,
  beginLogout,
  beginTaskLoad,
  canRedrawCapability,
  createClientFlow,
  endLogin,
  finishLogout,
  loginStillCurrent,
  mutationCsrf,
  noteAttemptCsrf,
  rememberPresented,
  resolveCancel,
  resolveCancelError,
  stampMatches,
  viewStamp,
} from "./client-flow.mjs";

const REF = `ref_${"ab".repeat(16)}`;
const SESSION = "ab".repeat(32);
const ATTEMPT = "cd".repeat(32);

function signedIn() {
  const flow = createClientFlow();
  assert.equal(adoptSession(flow, SESSION), true);
  const stamp = beginTaskLoad(flow, REF);
  assert.equal(rememberPresented(flow, stamp, { id: "plan" }, { ref: REF }), true);
  flow.privateRevision = 3;
  flow.privateCancelAllowed = true;
  return { flow, stamp: viewStamp(flow) };
}

test("stale task GET after logout does not paint the private plan", () => {
  const { flow, stamp } = signedIn();
  assert.equal(stampMatches(flow, stamp), true);
  const csrf = beginLogout(flow);
  assert.equal(csrf, SESSION);
  assert.equal(stampMatches(flow, stamp), false);
  assert.equal(rememberPresented(flow, stamp, { id: "stale" }, { ref: REF }), false);
  assert.equal(flow.lastPlan, null);
  assert.equal(flow.lastTask, null);
  finishLogout(flow);
  assert.equal(flow.sessionCsrf, null);
  assert.equal(applyUnauthenticated(flow, stamp), false);
  assert.equal(flow.lastPlan, null);
});

test("capability completion after logout does not redraw from the cached plan", () => {
  const { flow, stamp } = signedIn();
  assert.equal(canRedrawCapability(flow, stamp), true);
  beginLogout(flow);
  assert.equal(flow.observedCapability, "unknown");
  assert.equal(flow.lastPlan, null);
  assert.equal(canRedrawCapability(flow, stamp), false);
  assert.equal(rememberPresented(flow, stamp, { id: "redraw" }, { ref: REF }), false);
});

test("refusing personal_sign keeps the session CSRF that logout will send", () => {
  const flow = createClientFlow();
  assert.equal(adoptSession(flow, SESSION), true);
  const attempt = beginLogin(flow);
  assert.equal(typeof attempt, "number");
  assert.equal(beginLogin(flow), null);
  assert.equal(noteAttemptCsrf(flow, ATTEMPT), true);
  assert.equal(flow.sessionCsrf, SESSION);
  assert.equal(mutationCsrf(flow, "verify"), ATTEMPT);
  assert.equal(mutationCsrf(flow, "logout"), SESSION);
  assert.equal(loginStillCurrent(flow, attempt), true);
  endLogin(flow);
  assert.equal(flow.sessionCsrf, SESSION);
  assert.equal(flow.attemptCsrf, null);
  assert.equal(loginStillCurrent(flow, attempt), false);
  assert.equal(mutationCsrf(flow, "logout"), SESSION);
  const csrf = beginLogout(flow);
  assert.equal(csrf, SESSION);
  assert.notEqual(csrf, ATTEMPT);
});

test("malformed cancel 200 is not treated as cancelled", () => {
  const { flow, stamp } = signedIn();
  const flags = {
    execution_authorized: false,
    send_authorized: false,
    tx_hash_verified: false,
    on_chain_revoked: false,
  };
  assert.equal(resolveCancel(flow, stamp, { ok: true, body: {} }), "rejected");
  assert.equal(flow.privateCancelAllowed, true);
  assert.equal(
    resolveCancel(flow, stamp, {
      ok: true,
      body: { ref: `ref_${"ff".repeat(16)}`, revision: 3, status: "active", cancel_allowed: true, ...flags },
    }),
    "rejected",
  );
  assert.equal(
    resolveCancel(flow, stamp, {
      ok: true,
      body: { ref: REF, revision: 9, status: "active", cancel_allowed: false, ...flags },
    }),
    "rejected",
  );
  assert.equal(flow.lastPlan && flow.lastPlan.id, "plan");
  assert.equal(
    resolveCancel(flow, stamp, {
      ok: true,
      body: { ref: REF, revision: 3, status: "cancelled", cancel_allowed: false, ...flags },
    }),
    "cancelled",
  );
  assert.equal(flow.privateCancelAllowed, false);
  assert.equal(stampMatches(flow, stamp), false);
});

test("cancel timeout asks to re-read and does not claim success or failure", () => {
  const timeout = new DOMException("The operation was aborted due to timeout", "TimeoutError");
  const dropped = new TypeError("Failed to fetch");
  assert.equal(resolveCancelError(timeout), "pending");
  assert.equal(resolveCancelError(dropped), "pending");
  assert.equal(resolveCancelError(new Error("too large")), "failed");
  assert.equal(CANCEL_PENDING_CONFIRMATION.includes("待確認"), true);
  assert.equal(CANCEL_PENDING_CONFIRMATION.includes("未執行任務已取消"), false);
  assert.equal(CANCEL_PENDING_CONFIRMATION.includes("任務沒有取消"), false);
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  assert.equal(source.includes("CANCEL_PENDING_CONFIRMATION"), true);
  assert.equal(source.includes("isCancelUnconfirmed"), true);
  assert.equal(source.includes("loadPrivateTask(ref, stamp)"), true);
  assert.equal(source.includes("response.ok"), true);
  const cancelStart = source.indexOf("async function cancelPrivateTask");
  const cancelBody = source.slice(cancelStart, source.indexOf("function inspectLocation"));
  assert.equal(cancelBody.includes("未執行任務已取消"), true);
  assert.equal(cancelBody.includes("outcome === \"cancelled\""), true);
  assert.equal(cancelBody.includes("任務沒有取消"), true);
  const timeoutArm = cancelBody.indexOf("isCancelUnconfirmed");
  assert.equal(timeoutArm >= 0, true);
  assert.equal(cancelBody.slice(timeoutArm, timeoutArm + 180).includes("任務沒有取消"), false);
});

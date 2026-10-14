import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { clientConfirmDecision } from "./gate.mjs";
import { LOGIN_STATEMENT } from "./login-statement.mjs";
import { parsePrivateTaskRef, privateTaskPathFormatError, privateTaskSendAllowed, readBoundedJson } from "./private-task.mjs";

test("private task paths stay on /t/<ref>", () => {
  const ref = `ref_${"ab".repeat(16)}`;
  assert.equal(parsePrivateTaskRef(`/t/${ref}`), ref);
  assert.equal(parsePrivateTaskRef(`/t/${ref}/`), ref);
  assert.equal(parsePrivateTaskRef(`/t/${ref}/extra`), null);
  assert.equal(parsePrivateTaskRef(`/?ref=${ref}`), null);
  assert.equal(parsePrivateTaskRef(`/tasks/${ref}.json`), null);
  assert.equal(parsePrivateTaskRef(`/api/tasks/${ref}`), null);
  assert.equal(parsePrivateTaskRef(`/t/ref_${"AB".repeat(16)}`), null);
  assert.equal(parsePrivateTaskRef(`/t/ref_${"a".repeat(31)}`), null);
  assert.equal(privateTaskPathFormatError(`/t/${ref}`), null);
  assert.equal(typeof privateTaskPathFormatError(`/t/ref_${"AB".repeat(16)}`), "string");
  assert.equal(typeof privateTaskPathFormatError(`/t/ref_${"a".repeat(31)}`), "string");
  assert.equal(typeof privateTaskPathFormatError(`/t/${ref}/x`), "string");
});

test("preflight_ok alone does not authorize send", () => {
  const task = {
    mode: "A",
    requires_user_wallet_confirmation: true,
    preflight: { preflight_ok: true, revision_required: false, changes: [] },
    wallet_request_allowed: true,
    execution_authorized: true,
    plan: {
      wallet_request_allowed: true,
      block_reasons: [],
      simulation: {
        status: "PASS",
        effects_checked: true,
        usable_as_live_evidence: true,
        state_overrides: false,
      },
    },
  };
  const decision = clientConfirmDecision({ plan: task.plan, task, mainnetSend: true, mode: "A" });
  assert.equal(decision.buttonEnabled, false);
  assert.equal(decision.sendAllowed, false);
  assert.equal(decision.reason, "preflight_not_ok");
  assert.equal(decision.agentMaySign, false);
  assert.equal(privateTaskSendAllowed(task, true), false);
  assert.equal(privateTaskSendAllowed({ ...task, mode: "C" }, true), false);
});

test("client source does not fetch static tasks or send transactions", () => {
  const source = readFileSync(new URL("./main.ts", import.meta.url), "utf8");
  assert.equal(source.includes("`/tasks/"), false);
  assert.equal(source.includes('"/tasks/'), false);
  assert.equal(source.includes("/api/tasks/"), true);
  assert.equal(source.includes("eth_sendTransaction"), false);
  assert.equal(source.includes("eth_sendRawTransaction"), false);
  assert.equal(source.includes("wallet_sendCalls"), false);
  assert.equal(source.includes("eth_signTypedData"), false);
  assert.equal(source.includes("personal_sign"), true);
  assert.equal(source.includes("acceptLoginChallenge"), true);
  assert.equal(source.includes("beginLogout"), true);
  assert.equal(source.includes("noteAttemptCsrf"), true);
  assert.equal(source.includes("canRedrawCapability"), true);
  assert.equal(source.includes("resolveCancel"), true);
  assert.equal(source.includes("stampMatches"), true);
  assert.equal(source.includes(".includes(LOGIN_STATEMENT)"), false);
  assert.equal(LOGIN_STATEMENT.includes("does not authorize sending"), true);
  assert.equal(source.includes("parsePrivateTaskRef"), true);
});

test("readBoundedJson cancels and releases the reader when the body is too large", async () => {
  let cancelled = false;
  let released = false;
  const response = {
    headers: { get: () => "application/json" },
    body: {
      getReader() {
        return {
          async read() {
            return { done: false, value: new Uint8Array(64) };
          },
          async cancel() {
            cancelled = true;
          },
          releaseLock() {
            released = true;
          },
        };
      },
    },
  };
  await assert.rejects(() => readBoundedJson(response, 32), /too large/);
  assert.equal(cancelled, true);
  assert.equal(released, true);
});

import assert from "node:assert/strict";
import { clientConfirmDecision, confirmDecision, eip5792Status, executionMode, listaCallsOnly, simulationAllowsSend } from "./gate.mjs";

const clearPlan = {
  walletRequestAllowed: true,
  simulationComplete: true,
  planBlocked: false,
};

const off = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: false,
  mode: "A",
  ...clearPlan,
});
assert.equal(off.buttonEnabled, true);
assert.equal(off.sendAllowed, false);
assert.equal(off.reason, "mainnet_send_disabled");
assert.equal(off.agentMaySign, false);

const closed = confirmDecision({
  requiresUserWalletConfirmation: false,
  preflightOk: true,
  mainnetSend: true,
  mode: "A",
});
assert.equal(closed.buttonEnabled, false);

const stale = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: false,
  mainnetSend: true,
  mode: "A",
});
assert.equal(stale.buttonEnabled, false);
assert.equal(stale.reason, "preflight_not_ok");

const modeC = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: true,
  mode: "C",
});
assert.equal(modeC.buttonEnabled, false);
assert.equal(modeC.reason, "mode_c_unsupported");

const forgedPreflight = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: true,
  mode: "A",
  walletRequestAllowed: false,
  simulationComplete: false,
  planBlocked: false,
});
assert.equal(forgedPreflight.buttonEnabled, false);
assert.equal(forgedPreflight.sendAllowed, false);
assert.equal(forgedPreflight.reason, "wallet_request_denied");
assert.equal(forgedPreflight.agentMaySign, false);

const incompleteSimulation = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: true,
  mode: "A",
  walletRequestAllowed: true,
  simulationComplete: false,
  planBlocked: false,
});
assert.equal(incompleteSimulation.sendAllowed, false);
assert.equal(incompleteSimulation.reason, "simulation_incomplete");

const blockedPlan = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: true,
  mode: "A",
  walletRequestAllowed: true,
  simulationComplete: true,
  planBlocked: true,
});
assert.equal(blockedPlan.sendAllowed, false);
assert.equal(blockedPlan.reason, "plan_blocked");

const omittedDenials = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: true,
  mode: "A",
});
assert.equal(omittedDenials.sendAllowed, false);
assert.equal(omittedDenials.reason, "wallet_request_denied");

assert.equal(simulationAllowsSend({ status: "INCOMPLETE", effects_checked: false }), false);
assert.equal(simulationAllowsSend({ status: "PASS", effects_checked: true, usable_as_live_evidence: true, state_overrides: true }), false);
assert.equal(simulationAllowsSend({ status: "PASS", effects_checked: false, usable_as_live_evidence: true }), false);
assert.equal(
  simulationAllowsSend({ status: "PASS", effects_checked: true, usable_as_live_evidence: true }),
  true,
);

const forgedTask = clientConfirmDecision({
  plan: {
    wallet_request_allowed: false,
    simulation: { status: "INCOMPLETE", effects_checked: false },
    block_reasons: ["unsigned"],
  },
  task: {
    mode: "A",
    requires_user_wallet_confirmation: true,
    preflight: { preflight_ok: true, revision_required: false, changes: [] },
  },
  mainnetSend: true,
  mode: "A",
});
assert.equal(forgedTask.buttonEnabled, false);
assert.equal(forgedTask.sendAllowed, false);
assert.equal(forgedTask.agentMaySign, false);

const preflightAlone = clientConfirmDecision({
  plan: {
    wallet_request_allowed: true,
    simulation: { status: "PASS", effects_checked: true, usable_as_live_evidence: true, state_overrides: false },
    block_reasons: [],
  },
  task: {
    mode: "A",
    requires_user_wallet_confirmation: true,
    preflight: { preflight_ok: true, revision_required: false, changes: [] },
  },
  mainnetSend: true,
  mode: "A",
});
assert.equal(preflightAlone.sendAllowed, false);
assert.equal(preflightAlone.buttonEnabled, false);
assert.equal(preflightAlone.reason, "preflight_not_ok");

const missingBlockReasons = clientConfirmDecision({
  plan: {
    wallet_request_allowed: true,
    simulation: { status: "PASS", effects_checked: true, usable_as_live_evidence: true, state_overrides: false },
  },
  task: {
    mode: "A",
    requires_user_wallet_confirmation: true,
    preflight: { preflight_ok: true, revision_required: false, changes: [] },
  },
  mainnetSend: true,
  mode: "A",
});
assert.equal(missingBlockReasons.buttonEnabled, false);
assert.equal(missingBlockReasons.sendAllowed, false);

assert.equal(eip5792Status({ "0x38": { atomic: { status: "unsupported" } } }, 56), "unsupported");
assert.equal(executionMode({ capability: "unsupported", callCount: 2, sameChain: true }), "A");
assert.equal(executionMode({ capability: "supported", callCount: 2, sameChain: true }), "B");
assert.equal(executionMode({ capability: "supported", callCount: 1, sameChain: true }), "A");
assert.equal(executionMode({ capability: "ready", callCount: 2, sameChain: true }), "A");
assert.equal(eip5792Status({ "0x1": { atomic: { status: "supported" } } }, 56), "unsupported");

assert.equal(
  listaCallsOnly([
    {
      to: "0x1adB950d8bB3dA4bE104211D5AB038628e477fE6",
      data: "0xb13acedd0000000000000000000000000000000000000000000000000000000000000000",
      value: "0",
    },
  ]),
  true,
);
assert.equal(listaCallsOnly([{ to: "0x2222222222222222222222222222222222222222", data: "0xb13acedd", value: "0" }]), false);

console.log("gate ok");

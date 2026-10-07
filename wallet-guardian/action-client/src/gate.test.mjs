import assert from "node:assert/strict";
import { confirmDecision, eip5792Status, executionMode, listaCallsOnly } from "./gate.mjs";

const off = confirmDecision({
  requiresUserWalletConfirmation: true,
  preflightOk: true,
  mainnetSend: false,
  mode: "A",
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

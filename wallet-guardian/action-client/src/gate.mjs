/** Fixed-client gate. Keep in lockstep with guardian.actions.workbench.confirm_gate. */

export const CLIENT_BUILD = "action-client@0.3.0";
export const LISTA_PROXY = "0x1adb950d8bb3da4be104211d5ab038628e477fe6";
export const CLAIM_SELECTOR = "0xb13acedd";

export function simulationAllowsSend(simulation) {
  if (!simulation || typeof simulation !== "object") return false;
  return (
    simulation.effects_checked === true &&
    simulation.status === "PASS" &&
    simulation.usable_as_live_evidence === true &&
    simulation.state_overrides !== true
  );
}

export function confirmDecision({
  requiresUserWalletConfirmation,
  preflightOk,
  mainnetSend,
  mode,
  walletRequestAllowed = false,
  simulationComplete = false,
  planBlocked = true,
}) {
  if (mode === "C") {
    return { buttonEnabled: false, sendAllowed: false, reason: "mode_c_unsupported", agentMaySign: false };
  }
  if (!requiresUserWalletConfirmation) {
    return {
      buttonEnabled: false,
      sendAllowed: false,
      reason: "requires_user_wallet_confirmation_false",
      agentMaySign: false,
    };
  }
  if (!preflightOk) {
    return { buttonEnabled: false, sendAllowed: false, reason: "preflight_not_ok", agentMaySign: false };
  }
  // Task preflight booleans are not enough. Explicit denials win, including the
  // fail-closed defaults when a caller omits them.
  if (walletRequestAllowed !== true) {
    return { buttonEnabled: false, sendAllowed: false, reason: "wallet_request_denied", agentMaySign: false };
  }
  if (simulationComplete !== true) {
    return { buttonEnabled: false, sendAllowed: false, reason: "simulation_incomplete", agentMaySign: false };
  }
  if (planBlocked !== false) {
    return { buttonEnabled: false, sendAllowed: false, reason: "plan_blocked", agentMaySign: false };
  }
  if (!mainnetSend) {
    return { buttonEnabled: true, sendAllowed: false, reason: "mainnet_send_disabled", agentMaySign: false };
  }
  return { buttonEnabled: true, sendAllowed: true, reason: null, agentMaySign: false };
}

/** Task JSON preflight is ignored. A client re-read is not passed in and is not send authority. */
export function clientConfirmDecision({ plan, task, mainnetSend, mode }) {
  const resolvedMode = task && task.mode === "C" ? "C" : mode;
  return confirmDecision({
    requiresUserWalletConfirmation: Boolean(task && task.requires_user_wallet_confirmation === true),
    preflightOk: false,
    mainnetSend: mainnetSend === true,
    mode: resolvedMode,
    walletRequestAllowed: Boolean(plan && plan.wallet_request_allowed === true),
    simulationComplete: simulationAllowsSend(plan && plan.simulation),
    planBlocked: !Array.isArray(plan && plan.block_reasons) || plan.block_reasons.length > 0,
  });
}

export function eip5792Status(capabilities, chainId) {
  if (capabilities == null) return "unknown";
  const entry = capabilities[hexChain(chainId)] ?? capabilities[String(chainId)];
  if (!entry || typeof entry !== "object") return "unsupported";
  const status = entry.atomic && entry.atomic.status;
  if (status === "supported" || status === "ready" || status === "unsupported") return status;
  return "unknown";
}

export function executionMode({ capability, callCount, sameChain }) {
  if (capability === "supported" && sameChain && callCount > 1) return "B";
  return "A";
}

export function listaCallsOnly(calls) {
  if (!Array.isArray(calls) || calls.length === 0) return false;
  return calls.every(
    (call) =>
      String(call.to).toLowerCase() === LISTA_PROXY &&
      String(call.data).toLowerCase().startsWith(CLAIM_SELECTOR) &&
      String(call.value) === "0",
  );
}

function hexChain(chainId) {
  return `0x${Number(chainId).toString(16)}`;
}

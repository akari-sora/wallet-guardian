/** Fixed-client gate. Keep in lockstep with guardian.actions.workbench.confirm_gate. */

export const CLIENT_BUILD = "action-client@0.3.0";
export const LISTA_PROXY = "0x1adb950d8bb3da4be104211d5ab038628e477fe6";
export const CLAIM_SELECTOR = "0xb13acedd";

export function confirmDecision({ requiresUserWalletConfirmation, preflightOk, mainnetSend, mode }) {
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
  if (!mainnetSend) {
    return { buttonEnabled: true, sendAllowed: false, reason: "mainnet_send_disabled", agentMaySign: false };
  }
  return { buttonEnabled: true, sendAllowed: true, reason: null, agentMaySign: false };
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

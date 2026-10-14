/**
 * Send-variant entry. The default main.ts must not import this file.
 * requestModeAClaim runs only when the build flag is mode_a. The default build
 * defines that flag as "off" and does not put this module on its graph.
 */
import { requestModeAClaim } from "./mode-a-request.mjs";
import { issueModeAClaimCapability } from "./request-policy-allow.mjs";
import { walletScreenChecklist } from "./wallet-screen.mjs";
import { TRUSTED_LISTA } from "../trusted-lista.mjs";

function readSendVariant() {
  if (typeof __WG_SEND_VARIANT__ === "string" && __WG_SEND_VARIANT__ === "mode_a") return "mode_a";
  return "off";
}

function screenFor(preparedClaim, record) {
  const data = preparedClaim && typeof preparedClaim.data === "string" ? preparedClaim.data : "";
  const chainId = record && record.chainId;
  return {
    to: preparedClaim && preparedClaim.to,
    value: preparedClaim && preparedClaim.value === "0x0" ? 0 : preparedClaim && preparedClaim.value,
    function: data.startsWith("0xb13acedd") ? "claimWithdraw" : "",
    chain: chainId === TRUSTED_LISTA.chainId ? "BNB Smart Chain" : "",
    chainId,
  };
}

export async function sendVariantClaim(connection, preparedClaim, record) {
  if (readSendVariant() !== "mode_a") {
    throw new Error("send variant is not this build");
  }
  const screen = walletScreenChecklist(screenFor(preparedClaim, record));
  if (!screen.ok) {
    const error = new Error("wallet screen refusal");
    error.reason = screen.refusal;
    throw error;
  }
  const capability = issueModeAClaimCapability(record);
  return requestModeAClaim(connection, preparedClaim, capability);
}

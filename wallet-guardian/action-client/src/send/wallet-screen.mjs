/**
 * Pure checklist for the wallet confirmation screen.
 * A mismatch is a refusal. The only address is the existing Lista proxy.
 */
import { TRUSTED_LISTA } from "../trusted-lista.mjs";

const CHAIN_NAME = "BNB Smart Chain";

export function walletScreenChecklist(shown) {
  const expected = {
    to: TRUSTED_LISTA.proxy,
    value: 0,
    function: "claimWithdraw",
    chain: CHAIN_NAME,
    chainId: TRUSTED_LISTA.chainId,
  };
  const lines = [
    `to = ${expected.to}`,
    "value 0",
    "function claimWithdraw",
    `${CHAIN_NAME} (chain id ${expected.chainId})`,
  ];
  const mismatches = [];
  if (!shown || shown.to !== expected.to) mismatches.push("to");
  if (!shown || (shown.value !== 0 && shown.value !== "0")) mismatches.push("value");
  const functionName = shown && (shown.function ?? shown.functionName);
  if (functionName !== "claimWithdraw") mismatches.push("function");
  const chainLabel = shown && (shown.chain ?? shown.chainName);
  if (!shown || chainLabel !== expected.chain || shown.chainId !== expected.chainId) mismatches.push("chain");
  if (mismatches.length > 0) {
    return { ok: false, refusal: "wallet_screen_mismatch", mismatches, expected, lines };
  }
  return { ok: true, refusal: null, ...expected, lines };
}

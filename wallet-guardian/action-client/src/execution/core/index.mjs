/**
 * Client core entry (S-1b). Builds and checks calldata only.
 * No provider, no wallet call, no signing, no broadcast. The core build
 * (vite.core.config.mjs) bundles this entry and the marker scan must report 0.
 */
export { CoreRefusal } from "./errors.mjs";
export { parseSignature, encodeCall, decodeCall } from "./abi.mjs";
export { DENIED_SIGNATURES, DENIED_NAME_PREFIXES, deniedSelectorCategory, deniedName } from "./deny.mjs";
export { SEND_METHOD_KECCAK, isAllowedSendMethod } from "./methods.mjs";
export { CORE_ROLES, REVOKED_ROLE, loadAllowlist } from "./allowlist.mjs";
export { createCoreContext, checkTransaction, assertSendable, buildTransaction } from "./check.mjs";
export {
  LISTA_CLAIM_FULL_SIGNATURE,
  LISTA_DEPLOYMENT,
  LISTA_ALLOWLIST_ENTRY,
  BUILTIN_DEPLOYMENTS,
  BUILTIN_ALLOWLIST,
  BUILTIN_CONTEXT,
  buildListaClaimTransaction,
} from "./builtin.mjs";
export * as lista from "./lista.mjs";

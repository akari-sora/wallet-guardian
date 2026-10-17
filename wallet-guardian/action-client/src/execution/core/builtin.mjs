/**
 * Built-in pinned Lista entry (S-1b decision 11). Lista goes through the same
 * general checks as every other call. This entry does not approve the deployment
 * (DEPLOYMENT_APPROVALS stays empty) and does not open send.
 */
import { checkTransaction, createCoreContext } from "./check.mjs";
import { CLAIM_SELECTOR, TRUSTED_LISTA, claimCalldata } from "./lista.mjs";

export const LISTA_CLAIM_FULL_SIGNATURE = "claimWithdraw(uint256 idx)";

export const LISTA_DEPLOYMENT = Object.freeze({
  chain_id: TRUSTED_LISTA.chainId,
  address: TRUSTED_LISTA.proxy,
  permission_type: "queue_vault",
  protocol_id: TRUSTED_LISTA.protocolId,
  source: "core_builtin_pin",
});

export const LISTA_ALLOWLIST_ENTRY = Object.freeze({
  permission_type: "queue_vault",
  signature: LISTA_CLAIM_FULL_SIGNATURE,
  selector: CLAIM_SELECTOR,
  roles: Object.freeze({}),
});

export const BUILTIN_DEPLOYMENTS = Object.freeze([LISTA_DEPLOYMENT]);
export const BUILTIN_ALLOWLIST = Object.freeze([LISTA_ALLOWLIST_ENTRY]);

export const BUILTIN_CONTEXT = createCoreContext({ deployments: BUILTIN_DEPLOYMENTS, allowlist: BUILTIN_ALLOWLIST });

/** Lista claim through the core: same calldata bytes as claimCalldata(idx), value 0, checked. */
export function buildListaClaimTransaction({ account, idx }) {
  const tx = Object.freeze({
    from: account,
    to: TRUSTED_LISTA.proxy,
    data: claimCalldata(idx),
    value: "0x0",
    chainId: `0x${TRUSTED_LISTA.chainId.toString(16)}`,
  });
  checkTransaction(tx, { account, signature: LISTA_CLAIM_FULL_SIGNATURE }, BUILTIN_CONTEXT);
  return tx;
}

/**
 * Calls the core never produces, by selector and by function name (§3.5 (1), L220–223, L285–294).
 * The core is default-deny anyway: only allowlisted functions pass. This list makes the
 * refusal reason explicit and also blocks an allowlist that tries to add one of these.
 */
import { selectorOfCanonical } from "./hex.mjs";

/** category -> canonical signatures. Selectors are computed, never typed by hand. */
export const DENIED_SIGNATURES = Object.freeze({
  multicall: Object.freeze([
    "multicall(bytes[])",
    "multicall(uint256,bytes[])",
    "multicall(bytes32,bytes[])",
  ]),
  execute: Object.freeze([
    "execute(bytes,bytes[],uint256)",
    "execute(bytes,bytes[])",
    "execute(address,uint256,bytes)",
    "executeBatch(address[],uint256[],bytes[])",
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)",
  ]),
  aggregate: Object.freeze([
    "aggregate((address,bytes)[])",
    "aggregate3((address,bool,bytes)[])",
    "aggregate3Value((address,bool,uint256,bytes)[])",
    "tryAggregate(bool,(address,bytes)[])",
    "blockAndAggregate((address,bytes)[])",
    "tryBlockAndAggregate(bool,(address,bytes)[])",
  ]),
  permit: Object.freeze([
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
    "permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)",
  ]),
  permit2: Object.freeze([
    "approve(address,address,uint160,uint48)",
    "permit(address,((address,uint160,uint48,uint48),address,uint256),bytes)",
    "permit(address,((address,uint160,uint48,uint48)[],address,uint256),bytes)",
    "transferFrom(address,address,uint160,address)",
    "transferFrom((address,address,uint160,address)[])",
    "permitTransferFrom(((address,uint256),uint256,uint256),(address,uint256),address,bytes)",
    "lockdown((address,address)[])",
    "invalidateNonces(address,address,uint48)",
  ]),
  allowance_increase: Object.freeze([
    "increaseAllowance(address,uint256)",
  ]),
  transfer: Object.freeze([
    "transfer(address,uint256)",
    "transferFrom(address,address,uint256)",
    "safeTransferFrom(address,address,uint256)",
    "safeTransferFrom(address,address,uint256,bytes)",
  ]),
  admin: Object.freeze([
    "transferOwnership(address)",
    "grantRole(bytes32,address)",
    "changeAdmin(address)",
    "setAdmin(address)",
    "setPendingAdmin(address)",
    "addOwnerWithThreshold(address,uint256)",
  ]),
  upgrade: Object.freeze([
    "upgradeTo(address)",
    "upgradeToAndCall(address,bytes)",
  ]),
});

/** Function names refused at allowlist load. Prefix match catches variants (aggregate3, executeBatch, permitTransferFrom). */
export const DENIED_NAME_PREFIXES = Object.freeze([
  "multicall",
  "execute",
  "exectransaction",
  "aggregate",
  "tryaggregate",
  "blockandaggregate",
  "tryblockandaggregate",
  "permit",
  "increaseallowance",
  "transfer",
  "safetransfer",
  "grantrole",
  "changeadmin",
  "setadmin",
  "setpendingadmin",
  "addowner",
  "upgrade",
  "lockdown",
  "invalidatenonces",
]);

const SELECTOR_CATEGORY = new Map();
for (const [category, signatures] of Object.entries(DENIED_SIGNATURES)) {
  for (const signature of signatures) {
    const selector = selectorOfCanonical(signature);
    if (!SELECTOR_CATEGORY.has(selector)) SELECTOR_CATEGORY.set(selector, category);
  }
}

/** Category name when the selector is on the deny list, else null. */
export function deniedSelectorCategory(selector) {
  return SELECTOR_CATEGORY.get(String(selector).toLowerCase()) ?? null;
}

/** Category-like label when the function name is refused, else null. */
export function deniedName(name) {
  const lower = String(name).toLowerCase();
  const hit = DENIED_NAME_PREFIXES.find((prefix) => lower.startsWith(prefix));
  return hit ?? null;
}

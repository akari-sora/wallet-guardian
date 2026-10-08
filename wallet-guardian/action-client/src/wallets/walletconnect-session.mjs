/**
 * WALLET-CONNECT-01 W-B2: what a WalletConnect proposal asks for and what a session may be used for.
 * Pure functions over SDK data structures; no SDK import and no network.
 *
 * A wallet may approve more than was proposed. That never widens what the page uses: the usable
 * method set is always the intersection of the session's methods and WC_METHODS (personal_sign).
 * A session without an eip155:56 account or without personal_sign cannot be used for login.
 */
import { WC_CHAIN, WC_CHAIN_ID, WC_METHODS, WC_PROPOSAL_SCOPE } from "./walletconnect-config.mjs";

const CAIP10_56 = /^eip155:56:(0x[0-9a-fA-F]{40})$/;
const TX_LIKE = /send|signTransaction|signTypedData|eth_sign$|switch|addEthereumChain|Permission|grant|upgrade|Authorization|watchAsset|Calls/i;

function namespaceEntries(namespaces) {
  if (!namespaces || typeof namespaces !== "object") return [];
  return Object.entries(namespaces).filter(([key]) => key === "eip155" || key.startsWith("eip155:"));
}

/**
 * Flatten required + optional namespaces of a proposal (WB-01).
 * @returns {{ requiredKeys: string[], optionalKeys: string[], chains: string[], methods: string[], events: string[], txLikeMethods: string[], otherNamespaces: string[] }}
 */
export function proposalScope(proposal) {
  const required = proposal?.requiredNamespaces ?? {};
  const optional = proposal?.optionalNamespaces ?? {};
  const chains = new Set();
  const methods = new Set();
  const events = new Set();
  const other = [];
  for (const group of [required, optional]) {
    for (const [key, ns] of Object.entries(group)) {
      if (key !== "eip155" && !key.startsWith("eip155:")) {
        other.push(key);
        continue;
      }
      for (const chain of ns?.chains ?? (key.includes(":") ? [key] : [])) chains.add(chain);
      for (const method of ns?.methods ?? []) methods.add(method);
      for (const event of ns?.events ?? []) events.add(event);
    }
  }
  const sorted = (set) => [...set].sort();
  return {
    requiredKeys: Object.keys(required).sort(),
    optionalKeys: Object.keys(optional).sort(),
    chains: sorted(chains),
    methods: sorted(methods),
    events: sorted(events),
    txLikeMethods: sorted(methods).filter((method) => TX_LIKE.test(method)),
    otherNamespaces: other.sort(),
  };
}

/** True when the proposal asks for exactly WC_PROPOSAL_SCOPE (union of required + optional). */
export function proposalMatchesContract(proposal) {
  const scope = proposalScope(proposal);
  const want = WC_PROPOSAL_SCOPE.eip155;
  const same = (a, b) => a.length === b.length && a.every((value, index) => value === b[index]);
  return scope.otherNamespaces.length === 0 &&
    same(scope.chains, [...want.chains].sort()) &&
    same(scope.methods, [...want.methods].sort()) &&
    same(scope.events, [...want.events].sort());
}

/**
 * @param {any} session SessionTypes.Struct from the SDK
 * @param {{ nowSec?: number }} [options]
 * @returns {{ ok: boolean, reason: string, address: string | null, chainId: number | null,
 *   accounts: string[], grantedMethods: string[], usableMethods: string[], extraMethods: string[], expiry: number | null, topic: string | null }}
 */
export function sessionScope(session, { nowSec = Math.floor(Date.now() / 1000) } = {}) {
  const empty = { address: null, chainId: null, accounts: [], grantedMethods: [], usableMethods: [], extraMethods: [], expiry: null, topic: null };
  if (!session || typeof session !== "object") return { ok: false, reason: "no_session", ...empty };
  const expiry = typeof session.expiry === "number" ? session.expiry : null;
  const topic = typeof session.topic === "string" ? session.topic : null;
  const accounts = [];
  const granted = new Set();
  for (const [, ns] of namespaceEntries(session.namespaces)) {
    for (const account of Array.isArray(ns?.accounts) ? ns.accounts : []) {
      const match = typeof account === "string" ? CAIP10_56.exec(account) : null;
      if (match && !accounts.includes(match[1].toLowerCase())) accounts.push(match[1].toLowerCase());
    }
    for (const method of Array.isArray(ns?.methods) ? ns.methods : []) if (typeof method === "string") granted.add(method);
  }
  const grantedMethods = [...granted].sort();
  const usableMethods = WC_METHODS.filter((method) => granted.has(method));
  const extraMethods = grantedMethods.filter((method) => !WC_METHODS.includes(method));
  const base = { accounts, grantedMethods, usableMethods, extraMethods, expiry, topic };
  if (expiry == null || expiry <= nowSec) return { ok: false, reason: "expired", address: null, chainId: null, ...base };
  if (accounts.length === 0) return { ok: false, reason: "no_bsc_account", address: null, chainId: null, ...base };
  if (!usableMethods.includes("personal_sign")) return { ok: false, reason: "no_personal_sign", address: null, chainId: null, ...base };
  return { ok: true, reason: "ok", address: accounts[0], chainId: WC_CHAIN_ID, ...base };
}

export { WC_CHAIN };

/**
 * WALLET-CONNECT-01 W-B: WalletConnect v2 (Reown) build configuration, proposal contract and
 * versioned wallet deep links. Pure data/functions; no SDK import and no network.
 *
 * Public build settings: VITE_REOWN_PROJECT_ID (a public Reown client identifier, origin-allowlisted
 * in the Reown dashboard) and VITE_WALLETCONNECT_ENABLED. If either is missing or malformed the
 * WalletConnect option is not offered at all (fail closed) and the injected (W-A) path is unchanged.
 */

export const SOURCE_WALLETCONNECT = "walletconnect";

/** Pinned SDK (package.json uses this exact version, no range). */
export const WC_SDK_PACKAGE = "@walletconnect/ethereum-provider";
export const WC_SDK_VERSION = "2.25.0";

/** AUTH_CHAIN_ID stays 56. WalletConnect only ever proposes BNB Smart Chain. */
export const WC_CHAIN_ID = 56;
export const WC_CHAIN = `eip155:${WC_CHAIN_ID}`;
/** The only method the page proposes and the only one it will ever send over WalletConnect. */
export const WC_METHODS = Object.freeze(["personal_sign"]);
export const WC_EVENTS = Object.freeze(["chainChanged", "accountsChanged"]);

/**
 * Network endpoints the pinned SDK actually uses for this configuration (read from
 * @walletconnect/core 2.25.0 source; see docs/wallet-connect-01.md):
 * - relay: RELAYER_DEFAULT_RELAY_URL = wss://relay.walletconnect.org (the only relay host in 2.25.0)
 * - verify: Verify.register() embeds a hidden iframe from VERIFY_SERVER = https://verify.walletconnect.org
 * Not used and therefore not allowed by CSP: pulse.walletconnect.org (telemetryEnabled=false),
 * rpc.walletconnect.org (rpcMap points at the trusted RPC; no read call goes through the SDK),
 * echo.walletconnect.com (wallet push only), api.web3modal.org (AppKit modal not shipped).
 */
export const WC_RELAY_URL = "wss://relay.walletconnect.org";
export const WC_VERIFY_ORIGIN = "https://verify.walletconnect.org";

/**
 * What the session proposal may contain. @walletconnect/sign-client 2.25.0 moves every
 * requiredNamespaces entry into optionalNamespaces before sending ("requiredNamespaces are
 * deprecated"), so the wire proposal is requiredNamespaces={} and optionalNamespaces=<this scope>.
 * The union of both must equal this scope exactly; anything more is a test failure (WB-01).
 * Because nothing is "required" on the wire any more, the page itself rejects a session that lacks
 * an eip155:56 account or personal_sign (WB-02).
 */
export const WC_PROPOSAL_SCOPE = Object.freeze({
  eip155: Object.freeze({ chains: Object.freeze([WC_CHAIN]), methods: WC_METHODS, events: WC_EVENTS }),
});

const PROJECT_ID = /^[0-9a-f]{32}$/;

/**
 * @param {{ enabled?: unknown, projectId?: unknown }} env raw build values
 * @returns {{ enabled: boolean, projectId: string | null, reason: string }}
 */
export function walletConnectConfig(env = {}) {
  const enabled = env.enabled === "true";
  const projectId = typeof env.projectId === "string" ? env.projectId.trim() : "";
  if (!enabled) return Object.freeze({ enabled: false, projectId: null, reason: "disabled" });
  if (!projectId) return Object.freeze({ enabled: false, projectId: null, reason: "missing_project_id" });
  if (!PROJECT_ID.test(projectId)) return Object.freeze({ enabled: false, projectId: null, reason: "invalid_project_id" });
  return Object.freeze({ enabled: true, projectId, reason: "enabled" });
}

/**
 * Versioned wallet jump targets. Source: WalletConnect Explorer API v3 listing
 * 8a0ee50d1f22f6651afcae7eb4253e52a3310b90af5daef78a8c4929a9bb99d4 "Binance Wallet"
 * (mobile.native "bnc://app.binance.com/cedefi/", mobile.universal "https://app.binance.com/cedefi"),
 * fetched 2026-10-08. URL shape `<link>/wc?uri=<encoded pairing URI>` follows
 * @reown/appkit-controllers 1.8.19 CoreHelperUtil.formatNativeUrl/formatUniversalUrl (its own test
 * uses https://app.binance.com/cedefi). Never taken from a task, a query parameter or peer metadata.
 */
export const WC_DEEP_LINKS = Object.freeze({
  version: "2026-10-08.1",
  wallets: Object.freeze([
    Object.freeze({
      id: "binance",
      label: "在幣安錢包開啟（Binance Wallet App）",
      explorerId: "8a0ee50d1f22f6651afcae7eb4253e52a3310b90af5daef78a8c4929a9bb99d4",
      universal: "https://app.binance.com/cedefi",
      native: "bnc://app.binance.com/cedefi/",
    }),
  ]),
});

// wc:<64-hex topic>@2?<params> with relay-protocol and symKey (WalletConnect v2 pairing URI).
const PAIRING_URI = /^wc:[0-9a-f]{64}@2\?[A-Za-z0-9\-._~%&=]+$/;

export function isPairingUri(uri) {
  if (typeof uri !== "string" || uri.length > 2048 || !PAIRING_URI.test(uri)) return false;
  const params = new URLSearchParams(uri.slice(uri.indexOf("?") + 1));
  return params.get("relay-protocol") === "irn" && /^[0-9a-f]{64}$/.test(params.get("symKey") ?? "");
}

function withSlash(base) {
  return base.endsWith("/") ? base : `${base}/`;
}

/**
 * Jump links for a pairing URI, built only from WC_DEEP_LINKS. `newTab` is true only for https
 * universal links, so the original tab keeps the task page when the app is not installed.
 * @returns {{ id: string, label: string, href: string, kind: "universal" | "native" | "generic", newTab: boolean }[]}
 */
export function deepLinksFor(uri) {
  if (!isPairingUri(uri)) return [];
  const encoded = encodeURIComponent(uri);
  const out = [];
  for (const wallet of WC_DEEP_LINKS.wallets) {
    out.push({ id: `${wallet.id}-universal`, label: wallet.label, href: `${withSlash(wallet.universal)}wc?uri=${encoded}`, kind: "universal", newTab: true });
    out.push({ id: `${wallet.id}-native`, label: `${wallet.label}：備用 bnc:// 連結`, href: `${withSlash(wallet.native)}wc?uri=${encoded}`, kind: "native", newTab: false });
  }
  out.push({ id: "generic", label: "用其他 WalletConnect 錢包開啟（wc: 連結）", href: uri, kind: "generic", newTab: false });
  return out;
}

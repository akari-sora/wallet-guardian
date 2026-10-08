/**
 * WALLET-CONNECT-01 W-B1: the only module that imports the WalletConnect SDK.
 * Loaded with a dynamic import, and only after the user picked WalletConnect and pressed login.
 * A build with WalletConnect disabled does not contain this module at all.
 *
 * The SDK provider is narrowed before anything can reach it:
 * - request() only forwards personal_sign (the fixed SIWE login, already checked by request-policy);
 * - the SDK's internal wallet_switchEthereumChain (sent when a wallet announces another chain) is a no-op;
 * - no QR modal (AppKit is not shipped), telemetry off, the RPC map points at the trusted RPC.
 */
import { EthereumProvider } from "@walletconnect/ethereum-provider";
import qrcode from "qrcode-generator";
import { WC_CHAIN_ID, WC_EVENTS, WC_METHODS, WC_RELAY_URL } from "./walletconnect-config.mjs";

const FORWARDED = new Set(WC_METHODS);

export class ReadOnlyLoginProvider extends EthereumProvider {
  /** The SDK would ask the wallet to switch chains here. Chain switches are not part of login. */
  switchEthereumChain() {}

  async request(args, expiry) {
    const method = args && typeof args === "object" ? args.method : undefined;
    if (!FORWARDED.has(method)) {
      const error = new Error(`WalletConnect provider refuses ${String(method)}`);
      error.code = 4200;
      throw error;
    }
    return super.request(args, expiry);
  }
}

/**
 * @param {{ projectId: string, origin: string, rpcUrl: string, relayUrl?: string, storage?: any, customStoragePrefix?: string }} options
 */
export function providerOptions({ projectId, origin, rpcUrl, relayUrl, storage, customStoragePrefix = "wallet-guardian" }) {
  return {
    projectId,
    chains: [WC_CHAIN_ID],
    optionalChains: [],
    methods: [...WC_METHODS],
    optionalMethods: [],
    events: [...WC_EVENTS],
    optionalEvents: [],
    rpcMap: { [WC_CHAIN_ID]: rpcUrl },
    showQrModal: false,
    telemetryEnabled: false,
    disableProviderPing: true,
    logger: "silent",
    relayUrl: relayUrl ?? WC_RELAY_URL,
    customStoragePrefix,
    ...(storage ? { storage } : {}),
    metadata: {
      name: "Wallet Guardian",
      description: "唯讀登入：只簽固定讀取訊息，不送出交易。",
      url: origin,
      icons: [`${origin}/wallet-guardian-icon.png`],
    },
  };
}

export async function initProvider(options) {
  const provider = new ReadOnlyLoginProvider();
  await provider.initialize(providerOptions(options));
  return provider;
}

/** Draw a pairing URI as a QR code on a canvas. No innerHTML, no data URL, no network. */
export function drawQr(canvas, uri) {
  if (!canvas || typeof canvas.getContext !== "function") return false;
  const qr = qrcode(0, "M");
  qr.addData(uri);
  qr.make();
  const count = qr.getModuleCount();
  const margin = 4;
  const scale = Math.max(2, Math.floor(256 / (count + margin * 2)));
  const size = (count + margin * 2) * scale;
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return false;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = "#000000";
  for (let row = 0; row < count; row += 1) {
    for (let col = 0; col < count; col += 1) {
      if (qr.isDark(row, col)) ctx.fillRect((col + margin) * scale, (row + margin) * scale, scale, scale);
    }
  }
  return true;
}

export function clearQr(canvas) {
  if (!canvas || typeof canvas.getContext !== "function") return;
  const ctx = canvas.getContext("2d");
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
  canvas.width = 0;
  canvas.height = 0;
}

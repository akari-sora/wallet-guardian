/**
 * WALLET-CONNECT-01 W-A2/W-A3/W-A4: the injected (EIP-1193) connection adapter.
 *
 * Every wallet request goes through `guardedRequest`: policy first, then the selected provider
 * object only. A connection switch abandons pending requests so a late answer from the old
 * provider cannot be used. personal_sign payload encoding is an adapter detail; the signed
 * bytes are always the UTF-8 bytes of the fixed SIWE message.
 */
import { assertWalletRequest } from "./request-policy.mjs";
import { SOURCE_BINANCE, SOURCE_EIP6963, SOURCE_INJECTED } from "./registry.mjs";

export const ENCODING_UTF8 = "utf8";
export const ENCODING_HEX = "hex";

/**
 * Per-source personal_sign transport. All "utf8" (the plain message string, as the D1-LIVE-01
 * client sent it). "hex" exists for a provider that is shown on a real device to need it; it is
 * not enabled for any source without that evidence. rdns/name never select an encoding.
 */
export const SIGN_ENCODING = Object.freeze({
  [SOURCE_EIP6963]: ENCODING_UTF8,
  [SOURCE_BINANCE]: ENCODING_UTF8,
  [SOURCE_INJECTED]: ENCODING_UTF8,
});

export function signEncodingFor(candidate) {
  return SIGN_ENCODING[candidate?.source] ?? ENCODING_UTF8;
}

/** Transport form of the fixed message. Same bytes either way: no prefix, no newline change. */
export function encodePersonalSignMessage(message, encoding = ENCODING_UTF8) {
  if (typeof message !== "string") throw new TypeError("login message");
  if (encoding === ENCODING_UTF8) return message;
  if (encoding === ENCODING_HEX) {
    let out = "0x";
    for (const byte of new TextEncoder().encode(message)) out += byte.toString(16).padStart(2, "0");
    return out;
  }
  throw new TypeError("unknown personal_sign encoding");
}

export class ConnectionAbandonedError extends Error {
  constructor() {
    super("wallet connection switched; the pending request was abandoned");
    this.name = "ConnectionAbandonedError";
  }
}

export function createAbandonSignal() {
  let reject = () => {};
  const promise = new Promise((_resolve, no) => {
    reject = no;
  });
  promise.catch(() => {});
  let abandoned = false;
  return {
    promise,
    get abandoned() {
      return abandoned;
    },
    abandon() {
      if (abandoned) return;
      abandoned = true;
      reject(new ConnectionAbandonedError());
    },
  };
}

/**
 * @param {{ provider: { request: (args: { method: string, params?: unknown[] }) => Promise<unknown> } }} candidate
 * @param {string} method
 * @param {unknown[] | undefined} params
 * @param {object} context policy context
 * @param {{ promise: Promise<unknown>, abandoned: boolean } | null} [abandon]
 */
export async function guardedRequest(candidate, method, params, context = {}, abandon = null) {
  assertWalletRequest(method, params, context);
  if (abandon?.abandoned) throw new ConnectionAbandonedError();
  const provider = candidate?.provider;
  if (!provider || typeof provider.request !== "function") throw new Error("no selected wallet");
  const args = params === undefined ? { method } : { method, params };
  let pending;
  try {
    pending = Promise.resolve(provider.request(args));
  } catch (error) {
    pending = Promise.reject(error);
  }
  if (!abandon) return pending;
  pending.catch(() => {});
  return Promise.race([pending, abandon.promise]);
}

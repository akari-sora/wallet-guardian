/**
 * WALLET-CONNECT-01 W-B: WalletConnect v2 as one more W-A connection.
 *
 * `createWalletConnectConnection()` returns a candidate with the same shape as an injected wallet
 * ({ id, source, provider, name, rdns, uuid, icon }), so main.ts uses the same selection, switch,
 * `guardedRequest()` and request-policy path for both. The provider here is a small EIP-1193 facade:
 *
 * - eth_accounts / eth_chainId are answered from the validated session, locally. They never load the
 *   SDK and never touch the relay.
 * - eth_requestAccounts (only after the user picked WalletConnect and pressed login) loads the SDK,
 *   restores a stored session or creates a pairing (QR / deep links), and validates the session scope.
 * - personal_sign is forwarded only when the session is valid, on eip155:56, for the session address.
 * - everything else (wallet_getCapabilities included) is refused; the UI shows capability "unknown".
 *
 * Pairing URIs are handed to the UI callback only while pairing; they are never stored, logged or
 * sent anywhere else by this module.
 */
import { SOURCE_WALLETCONNECT, WC_CHAIN_ID, deepLinksFor, isPairingUri } from "./walletconnect-config.mjs";
import { sessionScope } from "./walletconnect-session.mjs";

export const WC_STATES = Object.freeze(["idle", "loading", "pairing", "connected", "signing", "failed"]);

export const WC_TIMEOUTS = Object.freeze({
  initMs: 15000, // SDK load + init
  uriMs: 20000, // no pairing URI from the relay within this time = relay failure
  pairingMs: 5 * 60 * 1000, // SDK proposal expiry is 5 minutes
});

export const FLAG_KEY = "wallet-guardian.walletconnect.v1.used";

export class WalletConnectError extends Error {
  /** @param {string} reason @param {string} message @param {number} [code] */
  constructor(reason, message, code = 4001) {
    super(message);
    this.name = "WalletConnectError";
    this.reason = reason;
    this.code = code;
  }
}

const REASON_TEXT = {
  cancelled: "已取消 WalletConnect 連線。",
  expired: "WalletConnect 連線邀請已過期（5 分鐘）。可重試，不會送出交易。",
  relay_unavailable: "WalletConnect 中繼（relay）連線失敗。瀏覽器錢包仍可使用；不會改用其他錢包。",
  sdk_failed: "WalletConnect 元件載入或初始化失敗。瀏覽器錢包仍可使用；不會改用其他錢包。",
  rejected: "錢包拒絕或沒有完成連線。",
  no_bsc_account: "錢包沒有核准 BNB Smart Chain（eip155:56）帳戶，不能用來登入。已中斷這個連線。",
  no_personal_sign: "錢包沒有核准 personal_sign，不能簽固定登入訊息。已中斷這個連線。",
  no_session: "沒有可用的 WalletConnect 連線。",
  disconnected: "WalletConnect 連線已中斷。",
};

export function reasonText(reason) {
  return REASON_TEXT[reason] ?? "WalletConnect 連線未完成。";
}

function hexChain(chainId) {
  return `0x${chainId.toString(16)}`;
}

function safeStorage(storage) {
  return {
    get() { try { return storage?.getItem(FLAG_KEY) === "1"; } catch { return false; } },
    set(on) { try { if (on) storage?.setItem(FLAG_KEY, "1"); else storage?.removeItem(FLAG_KEY); } catch { /* private mode */ } },
  };
}

/**
 * @param {{
 *   projectId: string, origin: string, rpcUrl: string,
 *   loadSdk: () => Promise<{ initProvider: (opts: any) => Promise<any>, drawQr?: Function, clearQr?: Function }>,
 *   sdkOptions?: object, onStatus?: (status: any) => void, flagStorage?: Storage | null,
 *   timers?: { setTimeout: typeof setTimeout, clearTimeout: typeof clearTimeout }, timeouts?: Partial<typeof WC_TIMEOUTS>,
 *   nowSec?: () => number,
 * }} options
 */
export function createWalletConnectConnection(options) {
  const {
    projectId, origin, rpcUrl, loadSdk, sdkOptions = {}, onStatus, flagStorage = null,
    timers = { setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms), clearTimeout: (id) => globalThis.clearTimeout(id) }, nowSec = () => Math.floor(Date.now() / 1000),
  } = options;
  const timeouts = { ...WC_TIMEOUTS, ...(options.timeouts ?? {}) };
  const flag = safeStorage(flagStorage);
  const listeners = new Map();
  let sdk = null;
  let sdkModule = null;
  let sdkPromise = null;
  let state = "idle";
  let scope = null; // validated sessionScope while connected
  let pending = null; // in-flight connect promise (repeated clicks share it)
  let attemptSeq = 0;
  let cancelCurrent = null;
  let lastReason = null;
  let chainId = null;

  function emit(event, value) {
    for (const handler of [...(listeners.get(event) ?? [])]) {
      try { handler(value); } catch { /* a UI listener cannot break the connection */ }
    }
  }

  function status(next, extra = {}) {
    state = next;
    if (typeof onStatus !== "function") return;
    try {
      onStatus({
        state: next,
        address: scope?.address ?? null,
        reason: extra.reason ?? null,
        message: extra.reason ? reasonText(extra.reason) : null,
        // Only while pairing. The UI must drop these as soon as the state changes.
        uri: next === "pairing" ? extra.uri ?? null : null,
        links: next === "pairing" && extra.uri ? deepLinksFor(extra.uri) : [],
        drawQr: next === "pairing" ? sdkModule?.drawQr ?? null : null,
        clearQr: sdkModule?.clearQr ?? null,
        extraMethods: scope?.extraMethods ?? [],
      });
    } catch { /* UI errors do not change connection state */ }
  }

  function currentScope() {
    if (!scope) return null;
    const fresh = sessionScope(sdk?.session, { nowSec: nowSec() });
    if (!fresh.ok || fresh.topic !== scope.topic) {
      dropSession(fresh.reason === "expired" ? "expired" : "disconnected");
      return null;
    }
    scope = fresh;
    return scope;
  }

  function dropSession(reason) {
    const had = scope != null;
    scope = null;
    chainId = null;
    flag.set(false);
    if (had) {
      lastReason = reason;
      status("idle", { reason });
      emit("accountsChanged", []);
      emit("disconnect", { code: 4900, reason });
    }
  }

  function adopt(session) {
    const next = sessionScope(session, { nowSec: nowSec() });
    if (!next.ok) return next;
    const before = scope?.address ?? null;
    scope = next;
    chainId = WC_CHAIN_ID;
    flag.set(true);
    status("connected");
    if (before !== next.address) emit("accountsChanged", [next.address]);
    return next;
  }

  /** Session changed on the wallet side (account switch, update, chain event). Never signs. */
  function onSessionChanged() {
    if (!scope || !sdk) return;
    const next = sessionScope(sdk.session, { nowSec: nowSec() });
    if (!next.ok) {
      dropSession(next.reason === "expired" ? "expired" : next.reason);
      return;
    }
    const before = scope.address;
    scope = next;
    const sdkChain = Number(sdk.chainId);
    const nextChain = Number.isSafeInteger(sdkChain) && sdkChain > 0 ? sdkChain : WC_CHAIN_ID;
    if (nextChain !== chainId) {
      chainId = nextChain;
      emit("chainChanged", hexChain(chainId));
    }
    if (before !== next.address) emit("accountsChanged", [next.address]);
    status("connected");
  }

  function wire(instance) {
    instance.on("accountsChanged", onSessionChanged);
    instance.on("chainChanged", onSessionChanged);
    instance.on("session_update", onSessionChanged);
    instance.on("session_event", onSessionChanged);
    instance.on("session_delete", () => dropSession("disconnected"));
    instance.on("disconnect", () => dropSession("disconnected"));
    try {
      instance.signer?.client?.on?.("session_expire", () => dropSession("expired"));
    } catch { /* optional */ }
  }

  /** A timer that rejects with `reason` unless cleared. Cleared timers never reject. */
  function deadline(ms, reason) {
    let timer = null;
    let reject = () => {};
    const promise = new Promise((_resolve, no) => { reject = no; });
    promise.catch(() => {});
    timer = timers.setTimeout(() => { timer = null; reject(new WalletConnectError(reason, reasonText(reason))); }, ms);
    if (timer && typeof timer.unref === "function") timer.unref();
    return {
      promise,
      clear() {
        if (timer != null) timers.clearTimeout(timer);
        timer = null;
      },
    };
  }

  async function ensureSdk() {
    if (sdk) return sdk;
    if (!sdkPromise) {
      sdkPromise = (async () => {
        const mod = await loadSdk();
        const instance = await mod.initProvider({ projectId, origin, rpcUrl, ...sdkOptions });
        sdkModule = mod;
        sdk = instance;
        wire(instance);
        return instance;
      })();
      sdkPromise.catch(() => { sdkPromise = null; });
    }
    return sdkPromise;
  }

  async function connect() {
    const live = currentScope();
    if (live) return [live.address];
    if (pending) return pending;
    const attempt = ++attemptSeq;
    let cancelled = false;
    let rejectCancel = () => {};
    const cancelSignal = new Promise((_resolve, reject) => { rejectCancel = reject; });
    cancelSignal.catch(() => {});
    cancelCurrent = (reason = "cancelled") => {
      cancelled = true;
      rejectCancel(new WalletConnectError(reason, reasonText(reason)));
    };
    const stillMine = () => attempt === attemptSeq && !cancelled;
    pending = (async () => {
      lastReason = null;
      status("loading");
      let instance;
      try {
        const initWatch = deadline(timeouts.initMs, "relay_unavailable");
        try {
          instance = await Promise.race([ensureSdk(), initWatch.promise, cancelSignal]);
        } finally {
          initWatch.clear();
        }
      } catch (error) {
        throw error instanceof WalletConnectError ? error : new WalletConnectError("sdk_failed", reasonText("sdk_failed"));
      }
      if (!stillMine()) throw new WalletConnectError("cancelled", reasonText("cancelled"));
      // A stored session (page reload, return from the wallet app) is reused, never re-signed here.
      if (instance.session) {
        const restored = sessionScope(instance.session, { nowSec: nowSec() });
        if (restored.ok) return [adopt(instance.session).address];
        await instance.disconnect().catch(() => {});
      }
      const uriWatch = deadline(timeouts.uriMs, "relay_unavailable");
      const pairingWatch = deadline(timeouts.pairingMs, "expired");
      const onUri = (uri) => {
        if (!stillMine() || !isPairingUri(uri)) return;
        uriWatch.clear();
        status("pairing", { uri });
      };
      instance.on("display_uri", onUri);
      const connecting = Promise.resolve().then(() => instance.connect());
      // A session that lands after cancel/expiry/switch is disconnected, never adopted.
      connecting.then(() => {
        if (!stillMine() && instance.session && instance.session.topic !== scope?.topic) {
          instance.disconnect().catch(() => {});
        }
      }, () => {});
      try {
        await Promise.race([connecting, cancelSignal, uriWatch.promise, pairingWatch.promise]);
      } catch (error) {
        if (error instanceof WalletConnectError) throw error;
        throw new WalletConnectError("rejected", reasonText("rejected"));
      } finally {
        uriWatch.clear();
        pairingWatch.clear();
        instance.removeListener("display_uri", onUri);
      }
      if (!stillMine()) throw new WalletConnectError("cancelled", reasonText("cancelled"));
      const checked = sessionScope(instance.session, { nowSec: nowSec() });
      if (!checked.ok) {
        await instance.disconnect().catch(() => {});
        throw new WalletConnectError(checked.reason, reasonText(checked.reason));
      }
      return [adopt(instance.session).address];
    })();
    const mine = pending;
    try {
      return await mine;
    } catch (error) {
      if (attempt === attemptSeq) {
        const reason = error instanceof WalletConnectError ? error.reason : "rejected";
        lastReason = reason;
        if (reason !== "cancelled" || state !== "idle") status("failed", { reason });
        if (sdk && (reason === "cancelled" || reason === "expired" || reason === "relay_unavailable")) {
          // Stop the old pairing so a late approval of it cannot become a session.
          sdk.signer?.cleanupPendingPairings?.({ deletePairings: true })?.catch?.(() => {});
        }
      }
      throw error;
    } finally {
      if (pending === mine) pending = null;
      if (attempt === attemptSeq) cancelCurrent = null;
    }
  }

  async function personalSign(params) {
    const live = currentScope();
    if (!live || !sdk) throw new WalletConnectError("no_session", reasonText("no_session"), 4900);
    if (chainId !== WC_CHAIN_ID) throw new WalletConnectError("wrong_chain", "WalletConnect 連線不在 BNB Smart Chain。", 4901);
    const address = Array.isArray(params) ? params[1] : null;
    if (typeof address !== "string" || address.toLowerCase() !== live.address) {
      throw new WalletConnectError("address", "登入地址和 WalletConnect 帳戶不同。", 4100);
    }
    status("signing");
    try {
      return await sdk.request({ method: "personal_sign", params });
    } finally {
      if (scope) status("connected");
    }
  }

  const provider = {
    isWalletGuardianWalletConnect: true,
    async request(args) {
      const method = args && typeof args === "object" ? args.method : undefined;
      const params = args?.params;
      switch (method) {
        case "eth_accounts": {
          const live = currentScope();
          return live ? [live.address] : [];
        }
        case "eth_chainId": {
          const live = currentScope();
          if (!live || chainId == null) throw new WalletConnectError("no_session", reasonText("no_session"), 4900);
          return hexChain(chainId);
        }
        case "eth_requestAccounts":
          return connect();
        case "personal_sign":
          return personalSign(params);
        default: {
          throw new WalletConnectError("unsupported", `WalletConnect 連線不提供 ${String(method)}。`, 4200);
        }
      }
    },
    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(handler);
    },
    removeListener(event, handler) {
      listeners.get(event)?.delete(handler);
    },
  };

  const candidate = Object.freeze({
    id: "walletconnect",
    source: SOURCE_WALLETCONNECT,
    provider,
    name: "WalletConnect（QR 掃碼／手機錢包 App）",
    rdns: null,
    uuid: null,
    icon: null,
  });

  return {
    candidate,
    provider,
    /** User pressed cancel, or the page switched away during pairing. */
    cancel(reason = "cancelled") {
      if (cancelCurrent) cancelCurrent(reason);
      else if (state === "pairing" || state === "loading") status("idle");
    },
    /** End the WalletConnect session (user action or switch). Local state clears even if the relay is down. */
    async disconnect() {
      if (cancelCurrent) cancelCurrent("cancelled");
      const instance = sdk;
      const had = scope != null;
      dropSession("disconnected");
      if (!had) status("idle");
      if (instance?.session) await instance.disconnect().catch(() => {});
    },
    /** Visibility/focus return (e.g. back from the wallet app). Re-checks the session only. */
    resume() {
      if (scope) currentScope();
    },
    state: () => state,
    address: () => scope?.address ?? null,
    lastReason: () => lastReason,
    sdkLoaded: () => sdk != null || sdkPromise != null,
    /** Read-only handle for tests/cleanup. */
    sdkInstance: () => sdk,
    hadSession: () => flag.get(),
  };
}

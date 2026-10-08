/**
 * WALLET-CONNECT-01 W-A1: injected wallet discovery.
 *
 * Candidate order: EIP-6963 announcements (in arrival order), then the officially documented
 * `window.binancew3w.ethereum`, then `window.ethereum` as an explicit fallback. One provider
 * object is listed once, whichever path it arrived on.
 *
 * name / rdns / uuid / icon are wallet self-description. They are for display and local
 * preference only. They are NOT authenticated identity and never feed login or D1 ACL.
 * Login identity is the SIWE-verified address plus the D1 session.
 */

export const SOURCE_EIP6963 = "eip6963";
export const SOURCE_BINANCE = "binancew3w";
export const SOURCE_INJECTED = "window.ethereum";

export const ANNOUNCE_EVENT = "eip6963:announceProvider";
export const REQUEST_EVENT = "eip6963:requestProvider";

export const MAX_NAME_LENGTH = 64;
export const MAX_ICON_LENGTH = 65536;
// Image data URIs only (no http(s), javascript:, text/html). Parameters such as ;base64 or
// ;charset=utf-8 are allowed. Control characters are rejected. The value is only ever an <img> src.
// eslint-disable-next-line no-control-regex
const ICON = /^data:image\/(?:png|jpeg|gif|webp|svg\+xml)(?:;[a-z0-9=._-]+)*,[^\u0000-\u001f\u007f]*$/i;
const RDNS = /^[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const FALLBACK_NAME = {
  [SOURCE_EIP6963]: "未命名錢包",
  [SOURCE_BINANCE]: "Binance Web3 Wallet（binancew3w 注入介面）",
  [SOURCE_INJECTED]: "瀏覽器注入錢包（未公告名稱）",
};

/** Display text only. Control characters removed, length capped. Rendered with textContent. */
export function sanitizeName(value, fallback = FALLBACK_NAME[SOURCE_EIP6963]) {
  if (typeof value !== "string") return fallback;
  // eslint-disable-next-line no-control-regex
  const cleaned = value.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, "").trim();
  if (!cleaned) return fallback;
  return [...cleaned].slice(0, MAX_NAME_LENGTH).join("");
}

/** Icons must be bounded image data URIs. SVG is only ever shown through <img>, never inline. */
export function sanitizeIcon(value) {
  if (typeof value !== "string") return null;
  if (value.length === 0 || value.length > MAX_ICON_LENGTH) return null;
  if (!ICON.test(value)) return null;
  return value;
}

export function sanitizeRdns(value) {
  if (typeof value !== "string" || value.length > 253 || !RDNS.test(value)) return null;
  return value.toLowerCase();
}

export function sanitizeUuid(value) {
  if (typeof value !== "string" || !UUID.test(value)) return null;
  return value.toLowerCase();
}

export function isProvider(value) {
  if (!value || (typeof value !== "object" && typeof value !== "function")) return false;
  try {
    return typeof value.request === "function";
  } catch {
    return false;
  }
}

function readInjected(win, path) {
  try {
    if (path === SOURCE_BINANCE) {
      const holder = win?.binancew3w;
      return holder && typeof holder === "object" ? holder.ethereum ?? null : null;
    }
    return win?.ethereum ?? null;
  } catch {
    return null;
  }
}

/**
 * @param {{ win: any, onChange?: () => void, EventCtor?: any }} options
 */
export function createWalletRegistry({ win, onChange, EventCtor } = {}) {
  /** @type {any[]} */
  const announced = [];
  const announcedProviders = new Set();
  const uuidOwner = new Map();
  /** @type {{ [key: string]: any }} */
  const injected = { [SOURCE_BINANCE]: null, [SOURCE_INJECTED]: null };
  let seq = 0;
  let conflicts = 0;
  let started = false;

  function candidate(source, provider, info) {
    seq += 1;
    return Object.freeze({
      id: `wallet-${seq}`,
      source,
      provider,
      name: sanitizeName(info?.name, FALLBACK_NAME[source]),
      rdns: sanitizeRdns(info?.rdns),
      uuid: sanitizeUuid(info?.uuid),
      icon: sanitizeIcon(info?.icon),
    });
  }

  function changed() {
    if (typeof onChange === "function") {
      try {
        onChange();
      } catch {
        // A UI refresh failure must not break discovery.
      }
    }
  }

  function onAnnounce(event) {
    let detail;
    let info;
    let provider;
    try {
      detail = event?.detail;
      if (!detail || typeof detail !== "object") return;
      info = detail.info;
      provider = detail.provider;
      if (!info || typeof info !== "object") return;
      info = { uuid: info.uuid, name: info.name, icon: info.icon, rdns: info.rdns };
    } catch {
      return;
    }
    if (!isProvider(provider)) return;
    // Repeated announcements of the same provider keep the first metadata.
    if (announcedProviders.has(provider)) return;
    const uuid = sanitizeUuid(info.uuid);
    if (!uuid) return;
    // Same uuid on a different object cannot replace the provider already listed (or selected).
    if (uuidOwner.has(uuid) && uuidOwner.get(uuid) !== provider) {
      conflicts += 1;
      return;
    }
    uuidOwner.set(uuid, provider);
    announcedProviders.add(provider);
    announced.push(candidate(SOURCE_EIP6963, provider, info));
    changed();
  }

  /** Re-read the documented injected interfaces. Returns true when the visible list changed. */
  function refresh() {
    let dirty = false;
    for (const source of [SOURCE_BINANCE, SOURCE_INJECTED]) {
      const provider = readInjected(win, source);
      const current = injected[source];
      if (!isProvider(provider)) {
        if (current) {
          injected[source] = null;
          dirty = true;
        }
        continue;
      }
      if (current && current.provider === provider) continue;
      injected[source] = candidate(source, provider, null);
      dirty = true;
    }
    return dirty;
  }

  function list() {
    const out = [...announced];
    const seen = new Set(announced.map((item) => item.provider));
    for (const source of [SOURCE_BINANCE, SOURCE_INJECTED]) {
      const item = injected[source];
      if (item && !seen.has(item.provider)) {
        out.push(item);
        seen.add(item.provider);
      }
    }
    return out;
  }

  function requestProviders() {
    const Ctor = EventCtor ?? win?.Event ?? (typeof Event === "function" ? Event : undefined);
    if (typeof win?.dispatchEvent !== "function" || typeof Ctor !== "function") return false;
    try {
      win.dispatchEvent(new Ctor(REQUEST_EVENT));
      return true;
    } catch {
      return false;
    }
  }

  /** Listener first, then the request. The listener stays for late announcements. */
  function start() {
    if (started) return;
    started = true;
    if (typeof win?.addEventListener === "function") win.addEventListener(ANNOUNCE_EVENT, onAnnounce);
    requestProviders();
    refresh();
  }

  function findByProvider(provider) {
    return list().find((item) => item.provider === provider) ?? null;
  }

  return { start, refresh, list, requestProviders, findByProvider, stats: () => ({ conflicts, announced: announced.length }) };
}

/** Human label for where a candidate came from. Not a brand verification. */
export function sourceLabel(item) {
  if (!item) return "";
  if (item.source === SOURCE_EIP6963) {
    return item.rdns ? `EIP-6963 公告｜rdns（錢包自述，未認證）：${item.rdns}` : "EIP-6963 公告（錢包自述，未認證）";
  }
  if (item.source === SOURCE_BINANCE) return "window.binancew3w.ethereum（幣安官方記載的注入介面，未認證）";
  return "window.ethereum 後備（可能被其他擴充功能覆蓋，未認證）";
}

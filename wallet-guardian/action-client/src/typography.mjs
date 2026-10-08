/**
 * Presentation-only text tokenisation for the action client.
 * Safe to import from Node tests; does not touch document at import time.
 */

/** @typedef {{ kind: "text" | "nw" | "mono"; value: string }} TextSegment */

const GLOSS_NW = /（[A-Za-z ._-]+）/y;
const PATH_REF = /\/t\/<ref>/y;
const LOCK_PHRASE = /lock screen ≠ lock chain\.?/y;
const PERSONAL_SIGN = /personal_sign/y;
const WINDOW_REF = /window\.[a-zA-Z][a-zA-Z0-9_.]*/y;
const EIP = /EIP-\d+/y;
const RDNS =
  /[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+/y;
const TASK_REF = /ref_[0-9a-f]{32}/iy;
const HEX = /0x[0-9a-fA-F]+/y;

const RULES = [
  { kind: "nw", re: GLOSS_NW },
  { kind: "nw", re: PATH_REF },
  { kind: "nw", re: LOCK_PHRASE },
  { kind: "nw", re: PERSONAL_SIGN },
  { kind: "mono", re: WINDOW_REF },
  { kind: "mono", re: EIP },
  { kind: "mono", re: RDNS },
  { kind: "mono", re: TASK_REF },
  { kind: "mono", re: HEX },
];

/**
 * Split a wallet display name into a primary line and trailing full-width parenthetical detail.
 * @param {string} name
 * @returns {{ primary: string; detail: string | null }}
 */
export function splitTrailingParenthetical(name) {
  const text = String(name ?? "");
  const match = text.match(/^(.+?)(（.+）)$/);
  if (!match) return { primary: text, detail: null };
  return { primary: match[1], detail: match[2] };
}

/**
 * @param {string} text
 * @param {number} index
 * @returns {{ kind: "nw" | "mono"; value: string } | null}
 */
function matchAt(text, index) {
  for (const rule of RULES) {
    rule.re.lastIndex = index;
    const hit = rule.re.exec(text);
    if (hit && hit.index === index) return { kind: rule.kind, value: hit[0] };
  }
  return null;
}

/**
 * @param {string} text
 * @param {number} from
 * @returns {number}
 */
function nextTokenIndex(text, from) {
  for (let i = from + 1; i < text.length; i += 1) {
    if (matchAt(text, i)) return i;
  }
  return text.length;
}

/**
 * @param {string} text
 * @returns {TextSegment[]}
 */
export function segmentTypography(text) {
  const input = String(text);
  const out = [];
  let index = 0;
  while (index < input.length) {
    const token = matchAt(input, index);
    if (token) {
      out.push(token);
      index += token.value.length;
      continue;
    }
    const end = nextTokenIndex(input, index);
    out.push({ kind: "text", value: input.slice(index, end) });
    index = end;
  }
  return out.length > 0 ? out : [{ kind: "text", value: input }];
}

/**
 * @param {HTMLElement} el
 */
export function enhanceText(el) {
  if (!el || typeof document === "undefined" || typeof document.createTextNode !== "function") return;
  // Splitting the text of a flex/grid container would turn each piece into its own item.
  if (typeof getComputedStyle === "function") {
    const display = getComputedStyle(el).display;
    if (/flex|grid/.test(display)) return;
  }
  const childNodes = el.childNodes;
  if (childNodes.length !== 1 || childNodes[0].nodeType !== 3) return;
  const raw = childNodes[0].nodeValue ?? "";
  if (!raw) return;
  const parts = segmentTypography(raw);
  if (parts.length === 1 && parts[0].kind === "text") return;
  const nodes = [];
  for (const part of parts) {
    if (part.kind === "text") nodes.push(document.createTextNode(part.value));
    else {
      const span = document.createElement("span");
      span.className = part.kind === "nw" ? "nw" : "mono";
      span.textContent = part.value;
      nodes.push(span);
    }
  }
  el.replaceChildren(...nodes);
}

const observed = new WeakMap();

/**
 * @param {ParentNode} root
 * @param {string} selector
 */
export function watchText(root, selector) {
  if (!root || typeof MutationObserver !== "function" || typeof document === "undefined") return;
  if (typeof root.querySelectorAll !== "function") return;
  if (observed.has(root)) return;
  const pending = new Set();
  let scheduled = false;
  const flush = () => {
    scheduled = false;
    for (const el of pending) {
      pending.delete(el);
      if (el.childNodes?.length === 1 && el.childNodes[0].nodeType === 3) enhanceText(el);
    }
  };
  const queue = (el) => {
    if (!el || el.nodeType !== 1) return;
    pending.add(el);
    if (!scheduled) {
      scheduled = true;
      queueMicrotask(flush);
    }
  };
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "characterData" && record.target?.parentElement) {
        queue(record.target.parentElement);
        continue;
      }
      if (record.type === "childList") {
        if (record.target?.nodeType === 1) {
          const target = record.target;
          if (target.matches?.(selector)) queue(target);
          target.querySelectorAll?.(selector).forEach((node) => queue(node));
        }
        for (const node of record.addedNodes) {
          if (node.nodeType !== 1) continue;
          if (node.matches?.(selector)) queue(node);
          node.querySelectorAll?.(selector).forEach((child) => queue(child));
        }
      }
    }
  });
  observer.observe(root, { childList: true, characterData: true, subtree: true });
  if (root.matches?.(selector)) enhanceText(root);
  root.querySelectorAll(selector).forEach((el) => enhanceText(el));
  observed.set(root, observer);
}

/**
 * Install observers for dynamic copy; no-op in the review harness (no createTextNode / querySelectorAll).
 */
export function installTypographyEnhancements() {
  if (typeof document === "undefined" || typeof document.createTextNode !== "function") return;
  const qsa = document.querySelectorAll?.bind(document);
  if (!qsa) return;

  // The banner text is replaced by many flows; re-segment it every time.
  const banner = document.querySelector("#banner");
  if (banner) watchText(banner, "#banner");

  const verify = document.querySelector("#verify");
  if (verify) {
    watchText(verify, "p[data-check], #wallet-outcome, #poison-note, .lock-note");
  }

  for (const id of ["session", "wallet-status"]) {
    const node = document.querySelector(`#${id}`);
    if (node) watchText(node, `#${id}`);
  }

  const walletList = document.querySelector("#wallet-list");
  if (walletList) watchText(walletList, ".wallet-meta");

  const picker = document.querySelector("#wallet-picker");
  if (picker) watchText(picker, ".wc-panel, .wc-panel p, .wc-status");

  const view = document.querySelector("#view");
  if (view) {
    watchText(view, "p, li, dt, dd, .mode, .status");
  }

  const strip = document.querySelector(".status-strip");
  if (strip) watchText(strip, ".chip-v");
}

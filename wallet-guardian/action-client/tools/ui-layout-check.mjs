/**
 * UI layout + CSP check for the BUILT action client (dist/). Developer tool, not part of the gates.
 *
 * Serves dist/ at the production origin inside headless Chrome (all requests intercepted; /api/* is
 * answered with the same unauthenticated responses production gives), applies dist/_headers (CSP),
 * and for 360/412/768/1440 widths x light/dark takes full-page screenshots of: home, task page,
 * wallets detected (synthetic EIP-6963 + binancew3w providers that reject every request),
 * WalletConnect selected, synthetic sample loaded, and the WalletConnect QR state against the
 * in-process MockRelay (never Reown's relay; pairing URIs are never logged).
 *
 * Reports per shot: CSP violations, console/page errors, horizontal overflow, elements whose content
 * overflows their box, status-chip rows with unequal height/value alignment, multi-line buttons, and
 * broken .nw (nowrap) tokens. Screenshots contain QR codes: keep the output outside the repo.
 *
 * Usage: node tools/ui-layout-check.mjs [distDir=dist] [outDir=/tmp/wg-ui-check] [state-prefixes,comma-separated]
 * Needs Chrome/Chromium (WG_CHROME, /opt/google/chrome/chrome, /usr/bin/chromium, /usr/bin/google-chrome).
 */
import { readFileSync, existsSync, writeFileSync, mkdirSync } from "node:fs";
import { join, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { MockRelay } from "../tests/wc-relay-mock.mjs";
import { productionViewOrigin } from "../src/view-origin.mjs";

const CLIENT = fileURLToPath(new URL("..", import.meta.url));
const [distArg, outArg, only] = process.argv.slice(2);
const dir = resolve(CLIENT, distArg || "dist");
const OUT = resolve(outArg || "/tmp/wg-ui-check");
mkdirSync(OUT, { recursive: true });
const label = "shot";
const ORIGIN = productionViewOrigin();
const REF = "ref_" + "ab".repeat(16); // synthetic; the page only sees the unauthenticated 401
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json", ".ico": "image/x-icon" };
if (!existsSync(join(dir, "_headers"))) throw new Error(`${dir}/_headers missing: run npm run build first`);
const headers = {};
for (const line of readFileSync(join(dir, "_headers"), "utf8").split("\n")) {
  const m = line.match(/^\s+([A-Za-z-]+):\s*(.+)$/);
  if (m) headers[m[1].toLowerCase()] = m[2];
}
function chromePath() {
  for (const candidate of [process.env.WG_CHROME, "/opt/google/chrome/chrome", "/usr/bin/chromium", "/usr/bin/google-chrome"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  throw new Error("Chrome/Chromium not found (set WG_CHROME)");
}
const API = {
  "/api/session": [200, { authenticated: false, erc1271: "unsupported", execution_authorized: false }],
  "/api/rpc": [404, { error: "rpc_not_supported", execution_authorized: false }],
};
const VIEWPORTS = {
  m360: { viewport: { width: 360, height: 800 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
  m412: { viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
  t768: { viewport: { width: 768, height: 1024 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 },
  d1440: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
};
const ICON = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#3b6fd8"/><path d="M6 12h12M12 6v12" stroke="#fff" stroke-width="2"/></svg>');

const STATES = [
  ...["m360", "m412", "t768", "d1440"].flatMap(v => ["light", "dark"].flatMap(s => [
    { name: `home-${v}-${s}`, path: "/", v, s },
    { name: `task-${v}-${s}`, path: `/t/${REF}`, v, s },
    { name: `task-wallets-${v}-${s}`, path: `/t/${REF}`, v, s, wallets: true, crops: v === "m412" },
    { name: `task-wc-selected-${v}-${s}`, path: `/t/${REF}`, v, s, wallets: true, action: "wc-select" },
    { name: `sample-${v}-${s}`, path: "/", v, s, action: "sample", crops: v === "m412" },
  ])),
  { name: "task-wc-qr-m412-light", path: `/t/${REF}`, v: "m412", s: "light", action: "wc-qr" },
  { name: "task-wc-qr-m412-dark", path: `/t/${REF}`, v: "m412", s: "dark", action: "wc-qr" },
  { name: "task-wc-qr-m360-light", path: `/t/${REF}`, v: "m360", s: "light", action: "wc-qr" },
  { name: "task-wc-qr-d1440-light", path: `/t/${REF}`, v: "d1440", s: "light", action: "wc-qr" },
].filter(x => !only || only.split(",").some(o => x.name.startsWith(o)));

const browser = await chromium.launch({ executablePath: chromePath(), headless: true, args: ["--no-sandbox"] });
const report = [];
const redact = (t) => String(t).replace(/wc:[^\s"']+/g, "wc:<redacted>");
for (const st of STATES) {
  const ctx = await browser.newContext({ ...VIEWPORTS[st.v], colorScheme: st.s, locale: "zh-TW" });
  const relay = new MockRelay();
  await ctx.routeWebSocket(/^wss?:\/\//, (ws) => {
    if (!ws.url().startsWith("wss://relay.walletconnect.org/")) { ws.close({ code: 1008, reason: "blocked" }); return; }
    const conn = relay.attach((text) => ws.send(text), "page");
    ws.onMessage((m) => conn.receive(String(m)));
    ws.onClose(() => conn.close());
  });
  await ctx.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === "https://verify.walletconnect.org") return route.fulfill({ status: 200, headers: { "content-type": "text/html" }, body: "<!doctype html><title>verify stub</title>" });
    if (url.origin !== ORIGIN) return route.abort();
    if (url.pathname.startsWith("/api/")) {
      const [status, body] = API[url.pathname] ?? (url.pathname.startsWith("/api/tasks/") ? [401, { error: "unauthenticated", execution_authorized: false }] : [404, { error: "not_found" }]);
      return route.fulfill({ status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" }, body: JSON.stringify(body) });
    }
    const file = url.pathname === "/" || url.pathname.startsWith("/t/") ? "index.html" : decodeURIComponent(url.pathname.slice(1));
    const path = join(dir, file);
    if (!path.startsWith(dir) || !existsSync(path)) return route.fulfill({ status: 404, headers, body: "not found" });
    return route.fulfill({ status: 200, headers: { ...headers, "content-type": TYPES[extname(file)] ?? "application/octet-stream" }, body: readFileSync(path) });
  });
  await ctx.addInitScript(({ wallets, icon }) => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    if (!wallets || window.top !== window) return;
    const mk = (name) => ({ on() {}, removeListener() {}, async request({ method }) {
      if (method === "eth_accounts") return []; if (method === "eth_chainId") return "0x38";
      const e = new Error("rejected in screenshot harness"); e.code = 4001; throw e; } });
    const a = mk("a");
    window.binancew3w = { ethereum: mk("binance") };
    window.addEventListener("eip6963:requestProvider", () => {
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", { detail: Object.freeze({
        info: Object.freeze({ uuid: "00000000-0000-4000-8000-000000000002", name: "Example Wallet", rdns: "io.example.wallet", icon }), provider: a }) }));
    });
  }, { wallets: !!st.wallets, icon: ICON });
  const page = await ctx.newPage();
  const rec = { console: [], pageerrors: [] };
  page.on("console", (m) => { if (m.type() === "error") rec.console.push(redact(m.text())); });
  page.on("pageerror", (e) => rec.pageerrors.push(redact(e.message)));
  page.on("dialog", (d) => d.dismiss());
  const resp = await page.goto(ORIGIN + st.path, { waitUntil: "load" });
  await page.waitForTimeout(2000);
  let note = "";
  if (st.action === "wc-select" || st.action === "wc-qr") {
    const wc = page.locator("#wallet-list button[data-source=walletconnect]");
    if (await wc.count()) { await wc.click(); await page.waitForTimeout(800); } else note += "no-wc-option ";
  }
  if (st.action === "sample") {
    await page.click("#load-sample");
    try { await page.waitForFunction(() => document.querySelector("#view")?.textContent?.includes("Lista"), null, { timeout: 15000 }); note += "sample-loaded "; }
    catch { note += "sample-timeout "; }
    await page.waitForTimeout(1500);
  }
  if (st.action === "wc-qr") {
    await page.click("#login");
    try { await page.waitForFunction(() => (document.querySelector("#wc-panel canvas")?.width ?? 0) > 0, null, { timeout: 20000 }); note += "qr-drawn "; }
    catch { note += "qr-timeout "; }
    await page.waitForTimeout(500);
  }
  const layout = await page.evaluate(() => {
    const out = { overflowing: [], chipRows: [], walletLeft: [], multilineButtons: [], brokenNowrap: 0 };
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const name = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : "");
    for (const el of document.querySelectorAll("main *")) {
      if (!vis(el)) continue;
      const cs = getComputedStyle(el);
      if (["auto", "scroll", "hidden", "clip"].includes(cs.overflowX) || el.closest("pre, details.code-fold pre, canvas")) continue;
      if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0 && cs.display !== "inline") out.overflowing.push(name(el) + ` ${el.scrollWidth}>${el.clientWidth}`);
      const r = el.getBoundingClientRect();
      if (r.right > window.innerWidth + 0.5) out.overflowing.push(name(el) + ` right=${Math.round(r.right)}>${window.innerWidth}`);
    }
    const chips = [...document.querySelectorAll(".status-strip .chip:not(.chip-seal)")].filter(vis);
    const rows = new Map();
    for (const c of chips) { const t = Math.round(c.getBoundingClientRect().top); if (!rows.has(t)) rows.set(t, []); rows.get(t).push(c); }
    for (const [t, cs] of rows) {
      const hs = cs.map(c => Math.round(c.getBoundingClientRect().height));
      const vt = cs.map(c => Math.round(c.querySelector(".chip-v")?.getBoundingClientRect().top ?? -1));
      out.chipRows.push({ top: t, heights: hs, valueTops: vt, equal: Math.max(...hs) - Math.min(...hs) <= 1 && Math.max(...vt) - Math.min(...vt) <= 1 });
    }
    out.walletLeft = [...document.querySelectorAll("#wallet-list .wallet-option")].filter(vis).map(b => {
      const n = b.querySelector(".wallet-name"); const icon = b.querySelector(".wallet-icon"); return [Math.round(b.getBoundingClientRect().left), Math.round(n.getBoundingClientRect().left)];
    });
    for (const b of document.querySelectorAll("main button:not(.wallet-option), main label.file, main a.wc-link")) {
      if (!vis(b)) continue;
      const range = document.createRange(); range.selectNodeContents(b);
      const tops = new Set([...range.getClientRects()].filter(r => r.width > 1).map(r => Math.round(r.top)));
      if (tops.size > 1) out.multilineButtons.push(b.textContent.trim().slice(0, 30));
    }
    for (const n of document.querySelectorAll(".nw")) if (vis(n) && n.getClientRects().length > 1) out.brokenNowrap += 1;
    return out;
  });
  if (st.crops) {
    for (const [sel, tag] of [[".status-strip", "chips"], ["#wallet-list", "wallets"], ["#verify", "verify"], ["#banner", "banner"], ["#view", "view"]]) {
      const loc = page.locator(sel).first();
      if (await loc.count() && await loc.isVisible()) await loc.screenshot({ path: `${OUT}/crop-${label}-${tag}-${st.name}.png` }).catch(() => {});
    }
  }
  const csp = await page.evaluate(() => window.__csp);
  const overflowX = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  const file = `${OUT}/${label}-${st.name}.png`;
  await page.screenshot({ path: file, fullPage: true });
  report.push({ shot: file, status: resp?.status(), csp_header: !!resp?.headers()["content-security-policy"], csp_violations: csp, console_errors: rec.console, page_errors: rec.pageerrors, horizontal_overflow: overflowX, layout, note: note.trim() });
  await ctx.close();
}
await browser.close();
writeFileSync(`${OUT}/${label}-report.json`, JSON.stringify(report, null, 2));
for (const r of report) console.log(`${r.shot.split("/").pop()} status=${r.status} csp=${r.csp_header} viol=${r.csp_violations.length} cerr=${r.console_errors.length} perr=${r.page_errors.length} ovx=${r.horizontal_overflow} ovf=${r.layout.overflowing.length} chipsEq=${r.layout.chipRows.every(x => x.equal)} walletEdges=${new Set(r.layout.walletLeft.map(x => x.join(","))).size <= 1} mlBtn=${r.layout.multilineButtons.length} nwBroken=${r.layout.brokenNowrap} ${r.note}`);

// Hard failures: CSP/console/page errors, overflow, unequal chip rows, broken nowrap tokens.
// Multi-line buttons are reported only (the WalletConnect deep-link labels are long by design).
const failed = report.filter((r) => r.csp_violations.length || r.console_errors.length || r.page_errors.length
  || r.horizontal_overflow || r.layout.overflowing.length || r.layout.chipRows.some((c) => !c.equal) || r.layout.brokenNowrap);
console.log(`${report.length} shots, ${failed.length} failing; report: ${OUT}/${label}-report.json`);
if (failed.length) process.exitCode = 1;

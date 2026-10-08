/** WALLET-CONNECT-01 W-A: WA-01 discovery-order, WA-03 binance-fallback, WA-04 metadata-untrusted.
 * Synthetic providers only. Not a real-wallet or browser test (see integration/wallets-artifact.test.mjs).
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import test from 'node:test';
import { harness, tick, REF } from './client-review-harness.mjs';
import { eip6963Window, makeProvider, uuid, unauthenticated } from './wallet-fixtures.mjs';
import {
  createWalletRegistry, sanitizeIcon, sanitizeName, SOURCE_BINANCE, SOURCE_EIP6963, SOURCE_INJECTED,
} from '../src/wallets/registry.mjs';

const BINANCE_INFO = { uuid: uuid(1), name: 'Binance Wallet', rdns: 'com.binance.wallet', icon: 'data:image/png;base64,iVBORw0KGgo=' };
const OTHER_INFO = { uuid: uuid(2), name: 'Other Wallet', rdns: 'io.example.wallet', icon: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=' };
const LATE_INFO = { uuid: uuid(3), name: 'Late Wallet', rdns: 'org.late.wallet', icon: 'data:image/webp;base64,UklGRg==' };

function fakeWindow({ onRequest, ethereum, binance } = {}) {
  const log = [];
  const handlers = [];
  const win = {
    ethereum, binancew3w: binance ? { ethereum: binance } : undefined,
    addEventListener(event, handler) { log.push(`listen:${event}`); if (event === 'eip6963:announceProvider') handlers.push(handler); },
    dispatchEvent(event) { log.push(`dispatch:${event.type}`); if (event.type === 'eip6963:requestProvider' && onRequest) onRequest(); return true; },
    Event: class { constructor(type) { this.type = type; } },
  };
  const announce = (provider, info) => { for (const h of handlers) h({ detail: { info, provider } }); };
  return { win, log, announce };
}

async function settle(h, predicate) {
  for (let i = 0; i < 500 && !predicate(); i += 1) await tick();
}

const listText = (h) => h.elements.get('wallet-list').children.map((button) => button.children.map((c) => c.textContent).join(' | '));

// ---------------------------------------------------------------- WA-01 discovery-order

test('WA-01 listener is registered before requestProvider, and late/duplicate announcements are handled', () => {
  const binance = makeProvider('binance');
  const other = makeProvider('other');
  const late = makeProvider('late');
  const injected = makeProvider('injected');
  let fw;
  fw = fakeWindow({ ethereum: injected, onRequest: () => { fw.announce(binance, BINANCE_INFO); fw.announce(other, OTHER_INFO); } });
  let changes = 0;
  const registry = createWalletRegistry({ win: fw.win, onChange: () => { changes += 1; } });
  registry.start();
  assert.deepEqual(fw.log, ['listen:eip6963:announceProvider', 'dispatch:eip6963:requestProvider']);
  assert.deepEqual(registry.list().map((c) => c.provider.label), ['binance', 'other', 'injected']);
  // Late announcement after the request: still accepted (no single timeout decides "no wallet").
  fw.announce(late, LATE_INFO);
  // Duplicate announcement of the same provider: listed once, first metadata kept.
  fw.announce(binance, { ...BINANCE_INFO, name: 'Renamed later' });
  const list = registry.list();
  assert.deepEqual(list.map((c) => c.provider.label), ['binance', 'other', 'late', 'injected']);
  assert.deepEqual(list.map((c) => c.source), [SOURCE_EIP6963, SOURCE_EIP6963, SOURCE_EIP6963, SOURCE_INJECTED]);
  assert.equal(list[0].name, 'Binance Wallet');
  assert.equal(changes, 3);
  // Discovery never asks any wallet for anything.
  for (const p of [binance, other, late, injected]) assert.deepEqual(p.calls, []);
});

test('WA-01 page discovery with several wallets asks no wallet for accounts, and login waits for a choice', async () => {
  const binance = makeProvider('binance');
  const other = makeProvider('other');
  const late = makeProvider('late');
  const env = eip6963Window({ wallets: [
    { provider: binance, info: BINANCE_INFO },
    { provider: other, info: OTHER_INFO },
    { provider: late, info: LATE_INFO, late: true },
  ], ethereum: other });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  assert.deepEqual(env.log.slice(0, 2), ['listen:eip6963:announceProvider', 'dispatch:eip6963:requestProvider']);
  assert.equal(h.elements.get('wallet-list').children.length, 2);
  env.announce(late, LATE_INFO);
  await tick();
  assert.equal(h.elements.get('wallet-list').children.length, 3, 'late announcement is shown');
  assert.match(h.elements.get('wallet-status').textContent, /多個錢包/);
  // window.ethereum is the same object as "other": shown once.
  assert.equal(h.app.walletCandidates().length, 3);
  await h.app.login();
  for (const p of [binance, other, late]) assert.deepEqual(p.calls, [], `${p.label} was not contacted`);
  assert.match(h.elements.get('banner').textContent, /請先選擇/);
  assert.equal(h.app.getState().authBusy, false);
});

test('WA-01 no wallet yet is not reported as "you have no wallet"; a late wallet still appears', async () => {
  const late = makeProvider('late');
  const env = eip6963Window({ wallets: [{ provider: late, info: LATE_INFO, late: true }] });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  assert.equal(h.elements.get('wallet-list').children.length, 0);
  assert.match(h.elements.get('wallet-status').textContent, /這不代表你沒有錢包/);
  env.announce(late, LATE_INFO);
  await tick();
  assert.equal(h.elements.get('wallet-list').children.length, 1);
  assert.match(h.elements.get('login').textContent, /使用此錢包登入/);
  assert.deepEqual(late.calls, []);
});

// ---------------------------------------------------------------- WA-03 binance-fallback

test('WA-03 only window.binancew3w.ethereum: listed and usable for the fixed login', async () => {
  const binance = makeProvider('binance');
  const env = eip6963Window({ binance });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  const candidates = h.app.walletCandidates();
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].source, SOURCE_BINANCE);
  assert.match(h.elements.get('wallet-status').textContent, /1 個錢包/);
  await h.app.login();
  assert.deepEqual(binance.calls.map((c) => c.method), ['eth_requestAccounts', 'eth_chainId', 'eth_accounts', 'eth_chainId', 'personal_sign']);
  assert.equal(h.app.getState().csrfToken, 'a'.repeat(64));
  assert.equal(h.app.getState().selected.provider, binance);
  assert.equal(binance.calls.some((c) => /send|signTypedData|eth_sign$/.test(c.method)), false);
});

test('WA-03 the same Binance provider via EIP-6963, binancew3w and window.ethereum is listed once', () => {
  const binance = makeProvider('binance');
  let fw;
  fw = fakeWindow({ ethereum: binance, binance, onRequest: () => fw.announce(binance, BINANCE_INFO) });
  const registry = createWalletRegistry({ win: fw.win });
  registry.start();
  const list = registry.list();
  assert.equal(list.length, 1);
  assert.equal(list[0].source, SOURCE_EIP6963);
  assert.equal(list[0].name, 'Binance Wallet');
});

test('WA-03 binancew3w is a supplementary candidate after EIP-6963 and before window.ethereum', () => {
  const binance = makeProvider('binance');
  const other = makeProvider('other');
  const injected = makeProvider('injected');
  let fw;
  fw = fakeWindow({ ethereum: injected, binance, onRequest: () => fw.announce(other, OTHER_INFO) });
  const registry = createWalletRegistry({ win: fw.win });
  registry.start();
  assert.deepEqual(registry.list().map((c) => [c.provider.label, c.source]),
    [['other', SOURCE_EIP6963], ['binance', SOURCE_BINANCE], ['injected', SOURCE_INJECTED]]);
});

// ---------------------------------------------------------------- WA-04 metadata-untrusted

test('WA-04 forged rdns and a duplicate uuid cannot replace or merge into the selected provider', async () => {
  const binance = makeProvider('binance');
  const impostor = makeProvider('impostor');
  const squatter = makeProvider('squatter');
  const env = eip6963Window({ wallets: [{ provider: binance, info: BINANCE_INFO }] });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.selectWallet(h.app.walletCandidates()[0]);
  // Same rdns + name, different object: a separate entry, never merged into the selection.
  env.announce(impostor, { ...BINANCE_INFO, uuid: uuid(9) });
  // Same uuid as the selected wallet, different object: ignored.
  env.announce(squatter, { ...BINANCE_INFO, name: 'Binance Wallet (official)' });
  await tick();
  const labels = h.app.walletCandidates().map((c) => c.provider.label);
  assert.deepEqual(labels, ['binance', 'impostor']);
  assert.equal(h.app.getState().selected.provider, binance);
  await h.app.login();
  assert.equal(binance.calls.filter((c) => c.method === 'personal_sign').length, 1);
  assert.deepEqual(impostor.calls, []);
  assert.deepEqual(squatter.calls, []);
  // The challenge request carries the address and chain only; no rdns/uuid/name reaches the server.
  const challengeCall = h.calls.find((c) => c.path === '/api/auth/challenge');
  assert.deepEqual(Object.keys(JSON.parse(challengeCall.init.body)).sort(), ['address', 'chainId']);
});

test('WA-04 hostile names render as text and unsafe icons are dropped', async () => {
  const evil = makeProvider('evil');
  const svg = makeProvider('svg');
  const hostileName = '<img src=x onerror="globalThis.__pwned=1">\u202eBinance';
  const env = eip6963Window({ wallets: [
    { provider: evil, info: { uuid: uuid(4), name: hostileName, rdns: 'javascript:alert(1)', icon: 'javascript:alert(1)' } },
    { provider: svg, info: { uuid: uuid(5), name: 'Svg Wallet', rdns: 'io.svg.wallet', icon: 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+' } },
  ] });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  const [first, second] = h.elements.get('wallet-list').children;
  assert.equal(first.tagName, 'BUTTON');
  assert.equal(first.children.some((c) => c.tagName === 'IMG'), false, 'javascript: icon not rendered');
  const name = first.children.find((c) => c.className === 'wallet-name');
  assert.equal(name.textContent, '<img src=x onerror="globalThis.__pwned=1">Binance', 'bidi override stripped, markup kept as text');
  assert.equal(h.app.walletCandidates()[0].rdns, null, 'invalid rdns dropped');
  const img = second.children.find((c) => c.tagName === 'IMG');
  assert.equal(img.src, 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+', 'SVG only as <img> src');
  assert.equal(globalThis.__pwned, undefined);
  for (const bad of ['data:text/html,<script>1</script>', 'https://evil.example/icon.png', 'data:image/svg+xml,<svg>\n</svg>',
    'data:image/png;base64,' + 'A'.repeat(70000), 'DATA:application/javascript,1', 42, null]) {
    assert.equal(sanitizeIcon(bad), null, String(bad).slice(0, 40));
  }
  assert.equal(sanitizeName('   '), '未命名錢包');
  assert.equal([...sanitizeName('x'.repeat(500))].length, 64);
  assert.deepEqual(evil.calls, []);
  assert.deepEqual(svg.calls, []);
});

test('WA-04 client code never parses wallet strings as HTML, and the server never trusts wallet brand metadata', () => {
  const dir = new URL('../src/wallets/', import.meta.url);
  const sources = [readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8'),
    ...readdirSync(dir).map((f) => readFileSync(new URL(f, dir), 'utf8'))];
  for (const text of sources) assert.doesNotMatch(text, /\.innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/);
  const serverDir = new URL('../src/server/', import.meta.url);
  const server = readdirSync(serverDir).filter((f) => !f.endsWith('.test.mjs')).map((f) => readFileSync(new URL(f, serverDir), 'utf8'));
  const functionsDir = new URL('../functions/api/', import.meta.url);
  server.push(...readdirSync(functionsDir).map((f) => readFileSync(new URL(f, functionsDir), 'utf8')));
  for (const text of server) assert.doesNotMatch(text, /rdns|uuid|eip6963|binancew3w|wallet_brand/i);
  // The migration (D1 ACL) has no wallet brand/uuid column either.
  const migration = readFileSync(new URL('../migrations/0001_p1_init.sql', import.meta.url), 'utf8');
  assert.doesNotMatch(migration, /rdns|uuid|wallet_brand|provider_name/i);
});

test('WA-04 a malformed announcement (no provider.request, no uuid, throwing getters) is ignored', () => {
  const fw = fakeWindow();
  const registry = createWalletRegistry({ win: fw.win });
  registry.start();
  fw.announce({ notRequest: true }, { ...OTHER_INFO, uuid: uuid(6) });
  fw.announce(makeProvider('nouuid'), { name: 'No uuid' });
  const hostile = {};
  Object.defineProperty(hostile, 'detail', { get() { throw new Error('boom'); } });
  // A detail getter that throws must not break discovery.
  const handlers = [];
  const win = { addEventListener: (_e, fn) => handlers.push(fn) };
  const r2 = createWalletRegistry({ win });
  r2.start();
  assert.doesNotThrow(() => handlers[0](hostile));
  assert.equal(registry.list().length, 0);
  assert.equal(r2.list().length, 0);
});

test('WA-01 /t/ page keeps loading the private task for a restored session with one injected wallet (regression)', async () => {
  const h = await harness();
  await settle(h, () => h.elements.get('view').textContent.includes('Lista'));
  assert.match(h.elements.get('view').textContent, /Lista/);
  assert.equal(h.app.getState().privateRef, REF);
  assert.equal(h.walletCalls.length, 0, 'opening the page asks the wallet nothing');
});

test('WA-01 a wallet injected after load (no announcement) shows up on the legacy initialized event, without a wallet request', async () => {
  const env = eip6963Window({});
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  assert.equal(h.app.walletCandidates().length, 0);
  const lateInjected = makeProvider('late-injected');
  env.window.ethereum = lateInjected;
  h.windowListeners.get('ethereum#initialized')();
  assert.equal(h.elements.get('wallet-list').children.length, 1);
  assert.equal(h.app.walletCandidates()[0].source, SOURCE_INJECTED);
  assert.deepEqual(lateInjected.calls, []);
});

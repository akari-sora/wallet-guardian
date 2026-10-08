/** WALLET-CONNECT-01 W-B at page level: the real main.ts with WalletConnect enabled by build env, a
 * deterministic stand-in for the lazily loaded SDK module (tests/walletconnect-sdk.test.mjs covers the
 * real SDK), synthetic injected wallets, and (where noted) the real Pages Function handler over an
 * in-memory D1. Covers WB-03 (page), WB-04 (page), WB-05 mobile-return, WB-06 relay-isolation, and
 * the "WalletConnect off" build. Not a browser and not a real wallet.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { secp256k1 } from '@noble/curves/secp256k1';
import { harness, tick, REF } from './client-review-harness.mjs';
import { addressOf, realBackend, unauthenticated, walletSign } from './wallet-fixtures.mjs';
import { ns56 } from './wc-fixtures.mjs';
import { checksumAddress } from '../src/siwe-message.mjs';
import { digestBody } from '../src/canonical.mjs';
import { sha256Hex, randomHex } from '../src/server/http.mjs';
import { personalSignBytes } from '../src/wallets/request-policy.mjs';

const WC_ENV = { VITE_WALLETCONNECT_ENABLED: 'true', VITE_REOWN_PROJECT_ID: 'a'.repeat(32) };
const URI = `wc:${'ab'.repeat(32)}@2?expiryTimestamp=1791433751&relay-protocol=irn&symKey=${'cd'.repeat(32)}`;
const SYMKEY = 'cd'.repeat(32);

async function waitFor(predicate, rounds = 1500) {
  for (let i = 0; i < rounds && !predicate(); i += 1) await tick();
  return predicate();
}

function deferred() {
  let resolve; let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

/** Stand-in for src/wallets/walletconnect-sdk.mjs. `outcomes` drives each connect(): 'approve' | 'reject' | a deferred. */
function fakeSdk({ address, key, outcomes = [], initError = null } = {}) {
  const log = { inits: 0, connects: 0, requests: [], disconnects: 0, opts: null };
  let instance = null;
  const mod = {
    log,
    instance: () => instance,
    drawQr: () => true,
    clearQr: () => {},
    async initProvider(opts) {
      log.inits += 1;
      log.opts = opts;
      if (initError) throw initError;
      const listeners = new Map();
      instance = {
        session: null,
        chainId: 56,
        on(event, handler) { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(handler); },
        removeListener(event, handler) { listeners.get(event)?.delete(handler); },
        emit(event, value) { for (const handler of [...(listeners.get(event) ?? [])]) handler(value); },
        async connect() {
          log.connects += 1;
          instance.emit('display_uri', URI);
          const outcome = outcomes.shift() ?? 'approve';
          const result = typeof outcome === 'string' ? outcome : await outcome.promise;
          if (result === 'reject') throw new Error('User rejected.');
          instance.session = { topic: `topic-${log.connects}`, expiry: Math.floor(Date.now() / 1000) + 3600, namespaces: ns56(address) };
        },
        async request({ method, params }) {
          log.requests.push({ method, params });
          if (method === 'personal_sign') return walletSign(params[0], key);
          throw new Error(`fake sdk refuses ${method}`);
        },
        async disconnect() { log.disconnects += 1; instance.session = null; },
        signer: { cleanupPendingPairings: async () => {} },
      };
      return instance;
    },
  };
  return mod;
}

function wcButton(h) {
  return h.elements.get('wallet-list').children.find((b) => b.dataset?.source === 'walletconnect');
}
function panel(h) {
  return h.elements.get('wallet-picker').children.find((c) => c.id === 'wc-panel');
}
function anchors(h) {
  const root = panel(h);
  if (!root) return [];
  const links = root.children.find((c) => c.className === 'wc-links');
  return links ? links.children : [];
}
const bannerText = (h) => h.elements.get('banner').textContent;

async function publishTask(backend, address) {
  const sample = JSON.parse(readFileSync(new URL('../public/sample-task.json', import.meta.url), 'utf8'));
  delete sample.ref;
  for (const field of ['sender', 'execution_account', 'recipient']) sample.plan[field] = address;
  sample.plan.human_summary.sender = address;
  sample.plan.human_summary.recipient = address;
  sample.plan.digest = await digestBody(sample.plan);
  const secret = randomHex(32);
  const id = randomHex(8);
  await backend.db().prepare(
    `INSERT INTO publish_credentials (credential_id, secret_hash, tenant_id, wallet_address, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, NULL, NULL)`,
  ).bind(id, await sha256Hex(secret), 'tenant-wc', address.toLowerCase()).run();
  const created = await backend.fetchApi('/api/bot/tasks', {
    method: 'POST',
    headers: { authorization: `Bearer wgpub_${id}.${secret}`, 'content-type': 'application/json' },
    body: JSON.stringify({ expires_at: Math.floor(Date.now() / 1000) + 3600, task: sample }),
  });
  const body = await created.json();
  assert.equal(created.status, 201, JSON.stringify(body));
  return body.ref;
}

function newKey() {
  const key = secp256k1.utils.randomPrivateKey();
  return { key, address: checksumAddress(addressOf(key)) };
}

// ---------------------------------------------------------------- build switch

test('W-B off: without both build settings there is no WalletConnect option, no SDK, and injected login is unchanged', async () => {
  for (const env of [{}, { VITE_WALLETCONNECT_ENABLED: 'true' }, { VITE_REOWN_PROJECT_ID: 'a'.repeat(32) }, { VITE_WALLETCONNECT_ENABLED: 'false', VITE_REOWN_PROJECT_ID: 'a'.repeat(32) }]) {
    const sdk = fakeSdk({});
    const h = await harness({ env, initialSession: unauthenticated(), importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk } });
    assert.equal(h.app.walletConnect, null, JSON.stringify(env));
    assert.equal(wcButton(h), undefined);
    await h.app.login();
    assert.equal(sdk.log.inits, 0);
    assert.equal(h.walletCalls[0].method, 'eth_requestAccounts');
    assert.ok(h.app.getState().csrfToken);
  }
});

// ---------------------------------------------------------------- WC-ARTIFACT-02 (page): not chosen = no relay

test('WB-06/WC-ARTIFACT-02 WalletConnect offered but not chosen: injected login never loads the SDK or touches the relay', async () => {
  const sdk = fakeSdk({});
  const h = await harness({ env: WC_ENV, initialSession: unauthenticated(), importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk } });
  assert.ok(wcButton(h), 'WalletConnect option is listed');
  assert.match(wcButton(h).textContent, /WalletConnect/);
  // A single injected wallet stays the implicit W-A choice; WalletConnect is never implicit.
  await h.app.login();
  assert.equal(h.walletCalls[0].method, 'eth_requestAccounts');
  assert.equal(sdk.log.inits, 0, 'SDK never loaded on the injected path');
  assert.equal(h.app.walletConnect.sdkLoaded(), false);
  assert.ok(h.app.getState().csrfToken);
});

// ---------------------------------------------------------------- WB-05 mobile-return + full login on the real backend

test('WB-05 mobile-return: jump links from versioned config, leave/return, approve, fixed SIWE, original task loads; injected wallet untouched', async () => {
  const { key, address } = newKey();
  const backend = await realBackend();
  const ref = await publishTask(backend, address);
  const approval = deferred();
  const sdk = fakeSdk({ address, key, outcomes: [approval] });
  const h = await harness({
    env: WC_ENV, pathname: `/t/${ref}`, initialSession: unauthenticated(),
    onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined),
    importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk },
  });
  await h.app.selectWallet(h.app.wcCandidate);
  assert.equal(sdk.log.inits, 0, 'selecting WalletConnect alone does not connect');
  const login = h.app.login();
  assert.equal(await waitFor(() => anchors(h).length === 3), true);
  const links = anchors(h).map((a) => ({ href: a.href, target: a.target, rel: a.rel, cls: a.className }));
  const encoded = encodeURIComponent(URI);
  assert.deepEqual(links, [
    { href: `https://app.binance.com/cedefi/wc?uri=${encoded}`, target: '_blank', rel: 'noopener noreferrer', cls: 'wc-link wc-link-universal' },
    { href: `bnc://app.binance.com/cedefi/wc?uri=${encoded}`, target: undefined, rel: 'noopener noreferrer', cls: 'wc-link wc-link-native' },
    { href: URI, target: undefined, rel: 'noopener noreferrer', cls: 'wc-link wc-link-generic' },
  ]);
  assert.equal(sdk.log.opts.origin, 'https://wallet-guardian-action-client.pages.dev');
  assert.equal(sdk.log.opts.projectId, 'a'.repeat(32), 'project id comes from the build env');
  assert.equal(bannerText(h).includes('wc:'), false);
  // The user jumps to the wallet app (page hidden) and comes back before approving.
  h.documentListeners.get('visibilitychange')?.();
  h.windowListeners.get('pageshow')?.();
  h.windowListeners.get('focus')?.();
  await tick();
  approval.resolve('approve');
  await login;
  assert.equal(backend.seen.find((r) => r.path === '/api/auth/verify')?.status, 200, 'backend verified the WalletConnect signature');
  assert.equal(h.app.getState().sessionAddress.toLowerCase(), address.toLowerCase());
  assert.equal(await waitFor(() => backend.seen.some((r) => r.path === `/api/tasks/${ref}` && r.status === 200)), true, 'the original task was read');
  assert.deepEqual(sdk.log.requests.map((r) => r.method), ['personal_sign']);
  assert.match(sdk.log.requests[0].params[0], /^0x[0-9a-f]+$/, 'WalletConnect transport is hex');
  assert.match(new TextDecoder().decode(personalSignBytes(sdk.log.requests[0].params[0])), /Sign in to Wallet Guardian/);
  assert.deepEqual(h.walletCalls, [], 'the injected wallet was never asked');
  assert.deepEqual(anchors(h), [], 'jump links (pairing URI) are removed once connected');
  for (const call of h.calls) assert.equal(String(call.init?.body ?? '').includes(SYMKEY) || String(call.init?.body ?? '').includes('wc:'), false, call.path);
});

test('WB-05 wallet app refuses / is not installed: clear failure, the page keeps the task and a retry creates a fresh pairing', async () => {
  const { key, address } = newKey();
  const backend = await realBackend();
  const sdk = fakeSdk({ address, key, outcomes: ['reject', 'approve'] });
  const h = await harness({
    env: WC_ENV, initialSession: unauthenticated(),
    onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined),
    importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk },
  });
  await h.app.selectWallet(h.app.wcCandidate);
  await h.app.login();
  assert.match(bannerText(h), /錢包拒絕或沒有完成連線/);
  assert.match(bannerText(h), /登入未完成/);
  assert.equal(h.app.getState().csrfToken, null);
  assert.equal(h.app.getState().privateRef, REF, 'still on the original task page');
  assert.deepEqual(h.walletCalls, [], 'no silent fallback to the injected wallet');
  await h.app.login();
  assert.equal(sdk.log.connects, 2);
  assert.equal(sdk.log.inits, 1, 'one SDK instance per page');
  assert.equal(h.app.getState().sessionAddress.toLowerCase(), address.toLowerCase());
});

// ---------------------------------------------------------------- WB-06 relay-isolation (page)

test('WB-06 relay/SDK failure: WalletConnect fails explicitly, no fallback; the injected wallet still logs in', async () => {
  const sdk = fakeSdk({ initError: new Error('relay unavailable') });
  const h = await harness({ env: WC_ENV, initialSession: unauthenticated(), importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk } });
  await h.app.selectWallet(h.app.wcCandidate);
  await h.app.login();
  assert.match(bannerText(h), /WalletConnect 元件載入或初始化失敗。瀏覽器錢包仍可使用；不會改用其他錢包/);
  assert.deepEqual(h.walletCalls, [], 'the injected wallet was not used behind the user\'s back');
  assert.equal(h.app.getState().csrfToken, null);
  await h.app.selectWallet(h.app.walletCandidates()[0]);
  await h.app.login();
  assert.equal(h.walletCalls[0].method, 'eth_requestAccounts');
  assert.ok(h.app.getState().csrfToken, 'W-A login works while W-B is down');
});

// ---------------------------------------------------------------- WB-03 page

test('WB-03 page: repeated login clicks during pairing share one pairing; cancel clears the QR/links and fails the login', async () => {
  const pending = deferred();
  const sdk = fakeSdk({ address: '0x' + '1'.repeat(40), key: secp256k1.utils.randomPrivateKey(), outcomes: [pending] });
  const h = await harness({ env: WC_ENV, initialSession: unauthenticated(), importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk } });
  await h.app.selectWallet(h.app.wcCandidate);
  const first = h.app.login();
  assert.equal(await waitFor(() => anchors(h).length === 3), true);
  await h.app.login();
  await h.app.login();
  assert.equal(sdk.log.connects, 1);
  const cancel = panel(h).children.find((c) => c.className === 'row').children.find((b) => b.textContent.includes('取消'));
  cancel.listeners.get('click')();
  await first;
  assert.match(bannerText(h), /已取消 WalletConnect 連線/);
  assert.deepEqual(anchors(h), []);
  pending.resolve('approve'); // a late approval
  for (let i = 0; i < 20; i += 1) await tick();
  assert.equal(h.app.getState().csrfToken, null);
  assert.deepEqual(sdk.log.requests, [], 'nothing signed');
  assert.deepEqual(h.walletCalls, []);
});

// ---------------------------------------------------------------- WB-04 page

test('WB-04 page: wallet-side account change warns, wallet disconnect clears the wallet identity; nothing is auto-signed', async () => {
  const { key, address } = newKey();
  const backend = await realBackend();
  const sdk = fakeSdk({ address, key });
  const h = await harness({
    env: WC_ENV, initialSession: unauthenticated(),
    onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined),
    importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk },
  });
  await h.app.selectWallet(h.app.wcCandidate);
  await h.app.login();
  assert.ok(h.app.getState().csrfToken);
  const other = '0x' + '2'.repeat(40);
  sdk.instance().session = { ...sdk.instance().session, namespaces: ns56(other) };
  sdk.instance().emit('session_update', {});
  await tick();
  assert.match(bannerText(h), /錢包帳戶已變更/);
  assert.equal(h.app.getState().sessionAddress.toLowerCase(), address.toLowerCase(), 'site identity unchanged until a new login');
  sdk.instance().session = null;
  sdk.instance().emit('session_delete', {});
  await tick();
  assert.equal(h.app.walletConnect.state(), 'idle');
  assert.deepEqual(await h.app.walletConnect.provider.request({ method: 'eth_accounts' }), []);
  assert.equal(h.app.getState().reread.reasons.includes('provider_disconnected'), true);
  assert.deepEqual(sdk.log.requests.map((r) => r.method), ['personal_sign'], 'only the one login signature, never re-signed');
});

test('WB-04/W-A3 page: switching from WalletConnect to an injected wallet ends the WC session and revokes the site session', async () => {
  const { key, address } = newKey();
  const backend = await realBackend();
  const sdk = fakeSdk({ address, key });
  const h = await harness({
    env: WC_ENV, initialSession: unauthenticated(),
    onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined),
    importOverrides: { './wallets/walletconnect-sdk.mjs': () => sdk },
  });
  await h.app.selectWallet(h.app.wcCandidate);
  await h.app.login();
  assert.ok(h.app.getState().csrfToken);
  await h.app.selectWallet(h.app.walletCandidates()[0]);
  assert.equal(await waitFor(() => h.app.getState().csrfToken == null), true);
  assert.equal(sdk.log.disconnects, 1);
  assert.match(bannerText(h), /revoked=true/);
  const row = await backend.db().prepare('SELECT revoked_at FROM sessions').first();
  assert.notEqual(row.revoked_at, null);
  assert.deepEqual(h.walletCalls, [], 'switching asks the new wallet for nothing');
});

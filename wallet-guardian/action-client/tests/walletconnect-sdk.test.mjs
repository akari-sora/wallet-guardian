/** WALLET-CONNECT-01 W-B: WB-01..WB-04, WB-06 against the REAL pinned SDK.
 * Page side: src/wallets/walletconnect.mjs + walletconnect-sdk.mjs (@walletconnect/ethereum-provider 2.25.0).
 * Wallet side: a synthetic test wallet on @walletconnect/sign-client 2.25.0. Both talk through an
 * in-process mock relay (tests/wc-relay-mock.mjs) on 127.0.0.1. No real relay, no real wallet app,
 * no Reown network access. Keys are ephemeral.
 */
process.env.DISABLE_GLOBAL_CORE = 'true'; // one SDK core per instance, like separate browsers
import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { WebSocket } from 'ws';
if (typeof globalThis.WebSocket === 'undefined') globalThis.WebSocket = WebSocket;
import { secp256k1 } from '@noble/curves/secp256k1';
import { startRelayServer, MemoryStorage } from './wc-relay-mock.mjs';
import { createTestWallet, closeSdk, ns56, TEST_PROJECT_ID } from './wc-fixtures.mjs';
import { addressOf } from './wallet-fixtures.mjs';
import { createWalletConnectConnection, FLAG_KEY, WalletConnectError } from '../src/wallets/walletconnect.mjs';
import { proposalMatchesContract, proposalScope } from '../src/wallets/walletconnect-session.mjs';
import { WC_PROPOSAL_SCOPE, WC_RELAY_URL, deepLinksFor, walletConnectConfig } from '../src/wallets/walletconnect-config.mjs';
import { guardedRequest, encodePersonalSignMessage, ENCODING_HEX } from '../src/wallets/injected.mjs';
import { REFUSED_METHODS } from '../src/wallets/request-policy.mjs';
import { buildSiweMessage, checksumAddress } from '../src/siwe-message.mjs';
import { LOGIN_DOMAIN, LOGIN_URI } from '../src/siwe-gate.mjs';
import { recoverAddress } from '../src/server/siwe.mjs';
import { productionViewOrigin } from '../src/view-origin.mjs';

const ORIGIN = productionViewOrigin();
const RPC = 'https://bsc-dataseed.bnbchain.org';
const loadSdk = () => import('../src/wallets/walletconnect-sdk.mjs');
const cleanups = [];
let prefixSeq = 0;
// SDK cores keep relay/heartbeat timers alive; cleanup is best effort and the scripts run node --test
// with --test-force-exit so a lingering SDK timer cannot hang the run.
after(() => { for (const fn of cleanups) fn().catch(() => {}); });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate, ms = 8000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await predicate()) return true; await sleep(10); }
  return false;
}

function newKey() {
  const key = secp256k1.utils.randomPrivateKey();
  return { key, address: checksumAddress(addressOf(key)) };
}

async function relay() {
  const server = await startRelayServer();
  cleanups.push(() => server.close());
  return server;
}

/** Page-side WalletConnect connection on the real SDK, pointed at the mock relay. */
function page(server, { storage = new MemoryStorage(), prefix, flag = new Map(), timeouts, nowSec } = {}) {
  const statuses = [];
  const flagStorage = { getItem: (k) => flag.get(k) ?? null, setItem: (k, v) => flag.set(k, v), removeItem: (k) => flag.delete(k) };
  const conn = createWalletConnectConnection({
    projectId: TEST_PROJECT_ID, origin: ORIGIN, rpcUrl: RPC, loadSdk,
    sdkOptions: { relayUrl: server.url, storage, customStoragePrefix: prefix ?? `page-${++prefixSeq}` },
    onStatus: (s) => statuses.push(s), flagStorage, timeouts, nowSec,
  });
  cleanups.push(() => closeSdk(conn.sdkInstance()));
  const events = [];
  for (const name of ['accountsChanged', 'chainChanged', 'disconnect']) conn.provider.on(name, (v) => events.push({ name, value: v }));
  return { conn, statuses, events, flag, storage };
}

/** Wallet "scans the QR": takes the URI from the pairing status, exactly what the canvas/links show. */
function scanWhenShown(p, wallet) {
  const seen = new Set();
  return until(async () => {
    const s = p.statuses.find((x) => x.state === 'pairing' && x.uri && !seen.has(x.uri));
    if (!s) return false;
    seen.add(s.uri);
    await wallet.pair(s.uri);
    return true;
  });
}

async function connected(p, wallet) {
  const accounts = p.conn.provider.request({ method: 'eth_requestAccounts' });
  await scanWhenShown(p, wallet);
  return accounts;
}

function fixedMessage(address) {
  const issued = new Date(Math.floor(Date.now() / 1000) * 1000);
  return buildSiweMessage({ domain: LOGIN_DOMAIN, address, statement: 'Sign in to Wallet Guardian. This only starts a read-only session. It is not a transaction and does not authorize sending.',
    uri: LOGIN_URI, version: '1', chainId: 56, nonce: 'e'.repeat(32), issuedAt: issued.toISOString(),
    expirationTime: new Date(issued.getTime() + 300_000).toISOString() });
}

// ---------------------------------------------------------------- config / fail closed

test('W-B config: WalletConnect is off unless VITE_WALLETCONNECT_ENABLED=true AND a valid project id', () => {
  assert.equal(walletConnectConfig({}).enabled, false);
  assert.equal(walletConnectConfig({ enabled: 'true' }).enabled, false);
  assert.equal(walletConnectConfig({ projectId: TEST_PROJECT_ID }).enabled, false);
  assert.equal(walletConnectConfig({ enabled: 'false', projectId: TEST_PROJECT_ID }).enabled, false);
  assert.equal(walletConnectConfig({ enabled: 'true', projectId: 'not-a-project-id' }).enabled, false);
  assert.deepEqual(walletConnectConfig({ enabled: 'true', projectId: TEST_PROJECT_ID }), { enabled: true, projectId: TEST_PROJECT_ID, reason: 'enabled' });
});

// ---------------------------------------------------------------- WB-01 actual-proposal

test('WB-01 actual-proposal: the SDK-generated proposal asks only eip155:56 personal_sign (no tx/permission methods)', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const wallet = await createTestWallet(server.url, { key, address });
  cleanups.push(() => wallet.close());
  const p = page(server);
  const [account] = await connected(p, wallet);
  assert.equal(account, address.toLowerCase());
  assert.equal(wallet.proposals.length, 1);
  const proposal = wallet.proposals[0];
  // sign-client 2.25.0 moves requiredNamespaces into optionalNamespaces on the wire.
  assert.deepEqual(proposal.requiredNamespaces, {});
  assert.deepEqual(proposal.optionalNamespaces, JSON.parse(JSON.stringify(WC_PROPOSAL_SCOPE)));
  assert.equal(proposalMatchesContract(proposal), true);
  const scope = proposalScope(proposal);
  assert.deepEqual(scope.methods, ['personal_sign']);
  assert.deepEqual(scope.chains, ['eip155:56']);
  assert.deepEqual(scope.txLikeMethods, []);
  for (const method of [...REFUSED_METHODS, 'eth_sendTransaction', 'wallet_sendCalls', 'wallet_getCapabilities']) {
    assert.equal(JSON.stringify(proposal).includes(`"${method}"`), false, method);
  }
  assert.equal(proposal.proposer.metadata.url, ORIGIN);
  // The options the page hands the SDK (not the SDK defaults, which include eth_sendTransaction).
  const { providerOptions } = await loadSdk();
  const options = providerOptions({ projectId: TEST_PROJECT_ID, origin: ORIGIN, rpcUrl: RPC });
  assert.deepEqual([options.chains, options.optionalChains, options.methods, options.optionalMethods, options.optionalEvents], [[56], [], ['personal_sign'], [], []]);
  assert.equal(options.showQrModal, false);
  assert.equal(options.telemetryEnabled, false);
  assert.equal(options.relayUrl, WC_RELAY_URL);
  assert.deepEqual(options.rpcMap, { 56: RPC });
  t_diag(`proposal=${JSON.stringify({ required: proposal.requiredNamespaces, optional: proposal.optionalNamespaces })}`);
});

function t_diag(text) { process.stdout.write(`# ${text}\n`); }

// ---------------------------------------------------------------- WB-02 session-scope

test('WB-02 session-scope: extra methods/chains approved by the wallet never widen what the page sends', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const extra = ['personal_sign', 'eth_sendTransaction', 'eth_signTypedData_v4', 'wallet_switchEthereumChain', 'wallet_sendCalls'];
  const wallet = await createTestWallet(server.url, { key, address,
    namespaces: (a) => ns56(a, { methods: extra, extraAccounts: [`eip155:1:${a}`] }) });
  cleanups.push(() => wallet.close());
  const p = page(server);
  await connected(p, wallet);
  const connectedStatus = p.statuses.filter((s) => s.state === 'connected').at(-1);
  assert.deepEqual(connectedStatus.extraMethods, ['eth_sendTransaction', 'eth_signTypedData_v4', 'wallet_sendCalls', 'wallet_switchEthereumChain']);
  assert.equal(await p.conn.provider.request({ method: 'eth_chainId' }), '0x38');
  const candidate = p.conn.candidate;
  // wallet_getCapabilities passes the policy on a user action (W-A) but this session does not offer it: "unknown".
  await assert.rejects(guardedRequest(candidate, 'wallet_getCapabilities', undefined, { userAction: true }), (e) => e instanceof WalletConnectError && e.code === 4200);
  for (const method of ['eth_sendTransaction', 'eth_signTypedData_v4', 'wallet_switchEthereumChain', 'wallet_sendCalls', 'eth_sign']) {
    // Request policy refuses first; the facade refuses even if called directly.
    await assert.rejects(guardedRequest(candidate, method, [{}], { userAction: true }), { name: 'WalletPolicyError' });
    await assert.rejects(p.conn.provider.request({ method, params: [{}] }), (e) => e instanceof WalletConnectError && e.code === 4200);
  }
  // The SDK subclass itself only forwards personal_sign.
  const sdk = p.conn.sdkInstance();
  await assert.rejects(sdk.request({ method: 'eth_sendTransaction', params: [{}] }), /refuses eth_sendTransaction/);
  // A wallet chain event to an approved non-56 chain: the SDK would send wallet_switchEthereumChain; ours does not.
  await wallet.emit('chainChanged', 1, 'eip155:1');
  assert.equal(await until(() => p.events.some((e) => e.name === 'chainChanged' && e.value === '0x1'), 4000), true);
  const message = fixedMessage(address);
  await assert.rejects(p.conn.provider.request({ method: 'personal_sign', params: [encodePersonalSignMessage(message, ENCODING_HEX), address] }), { reason: 'wrong_chain' });
  await sleep(300);
  assert.deepEqual(wallet.requests, [], 'the wallet received no request at all (no switch, no tx, no signature)');
});

test('WB-02 a session without an eip155:56 account, or without personal_sign, is refused and disconnected', async () => {
  for (const [label, namespaces, reason] of [
    ['no-56', (a) => ({ eip155: { accounts: [`eip155:1:${a}`], methods: ['personal_sign'], events: ['accountsChanged', 'chainChanged'], chains: ['eip155:1'] } }), 'no_bsc_account'],
    ['no-personal_sign', (a) => ns56(a, { methods: ['eth_sendTransaction'] }), 'no_personal_sign'],
  ]) {
    const server = await relay();
    const { key, address } = newKey();
    const wallet = await createTestWallet(server.url, { key, address, namespaces });
    cleanups.push(() => wallet.close());
    const p = page(server);
    const attempt = connected(p, wallet);
    await assert.rejects(attempt, (e) => e instanceof WalletConnectError && e.reason === reason, label);
    assert.deepEqual(await p.conn.provider.request({ method: 'eth_accounts' }), [], label);
    assert.equal(p.conn.state(), 'failed');
    assert.equal(await until(() => wallet.client.session.getAll().length === 0, 4000), true, `${label}: wallet session deleted by the page`);
    assert.equal(p.flag.has(FLAG_KEY), false);
  }
});

// ---------------------------------------------------------------- WB-03 pairing-lifecycle

test('WB-03 cancel during pairing, then a late approval of that pairing: never becomes connected', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const wallet = await createTestWallet(server.url, { key, address, autoApprove: false });
  cleanups.push(() => wallet.close());
  const p = page(server);
  const attempt = p.conn.provider.request({ method: 'eth_requestAccounts' });
  await scanWhenShown(p, wallet);
  assert.equal(await until(() => wallet.pendingProposal != null), true);
  p.conn.cancel();
  await assert.rejects(attempt, { reason: 'cancelled' });
  assert.equal(p.statuses.at(-1).uri, null, 'the URI is dropped from the UI state');
  // The user approves on the phone anyway, after cancelling on the page.
  await wallet.approve(wallet.pendingProposal.id).catch(() => {});
  await sleep(800);
  assert.deepEqual(await p.conn.provider.request({ method: 'eth_accounts' }), []);
  assert.notEqual(p.conn.state(), 'connected');
  assert.equal(p.flag.has(FLAG_KEY), false);
});

test('WB-03 repeated connect clicks share one pairing; proposal rejection and expiry fail cleanly', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const wallet = await createTestWallet(server.url, { key, address });
  cleanups.push(() => wallet.close());
  const p = page(server);
  const first = p.conn.provider.request({ method: 'eth_requestAccounts' });
  const second = p.conn.provider.request({ method: 'eth_requestAccounts' });
  await scanWhenShown(p, wallet);
  assert.deepEqual(await first, await second);
  assert.equal(wallet.proposals.length, 1);
  assert.equal(p.statuses.filter((s) => s.state === 'pairing').length, 1);

  const rejecting = await createTestWallet(server.url, { key, address, reject: true });
  cleanups.push(() => rejecting.close());
  const p2 = page(server);
  await assert.rejects(connected(p2, rejecting), { reason: 'rejected' });
  assert.equal(p2.conn.state(), 'failed');

  const p3 = page(server, { timeouts: { pairingMs: 600 } });
  await assert.rejects(p3.conn.provider.request({ method: 'eth_requestAccounts' }), { reason: 'expired' });
  assert.equal(p3.statuses.at(-1).state, 'failed');
  assert.equal(p3.statuses.at(-1).uri, null);
});

test('WB-03 no URI/key leak: pairing URI only in the pairing status (QR/links), never in console, fetch or storage', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const wallet = await createTestWallet(server.url, { key, address });
  cleanups.push(() => wallet.close());
  const logged = [];
  const fetched = [];
  const original = { log: console.log, warn: console.warn, error: console.error, info: console.info, debug: console.debug, fetch: globalThis.fetch };
  for (const name of ['log', 'warn', 'error', 'info', 'debug']) console[name] = (...args) => logged.push(args.map(String).join(' '));
  globalThis.fetch = async (...args) => { fetched.push(String(args[0])); throw new Error('no network in this test'); };
  try {
    const p = page(server);
    await connected(p, wallet);
    const pairing = p.statuses.filter((s) => s.state === 'pairing');
    assert.equal(pairing.length, 1);
    const uri = pairing[0].uri;
    const symKey = new URLSearchParams(uri.split('?')[1]).get('symKey');
    assert.match(symKey, /^[0-9a-f]{64}$/);
    assert.deepEqual(pairing[0].links.map((l) => l.href), deepLinksFor(uri).map((l) => l.href));
    for (const s of p.statuses.filter((x) => x.state !== 'pairing')) {
      assert.equal(s.uri, null);
      assert.deepEqual(s.links, []);
      assert.equal(JSON.stringify(s).includes(symKey), false);
    }
    assert.deepEqual([...p.flag.entries()], [[FLAG_KEY, '1']], 'only a non-secret "used" flag in page storage');
    assert.equal(logged.some((line) => line.includes('wc:') || line.includes(symKey)), false);
    // The page side makes no HTTP request here (telemetry off, rpcMap unused, Verify iframe is browser-only).
    // The synthetic *wallet* resolves the proposal attestation (verify.walletconnect.org) like a real wallet would.
    assert.equal(fetched.every((url) => url.startsWith('https://verify.walletconnect.org/attestation/')), true, fetched.join(','));
    assert.equal(fetched.some((url) => url.includes(symKey) || url.includes('wc:') || /pulse|rpc\.walletconnect|echo\./.test(url)), false);
  } finally {
    Object.assign(console, { log: original.log, warn: original.warn, error: original.error, info: original.info, debug: original.debug });
    globalThis.fetch = original.fetch;
  }
});

// ---------------------------------------------------------------- WB-04 update-and-restore

test('WB-04 account change via session update, wallet disconnect, expiry: identity follows, nothing is signed', async () => {
  const server = await relay();
  const one = newKey();
  const two = newKey();
  const wallet = await createTestWallet(server.url, { key: one.key, address: one.address });
  cleanups.push(() => wallet.close());
  let now = Math.floor(Date.now() / 1000);
  const p = page(server, { nowSec: () => now });
  await connected(p, wallet);
  assert.equal(p.conn.address(), one.address.toLowerCase());
  await wallet.update(ns56(two.address));
  assert.equal(await until(() => p.conn.address() === two.address.toLowerCase()), true);
  assert.deepEqual(p.events.filter((e) => e.name === 'accountsChanged').at(-1).value, [two.address.toLowerCase()]);
  // Expiry (clock moved past session.expiry): treated as disconnected.
  const expiry = p.conn.sdkInstance().session.expiry;
  now = expiry + 1;
  assert.deepEqual(await p.conn.provider.request({ method: 'eth_accounts' }), []);
  assert.equal(p.events.at(-1).name, 'disconnect');
  assert.equal(p.events.at(-1).value.reason, 'expired');
  now = Math.floor(Date.now() / 1000);
  // Wallet-side delete.
  const p2 = page(server);
  const wallet2 = await createTestWallet(server.url, { key: one.key, address: one.address });
  cleanups.push(() => wallet2.close());
  await connected(p2, wallet2);
  await wallet2.disconnect();
  assert.equal(await until(() => p2.events.some((e) => e.name === 'disconnect')), true);
  assert.deepEqual(await p2.conn.provider.request({ method: 'eth_accounts' }), []);
  assert.equal(p2.flag.has(FLAG_KEY), false);
  assert.deepEqual([...wallet.requests, ...wallet2.requests], [], 'no personal_sign or anything else was sent');
});

test('WB-04 reload: no relay before the user acts; login restores the stored session without a new pairing or signature', async () => {
  const server = await relay();
  const { key, address } = newKey();
  const wallet = await createTestWallet(server.url, { key, address });
  cleanups.push(() => wallet.close());
  const storage = new MemoryStorage();
  const flag = new Map();
  const before = page(server, { storage, flag, prefix: 'reload-page' });
  await connected(before, wallet);
  await closeSdk(before.conn.sdkInstance()); // the old page is gone
  const connectionsBefore = server.relay.connections;
  const after = page(server, { storage, flag, prefix: 'reload-page' });
  assert.equal(after.conn.hadSession(), true);
  assert.equal(after.conn.sdkLoaded(), false);
  assert.deepEqual(await after.conn.provider.request({ method: 'eth_accounts' }), [], 'not connected until the user acts');
  await sleep(200);
  assert.equal(after.conn.sdkLoaded(), false, 'eth_accounts never loads the SDK');
  assert.equal(server.relay.connections, connectionsBefore, 'no relay connection before the user acts');
  const restored = await after.conn.provider.request({ method: 'eth_requestAccounts' });
  assert.deepEqual(restored, [address.toLowerCase()]);
  assert.equal(after.statuses.some((s) => s.state === 'pairing'), false, 'no new QR / pairing');
  assert.equal(wallet.proposals.length, 1, 'no second proposal');
  assert.deepEqual(wallet.requests, [], 'restoring signs nothing');
  // The fixed login still needs the user's signature, and the signed bytes are the UTF-8 message.
  const message = fixedMessage(address);
  const signature = await after.conn.provider.request({ method: 'personal_sign', params: [encodePersonalSignMessage(message, ENCODING_HEX), address] });
  assert.equal(recoverAddress(message, signature).address, address.toLowerCase());
  assert.deepEqual(wallet.requests.map((r) => [r.method, r.chainId]), [['personal_sign', 'eip155:56']]);
  assert.deepEqual([...wallet.signedBytes[0]], [...new TextEncoder().encode(message)]);
});

// ---------------------------------------------------------------- WB-06 relay-isolation (SDK level)

test('WB-06 relay down: WalletConnect fails explicitly within the deadline and stays usable for a retry', async () => {
  const server = await relay();
  server.relay.down = true;
  const p = page(server, { timeouts: { initMs: 3000, uriMs: 1500, pairingMs: 10000 } });
  const started = Date.now();
  await assert.rejects(p.conn.provider.request({ method: 'eth_requestAccounts' }), (e) => e instanceof WalletConnectError && e.reason === 'relay_unavailable');
  assert.ok(Date.now() - started < 6000);
  assert.equal(p.conn.state(), 'failed');
  assert.equal(p.statuses.some((s) => s.state === 'pairing'), false);
  assert.deepEqual(await p.conn.provider.request({ method: 'eth_accounts' }), []);
});

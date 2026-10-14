/** WALLET-CONNECT-01 W-A: WA-02 selected-provider, WA-05 switch-and-late-response, WA-06 siwe-bytes.
 * Synthetic providers only. WA-06 signs with an ephemeral in-process test key and verifies with the
 * real Pages Function handler (in-memory D1 stand-in). Not a real-wallet test.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { secp256k1 } from '@noble/curves/secp256k1';
import { harness, deferred, envelope, jsonResponse, challenge, session, tick, SESSION_CSRF, ORIGIN } from './client-review-harness.mjs';
import { addressOf, eip6963Window, makeProvider, realBackend, unauthenticated, uuid, walletSign } from './wallet-fixtures.mjs';
import { checksumAddress, recoverAddress } from '../src/server/siwe.mjs';
import { buildSiweMessage } from '../src/siwe-message.mjs';
import { LOGIN_DOMAIN, LOGIN_URI } from '../src/siwe-gate.mjs';
import { ENCODING_HEX, ENCODING_UTF8, encodePersonalSignMessage, guardedRequest, SIGN_ENCODING } from '../src/wallets/injected.mjs';
import { personalSignBytes, REFUSED_METHODS, walletRequestDecision } from '../src/wallets/request-policy.mjs';

const BINANCE_INFO = { uuid: uuid(11), name: 'Binance Wallet', rdns: 'com.binance.wallet', icon: 'data:image/png;base64,iVBORw0KGgo=' };
const OTHER_INFO = { uuid: uuid(12), name: 'Other Wallet', rdns: 'io.example.wallet', icon: 'data:image/png;base64,iVBORw0KGgo=' };
const OTHER_SESSION_CSRF = 'd'.repeat(64);

async function waitFor(predicate) {
  for (let i = 0; i < 800 && !predicate(); i += 1) await tick();
  return predicate();
}

function twoWallets(extra = {}) {
  const binance = extra.binance ?? makeProvider('binance');
  const other = extra.other ?? makeProvider('other');
  // "other" also overwrote window.ethereum, like a second extension would.
  const env = eip6963Window({ wallets: [{ provider: other, info: OTHER_INFO }, { provider: binance, info: BINANCE_INFO }], ethereum: other });
  return { binance, other, env };
}

const byLabel = (h, label) => h.app.walletCandidates().find((c) => c.provider.label === label);

// ---------------------------------------------------------------- WA-02 selected-provider

test('WA-02 with Binance and another extension, every wallet operation goes only to the selected provider', async () => {
  const { binance, other, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.login();
  assert.deepEqual(binance.calls, [], 'no request before a choice');
  assert.deepEqual(other.calls, []);
  await h.app.selectWallet(byLabel(h, 'binance'));
  assert.deepEqual(binance.calls, [], 'selecting asks for nothing');
  await h.app.login();
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
  assert.equal(binance.calls[0].method, 'eth_requestAccounts', 'selection happens before eth_requestAccounts');
  assert.equal(binance.calls.filter((c) => c.method === 'personal_sign').length, 1);
  await waitFor(() => h.elements.get('view').textContent.includes('Lista'));
  await h.app.queryCapability(56, h.app.getState().loadGeneration);
  await h.app.reverifyCurrent();
  assert.equal(binance.calls.some((c) => c.method === 'wallet_getCapabilities'), true);
  assert.deepEqual(other.calls, [], 'the other extension received nothing');
  // Another extension overwrites window.ethereum later: the selection is not silently replaced.
  const intruder = makeProvider('intruder');
  env.window.ethereum = intruder;
  h.windowListeners.get('focus')();
  await h.app.reverifyCurrent();
  assert.equal(h.app.getState().selected.provider, binance);
  assert.deepEqual(intruder.calls, []);
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
  assert.equal(binance.calls.some((c) => /send|signTypedData|^eth_sign$/.test(c.method)), false);
});

test('WA-02 selected provider is also what the picker marks, and only the selected provider is watched', async () => {
  const { binance, other, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.selectWallet(byLabel(h, 'binance'));
  const buttons = h.elements.get('wallet-list').children;
  const marked = buttons.filter((b) => b.dataset.selected === 'true');
  assert.equal(marked.length, 1);
  assert.match(marked[0].textContent, /Binance Wallet/);
  assert.equal(marked[0].attributes['aria-pressed'], 'true');
  assert.equal(binance.listeners.has('accountsChanged'), true);
  assert.equal(other.listeners.size, 0);
  // Clicking the picker button goes through the same path.
  buttons.find((b) => b.textContent.includes('Other Wallet')).listeners.get('click')();
  await waitFor(() => h.app.getState().selected?.provider === other);
  assert.equal(h.app.getState().selected.provider, other);
  assert.equal(binance.listeners.size, 0, 'old provider listeners removed');
});

// ---------------------------------------------------------------- WA-05 switch-and-late-response

test('WA-05 switch while the old wallet is still asking: the late signature is discarded, no verify, new wallet works', async () => {
  const gate = deferred();
  const binance = makeProvider('binance');
  const other = makeProvider('other', { onRequest: (args) => args.method === 'personal_sign' ? gate.promise : undefined });
  const { env } = twoWallets({ binance, other });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.selectWallet(byLabel(h, 'other'));
  const first = h.app.login();
  assert.equal(await waitFor(() => other.calls.some((c) => c.method === 'personal_sign')), true);
  const epoch = h.app.getState().connectionEpoch;
  await h.app.selectWallet(byLabel(h, 'binance'));
  assert.equal(h.app.getState().connectionEpoch > epoch, true);
  await first; // abandoned immediately, does not wait for the old wallet
  assert.equal(h.app.getState().authBusy, false);
  gate.resolve('late-signature-from-old-wallet');
  for (let i = 0; i < 20; i += 1) await tick();
  assert.equal(h.calls.some((c) => c.path === '/api/auth/verify'), false, 'late signature never verified');
  assert.equal(h.app.getState().csrfToken, null);
  await h.app.login();
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
  assert.equal(h.calls.filter((c) => c.path === '/api/auth/verify').length, 1);
  assert.equal(binance.calls.filter((c) => c.method === 'personal_sign').length, 1);
  assert.equal(other.calls.filter((c) => c.method === 'personal_sign').length, 1);
});

test('WA-05 a switch keeps this tab revoked=true and logs the peer tab out', async () => {
  const { env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, tabGroup: 'switch-tabs', initialSession: unauthenticated() });
  const peer = await harness({ tabGroup: 'switch-tabs', initialSession: unauthenticated() });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  await peer.app.login();
  assert.equal(peer.app.getState().loginAddress !== null, true);
  await h.app.selectWallet(byLabel(h, 'binance'));
  await tick();
  assert.equal(h.calls.filter((c) => c.path === '/api/auth/logout').length, 1);
  assert.equal(h.app.getState().csrfToken, null);
  assert.match(h.elements.get('banner').textContent, /revoked=true/);
  assert.equal(h.elements.get('banner').textContent.includes('其他分頁已登出'), false);
  assert.equal(peer.app.getState().loginAddress, null);
  assert.equal(peer.app.getState().csrfToken, null);
  assert.match(peer.elements.get('banner').textContent, /其他分頁已登出/);
});

test('WA-05 switching providers with a live session revokes it on the server, with evidence', async () => {
  const { binance, other, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  await waitFor(() => h.elements.get('view').textContent.includes('Lista'));
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
  await h.app.selectWallet(byLabel(h, 'binance'));
  const logout = h.calls.filter((c) => c.path === '/api/auth/logout');
  assert.equal(logout.length, 1);
  assert.equal(logout[0].headers.get('x-csrf-token'), SESSION_CSRF);
  assert.deepEqual({ ...h.app.getState().lastRevocation }, { reason: 'provider_switch', confirmed: true, status: 200 });
  assert.equal(h.app.getState().csrfToken, null);
  assert.equal(h.app.getState().revokePending, false);
  assert.equal(h.app.getState().lastPlan, null);
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  assert.match(h.elements.get('banner').textContent, /revoked=true/);
  assert.deepEqual(binance.calls, [], 'switching does not request accounts');
  void other;
});

test('WA-05 unconfirmed revocation blocks the new login until the old session is revoked', async () => {
  let failLogout = true;
  const { binance, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, onFetch: (path) => path === '/api/auth/logout' && failLogout
    ? Promise.reject(new TypeError('synthetic network drop')) : undefined });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  await h.app.selectWallet(byLabel(h, 'binance'));
  assert.match(h.elements.get('banner').textContent, /未確認/);
  assert.equal(h.app.getState().revokePending, true);
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF, 'CSRF kept so revocation can be retried');
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  await h.app.login();
  assert.deepEqual(binance.calls, [], 'no account request while the old session may still be live');
  failLogout = false;
  await h.app.login();
  assert.equal(h.app.getState().revokePending, false);
  assert.equal(binance.calls[0].method, 'eth_requestAccounts');
  const order = h.calls.map((c) => c.path);
  assert.equal(order.lastIndexOf('/api/auth/logout') < order.lastIndexOf('/api/auth/challenge'), true);
});

test('WA-05 a verify answer that lands after a switch is not applied and its session is revoked', async () => {
  const verifyGate = deferred();
  const { binance, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated(),
    onFetch: (path) => path === '/api/auth/verify' ? verifyGate.promise : undefined });
  await h.app.selectWallet(byLabel(h, 'other'));
  const first = h.app.login();
  assert.equal(await waitFor(() => h.calls.some((c) => c.path === '/api/auth/verify')), true);
  await h.app.selectWallet(byLabel(h, 'binance'));
  verifyGate.resolve(jsonResponse({ ...session(), csrf_token: OTHER_SESSION_CSRF }));
  await first;
  assert.equal(h.app.getState().csrfToken, null, 'late verify did not restore the old identity');
  assert.equal(h.app.getState().sessionAddress, null);
  const logout = h.calls.filter((c) => c.path === '/api/auth/logout');
  assert.equal(logout.length, 1);
  assert.equal(logout[0].headers.get('x-csrf-token'), OTHER_SESSION_CSRF);
  assert.deepEqual({ ...h.app.getState().lastRevocation }, { reason: 'late_verify', confirmed: true, status: 200 });
  assert.equal(h.calls.some((c) => c.path.startsWith('/api/tasks/')), false);
  assert.deepEqual(binance.calls, []);
});

test('WA-05 a challenge that lands after a switch is never offered for signing', async () => {
  const challengeGate = deferred();
  const { binance, other, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated(),
    onFetch: (path) => path === '/api/auth/challenge' ? challengeGate.promise : undefined });
  await h.app.selectWallet(byLabel(h, 'other'));
  const first = h.app.login();
  assert.equal(await waitFor(() => h.calls.some((c) => c.path === '/api/auth/challenge')), true);
  await h.app.selectWallet(byLabel(h, 'binance'));
  challengeGate.resolve(jsonResponse(challenge()));
  await first;
  assert.equal(other.calls.some((c) => c.method === 'personal_sign'), false);
  assert.equal(binance.calls.length, 0);
  assert.equal(h.calls.some((c) => c.path === '/api/auth/verify'), false);
});

test('WA-05 a task response that lands after a switch cannot redraw the old private view', async () => {
  const taskGate = deferred();
  let gateTasks = false;
  const { env } = twoWallets();
  const h = await harness({ setupWindow: env.setup,
    onFetch: (path) => path.startsWith('/api/tasks/') && gateTasks ? taskGate.promise : undefined });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  await waitFor(() => h.elements.get('view').textContent.includes('Lista'));
  gateTasks = true;
  const reload = h.app.openPrivateTask(h.app.getState().privateRef);
  await waitFor(() => h.calls.filter((c) => c.path.startsWith('/api/tasks/')).length >= 3);
  await h.app.selectWallet(byLabel(h, 'binance'));
  taskGate.resolve(jsonResponse(await envelope()));
  await reload;
  for (let i = 0; i < 20; i += 1) await tick();
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  assert.equal(h.app.getState().lastPlan, null);
  assert.equal(h.app.getState().csrfToken, null);
});

test('WA-05 a new announcement alone is not a switch and does not log out', async () => {
  const binance = makeProvider('binance');
  const env = eip6963Window({ wallets: [{ provider: binance, info: BINANCE_INFO }] });
  const h = await harness({ setupWindow: env.setup });
  await h.app.login();
  await waitFor(() => h.elements.get('view').textContent.includes('Lista'));
  const before = h.app.getState();
  env.announce(makeProvider('newcomer'), { uuid: uuid(13), name: 'Newcomer', rdns: 'io.newcomer.wallet' });
  h.windowListeners.get('focus')();
  for (let i = 0; i < 10; i += 1) await tick();
  const after = h.app.getState();
  assert.equal(h.app.walletCandidates().length, 2);
  assert.equal(after.csrfToken, SESSION_CSRF);
  assert.equal(after.connectionEpoch, before.connectionEpoch);
  assert.equal(after.selected.provider, binance);
  assert.equal(h.calls.some((c) => c.path === '/api/auth/logout'), false);
  assert.equal(h.elements.get('view').textContent.includes('Lista'), true);
});

test('WA-05 events from the old provider after a switch are ignored', async () => {
  const other = makeProvider('other', { removable: false });
  const { binance, env } = twoWallets({ other });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.selectWallet(byLabel(h, 'other'));
  const stale = other.listeners.get('accountsChanged');
  const staleDisconnect = other.listeners.get('disconnect');
  await h.app.selectWallet(byLabel(h, 'binance'));
  const banner = h.elements.get('banner').textContent;
  const epoch = h.app.getState().connectionEpoch;
  stale(['0x' + '9'.repeat(40)]);
  staleDisconnect({ code: 4900 });
  assert.equal(h.elements.get('banner').textContent, banner);
  assert.equal(h.app.getState().connectionEpoch, epoch);
  assert.equal(h.app.getState().reread.reasons.includes('provider_disconnected'), false);
  void binance;
});

test('WA-05 logging in with a different account than the live session revokes the old session first', async () => {
  const other = '0x' + '2'.repeat(40);
  const binance = makeProvider('binance', { address: other });
  const env = eip6963Window({ wallets: [{ provider: binance, info: BINANCE_INFO }] });
  const h = await harness({ setupWindow: env.setup, onFetch: (path) => {
    if (path === '/api/auth/challenge') return jsonResponse(challenge({ address: other }));
    if (path === '/api/auth/verify') return jsonResponse({ ...session(), address: other });
    return undefined;
  } });
  await waitFor(() => h.elements.get('view').textContent.includes('Lista'));
  await h.app.login();
  const order = h.calls.map((c) => c.path);
  assert.equal(order.includes('/api/auth/logout'), true);
  assert.equal(order.indexOf('/api/auth/logout') < order.indexOf('/api/auth/challenge'), true);
  assert.deepEqual({ ...h.app.getState().lastRevocation }, { reason: 'account_switch', confirmed: true, status: 200 });
});

// ---------------------------------------------------------------- WA-06 siwe-bytes

function fixedMessage(address, nowMs = Date.now()) {
  const issued = new Date(Math.floor(nowMs / 1000) * 1000);
  return buildSiweMessage({
    domain: LOGIN_DOMAIN, address: checksumAddress(address), uri: LOGIN_URI, chainId: 56,
    nonce: 'e'.repeat(32), issuedAt: issued.toISOString(), expirationTime: new Date(issued.getTime() + 300_000).toISOString(),
  });
}

test('WA-06 utf8 and hex transport carry identical bytes, and both recover the signer on the backend', () => {
  const key = secp256k1.utils.randomPrivateKey();
  const address = addressOf(key);
  const message = fixedMessage(address);
  const utf8 = encodePersonalSignMessage(message, ENCODING_UTF8);
  const hexed = encodePersonalSignMessage(message, ENCODING_HEX);
  assert.equal(utf8, message);
  assert.equal(hexed, '0x' + Buffer.from(message, 'utf8').toString('hex'));
  assert.deepEqual(personalSignBytes(utf8), new TextEncoder().encode(message));
  assert.deepEqual(personalSignBytes(hexed), new TextEncoder().encode(message));
  for (const payload of [utf8, hexed]) {
    assert.equal(recoverAddress(message, walletSign(payload, key)).address, address.toLowerCase());
  }
  // A prefix or newline change is different bytes, and recovers a different address.
  for (const tampered of ['\n' + message, message.replace(/\n/g, '\r\n'), `\u0019Ethereum Signed Message:\n${message.length}${message}`]) {
    assert.notEqual(recoverAddress(message, walletSign(tampered, key)).address, address.toLowerCase());
  }
  // Injected sources stay utf8 (hex needs real-device evidence). W-B: WalletConnect uses hex because the
  // WalletConnect/Reown Ethereum JSON-RPC reference defines the personal_sign message as DATA (hex).
  assert.deepEqual({ ...SIGN_ENCODING }, {
    eip6963: ENCODING_UTF8, binancew3w: ENCODING_UTF8, 'window.ethereum': ENCODING_UTF8, walletconnect: ENCODING_HEX,
  });
});

test('WA-06 the policy only lets the fixed SIWE bytes through, to the address being logged in', () => {
  const key = secp256k1.utils.randomPrivateKey();
  const address = checksumAddress(addressOf(key));
  const message = fixedMessage(address);
  const ctx = { userAction: true, approvedMessage: message, address };
  assert.equal(walletRequestDecision('personal_sign', [message, address], ctx).allowed, true);
  assert.equal(walletRequestDecision('personal_sign', [encodePersonalSignMessage(message, ENCODING_HEX), address], ctx).allowed, true);
  assert.equal(walletRequestDecision('personal_sign', [message + '\n', address], ctx).reason, 'bytes_differ');
  assert.equal(walletRequestDecision('personal_sign', [message, '0x' + '3'.repeat(40)], ctx).reason, 'address');
  assert.equal(walletRequestDecision('personal_sign', [message, address], { ...ctx, userAction: false }).allowed, false);
  const arbitrary = 'Please sign to claim your airdrop';
  assert.equal(walletRequestDecision('personal_sign', [arbitrary, address], { ...ctx, approvedMessage: arbitrary }).reason, 'not_fixed_login');
  const foreign = message.replace(LOGIN_DOMAIN, 'attacker.invalid');
  assert.equal(walletRequestDecision('personal_sign', [foreign, address], { ...ctx, approvedMessage: foreign }).allowed, false);
  for (const method of [...REFUSED_METHODS, 'eth_coinbase', 'wallet_watchAsset', 'anything_else']) {
    assert.equal(walletRequestDecision(method, [], ctx).allowed, false, method);
  }
  assert.equal(walletRequestDecision('eth_requestAccounts', undefined, { userAction: true }).reason, 'not_selected');
  assert.equal(walletRequestDecision('eth_requestAccounts', undefined, { selected: true }).reason, 'needs_user_action');
  assert.equal(walletRequestDecision('wallet_getCapabilities', undefined, {}).allowed, false);
  assert.equal(walletRequestDecision('eth_accounts', ['x'], {}).allowed, false);
});

test('WA-06 a refused request never reaches the provider', async () => {
  const provider = makeProvider('p');
  for (const method of ['eth_sign', 'eth_sendTransaction', 'eth_signTypedData_v4', 'wallet_sendCalls', 'personal_sign']) {
    await assert.rejects(guardedRequest({ provider }, method, ['0x00', '0x' + '1'.repeat(40)], {}), /refused by policy/);
  }
  assert.deepEqual(provider.calls, []);
});

test('WA-06 a refused login signature is final: one personal_sign, no other signing method, no verify', async () => {
  const binance = makeProvider('binance', { onRequest: (args) => {
    if (args.method === 'personal_sign') { const e = new Error('User rejected the request.'); e.code = 4001; throw e; }
    return undefined;
  } });
  const env = eip6963Window({ binance });
  const h = await harness({ setupWindow: env.setup, initialSession: unauthenticated() });
  await h.app.login();
  assert.equal(binance.calls.filter((c) => c.method === 'personal_sign').length, 1);
  assert.equal(binance.calls.some((c) => /^eth_sign$|signTypedData|signTransaction|send/i.test(c.method)), false);
  assert.equal(h.calls.some((c) => c.path === '/api/auth/verify'), false);
  assert.equal(h.app.getState().csrfToken, null);
  assert.match(h.elements.get('banner').textContent, /登入未完成/);
});

for (const encoding of [ENCODING_UTF8, ENCODING_HEX]) {
  test(`WA-06 ${encoding} adapter: the page signs the fixed SIWE bytes and the real backend verifies the session`, async () => {
    const key = secp256k1.utils.randomPrivateKey();
    const address = checksumAddress(addressOf(key));
    const backend = await realBackend();
    const seenPayloads = [];
    const wallet = makeProvider('binance', { address, onRequest: (args) => {
      if (args.method !== 'personal_sign') return undefined;
      seenPayloads.push(args.params[0]);
      return walletSign(args.params[0], key);
    } });
    const env = eip6963Window({ wallets: [{ provider: wallet, info: BINANCE_INFO }] });
    const h = await harness({
      setupWindow: env.setup,
      onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined),
      importOverrides: encoding === ENCODING_HEX
        ? { './wallets/injected.mjs': (mod) => ({ ...mod, signEncodingFor: () => ENCODING_HEX }) }
        : {},
    });
    await h.app.login();
    assert.equal(seenPayloads.length, 1);
    if (encoding === ENCODING_HEX) assert.match(seenPayloads[0], /^0x[0-9a-f]+$/);
    else assert.match(seenPayloads[0], /wants you to sign in with your Ethereum account:/);
    const verify = backend.seen.find((r) => r.path === '/api/auth/verify');
    assert.equal(verify.status, 200, 'backend recovered the same address from the signed bytes');
    assert.equal(h.app.getState().sessionAddress.toLowerCase(), address.toLowerCase());
    const row = await backend.db().prepare('SELECT address, revoked_at FROM sessions').first();
    assert.equal(row.address, address.toLowerCase());
    assert.equal(row.revoked_at, null);
    await h.app.logout();
    const revoked = await backend.db().prepare('SELECT revoked_at FROM sessions').first();
    assert.notEqual(revoked.revoked_at, null);
    assert.equal(wallet.calls.some((c) => /^eth_sign$|signTypedData|send/i.test(c.method)), false);
    void ORIGIN;
  });
}

test('WA-05/06 switch evidence against the real backend: the old D1 session row is revoked', async () => {
  const keyA = secp256k1.utils.randomPrivateKey();
  const keyB = secp256k1.utils.randomPrivateKey();
  const backend = await realBackend();
  const signer = (key) => (args) => args.method === 'personal_sign' ? walletSign(args.params[0], key) : undefined;
  const a = makeProvider('other', { address: checksumAddress(addressOf(keyA)), onRequest: signer(keyA) });
  const b = makeProvider('binance', { address: checksumAddress(addressOf(keyB)), onRequest: signer(keyB) });
  const env = eip6963Window({ wallets: [{ provider: a, info: OTHER_INFO }, { provider: b, info: BINANCE_INFO }] });
  const h = await harness({ setupWindow: env.setup,
    onFetch: (path, init) => (String(path).startsWith('/api/') ? backend.fetchApi(path, init) : undefined) });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  assert.equal(h.app.getState().sessionAddress.toLowerCase(), addressOf(keyA));
  await h.app.selectWallet(byLabel(h, 'binance'));
  const rowA = await backend.db().prepare('SELECT revoked_at FROM sessions WHERE address = ?').bind(addressOf(keyA)).first();
  assert.notEqual(rowA.revoked_at, null, 'old session revoked in D1');
  assert.equal(backend.jar.has('__Host-wg_session'), false, 'old cookie cleared');
  await h.app.login();
  assert.equal(h.app.getState().sessionAddress.toLowerCase(), addressOf(keyB));
  const sessionCheck = await backend.fetchApi('/api/session');
  assert.equal((await sessionCheck.json()).address.toLowerCase(), addressOf(keyB));
});

test('WA-05 a switch during logout never revokes the next new session afterwards', async () => {
  const logoutGate = deferred();
  let gateLogout = true;
  const { binance, env } = twoWallets();
  const h = await harness({ setupWindow: env.setup,
    onFetch: (path) => path === '/api/auth/logout' && gateLogout ? logoutGate.promise : undefined });
  await h.app.selectWallet(byLabel(h, 'other'));
  await h.app.login();
  const leaving = h.app.logout();
  await h.app.selectWallet(byLabel(h, 'binance'));
  gateLogout = false;
  logoutGate.resolve(jsonResponse({ authenticated: false, revoked: true }));
  await leaving;
  assert.equal(h.app.getState().csrfToken, null);
  await h.app.login();
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF, 'new session kept');
  assert.equal(h.calls.filter((c) => c.path === '/api/auth/logout').length, 1, 'no extra revocation after the new login');
  assert.equal(binance.calls.filter((c) => c.method === 'personal_sign').length, 1);
});

import assert from 'node:assert/strict';
import test from 'node:test';
import { harness, deferred, envelope, jsonResponse, challenge, REF, SESSION_CSRF, tick } from './client-review-harness.mjs';

async function waitForPreview(h) {
  for (let i=0; i<500 && !h.elements.get('view').textContent.includes('Lista'); i++) await tick();
  assert.match(h.elements.get('view').textContent, /Lista/);
}

test('logout invalidates a task response already in flight', async () => {
  const gate = deferred();
  const h = await harness({ onFetch: path => path.startsWith('/api/tasks/') ? gate.promise : undefined });
  await h.app.logout();
  gate.resolve(jsonResponse(await envelope()));
  for(let i=0;i<30;i++) await tick();
  assert.equal(h.app.getState().csrfToken, null);
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  assert.equal(h.app.getState().lastPlan, null);
  assert.equal(h.elements.get('cancel-task').hidden, true);
});

test('logout prevents a pending capability lookup from resurrecting the old task', async () => {
  const gate = deferred();
  const h = await harness({onWallet: args => args.method === 'wallet_getCapabilities' ? gate.promise : undefined});
  await waitForPreview(h);
  const pending = h.app.queryCapability(56, h.app.getState().loadGeneration);
  await h.app.logout();
  gate.resolve({});
  await pending;
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  assert.equal(h.app.getState().lastPlan, null);
});

test('cancellation prevents a pending capability lookup from restoring a cancelled task', async () => {
  const gate = deferred();
  const h = await harness({onWallet: args => args.method === 'wallet_getCapabilities' ? gate.promise : undefined});
  await waitForPreview(h);
  const pending = h.app.queryCapability(56, h.app.getState().loadGeneration);
  await h.app.cancelPrivateTask();
  gate.resolve({});
  await pending;
  assert.equal(h.elements.get('view').textContent.includes('已取消'), true);
  assert.equal(h.app.getState().lastPlan, null);
});

test('refused login must not replace a valid session CSRF with the challenge token', async () => {
  const h = await harness({onWallet: args => {
    if(args.method === 'personal_sign') throw new Error('User rejected synthetic login');
  }});
  await waitForPreview(h);
  await h.app.login();
  await h.app.logout();
  const request = h.calls.find(x => x.path === '/api/auth/logout');
  assert.equal(request.headers.get('x-csrf-token'), SESSION_CSRF);
});

test('a different-domain login challenge is never offered to personal_sign', async () => {
  const h = await harness({onFetch:path => path === '/api/auth/challenge'
    ? jsonResponse(challenge({domain:'attacker.invalid',uri:'https://attacker.invalid'})) : undefined});
  await h.app.login();
  assert.equal(h.walletCalls.filter(x=>x.method === 'personal_sign').length, 0);
});

test('additional SIWE resources are rejected before a wallet signature request', async () => {
  const body = challenge();
  body.message += '\nResources:\n- https://attacker.invalid/permission';
  const h = await harness({onFetch:path => path === '/api/auth/challenge' ? jsonResponse(body) : undefined});
  await h.app.login();
  assert.equal(h.walletCalls.filter(x=>x.method === 'personal_sign').length, 0);
});

test('a valid fixed login is still offered exactly once, without a transaction', async () => {
  const h = await harness();
  await h.app.login();
  assert.equal(h.walletCalls.filter(x=>x.method === 'personal_sign').length, 1);
  assert.equal(h.walletCalls.some(x=>/send|signTypedData/i.test(x.method)), false);
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
});

test('verify uses challenge CSRF, and successful login restores session CSRF', async () => {
  const h = await harness();
  await h.app.login();
  const request = h.calls.find(x => x.path === '/api/auth/verify');
  assert.equal(request.headers.get('x-csrf-token'), 'b'.repeat(64));
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
});

test('late initial session restore cannot reauthenticate after logout', async () => {
  const gate = deferred();
  const h = await harness({onFetch:path => path === '/api/session' ? gate.promise : undefined});
  await h.app.logout();
  gate.resolve(jsonResponse({authenticated:true,address:'0x'+'1'.repeat(40),csrf_token:SESSION_CSRF,
    execution_authorized:false,send_authorized:false}));
  for(let i=0;i<20;i++) await tick();
  assert.equal(h.app.getState().csrfToken, null);
  assert.equal(h.calls.some(x=>x.path.startsWith('/api/tasks/')), false);
});

test('double login is serialized and cannot install a verify response after a concurrent logout', async () => {
  const gate = deferred();
  const h = await harness({onWallet:args => args.method === 'personal_sign' ? gate.promise : undefined});
  const first = h.app.login();
  for(let i=0;i<500 && !h.walletCalls.some(x=>x.method === 'personal_sign');i++) await tick();
  await h.app.login();
  await h.app.logout(); // Mutating auth is deliberately serialized while login is active.
  assert.equal(h.walletCalls.filter(x=>x.method === 'personal_sign').length, 1);
  assert.equal(h.calls.some(x=>x.path === '/api/auth/logout'), false);
  gate.resolve('synthetic-login-signature');
  await first;
  await h.app.logout();
  assert.equal(h.app.getState().csrfToken, null);
});

test('wallet account changes before signing invalidate the challenge', async () => {
  const h = await harness({onWallet:args => args.method === 'eth_accounts' ? ['0x'+'2'.repeat(40)] : undefined});
  await h.app.login();
  assert.equal(h.walletCalls.some(x=>x.method === 'personal_sign'), false);
});

test('wallet chain changes before signing invalidate the challenge', async () => {
  let chainReads=0;
  const h = await harness({onWallet:args => args.method === 'eth_chainId' ? (++chainReads === 1 ? '0x38' : '0x1') : undefined});
  await h.app.login();
  assert.equal(h.walletCalls.some(x=>x.method === 'personal_sign'), false);
});

test('unconfirmed logout clears private display but retains the ability to retry revocation', async () => {
  const h = await harness({onFetch:path => path === '/api/auth/logout' ? Promise.reject(new Error('synthetic network timeout')) : undefined});
  await waitForPreview(h);
  await h.app.logout();
  assert.equal(h.elements.get('view').textContent.includes('Lista'), false);
  assert.equal(h.app.getState().lastPlan, null);
  assert.equal(h.app.getState().csrfToken, SESSION_CSRF);
  assert.equal(h.elements.get('banner').textContent.includes('未確認'), true);
});

test('late cancellation reply cannot overwrite a view cleared by the user', async () => {
  const gate=deferred();
  const h=await harness({onFetch:path=>path.endsWith('/cancel')?gate.promise:undefined});
  await waitForPreview(h);
  const pending=h.app.cancelPrivateTask();
  h.elements.get('clear').listeners.get('click')();
  gate.resolve(jsonResponse({status:'cancelled',execution_authorized:false,send_authorized:false,
    tx_hash_verified:false,on_chain_revoked:false}));
  await pending;
  assert.equal(h.elements.get('view').textContent.includes('已取消'), false);
  assert.equal(h.app.getState().privateRef, null);
});

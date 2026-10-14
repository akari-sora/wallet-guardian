import assert from 'node:assert/strict';
import test from 'node:test';
import { ADDRESS, harness, jsonResponse, session } from './client-review-harness.mjs';

test('successful login remembers only the address that was signed and verified', async () => {
  const stored = [];
  const h = await harness({
    initialSession: { authenticated: false, erc1271: 'unsupported', execution_authorized: false },
    setupWindow(window) {
      window.localStorage = {
        getItem() { return null; },
        setItem(key) { stored.push(key); },
      };
      window.sessionStorage = {
        getItem() { return null; },
        setItem(key) { stored.push(key); },
      };
    },
  });
  assert.equal(h.app.getState().loginAddress, null);
  await h.app.login();
  assert.equal(h.app.getState().loginAddress, ADDRESS);
  assert.equal(h.app.getState().sessionAddress, ADDRESS);
  const challenge = h.calls.find((call) => call.path === '/api/auth/challenge');
  const verify = h.calls.find((call) => call.path === '/api/auth/verify');
  const challengeBody = JSON.parse(challenge.init.body);
  const verifyBody = JSON.parse(verify.init.body);
  assert.deepEqual(Object.keys(challengeBody).sort(), ['address', 'chainId']);
  assert.equal(challengeBody.chainId, 56);
  assert.equal(challengeBody.address, ADDRESS);
  assert.deepEqual(Object.keys(verifyBody).sort(), ['message', 'signature']);
  assert.equal(challengeBody.rdns, undefined);
  assert.equal(challengeBody.name, undefined);
  assert.equal(verifyBody.rdns, undefined);
  assert.equal(verifyBody.name, undefined);
  assert.deepEqual(stored, []);
  await h.app.logout();
  assert.equal(h.app.getState().loginAddress, null);
  assert.equal(h.app.getState().csrfToken, null);
});

test('a verify address that does not match the signed address is not remembered', async () => {
  const other = `0x${'2'.repeat(40)}`;
  const h = await harness({
    initialSession: { authenticated: false, erc1271: 'unsupported', execution_authorized: false },
    onFetch: (path) => path === '/api/auth/verify' ? jsonResponse({ ...session(), address: other }) : undefined,
  });
  await h.app.login();
  assert.equal(h.app.getState().loginAddress, null);
  assert.equal(h.app.getState().sessionAddress, null);
  assert.equal(h.calls.some((call) => call.path === '/api/auth/logout'), true);
});

test('logout in one tab clears the other tab login address', async () => {
  const group = 'tabs-a';
  const options = {
    tabGroup: group,
    initialSession: { authenticated: false, erc1271: 'unsupported', execution_authorized: false },
  };
  const left = await harness(options);
  const right = await harness(options);
  await left.app.login();
  await right.app.login();
  assert.equal(left.app.getState().loginAddress, ADDRESS);
  assert.equal(right.app.getState().loginAddress, ADDRESS);
  await left.app.logout();
  assert.equal(left.app.getState().loginAddress, null);
  assert.equal(right.app.getState().loginAddress, null);
  assert.equal(right.app.getState().csrfToken, null);
  assert.equal(right.app.getState().sessionAddress, null);
});

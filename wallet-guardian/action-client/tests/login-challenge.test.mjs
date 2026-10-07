import assert from 'node:assert/strict';
import test from 'node:test';
import { validateLoginChallenge } from '../src/login-challenge.mjs';
import { ADDRESS, ORIGIN, challenge } from './client-review-harness.mjs';
const context = () => ({origin:ORIGIN,address:ADDRESS,chainId:56});

test('fixed P1 challenge with or without root slash is accepted', () => {
  for(const uri of [ORIGIN,ORIGIN+'/']) assert.doesNotThrow(()=>validateLoginChallenge(challenge({uri}),context()));
});
for(const [label, mutate] of [
  ['different origin', b=>{b.domain='other.invalid';}],
  ['different URI', b=>{b.uri=ORIGIN+'/permission';}],
  ['different account', b=>{b.address='0x'+'2'.repeat(40);}],
  ['different chain', b=>{b.chain_id=1;}],
  ['string chain ID', b=>{b.chain_id='56';}],
  ['expired response', b=>{b.expires_at=1;}],
  ['missing negative permission', b=>{delete b.send_authorized;}],
  ['execution permission', b=>{b.execution_authorized=true;}],
  ['malformed CSRF', b=>{b.csrf_token='short';}],
  ['inconsistent message address', b=>{b.message=b.message.replace(ADDRESS,'0x'+'2'.repeat(40));}],
  ['extra resources', b=>{b.message+='\nResources:\n- https://other.invalid/';}],
  ['appended line', b=>{b.message+='\n';}],
  ['changed statement', b=>{b.message=b.message.replace('read-only session','spending permission');}],
  ['bad nonce', b=>{b.message=b.message.replace('c'.repeat(32),'nonce');}],
]) {
  test(`reject ${label} before any wallet operation`, ()=> {
    const body=challenge(); mutate(body);
    assert.throws(()=>validateLoginChallenge(body,context()));
  });
}
test('expiration and future issuance are checked against the client clock', () => {
  const body=challenge();
  assert.throws(()=>validateLoginChallenge(body,{...context(),nowMs:body.expires_at*1000}));
  assert.throws(()=>validateLoginChallenge(body,{...context(),nowMs:(body.expires_at-1000)*1000}));
});
test('a syntactically valid session for an untrusted origin still fails', () => {
  const body=challenge({domain:'other.invalid',uri:'https://other.invalid'});
  assert.throws(()=>validateLoginChallenge(body,context()));
});

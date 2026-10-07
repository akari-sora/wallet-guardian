import assert from 'node:assert/strict';
import test from 'node:test';
import { readBoundedJson } from '../src/private-task.mjs';

test('oversized private response is cancelled and its reader is released', async () => {
  let cancelled=false;
  const stream=new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode('x'.repeat(100)));},
    cancel(){cancelled=true;}});
  const response=new Response(stream,{headers:{'content-type':'application/json'}});
  await assert.rejects(readBoundedJson(response,10));
  assert.equal(cancelled,true);
  assert.equal(stream.locked,false);
});
test('valid private response releases its reader', async () => {
  const response=new Response('{"ok":true}',{headers:{'content-type':'application/json'}});
  assert.deepEqual(await readBoundedJson(response),{ok:true});
  assert.equal(response.body.locked,false);
});
test('malformed private JSON releases its reader', async () => {
  const response=new Response('{broken',{headers:{'content-type':'application/json'}});
  await assert.rejects(readBoundedJson(response));
  assert.equal(response.body.locked,false);
});
test('read error releases the reader', async () => {
  const stream=new ReadableStream({start(controller){controller.error(new Error('synthetic stream failure'));}});
  const response=new Response(stream,{headers:{'content-type':'application/json'}});
  await assert.rejects(readBoundedJson(response));
  assert.equal(stream.locked,false);
});

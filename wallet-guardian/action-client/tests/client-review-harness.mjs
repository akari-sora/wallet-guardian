/** Executes the actual client entry point with a synthetic DOM/provider/network.
 * Not a browser, real-wallet, Cloudflare, or deployed-artifact E2E test.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';
import ts from 'typescript';

export const ORIGIN = 'https://wallet-guardian-action-client.pages.dev';
export const ADDRESS = '0x' + '1'.repeat(40);
export const REF = 'ref_' + 'ab'.repeat(16);
export const SESSION_CSRF = 'a'.repeat(64);
export const CHALLENGE_CSRF = 'b'.repeat(64);
export const tick = () => new Promise(resolve => setTimeout(resolve, 2));
export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
export const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
});
export function session() {
  return { authenticated: true, address: ADDRESS, csrf_token: SESSION_CSRF,
    execution_authorized: false, send_authorized: false };
}
export function challenge(overrides = {}) {
  const now = Date.now();
  const statement = 'Sign in to Wallet Guardian. This only starts a read-only session. It is not a transaction and does not authorize sending.';
  const body = { address: ADDRESS, domain: new URL(ORIGIN).hostname, uri: ORIGIN,
    chain_id: 56, csrf_token: CHALLENGE_CSRF, erc1271: 'unsupported',
    execution_authorized: false, send_authorized: false, expires_at: Math.floor(now / 1000) + 600,
    ...overrides };
  body.message = [
    `${body.domain} wants you to sign in with your Ethereum account:`, body.address, '', statement, '',
    `URI: ${body.uri}`, 'Version: 1', `Chain ID: ${body.chain_id}`, `Nonce: ${'c'.repeat(32)}`,
    `Issued At: ${new Date(Math.floor(now / 1000) * 1000).toISOString()}`,
    `Expiration Time: ${new Date(body.expires_at * 1000).toISOString()}`,
  ].join('\n');
  return body;
}

class Element {
  constructor() { this.children = []; this.listeners = new Map(); this.hidden = false; this.disabled = false; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent ?? String(x)).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; }
  addEventListener(event, handler) { this.listeners.set(event, handler); }
}

export async function harness({ onFetch, onWallet, initialSession = session(), pathname = `/t/${REF}` } = {}) {
  const sourceRoot = process.env.GUARDIAN_REVIEW_SOURCE_DIR
    ? resolve(process.env.GUARDIAN_REVIEW_SOURCE_DIR)
    : new URL('../src/', import.meta.url).pathname;
  const source = readFileSync(resolve(sourceRoot, 'main.ts'), 'utf8');
  const imports = new Map();
  for (const match of source.matchAll(/from\s+["'](\.\/[^"']+)["']/g)) {
    imports.set(match[1], await import(pathToFileURL(resolve(sourceRoot, match[1])).href));
  }
  const elements = new Map(['view','banner','login','logout','cancel-task','load-sample','file','clear','session','verify','verify-reread','verify-simulation','verify-send','verify-reasons','wallet-outcome','poison-note'].map(x => [x, new Element()]));
  const calls = [], walletCalls = [];
  const providerListeners = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  const document = { querySelector: selector => elements.get(selector.replace(/^#/, '')),
    createElement: () => new Element(),
    addEventListener: (event, handler) => documentListeners.set(event, handler) };
  const window = { location: new URL(pathname, ORIGIN),
    addEventListener: (event, handler) => windowListeners.set(event, handler),
    ethereum: {
    request: async args => {
      walletCalls.push(args);
      if (onWallet) { const handled = onWallet(args); if (handled !== undefined) return handled; }
      if (args.method === 'eth_requestAccounts' || args.method === 'eth_accounts') return [ADDRESS];
      if (args.method === 'eth_chainId') return '0x38';
      if (args.method === 'personal_sign') return 'synthetic-signature-not-for-chain-use';
      if (args.method === 'wallet_getCapabilities') return {};
      throw new Error(`Unexpected provider operation: ${args.method}`);
    },
    on: (event, handler) => providerListeners.set(event, handler),
    removeListener: event => providerListeners.delete(event),
  } };
  const fetch = async (path, init = {}) => {
    calls.push({ path, init, headers: new Headers(init.headers) });
    if (onFetch) { const handled = onFetch(path, init); if (handled !== undefined) return handled; }
    if (path === '/api/session') return jsonResponse(initialSession);
    if (path === '/api/auth/challenge') return jsonResponse(challenge());
    if (path === '/api/auth/verify') return jsonResponse(session());
    if (path === '/api/auth/logout') return jsonResponse({ authenticated: false, revoked: true });
    if (path.endsWith('/cancel')) return jsonResponse({ status:'cancelled', ref:REF, revision:1,
      cancel_allowed:false, execution_authorized:false, send_authorized:false,
      tx_hash_verified:false, on_chain_revoked:false });
    if (path.startsWith('/api/tasks/')) return jsonResponse(await envelope());
    throw new Error(`Unexpected network request: ${path}`);
  };
  const context = vm.createContext({ document, window, fetch, crypto:globalThis.crypto,
    URL, URLSearchParams, Headers, Request, Response, AbortController, AbortSignal,
    TextEncoder, TextDecoder, console, setTimeout, clearTimeout, setInterval, clearInterval,
    exports: {}, require: name => {
      if (!imports.has(name)) throw new Error(`Unexpected import: ${name}`);
      return imports.get(name);
    },
  });
  const append = `\nglobalThis.__review = {
    login, logout, cancelPrivateTask, loadPrivateTask, openPrivateTask, queryCapability, restoreSession, reverifyCurrent,
    getState: () => ({ loadGeneration, lastPlan, lastTask, csrfToken, privateRef, privateRevision, privateCancelAllowed, reread: currentVerification() })
  };`;
  const transpiled = ts.transpileModule(
    source.replace('import.meta.env.VITE_GUARDIAN_MAINNET_SEND', JSON.stringify('true')) + append,
    { compilerOptions: { target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.CommonJS } },
  ).outputText;
  vm.runInContext(transpiled, context, { filename: 'review-actual-main.cjs', timeout:3000 });
  for (let i = 0; i < 5; i++) await tick();
  return { app:context.__review, elements, calls, walletCalls, providerListeners, documentListeners, windowListeners, window };
}

export async function envelope() {
  const { digestBody } = await import('../src/canonical.mjs');
  const plan = {
    protocol_version:'guardian.action_plan.v1', task_id:'synthetic-review-task', revision:1,
    chain_namespace:'evm', chain_id:56, sender:ADDRESS, execution_account:ADDRESS,
    execution_account_type:'eoa', recipient:ADDRESS, protocol_id:'lista-slisbnb',
    deployment:{proxy:'0x1adb950d8bb3da4be104211d5ab038628e477fe6',method:'claimWithdraw(uint256)'},
    intent:{type:'lista_claim_withdraw',uuid:'1',idx:0},
    calls:[{to:'0x1adb950d8bb3da4be104211d5ab038628e477fe6',data:'0xb13acedd'+'0'.repeat(64),value:'0'}],
    asset_in:{raw_amount:'5',decimals:18,symbol:'slisBNB'}, asset_out:{raw_amount:'6',decimals:18,symbol:'BNB'},
    preconditions:{is_claimable:true,paused:false}, bounds:{}, max_fee_wei:null,
    simulation:{status:'INCOMPLETE',effects_checked:false,source:'synthetic-offline'}, wallet_request_allowed:false,
    block_reasons:['Synthetic test, no chain transaction'],
    human_summary:{sender:ADDRESS,recipient:ADDRESS,chain_id:56,chain_label:'BNB Smart Chain (chain ID 56)',
      amount_in_raw:'5',amount_in_unit:'slisBNB',amount_out_raw:'6',amount_out_unit:'BNB',
      fee_label:'費用未知',simulation_status:'INCOMPLETE',wallet_request_allowed:false},
  };
  plan.digest = await digestBody(plan);
  const task = {protocol_version:'guardian.task.v1',client_build:'action-client@0.3.0',ref:REF,
    mode:'A',execution_authorized:false,wallet_request_allowed:false,
    steps:[{id:'execute',label:'Synthetic claim',required:true,separate:true}],link:{issued:false},plan};
  return {ref:REF,revision:1,cancel_allowed:true,execution_authorized:false,send_authorized:false,
    tx_hash_verified:false,on_chain_revoked:false,task};
}

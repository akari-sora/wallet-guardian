/** Synthetic EIP-1193 / EIP-6963 wallets for WALLET-CONNECT-01 W-A tests.
 * Not real wallets. Test keys are generated per test run and never leave the process.
 */
import { readFileSync } from 'node:fs';
import { secp256k1 } from '@noble/curves/secp256k1';
import { keccak_256 } from '@noble/hashes/sha3';
import { ADDRESS, ORIGIN, challenge, jsonResponse, session } from './client-review-harness.mjs';
import { personalSignBytes } from '../src/wallets/request-policy.mjs';

export const SEND_LIKE = /send|sign(?!_in)|Typed|grant|Permission|upgrade|Authorization|switch|addEthereumChain/i;

export function hex(bytes) {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function addressOf(key) {
  return `0x${hex(keccak_256(secp256k1.getPublicKey(key, false).slice(1)).slice(-20))}`;
}

/** EIP-191 personal_sign over the bytes a wallet would take from the payload (hex or UTF-8). */
export function walletSign(payload, key) {
  const body = personalSignBytes(payload);
  const prefix = new TextEncoder().encode(`\u0019Ethereum Signed Message:\n${body.length}`);
  const bytes = new Uint8Array(prefix.length + body.length);
  bytes.set(prefix, 0);
  bytes.set(body, prefix.length);
  const signature = secp256k1.sign(keccak_256(bytes), key);
  const out = new Uint8Array(65);
  out.set(signature.toCompactRawBytes(), 0);
  out[64] = signature.recovery + 27;
  return `0x${hex(out)}`;
}

export function uuid(n) {
  const tail = String(n).padStart(12, '0');
  return `00000000-0000-4000-8000-${tail}`;
}

/**
 * @param {string} label
 * @param {{ address?: string, onRequest?: (args: any) => any, removable?: boolean }} [options]
 */
export function makeProvider(label, { address = ADDRESS, onRequest, removable = true } = {}) {
  const calls = [];
  const listeners = new Map();
  const provider = {
    label,
    calls,
    listeners,
    async request(args) {
      calls.push({ method: args.method, params: args.params });
      if (onRequest) {
        const handled = await onRequest(args);
        if (handled !== undefined) return handled;
      }
      if (args.method === 'eth_requestAccounts' || args.method === 'eth_accounts') return [address];
      if (args.method === 'eth_chainId') return '0x38';
      if (args.method === 'personal_sign') return `synthetic-signature-from-${label}`;
      if (args.method === 'wallet_getCapabilities') return {};
      const error = new Error(`synthetic ${label} refuses ${args.method}`);
      error.code = 4200;
      throw error;
    },
    on(event, handler) { listeners.set(event, handler); },
  };
  if (removable) provider.removeListener = (event) => listeners.delete(event);
  return provider;
}

export function announcement(provider, info) {
  return { detail: Object.freeze({ info: Object.freeze({ ...info }), provider }) };
}

/**
 * Window setup with a real-ish event target for eip6963 events.
 * `wallets`: [{ provider, info, late?: boolean }]. Non-late wallets answer requestProvider.
 */
export function eip6963Window({ wallets = [], ethereum = null, binance = null, keepDefaultEthereum = false, log = [] } = {}) {
  const bus = new Map();
  const api = {
    log,
    bus,
    announce(provider, info) {
      for (const handler of bus.get('eip6963:announceProvider') ?? []) handler(announcement(provider, info));
    },
  };
  api.setup = (window) => {
    if (!keepDefaultEthereum) delete window.ethereum;
    if (ethereum) window.ethereum = ethereum;
    if (binance) window.binancew3w = { ethereum: binance };
    const plain = window.addEventListener;
    window.addEventListener = (event, handler) => {
      if (event.startsWith('eip6963:')) {
        log.push(`listen:${event}`);
        if (!bus.has(event)) bus.set(event, []);
        bus.get(event).push(handler);
        return;
      }
      plain(event, handler);
    };
    window.Event = class SyntheticEvent { constructor(type) { this.type = type; } };
    window.dispatchEvent = (event) => {
      log.push(`dispatch:${event.type}`);
      if (event.type === 'eip6963:requestProvider') {
        for (const wallet of wallets) if (!wallet.late) api.announce(wallet.provider, wallet.info);
      }
      return true;
    };
    api.window = window;
  };
  return api;
}

const schema = readFileSync(new URL('../migrations/0001_p1_init.sql', import.meta.url), 'utf8')
  + '\n' + readFileSync(new URL('../migrations/0002_session_lifecycle.sql', import.meta.url), 'utf8');

/** Routes the harness fetch to the real Pages Function handler with an in-memory D1 stand-in. */
export async function realBackend() {
  const { createPrimaryDatabase } = await import('../src/server/test-db.mjs');
  const { handleApi } = await import('../src/server/handle.mjs');
  const database = createPrimaryDatabase(schema);
  const env = {
    DB: { prepare() { throw new Error('direct d1 prepare'); }, withSession: (mode) => database.withSession(mode) },
    AUTH_DOMAIN: new URL(ORIGIN).host,
    AUTH_URI: ORIGIN,
    AUTH_CHAIN_ID: '56',
  };
  const jar = new Map();
  const seen = [];
  async function fetchApi(path, init = {}) {
    const headers = new Headers(init.headers);
    headers.set('cf-connecting-ip', '203.0.113.77');
    const method = (init.method ?? 'GET').toUpperCase();
    if (method !== 'GET') headers.set('origin', ORIGIN);
    const cookie = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
    if (cookie) headers.set('cookie', cookie);
    const response = await handleApi(new Request(`${ORIGIN}${path}`, { method, headers, body: init.body }), env);
    for (const line of response.headers.getSetCookie()) {
      const [pair, ...attrs] = line.split(';');
      const [name, ...rest] = pair.split('=');
      const value = rest.join('=');
      if (attrs.some((a) => /max-age=0/i.test(a)) || value === '') jar.delete(name.trim());
      else jar.set(name.trim(), value);
    }
    seen.push({ path, method, status: response.status });
    return response;
  }
  return { env, jar, seen, fetchApi, db: () => env.DB.withSession('first-primary') };
}

export function unauthenticated() {
  return { authenticated: false, erc1271: 'unsupported', execution_authorized: false };
}

export { ADDRESS, challenge, jsonResponse, session };

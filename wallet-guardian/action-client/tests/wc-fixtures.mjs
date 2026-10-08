/**
 * WALLET-CONNECT-01 W-B test fixtures. A *test* wallet on the real @walletconnect/sign-client 2.25.0
 * (same version the page uses), talking to the in-process MockRelay. Not a real wallet app.
 * Keys are generated per run and never leave the process.
 */
import { SignClient } from '@walletconnect/sign-client';
import { MemoryStorage } from './wc-relay-mock.mjs';
import { personalSignBytes } from '../src/wallets/request-policy.mjs';
import { walletSign } from './wallet-fixtures.mjs';

export const TEST_PROJECT_ID = 'a'.repeat(32);
let seq = 0;

export function ns56(address, { methods = ['personal_sign'], extraAccounts = [], chains } = {}) {
  const accounts = [`eip155:56:${address}`, ...extraAccounts];
  return {
    eip155: {
      accounts,
      methods,
      events: ['accountsChanged', 'chainChanged'],
      chains: chains ?? [...new Set(accounts.map((a) => a.split(':').slice(0, 2).join(':')))],
    },
  };
}

/**
 * @param {string} relayUrl
 * @param {{ key?: Uint8Array, address?: string, namespaces?: (address: string) => any, reject?: boolean, autoApprove?: boolean }} options
 */
export async function createTestWallet(relayUrl, { key, address, namespaces, reject = false, autoApprove = true } = {}) {
  const client = await SignClient.init({
    projectId: TEST_PROJECT_ID,
    relayUrl,
    storage: new MemoryStorage(),
    customStoragePrefix: `test-wallet-${++seq}`,
    logger: 'silent',
    telemetryEnabled: false,
    metadata: { name: 'Test Wallet (synthetic)', description: 'test only', url: 'https://wallet.invalid', icons: [] },
  });
  const wallet = {
    client,
    proposals: [],
    requests: [],
    sessions: [],
    signedBytes: [],
    pendingProposal: null,
    async approve(id) {
      const namespacesFor = namespaces ?? ((a) => ns56(a));
      const { topic, acknowledged } = await client.approve({ id, namespaces: namespacesFor(address) });
      await acknowledged();
      wallet.sessions.push(topic);
      return topic;
    },
    async rejectProposal(id) {
      await client.reject({ id, reason: { code: 5000, message: 'User rejected.' } });
    },
    async pair(uri) {
      await client.pair({ uri });
    },
    topic() {
      return wallet.sessions[wallet.sessions.length - 1];
    },
    async update(nextNamespaces) {
      const { acknowledged } = await client.update({ topic: wallet.topic(), namespaces: nextNamespaces });
      await acknowledged();
    },
    async emit(name, data, chainId = 'eip155:56') {
      await client.emit({ topic: wallet.topic(), event: { name, data }, chainId });
    },
    async disconnect() {
      await client.disconnect({ topic: wallet.topic(), reason: { code: 6000, message: 'User disconnected.' } });
    },
    async close() {
      try { await client.core.relayer.transportClose(); } catch { /* already closed */ }
      try { client.core.heartbeat.stop(); } catch { /* optional */ }
    },
  };
  client.on('session_proposal', async (event) => {
    wallet.proposals.push(event.params);
    if (reject) return wallet.rejectProposal(event.id);
    if (!autoApprove) { wallet.pendingProposal = event; return undefined; }
    return wallet.approve(event.id);
  });
  client.on('session_request', async (event) => {
    const { request } = event.params;
    wallet.requests.push({ method: request.method, params: request.params, chainId: event.params.chainId });
    if (request.method === 'personal_sign' && key) {
      const [payload] = request.params;
      wallet.signedBytes.push(personalSignBytes(payload));
      await client.respond({ topic: event.topic, response: { id: event.id, jsonrpc: '2.0', result: walletSign(payload, key) } });
      return;
    }
    await client.respond({ topic: event.topic, response: { id: event.id, jsonrpc: '2.0', error: { code: 4001, message: 'test wallet refuses' } } });
  });
  return wallet;
}

/** Close an SDK provider instance created by the page wrapper (test cleanup only). */
export async function closeSdk(instance) {
  if (!instance) return;
  const core = instance.signer?.client?.core;
  try { await core?.relayer?.transportClose(); } catch { /* closed */ }
  try { core?.heartbeat?.stop(); } catch { /* optional */ }
}

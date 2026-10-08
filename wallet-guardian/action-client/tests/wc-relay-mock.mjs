/**
 * In-process stand-in for the WalletConnect relay (irn JSON-RPC) used by the W-B tests.
 * Not the real relay and not a real wallet: it routes the SDK's own encrypted envelopes between
 * the page's @walletconnect/ethereum-provider and a test wallet built on @walletconnect/sign-client.
 * It never decrypts anything. It records which methods/topics were used so tests can assert on them.
 */
import { createHash } from 'node:crypto';

const sha = (value) => createHash('sha256').update(value).digest('hex');

export class MockRelay {
  constructor() {
    /** @type {Set<{ send: (text: string) => void, subs: Map<string, string>, label: string }>} */
    this.clients = new Set();
    /** topic -> [{ topic, message, publishedAt, tag, attestation }] */
    this.mailbox = new Map();
    this.log = [];
    this.down = false;
    this.connections = 0;
  }

  attach(send, label = 'client') {
    const client = { send, subs: new Map(), label };
    this.connections += 1;
    this.clients.add(client);
    return {
      receive: (text) => this.onMessage(client, text),
      close: () => this.clients.delete(client),
    };
  }

  /** Messages go to every subscriber of the topic except the client that published them. */
  store(topic, message, extra = {}, publisher = null) {
    const event = { topic, message, publishedAt: Date.now(), ...extra };
    if (!this.mailbox.has(topic)) this.mailbox.set(topic, []);
    this.mailbox.get(topic).push({ event, publisher });
    for (const client of this.clients) {
      const id = client.subs.get(topic);
      if (id && client !== publisher) this.deliver(client, id, event);
    }
  }

  pending(topic, client) {
    return (this.mailbox.get(topic) ?? []).filter((item) => item.publisher !== client).map((item) => item.event);
  }

  deliver(client, id, event) {
    const payload = { id: Date.now() * 1000 + Math.floor(Math.random() * 1000), jsonrpc: '2.0', method: 'irn_subscription',
      params: { id, data: event } };
    try { client.send(JSON.stringify(payload)); } catch { /* closed */ }
  }

  subscribe(client, topic) {
    const id = sha(`${client.label}:${topic}`);
    client.subs.set(topic, id);
    return id;
  }

  onMessage(client, text) {
    let request;
    try { request = JSON.parse(String(text)); } catch { return; }
    if (!request || typeof request.method !== 'string') return; // acks / responses
    const { id, method, params = {} } = request;
    this.log.push({ from: client.label, method, topic: params.topic ?? params.pairingTopic ?? null });
    const reply = (result) => client.send(JSON.stringify({ id, jsonrpc: '2.0', result }));
    const late = [];
    switch (method) {
      case 'irn_subscribe': {
        reply(this.subscribe(client, params.topic));
        late.push(params.topic);
        break;
      }
      case 'irn_batchSubscribe': {
        reply(params.topics.map((topic) => this.subscribe(client, topic)));
        late.push(...params.topics);
        break;
      }
      case 'irn_unsubscribe':
        client.subs.delete(params.topic);
        reply(true);
        break;
      case 'irn_batchUnsubscribe':
        for (const sub of params.subscriptions ?? []) client.subs.delete(sub.topic);
        reply(true);
        break;
      case 'irn_batchFetchMessages':
        reply({ messages: (params.topics ?? []).flatMap((topic) => this.pending(topic, client)), hasMore: false });
        break;
      case 'irn_publish':
        reply(true);
        this.store(params.topic, params.message, { tag: params.tag, attestation: params.attestation }, client);
        break;
      // sign-client 2.25 relay methods: publishing also subscribes the publisher (the SDK records
      // these subscriptions locally without its own irn_subscribe).
      case 'wc_proposeSession':
        reply(true);
        this.subscribe(client, params.pairingTopic);
        this.store(params.pairingTopic, params.sessionProposal, { tag: 1100, attestation: params.attestation }, client);
        break;
      case 'wc_approveSession':
        reply(true);
        this.subscribe(client, params.sessionTopic);
        this.store(params.pairingTopic, params.sessionProposalResponse, { tag: 1101 }, client);
        this.store(params.sessionTopic, params.sessionSettlementRequest, { tag: 1102 }, client);
        break;
      default:
        client.send(JSON.stringify({ id, jsonrpc: '2.0', error: { code: -32601, message: `mock relay: ${method}` } }));
    }
    // Mailbox delivery for messages published before this subscription.
    for (const topic of late) {
      const subId = client.subs.get(topic);
      for (const event of this.pending(topic, client)) this.deliver(client, subId, event);
    }
  }
}

/** Serve the mock relay over a real local WebSocket server (Node tests). */
export async function startRelayServer(relay = new MockRelay()) {
  const { WebSocketServer } = await import('ws');
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise((resolve) => server.once('listening', resolve));
  let n = 0;
  server.on('connection', (socket) => {
    if (relay.down) { socket.close(1011, 'relay down'); return; }
    const conn = relay.attach((text) => socket.send(text), `ws-${++n}`);
    socket.on('message', (data) => conn.receive(data.toString()));
    socket.on('close', () => conn.close());
  });
  const { port } = server.address();
  return {
    relay,
    url: `ws://127.0.0.1:${port}`,
    close: () => new Promise((resolve) => {
      for (const client of server.clients) client.terminate();
      server.close(() => resolve());
    }),
  };
}

/** In-memory IKeyValueStorage for SDK instances in tests (no files, no IndexedDB). */
export class MemoryStorage {
  constructor() { this.map = new Map(); }
  async getKeys() { return [...this.map.keys()]; }
  async getEntries() { return [...this.map.entries()].map(([k, v]) => [k, structuredClone(v)]); }
  async getItem(key) { const v = this.map.get(key); return v === undefined ? undefined : structuredClone(v); }
  async setItem(key, value) { this.map.set(key, structuredClone(value)); }
  async removeItem(key) { this.map.delete(key); }
}

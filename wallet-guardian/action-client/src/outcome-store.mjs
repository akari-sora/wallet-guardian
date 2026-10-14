/** Local in-flight outcome for one Mode A task. Nothing here is sent to the server. */

export const RETAIN_PENDING_ON_LOGOUT = true;

const DB_NAME = "wg-mode-a-outcomes";
const DB_VERSION = 1;
const STORE_NAME = "outcomes";
const SESSION_PREFIX = "wg-outcome:";

const lanes = new Map();

function hashText(value) {
  return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value);
}

export function outcomeKey(identity) {
  if (!identity || typeof identity !== "object") throw new Error("outcome key is incomplete");
  const origin = typeof identity.origin === "string" ? identity.origin : "";
  const account = typeof identity.account === "string" ? identity.account.toLowerCase() : "";
  const ref = typeof identity.ref === "string" ? identity.ref : "";
  const revision = identity.revision == null ? "" : String(identity.revision);
  if (!origin || !account || !ref || revision === "") throw new Error("outcome key is incomplete");
  if ([origin, account, ref, revision].some((part) => part.includes("|"))) {
    throw new Error("outcome key separator");
  }
  return `${origin}|${account}|${ref}|${revision}`;
}

function unavailable() {
  return {
    kind: null,
    blocked: "storage_unavailable",
    async get() {
      return null;
    },
    async put() {
      throw new Error("storage_unavailable");
    },
  };
}

function usable(store) {
  return Boolean(store) && store.blocked !== "storage_unavailable" && typeof store.get === "function" && typeof store.put === "function";
}

function globalBackend(name) {
  try {
    const value = globalThis[name];
    return value == null ? null : value;
  } catch {
    return null;
  }
}

function requestToPromise(request) {
  return new Promise((resolve, reject) => {
    if (!request || typeof request !== "object") {
      reject(new Error("indexedDB open failed"));
      return;
    }
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("indexedDB open failed"));
  });
}

async function openDatabase(indexedDB) {
  let request;
  try {
    request = indexedDB.open(DB_NAME, DB_VERSION);
  } catch (error) {
    throw error instanceof Error ? error : new Error("indexedDB open failed");
  }
  if (request && typeof request === "object") {
    request.onupgradeneeded = () => {
      const db = request.result;
      if (db && db.objectStoreNames && !db.objectStoreNames.contains(STORE_NAME) && typeof db.createObjectStore === "function") {
        db.createObjectStore(STORE_NAME);
      }
    };
  }
  return requestToPromise(request);
}

function idbBackend(db) {
  return {
    kind: "indexedDB",
    blocked: null,
    async get(key) {
      const tx = db.transaction(STORE_NAME, "readonly");
      const request = tx.objectStore(STORE_NAME).get(key);
      const value = await requestToPromise(request);
      return value == null ? null : structuredClone(value);
    },
    async put(key, value) {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const request = tx.objectStore(STORE_NAME).put(structuredClone(value), key);
      await requestToPromise(request);
    },
  };
}

function usableSession(session) {
  return Boolean(session)
    && typeof session.getItem === "function"
    && typeof session.setItem === "function";
}

function sessionBackend(session) {
  return {
    kind: "sessionStorage",
    blocked: null,
    async get(key) {
      const raw = session.getItem(SESSION_PREFIX + key);
      if (raw == null) return null;
      try {
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || typeof parsed.status !== "string") {
          throw new Error("unreadable");
        }
        return parsed;
      } catch (error) {
        if (error && error.code === "storage_unreadable") throw error;
        const unreadable = new Error("outcome store unreadable");
        unreadable.code = "storage_unreadable";
        throw unreadable;
      }
    },
    async put(key, value) {
      session.setItem(SESSION_PREFIX + key, JSON.stringify(value));
    },
  };
}

/** IndexedDB when it opens. sessionStorage when IndexedDB is missing or throws. Otherwise blocked. */
export async function openOutcomeStore(options = {}) {
  const indexedDB = Object.prototype.hasOwnProperty.call(options, "indexedDB")
    ? options.indexedDB
    : globalBackend("indexedDB");
  const session = Object.prototype.hasOwnProperty.call(options, "sessionStorage")
    ? options.sessionStorage
    : globalBackend("sessionStorage");
  if (indexedDB) {
    try {
      const db = await openDatabase(indexedDB);
      if (!db || typeof db.transaction !== "function") throw new Error("indexedDB open failed");
      return idbBackend(db);
    } catch {
      if (!usableSession(session)) return unavailable();
    }
  }
  if (!usableSession(session)) return unavailable();
  return sessionBackend(session);
}

/** One logical update at a time for a key. A later confirm waits until the earlier one has stored its result. */
export function withOutcomeLock(key, fn) {
  const previous = lanes.get(key) || Promise.resolve();
  let settled;
  const run = previous.catch(() => {}).then(fn);
  settled = run.then(() => {
    if (lanes.get(key) === settled) lanes.delete(key);
  }, () => {
    if (lanes.get(key) === settled) lanes.delete(key);
  });
  lanes.set(key, settled);
  return run;
}

async function previousOf(store, identity) {
  return store.get(outcomeKey(identity));
}

function requireIdentityFields(fields, previous) {
  const intent = fields && fields.intent_id != null ? fields.intent_id : previous && previous.intent_id;
  const requested = fields && fields.requested_at != null ? fields.requested_at : previous && previous.requested_at;
  if (typeof intent !== "string" || intent === "") throw new Error("missing intent_id");
  if (!Number.isSafeInteger(requested)) throw new Error("missing requested_at");
  return { intent_id: intent, requested_at: requested };
}

function submissionOf(fields, previous, txHash) {
  const source = fields && Object.prototype.hasOwnProperty.call(fields, "submission")
    ? fields.submission
    : (previous && previous.submission);
  if (!source || typeof source !== "object") return source ?? null;
  const copy = { ...source };
  if (hashText(txHash)) copy.txHash = txHash.toLowerCase();
  else delete copy.txHash;
  return copy;
}

async function save(store, identity, value) {
  const key = outcomeKey(identity);
  await store.put(key, value);
  return { key, value };
}

/** Call this before the wallet request. `requested` is the durable form of unknown. */
export async function writeRequested(store, identity, fields) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, null);
  const value = {
    status: "requested",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    submission: submissionOf(fields, previous, null),
  };
  return save(store, identity, value);
}

/** Call this only after the wallet has returned a transaction hash. A hash is not completion. */
export async function writePending(store, identity, fields) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  if (!hashText(fields && fields.txHash)) throw new Error("missing tx hash");
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const txHash = fields.txHash.toLowerCase();
  const value = {
    status: "pending",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    txHash,
    submission: submissionOf(fields, previous, txHash),
  };
  return save(store, identity, value);
}

/** Call this only after a receipt check has verified the economic result. */
export async function writeConfirmed(store, identity, fields = {}) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const txHash = hashText(fields.txHash) ? fields.txHash : previous && previous.txHash;
  if (!hashText(txHash)) throw new Error("missing tx hash");
  const value = {
    status: "confirmed",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    txHash: txHash.toLowerCase(),
    submission: submissionOf(fields, previous, txHash),
  };
  return save(store, identity, value);
}

/** On-chain revert. Distinct from a user rejection. */
export async function writeFailed(store, identity, fields = {}) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const txHash = hashText(fields.txHash) ? fields.txHash : previous && previous.txHash;
  const value = {
    status: "failed",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    submission: submissionOf(fields, previous, hashText(txHash) ? txHash : null),
  };
  if (hashText(txHash)) value.txHash = txHash.toLowerCase();
  return save(store, identity, value);
}

/** Explicit user rejection. Does not store a transaction hash. */
export async function writeRejected(store, identity, fields = {}) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const value = {
    status: "rejected",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    submission: submissionOf(fields, previous, null),
  };
  return save(store, identity, value);
}

/** Ambiguous wallet or chain result. Not a rejection and not a completion. */
export async function writeUnknown(store, identity, fields = {}) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const txHash = hashText(fields.txHash)
    ? fields.txHash.toLowerCase()
    : (fields.txHash === undefined && hashText(previous && previous.txHash) ? previous.txHash.toLowerCase() : null);
  const value = {
    status: "unknown",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    submission: submissionOf(fields, previous, txHash),
  };
  if (txHash) value.txHash = txHash;
  return save(store, identity, value);
}

/** Original hash is gone and the nonce was spent by something else. Not confirmed. */
export async function writeReplaced(store, identity, fields = {}) {
  if (!usable(store)) return { blocked: "storage_unavailable" };
  const previous = await previousOf(store, identity);
  const ids = requireIdentityFields(fields, previous);
  const txHash = hashText(fields.txHash) ? fields.txHash : previous && previous.txHash;
  const value = {
    status: "replaced",
    intent_id: ids.intent_id,
    requested_at: ids.requested_at,
    submission: submissionOf(fields, previous, hashText(txHash) ? txHash : null),
  };
  if (hashText(txHash)) value.txHash = txHash.toLowerCase();
  return save(store, identity, value);
}

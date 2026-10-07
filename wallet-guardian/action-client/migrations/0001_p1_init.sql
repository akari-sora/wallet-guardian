-- P1 private task access. Authz reads must use a D1 first-primary session.
-- Real tasks belong in this database, not in static Pages files.

CREATE TABLE challenges (
  nonce TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL,
  csrf_token TEXT NOT NULL,
  address TEXT NOT NULL,
  domain TEXT NOT NULL,
  uri TEXT NOT NULL,
  chain_id INTEGER NOT NULL,
  message TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  csrf_token TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE tenant_members (
  tenant_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  PRIMARY KEY (tenant_id, wallet_address)
);

CREATE TABLE publish_credentials (
  credential_id TEXT PRIMARY KEY,
  secret_hash TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER
);

CREATE TABLE tasks (
  ref TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  revision INTEGER NOT NULL,
  status TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  executed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE task_acl (
  ref TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  wallet_address TEXT NOT NULL,
  PRIMARY KEY (ref, tenant_id, wallet_address)
);

CREATE TABLE rate_buckets (
  bucket_key TEXT PRIMARY KEY,
  window_start INTEGER NOT NULL,
  count INTEGER NOT NULL
);

CREATE INDEX tasks_by_wallet ON tasks (wallet_address, tenant_id);

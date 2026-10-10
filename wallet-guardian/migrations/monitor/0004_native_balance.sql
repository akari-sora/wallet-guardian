-- Native balance checkpoints. PRAGMA user_version stays 1.
-- A failed read must not insert a zero balance. Alert rows stay in outbox.

CREATE TABLE IF NOT EXISTS native_balance_baselines (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  asset TEXT NOT NULL,
  version INTEGER NOT NULL,
  raw_balance TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT NOT NULL,
  block_timestamp TEXT,
  observed_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  confirmation_policy TEXT NOT NULL,
  binding TEXT NOT NULL,
  continuity TEXT NOT NULL,
  last_failed_at TEXT,
  nonce TEXT,
  PRIMARY KEY (tenant_id, wallet_id, network, asset)
);

CREATE TABLE IF NOT EXISTS native_balance_checkpoints (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  asset TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT NOT NULL,
  raw_balance TEXT NOT NULL,
  block_timestamp TEXT,
  observed_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  binding TEXT NOT NULL,
  confirmation_policy TEXT NOT NULL,
  nonce TEXT,
  PRIMARY KEY (tenant_id, wallet_id, network, asset, block_number, block_hash)
);

CREATE TABLE IF NOT EXISTS balance_explanations (
  tenant_id TEXT NOT NULL,
  observation_key TEXT NOT NULL,
  transfer_key TEXT NOT NULL,
  PRIMARY KEY (tenant_id, observation_key, transfer_key)
);

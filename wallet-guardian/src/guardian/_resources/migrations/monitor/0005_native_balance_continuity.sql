-- Conflict rows and stored balance decisions. PRAGMA user_version stays 1.
-- A non-alert decision is kept with the observation. It is not an outbox row.

CREATE TABLE IF NOT EXISTS native_balance_conflicts (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  asset TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  stored_hash TEXT NOT NULL,
  observed_hash TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (tenant_id, wallet_id, network, asset, block_number, observed_hash)
);

CREATE TABLE IF NOT EXISTS native_balance_decisions (
  tenant_id TEXT NOT NULL,
  observation_key TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  asset TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, observation_key)
);

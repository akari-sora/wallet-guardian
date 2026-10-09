-- MCM chain events, capability state, and cursors. PRAGMA user_version stays 1.
-- Applying this file again is a no-op; storage records its sha256.

CREATE TABLE IF NOT EXISTS chain_events (
  tenant_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  block_number TEXT,
  block_hash TEXT,
  finality TEXT NOT NULL,
  tx_status TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, identity)
);

CREATE TABLE IF NOT EXISTS capability_state (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  capability TEXT NOT NULL,
  quality TEXT NOT NULL,
  last_success_at TEXT,
  valid_until TEXT,
  cursor TEXT,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, wallet_id, network, capability)
);

CREATE TABLE IF NOT EXISTS capability_cursors (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  network TEXT NOT NULL,
  capability TEXT NOT NULL,
  cursor TEXT NOT NULL,
  block_number TEXT,
  PRIMARY KEY (tenant_id, wallet_id, network, capability)
);

CREATE INDEX IF NOT EXISTS chain_events_block
  ON chain_events(tenant_id, network, block_number);

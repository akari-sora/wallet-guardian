-- Bounded discovery progress and visible explanation failures.
-- PRAGMA user_version stays 1. A row here does not change an alert.

CREATE TABLE IF NOT EXISTS explanation_discovery (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  interval_cursor INTEGER,
  logs_json TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS explanation_lookback (
  tenant_id TEXT NOT NULL,
  network TEXT NOT NULL,
  account TEXT NOT NULL,
  manager TEXT NOT NULL,
  cursor_block INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, network, account, manager)
);

CREATE TABLE IF NOT EXISTS explanation_request_evidence (
  tenant_id TEXT NOT NULL,
  network TEXT NOT NULL,
  account TEXT NOT NULL,
  manager TEXT NOT NULL,
  tx_id TEXT NOT NULL,
  log_index INTEGER NOT NULL,
  block_number INTEGER NOT NULL,
  tx_index INTEGER NOT NULL,
  slis TEXT NOT NULL,
  topic TEXT NOT NULL,
  address TEXT NOT NULL,
  PRIMARY KEY (tenant_id, network, account, manager, tx_id, log_index)
);

CREATE TABLE IF NOT EXISTS explanation_failures (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  account TEXT,
  network TEXT,
  wallet_id TEXT,
  payload TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id, reason)
);

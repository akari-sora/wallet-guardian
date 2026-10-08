-- MCM notification lease and attempt counts. PRAGMA user_version stays 1.
-- Outbox rows stay in the existing outbox table. This file does not send.

CREATE TABLE IF NOT EXISTS delivery_leases (
  tenant_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
  tenant_id TEXT NOT NULL,
  outbox_id INTEGER NOT NULL,
  attempts INTEGER NOT NULL,
  last_error TEXT,
  PRIMARY KEY (tenant_id, outbox_id)
);

-- Probe cursors and trigger records. PRAGMA user_version stays 1.
-- These rows are not history.native_external or history.native_internal cursors.
-- A failed read must not insert a zero balance.

CREATE TABLE IF NOT EXISTS probe_cursors (
  tenant_id TEXT NOT NULL,
  network TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, network)
);

CREATE TABLE IF NOT EXISTS probe_trigger_records (
  record_id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  network TEXT,
  block_number INTEGER,
  block_hash TEXT,
  reason TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  handoff TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS probe_trigger_fingerprint
  ON probe_trigger_records(tenant_id, network, fingerprint);

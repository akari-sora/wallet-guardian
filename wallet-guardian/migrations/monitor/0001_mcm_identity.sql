-- MCM wallet identity tables. PRAGMA user_version stays 1.
-- Applying this file again is a no-op; storage records its sha256.

CREATE TABLE IF NOT EXISTS mcm_schema_migrations (
  migration_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL,
  sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_identities (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  family TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, wallet_id),
  UNIQUE (tenant_id, family, identity_key)
);

CREATE TABLE IF NOT EXISTS wallet_label_aliases (
  tenant_id TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  label TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (tenant_id, label)
);

CREATE TABLE IF NOT EXISTS legacy_imports (
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  report TEXT NOT NULL,
  PRIMARY KEY (tenant_id, source_kind, source_sha256)
);

CREATE TABLE IF NOT EXISTS legacy_alert_suppressions (
  tenant_id TEXT NOT NULL,
  network TEXT,
  tx_hash TEXT,
  wallet_id TEXT,
  legacy_key TEXT NOT NULL,
  PRIMARY KEY (tenant_id, legacy_key)
);

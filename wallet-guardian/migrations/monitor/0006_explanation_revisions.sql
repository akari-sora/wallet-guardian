-- Inflow explanation revisions. PRAGMA user_version stays 1.
-- A revision does not change an alert, its severity, or a safety label.

CREATE TABLE IF NOT EXISTS explanation_jobs (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  retry_reason TEXT,
  hold_token TEXT,
  account TEXT NOT NULL,
  network TEXT NOT NULL,
  wallet_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS explanation_revisions (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  event_revision INTEGER NOT NULL,
  evidence_fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS explanation_fingerprint_unique
  ON explanation_revisions(tenant_id, event_id, evidence_fingerprint);

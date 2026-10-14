-- Candidates committed with a leaf cursor before the round's alert decision.
-- The next round judges any row still here. PRAGMA user_version stays 1.

CREATE TABLE IF NOT EXISTS pending_alert_candidates (
  tenant_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, identity)
);

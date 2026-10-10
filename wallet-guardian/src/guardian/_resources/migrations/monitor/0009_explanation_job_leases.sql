-- A running explanation job keeps a lease. Another process does not take it
-- while the lease is unexpired. PRAGMA user_version stays 1.

CREATE TABLE explanation_job_leases (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id)
);

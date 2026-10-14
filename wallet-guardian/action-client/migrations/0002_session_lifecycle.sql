-- Session lifecycle and membership revocation.
-- New columns stay nullable so existing rows and older Functions keep working.

ALTER TABLE sessions ADD COLUMN auth_time INTEGER;
ALTER TABLE sessions ADD COLUMN last_seen_at INTEGER;
ALTER TABLE tenant_members ADD COLUMN revoked_at INTEGER;

-- Cached request evidence keeps the removal flag and the block hash.
-- PRAGMA user_version stays 1. A row here does not change an alert.

ALTER TABLE explanation_request_evidence ADD COLUMN block_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE explanation_request_evidence ADD COLUMN removed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE explanation_request_evidence ADD COLUMN conflict INTEGER NOT NULL DEFAULT 0;
ALTER TABLE explanation_request_evidence ADD COLUMN kind TEXT NOT NULL DEFAULT 'request';
ALTER TABLE explanation_request_evidence ADD COLUMN data_json TEXT NOT NULL DEFAULT '[]';

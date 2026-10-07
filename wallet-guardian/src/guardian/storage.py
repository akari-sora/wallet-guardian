"""SQLite storage for one local tenant. Private files stay outside git."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
  wallet_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  label TEXT NOT NULL,
  address TEXT NOT NULL,
  ownership_basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chain_capabilities (
  id INTEGER PRIMARY KEY,
  chain_id INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adapter_versions (
  adapter_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  generation INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_items (
  id INTEGER PRIMARY KEY,
  generation INTEGER NOT NULL,
  tenant_id TEXT NOT NULL,
  position_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_legs (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exposure_edges (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  identity TEXT NOT NULL UNIQUE,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
  scope TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allowances (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
  rule_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expected_actions (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_registry (
  source_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert_tenant(self, tenant_id: str, label: str) -> None:
        self.conn.execute(
            "INSERT INTO tenants(tenant_id, label) VALUES(?, ?) ON CONFLICT(tenant_id) DO UPDATE SET label=excluded.label",
            (tenant_id, label),
        )
        self.conn.commit()

    def upsert_wallet(self, wallet_id: str, tenant_id: str, label: str, address: str, basis: str) -> None:
        self.conn.execute(
            """
            INSERT INTO wallets(wallet_id, tenant_id, label, address, ownership_basis)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(wallet_id) DO UPDATE SET
              label=excluded.label, address=excluded.address, ownership_basis=excluded.ownership_basis
            """,
            (wallet_id, tenant_id, label, address, basis),
        )
        self.conn.commit()

    def latest_generation(self, tenant_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(generation), 0) AS gen FROM snapshots WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return int(row["gen"])

    def commit_snapshot(self, tenant_id: str, generation: int, created_at: str, items: list[dict[str, Any]]) -> None:
        latest = self.latest_generation(tenant_id)
        if generation <= latest:
            raise RuntimeError("stale snapshot generation")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO snapshots(generation, tenant_id, created_at) VALUES(?, ?, ?)",
                (generation, tenant_id, created_at),
            )
            for item in items:
                self.conn.execute(
                    "INSERT INTO snapshot_items(generation, tenant_id, position_id, payload) VALUES(?, ?, ?, ?)",
                    (generation, tenant_id, item["position_id"], json.dumps(item, sort_keys=True)),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def snapshot_items(self, tenant_id: str, generation: int | None = None) -> list[dict[str, Any]]:
        if generation is None:
            generation = self.latest_generation(tenant_id)
        rows = self.conn.execute(
            "SELECT payload FROM snapshot_items WHERE tenant_id=? AND generation=? ORDER BY id",
            (tenant_id, generation),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def add_alert(self, tenant_id: str, dedup_key: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO alerts(tenant_id, dedup_key, payload) VALUES(?, ?, ?)",
            (tenant_id, dedup_key, json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def alerts_for(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM alerts WHERE tenant_id=? ORDER BY id",
            (tenant_id,),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_task(self, tenant_id: str, task: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO tasks(task_id, tenant_id, revision, state, payload) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              revision=excluded.revision, state=excluded.state, payload=excluded.payload
            """,
            (task["task_id"], tenant_id, int(task["revision"]), task["state"], json.dumps(task, sort_keys=True)),
        )
        self.conn.commit()

    def get_task(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM tasks WHERE tenant_id=? AND task_id=?",
            (tenant_id, task_id),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_run(self, tenant_id: str, run_id: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_id, tenant_id, payload) VALUES(?, ?, ?)",
            (run_id, tenant_id, json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def table_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

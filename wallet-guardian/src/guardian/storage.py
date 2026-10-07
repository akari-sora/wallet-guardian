"""Tenant-scoped local SQLite persistence; not an authenticated public service."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError, StaleSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
  wallet_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  label TEXT NOT NULL,
  address TEXT NOT NULL,
  ownership_basis TEXT NOT NULL,
  PRIMARY KEY (tenant_id, wallet_id)
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
  generation INTEGER NOT NULL,
  tenant_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, generation)
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
  identity TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE (tenant_id, identity)
);
CREATE TABLE IF NOT EXISTS cursors (
  scope TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (tenant_id, scope)
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
  incident_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, incident_id)
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
  run_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, run_id)
);
CREATE TABLE IF NOT EXISTS source_registry (
  source_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (tenant_id, task_id)
);
CREATE TABLE IF NOT EXISTS alert_dedup (
  tenant_id TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  PRIMARY KEY (tenant_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS monitor_health (
  tenant_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_scope ON snapshot_items(tenant_id, generation);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class Database:
    """Tenant-scoped persistence; not a substitute for server authentication."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        try:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ConfigError("database schema is newer than this build")
            self.conn.executescript(SCHEMA)
            self._migrate(version)
        except Exception:
            self.conn.close()
            raise

    def _migrate(self, version: int) -> None:
        if version == 1:
            return
        # Rebuild only known tables, transactionally. No original records are
        # deleted to deduplicate alerts, and existing JSON payloads stay intact.
        tables = ("wallets", "snapshots", "events", "cursors", "incidents", "runs", "tasks")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                info = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
                columns = ", ".join(row["name"] for row in info)
                prefix = f"CREATE TABLE IF NOT EXISTS {table} ("
                definition = SCHEMA[SCHEMA.index(prefix):].split(";", 1)[0]
                temporary = f"guardian_migration_{table}"
                self.conn.execute(definition.replace(prefix, f"CREATE TABLE {temporary} (", 1))
                self.conn.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
                self.conn.execute(f"DROP TABLE {table}")
                self.conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            self.conn.execute(
                "INSERT OR IGNORE INTO alert_dedup(tenant_id, dedup_key) "
                "SELECT tenant_id, dedup_key FROM alerts"
            )
            self.conn.execute("PRAGMA user_version=1")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def upsert_tenant(self, tenant_id: str, label: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO tenants(tenant_id, label) VALUES(?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET label=excluded.label",
                (tenant_id, label),
            )

    def tenant_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT tenant_id FROM tenants UNION SELECT tenant_id FROM snapshots "
            "UNION SELECT tenant_id FROM tasks UNION SELECT tenant_id FROM alerts "
            "UNION SELECT tenant_id FROM wallets UNION SELECT tenant_id FROM runs "
            "ORDER BY tenant_id"
        ).fetchall()
        return [row["tenant_id"] for row in rows]

    def upsert_wallet(self, wallet_id: str, tenant_id: str, label: str, address: str, basis: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO wallets(wallet_id, tenant_id, label, address, ownership_basis)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, wallet_id) DO UPDATE SET
                  label=excluded.label, address=excluded.address, ownership_basis=excluded.ownership_basis
                """,
                (wallet_id, tenant_id, label, address, basis),
            )

    def latest_generation(self, tenant_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(generation), 0) AS gen FROM snapshots WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return int(row["gen"])

    def commit_snapshot(
        self, tenant_id: str, generation: int, created_at: str, items: list[dict[str, Any]],
        *, alerts: list[dict[str, Any]] | None = None,
        health: dict[str, Any] | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Commit a generation plus its alert outbox atomically.

        Capture the parent generation BEFORE network reads and submit parent+1.
        A competing run then fails closed instead of committing an older view.
        """
        emitted = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if type(generation) is not int or generation != self.latest_generation(tenant_id) + 1:
                raise StaleSnapshot("stale or non-sequential snapshot generation")
            self.conn.execute(
                "INSERT INTO snapshots(generation, tenant_id, created_at) VALUES(?, ?, ?)",
                (generation, tenant_id, created_at),
            )
            seen = set()
            for item in items:
                identity = (item.get("chain_id"), item["position_id"])
                if identity in seen:
                    raise ValueError("duplicate position in snapshot")
                seen.add(identity)
                self.conn.execute(
                    "INSERT INTO snapshot_items(generation, tenant_id, position_id, payload) VALUES(?, ?, ?, ?)",
                    (generation, tenant_id, item["position_id"], _json(item)),
                )
            for alert in alerts or []:
                if self._add_alert(tenant_id, alert["dedup_key"], alert):
                    emitted.append(alert)
            if health is not None:
                self.conn.execute(
                    "INSERT INTO monitor_health(tenant_id, payload) VALUES(?, ?) "
                    "ON CONFLICT(tenant_id) DO UPDATE SET payload=excluded.payload",
                    (tenant_id, _json(health)),
                )
            for task in tasks or []:
                self._save_task(tenant_id, task)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return emitted

    def snapshot_items(self, tenant_id: str, generation: int | None = None) -> list[dict[str, Any]]:
        if generation is None:
            generation = self.latest_generation(tenant_id)
        rows = self.conn.execute(
            "SELECT payload FROM snapshot_items WHERE tenant_id=? AND generation=? ORDER BY id",
            (tenant_id, generation),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def _add_alert(self, tenant_id: str, dedup_key: str, payload: dict[str, Any]) -> bool:
        encoded = _json(payload)
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO alert_dedup(tenant_id, dedup_key) VALUES(?, ?)",
            (tenant_id, dedup_key),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            "INSERT INTO alerts(tenant_id, dedup_key, payload) VALUES(?, ?, ?)",
            (tenant_id, dedup_key, encoded),
        )
        # Pending is not a delivery claim. No sender is enabled by this change.
        self.conn.execute(
            "INSERT INTO outbox(tenant_id, status, payload) VALUES(?, 'pending', ?)",
            (tenant_id, encoded),
        )
        return True

    def add_alert(self, tenant_id: str, dedup_key: str, payload: dict[str, Any]) -> bool:
        with self.conn:
            return self._add_alert(tenant_id, dedup_key, payload)

    def alerts_for(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM alerts WHERE tenant_id=? ORDER BY id", (tenant_id,),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def health_for(self, tenant_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload FROM monitor_health WHERE tenant_id=?", (tenant_id,),
        ).fetchone()
        return json.loads(row["payload"]) if row else {"blind": False, "episode": 0}

    def _save_task(self, tenant_id: str, task: dict[str, Any]) -> None:
        existing = self.get_task(tenant_id, task["task_id"])
        if existing and int(task["revision"]) <= int(existing["revision"]):
            if _json(existing) == _json(task):
                return
            raise StaleSnapshot("older task revision or changed same revision cannot overwrite a plan")
        self.conn.execute(
            """
            INSERT INTO tasks(task_id, tenant_id, revision, state, payload) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, task_id) DO UPDATE SET
              revision=excluded.revision, state=excluded.state, payload=excluded.payload
            """,
            (task["task_id"], tenant_id, int(task["revision"]), task["state"], _json(task)),
        )

    def save_task(self, tenant_id: str, task: dict[str, Any]) -> None:
        with self.conn:
            self._save_task(tenant_id, task)

    def get_task(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM tasks WHERE tenant_id=? AND task_id=?", (tenant_id, task_id),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_run(self, tenant_id: str, run_id: str, payload: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs(run_id, tenant_id, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(tenant_id, run_id) DO UPDATE SET payload=excluded.payload",
                (run_id, tenant_id, _json(payload)),
            )

    def table_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

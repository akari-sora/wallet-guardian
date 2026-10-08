"""Tenant-scoped local SQLite persistence; not an authenticated public service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError, StaleSnapshot
from guardian.events import ChainEvent
from guardian.identities import IdentityError

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
            # MCM tables are additive. user_version stays 1 so older builds can open the file.
            self._apply_mcm_migrations()
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

    def latest_run(self, tenant_id: str) -> dict[str, Any] | None:
        """Newest persisted run record by its finished timestamp. Missing is None."""
        best: dict[str, Any] | None = None
        best_key = ""
        for row in self.conn.execute("SELECT payload FROM runs WHERE tenant_id=?", (tenant_id,)):
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            key = str(payload.get("finished") or "")
            if best is None or key >= best_key:
                best = payload
                best_key = key
        return best

    def table_names(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def _apply_mcm_migrations(self) -> None:
        files = sorted(path for path in mcm_migration_directory().glob("*.sql") if path.is_file())
        if not files:
            raise ConfigError("mcm migration directory is missing")
        present = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        applied: dict[str, str] = {}
        if "mcm_schema_migrations" in present:
            for row in self.conn.execute("SELECT migration_id, sha256 FROM mcm_schema_migrations"):
                applied[row["migration_id"]] = row["sha256"]
        for path in files:
            migration_id = path.stem
            script = _normalized_sql(path)
            digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
            previous = applied.get(migration_id)
            if previous is not None:
                if previous != digest:
                    raise ConfigError(f"mcm migration sha256 changed: {migration_id}")
                continue
            self.conn.executescript(script)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO mcm_schema_migrations(migration_id, applied_at, sha256) VALUES (?, ?, ?)",
                    (migration_id, datetime.now(timezone.utc).isoformat(), digest),
                )

    def upsert_wallet_identity(self, tenant: str, wallet_id: str, family: str, identity_key: str, label: str) -> None:
        """Bind a stable wallet_id. Renaming the label keeps the id and records the old label."""
        if not all(isinstance(item, str) and item for item in (tenant, wallet_id, family, identity_key, label)):
            raise IdentityError("bad_wallet_identity")
        if _WALLET_ID.fullmatch(wallet_id) is None:
            raise IdentityError("bad_wallet_id")
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            existing = self.conn.execute(
                "SELECT family, identity_key, label FROM wallet_identities WHERE tenant_id=? AND wallet_id=?",
                (tenant, wallet_id),
            ).fetchone()
            owner = self.conn.execute(
                "SELECT wallet_id FROM wallet_identities WHERE tenant_id=? AND family=? AND identity_key=?",
                (tenant, family, identity_key),
            ).fetchone()
            if existing is None:
                if owner is not None:
                    raise IdentityError("duplicate_identity")
                self._reject_foreign_alias(tenant, wallet_id, label)
                self.conn.execute(
                    """INSERT INTO wallet_identities(
                           tenant_id, wallet_id, family, identity_key, label, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (tenant, wallet_id, family, identity_key, label, now, now),
                )
                return
            if existing["family"] != family or existing["identity_key"] != identity_key:
                raise IdentityError("identity_rebind")
            if owner is not None and owner["wallet_id"] != wallet_id:
                raise IdentityError("duplicate_identity")
            if existing["label"] != label:
                self._reject_foreign_alias(tenant, wallet_id, label)
                self._remember_alias(tenant, wallet_id, existing["label"], now)
            self.conn.execute(
                "UPDATE wallet_identities SET label=?, updated_at=? WHERE tenant_id=? AND wallet_id=?",
                (label, now, tenant, wallet_id),
            )

    def resolve_label(self, tenant: str, label: str) -> str | None:
        """Return the wallet_id for a current label or a previous alias."""
        alias = self.conn.execute(
            "SELECT wallet_id FROM wallet_label_aliases WHERE tenant_id=? AND label=?",
            (tenant, label),
        ).fetchone()
        currents = self.conn.execute(
            "SELECT wallet_id FROM wallet_identities WHERE tenant_id=? AND label=?",
            (tenant, label),
        ).fetchall()
        found = set()
        if alias is not None:
            found.add(alias["wallet_id"])
        found.update(row["wallet_id"] for row in currents)
        if not found:
            return None
        if len(found) > 1:
            raise IdentityError("ambiguous_label")
        return next(iter(found))

    def _reject_foreign_alias(self, tenant: str, wallet_id: str, label: str) -> None:
        row = self.conn.execute(
            "SELECT wallet_id FROM wallet_label_aliases WHERE tenant_id=? AND label=?",
            (tenant, label),
        ).fetchone()
        if row is not None and row["wallet_id"] != wallet_id:
            raise IdentityError("label_alias_taken")

    def _remember_alias(self, tenant: str, wallet_id: str, label: str, now: str) -> None:
        row = self.conn.execute(
            "SELECT wallet_id FROM wallet_label_aliases WHERE tenant_id=? AND label=?",
            (tenant, label),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO wallet_label_aliases(tenant_id, wallet_id, label, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (tenant, wallet_id, label, now, now),
            )
            return
        if row["wallet_id"] != wallet_id:
            raise IdentityError("label_alias_taken")
        self.conn.execute(
            "UPDATE wallet_label_aliases SET last_seen=? WHERE tenant_id=? AND label=?",
            (now, tenant, label),
        )

    def commit_capability_cycle(
        self,
        tenant_id: str,
        *,
        events: list[Any],
        capability_rows: list[dict[str, Any]],
        cursors: list[dict[str, Any]],
        alerts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Write events, capability state, cursors, and outbox alerts in one transaction.

        A failure rolls every write back, so a cursor cannot move without its events.
        A cursor moves backwards only when the caller sets rollback=True.
        """
        coerced = [_coerce_event(tenant_id, item) for item in events]
        rows = [_capability_row(item) for item in capability_rows]
        cursor_rows = [_cursor_row(item) for item in cursors]
        alert_rows = list(alerts or [])
        _reject_duplicate_events(coerced)
        _reject_duplicate_keys(rows, ("wallet_id", "network", "capability"), "duplicate capability row")
        _reject_duplicate_keys(cursor_rows, ("wallet_id", "network", "capability"), "duplicate cursor")
        emitted: list[dict[str, Any]] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._guard_cursors(tenant_id, cursor_rows, rows)
            for event in coerced:
                self._upsert_chain_event(event)
            for row in rows:
                self._upsert_capability_state(tenant_id, row)
            for cursor in cursor_rows:
                self._upsert_capability_cursor(tenant_id, cursor)
            for alert in alert_rows:
                if self._add_alert(tenant_id, alert["dedup_key"], alert):
                    emitted.append(alert)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return emitted

    def _guard_cursors(self, tenant_id: str, cursors: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
        errored = {
            (row["wallet_id"], row["network"], row["capability"])
            for row in rows
            if row["quality"] == "error"
        }
        for cursor in cursors:
            key = (cursor["wallet_id"], cursor["network"], cursor["capability"])
            existing = self.conn.execute(
                """
                SELECT cursor, block_number FROM capability_cursors
                WHERE tenant_id=? AND wallet_id=? AND network=? AND capability=?
                """,
                (tenant_id, key[0], key[1], key[2]),
            ).fetchone()
            if cursor.get("rollback") is True:
                continue
            if existing is not None and _cursor_regressed(
                existing["cursor"], existing["block_number"], cursor["cursor"], cursor.get("block_number"),
            ):
                raise ValueError("cursor_regressed")
            if key in errored and _cursor_advanced(
                None if existing is None else existing["cursor"],
                None if existing is None else existing["block_number"],
                cursor["cursor"],
                cursor.get("block_number"),
                existed=existing is not None,
            ):
                raise ValueError("cursor_advanced_on_error")

    def _upsert_chain_event(self, event: ChainEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO chain_events(
                tenant_id, identity, wallet_id, network, block_number, block_hash, finality, tx_status, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, identity) DO UPDATE SET
              wallet_id=excluded.wallet_id,
              network=excluded.network,
              block_number=excluded.block_number,
              block_hash=excluded.block_hash,
              finality=excluded.finality,
              tx_status=excluded.tx_status,
              payload=excluded.payload
            """,
            (
                event.tenant_id, event.identity, event.wallet_id, event.network,
                event.block_number, event.block_hash, event.finality, event.tx_status,
                _json(event.as_dict()),
            ),
        )

    def _upsert_capability_state(self, tenant_id: str, row: dict[str, Any]) -> None:
        existing = self.conn.execute(
            """
            SELECT last_success_at, cursor FROM capability_state
            WHERE tenant_id=? AND wallet_id=? AND network=? AND capability=?
            """,
            (tenant_id, row["wallet_id"], row["network"], row["capability"]),
        ).fetchone()
        payload = dict(row["payload"])
        last_success = row["last_success_at"]
        if row["quality"] == "error" and existing is not None and existing["last_success_at"]:
            last_success = existing["last_success_at"]
            if isinstance(payload.get("last_success_at"), str):
                payload["last_success_at"] = last_success
            coverage = payload.get("coverage")
            if isinstance(coverage, dict) and isinstance(coverage.get("last_success_at"), str):
                coverage = dict(coverage)
                coverage["last_success_at"] = last_success
                payload["coverage"] = coverage
        cursor_value = row["cursor"]
        if cursor_value is None and existing is not None:
            cursor_value = existing["cursor"]
        self.conn.execute(
            """
            INSERT INTO capability_state(
                tenant_id, wallet_id, network, capability, quality, last_success_at, valid_until, cursor, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, wallet_id, network, capability) DO UPDATE SET
              quality=excluded.quality,
              last_success_at=excluded.last_success_at,
              valid_until=excluded.valid_until,
              cursor=excluded.cursor,
              payload=excluded.payload
            """,
            (
                tenant_id, row["wallet_id"], row["network"], row["capability"], row["quality"],
                last_success, row["valid_until"], cursor_value, _json(payload),
            ),
        )

    def _upsert_capability_cursor(self, tenant_id: str, cursor: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO capability_cursors(
                tenant_id, wallet_id, network, capability, cursor, block_number
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, wallet_id, network, capability) DO UPDATE SET
              cursor=excluded.cursor,
              block_number=excluded.block_number
            """,
            (
                tenant_id, cursor["wallet_id"], cursor["network"], cursor["capability"],
                cursor["cursor"], cursor.get("block_number"),
            ),
        )

    def chain_events_for(
        self, tenant_id: str, *, wallet_id: str | None = None, network: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM chain_events WHERE tenant_id=?"
        args: list[Any] = [tenant_id]
        if wallet_id is not None:
            sql += " AND wallet_id=?"
            args.append(wallet_id)
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        rows = [json.loads(row["payload"]) for row in self.conn.execute(sql, args)]
        return sorted(rows, key=_event_sort_key)

    def capability_state_for(
        self, tenant_id: str, *, wallet_id: str | None = None, network: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT wallet_id, network, capability, quality, last_success_at, valid_until, cursor, payload "
            "FROM capability_state WHERE tenant_id=?"
        )
        args: list[Any] = [tenant_id]
        if wallet_id is not None:
            sql += " AND wallet_id=?"
            args.append(wallet_id)
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        sql += " ORDER BY wallet_id, network, capability"
        found = []
        for row in self.conn.execute(sql, args):
            found.append({
                "wallet_id": row["wallet_id"],
                "network": row["network"],
                "capability": row["capability"],
                "quality": row["quality"],
                "last_success_at": row["last_success_at"],
                "valid_until": row["valid_until"],
                "cursor": row["cursor"],
                "payload": json.loads(row["payload"]),
            })
        return found

    def capability_cursors_for(
        self, tenant_id: str, *, wallet_id: str | None = None, network: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT wallet_id, network, capability, cursor, block_number "
            "FROM capability_cursors WHERE tenant_id=?"
        )
        args: list[Any] = [tenant_id]
        if wallet_id is not None:
            sql += " AND wallet_id=?"
            args.append(wallet_id)
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        sql += " ORDER BY wallet_id, network, capability"
        return [
            {
                "wallet_id": row["wallet_id"],
                "network": row["network"],
                "capability": row["capability"],
                "cursor": row["cursor"],
                "block_number": row["block_number"],
            }
            for row in self.conn.execute(sql, args)
        ]

    def capability_items(self, tenant_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in self.capability_state_for(tenant_id):
            found = row["payload"].get("items")
            if isinstance(found, list):
                items.extend(item for item in found if isinstance(item, dict))
        return items

    def outbox_for(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, status, payload FROM outbox WHERE tenant_id=? ORDER BY id",
            (tenant_id,),
        ).fetchall()
        return [
            {"id": row["id"], "status": row["status"], "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def rollback_reorg(
        self,
        tenant_id: str,
        *,
        network: str,
        block_number: str,
        wallet_id: str | None = None,
    ) -> dict[str, int]:
        """Explicit reorg rollback. May move cursors backwards. Does not delete events."""
        target = _digits(block_number)
        if target is None:
            raise ValueError("rollback block is required")
        replacement = str(target - 1) if target > 0 else "0"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            event_sql = "SELECT identity, block_number, block_hash, finality, payload FROM chain_events WHERE tenant_id=? AND network=?"
            event_args: list[Any] = [tenant_id, network]
            if wallet_id is not None:
                event_sql += " AND wallet_id=?"
                event_args.append(wallet_id)
            orphaned = 0
            for row in self.conn.execute(event_sql, event_args).fetchall():
                number = _digits(row["block_number"])
                if number is None or number < target or row["finality"] == "orphaned":
                    continue
                payload = json.loads(row["payload"])
                payload["finality"] = "orphaned"
                payload["correction"] = {
                    "reason": "rollback",
                    "block_number": row["block_number"],
                    "previous_block_hash": row["block_hash"],
                    "previous_finality": row["finality"],
                }
                self.conn.execute(
                    "UPDATE chain_events SET finality=?, payload=? WHERE tenant_id=? AND identity=?",
                    ("orphaned", _json(payload), tenant_id, row["identity"]),
                )
                orphaned += 1
            cursor_sql = "SELECT wallet_id, capability, cursor, block_number FROM capability_cursors WHERE tenant_id=? AND network=?"
            cursor_args: list[Any] = [tenant_id, network]
            if wallet_id is not None:
                cursor_sql += " AND wallet_id=?"
                cursor_args.append(wallet_id)
            moved = 0
            for row in self.conn.execute(cursor_sql, cursor_args).fetchall():
                rank = _cursor_rank(row["cursor"], row["block_number"])
                if rank[0] != 0 or rank[1] < target:
                    continue
                self.conn.execute(
                    """
                    UPDATE capability_cursors SET cursor=?, block_number=?
                    WHERE tenant_id=? AND wallet_id=? AND network=? AND capability=?
                    """,
                    (replacement, replacement, tenant_id, row["wallet_id"], network, row["capability"]),
                )
                moved += 1
            state_sql = "SELECT wallet_id, capability, cursor FROM capability_state WHERE tenant_id=? AND network=?"
            state_args: list[Any] = [tenant_id, network]
            if wallet_id is not None:
                state_sql += " AND wallet_id=?"
                state_args.append(wallet_id)
            for row in self.conn.execute(state_sql, state_args).fetchall():
                if _digits(row["cursor"]) is None or _digits(row["cursor"]) < target:
                    continue
                self.conn.execute(
                    """
                    UPDATE capability_state SET cursor=?
                    WHERE tenant_id=? AND wallet_id=? AND network=? AND capability=?
                    """,
                    (replacement, tenant_id, row["wallet_id"], network, row["capability"]),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"orphaned": orphaned, "cursors": moved}


def _normalized_sql(path: Path) -> str:
    """Migration text with LF line endings, so a CRLF checkout (Windows autocrlf)
    hashes and runs exactly like the LF file in git."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def mcm_migration_directory() -> Path:
    """SQL files shipped beside the repo, found the same way service.ROOT is."""
    candidate = Path(__file__).resolve().parents[2] / "migrations" / "monitor"
    if candidate.is_dir() and any(candidate.glob("*.sql")):
        return candidate
    raise ConfigError("mcm migration directory is missing")


_WALLET_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_EVENT_FIELDS = (
    "tenant_id", "wallet_id", "network", "chain_uid", "tx_id", "sub_index", "kind", "asset",
    "raw_amount", "block_number", "block_hash", "tx_status", "finality", "source_ids", "observed_at",
)


def _coerce_event(tenant_id: str, value: Any) -> ChainEvent:
    if isinstance(value, ChainEvent):
        event = value
    elif isinstance(value, dict):
        data = {key: value[key] for key in _EVENT_FIELDS if key in value}
        event = ChainEvent(**data)
    else:
        raise ValueError("event must be a chain event")
    if event.tenant_id != tenant_id:
        raise ValueError("event tenant does not match")
    return event


def _capability_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("capability row must be an object")
    if "schema_version" in row and "payload" not in row:
        payload = dict(row)
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        return {
            "wallet_id": payload["wallet_id"],
            "network": payload["network"],
            "capability": payload["capability"],
            "quality": payload["quality"],
            "last_success_at": payload.get("last_success_at"),
            "valid_until": payload.get("valid_until"),
            "cursor": scope.get("cursor"),
            "payload": {"coverage": payload},
        }
    for key in ("wallet_id", "network", "capability", "quality"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"capability {key} is required")
    payload = row.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("capability payload must be an object")
    return {
        "wallet_id": row["wallet_id"],
        "network": row["network"],
        "capability": row["capability"],
        "quality": row["quality"],
        "last_success_at": row.get("last_success_at"),
        "valid_until": row.get("valid_until"),
        "cursor": row.get("cursor"),
        "payload": dict(payload),
    }


def _cursor_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("cursor must be an object")
    for key in ("wallet_id", "network", "capability", "cursor"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"cursor {key} is required")
    block_number = row.get("block_number")
    if block_number is not None and not isinstance(block_number, str):
        raise ValueError("cursor block_number must be a string")
    return {
        "wallet_id": row["wallet_id"],
        "network": row["network"],
        "capability": row["capability"],
        "cursor": row["cursor"],
        "block_number": block_number,
        "rollback": row.get("rollback") is True,
    }


def _reject_duplicate_events(events: list[ChainEvent]) -> None:
    seen = set()
    for event in events:
        if event.identity in seen:
            raise ValueError("duplicate event identity")
        seen.add(event.identity)


def _reject_duplicate_keys(rows: list[dict[str, Any]], fields: tuple[str, ...], message: str) -> None:
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in seen:
            raise ValueError(message)
        seen.add(key)


def _digits(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _cursor_rank(cursor: str | None, block_number: str | None) -> tuple[int, int | str]:
    number = _digits(block_number if block_number not in (None, "") else cursor)
    if number is not None:
        return (0, number)
    token = "" if cursor is None else str(cursor)
    return (1, token)


def _cursor_regressed(
    old_cursor: str | None,
    old_block: str | None,
    new_cursor: str,
    new_block: str | None,
) -> bool:
    return _cursor_rank(new_cursor, new_block) < _cursor_rank(old_cursor, old_block)


def _cursor_advanced(
    old_cursor: str | None,
    old_block: str | None,
    new_cursor: str,
    new_block: str | None,
    *,
    existed: bool,
) -> bool:
    if not existed:
        return True
    return _cursor_rank(new_cursor, new_block) > _cursor_rank(old_cursor, old_block)


def _event_sort_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    number = _digits(payload.get("block_number"))
    block_key: tuple[Any, ...] = (0, number) if number is not None else (1, payload.get("block_number") or "")
    sub = str(payload.get("sub_index") or "")
    sub_key: tuple[Any, ...] = (0, int(sub)) if sub.isdigit() else (1, sub)
    return (block_key, sub_key, payload.get("identity") or "")

"""Outbox delivery gate. There is no Telegram, Slack, email, or webhook client.

Rows stay pending until a configured single sender is handed the lease.
Shadow mode reports what would be sent and does not mark anything sent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from guardian.configuration import (
    PublicConfig,
    file_permission_bits,
    path_inside_work_tree,
)
from guardian.errors import ConfigError
from guardian.legacy_state import is_legacy_alerted
from guardian.security.redact import redact
from guardian.storage import Database

Clock = Callable[[], float]
_LEASE_SECONDS = 300


class Sender(Protocol):
    """Injected notification sender. This package has no network implementation."""

    def send(self, message: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class HandoffGate:
    public_mode: str
    single_sender_handoff: bool
    profile_present: bool
    misconfigured: bool
    reason: str


@dataclass(frozen=True)
class DeliveryReport:
    status: str
    sent: int
    pending: int
    skipped_legacy: int
    would_send: tuple[str, ...]
    misconfigured: bool
    attempts: tuple[tuple[int, int], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": {str(outbox_id): count for outbox_id, count in self.attempts},
            "misconfigured": self.misconfigured,
            "pending": self.pending,
            "sent": self.sent,
            "skipped_legacy": self.skipped_legacy,
            "status": self.status,
            "would_send": list(self.would_send),
        }


def load_handoff(public_config: PublicConfig, environ: Mapping[str, str]) -> HandoffGate:
    """Read the private handoff flag. The returned object has no secrets and no paths."""
    delivery = public_config.document.get("delivery")
    if not isinstance(delivery, Mapping):
        return HandoffGate("outbox_only", False, False, True, "sender_not_configured")
    mode = delivery.get("mode")
    if mode not in {"outbox_only", "configured_sender"}:
        return HandoffGate("outbox_only", False, False, True, "sender_not_configured")
    if mode != "configured_sender":
        return HandoffGate("outbox_only", False, False, False, "send_disabled")
    env_name = delivery.get("profile_env")
    if not isinstance(env_name, str) or not env_name:
        return HandoffGate(mode, False, False, True, "sender_not_configured")
    raw = environ.get(env_name)
    if not isinstance(raw, str) or not raw.strip():
        return HandoffGate(mode, False, False, True, "sender_not_configured")
    path = Path(raw).expanduser()
    try:
        if path_inside_work_tree(path):
            return HandoffGate(mode, False, False, True, "sender_not_configured")
    except ConfigError:
        return HandoffGate(mode, False, False, True, "sender_not_configured")
    if not path.is_file():
        return HandoffGate(mode, False, False, True, "sender_not_configured")
    mode_bits = file_permission_bits(path)
    if mode_bits is not None and mode_bits & 0o077:
        return HandoffGate(mode, False, True, True, "sender_not_configured")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HandoffGate(mode, False, True, True, "sender_not_configured")
    if not isinstance(document, dict):
        return HandoffGate(mode, False, True, True, "sender_not_configured")
    flagged = document.get("single_sender_handoff") is True
    if not flagged:
        return HandoffGate(mode, False, True, False, "send_disabled")
    return HandoffGate(mode, True, True, False, "ready")


def deliver(
    database: Database,
    tenant: str,
    *,
    mode: str,
    sender: Sender | None,
    handoff: HandoffGate,
    clock: Clock,
    owner_id: str | None = None,
    lease_seconds: int = _LEASE_SECONDS,
) -> DeliveryReport:
    """Send pending outbox rows, or report them, according to the handoff gate."""
    owner = owner_id or str(uuid.uuid4())
    rows = database.outbox_for(tenant)
    pending = [row for row in rows if row["status"] == "pending"]
    if mode == "no_send":
        would, skipped = _classify(database, tenant, pending)
        return DeliveryReport("no_send", 0, len(pending), skipped, tuple(would), False, _attempts(database, tenant))
    if handoff.public_mode != "configured_sender":
        return DeliveryReport("send_disabled", 0, len(pending), 0, (), False, _attempts(database, tenant))
    if handoff.misconfigured or handoff.reason == "sender_not_configured":
        return DeliveryReport("sender_not_configured", 0, len(pending), 0, (), True, _attempts(database, tenant))
    if not handoff.single_sender_handoff:
        return DeliveryReport("send_disabled", 0, len(pending), 0, (), False, _attempts(database, tenant))
    if sender is None:
        return DeliveryReport("sender_not_configured", 0, len(pending), 0, (), True, _attempts(database, tenant))
    now = int(clock())
    if not _acquire(database, tenant, owner, now, lease_seconds):
        return DeliveryReport("lease_held", 0, len(pending), 0, (), False, _attempts(database, tenant))
    sent = 0
    skipped = 0
    lost = False
    try:
        for row in database.outbox_for(tenant):
            if row["status"] != "pending":
                continue
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            if _legacy(database, tenant, payload):
                _set_status(database, tenant, int(row["id"]), "suppressed_legacy")
                skipped += 1
                continue
            # Renew before every send; if another holder took the lease, stop instead of double-sending.
            if not _acquire(database, tenant, owner, int(clock()), lease_seconds):
                lost = True
                break
            message = _message(payload)
            try:
                sender.send(message)
            except Exception as exc:
                _note_failure(database, tenant, int(row["id"]), redact(str(exc)))
                continue
            if _set_status(database, tenant, int(row["id"]), "sent"):
                sent += 1
    finally:
        _release(database, tenant, owner)
    left = sum(1 for row in database.outbox_for(tenant) if row["status"] == "pending")
    status = "lease_lost" if lost else "sent" if sent else "pending"
    return DeliveryReport(status, sent, left, skipped, (), False, _attempts(database, tenant))


def _classify(database: Database, tenant: str, pending: list[dict[str, Any]]) -> tuple[list[str], int]:
    would: list[str] = []
    skipped = 0
    for row in pending:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        if _legacy(database, tenant, payload):
            skipped += 1
            continue
        key = payload.get("dedup_key")
        if isinstance(key, str):
            would.append(key)
    return would, skipped


def _legacy(database: Database, tenant: str, payload: Mapping[str, Any]) -> bool:
    network = payload.get("network")
    tx_id = payload.get("tx_id")
    if not isinstance(network, str) or not isinstance(tx_id, str) or not tx_id:
        return False
    return is_legacy_alerted(database, tenant, network, tx_id)


def _message(payload: Mapping[str, Any]) -> dict[str, Any]:
    kept = {}
    for key in ("dedup_key", "kind", "network", "wallet_id", "identity", "valuation_unknown", "informational", "gross", "net", "tx_id"):
        if key in payload:
            kept[key] = payload[key]
    return kept


def _acquire(database: Database, tenant: str, owner: str, now: int, lease_seconds: int) -> bool:
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        row = database.conn.execute(
            "SELECT owner_id, expires_at FROM delivery_leases WHERE tenant_id=?",
            (tenant,),
        ).fetchone()
        if row is not None:
            try:
                expires = int(row["expires_at"])
            except (TypeError, ValueError):
                expires = 0
            if expires > now and row["owner_id"] != owner:
                database.conn.rollback()
                return False
        database.conn.execute(
            """
            INSERT INTO delivery_leases(tenant_id, owner_id, acquired_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
              owner_id=excluded.owner_id,
              acquired_at=excluded.acquired_at,
              expires_at=excluded.expires_at
            """,
            (tenant, owner, str(now), str(now + lease_seconds)),
        )
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise
    return True


def _release(database: Database, tenant: str, owner: str) -> None:
    with database.conn:
        database.conn.execute(
            "DELETE FROM delivery_leases WHERE tenant_id=? AND owner_id=?",
            (tenant, owner),
        )


def _set_status(database: Database, tenant: str, outbox_id: int, status: str) -> bool:
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.conn.execute(
            "UPDATE outbox SET status=? WHERE id=? AND tenant_id=? AND status='pending'",
            (status, outbox_id, tenant),
        )
        changed = bool(cursor.rowcount)
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise
    return changed


def _note_failure(database: Database, tenant: str, outbox_id: int, error: str) -> None:
    safe = redact(error)[:240]
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        current = database.conn.execute(
            "SELECT attempts FROM delivery_attempts WHERE tenant_id=? AND outbox_id=?",
            (tenant, outbox_id),
        ).fetchone()
        attempts = 1 if current is None else int(current["attempts"]) + 1
        database.conn.execute(
            """
            INSERT INTO delivery_attempts(tenant_id, outbox_id, attempts, last_error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, outbox_id) DO UPDATE SET
              attempts=excluded.attempts,
              last_error=excluded.last_error
            """,
            (tenant, outbox_id, attempts, safe),
        )
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise


def _attempts(database: Database, tenant: str) -> tuple[tuple[int, int], ...]:
    rows = database.conn.execute(
        "SELECT outbox_id, attempts FROM delivery_attempts WHERE tenant_id=? ORDER BY outbox_id",
        (tenant,),
    ).fetchall()
    return tuple((int(row["outbox_id"]), int(row["attempts"])) for row in rows)

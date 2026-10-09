"""Outbox delivery gate.

Default mode is ``outbox_only``. An injected sender still uses the single-owner
lease. A Telegram profile sends only after an explicit opt-in. Provider
acceptance is not a read receipt. Ambiguous results stay ``unknown`` and are
not resent. This module does not sign or broadcast a transaction.
"""

from __future__ import annotations

import json
import re
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
from guardian.errors import ConfigError, GuardianError
from guardian.jsonschema_lite import SchemaError, ValidationError, validate
from guardian.legacy_state import is_legacy_alerted
from guardian.resources import resource_path
from guardian.security.redact import redact
from guardian.storage import Database

Clock = Callable[[], float]
_LEASE_SECONDS = 300
CANARY_STATUS = "NOT_RUN"
_PROFILE_VERSION = "guardian.delivery-profile.v1"
_INFLIGHT = "wg-inflight:"
_RETRY = "wg-retry-after:"
_HTTP_REASON = re.compile(r"^http_[45][0-9][0-9]$")
_KNOWN_REASONS = frozenset({
    "connection_reset_after_write",
    "missing_message_id",
    "provider_accepted",
    "rate_limited",
    "redirect_refused",
    "restart_after_inflight",
    "send_failed",
    "timeout_after_write",
    "upstream_error",
    "wrong_recipient",
})


class Sender(Protocol):
    """Injected notification sender. A return value of None means the handoff succeeded."""

    def send(self, message: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class HandoffGate:
    public_mode: str
    single_sender_handoff: bool
    profile_present: bool
    misconfigured: bool
    reason: str
    channel: str | None = None
    opt_in: bool = False
    owner_id: str | None = None
    token_env: str | None = None
    event_watermark: int | None = None


@dataclass(frozen=True)
class TelegramChannel:
    """Chat id stays off HandoffGate so a logged gate cannot print the recipient."""

    chat_id: str
    token_env: str
    owner_id: str
    event_watermark: int | None = None

    def __repr__(self) -> str:
        return "TelegramChannel(redacted)"


@dataclass(frozen=True)
class DeliveryReport:
    status: str
    sent: int
    pending: int
    skipped_legacy: int
    would_send: tuple[str, ...]
    misconfigured: bool
    attempts: tuple[tuple[int, int], ...] = ()
    message_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": {str(outbox_id): count for outbox_id, count in self.attempts},
            "canary": CANARY_STATUS,
            "misconfigured": self.misconfigured,
            "pending": self.pending,
            "provider_message_ids": list(self.message_ids),
            "sent": self.sent,
            "skipped_legacy": self.skipped_legacy,
            "status": self.status,
            "would_send": list(self.would_send),
        }


def load_handoff(public_config: PublicConfig, environ: Mapping[str, str]) -> HandoffGate:
    """Read the private handoff flag. The returned object has no secrets and no paths."""
    opened = _open_profile(public_config, environ)
    if isinstance(opened, HandoffGate):
        return opened
    mode, document = opened
    if _is_telegram_profile(document):
        return _telegram_gate(mode, document, environ)
    flagged = document.get("single_sender_handoff") is True
    if not flagged:
        return HandoffGate(mode, False, True, False, "send_disabled")
    return HandoffGate(mode, True, True, False, "ready")


def read_telegram_channel(public_config: PublicConfig, environ: Mapping[str, str]) -> TelegramChannel | None:
    """Return the opted-in chat, or None when Telegram must not send."""
    opened = _open_profile(public_config, environ)
    if isinstance(opened, HandoffGate):
        return None
    mode, document = opened
    if not _is_telegram_profile(document):
        return None
    gate = _telegram_gate(mode, document, environ)
    chat_id = document.get("chat_id")
    if gate.reason != "ready" or not gate.opt_in or not gate.owner_id or not gate.token_env:
        return None
    if not isinstance(chat_id, str) or not chat_id:
        return None
    return TelegramChannel(
        chat_id=chat_id,
        token_env=gate.token_env,
        owner_id=gate.owner_id,
        event_watermark=gate.event_watermark,
    )


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
    rows = database.outbox_for(tenant)
    pending = [row for row in rows if row["status"] == "pending"]
    if mode == "no_send":
        would, skipped = _classify(database, tenant, pending)
        return _report("no_send", 0, len(pending), skipped, tuple(would), False, database, tenant)
    if handoff.public_mode != "configured_sender":
        return _report("send_disabled", 0, len(pending), 0, (), False, database, tenant)
    if handoff.misconfigured or handoff.reason == "sender_not_configured":
        return _report("sender_not_configured", 0, len(pending), 0, (), True, database, tenant)
    if handoff.channel == "telegram" and handoff.opt_in is not True:
        return _report("send_disabled", 0, len(pending), 0, (), False, database, tenant)
    if not handoff.single_sender_handoff:
        return _report("send_disabled", 0, len(pending), 0, (), False, database, tenant)
    if handoff.channel == "telegram" and handoff.owner_id:
        if owner_id is not None and owner_id != handoff.owner_id:
            return _report("owner_refused", 0, len(pending), 0, (), False, database, tenant)
        owner = handoff.owner_id
    else:
        owner = owner_id or str(uuid.uuid4())
    if sender is None:
        return _report("sender_not_configured", 0, len(pending), 0, (), True, database, tenant)
    now = int(clock())
    if not _acquire(database, tenant, owner, now, lease_seconds):
        return _report("lease_held", 0, len(pending), 0, (), False, database, tenant)
    sent = 0
    accepted = 0
    skipped = 0
    unknown_count = 0
    permanent = 0
    lost = False
    message_ids: list[str] = []
    tracks = getattr(sender, "tracks_inflight", False) is True
    try:
        for row in database.outbox_for(tenant):
            if row["status"] != "pending":
                continue
            outbox_id = int(row["id"])
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            if _legacy(database, tenant, payload):
                if _set_status(database, tenant, outbox_id, "suppressed_legacy"):
                    skipped += 1
                continue
            if (
                handoff.channel == "telegram"
                and handoff.event_watermark is not None
                and outbox_id <= handoff.event_watermark
            ):
                if _set_status(database, tenant, outbox_id, "suppressed_watermark"):
                    skipped += 1
                continue
            if tracks:
                marker = _marker(database, tenant, outbox_id)
                if marker.startswith(_INFLIGHT):
                    if _remember_unknown(database, tenant, outbox_id, "restart_after_inflight"):
                        unknown_count += 1
                    continue
                if marker.startswith(_RETRY):
                    until = _retry_until(marker)
                    if until is not None and int(clock()) < until:
                        continue
            if not _acquire(database, tenant, owner, int(clock()), lease_seconds):
                lost = True
                break
            if tracks:
                _write_attempt(database, tenant, outbox_id, f"{_INFLIGHT}{uuid.uuid4().hex}", increment=True)
            message = _message(payload)
            try:
                result = sender.send(message)
            except GuardianError as exc:
                _note_failure(database, tenant, outbox_id, redact(str(exc)))
                continue
            except Exception as exc:
                if tracks:
                    if _remember_unknown(database, tenant, outbox_id, redact(str(exc)) or "send_failed"):
                        unknown_count += 1
                    continue
                _note_failure(database, tenant, outbox_id, redact(str(exc)))
                continue
            kind = _outcome_kind(result)
            if kind == "legacy":
                if _set_status(database, tenant, outbox_id, "sent"):
                    sent += 1
                continue
            if kind == "provider_accepted":
                message_id = _clean_message_id(getattr(result, "message_id", None))
                if message_id is None:
                    if _remember_unknown(database, tenant, outbox_id, "missing_message_id"):
                        unknown_count += 1
                    continue
                if _set_status(database, tenant, outbox_id, "provider_accepted"):
                    accepted += 1
                    message_ids.append(message_id)
                    _write_attempt(database, tenant, outbox_id, f"provider_accepted:{message_id}", increment=False)
                    _record_receipt(database, tenant, outbox_id, "provider_accepted", "provider_accepted", message_id)
                else:
                    _record_receipt(database, tenant, outbox_id, "provider_accepted", "status_not_pending", message_id)
                continue
            if kind == "failed_permanent":
                reason = _safe_reason(getattr(result, "reason", ""))
                if _set_status(database, tenant, outbox_id, "failed_permanent"):
                    permanent += 1
                    _write_attempt(database, tenant, outbox_id, f"failed_permanent:{reason}", increment=False)
                    _record_receipt(database, tenant, outbox_id, "failed_permanent", reason, None)
                break
            if kind == "unknown":
                if _remember_unknown(database, tenant, outbox_id, _safe_reason(getattr(result, "reason", ""))):
                    unknown_count += 1
                continue
            if kind == "retry_after":
                until = int(clock()) + _clean_seconds(getattr(result, "retry_after_seconds", None))
                _write_attempt(database, tenant, outbox_id, f"{_RETRY}{until}", increment=False)
                _record_receipt(database, tenant, outbox_id, "retry_after", "rate_limited", None)
                break
            if tracks:
                if _remember_unknown(database, tenant, outbox_id, "send_failed"):
                    unknown_count += 1
            else:
                _note_failure(database, tenant, outbox_id, "unrecognized_result")
    finally:
        _release(database, tenant, owner)
    left = sum(1 for row in database.outbox_for(tenant) if row["status"] == "pending")
    if lost:
        status = "lease_lost"
    elif accepted:
        status = "provider_accepted"
    elif sent:
        status = "sent"
    elif unknown_count and left == 0:
        status = "unknown"
    elif permanent and left == 0:
        status = "failed_permanent"
    else:
        status = "pending"
    return _report(status, sent + accepted, left, skipped, (), False, database, tenant, tuple(message_ids))


def resolve_unknown(database: Database, tenant: str, outbox_id: int, *, action: str) -> dict[str, Any]:
    """Operator acknowledgement. This does not send and is never called on restart."""
    if action != "acknowledge":
        raise ConfigError("resolution_not_allowed")
    row = database.conn.execute(
        "SELECT status FROM outbox WHERE id=? AND tenant_id=?",
        (outbox_id, tenant),
    ).fetchone()
    if row is None:
        return {"canary": CANARY_STATUS, "changed": False, "resent": False, "status": "missing"}
    if row["status"] != "unknown":
        return {"canary": CANARY_STATUS, "changed": False, "resent": False, "status": row["status"]}
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.conn.execute(
            "UPDATE outbox SET status='resolved_acknowledged' WHERE id=? AND tenant_id=? AND status='unknown'",
            (outbox_id, tenant),
        )
        changed = bool(cursor.rowcount)
        database.conn.commit()
    except Exception:
        database.conn.rollback()
        raise
    if changed:
        _record_receipt(database, tenant, outbox_id, "resolved_acknowledged", "operator_acknowledge", None)
    return {
        "canary": CANARY_STATUS,
        "changed": changed,
        "resent": False,
        "status": "resolved_acknowledged" if changed else "unknown",
    }


def delivery_receipts(database: Database, tenant: str) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        "SELECT payload FROM deliveries WHERE tenant_id=? ORDER BY id",
        (tenant,),
    ).fetchall()
    found: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload"])
        if isinstance(payload, dict):
            found.append(payload)
    return found


def _report(
    status: str,
    sent: int,
    pending: int,
    skipped: int,
    would: tuple[str, ...],
    misconfigured: bool,
    database: Database,
    tenant: str,
    message_ids: tuple[str, ...] = (),
) -> DeliveryReport:
    return DeliveryReport(status, sent, pending, skipped, would, misconfigured, _attempts(database, tenant), message_ids)


def _open_profile(public_config: PublicConfig, environ: Mapping[str, str]) -> tuple[str, dict[str, Any]] | HandoffGate:
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
    return mode, document


def _is_telegram_profile(document: Mapping[str, Any]) -> bool:
    return document.get("channel") == "telegram" or document.get("schema_version") == _PROFILE_VERSION


def _telegram_gate(mode: str, document: dict[str, Any], environ: Mapping[str, str]) -> HandoffGate:
    if not _profile_valid(document):
        return HandoffGate(mode, False, True, True, "sender_not_configured")
    opt_in = document.get("opt_in") is True
    flagged = document.get("single_sender_handoff") is True
    owner = document.get("owner_id") if isinstance(document.get("owner_id"), str) else None
    token_env = document.get("token_env") if isinstance(document.get("token_env"), str) else None
    watermark = document.get("event_watermark")
    if isinstance(watermark, bool) or not isinstance(watermark, int):
        watermark = None
    if not opt_in:
        return HandoffGate(
            mode, flagged, True, False, "opt_in_required",
            channel="telegram", opt_in=False, owner_id=owner, token_env=token_env, event_watermark=watermark,
        )
    if not flagged:
        return HandoffGate(
            mode, False, True, False, "send_disabled",
            channel="telegram", opt_in=True, owner_id=owner, token_env=token_env, event_watermark=watermark,
        )
    secret = environ.get(token_env or "")
    if not isinstance(secret, str) or not secret.strip():
        return HandoffGate(
            mode, True, True, True, "sender_not_configured",
            channel="telegram", opt_in=True, owner_id=owner, token_env=token_env, event_watermark=watermark,
        )
    return HandoffGate(
        mode, True, True, False, "ready",
        channel="telegram", opt_in=True, owner_id=owner, token_env=token_env, event_watermark=watermark,
    )


def _profile_valid(document: dict[str, Any]) -> bool:
    try:
        schema = json.loads(resource_path("schemas", "delivery-profile.schema.json").read_text(encoding="utf-8"))
        validate(document, schema)
    except (OSError, json.JSONDecodeError, SchemaError, ValidationError, ValueError):
        return False
    return True


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


def _write_attempt(database: Database, tenant: str, outbox_id: int, marker: str, *, increment: bool) -> None:
    safe = redact(marker)[:240]
    database.conn.execute("BEGIN IMMEDIATE")
    try:
        current = database.conn.execute(
            "SELECT attempts FROM delivery_attempts WHERE tenant_id=? AND outbox_id=?",
            (tenant, outbox_id),
        ).fetchone()
        if current is None:
            attempts = 1
        else:
            attempts = int(current["attempts"]) + (1 if increment else 0)
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


def _marker(database: Database, tenant: str, outbox_id: int) -> str:
    row = database.conn.execute(
        "SELECT last_error FROM delivery_attempts WHERE tenant_id=? AND outbox_id=?",
        (tenant, outbox_id),
    ).fetchone()
    if row is None or not isinstance(row["last_error"], str):
        return ""
    return row["last_error"]


def _remember_unknown(database: Database, tenant: str, outbox_id: int, reason: str) -> bool:
    safe = _safe_reason(reason)
    changed = _set_status(database, tenant, outbox_id, "unknown")
    if changed:
        _write_attempt(database, tenant, outbox_id, f"unknown:{safe}", increment=False)
        _record_receipt(database, tenant, outbox_id, "unknown", safe, None)
    return changed


def _record_receipt(
    database: Database,
    tenant: str,
    outbox_id: int,
    status: str,
    reason: str,
    message_id: str | None,
) -> None:
    body: dict[str, Any] = {
        "canary": CANARY_STATUS,
        "channel": "telegram",
        "outbox_id": outbox_id,
        "reason": _safe_reason(reason),
        "status": status,
    }
    if message_id is not None:
        body["message_id"] = message_id
    with database.conn:
        database.conn.execute(
            "INSERT INTO deliveries(tenant_id, payload) VALUES (?, ?)",
            (tenant, json.dumps(body, ensure_ascii=True, sort_keys=True)),
        )


def _attempts(database: Database, tenant: str) -> tuple[tuple[int, int], ...]:
    rows = database.conn.execute(
        "SELECT outbox_id, attempts FROM delivery_attempts WHERE tenant_id=? ORDER BY outbox_id",
        (tenant,),
    ).fetchall()
    return tuple((int(row["outbox_id"]), int(row["attempts"])) for row in rows)


def _outcome_kind(result: Any) -> str:
    if result is None:
        return "legacy"
    status = getattr(result, "status", None)
    if status in {"provider_accepted", "failed_permanent", "unknown", "retry_after"}:
        return str(status)
    return "invalid"


def _clean_message_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _clean_seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    if value < 1:
        return 1
    if value > 86_400:
        return 86_400
    return value


def _safe_reason(value: Any) -> str:
    if not isinstance(value, str):
        return "send_failed"
    cleaned = redact(value).strip()[:120]
    if cleaned in _KNOWN_REASONS or _HTTP_REASON.fullmatch(cleaned):
        return cleaned
    if cleaned:
        return "send_failed"
    return "send_failed"


def _retry_until(marker: str) -> int | None:
    raw = marker[len(_RETRY):]
    if raw.isdigit():
        return int(raw)
    return None

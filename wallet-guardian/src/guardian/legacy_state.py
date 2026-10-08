"""Read a legacy private JSON state file into cursors and dedup.

The import does not create snapshots, alerts, or outbox rows, and it does not
modify the caller's document. The same bytes imported again are a no-op.
Missing fields write nothing; they are not stored as zero.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from guardian.configuration import AccountRecord, AccountsDocument
from guardian.errors import ConfigError
from guardian.identities import IdentityError, parse_address
from guardian.storage import Database

SOURCE_KIND = "private_json_v1"
LEGACY_CHAINS = {
    "eth": "ethereum-mainnet",
    "bsc": "bsc-mainnet",
    "arb": "arbitrum-one",
    "polygon": "polygon-pos",
    "base": "base-mainnet",
    "op": "optimism-mainnet",
}
_SKIPPED = ("_note", "last_run_taipei", "sources", "ignored_spam", "news_checked", "runs")
_HANDLED = set(_SKIPPED) | {"nonces", "latest_seen", "alerted", "bsc_last_block"}
_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LATEST = re.compile(r"^([^_]+)_([^_]+)_(.+)$")


def import_legacy_state(
    db: Database,
    tenant_id: str,
    legacy: dict[str, Any],
    *,
    tag_map: dict[str, str],
    accounts: AccountsDocument | list[AccountRecord],
) -> dict[str, Any]:
    """Import one legacy document in a single transaction. The input dict is not modified."""
    if not isinstance(legacy, dict):
        raise ConfigError("legacy state must be an object")
    if not isinstance(tag_map, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in tag_map.items()):
        raise ConfigError("tag_map must map tags to wallet ids")
    try:
        encoded = json.dumps(legacy, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ConfigError("legacy state is not valid json") from None
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    payload = json.loads(encoded)
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        existing = db.conn.execute(
            "SELECT report FROM legacy_imports WHERE tenant_id=? AND source_kind=? AND source_sha256=?",
            (tenant_id, SOURCE_KIND, digest),
        ).fetchone()
        if existing is not None:
            db.conn.rollback()
            report = json.loads(existing["report"])
            return {**report, "noop": True}
        report = _apply(db, tenant_id, payload, tag_map, accounts, digest)
        db.conn.execute(
            """INSERT INTO legacy_imports(tenant_id, source_kind, source_sha256, imported_at, report)
               VALUES (?, ?, ?, ?, ?)""",
            (tenant_id, SOURCE_KIND, digest, datetime.now(timezone.utc).isoformat(), _encode(report)),
        )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    return report


def is_legacy_alerted(db: Database, tenant_id: str, network: str, tx_hash: str) -> bool:
    if not isinstance(tenant_id, str) or not isinstance(network, str) or not isinstance(tx_hash, str):
        return False
    row = db.conn.execute(
        """SELECT 1 FROM legacy_alert_suppressions
           WHERE tenant_id=? AND network=? AND tx_hash=?""",
        (tenant_id, network, tx_hash.lower()),
    ).fetchone()
    return row is not None


def _apply(
    db: Database,
    tenant_id: str,
    legacy: dict[str, Any],
    tag_map: dict[str, str],
    accounts: AccountsDocument | list[AccountRecord],
    digest: str,
) -> dict[str, Any]:
    before = _counts(db, tenant_id)
    unmapped: list[dict[str, Any]] = []
    dedup_written = 0
    suppressions_written = 0
    cursors_written = 0
    if "alerted" in legacy:
        rows = legacy["alerted"]
        if not isinstance(rows, list):
            unmapped.append({"field": "alerted", "reason": "bad_type"})
        else:
            for entry in rows:
                wrote_dedup, wrote_suppression = _import_alert(db, tenant_id, entry, tag_map, unmapped)
                dedup_written += int(wrote_dedup)
                suppressions_written += int(wrote_suppression)
    if "latest_seen" in legacy:
        cursors_written += _import_latest(db, tenant_id, legacy["latest_seen"], tag_map, digest, unmapped)
    if "nonces" in legacy:
        cursors_written += _import_nonces(db, tenant_id, legacy["nonces"], accounts, digest, unmapped)
    if "bsc_last_block" in legacy:
        cursors_written += _import_block(db, tenant_id, legacy["bsc_last_block"], digest, unmapped)
    for key in legacy:
        if key not in _HANDLED:
            unmapped.append({"field": str(key), "reason": "unknown_field"})
    after = _counts(db, tenant_id)
    return {
        "source_kind": SOURCE_KIND,
        "source_sha256": digest,
        "noop": False,
        "alert_dedup_written": dedup_written,
        "suppressions_written": suppressions_written,
        "cursors_written": cursors_written,
        "snapshots_created": after["snapshots"] - before["snapshots"],
        "alerts_created": after["alerts"] - before["alerts"],
        "outbox_created": after["outbox"] - before["outbox"],
        "events_created": after["events"] - before["events"],
        "recognized_unimported": [key for key in _SKIPPED if key in legacy],
        "unmapped": unmapped,
    }


def _import_alert(db: Database, tenant_id: str, entry: Any, tag_map: dict[str, str], unmapped: list[dict[str, Any]]) -> tuple[bool, bool]:
    if not isinstance(entry, str):
        unmapped.append({"field": "alerted", "reason": "unparseable"})
        return False, False
    legacy_key = "legacy:" + hashlib.sha256(entry.encode("utf-8")).hexdigest()
    parsed = _parse_alert(entry)
    network = None
    tx_hash = None
    wallet_id = None
    if parsed is None:
        unmapped.append({"field": "alerted", "reason": "unparseable", "entry": _show(entry)})
    else:
        chain = LEGACY_CHAINS.get(parsed["chain"])
        if chain is None:
            unmapped.append({"field": "alerted", "reason": "unknown_chain", "entry": _show(entry)})
        else:
            network = chain
        if parsed["tag"] not in tag_map:
            unmapped.append({"field": "alerted", "reason": "unknown_wallet_tag", "entry": _show(entry)})
        else:
            wallet_id = tag_map[parsed["tag"]]
        if _TX.fullmatch(parsed["tx"]) is None:
            unmapped.append({"field": "alerted", "reason": "unparseable", "entry": _show(entry)})
        else:
            tx_hash = parsed["tx"].lower()
    dedup = db.conn.execute(
        "INSERT OR IGNORE INTO alert_dedup(tenant_id, dedup_key) VALUES (?, ?)",
        (tenant_id, legacy_key),
    )
    suppression = db.conn.execute(
        """INSERT OR IGNORE INTO legacy_alert_suppressions(tenant_id, network, tx_hash, wallet_id, legacy_key)
           VALUES (?, ?, ?, ?, ?)""",
        (tenant_id, network, tx_hash, wallet_id, legacy_key),
    )
    return bool(dedup.rowcount), bool(suppression.rowcount)


def _parse_alert(entry: str) -> dict[str, str] | None:
    head = entry.split(" ", 1)[0]
    parts = head.split(":")
    if len(parts) != 3 or any(part == "" for part in parts):
        return None
    return {"chain": parts[0], "tag": parts[1], "tx": parts[2]}


def _import_latest(db: Database, tenant_id: str, latest: Any, tag_map: dict[str, str], digest: str, unmapped: list[dict[str, Any]]) -> int:
    if not isinstance(latest, dict):
        unmapped.append({"field": "latest_seen", "reason": "bad_type"})
        return 0
    written = 0
    for key, value in latest.items():
        if not isinstance(key, str):
            unmapped.append({"field": "latest_seen", "reason": "unparseable"})
            continue
        match = _LATEST.fullmatch(key)
        if match is None:
            unmapped.append({"field": "latest_seen", "reason": "unparseable", "entry": _show(key)})
            continue
        tag, chain_tag, kind = match.group(1), match.group(2), match.group(3)
        network = LEGACY_CHAINS.get(chain_tag)
        wallet_id = tag_map.get(tag)
        if network is None:
            unmapped.append({"field": "latest_seen", "reason": "unknown_chain", "entry": _show(key)})
            continue
        if wallet_id is None:
            unmapped.append({"field": "latest_seen", "reason": "unknown_wallet_tag", "entry": _show(key)})
            continue
        if not isinstance(value, str) or _DATE.fullmatch(value) is None:
            unmapped.append({"field": "latest_seen", "reason": "bad_date", "entry": _show(key)})
            continue
        body = {
            "provenance": "legacy_import",
            "precision": "date",
            "value": value,
            "network": network,
            "wallet_id": wallet_id,
            "kind": kind,
            "legacy_key": key,
            "source_sha256": digest,
        }
        written += int(_put_cursor(db, tenant_id, f"legacy:latest_seen:{key}", body, unmapped))
    return written


def _import_nonces(db: Database, tenant_id: str, nonces: Any, accounts: AccountsDocument | list[AccountRecord], digest: str, unmapped: list[dict[str, Any]]) -> int:
    if not isinstance(nonces, dict):
        unmapped.append({"field": "nonces", "reason": "bad_type"})
        return 0
    written = 0
    for address, chains in nonces.items():
        wallet_id = _wallet_for_address(address, accounts) if isinstance(address, str) else None
        if wallet_id is None:
            unmapped.append({"field": "nonces", "reason": "unknown_address", "entry": _show(address)})
            continue
        if not isinstance(chains, dict):
            unmapped.append({"field": "nonces", "reason": "bad_type", "entry": _show(address)})
            continue
        for chain_tag, nonce in chains.items():
            network = LEGACY_CHAINS.get(chain_tag) if isinstance(chain_tag, str) else None
            if network is None:
                unmapped.append({"field": "nonces", "reason": "unknown_chain", "entry": _show(chain_tag)})
                continue
            if type(nonce) is not int:
                unmapped.append({"field": "nonces", "reason": "bad_nonce", "entry": _show(chain_tag)})
                continue
            body = {
                "provenance": "legacy_import",
                "precision": "nonce",
                "value": nonce,
                "network": network,
                "wallet_id": wallet_id,
                "chain_tag": chain_tag,
                "source_sha256": digest,
            }
            scope = f"legacy:nonce:{wallet_id}:{chain_tag}"
            written += int(_put_cursor(db, tenant_id, scope, body, unmapped))
    return written


def _import_block(db: Database, tenant_id: str, block: Any, digest: str, unmapped: list[dict[str, Any]]) -> int:
    if type(block) is not int:
        unmapped.append({"field": "bsc_last_block", "reason": "bad_type"})
        return 0
    body = {
        "provenance": "legacy_import",
        "precision": "block",
        "value": block,
        "network": "bsc-mainnet",
        "source_sha256": digest,
    }
    return int(_put_cursor(db, tenant_id, "legacy:bsc_last_block", body, unmapped))


def _put_cursor(db: Database, tenant_id: str, scope: str, body: dict[str, Any], unmapped: list[dict[str, Any]]) -> bool:
    encoded = _encode(body)
    existing = db.conn.execute(
        "SELECT value FROM cursors WHERE tenant_id=? AND scope=?",
        (tenant_id, scope),
    ).fetchone()
    if existing is not None:
        if existing["value"] != encoded:
            unmapped.append({"field": "cursors", "reason": "existing_cursor_preserved", "entry": scope})
        return False
    db.conn.execute(
        "INSERT INTO cursors(scope, tenant_id, value) VALUES (?, ?, ?)",
        (scope, tenant_id, encoded),
    )
    return True


def _wallet_for_address(address: str, accounts: AccountsDocument | list[AccountRecord]) -> str | None:
    try:
        parsed = parse_address("evm", address)
    except IdentityError:
        return None
    for record in _records(accounts):
        if record.family == "evm" and record.normalized.identity_key == parsed.identity_key:
            return record.wallet_id
    return None


def _records(accounts: AccountsDocument | list[AccountRecord]) -> list[AccountRecord]:
    if isinstance(accounts, AccountsDocument):
        return list(accounts.accounts)
    return list(accounts)


def _counts(db: Database, tenant_id: str) -> dict[str, int]:
    queries = {
        "snapshots": "SELECT COUNT(*) AS n FROM snapshots WHERE tenant_id=?",
        "alerts": "SELECT COUNT(*) AS n FROM alerts WHERE tenant_id=?",
        "outbox": "SELECT COUNT(*) AS n FROM outbox WHERE tenant_id=?",
        "events": "SELECT COUNT(*) AS n FROM events WHERE tenant_id=?",
    }
    return {name: int(db.conn.execute(query, (tenant_id,)).fetchone()["n"]) for name, query in queries.items()}


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _show(value: Any) -> str:
    if not isinstance(value, str):
        return type(value).__name__
    if len(value) > 180:
        return value[:180]
    return value

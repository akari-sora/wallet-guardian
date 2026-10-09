"""Read a legacy private JSON state file into cursors and dedup.

The import does not create snapshots, alerts, or outbox rows, and it does not
modify the caller's document. The same bytes imported again are a no-op.
Missing fields write nothing; they are not stored as zero.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# --- L3 shadow migration. Read-only. No signing, no send, no guessed addresses. ---

_FULL_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_FULL_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SHORT_HASH = re.compile(r"^0x[0-9a-fA-F]{1,63}$")
_SINCE_KEY = re.compile(r"^(?P<scope>.+)_since_(?P<hhmm>\d{4})$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

# Description keywords only. A bare "transfer" is not a direction. A block number in
# the text is never copied into position, subject, or a cursor.
_KIND_RULES = (
    ("transfer_out", ("transfer_out", "transfer out", "轉出")),
    ("transfer_in", ("transfer_in", "transfer in", "轉入")),
    ("approval", ("approval", "approve", "授權")),
)

_SCOPE_NETWORKS = {
    "eth": ("ethereum-mainnet",),
    "bsc": ("bsc-mainnet",),
    "bsc_in": ("bsc-mainnet",),
    "arb": ("arbitrum-one",),
    "polygon": ("polygon-pos",),
    "base": ("base-mainnet",),
    "op": ("optimism-mainnet",),
    "eth_arb_polygon": ("ethereum-mainnet", "arbitrum-one", "polygon-pos"),
}

# The two private poisoning notes are heuristic slots. Real addresses stay on the
# operator machine; a missing full address is never completed from a prefix.
POISON_NOTE_SLOTS = (
    {"slot": "poison-note-1", "spam_type": "address_poisoning", "source": "legacy_heuristic", "confidence": "low"},
    {"slot": "poison-note-2", "spam_type": "address_poisoning", "source": "legacy_heuristic", "confidence": "low"},
)
DUST_RAW_MAX = 10**15
SNAPSHOT_MODE = 0o600


def snapshot_legacy_readonly(source: str | Path, evidence_dir: str | Path, *, taken_at: str) -> dict[str, Any]:
    """Copy one legacy state file without writing, locking, or renaming the source.

    The snapshot is refused when the source bytes or mtime change during the copy.
    The caller retries later. The snapshot file mode is 0600.
    """
    src = Path(source)
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    before = _source_view(src)
    digest = hashlib.sha256(before["data"]).hexdigest()
    name = f"legacy-{digest}.snapshot"
    target = directory / name
    meta_path = directory / f"legacy-{digest}.snapshot.json"
    _write_0600(target, before["data"])
    meta = {
        "mode": SNAPSHOT_MODE,
        "mtime_ns": before["mtime_ns"],
        "sha256": digest,
        "size": before["size"],
        "snapshot_name": name,
        "taken_at": taken_at,
    }
    _write_0600(meta_path, (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    after = _source_view(src)
    if after["data"] != before["data"] or after["mtime_ns"] != before["mtime_ns"] or after["size"] != before["size"]:
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ConfigError("legacy state changed during copy")
    written = target.read_bytes()
    if written != before["data"] or hashlib.sha256(written).hexdigest() != digest:
        target.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ConfigError("legacy state changed during copy")
    return {**meta, "snapshot_path": str(target), "meta_path": str(meta_path)}


def convert_legacy_document(legacy: Mapping[str, Any], labels: Mapping[str, str]) -> dict[str, Any]:
    """Turn alerted strings, latest_seen, and runs into tx-level rows and windows.

    Position and subject stay empty. Block numbers, addresses, and log positions
    are not parsed out of free text.
    """
    if not isinstance(legacy, Mapping):
        raise ConfigError("legacy state must be an object")
    windows = _scan_windows(legacy.get("runs"))
    covered = _coverage_windows(legacy.get("latest_seen"), labels)
    latest = _latest_run_bounds(windows)
    rows: list[dict[str, str]] = []
    alerted = legacy.get("alerted")
    if isinstance(alerted, list):
        for entry in alerted:
            row = _tx_row(entry, labels, latest)
            if row is not None:
                rows.append(row)
    return {
        "common_since": None if latest is None else latest["since"],
        "common_until": None if latest is None else latest["until"],
        "coverage_windows": covered,
        "scan_windows": [item for item in windows if item.get("since")],
        "tx_rows": rows,
    }


def kind_from_description(description: str) -> str:
    """Map a free-text alert description to one keyword kind, or unknown."""
    if not isinstance(description, str) or not description:
        return "unknown"
    folded = description.casefold()
    for kind, needles in _KIND_RULES:
        if any(needle.casefold() in folded for needle in needles):
            return kind
    return "unknown"


def annotate_poison_notes(raw_notes: Any) -> list[dict[str, Any]]:
    """Heuristic labels for the two poisoning slots. Incomplete addresses are not guessed."""
    notes = raw_notes if isinstance(raw_notes, list) else []
    annotated: list[dict[str, Any]] = []
    for index, item in enumerate(notes):
        slot = POISON_NOTE_SLOTS[index] if index < len(POISON_NOTE_SLOTS) else {
            "slot": f"poison-note-{index + 1}",
            "spam_type": "address_poisoning",
            "source": "legacy_heuristic",
            "confidence": "low",
        }
        annotated.append(_one_poison_note(item, slot))
    return annotated


def review_poison_events(raw_notes: Any, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Suppress only repeated dust of one identified spam type.

    A non-dust transfer or an approval from the same sender still alerts.
    Nothing is labelled safe, and one sender is never blanket-ignored.
    """
    notes = annotate_poison_notes(raw_notes)
    seen: set[str] = set()
    decisions: list[dict[str, Any]] = []
    for event in events:
        decision = _poison_decision(notes, event, seen)
        decisions.append(decision)
        if decision["counts_as_seen"] and decision["pattern_key"]:
            seen.add(decision["pattern_key"])
    return {"decisions": decisions, "notes": notes, "safe": False}


def archive_freeform_cursors(legacy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Park prose and short-hash cursors. They never become a block height."""
    archived: list[dict[str, Any]] = []
    raw = legacy.get("freeform_cursors")
    if isinstance(raw, list):
        archived.extend(_archive_cursor(item) for item in raw)
    block = legacy.get("bsc_last_block")
    if isinstance(block, str):
        archived.append(_unmapped("prose_or_short_hash"))
    elif type(block) is int and not isinstance(legacy.get("bsc_cursor"), dict):
        archived.append(_unmapped("height_without_hash"))
    return archived


def verify_bsc_cursor(cursor: Any, block_hash_at: Callable[[int], str | None]) -> dict[str, Any]:
    """Use a BSC cursor only after chain, height, and full hash agree with the RPC.

    A short hash or prose does not yield a block. A mismatched hash is refused.
    """
    if not isinstance(cursor, dict):
        return _unmapped("freeform")
    chain = cursor.get("chain")
    height = cursor.get("height")
    block_hash = cursor.get("hash")
    if isinstance(block_hash, str) and _SHORT_HASH.fullmatch(block_hash) and not _FULL_TX.fullmatch(block_hash):
        return _unmapped("short_or_prose_hash")
    if not isinstance(block_hash, str) or _FULL_TX.fullmatch(block_hash) is None:
        return _unmapped("hash_not_verifiable")
    if chain not in {"bsc", "bsc-mainnet"}:
        return {"derived_block": None, "reason": "chain_mismatch", "status": "refused", "used": False}
    if type(height) is not int or isinstance(height, bool) or height < 0:
        return {"derived_block": None, "reason": "unverified_height", "status": "refused", "used": False}
    observed = block_hash_at(height)
    if not isinstance(observed, str) or observed.lower() != block_hash.lower():
        return {"derived_block": None, "reason": "hash_mismatch", "status": "refused", "used": False}
    return {
        "chain": "bsc-mainnet",
        "derived_block": None,
        "hash": block_hash.lower(),
        "height": height,
        "reason": "chain_height_hash_checked",
        "status": "verified",
        "used": True,
    }


def rebuild_from_verified(verified: Mapping[str, Any], events: Sequence[Mapping[str, Any]], *, overlap: int) -> dict[str, Any]:
    """Replay from a verified height with overlap. Receipt/event ids dedup a reorg."""
    if verified.get("status") != "verified" or verified.get("used") is not True:
        raise ConfigError("rebuild requires a verified cursor")
    if type(overlap) is not int or isinstance(overlap, bool) or overlap < 0:
        raise ConfigError("overlap is not usable")
    height = verified.get("height")
    if type(height) is not int or isinstance(height, bool):
        raise ConfigError("rebuild requires a verified cursor")
    floor = height - overlap
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    for event in events:
        block = event.get("block")
        event_id = event.get("event_id")
        if type(block) is not int or isinstance(block, bool) or block < floor:
            continue
        if not isinstance(event_id, str) or not event_id:
            continue
        if event_id in seen:
            dropped += 1
            continue
        seen.add(event_id)
        chosen.append({
            "block": block,
            "event_id": event_id,
            "tx": event.get("tx") if isinstance(event.get("tx"), str) else "",
        })
    return {
        "duplicates_dropped": dropped,
        "events": chosen,
        "floor": floor,
        "overlap": overlap,
    }


def resume_after_restart(
    *,
    committed_height: int,
    partial_height: int,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """A killed round does not commit. Resume does not skip ahead or double-alert."""
    if type(committed_height) is not int or isinstance(committed_height, bool):
        raise ConfigError("committed cursor is not a height")
    if type(partial_height) is not int or isinstance(partial_height, bool):
        raise ConfigError("partial cursor is not a height")
    used = committed_height
    seen: set[str] = set()
    emitted: list[dict[str, Any]] = []
    duplicates = 0
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        if event_id in seen:
            duplicates += 1
            continue
        seen.add(event_id)
        emitted.append({
            "block": event.get("block"),
            "event_id": event_id,
            "tx": event.get("tx") if isinstance(event.get("tx"), str) else "",
        })
    return {
        "adopted_partial": False,
        "cursor": used,
        "duplicate_alerts": duplicates,
        "events": emitted,
        "partial_height": partial_height,
        "skipped": False,
    }


def _source_view(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
        data = path.read_bytes()
    except OSError:
        raise ConfigError("legacy state was not found") from None
    return {"data": data, "mtime_ns": info.st_mtime_ns, "size": info.st_size}


def _write_0600(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0), SNAPSHOT_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(path, SNAPSHOT_MODE)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.unlink(missing_ok=True)
            raise ConfigError("legacy snapshot is group or world readable")


def _tx_row(entry: Any, labels: Mapping[str, str], latest: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(entry, str):
        return None
    parsed = _parse_alert(entry)
    if parsed is None or _FULL_TX.fullmatch(parsed["tx"]) is None:
        return None
    network = LEGACY_CHAINS.get(parsed["chain"])
    if network is None:
        return None
    description = entry.split(" ", 1)[1] if " " in entry else ""
    wallet_id = labels.get(parsed["tag"], "")
    observed = ""
    finality = "unknown"
    if latest is not None and network in latest["networks"]:
        observed = latest["until"]
        finality = "confirmed"
    return {
        "evidence_ref": "legacy:alerted:" + hashlib.sha256(entry.encode("utf-8")).hexdigest()[:16],
        "finality": finality,
        "kind": kind_from_description(description),
        "label": parsed["tag"] if _safe_label(parsed["tag"]) else "",
        "network": network,
        "observed_at": observed,
        "position": "",
        "subject": "",
        "tx": parsed["tx"].lower(),
        "wallet_id": wallet_id,
    }


def _safe_label(label: str) -> bool:
    return _FULL_ADDRESS.search(label) is None and "://" not in label and len(label) <= 96


def _scan_windows(runs: Any) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        return []
    found: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        end = _parse_time(run.get("at"))
        if end is None:
            continue
        for key in run:
            if not isinstance(key, str):
                continue
            match = _SINCE_KEY.fullmatch(key)
            if match is None:
                continue
            scope = match.group("scope")
            networks = _SCOPE_NETWORKS.get(scope)
            if networks is None:
                found.append({"key": key, "networks": [], "since": None, "status": "unmapped_scope", "until": None})
                continue
            start = _hhmm_on(end, match.group("hhmm"))
            if start is None:
                found.append({"key": key, "networks": [], "since": None, "status": "bad_hhmm", "until": None})
                continue
            found.append({
                "key": key,
                "networks": list(networks),
                "since": _fmt(start),
                "status": "mapped",
                "until": _fmt(end),
            })
    return found


def _latest_run_bounds(windows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    mapped = [item for item in windows if item.get("since") and item.get("until")]
    if not mapped:
        return None
    until = max(str(item["until"]) for item in mapped)
    same = [item for item in mapped if item["until"] == until]
    networks: set[str] = set()
    for item in same:
        networks.update(item.get("networks") or [])
    return {
        "networks": networks,
        "since": min(str(item["since"]) for item in same),
        "until": until,
    }


def _coverage_windows(latest: Any, labels: Mapping[str, str]) -> list[dict[str, str]]:
    if not isinstance(latest, dict):
        return []
    rows: list[dict[str, str]] = []
    for key, value in latest.items():
        if not isinstance(key, str) or not isinstance(value, str) or _DATE.fullmatch(value) is None:
            continue
        match = _LATEST.fullmatch(key)
        if match is None:
            continue
        tag, chain_tag, kind = match.group(1), match.group(2), match.group(3)
        network = LEGACY_CHAINS.get(chain_tag)
        wallet_id = labels.get(tag, "")
        if network is None or not wallet_id:
            continue
        rows.append({
            "kind": kind,
            "network": network,
            "seen_on": value,
            "wallet_id": wallet_id,
        })
    return rows


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or _ISO.fullmatch(value) is None:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm_on(end: datetime, hhmm: str) -> datetime | None:
    hour = int(hhmm[:2])
    minute = int(hhmm[2:])
    if hour > 23 or minute > 59:
        return None
    start = end.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start > end:
        start -= timedelta(days=1)
    return start


def _one_poison_note(item: Any, slot: Mapping[str, str]) -> dict[str, Any]:
    source = slot["source"]
    confidence = slot["confidence"]
    spam_type = slot["spam_type"]
    chain = ""
    sender = ""
    if isinstance(item, dict):
        if isinstance(item.get("source"), str) and item["source"]:
            source = item["source"]
        if isinstance(item.get("confidence"), str) and item["confidence"]:
            confidence = item["confidence"]
        if isinstance(item.get("spam_type"), str) and item["spam_type"]:
            spam_type = item["spam_type"]
        if isinstance(item.get("chain"), str):
            chain = item["chain"]
        if isinstance(item.get("sender"), str):
            sender = item["sender"].strip()
    complete = _FULL_ADDRESS.fullmatch(sender) is not None
    return {
        "address_status": "complete" if complete else "incomplete",
        "chain": chain,
        "confidence": confidence,
        "label": "address_poisoning",
        "safe": False,
        "sender": sender.lower() if complete else None,
        "slot": slot["slot"],
        "source": source,
        "spam_type": spam_type,
    }


def _poison_decision(notes: Sequence[Mapping[str, Any]], event: Mapping[str, Any], seen: set[str]) -> dict[str, Any]:
    kind = event.get("kind") if isinstance(event.get("kind"), str) else ""
    sender = event.get("sender") if isinstance(event.get("sender"), str) else ""
    chain = event.get("chain") if isinstance(event.get("chain"), str) else ""
    spam_type = event.get("spam_type") if isinstance(event.get("spam_type"), str) else ""
    base = {
        "alert": True,
        "counts_as_seen": False,
        "pattern_key": "",
        "safe": False,
        "suppress": False,
    }
    if kind in {"approval", "approval_set", "approve"}:
        return {**base, "reason": "approval_not_suppressed"}
    if not _event_is_dust(event):
        return {**base, "reason": "not_dust"}
    note = _matching_note(notes, sender, chain, spam_type)
    if note is None:
        return {**base, "reason": "no_matching_poison_note"}
    pattern = f"{sender.lower()}|{chain}|{spam_type}"
    if pattern not in seen:
        return {**base, "counts_as_seen": True, "pattern_key": pattern, "reason": "first_dust_observation"}
    return {
        "alert": False,
        "counts_as_seen": True,
        "pattern_key": pattern,
        "reason": "spam_suppressed_rule",
        "safe": False,
        "suppress": True,
    }


def _matching_note(notes: Sequence[Mapping[str, Any]], sender: str, chain: str, spam_type: str) -> Mapping[str, Any] | None:
    if _FULL_ADDRESS.fullmatch(sender) is None or not chain or not spam_type:
        return None
    folded = sender.lower()
    for note in notes:
        if note.get("sender") != folded:
            continue
        if note.get("chain") != chain or note.get("spam_type") != spam_type:
            continue
        if note.get("address_status") != "complete":
            continue
        return note
    return None


def _event_is_dust(event: Mapping[str, Any]) -> bool:
    if event.get("dust") is True:
        return True
    if event.get("dust") is False:
        return False
    amount = event.get("raw_amount")
    if isinstance(amount, str) and amount.isdigit():
        amount = int(amount)
    if isinstance(amount, bool) or not isinstance(amount, int):
        return False
    return amount == 0 or 0 < amount <= DUST_RAW_MAX


def _archive_cursor(item: Any) -> dict[str, Any]:
    if isinstance(item, str) and _SHORT_HASH.fullmatch(item) and not _FULL_TX.fullmatch(item):
        return _unmapped("short_hash")
    if isinstance(item, str) and _FULL_TX.fullmatch(item):
        return _unmapped("hash_without_verified_height")
    return _unmapped("prose")


def _unmapped(reason: str) -> dict[str, Any]:
    return {"derived_block": None, "reason": reason, "status": "unmapped", "used": False}

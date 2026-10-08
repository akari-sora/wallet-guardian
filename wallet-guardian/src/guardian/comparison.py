"""Shadow report against a legacy monitor_state.json file.

The legacy file is read and never written. Output keys are wallet ids and
masked transaction ids. Raw addresses are not copied out of the file.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from guardian.errors import ConfigError
from guardian.legacy_state import LEGACY_CHAINS, _parse_alert
from guardian.storage import Database

_ADDRESS = re.compile(r"(?i)0x[0-9a-f]{40}")
_LATEST = re.compile(r"^([^_]+)_([^_]+)_(.+)$")


def compare_with_legacy(legacy_state_path: str | Path, database: Database, tenant: str) -> dict[str, Any]:
    """Compare legacy alert keys with the MCM outbox. The file bytes stay put."""
    path = Path(legacy_state_path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError("legacy state was not found") from None
    try:
        legacy = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ConfigError("legacy state is not valid json") from None
    if not isinstance(legacy, dict):
        raise ConfigError("legacy state must be an object")
    labels = _labels(database, tenant)
    legacy_rows, unmapped, legacy_networks = _legacy_rows(legacy, labels)
    mcm_rows, mcm_networks = _mcm_rows(database, tenant)
    legacy_by = {row["tx_key"]: row for row in legacy_rows}
    mcm_by = {row["tx_key"]: row for row in mcm_rows}
    matched = [_public(legacy_by[key]) for key in sorted(set(legacy_by) & set(mcm_by))]
    legacy_only = [_public(legacy_by[key]) for key in sorted(set(legacy_by) - set(mcm_by))]
    mcm_only = [_public(mcm_by[key]) for key in sorted(set(mcm_by) - set(legacy_by))]
    never = sorted(set(mcm_networks) - set(LEGACY_CHAINS.values()))
    return {
        "counts": {
            "legacy_only": len(legacy_only),
            "matched": len(matched),
            "mcm_only": len(mcm_only),
            "unmapped_legacy": len(unmapped),
        },
        "coverage_differences": {
            "legacy_networks": sorted(legacy_networks),
            "mcm_networks": sorted(mcm_networks),
            "networks_legacy_never_checked": never,
        },
        "legacy_only": legacy_only,
        "matched": matched,
        "mcm_only": mcm_only,
        "unmapped_legacy": unmapped,
    }


def _legacy_rows(legacy: dict[str, Any], labels: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    unmapped: list[dict[str, str]] = []
    networks: set[str] = set()
    alerted = legacy.get("alerted")
    if "alerted" in legacy and not isinstance(alerted, list):
        unmapped.append({"reason": "bad_type"})
        alerted = []
    for entry in alerted or []:
        if not isinstance(entry, str):
            unmapped.append({"reason": "unparseable"})
            continue
        parsed = _parse_alert(entry)
        if parsed is None:
            unmapped.append({"reason": "free_text", "entry": _safe(entry)})
            continue
        network = LEGACY_CHAINS.get(parsed["chain"])
        if network is None or not parsed["tx"].startswith("0x") or len(parsed["tx"]) != 66:
            unmapped.append({"reason": "non_standard", "entry": _safe(entry.split(" ", 1)[0])})
            continue
        networks.add(network)
        wallet_id = labels.get(parsed["tag"], "")
        rows.append({
            "label": _safe(parsed["tag"]),
            "network": network,
            "tx": parsed["tx"].lower(),
            "tx_key": f"{network}:{parsed['tx'].lower()}",
            "wallet_id": wallet_id,
        })
    latest = legacy.get("latest_seen")
    if isinstance(latest, dict):
        for key in latest:
            if not isinstance(key, str):
                unmapped.append({"reason": "free_text"})
                continue
            match = _LATEST.fullmatch(key)
            if match is None:
                unmapped.append({"reason": "free_text", "entry": _safe(key)})
                continue
            network = LEGACY_CHAINS.get(match.group(1))
            if network is None:
                unmapped.append({"reason": "non_standard", "entry": _safe(key)})
            else:
                networks.add(network)
    elif "latest_seen" in legacy:
        unmapped.append({"reason": "bad_type"})
    for key in legacy:
        if key not in {"alerted", "latest_seen", "nonces", "bsc_last_block", "_note", "last_run_taipei", "sources", "ignored_spam", "news_checked", "runs"}:
            unmapped.append({"reason": "unknown_field", "entry": _safe(str(key))})
    return rows, unmapped, networks


def _mcm_rows(database: Database, tenant: str) -> tuple[list[dict[str, str]], set[str]]:
    rows: list[dict[str, str]] = []
    networks: set[str] = set()
    for row in database.outbox_for(tenant):
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        network = payload.get("network")
        tx_id = payload.get("tx_id")
        if isinstance(network, str):
            networks.add(network)
        if not isinstance(network, str) or not isinstance(tx_id, str) or not tx_id.startswith("0x"):
            continue
        wallet_id = payload.get("wallet_id") if isinstance(payload.get("wallet_id"), str) else ""
        rows.append({
            "label": "",
            "network": network,
            "tx": tx_id.lower(),
            "tx_key": f"{network}:{tx_id.lower()}",
            "wallet_id": wallet_id,
        })
    for row in database.capability_state_for(tenant):
        network = row.get("network")
        if isinstance(network, str):
            networks.add(network)
    return rows, networks


def _labels(database: Database, tenant: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in database.conn.execute(
        "SELECT label, wallet_id FROM wallet_label_aliases WHERE tenant_id=?",
        (tenant,),
    ):
        found[str(row["label"])] = str(row["wallet_id"])
    return found


def _public(row: dict[str, str]) -> dict[str, str]:
    return {
        "network": row["network"],
        "tx": _mask(row["tx"]),
        "wallet_id": row["wallet_id"],
        "label": row["label"],
    }


def _mask(tx: str) -> str:
    digest = hashlib.sha256(tx.encode("utf-8")).hexdigest()[:12]
    if len(tx) < 12:
        return digest
    return tx[:6] + "…" + tx[-4:] + ":" + digest


def _safe(text: str) -> str:
    if _ADDRESS.search(text) or "://" in text or len(text) > 96:
        return "redacted"
    return text

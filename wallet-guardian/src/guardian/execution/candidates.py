"""Candidate entries from packaged deployments and protocols.

The generator prints candidates. It does not write ``config/execution/`` and it
does not treat ``approved_by_user`` or ``execution_authorized`` as approval.
Keep action-client/src/execution/candidates.mjs on the same steps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_utils import to_checksum_address

from guardian.execution.errors import ExecutionRegistryError
from guardian.execution.loader import load_candidates_text
from guardian.execution.model import (
    EIP1967_IMPLEMENTATION_SLOT,
    PERMISSION_TYPES,
    PROXY_KINDS,
    RULES_VERSION,
    expect_object,
    require_text,
)
from guardian.execution.strict_json import parse_strict_json

_RULE_KEYS = frozenset(
    {
        "protocol_id",
        "also_protocol_ids",
        "role",
        "permission_type",
        "proxy_kind",
        "proxy_kind_basis",
        "requires_slot_file",
        "requires_note_includes",
    }
)
_PERMIT2_KEYS = frozenset({"permission_type", "proxy_kind", "proxy_kind_basis"})


def propose_execution_candidates(root: str | Path) -> dict[str, Any]:
    """Read packaged config under ``root`` and return unapproved candidates."""
    base = Path(root)
    assembled = _assemble(base)
    return load_candidates_text(json.dumps(assembled, ensure_ascii=False))


def _assemble(root: Path) -> dict[str, Any]:
    rules, permit2 = _load_rules(root)
    by_network, by_chain = _chain_index(root)
    observations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    protocols = root / "config" / "protocols"
    if protocols.is_dir():
        for path in sorted(protocols.glob("*.json")):
            _read_protocol(path, root, by_network, by_chain, observations, skipped)
    permit2_dir = root / "config" / "deployments" / "permit2"
    if permit2_dir.is_dir():
        for path in sorted(permit2_dir.glob("*.json")):
            _read_permit2(path, root, by_network, permit2, observations, skipped)
    return _classify(observations, skipped, rules, root)


def _load_rules(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = root / "config" / "execution" / "candidate-rules.json"
    document = parse_strict_json(path.read_text(encoding="utf-8"))
    allowed = frozenset({"schema_version", "eip1967_slot", "eip1967_slot_file", "rules", "permit2"})
    expect_object(document, allowed, allowed, "rules")
    if document["schema_version"] != RULES_VERSION:
        raise ExecutionRegistryError("bad_enum", "rules.schema_version")
    if document["eip1967_slot"] != EIP1967_IMPLEMENTATION_SLOT:
        raise ExecutionRegistryError("proxy_kind_rejected", "eip1967_slot")
    if document["eip1967_slot_file"] != "action-client/src/trusted-lista.mjs":
        raise ExecutionRegistryError("bad_path", "eip1967_slot_file")
    if not isinstance(document["rules"], list):
        raise ExecutionRegistryError("bad_type", "rules.rules")
    rules = [_rule(item, index) for index, item in enumerate(document["rules"])]
    permit2 = expect_object(document["permit2"], _PERMIT2_KEYS, _PERMIT2_KEYS, "rules.permit2")
    if permit2["permission_type"] not in PERMISSION_TYPES or permit2["proxy_kind"] not in PROXY_KINDS:
        raise ExecutionRegistryError("bad_enum", "rules.permit2")
    require_text(permit2["proxy_kind_basis"], "rules.permit2.proxy_kind_basis")
    return rules, permit2


def _rule(value: Any, index: int) -> dict[str, Any]:
    label = f"rules[{index}]"
    if not isinstance(value, dict):
        raise ExecutionRegistryError("bad_type", label)
    for key in value:
        if key not in _RULE_KEYS:
            raise ExecutionRegistryError("unknown_field", f"{label}.{key}")
    for key in ("protocol_id", "role", "permission_type", "proxy_kind", "proxy_kind_basis"):
        if key not in value:
            raise ExecutionRegistryError("missing_field", f"{label}.{key}")
    if value["permission_type"] not in PERMISSION_TYPES or value["proxy_kind"] not in PROXY_KINDS:
        raise ExecutionRegistryError("bad_enum", label)
    also = value.get("also_protocol_ids", [])
    if not isinstance(also, list):
        raise ExecutionRegistryError("bad_type", f"{label}.also_protocol_ids")
    slot = value.get("requires_slot_file", False)
    note = value.get("requires_note_includes")
    if slot is True and note is not None:
        raise ExecutionRegistryError("bad_type", f"{label}.basis")
    if slot is not True and (not isinstance(note, str) or note == ""):
        raise ExecutionRegistryError("missing_field", f"{label}.basis")
    if slot is not True and slot is not False:
        raise ExecutionRegistryError("bad_type", f"{label}.requires_slot_file")
    return value


def _chain_index(root: Path) -> tuple[dict[str, dict[str, Any]], dict[int, str]]:
    by_network: dict[str, dict[str, Any]] = {}
    by_chain: dict[int, str] = {}
    directory = root / "config" / "chains"
    if not directory.is_dir():
        return by_network, by_chain
    for path in sorted(directory.glob("*.json")):
        document = parse_strict_json(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        network = document.get("network")
        family = document.get("family")
        chain_id = document.get("evm_chain_id")
        if not isinstance(network, str) or not isinstance(family, str):
            continue
        if type(chain_id) is not int:
            chain_id = None
        by_network[network] = {"family": family, "chain_id": chain_id, "network_id": network}
        if chain_id is not None:
            if chain_id in by_chain:
                raise ExecutionRegistryError("duplicate_key", f"chain_id:{chain_id}")
            by_chain[chain_id] = network
    return by_network, by_chain


def _read_protocol(
    path: Path,
    root: Path,
    by_network: dict[str, dict[str, Any]],
    by_chain: dict[int, str],
    observations: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> None:
    document = parse_strict_json(path.read_text(encoding="utf-8"))
    relative = path.relative_to(root).as_posix()
    if not isinstance(document, dict):
        skipped.append(_skip(relative, "unrecognized_protocol_file", protocol_id=None))
        return
    if document.get("protocol_id") == "lista-slisbnb" and "proxy" in document and "contracts" not in document:
        _read_legacy(document, relative, by_chain, observations, skipped)
        return
    if "id" not in document or "contracts" not in document:
        skipped.append(_skip(relative, "unrecognized_protocol_file", protocol_id=_text_or_none(document.get("id"))))
        return
    protocol_id = document.get("id")
    version = document.get("version")
    contracts = document.get("contracts")
    if not isinstance(protocol_id, str) or not isinstance(version, str) or not isinstance(contracts, dict):
        skipped.append(_skip(relative, "unrecognized_protocol_file", protocol_id=_text_or_none(protocol_id)))
        return
    for network, roles in contracts.items():
        info = by_network.get(network) if isinstance(network, str) else None
        if not isinstance(roles, dict) or info is None:
            skipped.append(
                _skip(relative, "unknown_network", protocol_id=protocol_id, network_id=_text_or_none(network))
            )
            continue
        for role, entry in roles.items():
            if not isinstance(role, str) or not isinstance(entry, dict):
                skipped.append(
                    _skip(
                        relative,
                        "unrecognized_protocol_file",
                        protocol_id=protocol_id,
                        network_id=network,
                        role=_text_or_none(role),
                    )
                )
                continue
            observations.append(
                _observation(
                    protocol_id=protocol_id,
                    protocol_version=version,
                    network_id=network,
                    info=info,
                    role=role,
                    address=entry.get("address"),
                    notes=entry.get("notes") if isinstance(entry.get("notes"), str) else "",
                    sources=_entry_sources(entry),
                    source_file=relative,
                )
            )


def _read_legacy(
    document: dict[str, Any],
    relative: str,
    by_chain: dict[int, str],
    observations: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> None:
    chain_id = document.get("chain_id")
    network = by_chain.get(chain_id) if type(chain_id) is int else None
    if network is None:
        skipped.append(_skip(relative, "unknown_network", protocol_id="lista-slisbnb"))
        return
    sources = []
    retrieved = document.get("docs_read_at")
    for url in document.get("sources") or []:
        if isinstance(url, str) and isinstance(retrieved, str):
            sources.append({"retrieved": retrieved, "url": url})
    info = {"family": "evm", "chain_id": chain_id, "network_id": network}
    for role in ("proxy", "slisbnb"):
        observations.append(
            _observation(
                protocol_id="lista-slisbnb",
                protocol_version="1",
                network_id=network,
                info=info,
                role=role,
                address=document.get(role),
                notes="",
                sources=list(sources),
                source_file=relative,
            )
        )


def _read_permit2(
    path: Path,
    root: Path,
    by_network: dict[str, dict[str, Any]],
    permit2: dict[str, Any],
    observations: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> None:
    document = parse_strict_json(path.read_text(encoding="utf-8"))
    relative = path.relative_to(root).as_posix()
    if not isinstance(document, dict):
        skipped.append(_skip(relative, "unrecognized_protocol_file", protocol_id="permit2"))
        return
    network = document.get("network")
    info = by_network.get(network) if isinstance(network, str) else None
    if info is None:
        skipped.append(
            _skip(relative, "unknown_network", protocol_id="permit2", network_id=_text_or_none(network))
        )
        return
    sources = []
    if isinstance(document.get("source_url"), str) and isinstance(document.get("retrieved"), str):
        sources.append({"retrieved": document["retrieved"], "url": document["source_url"]})
    observation = _observation(
        protocol_id="permit2",
        protocol_version="1",
        network_id=network,
        info=info,
        role="permit2",
        address=document.get("address"),
        notes="",
        sources=sources,
        source_file=relative,
    )
    observation["rule"] = {
        "protocol_id": "permit2",
        "role": "permit2",
        "permission_type": permit2["permission_type"],
        "proxy_kind": permit2["proxy_kind"],
        "proxy_kind_basis": permit2["proxy_kind_basis"],
        "requires_slot_file": False,
        "requires_note_includes": None,
        "basis": "deployment_file",
    }
    observation["basis_ok"] = True
    observations.append(observation)


def _observation(
    *,
    protocol_id: str,
    protocol_version: str,
    network_id: str,
    info: dict[str, Any],
    role: str,
    address: Any,
    notes: str,
    sources: list[dict[str, str]],
    source_file: str,
) -> dict[str, Any]:
    return {
        "address": address,
        "chain_id": info["chain_id"],
        "family": info["family"],
        "network_id": network_id,
        "notes": notes,
        "official_sources": sources,
        "protocol_id": protocol_id,
        "protocol_version": protocol_version,
        "role": role,
        "rule": None,
        "source_file": source_file,
    }


def _entry_sources(entry: dict[str, Any]) -> list[dict[str, str]]:
    url = entry.get("source_url")
    retrieved = entry.get("retrieved")
    if isinstance(url, str) and isinstance(retrieved, str):
        return [{"retrieved": retrieved, "url": url}]
    return []


def _classify(
    observations: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    classified: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for observation in observations:
        prepared = _prepare_address(observation, skipped)
        if prepared is None:
            continue
        if prepared["family"] != "evm":
            row = _unclassified_row(prepared, "not_evm")
            if row is None:
                skipped.append(
                    _skip(
                        prepared["source_file"],
                        "official_source_missing",
                        protocol_id=prepared["protocol_id"],
                        network_id=prepared["network_id"],
                        role=prepared["role"],
                        address=_text_or_none(prepared["address"]),
                    )
                )
            else:
                unclassified.append(row)
            continue
        rule = _match_rule(rules, prepared["protocol_id"], prepared["role"])
        if rule is None and prepared.get("rule") is not None:
            rule = prepared["rule"]
        if rule is None:
            row = _unclassified_row(prepared, "proxy_kind_not_documented")
        elif not _basis_ok(rule, prepared["notes"], root):
            row = _unclassified_row(prepared, "proxy_basis_missing")
        else:
            prepared["rule"] = rule
            classified.append(prepared)
            continue
        if row is None:
            skipped.append(
                _skip(
                    prepared["source_file"],
                    "official_source_missing",
                    protocol_id=prepared["protocol_id"],
                    network_id=prepared["network_id"],
                    role=prepared["role"],
                    address=prepared["address"] if isinstance(prepared["address"], str) else None,
                )
            )
        else:
            unclassified.append(row)
    deployments, demoted = _merge_classified(classified)
    for item in demoted:
        row = _unclassified_row(item, "duplicate_address_conflict")
        if row is not None:
            unclassified.append(row)
    return {
        "approval": "candidate_unapproved",
        "deployments": deployments,
        "schema_version": "guardian.execution_candidates.v1",
        "skipped": skipped,
        "unclassified": unclassified,
        "user_approved": False,
    }


def _prepare_address(observation: dict[str, Any], skipped: list[dict[str, Any]]) -> dict[str, Any] | None:
    address = observation["address"]
    if observation["family"] != "evm":
        if not isinstance(address, str) or address == "":
            skipped.append(
                _skip(
                    observation["source_file"],
                    "bad_address",
                    protocol_id=observation["protocol_id"],
                    network_id=observation["network_id"],
                    role=observation["role"],
                )
            )
            return None
        return observation
    if not isinstance(address, str):
        skipped.append(
            _skip(
                observation["source_file"],
                "bad_address",
                protocol_id=observation["protocol_id"],
                network_id=observation["network_id"],
                role=observation["role"],
            )
        )
        return None
    try:
        observation["address"] = to_checksum_address(address)
    except ValueError:
        skipped.append(
            _skip(
                observation["source_file"],
                "bad_address",
                protocol_id=observation["protocol_id"],
                network_id=observation["network_id"],
                role=observation["role"],
                address=address,
            )
        )
        return None
    return observation


def _match_rule(rules: list[dict[str, Any]], protocol_id: str, role: str) -> dict[str, Any] | None:
    for rule in rules:
        names = [rule["protocol_id"], *rule.get("also_protocol_ids", [])]
        if protocol_id in names and rule["role"] == role:
            return rule
    return None


def _basis_ok(rule: dict[str, Any], notes: str, root: Path) -> bool:
    if rule.get("requires_slot_file") is True:
        path = root / "action-client" / "src" / "trusted-lista.mjs"
        if not path.is_file():
            return False
        return EIP1967_IMPLEMENTATION_SLOT in path.read_text(encoding="utf-8")
    needle = rule.get("requires_note_includes")
    if isinstance(needle, str) and needle != "":
        return needle in notes
    return rule.get("basis") == "deployment_file"


def _merge_classified(
    classified: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in classified:
        groups.setdefault((item["chain_id"], item["address"].lower()), []).append(item)
    deployments: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for group in groups.values():
        kinds = {(item["rule"]["permission_type"], item["rule"]["proxy_kind"], item["role"]) for item in group}
        if len(kinds) != 1:
            demoted.extend(group)
            continue
        preferred = group[0]
        for item in group[1:]:
            preferred = _prefer(preferred, item)
        sources: dict[str, str] = {}
        for item in group:
            for source in item["official_sources"]:
                current = sources.get(source["url"])
                if current is None or source["retrieved"] > current:
                    sources[source["url"]] = source["retrieved"]
        if not sources:
            demoted.append(preferred)
            continue
        deployments.append(_deployment_from(preferred, sources, sorted({item["source_file"] for item in group})))
    return deployments, demoted


def _prefer(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    primary = left["rule"]["protocol_id"]
    if left["protocol_id"] == primary:
        return left
    if right["protocol_id"] == primary:
        return right
    if "-" in left["protocol_id"] and "-" not in right["protocol_id"]:
        return right
    if "-" in right["protocol_id"] and "-" not in left["protocol_id"]:
        return left
    if left["protocol_id"] <= right["protocol_id"]:
        return left
    return right


def _deployment_from(item: dict[str, Any], sources: dict[str, str], source_files: list[str]) -> dict[str, Any]:
    rule = item["rule"]
    protocol_id = item["protocol_id"]
    return {
        "address": item["address"],
        "approval": "candidate_unapproved",
        "assertions": [],
        "chain_id": item["chain_id"],
        "id": f"candidate:{item['chain_id']}:{protocol_id}:{item['role']}",
        "implementation": None,
        "network_id": item["network_id"],
        "official_sources": [{"retrieved": sources[url], "url": url} for url in sorted(sources)],
        "permission_type": rule["permission_type"],
        "pin_status": "not_read_offline",
        "protocol_id": protocol_id,
        "protocol_version": item["protocol_version"],
        "proxy_kind": rule["proxy_kind"],
        "proxy_kind_basis": rule["proxy_kind_basis"],
        "record_status": "candidate",
        "revoked_at": None,
        "runtime_hash": None,
        "source_block_hash": None,
        "source_block_number": None,
        "source_files": source_files,
        "user_approved": False,
        "valid_until": None,
    }


def _unclassified_row(item: dict[str, Any], reason: str) -> dict[str, Any] | None:
    if not item["official_sources"]:
        return None
    chain_id = None if item["family"] != "evm" else item["chain_id"]
    return {
        "address": item["address"],
        "address_family": item["family"],
        "approval": "candidate_unapproved",
        "chain_id": chain_id,
        "network_id": item["network_id"],
        "official_sources": list(item["official_sources"]),
        "protocol_id": item["protocol_id"],
        "protocol_version": item["protocol_version"],
        "reason": reason,
        "role": item["role"],
        "source_file": item["source_file"],
        "user_approved": False,
    }


def _skip(
    source_file: str,
    reason: str,
    *,
    protocol_id: str | None = None,
    network_id: str | None = None,
    role: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    return {
        "address": address,
        "network_id": network_id,
        "protocol_id": protocol_id,
        "reason": reason,
        "role": role,
        "source_file": source_file,
    }


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None

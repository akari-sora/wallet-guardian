"""Strict loader for config/execution. It does not build or send transactions.

A document can describe deployments and policies. It cannot mark them
user-approved. ``approved_by_user`` is rejected. ``user_approved: true`` is
rejected. Loaded entries always report ``user_approved`` false.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from guardian.execution.errors import ExecutionRegistryError
from guardian.execution.model import (
    ACTIONS,
    ADDRESS_ROLES,
    ADDRESS_TYPES,
    CANDIDATES_VERSION,
    CHAINS_VERSION,
    PERMISSION_TYPES,
    PROXY_KINDS,
    REGISTRY_VERSION,
    RELATIONSHIPS,
    REQUIRED_TRUST_CATEGORIES,
    REQUIRED_TRUST_PATHS,
    SOLIDITY_TYPES,
    TRUST_NOTE,
    TRUST_PATH,
    TRUST_VERSION,
    expect_object,
    reject_inert_approval,
    require_block_hash,
    require_chain_id,
    require_checksum,
    require_date,
    require_hash64,
    require_https_url,
    require_relative_path,
    require_text,
    require_time,
    require_uint_string,
)
from guardian.execution.strict_json import parse_strict_json
from guardian.resources import resource_path

_CHAIN_KEYS = frozenset({"chain_id", "network_id", "status", "revoked_at", "rpc"})
_RPC_KEYS = frozenset({"url", "operator", "source"})
_SOURCE_KEYS = frozenset({"url", "retrieved"})
_IMPL_KEYS = frozenset({"address", "runtime_hash"})
_DEPLOYMENT_KEYS = frozenset(
    {
        "id",
        "chain_id",
        "network_id",
        "address",
        "protocol_id",
        "protocol_version",
        "permission_type",
        "proxy_kind",
        "proxy_kind_basis",
        "implementation",
        "runtime_hash",
        "source_block_number",
        "source_block_hash",
        "pin_status",
        "assertions",
        "valid_until",
        "official_sources",
        "record_status",
        "revoked_at",
        "approval",
        "user_approved",
        "source_files",
    }
)
_POLICY_KEYS = frozenset(
    {
        "id",
        "chain_id",
        "deployment_id",
        "market_id",
        "position_id",
        "asset_deployment_id",
        "allowed_spender_ids",
        "action",
        "recipient_rule",
        "spend_cap",
        "allowance_cap",
        "loss_cap",
        "penalty_cap",
        "amount_rule",
        "parameters",
        "record_status",
        "revoked_at",
        "approval",
        "user_approved",
    }
)
_PARAM_KEYS = frozenset({"name", "solidity_type", "role"})
_IDENTITY_KEYS = frozenset({"rule", "siwe_address", "own_address_list", "on_account_switch"})
_SWITCH_KEYS = frozenset({"clear", "keep"})
_UNCLASSIFIED_KEYS = frozenset(
    {
        "approval",
        "user_approved",
        "protocol_id",
        "protocol_version",
        "network_id",
        "chain_id",
        "address_family",
        "role",
        "address",
        "reason",
        "official_sources",
        "source_file",
    }
)
_SKIPPED_KEYS = frozenset({"address", "network_id", "protocol_id", "reason", "role", "source_file"})
_CANDIDATE_REASONS = frozenset(
    {
        "proxy_kind_not_documented",
        "not_evm",
        "proxy_basis_missing",
        "duplicate_address_conflict",
        "official_source_missing",
        "bad_address",
        "unknown_network",
        "unrecognized_protocol_file",
    }
)


def load_chains_text(text: str) -> dict[str, Any]:
    document = _parsed_object(text)
    return _chains(document)


def load_registry_text(text: str, chains: dict[str, Any]) -> dict[str, Any]:
    document = _parsed_object(text)
    return _registry(document, chains)


def load_trust_text(text: str) -> dict[str, Any]:
    document = _parsed_object(text)
    return _trust(document)


def load_candidates_text(text: str) -> dict[str, Any]:
    document = _parsed_object(text)
    return _candidates(document)


def load_execution_directory(path: str | Path | None = None) -> dict[str, Any]:
    """Load chains, registry, and the trust-critical path list from one directory."""
    directory = Path(path) if path is not None else resource_path("config", "execution")
    chains = load_chains_text((directory / "chains.json").read_text(encoding="utf-8"))
    registry = load_registry_text((directory / "registry.json").read_text(encoding="utf-8"), chains)
    trust = load_trust_text((directory / "trust-critical-paths.json").read_text(encoding="utf-8"))
    return {"chains": chains, "registry": registry, "trust_critical_paths": trust}


def user_approval_granted(text: str, chains_text: str | None = None) -> bool:
    """True only when a loaded document says the user approved it. This loader never does."""
    try:
        parsed = parse_strict_json(text)
        if not isinstance(parsed, dict):
            return False
        version = parsed.get("schema_version")
        if version == REGISTRY_VERSION:
            if chains_text is None:
                return False
            loaded = load_registry_text(text, load_chains_text(chains_text))
        elif version == CANDIDATES_VERSION:
            loaded = load_candidates_text(text)
        else:
            return False
    except ExecutionRegistryError:
        return False
    if loaded.get("user_approved") is True:
        return True
    for key in ("deployments", "policies", "unclassified"):
        for item in loaded.get(key, []):
            if isinstance(item, dict) and item.get("user_approved") is True:
                return True
    return False


def _parsed_object(text: str) -> dict[str, Any]:
    parsed = parse_strict_json(text)
    reject_inert_approval(parsed)
    if not isinstance(parsed, dict):
        raise ExecutionRegistryError("bad_type", "document")
    return parsed


def _chains(document: dict[str, Any]) -> dict[str, Any]:
    allowed = frozenset({"schema_version", "purpose", "enables_send", "relationships", "chains"})
    expect_object(document, allowed, allowed, "chains")
    if document["schema_version"] != CHAINS_VERSION:
        raise ExecutionRegistryError("bad_enum", "chains.schema_version")
    if document["purpose"] != "execution_rpc_catalog":
        raise ExecutionRegistryError("bad_enum", "chains.purpose")
    if document["enables_send"] is not False:
        raise ExecutionRegistryError("enables_send_rejected", "chains.enables_send")
    if document["relationships"] != [dict(item) for item in RELATIONSHIPS]:
        raise ExecutionRegistryError("catalog_relationship", "chains.relationships")
    if not isinstance(document["chains"], list):
        raise ExecutionRegistryError("bad_type", "chains.chains")
    chains = [_chain(item, index) for index, item in enumerate(document["chains"])]
    chains.sort(key=lambda item: item["chain_id"])
    seen: set[int] = set()
    for item in chains:
        if item["chain_id"] in seen:
            raise ExecutionRegistryError("duplicate_key", f"chain_id:{item['chain_id']}")
        seen.add(item["chain_id"])
    return {
        "chains": chains,
        "enables_send": False,
        "purpose": "execution_rpc_catalog",
        "relationships": [dict(item) for item in RELATIONSHIPS],
        "schema_version": CHAINS_VERSION,
    }


def _chain(value: Any, index: int) -> dict[str, Any]:
    label = f"chains[{index}]"
    item = expect_object(value, _CHAIN_KEYS, _CHAIN_KEYS, label)
    chain_id = require_chain_id(item["chain_id"], f"{label}.chain_id")
    network_id = require_text(item["network_id"], f"{label}.network_id", max_len=80)
    if not network_id.replace("-", "").isalnum() or network_id != network_id.lower():
        raise ExecutionRegistryError("bad_type", f"{label}.network_id")
    status = item["status"]
    if status not in {"active", "revoked"}:
        raise ExecutionRegistryError("bad_enum", f"{label}.status")
    if not isinstance(item["rpc"], list):
        raise ExecutionRegistryError("bad_type", f"{label}.rpc")
    rpc = [_rpc(entry, f"{label}.rpc[{rpc_index}]") for rpc_index, entry in enumerate(item["rpc"])]
    if status == "active":
        if item["revoked_at"] is not None:
            raise ExecutionRegistryError("bad_type", f"{label}.revoked_at")
        if not rpc:
            raise ExecutionRegistryError("missing_field", f"{label}.rpc")
        revoked_at = None
    else:
        revoked_at = require_time(item["revoked_at"], f"{label}.revoked_at")
    return {
        "chain_id": chain_id,
        "network_id": network_id,
        "revoked_at": revoked_at,
        "rpc": rpc,
        "status": status,
    }


def _rpc(value: Any, label: str) -> dict[str, str]:
    item = expect_object(value, _RPC_KEYS, _RPC_KEYS, label)
    return {
        "operator": require_text(item["operator"], f"{label}.operator", max_len=80),
        "source": require_https_url(item["source"], f"{label}.source"),
        "url": require_https_url(item["url"], f"{label}.url"),
    }


def _registry(document: dict[str, Any], chains: dict[str, Any]) -> dict[str, Any]:
    allowed = frozenset(
        {"schema_version", "send_enabled", "user_approved", "deployments", "policies", "identity"}
    )
    expect_object(document, allowed, allowed, "registry")
    if document["schema_version"] != REGISTRY_VERSION:
        raise ExecutionRegistryError("bad_enum", "registry.schema_version")
    if document["send_enabled"] is not False:
        raise ExecutionRegistryError("send_enabled_rejected", "registry.send_enabled")
    if document["user_approved"] is not False:
        raise ExecutionRegistryError("user_approved_has_no_effect", "registry.user_approved")
    if not isinstance(document["deployments"], list) or not isinstance(document["policies"], list):
        raise ExecutionRegistryError("bad_type", "registry.lists")
    by_chain = {item["chain_id"]: item for item in chains["chains"]}
    deployments = [
        _deployment(item, index, mode="registry", by_chain=by_chain)
        for index, item in enumerate(document["deployments"])
    ]
    deployments.sort(key=lambda item: item["id"])
    _unique(deployments, "id", "deployment")
    seen_address: set[tuple[int, str]] = set()
    for item in deployments:
        key = (item["chain_id"], item["address"].lower())
        if key in seen_address:
            raise ExecutionRegistryError("duplicate_key", item["address"])
        seen_address.add(key)
    by_id = {item["id"]: item for item in deployments}
    policies = [
        _policy(item, index, by_id) for index, item in enumerate(document["policies"])
    ]
    policies.sort(key=lambda item: item["id"])
    _unique(policies, "id", "policy")
    return {
        "deployments": deployments,
        "identity": _identity_block(document["identity"]),
        "policies": policies,
        "schema_version": REGISTRY_VERSION,
        "send_enabled": False,
        "user_approved": False,
    }


def _deployment(
    value: Any,
    index: int,
    *,
    mode: str,
    by_chain: dict[int, dict[str, Any]] | None,
) -> dict[str, Any]:
    label = f"deployments[{index}]"
    item = expect_object(value, _DEPLOYMENT_KEYS, _DEPLOYMENT_KEYS, label)
    if item["user_approved"] is not False:
        raise ExecutionRegistryError("user_approved_has_no_effect", f"{label}.user_approved")
    record_status = item["record_status"]
    if mode == "registry" and record_status not in {"active", "revoked"}:
        raise ExecutionRegistryError("bad_enum", f"{label}.record_status")
    if mode == "candidate" and record_status != "candidate":
        raise ExecutionRegistryError("bad_enum", f"{label}.record_status")
    approval = "candidate_unapproved" if mode == "candidate" else "not_approved"
    if item["approval"] != approval:
        raise ExecutionRegistryError("bad_enum", f"{label}.approval")
    chain_id = require_chain_id(item["chain_id"], f"{label}.chain_id")
    network_id = require_text(item["network_id"], f"{label}.network_id", max_len=80)
    if by_chain is not None:
        chain = by_chain.get(chain_id)
        if chain is None or chain["network_id"] != network_id:
            raise ExecutionRegistryError("chain_mismatch", label)
        if record_status == "active" and chain["status"] != "active":
            raise ExecutionRegistryError("chain_mismatch", f"{label}.status")
    proxy_kind = item["proxy_kind"]
    if proxy_kind not in PROXY_KINDS:
        raise ExecutionRegistryError("proxy_kind_rejected", str(proxy_kind))
    permission = item["permission_type"]
    if permission not in PERMISSION_TYPES:
        raise ExecutionRegistryError("bad_enum", f"{label}.permission_type")
    pin_status = item["pin_status"]
    if pin_status not in {"pinned", "not_read_offline"}:
        raise ExecutionRegistryError("bad_enum", f"{label}.pin_status")
    if record_status == "candidate":
        if pin_status != "not_read_offline":
            raise ExecutionRegistryError("bad_enum", f"{label}.pin_status")
        runtime_hash = _null_hash(item["runtime_hash"], f"{label}.runtime_hash")
        source_block_number = _null_uint(item["source_block_number"], f"{label}.source_block_number")
        source_block_hash = _null_block(item["source_block_hash"], f"{label}.source_block_hash")
        valid_until = _null_time(item["valid_until"], f"{label}.valid_until")
        implementation = _implementation(item["implementation"], f"{label}.implementation", proxy_kind, pinned=False)
    else:
        if pin_status != "pinned":
            raise ExecutionRegistryError("pin_required", label)
        runtime_hash = require_hash64(item["runtime_hash"], f"{label}.runtime_hash")
        source_block_number = require_uint_string(item["source_block_number"], f"{label}.source_block_number")
        source_block_hash = require_block_hash(item["source_block_hash"], f"{label}.source_block_hash")
        valid_until = require_time(item["valid_until"], f"{label}.valid_until")
        implementation = _implementation(item["implementation"], f"{label}.implementation", proxy_kind, pinned=True)
    if record_status == "revoked":
        revoked_at = require_time(item["revoked_at"], f"{label}.revoked_at")
    elif item["revoked_at"] is not None:
        raise ExecutionRegistryError("bad_type", f"{label}.revoked_at")
    else:
        revoked_at = None
    address = require_checksum(item["address"], f"{label}.address")
    if implementation is not None and implementation["address"] == address:
        raise ExecutionRegistryError("implementation_is_proxy", label)
    return {
        "address": address,
        "approval": approval,
        "assertions": _string_list(item["assertions"], f"{label}.assertions"),
        "chain_id": chain_id,
        "id": _identifier(item["id"], f"{label}.id"),
        "implementation": implementation,
        "network_id": network_id,
        "official_sources": _sources(item["official_sources"], f"{label}.official_sources"),
        "permission_type": permission,
        "pin_status": pin_status,
        "protocol_id": _identifier(item["protocol_id"], f"{label}.protocol_id"),
        "protocol_version": require_text(item["protocol_version"], f"{label}.protocol_version", max_len=40),
        "proxy_kind": proxy_kind,
        "proxy_kind_basis": require_text(item["proxy_kind_basis"], f"{label}.proxy_kind_basis"),
        "record_status": record_status,
        "revoked_at": revoked_at,
        "runtime_hash": runtime_hash,
        "source_block_hash": source_block_hash,
        "source_block_number": source_block_number,
        "source_files": _path_list(item["source_files"], f"{label}.source_files"),
        "user_approved": False,
        "valid_until": valid_until,
    }


def _implementation(value: Any, label: str, proxy_kind: str, *, pinned: bool) -> dict[str, str] | None:
    if not pinned or proxy_kind == "none":
        if value is not None:
            raise ExecutionRegistryError("bad_type", label)
        return None
    if proxy_kind != "eip1967_impl":
        raise ExecutionRegistryError("proxy_kind_rejected", proxy_kind)
    item = expect_object(value, _IMPL_KEYS, _IMPL_KEYS, label)
    return {
        "address": require_checksum(item["address"], f"{label}.address"),
        "runtime_hash": require_hash64(item["runtime_hash"], f"{label}.runtime_hash"),
    }


def _policy(value: Any, index: int, deployments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    label = f"policies[{index}]"
    item = expect_object(value, _POLICY_KEYS, _POLICY_KEYS, label)
    if item["user_approved"] is not False:
        raise ExecutionRegistryError("user_approved_has_no_effect", f"{label}.user_approved")
    if item["approval"] != "not_approved":
        raise ExecutionRegistryError("bad_enum", f"{label}.approval")
    record_status = item["record_status"]
    if record_status not in {"active", "revoked"}:
        raise ExecutionRegistryError("bad_enum", f"{label}.record_status")
    if item["recipient_rule"] != "self":
        raise ExecutionRegistryError("recipient_not_self", f"{label}.recipient_rule")
    if item["amount_rule"] != "all":
        raise ExecutionRegistryError("bad_enum", f"{label}.amount_rule")
    if item["penalty_cap"] != "0":
        raise ExecutionRegistryError("penalty_cap_not_zero", f"{label}.penalty_cap")
    action = item["action"]
    if action not in ACTIONS:
        raise ExecutionRegistryError("bad_enum", f"{label}.action")
    deployment_id = _identifier(item["deployment_id"], f"{label}.deployment_id")
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise ExecutionRegistryError("not_registered", deployment_id)
    chain_id = require_chain_id(item["chain_id"], f"{label}.chain_id")
    if chain_id != deployment["chain_id"]:
        raise ExecutionRegistryError("chain_mismatch", label)
    if record_status == "active" and deployment["record_status"] != "active":
        raise ExecutionRegistryError("not_registered", deployment_id)
    parameters = _parameters(item["parameters"], label)
    allowed = _id_list(item["allowed_spender_ids"], f"{label}.allowed_spender_ids")
    for spender_id in allowed:
        if spender_id not in deployments:
            raise ExecutionRegistryError("not_registered", spender_id)
    if any(param.get("role") == "spender" for param in parameters) and not allowed:
        raise ExecutionRegistryError("not_registered", f"{label}.allowed_spender_ids")
    if record_status == "revoked":
        revoked_at = require_time(item["revoked_at"], f"{label}.revoked_at")
    elif item["revoked_at"] is not None:
        raise ExecutionRegistryError("bad_type", f"{label}.revoked_at")
    else:
        revoked_at = None
    asset_id = item["asset_deployment_id"]
    if asset_id is not None:
        asset_id = _identifier(asset_id, f"{label}.asset_deployment_id")
        if asset_id not in deployments:
            raise ExecutionRegistryError("not_registered", asset_id)
    return {
        "action": action,
        "allowed_spender_ids": allowed,
        "allowance_cap": require_uint_string(item["allowance_cap"], f"{label}.allowance_cap"),
        "amount_rule": "all",
        "approval": "not_approved",
        "asset_deployment_id": asset_id,
        "chain_id": chain_id,
        "deployment_id": deployment_id,
        "id": _identifier(item["id"], f"{label}.id"),
        "loss_cap": require_uint_string(item["loss_cap"], f"{label}.loss_cap"),
        "market_id": _optional_id(item["market_id"], f"{label}.market_id"),
        "parameters": parameters,
        "penalty_cap": "0",
        "position_id": _optional_id(item["position_id"], f"{label}.position_id"),
        "recipient_rule": "self",
        "record_status": record_status,
        "revoked_at": revoked_at,
        "spend_cap": require_uint_string(item["spend_cap"], f"{label}.spend_cap"),
        "user_approved": False,
    }


def _parameters(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ExecutionRegistryError("bad_type", f"{label}.parameters")
    parsed: list[dict[str, str]] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        param_label = f"{label}.parameters[{index}]"
        if not isinstance(item, dict):
            raise ExecutionRegistryError("bad_type", param_label)
        solidity_type = item.get("solidity_type")
        if solidity_type not in SOLIDITY_TYPES:
            raise ExecutionRegistryError("bad_enum", f"{param_label}.solidity_type")
        if solidity_type in ADDRESS_TYPES and "role" not in item:
            raise ExecutionRegistryError("address_role_required", param_label)
        allowed = _PARAM_KEYS if solidity_type in ADDRESS_TYPES else frozenset({"name", "solidity_type"})
        expect_object(item, allowed, allowed, param_label)
        name = require_text(item["name"], f"{param_label}.name", max_len=80)
        if not name.replace("_", "").isalnum() or name[:1].isdigit():
            raise ExecutionRegistryError("bad_type", f"{param_label}.name")
        if name in names:
            raise ExecutionRegistryError("duplicate_key", name)
        names.add(name)
        parsed_item = {"name": name, "solidity_type": solidity_type}
        if solidity_type in ADDRESS_TYPES:
            role = item["role"]
            if role not in ADDRESS_ROLES:
                raise ExecutionRegistryError("bad_enum", f"{param_label}.role")
            parsed_item["role"] = role
        parsed.append(parsed_item)
    return parsed


def _identity_block(value: Any) -> dict[str, Any]:
    item = expect_object(value, _IDENTITY_KEYS, _IDENTITY_KEYS, "identity")
    if item["rule"] != "single_account_v1":
        raise ExecutionRegistryError("identity_rule", "identity.rule")
    if item["siwe_address"] != "compare_when_session_present":
        raise ExecutionRegistryError("identity_rule", "identity.siwe_address")
    if item["own_address_list"] is not False:
        raise ExecutionRegistryError("own_address_list_rejected", "identity.own_address_list")
    switch = expect_object(item["on_account_switch"], _SWITCH_KEYS, _SWITCH_KEYS, "identity.on_account_switch")
    if switch["clear"] != ["evidence"] or switch["keep"] != ["pending", "unknown"]:
        raise ExecutionRegistryError("identity_rule", "identity.on_account_switch")
    return {
        "on_account_switch": {"clear": ["evidence"], "keep": ["pending", "unknown"]},
        "own_address_list": False,
        "rule": "single_account_v1",
        "siwe_address": "compare_when_session_present",
    }


def _candidates(document: dict[str, Any]) -> dict[str, Any]:
    allowed = frozenset(
        {"schema_version", "approval", "user_approved", "deployments", "unclassified", "skipped"}
    )
    expect_object(document, allowed, allowed, "candidates")
    if document["schema_version"] != CANDIDATES_VERSION:
        raise ExecutionRegistryError("bad_enum", "candidates.schema_version")
    if document["approval"] != "candidate_unapproved":
        raise ExecutionRegistryError("bad_enum", "candidates.approval")
    if document["user_approved"] is not False:
        raise ExecutionRegistryError("user_approved_has_no_effect", "candidates.user_approved")
    for key in ("deployments", "unclassified", "skipped"):
        if not isinstance(document[key], list):
            raise ExecutionRegistryError("bad_type", f"candidates.{key}")
    deployments = [
        _deployment(item, index, mode="candidate", by_chain=None)
        for index, item in enumerate(document["deployments"])
    ]
    deployments.sort(key=lambda item: item["id"])
    _unique(deployments, "id", "deployment")
    seen_address: set[tuple[int, str]] = set()
    for item in deployments:
        key = (item["chain_id"], item["address"].lower())
        if key in seen_address:
            raise ExecutionRegistryError("duplicate_key", item["address"])
        seen_address.add(key)
    unclassified = [_unclassified(item, index) for index, item in enumerate(document["unclassified"])]
    unclassified.sort(key=_unclassified_key)
    skipped = [_skipped(item, index) for index, item in enumerate(document["skipped"])]
    skipped.sort(key=_skipped_key)
    return {
        "approval": "candidate_unapproved",
        "deployments": deployments,
        "schema_version": CANDIDATES_VERSION,
        "skipped": skipped,
        "unclassified": unclassified,
        "user_approved": False,
    }


def _unclassified(value: Any, index: int) -> dict[str, Any]:
    label = f"unclassified[{index}]"
    item = expect_object(value, _UNCLASSIFIED_KEYS, _UNCLASSIFIED_KEYS, label)
    if item["approval"] != "candidate_unapproved" or item["user_approved"] is not False:
        raise ExecutionRegistryError("user_approved_has_no_effect", label)
    reason = item["reason"]
    if reason not in _CANDIDATE_REASONS:
        raise ExecutionRegistryError("bad_enum", f"{label}.reason")
    chain_id = item["chain_id"]
    if chain_id is not None:
        chain_id = require_chain_id(chain_id, f"{label}.chain_id")
    address = require_text(item["address"], f"{label}.address", max_len=120)
    if item["address_family"] == "evm":
        address = require_checksum(address, f"{label}.address")
    return {
        "address": address,
        "address_family": require_text(item["address_family"], f"{label}.address_family", max_len=40),
        "approval": "candidate_unapproved",
        "chain_id": chain_id,
        "network_id": require_text(item["network_id"], f"{label}.network_id", max_len=80),
        "official_sources": _sources(item["official_sources"], f"{label}.official_sources"),
        "protocol_id": _identifier(item["protocol_id"], f"{label}.protocol_id"),
        "protocol_version": require_text(item["protocol_version"], f"{label}.protocol_version", max_len=40),
        "reason": reason,
        "role": require_text(item["role"], f"{label}.role", max_len=80),
        "source_file": require_relative_path(item["source_file"], f"{label}.source_file"),
        "user_approved": False,
    }


def _skipped(value: Any, index: int) -> dict[str, Any]:
    label = f"skipped[{index}]"
    item = expect_object(value, _SKIPPED_KEYS, _SKIPPED_KEYS, label)
    reason = item["reason"]
    if reason not in _CANDIDATE_REASONS:
        raise ExecutionRegistryError("bad_enum", f"{label}.reason")
    return {
        "address": _optional_text(item["address"], f"{label}.address"),
        "network_id": _optional_text(item["network_id"], f"{label}.network_id"),
        "protocol_id": _optional_text(item["protocol_id"], f"{label}.protocol_id"),
        "reason": reason,
        "role": _optional_text(item["role"], f"{label}.role"),
        "source_file": require_relative_path(item["source_file"], f"{label}.source_file"),
    }


def _trust(document: dict[str, Any]) -> dict[str, Any]:
    allowed = frozenset({"schema_version", "note", "categories", "paths"})
    expect_object(document, allowed, allowed, "trust")
    if document["schema_version"] != TRUST_VERSION:
        raise ExecutionRegistryError("bad_enum", "trust.schema_version")
    if document["note"] != TRUST_NOTE:
        raise ExecutionRegistryError("bad_enum", "trust.note")
    if not isinstance(document["categories"], list) or not isinstance(document["paths"], list):
        raise ExecutionRegistryError("bad_type", "trust.lists")
    categories = []
    from_categories: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(document["categories"]):
        label = f"trust.categories[{index}]"
        category = expect_object(item, frozenset({"name", "paths"}), frozenset({"name", "paths"}), label)
        name = require_text(category["name"], f"{label}.name", max_len=80)
        if name in names:
            raise ExecutionRegistryError("duplicate_key", name)
        names.add(name)
        if not isinstance(category["paths"], list) or not category["paths"]:
            raise ExecutionRegistryError("bad_type", f"{label}.paths")
        paths = [require_relative_path(path, f"{label}.paths") for path in category["paths"]]
        if len(paths) != len(set(paths)):
            raise ExecutionRegistryError("duplicate_key", name)
        from_categories.update(paths)
        categories.append({"name": name, "paths": sorted(paths)})
    if not REQUIRED_TRUST_CATEGORIES <= names:
        raise ExecutionRegistryError("missing_field", "trust.categories")
    declared = [require_relative_path(path, "trust.paths") for path in document["paths"]]
    if len(declared) != len(set(declared)):
        raise ExecutionRegistryError("duplicate_key", "trust.paths")
    if set(declared) != from_categories | {TRUST_PATH}:
        raise ExecutionRegistryError("bad_path", "trust.paths")
    if not REQUIRED_TRUST_PATHS <= set(declared):
        raise ExecutionRegistryError("missing_field", "trust.paths")
    return {
        "categories": categories,
        "note": TRUST_NOTE,
        "paths": sorted(declared),
        "schema_version": TRUST_VERSION,
    }


def _sources(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ExecutionRegistryError("missing_field", label)
    parsed = []
    urls: set[str] = set()
    for index, item in enumerate(value):
        source = expect_object(item, _SOURCE_KEYS, _SOURCE_KEYS, f"{label}[{index}]")
        url = require_https_url(source["url"], f"{label}[{index}].url")
        if url in urls:
            raise ExecutionRegistryError("duplicate_key", url)
        urls.add(url)
        parsed.append({"retrieved": require_date(source["retrieved"], f"{label}[{index}].retrieved"), "url": url})
    parsed.sort(key=lambda item: item["url"])
    return parsed


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ExecutionRegistryError("bad_type", label)
    return [require_text(item, f"{label}[{index}]", max_len=200) for index, item in enumerate(value)]


def _path_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ExecutionRegistryError("missing_field", label)
    paths = [require_relative_path(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(paths) != len(set(paths)):
        raise ExecutionRegistryError("duplicate_key", label)
    return sorted(paths)


def _id_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ExecutionRegistryError("bad_type", label)
    parsed = [_identifier(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(parsed) != len(set(parsed)):
        raise ExecutionRegistryError("duplicate_key", label)
    return parsed


def _identifier(value: Any, label: str) -> str:
    text = require_text(value, label, max_len=120)
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789:_-" for char in text):
        raise ExecutionRegistryError("bad_type", label)
    return text


def _optional_id(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, label)


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return require_text(value, label, max_len=160)


def _null_hash(value: Any, label: str) -> None:
    if value is not None:
        raise ExecutionRegistryError("pin_required", label)


def _null_uint(value: Any, label: str) -> None:
    if value is not None:
        raise ExecutionRegistryError("pin_required", label)


def _null_block(value: Any, label: str) -> None:
    if value is not None:
        raise ExecutionRegistryError("pin_required", label)


def _null_time(value: Any, label: str) -> None:
    if value is not None:
        raise ExecutionRegistryError("bad_time", label)


def _unique(items: list[dict[str, Any]], key: str, label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item[key] in seen:
            raise ExecutionRegistryError("duplicate_key", f"{label}:{item[key]}")
        seen.add(item[key])


def _unclassified_key(item: dict[str, Any]) -> tuple[str, str, str, str, str, int]:
    chain_id = -1 if item["chain_id"] is None else item["chain_id"]
    return (item["protocol_id"], item["network_id"], item["role"], item["source_file"], item["address"], chain_id)


def _skipped_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        item["source_file"],
        item["protocol_id"] or "",
        item["role"] or "",
        item["reason"],
        item["address"] or "",
    )

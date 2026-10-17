"""Identity binding from the execution plan.

v1 has one account for a call. ``from``, ``onBehalf``, receiver, transfer
``to``, ``eth_accounts[0]``, the plan sender, recipient, and execution account
are that account. A SIWE address is compared only when a login session
supplied one. There is no list of "own addresses".

Switching accounts drops evidence and keeps pending and unknown. This function
states that retention. It does not touch a browser store; later packages call it.
"""

from __future__ import annotations

from typing import Any

from guardian.execution.errors import ExecutionRegistryError
from guardian.execution.model import (
    ADDRESS_TYPES,
    IDENTITY_FIELDS,
    REGISTRY_ROLES,
    SELF_ROLES,
    require_checksum,
    require_uint_string,
)


def check_identity(account: str, identity: dict[str, Any]) -> None:
    if not isinstance(identity, dict):
        raise ExecutionRegistryError("bad_type", "identity")
    if "own_addresses" in identity or (
        "own_address_list" in identity and identity["own_address_list"] is not False
    ):
        raise ExecutionRegistryError("own_address_list_rejected", "identity")
    account_cs = require_checksum(account, "account")
    allowed = set(IDENTITY_FIELDS) | {"siwe_address"}
    for key in identity:
        if key not in allowed:
            raise ExecutionRegistryError("unknown_field", f"identity.{key}")
    for key in IDENTITY_FIELDS:
        if key not in identity:
            raise ExecutionRegistryError("missing_field", f"identity.{key}")
        if require_checksum(identity[key], f"identity.{key}") != account_cs:
            code = "recipient_not_self" if key in {"receiver", "transfer_to"} else "identity_mismatch"
            raise ExecutionRegistryError(code, key)
    if "siwe_address" in identity and identity["siwe_address"] is not None:
        if require_checksum(identity["siwe_address"], "identity.siwe_address") != account_cs:
            raise ExecutionRegistryError("identity_mismatch", "siwe_address")


def retain_after_account_switch(state: dict[str, Any]) -> dict[str, Any]:
    """Keep pending and unknown. Drop evidence."""
    if not isinstance(state, dict):
        raise ExecutionRegistryError("bad_type", "account_switch")
    allowed = {"evidence", "pending", "unknown"}
    for key in state:
        if key not in allowed:
            raise ExecutionRegistryError("unknown_field", f"account_switch.{key}")
    for key in allowed:
        if key not in state:
            raise ExecutionRegistryError("missing_field", f"account_switch.{key}")
    return {"pending": state["pending"], "unknown": state["unknown"]}


def check_call(
    registry: dict[str, Any],
    *,
    policy_id: str,
    account: str,
    identity: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Apply recipient, self-role, and registered spender/target rules to one call."""
    check_identity(account, identity)
    if not isinstance(arguments, dict):
        raise ExecutionRegistryError("bad_type", "arguments")
    policies = {item["id"]: item for item in registry["policies"]}
    policy = policies.get(policy_id)
    if policy is None:
        raise ExecutionRegistryError("unknown_policy", policy_id)
    if policy["recipient_rule"] != "self":
        raise ExecutionRegistryError("recipient_not_self", policy_id)
    if policy["record_status"] != "active":
        raise ExecutionRegistryError("not_registered", policy_id)
    parameters = policy["parameters"]
    if set(arguments) != {item["name"] for item in parameters}:
        raise ExecutionRegistryError("argument_mismatch", policy_id)
    account_cs = require_checksum(account, "account")
    active = [
        item
        for item in registry["deployments"]
        if item["chain_id"] == policy["chain_id"] and item["record_status"] == "active"
    ]
    by_address = {item["address"].lower(): item for item in active}
    allowed = set(policy["allowed_spender_ids"])
    for param in parameters:
        value = arguments[param["name"]]
        solidity_type = param["solidity_type"]
        if solidity_type in ADDRESS_TYPES:
            address = require_checksum(value, param["name"])
            role = param["role"]
            if role in SELF_ROLES:
                if address != account_cs:
                    code = "recipient_not_self" if role == "receiver" else "identity_mismatch"
                    raise ExecutionRegistryError(code, param["name"])
            elif role in REGISTRY_ROLES:
                found = by_address.get(address.lower())
                if found is None or (role == "spender" and found["id"] not in allowed):
                    raise ExecutionRegistryError("not_registered", param["name"])
            else:
                raise ExecutionRegistryError("bad_enum", role)
        elif solidity_type == "uint256":
            require_uint_string(value, param["name"])
        elif solidity_type == "bool":
            if type(value) is not bool:
                raise ExecutionRegistryError("bad_type", param["name"])
        elif solidity_type == "bytes":
            if not isinstance(value, str):
                raise ExecutionRegistryError("bad_type", param["name"])
        else:
            raise ExecutionRegistryError("bad_enum", solidity_type)

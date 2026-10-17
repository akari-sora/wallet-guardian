"""Shared constants and small checks for the execution registry.

The client loader in action-client/src/execution repeats these values. Parity
tests compare loaded documents, so a new enum member has to land on both sides
and in the JSON schema.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from eth_utils import to_checksum_address

from guardian.execution.errors import ExecutionRegistryError

REGISTRY_VERSION = "guardian.execution_registry.v1"
CHAINS_VERSION = "guardian.execution_chains.v1"
CANDIDATES_VERSION = "guardian.execution_candidates.v1"
TRUST_VERSION = "guardian.trust_critical_paths.v1"
RULES_VERSION = "guardian.execution_candidate_rules.v1"

UINT256_MAX = (1 << 256) - 1
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

PROXY_KINDS = ("none", "eip1967_impl")
PERMISSION_TYPES = (
    "erc20_allowance",
    "erc721_operator",
    "erc721_token",
    "permit2",
    "credit_delegation",
    "aave_debt_token",
    "morpho_like",
    "erc4626_vault",
    "queue_vault",
)
ADDRESS_ROLES = ("receiver", "onBehalf", "owner", "spender", "target")
SELF_ROLES = ("receiver", "onBehalf", "owner")
REGISTRY_ROLES = ("spender", "target")
SOLIDITY_TYPES = ("address", "address payable", "uint256", "bool", "bytes")
ADDRESS_TYPES = ("address", "address payable")
ACTIONS = (
    "revoke_erc20",
    "revoke_erc721_operator",
    "revoke_erc721_token",
    "revoke_credit_delegation",
    "revoke_morpho",
    "erc4626_redeem",
    "aave_withdraw",
    "moolah_withdraw_collateral",
    "queue_request",
    "queue_claim",
    "aave_repay",
    "moolah_repay",
)
IDENTITY_FIELDS = (
    "from",
    "onBehalf",
    "receiver",
    "transfer_to",
    "eth_accounts_0",
    "plan_sender",
    "plan_recipient",
    "execution_account",
)
TRUST_NOTE = "Changing this list is a trust-critical review change. The list is not a boundary."
TRUST_PATH = "wallet-guardian/config/execution/trust-critical-paths.json"
REQUIRED_TRUST_PATHS = frozenset(
    {
        "wallet-guardian/config/execution/",
        TRUST_PATH,
        "wallet-guardian/action-client/src/execution/",
        "wallet-guardian/action-client/src/send/",
        "wallet-guardian/action-client/src/wallets/",
        "wallet-guardian/action-client/src/mode-a.mjs",
        "wallet-guardian/action-client/vite.config.ts",
        "wallet-guardian/action-client/package.json",
        "wallet-guardian/action-client/package-lock.json",
        "wallet-guardian/action-client/tools/",
        "wallet-guardian/action-client/deploy/release.schema.json",
    }
)
REQUIRED_TRUST_CATEGORIES = frozenset(
    {
        "registry_and_policy",
        "client_core",
        "send_path",
        "guided_flow",
        "build",
        "release_schema",
    }
)
RELATIONSHIPS = (
    {
        "id": "monitor_chains_json",
        "path": "config/chains.json",
        "purpose": "monitor_enabled_declaration",
        "execution_rpc": False,
    },
    {
        "id": "planned_network_registry",
        "path": "config/chains/",
        "purpose": "mcm01_planned_registry",
        "execution_rpc": False,
    },
    {
        "id": "wheel_resource_copy",
        "path": "src/guardian/_resources/config/",
        "purpose": "installed_monitor_copy",
        "execution_rpc": False,
    },
    {
        "id": "execution_rpc_catalog",
        "path": "config/execution/chains.json",
        "purpose": "execution_rpc_catalog",
        "execution_rpc": True,
    },
)

_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_UINT = re.compile(r"^(0|[1-9][0-9]*)$")
_HASH64 = re.compile(r"^[0-9a-f]{64}$")
_BLOCK_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")


def expect_object(value: Any, allowed: frozenset[str], required: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionRegistryError("bad_type", label)
    for key in value:
        if key not in allowed:
            raise ExecutionRegistryError("unknown_field", f"{label}.{key}")
    for key in required:
        if key not in value:
            raise ExecutionRegistryError("missing_field", f"{label}.{key}")
    return value


def require_checksum(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ExecutionRegistryError("bad_address", label)
    try:
        checksum = to_checksum_address(value)
    except ValueError as exc:
        raise ExecutionRegistryError("bad_address", label) from exc
    if checksum != value:
        raise ExecutionRegistryError("address_not_checksum", label)
    return checksum


def require_uint_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UINT.fullmatch(value) is None:
        raise ExecutionRegistryError("bad_integer", label)
    if int(value) > UINT256_MAX:
        raise ExecutionRegistryError("out_of_range", label)
    return value


def require_https_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 400:
        raise ExecutionRegistryError("bad_url", label)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ExecutionRegistryError("control_character", label)
    host = value[8:].split("/", 1)[0]
    if host == "" or "@" in host or " " in value or "\\" in value:
        raise ExecutionRegistryError("bad_url", label)
    lowered = value.lower()
    if "apikey=" in lowered or "glpat-" in lowered or "nodereal.io/v1/" in lowered:
        raise ExecutionRegistryError("bad_url", label)
    return value


def require_date(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ExecutionRegistryError("bad_date", label)
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ExecutionRegistryError("bad_date", label) from exc
    return value


def require_time(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise ExecutionRegistryError("bad_time", label)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ExecutionRegistryError("bad_time", label) from exc
    return value


def require_text(value: Any, label: str, *, max_len: int = 400) -> str:
    if not isinstance(value, str) or value == "" or len(value) > max_len:
        raise ExecutionRegistryError("bad_type", label)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ExecutionRegistryError("control_character", label)
    return value


def require_relative_path(value: Any, label: str) -> str:
    text = require_text(value, label, max_len=240)
    if _RELATIVE_PATH.fullmatch(text) is None or ".." in text.split("/"):
        raise ExecutionRegistryError("bad_path", label)
    return text


def require_hash64(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH64.fullmatch(value) is None:
        raise ExecutionRegistryError("bad_hash", label)
    return value


def require_block_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _BLOCK_HASH.fullmatch(value) is None:
        raise ExecutionRegistryError("bad_hash", label)
    return value


def require_chain_id(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ExecutionRegistryError("bad_integer", label)
    if value < 1:
        raise ExecutionRegistryError("out_of_range", label)
    return value


def reject_inert_approval(value: Any) -> None:
    """File fields cannot grant user approval or introduce an address list."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "approved_by_user":
                raise ExecutionRegistryError("approved_by_user_has_no_effect", key)
            if key == "user_approved" and item is True:
                raise ExecutionRegistryError("user_approved_has_no_effect", key)
            if key == "own_addresses" or (key == "own_address_list" and item is not False):
                raise ExecutionRegistryError("own_address_list_rejected", key)
            reject_inert_approval(item)
        return
    if isinstance(value, list):
        for item in value:
            reject_inert_approval(item)

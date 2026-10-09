"""Planned network registry for MCM-01.

This loader only reads local JSON. It does not open a socket, sign, or broadcast.
A network in this package stays disabled until a later package proves it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FAMILIES = frozenset({"evm", "solana", "bitcoin", "tron", "ton", "sui", "aptos", "cosmos"})
PACKAGE_STATUS = "planned_not_live_verified"
IDENTITY_STATUSES = frozenset({"documented", "to_verify_by_probe"})
_NATIVE_PREFIX = {
    "evm": "evm-native:",
    "solana": "solana-native:",
    "bitcoin": "bitcoin-native:",
    "tron": "tron-native:",
    "ton": "ton-native:",
    "sui": "sui-native:",
    "aptos": "aptos-native:",
    "cosmos": "cosmos-native:",
}
_REQUIRED = (
    "network",
    "display_name",
    "family",
    "tier",
    "evm_chain_id",
    "native_asset",
    "identity",
    "finality_policy",
    "sources",
    "enabled",
    "status",
    "notes",
)
_ADDRESS = re.compile(r"(?<![0-9A-Fa-f])0x[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
_SYNTHETIC_ADDRESS = re.compile(r"0x0{39}[0-9A-Fa-f]$", re.IGNORECASE)
_SECRET_MARKERS = ("apikey=", "glpat-", "nodereal.io/v1/")
_RETRIEVED = re.compile(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]")


class RegistryError(ValueError):
    """The planned registry is inconsistent. Nothing in it is turned on."""


def _packaged_tier_plan() -> Path:
    """Tier plan shipped beside this module.

    This loader stays on the stdlib so it does not import the rest of guardian.
    The file lives in the package tree (``guardian/_resources``), not a repo walk.
    """
    plan = Path(__file__).resolve().parent / "_resources" / "docs" / "mcm-01" / "network-tiers.plan.json"
    if not plan.is_file():
        raise RegistryError("network tier plan is missing from the package")
    return plan


def load_registry(directory: str | Path, *, tier_plan: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load ``config/chains/<network-id>.json`` files and validate them against the tier plan."""
    directory = Path(directory)
    if not directory.is_dir():
        raise RegistryError(f"registry directory is missing: {directory}")
    plan = _read_tier_plan(Path(tier_plan) if tier_plan is not None else _packaged_tier_plan())
    entries: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        _reject_private_material(raw, path.name)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryError(f"invalid json: {path.name}") from exc
        if not isinstance(payload, dict):
            raise RegistryError(f"registry entry must be an object: {path.name}")
        network = payload.get("network")
        if not isinstance(network, str) or path.stem != network:
            raise RegistryError(f"file name must equal network id: {path.name}")
        if network in seen_ids:
            raise RegistryError(f"duplicate network id {network}")
        seen_ids[network] = path.name
        entries.append(payload)
    return validate_registry(entries, plan)


def validate_registry(entries: list[dict[str, Any]], tier_plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate entry objects. Used by ``load_registry`` and by negative tests."""
    if not isinstance(entries, list) or not isinstance(tier_plan, list):
        raise RegistryError("registry entries and tier plan must be lists")
    plan_by_id: dict[str, dict[str, Any]] = {}
    for row in tier_plan:
        if not isinstance(row, dict) or not isinstance(row.get("network"), str):
            raise RegistryError("tier plan rows need a network id")
        network = row["network"]
        if network in plan_by_id:
            raise RegistryError(f"duplicate network id in tier plan: {network}")
        plan_by_id[network] = row

    normalized: list[dict[str, Any]] = []
    seen_networks: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError("registry entry must be an object")
        _validate_entry(entry)
        network = entry["network"]
        if network in seen_networks:
            raise RegistryError(f"duplicate network id {network}")
        seen_networks.add(network)
        normalized.append(entry)

    seen_chain: dict[int, str] = {}
    for entry in normalized:
        chain_id = entry["evm_chain_id"]
        if chain_id is None:
            continue
        if chain_id in seen_chain:
            raise RegistryError(
                f"duplicate evm_chain_id {chain_id} on {seen_chain[chain_id]} and {entry['network']}"
            )
        seen_chain[chain_id] = entry["network"]

    extra = seen_networks - set(plan_by_id)
    missing = set(plan_by_id) - seen_networks
    if extra or missing:
        raise RegistryError(
            "network is outside network-tiers.plan.json and is not enabled: "
            f"extra={sorted(extra)} missing={sorted(missing)}"
        )

    ordered: dict[str, dict[str, Any]] = {}
    for network, planned in plan_by_id.items():
        entry = next(item for item in normalized if item["network"] == network)
        if entry["family"] != planned.get("family"):
            raise RegistryError(f"family does not match network-tiers.plan.json: {network}")
        if entry["tier"] != planned.get("tier"):
            raise RegistryError(f"tier does not match network-tiers.plan.json: {network}")
        if entry["evm_chain_id"] != planned.get("evm_chain_id"):
            raise RegistryError(f"evm_chain_id does not match network-tiers.plan.json: {network}")
        if entry["status"] != PACKAGE_STATUS or entry["enabled"] is not False:
            raise RegistryError(f"status must not claim live verification: {network}")
        ordered[network] = entry
    return ordered


def _validate_entry(entry: dict[str, Any]) -> None:
    missing = [key for key in _REQUIRED if key not in entry]
    if missing:
        raise RegistryError(f"missing required fields: {', '.join(missing)}")
    network = entry["network"]
    if not isinstance(network, str) or not network or network != network.strip():
        raise RegistryError("network id must be a non-empty string")
    if not isinstance(entry["display_name"], str) or not entry["display_name"].strip():
        raise RegistryError(f"display_name is empty: {network}")
    family = entry["family"]
    if family not in FAMILIES:
        raise RegistryError(f"unknown family: {family}")
    if type(entry["tier"]) is not int:
        raise RegistryError(f"tier must be an integer: {network}")
    if type(entry["enabled"]) is not bool or entry["enabled"] is not False:
        raise RegistryError(f"enabled must be false: {network}")
    if entry["status"] != PACKAGE_STATUS or entry.get("live_verified") is True:
        raise RegistryError(f"status must be {PACKAGE_STATUS}: {network}")
    if not isinstance(entry["finality_policy"], str) or not entry["finality_policy"].strip():
        raise RegistryError(f"finality_policy is empty: {network}")
    if not isinstance(entry["notes"], str) or not entry["notes"].strip():
        raise RegistryError(f"notes are empty: {network}")

    chain_id = entry["evm_chain_id"]
    if chain_id is not None and type(chain_id) is not int:
        raise RegistryError(f"evm_chain_id must be an integer or null: {network}")
    if family == "evm" and chain_id is None:
        raise RegistryError(f"EVM family is missing evm_chain_id: {network}")
    if family != "evm" and chain_id is not None:
        raise RegistryError(f"non-EVM family must not set evm_chain_id: {network}")

    native = entry["native_asset"]
    if not isinstance(native, dict):
        raise RegistryError(f"native_asset must be an object: {network}")
    for key in ("symbol", "decimals", "identity"):
        if key not in native:
            raise RegistryError(f"native_asset missing {key}: {network}")
    if not isinstance(native["symbol"], str) or not native["symbol"].strip():
        raise RegistryError(f"native_asset.symbol is empty: {network}")
    if type(native["decimals"]) is not int or native["decimals"] < 0:
        raise RegistryError(f"native_asset.decimals must be a non-negative integer: {network}")
    identity_name = native["identity"]
    prefix = _NATIVE_PREFIX[family]
    if not isinstance(identity_name, str) or not identity_name.startswith(prefix):
        raise RegistryError(f"native identity does not match family: {network}")
    if family == "evm" and identity_name != f"evm-native:{chain_id}":
        raise RegistryError(f"native identity does not match evm_chain_id: {network}")

    identity = entry["identity"]
    if not isinstance(identity, dict):
        raise RegistryError(f"identity must be an object: {network}")
    for key in ("method", "evm_chain_id", "expected_value", "status"):
        if key not in identity:
            raise RegistryError(f"identity missing {key}: {network}")
    if not isinstance(identity["method"], str) or not identity["method"].strip():
        raise RegistryError(f"identity.method is empty: {network}")
    if identity["evm_chain_id"] != chain_id:
        raise RegistryError(f"identity.evm_chain_id must match evm_chain_id: {network}")
    expected = identity["expected_value"]
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise RegistryError(f"identity.expected_value must be a string or null: {network}")
    if identity["status"] not in IDENTITY_STATUSES:
        raise RegistryError(f"identity.status is not documented or to_verify_by_probe: {network}")
    if identity["status"] == "documented" and not expected:
        raise RegistryError(f"documented identity requires expected_value: {network}")
    if identity["status"] == "to_verify_by_probe" and expected is not None:
        raise RegistryError(f"to_verify_by_probe must not claim expected_value: {network}")

    sources = entry["sources"]
    if not isinstance(sources, list) or not sources:
        raise RegistryError(f"sources must be a non-empty list: {network}")
    for source in sources:
        if not isinstance(source, dict):
            raise RegistryError(f"source must be an object: {network}")
        url = source.get("url")
        kind = source.get("kind")
        retrieved = source.get("retrieved")
        if not isinstance(url, str) or not url.startswith("https://") or "://" in url[8:]:
            raise RegistryError(f"source url must be https: {network}")
        if "@" in url.split("/")[2]:
            raise RegistryError(f"source url must not carry credentials: {network}")
        lowered = url.lower()
        if "apikey=" in lowered or "glpat-" in lowered or "nodereal.io/v1/" in lowered:
            raise RegistryError(f"source url looks like a secret: {network}")
        if not isinstance(kind, str) or not kind.strip():
            raise RegistryError(f"source kind is empty: {network}")
        if not isinstance(retrieved, str) or _RETRIEVED.fullmatch(retrieved) is None:
            raise RegistryError(f"source retrieved date must be YYYY-MM-DD: {network}")


def _read_tier_plan(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RegistryError(f"tier plan is missing: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError("tier plan is not valid json") from exc
    if not isinstance(payload, list):
        raise RegistryError("tier plan must be a list")
    return payload


def _reject_private_material(raw: str, label: str) -> None:
    lowered = raw.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise RegistryError(f"registry file contains a secret marker: {label}")
    for match in _ADDRESS.finditer(raw):
        if _SYNTHETIC_ADDRESS.fullmatch(match.group(0)) is None:
            raise RegistryError(f"registry file contains a non-synthetic address: {label}")

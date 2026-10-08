"""Load protocol manifests. The file chooses a registry id, never a module path.

Forbidden keys (entrypoint, import, exec, module, class, and path aliases) are
rejected anywhere in the document. View methods must already exist on the plugin
class. Nothing in this module imports code named by the document.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from guardian.protocols.network import chain_registry
from guardian.protocols.registry import PLUGIN_REGISTRY

_ROOT = Path(__file__).resolve().parents[3]
PROTOCOLS_DIR = _ROOT / "config" / "protocols"
_FORBIDDEN = frozenset({
    "__import__",
    "class",
    "class_path",
    "entrypoint",
    "eval",
    "exec",
    "import",
    "importlib",
    "loader",
    "module",
    "module_path",
    "plugin_path",
    "qualname",
})
_CAPABILITIES = frozenset({"bridges", "exposure_news", "positions", "withdrawals"})
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")


class ManifestError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Manifest:
    plugin_id: str
    version: str
    networks: tuple[str, ...]
    contracts: Mapping[str, Any]
    required_sources: tuple[str, ...]
    view_methods: tuple[str, ...]
    capabilities: tuple[str, ...]
    notes: str


def load_protocol_dir(directory: Path | None = None) -> dict[str, Manifest]:
    """Load packaged manifests. The legacy lista-bsc.json file is not a plugin manifest."""
    root = Path(directory) if directory is not None else PROTOCOLS_DIR
    found: dict[str, Manifest] = {}
    for path in sorted(root.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ManifestError("manifest_not_object")
        if "id" not in document and "protocol_id" in document:
            continue
        manifest = load_manifest(document)
        if manifest.plugin_id in found:
            raise ManifestError("duplicate_plugin")
        found[manifest.plugin_id] = manifest
    return found


def load_manifest(document: Mapping[str, Any]) -> Manifest:
    if not isinstance(document, Mapping):
        raise ManifestError("manifest_not_object")
    _reject_tree(document)
    plugin_id = document.get("id")
    if not isinstance(plugin_id, str) or plugin_id not in PLUGIN_REGISTRY:
        raise ManifestError("unknown_plugin")
    plugin = PLUGIN_REGISTRY[plugin_id]
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ManifestError("version")
    networks = _strings(document.get("networks"), "networks")
    known = chain_registry()
    if any(network not in known for network in networks):
        raise ManifestError("unknown_network")
    if set(networks) != set(plugin.networks):
        raise ManifestError("network_mismatch")
    sources = _strings(document.get("required_sources"), "required_sources")
    if set(sources) != set(plugin.adapters):
        raise ManifestError("source_mismatch")
    methods = _strings(document.get("view_methods"), "view_methods", allow_empty=True)
    allowed = set(plugin.specs)
    if any(method not in allowed for method in methods):
        raise ManifestError("view_not_allowlisted")
    if set(methods) != allowed:
        raise ManifestError("view_not_allowlisted")
    capabilities = _strings(document.get("capabilities"), "capabilities")
    if any(item not in _CAPABILITIES for item in capabilities):
        raise ManifestError("capability")
    contracts = document.get("contracts")
    if not isinstance(contracts, dict):
        raise ManifestError("contracts")
    _validate_contracts(contracts, networks)
    notes = document.get("notes", "")
    if not isinstance(notes, str):
        raise ManifestError("notes")
    return Manifest(
        plugin_id=plugin_id,
        version=version,
        networks=tuple(networks),
        contracts=contracts,
        required_sources=tuple(sources),
        view_methods=tuple(methods),
        capabilities=tuple(capabilities),
        notes=notes,
    )


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ManifestError(field)
    if any(not isinstance(item, str) or not item for item in value):
        raise ManifestError(field)
    return list(value)


def _validate_contracts(contracts: Mapping[str, Any], networks: list[str]) -> None:
    for network, roles in contracts.items():
        if network not in networks or not isinstance(roles, dict):
            raise ManifestError("contracts")
        for role, entry in roles.items():
            if not isinstance(role, str) or not isinstance(entry, dict):
                raise ManifestError("contracts")
            address = entry.get("address")
            source = entry.get("source_url")
            retrieved = entry.get("retrieved")
            status = entry.get("status")
            if not isinstance(address, str) or not address:
                raise ManifestError("contract_address")
            if not isinstance(source, str) or not source.startswith("https://"):
                raise ManifestError("contract_source")
            if not isinstance(retrieved, str) or _DATE.fullmatch(retrieved) is None:
                raise ManifestError("contract_retrieved")
            if status != "documented":
                raise ManifestError("contract_status")


def _reject_tree(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().lower() in _FORBIDDEN:
                raise ManifestError(f"forbidden_key:{key}")
            if str(key) == "enabled":
                raise ManifestError("enabled_not_allowed")
            _reject_tree(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_tree(item)
        return
    if isinstance(value, str) and value.strip().lower() == "live_pass":
        raise ManifestError("live_pass")

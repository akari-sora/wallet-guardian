"""Load config/source-capabilities. These files are a contract, not a live probe.

Support stays partial or unsupported until a later package implements the
adapter. ``permits.offchain_unused`` stays not_observable. ``live_pass`` is
rejected because this package has not verified a live source.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from guardian.errors import ConfigError
from guardian.network_registry import load_registry
from guardian.resources import resource_path
from guardian.security.transport_policy import ADAPTER_PROTOCOL, operation_records
CAPABILITIES = (
    "address_risk",
    "asset_risk",
    "balances.fungible",
    "balances.native",
    "balances.nft",
    "bridges",
    "exposure_news",
    "history.fungible",
    "history.native_external",
    "history.native_internal",
    "history.nft",
    "permissions.cosmos_authz",
    "permissions.cosmos_feegrant",
    "permissions.erc1155_operator",
    "permissions.erc20",
    "permissions.erc721_operator",
    "permissions.erc721_token",
    "permissions.move_capability",
    "permissions.spl_authority",
    "permissions.spl_delegate",
    "permissions.ton_extensions",
    "permissions.tron_account",
    "permits.dai_variant",
    "permits.erc2612_observed",
    "permits.offchain_unused",
    "permit2.allowance",
    "permit2.signature_usage",
    "positions",
    "sanctions",
    "withdrawals",
)
SUPPORT = frozenset({"implemented", "not_applicable", "not_observable", "partial", "unsupported"})
PROBE = frozenset({"blocked", "documented", "live_fail", "live_pass", "untested"})
_LIVE = frozenset({"live_fail", "live_pass"})
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
FAMILIES = frozenset({"aptos", "bitcoin", "cosmos", "evm", "solana", "sui", "ton", "tron"})


@dataclass(frozen=True)
class CapabilityStatus:
    support: str
    probe: str
    notes: str


@dataclass(frozen=True)
class AdapterCapabilities:
    adapter: str
    protocol: str
    backend: str
    families: tuple[str, ...]
    networks: frozenset[str]
    probe: str
    capabilities: dict[str, CapabilityStatus]
    limits: tuple[dict[str, Any], ...]
    allowed_operations: tuple[dict[str, Any], ...]
    notes: str

    def declares_support(self, network: str, capability: str) -> str | None:
        if self.networks and "*" not in self.networks and network not in self.networks:
            return None
        status = self.capabilities.get(capability)
        if status is None:
            return None
        return status.support


class CapabilityCatalog:
    def __init__(self, adapters: dict[str, AdapterCapabilities]) -> None:
        self.adapters = adapters

    def get(self, adapter: str) -> AdapterCapabilities:
        try:
            return self.adapters[adapter]
        except KeyError:
            raise ConfigError(f"unknown adapter {adapter}") from None

    def backend(self, adapter: str) -> str:
        return self.get(adapter).backend

    def declares(self, adapter: str, network: str, capability: str) -> bool:
        support = self.get(adapter).declares_support(network, capability)
        return support in {"implemented", "partial"}


def load_source_capabilities(directory: str | Path | None = None) -> CapabilityCatalog:
    directory = Path(directory) if directory is not None else resource_path("config", "source-capabilities")
    if not directory.is_dir():
        raise ConfigError("source capabilities directory is missing")
    networks = set(load_registry(resource_path("config", "chains")))
    found: dict[str, AdapterCapabilities] = {}
    for path in sorted(directory.glob("*.json")):
        entry = _load_one(path, networks)
        if entry.adapter in found:
            raise ConfigError(f"duplicate adapter {entry.adapter}")
        found[entry.adapter] = entry
    missing = sorted(set(ADAPTER_PROTOCOL) - set(found))
    if missing:
        raise ConfigError("source capabilities are incomplete")
    return CapabilityCatalog(found)


def _load_one(path: Path, networks: set[str]) -> AdapterCapabilities:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigError(f"source capabilities are not valid json: {path.name}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"source capabilities must be an object: {path.name}")
    adapter = document.get("adapter")
    if not isinstance(adapter, str) or path.stem != adapter or adapter not in ADAPTER_PROTOCOL:
        raise ConfigError(f"adapter file does not match its name: {path.name}")
    protocol = document.get("protocol")
    if protocol != ADAPTER_PROTOCOL[adapter]:
        raise ConfigError(f"protocol does not match transport policy: {adapter}")
    backend = document.get("backend")
    if not isinstance(backend, str) or _SLUG.fullmatch(backend) is None:
        raise ConfigError(f"backend is missing: {adapter}")
    families = _strings(document.get("families"), "families", adapter)
    if any(family not in FAMILIES for family in families):
        raise ConfigError(f"unknown family: {adapter}")
    raw_networks = _strings(document.get("networks"), "networks", adapter)
    if any(network != "*" and network not in networks for network in raw_networks):
        raise ConfigError(f"unknown network: {adapter}")
    probe = _probe(document.get("probe"), adapter)
    capabilities = _capabilities(document.get("capabilities"), adapter)
    limits = _limits(document.get("limits"), adapter)
    allowed = document.get("allowed_operations")
    expected = operation_records(adapter)
    if allowed != expected:
        raise ConfigError(f"operations_do_not_match_policy: {adapter}")
    notes = document.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        raise ConfigError(f"notes are missing: {adapter}")
    if "apikey=" in notes.lower() or "glpat-" in notes.lower():
        raise ConfigError(f"notes contain a secret: {adapter}")
    return AdapterCapabilities(
        adapter=adapter,
        protocol=protocol,
        backend=backend,
        families=tuple(families),
        networks=frozenset(raw_networks),
        probe=probe,
        capabilities=capabilities,
        limits=tuple(limits),
        allowed_operations=tuple(expected),
        notes=notes,
    )


def _capabilities(raw: Any, adapter: str) -> dict[str, CapabilityStatus]:
    if not isinstance(raw, dict):
        raise ConfigError(f"capabilities are missing: {adapter}")
    if set(raw) != set(CAPABILITIES):
        raise ConfigError(f"capability matrix is incomplete: {adapter}")
    parsed: dict[str, CapabilityStatus] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ConfigError(f"capability is malformed: {adapter} {name}")
        support = item.get("support")
        probe = _probe(item.get("probe"), f"{adapter} {name}")
        notes = item.get("notes", "")
        if support not in SUPPORT:
            raise ConfigError(f"unknown support: {adapter} {name}")
        if support == "implemented":
            raise ConfigError(f"adapter_not_implemented: {adapter} {name}")
        if not isinstance(notes, str):
            raise ConfigError(f"capability notes are malformed: {adapter} {name}")
        if name == "permits.offchain_unused" and support != "not_observable":
            raise ConfigError("offchain_unused_not_observable")
        parsed[name] = CapabilityStatus(support, probe, notes)
    return parsed


def _probe(value: Any, label: str) -> str:
    if value not in PROBE:
        raise ConfigError(f"unknown probe: {label}")
    if value in _LIVE:
        raise ConfigError(f"live_probe_not_verified: {label}")
    return str(value)


def _limits(raw: Any, adapter: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"limits are malformed: {adapter}")
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"limits are malformed: {adapter}")
        name = item.get("name")
        source_url = item.get("source_url")
        retrieved = item.get("retrieved")
        notes = item.get("notes")
        if not isinstance(name, str) or not isinstance(notes, str) or not notes.strip():
            raise ConfigError(f"limits are malformed: {adapter}")
        if not isinstance(source_url, str) or not source_url.startswith("https://") or "apikey=" in source_url.lower():
            raise ConfigError(f"limit source is malformed: {adapter}")
        if not isinstance(retrieved, str) or _DATE.fullmatch(retrieved) is None:
            raise ConfigError(f"limit date is malformed: {adapter}")
        cleaned.append(dict(item))
    return cleaned


NEWS_USER_AGENT = "WalletGuardianNews/1"
NEWS_CONDITIONAL_HEADERS = ("If-Modified-Since", "If-None-Match")


@dataclass(frozen=True)
class NewsFetchCapability:
    """GET-only metadata for official news. No credential and no env var."""

    http_method: str
    credential: None
    hosts: tuple[str, ...]
    conditional_headers: tuple[str, ...]
    user_agent: str


def news_fetch_capability(sources: Mapping[str, Any] | None = None) -> NewsFetchCapability:
    """Hosts come from feeds the catalog marked allowlisted_get. Nothing else."""
    from guardian.news.catalog import load_news_sources

    loaded = sources if sources is not None else load_news_sources()
    hosts: list[str] = []
    for source in loaded.values():
        for feed in source.feeds:
            if feed.fetch != "allowlisted_get":
                continue
            host = (urlsplit(feed.url).hostname or "").lower().rstrip(".")
            if host:
                hosts.append(host)
    return NewsFetchCapability(
        http_method="GET",
        credential=None,
        hosts=tuple(sorted(set(hosts))),
        conditional_headers=NEWS_CONDITIONAL_HEADERS,
        user_agent=NEWS_USER_AGENT,
    )


def _strings(raw: Any, field_name: str, adapter: str) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ConfigError(f"{field_name} are malformed: {adapter}")
    return list(raw)

"""Choose the existing routed source for one relations read.

No new source is registered here. Solana and Bitcoin are not queried.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

DISCLOSURE = "Responses include other addresses. Those addresses are not queried."

BLOCKSCOUT_NETWORKS = frozenset({
    "ethereum-mainnet",
    "optimism-mainnet",
    "base-mainnet",
    "arbitrum-one",
    "polygon-pos",
})
ETHERSCAN_FALLBACK = frozenset({
    "ethereum-mainnet",
    "arbitrum-one",
    "polygon-pos",
})
_HISTORY = (
    "history.native_external",
    "history.native_internal",
    "history.fungible",
    "history.token",
)


@dataclass
class Choice:
    role: str
    source_id: str | None = None
    adapter: str | None = None
    host: str = ""
    read_status: str | None = None
    reason: str = ""
    gap: str = ""


@dataclass
class Job:
    network: str
    address: str
    direction: str
    choices: list[Choice] = field(default_factory=list)


def host_of(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    host = urlsplit(url.strip()).hostname or ""
    return host.lower().rstrip(".")


def endpoint_text(endpoints: Any, source_id: str) -> str:
    try:
        source = endpoints.get(source_id)
    except Exception:
        return ""
    text = getattr(source, "endpoint", "")
    return text.strip() if isinstance(text, str) else ""


def build_jobs(
    *,
    command: str,
    seeds: Sequence[tuple[str, str]],
    payer: tuple[str, str] | None,
    explicit_networks: Sequence[str] | None,
    expand_network: str | None,
    enabled: Sequence[str],
    document: Mapping[str, Any],
    status_map: Mapping[str, Any],
    endpoints: Any,
    registry: Mapping[str, Any],
) -> list[Job]:
    if command == "expand":
        if payer is None or not expand_network:
            raise ValueError("payer")
        _require_network(expand_network, registry)
        family, canonical = payer
        _require_family(expand_network, family, registry, explicit=True, enabled=enabled)
        choices = choices_for(expand_network, document, status_map, endpoints, registry)
        return [Job(expand_network, canonical, "from", choices)]
    jobs: list[Job] = []
    for family, canonical in seeds:
        for network in networks_for(family, explicit_networks, enabled, registry):
            choices = choices_for(network, document, status_map, endpoints, registry)
            jobs.append(Job(network, canonical, "to", choices))
    return jobs


def networks_for(
    family: str,
    explicit: Sequence[str] | None,
    enabled: Sequence[str],
    registry: Mapping[str, Any],
) -> list[str]:
    if explicit:
        found: list[str] = []
        for network in explicit:
            _require_network(network, registry)
            _require_family(network, family, registry, explicit=True, enabled=enabled)
            if network not in found:
                found.append(network)
        return found
    if family == "evm":
        return [network for network in enabled if registry[network]["family"] == "evm"]
    if family == "tron":
        return ["tron-mainnet"]
    if family == "solana":
        return ["solana-mainnet"]
    if family == "bitcoin":
        return ["bitcoin-mainnet"]
    raise ValueError("family")


def choices_for(
    network: str,
    document: Mapping[str, Any],
    status_map: Mapping[str, Any],
    endpoints: Any,
    registry: Mapping[str, Any],
) -> list[Choice]:
    family = registry[network]["family"]
    if network == "bsc-mainnet":
        return [Choice("skip", read_status="gap", reason="bsc_source_gated", gap="credential_missing")]
    if family in {"solana", "bitcoin"}:
        return [Choice("skip", read_status="unsupported", reason="unsupported")]
    if family == "tron":
        return _tron_choice(document, endpoints)
    if family != "evm" or network not in BLOCKSCOUT_NETWORKS:
        return [Choice("skip", read_status="unsupported", reason="unsupported")]
    routed = _routed(document, network)
    sources = document.get("sources") or {}
    blockscout = _first_blockscout(routed, sources, status_map, endpoints)
    etherscan = None
    if network in ETHERSCAN_FALLBACK:
        etherscan = _first_etherscan(routed, sources, status_map, endpoints)
    if blockscout is not None and etherscan is not None:
        return [blockscout, Choice(
            "fallback",
            source_id=etherscan.source_id,
            adapter="etherscan_v2",
            host=etherscan.host,
        )]
    if blockscout is not None:
        return [blockscout]
    if etherscan is not None:
        return [etherscan]
    reason = "credential_missing" if _keyed_but_locked(routed, sources, status_map) else "endpoint_missing"
    gap = "credential_missing" if reason == "credential_missing" else ""
    return [Choice("skip", read_status="gap", reason=reason, gap=gap)]


def plan_for(jobs: Sequence[Job]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    unqueried: list[dict[str, Any]] = []
    seen_skip: set[tuple[str, str, str, str]] = set()
    for job in jobs:
        for choice in job.choices:
            if choice.role == "skip" or not choice.host or not choice.source_id:
                key = (job.network, job.address, job.direction, choice.reason)
                if key in seen_skip:
                    continue
                seen_skip.add(key)
                row = {
                    "address": job.address,
                    "direction": job.direction,
                    "network": job.network,
                    "read_status": choice.read_status or "gap",
                    "reason": choice.reason,
                }
                if choice.gap:
                    row["gap"] = choice.gap
                unqueried.append(row)
                continue
            bucket = grouped.setdefault(
                (job.network, choice.source_id, choice.host, job.direction),
                [],
            )
            if job.address not in bucket:
                bucket.append(job.address)
    queries = [
        {
            "addresses": addresses,
            "direction": direction,
            "host": host,
            "network": network,
            "source_id": source_id,
        }
        for (network, source_id, host, direction), addresses in grouped.items()
    ]
    queries.sort(key=lambda row: (row["network"], row["source_id"], row["direction"], row["host"]))
    unqueried.sort(key=lambda row: (row["network"], row["address"], row["reason"]))
    return {"note": DISCLOSURE, "queries": queries, "unqueried": unqueried}


def _require_network(network: str, registry: Mapping[str, Any]) -> None:
    if network not in registry:
        raise ValueError("unknown_network")


def _require_family(
    network: str,
    family: str,
    registry: Mapping[str, Any],
    *,
    explicit: bool,
    enabled: Sequence[str],
) -> None:
    actual = registry[network]["family"]
    if actual != family:
        raise ValueError("network_family")
    if family in {"solana", "bitcoin"}:
        return
    if explicit and network not in enabled:
        raise ValueError("network_disabled")


def _routed(document: Mapping[str, Any], network: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for capability in _HISTORY:
        for route in document.get("routing") or []:
            if route.get("network") != network or route.get("capability") != capability:
                continue
            for source_id in route.get("source_ids") or []:
                if isinstance(source_id, str) and source_id not in seen:
                    seen.add(source_id)
                    found.append(source_id)
    return found


def _first_blockscout(routed, sources, status_map, endpoints) -> Choice | None:
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("adapter") != "blockscout" or source.get("enabled") is not True:
            continue
        if isinstance(source.get("credential_env"), str) and source.get("credential_env") and not _present(status_map, source_id):
            continue
        host = host_of(endpoint_text(endpoints, source_id))
        if not host:
            continue
        return Choice("primary", source_id=source_id, adapter="blockscout", host=host)
    return None


def _first_etherscan(routed, sources, status_map, endpoints) -> Choice | None:
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("adapter") != "etherscan_v2" or source.get("enabled") is not True:
            continue
        if not _present(status_map, source_id):
            continue
        host = host_of(endpoint_text(endpoints, source_id))
        if not host:
            continue
        return Choice("primary", source_id=source_id, adapter="etherscan_v2", host=host)
    return None


def _tron_choice(document: Mapping[str, Any], endpoints: Any) -> list[Choice]:
    sources = document.get("sources") or {}
    preferred: list[str] = []
    rest: list[str] = []
    for source_id, source in sources.items():
        if source.get("adapter") != "tron_grid" or source.get("enabled") is not True:
            continue
        if source.get("endpoint_env") == "TRONGRID_API_URL":
            preferred.append(source_id)
        else:
            rest.append(source_id)
    for source_id in preferred + rest:
        host = host_of(endpoint_text(endpoints, source_id))
        if host:
            return [Choice("primary", source_id=source_id, adapter="tron_grid", host=host)]
    return [Choice("skip", read_status="gap", reason="endpoint_missing")]


def _present(status_map: Mapping[str, Any], source_id: str) -> bool:
    status = status_map.get(source_id)
    return bool(getattr(status, "credential_present", False))


def _keyed_but_locked(routed, sources, status_map) -> bool:
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("enabled") is not True:
            continue
        if source.get("adapter") not in {"blockscout", "etherscan_v2"}:
            continue
        if isinstance(source.get("credential_env"), str) and source.get("credential_env") and not _present(status_map, source_id):
            return True
    return False

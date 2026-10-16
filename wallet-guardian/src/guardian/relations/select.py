"""Choose the source for one relations read.

The saved profile is not modified. Solana and Bitcoin are not queried.
Optimism's keyless Blockscout origin is ``explorer.optimism.io``.
BSC uses NodeReal when that key and URL are both set, and otherwise the
keyless 48club log host.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from guardian.errors import ConfigError

DISCLOSURE = "Responses include other addresses. Those addresses are not queried."

BLOCKSCOUT_NETWORKS = frozenset({
    "ethereum-mainnet",
    "optimism-mainnet",
    "base-mainnet",
    "arbitrum-one",
    "polygon-pos",
})
_HISTORY = (
    "history.native_external",
    "history.native_internal",
    "history.fungible",
    "history.token",
)
_HOME = {
    "aptos": "aptos-mainnet",
    "bitcoin": "bitcoin-mainnet",
    "cosmos": "cosmos-hub",
    "evm": "ethereum-mainnet",
    "solana": "solana-mainnet",
    "sui": "sui-mainnet",
    "ton": "ton-mainnet",
    "tron": "tron-mainnet",
}
OPTIMISM_REDIRECT_HOST = "optimism.blockscout.com"
OPTIMISM_KEYLESS_HOST = "explorer.optimism.io"
OPTIMISM_KEYLESS_URL = "https://explorer.optimism.io"
CLUB_SOURCE_ID = "bsc-logs-48club"
CLUB_HOST = "rpc-bsc.48.club"
CLUB_URL = "https://rpc-bsc.48.club"
CLUB_BUDGET = "bsc-logs-48club"
CLUB_ENV = "RPC_BSC_LOGS_URL"


@dataclass
class Choice:
    role: str
    source_id: str | None = None
    adapter: str | None = None
    host: str = ""
    read_status: str | None = None
    reason: str = ""
    gap: str = ""
    endpoint: str = ""
    omit_credential: bool = False


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
    except (KeyError, ConfigError, ValueError):
        return ""
    text = getattr(source, "endpoint", "")
    return text.strip() if isinstance(text, str) else ""


def build_jobs(
    *,
    command: str,
    seeds: Sequence[tuple[str, str]],
    payer: tuple[str, str] | None,
    explicit_networks: Sequence[str] | None,
    expand_network: str | Sequence[str] | None,
    enabled: Sequence[str],
    document: Mapping[str, Any],
    status_map: Mapping[str, Any],
    endpoints: Any,
    registry: Mapping[str, Any],
) -> list[Job]:
    jobs: list[Job] = []
    if command == "expand":
        chosen = _expand_targets(expand_network)
        if payer is None or not chosen:
            raise ValueError("payer")
        family, canonical = payer
        for network in chosen:
            _require_network(network, registry)
            _require_family(network, family, registry, explicit=True, enabled=enabled)
            choices = choices_for(network, document, status_map, endpoints, registry)
            jobs.append(Job(network, canonical, "from", choices))
        return jobs
    explicit = list(explicit_networks) if explicit_networks else None
    if explicit:
        _validate_explicit(explicit, registry, enabled)
    matched = 0
    for family, canonical in seeds:
        chosen = networks_for(family, explicit, enabled, registry)
        if explicit and not chosen:
            jobs.append(Job(
                _HOME.get(family, ""),
                canonical,
                "to",
                [Choice("skip", read_status="unsupported", reason="network_family_mismatch")],
            ))
            continue
        if not chosen:
            continue
        matched += 1
        for network in chosen:
            choices = choices_for(network, document, status_map, endpoints, registry)
            jobs.append(Job(network, canonical, "to", choices))
    if explicit and seeds and matched == 0:
        raise ValueError("network_family")
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
            if registry[network]["family"] != family:
                continue
            if network not in found:
                found.append(network)
        return found
    if family == "evm":
        return [network for network in enabled if registry[network]["family"] == "evm"]
    home = _HOME.get(family)
    if home:
        return [home]
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
        return _bsc_choices(document, status_map, endpoints)
    if family in {"solana", "bitcoin"}:
        return [Choice("skip", read_status="unsupported", reason="unsupported")]
    if family == "tron":
        return _tron_choice(document, endpoints)
    if family != "evm" or network not in BLOCKSCOUT_NETWORKS:
        return [Choice("skip", read_status="unsupported", reason="unsupported")]
    routed = _routed(document, network)
    sources = document.get("sources") or {}
    blockscout = _blockscout_choice(network, routed, sources, status_map, endpoints)
    if blockscout is not None:
        return [blockscout]
    if _keyed_but_locked(routed, sources, status_map):
        return [Choice("skip", read_status="gap", reason="credential_missing", gap="credential_missing")]
    if any(
        (sources.get(source_id) or {}).get("adapter") == "etherscan_v2"
        and (sources.get(source_id) or {}).get("enabled") is True
        for source_id in routed
    ):
        return [Choice("skip", read_status="gap", reason="source_gated", gap="etherscan_deferred")]
    return [Choice("skip", read_status="gap", reason="endpoint_missing")]


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


def _expand_targets(value: str | Sequence[str] | None) -> list[str]:
    """Dedupe expand networks. Strings stay one item; other values contribute nothing."""
    if value is None:
        return []
    if isinstance(value, str):
        items: Sequence[object] = [value]
    elif isinstance(value, Sequence):
        items = value
    else:
        return []
    found: list[str] = []
    for item in items:
        if isinstance(item, str) and item and item not in found:
            found.append(item)
    return found


def _validate_explicit(explicit: Sequence[str], registry: Mapping[str, Any], enabled: Sequence[str]) -> None:
    allowed = set(enabled)
    for network in explicit:
        if network not in registry:
            raise ValueError("unknown_network")
        family = registry[network]["family"]
        if family in {"solana", "bitcoin"}:
            continue
        if network not in allowed:
            raise ValueError("network_disabled")


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


def _blockscout_choice(network, routed, sources, status_map, endpoints) -> Choice | None:
    skip = frozenset({OPTIMISM_REDIRECT_HOST}) if network == "optimism-mainnet" else frozenset()
    found = _first_blockscout(routed, sources, status_map, endpoints, skip_hosts=skip)
    if found is not None:
        if network == "optimism-mainnet" and found.host == OPTIMISM_KEYLESS_HOST:
            return replace(found, omit_credential=True)
        return found
    if network == "optimism-mainnet":
        return _optimism_keyless(routed, sources, status_map, endpoints)
    return None


def _first_blockscout(routed, sources, status_map, endpoints, skip_hosts: frozenset[str] = frozenset()) -> Choice | None:
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("adapter") != "blockscout" or source.get("enabled") is not True:
            continue
        if isinstance(source.get("credential_env"), str) and source.get("credential_env") and not _present(status_map, source_id):
            continue
        host = host_of(endpoint_text(endpoints, source_id))
        if not host or host in skip_hosts:
            continue
        return Choice("primary", source_id=source_id, adapter="blockscout", host=host)
    return None


def _optimism_keyless(routed, sources, status_map, endpoints) -> Choice | None:
    """Use the official explorer when the keyless host is empty or is the old redirect."""
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("adapter") != "blockscout" or source.get("enabled") is not True:
            continue
        keyed = isinstance(source.get("credential_env"), str) and bool(source.get("credential_env"))
        host = host_of(endpoint_text(endpoints, source_id))
        old_host = host == OPTIMISM_REDIRECT_HOST
        if not old_host and not (host == "" and not keyed):
            continue
        if keyed and not old_host and not _present(status_map, source_id):
            continue
        return Choice(
            "primary",
            source_id=source_id,
            adapter="blockscout",
            host=OPTIMISM_KEYLESS_HOST,
            endpoint=OPTIMISM_KEYLESS_URL,
            omit_credential=True,
        )
    return None


def _bsc_choices(document, status_map, endpoints) -> list[Choice]:
    routed = _routed(document, "bsc-mainnet")
    sources = document.get("sources") or {}
    for source_id in routed:
        source = sources.get(source_id) or {}
        if source.get("enabled") is not True or source.get("adapter") != "nodereal_transfers":
            continue
        credential = source.get("credential_env")
        if not isinstance(credential, str) or not credential or not _present(status_map, source_id):
            continue
        host = host_of(endpoint_text(endpoints, source_id))
        if not host:
            continue
        return [Choice("primary", source_id=source_id, adapter="nodereal_transfers", host=host)]
    return [_club_choice(sources, endpoints)]


def _club_choice(sources, endpoints) -> Choice:
    """Keyless BSC logs. A configured host wins. An empty one uses rpc-bsc.48.club."""
    named = sources.get(CLUB_SOURCE_ID)
    if (
        isinstance(named, Mapping)
        and named.get("adapter") == "evm_rpc"
        and named.get("enabled") is False
    ):
        return Choice("skip", read_status="gap", reason="source_disabled")
    chosen = _club_source(sources)
    if chosen is None:
        return Choice(
            "primary",
            source_id=CLUB_SOURCE_ID,
            adapter="evm_rpc",
            host=CLUB_HOST,
            endpoint=CLUB_URL,
            omit_credential=True,
        )
    source_id, _source = chosen
    host = host_of(endpoint_text(endpoints, source_id))
    if host:
        return Choice(
            "primary",
            source_id=source_id,
            adapter="evm_rpc",
            host=host,
            omit_credential=True,
        )
    return Choice(
        "primary",
        source_id=source_id,
        adapter="evm_rpc",
        host=CLUB_HOST,
        endpoint=CLUB_URL,
        omit_credential=True,
    )


def _club_source(sources: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    named = sources.get(CLUB_SOURCE_ID)
    if _club_row(named):
        return CLUB_SOURCE_ID, named
    for source_id, source in sources.items():
        if not _club_row(source):
            continue
        if source.get("endpoint_env") == CLUB_ENV:
            return source_id, source
    return None


def _club_row(source: Any) -> bool:
    return (
        isinstance(source, Mapping)
        and source.get("enabled") is True
        and source.get("adapter") == "evm_rpc"
    )


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
        if source.get("adapter") != "blockscout":
            continue
        if isinstance(source.get("credential_env"), str) and source.get("credential_env") and not _present(status_map, source_id):
            return True
    return False

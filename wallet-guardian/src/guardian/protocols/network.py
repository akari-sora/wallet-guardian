"""Local chain registry lookups. Importing this module does not open a socket."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from guardian.network_registry import load_registry
from guardian.resources import resource_path

_CHAINS = resource_path("config", "chains")


@lru_cache(maxsize=1)
def chain_registry() -> dict[str, dict[str, Any]]:
    return load_registry(_CHAINS)


def chain_entry(network: str) -> dict[str, Any] | None:
    if not isinstance(network, str):
        return None
    return chain_registry().get(network)


def chain_family(network: str) -> str | None:
    entry = chain_entry(network)
    if entry is None:
        return None
    family = entry.get("family")
    return family if isinstance(family, str) else None


def native_identity(network: str) -> str | None:
    entry = chain_entry(network)
    if entry is None:
        return None
    asset = entry.get("native_asset")
    if not isinstance(asset, dict):
        return None
    identity = asset.get("identity")
    return identity if isinstance(identity, str) and identity else None


def native_decimals(network: str) -> int | None:
    entry = chain_entry(network)
    if entry is None:
        return None
    asset = entry.get("native_asset")
    if not isinstance(asset, dict):
        return None
    decimals = asset.get("decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        return None
    return decimals


def expected_chain_id(network: str) -> int | None:
    entry = chain_entry(network)
    if entry is None:
        return None
    chain_id = entry.get("evm_chain_id")
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        return None
    return chain_id


def expected_genesis(network: str) -> str | None:
    entry = chain_entry(network)
    if entry is None:
        return None
    identity = entry.get("identity")
    if not isinstance(identity, dict):
        return None
    expected = identity.get("expected_value")
    return expected if isinstance(expected, str) and expected else None

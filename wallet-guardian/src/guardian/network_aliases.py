"""Explicit network aliases. Unknown names are refused, never guessed.

The registry id is the state key. ``arbitrum-mainnet`` and ``polygon-mainnet``
are the ``*-mainnet`` spellings people type for the two chains whose files are
``arbitrum-one`` and ``polygon-pos``. Typing the registry id is the other
direction and stays that id, so an existing baseline is reused.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from guardian.errors import ConfigError

# What an error may quote. A URL, a path, or an address is not a network id.
_NETWORK_TOKEN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ADDRESS_SHAPED = re.compile(r"0x[0-9a-f]{8,}")
_CAPABILITY_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_INVALID_NAME = "<invalid name>"

# Alias spelling -> registry id. Exact registry ids win over this table.
ALIASES: dict[str, str] = {
    "arbitrum-mainnet": "arbitrum-one",
    "polygon-mainnet": "polygon-pos",
}


def alias_table() -> tuple[tuple[str, str], ...]:
    """Typed name and the registry id it uses. Both spellings are listed."""
    rows: list[tuple[str, str]] = []
    for alias, canonical in ALIASES.items():
        rows.append((alias, canonical))
        rows.append((canonical, canonical))
    return tuple(rows)


def canonical_network(name: str, registry: Mapping[str, Any]) -> str:
    """Return the registry id. An unknown or ambiguous name raises."""
    if isinstance(name, str) and name in registry:
        return name
    if isinstance(name, str):
        target = ALIASES.get(name)
        if target is not None and target in registry:
            return target
    raise ConfigError(format_network_error(name, registry))


def normalize_network_list(names: Sequence[str], registry: Mapping[str, Any]) -> list[str]:
    """Canonical ids, first occurrence wins. Two spellings of one chain collapse."""
    found: list[str] = []
    for name in names:
        canonical = canonical_network(name, registry)
        if canonical not in found:
            found.append(canonical)
    return found


def apply_public_aliases(document: dict[str, Any], registry: Mapping[str, Any]) -> None:
    """Rewrite enabled networks and routes in memory. The file on disk is left as written.

    Identical routes collapse after normalization. Two routes that name the same
    canonical network and capability with different source lists, or the same
    sources in a different order, are a config error. Nothing is merged and the
    stored baseline is not rewritten.
    """
    networks = document.get("enabled_networks")
    normalized_networks = None
    if isinstance(networks, list) and all(isinstance(item, str) for item in networks):
        normalized_networks = normalize_network_list(networks, registry)
    routing = document.get("routing")
    normalized_routes = None
    if isinstance(routing, list):
        normalized_routes = _collapse_routes(routing, registry)
    if normalized_networks is not None:
        document["enabled_networks"] = normalized_networks
    if normalized_routes is not None:
        document["routing"] = normalized_routes


def _collapse_routes(routing: Sequence[Any], registry: Mapping[str, Any]) -> list[Any]:
    """One route per canonical network and capability. Conflicts are refused."""
    collapsed: list[Any] = []
    seen: dict[tuple[str, str], Any] = {}
    for route in routing:
        if not isinstance(route, dict) or not isinstance(route.get("network"), str):
            collapsed.append(route)
            continue
        network = canonical_network(route["network"], registry)
        capability = route.get("capability")
        cap = capability if isinstance(capability, str) else ""
        sources = route.get("source_ids")
        key = (network, cap)
        if key in seen:
            if seen[key] != sources:
                shown = cap if _CAPABILITY_TOKEN.fullmatch(cap) else "capability"
                raise ConfigError(
                    f"conflicting routes for {network} {shown}: "
                    "source lists differ after alias normalization"
                )
            continue
        seen[key] = list(sources) if isinstance(sources, list) else sources
        collapsed.append({**route, "network": network})
    return collapsed


def apply_account_aliases(document: dict[str, Any], registry: Mapping[str, Any]) -> None:
    """Rewrite each account's networks onto registry ids."""
    accounts = document.get("accounts")
    if not isinstance(accounts, list):
        return
    for account in accounts:
        if not isinstance(account, dict):
            continue
        networks = account.get("networks")
        if isinstance(networks, list) and all(isinstance(item, str) for item in networks):
            account["networks"] = normalize_network_list(networks, registry)


def display_network_name(name: object) -> str:
    """Identifier-shaped text only. URLs and addresses become a fixed marker."""
    if not isinstance(name, str):
        return _INVALID_NAME
    text = name.strip().lower()
    if (
        _NETWORK_TOKEN.fullmatch(text) is None
        or _ADDRESS_SHAPED.search(text) is not None
        or "://" in name
        or any(mark in text for mark in "/?@#\\")
    ):
        return _INVALID_NAME
    return text


def format_network_error(name: object, registry: Mapping[str, Any]) -> str:
    """List every registry id and close suggestions. The message does not choose one.

    The typed name is quoted only when it is an identifier. A URL or an address
    is replaced with ``<invalid name>`` and is not used as a suggestion needle.
    """
    shown = display_network_name(name)
    valid = ", ".join(sorted(registry))
    suggestions: list[str] = []
    if shown != _INVALID_NAME and isinstance(name, str):
        suggestions = [
            item
            for item in suggestions_for(name.strip(), registry)
            if display_network_name(item) == item
        ]
    kind = "ambiguous network" if len(suggestions) > 1 else "unknown network"
    hint = ", ".join(suggestions) if suggestions else "none"
    return f"{kind} {shown!r}: valid names: {valid}; suggestions: {hint}"


def suggestions_for(name: str, registry: Mapping[str, Any]) -> list[str]:
    """Close names only. A single suggestion is still not applied."""
    if not isinstance(name, str):
        return []
    needle = name.strip().lower()
    if len(needle) < 3 or any(character.isspace() for character in needle):
        return []
    pool = list(dict.fromkeys([*sorted(registry), *sorted(ALIASES)]))
    found: list[str] = []
    token = needle.split("-", 1)[0]
    for candidate in pool:
        if candidate == needle:
            continue
        head = candidate.split("-", 1)[0]
        if candidate.startswith(needle) or head == token or (len(needle) >= 3 and head.startswith(needle)):
            found.append(candidate)
    for candidate in difflib.get_close_matches(needle, pool, n=8, cutoff=0.74):
        if candidate not in found and candidate != needle:
            found.append(candidate)
    return found[:8]

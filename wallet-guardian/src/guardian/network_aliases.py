"""Explicit network aliases. Unknown names are refused, never guessed.

The registry id is the state key. ``arbitrum-mainnet`` and ``polygon-mainnet``
are the ``*-mainnet`` spellings people type for the two chains whose files are
``arbitrum-one`` and ``polygon-pos``. Typing the registry id is the other
direction and stays that id, so an existing baseline is reused.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from typing import Any

from guardian.errors import ConfigError

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
    """Rewrite enabled networks and routes in memory. The file on disk is left as written."""
    networks = document.get("enabled_networks")
    if isinstance(networks, list) and all(isinstance(item, str) for item in networks):
        document["enabled_networks"] = normalize_network_list(networks, registry)
    routing = document.get("routing")
    if not isinstance(routing, list):
        return
    for route in routing:
        if isinstance(route, dict) and isinstance(route.get("network"), str):
            route["network"] = canonical_network(route["network"], registry)


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


def format_network_error(name: object, registry: Mapping[str, Any]) -> str:
    """List every registry id and close suggestions. The message does not choose one."""
    shown = name.strip() if isinstance(name, str) else ""
    valid = ", ".join(sorted(registry))
    suggestions = suggestions_for(shown, registry)
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

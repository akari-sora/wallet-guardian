"""Exact chain and address labels. A label is not a safety judgment.

Membership does not lower severity, suppress a movement, or authorize an action.
A balance-only observation has no counterparty, so it never receives a label.
"""

from __future__ import annotations

from typing import Any

REGISTRY_VERSION = "2026-10-10"
LABEL_SOURCE = "static_local_registry"

# Public protocol address named by the balance-delta plan. Not a watched wallet.
_LISTA_STAKEMANAGER = "0x1adb950d8bb3da4be104211d5ab038628e477fe6"

_ENTRIES: tuple[dict[str, str], ...] = (
    {
        "network": "bsc-mainnet",
        "address": _LISTA_STAKEMANAGER,
        "label": "Lista StakeManager",
    },
)


def lookup(network: str, address: str) -> dict[str, str] | None:
    """Match the canonical network and the full address. Prefixes do not match."""
    if not isinstance(network, str) or not isinstance(address, str):
        return None
    if not address.startswith("0x") or len(address) != 42:
        return None
    target = address.lower()
    for entry in _ENTRIES:
        if entry["network"] == network and entry["address"] == target:
            return {
                "address": target,
                "counterparty_identity": "unverified",
                "label": entry["label"],
                "label_source": LABEL_SOURCE,
                "registry_version": REGISTRY_VERSION,
                "safety_assessment": "not_established",
            }
    return None


def attach(payload: dict[str, Any], network: str, address: str | None) -> None:
    """Copy a label onto evidence that already names the counterparty."""
    if not isinstance(address, str) or not address:
        return
    found = lookup(network, address)
    if found is None:
        return
    payload["counterparty_label"] = found

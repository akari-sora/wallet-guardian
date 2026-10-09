"""Seconds per block for a lookback-limited bootstrap.

The public config may override one network with ``polling.seconds_per_block``.
A network that is not listed uses 12 seconds, the same figure as Ethereum.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# Documented block times. Keys are network ids from the chain registry.
SECONDS_PER_BLOCK: dict[str, float] = {
    "ethereum-mainnet": 12,
    "bsc-mainnet": 0.75,
    "arbitrum-one": 0.25,
    "base-mainnet": 2,
    "optimism-mainnet": 2,
    "polygon-pos": 2.1,
    "avalanche-c": 2,
}
FALLBACK_SECONDS = 12.0


def seconds_per_block(network: str, overrides: Mapping[str, Any] | None = None) -> float:
    """Block time in seconds. An override wins, then the table, then 12."""
    if isinstance(overrides, Mapping) and network in overrides:
        value = overrides[network]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    if network in SECONDS_PER_BLOCK:
        return float(SECONDS_PER_BLOCK[network])
    return FALLBACK_SECONDS


def lookback_blocks(network: str, days: int, overrides: Mapping[str, Any] | None = None) -> int:
    """How many blocks ``days`` covers on ``network``. Zero days stays at the head."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        return 0
    seconds = seconds_per_block(network, overrides)
    return math.ceil(days * 86400 / seconds)

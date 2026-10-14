"""One cached ``decimals()`` eth_call per token contract per round.

A failed call is not stored as 0. The caller keeps the token unvalued and
names the reason.
"""

from __future__ import annotations

from typing import Any

from guardian.chains.evm.permissions import eth_call_operation
from guardian.errors import SecurityError

_SELECTOR = "0x313ce567"


def fill_row_decimals(
    rows: list[Any],
    transport: Any,
    source_id: str | None,
    cache: dict[tuple[str, str], tuple[int | None, str | None]],
) -> None:
    """Stamp decimals onto token rows. Each contract is called at most once."""
    if not isinstance(source_id, str) or not source_id:
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("is_native") is True:
            continue
        asset = row.get("asset")
        network = row.get("network")
        if not isinstance(asset, str) or not asset.startswith("0x") or len(asset) != 42:
            continue
        if not isinstance(network, str) or not network:
            continue
        existing = row.get("decimals")
        if isinstance(existing, int) and not isinstance(existing, bool):
            continue
        key = (network, asset.lower())
        if key not in cache:
            cache[key] = read_decimals(transport, source_id, key[1])
        decimals, reason = cache[key]
        if isinstance(decimals, int) and not isinstance(decimals, bool):
            row["decimals"] = decimals
        elif isinstance(reason, str) and reason:
            row["valuation_reason"] = reason


def read_decimals(transport: Any, source_id: str, contract: str) -> tuple[int | None, str | None]:
    """Return ``(decimals, None)`` or ``(None, reason)``. Never a failed 0."""
    try:
        result = transport.request(source_id, eth_call_operation(contract, _SELECTOR))
    except SecurityError as exc:
        if getattr(exc, "reason", None) == "fixture_miss":
            raise
        return None, "decimals_unreadable"
    except Exception:
        return None, "decimals_unreadable"
    body = getattr(result, "body", None)
    if isinstance(body, dict) and body.get("error"):
        return None, "decimals_unreadable"
    raw = body.get("result") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.startswith(("0x", "0X")):
        return None, "decimals_unreadable"
    text = raw[2:]
    if text == "" or len(text) > 64 or any(char not in "0123456789abcdefABCDEF" for char in text):
        return None, "decimals_unreadable"
    value = int(text, 16)
    if value > 255:
        return None, "decimals_unreadable"
    return value, None

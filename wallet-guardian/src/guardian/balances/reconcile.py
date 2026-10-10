"""Explain a native balance change with transfers that actually sum to it.

Token logs never explain a native balance. Two large opposing transfers stay
visible when their net is zero, because that is not a redundant copy of a
delta. A later transfer enriches the existing alert instead of paying it again.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from guardian.balances.labels import attach
from guardian.pricing.quote import value_usd


def stamp_native_quotes(candidates: list[Any], quotes: dict[str, dict[str, Any]], decimals: dict[str, int]) -> None:
    """Value native transfers when this run already fetched a fresh coin price."""
    stamped: list[Any] = []
    for item in candidates:
        quote = quotes.get(getattr(item, "network", "")) if getattr(item, "kind", None) == "asset" and getattr(item, "is_native", False) else None
        price = quote.get("price") if isinstance(quote, dict) else None
        if quote is None or quote.get("quality") != "available" or not isinstance(price, Decimal):
            stamped.append(item)
            continue
        raw = item.raw_amount if isinstance(item.raw_amount, int) and not isinstance(item.raw_amount, bool) else None
        if raw is None:
            stamped.append(item)
            continue
        try:
            amount = value_usd(raw, decimals.get(item.network, 18), price)
        except ValueError:
            stamped.append(item)
            continue
        stamped.append(replace(item, quote_usd=amount, quote_valuation="valued", quote_quality="available"))
    candidates[:] = stamped


def refresh_balance_decisions(database: Any, tenant: str, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the enriched alert row when attribution arrived after the first write."""
    stored = {
        item.get("dedup_key"): item
        for item in database.alerts_for(tenant)
        if isinstance(item, dict) and item.get("kind") == "balance_delta" and isinstance(item.get("dedup_key"), str)
    }
    refreshed: list[dict[str, Any]] = []
    for item in decisions:
        key = item.get("dedup_key") if isinstance(item, dict) else None
        refreshed.append(stored[key] if isinstance(key, str) and key in stored else item)
    return refreshed


def reconcile_native(database: Any, tenant: str, candidates: list[Any]) -> list[str]:
    """Drop transfer alerts that restate a stored native delta. Events stay."""
    alerts = [
        item for item in database.alerts_for(tenant)
        if isinstance(item, dict) and item.get("kind") == "balance_delta" and item.get("alert") is True
    ]
    suppressed: set[int] = set()
    for alert in alerts:
        matched = _matching(alert, candidates)
        if matched is None:
            continue
        keys = [_transfer_key(item) for item in matched]
        additions: dict[str, Any] = {
            "attribution": "reconciled",
            "explained_by": keys,
        }
        if len(matched) == 1:
            additions["tx_id"] = matched[0].tx_id
            party = matched[0].counterparty if isinstance(matched[0].counterparty, str) else None
            if party:
                additions["counterparty"] = party
                attach(additions, alert.get("network") or matched[0].network, party)
        dedup = alert.get("dedup_key")
        if isinstance(dedup, str) and database.enrich_balance_alert(tenant, dedup, additions):
            for item in matched:
                database.link_balance_explanation(tenant, dedup, _transfer_key(item))
                suppressed.add(id(item))
    removed = [_transfer_key(item) for item in candidates if id(item) in suppressed]
    if suppressed:
        candidates[:] = [item for item in candidates if id(item) not in suppressed]
    return removed


def _matching(alert: dict[str, Any], candidates: list[Any]) -> list[Any] | None:
    try:
        signed = int(alert["signed_delta_raw"])
        old_block = int(alert["old_checkpoint"]["block_number"])
        new_block = int(alert["new_checkpoint"]["block_number"])
    except (KeyError, TypeError, ValueError):
        return None
    if signed == 0:
        return None
    wallet = alert.get("wallet_id")
    network = alert.get("network")
    native = [
        item for item in candidates
        if getattr(item, "kind", None) == "asset"
        and getattr(item, "is_native", False)
        and getattr(item, "wallet_id", None) == wallet
        and getattr(item, "network", None) == network
        and getattr(item, "transfer_origin", None) in {"evm_external", "evm_internal"}
    ]
    if not native or any(getattr(item, "block_number", None) is None for item in native):
        return None
    window = [
        item for item in native
        if isinstance(item.block_number, int) and old_block < item.block_number <= new_block
    ]
    if not window:
        return None
    total = 0
    for item in window:
        amount = item.raw_amount
        if isinstance(amount, bool) or not isinstance(amount, int):
            return None
        if item.direction == "in":
            total += amount
        elif item.direction == "out":
            total -= amount
        else:
            return None
    if total != signed:
        return None
    directions = {item.direction for item in window}
    if signed == 0 or (len(directions) > 1 and total == 0):
        return None
    return window


def _transfer_key(item: Any) -> str:
    identity = getattr(item, "identity", "")
    return str(identity)

"""Explain a native balance change with transfers that actually sum to it.

Token logs never explain a native balance. Two large opposing transfers stay
visible when their net is zero, because that is not a redundant copy of a
delta. A later transfer enriches the existing alert instead of paying it again.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any, Mapping

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
        matched = _matching(alert, candidates, database, tenant)
        if matched is None:
            continue
        keys = [_transfer_key(item) for item in matched]
        additions: dict[str, Any] = {
            "attribution": "reconciled",
            "components": _components(matched, _fee_components(database, tenant, alert)),
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


def movement_link(conn: Any, tenant_id: str, alert: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Transfer alerts already stored for this delta. Empty means the delta may notify."""
    window = _window(alert)
    if window is None:
        return [], []
    signed, old_block, new_block = window
    wallet = alert.get("wallet_id")
    network = alert.get("network")
    asset = str(alert.get("asset") or "").lower()
    rows = conn.execute("SELECT payload FROM alerts WHERE tenant_id=?", (tenant_id,)).fetchall()
    matched: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        body = json.loads(row["payload"]) if not isinstance(row["payload"], dict) else row["payload"]
        if not isinstance(body, dict) or not _stored_transfer(body, wallet, network, asset, old_block, new_block):
            continue
        amount = int(body["raw_amount"])
        if body.get("direction") == "in":
            total += amount
        elif body.get("direction") == "out":
            total -= amount
        else:
            return [], []
        matched.append(body)
    fees = _fee_rows(conn, tenant_id, wallet, network, asset, old_block, new_block)
    if fees is None or not matched:
        return [], []
    fee_total = sum(int(item["raw_amount"]) for item in fees)
    if total - fee_total != signed:
        return [], []
    keys = [str(body.get("identity") or body.get("dedup_key")) for body in matched]
    components = [
        {
            "asset": body.get("asset") or asset,
            "direction": body.get("direction"),
            "raw_amount": str(body.get("raw_amount")),
            "role": "transfer",
            "tx_id": body.get("tx_id"),
        }
        for body in matched
    ]
    components.extend(fees)
    return keys, components


def _matching(alert: dict[str, Any], candidates: list[Any], database: Any, tenant: str) -> list[Any] | None:
    window = _window(alert)
    if window is None:
        return None
    signed, old_block, new_block = window
    wallet = alert.get("wallet_id")
    network = alert.get("network")
    asset = str(alert.get("asset") or "").lower()
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
    in_window = [
        item for item in native
        if isinstance(item.block_number, int) and old_block < item.block_number <= new_block
    ]
    if not in_window or any(_insufficient(item) for item in in_window):
        return None
    if asset and any(_asset_mismatch(item, asset) for item in in_window):
        return None
    total = 0
    for item in in_window:
        amount = item.raw_amount
        if isinstance(amount, bool) or not isinstance(amount, int):
            return None
        if item.direction == "in":
            total += amount
        elif item.direction == "out":
            total -= amount
        else:
            return None
    fees = _fee_rows(getattr(database, "conn", None), tenant, wallet, network, asset, old_block, new_block)
    if fees is None:
        return None
    if total - sum(int(item["raw_amount"]) for item in fees) != signed:
        return None
    directions = {item.direction for item in in_window}
    if len(directions) > 1 and signed == 0:
        return None
    return in_window


def _window(alert: Mapping[str, Any]) -> tuple[int, int, int] | None:
    try:
        signed = int(alert["signed_delta_raw"])
        old_block = int(alert["old_checkpoint"]["block_number"])
        new_block = int(alert["new_checkpoint"]["block_number"])
    except (KeyError, TypeError, ValueError):
        return None
    if signed == 0:
        return None
    return signed, old_block, new_block


def _stored_transfer(
    body: Mapping[str, Any], wallet: Any, network: Any, asset: str, old_block: int, new_block: int,
) -> bool:
    if body.get("alert") is not True or body.get("kind") not in {"native_eth", "asset_usd", "native_unit"}:
        return False
    if body.get("wallet_id") != wallet or body.get("network") != network:
        return False
    if body.get("execution_evidence") == "limited" or body.get("attribution") == "indexer_reported":
        return False
    found_asset = body.get("asset")
    if asset and isinstance(found_asset, str) and found_asset and found_asset.lower() != asset:
        return False
    block = _as_int(body.get("block_number"))
    amount = _as_int(body.get("raw_amount"))
    if block is None or amount is None or not old_block < block <= new_block:
        return False
    return body.get("direction") in {"in", "out"}


def _insufficient(item: Any) -> bool:
    evidence = getattr(item, "execution_evidence", None)
    attribution = getattr(item, "attribution", None)
    if evidence == "limited" or attribution == "indexer_reported":
        return True
    return evidence not in {None, "evidenced"}


def _asset_mismatch(item: Any, asset: str) -> bool:
    found = getattr(item, "asset", None)
    if not isinstance(found, str) or not found:
        return False
    return found.lower() != asset


def _components(matched: list[Any], fees: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        {
            "asset": getattr(item, "asset", None),
            "direction": item.direction,
            "raw_amount": str(item.raw_amount),
            "role": "transfer",
            "tx_id": item.tx_id,
        }
        for item in matched
    ]
    rows.extend(fees)
    return rows


def _fee_components(database: Any, tenant: str, alert: Mapping[str, Any]) -> list[dict[str, Any]]:
    window = _window(alert)
    if window is None:
        return []
    _signed, old_block, new_block = window
    fees = _fee_rows(
        getattr(database, "conn", None),
        tenant,
        alert.get("wallet_id"),
        alert.get("network"),
        str(alert.get("asset") or "").lower(),
        old_block,
        new_block,
    )
    return [] if fees is None else fees


def _fee_rows(
    conn: Any, tenant: Any, wallet: Any, network: Any, asset: str, old_block: int, new_block: int,
) -> list[dict[str, Any]] | None:
    if conn is None or not isinstance(tenant, str) or not isinstance(wallet, str) or not isinstance(network, str):
        return []
    try:
        rows = conn.execute(
            "SELECT payload FROM chain_events WHERE tenant_id=? AND wallet_id=? AND network=?",
            (tenant, wallet, network),
        ).fetchall()
    except Exception:
        return None
    found: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        body = payload if isinstance(payload, dict) else json.loads(payload)
        if not isinstance(body, dict) or body.get("kind") != "fee":
            continue
        block = _as_int(body.get("block_number"))
        if block is None or not old_block < block <= new_block:
            continue
        found_asset = body.get("asset")
        if asset and isinstance(found_asset, str) and found_asset and found_asset.lower() != asset:
            continue
        if body.get("tx_status") != "success":
            return None
        amount = _as_int(body.get("raw_amount"))
        if amount is None:
            return None
        found.append({
            "asset": found_asset,
            "direction": "out",
            "raw_amount": str(amount),
            "role": "fee",
            "tx_id": body.get("tx_id"),
        })
    return found


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _transfer_key(item: Any) -> str:
    identity = getattr(item, "identity", "")
    return str(identity)

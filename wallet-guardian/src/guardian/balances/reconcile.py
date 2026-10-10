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
    if body.get("execution_evidence") != "evidenced" or body.get("attribution") == "indexer_reported":
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
    """Positive execution evidence is required. Missing markers stay limited."""
    evidence = getattr(item, "execution_evidence", None)
    attribution = getattr(item, "attribution", None)
    if attribution == "indexer_reported":
        return True
    return evidence != "evidenced"


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
        if body.get("finality") == "orphaned":
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


def claim_notification(conn: Any, tenant_id: str, alert: dict[str, Any]) -> bool:
    """Decide a pending notification inside the caller's transaction.

    Either producer may call this. False means the other record already holds
    the movement: the alert may still be stored, and a sent outbox row is left
    as it is. True means this call may insert the pending row. A transfer is
    judged with every eligible sibling already stored in this transaction. When
    that complete set covers a delta that already has a notification, pending
    component notices are marked superseded and linked. They are not marked sent.
    """
    kind = alert.get("kind")
    if kind == "balance_delta":
        keys, components = movement_link(conn, tenant_id, alert)
        if not keys:
            return True
        alert["components"] = components
        alert["linked_transfers"] = keys
        dedup = alert.get("dedup_key")
        if isinstance(dedup, str):
            for key in keys:
                _insert_link(conn, tenant_id, dedup, key)
        return False
    if kind in {"native_eth", "asset_usd", "native_unit"}:
        found = _deltas_covering(conn, tenant_id, alert)
        if not found:
            return True
        for dedup, components, link_keys in found:
            _enrich_stored_delta(conn, tenant_id, dedup, link_keys, components)
            _supersede_pending_components(conn, tenant_id, dedup, link_keys)
        return False
    return True


def _deltas_covering(
    conn: Any, tenant_id: str, alert: Mapping[str, Any],
) -> list[tuple[str, list[dict[str, Any]], list[str]]]:
    """Stored balance deltas this transfer already explains, with their components."""
    if alert.get("alert") is not True:
        return []
    if alert.get("execution_evidence") != "evidenced" or alert.get("attribution") == "indexer_reported":
        return []
    wallet = alert.get("wallet_id")
    network = alert.get("network")
    asset = str(alert.get("asset") or "").lower()
    block = _as_int(alert.get("block_number"))
    if block is None or alert.get("direction") not in {"in", "out"}:
        return []
    rows = conn.execute("SELECT payload FROM alerts WHERE tenant_id=?", (tenant_id,)).fetchall()
    bodies: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        body = payload if isinstance(payload, dict) else json.loads(payload)
        if isinstance(body, dict):
            bodies.append(body)
    identity = str(alert.get("identity") or alert.get("dedup_key") or "")
    found: list[tuple[str, list[dict[str, Any]], list[str]]] = []
    for body in bodies:
        if body.get("kind") != "balance_delta" or body.get("alert") is not True:
            continue
        if body.get("wallet_id") != wallet or body.get("network") != network:
            continue
        window = _window(body)
        if window is None:
            continue
        signed, old_block, new_block = window
        if not old_block < block <= new_block:
            continue
        delta_asset = str(body.get("asset") or "").lower()
        if asset and delta_asset and asset != delta_asset:
            continue
        scoped = asset or delta_asset
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()
        for other in bodies:
            if not _stored_transfer(other, wallet, network, scoped, old_block, new_block):
                continue
            key = str(other.get("identity") or other.get("dedup_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            matched.append(other)
        if identity and identity not in seen and _stored_transfer(alert, wallet, network, scoped, old_block, new_block):
            matched.append(alert)
        if not matched:
            continue
        total = 0
        broken = False
        for item in matched:
            amount = _as_int(item.get("raw_amount"))
            if amount is None:
                broken = True
                break
            if item.get("direction") == "in":
                total += amount
            elif item.get("direction") == "out":
                total -= amount
            else:
                broken = True
                break
        if broken:
            continue
        fees = _fee_rows(conn, tenant_id, wallet, network, scoped, old_block, new_block)
        if fees is None:
            continue
        if total - sum(int(item["raw_amount"]) for item in fees) != signed:
            continue
        directions = {item.get("direction") for item in matched}
        if len(directions) > 1 and signed == 0:
            continue
        components = [
            {
                "asset": item.get("asset") or scoped,
                "direction": item.get("direction"),
                "raw_amount": str(item.get("raw_amount")),
                "role": "transfer",
                "tx_id": item.get("tx_id"),
            }
            for item in matched
        ]
        components.extend(fees)
        dedup = body.get("dedup_key")
        link_keys = [_body_key(item) for item in matched]
        link_keys = [key for key in link_keys if key]
        if isinstance(dedup, str) and dedup and link_keys:
            found.append((dedup, components, link_keys))
    return found


def _insert_link(conn: Any, tenant_id: str, observation_key: str, transfer_key: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO balance_explanations(tenant_id, observation_key, transfer_key)
        VALUES (?, ?, ?)
        """,
        (tenant_id, observation_key, transfer_key),
    )


def _body_key(item: Mapping[str, Any]) -> str:
    return str(item.get("identity") or item.get("dedup_key") or "")


def _supersede_pending_components(conn: Any, tenant_id: str, dedup: str, link_keys: list[str]) -> None:
    """Retire pending component notices once the delta already holds the movement.

    A sent receipt is left sent. The evidence alert row is not rewritten.
    """
    keys = {key for key in link_keys if key}
    if not keys:
        return
    rows = list(conn.execute("SELECT id, status, payload FROM outbox WHERE tenant_id=?", (tenant_id,)))
    delta_held = False
    targets: list[tuple[int, dict[str, Any]]] = []
    for outbox in rows:
        current = outbox["payload"]
        body = current if isinstance(current, dict) else json.loads(current)
        if not isinstance(body, dict):
            continue
        if body.get("dedup_key") == dedup and body.get("kind") == "balance_delta":
            delta_held = True
        if outbox["status"] != "pending":
            continue
        if body.get("kind") == "balance_delta" and body.get("dedup_key") == dedup:
            continue
        identity = body.get("identity")
        own = body.get("dedup_key")
        if (isinstance(identity, str) and identity in keys) or (isinstance(own, str) and own in keys):
            targets.append((int(outbox["id"]), body))
    if not delta_held:
        return
    for outbox_id, body in targets:
        body["superseded_by"] = dedup
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)
        conn.execute(
            "UPDATE outbox SET status='superseded', payload=? WHERE id=? AND status='pending'",
            (encoded, outbox_id),
        )


def _enrich_stored_delta(
    conn: Any, tenant_id: str, dedup: str, transfer_keys: list[str], components: list[dict[str, Any]],
) -> None:
    """Attach the transfers to the stored delta. Pending payload may follow. Sent rows stay sent."""
    row = conn.execute(
        "SELECT id, payload FROM alerts WHERE tenant_id=? AND dedup_key=?",
        (tenant_id, dedup),
    ).fetchone()
    for transfer_key in transfer_keys:
        if transfer_key:
            _insert_link(conn, tenant_id, dedup, transfer_key)
    if row is None:
        return
    payload = row["payload"]
    body = payload if isinstance(payload, dict) else json.loads(payload)
    if not isinstance(body, dict):
        return
    links = [str(item) for item in body.get("linked_transfers") or [] if isinstance(item, str) and item]
    for transfer_key in transfer_keys:
        if transfer_key and transfer_key not in links:
            links.append(transfer_key)
    body["linked_transfers"] = links
    body["components"] = components
    if body.get("attribution") in {None, "", "unavailable"}:
        body["attribution"] = "reconciled"
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)
    conn.execute("UPDATE alerts SET payload=? WHERE id=?", (encoded, row["id"]))
    for outbox in conn.execute("SELECT id, status, payload FROM outbox WHERE tenant_id=?", (tenant_id,)):
        if outbox["status"] != "pending":
            continue
        current = outbox["payload"]
        found = current if isinstance(current, dict) else json.loads(current)
        if isinstance(found, dict) and found.get("dedup_key") == dedup:
            conn.execute("UPDATE outbox SET payload=? WHERE id=?", (encoded, outbox["id"]))

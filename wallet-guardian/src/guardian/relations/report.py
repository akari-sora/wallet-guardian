"""Group reads into counterparties or co-recipients."""

from __future__ import annotations

from datetime import datetime, timezone

from guardian.balances.labels import lookup
from guardian.relations.fetch import Edge


def overall(reads: list[dict]) -> str:
    if reads and all(row.get("read_status") == "complete" for row in reads):
        return "complete"
    return "partial"


def inbound_report(edges: list[Edge], reads: list[dict]) -> dict:
    rows = _groups(edges, "sender", {})
    return {"counterparties": _list_or_null(rows, reads)}


def expand_report(
    edges: list[Edge],
    reads: list[dict],
    *,
    payer: str,
    network: str,
    seeds: list[str],
    family: str,
    hub_threshold: int,
    expand_hub: bool,
    payer_is_contract: bool | None,
) -> dict:
    seed_txs = {
        (edge.network, edge.tx_hash)
        for edge in edges
        if any(_same(family, edge.recipient, seed) for seed in seeds)
    }
    kept = [
        edge for edge in edges
        if edge.network == network and not _same(family, edge.recipient, payer)
        and not any(_same(family, edge.recipient, seed) for seed in seeds)
    ]
    rows = _groups(kept, "recipient", seed_txs)
    rows.sort(key=lambda row: (
        not row["same_tx_as_seed"],
        -row["count"],
        -row["_last"],
        row["address"],
    ))
    for row in rows:
        row.pop("_last", None)
    reasons: list[str] = []
    if payer_is_contract is True:
        reasons.append("contract")
    found = lookup(network, payer)
    if found is not None:
        reasons.append("label")
    known = any(row.get("read_status") in {"complete", "partial"} for row in reads)
    count: int | None
    if not known and not rows:
        count = None
    else:
        count = len(rows)
    if count is not None and count > hub_threshold:
        reasons.append("recipient_count")
    hub = bool(reasons)
    body: dict = {
        "hub": hub,
        "hub_reasons": reasons,
        "network": network,
        "payer": payer,
        "recipient_count": count,
    }
    if hub and expand_hub:
        body["noise"] = "high"
    if not (hub and not expand_hub):
        body["co_recipients"] = _list_or_null(rows, reads)
    return body


def _groups(edges: list[Edge], side: str, seed_txs: dict | set) -> list[dict]:
    buckets: dict[tuple[str, str], list[Edge]] = {}
    for edge in edges:
        address = edge.sender if side == "sender" else edge.recipient
        if side == "sender" and _same(_family_of(edge), edge.sender, edge.recipient):
            continue
        buckets.setdefault((edge.network, address), []).append(edge)
    rows = [_one_group(key, items, side, seed_txs) for key, items in buckets.items()]
    if side == "sender":
        rows.sort(key=lambda row: (-row["count"], -row["_last"], row["address"]))
        for row in rows:
            row.pop("_last", None)
    return rows


def _one_group(key: tuple[str, str], items: list[Edge], side: str, seed_txs) -> dict:
    network, address = key
    stamps = [item.timestamp for item in items]
    hashes: list[str] = []
    for item in items:
        if item.tx_hash not in hashes:
            hashes.append(item.tx_hash)
    flags = [item.sender_contract if side == "sender" else item.recipient_contract for item in items]
    if any(flag is True for flag in flags):
        contract: bool | str = True
    elif any(flag is False for flag in flags):
        contract = False
    else:
        contract = "unknown"
    found = lookup(network, address)
    row = {
        "address": address,
        "assets": _assets(items),
        "count": len(items),
        "first_timestamp": _iso(min(stamps)),
        "is_contract": contract,
        "label": None if found is None else found.get("label"),
        "label_source": None if found is None else found.get("label_source"),
        "last_timestamp": _iso(max(stamps)),
        "network": network,
        "transfers": [_transfer(item) for item in items],
        "tx_hash_count": len(hashes),
        "tx_hashes": hashes[:20],
        "_last": max(stamps),
    }
    if side == "recipient":
        row["same_tx_as_seed"] = any((item.network, item.tx_hash) in seed_txs for item in items)
    return row


def _assets(items: list[Edge]) -> list[dict]:
    buckets: dict[tuple[str, str], list[Edge]] = {}
    for item in items:
        buckets.setdefault((item.kind, item.contract or ""), []).append(item)
    rows = []
    for (kind, contract), group in buckets.items():
        sample = group[0]
        unknown = any(item.raw_amount is None for item in group)
        asset = {
            "count": len(group),
            "decimals": sample.decimals,
            "kind": kind,
            "raw_amount": None if unknown else str(sum(item.raw_amount or 0 for item in group)),
            "symbol": sample.symbol,
        }
        if kind == "token":
            asset["contract"] = contract or None
        rows.append(asset)
    rows.sort(key=lambda row: (row["kind"], row.get("contract") or "", row.get("symbol") or ""))
    return rows


def _transfer(item: Edge) -> dict:
    asset = {
        "decimals": item.decimals,
        "kind": item.kind,
        "symbol": item.symbol,
    }
    if item.kind == "token":
        asset["contract"] = item.contract
    return {
        "asset": asset,
        "block_number": item.block_number,
        "raw_amount": None if item.raw_amount is None else str(item.raw_amount),
        "source_id": item.source_id,
        "timestamp": _iso(item.timestamp),
        "tx_hash": item.tx_hash,
    }


def _list_or_null(rows: list[dict], reads: list[dict]):
    if rows:
        return rows
    if reads and all(row.get("read_status") == "complete" for row in reads):
        return []
    return None


def _same(family: str, left: str, right: str) -> bool:
    if family == "evm":
        return left.lower() == right.lower()
    return left == right


def _family_of(edge: Edge) -> str:
    if edge.sender.startswith("0x") or edge.recipient.startswith("0x"):
        return "evm"
    return "tron"


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

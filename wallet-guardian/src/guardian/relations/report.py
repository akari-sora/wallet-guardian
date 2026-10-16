"""Group reads into counterparties or co-recipients."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from guardian.balances.labels import lookup
from guardian.relations.evidence import evidence_for, seed_summary
from guardian.relations.fetch import Edge
from guardian.relations.labels import HUB_KINDS, match_label


def overall(reads: list[dict]) -> str:
    if reads and all(row.get("read_status") == "complete" for row in reads):
        return "complete"
    return "partial"


def inbound_report(
    edges: list[Edge],
    reads: list[dict],
    *,
    max_nodes: int = 500,
    labels: Sequence[Mapping] | None = None,
    registry: Mapping | None = None,
) -> dict:
    rows = _groups(edges, "sender", {}, labels or [], registry or {})
    shown, truncated = _cap_rows(rows, max_nodes)
    listed = _list_or_null(shown, reads)
    body: dict = {"counterparties": listed}
    if listed is None:
        body["merged_addresses"] = None
    else:
        body["merged_addresses"] = _merged(shown, with_score=False)
    if truncated is not None and isinstance(body["counterparties"], list):
        body["truncated"] = truncated
    return body


def expand_report(
    edges: list[Edge],
    reads: list[dict],
    *,
    payer: str,
    networks: Sequence[str],
    seeds: list[str],
    family: str,
    hub_threshold: int,
    expand_hub: bool,
    payer_is_contract: bool | None,
    max_nodes: int = 500,
    source_complete: bool | None = None,
    contract_withhold: bool = False,
    labels: Sequence[Mapping] | None = None,
    registry: Mapping | None = None,
) -> dict:
    network_list = [network for network in networks if isinstance(network, str) and network]
    network_set = set(network_list)
    user_labels = labels or []
    chain_registry = registry or {}
    counted = [
        edge for edge in edges
        if edge.network in network_set and not _same(family, edge.recipient, payer)
    ]
    seed_edges = [
        edge for edge in counted
        if any(_same(family, edge.recipient, seed) for seed in seeds)
    ]
    summary = seed_summary(seed_edges)
    seed_txs = {(edge.network, edge.tx_hash) for edge in seed_edges}
    rows = _groups(counted, "recipient", seed_txs, user_labels, chain_registry)
    unique = {_merge_key(family, edge.recipient) for edge in counted}
    if source_complete is None:
        source_complete = bool(reads) and all(row.get("read_status") == "complete" for row in reads)
    any_complete = any(row.get("read_status") == "complete" for row in reads)
    count: int | None
    if not counted and not any_complete:
        count = None
    else:
        count = len(unique)
    reasons: list[str] = []
    if payer_is_contract is True:
        reasons.append("contract")
    if _label_hub(network_list, payer, user_labels, chain_registry):
        reasons.append("label")
    if count is not None and count > hub_threshold:
        reasons.append("recipient_count")
    hub: bool | None
    if reasons:
        hub = True
    elif source_complete and payer_is_contract is False:
        hub = False
    else:
        hub = None
    visible = [
        row for row in rows
        if not any(_same(family, row["address"], seed) for seed in seeds)
    ]
    by_key: dict[str, list[Edge]] = {}
    for edge in counted:
        if any(_same(family, edge.recipient, seed) for seed in seeds):
            continue
        by_key.setdefault(_merge_key(family, edge.recipient), []).append(edge)
    cached: dict[str, dict] = {}
    for row in visible:
        key = _merge_key(family, row["address"])
        if key not in cached:
            cached[key] = evidence_for(by_key.get(key, []), summary)
        built = cached[key]
        row["confidence"] = built["confidence"]
        row["evidence"] = [dict(item) for item in built["evidence"]]
        row["reasons"] = list(built["reasons"])
        row["score"] = built["score"]
    visible.sort(key=lambda row: (
        -row["score"],
        0 if _has_kind(row, "same_tx") else 1,
        -row["count"],
        -row["_last"],
        row["address"],
    ))
    for row in visible:
        row.pop("_last", None)
    shown, truncated = _cap_rows(visible, max_nodes)
    if not counted and not any_complete:
        statuses = [row.get("read_status") for row in reads]
        if statuses and all(status in {"gap", "unsupported"} for status in statuses):
            receipts, note = None, "seed_receipts_unknown"
        else:
            receipts, note = None, None
    else:
        receipts, note = _seed_fields(reads, summary)
    body: dict = {
        "hub": hub,
        "hub_reasons": reasons,
        "networks": network_list,
        "payer": payer,
        "recipient_count": count,
        "recipient_count_complete": source_complete,
        "seed_receipts": receipts,
    }
    if len(network_list) == 1:
        body["network"] = network_list[0]
    if note:
        body["evidence_note"] = note
    body.update(_payer_label(network_list, payer, user_labels, chain_registry))
    if hub is True and expand_hub:
        body["noise"] = "high"
    if (hub is True or contract_withhold) and not expand_hub:
        return body
    if shown:
        body["co_recipients"] = shown
    elif source_complete:
        body["co_recipients"] = []
    else:
        body["co_recipients"] = None
    if isinstance(body["co_recipients"], list):
        body["merged_addresses"] = _merged(shown, with_score=True)
    else:
        body["merged_addresses"] = None
    if truncated is not None and isinstance(body["co_recipients"], list):
        body["truncated"] = truncated
    return body


def _groups(edges: list[Edge], side: str, seed_txs: dict | set, labels, registry) -> list[dict]:
    buckets: dict[tuple[str, str], list[Edge]] = {}
    for edge in edges:
        address = edge.sender if side == "sender" else edge.recipient
        if side == "sender" and _same(_family_of(edge), edge.sender, edge.recipient):
            continue
        buckets.setdefault((edge.network, address), []).append(edge)
    rows = [_one_group(key, items, side, seed_txs, labels, registry) for key, items in buckets.items()]
    if side == "sender":
        rows.sort(key=lambda row: (-row["count"], -row["_last"], row["address"]))
        for row in rows:
            row.pop("_last", None)
    return rows


def _one_group(key: tuple[str, str], items: list[Edge], side: str, seed_txs, labels, registry) -> dict:
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
    row = {
        "address": address,
        "assets": _assets(items),
        "count": len(items),
        "first_timestamp": _iso(min(stamps)),
        "is_contract": contract,
        "last_timestamp": _iso(max(stamps)),
        "network": network,
        "transfers": [_transfer(item) for item in items],
        "tx_hash_count": len(hashes),
        "tx_hashes": hashes[:20],
        "_last": max(stamps),
    }
    _apply_label(row, network, address, labels, registry)
    if side == "recipient":
        row["same_tx_as_seed"] = any((item.network, item.tx_hash) in seed_txs for item in items)
    return row


def _apply_label(row: dict, network: str, address: str, labels, registry) -> None:
    family = _network_family(registry, network)
    user = match_label(labels, network=network, address=address, family=family)
    static = lookup(network, address)
    if user is not None:
        row["label"] = user["label"]
        row["label_source"] = "user_private_file"
        row["label_kind"] = user["kind"]
        if static is not None and isinstance(static.get("label"), str):
            row["static_label"] = static["label"]
        return
    row["label"] = None if static is None else static.get("label")
    row["label_source"] = None if static is None else static.get("label_source")


def _payer_label(networks: Sequence[str], payer: str, labels, registry) -> dict:
    user = None
    static = None
    for network in networks:
        family = _network_family(registry, network)
        if user is None:
            user = match_label(labels, network=network, address=payer, family=family)
        if static is None:
            found = lookup(network, payer)
            if found is not None:
                static = found
    if user is not None:
        extra = {
            "label": user["label"],
            "label_kind": user["kind"],
            "label_source": "user_private_file",
        }
        if static is not None and isinstance(static.get("label"), str):
            extra["static_label"] = static["label"]
        return extra
    if static is not None:
        return {"label": static.get("label"), "label_source": static.get("label_source")}
    return {}


def _label_hub(networks: Sequence[str], payer: str, labels, registry) -> bool:
    for network in networks:
        if lookup(network, payer) is not None:
            return True
        family = _network_family(registry, network)
        user = match_label(labels, network=network, address=payer, family=family)
        if user is not None and user.get("kind") in HUB_KINDS:
            return True
    return False


def _merged(rows: Sequence[dict], *, with_score: bool) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        address = str(row.get("address") or "")
        if not address.startswith("0x"):
            continue
        buckets.setdefault(address.lower(), []).append(row)
    merged = []
    for group in buckets.values():
        networks = sorted({str(row.get("network") or "") for row in group})
        if len(networks) < 2:
            continue
        flags = [row.get("is_contract") for row in group]
        if any(flag is True for flag in flags):
            contract: bool | str = True
        elif flags and all(flag is False for flag in flags):
            contract = False
        else:
            contract = "unknown"
        entry = {
            "address": group[0]["address"],
            "count": sum(int(row.get("count") or 0) for row in group),
            "first_timestamp": min(str(row.get("first_timestamp") or "") for row in group),
            "is_contract": contract,
            "last_timestamp": max(str(row.get("last_timestamp") or "") for row in group),
            "networks": networks,
            "tx_hash_count": sum(int(row.get("tx_hash_count") or 0) for row in group),
        }
        if with_score:
            entry["confidence"] = group[0].get("confidence")
            entry["score"] = group[0].get("score")
        merged.append(entry)
    merged.sort(key=lambda row: row["address"])
    return merged


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


def _seed_fields(reads: list[dict], summary: dict) -> tuple[dict | None, str | None]:
    statuses = [row.get("read_status") for row in reads]
    count = summary["count"]
    known = {"assets": summary["assets"], "count": count}
    if statuses and all(status in {"gap", "unsupported"} for status in statuses):
        return None, "seed_receipts_unknown"
    all_complete = bool(statuses) and all(status == "complete" for status in statuses)
    usable = any(status in {"complete", "partial"} for status in statuses)
    if count == 0 and usable and not all_complete:
        return known, "no_seed_receipts_in_partial_read"
    if count == 0:
        return known, "no_seed_receipts"
    return known, None


def _cap_rows(rows: list[dict], max_nodes: int) -> tuple[list[dict], dict | None]:
    if len(rows) > max_nodes:
        return rows[:max_nodes], {"reason": "node_cap", "total": len(rows)}
    return rows, None


def _list_or_null(rows: list[dict], reads: list[dict]):
    if rows:
        return rows
    if reads and all(row.get("read_status") == "complete" for row in reads):
        return []
    return None


def _has_kind(row: dict, kind: str) -> bool:
    return any(item.get("kind") == kind for item in row.get("evidence") or [])


def _merge_key(family: str, address: str) -> str:
    if family == "evm":
        return address.lower()
    return address


def _same(family: str, left: str, right: str) -> bool:
    if family == "evm":
        return left.lower() == right.lower()
    return left == right


def _family_of(edge: Edge) -> str:
    if edge.sender.startswith("0x") or edge.recipient.startswith("0x"):
        return "evm"
    return "tron"


def _network_family(registry: Mapping, network: str) -> str:
    entry = registry.get(network) if isinstance(registry, Mapping) else None
    if isinstance(entry, Mapping) and isinstance(entry.get("family"), str):
        return entry["family"]
    if network == "tron-mainnet":
        return "tron"
    return "evm"


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

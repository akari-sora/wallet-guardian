"""Heuristic evidence for expand co-recipients. Absent evidence is omitted."""

from __future__ import annotations

import re
from collections.abc import Sequence

from guardian.relations.fetch import Edge

_ORDER = ("same_tx", "same_asset", "amount_band", "cadence")
_WEIGHTS = {"same_tx": 3, "same_asset": 1, "amount_band": 1, "cadence": 1}
_SYMBOL = re.compile(r"^[0-9A-Za-z._-]{1,16}$")


def seed_summary(edges: Sequence[Edge]) -> dict:
    amounts: dict[tuple[str, str, str], list[int]] = {}
    samples: dict[tuple[str, str, str], dict] = {}
    timestamps: list[int] = []
    tx_keys: set[tuple[str, str]] = set()
    for edge in edges:
        key = _asset_key(edge)
        timestamps.append(edge.timestamp)
        tx_keys.add((edge.network, edge.tx_hash))
        if key not in samples:
            samples[key] = _sample(edge)
        if edge.raw_amount is not None:
            amounts.setdefault(key, []).append(edge.raw_amount)
    assets = [_public_asset(samples[key]) for key in sorted(samples)]
    return {
        "amounts": amounts,
        "assets": assets,
        "count": len(edges),
        "samples": samples,
        "timestamps": timestamps,
        "tx_keys": tx_keys,
    }


def evidence_for(edges: Sequence[Edge], summary: dict) -> dict:
    if summary["count"] == 0 or not edges:
        return _scored([])
    found: list[dict] = []
    same_tx = _same_tx(edges, summary)
    if same_tx is not None:
        found.append(same_tx)
    same_asset = _same_asset(edges, summary)
    if same_asset is not None:
        found.append(same_asset)
    amount = _amount_band(edges, summary)
    if amount is not None:
        found.append(amount)
    cadence = _cadence(summary["timestamps"], [edge.timestamp for edge in edges])
    if cadence is not None:
        found.append(cadence)
    found.sort(key=lambda item: _ORDER.index(item["kind"]))
    return _scored(found)


def _same_tx(edges: Sequence[Edge], summary: dict) -> dict | None:
    seen: set[tuple[str, str]] = set()
    hashes: list[str] = []
    for edge in sorted(edges, key=lambda item: (item.timestamp, item.tx_hash)):
        token = (edge.network, edge.tx_hash)
        if token not in summary["tx_keys"] or token in seen:
            continue
        seen.add(token)
        hashes.append(edge.tx_hash)
    if not hashes:
        return None
    return {"kind": "same_tx", "tx_hashes": hashes[:5], "transaction_count": len(hashes)}


def _same_asset(edges: Sequence[Edge], summary: dict) -> dict | None:
    assets = []
    seen: set[tuple[str, str, str]] = set()
    for edge in sorted(edges, key=lambda item: _asset_key(item)):
        key = _asset_key(edge)
        if key not in summary["samples"] or key in seen:
            continue
        seen.add(key)
        assets.append(_public_asset(summary["samples"][key]))
    if not assets:
        return None
    assets.sort(key=lambda item: (item["network"], item["kind"], item["contract"], item.get("symbol") or ""))
    return {"assets": assets, "kind": "same_asset"}


def _amount_band(edges: Sequence[Edge], summary: dict) -> dict | None:
    own = sorted(edges, key=lambda item: (item.timestamp, item.tx_hash))
    for key in sorted(summary["amounts"]):
        seed_amounts = summary["amounts"][key]
        if not seed_amounts or key not in { _asset_key(edge) for edge in own }:
            continue
        min_seed = min(seed_amounts)
        max_seed = max(seed_amounts)
        low = min_seed * 80 // 100
        high = (max_seed * 120 + 99) // 100
        matched = [
            edge.raw_amount
            for edge in own
            if _asset_key(edge) == key and edge.raw_amount is not None and low <= edge.raw_amount <= high
        ]
        if not matched:
            continue
        sample = summary["samples"][key]
        return {
            "asset": {
                "contract": sample["contract"],
                "kind": sample["kind"],
                "network": sample["network"],
                "symbol": sample["symbol"],
            },
            "decimals": sample["decimals"],
            "high": str(high),
            "kind": "amount_band",
            "low": str(low),
            "matched_raw_amounts": [str(amount) for amount in matched[:5]],
        }
    return None


def _cadence(seed_stamps: Sequence[int], own_stamps: Sequence[int]) -> dict | None:
    seed_unique = sorted(set(seed_stamps))
    own_unique = sorted(set(own_stamps))
    if len(seed_unique) < 2 or len(own_unique) < 2:
        return None
    seed_m2 = _double_median(seed_unique)
    own_m2 = _double_median(own_unique)
    if seed_m2 is None or own_m2 is None:
        return None
    if seed_m2 < 7200 or own_m2 < 7200:
        return None
    if abs(seed_m2 - own_m2) * 100 > 20 * max(seed_m2, own_m2):
        return None
    return {
        "kind": "cadence",
        "median_seconds": _json_median(own_m2),
        "seed_median_seconds": _json_median(seed_m2),
    }


def _double_median(unique_stamps: Sequence[int]) -> int | None:
    """Twice the median gap. Odd counts use the middle gap times two."""
    if len(unique_stamps) < 2:
        return None
    gaps = [unique_stamps[index + 1] - unique_stamps[index] for index in range(len(unique_stamps) - 1)]
    gaps.sort()
    mid = len(gaps) // 2
    if len(gaps) % 2 == 1:
        return gaps[mid] * 2
    return gaps[mid - 1] + gaps[mid]


def _json_median(doubled: int) -> int | float:
    if doubled % 2 == 0:
        return doubled // 2
    return doubled / 2


def _scored(items: list[dict]) -> dict:
    score = sum(_WEIGHTS[item["kind"]] for item in items)
    if score >= 4:
        confidence = "high"
    elif score >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    public = []
    for item in items:
        copied = dict(item)
        copied.pop("transaction_count", None)
        public.append(copied)
    return {
        "confidence": confidence,
        "evidence": public,
        "reasons": [_reason(item) for item in items],
        "score": score,
    }


def _reason(item: dict) -> str:
    kind = item["kind"]
    if kind == "same_tx":
        count = item["transaction_count"]
        return f"paid in the same transaction as a seed address ({count} transactions)"
    if kind == "same_asset":
        return "received an asset the seed addresses also received from this payer"
    if kind == "amount_band":
        raw = item["matched_raw_amounts"][0]
        decimals = item["decimals"] if item["decimals"] is not None else "null"
        name = _asset_name(item["asset"])
        return (
            f"received {raw} (decimals {decimals}) of {name}, "
            f"inside {item['low']}\u2013{item['high']} derived from the seed receipts"
        )
    median = item["median_seconds"]
    seed_median = item["seed_median_seconds"]
    return f"median interval {median}s is within 20% of the seed median {seed_median}s"


def display_symbol(symbol: object, contract: object) -> str:
    """Whitelist for sentences and Markdown. Source fields keep the raw symbol."""
    if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol):
        return symbol
    if isinstance(contract, str) and contract:
        return contract
    return "native"


def _asset_name(asset: dict) -> str:
    return display_symbol(asset.get("symbol"), asset.get("contract"))


def _asset_key(edge: Edge) -> tuple[str, str, str]:
    return (edge.network, edge.kind, edge.contract or "")


def _sample(edge: Edge) -> dict:
    return {
        "contract": edge.contract or "",
        "decimals": edge.decimals,
        "kind": edge.kind,
        "network": edge.network,
        "symbol": edge.symbol,
    }


def _public_asset(sample: dict) -> dict:
    return {
        "contract": sample["contract"],
        "decimals": sample["decimals"],
        "kind": sample["kind"],
        "network": sample["network"],
        "symbol": sample["symbol"],
    }

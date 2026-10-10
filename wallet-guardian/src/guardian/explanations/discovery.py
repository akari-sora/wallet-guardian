"""Bounded log discovery and swap-and-pop replay. Amounts are not identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode
from eth_utils import keccak

from guardian.chains.evm.accounting import normalize_address, pad_address
from guardian.explanations.models import LOG_SPAN_MAX
from guardian.explanations.registry import ExplainerManifest
from guardian.explanations.rpc import ExplanationRpc


@dataclass
class Slot:
    identity: str
    uuid: int | None
    slis: int
    bnb: int | None = None
    tx_id: str | None = None
    log_index: int | None = None
    block_hash: str = ""
    removed: bool = False
    conflict: bool = False


@dataclass
class FoundLog:
    kind: str
    address: str
    account: str
    tx_id: str
    block_number: int
    tx_index: int
    log_index: int
    data_words: tuple[int, ...]
    raw: dict[str, Any]
    removed: bool = False
    block_hash: str = ""
    conflict: bool = False


@dataclass
class ReplayLink:
    log: FoundLog
    slot: Slot | None
    reason: str


def replay(start: list[Slot], logs: list[FoundLog]) -> tuple[list[ReplayLink], list[Slot]]:
    """Apply request appends and claim swap-and-pop in log order. Index is not identity."""
    queue = list(start)
    links: list[ReplayLink] = []
    ordered = sorted(logs, key=lambda item: (item.block_number, item.tx_index, item.log_index))
    for log in ordered:
        if log.removed or log.conflict:
            continue
        if log.kind == "request":
            slis = log.data_words[0] if log.data_words else 0
            queue.append(Slot(
                _request_identity(log), None, slis, None, log.tx_id, log.log_index,
                log.block_hash, False, False,
            ))
            continue
        if log.kind != "claim":
            continue
        idx = log.data_words[0] if log.data_words else -1
        if idx < 0 or idx >= len(queue):
            links.append(ReplayLink(log, None, "index_missing"))
            continue
        chosen = queue[idx]
        queue[idx] = queue[-1]
        queue.pop()
        links.append(ReplayLink(log, chosen, "linked"))
    return links, queue


def discover_logs(
    rpc: ExplanationRpc,
    manifest: ExplainerManifest,
    account: str,
    start_block: int,
    end_block: int,
    *,
    window_high: int | None = None,
) -> tuple[list[FoundLog], str | None, int]:
    """Claim and request logs inside one bounded slice of ``(start, end]``.

    The third value is the next exclusive high block, or ``-1`` when this
    interval is fully covered. A wider span is resumable (``truncated_logs``),
    not an empty result.
    """
    if end_block <= start_block:
        return [], "empty_interval", -1
    high = end_block if window_high is None else window_high
    if high <= start_block:
        return [], None, -1
    owner = normalize_address(account)
    if owner is None:
        return [], "bad_address", -1
    incomplete = high - start_block > LOG_SPAN_MAX
    low = high - LOG_SPAN_MAX if incomplete else start_block
    found: list[FoundLog] = []
    for signature, kind in (
        ("ClaimWithdrawal(address,uint256,uint256)", "claim"),
        ("RequestWithdraw(address,uint256)", "request"),
    ):
        spec = manifest.event(signature)
        if spec is None:
            return [], "stale_abi", high
        topic = str(spec["topic"]).lower()
        rows, error = rpc.call("eth_getLogs", [{
            "address": manifest.proxy(),
            "fromBlock": hex(low + 1),
            "toBlock": hex(high),
            "topics": [topic, pad_address(owner)],
        }])
        if error:
            return [], error, high
        if not isinstance(rows, list):
            return [], "logs_unavailable", high
        for row in rows:
            parsed = _parse_log(row, kind, topic, manifest.proxy(), owner)
            if parsed is None:
                continue
            found.append(parsed)
    return found, ("truncated_logs" if incomplete else None), (low if incomplete else -1)


def lookback_bounds(old_block: int, cursor: int | None) -> tuple[int, int, int] | None:
    """One bounded request search at or before the old checkpoint.

    Returns ``(low_inclusive, high_inclusive, next_cursor)``. ``next_cursor``
    is ``0`` when the search reached genesis. ``None`` means nothing remains.
    """
    if cursor == 0 or old_block <= 0:
        return None
    high = old_block if cursor is None else cursor
    if high <= 0:
        return None
    low = max(0, high - LOG_SPAN_MAX)
    return low, high, (0 if low == 0 else low)


def reconstruct_genesis_queue(slots: list[Slot], logs: list[FoundLog]) -> bool:
    """Link slots by replaying every request and claim from an empty queue.

    ``logs`` has to cover genesis through the block whose queue is ``slots``.
    The only log with a matching slis amount is not a link. A removed log, a
    conflicting copy, or a log with no block hash does not become an identity.
    Returns False without writing when the replay does not reproduce ``slots``.
    """
    _links, queue = replay([], [item for item in logs if item.kind in {"request", "claim"}])
    if len(queue) != len(slots):
        return False
    if any(left.slis != right.slis for left, right in zip(queue, slots)):
        return False
    linked: list[tuple[str, int, str]] = []
    for rebuilt in queue:
        if (
            not rebuilt.tx_id
            or rebuilt.log_index is None
            or not rebuilt.block_hash
            or rebuilt.removed
            or rebuilt.conflict
        ):
            return False
        linked.append((rebuilt.tx_id, int(rebuilt.log_index), rebuilt.block_hash))
    for slot, (tx_id, log_index, _block_hash) in zip(slots, linked):
        if slot.tx_id and slot.log_index is not None and (slot.tx_id, slot.log_index) != (tx_id, log_index):
            return False
    for slot, (tx_id, log_index, block_hash) in zip(slots, linked):
        if slot.tx_id and slot.log_index is not None:
            continue
        slot.tx_id = tx_id
        slot.log_index = log_index
        slot.block_hash = block_hash
        slot.removed = False
        slot.conflict = False
    return True


def union_logs(existing: list[FoundLog], found: list[FoundLog]) -> list[FoundLog]:
    """Keep one copy of each transaction and log index.

    A second copy that disagrees on kind, order, amount, block hash, or the
    removal flag marks the kept row as a conflict. The two copies are not merged
    into the cleaner one.
    """
    merged: list[FoundLog] = []
    index: dict[tuple[str, int], FoundLog] = {}
    for item in [*existing, *found]:
        key = (item.tx_id, item.log_index)
        prior = index.get(key)
        if prior is None:
            index[key] = item
            merged.append(item)
            continue
        if _same_provenance(prior, item):
            continue
        prior.conflict = True
        prior.removed = prior.removed or item.removed
    return merged


def _same_provenance(left: FoundLog, right: FoundLog) -> bool:
    return (
        left.kind == right.kind
        and left.address == right.address
        and left.account == right.account
        and left.block_number == right.block_number
        and left.tx_index == right.tx_index
        and left.data_words == right.data_words
        and left.removed == right.removed
        and left.block_hash == right.block_hash
        and left.conflict == right.conflict
    )


def log_record(log: FoundLog) -> dict[str, Any]:
    topic = ""
    topics = log.raw.get("topics") if isinstance(log.raw, dict) else None
    if isinstance(topics, list) and topics:
        topic = str(topics[0])
    return {
        "account": log.account,
        "address": log.address,
        "block_hash": log.block_hash,
        "block_number": log.block_number,
        "conflict": bool(log.conflict),
        "data_words": list(log.data_words),
        "kind": log.kind,
        "log_index": log.log_index,
        "removed": bool(log.removed),
        "topic": topic,
        "tx_id": log.tx_id,
        "tx_index": log.tx_index,
    }


def log_from_record(row: dict[str, Any]) -> FoundLog:
    topic = str(row.get("topic") or "")
    words = tuple(int(item) for item in row.get("data_words") or [])
    block_hash = str(row.get("block_hash") or "")
    removed = row.get("removed") in {True, 1, "1", "true"}
    conflict = row.get("conflict") in {True, 1, "1", "true"}
    return FoundLog(
        str(row.get("kind") or ""),
        str(row.get("address") or ""),
        str(row.get("account") or ""),
        str(row.get("tx_id") or ""),
        int(row.get("block_number") or 0),
        int(row.get("tx_index") or 0),
        int(row.get("log_index") or 0),
        words,
        {"blockHash": block_hash, "removed": removed, "topics": [topic]},
        removed,
        block_hash.lower(),
        conflict,
    )


def _parse_log(row: Any, kind: str, topic: str, proxy: str, account: str) -> FoundLog | None:
    if not isinstance(row, dict):
        return None
    address = normalize_address(row.get("address"))
    if address != proxy:
        return None
    topics = row.get("topics")
    if not isinstance(topics, list) or not topics or str(topics[0]).lower() != topic:
        return None
    if len(topics) < 2:
        return None
    logged = _topic_address(topics[1])
    if logged != account:
        return None
    tx_id = row.get("transactionHash")
    if not isinstance(tx_id, str) or not tx_id.startswith("0x"):
        return None
    block = _hex_int(row.get("blockNumber"))
    tx_index = _hex_int(row.get("transactionIndex"))
    log_index = _hex_int(row.get("logIndex"))
    if block is None or tx_index is None or log_index is None:
        return None
    words = _data_words(row.get("data"), 1 if kind == "request" else 2)
    if words is None:
        return None
    return FoundLog(
        kind, address, account, tx_id.lower(), block, tx_index, log_index, words, row,
        _removed_flag(row), _block_hash(row), False,
    )


def _removed_flag(row: dict[str, Any]) -> bool:
    return row.get("removed") in {True, 1, "true", "0x1", "0x01"}


def _block_hash(row: dict[str, Any]) -> str:
    digest = row.get("blockHash")
    if isinstance(digest, str) and digest.startswith("0x") and len(digest) == 66:
        return digest.lower()
    return ""


def _data_words(data: Any, count: int) -> tuple[int, ...] | None:
    if not isinstance(data, str) or not data.startswith("0x"):
        return None
    raw = data[2:]
    if len(raw) != count * 64:
        return None
    try:
        blob = bytes.fromhex(raw)
    except ValueError:
        return None
    kinds = ["uint256"] * count
    try:
        decoded = decode(kinds, blob)
    except Exception:
        return None
    return tuple(int(item) for item in decoded)


def _topic_address(topic: Any) -> str | None:
    if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
        return None
    return normalize_address("0x" + topic[-40:])


def _hex_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _request_identity(log: FoundLog) -> str:
    return f"{log.tx_id}:{log.log_index}"


def upgraded_topic() -> str:
    return "0x" + keccak(b"Upgraded(address)").hex()


def request_identity(chain_id: int, manager: str, account: str, tx_id: str, log_index: int) -> str:
    """Stable identity. The array index and the uuid are not part of it."""
    return f"eip155:{chain_id}:{manager}:{account}:{tx_id}:{log_index}"

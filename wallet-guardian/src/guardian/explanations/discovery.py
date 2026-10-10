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
        if log.kind == "request":
            slis = log.data_words[0] if log.data_words else 0
            queue.append(Slot(_request_identity(log), None, slis, None, log.tx_id, log.log_index))
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
) -> tuple[list[FoundLog], str | None]:
    """Claim and request logs for one account on the registered manager, inside the interval."""
    if end_block <= start_block:
        return [], "empty_interval"
    if end_block - start_block > LOG_SPAN_MAX:
        return [], "truncated_logs"
    owner = normalize_address(account)
    if owner is None:
        return [], "bad_address"
    found: list[FoundLog] = []
    for signature, kind in (
        ("ClaimWithdrawal(address,uint256,uint256)", "claim"),
        ("RequestWithdraw(address,uint256)", "request"),
    ):
        spec = manifest.event(signature)
        if spec is None:
            return [], "stale_abi"
        topic = str(spec["topic"]).lower()
        rows, error = rpc.call("eth_getLogs", [{
            "address": manifest.proxy(),
            "fromBlock": hex(start_block + 1),
            "toBlock": hex(end_block),
            "topics": [topic, pad_address(owner)],
        }])
        if error:
            return [], error
        if not isinstance(rows, list):
            return [], "logs_unavailable"
        for row in rows:
            parsed = _parse_log(row, kind, topic, manifest.proxy(), owner)
            if parsed is None:
                continue
            found.append(parsed)
    return found, None


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
    return FoundLog(kind, address, account, tx_id.lower(), block, tx_index, log_index, words, row)


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

"""ERC-20, ERC-721, and ERC-1155 log accounting.

Amounts stay raw integers. A topic layout this module does not know becomes
``unknown`` and is not given a zero amount. A self-transfer is not an inflow
plus an outflow. The same transaction and log index from two sources counts once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from eth_hash.auto import keccak

from guardian.amounts import format_units, parse_raw

_SOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDRESS = "0x" + "0" * 40


def _topic(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii")).hex()


TRANSFER_TOPIC = _topic("Transfer(address,address,uint256)")
TRANSFER_SINGLE_TOPIC = _topic("TransferSingle(address,address,address,uint256,uint256)")
TRANSFER_BATCH_TOPIC = _topic("TransferBatch(address,address,address,uint256[],uint256[])")


def pad_address(address: str) -> str:
    normalized = normalize_address(address)
    if normalized is None:
        raise ValueError("address")
    return "0x" + normalized[2:].rjust(64, "0")


def hex_word(value: int) -> str:
    number = parse_raw(value)
    return "0x" + number.to_bytes(32, "big").hex()


def normalize_address(value: Any) -> str | None:
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        return None
    return "0x" + value[2:].lower()


@dataclass(frozen=True)
class TokenMovement:
    tx_id: str
    log_index: int
    sub_index: int
    kind: str
    direction: str
    token: str
    token_id: str | None
    raw_amount: str | None
    decimals: int | None
    source_ids: tuple[str, ...]
    flags: tuple[str, ...] = ()
    counterparty: str = ""

    @property
    def display_amount(self) -> str | None:
        if self.raw_amount is None or self.decimals is None or self.kind != "erc20":
            return None
        return format_units(self.raw_amount, self.decimals)


@dataclass(frozen=True)
class TokenLedger:
    movements: tuple[TokenMovement, ...]
    flags: tuple[str, ...]

    def net(self, token: str, token_id: str | None = None) -> int:
        total = 0
        wanted = normalize_address(token) or token
        for movement in self.movements:
            if movement.token != wanted or movement.token_id != token_id:
                continue
            total += _signed(movement)
        return total

    def inbound(self, token: str, token_id: str | None = None) -> int:
        return _flow(self.movements, token, token_id, {"in", "mint"})

    def outbound(self, token: str, token_id: str | None = None) -> int:
        return _flow(self.movements, token, token_id, {"out", "burn"})


def account_logs(
    logs: Sequence[Mapping[str, Any]],
    wallet: str,
    *,
    decimals: Mapping[str, int] | None = None,
    balance_deltas: Mapping[str, int] | None = None,
) -> TokenLedger:
    """Parse logs for ``wallet``. Unrelated transfers are omitted; unknown layouts stay."""
    owner = normalize_address(wallet)
    if owner is None:
        raise ValueError("wallet")
    groups: dict[tuple[str, int], list[TokenMovement]] = {}
    order: list[tuple[str, int]] = []
    loose = 0
    for log in logs:
        key = _log_key(log)
        if key is None:
            loose += 1
            key = ("", loose)
        parsed = _parse_log(log, owner, decimals)
        if key in groups:
            groups[key] = _merge_duplicate(groups[key], parsed)
            continue
        groups[key] = parsed
        order.append(key)
    movements = tuple(item for key in order for item in groups[key])
    return TokenLedger(movements, _flags(movements, balance_deltas))


def movements_to_effects(movements: Sequence[TokenMovement]) -> list[dict[str, Any]]:
    """Asset effects only. Self-transfers and unknown layouts are not effects."""
    effects: list[dict[str, Any]] = []
    for movement in movements:
        if movement.raw_amount is None or movement.direction in {"self", "unknown"}:
            continue
        if movement.direction in {"in", "mint"}:
            kind = "asset_in"
        elif movement.direction in {"out", "burn"}:
            kind = "asset_out"
        else:
            continue
        asset = movement.token if movement.token_id is None else f"{movement.token}:{movement.token_id}"
        effects.append({
            "asset": asset,
            "kind": kind,
            "raw_amount": movement.raw_amount,
            "sub_index": f"{movement.log_index}:{movement.sub_index}",
        })
    return effects


def _flow(movements: Sequence[TokenMovement], token: str, token_id: str | None, directions: set[str]) -> int:
    wanted = normalize_address(token) or token
    total = 0
    for movement in movements:
        if movement.token != wanted or movement.token_id != token_id:
            continue
        if movement.direction in directions and movement.raw_amount is not None:
            total += int(movement.raw_amount)
    return total


def _signed(movement: TokenMovement) -> int:
    if movement.raw_amount is None or movement.direction in {"self", "unknown"}:
        return 0
    amount = int(movement.raw_amount)
    if movement.direction in {"in", "mint"}:
        return amount
    if movement.direction in {"out", "burn"}:
        return -amount
    return 0


def _flags(movements: Sequence[TokenMovement], balance_deltas: Mapping[str, int] | None) -> tuple[str, ...]:
    found: list[str] = []
    for movement in movements:
        for flag in movement.flags:
            if flag not in found:
                found.append(flag)
    if not balance_deltas:
        return tuple(found)
    for token, delta in balance_deltas.items():
        if isinstance(delta, bool) or not isinstance(delta, int):
            if "amount_mismatch" not in found:
                found.append("amount_mismatch")
            continue
        wanted = normalize_address(token) or token
        if _fungible_net(movements, wanted) != delta and "amount_mismatch" not in found:
            found.append("amount_mismatch")
    return tuple(found)


def _fungible_net(movements: Sequence[TokenMovement], token: str) -> int:
    total = 0
    for movement in movements:
        if movement.token == token and movement.token_id is None:
            total += _signed(movement)
    return total


def _merge_duplicate(left: list[TokenMovement], right: list[TokenMovement]) -> list[TokenMovement]:
    sources: list[str] = []
    for movement in (*left, *right):
        for source in movement.source_ids:
            if source not in sources:
                sources.append(source)
    mismatch = not _same_shape(left, right)
    merged: list[TokenMovement] = []
    for movement in left:
        flags = movement.flags
        if mismatch and "amount_mismatch" not in flags:
            flags = (*flags, "amount_mismatch")
        merged.append(replace(movement, source_ids=tuple(sources), flags=flags))
    return merged


def _same_shape(left: Sequence[TokenMovement], right: Sequence[TokenMovement]) -> bool:
    if len(left) != len(right):
        return False
    for first, second in zip(left, right):
        if (
            first.kind,
            first.direction,
            first.token,
            first.token_id,
            first.raw_amount,
            first.sub_index,
        ) != (
            second.kind,
            second.direction,
            second.token,
            second.token_id,
            second.raw_amount,
            second.sub_index,
        ):
            return False
    return True


def _parse_log(log: Mapping[str, Any], wallet: str, decimals: Mapping[str, int] | None) -> list[TokenMovement]:
    if not isinstance(log, Mapping):
        return [_unknown("unknown-tx", 0, "unknown-token", ())]
    topics = log.get("topics")
    base_token = normalize_address(log.get("address")) or "unknown-token"
    source = _source(log)
    sources = (source,) if source else ()
    tx_id = _tx(log) or "unknown-tx"
    index = _log_index(log)
    log_index = 0 if index is None else index
    if not isinstance(topics, list) or not topics or any(not isinstance(item, str) for item in topics):
        return [_unknown(tx_id, log_index, base_token, sources)]
    topic0 = topics[0].lower()
    if topic0 == TRANSFER_TOPIC:
        return _parse_transfer(topics, log.get("data"), wallet, tx_id, log_index, base_token, sources, decimals)
    if topic0 == TRANSFER_SINGLE_TOPIC:
        return _parse_single(topics, log.get("data"), wallet, tx_id, log_index, base_token, sources)
    if topic0 == TRANSFER_BATCH_TOPIC:
        return _parse_batch(topics, log.get("data"), wallet, tx_id, log_index, base_token, sources)
    return [_unknown(tx_id, log_index, base_token, sources)]


def _parse_transfer(
    topics: list[Any],
    data: Any,
    wallet: str,
    tx_id: str,
    log_index: int,
    token: str,
    sources: tuple[str, ...],
    decimals: Mapping[str, int] | None,
) -> list[TokenMovement]:
    if len(topics) == 3:
        sender = _topic_address(topics[1])
        receiver = _topic_address(topics[2])
        amount = _one_word(data)
        if sender is None or receiver is None or amount is None:
            return [_unknown(tx_id, log_index, token, sources)]
        direction = _direction(sender, receiver, wallet)
        if direction == "unknown":
            return []
        return [_movement(tx_id, log_index, 0, "erc20", direction, token, None, str(amount), _decimals(token, decimals), sources, _counterparty(sender, receiver, wallet, direction))]
    if len(topics) == 4:
        sender = _topic_address(topics[1])
        receiver = _topic_address(topics[2])
        token_id = _topic_uint(topics[3])
        if sender is None or receiver is None or token_id is None or _data_nonempty(data):
            return [_unknown(tx_id, log_index, token, sources)]
        direction = _direction(sender, receiver, wallet)
        if direction == "unknown":
            return []
        return [_movement(tx_id, log_index, 0, "erc721", direction, token, str(token_id), "1", None, sources, _counterparty(sender, receiver, wallet, direction))]
    return [_unknown(tx_id, log_index, token, sources)]


def _parse_single(
    topics: list[Any],
    data: Any,
    wallet: str,
    tx_id: str,
    log_index: int,
    token: str,
    sources: tuple[str, ...],
) -> list[TokenMovement]:
    if len(topics) != 4:
        return [_unknown(tx_id, log_index, token, sources)]
    sender = _topic_address(topics[2])
    receiver = _topic_address(topics[3])
    words = _words(data, 2)
    if sender is None or receiver is None or words is None:
        return [_unknown(tx_id, log_index, token, sources)]
    direction = _direction(sender, receiver, wallet)
    if direction == "unknown":
        return []
    token_id, amount = words
    party = _counterparty(sender, receiver, wallet, direction)
    return [_movement(tx_id, log_index, 0, "erc1155", direction, token, str(token_id), str(amount), None, sources, party)]


def _parse_batch(
    topics: list[Any],
    data: Any,
    wallet: str,
    tx_id: str,
    log_index: int,
    token: str,
    sources: tuple[str, ...],
) -> list[TokenMovement]:
    if len(topics) != 4:
        return [_unknown(tx_id, log_index, token, sources)]
    sender = _topic_address(topics[2])
    receiver = _topic_address(topics[3])
    pairs = _decode_batch(data)
    if sender is None or receiver is None or pairs is None:
        return [_unknown(tx_id, log_index, token, sources)]
    direction = _direction(sender, receiver, wallet)
    if direction == "unknown":
        return []
    if not pairs:
        return []
    party = _counterparty(sender, receiver, wallet, direction)
    return [
        _movement(tx_id, log_index, index, "erc1155", direction, token, str(token_id), str(amount), None, sources, party)
        for index, (token_id, amount) in enumerate(pairs)
    ]


def _movement(
    tx_id: str,
    log_index: int,
    sub_index: int,
    kind: str,
    direction: str,
    token: str,
    token_id: str | None,
    raw_amount: str | None,
    decimals: int | None,
    sources: tuple[str, ...],
    counterparty: str = "",
) -> TokenMovement:
    return TokenMovement(
        tx_id, log_index, sub_index, kind, direction, token, token_id, raw_amount, decimals, sources, (), counterparty,
    )


def _counterparty(sender: str, receiver: str, wallet: str, direction: str) -> str:
    if direction in {"in", "mint"}:
        party = sender
    elif direction in {"out", "burn"}:
        party = receiver
    else:
        party = ""
    if party in {"", wallet, ZERO_ADDRESS}:
        return ""
    return party


def _unknown(tx_id: str, log_index: int, token: str, sources: tuple[str, ...]) -> TokenMovement:
    return _movement(tx_id, log_index, 0, "unknown", "unknown", token, None, None, None, sources)


def _direction(sender: str, receiver: str, wallet: str) -> str:
    if sender == ZERO_ADDRESS and receiver == wallet:
        return "mint"
    if receiver == ZERO_ADDRESS and sender == wallet:
        return "burn"
    if sender == wallet and receiver == wallet:
        return "self"
    if receiver == wallet:
        return "in"
    if sender == wallet:
        return "out"
    return "unknown"


def _log_key(log: Mapping[str, Any]) -> tuple[str, int] | None:
    tx_id = _tx(log)
    index = _log_index(log)
    if tx_id is None or index is None:
        return None
    return (tx_id, index)


def _tx(log: Mapping[str, Any]) -> str | None:
    for key in ("transactionHash", "hash", "transaction_hash"):
        value = log.get(key)
        if isinstance(value, str) and value.startswith("0x") and len(value) >= 4:
            return value.lower()
    return None


def _log_index(log: Mapping[str, Any]) -> int | None:
    for key in ("logIndex", "log_index"):
        if key in log:
            return _quantity(log.get(key))
    return None


def _source(log: Mapping[str, Any]) -> str:
    value = log.get("source_id")
    if isinstance(value, str) and _SOURCE.fullmatch(value):
        return value
    return "log"


def _decimals(token: str, table: Mapping[str, int] | None) -> int | None:
    if table is None:
        return None
    value = None
    for key, candidate in table.items():
        normalized = normalize_address(key)
        if normalized == token or key == token:
            value = candidate
            break
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 255:
        return None
    return value


def _topic_address(topic: Any) -> str | None:
    raw = _hex_bytes(topic)
    if raw is None or len(raw) != 32:
        return None
    return "0x" + raw[-20:].hex()


def _topic_uint(topic: Any) -> int | None:
    raw = _hex_bytes(topic)
    if raw is None or len(raw) != 32:
        return None
    try:
        return parse_raw(int.from_bytes(raw, "big"))
    except ValueError:
        return None


def _one_word(data: Any) -> int | None:
    words = _words(data, 1)
    if words is None:
        return None
    return words[0]


def _words(data: Any, count: int) -> tuple[int, ...] | None:
    raw = _hex_bytes(data)
    if raw is None or len(raw) != 32 * count:
        return None
    values = []
    for index in range(count):
        number = int.from_bytes(raw[index * 32:(index + 1) * 32], "big")
        try:
            values.append(parse_raw(number))
        except ValueError:
            return None
    return tuple(values)


def _data_nonempty(data: Any) -> bool:
    raw = _hex_bytes(data)
    if raw is None:
        return True
    return len(raw) != 0


def _decode_batch(data: Any) -> list[tuple[int, int]] | None:
    raw = _hex_bytes(data)
    if raw is None or len(raw) < 64 or len(raw) % 32 != 0:
        return None
    offset_ids = int.from_bytes(raw[0:32], "big")
    offset_values = int.from_bytes(raw[32:64], "big")
    ids = _read_array(raw, offset_ids)
    values = _read_array(raw, offset_values)
    if ids is None or values is None or len(ids) != len(values):
        return None
    return list(zip(ids, values))


def _read_array(raw: bytes, offset: int) -> list[int] | None:
    if offset < 0 or offset % 32 != 0 or offset + 32 > len(raw):
        return None
    length = int.from_bytes(raw[offset:offset + 32], "big")
    if length > 1024:
        return None
    end = offset + 32 + length * 32
    if end > len(raw):
        return None
    items = []
    for index in range(length):
        start = offset + 32 + index * 32
        try:
            items.append(parse_raw(int.from_bytes(raw[start:start + 32], "big")))
        except ValueError:
            return None
    return items


def _hex_bytes(value: Any) -> bytes | None:
    if value is None:
        return b""
    if not isinstance(value, str) or not value.startswith(("0x", "0X")):
        return None
    body = value[2:]
    if len(body) % 2 == 1:
        return None
    try:
        return bytes.fromhex(body)
    except ValueError:
        return None


def _quantity(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if text.startswith(("0x", "0X")):
            return int(text, 16)
        if text.isdigit():
            return int(text)
    except ValueError:
        return None
    return None

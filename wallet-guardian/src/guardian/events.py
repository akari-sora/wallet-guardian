"""Normalized chain events, effects, and finality.

Pure functions. No sockets, no signing, no broadcast. A failed transaction does
not become an asset or approval success. Bitcoin confirmation depth is a policy
threshold, not absolute finality. Source-chain finality is not a cross-chain
completion.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence

_TX_STATUS = frozenset({"success", "failed", "unknown"})
_FINALITY = frozenset({"observed", "confirmed", "finalized", "orphaned", "unknown"})
_KINDS = frozenset({
    "asset_in",
    "asset_out",
    "fee",
    "approval_set",
    "approval_revoked",
    "operator_set",
    "delegate_set",
    "tx_failed",
})
_SUCCESS_KINDS = frozenset({
    "asset_in",
    "asset_out",
    "approval_set",
    "approval_revoked",
    "operator_set",
    "delegate_set",
})
_FAMILY_MODE = {
    "evm": "finalized_tag",
    "solana": "solana_commitment",
    "bitcoin": "confirmation_threshold",
    "tron": "finalized_tag",
    "ton": "finalized_tag",
    "sui": "finalized_tag",
    "aptos": "finalized_tag",
    "cosmos": "finalized_tag",
}
_SOLANA_COMMITMENT = {
    "processed": "observed",
    "confirmed": "confirmed",
    "finalized": "finalized",
}


class FlowStatus(str, Enum):
    initiated = "initiated"
    source_confirmed = "source_confirmed"
    source_finalized = "source_finalized"
    destination_observed = "destination_observed"
    completed = "completed"
    refunded = "refunded"
    timed_out = "timed_out"
    unknown = "unknown"


def event_identity(
    tenant_id: str,
    wallet_id: str,
    network: str,
    tx_id: str,
    sub_index: str,
    kind: str,
    asset: str,
) -> str:
    """Identity ignores block hash and finality so a re-observed event matches."""
    material = "\x1f".join((tenant_id, wallet_id, network, tx_id, sub_index, kind, asset))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _raw_amount(value: Any) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("raw amount must be a decimal integer string")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("raw amount must be non-negative")
        return str(value)
    if not isinstance(value, str):
        raise ValueError("raw amount must be a decimal integer string")
    text = value.strip()
    if not text or text[0] == "+" or not text.isdigit():
        raise ValueError("raw amount must be a non-negative integer string")
    return str(int(text))


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is required")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} is malformed")
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is malformed")
    return value


def _block_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith(("0x", "0X")):
        try:
            number = int(text, 16)
        except ValueError:
            return None
        return number if number >= 0 else None
    if text.isdigit():
        return int(text)
    return None


@dataclass(frozen=True)
class ChainEvent:
    tenant_id: str
    wallet_id: str
    network: str
    chain_uid: str
    tx_id: str
    sub_index: str
    kind: str
    asset: str
    raw_amount: str
    block_number: str | None
    block_hash: str | None
    tx_status: str
    finality: str
    source_ids: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "wallet_id", _text(self.wallet_id, "wallet_id"))
        object.__setattr__(self, "network", _text(self.network, "network"))
        object.__setattr__(self, "chain_uid", _text(self.chain_uid, "chain_uid"))
        object.__setattr__(self, "tx_id", _text(self.tx_id, "tx_id"))
        if isinstance(self.sub_index, int) and not isinstance(self.sub_index, bool):
            object.__setattr__(self, "sub_index", str(self.sub_index))
        else:
            object.__setattr__(self, "sub_index", _text(self.sub_index, "sub_index"))
        object.__setattr__(self, "kind", _text(self.kind, "kind"))
        if self.kind not in _KINDS:
            raise ValueError("unknown event kind")
        object.__setattr__(self, "asset", _text(self.asset, "asset"))
        object.__setattr__(self, "raw_amount", _raw_amount(self.raw_amount))
        object.__setattr__(self, "block_number", _optional_text(self.block_number, "block_number"))
        object.__setattr__(self, "block_hash", _optional_text(self.block_hash, "block_hash"))
        if self.tx_status not in _TX_STATUS:
            raise ValueError("tx_status is required")
        if self.finality not in _FINALITY:
            raise ValueError("finality is required")
        if isinstance(self.source_ids, str) or not isinstance(self.source_ids, tuple | list):
            raise ValueError("source_ids must be a list of strings")
        sources = tuple(self.source_ids)
        if any(not isinstance(item, str) or not item for item in sources):
            raise ValueError("source_ids must be a list of strings")
        object.__setattr__(self, "source_ids", sources)
        object.__setattr__(self, "observed_at", _text(self.observed_at, "observed_at"))

    @property
    def identity(self) -> str:
        return event_identity(
            self.tenant_id,
            self.wallet_id,
            self.network,
            self.tx_id,
            self.sub_index,
            self.kind,
            self.asset,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "block_hash": self.block_hash,
            "block_number": self.block_number,
            "chain_uid": self.chain_uid,
            "finality": self.finality,
            "identity": self.identity,
            "kind": self.kind,
            "network": self.network,
            "observed_at": self.observed_at,
            "raw_amount": self.raw_amount,
            "source_ids": list(self.source_ids),
            "sub_index": self.sub_index,
            "tenant_id": self.tenant_id,
            "tx_id": self.tx_id,
            "tx_status": self.tx_status,
            "wallet_id": self.wallet_id,
        }


@dataclass(frozen=True)
class Correction:
    identity: str
    reason: str
    block_number: str | None
    previous_block_hash: str | None
    previous_finality: str
    replacement_block_hash: str | None = None


@dataclass(frozen=True)
class ReconcileResult:
    events: tuple[ChainEvent, ...]
    added: tuple[ChainEvent, ...]
    updated: tuple[ChainEvent, ...]
    orphaned: tuple[ChainEvent, ...]
    corrections: tuple[Correction, ...]
    noop_identities: tuple[str, ...]


def success_alertable(event: ChainEvent) -> bool:
    """Unknown or failed transactions are not success alerts. Fees are not asset success."""
    return (
        event.tx_status == "success"
        and event.finality != "orphaned"
        and event.kind in _SUCCESS_KINDS
    )


def transaction_status(tx: Mapping[str, Any]) -> str:
    """Fail closed. EVM status 0x0 and Solana meta.err are both failures."""
    flags: list[str] = []
    meta = tx.get("meta")
    if isinstance(meta, dict) and "err" in meta:
        flags.append("failed" if meta.get("err") is not None else "success")
    if "status" in tx or "tx_status" in tx:
        flags.append(_parse_status(tx.get("status", tx.get("tx_status"))))
    if not flags:
        return "unknown"
    if "failed" in flags:
        return "failed"
    if "unknown" in flags:
        return "unknown"
    if all(flag == "success" for flag in flags):
        return "success"
    return "unknown"


def _parse_status(status: Any) -> str:
    if isinstance(status, bool):
        return "success" if status else "failed"
    if isinstance(status, int):
        if status == 1:
            return "success"
        if status == 0:
            return "failed"
        return "unknown"
    if not isinstance(status, str):
        return "unknown"
    token = status.strip().lower()
    if token in {"0x1", "0x01", "success", "successful", "ok"}:
        return "success"
    if token in {"0x0", "0x00", "failed", "reverted", "0"}:
        return "failed"
    if token == "unknown":
        return "unknown"
    return "unknown"


def _fee_amount(tx: Mapping[str, Any]) -> tuple[str, str] | None:
    fee = tx.get("fee")
    if isinstance(fee, dict) and "raw_amount" in fee:
        amount = _raw_amount(fee.get("raw_amount"))
        asset = fee.get("asset") or "native"
        if not isinstance(asset, str) or not asset:
            raise ValueError("fee asset is required")
        if amount == "0":
            return None
        return asset, amount
    meta = tx.get("meta")
    if isinstance(meta, dict) and meta.get("fee") is not None:
        amount = _raw_amount(meta.get("fee"))
        if amount == "0":
            return None
        return "native", amount
    if "gas_used" in tx and ("effective_gas_price" in tx or "gas_price" in tx):
        used = int(_raw_amount(tx.get("gas_used")))
        price = int(_raw_amount(tx.get("effective_gas_price", tx.get("gas_price"))))
        amount = str(used * price)
        if amount == "0":
            return None
        return "native", amount
    return None


def _effect_rows(tx: Mapping[str, Any], status: str) -> list[dict[str, Any]]:
    if status == "failed":
        return []
    raw = tx.get("effects")
    if raw is None:
        value = tx.get("value")
        direction = tx.get("direction")
        if value is None or direction not in {"in", "out"}:
            return []
        asset = tx.get("value_asset") or "native"
        if not isinstance(asset, str) or not asset:
            raise ValueError("value asset is required")
        return [{
            "kind": "asset_in" if direction == "in" else "asset_out",
            "asset": asset,
            "raw_amount": value,
            "sub_index": "0",
        }]
    if not isinstance(raw, list):
        raise ValueError("effects must be a list")
    return [item for item in raw if isinstance(item, dict)]


def effects_from_tx(tx: Mapping[str, Any]) -> list[ChainEvent]:
    """Build events for one transaction.

    A failed or reverted transaction yields no asset, approval, operator, or
    delegate success. It may yield the fee actually paid and one tx_failed
    event. Unknown status keeps the effects but they are not success alerts.
    """
    if not isinstance(tx, Mapping):
        raise ValueError("transaction must be an object")
    status = transaction_status(tx)
    common = {
        "tenant_id": tx.get("tenant_id"),
        "wallet_id": tx.get("wallet_id"),
        "network": tx.get("network"),
        "chain_uid": tx.get("chain_uid"),
        "tx_id": tx.get("tx_id"),
        "block_number": tx.get("block_number"),
        "block_hash": tx.get("block_hash"),
        "tx_status": status,
        "finality": tx.get("finality") or "unknown",
        "source_ids": tuple(tx.get("source_ids") or ()),
        "observed_at": tx.get("observed_at"),
    }
    events: list[ChainEvent] = []
    for effect in _effect_rows(tx, status):
        kind = effect.get("kind")
        if status == "failed" and kind in _SUCCESS_KINDS:
            continue
        sub_index = effect.get("sub_index")
        if isinstance(sub_index, bool) or not isinstance(sub_index, (str, int)):
            raise ValueError("sub_index is required")
        events.append(ChainEvent(
            sub_index=str(sub_index),
            kind=kind,
            asset=effect.get("asset"),
            raw_amount=effect.get("raw_amount"),
            **common,
        ))
    fee = _fee_amount(tx)
    if fee is not None:
        asset, amount = fee
        events.append(ChainEvent(
            sub_index="fee",
            kind="fee",
            asset=asset,
            raw_amount=amount,
            **common,
        ))
    if status == "failed":
        events.append(ChainEvent(
            sub_index="status",
            kind="tx_failed",
            asset="native",
            raw_amount="0",
            **common,
        ))
    return events


def _finalized_tag(block_number: Any, head: Any, finalized_head: Any, policy: Mapping[str, Any]) -> str:
    block = _block_int(block_number)
    tip = _block_int(head)
    if block is None or tip is None or block > tip:
        return "unknown"
    finalized = _block_int(finalized_head)
    if finalized is not None and block <= finalized:
        return "finalized"
    safe = _block_int(policy.get("safe_head"))
    if safe is not None and block <= safe:
        return "confirmed"
    return "observed"


def _solana_commitment(block_number: Any, head: Any, policy: Mapping[str, Any]) -> str:
    block = _block_int(block_number)
    tip = _block_int(head)
    if block is None:
        return "unknown"
    if tip is not None and block > tip:
        return "unknown"
    commitment = policy.get("commitment")
    if not isinstance(commitment, str):
        return "unknown"
    return _SOLANA_COMMITMENT.get(commitment, "unknown")


def _confirmation_threshold(block_number: Any, head: Any, policy: Mapping[str, Any] | None = None) -> str:
    """Policy depth is confirmed, never finalized. Missing threshold does not invent 6 or 12."""
    block = _block_int(block_number)
    tip = _block_int(head)
    if block is None or tip is None or block > tip:
        return "unknown"
    confirmations = tip - block + 1
    threshold = None if policy is None else policy.get("confirmation_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        return "observed"
    if confirmations >= threshold:
        return "confirmed"
    return "observed"


def finality_for(
    network: str,
    block_number: Any,
    head: Any,
    finalized_head: Any,
    policy: Mapping[str, Any],
) -> str:
    """Per-network finality. Confirmation count 12 is not a global finalized rule.

    EVM uses the finalized tag (and an optional safe head), never a fixed depth.
    Solana uses its commitment name. Bitcoin's threshold can yield confirmed only.
    """
    if not isinstance(network, str) or not network:
        raise ValueError("network is required")
    if not isinstance(policy, Mapping):
        raise ValueError("finality policy is required")
    declared = policy.get("network")
    if isinstance(declared, str) and declared != network:
        raise ValueError("finality policy network does not match")
    text = policy.get("finality_policy", policy.get("text"))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("finality policy text is required")
    mode = policy.get("mode")
    if mode is None:
        mode = _FAMILY_MODE.get(str(policy.get("family") or ""))
    if mode == "finalized_tag":
        return _finalized_tag(block_number, head, finalized_head, policy)
    if mode == "solana_commitment":
        return _solana_commitment(block_number, head, policy)
    if mode == "confirmation_threshold":
        return _confirmation_threshold(block_number, head, policy)
    if mode is None:
        raise ValueError("finality mode is required")
    raise ValueError("unknown finality mode")


def flow_status(
    source_event: ChainEvent | None,
    destination_event: ChainEvent | None = None,
    *,
    refund_event: ChainEvent | None = None,
    timed_out: bool = False,
) -> FlowStatus:
    """Source finalized alone is never completed. Completion needs the destination."""
    if _flow_success(refund_event):
        return FlowStatus.refunded
    if destination_event is not None:
        if _flow_success(destination_event):
            return FlowStatus.completed
        if destination_event.tx_status == "unknown":
            return FlowStatus.destination_observed
    if timed_out:
        return FlowStatus.timed_out
    if source_event is None or source_event.tx_status != "success":
        return FlowStatus.unknown
    if source_event.finality == "finalized":
        return FlowStatus.source_finalized
    if source_event.finality == "confirmed":
        return FlowStatus.source_confirmed
    if source_event.finality == "orphaned":
        return FlowStatus.unknown
    return FlowStatus.initiated


def _flow_success(event: ChainEvent | None) -> bool:
    return event is not None and event.tx_status == "success" and event.kind in {"asset_in", "asset_out"}


def reconcile_events(
    stored: Sequence[ChainEvent],
    incoming: Sequence[ChainEvent],
    canonical: Mapping[Any, Any],
) -> ReconcileResult:
    """Idempotent merge. Re-inclusion updates one identity. Missing canonical events are orphaned."""
    canonical_hashes = {str(key): value for key, value in canonical.items()}
    stored_by: dict[str, ChainEvent] = {}
    for event in stored:
        if event.identity in stored_by:
            raise ValueError("duplicate stored event")
        stored_by[event.identity] = event
    incoming_by: dict[str, ChainEvent] = {}
    for event in incoming:
        previous = incoming_by.get(event.identity)
        incoming_by[event.identity] = event if previous is None else _prefer(previous, event, canonical_hashes)
    added: list[ChainEvent] = []
    updated: list[ChainEvent] = []
    orphaned: list[ChainEvent] = []
    corrections: list[Correction] = []
    noop: list[str] = []
    chosen: list[ChainEvent] = []
    for identity in sorted(set(stored_by) | set(incoming_by)):
        old = stored_by.get(identity)
        new = incoming_by.get(identity)
        if old is not None and new is not None and _same_observation(old, new):
            chosen.append(old)
            noop.append(identity)
            continue
        if old is not None and new is not None:
            current, correction = _accept_incoming(old, new, canonical_hashes)
            chosen.append(current)
            if current.finality == "orphaned" and _contradicts(new, canonical_hashes):
                orphaned.append(current)
            else:
                updated.append(current)
            if correction is not None:
                corrections.append(correction)
            continue
        if new is not None:
            if _contradicts(new, canonical_hashes):
                current = replace(new, finality="orphaned")
                orphaned.append(current)
                corrections.append(_correction(new, "reorg", current.block_hash))
                chosen.append(current)
            else:
                added.append(new)
                chosen.append(new)
            continue
        assert old is not None
        if _contradicts(old, canonical_hashes):
            current = replace(old, finality="orphaned")
            orphaned.append(current)
            corrections.append(_correction(old, "reorg", None))
            chosen.append(current)
        else:
            chosen.append(old)
            noop.append(identity)
    ordered = tuple(_sorted(chosen))
    return ReconcileResult(
        events=ordered,
        added=tuple(_sorted(added)),
        updated=tuple(_sorted(updated)),
        orphaned=tuple(_sorted(orphaned)),
        corrections=tuple(sorted(corrections, key=lambda item: (item.identity, item.reason))),
        noop_identities=tuple(sorted(noop)),
    )


def rollback_to(events: Sequence[ChainEvent], block_number: Any) -> ReconcileResult:
    """Mark events at or above block_number orphaned. Do not delete them."""
    target = _block_int(block_number)
    if target is None:
        raise ValueError("rollback block is required")
    output: list[ChainEvent] = []
    orphaned: list[ChainEvent] = []
    corrections: list[Correction] = []
    noop: list[str] = []
    for event in events:
        number = _block_int(event.block_number)
        if number is not None and number >= target and event.finality != "orphaned":
            current = replace(event, finality="orphaned")
            output.append(current)
            orphaned.append(current)
            corrections.append(_correction(event, "rollback", None))
        else:
            output.append(event)
            noop.append(event.identity)
    return ReconcileResult(
        events=tuple(_sorted(output)),
        added=(),
        updated=(),
        orphaned=tuple(_sorted(orphaned)),
        corrections=tuple(corrections),
        noop_identities=tuple(noop),
    )


def _accept_incoming(
    old: ChainEvent,
    new: ChainEvent,
    canonical: Mapping[str, Any],
) -> tuple[ChainEvent, Correction | None]:
    if _contradicts(new, canonical):
        current = replace(new, finality="orphaned")
        return current, _correction(old, "reorg", current.block_hash)
    reason = None
    if old.block_hash != new.block_hash or old.block_number != new.block_number:
        reason = _correction(old, "reorg_reincluded", new.block_hash)
    elif old.finality != new.finality or old.tx_status != new.tx_status or old.raw_amount != new.raw_amount:
        reason = _correction(old, "updated", new.block_hash)
    return new, reason


def _prefer(left: ChainEvent, right: ChainEvent, canonical: Mapping[str, Any]) -> ChainEvent:
    left_match = not _contradicts(left, canonical)
    right_match = not _contradicts(right, canonical)
    if left_match != right_match:
        return left if left_match else right
    left_number = _block_int(left.block_number)
    right_number = _block_int(right.block_number)
    if left_number is None:
        return right
    if right_number is None:
        return left
    return right if right_number >= left_number else left


def _contradicts(event: ChainEvent, canonical: Mapping[str, Any]) -> bool:
    if event.block_number is None or event.block_number not in canonical:
        return False
    return canonical[event.block_number] != event.block_hash


def _same_observation(left: ChainEvent, right: ChainEvent) -> bool:
    return (
        left.block_number == right.block_number
        and left.block_hash == right.block_hash
        and left.finality == right.finality
        and left.tx_status == right.tx_status
        and left.raw_amount == right.raw_amount
        and left.kind == right.kind
        and left.asset == right.asset
        and left.sub_index == right.sub_index
        and left.chain_uid == right.chain_uid
    )


def _correction(event: ChainEvent, reason: str, replacement: str | None) -> Correction:
    return Correction(
        identity=event.identity,
        reason=reason,
        block_number=event.block_number,
        previous_block_hash=event.block_hash,
        previous_finality=event.finality,
        replacement_block_hash=replacement,
    )


def _sorted(events: Sequence[ChainEvent]) -> list[ChainEvent]:
    return sorted(events, key=_sort_key)


def _sort_key(event: ChainEvent) -> tuple[Any, ...]:
    number = _block_int(event.block_number)
    block_key: tuple[Any, ...] = (0, number) if number is not None else (1, event.block_number or "")
    if event.sub_index.isdigit():
        sub_key: tuple[Any, ...] = (0, int(event.sub_index))
    else:
        sub_key = (1, event.sub_index)
    return (block_key, sub_key, event.identity)

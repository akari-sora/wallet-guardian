"""Receipt status and fees. Asset success still goes through effects_from_tx.

OP Stack chains add l1Fee when the field is present. A missing L1 component is
flagged and is not replaced with a guessed amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from guardian.chains.evm.accounting import account_logs, movements_to_effects
from guardian.events import ChainEvent, effects_from_tx, finality_for, transaction_status

OP_STACK = frozenset({"optimism-mainnet", "base-mainnet"})


@dataclass(frozen=True)
class ReceiptEffects:
    events: tuple[ChainEvent, ...]
    fee_wei: str | None
    fee_incomplete: bool
    tx_status: str
    reason: str


def execution_fee(receipt: Mapping[str, Any], *, op_stack: bool) -> tuple[int | None, bool]:
    """Return ``(wei, fee_incomplete)``. Incomplete means a component was absent."""
    gas = _quantity(receipt.get("gasUsed"))
    price = _quantity(receipt.get("effectiveGasPrice"))
    if gas is None or price is None:
        return None, True
    fee = gas * price
    if not op_stack:
        return fee, False
    if "l1Fee" not in receipt:
        return fee, True
    extra = _quantity(receipt.get("l1Fee"))
    if extra is None:
        return fee, True
    return fee + extra, False


def events_from_receipt(
    receipt: Mapping[str, Any],
    *,
    network: str,
    family: str,
    finality_policy: str,
    chain_id: int,
    native_asset: str,
    tenant_id: str,
    wallet_id: str,
    wallet: str,
    observed_at: str,
    source_id: str = "evm-rpc",
) -> ReceiptEffects:
    if not isinstance(receipt, Mapping):
        return ReceiptEffects((), None, True, "unknown", "receipt_missing")
    if "status" not in receipt:
        return ReceiptEffects((), None, True, "unknown", "status_missing")
    status = receipt.get("status")
    tx_status = transaction_status({"status": status})
    tx_hash = receipt.get("transactionHash") or receipt.get("hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        return ReceiptEffects((), None, True, tx_status, "tx_hash_missing")
    logs = receipt.get("logs")
    if logs is None:
        logs = []
    if not isinstance(logs, list):
        return ReceiptEffects((), None, True, tx_status, "logs_malformed")
    ledger = account_logs([item for item in logs if isinstance(item, Mapping)], wallet)
    fee, incomplete = execution_fee(receipt, op_stack=network in OP_STACK)
    block = _quantity(receipt.get("blockNumber"))
    block_text = None if block is None else str(block)
    finality = "unknown"
    if block_text is not None and isinstance(finality_policy, str) and finality_policy.strip():
        finality = finality_for(
            network,
            block_text,
            block_text,
            None,
            {"family": family, "finality_policy": finality_policy, "network": network},
        )
    tx: dict[str, Any] = {
        "block_number": block_text,
        "chain_uid": f"eip155:{chain_id}",
        "effects": movements_to_effects(ledger.movements),
        "finality": finality,
        "network": network,
        "observed_at": observed_at,
        "source_ids": (source_id,),
        "status": status,
        "tenant_id": tenant_id,
        "tx_id": tx_hash,
        "wallet_id": wallet_id,
    }
    block_hash = receipt.get("blockHash")
    if isinstance(block_hash, str) and block_hash:
        tx["block_hash"] = block_hash
    if fee is not None and fee > 0:
        tx["fee"] = {"asset": native_asset, "raw_amount": str(fee)}
    events = tuple(effects_from_tx(tx))
    fee_text = None if fee is None else str(fee)
    return ReceiptEffects(events, fee_text, incomplete, tx_status, "")


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

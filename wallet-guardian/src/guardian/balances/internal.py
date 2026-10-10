"""Validate NodeReal internal transfers. This is not kind=\"internal\".

kind=\"internal\" in the alert rules means a transfer between the operator's
own wallets. An EVM internal call uses transfer_origin=evm_internal instead.
A failed child is not a value transfer. A missing trace id is limited
attribution, not an invented trace index.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

_FAILED = frozenset({
    "0",
    "0x0",
    "error",
    "fail",
    "failed",
    "reverted",
    "false",
})
_SUCCESS = frozenset({"1", "0x1", "ok", "success", "true"})
_HEX_HASH = 66
_VALUE_CALLS = frozenset({"call", "callcode", "create", "create2", "selfdestruct", "suicide"})
_NON_VALUE_CALLS = frozenset({"delegatecall", "staticcall"})


@dataclass(frozen=True)
class InternalNormalization:
    """Parsed native-value rows. ``gap`` means the page is not a complete empty history."""

    rows: tuple[dict[str, Any], ...]
    gap: str | None = None


def normalize_internal_batch(
    items: Any,
    *,
    chain_id: int | None,
    owner: str,
) -> InternalNormalization:
    """Keep successful native-value frames. Ambiguous rows are a coverage gap."""
    if not isinstance(items, (list, tuple)):
        return InternalNormalization((), "parsing_gap")
    owner_text = owner.lower() if isinstance(owner, str) else ""
    kept: list[dict[str, Any]] = []
    gap: str | None = None
    for item in items:
        verdict, row = _classify(item, chain_id=chain_id, owner=owner_text)
        if verdict == "gap":
            gap = "parsing_gap"
            continue
        if verdict == "keep" and row is not None:
            kept.append(row)
    return InternalNormalization(tuple(kept), gap)


def status_for_refusal(reason: str | None) -> str:
    """Map a provider failure onto the disclosed internal-history status."""
    text = reason or ""
    if text in {"credential_missing"}:
        return "credential_missing"
    if text in {"not_queried", "endpoint_missing", "source_disabled"}:
        return "not_queried"
    if text in {"rate_limited", "429"}:
        return "rate_limited"
    if text in {
        "method_not_found",
        "unauthorized",
        "plan_not_supported",
        "nodereal_chain_not_supported",
    }:
        return "plan_not_supported"
    lowered = text.lower()
    if "rate" in lowered and "limit" in lowered:
        return "rate_limited"
    if "plan" in lowered or "not supported" in lowered or "method not found" in lowered:
        return "plan_not_supported"
    return "read_error"


def _classify(item: Any, *, chain_id: int | None, owner: str) -> tuple[str, dict[str, Any] | None]:
    """Return keep, drop, or gap. A gap is not a silent empty page."""
    if not isinstance(item, Mapping):
        return "gap", None
    call = _call_type(item)
    if call == "ambiguous":
        return "gap", None
    if call == "not_transfer":
        return "drop", None
    receipt = _receipt_state(item)
    if receipt == "ambiguous":
        return "gap", None
    failure = _trace_failure(item)
    success = _explicit_success(item) or receipt == "success"
    if success and failure:
        return "gap", None
    if receipt == "failure" or failure or _explicit_failure(item):
        return "drop", None
    declared = _chain(item)
    if declared is not None and chain_id is not None and declared != chain_id:
        return "drop", None
    sender = _address(item.get("from"))
    receiver = _address(item.get("to"))
    if owner and sender != owner and receiver != owner:
        return "drop", None
    amount = _amount(item.get("value"))
    if amount is None:
        raw = item.get("rawContract")
        if isinstance(raw, Mapping):
            amount = _amount(raw.get("value"))
    if amount is None or amount <= 0:
        return "drop", None
    tx = _hash(item.get("hash") or item.get("tx_hash") or item.get("transactionHash"))
    block = _block(item)
    if tx is None or block is None:
        return "gap", None
    trace, attribution = _trace(item, tx, sender, receiver, amount, block)
    evidenced = success
    execution = "evidenced" if evidenced else "limited"
    if not evidenced:
        attribution = "indexer_reported"
    return "keep", {
        "attribution": attribution,
        "blockHash": _hash(item.get("blockHash")),
        "blockNumber": str(block),
        "execution_evidence": execution,
        "from": sender,
        "hash": tx,
        "isError": "0",
        "to": receiver,
        "traceId": trace,
        "transfer_origin": "evm_internal",
        "value": str(amount),
    }


def _call_type(item: Mapping[str, Any]) -> str | None:
    raw = None
    for key in ("type", "callType", "call_type"):
        if key in item and item.get(key) not in (None, ""):
            raw = item.get(key)
            break
    if raw is None:
        return None
    text = _flag(raw)
    if text in _NON_VALUE_CALLS:
        return "not_transfer"
    if text in _VALUE_CALLS:
        return "value"
    return "ambiguous"


def _receipt_state(item: Mapping[str, Any]) -> str | None:
    raw = None
    for key in ("receiptsStatus", "receipts_status"):
        if key in item:
            raw = item.get(key)
            break
    if raw is None and "receiptsStatus" not in item and "receipts_status" not in item:
        return None
    text = _flag(raw)
    if text in _SUCCESS:
        return "success"
    if text in _FAILED or text in {"0x00"}:
        return "failure"
    return "ambiguous"


def _trace_failure(item: Mapping[str, Any]) -> bool:
    for key in ("callError", "revert"):
        if key not in item:
            continue
        text = item.get(key)
        if isinstance(text, str) and text.strip():
            return True
        if text not in (None, "", 0, "0"):
            return True
    error = item.get("error")
    if isinstance(error, str) and error.strip():
        return True
    if isinstance(error, dict) and error:
        return True
    return False


def _explicit_failure(item: Mapping[str, Any]) -> bool:
    if _flag(item.get("isError")) in {"1", "true"}:
        return True
    for key in ("status", "txreceipt_status"):
        if key not in item:
            continue
        text = _flag(item.get(key))
        if text in _FAILED or "revert" in text or text in {"failed", "error"}:
            return True
    return False


def _explicit_success(item: Mapping[str, Any]) -> bool:
    for key in ("status", "txreceipt_status", "receiptsStatus", "receipts_status"):
        if key not in item:
            continue
        if _flag(item.get(key)) in _SUCCESS:
            return True
    return False


def _trace(
    item: Mapping[str, Any],
    tx: str,
    sender: str,
    receiver: str,
    amount: int,
    block: int,
) -> tuple[str, str]:
    for key in ("uniqueId", "traceId", "traceAddress"):
        found = item.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip(), "trace"
        if isinstance(found, list) and found and all(isinstance(part, int) and not isinstance(part, bool) for part in found):
            return ".".join(str(part) for part in found), "trace"
    material = "\x1f".join((tx, sender, receiver, str(amount), str(block)))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"limited:{digest}", "indexer_reported"


def _chain(item: Mapping[str, Any]) -> int | None:
    for key in ("chainId", "chain_id"):
        parsed = _int(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text.startswith("0x") or len(text) != _HEX_HASH:
        return None
    try:
        int(text[2:], 16)
    except ValueError:
        return None
    return text


def _address(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hash") or value.get("address")
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text.startswith("0x") or len(text) != 42:
        return ""
    return text


def _amount(value: Any) -> int | None:
    parsed = _int(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _block(item: Mapping[str, Any]) -> int | None:
    for key in ("blockNumber", "blockNum", "block_number", "block"):
        parsed = _int(item.get(key))
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.startswith(("0x", "0X")):
                return int(text, 16)
            if text.isdigit() or (text[0] == "-" and text[1:].isdigit()):
                return int(text)
        except ValueError:
            return None
    return None


def _flag(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip().lower()

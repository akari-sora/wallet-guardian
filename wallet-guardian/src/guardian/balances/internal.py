"""Validate NodeReal internal transfers. This is not kind=\"internal\".

kind=\"internal\" in the alert rules means a transfer between the operator's
own wallets. An EVM internal call uses transfer_origin=evm_internal instead.
A failed child is not a value transfer. A missing trace id is limited
attribution, not an invented trace index.
"""

from __future__ import annotations

import hashlib
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


def normalize_internal_batch(
    items: Any,
    *,
    chain_id: int | None,
    owner: str,
) -> list[dict[str, Any]]:
    """Keep value-moving frames the indexer actually returned."""
    if not isinstance(items, (list, tuple)):
        return []
    owner_text = owner.lower() if isinstance(owner, str) else ""
    kept: list[dict[str, Any]] = []
    for item in items:
        row = _one(item, chain_id=chain_id, owner=owner_text)
        if row is not None:
            kept.append(row)
    return kept


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


def _one(item: Any, *, chain_id: int | None, owner: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    if _explicit_failure(item):
        return None
    declared = _chain(item)
    if declared is not None and chain_id is not None and declared != chain_id:
        return None
    tx = _hash(item.get("hash") or item.get("tx_hash") or item.get("transactionHash"))
    if tx is None:
        return None
    sender = _address(item.get("from"))
    receiver = _address(item.get("to"))
    if owner and sender != owner and receiver != owner:
        return None
    amount = _amount(item.get("value"))
    if amount is None:
        raw = item.get("rawContract")
        if isinstance(raw, Mapping):
            amount = _amount(raw.get("value"))
    if amount is None or amount < 0:
        return None
    block = _block(item)
    if block is None:
        return None
    trace, attribution = _trace(item, tx, sender, receiver, amount, block)
    execution = "evidenced" if _explicit_success(item) else "limited"
    if not _explicit_success(item):
        attribution = "indexer_reported"
    return {
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


def _explicit_failure(item: Mapping[str, Any]) -> bool:
    if _flag(item.get("isError")) in {"1", "true"}:
        return True
    error = item.get("error")
    if isinstance(error, str) and error.strip():
        return True
    if error not in (None, "", 0, "0"):
        if isinstance(error, dict) and error:
            return True
    for key in ("status", "callError", "revert"):
        if key not in item:
            continue
        text = _flag(item.get(key))
        if text in _FAILED or text in {"reverted", "failed", "error"}:
            return True
    return False


def _explicit_success(item: Mapping[str, Any]) -> bool:
    for key in ("status", "txreceipt_status"):
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

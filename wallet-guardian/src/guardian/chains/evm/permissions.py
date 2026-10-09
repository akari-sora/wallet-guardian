"""ERC-20 and NFT approval families. Read-only: view calls only.

Owner is the indexed Approval topic, not the transaction sender. A missing
prior is ``first_observed``. A failed current read stays unknown and is not a
revoke, a zero, or a safe result. This module does not encode approve,
permit, revoke, increaseAllowance, or any other state-changing call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from eth_abi import encode
from eth_hash.auto import keccak
from eth_utils import function_signature_to_4byte_selector

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import (
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    normalize_address,
    pad_address,
)
from guardian.chains.evm.adapter import LOG_RANGE_MAX, evm_rpc
from guardian.coverage import CoverageObservation
from guardian.events import ChainEvent, effects_from_tx

HISTORY_INCOMPLETE = "history_incomplete_permissions_unknown"
UNLIMITED_FLOOR = 2**255
_PROBE = "untested"
_PERMISSION_CAPABILITIES = (
    "permissions.erc1155_operator",
    "permissions.erc20",
    "permissions.erc721_operator",
    "permissions.erc721_token",
)
_VIEW_ARGUMENTS = {
    "allowance(address,address)": ("address", "address"),
    "allowance(address,address,address)": ("address", "address", "address"),
    "balanceOf(address)": ("address",),
    "getApproved(uint256)": ("uint256",),
    "isApprovedForAll(address,address)": ("address", "address"),
    "nonceBitmap(address,uint256)": ("address", "uint256"),
    "nonces(address)": ("address",),
    "supportsInterface(bytes4)": ("bytes4",),
}
_CHANGES = frozenset({
    "consumed",
    "decreased",
    "extended",
    "first_observed",
    "increased",
    "new",
    "revoked",
    "unchanged",
    "unknown",
})
_STATUSES = frozenset({
    "active",
    "consumed",
    "expired",
    "inactive",
    "revoked",
    "unknown",
})
_RISKY_LABELS = frozenset({"malicious", "phishing", "risky", "sanctioned", "unlabeled"})
ERC721_INTERFACE = bytes.fromhex("80ac58cd")
ERC1155_INTERFACE = bytes.fromhex("d9b67a26")


def _topic(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii")).hex()


ERC20_APPROVAL_TOPIC = _topic("Approval(address,address,uint256)")
APPROVAL_FOR_ALL_TOPIC = _topic("ApprovalForAll(address,address,bool)")
APPROVAL_LOG_TOPICS = (ERC20_APPROVAL_TOPIC, APPROVAL_FOR_ALL_TOPIC)


def encode_view_call(signature: str, args: Sequence[Any]) -> str:
    """ABI-encode one allowlisted view. Anything else, including permit, is refused."""
    kinds = _VIEW_ARGUMENTS.get(signature)
    if kinds is None or len(args) != len(kinds):
        raise ValueError("not_a_view_call")
    payload = encode(list(kinds), [_coerce(kind, value) for kind, value in zip(kinds, args)])
    selector = function_signature_to_4byte_selector(signature)
    return "0x" + (selector + payload).hex()


def eth_call_operation(to: str, data: str, block: str = "latest"):
    token = normalize_address(to)
    if token is None or not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("bad_call")
    return evm_rpc("eth_call", [{"data": data, "to": token}, block])


def approval_log_operation(owner: str, start: int, end: int):
    wallet = normalize_address(owner)
    if wallet is None:
        raise ValueError("bad_address")
    return evm_rpc("eth_getLogs", [{
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [list(APPROVAL_LOG_TOPICS), pad_address(wallet)],
    }])


def decode_uint256(data: str | None) -> int | None:
    raw = _word(data)
    if raw is None:
        return None
    return int.from_bytes(raw, "big")


def decode_address(data: str | None) -> str | None:
    raw = _word(data)
    if raw is None or raw[:12] != bytes(12):
        return None
    return "0x" + raw[-20:].hex()


def decode_bool(data: str | None) -> bool | None:
    number = decode_uint256(data)
    if number == 0:
        return False
    if number == 1:
        return True
    return None


def decode_words(data: str | None, count: int) -> tuple[int, ...] | None:
    raw = _hex_bytes(data)
    if raw is None or len(raw) != 32 * count:
        return None
    return tuple(int.from_bytes(raw[index * 32:(index + 1) * 32], "big") for index in range(count))


@dataclass(frozen=True)
class StateRead:
    ok: bool
    amount: int | None = None
    approved: bool | None = None
    approved_for: str | None = None
    expiration: int | None = None
    nonce: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class PriorState:
    known: bool
    amount: int | None = None
    approved: bool | None = None
    spender: str | None = None
    expiration: int | None = None
    nonce: int | None = None


@dataclass(frozen=True)
class ContractEvidence:
    """ERC-165 answers or a W4 transfer kind. Missing answers are not a guess."""

    erc165_erc721: bool | None = None
    erc165_erc1155: bool | None = None
    transfer_kind: str | None = None


@dataclass(frozen=True)
class RiskPolicy:
    raw_high_threshold: int | None = None
    balance_bps: int | None = None


@dataclass(frozen=True)
class ApprovalRow:
    family: str
    capability: str
    owner: str
    token: str
    spender: str
    token_id: str | None
    raw_amount: str | None
    approved: bool | None
    change: str
    status: str
    flags: tuple[str, ...]
    reason_codes: tuple[str, ...]
    tx_id: str | None
    tx_from: str | None
    log_index: int | None
    layer: str = "token"
    expiration: str | None = None
    nonce: str | None = None

    def __post_init__(self) -> None:
        if self.change not in _CHANGES or self.status not in _STATUSES:
            raise ValueError("approval classification")
        if self.status == "revoked" and "current_state_unreadable" in self.reason_codes:
            raise ValueError("unconfirmed revoke")


@dataclass(frozen=True)
class PermissionScan:
    rows: tuple[ApprovalRow, ...]
    coverage: dict[str, CoverageObservation]
    cursor: str | None
    summary: str
    probe: str = _PROBE

    def __post_init__(self) -> None:
        if self.probe != _PROBE or _banned(self.summary):
            raise ValueError("permission summary")


@dataclass(frozen=True)
class _Parsed:
    family: str
    capability: str
    token: str
    owner: str
    spender: str
    token_id: str | None
    amount: int | None
    approved: bool | None
    tx_id: str | None
    log_index: int
    block_number: int | None
    key: tuple[Any, ...]


def resolve_operator_standard(evidence: ContractEvidence | None) -> str:
    """Return erc721, erc1155, or unknown. Conflicting interfaces are unknown."""
    if evidence is None:
        return "unknown"
    erc721 = evidence.erc165_erc721
    erc1155 = evidence.erc165_erc1155
    if erc721 is True and erc1155 is True:
        return "unknown"
    if erc721 is True and erc1155 is not True:
        return "erc721"
    if erc1155 is True and erc721 is not True:
        return "erc1155"
    if erc721 is None and erc1155 is None and evidence.transfer_kind in {"erc721", "erc1155"}:
        return str(evidence.transfer_kind)
    return "unknown"


def history_summary(*, found: bool, complete: bool) -> str:
    """Incomplete history is never an empty or safe approval set."""
    if not complete:
        text = HISTORY_INCOMPLETE
    elif found:
        text = "approvals_present"
    else:
        text = "empty_in_complete_history"
    if _banned(text):
        raise ValueError("permission summary")
    return text


def permission_observation(
    *,
    found: bool,
    pagination_complete: bool,
    gap_detection_complete: bool,
    gaps: tuple[str, ...],
    read_failed: bool,
    cursor: str | None,
    scope_from: str | None = None,
    scope_to: str | None = None,
    extra_reasons: tuple[str, ...] = (),
    declared: str = "approval_logs",
) -> CoverageObservation:
    complete = pagination_complete and gap_detection_complete and not gaps and not read_failed
    reasons: list[str] = []
    if not complete:
        reasons.append(HISTORY_INCOMPLETE)
    if read_failed:
        reasons.append("read_error")
    if gaps:
        reasons.append("gaps_present")
    reasons.extend(extra_reasons)
    if read_failed:
        category = "error"
    elif not complete or extra_reasons:
        category = "partial"
    elif found:
        category = "ok"
    else:
        category = "empty"
    return CoverageObservation(
        category=category,
        pagination_complete=pagination_complete and not read_failed,
        gap_detection_complete=gap_detection_complete and not read_failed,
        gaps=gaps,
        cursor=cursor,
        declared=declared,
        scope_from=scope_from,
        scope_to=scope_to,
        checkpoint_kind="block",
        checkpoint_number=scope_to if complete and not extra_reasons else None,
        checkpoint_status="observed" if complete and not extra_reasons else "unknown",
        sources=("evm-rpc",),
        trust="single_provider" if complete and not extra_reasons else "unverified",
        reason_codes=tuple(_unique(reasons)),
    )


def kept_cursor(previous: str | None, proposed: str | None, *, complete: bool) -> str | None:
    """A failed or incomplete read does not move the cursor."""
    if not complete:
        return previous
    return proposed


def parse_approval_logs(
    logs: Sequence[Mapping[str, Any]],
    owner: str,
    *,
    evidence: Mapping[str, ContractEvidence] | None = None,
    failed_txs: set[str] | None = None,
) -> tuple[tuple[_Parsed, ...], tuple[str, ...]]:
    """Parse approval logs for ``owner``. Malformed layouts are not zero allowances."""
    wallet = normalize_address(owner)
    if wallet is None:
        raise ValueError("bad_address")
    evidence = {
        normalize_address(key) or key: value
        for key, value in (evidence or {}).items()
    }
    failed = failed_txs or set()
    parsed: list[_Parsed] = []
    reasons: list[str] = []
    seen: dict[tuple[str, int], tuple[Any, ...]] = {}
    for log in logs:
        if not isinstance(log, Mapping):
            reasons.append("malformed_approval_log")
            continue
        item, reason = _parse_one(log, wallet, evidence, failed)
        if reason:
            reasons.append(reason)
        if item is None:
            continue
        identity = (item.tx_id or "", item.log_index)
        signature = (item.family, item.spender, item.token_id, item.amount, item.approved)
        previous = seen.get(identity)
        if previous is not None:
            if previous != signature:
                reasons.append("duplicate_log_mismatch")
            continue
        seen[identity] = signature
        parsed.append(item)
    parsed.sort(key=lambda item: (item.block_number is None, item.block_number or 0, item.log_index, item.tx_id or ""))
    return tuple(parsed), tuple(_unique(reasons))


def reconcile_approvals(
    logs: Sequence[Mapping[str, Any]],
    owner: str,
    *,
    states: Mapping[tuple[Any, ...], StateRead],
    prior: Mapping[tuple[Any, ...], PriorState] | None = None,
    evidence: Mapping[str, ContractEvidence] | None = None,
    balances: Mapping[str, tuple[bool, int | None]] | None = None,
    labels: Mapping[str, str] | None = None,
    policy: RiskPolicy | None = None,
    failed_txs: set[str] | None = None,
    tx_from: Mapping[str, str] | None = None,
    permit2_address: str | None = None,
    pagination_complete: bool = False,
    gap_detection_complete: bool = False,
    gaps: tuple[str, ...] = (),
    read_failed: bool = False,
    previous_cursor: str | None = None,
    next_cursor: str | None = None,
    scope_from: str | None = None,
    scope_to: str | None = None,
    now: int | None = None,
) -> PermissionScan:
    """Classify each approval. Current state wins; a failed read stays unknown."""
    wallet = normalize_address(owner)
    if wallet is None:
        raise ValueError("bad_address")
    parsed, parse_reasons = parse_approval_logs(logs, wallet, evidence=evidence, failed_txs=failed_txs)
    transfers = _transfers_from_owner(logs, wallet, failed_txs or set())
    permit2 = normalize_address(permit2_address) if permit2_address else None
    balances = _normalize_map(balances or {})
    grouped: dict[tuple[Any, ...], list[_Parsed]] = {}
    order: list[tuple[Any, ...]] = []
    for item in parsed:
        if item.key not in grouped:
            order.append(item.key)
        grouped.setdefault(item.key, []).append(item)
    rows: list[ApprovalRow] = []
    state_failed = read_failed
    for key in order:
        state = states.get(key)
        if state is None or not state.ok:
            state_failed = True
        rows.extend(_rows_for_key(
            grouped[key],
            state if state is not None else StateRead(False, reason="not_read"),
            (prior or {}).get(key, PriorState(False)),
            transfers,
            balances or {},
            labels or {},
            policy or RiskPolicy(),
            tx_from or {},
            permit2,
            now,
        ))
    extra = tuple(parse_reasons)
    unknown_operator = any(row.family == "operator_unknown_standard" for row in rows)
    log_clean = not read_failed and not parse_reasons
    complete = (
        log_clean
        and not state_failed
        and pagination_complete
        and gap_detection_complete
        and not gaps
    )
    cursor = kept_cursor(previous_cursor, next_cursor, complete=complete)
    coverage = _coverage(
        rows,
        pagination_complete=pagination_complete and log_clean,
        gap_detection_complete=gap_detection_complete and log_clean,
        gaps=gaps,
        read_failed=read_failed or not log_clean or state_failed,
        cursor=cursor,
        scope_from=scope_from,
        scope_to=scope_to,
        extra_reasons=extra,
        unknown_operator=unknown_operator,
    )
    return PermissionScan(tuple(rows), coverage, cursor, history_summary(found=bool(rows), complete=complete))


def approval_events_from_receipt(
    receipt: Mapping[str, Any],
    *,
    tenant_id: str,
    wallet_id: str,
    owner: str,
    network: str,
    chain_uid: str,
    observed_at: str,
    evidence: Mapping[str, ContractEvidence] | None = None,
    source_id: str = "evm-rpc",
) -> list[ChainEvent]:
    """Turn approval logs into chain events. A failed receipt yields none of them."""
    if not isinstance(receipt, Mapping):
        raise ValueError("receipt")
    tx_hash = receipt.get("transactionHash") or receipt.get("hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        raise ValueError("tx_hash")
    logs = receipt.get("logs") or []
    if not isinstance(logs, list):
        raise ValueError("logs")
    parsed, _reasons = parse_approval_logs(
        [item for item in logs if isinstance(item, Mapping)],
        owner,
        evidence=evidence,
    )
    effects = [_effect(item) for item in parsed]
    block = _quantity(receipt.get("blockNumber"))
    tx: dict[str, Any] = {
        "chain_uid": chain_uid,
        "effects": effects,
        "finality": "observed" if block is not None else "unknown",
        "network": network,
        "observed_at": observed_at,
        "source_ids": (source_id,),
        "status": receipt.get("status"),
        "tenant_id": tenant_id,
        "tx_id": tx_hash,
        "wallet_id": wallet_id,
    }
    if block is not None:
        tx["block_number"] = str(block)
    block_hash = receipt.get("blockHash")
    if isinstance(block_hash, str) and block_hash:
        tx["block_hash"] = block_hash
    return effects_from_tx(tx)


def read_view(
    transport: Any,
    source_id: str,
    to: str,
    signature: str,
    args: Sequence[Any],
) -> tuple[bool, str | None, str]:
    """eth_call one allowlisted view through the read-only transport."""
    try:
        data = encode_view_call(signature, args)
        operation = eth_call_operation(to, data)
        result = transport.request(source_id, operation)
    except Exception:
        return False, None, "read_error"
    if not getattr(result, "ok", False) or not isinstance(getattr(result, "body", None), dict):
        return False, None, "read_error"
    raw = result.body.get("result")
    if not isinstance(raw, str) or not raw.startswith(("0x", "0X")):
        return False, None, "malformed_result"
    return True, raw, ""


def read_approval_logs(
    transport: Any,
    source_id: str,
    owner: str,
    start: int,
    end: int,
) -> tuple[bool, tuple[Mapping[str, Any], ...], str]:
    if isinstance(start, bool) or isinstance(end, bool):
        return False, (), "bad_range"
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return False, (), "bad_range"
    if end - start > LOG_RANGE_MAX:
        return False, (), "log_range_exceeds_bound"
    try:
        operation = approval_log_operation(owner, start, end)
        result = transport.request(source_id, operation)
    except Exception:
        return False, (), "read_error"
    if not getattr(result, "ok", False) or not isinstance(getattr(result, "body", None), dict):
        return False, (), "read_error"
    logs = result.body.get("result")
    if not isinstance(logs, list) or any(not isinstance(item, Mapping) for item in logs):
        return False, (), "malformed_logs"
    return True, tuple(logs), ""


def read_erc20_allowance(transport: Any, source_id: str, token: str, owner: str, spender: str) -> StateRead:
    ok, raw, reason = read_view(transport, source_id, token, "allowance(address,address)", [owner, spender])
    if not ok or raw is None:
        return StateRead(False, reason=reason or "read_error")
    amount = decode_uint256(raw)
    if amount is None:
        return StateRead(False, reason="malformed_allowance")
    return StateRead(True, amount=amount)


def read_token_balance(transport: Any, source_id: str, token: str, owner: str) -> tuple[bool, int | None]:
    ok, raw, _reason = read_view(transport, source_id, token, "balanceOf(address)", [owner])
    if not ok or raw is None:
        return False, None
    amount = decode_uint256(raw)
    if amount is None:
        return False, None
    return True, amount


def read_get_approved(transport: Any, source_id: str, token: str, token_id: int) -> StateRead:
    ok, raw, reason = read_view(transport, source_id, token, "getApproved(uint256)", [token_id])
    if not ok or raw is None:
        return StateRead(False, reason=reason or "read_error")
    approved_for = decode_address(raw)
    if approved_for is None:
        return StateRead(False, reason="malformed_approval")
    active = approved_for != ZERO_ADDRESS
    return StateRead(True, amount=1 if active else 0, approved=active, approved_for=approved_for)


def read_operator(transport: Any, source_id: str, token: str, owner: str, operator: str) -> StateRead:
    ok, raw, reason = read_view(
        transport,
        source_id,
        token,
        "isApprovedForAll(address,address)",
        [owner, operator],
    )
    if not ok or raw is None:
        return StateRead(False, reason=reason or "read_error")
    approved = decode_bool(raw)
    if approved is None:
        return StateRead(False, reason="malformed_operator")
    return StateRead(True, approved=approved, amount=1 if approved else 0)


def read_supports_interface(transport: Any, source_id: str, token: str, interface_id: bytes) -> bool | None:
    ok, raw, _reason = read_view(transport, source_id, token, "supportsInterface(bytes4)", [interface_id])
    if not ok or raw is None:
        return None
    return decode_bool(raw)


def scan_approvals(
    transport: Any,
    source_id: str,
    owner: str,
    start_block: int,
    end_block: int,
    *,
    previous_cursor: str | None,
    evidence: Mapping[str, ContractEvidence] | None = None,
    prior: Mapping[tuple[Any, ...], PriorState] | None = None,
    labels: Mapping[str, str] | None = None,
    policy: RiskPolicy | None = None,
    pagination_complete: bool = True,
    gap_detection_complete: bool = False,
    gaps: tuple[str, ...] = (),
    permit2_address: str | None = None,
    now: int | None = None,
    tx_from: Mapping[str, str] | None = None,
    read_balances: bool = True,
    prefetched_logs: tuple[Mapping[str, Any], ...] | None = None,
) -> PermissionScan:
    """Logs plus current allowance reads. An error keeps ``previous_cursor``."""
    scope_from = str(start_block) if isinstance(start_block, int) and not isinstance(start_block, bool) else None
    scope_to = str(end_block) if isinstance(end_block, int) and not isinstance(end_block, bool) else None
    if prefetched_logs is None:
        ok, logs, reason = read_approval_logs(transport, source_id, owner, start_block, end_block)
    else:
        ok, logs, reason = True, prefetched_logs, ""
    if not ok:
        return _failed_scan(previous_cursor, scope_from, scope_to, (reason,))
    parsed, _parse_reasons = parse_approval_logs(logs, owner, evidence=evidence)
    states: dict[tuple[Any, ...], StateRead] = {}
    balances: dict[str, tuple[bool, int | None]] = {}
    for item in parsed:
        if item.key in states:
            continue
        states[item.key] = _read_state(transport, source_id, owner, item)
        if read_balances and item.family == "erc20" and item.token not in balances:
            balances[item.token] = read_token_balance(transport, source_id, item.token, owner)
    return reconcile_approvals(
        logs,
        owner,
        states=states,
        prior=prior,
        evidence=evidence,
        balances=balances,
        labels=labels,
        policy=policy,
        permit2_address=permit2_address,
        pagination_complete=pagination_complete,
        gap_detection_complete=gap_detection_complete,
        gaps=gaps,
        read_failed=False,
        previous_cursor=previous_cursor,
        next_cursor=str(end_block),
        scope_from=scope_from,
        scope_to=scope_to,
        now=now,
        tx_from=tx_from,
    )


def _read_state(transport: Any, source_id: str, owner: str, item: _Parsed) -> StateRead:
    if item.family == "erc20":
        return read_erc20_allowance(transport, source_id, item.token, owner, item.spender)
    if item.family == "erc721_token":
        if item.token_id is None:
            return StateRead(False, reason="malformed_approval")
        return read_get_approved(transport, source_id, item.token, int(item.token_id))
    if item.family in {"erc721_operator", "erc1155_operator", "operator_unknown_standard"}:
        return read_operator(transport, source_id, item.token, owner, item.spender)
    return StateRead(False, reason="unknown_family")


def _failed_scan(
    previous: str | None,
    scope_from: str | None,
    scope_to: str | None,
    reasons: tuple[str, ...],
) -> PermissionScan:
    coverage = {
        capability: permission_observation(
            found=False,
            pagination_complete=False,
            gap_detection_complete=False,
            gaps=(),
            read_failed=True,
            cursor=previous,
            scope_from=scope_from,
            scope_to=scope_to,
            extra_reasons=reasons,
        )
        for capability in _PERMISSION_CAPABILITIES
    }
    return PermissionScan((), coverage, previous, history_summary(found=False, complete=False))


def _coverage(
    rows: Sequence[ApprovalRow],
    *,
    pagination_complete: bool,
    gap_detection_complete: bool,
    gaps: tuple[str, ...],
    read_failed: bool,
    cursor: str | None,
    scope_from: str | None,
    scope_to: str | None,
    extra_reasons: tuple[str, ...],
    unknown_operator: bool,
) -> dict[str, CoverageObservation]:
    coverage: dict[str, CoverageObservation] = {}
    for capability in _PERMISSION_CAPABILITIES:
        reasons = extra_reasons
        if unknown_operator and capability in {"permissions.erc721_operator", "permissions.erc1155_operator"}:
            reasons = tuple(_unique((*reasons, "operator_standard_unknown")))
        coverage[capability] = permission_observation(
            found=any(row.capability == capability for row in rows),
            pagination_complete=pagination_complete,
            gap_detection_complete=gap_detection_complete,
            gaps=gaps,
            read_failed=read_failed,
            cursor=cursor,
            scope_from=scope_from,
            scope_to=scope_to,
            extra_reasons=reasons,
        )
    return coverage


def _rows_for_key(
    events: Sequence[_Parsed],
    state: StateRead,
    prior: PriorState,
    transfers: set[tuple[str, str]],
    balances: Mapping[str, tuple[bool, int | None]],
    labels: Mapping[str, str],
    policy: RiskPolicy,
    tx_from: Mapping[str, str],
    permit2: str | None,
    now: int | None,
) -> list[ApprovalRow]:
    running = prior
    rows: list[ApprovalRow] = []
    for index, event in enumerate(events):
        last = index == len(events) - 1
        current_ok = state.ok if last else True
        sender = tx_from.get(event.tx_id or "")
        if event.family == "erc20":
            amount = state.amount if last else event.amount
            expiration = state.expiration if last else None
            if last and not state.ok:
                amount = None
            consumed = _consumed(event, running, amount if current_ok else event.amount, transfers)
            change, status, reasons = classify_erc20(
                prior_known=running.known,
                prior_amount=running.amount,
                prior_expiration=running.expiration,
                current_ok=current_ok,
                current_amount=amount,
                current_expiration=expiration,
                event_amount=event.amount,
                consumed=consumed,
                now=now,
            )
            spender = event.spender
            row_amount = None if amount is None or not current_ok else amount
            approved = None
        elif event.family == "erc721_token":
            change, status, reasons, spender, row_amount, approved = _nft_outcome(event, running, state, last)
        else:
            approved_now = state.approved if last else event.approved
            change, status, reasons = classify_operator(
                prior_known=running.known,
                prior_approved=running.approved,
                current_ok=current_ok,
                current_approved=approved_now,
                event_approved=event.approved,
            )
            spender = event.spender
            row_amount = None if not current_ok else (1 if approved_now else 0)
            approved = None if not current_ok else approved_now
        if event.family == "erc20" and not current_ok:
            row_amount = None
        balance = balances.get(event.token, (False, None))
        flags = risk_flags(
            family=event.family,
            amount=row_amount,
            balance_known=balance[0],
            balance=balance[1],
            label=_label(labels, spender),
            operator=event.family in {"erc1155_operator", "erc721_operator", "operator_unknown_standard"},
            approved=bool(approved) if event.family != "erc20" else status == "active",
            status=status,
            policy=policy,
        )
        layer = "erc20_to_permit2" if event.family == "erc20" and permit2 is not None and spender == permit2 else "token"
        rows.append(ApprovalRow(
            family=event.family,
            capability=event.capability,
            owner=event.owner,
            token=event.token,
            spender=spender,
            token_id=event.token_id,
            raw_amount=None if row_amount is None else str(row_amount),
            approved=approved if event.family != "erc20" else None,
            change=change,
            status=status,
            flags=flags,
            reason_codes=reasons,
            tx_id=event.tx_id,
            tx_from=normalize_address(sender) if isinstance(sender, str) else None,
            log_index=event.log_index,
            layer=layer,
            expiration=None if state.expiration is None or not last or not state.ok else str(state.expiration),
            nonce=None if state.nonce is None or not last or not state.ok else str(state.nonce),
        ))
        running = PriorState(
            True,
            amount=row_amount if row_amount is not None else event.amount,
            approved=approved if approved is not None else event.approved,
            spender=spender,
            expiration=state.expiration if last and state.ok else running.expiration,
            nonce=state.nonce if last and state.ok else running.nonce,
        )
    return rows


def classify_erc20(
    *,
    prior_known: bool,
    prior_amount: int | None,
    prior_expiration: int | None,
    current_ok: bool,
    current_amount: int | None,
    current_expiration: int | None,
    event_amount: int | None,
    consumed: bool,
    now: int | None,
) -> tuple[str, str, tuple[str, ...]]:
    if not current_ok or current_amount is None:
        change = "first_observed" if not prior_known else "unknown"
        return change, "unknown", ("current_state_unreadable",)
    reasons: list[str] = []
    if event_amount is not None and event_amount != current_amount:
        reasons.append("event_state_mismatch")
    extended = (
        prior_known
        and prior_expiration is not None
        and current_expiration is not None
        and current_expiration > prior_expiration
    )
    dropped = prior_known and prior_amount is not None and current_amount < prior_amount
    if not prior_known:
        change = "first_observed"
    elif prior_amount is None:
        change = "unknown"
    elif consumed and dropped:
        change = "consumed"
    elif prior_amount == 0 and current_amount > 0:
        change = "new"
    elif current_amount > prior_amount:
        change = "increased"
    elif extended and current_amount == prior_amount:
        change = "extended"
    elif dropped and current_amount == 0:
        change = "revoked"
    elif dropped:
        change = "decreased"
    elif extended:
        change = "extended"
    else:
        change = "unchanged"
    if extended and change != "extended":
        reasons.append("expiration_extended")
    status = _amount_status(change, current_amount, current_expiration, now, reasons)
    return change, status, tuple(_unique(reasons))


def classify_operator(
    *,
    prior_known: bool,
    prior_approved: bool | None,
    current_ok: bool,
    current_approved: bool | None,
    event_approved: bool | None,
) -> tuple[str, str, tuple[str, ...]]:
    if not current_ok or current_approved is None:
        change = "first_observed" if not prior_known else "unknown"
        return change, "unknown", ("current_state_unreadable",)
    reasons: list[str] = []
    if event_approved is not None and event_approved != current_approved:
        reasons.append("event_state_mismatch")
    if not prior_known:
        change = "first_observed"
    elif prior_approved is False and current_approved is True:
        change = "new"
    elif prior_approved is True and current_approved is False:
        change = "revoked"
    elif prior_approved == current_approved:
        change = "unchanged"
    else:
        change = "unknown"
    if current_approved:
        status = "active"
    elif change == "revoked":
        status = "revoked"
    else:
        status = "inactive"
    return change, status, tuple(reasons)


def risk_flags(
    *,
    family: str,
    amount: int | None,
    balance_known: bool,
    balance: int | None,
    label: str | None,
    operator: bool,
    approved: bool,
    status: str,
    policy: RiskPolicy,
) -> tuple[str, ...]:
    flags: list[str] = []
    if family == "erc20" and amount is not None and (amount == UINT256_MAX or amount >= UNLIMITED_FLOOR):
        flags.append("unlimited")
    if (
        family == "erc20"
        and amount is not None
        and policy.raw_high_threshold is not None
        and amount >= policy.raw_high_threshold
    ):
        flags.append("high")
    if (
        family == "erc20"
        and amount is not None
        and balance_known
        and isinstance(balance, int)
        and not isinstance(balance, bool)
        and balance > 0
        and isinstance(policy.balance_bps, int)
        and not isinstance(policy.balance_bps, bool)
        and policy.balance_bps > 0
        and amount * 10_000 >= balance * policy.balance_bps
    ):
        flags.append("high")
    if operator and approved and status == "active":
        flags.append("operator_for_all")
    if label is None or label.lower() == "safe":
        flags.append("unlabeled")
    else:
        flags.append("spender_label:" + label)
    risky = label is None or label.lower() == "safe" or label in _RISKY_LABELS
    if family == "erc20" and status == "active" and balance_known and balance == 0 and risky:
        flags.append("zero_balance_exposure")
    return tuple(_unique(flags))


def _amount_status(
    change: str,
    amount: int,
    expiration: int | None,
    now: int | None,
    reasons: list[str],
) -> str:
    if change == "consumed" and amount == 0:
        return "consumed"
    if amount == 0:
        return "revoked" if change == "revoked" else "inactive"
    if expiration is not None:
        if now is None:
            reasons.append("expiry_time_missing")
            return "unknown"
        if expiration < now:
            return "expired"
    return "active"


def _nft_outcome(
    event: _Parsed,
    prior: PriorState,
    state: StateRead,
    last: bool,
) -> tuple[str, str, tuple[str, ...], str, int | None, bool | None]:
    if not last:
        active = event.spender != ZERO_ADDRESS
        change, status, reasons = classify_operator(
            prior_known=prior.known,
            prior_approved=prior.approved,
            current_ok=True,
            current_approved=active,
            event_approved=active,
        )
        if prior.known and prior.spender and event.spender != prior.spender and active:
            change = "new"
            reasons = tuple(_unique((*reasons, "approval_replaced")))
        return change, status, reasons, event.spender, 1 if active else 0, active
    if not state.ok or state.approved_for is None:
        change = "first_observed" if not prior.known else "unknown"
        return change, "unknown", ("current_state_unreadable",), event.spender, None, None
    current = state.approved_for
    active = current != ZERO_ADDRESS
    reasons: list[str] = []
    if event.spender != current:
        reasons.append("event_state_mismatch")
    if not prior.known:
        change = "first_observed"
    elif (prior.spender in {None, ZERO_ADDRESS}) and active and prior.approved is not True:
        change = "new"
    elif prior.approved is True and not active:
        change = "revoked"
    elif prior.spender == current:
        change = "unchanged"
    elif active and prior.spender not in {None, current}:
        change = "new"
        reasons.append("approval_replaced")
    else:
        change = "unknown"
    if active:
        status = "active"
    elif change == "revoked":
        status = "revoked"
    else:
        status = "inactive"
    return change, status, tuple(_unique(reasons)), current, 1 if active else 0, active


def _consumed(event: _Parsed, prior: PriorState, amount: int | None, transfers: set[tuple[str, str]]) -> bool:
    if event.tx_id is None or (event.tx_id, event.token) not in transfers:
        return False
    if not prior.known or prior.amount is None or amount is None:
        return False
    return amount < prior.amount


def _parse_one(
    log: Mapping[str, Any],
    owner: str,
    evidence: Mapping[str, ContractEvidence],
    failed: set[str],
) -> tuple[_Parsed | None, str | None]:
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics or any(not isinstance(item, str) for item in topics):
        return None, None
    topic0 = topics[0].lower()
    if topic0 not in {ERC20_APPROVAL_TOPIC, APPROVAL_FOR_ALL_TOPIC}:
        return None, None
    tx_id = _tx(log)
    if tx_id is not None and tx_id in failed:
        return None, None
    token = normalize_address(log.get("address"))
    log_index = _log_index(log)
    if token is None or log_index is None:
        return None, "malformed_approval_log"
    if topic0 == ERC20_APPROVAL_TOPIC:
        return _parse_approval(topics, log.get("data"), owner, token, tx_id, log_index, _block(log))
    return _parse_for_all(topics, log.get("data"), owner, token, tx_id, log_index, _block(log), evidence.get(token))


def _parse_approval(
    topics: Sequence[Any],
    data: Any,
    owner: str,
    token: str,
    tx_id: str | None,
    log_index: int,
    block_number: int | None,
) -> tuple[_Parsed | None, str | None]:
    if len(topics) == 3:
        wallet = _topic_address(topics[1])
        spender = _topic_address(topics[2])
        words = decode_words(data if isinstance(data, str) else None, 1)
        if wallet != owner:
            return None, None
        if spender is None or words is None:
            return None, "malformed_approval_log"
        amount = words[0]
        if amount > UINT256_MAX:
            return None, "malformed_approval_log"
        return _parsed(
            "erc20",
            "permissions.erc20",
            token,
            owner,
            spender,
            None,
            amount,
            None,
            tx_id,
            log_index,
            block_number,
            ("erc20", token, spender),
        ), None
    if len(topics) == 4:
        wallet = _topic_address(topics[1])
        spender = _topic_address(topics[2])
        token_id = _topic_uint(topics[3])
        if wallet != owner:
            return None, None
        if spender is None or token_id is None or _data_nonempty(data):
            return None, "malformed_approval_log"
        return _parsed(
            "erc721_token",
            "permissions.erc721_token",
            token,
            owner,
            spender,
            str(token_id),
            None,
            spender != ZERO_ADDRESS,
            tx_id,
            log_index,
            block_number,
            ("erc721", token, str(token_id)),
        ), None
    return None, "malformed_approval_log"


def _parse_for_all(
    topics: Sequence[Any],
    data: Any,
    owner: str,
    token: str,
    tx_id: str | None,
    log_index: int,
    block_number: int | None,
    evidence: ContractEvidence | None,
) -> tuple[_Parsed | None, str | None]:
    if len(topics) != 3:
        return None, "malformed_approval_log"
    wallet = _topic_address(topics[1])
    spender = _topic_address(topics[2])
    if wallet != owner:
        return None, None
    approved = decode_bool(data if isinstance(data, str) else None)
    if spender is None or approved is None:
        return None, "malformed_approval_log"
    standard = resolve_operator_standard(evidence)
    if standard == "erc721":
        family, capability = "erc721_operator", "permissions.erc721_operator"
    elif standard == "erc1155":
        family, capability = "erc1155_operator", "permissions.erc1155_operator"
    else:
        family, capability = "operator_unknown_standard", "operator_unknown_standard"
    return _parsed(
        family,
        capability,
        token,
        owner,
        spender,
        None,
        None,
        approved,
        tx_id,
        log_index,
        block_number,
        ("operator", standard, token, spender),
    ), None


def _parsed(
    family: str,
    capability: str,
    token: str,
    owner: str,
    spender: str,
    token_id: str | None,
    amount: int | None,
    approved: bool | None,
    tx_id: str | None,
    log_index: int,
    block_number: int | None,
    key: tuple[Any, ...],
) -> _Parsed:
    return _Parsed(
        family,
        capability,
        token,
        owner,
        spender,
        token_id,
        amount,
        approved,
        tx_id,
        log_index,
        block_number,
        key,
    )


def _effect(item: _Parsed) -> dict[str, Any]:
    if item.family in {"erc721_operator", "erc1155_operator", "operator_unknown_standard"}:
        kind = "operator_set" if item.approved else "approval_revoked"
        amount = "1" if item.approved else "0"
        asset = item.token
    elif item.family == "erc721_token":
        kind = "approval_revoked" if item.spender == ZERO_ADDRESS else "approval_set"
        amount = "0" if item.spender == ZERO_ADDRESS else "1"
        asset = item.token if item.token_id is None else f"{item.token}:{item.token_id}"
    else:
        kind = "approval_revoked" if item.amount == 0 else "approval_set"
        amount = "0" if item.amount is None else str(item.amount)
        asset = item.token
    return {"asset": asset, "kind": kind, "raw_amount": amount, "sub_index": str(item.log_index)}


def _transfers_from_owner(logs: Sequence[Mapping[str, Any]], owner: str, failed: set[str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for log in logs:
        if not isinstance(log, Mapping):
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) != 3 or not isinstance(topics[0], str):
            continue
        if topics[0].lower() != TRANSFER_TOPIC:
            continue
        tx_id = _tx(log)
        token = normalize_address(log.get("address"))
        sender = _topic_address(topics[1])
        if tx_id is None or token is None or sender != owner or tx_id in failed:
            continue
        found.add((tx_id, token))
    return found


def _normalize_map(balances: Mapping[str, tuple[bool, int | None]]) -> dict[str, tuple[bool, int | None]]:
    normalized: dict[str, tuple[bool, int | None]] = {}
    for key, value in balances.items():
        token = normalize_address(key)
        normalized[token or key] = value
    return normalized


def _label(labels: Mapping[str, str], spender: str) -> str | None:
    for key, value in labels.items():
        if normalize_address(key) == spender and isinstance(value, str) and value:
            return value
    return None


def _coerce(kind: str, value: Any) -> Any:
    if kind == "address":
        if not isinstance(value, str):
            raise ValueError("not_a_view_call")
        normalized = normalize_address(value)
        if normalized is None:
            raise ValueError("not_a_view_call")
        return normalized
    if kind == "uint256":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
            raise ValueError("not_a_view_call")
        return value
    if kind == "bytes4":
        if isinstance(value, bytes) and len(value) == 4:
            return value
        if isinstance(value, str) and value.startswith("0x") and len(value) == 10:
            return bytes.fromhex(value[2:])
        raise ValueError("not_a_view_call")
    raise ValueError("not_a_view_call")


def _topic_address(topic: Any) -> str | None:
    raw = _hex_bytes(topic)
    if raw is None or len(raw) != 32 or raw[:12] != bytes(12):
        return None
    return "0x" + raw[-20:].hex()


def _topic_uint(topic: Any) -> int | None:
    raw = _hex_bytes(topic)
    if raw is None or len(raw) != 32:
        return None
    number = int.from_bytes(raw, "big")
    if number > UINT256_MAX:
        return None
    return number


def _word(data: str | None) -> bytes | None:
    words = decode_words(data, 1)
    if words is None:
        return None
    return words[0].to_bytes(32, "big")


def _data_nonempty(data: Any) -> bool:
    raw = _hex_bytes(data)
    if raw is None:
        return True
    return len(raw) != 0


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


def _block(log: Mapping[str, Any]) -> int | None:
    return _quantity(log.get("blockNumber"))


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


def _banned(text: str) -> bool:
    lowered = text.lower()
    return lowered in {"safe", "none", "no approvals", "no_approvals"} or "no approvals" in lowered


def _unique(values: Sequence[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found

"""On-chain permits and Permit2. Off-chain signatures stay not observable.

ERC-2612 and DAI are recognized from calldata that was already submitted, plus
the Approval log and a nonce increase. The owner is the permit argument, not
``tx.from``. Permit2 keeps the token approval and the spender allowance apart.
SignatureTransfer nonces are used bits only. This module does not encode
permit, approve, lockdown, or any other state-changing call, and it does not
check signature cryptography.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from eth_abi import decode
from eth_hash.auto import keccak
from eth_utils import function_signature_to_4byte_selector

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address, pad_address
from guardian.chains.evm.adapter import LOG_RANGE_MAX, evm_rpc
from guardian.chains.evm.permissions import (
    ApprovalRow,
    PriorState,
    StateRead,
    classify_erc20,
    decode_uint256,
    decode_words,
    history_summary,
    kept_cursor,
    parse_approval_logs,
    permission_observation,
    read_view,
)
from guardian.coverage import CoverageObservation
from guardian.identities import IdentityError, parse_address
from guardian.resources import resource_path

OFFCHAIN_REASON = "offchain_signature_not_observable"
CANONICAL_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
T0_EVM_NETWORKS = frozenset({
    "arbitrum-one",
    "avalanche-c",
    "base-mainnet",
    "bsc-mainnet",
    "ethereum-mainnet",
    "optimism-mainnet",
    "polygon-pos",
})
DEFAULT_PERMIT2_DIR = resource_path("config", "deployments", "permit2")
_FIELDS = frozenset({"address", "network", "notes", "retrieved", "source_url", "status"})
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_SOURCE = "https://github.com/Uniswap/permit2"
_NOTES = "not verified on-chain by this package"
_PROBE = "untested"
_UINT160 = 2**160
_UINT48 = 2**48


def _topic(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii")).hex()


def _selector(signature: str) -> bytes:
    return function_signature_to_4byte_selector(signature)


PERMIT2_APPROVAL_TOPIC = _topic("Approval(address,address,address,uint160,uint48)")
PERMIT2_PERMIT_TOPIC = _topic("Permit(address,address,address,uint160,uint48,uint48)")
PERMIT2_LOCKDOWN_TOPIC = _topic("Lockdown(address,address,address)")
PERMIT2_NONCE_TOPIC = _topic("NonceInvalidation(address,address,address,uint48,uint48)")
PERMIT2_UNORDERED_TOPIC = _topic("UnorderedNonceInvalidation(address,uint256,uint256)")
PERMIT2_LOG_TOPICS = (
    PERMIT2_APPROVAL_TOPIC,
    PERMIT2_LOCKDOWN_TOPIC,
    PERMIT2_NONCE_TOPIC,
    PERMIT2_PERMIT_TOPIC,
    PERMIT2_UNORDERED_TOPIC,
)
_SEL_2612 = _selector("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)")
_SEL_DAI = _selector("permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)")
_SEL_MULTICALL = _selector("multicall(bytes[])")
_SEL_AGGREGATE = _selector("aggregate((address,bytes)[])")
_SEL_AGGREGATE3 = _selector("aggregate3((address,bool,bytes)[])")


class Permit2ConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Permit2Deployment:
    network: str
    address: str
    status: str
    source_url: str
    retrieved: str
    notes: str

    def __post_init__(self) -> None:
        if self.status != "documented" or self.address != CANONICAL_PERMIT2:
            raise Permit2ConfigError("deployment")
        if "live_pass" in self.status or "not verified" not in self.notes:
            raise Permit2ConfigError("deployment")


@dataclass(frozen=True)
class SubmittedPermit:
    """A permit that already produced an Approval. Not a signature validity check."""

    capability: str
    owner: str
    spender: str
    token: str | None
    tx_from: str | None
    raw_amount: str | None
    allowed: bool | None
    nonce_before: str | None
    nonce_after: str | None
    nonce_incremented: bool | None
    via: str
    status: str
    tx_id: str | None
    calldata_nonce: str | None = None
    deadline: str | None = None

    def __post_init__(self) -> None:
        if self.capability not in {"permits.dai_variant", "permits.erc2612_observed"}:
            raise ValueError("permit capability")
        if self.status not in {"observed", "unknown"}:
            raise ValueError("permit status")
        if self.status == "observed" and self.nonce_incremented is not True:
            raise ValueError("permit requires a nonce increment")
        if self.via not in {"direct", "multicall"}:
            raise ValueError("permit path")


@dataclass(frozen=True)
class Permit2Allowance:
    capability: str
    layer: str
    owner: str
    token: str
    spender: str
    raw_amount: str | None
    expiration: str | None
    nonce: str | None
    change: str
    status: str
    persistent_allowance: bool
    reason_codes: tuple[str, ...]
    tx_id: str | None
    log_index: int | None

    def __post_init__(self) -> None:
        if self.capability != "permit2.allowance" or self.layer != "permit2_to_spender":
            raise ValueError("permit2 layer")
        if not self.persistent_allowance:
            raise ValueError("signature nonce is not an allowance")
        if self.status not in {"active", "consumed", "expired", "inactive", "revoked", "unknown"}:
            raise ValueError("permit2 status")


@dataclass(frozen=True)
class SignatureUse:
    capability: str
    owner: str
    word: str
    nonce: str
    persistent_allowance: bool
    source: str

    def __post_init__(self) -> None:
        if self.capability != "permit2.signature_usage" or self.persistent_allowance:
            raise ValueError("signature usage is not an allowance")
        if self.source not in {"nonce_bitmap", "unordered_nonce_invalidation"}:
            raise ValueError("signature source")


@dataclass(frozen=True)
class OffchainUnused:
    capability: str
    support: str
    quality: str
    reason: str
    observation: CoverageObservation

    def __post_init__(self) -> None:
        if (
            self.capability != "permits.offchain_unused"
            or self.support != "not_observable"
            or self.quality != "unknown"
            or self.reason != OFFCHAIN_REASON
        ):
            raise ValueError("offchain blindspot")
        if self.quality in {"safe", "none"} or self.support in {"safe", "none"}:
            raise ValueError("offchain blindspot")


@dataclass(frozen=True)
class PermitReport:
    submitted: tuple[SubmittedPermit, ...]
    allowances: tuple[Permit2Allowance, ...]
    signature_uses: tuple[SignatureUse, ...]
    erc20_to_permit2: tuple[ApprovalRow, ...]
    coverage: dict[str, CoverageObservation]
    offchain: OffchainUnused
    cursor: str | None
    summary: str
    probe: str = _PROBE

    def __post_init__(self) -> None:
        if self.probe != _PROBE or self.summary.lower() in {"safe", "none", "no approvals"}:
            raise ValueError("permit summary")
        if self.offchain.quality != "unknown":
            raise ValueError("offchain blindspot")


@dataclass(frozen=True)
class _PermitCall:
    kind: str
    owner: str
    spender: str
    value: int | None
    allowed: bool | None
    target: str | None
    via: str
    deadline: int | None
    calldata_nonce: int | None


@dataclass(frozen=True)
class _Permit2Log:
    kind: str
    owner: str
    token: str | None
    spender: str | None
    amount: int | None
    expiration: int | None
    nonce: int | None
    old_nonce: int | None
    word: int | None
    mask: int | None
    tx_id: str | None
    log_index: int
    block_number: int | None


def offchain_unused_status(_claim: str | None = None) -> OffchainUnused:
    """Unsubmitted signatures are never safe, none, or fully enumerated."""
    observation = CoverageObservation(
        category="partial",
        pagination_complete=False,
        gap_detection_complete=False,
        declared="not_observable",
        reason_codes=(OFFCHAIN_REASON,),
        trust="unverified",
    )
    return OffchainUnused(
        capability="permits.offchain_unused",
        support="not_observable",
        quality="unknown",
        reason=OFFCHAIN_REASON,
        observation=observation,
    )


def unsubmitted_signatures(_bitmap: int | None = None, claim: str | None = None) -> OffchainUnused:
    """A bitmap's clear bits are not a list of unused signatures."""
    return offchain_unused_status(claim)


def used_nonces(word: int, mask: int) -> tuple[int, ...]:
    """Set bits only. Clear bits are omitted on purpose."""
    if isinstance(word, bool) or isinstance(mask, bool):
        raise ValueError("bitmap")
    if not isinstance(word, int) or not isinstance(mask, int) or word < 0 or mask < 0 or mask >= 2**256:
        raise ValueError("bitmap")
    return tuple(word * 256 + bit for bit in range(256) if mask & (1 << bit))


def signature_uses_from_bitmap(
    owner: str,
    word: int,
    mask: int,
    *,
    source: str = "nonce_bitmap",
) -> tuple[SignatureUse, ...]:
    wallet = normalize_address(owner)
    if wallet is None:
        raise ValueError("bad_address")
    return tuple(
        SignatureUse(
            capability="permit2.signature_usage",
            owner=wallet,
            word=str(word),
            nonce=str(nonce),
            persistent_allowance=False,
            source=source,
        )
        for nonce in used_nonces(word, mask)
    )


def validate_permit2_document(document: Mapping[str, Any], *, filename: str | None = None) -> Permit2Deployment:
    if not isinstance(document, Mapping) or set(document) != _FIELDS:
        raise Permit2ConfigError("fields")
    if "live_pass" in json.dumps(document, ensure_ascii=True):
        raise Permit2ConfigError("live_claim")
    network = document.get("network")
    address = document.get("address")
    status = document.get("status")
    source_url = document.get("source_url")
    retrieved = document.get("retrieved")
    notes = document.get("notes")
    if not isinstance(network, str) or network not in T0_EVM_NETWORKS:
        raise Permit2ConfigError("network")
    if filename is not None and filename != f"{network}.json":
        raise Permit2ConfigError("filename")
    if not isinstance(address, str):
        raise Permit2ConfigError("address")
    try:
        parsed = parse_address("evm", address)
    except IdentityError as exc:
        raise Permit2ConfigError(exc.reason) from None
    if parsed.canonical != address or parsed.canonical != CANONICAL_PERMIT2:
        raise Permit2ConfigError("checksum")
    if status != "documented":
        raise Permit2ConfigError("status")
    if not isinstance(source_url, str) or not source_url.startswith(_SOURCE):
        raise Permit2ConfigError("source_url")
    if not isinstance(retrieved, str) or _DATE.fullmatch(retrieved) is None:
        raise Permit2ConfigError("retrieved")
    if not isinstance(notes, str) or _NOTES not in notes:
        raise Permit2ConfigError("notes")
    return Permit2Deployment(network, parsed.canonical, status, source_url, retrieved, notes)


def load_permit2_deployments(directory: Path | None = None) -> dict[str, Permit2Deployment]:
    folder = directory or DEFAULT_PERMIT2_DIR
    found: dict[str, Permit2Deployment] = {}
    paths = sorted(Path(folder).glob("*.json"))
    if not paths:
        raise Permit2ConfigError("missing")
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        deployment = validate_permit2_document(document, filename=path.name)
        if deployment.network in found:
            raise Permit2ConfigError("duplicate_network")
        found[deployment.network] = deployment
    if set(found) != T0_EVM_NETWORKS:
        raise Permit2ConfigError("t0_set")
    return found


def permit2_for(network: str, directory: Path | None = None) -> Permit2Deployment:
    deployments = load_permit2_deployments(directory)
    try:
        return deployments[network]
    except KeyError:
        raise Permit2ConfigError("network") from None


def coverage_for_submitted(
    permits: Sequence[SubmittedPermit],
    *,
    nonce_known: bool,
    pagination_complete: bool,
    gap_detection_complete: bool,
    gaps: tuple[str, ...] = (),
    previous_cursor: str | None = None,
    next_cursor: str | None = None,
) -> dict[str, CoverageObservation]:
    """Nonce evidence that is missing keeps the permit capabilities incomplete."""
    cursor = kept_cursor(
        previous_cursor,
        next_cursor,
        complete=nonce_known and pagination_complete and gap_detection_complete and not gaps,
    )
    coverage: dict[str, CoverageObservation] = {}
    for capability in ("permits.dai_variant", "permits.erc2612_observed"):
        matched = [item for item in permits if item.capability == capability]
        unknown = any(item.status != "observed" for item in matched)
        coverage[capability] = permission_observation(
            found=any(item.status == "observed" for item in matched),
            pagination_complete=pagination_complete and nonce_known and not unknown,
            gap_detection_complete=gap_detection_complete and nonce_known and not unknown,
            gaps=gaps,
            read_failed=not nonce_known,
            cursor=cursor,
            extra_reasons=() if nonce_known and not unknown else ("nonce_evidence_missing",),
            declared="submitted_permit",
        )
    coverage["permits.offchain_unused"] = offchain_unused_status().observation
    return coverage


def observe_submitted_permits(
    transactions: Sequence[Mapping[str, Any]],
    logs: Sequence[Mapping[str, Any]],
    owner: str,
    *,
    nonces: Mapping[str, tuple[int | None, int | None]],
) -> tuple[SubmittedPermit, ...]:
    """Attribute a submitted permit to its owner. A relayer or multicall may send it."""
    wallet = normalize_address(owner)
    if wallet is None:
        raise ValueError("bad_address")
    parsed, _reasons = parse_approval_logs(logs, wallet)
    found: list[SubmittedPermit] = []
    for tx in transactions:
        if not isinstance(tx, Mapping):
            continue
        blob = _calldata(tx.get("input", tx.get("data")))
        if blob is None:
            continue
        target = normalize_address(tx.get("to"))
        sender = normalize_address(tx.get("from"))
        tx_id = _tx_id(tx)
        for call in _iter_permits(blob, target, "direct"):
            if call.owner != wallet:
                continue
            approval = _matching_approval(parsed, tx_id, call)
            token = approval[0] if approval is not None else call.target
            before, after = _nonce_pair(nonces, token)
            incremented = _incremented(before, after)
            status = "observed" if approval is not None and incremented is True else "unknown"
            amount = approval[1] if approval is not None and call.kind == "dai" else call.value
            found.append(SubmittedPermit(
                capability="permits.dai_variant" if call.kind == "dai" else "permits.erc2612_observed",
                owner=wallet,
                spender=call.spender,
                token=token,
                tx_from=sender,
                raw_amount=None if amount is None else str(amount),
                allowed=call.allowed,
                nonce_before=None if before is None else str(before),
                nonce_after=None if after is None else str(after),
                nonce_incremented=incremented,
                via=call.via,
                status=status,
                tx_id=tx_id,
                calldata_nonce=None if call.calldata_nonce is None else str(call.calldata_nonce),
                deadline=None if call.deadline is None else str(call.deadline),
            ))
    return tuple(found)


def reconcile_permit2(
    logs: Sequence[Mapping[str, Any]],
    owner: str,
    permit2: str,
    *,
    states: Mapping[tuple[str, str], StateRead],
    prior: Mapping[tuple[str, str], PriorState] | None = None,
    bitmap_uses: Sequence[SignatureUse] = (),
    pagination_complete: bool = False,
    gap_detection_complete: bool = False,
    gaps: tuple[str, ...] = (),
    read_failed: bool = False,
    previous_cursor: str | None = None,
    next_cursor: str | None = None,
    scope_from: str | None = None,
    scope_to: str | None = None,
    now: int | None = None,
    erc20_rows: Sequence[ApprovalRow] = (),
) -> PermitReport:
    """Permit2 spender allowances stay separate from token approvals and from used nonces."""
    wallet = normalize_address(owner)
    contract = normalize_address(permit2)
    if wallet is None or contract is None:
        raise ValueError("bad_address")
    events, uses, reasons = parse_permit2_logs(logs, wallet, contract)
    grouped: dict[tuple[str, str], list[_Permit2Log]] = {}
    order: list[tuple[str, str]] = []
    for event in events:
        if event.token is None or event.spender is None:
            reasons = tuple(_unique((*reasons, "malformed_permit2_log")))
            continue
        key = (event.token, event.spender)
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(event)
    allowances: list[Permit2Allowance] = []
    state_failed = read_failed or bool(reasons)
    for key in order:
        state = states.get(key)
        if state is None or not state.ok:
            state_failed = True
        allowances.extend(_allowance_rows(
            grouped[key],
            state if state is not None else StateRead(False, reason="not_read"),
            (prior or {}).get(key, PriorState(False)),
            wallet,
            now,
        ))
    signature_uses = _merge_uses(uses, bitmap_uses)
    complete = (
        not read_failed
        and not reasons
        and not state_failed
        and pagination_complete
        and gap_detection_complete
        and not gaps
    )
    cursor = kept_cursor(previous_cursor, next_cursor, complete=complete)
    token_layer = tuple(row for row in erc20_rows if row.layer == "erc20_to_permit2")
    coverage = {
        "permit2.allowance": permission_observation(
            found=bool(allowances),
            pagination_complete=pagination_complete and not reasons and not read_failed,
            gap_detection_complete=gap_detection_complete and not reasons and not read_failed,
            gaps=gaps,
            read_failed=read_failed or bool(reasons) or state_failed,
            cursor=cursor,
            scope_from=scope_from,
            scope_to=scope_to,
            extra_reasons=tuple(reasons),
            declared="permit2_allowance",
        ),
        "permit2.signature_usage": permission_observation(
            found=bool(signature_uses),
            pagination_complete=False,
            gap_detection_complete=False,
            gaps=(),
            read_failed=read_failed,
            cursor=previous_cursor if read_failed else cursor,
            extra_reasons=() if read_failed else ("used_nonces_only",),
            declared="used_nonce_bits",
        ),
    }
    offchain = offchain_unused_status()
    summary = history_summary(found=bool(allowances) or bool(token_layer), complete=complete)
    return PermitReport(
        (),
        tuple(allowances),
        signature_uses,
        token_layer,
        coverage,
        offchain,
        cursor,
        summary,
    )


def parse_permit2_logs(
    logs: Sequence[Mapping[str, Any]],
    owner: str,
    permit2: str,
) -> tuple[tuple[_Permit2Log, ...], tuple[SignatureUse, ...], tuple[str, ...]]:
    wallet = normalize_address(owner)
    contract = normalize_address(permit2)
    if wallet is None or contract is None:
        raise ValueError("bad_address")
    events: list[_Permit2Log] = []
    uses: list[SignatureUse] = []
    reasons: list[str] = []
    for log in logs:
        if not isinstance(log, Mapping):
            reasons.append("malformed_permit2_log")
            continue
        if normalize_address(log.get("address")) != contract:
            continue
        item, use, reason = _parse_permit2(log, wallet)
        if reason:
            reasons.append(reason)
        if item is not None:
            events.append(item)
        if use is not None:
            uses.extend(use)
    events.sort(key=lambda item: (item.block_number is None, item.block_number or 0, item.log_index))
    return tuple(events), tuple(uses), tuple(_unique(reasons))


def read_token_nonce(transport: Any, source_id: str, token: str, owner: str) -> int | None:
    ok, raw, _reason = read_view(transport, source_id, token, "nonces(address)", [owner])
    if not ok or raw is None:
        return None
    return decode_uint256(raw)


def read_permit2_allowance(
    transport: Any,
    source_id: str,
    permit2: str,
    owner: str,
    token: str,
    spender: str,
) -> StateRead:
    ok, raw, reason = read_view(
        transport,
        source_id,
        permit2,
        "allowance(address,address,address)",
        [owner, token, spender],
    )
    if not ok or raw is None:
        return StateRead(False, reason=reason or "read_error")
    words = decode_words(raw, 3)
    if words is None or words[0] >= _UINT160 or words[1] >= _UINT48 or words[2] >= _UINT48:
        return StateRead(False, reason="malformed_allowance")
    return StateRead(True, amount=words[0], expiration=words[1], nonce=words[2])


def read_nonce_bitmap(transport: Any, source_id: str, permit2: str, owner: str, word: int) -> int | None:
    ok, raw, _reason = read_view(transport, source_id, permit2, "nonceBitmap(address,uint256)", [owner, word])
    if not ok or raw is None:
        return None
    return decode_uint256(raw)


def permit2_log_operation(permit2: str, owner: str, start: int, end: int):
    contract = normalize_address(permit2)
    wallet = normalize_address(owner)
    if contract is None or wallet is None:
        raise ValueError("bad_address")
    return evm_rpc("eth_getLogs", [{
        "address": contract,
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [list(PERMIT2_LOG_TOPICS), pad_address(wallet)],
    }])


def read_permit2_logs(
    transport: Any,
    source_id: str,
    permit2: str,
    owner: str,
    start: int,
    end: int,
) -> tuple[bool, tuple[Mapping[str, Any], ...], str]:
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or end < start:
        return False, (), "bad_range"
    if end - start > LOG_RANGE_MAX:
        return False, (), "log_range_exceeds_bound"
    try:
        result = transport.request(source_id, permit2_log_operation(permit2, owner, start, end))
    except Exception:
        return False, (), "read_error"
    if not getattr(result, "ok", False) or not isinstance(getattr(result, "body", None), dict):
        return False, (), "read_error"
    logs = result.body.get("result")
    if not isinstance(logs, list) or any(not isinstance(item, Mapping) for item in logs):
        return False, (), "malformed_logs"
    return True, tuple(logs), ""


def _allowance_rows(
    events: Sequence[_Permit2Log],
    state: StateRead,
    prior: PriorState,
    owner: str,
    now: int | None,
) -> list[Permit2Allowance]:
    rows: list[Permit2Allowance] = []
    running = prior
    for index, event in enumerate(events):
        last = index == len(events) - 1
        if event.kind == "unordered_nonce":
            continue
        if not last:
            amount = event.amount if event.amount is not None else running.amount
            expiration = event.expiration if event.expiration is not None else running.expiration
            nonce = event.nonce if event.nonce is not None else running.nonce
            if amount is None:
                current = StateRead(False, reason="amount_unknown")
            else:
                current = StateRead(True, amount=amount, expiration=expiration, nonce=nonce)
        else:
            current = state
        change, status, reasons, raw_amount, expiration, nonce = _classify_permit2(event, running, current, now)
        token = event.token or ""
        spender = event.spender or ""
        rows.append(Permit2Allowance(
            capability="permit2.allowance",
            layer="permit2_to_spender",
            owner=owner,
            token=token,
            spender=spender,
            raw_amount=raw_amount,
            expiration=expiration,
            nonce=nonce,
            change=change,
            status=status,
            persistent_allowance=True,
            reason_codes=reasons,
            tx_id=event.tx_id,
            log_index=event.log_index,
        ))
        if current.ok and current.amount is not None:
            running = PriorState(
                True,
                amount=current.amount,
                expiration=current.expiration,
                nonce=current.nonce,
            )
    return rows


def _classify_permit2(
    event: _Permit2Log,
    prior: PriorState,
    state: StateRead,
    now: int | None,
) -> tuple[str, str, tuple[str, ...], str | None, str | None, str | None]:
    if event.kind == "lockdown":
        if not state.ok or state.amount is None:
            change = "first_observed" if not prior.known else "unknown"
            return change, "unknown", ("current_state_unreadable",), None, None, None
        if state.amount == 0:
            return "revoked", "revoked", (), "0", _text(state.expiration), _text(state.nonce)
        change, status, reasons = classify_erc20(
            prior_known=prior.known,
            prior_amount=prior.amount,
            prior_expiration=prior.expiration,
            current_ok=True,
            current_amount=state.amount,
            current_expiration=state.expiration,
            event_amount=0,
            consumed=False,
            now=now,
        )
        return (
            change,
            status,
            tuple(_unique((*reasons, "event_state_mismatch"))),
            str(state.amount),
            _text(state.expiration),
            _text(state.nonce),
        )
    if not state.ok or state.amount is None:
        change = "first_observed" if not prior.known else "unknown"
        return change, "unknown", ("current_state_unreadable",), None, None, None
    reasons_extra: tuple[str, ...] = ()
    if event.kind == "nonce_invalidation":
        reasons_extra = ("nonce_invalidated",)
    change, status, reasons = classify_erc20(
        prior_known=prior.known,
        prior_amount=prior.amount,
        prior_expiration=prior.expiration,
        current_ok=True,
        current_amount=state.amount,
        current_expiration=state.expiration,
        event_amount=event.amount,
        consumed=False,
        now=now,
    )
    if event.expiration is not None and state.expiration is not None and event.expiration != state.expiration:
        reasons = tuple(_unique((*reasons, "event_state_mismatch")))
    return (
        change,
        status,
        tuple(_unique((*reasons, *reasons_extra))),
        str(state.amount),
        _text(state.expiration),
        _text(state.nonce),
    )


def _parse_permit2(
    log: Mapping[str, Any],
    owner: str,
) -> tuple[_Permit2Log | None, tuple[SignatureUse, ...] | None, str | None]:
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics or any(not isinstance(item, str) for item in topics):
        return None, None, None
    topic0 = topics[0].lower()
    if topic0 not in set(PERMIT2_LOG_TOPICS):
        return None, None, None
    tx_id = _log_tx(log)
    index = _log_index(log)
    if index is None:
        return None, None, "malformed_permit2_log"
    block = _quantity(log.get("blockNumber"))
    data = log.get("data") if isinstance(log.get("data"), str) else None
    if topic0 in {PERMIT2_APPROVAL_TOPIC, PERMIT2_PERMIT_TOPIC}:
        return _parse_allowance_event(topics, data, owner, topic0, tx_id, index, block)
    if topic0 == PERMIT2_LOCKDOWN_TOPIC:
        return _parse_lockdown(topics, data, owner, tx_id, index, block)
    if topic0 == PERMIT2_NONCE_TOPIC:
        return _parse_nonce_invalidation(topics, data, owner, tx_id, index, block)
    return _parse_unordered(topics, data, owner, tx_id, index, block)


def _parse_allowance_event(
    topics: Sequence[Any],
    data: str | None,
    owner: str,
    topic0: str,
    tx_id: str | None,
    index: int,
    block: int | None,
) -> tuple[_Permit2Log | None, None, str | None]:
    if len(topics) != 4:
        return None, None, "malformed_permit2_log"
    wallet = _topic_address(topics[1])
    token = _topic_address(topics[2])
    spender = _topic_address(topics[3])
    if wallet != owner:
        return None, None, None
    count = 3 if topic0 == PERMIT2_PERMIT_TOPIC else 2
    words = decode_words(data, count)
    if token is None or spender is None or words is None:
        return None, None, "malformed_permit2_log"
    amount, expiration = words[0], words[1]
    nonce = words[2] if count == 3 else None
    if amount >= _UINT160 or expiration >= _UINT48 or (nonce is not None and nonce >= _UINT48):
        return None, None, "malformed_permit2_log"
    kind = "permit" if topic0 == PERMIT2_PERMIT_TOPIC else "approval"
    return _Permit2Log(kind, owner, token, spender, amount, expiration, nonce, None, None, None, tx_id, index, block), None, None


def _parse_lockdown(
    topics: Sequence[Any],
    data: str | None,
    owner: str,
    tx_id: str | None,
    index: int,
    block: int | None,
) -> tuple[_Permit2Log | None, None, str | None]:
    if len(topics) != 2:
        return None, None, "malformed_permit2_log"
    wallet = _topic_address(topics[1])
    if wallet != owner:
        return None, None, None
    words = decode_words(data, 2)
    if words is None:
        return None, None, "malformed_permit2_log"
    token = _address_word(words[0])
    spender = _address_word(words[1])
    if token is None or spender is None:
        return None, None, "malformed_permit2_log"
    return _Permit2Log("lockdown", owner, token, spender, None, None, None, None, None, None, tx_id, index, block), None, None


def _parse_nonce_invalidation(
    topics: Sequence[Any],
    data: str | None,
    owner: str,
    tx_id: str | None,
    index: int,
    block: int | None,
) -> tuple[_Permit2Log | None, None, str | None]:
    if len(topics) != 4:
        return None, None, "malformed_permit2_log"
    wallet = _topic_address(topics[1])
    token = _topic_address(topics[2])
    spender = _topic_address(topics[3])
    if wallet != owner:
        return None, None, None
    words = decode_words(data, 2)
    if token is None or spender is None or words is None or words[0] >= _UINT48 or words[1] >= _UINT48:
        return None, None, "malformed_permit2_log"
    return _Permit2Log(
        "nonce_invalidation",
        owner,
        token,
        spender,
        None,
        None,
        words[0],
        words[1],
        None,
        None,
        tx_id,
        index,
        block,
    ), None, None


def _parse_unordered(
    topics: Sequence[Any],
    data: str | None,
    owner: str,
    tx_id: str | None,
    index: int,
    block: int | None,
) -> tuple[None, tuple[SignatureUse, ...] | None, str | None]:
    if len(topics) != 2:
        return None, None, "malformed_permit2_log"
    wallet = _topic_address(topics[1])
    if wallet != owner:
        return None, None, None
    words = decode_words(data, 2)
    if words is None:
        return None, None, "malformed_permit2_log"
    try:
        uses = signature_uses_from_bitmap(owner, words[0], words[1], source="unordered_nonce_invalidation")
    except ValueError:
        return None, None, "malformed_permit2_log"
    return None, uses, None


def _iter_permits(blob: bytes, target: str | None, via: str, depth: int = 0) -> list[_PermitCall]:
    if depth > 4 or len(blob) < 4:
        return []
    selector, body = blob[:4], blob[4:]
    if selector == _SEL_2612:
        call = _decode_2612(body, target, via)
        return [] if call is None else [call]
    if selector == _SEL_DAI:
        call = _decode_dai(body, target, via)
        return [] if call is None else [call]
    if selector == _SEL_MULTICALL:
        try:
            inners = decode(["bytes[]"], body)[0]
        except Exception:
            return []
        found: list[_PermitCall] = []
        for inner in inners:
            if isinstance(inner, (bytes, bytearray)):
                found.extend(_iter_permits(bytes(inner), target, "multicall", depth + 1))
        return found
    if selector == _SEL_AGGREGATE:
        try:
            rows = decode(["(address,bytes)[]"], body)[0]
        except Exception:
            return []
        found = []
        for row_target, inner in rows:
            found.extend(_iter_permits(bytes(inner), normalize_address(row_target), "multicall", depth + 1))
        return found
    if selector == _SEL_AGGREGATE3:
        try:
            rows = decode(["(address,bool,bytes)[]"], body)[0]
        except Exception:
            return []
        found = []
        for row_target, _flag, inner in rows:
            found.extend(_iter_permits(bytes(inner), normalize_address(row_target), "multicall", depth + 1))
        return found
    return []


def _decode_2612(body: bytes, target: str | None, via: str) -> _PermitCall | None:
    try:
        owner, spender, value, deadline, _v, _r, _s = decode(
            ["address", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
            body,
        )
    except Exception:
        return None
    wallet = normalize_address(owner)
    allowed_spender = normalize_address(spender)
    if wallet is None or allowed_spender is None or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
        return None
    return _PermitCall("erc2612", wallet, allowed_spender, value, None, target, via, int(deadline), None)


def _decode_dai(body: bytes, target: str | None, via: str) -> _PermitCall | None:
    try:
        holder, spender, nonce, expiry, allowed, _v, _r, _s = decode(
            ["address", "address", "uint256", "uint256", "bool", "uint8", "bytes32", "bytes32"],
            body,
        )
    except Exception:
        return None
    wallet = normalize_address(holder)
    allowed_spender = normalize_address(spender)
    if wallet is None or allowed_spender is None or not isinstance(allowed, bool):
        return None
    if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0 or nonce > UINT256_MAX:
        return None
    return _PermitCall("dai", wallet, allowed_spender, None, allowed, target, via, int(expiry), int(nonce))


def _matching_approval(
    parsed: Sequence[Any],
    tx_id: str | None,
    call: _PermitCall,
) -> tuple[str, int | None] | None:
    for item in parsed:
        if item.family != "erc20" or item.owner != call.owner or item.spender != call.spender:
            continue
        if tx_id is not None and item.tx_id != tx_id:
            continue
        if call.target is not None and item.token != call.target:
            continue
        return item.token, item.amount
    return None


def _nonce_pair(
    nonces: Mapping[str, tuple[int | None, int | None]],
    token: str | None,
) -> tuple[int | None, int | None]:
    if token is None:
        return None, None
    for key, value in nonces.items():
        if normalize_address(key) == token and isinstance(value, tuple) and len(value) == 2:
            before, after = value
            return _nonce_int(before), _nonce_int(after)
    return None, None


def _nonce_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _incremented(before: int | None, after: int | None) -> bool | None:
    if before is None or after is None:
        return None
    return after > before


def _merge_uses(events: Sequence[SignatureUse], bitmap: Sequence[SignatureUse]) -> tuple[SignatureUse, ...]:
    found: dict[tuple[str, str], SignatureUse] = {}
    for item in (*bitmap, *events):
        found[(item.owner, item.nonce)] = item
    return tuple(found[key] for key in sorted(found))


def _calldata(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.startswith(("0x", "0X")) or len(value) % 2 != 0:
        return None
    try:
        return bytes.fromhex(value[2:])
    except ValueError:
        return None


def _tx_id(tx: Mapping[str, Any]) -> str | None:
    for key in ("hash", "transactionHash", "tx_id"):
        value = tx.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            return value.lower()
    return None


def _log_tx(log: Mapping[str, Any]) -> str | None:
    for key in ("transactionHash", "hash"):
        value = log.get(key)
        if isinstance(value, str) and value.startswith("0x"):
            return value.lower()
    return None


def _log_index(log: Mapping[str, Any]) -> int | None:
    for key in ("logIndex", "log_index"):
        if key in log:
            return _quantity(log.get(key))
    return None


def _topic_address(topic: Any) -> str | None:
    if not isinstance(topic, str) or not topic.startswith(("0x", "0X")):
        return None
    body = topic[2:]
    if len(body) != 64:
        return None
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None
    if raw[:12] != bytes(12):
        return None
    return "0x" + raw[-20:].hex()


def _address_word(word: int) -> str | None:
    if word < 0 or word >= 2**160:
        return None
    return "0x" + word.to_bytes(20, "big").hex()


def _text(value: int | None) -> str | None:
    if value is None:
        return None
    return str(value)


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


def _unique(values: Sequence[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value not in found:
            found.append(value)
    return found

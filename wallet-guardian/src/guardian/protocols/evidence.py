"""Claim and bridge evidence. A source transaction is not a destination completion.

``is_claimable`` is never derived from a waiting period. A request that leaves
the on-chain list without a successful claim event stays ``disappeared_unverified``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from guardian.events import ChainEvent, FlowStatus, flow_status
from guardian.protocols.models import WithdrawalAssessment, WithdrawalRequest

_SUCCESS = frozenset({"0x01", "0x1", "success"})


def assess_withdrawals(
    prior_ids: Sequence[str],
    current: Sequence[WithdrawalRequest],
    evidence: Sequence[Mapping[str, Any]] | Sequence[Any],
) -> tuple[WithdrawalAssessment, ...]:
    """Compare the previous request ids with the current list.

    The clock is intentionally not an argument. Claimable comes only from
    ``WithdrawalRequest.is_claimable``.
    """
    current_ids = {item.request_id for item in current}
    if len(current_ids) != len(current):
        raise ValueError("duplicate_request_id")
    proven = _successful_claims(evidence)
    rows: list[WithdrawalAssessment] = []
    for item in current:
        rows.append(WithdrawalAssessment(
            request_id=item.request_id,
            status="open",
            is_claimable=item.is_claimable,
            reason="on_chain_status",
        ))
    for request_id in prior_ids:
        if not isinstance(request_id, str) or not request_id or request_id in current_ids:
            continue
        if request_id in proven:
            rows.append(WithdrawalAssessment(
                request_id=request_id,
                status="claimed",
                is_claimable=None,
                reason="claim_evidence",
            ))
        else:
            rows.append(WithdrawalAssessment(
                request_id=request_id,
                status="disappeared_unverified",
                is_claimable=None,
                reason="missing_without_claim_evidence",
            ))
    return tuple(rows)


def assess_bridge(
    source: ChainEvent | None = None,
    destination: ChainEvent | None = None,
    *,
    refund: ChainEvent | None = None,
    timed_out: bool = False,
    plugin_has_data: bool = True,
) -> tuple[FlowStatus, str]:
    """Source success stays source_confirmed or source_finalized until the destination is seen.

    A plugin with no data is unknown. A timeout is timed_out, not failed and not lost.
    """
    if not plugin_has_data:
        return FlowStatus.unknown, "plugin_without_data"
    status = flow_status(source, destination, refund_event=refund, timed_out=timed_out)
    if status == FlowStatus.completed:
        return status, "destination_confirmed"
    if status == FlowStatus.timed_out:
        return status, "timed_out"
    if status in {FlowStatus.source_confirmed, FlowStatus.source_finalized, FlowStatus.initiated}:
        return status, "destination_not_observed"
    if status == FlowStatus.refunded:
        return status, "refunded"
    if status == FlowStatus.destination_observed:
        return status, "destination_unconfirmed"
    return status, "unknown"


def _successful_claims(evidence: Sequence[Any]) -> set[str]:
    proven: set[str] = set()
    for item in evidence:
        if isinstance(item, Mapping):
            request_id = item.get("request_id")
            tx_status = item.get("tx_status")
        else:
            request_id = getattr(item, "request_id", None)
            tx_status = getattr(item, "tx_status", None)
        if not isinstance(request_id, str) or not request_id:
            continue
        if isinstance(tx_status, str) and tx_status.strip().lower() in _SUCCESS:
            proven.add(request_id)
    return proven

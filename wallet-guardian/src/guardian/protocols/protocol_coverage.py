"""Coverage rows for positions, withdrawals, bridges, and exposure news.

An unknown plugin is named in the row. A plugin with no data is not an empty
bridge book and not a complete news scan.
"""

from __future__ import annotations

from typing import Sequence

from guardian.coverage import CoverageObservation
from guardian.events import FlowStatus
from guardian.protocols.models import PositionObservation
from guardian.protocols.registry import PLUGIN_REGISTRY

_SOURCE = "protocol-plugin"


def build_protocol_coverage(
    *,
    wallet_id: str,
    network: str,
    positions: Sequence[PositionObservation],
    requested_plugins: Sequence[str],
    bridge_status: FlowStatus | None = None,
    bridge_known: bool = False,
    news_known: bool = False,
    prior_success_at: str | None = None,
) -> dict[str, CoverageObservation]:
    by_plugin = {item.plugin_id: item for item in positions}
    unknown = [name for name in requested_plugins if name not in PLUGIN_REGISTRY]
    missing = [name for name in requested_plugins if name in PLUGIN_REGISTRY and name not in by_plugin]
    failed = [item for item in positions if item.status == "unknown"]
    reasons = [f"unknown_plugin:{name}" for name in unknown]
    reasons.extend(f"plugin_not_run:{name}" for name in missing)
    for item in failed:
        reasons.extend(item.reason_codes)
    if unknown or missing or failed:
        cursor = failed[0].cursor if failed else None
        position_row = _error(tuple(reasons), cursor, prior_success_at, tuple(f"unknown_plugin:{name}" for name in unknown))
    elif positions and all(item.status == "none_observed" for item in positions):
        position_row = _done("empty", positions[-1].cursor)
    elif positions:
        position_row = _done("ok", positions[-1].cursor)
    else:
        position_row = _error(("not_queried",), None, prior_success_at, ())

    withdrawal_reasons = [
        assessment.status
        for item in positions
        for assessment in item.assessments
        if assessment.status == "disappeared_unverified"
    ]
    if failed:
        withdrawal_row = _error(("read_failed",), failed[0].cursor, prior_success_at, ())
    elif withdrawal_reasons:
        withdrawal_row = CoverageObservation(
            category="partial",
            pagination_complete=True,
            gap_detection_complete=True,
            gaps=tuple(withdrawal_reasons),
            cursor=positions[-1].cursor if positions else None,
            declared="current_state",
            sources=(_SOURCE,),
            trust="unverified",
            price_quality="unknown",
            reason_codes=("disappeared_unverified",),
        )
    elif positions:
        withdrawal_row = _done(
            "empty" if all(item.status == "none_observed" and not item.withdrawals for item in positions) else "ok",
            positions[-1].cursor,
        )
    else:
        withdrawal_row = _error(("not_queried",), None, prior_success_at, ())

    if not bridge_known or bridge_status is None:
        bridge_row = _error(("plugin_without_data",), None, prior_success_at, ())
    elif bridge_status == FlowStatus.completed:
        bridge_row = _done("ok", None)
    elif bridge_status == FlowStatus.unknown:
        bridge_row = _error(("unknown",), None, prior_success_at, ())
    elif bridge_status == FlowStatus.timed_out:
        bridge_row = _partial("timed_out")
    else:
        bridge_row = _partial("destination_not_observed")

    news_row = _done("empty", None) if news_known else _error(("news_not_parsed",), None, prior_success_at, ())
    return {
        "bridges": bridge_row,
        "exposure_news": news_row,
        "positions": position_row,
        "withdrawals": withdrawal_row,
    }


def _done(category: str, cursor: str | None) -> CoverageObservation:
    return CoverageObservation(
        category=category,
        pagination_complete=True,
        gap_detection_complete=True,
        cursor=cursor,
        declared="current_state",
        sources=(_SOURCE,),
        trust="unverified",
        price_quality="unknown",
    )


def _partial(reason: str) -> CoverageObservation:
    return CoverageObservation(
        category="partial",
        pagination_complete=True,
        gap_detection_complete=False,
        declared="current_state",
        sources=(_SOURCE,),
        trust="unverified",
        price_quality="unknown",
        reason_codes=(reason,),
    )


def _error(
    reasons: tuple[str, ...],
    cursor: str | None,
    prior_success_at: str | None,
    gaps: tuple[str, ...],
) -> CoverageObservation:
    return CoverageObservation(
        category="error",
        pagination_complete=False,
        gap_detection_complete=False,
        gaps=gaps,
        cursor=cursor,
        declared="current_state",
        last_success_at=prior_success_at,
        sources=(_SOURCE,),
        trust="unverified",
        price_quality="unknown",
        reason_codes=reasons,
    )

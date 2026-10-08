"""Position observations. A failed read is unknown and keeps the prior cursor.

``none_observed`` is only a successful empty read. It is not a substitute for a
read that did not happen.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

_STATUSES = frozenset({"none_observed", "observed", "unknown"})
_KINDS = frozenset({"debt", "receipt", "underlying"})
_WITHDRAWAL = frozenset({"claimed", "disappeared_unverified", "open"})


@dataclass(frozen=True)
class PositionLeg:
    """One receipt, its underlying quote, or a debt. Amounts are non-negative integers."""

    kind: str
    identity: str
    raw_amount: int
    decimals: int | None
    network: str
    plugin_id: str
    underlying_identity: str | None = None
    duplicates: str | None = None
    detail: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError("leg_kind")
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("leg_identity")
        if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int) or self.raw_amount < 0:
            raise ValueError("leg_amount")
        if self.decimals is not None and (isinstance(self.decimals, bool) or not isinstance(self.decimals, int)):
            raise ValueError("leg_decimals")
        if self.decimals is not None and not 0 <= self.decimals <= 255:
            raise ValueError("leg_decimals")
        object.__setattr__(self, "detail", tuple(self.detail))


@dataclass(frozen=True)
class WithdrawalRequest:
    """One open Lista request. ``is_claimable`` is the chain status bit, not a clock."""

    request_id: str
    index: int
    slis_raw: int
    bnb_raw: int
    is_claimable: bool
    start_time: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id")
        for name in ("index", "slis_raw", "bnb_raw", "start_time"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(name)
        if not isinstance(self.is_claimable, bool):
            raise ValueError("is_claimable")


@dataclass(frozen=True)
class WithdrawalAssessment:
    """What happened to a request id. Disappearance without a claim tx is not claimed."""

    request_id: str
    status: str
    is_claimable: bool | None
    reason: str

    def __post_init__(self) -> None:
        if self.status not in _WITHDRAWAL:
            raise ValueError("withdrawal_status")
        if self.status == "open" and not isinstance(self.is_claimable, bool):
            raise ValueError("open_requires_status")
        if self.status != "open" and self.is_claimable is not None:
            raise ValueError("closed_claimable_unknown")


@dataclass(frozen=True)
class PositionObservation:
    plugin_id: str
    wallet_id: str
    network: str
    status: str
    legs: tuple[PositionLeg, ...] = ()
    withdrawals: tuple[WithdrawalRequest, ...] = ()
    assessments: tuple[WithdrawalAssessment, ...] = ()
    reason_codes: tuple[str, ...] = ()
    cursor: str | None = None
    prior_kept: bool = False
    retained_prior: Mapping[str, Any] | None = None
    metrics: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("position_status")
        if self.status == "unknown" and (self.legs or self.withdrawals):
            raise ValueError("unknown_must_not_invent_legs")
        if self.status == "none_observed" and self.legs:
            raise ValueError("none_observed_has_legs")
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "withdrawals", tuple(self.withdrawals))
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "metrics", tuple(self.metrics))
        if self.cursor is not None and not isinstance(self.cursor, str):
            raise ValueError("cursor")


def contract_address(manifest: Any, network: str, role: str) -> str | None:
    contracts = getattr(manifest, "contracts", None)
    if not isinstance(contracts, Mapping):
        return None
    per_network = contracts.get(network)
    if not isinstance(per_network, Mapping):
        return None
    entry = per_network.get(role)
    if not isinstance(entry, Mapping):
        return None
    address = entry.get("address")
    if not isinstance(address, str) or not address:
        return None
    return address


def prior_cursor(prior: Mapping[str, Any] | None) -> str | None:
    if not isinstance(prior, Mapping):
        return None
    cursor = prior.get("cursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def unknown_position(
    *,
    plugin_id: str,
    wallet_id: str,
    network: str,
    prior: Mapping[str, Any] | None,
    reason: str,
    detail: str = "",
) -> PositionObservation:
    """Keep the prior cursor and do not replace it with empty legs."""
    codes = (reason,) if not detail or detail == reason else (reason, detail)
    retained = deepcopy(dict(prior)) if isinstance(prior, Mapping) else {}
    return PositionObservation(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        status="unknown",
        reason_codes=codes,
        cursor=prior_cursor(prior),
        prior_kept=True,
        retained_prior=retained,
    )

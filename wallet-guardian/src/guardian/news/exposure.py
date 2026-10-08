"""Link a news incident to a wallet only through a real position or a live approval.

An unknown position becomes one deduped check item, not a loss alert. No position
and no active approval produces no notice. Kinza, Lista, and UXLINK are not
attached just because the wallet exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionObservation
from guardian.protocols.pins import STAKE_PROGRAM, evm_contracts

_CONTRACTS = {
    "aave_v3": evm_contracts("aave_v3"),
    "erc4626": frozenset(),
    "kinza": frozenset(),
    "lista_slisbnb": evm_contracts("lista_slisbnb"),
    "solana_stake": frozenset({STAKE_PROGRAM}),
    "uxlink": frozenset(),
}


@dataclass(frozen=True)
class WalletExposure:
    wallet_id: str
    positions: tuple[PositionObservation, ...] = ()
    approvals: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ExposureNotice:
    wallet_id: str
    kind: str
    protocols: tuple[str, ...]
    incident_id: str | None
    loss_alert: bool
    via: str

    def __post_init__(self) -> None:
        if self.kind not in {"exposure", "exposure_unknown"}:
            raise ValueError("notice_kind")
        if self.loss_alert:
            raise ValueError("news_is_not_a_loss_alert")
        if self.kind == "exposure_unknown" and self.incident_id is not None:
            raise ValueError("exposure_unknown_is_deduped")


def link_exposure(incidents: Sequence[Any], wallets: Sequence[WalletExposure]) -> tuple[ExposureNotice, ...]:
    notices: list[ExposureNotice] = []
    for wallet in wallets:
        approved = _approved_protocols(wallet.approvals)
        unknown: set[str] = set()
        for incident in incidents:
            matched: list[str] = []
            vias: list[str] = []
            for protocol in incident.protocols:
                state = _position_state(wallet.positions, protocol)
                if state == "observed":
                    matched.append(protocol)
                    vias.append("position")
                elif protocol in approved:
                    matched.append(protocol)
                    vias.append("approval")
                elif state == "unknown":
                    unknown.add(protocol)
            if matched:
                notices.append(ExposureNotice(
                    wallet_id=wallet.wallet_id,
                    kind="exposure",
                    protocols=tuple(sorted(set(matched))),
                    incident_id=incident.incident_id,
                    loss_alert=False,
                    via="position" if "position" in vias else "approval",
                ))
        if unknown:
            notices.append(ExposureNotice(
                wallet_id=wallet.wallet_id,
                kind="exposure_unknown",
                protocols=tuple(sorted(unknown)),
                incident_id=None,
                loss_alert=False,
                via="check",
            ))
    return tuple(notices)


def _position_state(positions: Sequence[PositionObservation], protocol: str) -> str:
    rows = [item for item in positions if item.plugin_id == protocol]
    if any(item.status == "observed" for item in rows):
        return "observed"
    if any(item.status == "unknown" for item in rows):
        return "unknown"
    if rows:
        return "none"
    return "absent"


def _approved_protocols(approvals: Sequence[Any]) -> set[str]:
    found: set[str] = set()
    for approval in approvals:
        if _status(approval) != "active":
            continue
        spender = _spender(approval)
        if spender is None:
            continue
        for protocol, contracts in _CONTRACTS.items():
            if spender in contracts:
                found.add(protocol)
    return found


def _status(approval: Any) -> str:
    if isinstance(approval, Mapping):
        value = approval.get("status")
    else:
        value = getattr(approval, "status", None)
    return value if isinstance(value, str) else ""


def _spender(approval: Any) -> str | None:
    if isinstance(approval, Mapping):
        raw = approval.get("spender", approval.get("operator"))
    else:
        raw = getattr(approval, "spender", None) or getattr(approval, "operator", None)
    if not isinstance(raw, str) or not raw:
        return None
    evm = normalize_address(raw)
    if evm is not None:
        return evm
    return raw

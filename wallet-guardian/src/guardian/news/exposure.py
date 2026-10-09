"""Link a news incident to a wallet only through a real position or a live approval.

An unknown position becomes one deduped check item, not a loss alert. No position
and no active approval produces no notice. Kinza, Lido, Lista, and UXLINK are not
attached just because the wallet exists.

Kinza approval exposure matches the Pool only. The PoolAddressesProvider is a
registry identity, not a spender. UXLINK matches a pinned token (network plus
address) on an active approval, or a UXLINK holdings leg. An incident is not
expanded into a claim that every chain suffered the same event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.chains.evm.accounting import normalize_address
from guardian.news.feeds import parse_feed, parse_structured
from guardian.news.incidents import IncidentBook
from guardian.protocols.models import PositionLeg, PositionObservation
from guardian.protocols.pins import STAKE_PROGRAM, evm_contracts, uxlink_identity

_CONTRACTS = {
    "aave_v3": evm_contracts("aave_v3"),
    "erc4626": frozenset(),
    "kinza": evm_contracts("kinza"),
    "lido": evm_contracts("lido"),
    "lista_slisbnb": evm_contracts("lista_slisbnb"),
    "solana_stake": frozenset({STAKE_PROGRAM}),
    "uxlink": frozenset(),
}
_POSITION_PLUGIN = {
    "uxlink": "uxlink_holdings",
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
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"exposure", "exposure_unknown", "exposure_update"}:
            raise ValueError("notice_kind")
        if self.loss_alert:
            raise ValueError("news_is_not_a_loss_alert")
        if self.kind == "exposure_unknown" and self.incident_id is not None:
            raise ValueError("exposure_unknown_is_deduped")
        if self.kind == "exposure_update" and not self.incident_id:
            raise ValueError("exposure_update_needs_incident")


def load_caller_incidents(data: bytes) -> tuple[IncidentBook | None, str]:
    """Parse caller-supplied RSS, Atom, or structured rows. This does not fetch."""
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).strip():
        return None, "news_not_supplied"
    raw = bytes(data)
    stripped = raw.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return None, "news_unparseable"
        rows = document.get("items") if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            return None, "news_unparseable"
        cleaned = []
        for row in rows:
            if not isinstance(row, Mapping):
                return None, "news_unparseable"
            item = dict(row)
            item["source_id"] = "caller_supplied"
            cleaned.append(item)
        parsed = parse_structured(cleaned, source_id="caller_supplied")
    else:
        parsed = parse_feed(raw, source_id="caller_supplied")
    if not parsed.ok:
        return None, parsed.reason or "news_unparseable"
    book = IncidentBook()
    book.add_all(parsed.items)
    return book, ""


def exposure_alert(
    tenant: str,
    notice: ExposureNotice,
    network: str,
    *,
    source: str = "caller_supplied",
) -> dict[str, Any]:
    """One outbox row. Dedup is wallet plus incident, not a loss alert.

    An updated incident uses ``exposure_update`` and includes the content hash,
    so the same revision is deduped and a later revision can notify once.
    """
    if source not in {"allowlisted_get", "caller_supplied"}:
        raise ValueError("news_source")
    incident = notice.incident_id or "exposure_unknown"
    parts = [tenant, notice.wallet_id, notice.kind, incident]
    if notice.kind == "exposure_update":
        parts.append(notice.content_hash)
    material = "\x1f".join(parts)
    return {
        "dedup_key": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "identity": f"{notice.kind}:{incident}:{','.join(notice.protocols)}",
        "incident_id": notice.incident_id,
        "kind": notice.kind,
        "loss_alert": False,
        "network": network,
        "protocols": list(notice.protocols),
        "source": source,
        "valuation_unknown": False,
        "via": notice.via,
        "wallet_id": notice.wallet_id,
    }


def exposed_protocols(wallet: WalletExposure) -> frozenset[str]:
    """Canonical protocols with an observed or unknown position, a UXLINK leg, or an active approval."""
    found: set[str] = set()
    for protocol in _approved_protocols(wallet.approvals):
        found.add(protocol)
    for observation in wallet.positions:
        mapped = {
            "aave_v3": "aave_v3",
            "kinza": "kinza",
            "lido": "lido",
            "lista_slisbnb": "lista_slisbnb",
            "solana_stake": "solana_stake",
            "uxlink_holdings": "uxlink",
        }.get(observation.plugin_id)
        if observation.status in {"observed", "unknown"} and mapped:
            found.add(mapped)
        if _has_uxlink_leg(observation):
            found.add("uxlink")
    return frozenset(found)


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
    plugin_id = _POSITION_PLUGIN.get(protocol, protocol)
    rows = [item for item in positions if item.plugin_id == plugin_id or (protocol == "uxlink" and _has_uxlink_leg(item))]
    if any(item.status == "observed" for item in rows) or any(_has_uxlink_leg(item) for item in rows if protocol == "uxlink"):
        return "observed"
    if any(item.status == "unknown" for item in rows):
        return "unknown"
    if rows:
        return "none"
    return "absent"


def _has_uxlink_leg(observation: PositionObservation) -> bool:
    return any(_leg_is_uxlink(leg) for leg in observation.legs)


def _leg_is_uxlink(leg: PositionLeg) -> bool:
    if ":" not in leg.identity:
        return uxlink_identity(leg.network, leg.identity)
    network, address = leg.identity.split(":", 1)
    return uxlink_identity(network, address)


def _approved_protocols(approvals: Sequence[Any]) -> set[str]:
    found: set[str] = set()
    for approval in approvals:
        if _status(approval) != "active":
            continue
        spender = _spender(approval)
        if spender is not None:
            for protocol, contracts in _CONTRACTS.items():
                if spender in contracts:
                    found.add(protocol)
        token = _token(approval)
        network = _text(approval, "network")
        if token is not None and network is not None and uxlink_identity(network, token):
            found.add("uxlink")
    return found


def _status(approval: Any) -> str:
    if isinstance(approval, Mapping):
        value = approval.get("status")
    else:
        value = getattr(approval, "status", None)
    return value if isinstance(value, str) else ""


def _spender(approval: Any) -> str | None:
    return _address_field(approval, "spender", "operator")


def _token(approval: Any) -> str | None:
    return _address_field(approval, "token")


def _address_field(approval: Any, *names: str) -> str | None:
    raw = None
    for name in names:
        if isinstance(approval, Mapping):
            raw = approval.get(name)
        else:
            raw = getattr(approval, name, None)
        if raw:
            break
    if not isinstance(raw, str) or not raw:
        return None
    evm = normalize_address(raw)
    if evm is not None:
        return evm
    return raw


def _text(approval: Any, name: str) -> str | None:
    if isinstance(approval, Mapping):
        value = approval.get(name)
    else:
        value = getattr(approval, name, None)
    return value if isinstance(value, str) and value else None

"""Alert thresholds for the read-only monitor.

Native ETH uses wei. Every other asset, including wrapped ETH, uses a valued
USD quote. A missing price is valuation_unknown, never 0 and never 1 USD.
The symbol string is not an input to the native-ETH rule.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from guardian.errors import ConfigError

ETH_NETWORKS = frozenset({
    "ethereum-mainnet",
    "arbitrum-one",
    "base-mainnet",
    "optimism-mainnet",
})
_WEI = Decimal(10) ** 18
_PERMISSION_ALERTS = frozenset({
    "expiry_extended",
    "extended",
    "increased",
    "new",
    "new_authority",
    "new_delegate",
    "operator_enabled",
})
_INFORMATIONAL = frozenset({"decreased", "revoked", "revocation"})


@dataclass(frozen=True)
class Candidate:
    """One possible alert. ``symbol`` is ignored by the threshold rules."""

    tenant_id: str
    wallet_id: str
    network: str
    capability: str
    identity: str
    kind: str
    raw_amount: int | None = None
    is_native: bool = False
    direction: str | None = None
    counterparty_wallet_id: str | None = None
    quote_usd: Decimal | None = None
    quote_valuation: str | None = None
    quote_quality: str | None = None
    permission_change: str | None = None
    prior_known: bool = True
    explicit_risk: bool = False
    tx_id: str | None = None
    symbol: str | None = None
    net_amount: int | None = None
    transfer_origin: str | None = None
    attribution: str | None = None
    execution_evidence: str | None = None
    counterparty: str | None = None
    block_number: int | None = None
    asset: str | None = None


@dataclass(frozen=True)
class AlertDecision:
    alert: bool
    kind: str
    dedup_key: str
    tenant_id: str
    wallet_id: str
    network: str
    identity: str
    valuation_unknown: bool
    informational: bool = False
    gross: str | None = None
    net: str | None = None
    tx_id: str | None = None
    counts: tuple[tuple[str, int], ...] = ()
    asset: str | None = None
    attribution: str | None = None
    block_number: str | None = None
    direction: str | None = None
    execution_evidence: str | None = None
    raw_amount: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Public fields only. No address and no price treated as zero."""
        payload: dict[str, Any] = {
            "alert": self.alert,
            "counts": {key: value for key, value in self.counts},
            "dedup_key": self.dedup_key,
            "identity": self.identity,
            "informational": self.informational,
            "kind": self.kind,
            "network": self.network,
            "tenant_id": self.tenant_id,
            "valuation_unknown": self.valuation_unknown,
            "wallet_id": self.wallet_id,
        }
        if self.gross is not None:
            payload["gross"] = self.gross
        if self.net is not None:
            payload["net"] = self.net
        if self.tx_id:
            payload["tx_id"] = self.tx_id
        for key in ("asset", "attribution", "block_number", "direction", "execution_evidence", "raw_amount"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


class AlertPolicy:
    """OR of a native-unit floor and a valued USD floor. Permissions ignore both.

    Own-wallet pairs are still judged on their net. A native-unit floor does
    not turn that pair into two gross alerts.
    """

    def __init__(self, native_eth: Decimal, usd: Decimal, units: Mapping[str, Decimal] | None = None) -> None:
        self.native_eth = native_eth
        self.native_eth_wei = int((native_eth * _WEI).to_integral_value())
        self.usd = usd
        self.units = dict(units or {})

    @classmethod
    def from_public(cls, alerts: Mapping[str, Any]) -> AlertPolicy:
        if not isinstance(alerts, Mapping):
            raise ValueError("alerts config is missing")
        return cls(
            _decimal(alerts.get("native_eth_threshold"), "native_eth_threshold"),
            _decimal(alerts.get("asset_usd_threshold"), "asset_usd_threshold"),
            validate_native_unit_thresholds(alerts.get("native_unit_thresholds")),
        )

    def apply(
        self,
        candidates: Sequence[Candidate],
        *,
        baseline_scopes: Sequence[tuple[str, str, str]] = (),
    ) -> list[AlertDecision]:
        """Return one decision per surviving event. Baseline history is one summary."""
        scopes = set(baseline_scopes)
        rest, internal = _pair_internal(list(candidates))
        tenant_fallback = candidates[0].tenant_id if candidates else ""
        summaries: dict[tuple[str, str, str], list[Candidate]] = {scope: [] for scope in scopes}
        outgoing: list[Candidate] = []
        for candidate in [*rest, *internal]:
            scope = (candidate.wallet_id, candidate.network, candidate.capability)
            if scope in summaries:
                summaries[scope].append(candidate)
                continue
            outgoing.append(candidate)
        decisions = [self._one(candidate) for candidate in outgoing]
        for scope, grouped in summaries.items():
            decisions.append(self._summary(scope, grouped, tenant_fallback))
        return decisions

    def _one(self, candidate: Candidate) -> AlertDecision:
        if candidate.kind == "internal":
            return self._internal(candidate)
        if candidate.kind == "position":
            return self._decision(
                candidate,
                alert=True,
                kind="position_change",
                valuation_unknown=False,
                identity=candidate.identity,
            )
        if candidate.permission_change:
            return self._permission(candidate)
        eth_hit = self._native_eth_hit(candidate)
        unit_hit = self._native_unit_hit(candidate)
        usd_hit, unknown = self._usd_hit(candidate)
        if eth_hit:
            kind = "native_eth"
        elif usd_hit:
            kind = "asset_usd"
        elif unit_hit:
            kind = "native_unit"
        else:
            kind = "below_threshold"
        movement = _native_movement(candidate) if kind in {"native_eth", "asset_usd", "native_unit"} else None
        return self._decision(
            candidate,
            alert=eth_hit or unit_hit or usd_hit,
            kind=kind,
            valuation_unknown=unknown,
            identity=candidate.identity,
            movement=movement,
        )

    def _permission(self, candidate: Candidate) -> AlertDecision:
        change = candidate.permission_change or ""
        if not candidate.prior_known or change == "first_observed":
            kind = "first_observed"
        elif change in {"new", "operator_enabled", "new_delegate", "new_authority"}:
            kind = "permission_new"
        elif change in {"increased"}:
            kind = "permission_increased"
        elif change in {"extended", "expiry_extended"}:
            kind = "permission_extended"
        elif change in _INFORMATIONAL:
            kind = "revocation"
        else:
            kind = "permission_noted"
        always = change in _PERMISSION_ALERTS or change == "first_observed" or not candidate.prior_known
        informational = change in _INFORMATIONAL
        if kind == "first_observed":
            always = True
        return self._decision(
            candidate,
            alert=always or informational,
            kind=kind,
            valuation_unknown=False,
            identity=candidate.identity,
            informational=informational and not always,
        )

    def _internal(self, candidate: Candidate) -> AlertDecision:
        gross = _as_int(candidate.raw_amount)
        net = _as_int(candidate.net_amount)
        if net is None:
            net = 0
        probe = Candidate(
            tenant_id=candidate.tenant_id,
            wallet_id=candidate.wallet_id,
            network=candidate.network,
            capability=candidate.capability,
            identity=candidate.identity,
            kind="asset",
            raw_amount=net,
            is_native=candidate.is_native,
            quote_usd=candidate.quote_usd,
            quote_valuation=candidate.quote_valuation,
            quote_quality=candidate.quote_quality,
        )
        eth_hit = self._native_eth_hit(probe)
        unit_hit = self._native_unit_hit(probe)
        usd_hit, unknown = self._usd_hit(probe)
        if net == 0:
            eth_hit = False
            unit_hit = False
            usd_hit = False
        return self._decision(
            candidate,
            alert=eth_hit or unit_hit or usd_hit,
            kind="internal_transfer",
            valuation_unknown=unknown,
            identity=candidate.identity,
            gross=str(gross if gross is not None else 0),
            net=str(net),
        )

    def _summary(self, scope: tuple[str, str, str], grouped: Sequence[Candidate], tenant_fallback: str) -> AlertDecision:
        wallet_id, network, capability = scope
        tenant = grouped[0].tenant_id if grouped else tenant_fallback
        risks = sum(1 for item in grouped if item.explicit_risk or item.permission_change)
        history = sum(1 for item in grouped if item.kind in {"asset", "internal"})
        sample = grouped[0] if grouped else Candidate(tenant, wallet_id, network, capability, "baseline", "baseline")
        counts = (("events", history), ("explicit_risks", risks))
        return self._decision(
            sample,
            alert=True,
            kind="baseline_summary",
            valuation_unknown=False,
            identity=f"baseline:{capability}",
            counts=counts,
            wallet_id=wallet_id,
            network=network,
        )

    def _native_unit_hit(self, candidate: Candidate) -> bool:
        threshold = self.units.get(candidate.network)
        if threshold is None or not candidate.is_native:
            return False
        raw = _as_int(candidate.raw_amount)
        if raw is None:
            return False
        decimals = canonical_native_decimals().get(candidate.network, 18)
        return abs(raw) >= native_threshold_raw(threshold, decimals)

    def _native_eth_hit(self, candidate: Candidate) -> bool:
        if not candidate.is_native or candidate.network not in ETH_NETWORKS:
            return False
        raw = _as_int(candidate.raw_amount)
        if raw is None:
            return False
        return raw >= self.native_eth_wei

    def _usd_hit(self, candidate: Candidate) -> tuple[bool, bool]:
        valued = (
            candidate.quote_valuation == "valued"
            and candidate.quote_quality == "available"
            and isinstance(candidate.quote_usd, Decimal)
        )
        if not valued:
            return False, True
        return candidate.quote_usd >= self.usd, False

    def _decision(
        self,
        candidate: Candidate,
        *,
        alert: bool,
        kind: str,
        valuation_unknown: bool,
        identity: str,
        informational: bool = False,
        gross: str | None = None,
        net: str | None = None,
        counts: tuple[tuple[str, int], ...] = (),
        wallet_id: str | None = None,
        network: str | None = None,
        capability: str | None = None,
        movement: Mapping[str, Any] | None = None,
    ) -> AlertDecision:
        del capability
        if kind == "newly_added":
            kind = "first_observed"
        wallet = wallet_id or candidate.wallet_id
        net_id = network or candidate.network
        extra = movement if isinstance(movement, Mapping) else {}
        return AlertDecision(
            alert=alert,
            kind=kind,
            dedup_key=dedup_key(candidate.tenant_id, wallet, net_id, kind, identity),
            tenant_id=candidate.tenant_id,
            wallet_id=wallet,
            network=net_id,
            identity=identity,
            valuation_unknown=valuation_unknown,
            informational=informational,
            gross=gross,
            net=net,
            tx_id=candidate.tx_id,
            counts=counts,
            asset=_text_or_none(extra.get("asset")),
            attribution=_text_or_none(extra.get("attribution")),
            block_number=_text_or_none(extra.get("block_number")),
            direction=_text_or_none(extra.get("direction")),
            execution_evidence=_text_or_none(extra.get("execution_evidence")),
            raw_amount=_text_or_none(extra.get("raw_amount")),
        )


def native_threshold_raw(amount: Decimal, decimals: int) -> int:
    """Smallest-unit integer for a native-unit threshold. Truncates toward zero."""
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise ValueError("decimals")
    return int((amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def canonical_native_decimals() -> dict[str, int]:
    """Decimals from the canonical network registry on this branch."""
    found: dict[str, int] = {}
    for network, entry in _canonical_registry().items():
        native = entry.get("native_asset") if isinstance(entry.get("native_asset"), Mapping) else {}
        places = native.get("decimals") if isinstance(native, Mapping) else None
        if isinstance(places, bool) or not isinstance(places, int) or places < 0:
            places = 18
        found[network] = places
    return found


def validate_native_unit_thresholds(value: Any) -> dict[str, Decimal]:
    """Map canonical network ids to native-unit floors. Unknown keys are refused."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError("native_unit_thresholds must be an object")
    known = canonical_native_decimals()
    unknown = [str(key) for key in value if not isinstance(key, str) or key not in known]
    if unknown:
        raise ConfigError(
            "unknown native_unit_thresholds key(s): "
            + ", ".join(sorted(unknown))
            + "; valid networks: "
            + ", ".join(sorted(known))
        )
    found: dict[str, Decimal] = {}
    for network, raw in value.items():
        found[str(network)] = _decimal(raw, "native_unit_thresholds")
    return found


def _native_movement(candidate: Candidate) -> dict[str, str] | None:
    """Fields a later balance delta needs in order to recognise this transfer."""
    if not candidate.is_native or candidate.transfer_origin not in {"evm_external", "evm_internal"}:
        return None
    found: dict[str, str] = {}
    if isinstance(candidate.asset, str) and candidate.asset:
        found["asset"] = candidate.asset
    if isinstance(candidate.attribution, str) and candidate.attribution:
        found["attribution"] = candidate.attribution
    if isinstance(candidate.block_number, int) and not isinstance(candidate.block_number, bool):
        found["block_number"] = str(candidate.block_number)
    if isinstance(candidate.direction, str) and candidate.direction:
        found["direction"] = candidate.direction
    if isinstance(candidate.execution_evidence, str) and candidate.execution_evidence:
        found["execution_evidence"] = candidate.execution_evidence
    raw = _as_int(candidate.raw_amount)
    if raw is not None:
        found["raw_amount"] = str(raw)
    return found or None


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def dedup_key(tenant_id: str, wallet_id: str, network: str, kind: str, identity: str) -> str:
    material = "\x1f".join((tenant_id, wallet_id, network, kind, identity))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _pair_internal(candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    groups: dict[tuple[str, str, str], list[Candidate]] = {}
    rest: list[Candidate] = []
    for candidate in candidates:
        if candidate.kind == "asset" and candidate.direction in {"in", "out"} and candidate.counterparty_wallet_id:
            groups.setdefault((candidate.tenant_id, candidate.network, candidate.identity), []).append(candidate)
        else:
            rest.append(candidate)
    internal: list[Candidate] = []
    for rows in groups.values():
        incoming = [row for row in rows if row.direction == "in"]
        outgoing = [row for row in rows if row.direction == "out"]
        if (
            len(incoming) == 1
            and len(outgoing) == 1
            and incoming[0].wallet_id == outgoing[0].counterparty_wallet_id
            and outgoing[0].wallet_id == incoming[0].counterparty_wallet_id
        ):
            left, right = outgoing[0], incoming[0]
            gross = (_as_int(left.raw_amount) or 0) + (_as_int(right.raw_amount) or 0)
            net = abs((_as_int(left.raw_amount) or 0) - (_as_int(right.raw_amount) or 0))
            wallet_id = min(left.wallet_id, right.wallet_id)
            internal.append(Candidate(
                tenant_id=left.tenant_id,
                wallet_id=wallet_id,
                network=left.network,
                capability=left.capability,
                identity=left.identity,
                kind="internal",
                raw_amount=gross,
                is_native=left.is_native and right.is_native,
                quote_usd=left.quote_usd if left.quote_usd is not None else right.quote_usd,
                quote_valuation=left.quote_valuation or right.quote_valuation,
                quote_quality=left.quote_quality or right.quote_quality,
                tx_id=left.tx_id or right.tx_id,
                net_amount=net,
            ))
        else:
            rest.extend(rows)
    return rest, internal


_REGISTRY: dict[str, dict[str, Any]] | None = None


def _canonical_registry() -> dict[str, dict[str, Any]]:
    global _REGISTRY
    if _REGISTRY is None:
        from guardian.network_registry import load_registry
        from guardian.resources import resource_path

        _REGISTRY = load_registry(resource_path("config", "chains"))
    return _REGISTRY


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError(label) from None
    else:
        raise ValueError(label)
    if not number.is_finite() or number < 0:
        raise ValueError(label)
    return number


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None

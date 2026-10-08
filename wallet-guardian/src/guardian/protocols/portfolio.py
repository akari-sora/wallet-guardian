"""Portfolio sum. A receipt and its underlying quote count once. Debt is subtracted once.

A W4 fungible balance of the receipt token is the same asset as the receipt leg.
Missing prices stay unknown and are not treated as zero. Unknown plugin results
are omitted rather than entered as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from guardian.pricing.quote import PriceQuote, value_usd
from guardian.protocols.models import PositionLeg, PositionObservation


@dataclass(frozen=True)
class FungibleBalance:
    """A W4 fungible balance already counted outside the protocol plugin."""

    network: str
    identity: str
    raw_amount: int
    decimals: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int) or self.raw_amount < 0:
            raise ValueError("fungible_amount")


@dataclass(frozen=True)
class CountedLine:
    network: str
    identity: str
    kind: str
    raw_amount: int
    decimals: int | None
    counted: bool
    reason: str
    plugin_id: str | None
    priced: Decimal | None
    valuation: str


@dataclass(frozen=True)
class Portfolio:
    lines: tuple[CountedLine, ...]
    unknown_plugins: tuple[str, ...]
    valuation: str
    value_usd: Decimal | None
    reason: str

    def amount(self, network: str, identity: str) -> int:
        total = 0
        for line in self.lines:
            if line.counted and line.network == network and _same(line.identity, identity):
                total += line.raw_amount
        return total

    @property
    def counted(self) -> tuple[CountedLine, ...]:
        return tuple(line for line in self.lines if line.counted)


def aggregate_portfolio(
    observations: Sequence[PositionObservation],
    fungibles: Sequence[FungibleBalance] = (),
    quotes: Mapping[tuple[str, str], PriceQuote] | None = None,
) -> Portfolio:
    unknown: list[str] = []
    legs: list[PositionLeg] = []
    for observation in observations:
        if observation.status == "unknown":
            unknown.append(observation.plugin_id)
            continue
        if observation.status != "observed":
            continue
        legs.extend(observation.legs)
    receipt_ids = {_norm(leg.identity) for leg in legs if leg.kind == "receipt"}
    debt_ids = {_norm(leg.identity) for leg in legs if leg.kind == "debt"}
    lines: list[CountedLine] = []
    for leg in legs:
        counted, reason, signed = _leg_count(leg, receipt_ids)
        priced, valuation = _price(leg.network, leg.identity, signed, leg.decimals, quotes, counted)
        lines.append(CountedLine(
            network=leg.network,
            identity=leg.identity,
            kind=leg.kind,
            raw_amount=signed,
            decimals=leg.decimals,
            counted=counted,
            reason=reason,
            plugin_id=leg.plugin_id,
            priced=priced,
            valuation=valuation,
        ))
    for fungible in fungibles:
        counted, reason = _fungible_count(fungible, receipt_ids, debt_ids)
        priced, valuation = _price(
            fungible.network, fungible.identity, fungible.raw_amount, fungible.decimals, quotes, counted,
        )
        lines.append(CountedLine(
            network=fungible.network,
            identity=fungible.identity,
            kind="fungible",
            raw_amount=fungible.raw_amount,
            decimals=fungible.decimals,
            counted=counted,
            reason=reason,
            plugin_id=None,
            priced=priced,
            valuation=valuation,
        ))
    total, total_valuation, total_reason = _total(lines, unknown)
    return Portfolio(
        lines=tuple(lines),
        unknown_plugins=tuple(unknown),
        valuation=total_valuation,
        value_usd=total,
        reason=total_reason,
    )


def _leg_count(leg: PositionLeg, receipt_ids: set[str]) -> tuple[bool, str, int]:
    signed = -leg.raw_amount if leg.kind == "debt" else leg.raw_amount
    if leg.kind == "underlying" and leg.duplicates is not None and _norm(leg.duplicates) in receipt_ids:
        return False, "underlying_of_counted_receipt", signed
    if signed == 0:
        return False, "zero_amount", signed
    return True, "", signed


def _fungible_count(fungible: FungibleBalance, receipt_ids: set[str], debt_ids: set[str]) -> tuple[bool, str]:
    key = _norm(fungible.identity)
    if key in receipt_ids:
        return False, "w4_same_as_receipt"
    if key in debt_ids:
        return False, "w4_same_as_debt"
    if fungible.raw_amount == 0:
        return False, "zero_amount"
    return True, ""


def _price(
    network: str,
    identity: str,
    signed: int,
    decimals: int | None,
    quotes: Mapping[tuple[str, str], PriceQuote] | None,
    counted: bool,
) -> tuple[Decimal | None, str]:
    if not counted or quotes is None or decimals is None:
        return None, "valuation_unknown"
    quote = quotes.get((network, identity))
    if quote is None or quote.price_usd is None or quote.price_quality != "available" or quote.display_only:
        return None, "valuation_unknown"
    if quote.valuation != "valued":
        return None, "valuation_unknown"
    amount = value_usd(abs(signed), decimals, quote.price_usd)
    if signed < 0:
        amount = -amount
    return amount, "valued"


def _total(lines: Sequence[CountedLine], unknown: Sequence[str]) -> tuple[Decimal | None, str, str]:
    if unknown:
        return None, "valuation_unknown", "position_unknown"
    counted = [line for line in lines if line.counted]
    if any(line.valuation != "valued" or line.priced is None for line in counted):
        return None, "valuation_unknown", "valuation_unknown"
    if not counted:
        return None, "valuation_unknown", "nothing_priced"
    total = sum((line.priced for line in counted if line.priced is not None), Decimal(0))
    return total, "valued", ""


def _norm(identity: str) -> str:
    if identity.startswith(("0x", "0X")):
        return identity.lower()
    return identity


def _same(left: str, right: str) -> bool:
    return _norm(left) == _norm(right)

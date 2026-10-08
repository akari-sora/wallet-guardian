"""Decimal valuations. A missing or stale price is never 0 and never 1 USD."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from guardian.amounts import parse_raw

USD_THRESHOLD = Decimal("1000")
_QUALITIES = frozenset({"available", "stale", "unknown"})
_VALUATIONS = frozenset({"valued", "valuation_unknown"})
_DECISIONS = frozenset({"above", "below", "valuation_unknown"})


@dataclass(frozen=True)
class PriceProvenance:
    """Where a number came from. The path has no query string and no credential."""

    source: str
    fetched_at: int | None
    last_updated_at: int | None
    path: str


@dataclass(frozen=True)
class PriceQuote:
    """One identity on one network. ``symbol`` is not part of the lookup key."""

    network_id: str
    identity: str
    symbol: str
    price_usd: Decimal | None
    price_quality: str
    valuation: str
    reason: str
    value_usd: Decimal | None
    threshold_decision: str
    age_seconds: int | None
    display_only: bool
    provenance: PriceProvenance

    def __post_init__(self) -> None:
        if isinstance(self.price_usd, float) or isinstance(self.value_usd, float):
            raise ValueError("prices must not be binary floats")
        if self.price_quality not in _QUALITIES:
            raise ValueError("price_quality")
        if self.valuation not in _VALUATIONS:
            raise ValueError("valuation")
        if self.threshold_decision not in _DECISIONS:
            raise ValueError("threshold")
        if self.price_usd is None and self.price_quality != "unknown":
            raise ValueError("missing_price_quality")
        if self.price_quality == "stale" and self.price_usd is None:
            raise ValueError("stale_requires_price")
        if self.display_only and self.threshold_decision != "valuation_unknown":
            raise ValueError("display_only_cannot_decide")
        if self.price_quality != "available" and self.threshold_decision != "valuation_unknown":
            raise ValueError("threshold_requires_fresh_price")
        if self.valuation == "valued":
            if self.threshold_decision not in {"above", "below"}:
                raise ValueError("valued_decision")
            if not isinstance(self.value_usd, Decimal):
                raise ValueError("valued_amount")
            if self.display_only or self.price_quality != "available":
                raise ValueError("valued_requires_fresh_price")


def provider_decimal(value: Any) -> Decimal:
    """Convert one provider JSON number. Floats cross this boundary once."""
    if isinstance(value, bool) or value is None:
        raise ValueError("malformed_number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("malformed_number")
        number = Decimal(str(value))
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("malformed_number")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("malformed_number") from exc
    else:
        raise ValueError("malformed_number")
    if not number.is_finite():
        raise ValueError("malformed_number")
    return number


def provider_timestamp(value: Any) -> int | None:
    """Unix seconds from a provider field. Missing stays missing."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("malformed_timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("malformed_timestamp")
        return int(value)
    raise ValueError("malformed_timestamp")


def value_usd(raw: str | int, decimals: int, price: Decimal) -> Decimal:
    """``raw * price / 10**decimals`` in Decimal. Floats are rejected."""
    if isinstance(price, float) or isinstance(raw, float) or isinstance(decimals, bool):
        raise ValueError("prices must not be binary floats")
    if not isinstance(price, Decimal):
        raise ValueError("price")
    if not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError("decimals")
    if not price.is_finite():
        raise ValueError("price")
    amount = Decimal(parse_raw(raw))
    return amount * price / (Decimal(10) ** decimals)


def crosses_usd_threshold(value: Decimal | None, *, threshold: Decimal = USD_THRESHOLD) -> str:
    """``above`` when value is at least the threshold. 999.99 is below 1000."""
    if isinstance(value, float) or isinstance(value, bool) or not isinstance(value, Decimal):
        return "valuation_unknown"
    if isinstance(threshold, float) or not isinstance(threshold, Decimal):
        return "valuation_unknown"
    if not value.is_finite() or not threshold.is_finite():
        return "valuation_unknown"
    if value >= threshold:
        return "above"
    return "below"


def price_age(now: int, updated: int | None, *, max_age_seconds: int) -> tuple[str, int | None, str]:
    """Return quality, age, and a reason. Missing or future timestamps are unknown."""
    if isinstance(now, bool) or not isinstance(now, int):
        return "unknown", None, "malformed_timestamp"
    if updated is None:
        return "unknown", None, "timestamp_missing"
    if updated < 0 or updated > now:
        return "unknown", None, "timestamp_invalid"
    age = now - updated
    if age > max_age_seconds:
        return "stale", age, "stale"
    return "available", age, ""

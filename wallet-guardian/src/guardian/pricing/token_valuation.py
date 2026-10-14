"""Quote token transfers for one round, then mark large moves that stay unpriced.

CoinGecko's free ``contract_addresses`` read is used only when a CoinGecko
source is already enabled. A missing, stale, or failed quote is not stored as
0. A pinned stablecoin, or an operator raw threshold, can still alert as
``unvalued_large_move`` without claiming a USD price.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from guardian.balances.native import QUOTE_MAX_AGE_SECONDS, _price_source
from guardian.pricing.catalog import load_risk_sources
from guardian.pricing.coingecko import CoinGeckoClient
from guardian.pricing.quote import value_usd
from guardian.pricing.stables import stable_decimals
from guardian.rules import Candidate, validate_token_raw_thresholds
from guardian.sources.budget import BudgetExhausted

_BATCH = 50


def apply_token_valuation(candidates: list[Any], public_config: Any, transport: Any, clock: Any) -> None:
    """Stamp fresh contract quotes and flag unvalued large token moves."""
    alerts = _alerts(public_config)
    usd = _usd(alerts.get("asset_usd_threshold"))
    raw_thresholds = validate_token_raw_thresholds(alerts.get("token_raw_thresholds"))
    quotes = _fetch(candidates, public_config, transport, clock)
    stamped: list[Any] = []
    for item in candidates:
        stamped.append(_one(item, quotes, usd, raw_thresholds))
    candidates[:] = stamped


def _one(
    item: Any,
    quotes: Mapping[tuple[str, str], Mapping[str, Any]],
    usd: Decimal,
    raw_thresholds: Mapping[tuple[str, str], int],
) -> Any:
    if not isinstance(item, Candidate) or item.is_native or item.kind != "asset":
        return item
    asset = item.asset if isinstance(item.asset, str) else ""
    contract = asset.lower()
    raw = item.raw_amount if isinstance(item.raw_amount, int) and not isinstance(item.raw_amount, bool) else None
    if not contract.startswith("0x") or raw is None:
        return item
    quote = quotes.get((item.network, contract))
    decimals = item.decimals if isinstance(item.decimals, int) and not isinstance(item.decimals, bool) else None
    if decimals is None:
        decimals = stable_decimals(item.network, contract)
    price = quote.get("price") if isinstance(quote, Mapping) else None
    fresh = (
        isinstance(quote, Mapping)
        and quote.get("quality") == "available"
        and isinstance(price, Decimal)
        and price > 0
    )
    if fresh and decimals is not None:
        try:
            amount = value_usd(raw, decimals, price)
        except ValueError:
            amount = None
        if isinstance(amount, Decimal):
            return _replace(
                item,
                quote_usd=amount,
                quote_valuation="valued",
                quote_quality="available",
                unvalued_large=False,
                decimals=decimals,
                valuation_reason=None,
            )
    if _stable_large(item.network, contract, raw, usd) or _raw_hit(item.network, contract, raw, raw_thresholds):
        return _replace(item, unvalued_large=True, decimals=decimals if decimals is not None else item.decimals)
    if fresh and decimals is None:
        reason = item.valuation_reason if isinstance(item.valuation_reason, str) and item.valuation_reason else "decimals_unknown"
        return _replace(
            item,
            unvalued_large=True,
            valuation_reason=reason,
            quote_usd=None,
            quote_valuation=None,
            quote_quality=None,
        )
    return item


def _stable_large(network: str, contract: str, raw: int, usd: Decimal) -> bool:
    decimals = stable_decimals(network, contract)
    if decimals is None:
        return False
    floor = int((usd * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))
    return abs(raw) >= floor


def _raw_hit(network: str, contract: str, raw: int, thresholds: Mapping[tuple[str, str], int]) -> bool:
    floor = thresholds.get((network, contract))
    return isinstance(floor, int) and abs(raw) >= floor


def _fetch(candidates: list[Any], public_config: Any, transport: Any, clock: Any) -> dict[tuple[str, str], dict[str, Any]]:
    source_id = _price_source(public_config, transport)
    if source_id is None:
        return {}
    grouped: dict[str, list[str]] = {}
    for item in candidates:
        if not isinstance(item, Candidate) or item.is_native or item.kind != "asset":
            continue
        asset = item.asset if isinstance(item.asset, str) else ""
        if not asset.startswith("0x"):
            continue
        bucket = grouped.setdefault(item.network, [])
        contract = asset.lower()
        if contract not in bucket:
            bucket.append(contract)
    if not grouped:
        return {}
    from guardian.balances.native import _quote_environ, _quote_sleep

    catalog = load_risk_sources()
    client = CoinGeckoClient(
        transport,
        source_id,
        catalog,
        _quote_environ(public_config),
        clock=clock,
        sleep=_quote_sleep(transport, None),
    )
    now = _now(clock)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for network, contracts in grouped.items():
        for start in range(0, len(contracts), _BATCH):
            chunk = contracts[start:start + _BATCH]
            try:
                parsed = client.token_prices(
                    network_id=network,
                    contracts=chunk,
                    max_age_seconds=QUOTE_MAX_AGE_SECONDS,
                    now=now,
                )
            except BudgetExhausted:
                parsed = {contract: {"quality": "unavailable", "reason": "budget"} for contract in chunk}
            except Exception as exc:
                if getattr(exc, "reason", None) == "fixture_miss":
                    raise
                parsed = {contract: {"quality": "unavailable", "reason": "read_error"} for contract in chunk}
            for contract, row in parsed.items():
                if isinstance(row, dict):
                    found[(network, contract.lower())] = row
    return found


def _alerts(public_config: Any) -> Mapping[str, Any]:
    document = getattr(public_config, "document", None)
    alerts = document.get("alerts") if isinstance(document, Mapping) else None
    return alerts if isinstance(alerts, Mapping) else {}


def _usd(value: Any) -> Decimal:
    if isinstance(value, str):
        try:
            number = Decimal(value)
        except Exception:
            return Decimal("1000")
        if number.is_finite() and number > 0:
            return number
    return Decimal("1000")


def _now(clock: Any) -> int:
    try:
        value = clock()
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _replace(item: Candidate, **changes: Any) -> Candidate:
    from dataclasses import replace

    return replace(item, **changes)

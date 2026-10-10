"""CoinGecko simple-price reads. The lookup key is never a symbol or a name.

A missing key is not an error. The optional demo key travels only as the query
parameter named in the catalog. Stablecoins are not assigned 1 USD.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from guardian.errors import SecurityError
from guardian.identities import IdentityError, parse_address
from guardian.pricing.catalog import PricedNetwork, RiskSources
from guardian.pricing.quote import (
    PriceProvenance,
    PriceQuote,
    crosses_usd_threshold,
    price_age,
    provider_decimal,
    provider_timestamp,
    value_usd,
)
from guardian.security.redact import redact
from guardian.security.transport_policy import Operation
from guardian.sources.budget import BudgetExhausted
from guardian.sources.etherscan import resolve_credential
from guardian.sources.retry import read_with_retry
from guardian.sources.transport import TransportError

_SOURCE = "coingecko"


class CoinGeckoClient:
    """GET /api/v3/simple/price and /api/v3/simple/token_price/{platform}."""

    def __init__(
        self,
        transport: Any,
        source_id: str,
        catalog: RiskSources,
        environ: Mapping[str, str],
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        self.transport = transport
        self.source_id = source_id
        self.catalog = catalog
        self.credential_env = catalog.coingecko_credential_env
        self.query_param = catalog.coingecko_query_param
        try:
            self._credential: str | None = resolve_credential(environ, self.credential_env)
        except ValueError as exc:
            if str(exc) != "credential_missing":
                raise
            self._credential = None
        self.clock = clock or (lambda: 0.0)
        self.sleep = sleep or (lambda _seconds: None)
        self.random = random or (lambda: 0.0)

    def __repr__(self) -> str:
        mode = "keyless" if self._credential is None else "query"
        return (
            f"CoinGeckoClient(source_id={self.source_id!r}, "
            f"credential_env={self.credential_env!r}, credential={mode})"
        )

    def redact_error(self, text: str) -> str:
        secrets = [self._credential] if self._credential else None
        return redact(text, secrets)

    def price_operation(self, *, network_id: str, identity: str) -> Operation | None:
        """Build the read for this identity. None means no request should be sent."""
        network = self.catalog.priced_network(network_id)
        if network is None:
            return None
        normalized = _normalize(network, identity)
        if normalized is None:
            return None
        kind, key = normalized
        if kind == "native":
            path = "/api/v3/simple/price"
            identifying = {"ids": network.native_coin_id}
        else:
            path = f"/api/v3/simple/token_price/{network.platform_id}"
            identifying = {"contract_addresses": key}
        return Operation(
            protocol="rest",
            adapter="coingecko",
            http_method="GET",
            path=path,
            query=self._query(identifying),
        )

    def quote(
        self,
        *,
        network_id: str,
        identity: str,
        symbol: str = "",
        name: str = "",
        decimals: int | None = None,
        raw: int | str | None = None,
        low_liquidity: bool = False,
        unverified: bool = False,
    ) -> PriceQuote:
        """Price ``identity`` on ``network_id``. ``symbol`` and ``name`` are ignored."""
        del name  # display text must not select a coin id
        display_symbol = symbol if isinstance(symbol, str) else ""
        network = self.catalog.priced_network(network_id)
        if network is None:
            return _unknown(network_id, identity, display_symbol, "unsupported_network", "")
        normalized = _normalize(network, identity)
        if normalized is None:
            return _unknown(network_id, identity, display_symbol, "bad_identity", "")
        kind, _lookup = normalized
        stored = network.native_identity if kind == "native" else _lookup
        operation = self.price_operation(network_id=network_id, identity=stored)
        if operation is None or operation.path is None:
            return _unknown(network_id, stored, display_symbol, "bad_identity", "")
        now = _now(self.clock)
        try:
            result = read_with_retry(
                lambda: self.transport.request(self.source_id, operation),
                retry_attempts=self.transport.retry_attempts(self.source_id),
                sleep=self.sleep,
                random=self.random,
                clock=self.clock,
            )
        except BudgetExhausted as exc:
            return _unknown(network_id, stored, display_symbol, exc.reason or "budget", operation.path, now)
        except SecurityError as exc:
            return _unknown(network_id, stored, display_symbol, exc.reason or "blocked", operation.path, now)
        except TransportError as exc:
            if exc.reason == "fixture_miss":
                raise
            return _unknown(network_id, stored, display_symbol, exc.reason or "read_error", operation.path, now)
        detail = self._detail(operation)
        if getattr(result, "blocked", False) or getattr(result, "status", 0) in {401, 403}:
            return _unknown(network_id, stored, display_symbol, str(result.reason or "blocked"), operation.path, now, detail)
        if result.category == "rate_limited" or getattr(result, "status", 0) == 429:
            return _unknown(network_id, stored, display_symbol, "rate_limited", operation.path, now, detail)
        if result.category != "ok" or not isinstance(result.body, dict):
            return _unknown(network_id, stored, display_symbol, "malformed", operation.path, now, detail)
        return self._parse(
            network=network,
            identity=stored,
            symbol=display_symbol,
            kind=kind,
            body=result.body,
            path=operation.path,
            now=now,
            decimals=decimals,
            raw=raw,
            low_liquidity=low_liquidity,
            unverified=unverified,
        )

    def simple_prices(
        self,
        coin_ids: list[str],
        *,
        max_age_seconds: int,
        now: int,
    ) -> dict[str, dict[str, Any]]:
        """One simple/price call. A missing or stale coin is not priced at zero."""
        ids = [coin_id for coin_id in coin_ids if isinstance(coin_id, str) and coin_id]
        if not ids:
            return {}
        operation = Operation(
            protocol="rest",
            adapter="coingecko",
            http_method="GET",
            path="/api/v3/simple/price",
            query=self._query({"ids": ",".join(ids)}),
        )
        parsed: dict[str, dict[str, Any]] = {coin_id: {"quality": "unavailable", "reason": "not_read"} for coin_id in ids}
        try:
            result = read_with_retry(
                lambda: self.transport.request(self.source_id, operation),
                retry_attempts=self.transport.retry_attempts(self.source_id),
                sleep=self.sleep,
                random=self.random,
                clock=self.clock,
            )
        except BudgetExhausted as exc:
            return {coin_id: {"quality": "unavailable", "reason": exc.reason or "budget"} for coin_id in ids}
        except (SecurityError, TransportError) as exc:
            if getattr(exc, "reason", None) == "fixture_miss":
                raise
            return {coin_id: {"quality": "unavailable", "reason": getattr(exc, "reason", None) or "read_error"} for coin_id in ids}
        if getattr(result, "category", "") == "rate_limited" or getattr(result, "status", 0) == 429:
            return {coin_id: {"quality": "unavailable", "reason": "rate_limited"} for coin_id in ids}
        body = getattr(result, "body", None)
        if not isinstance(body, dict):
            return {coin_id: {"quality": "unavailable", "reason": "malformed"} for coin_id in ids}
        for coin_id in ids:
            entry = body.get(coin_id)
            if not isinstance(entry, dict) or entry.get("usd") is None:
                parsed[coin_id] = {"quality": "unavailable", "reason": "absent"}
                continue
            try:
                price = provider_decimal(entry.get("usd"))
                updated = provider_timestamp(entry.get("last_updated_at")) if "last_updated_at" in entry else None
            except ValueError:
                parsed[coin_id] = {"quality": "unavailable", "reason": "malformed"}
                continue
            if price < 0:
                parsed[coin_id] = {"quality": "unavailable", "reason": "negative_price"}
                continue
            quality, age, reason = price_age(now, updated, max_age_seconds=max_age_seconds)
            if quality != "available":
                parsed[coin_id] = {"quality": quality, "reason": reason or quality, "age_seconds": age}
                continue
            parsed[coin_id] = {
                "age_seconds": age,
                "price": price,
                "quality": "available",
                "reason": "",
            }
        return parsed

    def _parse(
        self,
        *,
        network: PricedNetwork,
        identity: str,
        symbol: str,
        kind: str,
        body: Mapping[str, Any],
        path: str,
        now: int,
        decimals: int | None,
        raw: int | str | None,
        low_liquidity: bool,
        unverified: bool,
    ) -> PriceQuote:
        entry = _entry(network, identity, kind, body)
        if not isinstance(entry, dict):
            return _unknown(network.network_id, identity, symbol, "absent", path, now)
        if "usd" not in entry or entry.get("usd") is None:
            return _unknown(network.network_id, identity, symbol, "null_price", path, now)
        try:
            price = provider_decimal(entry.get("usd"))
            updated = provider_timestamp(entry.get("last_updated_at")) if "last_updated_at" in entry else None
        except ValueError:
            return _unknown(network.network_id, identity, symbol, "malformed", path, now)
        if price < 0:
            return _unknown(network.network_id, identity, symbol, "negative_price", path, now)
        quality, age, age_reason = price_age(now, updated, max_age_seconds=self.catalog.max_price_age_seconds)
        display_only = _display_only(entry, low_liquidity=low_liquidity, unverified=unverified)
        amount: Decimal | None = None
        if raw is not None and decimals is not None:
            try:
                amount = value_usd(raw, decimals, price)
            except ValueError:
                return _unknown(network.network_id, identity, symbol, "malformed_amount", path, now)
        decision = _decision(
            price=price,
            quality=quality,
            display_only=display_only,
            amount=amount,
            threshold=self.catalog.usd_threshold,
        )
        reason = age_reason
        if display_only and quality == "available":
            reason = "unverified_or_low_liquidity"
        valuation = "valued" if decision in {"above", "below"} else "valuation_unknown"
        return PriceQuote(
            network_id=network.network_id,
            identity=identity,
            symbol=symbol,
            price_usd=price,
            price_quality=quality,
            valuation=valuation,
            reason=reason,
            value_usd=amount,
            threshold_decision=decision,
            age_seconds=age,
            display_only=display_only,
            provenance=PriceProvenance(_SOURCE, now, updated, path),
        )

    def _query(self, identifying: dict[str, str]) -> dict[str, str]:
        query = {
            "include_last_updated_at": "true",
            "vs_currencies": "usd",
            **identifying,
        }
        if self._credential is not None:
            query[self.query_param] = self._credential
        if "symbols" in query or "names" in query:
            raise ValueError("symbol_lookup_forbidden")
        return query

    def _detail(self, operation: Operation) -> str:
        try:
            url = self.transport.encode(self.source_id, operation).url
        except Exception:
            url = operation.path or ""
        return self.redact_error(url)


def _normalize(network: PricedNetwork, identity: str) -> tuple[str, str] | None:
    if not isinstance(identity, str) or not identity:
        return None
    if identity == network.native_identity:
        return "native", network.native_coin_id
    try:
        parsed = parse_address(network.family, identity)
    except IdentityError:
        return None
    if network.family == "evm":
        return "token", parsed.identity_key
    return "token", parsed.canonical


def _entry(network: PricedNetwork, identity: str, kind: str, body: Mapping[str, Any]) -> Any:
    if kind == "native":
        # The coin id is the only accepted key. A symbol-shaped key is not a fallback.
        if network.native_coin_id in body:
            return body.get(network.native_coin_id)
        return None
    for key, value in body.items():
        if not isinstance(key, str):
            continue
        if network.family == "evm" and key.lower() == identity.lower():
            return value
        if network.family != "evm" and key == identity:
            return value
    return None


def _display_only(entry: Mapping[str, Any], *, low_liquidity: bool, unverified: bool) -> bool:
    if low_liquidity or unverified:
        return True
    for field in ("low_liquidity", "unverified"):
        flag = entry.get(field)
        if flag is True or flag == "1":
            return True
    return False


def _decision(
    *,
    price: Decimal | None,
    quality: str,
    display_only: bool,
    amount: Decimal | None,
    threshold: Decimal,
) -> str:
    if price is None or quality != "available" or display_only or amount is None:
        return "valuation_unknown"
    return crosses_usd_threshold(amount, threshold=threshold)


def _unknown(
    network_id: str,
    identity: str,
    symbol: str,
    reason: str,
    path: str,
    fetched_at: int | None = None,
    detail: str = "",
) -> PriceQuote:
    # detail is already redacted by the caller. It is not a price.
    del detail
    return PriceQuote(
        network_id=network_id,
        identity=identity,
        symbol=symbol,
        price_usd=None,
        price_quality="unknown",
        valuation="valuation_unknown",
        reason=reason,
        value_usd=None,
        threshold_decision="valuation_unknown",
        age_seconds=None,
        display_only=False,
        provenance=PriceProvenance(_SOURCE, fetched_at, None, path),
    )


def _now(clock: Callable[[], float]) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)

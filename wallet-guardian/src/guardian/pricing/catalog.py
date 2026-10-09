"""Local risk-source catalog. Status stays documented; nothing here is a live result."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from guardian.errors import ConfigError
from guardian.resources import resource_path

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_CHAIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PLACEHOLDERS = frozenset({"address", "chain_id"})
T0_NETWORKS = (
    "ethereum-mainnet",
    "bsc-mainnet",
    "arbitrum-one",
    "base-mainnet",
    "optimism-mainnet",
    "polygon-pos",
    "avalanche-c",
    "solana-mainnet",
)


def default_risk_sources_path() -> Path:
    """Editable checkout catalog when this process loaded that checkout; otherwise the packaged copy."""
    from guardian.runtime_binding import source_checkout_root

    root = source_checkout_root()
    if root is not None:
        candidate = root / "config" / "risk-sources.json"
        if candidate.is_file():
            return candidate
    return resource_path("config", "risk-sources.json")


@dataclass(frozen=True)
class SourceNote:
    url: str
    retrieved: str
    status: str


@dataclass(frozen=True)
class PricedNetwork:
    network_id: str
    family: str
    native_identity: str
    native_decimals: int
    native_symbol: str
    platform_id: str
    native_coin_id: str
    source: SourceNote


@dataclass(frozen=True)
class GoPlusFunction:
    name: str
    result: str
    path: str
    query: tuple[tuple[str, str], ...]
    chains: tuple[tuple[str, str], ...]
    documented_chain_ids: tuple[str, ...]
    source: SourceNote

    def chain_id(self, network_id: str) -> str | None:
        for name, chain_id in self.chains:
            if name == network_id:
                return chain_id
        return None


@dataclass(frozen=True)
class TickerRule:
    ticker: str
    family: str
    networks: tuple[str, ...]


@dataclass(frozen=True)
class RiskSources:
    pricing_networks: tuple[PricedNetwork, ...]
    max_price_age_seconds: int
    usd_threshold: Decimal
    coingecko_credential_env: str
    coingecko_query_param: str
    coingecko_endpoint_env: str
    goplus_functions: tuple[GoPlusFunction, ...]
    goplus_credential_env: str
    goplus_endpoint_env: str
    ofac_tickers: tuple[TickerRule, ...]
    ofac_max_age_days: int
    chainabuse_credential_env: str
    chainabuse_endpoint_env: str

    def priced_network(self, network_id: str) -> PricedNetwork | None:
        for row in self.pricing_networks:
            if row.network_id == network_id:
                return row
        return None

    def goplus_function(self, name: str) -> GoPlusFunction | None:
        for row in self.goplus_functions:
            if row.name == name:
                return row
        return None

    def ticker(self, name: str) -> TickerRule | None:
        folded = name.strip().upper()
        for row in self.ofac_tickers:
            if row.ticker == folded:
                return row
        return None

    def family_for_network(self, network_id: str) -> str | None:
        families = {row.family for row in self.ofac_tickers if network_id in row.networks}
        if len(families) == 1:
            return families.pop()
        priced = self.priced_network(network_id)
        if priced is not None and not families:
            return priced.family
        return None


def load_risk_sources(path: Path | None = None) -> RiskSources:
    """Read the committed catalog. Endpoint values are not in this file."""
    file = Path(path) if path is not None else default_risk_sources_path()
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("risk sources are not readable") from exc
    if not isinstance(document, dict):
        raise ConfigError("risk sources are not readable")
    _reject_live_claim(document)
    pricing = _mapping(document.get("pricing"), "pricing")
    goplus = _mapping(document.get("goplus"), "goplus")
    ofac = _mapping(document.get("ofac"), "ofac")
    chainabuse = _mapping(document.get("chainabuse"), "chainabuse")
    networks = tuple(_network(name, row) for name, row in _mapping(pricing.get("networks"), "networks").items())
    found = {row.network_id for row in networks}
    if found != set(T0_NETWORKS):
        raise ConfigError("pricing networks must be the eight T0 networks")
    functions = tuple(
        _function(name, row) for name, row in _mapping(goplus.get("functions"), "functions").items()
    )
    tickers = tuple(_ticker(name, row) for name, row in _mapping(ofac.get("tickers"), "tickers").items())
    threshold = _decimal(pricing.get("usd_threshold"), "usd_threshold")
    max_age = _positive_int(pricing.get("max_age_seconds"), "max_age_seconds")
    ofac_age = _positive_int(ofac.get("max_age_days"), "max_age_days")
    return RiskSources(
        pricing_networks=networks,
        max_price_age_seconds=max_age,
        usd_threshold=threshold,
        coingecko_credential_env=_env(pricing.get("credential_env")),
        coingecko_query_param=_query_param(pricing.get("credential_query_param")),
        coingecko_endpoint_env=_env(pricing.get("endpoint_env")),
        goplus_functions=functions,
        goplus_credential_env=_env(goplus.get("credential_env")),
        goplus_endpoint_env=_env(goplus.get("endpoint_env")),
        ofac_tickers=tickers,
        ofac_max_age_days=ofac_age,
        chainabuse_credential_env=_env(chainabuse.get("credential_env")),
        chainabuse_endpoint_env=_env(chainabuse.get("endpoint_env")),
    )


def _network(name: str, row: Any) -> PricedNetwork:
    body = _mapping(row, name)
    family = body.get("family")
    if family not in {"evm", "solana"}:
        raise ConfigError("pricing family")
    decimals = body.get("native_decimals")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ConfigError("native_decimals")
    return PricedNetwork(
        network_id=_token(name, "network"),
        family=family,
        native_identity=_token(body.get("native_identity"), "native_identity"),
        native_decimals=decimals,
        native_symbol=_token(body.get("native_symbol"), "native_symbol"),
        platform_id=_token(body.get("platform_id"), "platform_id"),
        native_coin_id=_token(body.get("native_coin_id"), "native_coin_id"),
        source=_note(body.get("source")),
    )


def _function(name: str, row: Any) -> GoPlusFunction:
    body = _mapping(row, name)
    result = body.get("result")
    if result not in {"keyed", "flat", "keyed_or_flat"}:
        raise ConfigError("goplus result shape")
    path = body.get("path")
    if not isinstance(path, str) or not path.startswith("/") or any(token in path for token in ("..", "?", "#")):
        raise ConfigError("goplus path")
    _placeholders(path)
    query_body = body.get("query")
    if not isinstance(query_body, dict):
        raise ConfigError("goplus query")
    query: list[tuple[str, str]] = []
    for key, value in query_body.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("goplus query")
        _placeholders(value)
        query.append((key, value))
    chains_body = _mapping(body.get("chains"), "chains")
    chains: list[tuple[str, str]] = []
    for network_id, chain_id in chains_body.items():
        if not isinstance(chain_id, str) or _CHAIN_ID.fullmatch(chain_id) is None:
            raise ConfigError("goplus chain id")
        chains.append((_token(network_id, "network"), chain_id))
    documented = body.get("documented_chain_ids")
    if not isinstance(documented, list) or any(not isinstance(item, str) for item in documented):
        raise ConfigError("documented_chain_ids")
    return GoPlusFunction(
        name=_token(name, "function"),
        result=result,
        path=path,
        query=tuple(query),
        chains=tuple(chains),
        documented_chain_ids=tuple(documented),
        source=_note(body.get("source")),
    )


def _ticker(name: str, row: Any) -> TickerRule:
    body = _mapping(row, name)
    family = body.get("family")
    if not isinstance(family, str) or not family:
        raise ConfigError("ticker family")
    networks = body.get("networks")
    if not isinstance(networks, list) or not networks or any(not isinstance(item, str) or not item for item in networks):
        raise ConfigError("ticker networks")
    return TickerRule(ticker=name.strip().upper(), family=family, networks=tuple(networks))


def _note(value: Any) -> SourceNote:
    body = _mapping(value, "source")
    url = body.get("url")
    retrieved = body.get("retrieved")
    status = body.get("status")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ConfigError("source url")
    if not isinstance(retrieved, str) or len(retrieved) != 10:
        raise ConfigError("source retrieved")
    if not isinstance(status, str) or "documented" not in status or "live_pass" in status:
        raise ConfigError("source status")
    return SourceNote(url=url, retrieved=retrieved, status=status)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(label)
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ConfigError(label)
    return value


def _env(value: Any) -> str:
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        raise ConfigError("credential_env")
    return value


def _query_param(value: Any) -> str:
    if value != "x_cg_demo_api_key":
        raise ConfigError("credential_query_param")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, float) or not isinstance(value, str):
        raise ConfigError(label)
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise ConfigError(label)
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(label)
    return value


def _placeholders(text: str) -> None:
    for match in re.findall(r"\{([^{}]+)\}", text):
        if match not in _PLACEHOLDERS:
            raise ConfigError("template")


def _reject_live_claim(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "enabled" and item is True:
                raise ConfigError("risk sources are not live")
            if key in {"probe", "status"} and item == "live_pass":
                raise ConfigError("risk sources are not live")
            if isinstance(item, str) and "live_pass" in item:
                raise ConfigError("risk sources are not live")
            _reject_live_claim(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_live_claim(item)

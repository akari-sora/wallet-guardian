"""Yield records, the synthetic pool mapping, and the noise-policy knobs.

This package does not fetch, sign, or send. ``ENABLED`` stays false. The
mapping file is a versioned fixture: pool ids are UUIDs, and contract addresses
are ``0x000…000N`` stand-ins, not monitored wallets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from guardian.chains.evm.accounting import normalize_address
from guardian.jsonschema_lite import validate
from guardian.resources import read_resource_json, resource_path

ENABLED = False
SCHEMA_VERSION = "yield-state.v1"
RECORD_VERSION = "yield-record.v1"
MAPPING_SCHEMA_VERSION = "yield-pool-mapping.v1"
LABEL_BASE = "reported base yield"
LABEL_INCENTIVE = "reported incentive/reward yield"
LABEL_TOTAL = "reported total yield"
POLICY_NOTE = "configurable product policy, not a risk guarantee"
STATE_WALLET = "yield-promotions"
STATE_NETWORK = "yields"
STATE_CAPABILITY = "yield_promotions"

_POOL_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SYNTHETIC = re.compile(r"^0x0{39}[0-9a-f]$")
_PROTOCOL = re.compile(r"^synthetic-[a-z0-9-]{1,40}$")
_CAMPAIGN = re.compile(r"^[a-z0-9-]{1,64}$")
_RECORD = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

BLOCKED_SOURCES = (
    {
        "source": "binance_hold_to_earn_html",
        "status": "blocked",
        "reason": "waf_challenge_not_bypassed",
        "fetched": False,
        "terms": "unknown",
    },
)


class MappingError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class NoisePolicy:
    """Product thresholds. They are not a risk guarantee and they do not convert units."""

    min_absolute_points: Decimal = Decimal("2")
    min_relative: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        points = self.min_absolute_points
        relative = self.min_relative
        if not isinstance(points, Decimal) or not points.is_finite() or points <= 0:
            raise ValueError("min_absolute_points")
        if not isinstance(relative, Decimal) or not relative.is_finite() or relative < 0:
            raise ValueError("min_relative")


@dataclass(frozen=True)
class MappedContract:
    role: str
    address: str


@dataclass(frozen=True)
class MappedPool:
    pool_id: str
    chain: str
    protocol: str
    contracts: tuple[MappedContract, ...]


@dataclass(frozen=True)
class MappedCampaign:
    campaign_id: str
    pool_id: str
    source: str
    product: str


@dataclass(frozen=True)
class PoolMapping:
    mapping_version: str
    pools: tuple[MappedPool, ...]
    campaigns: tuple[MappedCampaign, ...]

    def pool(self, pool_id: str) -> MappedPool | None:
        for item in self.pools:
            if item.pool_id == pool_id:
                return item
        return None

    def campaign(self, campaign_id: str) -> MappedCampaign | None:
        for item in self.campaigns:
            if item.campaign_id == campaign_id:
                return item
        return None


@dataclass(frozen=True)
class HeldPosition:
    wallet_id: str
    chain: str
    protocol: str
    contracts: tuple[str, ...]
    raw_amount: int

    def __post_init__(self) -> None:
        if not isinstance(self.wallet_id, str) or not self.wallet_id:
            raise ValueError("wallet_id")
        if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int) or self.raw_amount < 0:
            raise ValueError("raw_amount")
        normalized: list[str] = []
        for addr in self.contracts:
            got = normalize_address(addr)
            if got is None:
                raise ValueError("contract")
            normalized.append(got)
        object.__setattr__(self, "contracts", tuple(normalized))


@dataclass(frozen=True)
class StandingApproval:
    wallet_id: str
    chain: str
    token: str
    spender: str
    active: bool

    def __post_init__(self) -> None:
        if not isinstance(self.wallet_id, str) or not self.wallet_id:
            raise ValueError("wallet_id")
        if not isinstance(self.active, bool):
            raise ValueError("active")
        token = normalize_address(self.token)
        spender = normalize_address(self.spender)
        if token is None or spender is None:
            raise ValueError("approval_address")
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "spender", spender)


@dataclass(frozen=True)
class Holdings:
    positions: tuple[HeldPosition, ...] = ()
    approvals: tuple[StandingApproval, ...] = ()


def promotions_enabled() -> bool:
    return ENABLED


def canonical(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def json_number(text: str | None) -> int | float | None:
    if text is None:
        return None
    number = Decimal(text)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def yield_decimal(value: Any) -> Decimal | None:
    """A publisher number, or None when the field is missing or not a finite number.

    Booleans and strings are not coerced. Null stays null. This is not a zero.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal):
        return None
    if not value.is_finite():
        return None
    return value


def record_id_ok(value: Any) -> bool:
    return isinstance(value, str) and _RECORD.fullmatch(value) is not None


def campaign_id_ok(value: Any) -> bool:
    return isinstance(value, str) and _CAMPAIGN.fullmatch(value) is not None


def synthetic_address(value: str) -> bool:
    return _SYNTHETIC.fullmatch(value) is not None


def policy_document(policy: NoisePolicy) -> dict[str, Any]:
    return {
        "min_absolute_points": canonical(policy.min_absolute_points),
        "min_relative": canonical(policy.min_relative),
        "risk_guarantee": False,
        "note": POLICY_NOTE,
    }


def load_pool_mapping(path: Path | None = None) -> PoolMapping:
    """Load the versioned synthetic mapping. A pool id that is an address is rejected."""
    if path is None:
        document = read_resource_json("config", "yields", "pool-mapping.v1.json")
    else:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = read_resource_json("schemas", "yield-pool-mapping.schema.json")
    validate(document, schema)
    if document.get("synthetic") is not True:
        raise MappingError("mapping_must_be_synthetic")
    if document.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise MappingError("mapping_schema")
    pools: list[MappedPool] = []
    seen: set[str] = set()
    for row in document["pools"]:
        pool_id = row["pool_id"]
        if pool_id in seen or _POOL_ID.fullmatch(pool_id) is None:
            raise MappingError("pool_id")
        if normalize_address(pool_id) is not None:
            raise MappingError("pool_id_must_not_be_an_address")
        seen.add(pool_id)
        if _PROTOCOL.fullmatch(row["protocol"]) is None:
            raise MappingError("protocol")
        contracts: list[MappedContract] = []
        for item in row["contracts"]:
            address = normalize_address(item["address"])
            if address is None or not synthetic_address(address):
                raise MappingError("synthetic_address")
            contracts.append(MappedContract(item["role"], address))
        if not contracts:
            raise MappingError("contracts")
        pools.append(MappedPool(pool_id, row["chain"], row["protocol"], tuple(contracts)))
    campaigns: list[MappedCampaign] = []
    campaign_ids: set[str] = set()
    for row in document["campaigns"]:
        campaign_id = row["campaign_id"]
        if campaign_id in campaign_ids or not campaign_id_ok(campaign_id):
            raise MappingError("campaign_id")
        campaign_ids.add(campaign_id)
        if row["pool_id"] not in seen:
            raise MappingError("campaign_pool")
        campaigns.append(MappedCampaign(campaign_id, row["pool_id"], row["source"], row["product"]))
    return PoolMapping(document["mapping_version"], tuple(pools), tuple(campaigns))


def state_schema() -> dict[str, Any]:
    return read_resource_json("schemas", "yield-state.schema.json")


def packaged_mapping_path() -> Path:
    return resource_path("config", "yields", "pool-mapping.v1.json")

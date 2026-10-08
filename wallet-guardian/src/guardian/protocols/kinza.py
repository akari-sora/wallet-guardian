"""Kinza positions. Pools are documented read-only candidates, not a trust list.

The plugin reads Provider.getPool and Pool.ADDRESSES_PROVIDER in one observation,
both at the latest block tag, before it trusts either address. A mismatch or an
unreadable identity is unknown. Reserve tokens are discovered on-chain and checked
against the pool. Account totals that the reserve legs do not explain stay unknown.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.pins import KINZA_POOLS, KINZA_PROVIDERS
from guardian.protocols.views import ViewPlugin, ViewSpec

_SPECS = {
    "ADDRESSES_PROVIDER()": ViewSpec((), ("address",)),
    "POOL()": ViewSpec((), ("address",)),
    "UNDERLYING_ASSET_ADDRESS()": ViewSpec((), ("address",)),
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "decimals()": ViewSpec((), ("uint8",)),
    "getPool()": ViewSpec((), ("address",)),
    "getReserveData(address)": ViewSpec(("address",), ("uint256",) * 16),
    "getReservesList()": ViewSpec((), ("address[]",)),
    "getUserAccountData(address)": ViewSpec(("address",), ("uint256",) * 6),
}
_RESERVE_BOUND = 40
_ATOKEN_WORD = 8
_DEBT_WORD = 10
_ADDRESS_SPACE = 2**160


class Kinza(ViewPlugin):
    plugin_id = "kinza"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset(KINZA_POOLS)
    specs = _SPECS

    def observe(
        self,
        ctx: Any,
        *,
        wallet_id: str,
        wallet: str,
        network: str,
        manifest: Any,
        private: Mapping[str, Any] | None,
    ) -> PositionObservation:
        del private
        prior = ctx.prior if isinstance(ctx.prior, Mapping) else {}
        pool = KINZA_POOLS.get(network)
        provider = KINZA_PROVIDERS.get(network)
        if pool is None or provider is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )
        declared_pool = normalize_address(contract_address(manifest, network, "pool"))
        declared_provider = normalize_address(contract_address(manifest, network, "addresses_provider"))
        if declared_pool != pool.lower() or declared_provider != provider.lower():
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="contract_mismatch",
            )
        owner = normalize_address(wallet)
        if owner is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="bad_address",
            )
        identity = _reciprocal(ctx, network, pool, provider)
        if identity:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=identity,
            )
        account = ctx.eth_call(network, pool, "getUserAccountData(address)", [owner])
        fields = _six(account)
        if fields is None:
            return _failed(self.plugin_id, wallet_id, network, prior, account.reason)
        collateral_base, debt_base, available, threshold, ltv, health = fields
        metrics = (
            ("available_borrows_base", str(available)),
            ("collateral_base", str(collateral_base)),
            ("debt_base", str(debt_base)),
            ("health_factor", str(health)),
            ("liquidation_threshold", str(threshold)),
            ("ltv", str(ltv)),
        )
        listed = ctx.eth_call(network, pool, "getReservesList()", [])
        assets = _address_list(listed)
        if assets is None:
            return _failed(self.plugin_id, wallet_id, network, prior, listed.reason)
        if len(assets) > _RESERVE_BOUND:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="reserve_bound",
            )
        if len(set(assets)) != len(assets):
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="reserve_identity_mismatch",
            )
        reserves: list[dict[str, Any]] = []
        for asset in assets:
            reserve, reason = _reserve(ctx, network, pool, asset)
            if reason == "reserve_identity_mismatch":
                return unknown_position(
                    plugin_id=self.plugin_id,
                    wallet_id=wallet_id,
                    network=network,
                    prior=prior,
                    reason="reserve_identity_mismatch",
                )
            if reserve is None:
                return _failed(self.plugin_id, wallet_id, network, prior, reason)
            reserves.append(reserve)
        legs: list[PositionLeg] = []
        saw_balance = False
        for reserve in reserves:
            a_balance = ctx.eth_call(network, reserve["a_token"], "balanceOf(address)", [owner])
            a_raw = _one(a_balance)
            if a_raw is None:
                return _failed(self.plugin_id, wallet_id, network, prior, a_balance.reason)
            d_balance = ctx.eth_call(network, reserve["variable_debt_token"], "balanceOf(address)", [owner])
            d_raw = _one(d_balance)
            if d_raw is None:
                return _failed(self.plugin_id, wallet_id, network, prior, d_balance.reason)
            if a_raw or d_raw:
                saw_balance = True
            if a_raw:
                legs.append(PositionLeg(
                    kind="receipt",
                    identity=reserve["a_token"],
                    raw_amount=a_raw,
                    decimals=reserve["decimals"],
                    network=network,
                    plugin_id=self.plugin_id,
                    underlying_identity=reserve["underlying"],
                ))
            if d_raw:
                legs.append(PositionLeg(
                    kind="debt",
                    identity=reserve["variable_debt_token"],
                    raw_amount=d_raw,
                    decimals=reserve["debt_decimals"],
                    network=network,
                    plugin_id=self.plugin_id,
                    underlying_identity=reserve["underlying"],
                ))
        has_receipt = any(leg.kind == "receipt" for leg in legs)
        has_debt = any(leg.kind == "debt" for leg in legs)
        if (collateral_base and not has_receipt) or (debt_base and not has_debt) or (
            not saw_balance and (collateral_base or debt_base)
        ):
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="reserves_incomplete",
            )
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed" if legs else "none_observed",
            legs=tuple(legs),
            cursor=f"clock:{ctx.clock()}",
            metrics=metrics,
        )


def _reciprocal(ctx: Any, network: str, pool: str, provider: str) -> str:
    """Empty string when both directions match. Otherwise the unknown reason."""
    pool_of = _address(ctx.eth_call(network, provider, "getPool()", []))
    if pool_of is None:
        return "identity_unreadable"
    provider_of = _address(ctx.eth_call(network, pool, "ADDRESSES_PROVIDER()", []))
    if provider_of is None:
        return "identity_unreadable"
    if pool_of != pool.lower() or provider_of != provider.lower():
        return "provider_mismatch"
    return ""


def _reserve(ctx: Any, network: str, pool: str, asset: str) -> tuple[dict[str, Any] | None, str]:
    read = ctx.eth_call(network, pool, "getReserveData(address)", [asset])
    words = _words(read, 16)
    if words is None:
        return None, read.reason or "read_error"
    a_token = _word_address(words[_ATOKEN_WORD])
    debt = _word_address(words[_DEBT_WORD])
    if a_token is None or debt is None or a_token == debt:
        return None, "reserve_identity_mismatch"
    for token in (a_token, debt):
        reason = _token_identity(ctx, network, token, pool, asset)
        if reason:
            return None, reason
    a_decimals = _one(ctx.eth_call(network, a_token, "decimals()", []))
    debt_decimals = _one(ctx.eth_call(network, debt, "decimals()", []))
    if a_decimals is None or debt_decimals is None or a_decimals > 255 or debt_decimals > 255:
        return None, "read_error"
    return {
        "a_token": a_token,
        "debt_decimals": debt_decimals,
        "decimals": a_decimals,
        "underlying": asset,
        "variable_debt_token": debt,
    }, ""


def _token_identity(ctx: Any, network: str, token: str, pool: str, asset: str) -> str:
    pool_of = _address(ctx.eth_call(network, token, "POOL()", []))
    if pool_of is None:
        return "read_failed"
    underlying = _address(ctx.eth_call(network, token, "UNDERLYING_ASSET_ADDRESS()", []))
    if underlying is None:
        return "read_failed"
    if pool_of != pool.lower() or underlying != asset:
        return "reserve_identity_mismatch"
    return ""


def _address_list(read: Any) -> list[str] | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    values = read.decoded[0]
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        return None
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return None
        normalized = normalize_address(value)
        if normalized is None:
            return None
        found.append(normalized)
    return found


def _address(read: Any) -> str | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    value = read.decoded[0]
    if isinstance(value, str):
        return normalize_address(value)
    return None


def _word_address(value: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value >= _ADDRESS_SPACE:
        return None
    return "0x" + value.to_bytes(20, "big").hex()


def _words(read: Any, count: int) -> tuple[int, ...] | None:
    if not read.ok or read.decoded is None or len(read.decoded) != count:
        return None
    numbers: list[int] = []
    for value in read.decoded:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
            return None
        numbers.append(value)
    return tuple(numbers)


def _six(read: Any) -> tuple[int, int, int, int, int, int] | None:
    words = _words(read, 6)
    if words is None:
        return None
    return words[0], words[1], words[2], words[3], words[4], words[5]


def _one(read: Any) -> int | None:
    words = _words(read, 1)
    if words is None:
        return None
    return words[0]


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )

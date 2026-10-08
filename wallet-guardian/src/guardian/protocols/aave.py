"""Aave v3 account data. Collateral and debt stay separate legs.

Per-reserve aToken and variable-debt balances are read only when the caller
supplies the reserve list. Base-currency totals are not added on top of those
token legs. Health factor stays a 1e18 integer.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.pins import AAVE_POOLS
from guardian.protocols.views import ViewPlugin, ViewSpec

_SPECS = {
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "getUserAccountData(address)": ViewSpec(("address",), ("uint256", "uint256", "uint256", "uint256", "uint256", "uint256")),
}
_BASE_DECIMALS = 8


class AaveV3(ViewPlugin):
    plugin_id = "aave_v3"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset(AAVE_POOLS)
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
        prior = ctx.prior if isinstance(ctx.prior, Mapping) else {}
        pool = AAVE_POOLS.get(network)
        if pool is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )
        declared = contract_address(manifest, network, "pool")
        if normalize_address(declared) != pool.lower():
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="contract_mismatch",
            )
        owner = normalize_address(wallet)
        if owner is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="bad_address",
            )
        reserves, reserve_error = _reserves(private, network)
        if reserve_error:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=reserve_error,
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
        if reserves is None:
            return _base_only(
                self.plugin_id, wallet_id, network, collateral_base, debt_base, metrics, ctx.clock(),
            )
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
        if not saw_balance and (collateral_base or debt_base):
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="reserves_incomplete",
            )
        status = "observed" if legs else "none_observed"
        reasons = () if reserves else ("reserves_not_supplied",)
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status=status,
            legs=tuple(legs),
            reason_codes=reasons,
            cursor=f"clock:{ctx.clock()}",
            metrics=metrics,
        )


def _base_only(
    plugin_id: str,
    wallet_id: str,
    network: str,
    collateral_base: int,
    debt_base: int,
    metrics: tuple[tuple[str, str], ...],
    clock: int,
) -> PositionObservation:
    legs: list[PositionLeg] = []
    if collateral_base:
        legs.append(PositionLeg(
            kind="underlying",
            identity="aave-v3:collateral-base",
            raw_amount=collateral_base,
            decimals=_BASE_DECIMALS,
            network=network,
            plugin_id=plugin_id,
            detail=(("unit", "usd_base_8"),),
        ))
    if debt_base:
        legs.append(PositionLeg(
            kind="debt",
            identity="aave-v3:debt-base",
            raw_amount=debt_base,
            decimals=_BASE_DECIMALS,
            network=network,
            plugin_id=plugin_id,
            detail=(("unit", "usd_base_8"),),
        ))
    return PositionObservation(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        status="observed" if legs else "none_observed",
        legs=tuple(legs),
        reason_codes=("reserves_not_supplied",),
        cursor=f"clock:{clock}",
        metrics=metrics,
    )


def _reserves(private: Mapping[str, Any] | None, network: str) -> tuple[list[dict[str, Any]] | None, str]:
    """None means the caller did not supply a reserve list. A bad list is an error."""
    if not isinstance(private, Mapping) or "reserves" not in private:
        return None, ""
    raw = private.get("reserves")
    if not isinstance(raw, list):
        return None, "malformed_reserves"
    selected: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None, "malformed_reserves"
        if item.get("network") != network:
            continue
        underlying = normalize_address(item.get("underlying"))
        a_token = normalize_address(item.get("a_token"))
        debt = normalize_address(item.get("variable_debt_token"))
        if underlying is None or a_token is None or debt is None:
            return None, "malformed_reserves"
        decimals = _decimals(item.get("decimals"))
        debt_decimals = _decimals(item.get("debt_decimals"))
        if item.get("decimals") is not None and decimals is None:
            return None, "malformed_reserves"
        if item.get("debt_decimals") is not None and debt_decimals is None:
            return None, "malformed_reserves"
        selected.append({
            "a_token": a_token,
            "debt_decimals": debt_decimals,
            "decimals": decimals,
            "underlying": underlying,
            "variable_debt_token": debt,
        })
    return selected, ""


def _decimals(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        return None
    return value


def _six(read: Any) -> tuple[int, int, int, int, int, int] | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 6:
        return None
    numbers: list[int] = []
    for value in read.decoded:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
            return None
        numbers.append(value)
    return numbers[0], numbers[1], numbers[2], numbers[3], numbers[4], numbers[5]


def _one(read: Any) -> int | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    value = read.decoded[0]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
        return None
    return value


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )

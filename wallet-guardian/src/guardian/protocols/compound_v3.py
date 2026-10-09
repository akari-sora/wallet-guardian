"""Compound III Ethereum USDC Comet. Read-only eth_call views on the proxy.

Base supply, base debt, and collateral stay separate. Collateral is not borrowed
principal and not a base deposit. ``userBasic.principal`` is the stored principal.
It is not labeled accrued to now. ``isBorrowCollateralized`` and ``isLiquidatable``
are the protocol's named predicates. This module does not invent a health factor
and does not call ``accrueAccount``.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.pins import COMET_IMPLEMENTATION, COMET_USDC, USDC
from guardian.protocols.views import ViewPlugin, ViewSpec

_ASSET_BOUND = 32
_BASE_DECIMALS = 6
STALE_ACCRUAL_SECONDS = 3600
_ASSET_INFO = "(uint8,address,address,uint64,uint64,uint64,uint64,uint128)"
_TOTALS = "(uint64,uint64,uint64,uint64,uint104,uint104,uint40,uint8)"
_USER = "(int104,uint64,uint64,uint16,uint8)"
_INT104 = 1 << 103
_SPECS = {
    "baseToken()": ViewSpec((), ("address",)),
    "baseTokenPriceFeed()": ViewSpec((), ("address",)),
    "collateralBalanceOf(address,address)": ViewSpec(("address", "address"), ("uint128",)),
    "decimals()": ViewSpec((), ("uint8",)),
    "getAssetInfo(uint8)": ViewSpec(("uint8",), (_ASSET_INFO,)),
    "getPrice(address)": ViewSpec(("address",), ("uint256",)),
    "isBorrowCollateralized(address)": ViewSpec(("address",), ("bool",)),
    "isLiquidatable(address)": ViewSpec(("address",), ("bool",)),
    "numAssets()": ViewSpec((), ("uint8",)),
    "totalsBasic()": ViewSpec((), (_TOTALS,)),
    "userBasic(address)": ViewSpec(("address",), (_USER,)),
}


class CompoundV3(ViewPlugin):
    plugin_id = "compound_v3"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset({"ethereum-mainnet"})
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
        if network not in self.networks:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )
        if not _matches(manifest, network):
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="contract_mismatch",
            )
        owner = normalize_address(wallet)
        if owner is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="bad_address",
            )
        head = ctx.evm_head(network)
        if head is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "head_unreadable")
        number, timestamp = head
        block = hex(number)
        comet = normalize_address(COMET_USDC)
        base = normalize_address(USDC)
        if comet is None or base is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "bad_address")

        base_of = _address(_call(ctx, network, comet, "baseToken()", [], block))
        if base_of is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        if base_of != base:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="pin_mismatch",
            )
        feed = _address(_call(ctx, network, comet, "baseTokenPriceFeed()", [], block))
        if feed is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        decimals = _uint8(_call(ctx, network, comet, "decimals()", [], block))
        if decimals is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        if decimals != _BASE_DECIMALS:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="pin_mismatch",
            )
        count = _uint8(_call(ctx, network, comet, "numAssets()", [], block))
        if count is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        if count > _ASSET_BOUND:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="result_exceeds_bound",
            )
        totals = _tuple(_call(ctx, network, comet, "totalsBasic()", [], block), 8)
        if totals is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        supply_index = _uint(totals[0])
        borrow_index = _uint(totals[1])
        last_accrual = _uint(totals[6])
        pause_flags = _uint(totals[7])
        if supply_index is None or borrow_index is None or last_accrual is None or pause_flags is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "malformed_result")
        if last_accrual > timestamp:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="accrual_inconsistent",
            )
        accrual = "stale_stored_index" if timestamp - last_accrual > STALE_ACCRUAL_SECONDS else "stored_index"
        user = _tuple(_call(ctx, network, comet, "userBasic(address)", [owner], block), 5)
        if user is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
        principal = _principal(user[0])
        if principal is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "malformed_result")

        infos: list[dict[str, Any]] = []
        for index in range(count):
            info = _asset(_call(ctx, network, comet, "getAssetInfo(uint8)", [index], block))
            if info is None:
                return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
            if info["asset"] == base or info["asset"] == comet:
                return unknown_position(
                    plugin_id=self.plugin_id,
                    wallet_id=wallet_id,
                    network=network,
                    prior=prior,
                    reason="collateral_identity_mismatch",
                )
            infos.append(info)
        if len({item["asset"] for item in infos}) != len(infos):
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="collateral_identity_mismatch",
            )
        held: list[dict[str, Any]] = []
        for info in infos:
            balance = _one(_call(ctx, network, comet, "collateralBalanceOf(address,address)", [owner, info["asset"]], block))
            if balance is None:
                return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
            asset_decimals = _uint8(_call(ctx, network, info["asset"], "decimals()", [], block))
            if asset_decimals is None:
                return _failed(self.plugin_id, wallet_id, network, prior, "read_error")
            held.append({"asset": info["asset"], "balance": balance, "decimals": asset_decimals, "feed": info["feed"]})

        if _price(_call(ctx, network, comet, "getPrice(address)", [feed], block)):
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="oracle_failed",
            )
        for item in held:
            if _price(_call(ctx, network, comet, "getPrice(address)", [item["feed"]], block)):
                return unknown_position(
                    plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="oracle_failed",
                )
        collateralized = _bool(_call(ctx, network, comet, "isBorrowCollateralized(address)", [owner], block))
        liquidatable = _bool(_call(ctx, network, comet, "isLiquidatable(address)", [owner], block))
        if collateralized is None or liquidatable is None:
            return _failed(self.plugin_id, wallet_id, network, prior, "read_error")

        common = (
            ("accrual", accrual),
            ("base_borrow_index", str(borrow_index)),
            ("base_supply_index", str(supply_index)),
            ("block_number", str(number)),
            ("block_timestamp", str(timestamp)),
            ("is_borrow_collateralized", "true" if collateralized else "false"),
            ("is_liquidatable", "true" if liquidatable else "false"),
            ("last_accrual_time", str(last_accrual)),
            ("pause_flags", str(pause_flags)),
            ("safety_certificate", "false"),
        )
        metrics = tuple(common)
        legs: list[PositionLeg] = []
        if principal > 0:
            legs.append(PositionLeg(
                kind="receipt",
                identity=comet,
                raw_amount=principal,
                decimals=decimals,
                network=network,
                plugin_id=self.plugin_id,
                underlying_identity=base,
                detail=(("role", "base_supply"), *common),
            ))
        elif principal < 0:
            legs.append(PositionLeg(
                kind="debt",
                identity="compound-v3:base-debt",
                raw_amount=-principal,
                decimals=decimals,
                network=network,
                plugin_id=self.plugin_id,
                underlying_identity=base,
                detail=(("role", "base_debt"), *common),
            ))
        for item in held:
            if item["balance"] == 0:
                continue
            legs.append(PositionLeg(
                kind="underlying",
                identity=item["asset"],
                raw_amount=item["balance"],
                decimals=item["decimals"],
                network=network,
                plugin_id=self.plugin_id,
                underlying_identity=item["asset"],
                detail=(("role", "collateral"), *common),
            ))
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed" if legs else "none_observed",
            legs=tuple(legs),
            cursor=f"block:{number}",
            metrics=metrics,
        )


def _matches(manifest: Any, network: str) -> bool:
    comet = normalize_address(contract_address(manifest, network, "comet"))
    base = normalize_address(contract_address(manifest, network, "base_token"))
    implementation = normalize_address(contract_address(manifest, network, "implementation"))
    return (
        comet == normalize_address(COMET_USDC)
        and base == normalize_address(USDC)
        and implementation == normalize_address(COMET_IMPLEMENTATION)
    )


def _call(ctx: Any, network: str, to: str, signature: str, args: list[Any], block: str) -> Any:
    return ctx.eth_call(network, to, signature, args, block=block)


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )


def _address(read: Any) -> str | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    return normalize_address(read.decoded[0])


def _one(read: Any) -> int | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    return _uint(read.decoded[0])


def _uint8(read: Any) -> int | None:
    value = _one(read)
    if value is None or value > 255:
        return None
    return value


def _uint(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > UINT256_MAX:
        return None
    return value


def _principal(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= -_INT104 or value >= _INT104:
        return None
    return value


def _bool(read: Any) -> bool | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    value = read.decoded[0]
    if not isinstance(value, bool):
        return None
    return value


def _tuple(read: Any, size: int) -> tuple[Any, ...] | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    row = read.decoded[0]
    if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)) or len(row) != size:
        return None
    return tuple(row)


def _asset(read: Any) -> dict[str, str] | None:
    row = _tuple(read, 8)
    if row is None:
        return None
    asset = normalize_address(row[1])
    feed = normalize_address(row[2])
    if asset is None or feed is None:
        return None
    if asset == "0x" + "0" * 40 or feed == "0x" + "0" * 40:
        return None
    return {"asset": asset, "feed": feed}


def _price(read: Any) -> str:
    """Empty when the feed returned a positive price. Otherwise ``oracle_failed``."""
    if not read.ok:
        return "oracle_failed"
    value = _one(read)
    if value is None or value <= 0:
        return "oracle_failed"
    return ""

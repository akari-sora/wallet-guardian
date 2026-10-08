"""ERC-4626 vaults named by the caller. The public registry is empty.

Shares are a receipt. ``convertToAssets`` is the underlying quote of those shares
and is not a second holding. ``asset()`` supplies the underlying identity.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, unknown_position
from guardian.protocols.pins import AAVE_POOLS
from guardian.protocols.views import ViewPlugin, ViewSpec

_SPECS = {
    "asset()": ViewSpec((), ("address",)),
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "convertToAssets(uint256)": ViewSpec(("uint256",), ("uint256",)),
}
_NETWORKS = frozenset(AAVE_POOLS) | frozenset({"bsc-mainnet"})


class Erc4626(ViewPlugin):
    plugin_id = "erc4626"
    adapters = frozenset({"evm_rpc"})
    networks = _NETWORKS
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
        del manifest
        prior = ctx.prior if isinstance(ctx.prior, Mapping) else {}
        if network not in self.networks:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )
        owner = normalize_address(wallet)
        if owner is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="bad_address",
            )
        vaults, error = _vaults(private, network)
        if error:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=error,
            )
        if not vaults:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="vaults_not_configured",
            )
        legs: list[PositionLeg] = []
        for vault in vaults:
            asset_read = ctx.eth_call(network, vault["vault"], "asset()", [])
            asset = _address(asset_read)
            if asset is None:
                return _failed(self.plugin_id, wallet_id, network, prior, asset_read.reason)
            balance = ctx.eth_call(network, vault["vault"], "balanceOf(address)", [owner])
            shares = _one(balance)
            if shares is None:
                return _failed(self.plugin_id, wallet_id, network, prior, balance.reason)
            quote = ctx.eth_call(network, vault["vault"], "convertToAssets(uint256)", [shares])
            assets = _one(quote)
            if assets is None:
                return _failed(self.plugin_id, wallet_id, network, prior, quote.reason)
            if shares == 0 and assets == 0:
                continue
            legs.append(PositionLeg(
                kind="receipt",
                identity=vault["vault"],
                raw_amount=shares,
                decimals=vault["share_decimals"],
                network=network,
                plugin_id=self.plugin_id,
                underlying_identity=asset,
            ))
            legs.append(PositionLeg(
                kind="underlying",
                identity=asset,
                raw_amount=assets,
                decimals=vault["asset_decimals"],
                network=network,
                plugin_id=self.plugin_id,
                underlying_identity=asset,
                duplicates=vault["vault"],
                detail=(("role", "quote"),),
            ))
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed" if legs else "none_observed",
            legs=tuple(legs),
            cursor=f"clock:{ctx.clock()}",
        )


def _vaults(private: Mapping[str, Any] | None, network: str) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(private, Mapping) or "vaults" not in private:
        return [], ""
    raw = private.get("vaults")
    if not isinstance(raw, list):
        return [], "malformed_vaults"
    selected: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return [], "malformed_vaults"
        if item.get("network") != network:
            continue
        vault = normalize_address(item.get("vault"))
        if vault is None:
            return [], "malformed_vaults"
        share_decimals = _decimals(item.get("share_decimals"))
        asset_decimals = _decimals(item.get("asset_decimals"))
        if item.get("share_decimals") is not None and share_decimals is None:
            return [], "malformed_vaults"
        if item.get("asset_decimals") is not None and asset_decimals is None:
            return [], "malformed_vaults"
        selected.append({
            "asset_decimals": asset_decimals,
            "share_decimals": share_decimals,
            "vault": vault,
        })
    return selected, ""


def _decimals(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        return None
    return value


def _address(read: Any) -> str | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    return normalize_address(read.decoded[0])


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

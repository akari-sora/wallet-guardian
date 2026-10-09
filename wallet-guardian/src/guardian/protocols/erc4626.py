"""ERC-4626 vaults.

``Erc4626`` reads vaults named by the caller. That public registry is empty.
``Erc4626Pins`` reads the documented sDAI and Steakhouse USDC candidates when
the operator opts in. It is not part of the default example monitor.

Shares are a receipt. ``convertToAssets`` rounds down and is the underlying
quote of those shares, not a second holding and not a promise that the quote
can be withdrawn. ``maxWithdraw`` is the withdrawal limit. ``asset()`` or
``MORPHO()`` agreement is an identity check, not a safety certificate.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.amounts import UINT256_MAX
from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.pins import AAVE_POOLS, DAI, MORPHO, SDAI, STEAKHOUSE_USDC, USDC
from guardian.protocols.views import ViewPlugin, ViewSpec
from guardian.security.untrusted import sanitize_untrusted

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


_PIN_SPECS = {
    "MORPHO()": ViewSpec((), ("address",)),
    "asset()": ViewSpec((), ("address",)),
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "convertToAssets(uint256)": ViewSpec(("uint256",), ("uint256",)),
    "decimals()": ViewSpec((), ("uint8",)),
    "maxWithdraw(address)": ViewSpec(("address",), ("uint256",)),
    "name()": ViewSpec((), ("string",)),
    "symbol()": ViewSpec((), ("string",)),
}


class Erc4626Pins(ViewPlugin):
    """Opt-in sDAI and Steakhouse USDC. No Morpho Blue look-through and no default enablement."""

    plugin_id = "erc4626_pins"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset({"ethereum-mainnet"})
    specs = _PIN_SPECS

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
        if not _pins_match(manifest, network):
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
        number, _timestamp = head
        block = hex(number)
        legs: list[PositionLeg] = []
        for pin in _REGISTERED:
            built, reason = _read_pin(ctx, self.plugin_id, network, owner, pin, block)
            if reason:
                if reason == "pin_mismatch":
                    return unknown_position(
                        plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=reason,
                    )
                if reason == "quote_mismatch":
                    return unknown_position(
                        plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=reason,
                    )
                return _failed(self.plugin_id, wallet_id, network, prior, reason)
            legs.extend(built)
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed" if legs else "none_observed",
            legs=tuple(legs),
            cursor=f"block:{number}",
            metrics=(("block_number", str(number)), ("safety_certificate", "false")),
        )


def _pins_match(manifest: Any, network: str) -> bool:
    for pin in _REGISTERED:
        vault = normalize_address(contract_address(manifest, network, pin["role"]))
        asset = normalize_address(contract_address(manifest, network, pin["asset_role"]))
        if vault != pin["vault"] or asset != pin["asset"]:
            return False
        if pin["morpho"] is not None:
            morpho = normalize_address(contract_address(manifest, network, pin["morpho_role"]))
            if morpho != pin["morpho"]:
                return False
    return True


def _read_pin(
    ctx: Any,
    plugin_id: str,
    network: str,
    owner: str,
    pin: Mapping[str, Any],
    block: str,
) -> tuple[list[PositionLeg], str]:
    vault = pin["vault"]
    asset_read = ctx.eth_call(network, vault, "asset()", [], block=block)
    asset = _address(asset_read)
    if asset is None:
        return [], asset_read.reason or "read_error"
    if asset != pin["asset"]:
        return [], "pin_mismatch"
    share_decimals = _uint8(ctx.eth_call(network, vault, "decimals()", [], block=block))
    if share_decimals is None:
        return [], "read_error"
    if share_decimals != pin["share_decimals"]:
        return [], "pin_mismatch"
    asset_decimals = _uint8(ctx.eth_call(network, asset, "decimals()", [], block=block))
    if asset_decimals is None:
        return [], "read_error"
    if asset_decimals != pin["asset_decimals"]:
        return [], "pin_mismatch"
    if pin["morpho"] is not None:
        morpho_read = ctx.eth_call(network, vault, "MORPHO()", [], block=block)
        morpho = _address(morpho_read)
        if morpho is None:
            return [], morpho_read.reason or "read_error"
        if morpho != pin["morpho"]:
            return [], "pin_mismatch"
    symbol = _text(ctx.eth_call(network, vault, "symbol()", [], block=block))
    if symbol is None:
        return [], "read_error"
    token_name = _text(ctx.eth_call(network, vault, "name()", [], block=block))
    if token_name is None:
        return [], "read_error"
    balance = ctx.eth_call(network, vault, "balanceOf(address)", [owner], block=block)
    shares = _one(balance)
    if shares is None:
        return [], balance.reason or "read_error"
    quote = ctx.eth_call(network, vault, "convertToAssets(uint256)", [shares], block=block)
    assets = _one(quote)
    if assets is None:
        return [], quote.reason or "read_error"
    if shares == 0 and assets != 0:
        return [], "quote_mismatch"
    if shares == 0 and assets == 0:
        return [], ""
    withdraw = ctx.eth_call(network, vault, "maxWithdraw(address)", [owner], block=block)
    limit = _one(withdraw)
    if limit is None:
        return [], withdraw.reason or "read_error"
    common = (
        ("agreement_is_not_safety_certificate", "true"),
        ("asset_agreement", "true"),
        ("block_number", str(int(block, 16))),
        ("exposure", "vault"),
        ("name", sanitize_untrusted(token_name)),
        ("quote_is_withdrawal_promise", "false"),
        ("redemption_guaranteed", "false"),
        ("safety_certificate", "false"),
        ("symbol", sanitize_untrusted(symbol)),
        ("symbol_untrusted", "true"),
    )
    if pin["morpho"] is not None:
        common = (*common, ("morpho_agreement", "true"))
    return [
        PositionLeg(
            kind="receipt",
            identity=vault,
            raw_amount=shares,
            decimals=share_decimals,
            network=network,
            plugin_id=plugin_id,
            underlying_identity=asset,
            detail=(("role", "shares"), *common),
        ),
        PositionLeg(
            kind="underlying",
            identity=asset,
            raw_amount=assets,
            decimals=asset_decimals,
            network=network,
            plugin_id=plugin_id,
            underlying_identity=asset,
            duplicates=vault,
            detail=(
                ("counted", "false"),
                ("duplicates", vault),
                ("max_withdraw", str(limit)),
                ("role", "quote"),
                ("rounding", "down"),
                *common,
            ),
        ),
    ], ""


def _uint8(read: Any) -> int | None:
    value = _one(read)
    if value is None or value > 255:
        return None
    return value


def _text(read: Any) -> str | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    value = read.decoded[0]
    if not isinstance(value, str) or len(value) > 256:
        return None
    return value


def _pin(
    role: str,
    vault: str,
    asset_role: str,
    asset: str,
    share_decimals: int,
    asset_decimals: int,
    morpho_role: str | None = None,
    morpho: str | None = None,
) -> dict[str, Any]:
    return {
        "asset": normalize_address(asset),
        "asset_decimals": asset_decimals,
        "asset_role": asset_role,
        "morpho": None if morpho is None else normalize_address(morpho),
        "morpho_role": morpho_role,
        "role": role,
        "share_decimals": share_decimals,
        "vault": normalize_address(vault),
    }


_REGISTERED = (
    _pin("sdai", SDAI, "sdai_asset", DAI, 18, 18),
    _pin(
        "steakhouse_usdc",
        STEAKHOUSE_USDC,
        "steakhouse_asset",
        USDC,
        18,
        6,
        "steakhouse_morpho",
        MORPHO,
    ),
)

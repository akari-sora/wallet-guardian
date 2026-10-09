"""Lido stETH, wstETH, and the withdrawal queue. Read-only eth_call views.

stETH ``balanceOf`` is pooled ETH. It is checked against
``getPooledEthByShares(sharesOf)``. A gap wider than 2 wei is unknown.
wstETH is a receipt; ``getStETHByWstETH`` is its stETH quote, and that same
integer is the ETH quote because stETH is denominated in pooled ETH. Quotes
use ``duplicates`` so the receipt is counted once. Withdrawal requests are
reported as pending, finalized-claimable, or claimed. This module does not
encode a claim.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.network import native_decimals, native_identity
from guardian.protocols.pins import LIDO_STETH, LIDO_WITHDRAWAL_QUEUE, LIDO_WSTETH
from guardian.protocols.views import ViewPlugin, ViewSpec

_MAX_REQUESTS = 64
_WEI_TOLERANCE = 2
_STATUS_TUPLE = "(uint256,uint256,address,uint256,bool,bool)"
_SPECS = {
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "getPooledEthByShares(uint256)": ViewSpec(("uint256",), ("uint256",)),
    "getStETHByWstETH(uint256)": ViewSpec(("uint256",), ("uint256",)),
    "getWithdrawalRequests(address)": ViewSpec(("address",), ("uint256[]",)),
    "getWithdrawalStatus(uint256[])": ViewSpec(("uint256[]",), (_STATUS_TUPLE + "[]",)),
    "sharesOf(address)": ViewSpec(("address",), ("uint256",)),
}
_WSTETH_ETH = "lido:wsteth:eth"


class Lido(ViewPlugin):
    plugin_id = "lido"
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
        decimals = native_decimals(network)
        native = native_identity(network)
        if decimals is None or native is None:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="unsupported_network",
            )

        requests = ctx.eth_call(network, LIDO_WITHDRAWAL_QUEUE, "getWithdrawalRequests(address)", [owner])
        ids, id_error = _request_ids(requests)
        if id_error or ids is None:
            return _failed(self.plugin_id, wallet_id, network, prior, id_error or requests.reason)
        if len(ids) > _MAX_REQUESTS:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="result_exceeds_bound",
            )
        if len(set(ids)) != len(ids):
            return _failed(self.plugin_id, wallet_id, network, prior, "malformed_result")
        opened: tuple[tuple[Any, ...], ...] = ()
        if ids:
            status = ctx.eth_call(network, LIDO_WITHDRAWAL_QUEUE, "getWithdrawalStatus(uint256[])", [list(ids)])
            parsed, status_error = _statuses(status, ids, owner)
            if status_error or parsed is None:
                return _failed(self.plugin_id, wallet_id, network, prior, status_error or status.reason)
            opened = parsed

        balance_read = ctx.eth_call(network, LIDO_STETH, "balanceOf(address)", [owner])
        balance = _one_uint(balance_read)
        if balance is None:
            return _failed(self.plugin_id, wallet_id, network, prior, balance_read.reason)
        shares_read = ctx.eth_call(network, LIDO_STETH, "sharesOf(address)", [owner])
        shares = _one_uint(shares_read)
        if shares is None:
            return _failed(self.plugin_id, wallet_id, network, prior, shares_read.reason)
        pooled_read = ctx.eth_call(network, LIDO_STETH, "getPooledEthByShares(uint256)", [shares])
        pooled = _one_uint(pooled_read)
        if pooled is None:
            return _failed(self.plugin_id, wallet_id, network, prior, pooled_read.reason)
        if abs(balance - pooled) > _WEI_TOLERANCE:
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="share_balance_mismatch",
            )

        wst_read = ctx.eth_call(network, LIDO_WSTETH, "balanceOf(address)", [owner])
        wst_balance = _one_uint(wst_read)
        if wst_balance is None:
            return _failed(self.plugin_id, wallet_id, network, prior, wst_read.reason)
        quote_read = ctx.eth_call(network, LIDO_WSTETH, "getStETHByWstETH(uint256)", [wst_balance])
        wst_steth = _one_uint(quote_read)
        if wst_steth is None:
            return _failed(self.plugin_id, wallet_id, network, prior, quote_read.reason)
        if wst_balance == 0 and wst_steth != 0:
            return unknown_position(
                plugin_id=self.plugin_id,
                wallet_id=wallet_id,
                network=network,
                prior=prior,
                reason="wsteth_quote_mismatch",
            )

        legs = _legs(network, decimals, native, balance, shares, pooled, wst_balance, wst_steth, opened)
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status="observed" if legs else "none_observed",
            legs=tuple(legs),
            cursor=f"clock:{ctx.clock()}",
        )


def _matches(manifest: Any, network: str) -> bool:
    steth = normalize_address(contract_address(manifest, network, "steth"))
    wsteth = normalize_address(contract_address(manifest, network, "wsteth"))
    queue = normalize_address(contract_address(manifest, network, "withdrawal_queue"))
    return steth == LIDO_STETH.lower() and wsteth == LIDO_WSTETH.lower() and queue == LIDO_WITHDRAWAL_QUEUE.lower()


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )


def _request_ids(read: Any) -> tuple[tuple[int, ...] | None, str]:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None, read.reason or "malformed_result"
    rows = read.decoded[0]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, (list, tuple)):
        return None, "malformed_result"
    found: list[int] = []
    for value in rows:
        number = _uint(value)
        if number is None:
            return None, "malformed_result"
        found.append(number)
    return tuple(found), ""


def _statuses(read: Any, ids: Sequence[int], owner: str) -> tuple[tuple[tuple[Any, ...], ...] | None, str]:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None, read.reason or "malformed_result"
    rows = read.decoded[0]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, (list, tuple)):
        return None, "malformed_result"
    if len(rows) != len(ids):
        return None, "malformed_result"
    parsed: list[tuple[Any, ...]] = []
    for request_id, row in zip(ids, rows):
        item, error = _one_status(request_id, row, owner)
        if error or item is None:
            return None, error or "malformed_result"
        parsed.append(item)
    return tuple(parsed), ""


def _one_status(request_id: int, row: Any, owner: str) -> tuple[tuple[Any, ...] | None, str]:
    if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)) or len(row) != 6:
        return None, "malformed_result"
    steth, shares, row_owner, timestamp, finalized, claimed = row
    if _uint(steth) is None or _uint(shares) is None or _uint(timestamp) is None:
        return None, "malformed_result"
    if not isinstance(finalized, bool) or not isinstance(claimed, bool):
        return None, "malformed_result"
    if normalize_address(row_owner) != owner:
        return None, "malformed_result"
    status = _withdrawal_status(finalized, claimed)
    if status is None:
        return None, "malformed_result"
    return (request_id, int(steth), int(shares), int(timestamp), status, finalized, claimed), ""


def _withdrawal_status(finalized: bool, claimed: bool) -> str | None:
    if claimed and not finalized:
        return None
    if claimed:
        return "claimed"
    if finalized:
        return "finalized-claimable"
    return "pending"


def _legs(
    network: str,
    decimals: int,
    native: str,
    balance: int,
    shares: int,
    pooled: int,
    wst_balance: int,
    wst_steth: int,
    opened: Sequence[tuple[Any, ...]],
) -> list[PositionLeg]:
    steth = LIDO_STETH.lower()
    wsteth = LIDO_WSTETH.lower()
    legs: list[PositionLeg] = []
    if balance > 0 or pooled > 0:
        legs.append(PositionLeg(
            kind="receipt",
            identity=steth,
            raw_amount=balance,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=native,
            detail=(("shares", str(shares)),),
        ))
        legs.append(PositionLeg(
            kind="underlying",
            identity=native,
            raw_amount=pooled,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=native,
            duplicates=steth,
            detail=(("role", "quote"),),
        ))
    if wst_balance > 0:
        legs.append(PositionLeg(
            kind="receipt",
            identity=wsteth,
            raw_amount=wst_balance,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=steth,
        ))
        legs.append(PositionLeg(
            kind="underlying",
            identity=steth,
            raw_amount=wst_steth,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=steth,
            duplicates=wsteth,
            detail=(("asset", "stETH"), ("role", "quote")),
        ))
        legs.append(PositionLeg(
            kind="underlying",
            identity=_WSTETH_ETH,
            raw_amount=wst_steth,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=native,
            duplicates=wsteth,
            detail=(("asset", "ETH"), ("role", "quote")),
        ))
    for request_id, steth_amount, share_amount, timestamp, status, finalized, claimed in opened:
        legs.append(PositionLeg(
            kind="underlying",
            identity=f"lido:withdrawal:{request_id}:{status}",
            raw_amount=steth_amount,
            decimals=decimals,
            network=network,
            plugin_id=Lido.plugin_id,
            underlying_identity=native,
            detail=(
                ("amount_of_shares", str(share_amount)),
                ("is_claimed", "true" if claimed else "false"),
                ("is_finalized", "true" if finalized else "false"),
                ("request_id", str(request_id)),
                ("role", "withdrawal"),
                ("status", status),
                ("timestamp", str(timestamp)),
            ),
        ))
    return legs


def _uint(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _one_uint(read: Any) -> int | None:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None
    return _uint(read.decoded[0])

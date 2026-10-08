"""Lista slisBNB reads. Claimable comes from getUserRequestStatus, not from a 7-day clock.

This module encodes only the views in ``specs``. It does not build claimWithdraw.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from guardian.chains.evm.accounting import normalize_address
from guardian.protocols.evidence import assess_withdrawals
from guardian.protocols.models import (
    PositionLeg,
    PositionObservation,
    WithdrawalRequest,
    contract_address,
    unknown_position,
)
from guardian.protocols.network import native_decimals, native_identity
from guardian.protocols.pins import LISTA_PROXY, SLISBNB
from guardian.protocols.views import ViewPlugin, ViewSpec

_MAX_REQUESTS = 32
_SPECS = {
    "balanceOf(address)": ViewSpec(("address",), ("uint256",)),
    "convertSnBnbToBnb(uint256)": ViewSpec(("uint256",), ("uint256",)),
    "getUserRequestStatus(address,uint256)": ViewSpec(("address", "uint256"), ("bool", "uint256")),
    "getUserWithdrawalRequests(address)": ViewSpec(("address",), ("(uint256,uint256,uint256)[]",)),
    "paused()": ViewSpec((), ("bool",)),
}


class ListaSlisBnb(ViewPlugin):
    plugin_id = "lista_slisbnb"
    adapters = frozenset({"evm_rpc"})
    networks = frozenset({"bsc-mainnet"})
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
        prior_ids, id_error = _prior_ids(prior)
        if id_error:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=id_error,
            )
        evidence, evidence_error = _evidence(private)
        if evidence_error:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=evidence_error,
            )

        paused = ctx.eth_call(network, LISTA_PROXY, "paused()", [])
        if not paused.ok or paused.decoded is None or len(paused.decoded) != 1 or not isinstance(paused.decoded[0], bool):
            return _failed(self.plugin_id, wallet_id, network, prior, paused.reason)
        requests = ctx.eth_call(network, LISTA_PROXY, "getUserWithdrawalRequests(address)", [owner])
        rows, row_error = _request_rows(requests)
        if row_error:
            return _failed(self.plugin_id, wallet_id, network, prior, row_error)
        assert rows is not None
        if len(rows) > _MAX_REQUESTS:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="result_exceeds_bound",
            )
        opened: list[WithdrawalRequest] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            parsed, error = _one_request(ctx, network, owner, index, row)
            if error or parsed is None:
                return _failed(self.plugin_id, wallet_id, network, prior, error or "malformed_result")
            if parsed.request_id in seen:
                return _failed(self.plugin_id, wallet_id, network, prior, "malformed_result")
            seen.add(parsed.request_id)
            opened.append(parsed)

        balance = ctx.eth_call(network, SLISBNB, "balanceOf(address)", [owner])
        shares = _one_uint(balance)
        if shares is None:
            return _failed(self.plugin_id, wallet_id, network, prior, balance.reason or "malformed_result")
        quote = ctx.eth_call(network, LISTA_PROXY, "convertSnBnbToBnb(uint256)", [shares])
        quoted = _one_uint(quote)
        if quoted is None:
            return _failed(self.plugin_id, wallet_id, network, prior, quote.reason or "malformed_result")

        assessments = assess_withdrawals(prior_ids, opened, evidence)
        legs = _legs(network, shares, quoted, opened)
        reasons: list[str] = []
        if paused.decoded[0]:
            reasons.append("protocol_paused")
        if any(item.status == "disappeared_unverified" for item in assessments):
            reasons.append("disappeared_unverified")
        status = "observed" if legs or opened else "none_observed"
        return PositionObservation(
            plugin_id=self.plugin_id,
            wallet_id=wallet_id,
            network=network,
            status=status,
            legs=tuple(legs),
            withdrawals=tuple(opened),
            assessments=assessments,
            reason_codes=tuple(reasons),
            cursor=f"clock:{ctx.clock()}",
            prior_kept=False,
            metrics=(("paused", "true" if paused.decoded[0] else "false"),),
        )


def _matches(manifest: Any, network: str) -> bool:
    proxy = contract_address(manifest, network, "proxy")
    slis = contract_address(manifest, network, "slisbnb")
    return normalize_address(proxy) == LISTA_PROXY.lower() and normalize_address(slis) == SLISBNB.lower()


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )


def _prior_ids(prior: Mapping[str, Any]) -> tuple[list[str], str]:
    if "request_ids" not in prior:
        return [], ""
    raw = prior.get("request_ids")
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) or not item for item in raw):
        return [], "malformed_prior"
    return list(raw), ""


def _evidence(private: Mapping[str, Any] | None) -> tuple[list[Any], str]:
    if not isinstance(private, Mapping) or "claim_evidence" not in private:
        return [], ""
    raw = private.get("claim_evidence")
    if not isinstance(raw, list):
        return [], "malformed_prior"
    return raw, ""


def _request_rows(read: Any) -> tuple[tuple[Any, ...] | None, str]:
    if not read.ok or read.decoded is None or len(read.decoded) != 1:
        return None, read.reason or "malformed_result"
    rows = read.decoded[0]
    if not isinstance(rows, (list, tuple)):
        return None, "malformed_result"
    return tuple(rows), ""


def _one_request(ctx: Any, network: str, owner: str, index: int, row: Any) -> tuple[WithdrawalRequest | None, str]:
    if not isinstance(row, (list, tuple)) or len(row) != 3:
        return None, "malformed_result"
    uuid, slis_raw, start = row
    if any(_uint(value) is None for value in (uuid, slis_raw, start)):
        return None, "malformed_result"
    status = ctx.eth_call(network, LISTA_PROXY, "getUserRequestStatus(address,uint256)", [owner, index])
    if not status.ok or status.decoded is None or len(status.decoded) != 2:
        return None, status.reason or "malformed_result"
    claimable, bnb_raw = status.decoded
    if not isinstance(claimable, bool) or _uint(bnb_raw) is None:
        return None, "malformed_result"
    return WithdrawalRequest(
        request_id=str(uuid),
        index=index,
        slis_raw=int(slis_raw),
        bnb_raw=int(bnb_raw),
        is_claimable=claimable,
        start_time=int(start),
    ), ""


def _legs(network: str, shares: int, quoted: int, opened: Sequence[WithdrawalRequest]) -> list[PositionLeg]:
    decimals = native_decimals(network)
    native = native_identity(network)
    legs: list[PositionLeg] = []
    if shares > 0:
        legs.append(PositionLeg(
            kind="receipt",
            identity=SLISBNB.lower(),
            raw_amount=shares,
            decimals=decimals,
            network=network,
            plugin_id=ListaSlisBnb.plugin_id,
            underlying_identity=native,
        ))
        if native is not None:
            legs.append(PositionLeg(
                kind="underlying",
                identity=native,
                raw_amount=quoted,
                decimals=decimals,
                network=network,
                plugin_id=ListaSlisBnb.plugin_id,
                underlying_identity=native,
                duplicates=SLISBNB.lower(),
                detail=(("role", "quote"),),
            ))
    for item in opened:
        if item.bnb_raw <= 0 or native is None:
            continue
        legs.append(PositionLeg(
            kind="underlying",
            identity=f"lista-slisbnb:withdrawal:{item.request_id}",
            raw_amount=item.bnb_raw,
            decimals=decimals,
            network=network,
            plugin_id=ListaSlisBnb.plugin_id,
            underlying_identity=native,
            detail=(("role", "withdrawal"), ("request_id", item.request_id), ("claimable", "true" if item.is_claimable else "false")),
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

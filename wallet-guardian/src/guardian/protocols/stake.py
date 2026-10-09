"""Solana stake accounts. Lamports stay integers. Delegation stake is not a second SOL balance.

Accounts come from the caller or from getProgramAccounts memcmp on the stake
program's documented staker (offset 12) or withdrawer (offset 44) field.
A float amount is malformed, not zero.
"""

from __future__ import annotations

from typing import Any, Mapping

from guardian.chains.solana.adapter import solana_rpc
from guardian.identities import IdentityError, parse_address
from guardian.protocols.models import PositionLeg, PositionObservation, contract_address, unknown_position
from guardian.protocols.network import native_decimals, native_identity
from guardian.protocols.pins import STAKE_PROGRAM, STAKE_STAKER_OFFSET, STAKE_WITHDRAWER_OFFSET
from guardian.protocols.views import ViewPlugin
from guardian.security.transport_policy import Operation

_MAX_ACCOUNTS = 32
_OFFSET = {"staker": STAKE_STAKER_OFFSET, "withdrawer": STAKE_WITHDRAWER_OFFSET}


def program_accounts_operation(authority: str, offset: int) -> Operation:
    """getProgramAccounts memcmp. Only the documented staker and withdrawer offsets."""
    canonical = _solana(authority)
    if canonical is None or offset not in _OFFSET.values():
        raise ValueError("not_a_read")
    return solana_rpc("getProgramAccounts", [
        STAKE_PROGRAM,
        {
            "commitment": "confirmed",
            "encoding": "jsonParsed",
            "filters": [{"memcmp": {"bytes": canonical, "offset": offset}}],
        },
    ])


class SolanaStake(ViewPlugin):
    plugin_id = "solana_stake"
    adapters = frozenset({"solana_rpc"})
    networks = frozenset({"solana-mainnet"})
    specs: dict[str, Any] = {}

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
        declared = contract_address(manifest, network, "stake_program")
        if declared != STAKE_PROGRAM:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="contract_mismatch",
            )
        accounts, authority, error = _selection(private)
        if error == "stake_accounts_not_provided":
            # No caller-supplied accounts: the monitored wallet itself is the
            # authority. Read both staker and withdrawer matches (read-only gPA).
            own = _solana(wallet)
            if own is not None:
                return _from_wallet(self.plugin_id, ctx, wallet_id, network, prior, own)
        if error:
            return unknown_position(
                plugin_id=self.plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason=error,
            )
        if accounts:
            return _from_accounts(self.plugin_id, ctx, wallet_id, network, prior, accounts)
        assert authority is not None
        return _from_program(self.plugin_id, ctx, wallet_id, network, prior, authority)


def _selection(private: Mapping[str, Any] | None) -> tuple[list[str] | None, dict[str, str] | None, str]:
    if not isinstance(private, Mapping):
        return None, None, "stake_accounts_not_provided"
    if "stake_accounts" in private:
        raw = private.get("stake_accounts")
        if not isinstance(raw, list):
            return None, None, "malformed_stake_accounts"
        if len(raw) > _MAX_ACCOUNTS:
            return None, None, "result_exceeds_bound"
        accounts: list[str] = []
        for item in raw:
            address = _solana(item)
            if address is None:
                return None, None, "bad_address"
            accounts.append(address)
        if accounts:
            return accounts, None, ""
    authority = private.get("stake_authority")
    if isinstance(authority, Mapping):
        role = authority.get("role")
        address = _solana(authority.get("address"))
        if role not in _OFFSET or address is None:
            return None, None, "bad_address"
        return None, {"address": address, "role": str(role)}, ""
    return None, None, "stake_accounts_not_provided"


def _from_accounts(
    plugin_id: str,
    ctx: Any,
    wallet_id: str,
    network: str,
    prior: Mapping[str, Any],
    accounts: list[str],
) -> PositionObservation:
    read = ctx.solana_accounts(accounts)
    if not read.ok:
        return _failed(plugin_id, wallet_id, network, prior, read.reason)
    values = _value_list(read.result, len(accounts))
    if values is None:
        return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
    slot = _slot(read.result)
    parsed: list[tuple[str, int, tuple[tuple[str, str], ...]]] = []
    for address, value in zip(accounts, values):
        if value is None:
            continue
        item, error = _parse_account(value)
        if error:
            return _failed(plugin_id, wallet_id, network, prior, error)
        assert item is not None
        lamports, detail = item
        if lamports > 0:
            parsed.append((address, lamports, detail))
    return _observation(plugin_id, wallet_id, network, ctx.clock(), slot, parsed)


def _from_wallet(
    plugin_id: str,
    ctx: Any,
    wallet_id: str,
    network: str,
    prior: Mapping[str, Any],
    address: str,
) -> PositionObservation:
    """Discover stake accounts where the wallet is the staker or the withdrawer."""
    seen: dict[str, Mapping[str, Any]] = {}
    for role in ("staker", "withdrawer"):
        try:
            read = ctx.solana_program_accounts(
                STAKE_PROGRAM, memcmp_offset=_OFFSET[role], memcmp_bytes=address,
            )
        except ValueError:
            return _failed(plugin_id, wallet_id, network, prior, "not_a_read")
        if not read.ok:
            return _failed(plugin_id, wallet_id, network, prior, read.reason)
        if not isinstance(read.result, list):
            return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
        for row in read.result:
            if not isinstance(row, Mapping):
                return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
            pubkey = _solana(row.get("pubkey"))
            if pubkey is None:
                return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
            seen.setdefault(pubkey, row)
        if len(seen) > _MAX_ACCOUNTS:
            return unknown_position(
                plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="result_exceeds_bound",
            )
    return _from_rows(plugin_id, ctx, wallet_id, network, prior, list(seen.values()))


def _from_program(
    plugin_id: str,
    ctx: Any,
    wallet_id: str,
    network: str,
    prior: Mapping[str, Any],
    authority: Mapping[str, str],
) -> PositionObservation:
    try:
        read = ctx.solana_program_accounts(
            STAKE_PROGRAM,
            memcmp_offset=_OFFSET[authority["role"]],
            memcmp_bytes=authority["address"],
        )
    except ValueError:
        return _failed(plugin_id, wallet_id, network, prior, "not_a_read")
    if not read.ok:
        return _failed(plugin_id, wallet_id, network, prior, read.reason)
    if not isinstance(read.result, list):
        return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
    if len(read.result) > _MAX_ACCOUNTS:
        return unknown_position(
            plugin_id=plugin_id, wallet_id=wallet_id, network=network, prior=prior, reason="result_exceeds_bound",
        )
    return _from_rows(plugin_id, ctx, wallet_id, network, prior, list(read.result))


def _from_rows(
    plugin_id: str,
    ctx: Any,
    wallet_id: str,
    network: str,
    prior: Mapping[str, Any],
    rows: list[Any],
) -> PositionObservation:
    parsed: list[tuple[str, int, tuple[tuple[str, str], ...]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
        pubkey = _solana(row.get("pubkey"))
        account = row.get("account")
        if pubkey is None or not isinstance(account, Mapping):
            return _failed(plugin_id, wallet_id, network, prior, "malformed_result")
        item, error = _parse_account(account)
        if error:
            return _failed(plugin_id, wallet_id, network, prior, error)
        assert item is not None
        lamports, detail = item
        if lamports > 0:
            parsed.append((pubkey, lamports, detail))
    return _observation(plugin_id, wallet_id, network, ctx.clock(), None, parsed)


def _observation(
    plugin_id: str,
    wallet_id: str,
    network: str,
    clock: int,
    slot: int | None,
    parsed: list[tuple[str, int, tuple[tuple[str, str], ...]]],
) -> PositionObservation:
    native = native_identity(network)
    decimals = native_decimals(network)
    legs: list[PositionLeg] = []
    for address, lamports, detail in parsed:
        legs.append(PositionLeg(
            kind="receipt",
            identity=address,
            raw_amount=lamports,
            decimals=decimals,
            network=network,
            plugin_id=plugin_id,
            underlying_identity=native,
            detail=detail,
        ))
        if native is not None:
            legs.append(PositionLeg(
                kind="underlying",
                identity=native,
                raw_amount=lamports,
                decimals=decimals,
                network=network,
                plugin_id=plugin_id,
                underlying_identity=native,
                duplicates=address,
                detail=(("role", "quote"),),
            ))
    cursor = f"slot:{slot}" if slot is not None else f"clock:{clock}"
    return PositionObservation(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        status="observed" if legs else "none_observed",
        legs=tuple(legs),
        cursor=cursor,
        metrics=(("commitment", "confirmed"),),
    )


def _parse_account(value: Mapping[str, Any]) -> tuple[tuple[int, tuple[tuple[str, str], ...]] | None, str]:
    lamports = _chain_int(value.get("lamports"))
    if lamports is None:
        return None, "malformed_result"
    if value.get("owner") != STAKE_PROGRAM:
        return None, "not_stake_account"
    if value.get("executable") not in {False, None}:
        return None, "not_stake_account"
    data = value.get("data")
    if not isinstance(data, Mapping):
        return None, "malformed_result"
    parsed = data.get("parsed")
    if not isinstance(parsed, Mapping):
        return None, "malformed_result"
    info = parsed.get("info")
    if not isinstance(info, Mapping):
        return None, "malformed_result"
    detail: list[tuple[str, str]] = []
    stake = info.get("stake")
    if stake is not None:
        if not isinstance(stake, Mapping):
            return None, "malformed_result"
        delegation = stake.get("delegation")
        if delegation is not None:
            if not isinstance(delegation, Mapping):
                return None, "malformed_result"
            if "stake" in delegation:
                delegated = _chain_int(delegation.get("stake"))
                if delegated is None:
                    return None, "malformed_result"
                if delegated > lamports:
                    return None, "delegation_exceeds_lamports"
                detail.append(("delegated_stake", str(delegated)))
            for key, out in (("activationEpoch", "activation_epoch"), ("deactivationEpoch", "deactivation_epoch")):
                if key in delegation:
                    epoch = _chain_int(delegation.get(key))
                    if epoch is None:
                        return None, "malformed_result"
                    detail.append((out, str(epoch)))
    return (lamports, tuple(detail)), ""


def _value_list(result: Any, count: int) -> list[Any] | None:
    if not isinstance(result, Mapping) or "value" not in result:
        return None
    value = result.get("value")
    if count == 1:
        return [value]
    if not isinstance(value, list) or len(value) != count:
        return None
    return list(value)


def _slot(result: Any) -> int | None:
    if not isinstance(result, Mapping):
        return None
    context = result.get("context")
    if not isinstance(context, Mapping):
        return None
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        return None
    return slot


def _chain_int(value: Any) -> int | None:
    """Accept an integer or a digit string. Floats are not amounts."""
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _solana(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_address("solana", value).canonical
    except IdentityError:
        return None


def _failed(plugin_id: str, wallet_id: str, network: str, prior: Mapping[str, Any], detail: str) -> PositionObservation:
    return unknown_position(
        plugin_id=plugin_id,
        wallet_id=wallet_id,
        network=network,
        prior=prior,
        reason="read_failed",
        detail=detail or "read_error",
    )

"""Bounded TRON read. Not "TRON fully supported".

Direct TRX, solidified liquid balance, configured TRC-20 balances, and
configured allowance pairs. Account permissions are counts only. Staking,
resources, internal transfers, TRC-10, activation, and account-key updates
stay out of scope. Nothing here signs, builds, or broadcasts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from guardian.chains.tron.abi import abi_address_word, assert_read_selector
from guardian.coverage import CoverageObservation
from guardian.events import ChainEvent, finality_for
from guardian.identities import IdentityError, parse_address
from guardian.sources.tron_grid import FROZEN_FILTERS, TronGridClient, resume_fingerprint

_TXID = re.compile(r"^[0-9a-fA-F]{64}$")
_BLOCK_ID = re.compile(r"^[0-9a-fA-F]{64}$")
_FAIL = frozenset({"FAILED", "REVERT", "OUT_OF_ENERGY", "OUT_OF_TIME", "TRANSFER_FAILED"})
_ACTIVATION = frozenset({"AccountCreateContract"})
_KEY_UPDATES = frozenset({"AccountPermissionUpdateContract", "AccountUpdateContract", "SetAccountIdContract"})
_RESOURCE = frozenset({
    "FreezeBalanceContract",
    "UnfreezeBalanceContract",
    "FreezeBalanceV2Contract",
    "UnfreezeBalanceV2Contract",
    "DelegateResourceContract",
    "UnDelegateResourceContract",
    "CancelAllUnfreezeV2Contract",
    "VoteWitnessContract",
    "WithdrawBalanceContract",
    "WithdrawExpireUnfreezeContract",
})
_HISTORY_GAPS = (
    "internal_transfers_not_in_scope",
    "resource_accounting_not_in_scope",
    "staking_not_in_scope",
)
_DECLARED_NATIVE = "solidified_liquid_trx_not_staked_or_resources"
_DECLARED_TRX = "only_confirmed_trx_transfers"
_DECLARED_TRC20 = "trc20_typed_events"
_DECLARED_TOKENS = "configured_tokens_only"
_DECLARED_PAIRS = "configured_pairs_only"
_DECLARED_PERMISSIONS = "account_permissions_observation"
_MAX_PAIRS = 8


@dataclass(frozen=True)
class TronRead:
    native_sun: int | None
    observations: dict[str, CoverageObservation]
    events: tuple[ChainEvent, ...]
    cursors: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _Head:
    number: int
    block_id: str


@dataclass(frozen=True)
class _Verdict:
    kind: str
    block: int | None
    fee: int
    finality: str
    tx: Mapping[str, Any] | None


@dataclass
class _Verify:
    client: TronGridClient
    cache: dict[str, _Verdict]
    failed: bool
    lag: bool
    fees: set[str]
    failures: set[str]


def read_tron_account(
    transport: Any,
    source_id: str,
    address: str,
    entry: Mapping[str, Any],
    *,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    contracts: tuple[str, ...] = (),
    spenders: tuple[str, ...] = (),
    native_cursor: str | None = None,
    trc20_cursor: str | None = None,
) -> TronRead:
    """One watch address. Large balances stay integers. Failed reads are not zero."""
    parsed = parse_address("tron", address)
    network = entry.get("network") if isinstance(entry.get("network"), str) else "tron-mainnet"
    asset = _native_asset(entry)
    chain_uid = _chain_uid(entry, network)
    client = TronGridClient(transport, source_id)
    solidity_head, latest_head, solidity_ready = _heads(client)
    account_body, account_reason, account_solid = _load_account(client, parsed.canonical, solidity_ready)
    native_sun, balance = _native_observation(
        account_body, account_reason, account_solid, solidity_head, latest_head, source_id, entry,
    )
    permissions = _permission_observation(account_body, account_reason, source_id)
    fungible = _token_balances(client, parsed.canonical, contracts, solidity_head, source_id)
    verify = _Verify(client, {}, False, False, set(), set())
    native_events, native_obs, native_cursor_row = _native_history(
        client, verify, parsed.canonical, parsed.identity_key, native_cursor, entry, network, asset,
        chain_uid, tenant_id, wallet_id, observed_at, source_id, solidity_head, latest_head,
    )
    trc20_events, trc20_obs, trc20_cursor_row = _trc20_history(
        client, verify, parsed.canonical, parsed.identity_key, trc20_cursor, entry, network, asset,
        chain_uid, tenant_id, wallet_id, observed_at, source_id, solidity_head, latest_head,
    )
    allowance = _allowances(client, parsed.canonical, contracts, spenders, solidity_head, source_id)
    cursors = tuple(row for row in (native_cursor_row, trc20_cursor_row) if row is not None)
    return TronRead(
        native_sun,
        {
            "balances.native": balance,
            "balances.fungible": fungible,
            "history.native_external": native_obs,
            "history.fungible": trc20_obs,
            "permissions.tron_account": permissions,
            "permissions.erc20": allowance,
        },
        native_events + trc20_events,
        cursors,
    )


def _heads(client: TronGridClient) -> tuple[_Head | None, _Head | None, bool]:
    solidity = _head(client.now_block(solidity=True))
    latest = _head(client.now_block(solidity=False))
    return solidity, latest, solidity is not None


def _head(got: Any) -> _Head | None:
    if not got.ok or not isinstance(got.body, dict):
        return None
    header = got.body.get("block_header")
    raw = header.get("raw_data") if isinstance(header, dict) else None
    number = _uint(raw.get("number")) if isinstance(raw, dict) else None
    block_id = got.body.get("blockID")
    if number is None or not isinstance(block_id, str) or _BLOCK_ID.fullmatch(block_id) is None:
        return None
    return _Head(number, block_id.lower())


def _load_account(client: TronGridClient, address: str, solidity_ready: bool) -> tuple[Any, str, bool]:
    if solidity_ready:
        got = client.account(address, solidity=True)
        if got.ok:
            return got.body, "", True
        if got.reason == "bad_address":
            return None, got.reason, False
    got = client.account(address, solidity=False)
    if got.ok:
        return got.body, "latest_not_solidified", False
    return None, got.reason or "read_error", False


def _native_observation(
    body: Any,
    reason: str,
    solidity: bool,
    solidity_head: _Head | None,
    latest_head: _Head | None,
    source_id: str,
    entry: Mapping[str, Any],
) -> tuple[int | None, CoverageObservation]:
    evidence = _head_evidence(entry, solidity_head, latest_head)
    if reason and reason != "latest_not_solidified":
        return None, _error(source_id, reason, gaps=_HISTORY_GAPS[:0])
    kind, amount = _account_balance(body)
    if kind == "malformed":
        return None, _error(source_id, "malformed_account", evidence=evidence)
    latest_only = not solidity or reason == "latest_not_solidified"
    codes: list[str] = []
    category = "ok"
    if kind == "unactivated":
        category = "partial"
        codes.append("unactivated_account")
    if latest_only:
        category = "partial"
        codes.append("latest_not_solidified")
        evidence.append("balance_source:wallet")
    else:
        evidence.append("balance_source:walletsolidity")
    head = solidity_head if not latest_only else None
    return amount, _obs(
        category,
        source_id,
        declared=_DECLARED_NATIVE,
        complete=category == "ok",
        evidence=tuple(evidence),
        reasons=tuple(codes),
        number=None if head is None else str(head.number),
        block_hash=None if head is None else head.block_id,
        status="finalized" if head is not None else "observed",
    )


def _account_balance(body: Any) -> tuple[str, int | None]:
    if not isinstance(body, dict):
        return "malformed", None
    if not body:
        return "unactivated", 0
    if "balance" not in body:
        return "malformed", None
    amount = _uint(body.get("balance"))
    if amount is None:
        return "malformed", None
    return "amount", amount


def _permission_observation(body: Any, reason: str, source_id: str) -> CoverageObservation:
    gaps = ("account_keys_not_handled",)
    if reason and reason != "latest_not_solidified":
        return _error(source_id, reason, gaps=gaps, declared=_DECLARED_PERMISSIONS)
    if not isinstance(body, dict) or _account_balance(body)[0] == "malformed":
        return _error(source_id, "malformed_account", gaps=gaps, declared=_DECLARED_PERMISSIONS)
    counts = _permission_counts(body)
    if counts is None:
        return _error(source_id, "malformed_account", gaps=gaps, declared=_DECLARED_PERMISSIONS)
    threshold, keys, active = counts
    evidence = (f"owner_threshold:{threshold}", f"owner_key_count:{keys}", f"active_permission_count:{active}")
    reasons = ["account_keys_not_handled"]
    if not body:
        reasons.append("unactivated_account")
    if reason == "latest_not_solidified":
        reasons.append("latest_not_solidified")
    return _obs(
        "partial",
        source_id,
        declared=_DECLARED_PERMISSIONS,
        complete=False,
        gaps=gaps,
        evidence=evidence,
        reasons=tuple(reasons),
    )


def _permission_counts(body: Mapping[str, Any]) -> tuple[int, int, int] | None:
    owner = body.get("owner_permission")
    if owner is None:
        threshold, key_count = 0, 0
    elif not isinstance(owner, dict):
        return None
    else:
        threshold_raw = owner.get("threshold", 0)
        keys = owner.get("keys", [])
        threshold = _uint(threshold_raw)
        if threshold is None or not isinstance(keys, list):
            return None
        key_count = len(keys)
    active = body.get("active_permission")
    if active is None:
        active_count = 0
    elif not isinstance(active, list):
        return None
    else:
        active_count = len(active)
    return threshold, key_count, active_count


def _token_balances(
    client: TronGridClient,
    owner: str,
    contracts: tuple[str, ...],
    solidity_head: _Head | None,
    source_id: str,
) -> CoverageObservation:
    chosen = contracts[:_MAX_PAIRS]
    if not chosen:
        return _obs(
            "partial", source_id, declared=_DECLARED_TOKENS, complete=False,
            reasons=("no_configured_tokens",),
        )
    evidence: list[str] = []
    latest = False
    failed = False
    for contract in chosen:
        amount, solidity, ok = _constant_uint(client, owner, contract, "balanceOf(address)", (owner,))
        identity = parse_address("tron", contract).identity_key
        if not ok or amount is None:
            failed = True
            evidence.append(f"token_balance:{identity}:unreadable")
            continue
        latest = latest or not solidity
        evidence.append(f"token_balance:{identity}:{amount}")
    if failed:
        return _obs(
            "partial", source_id, declared=_DECLARED_TOKENS, complete=False,
            evidence=tuple(evidence), reasons=("token_balance_unreadable",),
        )
    reasons = ("latest_not_solidified",) if latest or solidity_head is None else ()
    head = None if reasons else solidity_head
    return _obs(
        "partial" if reasons else "ok",
        source_id,
        declared=_DECLARED_TOKENS,
        complete=not reasons,
        evidence=tuple(evidence),
        reasons=reasons,
        number=None if head is None else str(head.number),
        block_hash=None if head is None else head.block_id,
        status="finalized" if head is not None else "observed",
    )


def _allowances(
    client: TronGridClient,
    owner: str,
    contracts: tuple[str, ...],
    spenders: tuple[str, ...],
    solidity_head: _Head | None,
    source_id: str,
) -> CoverageObservation:
    del solidity_head
    pairs = [(contract, spender) for contract in contracts for spender in spenders][:_MAX_PAIRS]
    gaps = ("approval_history_not_indexed",)
    if not pairs:
        return _obs(
            "partial", source_id, declared=_DECLARED_PAIRS, complete=False,
            gaps=gaps, reasons=("no_configured_pairs", "approval_history_not_indexed"),
        )
    evidence: list[str] = []
    reasons = ["configured_pairs_only", "approval_history_not_indexed"]
    for contract, spender in pairs:
        amount, solidity, ok = _constant_uint(
            client, owner, contract, "allowance(address,address)", (owner, spender),
        )
        token_id = parse_address("tron", contract).identity_key
        spender_id = parse_address("tron", spender).identity_key
        if not ok or amount is None:
            evidence.append(f"allowance:{token_id}:{spender_id}:unreadable")
            reasons.append("allowance_unreadable")
            continue
        if not solidity:
            reasons.append("latest_not_solidified")
        evidence.append(f"allowance:{token_id}:{spender_id}:{amount}")
    return _obs(
        "partial", source_id, declared=_DECLARED_PAIRS, complete=False,
        gaps=gaps, evidence=tuple(evidence), reasons=tuple(dict.fromkeys(reasons)),
    )


def _constant_uint(
    client: TronGridClient,
    owner: str,
    contract: str,
    selector: str,
    args: tuple[str, ...],
) -> tuple[int | None, bool, bool]:
    assert_read_selector(selector)
    parameter = "".join(abi_address_word(item) for item in args)
    body = {
        "contract_address": contract,
        "function_selector": selector,
        "owner_address": owner,
        "parameter": parameter,
        "visible": True,
    }
    got = client.trigger_constant(body, solidity=True)
    if got.reason == "selector_rejected":
        return None, True, False
    solidity = True
    if not got.ok:
        got = client.trigger_constant(body, solidity=False)
        solidity = False
    if not got.ok:
        return None, solidity, False
    if _constant_rejected(got.body):
        return None, solidity, False
    amount = _decode_uint(got.body)
    if amount is None:
        return None, solidity, False
    return amount, solidity, True


def _constant_rejected(body: Any) -> bool:
    if not isinstance(body, dict):
        return True
    result = body.get("result")
    return isinstance(result, dict) and result.get("result") is False


def _decode_uint(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    result = body.get("result")
    if not isinstance(result, dict) or result.get("result") is not True:
        return None
    words = body.get("constant_result")
    if not isinstance(words, list) or not words or not isinstance(words[0], str):
        return None
    text = words[0].strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or len(text) > 64 or any(char not in "0123456789abcdef" for char in text):
        return None
    return int(text, 16)


def _native_history(
    client, verify, address, watch, cursor, entry, network, asset, chain_uid, tenant_id, wallet_id,
    observed_at, source_id, solidity_head, latest_head,
):
    events: list[ChainEvent] = []
    reasons: list[str] = []
    gaps = list(_HISTORY_GAPS)
    try:
        resume = resume_fingerprint(cursor)
    except ValueError:
        observation = _history_observation(source_id, False, "bad_cursor", gaps, reasons, None, solidity_head, _DECLARED_TRX, hard=True)
        return (), observation, None
    path = f"/v1/accounts/{address}/transactions"
    walk = client.history(path, FROZEN_FILTERS, resume)
    if walk.reason:
        reasons.append(walk.reason)
        if walk.reason in {"page_cap", "repeated_fingerprint"}:
            gaps.append(walk.reason)
    if not walk.error:
        for row in walk.rows:
            _native_row(
                row, verify, watch, entry, network, asset, chain_uid, tenant_id, wallet_id,
                observed_at, source_id, solidity_head, latest_head, events, reasons, gaps,
            )
    complete = walk.complete and not walk.error and not verify.failed
    if verify.lag:
        reasons.append("indexer_lag")
    if verify.failed:
        reasons.append("verification_unread")
    hard = bool(walk.error and not walk.rows)
    observation = _history_observation(
        source_id, complete, "", gaps, reasons, _cursor_token(walk, complete, solidity_head),
        solidity_head, _DECLARED_TRX, hard=hard,
    )
    stored = _stored_cursor(wallet_id, network, "history.native_external", observation.cursor, solidity_head, walk.error and not walk.rows)
    return tuple(events), observation, stored


def _native_row(
    row, verify, watch, entry, network, asset, chain_uid, tenant_id, wallet_id, observed_at, source_id,
    solidity_head, latest_head, events, reasons, gaps,
) -> None:
    kind = _contract_type(row)
    if kind in _ACTIVATION:
        reasons.append("activation_refused")
        return
    if kind in _KEY_UPDATES:
        reasons.append("account_key_update_refused")
        return
    if kind in _RESOURCE or kind == "TransferAssetContract" or kind not in {"TransferContract", "TriggerSmartContract"}:
        if kind == "TransferAssetContract":
            gaps.append("trc10_not_in_scope")
        return
    if kind == "TriggerSmartContract" and (_uint(_param(row).get("call_value")) or 0) <= 0:
        return
    txid = _row_id(row)
    if txid is None:
        reasons.append("indexer_lag")
        verify.lag = True
        return
    verdict = _verdict(verify, txid, _uint(row.get("blockNumber")), entry, network, solidity_head, latest_head)
    _emit_verified(
        verdict, txid, "native", watch, None, asset, chain_uid, tenant_id, wallet_id, network,
        observed_at, source_id, events, verify,
    )


def _trc20_history(
    client, verify, address, watch, cursor, entry, network, asset, chain_uid, tenant_id, wallet_id,
    observed_at, source_id, solidity_head, latest_head,
):
    events: list[ChainEvent] = []
    reasons: list[str] = []
    gaps = ["approval_is_not_a_transfer", *_HISTORY_GAPS]
    try:
        resume = resume_fingerprint(cursor)
    except ValueError:
        observation = _history_observation(source_id, False, "bad_cursor", gaps, reasons, None, solidity_head, _DECLARED_TRC20, hard=True)
        return (), observation, None
    path = f"/v1/accounts/{address}/transactions/trc20"
    walk = client.history(path, FROZEN_FILTERS, resume)
    if walk.reason:
        reasons.append(walk.reason)
        if walk.reason in {"page_cap", "repeated_fingerprint"}:
            gaps.append(walk.reason)
    if not walk.error:
        for index, row in enumerate(walk.rows):
            _trc20_row(
                row, index, verify, watch, entry, network, asset, chain_uid, tenant_id, wallet_id,
                observed_at, source_id, solidity_head, latest_head, events, reasons, gaps,
            )
    complete = walk.complete and not walk.error and not verify.failed
    if verify.lag:
        reasons.append("indexer_lag")
    if verify.failed:
        reasons.append("verification_unread")
    hard = bool(walk.error and not walk.rows)
    observation = _history_observation(
        source_id, complete, "", gaps, reasons, _cursor_token(walk, complete, solidity_head),
        solidity_head, _DECLARED_TRC20, hard=hard,
    )
    stored = _stored_cursor(wallet_id, network, "history.fungible", observation.cursor, solidity_head, walk.error and not walk.rows)
    return tuple(events), observation, stored


def _trc20_row(
    row, index, verify, watch, entry, network, asset, chain_uid, tenant_id, wallet_id, observed_at,
    source_id, solidity_head, latest_head, events, reasons, gaps,
) -> None:
    label = row.get("type")
    kind = label.casefold() if isinstance(label, str) else ""
    if kind not in {"transfer", "approval"}:
        gaps.append("unknown_trc20_type")
        return
    token = row.get("token_info")
    token_addr = token.get("address") if isinstance(token, dict) else None
    token_id = _party(token_addr)
    amount = _uint(row.get("value"))
    if token_id is None or amount is None:
        reasons.append("bad_trc20_row")
        return
    txid = _row_id(row)
    if txid is None:
        verify.lag = True
        reasons.append("indexer_lag")
        return
    verdict = _verdict(verify, txid, _uint(row.get("blockNumber") or row.get("block")), entry, network, solidity_head, latest_head)
    token_asset = f"tron-trc20:{token_id}"
    movement = "approval" if kind == "approval" else "transfer"
    _emit_verified(
        verdict, txid, movement, watch, (row, index, token_asset, amount), asset, chain_uid,
        tenant_id, wallet_id, network, observed_at, source_id, events, verify,
    )


def _verdict(verify, txid, list_block, entry, network, solidity_head, latest_head) -> _Verdict:
    key = txid.lower()
    cached = verify.cache.get(key)
    if cached is not None:
        return cached
    tx_got = verify.client.transaction_by_id(txid, info=False)
    info_got = verify.client.transaction_by_id(txid, info=True)
    if not tx_got.ok or not info_got.ok:
        verify.failed = True
        verdict = _Verdict("unread", None, 0, "unknown", None)
        verify.cache[key] = verdict
        return verdict
    tx = _tx_body(tx_got.body)
    info = info_got.body if isinstance(info_got.body, dict) else {}
    if not _has_id(tx, key) or not _has_id(info, key):
        verify.lag = True
        verdict = _Verdict("lag", None, 0, "unknown", None)
        verify.cache[key] = verdict
        return verdict
    codes = _status_codes(tx, info)
    fee = _fee(info)
    block = _uint(info.get("blockNumber"))
    finality = _finality(block, entry, network, solidity_head, latest_head)
    if codes & _FAIL:
        verdict = _Verdict("failed", block, fee, finality, tx)
    elif _execution_success(tx, info) and _blocks_agree(tx, info, list_block) and finality == "finalized":
        verdict = _Verdict("success", block, fee, finality, tx)
    else:
        verdict = _Verdict("not_success", block, fee, finality, tx)
    verify.cache[key] = verdict
    return verdict


def _emit_verified(
    verdict, txid, movement, watch, trc20, native_asset, chain_uid, tenant_id, wallet_id, network,
    observed_at, source_id, events, verify: _Verify,
) -> None:
    if verdict.kind not in {"success", "failed"}:
        return
    if verdict.kind == "failed":
        _emit_failed(txid, verdict, native_asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events, verify.failures)
        _emit_fee(txid, verdict, native_asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events, verify.fees)
        return
    _emit_fee(txid, verdict, native_asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events, verify.fees)
    if movement == "native":
        _emit_native_asset(verdict, txid, watch, native_asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events)
        return
    if movement == "approval":
        _emit_approval(trc20, txid, verdict, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events)
        return
    _emit_trc20_transfer(trc20, txid, watch, verdict, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events)


def _emit_native_asset(verdict, txid, watch, asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events) -> None:
    tx = verdict.tx or {}
    kind = _contract_type(tx)
    value = _param(tx)
    if kind == "TransferContract":
        amount = _uint(value.get("amount"))
        owner = _party(value.get("owner_address"))
        target = _party(value.get("to_address"))
    elif kind == "TriggerSmartContract":
        amount = _uint(value.get("call_value"))
        owner = _party(value.get("owner_address"))
        target = None
    else:
        return
    if amount is None or amount <= 0:
        return
    if owner == watch:
        events.append(_event(tenant_id, wallet_id, network, chain_uid, txid, "0", "asset_out", asset, amount, verdict, observed_at, source_id))
    elif target == watch:
        events.append(_event(tenant_id, wallet_id, network, chain_uid, txid, "0", "asset_in", asset, amount, verdict, observed_at, source_id))


def _emit_trc20_transfer(payload, txid, watch, verdict, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events) -> None:
    row, index, token_asset, amount = payload
    if amount <= 0:
        return
    sender = _party(row.get("from"))
    target = _party(row.get("to"))
    if sender == watch:
        kind = "asset_out"
    elif target == watch:
        kind = "asset_in"
    else:
        return
    events.append(_event(
        tenant_id, wallet_id, network, chain_uid, txid, f"trc20-{index}", kind, token_asset, amount,
        verdict, observed_at, source_id,
    ))


def _emit_approval(payload, txid, verdict, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events) -> None:
    row, index, token_asset, amount = payload
    if _party(row.get("from")) is None:
        return
    kind = "approval_revoked" if amount == 0 else "approval_set"
    events.append(_event(
        tenant_id, wallet_id, network, chain_uid, txid, f"approval-{index}", kind, token_asset, amount,
        verdict, observed_at, source_id,
    ))


def _emit_fee(txid, verdict, asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events, seen) -> None:
    if verdict.fee <= 0 or txid.lower() in seen:
        return
    seen.add(txid.lower())
    events.append(_event(
        tenant_id, wallet_id, network, chain_uid, txid, "fee", "fee", asset, verdict.fee, verdict, observed_at, source_id,
    ))


def _emit_failed(txid, verdict, asset, chain_uid, tenant_id, wallet_id, network, observed_at, source_id, events, seen) -> None:
    if txid.lower() in seen:
        return
    seen.add(txid.lower())
    events.append(_event(
        tenant_id, wallet_id, network, chain_uid, txid, "status", "tx_failed", asset, 0, verdict, observed_at, source_id,
    ))


def _event(tenant_id, wallet_id, network, chain_uid, txid, sub_index, kind, asset, amount, verdict, observed_at, source_id) -> ChainEvent:
    return ChainEvent(
        tenant_id=tenant_id,
        wallet_id=wallet_id,
        network=network,
        chain_uid=chain_uid,
        tx_id=txid.lower(),
        sub_index=sub_index,
        kind=kind,
        asset=asset,
        raw_amount=str(amount),
        block_number=None if verdict.block is None else str(verdict.block),
        block_hash=None,
        tx_status="failed" if verdict.kind == "failed" else "success",
        finality=verdict.finality if verdict.finality in {"observed", "confirmed", "finalized", "orphaned", "unknown"} else "unknown",
        source_ids=(source_id,),
        observed_at=observed_at,
    )


def _history_observation(source_id, complete, reason, gaps, reasons, cursor, head, declared, *, hard: bool = False) -> CoverageObservation:
    codes = [item for item in reasons if item]
    if reason and reason not in codes:
        codes.append(reason)
    unique_gaps = tuple(dict.fromkeys(gaps))
    category = "error" if hard else "partial"
    return _obs(
        category,
        source_id,
        declared=declared,
        complete=False,
        pagination=complete,
        gaps=unique_gaps,
        evidence=(),
        reasons=tuple(dict.fromkeys(codes)),
        cursor=cursor,
        number=None if head is None else str(head.number),
        block_hash=None if head is None else head.block_id,
        status="finalized" if head is not None and complete else "observed",
    )


def _cursor_token(walk, complete: bool, head: _Head | None) -> str | None:
    if walk.error and not walk.rows:
        return None
    if complete and head is not None:
        return f"solidified:{head.number}"
    if walk.cursor:
        return f"fp:{walk.cursor}"
    return None


def _stored_cursor(wallet_id, network, capability, cursor, head, blocked: bool) -> dict[str, Any] | None:
    if blocked or not cursor:
        return None
    return {
        "block_number": None if head is None else str(head.number),
        "capability": capability,
        "cursor": cursor,
        "network": network,
        "wallet_id": wallet_id,
    }


def _tx_body(body: Any) -> Mapping[str, Any]:
    if not isinstance(body, dict):
        return {}
    nested = body.get("transaction")
    if isinstance(nested, dict):
        return nested
    return body


def _has_id(body: Mapping[str, Any], txid: str) -> bool:
    return txid in _ids(body)


def _ids(body: Mapping[str, Any]) -> set[str]:
    found = set()
    for key in ("txID", "tx_id", "id", "transaction_id"):
        value = body.get(key)
        if isinstance(value, str) and _TXID.fullmatch(value):
            found.add(value.lower())
    return found


def _row_id(row: Mapping[str, Any]) -> str | None:
    for key in ("txID", "transaction_id", "tx_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and _TXID.fullmatch(value):
            return value.lower()
    return None


def _contract_type(tx: Mapping[str, Any]) -> str:
    raw = tx.get("raw_data")
    if not isinstance(raw, dict):
        return ""
    contracts = raw.get("contract")
    if not isinstance(contracts, list) or not contracts or not isinstance(contracts[0], dict):
        return ""
    kind = contracts[0].get("type")
    return kind if isinstance(kind, str) else ""


def _param(tx: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = tx.get("raw_data")
    if not isinstance(raw, dict):
        return {}
    contracts = raw.get("contract")
    if not isinstance(contracts, list) or not contracts or not isinstance(contracts[0], dict):
        return {}
    parameter = contracts[0].get("parameter")
    if not isinstance(parameter, dict):
        return {}
    value = parameter.get("value")
    return value if isinstance(value, dict) else {}


def _status_codes(tx: Mapping[str, Any], info: Mapping[str, Any]) -> set[str]:
    found = set()
    ret = tx.get("ret")
    if isinstance(ret, list):
        for item in ret:
            if isinstance(item, dict) and isinstance(item.get("contractRet"), str):
                found.add(item["contractRet"])
    receipt = info.get("receipt")
    if isinstance(receipt, dict) and isinstance(receipt.get("result"), str):
        found.add(receipt["result"])
    if isinstance(info.get("result"), str):
        found.add(info["result"])
    return found


def _execution_success(tx: Mapping[str, Any], info: Mapping[str, Any]) -> bool:
    ret = tx.get("ret")
    if not isinstance(ret, list) or not ret or not isinstance(ret[0], dict):
        return False
    if ret[0].get("contractRet") != "SUCCESS":
        return False
    receipt = info.get("receipt")
    receipt_result = receipt.get("result") if isinstance(receipt, dict) else None
    info_result = info.get("result")
    return receipt_result in (None, "SUCCESS") and info_result in (None, "SUCCESS")


def _blocks_agree(tx: Mapping[str, Any], info: Mapping[str, Any], list_block: int | None) -> bool:
    info_block = _uint(info.get("blockNumber"))
    if info_block is None:
        return False
    tx_block = _uint(tx.get("blockNumber"))
    if tx_block is not None and tx_block != info_block:
        return False
    return list_block is None or list_block == info_block


def _fee(info: Mapping[str, Any]) -> int:
    amount = _uint(info.get("fee"))
    return 0 if amount is None else amount


def _finality(block, entry, network, solidity_head, latest_head) -> str:
    if block is None or solidity_head is None or latest_head is None:
        return "unknown"
    text = entry.get("finality_policy")
    if not isinstance(text, str) or not text.strip():
        return "unknown"
    return finality_for(network, block, latest_head.number, solidity_head.number, {
        "family": "tron",
        "finality_policy": text,
        "mode": "finalized_tag",
        "network": network,
    })


def _party(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_address("tron", value).identity_key
    except IdentityError:
        return None


def _uint(value: Any) -> int | None:
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _head_evidence(entry: Mapping[str, Any], solidity_head: _Head | None, latest_head: _Head | None) -> list[str]:
    identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
    status = identity.get("status") if isinstance(identity, dict) else None
    evidence = [f"identity_status:{status if isinstance(status, str) and status else 'to_verify_by_probe'}"]
    if solidity_head is not None:
        evidence.append(f"solidified_number:{solidity_head.number}")
    if latest_head is not None:
        evidence.append(f"latest_number:{latest_head.number}")
    return evidence


def _native_asset(entry: Mapping[str, Any]) -> str:
    native = entry.get("native_asset")
    if isinstance(native, Mapping) and isinstance(native.get("identity"), str):
        return native["identity"]
    return "tron-native:TRX"


def _chain_uid(entry: Mapping[str, Any], network: str) -> str:
    identity = entry.get("identity")
    if isinstance(identity, dict):
        expected = identity.get("expected_value")
        if isinstance(expected, str) and expected.strip():
            return expected
    return network


def _error(source_id: str, reason: str, *, gaps: tuple[str, ...] = (), declared: str = "not_read", evidence: tuple[str, ...] | list[str] = ()) -> CoverageObservation:
    return _obs(
        "error", source_id, declared=declared, complete=False, gaps=gaps,
        evidence=tuple(evidence), reasons=(reason, "blocked") if reason in {"endpoint_missing", "credential_missing"} else (reason,),
    )


def _obs(
    category: str,
    source_id: str,
    *,
    declared: str,
    complete: bool,
    gaps: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
    cursor: str | None = None,
    number: str | None = None,
    block_hash: str | None = None,
    status: str | None = None,
    pagination: bool | None = None,
) -> CoverageObservation:
    pages = complete if pagination is None else pagination
    gap_done = pages and not gaps and category in {"ok", "empty"}
    return CoverageObservation(
        category=category,
        pagination_complete=pages,
        gap_detection_complete=gap_done,
        gaps=gaps,
        cursor=cursor,
        declared=declared,
        checkpoint_kind="block" if number or block_hash else None,
        checkpoint_number=number,
        checkpoint_hash=block_hash,
        checkpoint_status=status,
        sources=(source_id,),
        trust="single_provider" if category in {"ok", "empty", "partial"} else "unverified",
        price_quality="unknown",
        evidence_refs=evidence,
        reason_codes=reasons,
    )

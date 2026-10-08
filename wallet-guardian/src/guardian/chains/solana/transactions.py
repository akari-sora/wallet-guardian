"""Parse confirmed Solana transactions. Instruction data is only decoded.

jsonParsed bodies are read. This module does not serialize, sign, simulate, or
suggest a transaction. Fee is counted once. Rent is rent. A wSOL wrap or unwrap
is one movement. Token-2022 transfer fees are explicit. Float uiAmount values
are ignored. An unknown Token-2022 extension means the report is not complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from guardian.chains.solana.adapter import (
    DEFAULT_COMMITMENT,
    SPL_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    UNKNOWN_EXTENSION,
    WRAPPED_SOL_MINT,
    SolanaSession,
    canonical_address,
    commitment_finality,
    make_observation,
    parse_raw_amount,
    transaction_operation,
)
from guardian.events import ChainEvent, effects_from_tx

_TOKEN_PROGRAMS = frozenset({SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
_FINAL = frozenset({"confirmed", "finalized", "observed", "orphaned", "unknown"})


@dataclass(frozen=True)
class InstructionView:
    program_id: str
    ix_type: str | None
    source: str
    amount: int | None
    decimals: int | None
    source_account: str | None
    destination: str | None
    delegate: str | None
    owner: str | None
    authority: str | None
    mint: str | None
    authority_type: str | None
    new_authority: str | None
    new_authority_is_null: bool
    lamports: int | None
    new_account: str | None
    account: str | None
    unparsed: bool
    amount_rejected_float: bool


@dataclass(frozen=True)
class AccountKeys:
    ordered: tuple[str, ...]
    static: tuple[str, ...]
    loaded_writable: tuple[str, ...]
    loaded_readonly: tuple[str, ...]
    alt_unresolved: bool


@dataclass(frozen=True)
class TokenDelta:
    account: str
    mint: str
    owner: str | None
    decimals: int
    pre_amount: int
    post_amount: int
    delta: int
    owner_in_account_keys: bool


@dataclass(frozen=True)
class Effect:
    kind: str
    asset: str
    raw_amount: int
    decimals: int
    direction: str
    account: str | None = None
    counterparty: str | None = None
    sent: int | None = None
    received: int | None = None
    transfer_fee: int | None = None


@dataclass(frozen=True)
class ParsedTransaction:
    signature: str
    version: str
    status: str
    finality: str
    slot: int | None
    fee_lamports: int | None
    account_keys: tuple[str, ...]
    static_keys: tuple[str, ...]
    loaded_writable: tuple[str, ...]
    loaded_readonly: tuple[str, ...]
    owner_in_account_keys: bool
    instructions: tuple[InstructionView, ...]
    token_deltas: tuple[TokenDelta, ...]
    effects: tuple[Effect, ...]
    events: tuple[ChainEvent, ...]
    complete: bool
    reasons: tuple[str, ...]
    counted_as_final: bool
    gross: int
    net: int
    inner_instruction_count: int


def fetch_transaction(
    session: SolanaSession,
    signature: str,
    *,
    commitment: str = DEFAULT_COMMITMENT,
    owner: str,
    owned_accounts: set[str] | None = None,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    unknown_extensions: Sequence[str] = (),
) -> ParsedTransaction:
    """getTransaction then parse. A miss is unknown, not success and not final."""
    try:
        operation = transaction_operation(signature, commitment)
    except ValueError as exc:
        return _unavailable(signature, commitment, session, str(exc))
    outcome = session.rpc(operation)
    if not outcome.ok:
        parsed = _unavailable(signature, commitment, session, outcome.reason or "read_error")
        return parsed
    return parse_transaction(
        outcome.result,
        entry=session.adapter.entry,
        commitment=commitment,
        owner=owner,
        owned_accounts=owned_accounts or set(),
        tenant_id=tenant_id,
        wallet_id=wallet_id,
        observed_at=observed_at,
        signature=signature,
        unknown_extensions=unknown_extensions,
    )


def parse_transaction(
    result: Mapping[str, Any] | None,
    *,
    entry: Mapping[str, Any],
    commitment: str,
    owner: str,
    owned_accounts: set[str] | None = None,
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    signature: str = "",
    unknown_extensions: Sequence[str] = (),
) -> ParsedTransaction:
    """One jsonParsed getTransaction result. Null and meta null stay unknown."""
    owned = {item for item in (owned_accounts or set()) if item}
    wallet = canonical_address(owner)
    reasons: list[str] = []
    if unknown_extensions:
        reasons.append(UNKNOWN_EXTENSION)
    if not isinstance(result, Mapping):
        return _shell(
            signature, "unknown", "unknown", commitment, entry, None, None,
            AccountKeys((), (), (), () , False), False, (), (), (), (), False,
            tuple(reasons) + ("transaction_unavailable",), 0, 0, 0,
        )
    body = result.get("transaction")
    meta = result.get("meta") if "meta" in result else None
    slot = _slot(result.get("slot"))
    version, version_reason = _version(result)
    keys = _account_keys(body if isinstance(body, Mapping) else {}, meta if isinstance(meta, Mapping) else {})
    sig = signature or _signature(body)
    owner_present = wallet is not None and wallet in keys.ordered
    if body is None or not isinstance(body, Mapping):
        reasons.append("transaction_unavailable")
        return _shell(
            sig, "unknown", "unknown", commitment, entry, slot, None, keys, owner_present,
            (), (), (), (), False, tuple(reasons), 0, 0, 0,
        )
    if meta is None or not isinstance(meta, Mapping):
        reasons.append("transaction_unavailable")
        return _shell(
            sig, version if version != "unsupported" else "unsupported", "unknown", commitment, entry,
            slot, None, keys, owner_present, (), (), (), (), False, tuple(reasons), 0, 0, 0,
        )
    if version_reason:
        reasons.append(version_reason)
        return _shell(
            sig, version, "unknown", commitment, entry, slot, None, keys, owner_present,
            (), (), (), (), False, tuple(reasons), 0, 0, 0,
        )
    if keys.alt_unresolved:
        reasons.append("alt_unresolved")
    instructions, inner_count, instruction_reasons = _instructions(body, meta)
    reasons.extend(instruction_reasons)
    fee = _fee(meta)
    status = _status(meta)
    finality = commitment_finality(entry, slot, commitment)
    if status == "failed":
        events = _events(
            entry, sig, slot, finality, status, fee, (), tenant_id, wallet_id, observed_at, meta,
        )
        fee_effects = () if fee is None else (Effect("fee", "native", fee, 9, "fee", keys.ordered[0] if keys.ordered else None),)
        return _done(
            sig, version, status, finality, slot, fee, keys, owner_present, instructions,
            (), fee_effects, events, False, tuple(dict.fromkeys([*reasons])), inner_count, 0, 0,
        )
    if status != "success":
        reasons.append("transaction_unavailable")
        return _shell(
            sig, version, "unknown", commitment, entry, slot, fee, keys, owner_present,
            instructions, (), (), (), False, tuple(dict.fromkeys(reasons)), inner_count, 0, 0,
        )
    deltas, delta_reasons = _token_deltas(meta, keys, wallet, owner_present)
    reasons.extend(delta_reasons)
    effects, effect_reasons, gross, net = _effects(meta, keys, instructions, deltas, wallet, owned, fee)
    reasons.extend(effect_reasons)
    if unknown_extensions or keys.alt_unresolved or effect_reasons or delta_reasons or instruction_reasons:
        complete = False
    else:
        complete = True
    asset_effects = _chain_effects(effects)
    events = _events(
        entry, sig, slot, finality, status, fee, asset_effects, tenant_id, wallet_id, observed_at, meta,
    )
    return _done(
        sig, version, status, finality, slot, fee, keys, owner_present, instructions,
        deltas, effects, events, complete, tuple(dict.fromkeys(reasons)), inner_count, gross, net,
    )


def resolve_account_keys(message: Mapping[str, Any], meta: Mapping[str, Any] | None) -> AccountKeys:
    """Static keys, then loaded writable, then loaded readonly."""
    return _account_keys({"message": message}, meta)


def _done(
    signature: str,
    version: str,
    status: str,
    finality: str,
    slot: int | None,
    fee: int | None,
    keys: AccountKeys,
    owner_present: bool,
    instructions: tuple[InstructionView, ...],
    deltas: tuple[TokenDelta, ...],
    effects: tuple[Effect, ...],
    events: tuple[ChainEvent, ...],
    complete: bool,
    reasons: tuple[str, ...],
    inner_count: int,
    gross: int,
    net: int,
) -> ParsedTransaction:
    counted = status == "success" and finality == "finalized" and complete
    return ParsedTransaction(
        signature=signature,
        version=version,
        status=status,
        finality=finality if finality in _FINAL else "unknown",
        slot=slot,
        fee_lamports=fee,
        account_keys=keys.ordered,
        static_keys=keys.static,
        loaded_writable=keys.loaded_writable,
        loaded_readonly=keys.loaded_readonly,
        owner_in_account_keys=owner_present,
        instructions=instructions,
        token_deltas=deltas,
        effects=effects,
        events=events,
        complete=complete and status == "success",
        reasons=reasons,
        counted_as_final=counted,
        gross=gross,
        net=net,
        inner_instruction_count=inner_count,
    )


def _shell(
    signature: str,
    version: str,
    status: str,
    commitment: str,
    entry: Mapping[str, Any],
    slot: int | None,
    fee: int | None,
    keys: AccountKeys,
    owner_present: bool,
    instructions: tuple[InstructionView, ...],
    deltas: tuple[TokenDelta, ...],
    effects: tuple[Effect, ...],
    events: tuple[ChainEvent, ...],
    complete: bool,
    reasons: tuple[str, ...],
    inner_count: int,
    gross: int,
    net: int,
) -> ParsedTransaction:
    finality = "unknown" if status != "success" else commitment_finality(entry, slot, commitment)
    if status != "success":
        finality = "unknown"
    return _done(
        signature, version, status, finality, slot, fee, keys, owner_present, instructions,
        deltas, effects, events, complete, reasons, inner_count, gross, net,
    )


def _unavailable(signature: str, commitment: str, session: SolanaSession, reason: str) -> ParsedTransaction:
    return _shell(
        signature, "unknown", "unknown", commitment, session.adapter.entry, None, None,
        AccountKeys((), (), (), (), False), False, (), (), (), (), False, (reason,), 0, 0, 0,
    )


def _version(result: Mapping[str, Any]) -> tuple[str, str]:
    version = result.get("version", "legacy")
    if version in {"legacy", "LEGACY"}:
        return "legacy", ""
    if version == 0 or version == "0":
        return "0", ""
    return "unsupported", "unsupported_transaction_version"


def _status(meta: Mapping[str, Any]) -> str:
    if "err" not in meta:
        return "unknown"
    if meta.get("err") is None:
        return "success"
    return "failed"


def _fee(meta: Mapping[str, Any]) -> int | None:
    if "fee" not in meta:
        return None
    return parse_raw_amount(meta.get("fee"))


def _slot(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _signature(body: Any) -> str:
    if not isinstance(body, Mapping):
        return ""
    signatures = body.get("signatures")
    if isinstance(signatures, list) and signatures and isinstance(signatures[0], str):
        return signatures[0]
    return ""


def _pubkey(value: Any) -> tuple[str | None, str]:
    if isinstance(value, str):
        return canonical_address(value), ""
    if isinstance(value, Mapping):
        source = value.get("source")
        source_name = source if isinstance(source, str) else ""
        return canonical_address(value.get("pubkey")), source_name
    return None, ""


def _account_keys(body: Mapping[str, Any], meta: Mapping[str, Any] | None) -> AccountKeys:
    message = body.get("message") if isinstance(body, Mapping) else None
    raw_keys = message.get("accountKeys") if isinstance(message, Mapping) else None
    static: list[str] = []
    embedded: list[str] = []
    if isinstance(raw_keys, list):
        for item in raw_keys:
            pubkey, source = _pubkey(item)
            if pubkey is None:
                continue
            if source == "lookupTable":
                embedded.append(pubkey)
            else:
                static.append(pubkey)
    writable: list[str] = []
    readonly: list[str] = []
    loaded = meta.get("loadedAddresses") if isinstance(meta, Mapping) else None
    alt_declared = False
    if isinstance(message, Mapping) and message.get("addressTableLookups"):
        alt_declared = True
    if isinstance(loaded, Mapping):
        writable = _key_list(loaded.get("writable"))
        readonly = _key_list(loaded.get("readonly"))
    elif alt_declared and not embedded:
        ordered = _dedupe([*static])
        return AccountKeys(tuple(ordered), tuple(static), (), (), True)
    ordered = _dedupe([*static, *writable, *readonly, *embedded])
    return AccountKeys(tuple(ordered), tuple(static), tuple(writable), tuple(readonly), False)


def _key_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        pubkey, _source = _pubkey(item)
        if pubkey is not None:
            found.append(pubkey)
    return found


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _instructions(
    body: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> tuple[tuple[InstructionView, ...], int, tuple[str, ...]]:
    message = body.get("message")
    outer = message.get("instructions") if isinstance(message, Mapping) else None
    if not isinstance(outer, list):
        return (), 0, ("malformed_instructions",)
    found: list[InstructionView] = []
    reasons: list[str] = []
    for item in outer:
        view, reason = _one_instruction(item, "outer")
        if view is None:
            reasons.append(reason or "malformed_instructions")
            continue
        found.append(view)
        if view.unparsed or view.amount_rejected_float:
            reasons.append("unparsed_instruction" if view.unparsed else "float_amount_rejected")
    inner_groups = meta.get("innerInstructions")
    inner_count = 0
    if inner_groups is None:
        inner_groups = []
    if not isinstance(inner_groups, list):
        return tuple(found), 0, tuple(dict.fromkeys([*reasons, "malformed_inner_instructions"]))
    for group in inner_groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("instructions"), list):
            reasons.append("malformed_inner_instructions")
            continue
        for item in group["instructions"]:
            inner_count += 1
            view, reason = _one_instruction(item, "inner")
            if view is None:
                reasons.append(reason or "malformed_inner_instructions")
                continue
            found.append(view)
            if view.unparsed or view.amount_rejected_float:
                reasons.append("unparsed_instruction" if view.unparsed else "float_amount_rejected")
    return tuple(found), inner_count, tuple(dict.fromkeys(reasons))


def _one_instruction(item: Any, source: str) -> tuple[InstructionView | None, str]:
    if not isinstance(item, Mapping):
        return None, "malformed_instructions"
    program_id = canonical_address(item.get("programId")) or ""
    parsed = item.get("parsed")
    if not isinstance(parsed, Mapping):
        return InstructionView(
            program_id, None, source, None, None, None, None, None, None, None, None,
            None, None, False, None, None, None, True, False,
        ), ""
    ix_type = parsed.get("type")
    info = parsed.get("info")
    if not isinstance(ix_type, str) or not isinstance(info, Mapping):
        return InstructionView(
            program_id, None, source, None, None, None, None, None, None, None, None,
            None, None, False, None, None, None, True, False,
        ), ""
    amount, decimals, rejected = _instruction_amount(info)
    new_authority, new_is_null = _new_authority(info)
    return InstructionView(
        program_id=program_id,
        ix_type=ix_type,
        source=source,
        amount=amount,
        decimals=decimals,
        source_account=_addr(info.get("source")),
        destination=_addr(info.get("destination")),
        delegate=_addr(info.get("delegate")),
        owner=_addr(info.get("owner")),
        authority=_addr(info.get("authority")),
        mint=_addr(info.get("mint")),
        authority_type=info.get("authorityType") if isinstance(info.get("authorityType"), str) else None,
        new_authority=new_authority,
        new_authority_is_null=new_is_null,
        lamports=parse_raw_amount(info.get("lamports")),
        new_account=_addr(info.get("newAccount")),
        account=_addr(info.get("account")),
        unparsed=False,
        amount_rejected_float=rejected,
    ), ""


def _instruction_amount(info: Mapping[str, Any]) -> tuple[int | None, int | None, bool]:
    token_amount = info.get("tokenAmount")
    if isinstance(token_amount, Mapping):
        if isinstance(token_amount.get("uiAmount"), float) and "amount" not in token_amount:
            return None, None, True
        if isinstance(token_amount.get("amount"), float):
            return None, None, True
        amount = parse_raw_amount(token_amount.get("amount"))
        decimals = token_amount.get("decimals")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
            decimals = None
        return amount, decimals, False
    if isinstance(info.get("amount"), float) or isinstance(info.get("uiAmount"), float):
        if "amount" not in info or isinstance(info.get("amount"), float):
            return None, None, True
    if "amount" in info:
        return parse_raw_amount(info.get("amount")), None, isinstance(info.get("amount"), float)
    return None, None, False


def _new_authority(info: Mapping[str, Any]) -> tuple[str | None, bool]:
    if "newAuthority" not in info:
        return None, False
    value = info.get("newAuthority")
    if value is None:
        return None, True
    return _addr(value), False


def _addr(value: Any) -> str | None:
    return canonical_address(value)


def _token_deltas(
    meta: Mapping[str, Any],
    keys: AccountKeys,
    wallet: str | None,
    owner_present: bool,
) -> tuple[tuple[TokenDelta, ...], tuple[str, ...]]:
    pre, pre_reason = _balance_map(meta.get("preTokenBalances"), keys)
    post, post_reason = _balance_map(meta.get("postTokenBalances"), keys)
    reasons = tuple(dict.fromkeys(reason for reason in (pre_reason, post_reason) if reason))
    if pre_reason or post_reason:
        return (), reasons
    indexes = set(pre) | set(post)
    deltas: list[TokenDelta] = []
    for index in sorted(indexes):
        before = pre.get(index)
        after = post.get(index)
        chosen = after or before
        if chosen is None:
            continue
        pre_amount = 0 if before is None else before[0]
        post_amount = 0 if after is None else after[0]
        if before is not None and after is not None and (before[1] != after[1] or before[2] != after[2]):
            return (), tuple(dict.fromkeys([*reasons, "token_balance_identity"]))
        account = keys.ordered[index] if index < len(keys.ordered) else ""
        deltas.append(TokenDelta(
            account=account,
            mint=chosen[1],
            owner=chosen[3],
            decimals=chosen[2],
            pre_amount=pre_amount,
            post_amount=post_amount,
            delta=post_amount - pre_amount,
            owner_in_account_keys=owner_present if chosen[3] == wallet else wallet in keys.ordered if wallet else False,
        ))
    return tuple(deltas), reasons


def _balance_map(
    rows: Any,
    keys: AccountKeys,
) -> tuple[dict[int, tuple[int, str, int, str | None]], str]:
    """index -> (raw amount, mint, decimals, owner). uiAmount is never read."""
    if rows is None:
        return {}, ""
    if not isinstance(rows, list):
        return {}, "malformed_token_balances"
    found: dict[int, tuple[int, str, int, str | None]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return {}, "malformed_token_balances"
        index = row.get("accountIndex")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(keys.ordered):
            return {}, "malformed_token_balances"
        mint = canonical_address(row.get("mint"))
        token = row.get("uiTokenAmount")
        if mint is None or not isinstance(token, Mapping):
            return {}, "malformed_token_balances"
        if isinstance(token.get("amount"), float) or (token.get("amount") is None and "uiAmount" in token):
            return {}, "float_amount_rejected"
        amount = parse_raw_amount(token.get("amount"))
        decimals = token.get("decimals")
        if amount is None or isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
            return {}, "malformed_token_balances"
        owner = canonical_address(row.get("owner"))
        found[index] = (amount, mint, decimals, owner)
    return found, ""


def _effects(
    meta: Mapping[str, Any],
    keys: AccountKeys,
    instructions: Sequence[InstructionView],
    deltas: Sequence[TokenDelta],
    wallet: str | None,
    owned: set[str],
    fee: int | None,
) -> tuple[tuple[Effect, ...], tuple[str, ...], int, int]:
    reasons: list[str] = []
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys.ordered) or len(post) != len(keys.ordered):
        return (), ("malformed_balances",), 0, 0
    lamports_pre: list[int] = []
    lamports_post: list[int] = []
    for before, after in zip(pre, post):
        left = parse_raw_amount(before)
        right = parse_raw_amount(after)
        if left is None or right is None:
            return (), ("malformed_balances",), 0, 0
        lamports_pre.append(left)
        lamports_post.append(right)
    if fee is None:
        reasons.append("fee_unknown")
        fee_paid = 0
        effects: list[Effect] = []
    else:
        fee_paid = fee
        payer = keys.ordered[0] if keys.ordered else None
        effects = [] if fee_paid == 0 else [Effect("fee", "native", fee_paid, 9, "fee", payer)]
    residual = [lamports_post[i] - lamports_pre[i] for i in range(len(keys.ordered))]
    if residual:
        residual[0] += fee_paid
    index = {key: pos for pos, key in enumerate(keys.ordered)}
    explained_token_mints: set[str] = set()
    sync_accounts = {item.account for item in instructions if item.ix_type == "syncNative" and item.account}
    closes = [item for item in instructions if item.ix_type == "closeAccount"]
    creates = [item for item in instructions if item.ix_type in {"createAccount", "createAccountWithSeed"}]

    def consume(pos: int | None, signed: int) -> None:
        if pos is None:
            reasons.append("unexplained_lamports")
            return
        residual[pos] -= signed

    for item in creates:
        dest = index.get(item.new_account or "")
        src = index.get(item.source_account or "")
        if item.lamports is None or dest is None:
            reasons.append("rent_amount_mismatch")
            continue
        minted = _mint_at(deltas, item.new_account)
        token_increase = _token_increase(deltas, item.new_account)
        if item.new_account in sync_accounts or minted == WRAPPED_SOL_MINT:
            wrap_amount = token_increase if token_increase is not None else 0
            rent = item.lamports - wrap_amount
            if rent < 0 or token_increase is None:
                reasons.append("wsol_amount_mismatch")
                continue
            if wrap_amount:
                effects.append(Effect("wrap", WRAPPED_SOL_MINT, wrap_amount, 9, "wrap", item.new_account))
                explained_token_mints.add(WRAPPED_SOL_MINT)
            if rent:
                effects.append(Effect("rent_deposit", "native", rent, 9, "rent", item.new_account, item.source_account))
            consume(dest, item.lamports)
            consume(src, -item.lamports)
        else:
            effects.append(Effect("rent_deposit", "native", item.lamports, 9, "rent", item.new_account, item.source_account))
            consume(dest, item.lamports)
            consume(src, -item.lamports)

    for item in instructions:
        if item.ix_type != "transfer" or item.program_id != SYSTEM_PROGRAM:
            continue
        dest_key = item.destination or ""
        if dest_key not in sync_accounts:
            continue
        if item.lamports is None:
            reasons.append("wsol_amount_mismatch")
            continue
        increase = _token_increase(deltas, dest_key)
        if increase is None or increase != item.lamports:
            reasons.append("wsol_amount_mismatch")
            continue
        if not any(effect.kind == "wrap" and effect.account == dest_key and effect.raw_amount == item.lamports for effect in effects):
            effects.append(Effect("wrap", WRAPPED_SOL_MINT, item.lamports, 9, "wrap", dest_key, item.source_account))
        explained_token_mints.add(WRAPPED_SOL_MINT)
        consume(index.get(dest_key), item.lamports)
        consume(index.get(item.source_account or ""), -item.lamports)

    for item in closes:
        account = item.account or ""
        pos = index.get(account)
        if pos is None:
            reasons.append("unexplained_lamports")
            continue
        minted = _mint_at(deltas, account)
        pre_token = _pre_token(deltas, account)
        whole = lamports_pre[pos] - lamports_post[pos]
        if minted == WRAPPED_SOL_MINT:
            unwrap_amount = pre_token if pre_token is not None else 0
            rent = lamports_pre[pos] - unwrap_amount - lamports_post[pos]
            if pre_token is None or rent < 0:
                reasons.append("wsol_amount_mismatch")
                continue
            if unwrap_amount:
                effects.append(Effect("unwrap", WRAPPED_SOL_MINT, unwrap_amount, 9, "unwrap", account, item.destination))
                explained_token_mints.add(WRAPPED_SOL_MINT)
            if rent:
                effects.append(Effect("rent_refund", "native", rent, 9, "rent", account, item.destination))
            consume(pos, -whole)
            consume(index.get(item.destination or ""), whole)
        else:
            if whole < 0:
                reasons.append("rent_amount_mismatch")
                continue
            if whole:
                effects.append(Effect("rent_refund", "native", whole, 9, "rent", account, item.destination))
            consume(pos, -whole)
            consume(index.get(item.destination or ""), whole)

    if any(value != 0 for value in residual):
        paired, pair_reason = _pair_native(residual, keys.ordered, wallet, owned)
        effects.extend(paired)
        if pair_reason:
            reasons.append(pair_reason)
    token_effects, token_reasons, gross, net = _token_effects(deltas, instructions, wallet, owned, explained_token_mints)
    effects.extend(token_effects)
    reasons.extend(token_reasons)
    return tuple(effects), tuple(dict.fromkeys(reasons)), gross, net


def _mint_at(deltas: Sequence[TokenDelta], account: str | None) -> str | None:
    if not account:
        return None
    for item in deltas:
        if item.account == account:
            return item.mint
    return None


def _token_increase(deltas: Sequence[TokenDelta], account: str | None) -> int | None:
    if not account:
        return None
    for item in deltas:
        if item.account == account:
            return item.delta
    return None


def _pre_token(deltas: Sequence[TokenDelta], account: str) -> int | None:
    for item in deltas:
        if item.account == account:
            return item.pre_amount
    return None


def _pair_native(
    residual: Sequence[int],
    keys: Sequence[str],
    wallet: str | None,
    owned: set[str],
) -> tuple[list[Effect], str]:
    negatives = [[index, -residual[index]] for index in range(len(residual)) if residual[index] < 0]
    positives = [[index, residual[index]] for index in range(len(residual)) if residual[index] > 0]
    effects: list[Effect] = []
    reason = ""
    for src in negatives:
        for dst in positives:
            if src[1] == 0 or dst[1] == 0:
                continue
            amount = min(src[1], dst[1])
            src[1] -= amount
            dst[1] -= amount
            source = keys[src[0]]
            dest = keys[dst[0]]
            source_owned = source == wallet or source in owned
            dest_owned = dest == wallet or dest in owned
            if source_owned and dest_owned:
                direction = "internal"
            elif source_owned:
                direction = "out"
            elif dest_owned:
                direction = "in"
            else:
                direction = "external"
            effects.append(Effect("native_transfer", "native", amount, 9, direction, source, dest))
    if any(item[1] for item in negatives) or any(item[1] for item in positives):
        reason = "unexplained_lamports"
    return effects, reason


def _token_effects(
    deltas: Sequence[TokenDelta],
    instructions: Sequence[InstructionView],
    wallet: str | None,
    owned: set[str],
    skip_mints: set[str],
) -> tuple[list[Effect], list[str], int, int]:
    by_mint: dict[str, list[TokenDelta]] = {}
    for item in deltas:
        if item.mint in skip_mints or item.delta == 0:
            continue
        by_mint.setdefault(item.mint, []).append(item)
    effects: list[Effect] = []
    reasons: list[str] = []
    owned_deltas = [
        item for item in deltas
        if item.delta != 0 and item.mint not in skip_mints and (item.account in owned or item.owner == wallet)
    ]
    net = sum(item.delta for item in owned_deltas)
    abs_sum = sum(abs(item.delta) for item in owned_deltas)
    if net == 0 and abs_sum and all(item.account in owned or item.owner == wallet for item in owned_deltas):
        gross = abs_sum // 2
    else:
        gross = sum(abs(item.delta) for item in owned_deltas)
    for mint, rows in by_mint.items():
        transfers = [
            item for item in instructions
            if item.ix_type in {"transfer", "transferChecked"} and item.program_id in _TOKEN_PROGRAMS and (item.mint in {None, mint})
        ]
        if len(rows) == 1 and not transfers:
            reasons.append("unexplained_token_delta")
            continue
        source_row = None
        dest_row = None
        for item in transfers:
            if item.mint not in {None, mint}:
                continue
            matched_source = next((row for row in rows if row.account == item.source_account), None)
            matched_dest = next((row for row in rows if row.account == item.destination), None)
            if matched_source is not None:
                source_row = matched_source
            if matched_dest is not None:
                dest_row = matched_dest
        if source_row is None:
            decreases = [row for row in rows if row.delta < 0]
            source_row = decreases[0] if len(decreases) == 1 else None
        if dest_row is None:
            increases = [row for row in rows if row.delta > 0 and row is not source_row]
            dest_row = increases[0] if increases else None
        if source_row is None and dest_row is None:
            reasons.append("unexplained_token_delta")
            continue
        sent = -source_row.delta if source_row is not None and source_row.delta < 0 else None
        received = dest_row.delta if dest_row is not None and dest_row.delta > 0 else None
        if sent is None and received is not None:
            sent = received
        if received is None and sent is not None:
            received = sent
        if sent is None or received is None:
            reasons.append("unexplained_token_delta")
            continue
        fee = sent - received
        other = sum(row.delta for row in rows if row not in {source_row, dest_row} and row.delta > 0)
        if fee < 0 or (fee and other not in {0, fee}):
            reasons.append("token_fee_unbalanced")
            fee = max(fee, 0)
        decimals = (source_row or dest_row).decimals
        user_out = source_row is not None and (source_row.account in owned or source_row.owner == wallet)
        user_in = dest_row is not None and (dest_row.account in owned or dest_row.owner == wallet)
        if user_out and user_in and sent == received:
            effects.append(Effect(
                "token_transfer", mint, sent, decimals, "internal",
                None if source_row is None else source_row.account,
                None if dest_row is None else dest_row.account,
                sent, received, fee or None,
            ))
        else:
            if user_out and source_row is not None:
                effects.append(Effect(
                    "token_transfer", mint, sent, decimals, "out", source_row.account,
                    None if dest_row is None else dest_row.account, sent, received, fee or None,
                ))
            if user_in and dest_row is not None and not (user_out and sent == received):
                effects.append(Effect(
                    "token_transfer", mint, received, decimals, "in", dest_row.account,
                    None if source_row is None else source_row.account, sent, received, fee or None,
                ))
            if not user_out and not user_in:
                anchor = source_row or dest_row
                effects.append(Effect(
                    "token_transfer", mint, sent, decimals, "external",
                    None if anchor is None else anchor.account, None, sent, received, fee or None,
                ))
        if fee:
            effects.append(Effect(
                "token_transfer_fee", mint, fee, decimals, "fee",
                None if source_row is None else source_row.account,
                None if dest_row is None else dest_row.account,
                sent, received, fee,
            ))
    return effects, reasons, gross, net


def _chain_effects(effects: Sequence[Effect]) -> list[dict[str, Any]]:
    """Economic events only. Rent is not income. Wrap is not also a SOL transfer."""
    rows: list[dict[str, Any]] = []
    for index, effect in enumerate(effects):
        if effect.kind == "fee":
            continue
        if effect.kind in {"rent_deposit", "rent_refund", "token_transfer_fee"}:
            continue
        if effect.direction in {"internal", "external", "rent"}:
            continue
        if effect.kind == "wrap":
            kind = "asset_in"
        elif effect.kind == "unwrap":
            kind = "asset_out"
        elif effect.direction == "in":
            kind = "asset_in"
        elif effect.direction == "out":
            kind = "asset_out"
        else:
            continue
        if effect.raw_amount <= 0:
            continue
        rows.append({
            "asset": effect.asset,
            "kind": kind,
            "raw_amount": effect.raw_amount,
            "sub_index": str(index),
        })
    return rows


def _events(
    entry: Mapping[str, Any],
    signature: str,
    slot: int | None,
    finality: str,
    status: str,
    fee: int | None,
    asset_effects: Sequence[Mapping[str, Any]],
    tenant_id: str,
    wallet_id: str,
    observed_at: str,
    meta: Mapping[str, Any] | None,
) -> tuple[ChainEvent, ...]:
    if not tenant_id or not wallet_id or not observed_at or not signature:
        return ()
    err: Any
    if status == "failed":
        err = {"failed": True} if not isinstance(meta, Mapping) else meta.get("err")
        if err is None:
            err = {"failed": True}
    elif status == "success":
        err = None
    else:
        return ()
    body: dict[str, Any] = {
        "block_number": None if slot is None else str(slot),
        "chain_uid": str(entry.get("identity", {}).get("expected_value") or entry.get("network")),
        "finality": finality,
        "meta": {"err": err},
        "network": entry.get("network"),
        "observed_at": observed_at,
        "source_ids": ("solana-rpc",),
        "tenant_id": tenant_id,
        "tx_id": signature,
        "wallet_id": wallet_id,
    }
    if fee is not None and fee > 0:
        body["meta"]["fee"] = fee
    if status == "success" and asset_effects:
        body["effects"] = list(asset_effects)
    try:
        return tuple(effects_from_tx(body))
    except (TypeError, ValueError):
        return ()


def transaction_observation(parsed: ParsedTransaction, *, cursor: str | None, source: str = "solana-rpc") -> Any:
    category = "ok" if parsed.status == "success" and parsed.complete else "partial" if parsed.status == "success" else "error" if parsed.status == "failed" else "partial"
    if parsed.status == "unknown":
        category = "error"
    return make_observation(
        category=category,
        pagination_complete=parsed.complete and parsed.status == "success",
        gap_detection_complete=parsed.complete and parsed.status == "success",
        gaps=parsed.reasons if parsed.status != "success" or not parsed.complete else (),
        cursor=cursor,
        declared="solana_transaction",
        reason_codes=parsed.reasons,
        slot=parsed.slot,
        finality=parsed.finality,
        source=source,
    )
